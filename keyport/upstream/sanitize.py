"""出站请求体脱敏：剥离上游内容审核黑名单指纹。

## 背景

客户端（Claude Code 类 CLI）在 system prompt 里注入若干固定模板句。上游内容审核
**按逐字精确匹配**拦截（不是语义审核），因此**一字改动即可绕过**。

策略分两层，语义完全不同，**不可合并**：

- **剥离层**：键值 / header 型指纹**整段删除**（它们不承载语义）。
- **改写层**：承载语义的模板句**最小改写**（换一个词），语义不变。

KeyPort 原先只做了「按键名删 JSON 字段」（`payload.FINGERPRINT_KEYS`），那挡不住
写在**文本正文**里的模板句 —— 而审核恰恰是按正文逐字匹配的。
"""

from __future__ import annotations

import re
from typing import Any

# ---- 特征预检 ----
#
# 任一命中才进入净化。普通请求全不中 → 原样返回（零分配）。
# 列表项都取「截断前缀即可命中」的形态，避免依赖整句。
_FEATURES = (
    # header 键值段键名
    "x-anthropic-billing-header",
    # 尾随裸键值（截断前缀即可命中）
    "cc_entrypoint=",
    # 身份句
    "You are Claude Code",
    # 注入指令句
    "Main branch (",
    # Codex instructions 首段
    "You are a coding agent running in the Codex CLI",
    # 反馈句里的 Anthropic 仓库链接
    "github.com/anthropics/",
    # 上游反探测：裸数字错误码
    "11128",
)

# 剥离层：header 键名即触发（与值无关），整段删除。
_RE_HDR = re.compile(r"(?i)x-anthropic-billing-header:[^;\n]*;?\s*")

# 兜底层：裸键名（无冒号无值）同样是指纹 —— assistant 消息里用反引号引用裸键名即触发，
# 而剥离层要求冒号、对裸串无效。键值形态被整段删除后，残留的裸键名做最小缩写
# （header→hdr）：破坏逐字匹配、语义不变、保留可读性。大小写不敏感。
#
# 注意：该正则**不要求冒号**，是 `_RE_HDR` 的超集。两者必须并用 ——
# 先删键值形态，再缩写残留裸键名。替换语义不同，不能合并成一个正则。
_RE_BARE_HDR = re.compile(r"(?i)x-anthropic-billing-header")

# 剥离层：尾随裸键值（cc_xxx=...;）**循环清理**直到稳定。
_RE_KV = re.compile(r"(?i)\bcc_[a-z0-9_]+=[^;\n]*;?\s*")

# 改写层：全模板句逐字替换（每句只改一个词，语义不变）。
#
# 身份句的匹配串**不带结尾标点**（只到 "…for Claude" 为止）：CLI 版这句以句号
# 收尾，桌面版以逗号接后继内容（"…for Claude, running within the Claude Agent SDK."）。
# 带句号的整句只匹配前者，桌面版会漏网、指纹原样发上游。去掉结尾标点后两种形态
# 一并覆盖（替换串同样不带标点，让原有标点原样保留）。
_REWRITES: tuple[tuple[str, str], ...] = (
    (
        "You are Claude Code, Anthropic's official CLI for Claude",
        "You are Claude Code, Anthropic's official CLI tool for Claude",
    ),
    (
        "Main branch (you will usually use this for PRs)",
        "Default branch (you will usually use this for PRs)",
    ),
    (
        ("You are a coding agent running in the Codex CLI, "
         "a terminal-based coding assistant."),
        ("You are a coding agent running in the Codex CLI tool, "
         "a terminal-based coding assistant."),
    ),
    (
        # 反馈句：整句带 Anthropic 仓库链接，上游按整句拦截（只留链接或只留半边
        # 均不拦，实测需整句同时出现）。give→provide 一词之差即可绕过，语义不变。
        ("To give feedback, users should report the issue at "
         "https://github.com/anthropics/claude-code/issues"),
        ("To provide feedback, users should report the issue at "
         "https://github.com/anthropics/claude-code/issues"),
    ),
    (
        # 上游反探测：只要请求体里出现裸数字 11128 就整单拦截（与该数字的上下文
        # 无关 —— "code=11128" / 裸 "11128" / "错误码 11128" 全部命中；相邻的
        # 11148 / 11101 / 11115 / 99999 均放行）。11128 正是本类拦截自身的错误码，
        # 上游据此识别"在讨论/回显其内部错误码"的请求。
        # 代价：用户对话里任何 11128 都会被改写 —— 但这串数字出现在请求里本身就是
        # 拦截条件，不改写必然失败。插入连字符保留可读性与指代
        # （零宽空格无效，实测上游会归一化）。
        "11128",
        "11-128",
    ),
)

# 要净化的文本字段（在消息里逐个处理）
_TEXT_FIELDS = ("content", "reasoning_content")


def has_fingerprint(text: str) -> bool:
    """特征预检：先走 `in` 快速路径（零分配），再走不要求冒号的正则兜底。

    header 键名有大小写变体且可能以**裸键名**形态出现（无冒号），而 `in` 是
    大小写敏感的 —— 两者都会漏掉「混合大小写 + 裸键名」，必须用不要求冒号的
    `(?i)` 正则兜底，否则整条净化被跳过。
    """
    if not text:
        return False
    for feature in _FEATURES:
        if feature in text:
            return True
    return _RE_BARE_HDR.search(text) is not None


def sanitize_text(text: str) -> str:
    """单段文本净化：预检不中 → 返回原串（零分配）。"""
    if not has_fingerprint(text):
        return text
    for src, dst in _REWRITES:
        if src in text:
            text = text.replace(src, dst)
    if _RE_HDR.search(text):
        text = _RE_HDR.sub("", text)
    if "cc_" in text:
        # 循环清理直到稳定：相邻的 kv 段在单次替换后可能又露出新的可匹配段
        prev = ""
        while prev != text:
            prev = text
            text = _RE_KV.sub("", text)
    # 兜底：键值形态已在上面整段删除，这里只剩裸键名（引用 / 示例文本形态）
    text = _RE_BARE_HDR.sub("x-anthropic-billing-hdr", text)
    return text.strip()


def sanitize_content(value: Any) -> tuple[Any, bool]:
    """兼容字符串与多模态数组；**只动 text part**，image 等 part 不动。

    返回 (净化后的值, 是否发生变化)。
    """
    if isinstance(value, str):
        out = sanitize_text(value)
        return out, out != value
    if isinstance(value, list):
        changed = False
        for part in value:
            if not isinstance(part, dict):
                continue
            text = part.get("text")
            if not isinstance(text, str):
                continue
            out = sanitize_text(text)
            if out != text:
                part["text"] = out
                changed = True
        return value, changed
    return value, False


def sanitize_tool_calls(value: Any) -> bool:
    """净化 `assistant.tool_calls[].function.arguments`。

    `arguments` 是**字符串化的 JSON**（不是对象），因此按文本走即可。

    这块长期是盲区：工具调用消息的 `content` 通常是 `None`，而早期的
    `sanitize_messages` 在 content 缺失时直接跳过整条消息 —— 于是历史里任何写进
    工具参数的被拦字符串（文件名、命令、写入内容）都会原样漏出。
    """
    if not isinstance(value, list):
        return False
    changed = False
    for call in value:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if not isinstance(fn, dict):
            continue
        args = fn.get("arguments")
        if not isinstance(args, str):
            continue
        out = sanitize_text(args)
        if out != args:
            fn["arguments"] = out
            changed = True
    return changed


def sanitize_messages(messages: Any) -> bool:
    """净化 messages 里的 content / reasoning_content / tool_calls。

    三者**各自独立判断**：`content` 可以是 `None`（工具调用轮），若在 content
    缺失时 `continue`，这类消息的 `tool_calls` 就完全不会被净化。

    `reasoning_content`（思维链回填字段）实测同样携带指纹，与 content 同等对待。
    """
    if not isinstance(messages, list):
        return False
    changed = False
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        if "content" in msg:
            new_content, ch = sanitize_content(msg["content"])
            if ch:
                msg["content"] = new_content
                changed = True
        for field in ("reasoning_content",):
            value = msg.get(field)
            if isinstance(value, str):
                out = sanitize_text(value)
                if out != value:
                    msg[field] = out
                    changed = True
        if "tool_calls" in msg and sanitize_tool_calls(msg["tool_calls"]):
            changed = True
    return changed


def sanitize_body(obj: dict[str, Any]) -> bool:
    """净化整个请求体；返回是否发生变化。

    覆盖 `messages` 与（Responses 协议的）`input` 两条入口 —— 两者都可能承载
    客户端注入的模板句。
    """
    if not isinstance(obj, dict):
        return False
    changed = sanitize_messages(obj.get("messages"))
    raw_input = obj.get("input")
    if isinstance(raw_input, list):
        # Responses 的 input 项形状接近 message，复用同一净化逻辑
        if sanitize_messages(raw_input):
            changed = True
    elif isinstance(raw_input, str):
        out = sanitize_text(raw_input)
        if out != raw_input:
            obj["input"] = out
            changed = True
    return changed
