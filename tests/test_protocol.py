"""协议转换与 SSE 聚合单元测试。"""

from __future__ import annotations

import json

import pytest

from keyport.protocol import anthropic, responses
from keyport.upstream.sse import aggregate, ensure_usage_total


def _chunk(content: str = "", **extra) -> dict:
    delta = {"content": content} if content else {}
    delta.update(extra)
    return {"id": "c1", "model": "m", "created": 1, "choices": [{"index": 0, "delta": delta}]}


# ---- 聚合 ----


def test_aggregate_joins_content_and_usage():
    chunks = [_chunk("Hello"), _chunk(" world"),
              {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
               "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}]
    agg = aggregate(chunks)
    agg.saw_done = True  # 上游正常收尾会发 [DONE]，由读取侧置位
    assert agg.content == "Hello world"
    assert agg.finish_reason == "stop"
    assert agg.usage["total_tokens"] == 5
    assert agg.truncated is False


def test_aggregate_merges_tool_call_deltas():
    chunks = [
        _chunk(tool_calls=[{"index": 0, "id": "call_1", "type": "function",
                            "function": {"name": "get_weather", "arguments": ""}}]),
        _chunk(tool_calls=[{"index": 0, "function": {"arguments": '{"city":'}}]),
        _chunk(tool_calls=[{"index": 0, "function": {"arguments": '"SF"}'}}]),
        {"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]},
    ]
    agg = aggregate(chunks)
    assert len(agg.tool_calls) == 1
    assert agg.tool_calls[0]["function"]["name"] == "get_weather"
    assert agg.tool_calls[0]["function"]["arguments"] == '{"city":"SF"}'
    assert agg.finish_reason == "tool_calls"


def test_aggregate_flags_length_truncation():
    agg = aggregate([_chunk("partial"),
                     {"choices": [{"index": 0, "delta": {}, "finish_reason": "length"}]}])
    assert agg.truncated is True
    assert agg.truncation_reason == "max_tokens"


def test_aggregate_flags_eof_truncation():
    agg = aggregate([_chunk("partial")])
    agg.saw_done = False
    assert agg.truncated is True
    assert agg.truncation_reason == "upstream_eof"


def test_ensure_usage_total_fills_missing_total():
    assert ensure_usage_total({"prompt_tokens": 4, "completion_tokens": 6})["total_tokens"] == 10
    assert ensure_usage_total(None)["total_tokens"] == 0


# ---- Anthropic 入站 ----


def test_anthropic_request_maps_system_and_tools():
    body = {
        "model": "claude-sonnet",
        "system": "be terse",
        "max_tokens": 100,
        "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        "tools": [{"name": "f", "description": "d", "input_schema": {"type": "object"}}],
        "tool_choice": {"type": "any"},
    }
    out = anthropic.request_to_openai(body)
    assert out["messages"][0] == {"role": "system", "content": "be terse"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["tools"][0]["function"]["name"] == "f"
    assert out["tool_choice"] == "required"
    assert out["max_tokens"] == 100


def test_anthropic_tool_result_becomes_tool_message():
    body = {
        "model": "m",
        "messages": [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "f", "input": {"a": 1}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "ok"}]},
        ],
    }
    out = anthropic.request_to_openai(body)
    assert out["messages"][0]["role"] == "assistant"
    assert out["messages"][0]["tool_calls"][0]["id"] == "tu_1"
    assert out["messages"][1] == {"role": "tool", "tool_call_id": "tu_1", "content": "ok"}


def test_anthropic_response_shape():
    result = anthropic.response_from_openai(
        content_text="hi", tool_calls=[], finish_reason="length",
        usage={"prompt_tokens": 5, "completion_tokens": 7}, model="m", msg_id="msg_1",
    )
    assert result["type"] == "message"
    assert result["stop_reason"] == "max_tokens"
    assert result["usage"] == {"input_tokens": 5, "output_tokens": 7}
    assert result["content"][0]["text"] == "hi"


def test_anthropic_response_tool_use_block():
    result = anthropic.response_from_openai(
        content_text="",
        tool_calls=[{"id": "c1", "function": {"name": "f", "arguments": '{"x":1}'}}],
        finish_reason="tool_calls", usage={}, model="m", msg_id="msg_2",
    )
    block = result["content"][0]
    assert block["type"] == "tool_use"
    assert block["input"] == {"x": 1}
    assert result["stop_reason"] == "tool_use"


@pytest.mark.asyncio
async def test_anthropic_stream_event_sequence():
    async def lines():
        yield "data: " + json.dumps(_chunk("He"))
        yield "data: " + json.dumps(_chunk("llo"))
        yield "data: " + json.dumps(
            {"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
             "usage": {"completion_tokens": 2}})
        yield "data: [DONE]"

    events = [chunk async for chunk in anthropic.stream_events(lines(), "m", "msg_1")]
    text = b"".join(events).decode()
    assert "event: message_start" in text
    assert "event: content_block_start" in text
    assert '"text_delta"' in text
    assert "event: message_delta" in text
    assert "event: message_stop" in text


# ---- Anthropic → OpenAI chunk（z.ai 上游） ----


async def _collect_openai(events: list[dict]) -> list[dict]:
    """把 Anthropic 事件序列喂给 openai_chunks_from_anthropic，收集 OpenAI chunk。"""

    async def lines():
        for event in events:
            yield "event: " + str(event.get("type", ""))
            yield "data: " + json.dumps(event)

    out: list[dict] = []
    async for raw in anthropic.openai_chunks_from_anthropic(lines(), "m", "cmpl_1"):
        text = raw.decode()
        for line in text.splitlines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload and payload != "[DONE]":
                    out.append(json.loads(payload))
    return out


@pytest.mark.asyncio
async def test_openai_usage_takes_input_tokens_from_message_delta():
    """z.ai 的 input_tokens 只在 message_delta 有真实值。

    message_start 恒为 0（实测），若沿用其值则 prompt_tokens 永远 0，
    token 配额会严重低估。此处断言取 message_delta 的值。
    """
    events = [
        {"type": "message_start", "message": {
            "model": "glm-5.3-flash",
            "usage": {"input_tokens": 0, "output_tokens": 0}}},
        {"type": "content_block_start", "index": 0,
         "content_block": {"type": "text", "text": ""}},
        {"type": "content_block_delta", "index": 0,
         "delta": {"type": "text_delta", "text": "OK"}},
        {"type": "message_delta",
         "delta": {"stop_reason": "end_turn"},
         "usage": {"input_tokens": 1508, "output_tokens": 19,
                   "cache_read_input_tokens": 192}},
        {"type": "message_stop"},
    ]
    chunks = await _collect_openai(events)
    usage = next((c["usage"] for c in chunks if c.get("usage")), None)
    assert usage is not None, "末尾应附带 usage chunk"
    assert usage["prompt_tokens"] == 1508
    assert usage["completion_tokens"] == 19
    assert usage["total_tokens"] == 1527


@pytest.mark.asyncio
async def test_openai_usage_falls_back_to_message_start_when_delta_lacks_input():
    """message_delta 未带 input_tokens 时，回落 message_start 的值。"""
    events = [
        {"type": "message_start", "message": {
            "model": "m",
            "usage": {"input_tokens": 42, "output_tokens": 0}}},
        {"type": "message_delta",
         "delta": {"stop_reason": "end_turn"},
         "usage": {"output_tokens": 7}},
        {"type": "message_stop"},
    ]
    chunks = await _collect_openai(events)
    usage = next((c["usage"] for c in chunks if c.get("usage")), None)
    assert usage is not None
    assert usage["prompt_tokens"] == 42
    assert usage["completion_tokens"] == 7
    assert usage["total_tokens"] == 49


# ---- OpenAI → Anthropic（原生上游） ----


def test_openai_to_anthropic_moves_system_to_top_level():
    body = {
        "model": "glm-4.6",
        "messages": [
            {"role": "system", "content": "be terse"},
            {"role": "user", "content": "hi"},
        ],
        "max_tokens": 100,
    }
    out = anthropic.openai_to_anthropic_request(body)
    assert out["system"] == "be terse"
    assert all(m["role"] != "system" for m in out["messages"])
    assert out["messages"][0] == {"role": "user", "content": [{"type": "text", "text": "hi"}]}
    assert out["max_tokens"] == 100


def test_openai_to_anthropic_keeps_tool_names():
    """回归：曾误用反方向转换器，导致工具名全变空串 → 上游 400。"""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "go"}],
        "tools": [
            {"type": "function", "name": "shell", "description": "d",
             "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}}}},
        ],
        "tool_choice": "auto",
    }
    out = anthropic.openai_to_anthropic_request(body)
    assert out["tools"][0]["name"] == "shell"
    assert out["tools"][0]["input_schema"]["properties"] == {"cmd": {"type": "string"}}
    assert out["tool_choice"] == {"type": "auto"}


def test_openai_to_anthropic_tool_calls_become_tool_use_blocks():
    """回归：assistant.tool_calls 曾整条丢失、tool 消息丢 tool_call_id。"""
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "ls"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "shell", "arguments": '{"cmd":"ls"}'}}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "a.txt"},
        ],
    }
    out = anthropic.openai_to_anthropic_request(body)
    msgs = out["messages"]
    assert msgs[0]["role"] == "user"
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"][0] == {
        "type": "tool_use", "id": "call_1", "name": "shell", "input": {"cmd": "ls"}}
    # tool 结果转成 user 消息里的 tool_result，且必须保留 tool_use_id
    assert msgs[2]["role"] == "user"
    assert msgs[2]["content"][0] == {
        "type": "tool_result", "tool_use_id": "call_1", "content": "a.txt"}


def test_openai_to_anthropic_merges_adjacent_same_role():
    """Anthropic 要求 user / assistant 交替，连续同角色必须合并。"""
    body = {
        "model": "m",
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "user", "content": "two"},
        ],
    }
    out = anthropic.openai_to_anthropic_request(body)
    assert len(out["messages"]) == 1
    assert out["messages"][0]["content"] == [
        {"type": "text", "text": "one"}, {"type": "text", "text": "two"}]


def test_openai_to_anthropic_drops_nameless_tools():
    """无名工具上游会拒，直接丢弃而不是发出空 name。"""
    body = {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": ""}}],
    }
    out = anthropic.openai_to_anthropic_request(body)
    assert "tools" not in out


def test_openai_to_anthropic_max_tokens_always_present():
    """Anthropic 要求 max_tokens 必填：缺失/非法时补默认值。"""
    for bad in (None, 0, -1, "128", True):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}]}
        if bad is not None:
            body["max_tokens"] = bad
        out = anthropic.openai_to_anthropic_request(body)
        assert out["max_tokens"] == anthropic.DEFAULT_MAX_TOKENS


# ---- Responses 入站 ----


def test_responses_request_maps_input_and_tools():
    body = {
        "model": "gpt-5",
        "instructions": "sys",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "hi"}]},
        ],
        "tools": [{"type": "function", "name": "f", "parameters": {"type": "object"}}],
        "reasoning": {"effort": "medium"},
        "max_output_tokens": 50,
    }
    out = responses.request_to_openai(body)
    assert out["messages"][0] == {"role": "system", "content": "sys"}
    assert out["messages"][1] == {"role": "user", "content": "hi"}
    assert out["tools"][0]["function"]["name"] == "f"
    assert out["reasoning_effort"] == "medium"
    assert out["max_tokens"] == 50


def test_responses_function_call_roundtrip():
    body = {
        "model": "m",
        "input": [
            {"type": "function_call", "call_id": "c1", "name": "f", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "c1", "output": "42"},
        ],
    }
    out = responses.request_to_openai(body)
    assert out["messages"][0]["tool_calls"][0]["id"] == "c1"
    assert out["messages"][1] == {"role": "tool", "tool_call_id": "c1", "content": "42"}


def test_responses_response_shape():
    result = responses.response_from_openai(
        content_text="answer", tool_calls=[], finish_reason="stop",
        usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        model="m", response_id="resp_1",
    )
    assert result["object"] == "response"
    assert result["status"] == "completed"
    assert result["output"][0]["content"][0]["text"] == "answer"


def test_responses_length_marks_incomplete():
    result = responses.response_from_openai(
        content_text="x", tool_calls=[], finish_reason="length",
        usage={}, model="m", response_id="resp_2",
    )
    assert result["status"] == "incomplete"
    assert result["incomplete_details"]["reason"] == "max_output_tokens"


@pytest.mark.asyncio
async def test_responses_stream_event_sequence():
    async def lines():
        yield "data: " + json.dumps(_chunk("Hi"))
        yield "data: [DONE]"

    events = [chunk async for chunk in responses.stream_events(lines(), "m", "resp_1")]
    text = b"".join(events).decode()
    assert "event: response.created" in text
    assert "response.output_text.delta" in text
    assert "event: response.completed" in text
