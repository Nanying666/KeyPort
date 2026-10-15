"""截断工具调用丢弃。

移植自 sheng-du0521/workbuddy2api 的 `internal/upstream/truncation.go`
（`isTruncatedArguments` / `dropTruncatedToolCalls`）。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.security import hash_api_key
from keyport.upstream.base import AccountAuth
from keyport.upstream.truncation import (
    drop_truncated_tool_calls,
    is_truncated_arguments,
)

# ---- 判定口径 ----


def test_empty_arguments_is_not_truncated():
    """空串是合法无参工具 —— 模型可以不带参数调用，不是截断。"""
    assert is_truncated_arguments("") is False
    assert is_truncated_arguments("   ") is False
    assert is_truncated_arguments(None) is False
    assert is_truncated_arguments(123) is False


def test_half_json_is_truncated():
    """非空但无法解析 → 截断。"""
    assert is_truncated_arguments('{"cmd":') is True
    assert is_truncated_arguments('{"cmd":"ls') is True
    assert is_truncated_arguments("{") is True


def test_valid_json_is_not_truncated():
    """能解析即不是截断 —— 类型不对属于模型输出错误，交客户端 schema 校验。"""
    for raw in ('{"cmd":"ls"}', "{}", "null", "[]", "42", '"str"', "true"):
        assert is_truncated_arguments(raw) is False, raw


# ---- 丢弃 ----


def test_drop_removes_only_truncated():
    calls = [
        {"id": "a", "function": {"name": "f", "arguments": '{"x":1}'}},
        {"id": "b", "function": {"name": "g", "arguments": '{"y":'}},
        {"id": "c", "function": {"name": "h", "arguments": ""}},
    ]
    kept, dropped = drop_truncated_tool_calls(calls)
    assert dropped == 1
    assert [c["id"] for c in kept] == ["a", "c"]


def test_drop_keeps_positive_cases_untouched():
    """正例零改动。"""
    calls = [{"id": "a", "function": {"name": "f", "arguments": '{"x":1}'}}]
    kept, dropped = drop_truncated_tool_calls(calls)
    assert dropped == 0 and kept == calls


def test_drop_handles_malformed_shapes():
    assert drop_truncated_tool_calls(None) == (None, 0)
    assert drop_truncated_tool_calls("nope") == ("nope", 0)
    kept, dropped = drop_truncated_tool_calls([{"no_function": 1}])
    assert dropped == 0 and len(kept) == 1


# ---- 接线（非流式聚合） ----


def _sse(chunks: list[dict]) -> bytes:
    out = b""
    for c in chunks:
        out += f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode()
    return out + b"data: [DONE]\n\n"


def _chunk(delta: dict, finish: str | None = None) -> dict:
    choice: dict = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return {"id": "c1", "model": "hy3", "choices": [choice]}


def _build(tmp_path: Path, raw_chunks: list[dict]):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    key = "sk-kp-trunc"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("t", hash_api_key(key), key[:12]),
    )
    body = _sse(raw_chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith(("/chat/completions", "/messages")):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, content=body,
                              headers={"content-type": "text/event-stream"})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    return app, {"Authorization": f"Bearer {key}"}


def test_aggregate_drops_truncated_tool_call(tmp_path: Path):
    """半截 arguments 不得原样交给客户端（会解析失败并卡死会话）。"""
    app, auth = _build(tmp_path, [
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "f", "arguments": '{"cmd":'}}]}),
        _chunk({}, "length"),
    ])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={"model": "hy3", "messages": [{"role": "user", "content": "x"}]},
                       headers=auth)
        assert resp.status_code == 200, resp.text
        data = resp.json()
    msg = data["choices"][0]["message"]
    assert "tool_calls" not in msg
    # 整条只剩残缺调用 → 如实标记为截断，而不是假装成功
    assert data["choices"][0]["finish_reason"] == "length"


def test_aggregate_keeps_valid_tool_call(tmp_path: Path):
    app, auth = _build(tmp_path, [
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "f", "arguments": '{"cmd":"ls"}'}}]}),
        _chunk({}, "tool_calls"),
    ])
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={"model": "hy3", "messages": [{"role": "user", "content": "x"}]},
                       headers=auth)
        data = resp.json()
    calls = data["choices"][0]["message"]["tool_calls"]
    assert len(calls) == 1
    assert json.loads(calls[0]["function"]["arguments"])["cmd"] == "ls"


def test_responses_protocol_drops_truncated(tmp_path: Path):
    """Codex 走的这条：残缺 function_call 不得出现在 output 里。"""
    app, auth = _build(tmp_path, [
        _chunk({"tool_calls": [{"index": 0, "id": "call_1", "type": "function",
                                "function": {"name": "f", "arguments": '{"cmd":'}}]}),
        _chunk({}, "length"),
    ])
    with TestClient(app) as tc:
        resp = tc.post("/v1/responses",
                       json={"model": "hy3", "input": "x"}, headers=auth)
        assert resp.status_code == 200, resp.text
        data = resp.json()
    calls = [i for i in data.get("output", []) if i.get("type") == "function_call"]
    assert calls == []
