"""OpenAI Responses API ↔ Chat Completions 转换（Codex CLI 接入）。

入站：/v1/responses 请求 → 上游 OpenAI chat 请求
出站：上游 chunk 流 → Responses 事件流（response.created / output_text.delta /
      response.completed 等）
"""

from __future__ import annotations

import time
from typing import Any

STATUS_MAP = {
    "stop": "completed",
    "length": "incomplete",
    "tool_calls": "completed",
    "content_filter": "incomplete",
}


def _input_item_text(item: dict[str, Any]) -> str:
    content = item.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") in ("input_text", "output_text", "text"):
                parts.append(str(part.get("text", "")))
        return "".join(parts)
    return ""


def request_to_openai(body: dict[str, Any], model_override: str | None = None) -> dict[str, Any]:
    """Responses 请求体 → OpenAI chat 请求体。"""
    messages: list[dict[str, Any]] = []

    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append({"role": "system", "content": instructions})

    raw_input = body.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type") or "message"
            if itype == "message":
                role = str(item.get("role", "user"))
                messages.append({"role": role, "content": _input_item_text(item)})
            elif itype == "function_call":
                messages.append({
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{
                        "id": str(item.get("call_id") or item.get("id") or ""),
                        "type": "function",
                        "function": {
                            "name": str(item.get("name", "")),
                            "arguments": str(item.get("arguments", "{}")),
                        },
                    }],
                })
            elif itype == "function_call_output":
                messages.append({
                    "role": "tool",
                    "tool_call_id": str(item.get("call_id") or ""),
                    "content": str(item.get("output", "")),
                })
            elif itype == "reasoning":
                continue  # 思维链不回传

    out: dict[str, Any] = {
        "model": model_override or str(body.get("model", "")),
        "messages": messages,
    }
    if body.get("max_output_tokens") is not None:
        out["max_tokens"] = body["max_output_tokens"]
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    if body.get("stream"):
        out["stream"] = True

    reasoning = body.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning["effort"]

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            if tool.get("type") != "function":
                continue  # 仅桥接函数工具
            converted.append({
                "type": "function",
                "function": {
                    "name": str(tool.get("name", "")),
                    "description": str(tool.get("description", "")),
                    "parameters": tool.get("parameters") or {"type": "object", "properties": {}},
                },
            })
        if converted:
            out["tools"] = converted
            tc = body.get("tool_choice")
            if isinstance(tc, str):
                out["tool_choice"] = tc
            elif isinstance(tc, dict) and tc.get("type") == "function":
                out["tool_choice"] = str(tc.get("name", "")) or "auto"
    return out


def response_from_openai(
    content_text: str,
    tool_calls: list[dict[str, Any]],
    finish_reason: str,
    usage: dict[str, Any] | None,
    model: str,
    response_id: str,
) -> dict[str, Any]:
    """OpenAI 聚合结果 → Responses 响应体。"""
    output: list[dict[str, Any]] = []
    if content_text:
        output.append({
            "id": f"msg_{response_id}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content_text, "annotations": []}],
        })
    for tc in tool_calls:
        fn = tc.get("function") or {}
        output.append({
            "id": tc.get("id") or f"fc_{int(time.time() * 1000)}",
            "type": "function_call",
            "status": "completed",
            "call_id": tc.get("id") or "",
            "name": fn.get("name", ""),
            "arguments": fn.get("arguments") or "{}",
        })
    u = usage or {}
    status = STATUS_MAP.get(finish_reason, "completed")
    result: dict[str, Any] = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": status,
        "model": model,
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "usage": {
            "input_tokens": int(u.get("prompt_tokens") or 0),
            "output_tokens": int(u.get("completion_tokens") or 0),
            "total_tokens": int(u.get("total_tokens") or 0),
        },
    }
    if status == "incomplete":
        result["incomplete_details"] = {"reason": "max_output_tokens"}
    return result


async def stream_events(chunks: Any, model: str, response_id: str,
                        repair: Any = None) -> Any:
    """上游 OpenAI chunk 流 → Responses 事件流（Codex CLI 消费）。

    `repair` 是 `keyport.upstream.markup.MarkupRepair`（可为 None）：模型把原生工具
    调用吐成正文标记时，由它还原成 `function_call` 输出项。

    这条路径是标记修复最关键的落点：Codex 只执行结构化的 `function_call` 条目，
    标记若以纯文本落进 `output_text`，会被当普通正文写进会话历史，回合直接结束、
    工具一次都不跑。

    为此**正文改为延迟发出**：正文先进修复器缓冲，确认是普通正文才发
    `output_text.delta`。一旦上游给出结构化 `tool_calls`，说明这轮不是标记泄漏，
    立即把缓冲正文原样吐出并停用修复器。
    """
    from ..upstream.sse import iter_sse_json, sse_line

    base_response = {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "model": model,
        "output": [],
        "parallel_tool_calls": True,
        "tool_choice": "auto",
    }
    yield sse_line({"type": "response.created", "response": base_response}, "response.created")
    yield sse_line(
        {"type": "response.in_progress", "response": base_response}, "response.in_progress"
    )

    item_id = f"msg_{response_id}"
    text_started = False
    finish_reason = "stop"
    usage: dict[str, Any] = {}
    tool_buffers: dict[int, dict[str, Any]] = {}
    markup_calls: list[Any] = []

    def _open_text():
        return [
            sse_line({
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": item_id, "type": "message", "status": "in_progress",
                         "role": "assistant", "content": []},
            }, "response.output_item.added"),
            sse_line({
                "type": "response.content_part.added",
                "item_id": item_id,
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            }, "response.content_part.added"),
        ]

    def _text_delta(text: str):
        return sse_line({
            "type": "response.output_text.delta",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
            "delta": text,
        }, "response.output_text.delta")

    async for chunk in iter_sse_json(chunks):
        if chunk.get("usage"):
            usage = chunk["usage"]
        for choice in chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            fr = choice.get("finish_reason")
            if isinstance(fr, str) and fr:
                finish_reason = fr
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue

            raw_tcs = [tc for tc in (delta.get("tool_calls") or []) if isinstance(tc, dict)]
            text = delta.get("content")
            text = text if isinstance(text, str) and text else ""

            if raw_tcs:
                if repair is not None and repair.enabled():
                    pending = repair.flush()
                    if pending:
                        if not text_started:
                            text_started = True
                            for ev in _open_text():
                                yield ev
                        yield _text_delta(pending)
                    repair = None
                if text:
                    if not text_started:
                        text_started = True
                        for ev in _open_text():
                            yield ev
                    yield _text_delta(text)
                for tc in raw_tcs:
                    tci = int(tc.get("index") or 0)
                    slot = tool_buffers.setdefault(tci, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = str(tc["id"])
                    fn = tc.get("function") or {}
                    if fn.get("name"):
                        slot["name"] = str(fn["name"])
                    if isinstance(fn.get("arguments"), str):
                        slot["arguments"] += fn["arguments"]
                continue

            if text:
                if repair is not None and repair.enabled():
                    emit, calls = repair.feed(text)
                    if emit:
                        if not text_started:
                            text_started = True
                            for ev in _open_text():
                                yield ev
                        yield _text_delta(emit)
                    markup_calls.extend(calls)
                    continue
                if not text_started:
                    text_started = True
                    for ev in _open_text():
                        yield ev
                yield _text_delta(text)

    if repair is not None and repair.enabled():
        tail = repair.flush()
        if tail:
            if not text_started:
                text_started = True
                for ev in _open_text():
                    yield ev
            yield _text_delta(tail)

    if text_started:
        yield sse_line({
            "type": "response.output_text.done",
            "item_id": item_id,
            "output_index": 0,
            "content_index": 0,
        }, "response.output_text.done")

    output: list[dict[str, Any]] = []
    if text_started:
        output.append({"id": item_id, "type": "message", "status": "completed",
                       "role": "assistant", "content": []})
    # 截断的工具调用直接丢弃：arguments 只剩半截 JSON 时交给 Codex 会解析失败并卡死会话
    from ..upstream.truncation import is_truncated_arguments

    for slot in tool_buffers.values():
        if is_truncated_arguments(slot["arguments"]):
            continue
        call_id = slot["id"] or f"fc_{int(time.time() * 1000)}"
        output.append({"id": call_id, "type": "function_call", "status": "completed",
                       "call_id": call_id, "name": slot["name"], "arguments": slot["arguments"]})
    for i, call in enumerate(markup_calls):
        call_id = f"fc_mk_{response_id}_{i}"
        output.append({"id": call_id, "type": "function_call", "status": "completed",
                       "call_id": call_id, "name": call.name, "arguments": call.arguments})

    u = usage or {}
    status = STATUS_MAP.get(finish_reason, "completed")
    final = {
        **base_response,
        "status": status,
        "output": output,
        "usage": {
            "input_tokens": int(u.get("prompt_tokens") or 0),
            "output_tokens": int(u.get("completion_tokens") or 0),
            "total_tokens": int(u.get("total_tokens") or 0),
        },
    }
    yield sse_line({"type": "response.completed", "response": final}, "response.completed")
    yield sse_line("[DONE]")
