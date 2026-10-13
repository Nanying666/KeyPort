"""zcode.z.ai 请求形态：官方头组 + 官方 system 身份块 + 一次性验证码参数。

依据实测（见 keyport/upstream/captcha.py 模块文档）：
  - 缺 verifyParam          → 400 / 3007
  - 复用同一个 verifyParam  → 第二次起 3007
  - 缺 ZCode 身份块         → 3012
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app
from keyport.config import CaptchaConfig
from keyport.security import hash_api_key
from keyport.upstream.base import AccountAuth

ZAI_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
AUTH = {"Authorization": "Bearer sk-kp-shape"}
MSG = {"model": "glm-5.3-flash", "messages": [{"role": "user", "content": "hi"}]}


def _sse(text: str) -> str:
    payload = {"type": "content_block_delta", "index": 0,
               "delta": {"type": "text_delta", "text": text}}
    return f"event: content_block_delta\ndata: {json.dumps(payload)}\n\n"


class _FakeCaptcha:
    """按序发放不同参数，并记录发放次序（用于断言不复用）。"""

    def __init__(self) -> None:
        self.issued: list[str] = []
        self.cfg = CaptchaConfig(region="cn")

    async def acquire(self, jwt: str = "") -> str | None:
        param = f"param-{len(self.issued) + 1}"
        self.issued.append(param)
        return param


@pytest.fixture()
def captured() -> list[dict]:
    return []


@pytest.fixture()
def fake_captcha() -> _FakeCaptcha:
    return _FakeCaptcha()


@pytest.fixture()
def client(tmp_path: Path, captured: list[dict], fake_captcha: _FakeCaptcha) -> TestClient:
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.client.captcha = fake_captcha

    ctx.pool.upsert_account(AccountAuth(
        uid="zcode-uid", realm="zai", nickname="iyzcfc",
        access_token="CODING_PLAN_JWT", kind="apikey",
        device_token="dev-mid-1", base_url=ZAI_BASE, protocol="anthropic",
    ))

    raw = "sk-kp-shape"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("shape", hash_api_key(raw), raw[:12]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append({"body": json.loads(request.content),
                         "headers": dict(request.headers)})
        return httpx.Response(200, text=_sse("OK"))

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


def _hdrs(captured: list[dict], i: int = 0) -> dict[str, str]:
    return {k.lower(): v for k, v in captured[i]["headers"].items()}



def test_zcode_plan_sends_official_headers(client: TestClient, captured: list[dict]):
    client.post("/v1/chat/completions", json=MSG, headers=AUTH)
    h = _hdrs(captured)
    assert h["http-referer"] == "https://zcode.z.ai"
    assert h["x-zcode-app-version"]
    assert h["x-zcode-agent"] == "glm"
    assert h["user-agent"].startswith("ZCode/")
    assert "ai-sdk/anthropic/" in h["user-agent"]
    assert h["x-api-key"]       # 与 authorization 同时携带
    assert h["x-device-mid"] == "dev-mid-1"


def test_zcode_plan_injects_captcha_param(client: TestClient, captured: list[dict]):
    client.post("/v1/chat/completions", json=MSG, headers=AUTH)
    h = _hdrs(captured)
    assert h["x-aliyun-captcha-verify-param"]
    assert h["x-aliyun-captcha-verify-region"] == "cn"


def test_each_request_gets_fresh_param(client: TestClient, captured: list[dict],
                                       fake_captcha: _FakeCaptcha):
    """verifyParam 一次性：每次请求都必须取新参数，不能复用。"""
    for _ in range(3):
        client.post("/v1/chat/completions", json=MSG, headers=AUTH)
    params = [_hdrs(captured, i)["x-aliyun-captcha-verify-param"]
              for i in range(3)]
    assert len(set(params)) == 3, f"参数被复用: {params}"
    assert len(set(fake_captcha.issued)) == 3


def test_zcode_plan_system_has_identity_blocks(client: TestClient, captured: list[dict]):
    """缺身份块会被上游判 3012。"""
    client.post("/v1/chat/completions", json=MSG, headers=AUTH)
    system = captured[0]["body"]["system"]
    texts = [b["text"] for b in system]
    assert any("You are ZCode" in t for t in texts)
    assert any("# Environment" in t for t in texts)
    # 官方形态：前三块都带 ephemeral cache_control
    assert all(b.get("cache_control") == {"type": "ephemeral"} for b in system[:3])


def test_first_user_message_gets_currentdate_context(client: TestClient,
                                                     captured: list[dict]):
    client.post("/v1/chat/completions", json=MSG, headers=AUTH)
    first = captured[0]["body"]["messages"][0]
    assert first["role"] == "user"
    texts = [c["text"] for c in first["content"]]
    assert any(t.startswith("<system-reminder>") for t in texts)
    assert "hi" in texts


def test_model_name_is_lowercased(client: TestClient, captured: list[dict]):
    """官方客户端发小写模型名。"""
    client.post("/v1/chat/completions",
                json={"model": "GLM-5.3-Flash",
                      "messages": [{"role": "user", "content": "hi"}]},
                headers=AUTH)
    assert captured[0]["body"]["model"] == "glm-5.3-flash"


def test_non_zcode_plan_endpoint_untouched(tmp_path: Path):
    """非 zcode-plan 端点不应被注入身份块 / 验证码头。"""
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(
        uid="zai-api", realm="zai", access_token="KEY", kind="apikey",
        base_url="https://api.z.ai/api/anthropic", protocol="anthropic",
    ))
    raw = "sk-kp-other"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("other", hash_api_key(raw), raw[:12]),
    )
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append({"body": json.loads(request.content),
                     "headers": dict(request.headers)})
        return httpx.Response(200, text=_sse("OK"))

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions", json=MSG,
                       headers={"Authorization": f"Bearer {raw}"})
        assert resp.status_code == 200
    assert "x-aliyun-captcha-verify-param" not in {k.lower() for k in seen[0]["headers"]}
    assert not any("You are ZCode" in str(b) for b in seen[0]["body"].get("system", []))
