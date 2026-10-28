"""系统提示词与脱敏体系。

- custom 模式：用网关自有提示词整体替换客户端 system 消息（从源头消灭
  system 来源的内容误报）；客户端非 system 消息原样保留。
- passthrough 模式：原样透传（网关在收到上游拦截错误时可降级重试为 custom）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

MODE_CUSTOM = "custom"
MODE_PASSTHROUGH = "passthrough"
MODE_APPEND = "append"

DEFAULT_PROMPT = "You are a helpful assistant."

# 降级中性提示词：内容策略**误报**时用，刻意极简、无任何客户端指纹模板句。
#
# 触发场景（对齐 workbuddy2api-panel 的 prompt.Degraded）：passthrough / append
# 模式下客户端（Claude Code / Codex 等 CLI）在 system 里注入固定模板句，上游按
# **逐字精确匹配**审核，合法流量被误杀成「非授权渠道」：
#   HTTP 400 + `Illegal API invocation from an unapproved channel`
# 换这段最小中性提示词重试即可绕开 system 来源的误报；不改变用户指令语义。
DEGRADED_PROMPT = (
    "You are a helpful assistant. Respond in the user's language, "
    "follow the user's instructions, and be direct and concise."
)

# 可降级的提示词模式：custom 已用自有提示词替换 system，不存在 system 指纹误报
DEGRADABLE_MODES = (MODE_PASSTHROUGH, MODE_APPEND)


def load_prompt_text(mode: str, file_path: str, default_text: str) -> str:
    """读取自定义提示词文件；缺失/为空回落内置默认。"""
    if mode not in (MODE_CUSTOM, MODE_APPEND):
        return ""
    if file_path:
        try:
            text = Path(file_path).read_text(encoding="utf-8").strip()
            if text:
                return text
        except OSError:
            pass
    return default_text or DEFAULT_PROMPT


def apply_system_prompt(obj: dict[str, Any], mode: str, prompt_text: str) -> None:
    """按模式改写 messages 中的 system 消息（就地修改）。"""
    if mode not in (MODE_CUSTOM, MODE_APPEND) or not prompt_text:
        return
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return

    if mode == MODE_CUSTOM:
        # 整体替换：丢弃客户端全部 system，保留其余消息顺序，在最前插入自有提示词
        rest = [m for m in msgs if not (isinstance(m, dict) and str(m.get("role", "")) == "system")]
        obj["messages"] = [{"role": "system", "content": prompt_text}, *rest]
        return

    # append：在首条 system 后追加，无 system 则补一条
    for msg in msgs:
        if isinstance(msg, dict) and str(msg.get("role", "")) == "system":
            existing = msg.get("content")
            if isinstance(existing, str):
                msg["content"] = f"{existing}\n\n{prompt_text}"
            elif isinstance(existing, list):
                existing.append({"type": "text", "text": prompt_text})
            else:
                msg["content"] = prompt_text
            return
    obj["messages"] = [{"role": "system", "content": prompt_text}, *msgs]


def has_custom_system(obj: dict[str, Any]) -> bool:
    msgs = obj.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return False
    first = msgs[0]
    return isinstance(first, dict) and str(first.get("role", "")) == "system"


# 上游内容拦截的识别指纹（用于 passthrough → custom 降级重试判定）
#
# 后三条来自上游对「渠道指纹」的审核文案（实测 400）：
#   Illegal API invocation from an unapproved channel / blocked by security policy
# 语义是**内容审核**而非账号鉴权 —— 账号余额/权限/限流状态都健康，判成 AUTH
# 会把健康号直接禁用（见 `base.classify_status` 的判定顺序）。
BLOCK_MARKERS = (
    "内容安全",
    "content_filter",
    "content policy",
    "risk control",
    "sensitive",
    "11-128",
    "11-130",
    "blocked by security policy",
    "unapproved channel",
    "illegal api invocation",
)


def looks_blocked(status: int, body: str) -> bool:
    """判断上游响应是否疑似内容拦截（用于提示词降级重试）。"""
    if status not in (400, 403):
        return False
    text = (body or "").lower()
    return any(marker.lower() in text for marker in BLOCK_MARKERS)
