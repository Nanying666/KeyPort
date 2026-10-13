"""端到端 API 测试：用 MockTransport 替换上游，验证鉴权、路由、协议与落库。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app
from keyport.security import hash_api_key


def _sse(*chunks: dict) -> bytes:
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks)
    return (body + "data: [DONE]\n\n").encode()


def _upstream_stream(text: str = "hello world") -> bytes:
    return _sse(
        {"id": "c1", "model": "glm-4.6", "created": 1,
         "choices": [{"index": 0, "delta": {"role": "assistant"}}]},
        {"id": "c1", "model": "glm-4.6",
         "choices": [{"index": 0, "delta": {"content": text}}]},
        {"id": "c1", "model": "glm-4.6",
         "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
         "usage": {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}},
    )


def _handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/chat/completions"):
        return httpx.Response(
            200, content=_upstream_stream(), headers={"content-type": "text/event-stream"}
        )
    if "models" in path:
        return httpx.Response(200, json={"data": [
            {"id": "glm-4.6", "displayName": "GLM-4.6", "contextWindow": 200000,
             "maxTokens": 32000, "supportedEfforts": ["low", "high"], "series": "glm"},
        ]})
    if "get-user-resource" in path:
        return httpx.Response(200, json={"code": 0, "data": {"credits": 1234, "used": 10}})
    if "daily-checkin" in path:
        return httpx.Response(200, json={"code": 0, "msg": "ok"})
    return httpx.Response(404, json={"code": 404, "msg": "not found"})


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx

    from keyport.upstream.base import AccountAuth

    ctx.pool.upsert_account(AccountAuth(
        uid="u1", realm="cn", access_token="tok", refresh_token="r", expires_at=0,
    ))

    raw = "sk-kp-test-key-123456"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("test", hash_api_key(raw), raw[:12]),
    )

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


AUTH = {"Authorization": "Bearer sk-kp-test-key-123456"}


def test_healthz(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["service"] == "keyport"
    assert resp.json()["total"] == 1


def test_missing_key_rejected(client: TestClient):
    resp = client.post("/v1/chat/completions", json={"model": "glm-4.6", "messages": []})
    assert resp.status_code == 401


def test_bad_key_rejected(client: TestClient):
    resp = client.post("/v1/chat/completions", json={"model": "glm-4.6", "messages": []},
                       headers={"Authorization": "Bearer wrong"})
    assert resp.status_code == 401


def test_model_whitelist_blocks(client: TestClient):
    ctx = client.app.state.ctx
    ctx.db.execute("UPDATE api_keys SET model_allow=? WHERE id=1",
                   (json.dumps(["allowed-only"]),))
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6", "messages": [{"role": "user", "content": "x"}]},
                       headers=AUTH)
    assert resp.status_code == 400
    assert "未授权模型" in resp.json()["error"]["message"]


# ---- chat/completions ----


def test_chat_non_stream(client: TestClient):
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6", "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert body["choices"][0]["message"]["content"] == "hello world"
    assert body["usage"]["total_tokens"] == 10


def test_chat_stream(client: TestClient):
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6", "stream": True,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    assert "data: " in resp.text
    assert "[DONE]" in resp.text


def test_chat_logged_to_db(client: TestClient):
    client.post("/v1/chat/completions",
                json={"model": "glm-4.6", "messages": [{"role": "user", "content": "hi"}]},
                headers=AUTH)
    rows = client.app.state.ctx.db.query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 1")
    assert rows
    assert rows[0]["model"] == "glm-4.6"
    assert rows[0]["total_tokens"] == 10


# ---- responses ----


def test_responses_non_stream(client: TestClient):
    resp = client.post("/v1/responses",
                       json={"model": "glm-4.6", "input": "hi"}, headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "response"
    assert body["status"] == "completed"
    assert body["output"][0]["content"][0]["text"] == "hello world"


def test_responses_stream(client: TestClient):
    resp = client.post("/v1/responses",
                       json={"model": "glm-4.6", "input": "hi", "stream": True}, headers=AUTH)
    assert resp.status_code == 200
    assert "response.created" in resp.text
    assert "response.completed" in resp.text


# ---- messages ----


def test_messages_non_stream(client: TestClient):
    resp = client.post("/v1/messages",
                       json={"model": "glm-4.6", "max_tokens": 100,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    body = resp.json()
    assert body["type"] == "message"
    assert body["content"][0]["text"] == "hello world"
    assert body["stop_reason"] == "end_turn"


def test_messages_stream(client: TestClient):
    resp = client.post("/v1/messages",
                       json={"model": "glm-4.6", "stream": True, "max_tokens": 10,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers=AUTH)
    assert resp.status_code == 200
    assert "event: message_start" in resp.text
    assert "event: message_stop" in resp.text


def test_messages_x_api_key_header(client: TestClient):
    resp = client.post("/v1/messages",
                       json={"model": "glm-4.6", "max_tokens": 10,
                             "messages": [{"role": "user", "content": "hi"}]},
                       headers={"x-api-key": "sk-kp-test-key-123456"})
    assert resp.status_code == 200


# ---- models ----


def test_models_listing(client: TestClient):
    resp = client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data[0]["id"] == "glm-4.6"
    assert data[0]["context_window"] == 200000
    assert data[0]["reasoning_supported_efforts"] == ["low", "high"]


def test_models_requires_key(client: TestClient):
    """回归：/v1/models 未鉴权必须返回 401 而不是 500。"""
    assert client.get("/v1/models").status_code == 401


def test_models_bad_key(client: TestClient):
    resp = client.get("/v1/models", headers={"Authorization": "Bearer nope"})
    assert resp.status_code == 401


# ---- 域前缀路由 ----


def test_realm_prefix_routes_to_zai(client: TestClient):
    """`zai:` 前缀应改写 realm 与裸模型名，且请求仍成功。"""
    ctx = client.app.state.ctx
    from keyport.upstream.base import AccountAuth

    ctx.pool.upsert_account(AccountAuth(
        uid="zai1", realm="zai", access_token="ztok", expires_at=0,
    ))
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "zai:glm-4.6", "messages": [{"role": "user", "content": "hi"}]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    rows = ctx.db.query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 1")
    assert rows[0]["realm"] == "zai"
    assert rows[0]["model"] == "glm-4.6"


def test_realm_isolation_blocks_when_no_account_in_realm(client: TestClient):
    """严格隔离串号：目标域无账号时必须失败，而不是回落用其他域的账号。"""
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "zai:glm-4.6", "messages": [{"role": "user", "content": "hi"}]},
        headers=AUTH,
    )
    assert resp.status_code == 503


# ---- 配额 ----


def test_key_credit_quota_blocks(client: TestClient):
    ctx = client.app.state.ctx
    ctx.db.execute("UPDATE api_keys SET quota_credits=1, used_credits=5 WHERE id=1")
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6", "messages": [{"role": "user", "content": "x"}]},
                       headers=AUTH)
    assert resp.status_code == 429


def test_key_token_quota_blocks(client: TestClient):
    ctx = client.app.state.ctx
    ctx.db.execute("UPDATE api_keys SET quota_tokens=1, used_tokens=5 WHERE id=1")
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6", "messages": [{"role": "user", "content": "x"}]},
                       headers=AUTH)
    assert resp.status_code == 429


def test_disabled_key_rejected(client: TestClient):
    ctx = client.app.state.ctx
    ctx.db.execute("UPDATE api_keys SET enabled=0 WHERE id=1")
    resp = client.post("/v1/chat/completions",
                       json={"model": "glm-4.6", "messages": [{"role": "user", "content": "x"}]},
                       headers=AUTH)
    assert resp.status_code == 401


# ---- 全局 IP 管控 ----
# TestClient 的对端 host 是 "testclient"（非合法 IP），因此必须开启代理头信任
# 并显式提供 X-Forwarded-For，才能走到真实的 IP 判定分支。


def test_global_blocklist_rejects(client: TestClient):
    ctx = client.app.state.ctx
    ctx.cfg.security.trust_proxy_headers = True
    ctx.cfg.security.global_blocklist = ["203.0.113.0/24"]
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "glm-4.6", "messages": [{"role": "user", "content": "x"}]},
        headers={**AUTH, "X-Forwarded-For": "203.0.113.9"},
    )
    assert resp.status_code == 403


def test_global_allowlist_rejects_others(client: TestClient):
    ctx = client.app.state.ctx
    ctx.cfg.security.trust_proxy_headers = True
    ctx.cfg.security.global_allowlist = ["10.0.0.0/8"]
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "glm-4.6", "messages": [{"role": "user", "content": "x"}]},
        headers={**AUTH, "X-Forwarded-For": "203.0.113.9"},
    )
    assert resp.status_code == 403


def test_global_allowlist_permits_listed_ip(client: TestClient):
    ctx = client.app.state.ctx
    ctx.cfg.security.trust_proxy_headers = True
    ctx.cfg.security.global_allowlist = ["10.0.0.0/8"]
    resp = client.post(
        "/v1/chat/completions",
        json={"model": "glm-4.6", "messages": [{"role": "user", "content": "x"}]},
        headers={**AUTH, "X-Forwarded-For": "10.1.2.3"},
    )
    assert resp.status_code == 200


# ---- 记账回归：三种协议的流式与非流式都必须落库，且每次请求恰好一条 ----
# （历史 bug：messages / responses 的流式路径漏调 finish()，导致用量与日志丢失）


@pytest.mark.parametrize(
    "path,payload",
    [
        ("/v1/chat/completions",
         {"model": "glm-4.6", "messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/messages",
         {"model": "glm-4.6", "max_tokens": 16,
          "messages": [{"role": "user", "content": "hi"}]}),
        ("/v1/responses", {"model": "glm-4.6", "input": "hi"}),
    ],
)
@pytest.mark.parametrize("stream", [False, True])
def test_every_protocol_is_logged_exactly_once(client: TestClient, path, payload, stream):
    ctx = client.app.state.ctx
    ctx.db.execute("DELETE FROM request_logs")

    resp = client.post(path, json={**payload, "stream": stream}, headers=AUTH)
    assert resp.status_code == 200

    rows = ctx.db.query("SELECT * FROM request_logs ORDER BY id")
    assert len(rows) == 1, f"{path} stream={stream} 应恰好记录 1 条，实际 {len(rows)}"
    assert rows[0]["status"] == 200
    assert rows[0]["total_tokens"] == 10


def test_streaming_records_usage_tokens(client: TestClient):
    """流式路径的 usage 必须从上游 chunk 里捕获并记账。"""
    ctx = client.app.state.ctx
    ctx.db.execute("DELETE FROM request_logs")
    resp = client.post(
        "/v1/messages",
        json={"model": "glm-4.6", "stream": True, "max_tokens": 16,
              "messages": [{"role": "user", "content": "hi"}]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    row = ctx.db.query("SELECT * FROM request_logs ORDER BY id DESC LIMIT 1")[0]
    assert row["prompt_tokens"] == 4
    assert row["completion_tokens"] == 6
    assert row["total_tokens"] == 10
