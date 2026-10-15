"""端到端：上游把工具调用吐成正文标记时，三种协议都要还原成结构化调用。

修复层单测在 `test_markup.py`；这里只验证**接线**是否正确 —— 即
`ChatSession` → `iter_lines` → 三条下游协议 的链路真的把标记吃掉了。

背景：上游只在请求声明了 `tools` 时才把原生标记解析成 `tool_calls`；`tools` 在
中转环节丢失时，标记会以纯文本落进 `content`。下游（尤其 Codex）只执行结构化
条目，于是把标记当普通正文写进历史，回合直接结束、工具一次都不跑。
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

_PIPE = "\uff5c\uff5c"
_OPEN = f"<{_PIPE}DSML{_PIPE} calls>"
_CLOSE = f"</{_PIPE}DSML{_PIPE} calls>"
_INVOKE_CLOSE = f"</{_PIPE}DSML{_PIPE} invoke>"
_PARAM_CLOSE = f"</{_PIPE}DSML{_PIPE} parameter>"


def _param(name: str, value: str) -> str:
    return f'<{_PIPE}DSML{_PIPE} parameter name="{name}" string="true">{value}{_PARAM_CLOSE}'


def _invoke(name: str, *params: str) -> str:
    body = "\n" + "\n".join(params) + "\n" if params else ""
    return f'<{_PIPE}DSML{_PIPE} invoke name="{name}">{body}{_INVOKE_CLOSE}'


def _block(*invokes: str) -> str:
    return _OPEN + "\n" + "\n".join(invokes) + "\n" + _CLOSE


RAW = "raw"
KEY = "sk-kp-markup"


def _sse(chunks: list[dict]) -> bytes:
    out = b""
    for c in chunks:
        out += f"data: {json.dumps(c, ensure_ascii=False)}\n\n".encode()
    return out + b"data: [DONE]\n\n"


def _chunk(delta: dict, finish: str | None = None) -> dict:
    choice: dict = {"index": 0, "delta": delta}
    if finish:
        choice["finish_reason"] = finish
    return {"id": "c1", "model": "deepseek-v4.1-flash", "choices": [choice]}


def _build(tmp_path: Path, raw_chunks: list[dict], *, with_tools: bool):
    """构造一个真实跑起来的下游网关，上游按 raw_chunks 返回。"""
    cfg = make_test_config(tmp_path)
    cfg.pool.retry_429_wait = 0
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="G", kind="oauth"))

    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("mk", hash_api_key(KEY), KEY[:12]),
    )

    body_bytes = _sse(raw_chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith(("/chat/completions", "/messages")):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, content=body_bytes,
                              headers={"content-type": "text/event-stream"})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]

    def payload(model: str) -> dict:
        body: dict = {"model": model,
                      "messages": [{"role": "user", "content": "看看仓库结构"}]}
        if with_tools:
            body["tools"] = [{"type": "function",
                              "function": {"name": "exec_command", "parameters": {}}}]
        return body

    return app, payload


AUTH = {"Authorization": f"Bearer {KEY}"}
MARKUP = _block(_invoke("exec_command", _param("cmd", "ls -la")))


# ---------------------------------------------------------------- OpenAI 协议


def test_openai_stream_repairs_markup(tmp_path: Path):
    """非流式聚合路径：标记 → message.tool_calls。"""
    app, payload = _build(tmp_path, [
        _chunk({"role": "assistant", "content": "先看一眼：\n"}),
        _chunk({"content": MARKUP}),
        _chunk({}, "stop"),
    ], with_tools=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=payload("deepseek-v4.1-flash"),
                       headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
    choice = data["choices"][0]
    assert choice["finish_reason"] == "tool_calls"
    assert choice["message"]["content"] == "先看一眼：\n"
    tcs = choice["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["function"]["name"] == "exec_command"
    assert json.loads(tcs[0]["function"]["arguments"])["cmd"] == "ls -la"


def test_openai_stream_repairs_markup_streaming(tmp_path: Path):
    """流式路径：标记 → delta.tool_calls。"""
    app, payload = _build(tmp_path, [
        _chunk({"role": "assistant", "content": "先看一眼：\n"}),
        _chunk({"content": MARKUP}),
        _chunk({}, "stop"),
    ], with_tools=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={**payload("deepseek-v4.1-flash"), "stream": True},
                       headers=AUTH)
        assert resp.status_code == 200, resp.text
        text = resp.text

    assert _PIPE not in text, "标记不得泄漏到下游流里"
    deltas = [json.loads(line[6:]) for line in text.splitlines()
              if line.startswith("data: ") and line[6:].strip() != "[DONE]"]
    tool_deltas = [d for d in deltas
                   if any(c.get("delta", {}).get("tool_calls") for c in d.get("choices", []))]
    assert tool_deltas, f"应产出 delta.tool_calls，实际帧: {deltas}"
    tc0 = tool_deltas[0]["choices"][0]["delta"]["tool_calls"][0]
    assert tc0["function"]["name"] == "exec_command"
    assert json.loads(tc0["function"]["arguments"])["cmd"] == "ls -la"


def test_openai_weak_mode_without_tools_declared(tmp_path: Path):
    """实测最常见的泄漏形态：请求体里根本没有 tools 名单。"""
    app, payload = _build(tmp_path, [
        _chunk({"content": MARKUP}),
        _chunk({}, "stop"),
    ], with_tools=False)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=payload("deepseek-v4.1-flash"),
                       headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
    assert data["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "exec_command"


def test_openai_real_tool_calls_are_not_overwritten(tmp_path: Path):
    """上游给了真实结构化调用 → 修复层不得插手，正文原样透出。"""
    app, payload = _build(tmp_path, [
        _chunk({"content": MARKUP}),
        _chunk({"tool_calls": [{"index": 0, "id": "call_real", "type": "function",
                                "function": {"name": "exec_command",
                                             "arguments": '{"cmd":"real"}'}}]}),
        _chunk({}, "tool_calls"),
    ], with_tools=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=payload("deepseek-v4.1-flash"),
                       headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
    msg = data["choices"][0]["message"]
    assert [tc["id"] for tc in msg["tool_calls"]] == ["call_real"]
    assert json.loads(msg["tool_calls"][0]["function"]["arguments"])["cmd"] == "real"
    # 缓冲的正文必须原样补回，不能被吞掉
    assert MARKUP in msg["content"]


# ---------------------------------------------------------------- Responses 协议


def test_responses_protocol_repairs_markup(tmp_path: Path):
    """Codex 走的这条：标记必须变成 function_call 输出项。"""
    app, payload = _build(tmp_path, [
        _chunk({"role": "assistant", "content": "先看一眼：\n"}),
        _chunk({"content": MARKUP}),
        _chunk({}, "stop"),
    ], with_tools=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/responses", json={**payload("deepseek-v4.1-flash"),
                                              "input": "看看仓库结构"},
                       headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
    calls = [item for item in data.get("output", []) if item.get("type") == "function_call"]
    assert calls, f"应产出 function_call，实际 output={data.get('output')}"
    assert calls[0]["name"] == "exec_command"
    assert json.loads(calls[0]["arguments"])["cmd"] == "ls -la"
    texts = "".join(
        part.get("text", "")
        for item in data.get("output", []) if item.get("type") == "message"
        for part in item.get("content", [])
    )
    assert _PIPE not in texts


def test_responses_protocol_repairs_markup_streaming(tmp_path: Path):
    app, payload = _build(tmp_path, [
        _chunk({"content": MARKUP}),
        _chunk({}, "stop"),
    ], with_tools=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/responses", json={**payload("deepseek-v4.1-flash"),
                                              "input": "x", "stream": True},
                       headers=AUTH)
        assert resp.status_code == 200, resp.text
        text = resp.text
    assert _PIPE not in text, "标记不得泄漏到 Responses 流里"
    assert "function_call" in text
    assert "exec_command" in text


# ---------------------------------------------------------------- Anthropic 协议


def test_anthropic_protocol_repairs_markup(tmp_path: Path):
    """Claude Code 走的这条：标记必须变成 tool_use 块。"""
    app, _payload = _build(tmp_path, [
        _chunk({"role": "assistant", "content": "先看一眼：\n"}),
        _chunk({"content": MARKUP}),
        _chunk({}, "stop"),
    ], with_tools=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/messages", json={
            "model": "deepseek-v4.1-flash", "max_tokens": 128,
            "messages": [{"role": "user", "content": "看看仓库结构"}],
            "tools": [{"name": "exec_command", "input_schema": {}}],
        }, headers={**AUTH, "anthropic-version": "2023-06-01"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
    blocks = data.get("content", [])
    tool_use = [b for b in blocks if b.get("type") == "tool_use"]
    assert tool_use, f"应产出 tool_use 块，实际 {blocks}"
    assert tool_use[0]["name"] == "exec_command"
    assert tool_use[0]["input"]["cmd"] == "ls -la"
    assert data.get("stop_reason") == "tool_use"
    assert all(_PIPE not in (b.get("text") or "") for b in blocks)


def test_anthropic_protocol_repairs_markup_streaming(tmp_path: Path):
    app, _payload = _build(tmp_path, [
        _chunk({"content": MARKUP}),
        _chunk({}, "stop"),
    ], with_tools=True)
    with TestClient(app) as tc:
        resp = tc.post("/v1/messages", json={
            "model": "deepseek-v4.1-flash", "max_tokens": 128, "stream": True,
            "messages": [{"role": "user", "content": "x"}],
            "tools": [{"name": "exec_command", "input_schema": {}}],
        }, headers={**AUTH, "anthropic-version": "2023-06-01"})
        assert resp.status_code == 200, resp.text
        text = resp.text
    assert _PIPE not in text, "标记不得泄漏到 Anthropic 流里"
    assert '"type": "tool_use"' in text
    assert "exec_command" in text
    # 每个 content_block_start 都要有配对的 stop
    assert text.count("content_block_start") == text.count("content_block_stop")


# ---------------------------------------------------------------- 开关


def test_feature_flag_disables_repair(tmp_path: Path):
    """关掉开关后行为与修复前一致：标记原样透出。"""
    cfg = make_test_config(tmp_path)
    cfg.features.repair_native_tool_markup = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="G", kind="oauth"))
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("mk", hash_api_key(KEY), KEY[:12]),
    )
    body_bytes = _sse([_chunk({"content": MARKUP}), _chunk({}, "stop")])

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith(("/chat/completions", "/messages")):
            return httpx.Response(200, json={"code": 0, "data": {}})
        return httpx.Response(200, content=body_bytes,
                              headers={"content-type": "text/event-stream"})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]

    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={"model": "deepseek-v4.1-flash",
                             "messages": [{"role": "user", "content": "x"}]},
                       headers=AUTH)
        assert resp.status_code == 200, resp.text
        data = resp.json()
    msg = data["choices"][0]["message"]
    assert "tool_calls" not in msg
    assert MARKUP in msg["content"]


def test_session_repair_disabled_flag_returns_none(tmp_path: Path):
    """关闭时不应构造修复器（避免无谓开销）。"""
    app, _payload = _build(tmp_path, [_chunk({}, "stop")], with_tools=True)
    ctx = app.state.ctx
    cfg = make_test_config(tmp_path / "b")
    cfg.features.repair_native_tool_markup = False
    from keyport.api.gateway import ChatParams, ChatSession

    gw = ctx.gateway
    old = gw.cfg
    gw.cfg = cfg
    try:
        s = ChatSession(gw, ChatParams(protocol="openai", model="m", body={}))
        assert s.markup_repair is None
    finally:
        gw.cfg = old
