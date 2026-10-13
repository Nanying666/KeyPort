"""请求体改写按「实际选中账号的域」进行，而非请求声明的域。

背景（真实 400）：`/v1/responses` 请求不带域前缀时 `realm` 为空串，选号可能
落到 global（workbuddy）账号，而改写用空串判定 → 不注入 system → 上游报
`11-128 first message is not system prompt`。修复：改写移入选号循环，用实际
账号的域。
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


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _upstream_stream(text: str = "OK") -> bytes:
    return _sse(
        {"id": "c1", "model": "glm-5.3", "created": 1,
         "choices": [{"index": 0, "delta": {"content": text}}]},
        {"id": "c1", "model": "glm-5.3",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6}},
    )


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx

    # 只放一个 global 账号：请求不带域前缀时选号必然落到它
    ctx.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="T", kind="oauth",
        base_url="https://www.workbuddy.ai", protocol="openai",
    ))

    raw = "sk-kp-realm-test"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("realm", hash_api_key(raw), raw[:12]),
    )

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/chat/completions"):
            captured["body"] = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200, content=_upstream_stream(),
                headers={"content-type": "text/event-stream"})
        return httpx.Response(404, json={"code": 404})

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        tc.captured = captured  # type: ignore[attr-defined]
        yield tc


AUTH = {"Authorization": "Bearer sk-kp-realm-test"}


def test_responses_without_realm_prefix_gets_system_injected(client: TestClient):
    """不带域前缀的 responses 请求落到 global 账号时，必须补 system 首条。

    回归：改写曾用请求声明的域（空串）判定，导致 system 未注入 → 上游 11-128。
    """
    resp = client.post("/v1/responses",
                       json={"model": "glm-5.3", "input": "hi", "stream": True},
                       headers=AUTH)
    assert resp.status_code == 200, resp.text

    body = client.captured["body"]
    assert body["messages"], "请求体不应为空"
    assert body["messages"][0]["role"] == "system", (
        f"首条必须是 system，实际={body['messages'][0]}")


def test_chat_without_realm_prefix_gets_system_injected(client: TestClient):
    """chat/completions 同样路径。"""
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-5.3",
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = client.captured["body"]
    assert body["messages"][0]["role"] == "system"


def test_messages_without_realm_prefix_gets_system_injected(client: TestClient):
    """messages 同样路径。"""
    resp = client.post("/v1/messages",
                       json={"model": "glm-5.3", "max_tokens": 32,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200, resp.text
    body = client.captured["body"]
    assert body["messages"][0]["role"] == "system"


def test_rewrite_does_not_accumulate_across_retries(client: TestClient):
    """换号重试时从原始快照改写，不会在已改写结果上二次改写。"""
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-5.3",
                             "messages": [{"role": "system", "content": "custom"},
                                          {"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    body = client.captured["body"]
    # system 只应有一条，且是客户端那条（passthrough 模式保留）
    systems = [m for m in body["messages"] if m.get("role") == "system"]
    assert len(systems) == 1, f"system 不应重复注入: {systems}"
