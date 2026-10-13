"""出站请求体改写管线（OpenAI chat 协议）。

改写顺序（与上游兼容性一一对应）：
  1. 强制 stream=true（上游拒绝非流式）
  2. max_completion_tokens → max_tokens 翻译
  3. 补 stream_options.include_usage
  4. tool_choice 归一化（上游该字段是 string）
  5. role 归一化（developer → system）
  6. tool 配对重排 + 孤儿清理（让请求能过）
  7. DeepSeek 思维链注入 + reasoning_effort 档位降级 + reasoning_content 回填
  8. 指纹脱敏（可关闭）
  9. 系统提示词替换（custom 模式）
"""

from __future__ import annotations

import json
from typing import Any

from .prompt import DEFAULT_PROMPT, MODE_CUSTOM, apply_system_prompt
from .thinking import (
    backfill_reasoning_content,
    inject_thinking,
    normalize_reasoning_effort,
)

# ---- 指纹脱敏黑名单（出站请求体字段清洗）----

FINGERPRINT_KEYS = {
    "client_version", "clientVersion", "ide_version", "ideVersion",
    "machine_id", "machineId", "device_id", "deviceId",
    "session_id", "sessionId", "user_agent", "userAgent",
    "os_version", "osVersion", "hostname", "username",
    "phone", "email",
}


def parse_body(body: bytes) -> dict[str, Any] | None:
    """解析请求体为 dict；非 JSON 对象返回 None。"""
    try:
        obj = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return obj if isinstance(obj, dict) else None


def translate_max_completion_tokens(obj: dict[str, Any]) -> None:
    """别名翻译：显式 max_tokens 优先；别名 0/null/负数/非数值不翻译。"""
    if "max_completion_tokens" not in obj:
        return
    alias = obj.get("max_completion_tokens")
    is_num = isinstance(alias, (int, float)) and not isinstance(alias, bool)
    if "max_tokens" not in obj and is_num and alias > 0:
        obj["max_tokens"] = int(alias)
    del obj["max_completion_tokens"]


def normalize_tool_choice(obj: dict[str, Any]) -> None:
    """按上游 Go struct（string 类型）改写 OpenAI tool_choice。"""

    def suppress() -> None:
        obj.pop("tools", None)
        obj.pop("functions", None)

    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        if tc.strip().lower() == "none":
            obj.pop("tool_choice", None)
            suppress()
        return
    if isinstance(tc, dict):
        typ = str(tc.get("type", "")).strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            suppress()
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = ""
            fn = tc.get("function")
            if isinstance(fn, dict):
                name = str(fn.get("name", ""))
            if not name:
                name = str(tc.get("name", ""))
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
        return
    obj.pop("tool_choice", None)


def normalize_roles(obj: dict[str, Any]) -> None:
    """messages 里的 developer 角色归一为 system（上游 role 白名单不含 developer）。"""
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return
    for msg in msgs:
        if isinstance(msg, dict) and str(msg.get("role", "")).strip().lower() == "developer":
            msg["role"] = "system"


def ensure_console_system(obj: dict[str, Any], default_text: str) -> None:
    """global 域兜底 system 注入：首条非 system 时在最前补一条，防上游 11-128。"""
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return
    first = msgs[0]
    if isinstance(first, dict) and str(first.get("role", "")).strip().lower() == "system":
        return
    obj["messages"] = [{"role": "system", "content": default_text}, *msgs]


# ---- tool 配对 ----

def _has_tool_calls(msg: dict[str, Any]) -> bool:
    tcs = msg.get("tool_calls")
    return isinstance(tcs, list) and len(tcs) > 0


def _tool_call_ids(msg: dict[str, Any]) -> set[str]:
    ids: set[str] = set()
    for tc in msg.get("tool_calls") or []:
        if isinstance(tc, dict) and tc.get("id"):
            ids.add(str(tc["id"]))
    fc = msg.get("function_call")
    if isinstance(fc, dict) and fc.get("id"):
        ids.add(str(fc["id"]))
    return ids


def cleanup_orphan_tool_calls(msgs: list[Any]) -> tuple[list[Any], bool]:
    """剔除不完整配对的 tool_calls / tool 结果。

    上游对不完整配对（有 tool_calls 无对应结果，或有结果无对应调用）会对之后
    每条消息返 400，必须先行剔除。
    """
    declared: set[str] = set()
    answered: set[str] = set()
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", ""))
        if role == "assistant":
            declared |= _tool_call_ids(msg)
        elif role == "tool" and msg.get("tool_call_id"):
            answered.add(str(msg["tool_call_id"]))

    valid = declared & answered
    changed = False
    out: list[Any] = []
    for msg in msgs:
        if not isinstance(msg, dict):
            out.append(msg)
            continue
        role = str(msg.get("role", ""))
        if role == "assistant" and _has_tool_calls(msg):
            kept = [tc for tc in msg["tool_calls"]
                    if isinstance(tc, dict) and str(tc.get("id", "")) in valid]
            if len(kept) != len(msg["tool_calls"]):
                changed = True
            if not kept:
                new_msg = {k: v for k, v in msg.items() if k != "tool_calls"}
                # 无 tool_calls 且无正文的 assistant 消息是纯噪声，直接丢弃
                if not new_msg.get("content"):
                    continue
                out.append(new_msg)
                continue
            out.append({**msg, "tool_calls": kept} if len(kept) != len(msg["tool_calls"]) else msg)
            continue
        if role == "tool" and msg.get("tool_call_id"):
            if str(msg["tool_call_id"]) not in valid:
                changed = True
                continue
        out.append(msg)
    return out, changed


def repack_tool_result_blocks(msgs: list[Any]) -> tuple[list[Any], bool]:
    """把插在 tool 结果中间的非 tool 消息挪到 tool 结果块之后。

    Codex 的 image_resize_notice 等会在 tool_calls 与对应 tool 结果之间插入
    非 tool 消息，导致配对断裂。
    """
    changed = False
    out: list[Any] = []
    i = 0
    n = len(msgs)
    while i < n:
        msg = msgs[i]
        out.append(msg)
        if not (isinstance(msg, dict) and str(msg.get("role", "")) == "assistant" and _has_tool_calls(msg)):
            i += 1
            continue
        pending = _tool_call_ids(msg)
        if not pending:
            i += 1
            continue
        j = i + 1
        deferred: list[Any] = []
        while j < n and pending:
            cur = msgs[j]
            if isinstance(cur, dict) and str(cur.get("role", "")) == "tool":
                out.append(cur)
                pending.discard(str(cur.get("tool_call_id", "")))
                j += 1
                continue
            deferred.append(cur)
            j += 1
            if len(deferred) > 32:
                break
        if deferred:
            changed = True
            out.extend(deferred)
        i = j
    return out, changed


def sanitize_fingerprints(obj: dict[str, Any]) -> None:
    """递归清洗请求体里的指纹字段（就地修改）。"""
    stack: list[Any] = [obj]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            for key in list(cur.keys()):
                if key in FINGERPRINT_KEYS:
                    cur.pop(key, None)
                    continue
                stack.append(cur[key])
        elif isinstance(cur, list):
            stack.extend(cur)


# ---- 长上下文压缩投影 ----

def estimate_tokens(obj: dict[str, Any]) -> int:
    """粗略 token 估算（字符数 / 3），仅用于决定是否压缩，不做计量。"""
    import json as _json

    total = 0
    for msg in obj.get("messages") or []:
        if isinstance(msg, dict):
            content = msg.get("content")
            if isinstance(content, str):
                total += len(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and isinstance(part.get("text"), str):
                        total += len(part["text"])
            total += len(_json.dumps(msg.get("tool_calls") or [], ensure_ascii=False))
    return total // 3


def compress_long_context(obj: dict[str, Any], max_tokens: int, keep_recent: int = 12) -> bool:
    """后端友好的压缩投影：超限时保留 system + 最近若干轮，中间段落折叠为摘要占位。

    返回是否发生了压缩。折叠而非丢弃可避免上游因上下文缺失产生幻觉。
    """
    if max_tokens <= 0 or estimate_tokens(obj) <= max_tokens:
        return False
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or len(msgs) <= keep_recent + 2:
        return False

    system_msgs = [m for m in msgs if isinstance(m, dict) and str(m.get("role", "")) == "system"]
    non_system = [m for m in msgs if not (isinstance(m, dict) and str(m.get("role", "")) == "system")]
    if len(non_system) <= keep_recent:
        return False

    head_keep = 2
    head = non_system[:head_keep]
    tail = non_system[-keep_recent:]
    dropped = non_system[head_keep:-keep_recent]
    if not dropped:
        return False

    notice = {
        "role": "user",
        "content": (
            f"[历史上下文已压缩：{len(dropped)} 条较早消息因长度限制被折叠。"
            "如需精确历史请重新提问。]"
        ),
    }
    obj["messages"] = [*system_msgs, *head, notice, *tail]
    return True


# ---- 主管线 ----

def prepare_body(
    body: bytes,
    *,
    sanitize: bool = True,
    prompt_mode: str = "passthrough",
    prompt_text: str = "",
    efforts: list[str] | None = None,
    default_effort: str | None = None,
    is_global: bool = False,
    compress_max_tokens: int = 0,
    force_custom_system: bool = False,
) -> bytes:
    """把下游 OpenAI chat 请求体改写为上游可接受形态。

    任何解析失败都原样返回（坏 body 不在这里二次错误化，由上游报错）。
    """
    obj = parse_body(body)
    if obj is None:
        return body

    obj["stream"] = True
    translate_max_completion_tokens(obj)
    if "stream_options" not in obj:
        obj["stream_options"] = {"include_usage": True}
    normalize_tool_choice(obj)
    normalize_roles(obj)

    msgs = obj.get("messages")
    if isinstance(msgs, list):
        msgs, _ = repack_tool_result_blocks(msgs)
        msgs, _ = cleanup_orphan_tool_calls(msgs)
        obj["messages"] = msgs

    inject_thinking(obj, default_effort)
    normalize_reasoning_effort(obj, efforts)
    backfill_reasoning_content(obj)

    if sanitize:
        sanitize_fingerprints(obj)

    if force_custom_system:
        prompt_mode = MODE_CUSTOM
    apply_system_prompt(obj, prompt_mode, prompt_text)

    if is_global:
        ensure_console_system(obj, prompt_text or DEFAULT_PROMPT)

    if compress_max_tokens > 0:
        compress_long_context(obj, compress_max_tokens)

    return json.dumps(obj, ensure_ascii=False).encode("utf-8")
