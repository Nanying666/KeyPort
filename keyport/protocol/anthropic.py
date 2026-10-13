"""Anthropic Messages ↔ OpenAI Chat 协议转换。

入站：Anthropic Messages 请求 → 上游 OpenAI chat 请求
出站：上游 OpenAI 响应/流 → Anthropic Messages 响应/事件流
"""

from __future__ import annotations

import json
import time
from typing import Any

# Anthropic stop_reason ← OpenAI finish_reason
STOP_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "function_call": "tool_use",
    "content_filter": "stop_sequence",
}


def text_of(content: Any) -> str:
    """把 Anthropic content（str 或 block 列表）压成纯文本。"""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return ""


def tool_result_text(block: dict[str, Any]) -> str:
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                parts.append(str(part.get("text", "")))
            elif part.get("type") == "image":
                parts.append("[image]")
        return "".join(parts)
    return json.dumps(content, ensure_ascii=False) if content is not None else ""


def _tool_choice(tc: Any) -> str:
    """Anthropic tool_choice → OpenAI（上游只认 string）。"""
    if not isinstance(tc, dict):
        return "auto"
    ttype = tc.get("type")
    if ttype == "any":
        return "required"
    if ttype == "tool":
        return str(tc.get("name", "")) or "auto"
    if ttype == "none":
        return "none"
    return "auto"


DEFAULT_MAX_TOKENS = 8192


def _blocks_text(blocks: list[dict[str, Any]]) -> str:
    """拼接内容块里的文本（忽略 tool_use / image 等非文本块）。"""
    return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")


def _parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """tool_calls.function.arguments（JSON 字符串）→ dict。"""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {"_raw": raw}
        return parsed if isinstance(parsed, dict) else {"_raw": raw}
    return {}


def _openai_image_block(part: dict[str, Any]) -> dict[str, Any] | None:
    """OpenAI image part → Anthropic image block。

    Anthropic 只接受 base64 source，远程 URL 无法透传，返回 None 由调用方跳过。
    """
    url = ""
    image_url = part.get("image_url")
    if isinstance(image_url, dict):
        url = str(image_url.get("url") or "")
    elif isinstance(image_url, str):
        url = image_url
    if not url:
        url = str(part.get("url") or "")
    if not url.startswith("data:"):
        return None
    try:
        header, b64 = url.split(",", 1)
        media = header[5:].split(";", 1)[0] or "image/png"
    except ValueError:
        return None
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": b64},
    }


def _openai_content_blocks(content: Any) -> list[dict[str, Any]]:
    """OpenAI content（str 或 parts 列表）→ Anthropic content block 列表。"""
    if content is None:
        return []
    if isinstance(content, str):
        return [{"type": "text", "text": content}] if content else []
    if not isinstance(content, list):
        return [{"type": "text", "text": str(content)}]
    blocks: list[dict[str, Any]] = []
    for part in content:
        if isinstance(part, str):
            if part:
                blocks.append({"type": "text", "text": part})
            continue
        if not isinstance(part, dict):
            continue
        ptype = part.get("type")
        if ptype in ("text", "input_text", "output_text"):
            text = part.get("text")
            if isinstance(text, str) and text:
                blocks.append({"type": "text", "text": text})
        elif ptype in ("image_url", "input_image"):
            block = _openai_image_block(part)
            if block is not None:
                blocks.append(block)
    return blocks


def _anthropic_tool_choice(tc: Any) -> dict[str, Any] | None:
    """OpenAI tool_choice → Anthropic tool_choice。"""
    if tc is None:
        return None
    if isinstance(tc, str):
        key = tc.strip().lower()
        if key == "auto":
            return {"type": "auto"}
        if key in ("required", "any"):
            return {"type": "any"}
        if key == "none":
            return {"type": "none"}
        return {"type": "tool", "name": tc.strip()} if tc.strip() else None
    if isinstance(tc, dict):
        ttype = str(tc.get("type", "")).strip().lower()
        if ttype == "function":
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            name = str(fn.get("name") or tc.get("name") or "")
            return {"type": "tool", "name": name} if name else {"type": "auto"}
        if ttype in ("auto", "any", "required", "none"):
            return {"type": "any" if ttype == "required" else ttype}
        if ttype == "tool":
            name = str(tc.get("name") or "")
            return {"type": "tool", "name": name} if name else None
    return None


def openai_to_anthropic_request(
    body: dict[str, Any], model_override: str | None = None
) -> dict[str, Any]:
    """OpenAI chat 请求体 → Anthropic Messages 请求体（与 request_to_openai 互逆）。

    历史 bug：`_build_anthropic_body` 误用 `request_to_openai`（反方向），
    导致 tools 的 name 全变空串、assistant.tool_calls 丢失、tool 消息丢
    tool_call_id，上游直接 400。本函数处理四处关键差异：
      - `system` 提到顶层（Anthropic 不接受 messages 里的 system 角色）
      - `assistant.tool_calls` → `tool_use` 内容块（保留 id / name / input）
      - `role=tool` → user 消息里的 `tool_result` 块（保留 tool_use_id）
      - 相邻同角色消息合并（Anthropic 要求 user / assistant 交替）
    """
    system_parts: list[str] = []
    messages: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if not blocks:
            return
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"].extend(blocks)
        else:
            messages.append({"role": role, "content": blocks})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user")).strip().lower()
        content = msg.get("content")

        if role == "system":
            text = _blocks_text(_openai_content_blocks(content))
            if text:
                system_parts.append(text)
            continue

        if role == "tool":
            push("user", [{
                "type": "tool_result",
                "tool_use_id": str(msg.get("tool_call_id") or ""),
                "content": _blocks_text(_openai_content_blocks(content)),
            }])
            continue

        if role == "assistant":
            blocks = _openai_content_blocks(content)
            for tc in msg.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name", "")),
                    "input": _parse_tool_arguments(fn.get("arguments")),
                })
            push("assistant", blocks)
            continue

        push("user", _openai_content_blocks(content))

    out: dict[str, Any] = {
        "model": model_override or str(body.get("model", "")),
        "messages": messages,
    }
    if system_parts:
        out["system"] = "\n\n".join(system_parts)

    max_tokens = body.get("max_tokens")
    if isinstance(max_tokens, (int, float)) and not isinstance(max_tokens, bool) and max_tokens > 0:
        out["max_tokens"] = int(max_tokens)
    else:
        out["max_tokens"] = DEFAULT_MAX_TOKENS  # Anthropic 必填
    if body.get("temperature") is not None:
        out["temperature"] = body["temperature"]
    if body.get("top_p") is not None:
        out["top_p"] = body["top_p"]
    stop = body.get("stop")
    if isinstance(stop, str) and stop:
        out["stop_sequences"] = [stop]
    elif isinstance(stop, list) and stop:
        out["stop_sequences"] = [str(s) for s in stop]
    if body.get("stream"):
        out["stream"] = True

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            fn = tool.get("function") if isinstance(tool.get("function"), dict) else tool
            name = str(fn.get("name", ""))
            if not name:
                continue  # 无名工具上游会拒，直接丢弃
            converted.append({
                "name": name,
                "description": str(fn.get("description", "")),
                "input_schema": fn.get("parameters") or fn.get("input_schema")
                or {"type": "object", "properties": {}},
            })
        if converted:
            out["tools"] = converted
            choice = _anthropic_tool_choice(body.get("tool_choice"))
            if choice is not None:
                out["tool_choice"] = choice
    return out


def request_to_openai(body: dict[str, Any], model_override: str | None = None) -> dict[str, Any]:
    """Anthropic Messages 请求体 → OpenAI chat 请求体。"""
    messages: list[dict[str, Any]] = []

    system_text = text_of(body.get("system"))
    if system_text:
        messages.append({"role": "system", "content": system_text})

    for msg in body.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role", "user"))
        content = msg.get("content")

        if isinstance(content, str):
            messages.append({"role": role, "content": content})
            continue
        if not isinstance(content, list):
            continue

        text_parts: list[str] = []
        tool_uses: list[dict[str, Any]] = []
        tool_results: list[dict[str, Any]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text_parts.append(str(block.get("text", "")))
            elif btype == "tool_use":
                tool_uses.append(block)
            elif btype == "tool_result":
                tool_results.append(block)
            elif btype == "image":
                src = block.get("source") or {}
                if src.get("type") == "base64":
                    text_parts.append("[image]")
            # thinking 块不回传（网关侧统一注入 thinking 开关）

        # tool_result 转 tool 角色消息（OpenAI 要求紧跟对应 tool_calls）
        for tr in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": str(tr.get("tool_use_id", "")),
                "content": tool_result_text(tr),
            })

        if role == "assistant" and tool_uses:
            messages.append({
                "role": "assistant",
                "content": "".join(text_parts) if text_parts else None,
                "tool_calls": [
                    {
                        "id": str(tu.get("id", "")),
                        "type": "function",
                        "function": {
                            "name": str(tu.get("name", "")),
                            "arguments": json.dumps(tu.get("input") or {}, ensure_ascii=False),
                        },
                    }
                    for tu in tool_uses
                ],
            })
            continue

        if text_parts:
            messages.append({"role": role, "content": "".join(text_parts)})

    out: dict[str, Any] = {
        "model": model_override or str(body.get("model", "")),
        "messages": messages,
    }
    for field_name in ("max_tokens", "temperature", "top_p"):
        if body.get(field_name) is not None:
            out[field_name] = body[field_name]
    if body.get("stop_sequences"):
        out["stop"] = body["stop_sequences"]
    if body.get("stream"):
        out["stream"] = True

    tools = body.get("tools")
    if isinstance(tools, list) and tools:
        converted = []
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            converted.append({
                "type": "function",
                "function": {
                    "name": str(tool.get("name", "")),
                    "description": str(tool.get("description", "")),
                    "parameters": tool.get("input_schema") or {"type": "object", "properties": {}},
                },
            })
        if converted:
            out["tools"] = converted
            out["tool_choice"] = _tool_choice(body.get("tool_choice"))
    return out


def response_from_openai(
    content_text: str,
    tool_calls: list[dict[str, Any]],
    finish_reason: str,
    usage: dict[str, Any] | None,
    model: str,
    msg_id: str,
) -> dict[str, Any]:
    """OpenAI 聚合结果 → Anthropic Messages 响应体。"""
    content: list[dict[str, Any]] = []
    if content_text:
        content.append({"type": "text", "text": content_text})
    for tc in tool_calls:
        fn = tc.get("function") or {}
        raw_args = fn.get("arguments") or "{}"
        try:
            parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
        except json.JSONDecodeError:
            parsed = {"_raw": raw_args}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{int(time.time() * 1000)}",
            "name": fn.get("name", ""),
            "input": parsed,
        })
    u = usage or {}
    return {
        "id": msg_id or f"msg_{int(time.time() * 1000)}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content or [{"type": "text", "text": ""}],
        "stop_reason": STOP_MAP.get(finish_reason, "end_turn"),
        "stop_sequence": None,
        "usage": {
            "input_tokens": int(u.get("prompt_tokens") or 0),
            "output_tokens": int(u.get("completion_tokens") or 0),
        },
    }


async def stream_events(chunks: Any, model: str, msg_id: str) -> Any:
    """把上游 OpenAI chunk 异步流转换为 Anthropic 事件流（async generator）。

    事件顺序：message_start → (content_block_start/delta)* → content_block_stop*
    → message_delta → message_stop。
    """
    from ..upstream.sse import iter_sse_json, sse_line

    yield sse_line({
        "type": "message_start",
        "message": {
            "id": msg_id,
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    }, "message_start")

    text_index = 0
    text_open = False
    tool_index: dict[int, int] = {}
    next_index = 1
    finish_reason = "stop"
    usage: dict[str, Any] = {}

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

            text = delta.get("content")
            if isinstance(text, str) and text:
                if not text_open:
                    text_open = True
                    yield sse_line({
                        "type": "content_block_start",
                        "index": text_index,
                        "content_block": {"type": "text", "text": ""},
                    }, "content_block_start")
                yield sse_line({
                    "type": "content_block_delta",
                    "index": text_index,
                    "delta": {"type": "text_delta", "text": text},
                }, "content_block_delta")

            for tc in delta.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tci = int(tc.get("index") or 0)
                if tci not in tool_index:
                    if text_open:
                        text_open = False
                        yield sse_line({"type": "content_block_stop", "index": text_index},
                                       "content_block_stop")
                    tool_index[tci] = next_index
                    next_index += 1
                    fn = tc.get("function") or {}
                    yield sse_line({
                        "type": "content_block_start",
                        "index": tool_index[tci],
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.get("id") or f"toolu_{tci}",
                            "name": fn.get("name", ""),
                            "input": {},
                        },
                    }, "content_block_start")
                args = (tc.get("function") or {}).get("arguments")
                if isinstance(args, str) and args:
                    yield sse_line({
                        "type": "content_block_delta",
                        "index": tool_index[tci],
                        "delta": {"type": "input_json_delta", "partial_json": args},
                    }, "content_block_delta")

    if text_open:
        yield sse_line({"type": "content_block_stop", "index": text_index}, "content_block_stop")
    for idx in tool_index.values():
        yield sse_line({"type": "content_block_stop", "index": idx}, "content_block_stop")

    u = usage or {}
    yield sse_line({
        "type": "message_delta",
        "delta": {"stop_reason": STOP_MAP.get(finish_reason, "end_turn"), "stop_sequence": None},
        "usage": {"output_tokens": int(u.get("completion_tokens") or 0)},
    }, "message_delta")
    yield sse_line({"type": "message_stop"}, "message_stop")


# ---- 反向：Anthropic 原生上游 → OpenAI（chat/completions） ----

# OpenAI finish_reason ← Anthropic stop_reason
REVERSE_STOP_MAP = {
    "end_turn": "stop",
    "stop_sequence": "stop",
    "max_tokens": "length",
    "tool_use": "tool_calls",
}


def openai_usage_from_anthropic(usage: Any) -> dict[str, Any]:
    """Anthropic usage → OpenAI usage（字段名与语义对齐）。"""
    u = usage if isinstance(usage, dict) else {}
    prompt = int(u.get("input_tokens") or 0)
    completion = int(u.get("output_tokens") or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


def openai_from_anthropic_response(
    body: dict[str, Any], model: str, completion_id: str
) -> dict[str, Any]:
    """Anthropic Messages 响应体 → OpenAI chat.completion 响应体。"""
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in body.get("content") or []:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            text_parts.append(str(block.get("text", "")))
        elif btype == "thinking":
            reasoning_parts.append(str(block.get("thinking", "")))
        elif btype == "tool_use":
            tool_calls.append({
                "id": str(block.get("id") or ""),
                "type": "function",
                "function": {
                    "name": str(block.get("name", "")),
                    "arguments": json.dumps(block.get("input") or {}, ensure_ascii=False),
                },
            })
    message: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = tool_calls
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model or str(body.get("model", "")),
        "choices": [{
            "index": 0,
            "message": message,
            "finish_reason": REVERSE_STOP_MAP.get(str(body.get("stop_reason")), "stop"),
        }],
        "usage": openai_usage_from_anthropic(body.get("usage")),
    }


async def openai_chunks_from_anthropic(lines: Any, model: str, completion_id: str) -> Any:
    """把上游 Anthropic 事件流转换为 OpenAI chunk 流（async generator）。

    产出与原生 OpenAI 上游同构的 chunk，使下游 chat / responses 两条路径
    无需感知上游协议差异；末尾附带 usage chunk 供记账。
    """
    from ..upstream.sse import sse_line

    tool_index: dict[int, int] = {}
    next_tool = 0
    finish_reason = "stop"
    usage: dict[str, Any] = {}
    emitted_role = False
    current_model = model

    def chunk(delta: dict, finish: str | None = None) -> bytes:
        return sse_line({
            "id": completion_id,
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": current_model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        })

    async for line in lines:
        if not isinstance(line, str) or not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            event = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue

        etype = str(event.get("type", ""))

        if etype == "message_start":
            msg = event.get("message") if isinstance(event.get("message"), dict) else {}
            if not emitted_role:
                emitted_role = True
                yield chunk({"role": "assistant", "content": ""})
            if isinstance(msg.get("usage"), dict):
                usage.update(openai_usage_from_anthropic(msg["usage"]))
            if msg.get("model"):
                current_model = str(msg["model"])
            continue

        if etype == "content_block_start":
            block = event.get("content_block")
            if isinstance(block, dict) and block.get("type") == "tool_use":
                idx = int(event.get("index") or 0)
                tool_index[idx] = next_tool
                yield chunk({"tool_calls": [{
                    "index": next_tool,
                    "id": str(block.get("id") or ""),
                    "type": "function",
                    "function": {"name": str(block.get("name", "")), "arguments": ""},
                }]})
                next_tool += 1
            continue

        if etype == "content_block_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            dtype = delta.get("type")
            if dtype == "text_delta":
                yield chunk({"content": str(delta.get("text", ""))})
            elif dtype == "thinking_delta":
                yield chunk({"reasoning_content": str(delta.get("thinking", ""))})
            elif dtype == "input_json_delta":
                idx = int(event.get("index") or 0)
                yield chunk({"tool_calls": [{
                    "index": tool_index.get(idx, 0),
                    "function": {"arguments": str(delta.get("partial_json", ""))},
                }]})
            continue

        if etype == "message_delta":
            delta = event.get("delta") if isinstance(event.get("delta"), dict) else {}
            finish_reason = REVERSE_STOP_MAP.get(str(delta.get("stop_reason")), "stop")
            if isinstance(event.get("usage"), dict):
                # 上游实测（zcode.z.ai / z.ai）：input_tokens 只在 message_delta
                # 给出真实值（message_start 恒为 0）。因此这里**优先取本事件的
                # input_tokens**，缺失时才回落 message_start 的值 —— 否则
                # prompt_tokens 永远是 0，token 配额会严重低估。
                out = openai_usage_from_anthropic(event["usage"])
                prompt = int(event["usage"].get("input_tokens") or 0)
                if prompt <= 0:
                    prompt = int(usage.get("prompt_tokens") or 0)
                completion = int(out.get("completion_tokens") or 0)
                if completion <= 0:
                    completion = int(usage.get("completion_tokens") or 0)
                usage = {
                    "prompt_tokens": prompt,
                    "completion_tokens": completion,
                    "total_tokens": prompt + completion,
                }
            continue

        if etype == "message_stop":
            yield chunk({}, finish_reason)
            continue

        if etype == "error":
            err = event.get("error") if isinstance(event.get("error"), dict) else {}
            raise RuntimeError(str(err.get("message") or "upstream anthropic error"))

    if not usage:
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    yield sse_line({
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": current_model,
        "choices": [],
        "usage": usage,
    })
    yield sse_line("[DONE]")
