"""z.ai Coding Plan（Anthropic 原生上游）端到端测试。

用 MockTransport 模拟 zcode.z.ai 的 Anthropic SSE，验证：
  1. 出站打到 /v1/messages，令牌走 Authorization: Bearer
  2. 携带 X-Device-Mid（设备身份，服务端按账号+设备算额度）
  3. 上游 Anthropic 事件流能正确转回 OpenAI chunk / Responses / Anthropic
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app
from keyport.security import hash_api_key
from keyport.upstream.base import AccountAuth

ZAI_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"


def _anthropic_sse(text: str = "你好，我是 GLM") -> bytes:
    """构造上游 Anthropic Messages SSE 事件流。"""
    events = [
        ("message_start", {"type": "message_start", "message": {
            "id": "msg_1", "type": "message", "role": "assistant", "model": "glm-4.6",
            "content": [], "usage": {"input_tokens": 7, "output_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0,
                                 "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                 "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta",
                           "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                           "usage": {"output_tokens": 5}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    body = ""
    for name, payload in events:
        body += f"event: {name}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
    return body.encode("utf-8")


@pytest.fixture()
def captured() -> dict:
    return {}


@pytest.fixture()
def client(tmp_path: Path, captured: dict) -> TestClient:
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx

    ctx.pool.upsert_account(AccountAuth(
        uid="zai-uid-1", realm="zai", nickname="iyzcfc",
        access_token="CODING_PLAN_JWT", kind="apikey",
        device_token="78b921b4-ef0e-47cf-a333-555a14baa2f1",
        base_url=ZAI_BASE, protocol="anthropic",
    ))

    raw = "sk-kp-zai-test-key"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("zai", hash_api_key(raw), raw[:12]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["body"] = json.loads(request.content.decode("utf-8"))
        return httpx.Response(
            200, content=_anthropic_sse(),
            headers={"content-type": "text/event-stream"},
        )

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


AUTH = {"Authorization": "Bearer sk-kp-zai-test-key"}


def test_request_goes_to_anthropic_messages_endpoint(client: TestClient, captured: dict):
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    assert captured["url"] == ZAI_BASE + "/v1/messages"


def test_bearer_token_and_device_mid_headers(client: TestClient, captured: dict):
    client.post("/v1/chat/completions",
                json={"model": "glm-4.6", "messages": [{"role": "user", "content": "hi"}]},
                headers=AUTH)
    hdrs = {k.lower(): v for k, v in captured["headers"].items()}
    assert hdrs["authorization"] == "Bearer CODING_PLAN_JWT"
    # 设备身份必须带上：服务端按「账号+设备」算额度
    assert hdrs["x-device-mid"] == "78b921b4-ef0e-47cf-a333-555a14baa2f1"
    assert hdrs["anthropic-version"] == "2023-06-01"


def test_outbound_body_is_anthropic_shape(client: TestClient, captured: dict):
    client.post("/v1/chat/completions",
                json={"model": "glm-4.6", "max_tokens": 128,
                      "messages": [{"role": "system", "content": "sys"},
                                   {"role": "user", "content": "hi"}]},
                headers=AUTH)
    body = captured["body"]
    assert body["stream"] is True
    # 客户端显式给的 max_tokens 必须透传（Anthropic 该字段必填）
    assert body["max_tokens"] == 128
    # Anthropic 的 system 是顶层字段，不在 messages 里
    assert body["system"]
    assert all(m["role"] != "system" for m in body["messages"])
    # zcode-plan 端点会给首条 user 消息挂 currentDate 上下文（<system-reminder>），
    # 用户原文作为后一个文本块保留
    first = body["messages"][0]
    assert first["role"] == "user"
    texts = [c["text"] for c in first["content"] if c.get("type") == "text"]
    assert "hi" in texts


def test_custom_prompt_mode_replaces_system_on_anthropic_path(
    client: TestClient, captured: dict
):
    """默认 custom 模式下，客户端 system 被网关自有提示词整体替换。

    zcode-plan 端点上，替换后的提示词仍会被追加在官方身份块之后
    （端点内容检查要求，见 zcode_system 模块文档）。
    """
    cfg = client.app.state.ctx.cfg
    cfg.prompt.mode = "custom"
    cfg.prompt.default_text = "GATEWAY PROMPT"
    client.post("/v1/chat/completions",
                json={"model": "glm-4.6",
                      "messages": [{"role": "system", "content": "client sys"},
                                   {"role": "user", "content": "hi"}]},
                headers=AUTH)
    system = captured["body"]["system"]
    texts = [b["text"] for b in system]
    assert "GATEWAY PROMPT" in texts
    assert "client sys" not in texts


def test_passthrough_prompt_mode_keeps_client_system(client: TestClient, captured: dict):
    """passthrough 模式下客户端 system 保留。

    zcode-plan 端点会先放官方身份块（否则上游 3012），
    客户端 system 追加在末尾——内容不丢，只是不再独占该字段。
    """
    client.app.state.ctx.cfg.prompt.mode = "passthrough"
    client.post("/v1/chat/completions",
                json={"model": "glm-4.6",
                      "messages": [{"role": "system", "content": "client sys"},
                                   {"role": "user", "content": "hi"}]},
                headers=AUTH)
    system = captured["body"]["system"]
    texts = [b["text"] for b in system]
    # 客户端提示词必须保留，且排在官方身份块之后
    assert "client sys" in texts
    assert texts[-1] == "client sys"


def test_max_tokens_defaults_when_client_omits_it(client: TestClient, captured: dict):
    """Anthropic 要求 max_tokens 必填；客户端没给时要补默认值而不是报错。"""
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    assert captured["body"]["max_tokens"] > 0


def test_chat_non_stream_translates_back_to_openai(client: TestClient):
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "你好，我是 GLM"
    assert body["choices"][0]["finish_reason"] == "stop"
    # Anthropic 的 input/output tokens 要映射成 OpenAI 的 prompt/completion
    assert body["usage"]["prompt_tokens"] == 7
    assert body["usage"]["completion_tokens"] == 5
    assert body["usage"]["total_tokens"] == 12


def test_chat_stream_yields_openai_chunks(client: TestClient):
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    text = resp.text
    assert "chat.completion.chunk" in text
    assert "你好，我是 GLM" in text
    assert "[DONE]" in text
    # 不应泄漏上游 Anthropic 的事件名
    assert "message_start" not in text
    assert "content_block_delta" not in text


def test_messages_passthrough_stays_anthropic(client: TestClient):
    """下游要 Anthropic 时，转换后应还原成 Anthropic 事件流。"""
    resp = client.post("/v1/messages",
                       json={"model": "glm-4.6", "stream": True, "max_tokens": 64,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    assert "event: message_start" in resp.text
    assert "event: content_block_delta" in resp.text
    assert "event: message_stop" in resp.text


def test_messages_non_stream(client: TestClient):
    resp = client.post("/v1/messages",
                       json={"model": "glm-4.6", "max_tokens": 64,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "你好，我是 GLM"
    assert body["usage"] == {"input_tokens": 7, "output_tokens": 5}


def test_responses_stream(client: TestClient):
    resp = client.post("/v1/responses",
                       json={"model": "glm-4.6", "input": "hi", "stream": True},
                       headers=AUTH)
    assert resp.status_code == 200
    assert "response.created" in resp.text
    assert "response.output_text.delta" in resp.text
    assert "response.completed" in resp.text


def test_responses_with_tools_reaches_upstream_with_valid_tool_names(
    client: TestClient, captured: dict
):
    """回归：Codex CLI 带工具的 /v1/responses 曾因工具名空串被上游 400。

    走完整网关路径（responses → openai → anthropic 上游），确认最终出站
    body 里工具名与 tool_use 块都完好 —— 这是之前 502/400 的直接原因。
    """
    resp = client.post("/v1/responses", json={
        "model": "glm-4.6",
        "instructions": "You are a coding agent.",
        "input": [
            {"type": "message", "role": "user",
             "content": [{"type": "input_text", "text": "list files"}]},
            {"type": "function_call", "call_id": "call_1",
             "name": "shell", "arguments": '{"cmd":"ls"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "a.txt"},
        ],
        "tools": [{
            "type": "function", "name": "shell", "description": "run shell",
            "parameters": {"type": "object", "properties": {"cmd": {"type": "string"}},
                           "required": ["cmd"]},
        }],
        "tool_choice": "auto",
        "stream": True,
    }, headers=AUTH)
    assert resp.status_code == 200, resp.text

    body = captured["body"]
    # 工具名必须非空（空 name 正是上游 400 的原因）
    names = [t["name"] for t in body.get("tools", [])]
    assert names == ["shell"], f"工具名丢失: {names}"

    blocks = [b for m in body["messages"] for b in m["content"]]
    tool_use = [b for b in blocks if b.get("type") == "tool_use"]
    tool_result = [b for b in blocks if b.get("type") == "tool_result"]
    assert tool_use and tool_use[0]["name"] == "shell"
    assert tool_use[0]["input"] == {"cmd": "ls"}
    assert tool_result and tool_result[0]["tool_use_id"] == "call_1"

    # Anthropic 形态：system 在顶层，messages 里只有 user / assistant
    assert body["system"]
    assert all(m["role"] in ("user", "assistant") for m in body["messages"])
    assert body["max_tokens"] > 0


def test_messages_with_tools_maps_to_anthropic_tool_use(
    client: TestClient, captured: dict
):
    """z.ai（Anthropic 原生上游）的 /v1/messages 带工具：出站必须是 tool_use 块。

    覆盖 Anthropic → OpenAI → Anthropic 的往返：入站 tool_use/tool_result 经
    网关转换后回到上游，工具名与 input 必须完好（否则上游 400）。
    """
    resp = client.post("/v1/messages", json={
        "model": "glm-4.6", "max_tokens": 64, "stream": True,
        "messages": [
            {"role": "user", "content": "list files"},
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "tu_1", "name": "shell",
                 "input": {"cmd": "ls"}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "tu_1", "content": "a.txt"}]},
        ],
        "tools": [{"name": "shell", "description": "run shell",
                   "input_schema": {"type": "object",
                                    "properties": {"cmd": {"type": "string"}}}}],
    }, headers=AUTH)
    assert resp.status_code == 200, resp.text

    body = captured["body"]
    names = [t["name"] for t in body.get("tools", [])]
    assert names == ["shell"], f"工具名丢失: {names}"

    blocks = [b for m in body["messages"] for b in m["content"]]
    tool_use = [b for b in blocks if b.get("type") == "tool_use"]
    tool_result = [b for b in blocks if b.get("type") == "tool_result"]
    assert tool_use and tool_use[0]["name"] == "shell"
    assert tool_use[0]["input"] == {"cmd": "ls"}
    assert tool_result and tool_result[0]["tool_use_id"] == "tu_1"
    # Anthropic 形态：system 在顶层，messages 里只有 user / assistant
    assert all(m["role"] in ("user", "assistant") for m in body["messages"])


def test_chat_with_tools_preserves_tool_names(client: TestClient, captured: dict):
    """z.ai 的 /v1/chat/completions 带工具：出站 tools 名不得为空。"""
    resp = client.post("/v1/chat/completions", json={
        "model": "glm-4.6", "stream": True,
        "messages": [{"role": "user", "content": "run ls"}],
        "tools": [{"type": "function", "function": {
            "name": "shell", "description": "run shell",
            "parameters": {"type": "object",
                           "properties": {"cmd": {"type": "string"}}}}}],
    }, headers=AUTH)
    assert resp.status_code == 200, resp.text
    names = [t["name"] for t in captured["body"].get("tools", [])]
    assert names == ["shell"]


def test_logged_with_usage(client: TestClient):
    ctx = client.app.state.ctx
    ctx.db.execute("DELETE FROM request_logs")
    client.post("/v1/chat/completions",
                json={"model": "glm-4.6", "messages": [{"role": "user", "content": "hi"}]},
                headers=AUTH)
    row = ctx.db.query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 1")[0]
    assert row["realm"] == "zai"
    assert row["total_tokens"] == 12
    assert row["status"] == 200
