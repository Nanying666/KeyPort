"""模型原生工具调用标记（DSML）的还原层。

## 背景

本网关背后的上游由 DeepSeek 系模型出结果，它的工具调用原生语法不是 OpenAI 的
JSON `tool_calls`，而是一段带**全角竖线**分隔符的标记文本：

    <｜｜DSML｜｜ calls>
    <｜｜DSML｜｜ invoke name="exec_command">
    <｜｜DSML｜｜ parameter name="cmd" string="true">ls -la<｜｜DSML｜｜ parameter>
    <｜｜DSML｜｜ invoke>
    <｜｜DSML｜｜ calls>

（上面的 `｜` 是 U+FF5C 全角竖线，两个一组构成分隔符。）

上游**只在请求声明了 `tools` 时**才把这段标记解析成结构化 `tool_calls`。一旦某次
请求没带上 `tools`（客户端一次性探测、auto-review 复核回合、或任何 `tools` 在中转
环节丢失的请求），模型仍然想调工具，于是标记以**纯文本**落进 `content`。

下游没有任何一层认识它：Codex 只执行 Responses API 的结构化 `function_call` 条目，
于是把标记当普通助手文本写进会话历史，回合直接结束、工具一次都没跑 —— 用户看到的
就是历史里躺着一大段莫名其妙的标记，任务没有推进。

## 判定分两级

「客户端声明了哪些工具」这件事在链路上并不可靠 —— 最常见的泄漏场景恰恰是 `tools`
声明丢了，此时请求体里根本没有工具名单：

- **严格判定**（名单非空）：块内每个 invoke 的工具名都必须在名单里。
- **弱判定**（名单为空）：只要求块结构完美闭合、块内不夹带正文，且每个工具名形如
  合法标识符（`_TOOL_NAME_RE`）。模型在正文里「讨论」这段语法时写出的多是
  `<tool_name>`、`NAME` 之类占位符，会被形态检查挡住。

两级共同前提（任一不满足即原文透出）：

- 标记块完整闭合，块内除 invoke/parameter 与空白外没有夹带其他正文；
- 本回合上游没有给出任何结构化 `tool_calls`（不覆盖真实调用）；
- 非流式路径额外要求 `finish_reason == "stop"`（`length` 表示正文被截断）。

**修复层绝不吞字节**：所有「拿不准」的分支都走回吐路径。不变量是
`所有 Feed 入参拼接 == 所有返回正文 + Flush() + 被成功转换的标记块原文`。
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

# 标记分隔符：两个全角竖线 U+FF5C
_PIPE = "\uff5c\uff5c"

# 块与内层元素的起止标记。
# 结束标记一律比同名的开始标记多一个 '/'，因此下面的 invoke/parameter 正则里
# 「< 开头」的匹配不可能落到结束标记上。
_MARKUP_OPEN = f"<{_PIPE}DSML{_PIPE} calls>"
_MARKUP_CLOSE = f"</{_PIPE}DSML{_PIPE} calls>"
_INVOKE_CLOSE = f"</{_PIPE}DSML{_PIPE} invoke>"
_PARAM_CLOSE = f"</{_PIPE}DSML{_PIPE} parameter>"

_TAG_PREFIX = re.escape(f"<{_PIPE}DSML{_PIPE}")

_INVOKE_RE = re.compile(_TAG_PREFIX + r'\s+invoke\s+name="([^"]*)"\s*>')
_PARAM_RE = re.compile(
    _TAG_PREFIX + r'\s+parameter\s+name="([^"]*)"(?:\s+string="(true|false)")?\s*>'
)

# 工具名的合理形态：各家 SDK 的工具名一律是这种标识符。
# 弱判定（客户端没给 tools 名单）时用它兜底。
_TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:-]{0,63}$")

# 单次标记块的累积上限。正常块从几百字节（一条 ls）到几十 KB（一段 apply_patch）。
# 超过上限仍未闭合 → 判定「这不是标记」（例如模型在正文里讨论这段语法），
# 立即原文回吐并退出块态，避免无限缓冲。
MAX_BLOCK_BYTES = 256 * 1024


@dataclass
class MarkupCall:
    """一次从正文里还原出来的工具调用。"""

    name: str
    arguments: str  # JSON 对象文本


@dataclass
class MarkupRepair:
    """流式与非流式共用的标记修复状态机（一条响应流一个实例）。

    `allowed` 为请求体里提取到的工具名集合（可能为空 —— tools 声明在中转环节丢失
    是常态）。`allow_unknown` 为真时启用弱判定：名单为空也尝试修复，改由
    「块结构完美 + 工具名形如标识符」把关。

    两者都为空/假 → 整体禁用，`feed()` 恒等透传。
    """

    allowed: set[str] | None = None
    allow_unknown: bool = False
    _hold: str = ""
    _in_block: bool = False
    seen: int = 0
    rejected: int = 0
    converted: int = 0

    def enabled(self) -> bool:
        return bool(self.allowed) or self.allow_unknown

    # ---- 流式 ----

    def feed(self, text: str) -> tuple[str, list[MarkupCall]]:
        """吃进一段正文增量，返回 (应当透出的正文, 还原出来的调用)。

        调用方必须把返回正文按原顺序写出，并把返回调用按出现顺序转成 tool_call。
        """
        if not self.enabled():
            return text, []
        s = self._hold + text
        self._hold = ""
        emit: list[str] = []
        calls: list[MarkupCall] = []
        while True:
            if not self._in_block:
                i = _start_candidate(s)
                if i < 0:
                    emit.append(s)
                    break
                if len(s) - i < len(_MARKUP_OPEN):
                    # 起点被截断在增量边界（上游把标记切成了两片）：留到下一片判定
                    emit.append(s[:i])
                    self._hold = s[i:]
                    break
                emit.append(s[:i])
                s = s[i + len(_MARKUP_OPEN):]
                self._in_block = True
                continue
            k = s.find(_MARKUP_CLOSE)
            if k < 0:
                if len(s) > MAX_BLOCK_BYTES:
                    # 超长未闭合：不是标记，原文回吐并退出块态
                    emit.append(_MARKUP_OPEN)
                    emit.append(s)
                    self._in_block = False
                    break
                self._hold = s
                break
            inner = s[:k]
            s = s[k + len(_MARKUP_CLOSE):]
            self._in_block = False
            self.seen += 1
            got = parse_markup_block(inner, self.allowed)
            if got is not None:
                calls.extend(got)
                self.converted += len(got)
                continue
            # 块内不是纯调用（夹带正文 / 工具名不合规 / 参数畸形）：原文回吐，绝不吞字节
            self.rejected += 1
            emit.append(_MARKUP_OPEN)
            emit.append(inner)
            emit.append(_MARKUP_CLOSE)
        return "".join(emit), calls

    def flush(self) -> str:
        """响应流结束时调用：把仍未判定的尾部缓冲原文交还。

        未闭合的块连同起始标记一起回吐 —— 上游发了什么就透出什么。
        """
        if not self.enabled():
            return ""
        out = (_MARKUP_OPEN + self._hold) if self._in_block else self._hold
        self._hold = ""
        self._in_block = False
        return out


def _start_candidate(s: str) -> int:
    """返回 s 中最早的「可能是 _MARKUP_OPEN 起点」的下标；没有返回 -1。

    该位置之后要么正好是完整的起始标记，要么是它的一个前缀（被增量边界截断）。
    """
    for i, ch in enumerate(s):
        if ch != "<":
            continue
        rest = s[i:]
        if len(rest) >= len(_MARKUP_OPEN):
            if rest[:len(_MARKUP_OPEN)] == _MARKUP_OPEN:
                return i
            continue
        if _MARKUP_OPEN.startswith(rest):
            return i
    return -1


def parse_markup_block(inner: str, allowed: set[str] | None) -> list[MarkupCall] | None:
    """解析 calls 块内部；不合法返回 None（由调用方原文回吐）。

    成功要求：块内除 invoke 与空白外没有别的字节；每个 invoke 都闭合；每个 invoke
    的参数列表合法；工具名判定通过（名单非空走严格比对，名单为空走形态检查）。
    """
    strict = bool(allowed)
    out: list[MarkupCall] = []
    pos = 0
    while True:
        loc = _INVOKE_RE.search(inner, pos)
        if loc is None:
            break
        if inner[pos:loc.start()].strip():
            return None  # invoke 之前夹带了正文
        name = loc.group(1)
        body_start = loc.end()
        end = inner.find(_INVOKE_CLOSE, body_start)
        if end < 0:
            return None  # invoke 未闭合
        body = inner[body_start:end]
        pos = end + len(_INVOKE_CLOSE)
        args = _parse_params(body)
        if args is None:
            return None
        if strict:
            if name not in allowed:  # type: ignore[operator]
                return None  # 不在客户端声明的 tools 里 → 不认，原文透出
        elif not _TOOL_NAME_RE.match(name):
            return None  # 名单缺失时的弱判定：名字不像工具名 → 不认，原文透出
        out.append(MarkupCall(name=name, arguments=args))
    if not out:
        return None
    if inner[pos:].strip():
        return None  # 最后一个 invoke 之后夹带了正文
    return out


def _parse_params(body: str) -> str | None:
    """解析一个 invoke 内部的 parameter 列表，产出 JSON 对象文本。

    `string="true"` 的取值按原文字符串处理（走 JSON 转义，命令里的换行/引号/反斜杠
    原样保留）；其余先按 JSON 解析，失败则退化为字符串 —— 参数值永远不丢，最坏情况
    是类型不如模型本意。无参数的工具产出 `{}`。
    """
    args: dict[str, Any] = {}
    pos = 0
    while True:
        loc = _PARAM_RE.search(body, pos)
        if loc is None:
            break
        if body[pos:loc.start()].strip():
            return None
        name = loc.group(1)
        is_string = loc.group(2) == "true"
        val_start = loc.end()
        end = body.find(_PARAM_CLOSE, val_start)
        if end < 0:
            return None
        raw = body[val_start:end]
        pos = end + len(_PARAM_CLOSE)
        if is_string:
            args[name] = raw
            continue
        try:
            args[name] = json.loads(raw.strip())
        except (json.JSONDecodeError, ValueError):
            args[name] = raw
    if body[pos:].strip():
        return None
    return json.dumps(args, ensure_ascii=False)


def tool_name_allowlist(body: bytes | str | dict[str, Any] | None) -> set[str]:
    """从客户端请求体里收集「可用于判定工具调用」的工具名集合。

    两个来源都要，因为二者会分别缺失：

    1. **本请求声明的工具**：`tools[].function.name`、Responses 风格裸
       `tools[].name`、旧版 `functions[].name`。`tools` 声明在中转环节丢失是常态。
    2. **会话历史里出现过的工具**：`messages[].tool_calls[].function.name` 与旧版
       `messages[].function_call.name`。多轮会话里这份名单几乎总在，且天然可信
       —— 它是客户端自己写进历史的工具名。

    解析失败按空处理（不编造名单）。
    """
    if body is None:
        return set()
    if isinstance(body, (bytes, str)):
        try:
            req = json.loads(body)
        except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
            return set()
    else:
        req = body
    if not isinstance(req, dict):
        return set()

    names: set[str] = set()

    def _add(value: Any) -> None:
        if isinstance(value, str) and value:
            names.add(value)

    for item in req.get("tools") or []:
        if not isinstance(item, dict):
            continue
        fn = item.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            _add(fn["name"])
            continue
        _add(item.get("name"))
    for item in req.get("functions") or []:
        if isinstance(item, dict):
            _add(item.get("name"))
    for msg in req.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function")
                if isinstance(fn, dict):
                    _add(fn.get("name"))
        fc = msg.get("function_call")
        if isinstance(fc, dict):
            _add(fc.get("name"))
    return names


# ---- 组装成下游协议的结构 ----


def _call_id(idx: int) -> str:
    """生成形态接近 OpenAI 的 tool_call id（序号参与，保证同响应内互不相同）。"""
    return f"call_{int(time.time() * 1_000_000_000)}_{idx}"


def to_message_tool_calls(calls: list[MarkupCall], base: int = 0) -> list[dict[str, Any]]:
    """转成**非流式** `message.tool_calls`。

    非流式形态不带 `index` —— index 是流式 delta 的分片归属标记，写进完整消息里
    属于多余字段，严格校验的客户端会挑刺。
    """
    return [
        {
            "id": _call_id(base + i),
            "type": "function",
            "function": {"name": c.name, "arguments": c.arguments},
        }
        for i, c in enumerate(calls)
    ]


def to_delta_tool_calls(calls: list[MarkupCall], base: int = 0) -> list[dict[str, Any]]:
    """转成**流式** `delta.tool_calls`（含 index，供下游按分片合并）。"""
    out = []
    for i, c in enumerate(calls):
        out.append({
            "index": base + i,
            "id": _call_id(base + i),
            "type": "function",
            "function": {"name": c.name, "arguments": c.arguments},
        })
    return out


# ---- 非流式：直接修补聚合响应 ----

def repair_aggregated_response(
    resp: dict[str, Any],
    allowed: set[str] | None = None,
    allow_unknown: bool = True,
) -> MarkupRepair:
    """在非流式聚合响应里把正文中的标记还原为 `tool_calls`。

    只在「该 choice 没有结构化 tool_calls」且「finish_reason == stop」时生效：
    前者保证不覆盖真实调用，后者保证正文没有被 max_tokens 截断。

    返回本次使用的修复器，调用方据 `converted` / `seen` / `rejected` 记观测。
    """
    repair = MarkupRepair(allowed=allowed, allow_unknown=allow_unknown)
    if not repair.enabled():
        return repair
    choices = resp.get("choices")
    if not isinstance(choices, list):
        return repair
    for choice in choices:
        if not isinstance(choice, dict):
            continue
        msg = choice.get("message")
        if not isinstance(msg, dict):
            continue
        existing = msg.get("tool_calls")
        if isinstance(existing, list) and existing:
            continue  # 已有真实结构化调用，绝不覆盖
        if choice.get("finish_reason") != "stop":
            continue
        text = msg.get("content")
        if not isinstance(text, str) or not text or _PIPE not in text:
            continue
        emit, calls = repair.feed(text)
        emit += repair.flush()
        if not calls:
            continue
        msg["content"] = emit
        msg["tool_calls"] = to_message_tool_calls(calls)
        choice["finish_reason"] = "tool_calls"
    return repair
