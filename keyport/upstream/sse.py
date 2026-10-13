"""SSE 引擎：上游流解析、非流式聚合、以及跨协议流式转换。

上游固定返回 OpenAI chat SSE（`data: {...}` + `data: [DONE]`）。本模块负责：
  - `iter_sse_json`：把上游字节流拆成 JSON chunk
  - `aggregate`：把 chunk 流聚合成非流式 OpenAI 响应
  - `OpenAIToAnthropic`：OpenAI chunk → Anthropic Messages 事件
  - `OpenAIToResponses`：OpenAI chunk → OpenAI Responses 事件
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Iterable

DONE = "[DONE]"


async def iter_sse_json(lines: AsyncIterator[bytes | str]) -> AsyncIterator[dict[str, Any]]:
    """把上游 SSE 行流拆成 JSON chunk（跳过注释/空行/[DONE]）。"""
    async for raw in lines:
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8", errors="replace")
            except Exception:  # pragma: no cover - decode 已容错
                continue
        else:
            line = raw
        line = line.strip("\r\n")
        if not line or line.startswith(":"):
            continue
        if line.startswith("data:"):
            payload = line[5:].strip()
            if payload == DONE:
                return
            if not payload:
                continue
            try:
                chunk = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if isinstance(chunk, dict):
                yield chunk


def sse_line(data: Any, event: str | None = None) -> bytes:
    """构造一条 SSE 帧（Anthropic 需要 event 行，OpenAI 只要 data 行）。"""
    body = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    if event:
        return f"event: {event}\ndata: {body}\n\n".encode("utf-8")
    return f"data: {body}\n\n".encode("utf-8")


# ---- 聚合 ----

@dataclass
class Aggregated:
    """非流式聚合结果。"""

    id: str = ""
    model: str = ""
    created: int = 0
    content: str = ""
    reasoning: str = ""
    role: str = "assistant"
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    finish_reason: str = "stop"
    usage: dict[str, Any] | None = None
    saw_done: bool = False
    got_content: bool = False
    truncation_reason: str = ""

    @property
    def truncated(self) -> bool:
        """是否因长度截断或流中断导致输出不完整。"""
        if self.finish_reason == "length":
            return True
        if not self.saw_done and self.got_content:
            return True
        return False


def merge_tool_call_delta(merged: dict[int, dict[str, Any]], delta: dict[str, Any]) -> None:
    """按 index 合并流式 tool_call 片段（首片带 id/type/name，后续只带 arguments）。"""
    idx = delta.get("index")
    if not isinstance(idx, int):
        idx = len(merged)
    slot = merged.setdefault(idx, {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
    if delta.get("id"):
        slot["id"] = str(delta["id"])
    if delta.get("type"):
        slot["type"] = str(delta["type"])
    fn = delta.get("function")
    if isinstance(fn, dict):
        if fn.get("name"):
            slot["function"]["name"] = str(fn["name"])
        if isinstance(fn.get("arguments"), str):
            slot["function"]["arguments"] += fn["arguments"]


def aggregate(chunks: Iterable[dict[str, Any]]) -> Aggregated:
    """把 OpenAI chunk 序列聚合成完整响应。"""
    agg = Aggregated()
    merged: dict[int, dict[str, Any]] = {}
    for chunk in chunks:
        if not agg.id and chunk.get("id"):
            agg.id = str(chunk["id"])
        if not agg.model and chunk.get("model"):
            agg.model = str(chunk["model"])
        if not agg.created and chunk.get("created"):
            agg.created = int(chunk["created"])
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            agg.usage = usage
        choices = chunk.get("choices")
        if not isinstance(choices, list):
            continue
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            fr = choice.get("finish_reason")
            if isinstance(fr, str) and fr:
                agg.finish_reason = fr
            delta = choice.get("delta")
            if isinstance(delta, dict):
                if isinstance(delta.get("role"), str) and delta["role"]:
                    agg.role = delta["role"]
                if isinstance(delta.get("content"), str) and delta["content"]:
                    agg.content += delta["content"]
                    agg.got_content = True
                if isinstance(delta.get("reasoning_content"), str) and delta["reasoning_content"]:
                    agg.reasoning += delta["reasoning_content"]
                for tc in delta.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        merge_tool_call_delta(merged, tc)
            msg = choice.get("message")
            if isinstance(msg, dict) and not agg.got_content:
                if isinstance(msg.get("content"), str) and msg["content"]:
                    agg.content += msg["content"]
                    agg.got_content = True
                if isinstance(msg.get("reasoning_content"), str):
                    agg.reasoning += msg["reasoning_content"]
                for tc in msg.get("tool_calls") or []:
                    if isinstance(tc, dict):
                        merge_tool_call_delta(merged, tc)
    agg.tool_calls = [merged[k] for k in sorted(merged)]
    if agg.finish_reason == "length":
        agg.truncation_reason = "max_tokens"
    elif not agg.saw_done and agg.got_content:
        agg.truncation_reason = "upstream_eof"
    return agg


def ensure_usage_total(usage: dict[str, Any] | None) -> dict[str, Any]:
    """补齐 total_tokens（上游有时只发 prompt/completion）。"""
    if not isinstance(usage, dict):
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    out = dict(usage)
    if "total_tokens" not in out:
        p = int(out.get("prompt_tokens") or 0)
        c = int(out.get("completion_tokens") or 0)
        out["total_tokens"] = p + c
    return out
