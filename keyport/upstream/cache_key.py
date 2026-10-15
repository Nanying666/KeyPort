"""会话键提取与上游前缀缓存键注入。

## 为什么需要「内容派生回退」

通用 OpenAI 客户端（Codex / Cherry Studio / dsh 等）请求体里既没有
`conversationId` 也没有 `metadata`，会话键恒为空 → 粘性路由完全不命中 →
同一对话每轮换账号 → 上游前缀缓存全部 miss。本模块让这类客户端**零配置**也能
享受粘性 + 前缀缓存。

派生算法：`sha256(system + "\x00" + 首条 user)[:16]`，前缀 `d-`。
用「system + 首条 user」而不是全部消息：多轮对话里历史消息每轮追加，全量哈希会
每轮变化 → 粘性完全失效；而 system 与首条 user 在一次对话中恒定，足以区分会话。

## 为什么 `prompt_cache_key` 是硬要求（不是优化）

实测同一段 8k token 前缀：**不带** `prompt_cache_key` → `prompt_cache_hit_tokens=0,
credit≈0.34`；**带** → `prompt_cache_hit_tokens=7808, credit≈0.02`（费用降约 17×）。

**按账号隔离是硬约束**：跨账号复用同一 cache key 会让上游命中错账号的前缀缓存，
**泄露对方对话内容**。故 uid 是隔离因子的一部分。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# 派生键前缀：与显式会话 id 的命名空间隔离（客户端恰好传 `d-<hex>` 也不混淆，
# 因为显式 id 优先返回）
DERIVED_PREFIX = "d-"

# cache key 前缀（便于在上游侧辨认来源）
CACHE_KEY_PREFIX = "wb2a"

# 显式会话键的键名（按优先级）。刻意**不含** `user`：
# user 维度粒度过粗，一个用户的全部并行对话会被钉到同一账号，
# 粘性范围远超上游 prompt cache 的对话级边界。
_BODY_KEY_CANDIDATES = ("conversation_id", "conversationId", "session_id", "sessionId")

_HEADER_KEY_CANDIDATES = ("x-conversation-id", "x-session-id", "x-trace-id")


def _text_of(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _part_signature(part: Any) -> str:
    """把一条消息内容压成签名。

    多模态数组里非文本 part 用 `[type:摘要]` 入键 —— 纯图片轮（无 text part）在
    纯文本提取下恒空串，会让首图会话的粘性失效。
    """
    if isinstance(part, str):
        return part
    if not isinstance(part, list):
        return ""
    out: list[str] = []
    for item in part:
        if isinstance(item, str):
            out.append(item)
            continue
        if not isinstance(item, dict):
            continue
        kind = _text_of(item.get("type"))
        text = _text_of(item.get("text"))
        if text:
            out.append(text)
            continue
        if kind:
            digest = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True).encode("utf-8")
            ).hexdigest()[:8]
            out.append(f"[{kind}:{digest}]")
    return "".join(out)


def derive_conversation_key(body: dict[str, Any] | None) -> str:
    """按内容派生会话键；取不到 system 与首条 user 时返回空串（安全降级）。

    空串 = 不做粘性，退回普通轮换。**绝不编造**一个键 —— 那会把无关对话钉在一起。
    """
    if not isinstance(body, dict):
        return ""
    messages = body.get("messages")
    if not isinstance(messages, list):
        return ""

    system_text = ""
    first_user = ""
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = _text_of(msg.get("role"))
        if role == "system" and not system_text:
            system_text = _part_signature(msg.get("content"))
        elif role == "user" and not first_user:
            first_user = _part_signature(msg.get("content"))
        if system_text and first_user:
            break
    if not system_text and not first_user:
        return ""
    raw = f"{system_text}\x00{first_user}".encode()
    return DERIVED_PREFIX + hashlib.sha256(raw).hexdigest()[:16]


def _has_user_id(body: dict[str, Any]) -> bool:
    """body 是否显式带了 user 维度标识。

    带了就**不派生**、回落加权轮换：否则「只发 user_id」的请求会借内容哈希
    重新获得粘性，让上面的契约在回退路径上失效。
    """
    metadata = body.get("metadata")
    if isinstance(metadata, dict) and _text_of(metadata.get("user_id")):
        return True
    return bool(_text_of(body.get("user_id")))


def extract_session_key(body: dict[str, Any] | None, headers: dict[str, str] | None) -> str:
    """提取会话键；取不到返回空串（**绝不失败**）。

    优先级：header 显式 → `metadata.conversation_id` → `metadata.conversationId`
    → 顶层 snake/camel → `prompt_cache_key` → 内容派生 → 空串。
    """
    body = body if isinstance(body, dict) else {}
    headers = headers or {}

    for name in _HEADER_KEY_CANDIDATES:
        value = headers.get(name) or headers.get(name.title())
        if value:
            return str(value)

    metadata = body.get("metadata")
    if isinstance(metadata, dict):
        for key in ("conversation_id", "conversationId"):
            value = _text_of(metadata.get(key))
            if value:
                return value

    for key in _BODY_KEY_CANDIDATES:
        value = _text_of(body.get(key))
        if value:
            return value

    # OpenAI 系前缀缓存键：pi-ai 驱动的客户端把会话 ID 放在这里
    value = _text_of(body.get("prompt_cache_key"))
    if value:
        return value

    if _has_user_id(body):
        return ""
    return derive_conversation_key(body)


def build_prompt_cache_key(uid: str, conversation: str) -> str:
    """构造上游前缀缓存键：`wb2a-<uid8>-<convHex>`。

    `convHex = sha256(uid + "|" + conversation)[:16]` —— **uid 参与哈希是硬要求**，
    跨账号绝不相同（否则会命中别人的前缀缓存并泄露对话）。
    会话源为空时仍由 uid 单独哈希：跨账号不碰撞，同一空会话不复用
    （空会话 = 新会话语义）。
    """
    uid8 = (uid or "")[:8] or "-"
    source = f"{uid or ''}|{conversation or ''}".encode()
    return f"{CACHE_KEY_PREFIX}-{uid8}-{hashlib.sha256(source).hexdigest()[:16]}"


def cache_key_source(body: dict[str, Any] | None, conversation: str = "") -> str:
    """cache key 的会话来源：body 自带的 conversation_id 优先，其次调用方给的键。"""
    if isinstance(body, dict):
        for key in ("conversation_id", "conversationId"):
            value = _text_of(body.get(key))
            if value:
                return value
    return conversation or ""


def inject_prompt_cache_key(
    obj: dict[str, Any] | None, uid: str, conversation: str = "",
) -> bool:
    """给上游请求体注入 `prompt_cache_key`；返回是否真的写入。

    客户端已带该字段时**原值保留、绝不覆盖** —— 它比我们更清楚该复用哪个键。
    body 不是 dict 时原样返回 False（坏 body 不在这里二次错误化）。
    """
    if not isinstance(obj, dict):
        return False
    if _text_of(obj.get("prompt_cache_key")):
        return False
    obj["prompt_cache_key"] = build_prompt_cache_key(
        uid, cache_key_source(obj, conversation)
    )
    return True
