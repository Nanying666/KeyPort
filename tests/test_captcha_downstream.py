"""上游 CAPTCHA 挑战在下游三协议路径上的一致表现。

背景：zcode.z.ai Coding Plan 网关对每个请求要求人机验证，返回
400 + `{"code":3007,"msg":"captcha verify failed"}`。KeyPort 无法代答，
应统一映射为 502 upstream_captcha 并给出可诊断提示，且不误罚账号。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app
from keyport.security import hash_api_key
from keyport.upstream.base import AccountAuth

CAPTCHA_BODY = ('{"code":3007,"msg":"captcha verify failed",'
                '"logid":"20260928002028e1991f398fc7e5005c57"}')
ZAI_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx

    ctx.pool.upsert_account(AccountAuth(
        uid="zai-captcha-1", realm="zai", nickname="iyzcfc",
        access_token="CODING_PLAN_JWT", kind="apikey",
        device_token="78b921b4-ef0e-47cf-a333-555a14baa2f1",
        base_url=ZAI_BASE, protocol="anthropic",
    ))

    raw = "sk-kp-captcha"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("captcha", hash_api_key(raw), raw[:12]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        # 上游以 400 + 业务码 3007 拒绝
        return httpx.Response(400, content=CAPTCHA_BODY.encode(),
                              headers={"content-type": "application/json"})

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


AUTH = {"Authorization": "Bearer sk-kp-captcha"}
MSG = {"model": "glm-5.3-flash", "max_tokens": 16,
       "messages": [{"role": "user", "content": "hi"}]}


def test_responses_returns_502_upstream_captcha(client: TestClient):
    resp = client.post("/v1/responses",
                       json={"model": "glm-5.3-flash", "input": "hi"}, headers=AUTH)
    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    assert err["type"] == "upstream_captcha"
    # 提示需可诊断，不能只透传 "captcha verify failed"
    assert "人机验证" in err["message"]


def test_chat_completions_returns_502_upstream_captcha(client: TestClient):
    resp = client.post("/v1/chat/completions", json=MSG, headers=AUTH)
    assert resp.status_code == 502, resp.text
    assert resp.json()["error"]["type"] == "upstream_captcha"


def test_messages_returns_502_upstream_captcha(client: TestClient):
    """Anthropic 风格错误体，状态码与 OpenAI 路径一致。"""
    resp = client.post("/v1/messages", json=MSG, headers=AUTH)
    assert resp.status_code == 502, resp.text
    err = resp.json()["error"]
    assert err["type"] == "upstream_captcha"
    assert "人机验证" in err["message"]


def test_captcha_does_not_penalise_account(client: TestClient):
    """端点级限制，非账号过错：不冷却、不熔断、不计连败、不摘除。"""
    for _ in range(5):
        client.post("/v1/chat/completions", json=MSG, headers=AUTH)
    entry = client.app.state.ctx.pool.get("zai-captcha-1")
    assert entry.cooldown_until == 0
    assert entry.breaker_until == 0
    assert entry.consecutive_failures == 0
    assert entry.failed_calls == 0
    assert entry.enabled is True
