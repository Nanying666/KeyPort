"""z.ai 官方 CLI OAuth 协议（init → poll）与干净请求头规范。

关键点：
  - init 响应可能由服务端下发 `poll_token`，后续 poll 必须采用该值；
    仅当服务端未返回时回落自造 token（旧协议兼容）。
  - 请求头必须「干净」：只带 Authorization 与 Content-Type，不带设备伪装头，
    否则上游会对 OAuth 会话产生异常的设备/上下文绑定限制。
  - poll 的 `code=3004` 明确映射为「会话过期」，与「尚未登录」区分开。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.auth.oauth import ZAI_SESSION_EXPIRED_CODE, ZaiOAuthClient
from keyport.security import hash_password
from keyport.upstream.base import ErrorKind, UpstreamError


@pytest.fixture()
def patch_httpx(monkeypatch):
    """把 auth.oauth 的 httpx 换成 MockTransport 版 AsyncClient。"""
    import keyport.auth.oauth as mod

    real = mod.httpx

    def install(handler):
        class _Patched:
            def __init__(self, *_a, **_kw) -> None:
                self._inner = httpx.AsyncClient(transport=httpx.MockTransport(handler))

            async def __aenter__(self):
                return self._inner

            async def __aexit__(self, *exc) -> None:
                await self._inner.aclose()

        monkeypatch.setattr(
            mod, "httpx",
            type("m", (), {"AsyncClient": _Patched, "HTTPError": real.HTTPError}),
        )

    yield install
    monkeypatch.setattr(mod, "httpx", real)


# ---- init ----


async def test_init_returns_flow_and_uses_server_poll_token(patch_httpx):
    """服务端下发 poll_token 时必须采用它（新协议）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0, "data": {
            "flow_id": "flow-1", "authorize_url": "https://z.ai/auth?x=1",
            "poll_token": "server-token",
        }})

    patch_httpx(handler)
    flow = await ZaiOAuthClient().init()
    assert flow.flow_id == "flow-1"
    assert flow.authorize_url == "https://z.ai/auth?x=1"
    assert flow.poll_token == "server-token"
    assert flow.server_poll_token is True
    assert seen["path"].endswith("/oauth/cli/init")
    assert seen["body"] == {"provider": "zai"}


async def test_init_falls_back_to_self_token(patch_httpx):
    """服务端未下发 poll_token 时回落自造 token（旧协议兼容）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {
            "flow_id": "f", "authorize_url": "https://z.ai/auth",
        }})

    patch_httpx(handler)
    flow = await ZaiOAuthClient().init()
    assert flow.poll_token
    assert flow.server_poll_token is False


async def test_init_headers_are_clean(patch_httpx):
    """OAuth 请求头必须干净：不带任何设备/平台指纹头。

    那些头是业务通道（messages / billing）的伪装，用在 OAuth 会话上会让上游
    把会话与「某台设备」绑定，反而增加异常判定。
    """
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower() for k in request.headers}
        return httpx.Response(200, json={"code": 0, "data": {
            "flow_id": "f", "authorize_url": "https://z.ai/auth"}})

    patch_httpx(handler)
    await ZaiOAuthClient().init()
    assert "authorization" in seen["headers"]
    assert "content-type" in seen["headers"]
    # 不得携带任何设备/平台伪装头
    for banned in ("x-device-mid", "x-platform", "x-os-category", "x-os-version",
                   "x-title", "x-client-language", "x-client-timezone",
                   "x-release-channel", "http-referer", "x-zcode-agent"):
        assert banned not in seen["headers"], f"OAuth 请求不该带 {banned}"


async def test_init_raises_on_business_error(patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 4001, "msg": "rejected"})

    patch_httpx(handler)
    with pytest.raises(UpstreamError) as ei:
        await ZaiOAuthClient().init()
    assert "4001" in ei.value.message


async def test_init_raises_on_incomplete_payload(patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"flow_id": "f"}})

    patch_httpx(handler)
    with pytest.raises(UpstreamError):
        await ZaiOAuthClient().init()


# ---- poll ----


async def test_poll_uses_flow_poll_token(patch_httpx):
    """poll 必须用 init 拿到的 poll_token（而非自造值）。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init"):
            return httpx.Response(200, json={"code": 0, "data": {
                "flow_id": "f1", "authorize_url": "u", "poll_token": "srv-tok"}})
        seen.append(request.headers.get("authorization") or "")
        return httpx.Response(200, json={"code": 0, "data": {"access_token": "at"}})

    patch_httpx(handler)
    client = ZaiOAuthClient()
    flow = await client.init()
    await client.poll(flow)
    assert seen == ["Bearer srv-tok"]


async def test_poll_pending_raises_auth(patch_httpx):
    """尚未登录 → AUTH（调用方按「等待中」处理）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 1001, "msg": "pending"})

    patch_httpx(handler)
    from keyport.auth.oauth import ZaiCliFlow

    flow = ZaiCliFlow("f", "u", "t")
    with pytest.raises(UpstreamError) as ei:
        await ZaiOAuthClient().poll(flow)
    assert ei.value.kind == ErrorKind.AUTH
    assert ei.value.status != 401       # 不是「会话过期」


async def test_poll_session_expired_code_3004(patch_httpx):
    """code=3004 必须映射为会话过期（status 401），与「等待中」区分。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": ZAI_SESSION_EXPIRED_CODE, "msg": "expired"})

    patch_httpx(handler)
    from keyport.auth.oauth import ZaiCliFlow

    flow = ZaiCliFlow("f", "u", "t")
    with pytest.raises(UpstreamError) as ei:
        await ZaiOAuthClient().poll(flow)
    assert ei.value.status == 401
    assert "过期" in ei.value.message


async def test_poll_http_4xx_is_pending(patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, content=b"{}")

    patch_httpx(handler)
    from keyport.auth.oauth import ZaiCliFlow

    with pytest.raises(UpstreamError) as ei:
        await ZaiOAuthClient().poll(ZaiCliFlow("f", "u", "t"))
    assert ei.value.kind == ErrorKind.AUTH
    assert ei.value.status == 404


async def test_poll_5xx_is_network_error(patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b"down")

    patch_httpx(handler)
    from keyport.auth.oauth import ZaiCliFlow

    with pytest.raises(UpstreamError) as ei:
        await ZaiOAuthClient().poll(ZaiCliFlow("f", "u", "t"))
    assert ei.value.kind == ErrorKind.NETWORK


# ---- complete ----


async def test_complete_produces_anthropic_coding_plan_account(patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init"):
            return httpx.Response(200, json={"code": 0, "data": {
                "flow_id": "f1", "authorize_url": "u", "poll_token": "t"}})
        return httpx.Response(200, json={"code": 0, "data": {
            "access_token": "JWT-TOKEN", "refresh_token": "R",
            "expires_in": 3600, "user_id": "uid-7",
            "user": {"displayName": "昵称"}}})

    patch_httpx(handler)
    client = ZaiOAuthClient()
    flow = await client.init()
    auth = await client.complete(flow)
    assert auth.uid == "uid-7"
    assert auth.realm == "zai"
    assert auth.nickname == "昵称"
    assert auth.access_token == "JWT-TOKEN"
    assert auth.refresh_token == "R"
    assert auth.expires_at > 0
    # Coding Plan 走 Anthropic 原生协议
    assert auth.protocol == "anthropic"
    assert auth.base_url.endswith("/zcode-plan/anthropic")
    assert auth.is_anthropic() and auth.is_zai()


async def test_complete_uid_falls_back_to_token_digest(patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"access_token": "TOK"}})

    patch_httpx(handler)
    from keyport.auth.oauth import ZaiCliFlow

    auth = await ZaiOAuthClient().complete(ZaiCliFlow("f", "u", "t"))
    assert auth.uid.startswith("zai-")


# ---- 管理台集成 ----


@pytest.fixture()
def api(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    cfg.install.auto_on_add = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    with TestClient(app) as tc:
        resp = tc.post("/api/login",
                       json={"username": "admin", "password": "pw-12345678"})
        assert resp.status_code == 200
        tc.headers.update({"x-kp-session": tc.cookies.get("kp_session", "")})
        yield tc


def test_oauth_start_routes_zai_to_cli_protocol(api: TestClient, patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {
            "flow_id": "flow-z", "authorize_url": "https://z.ai/auth",
            "poll_token": "srv"}})

    patch_httpx(handler)
    resp = api.post("/api/oauth/start", json={"realm": "zai"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["protocol"] == "zai-cli"
    assert body["state"] == "flow-z"
    assert body["auth_url"] == "https://z.ai/auth"


def test_oauth_poll_expired_returns_410(api: TestClient, patch_httpx):
    """会话过期必须返回 410 并清掉登录态（而非让前端无限轮询 202）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init"):
            return httpx.Response(200, json={"code": 0, "data": {
                "flow_id": "f-exp", "authorize_url": "u", "poll_token": "t"}})
        return httpx.Response(200, json={"code": ZAI_SESSION_EXPIRED_CODE, "msg": "expired"})

    patch_httpx(handler)
    started = api.post("/api/oauth/start", json={"realm": "zai"}).json()
    resp = api.post("/api/oauth/poll", json={"state": started["state"]})
    assert resp.status_code == 410
    assert resp.json()["expired"] is True
    # 登录态已清理：再轮询应 404（要求重新发起）
    again = api.post("/api/oauth/poll", json={"state": started["state"]})
    assert again.status_code == 404


def test_oauth_poll_pending_returns_202(api: TestClient, patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init"):
            return httpx.Response(200, json={"code": 0, "data": {
                "flow_id": "f-pend", "authorize_url": "u", "poll_token": "t"}})
        return httpx.Response(200, json={"code": 1001, "msg": "pending"})

    patch_httpx(handler)
    started = api.post("/api/oauth/start", json={"realm": "zai"}).json()
    resp = api.post("/api/oauth/poll", json={"state": started["state"]})
    assert resp.status_code == 202
    assert resp.json()["pending"] is True


def test_oauth_poll_success_upserts_account(api: TestClient, patch_httpx):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/init"):
            return httpx.Response(200, json={"code": 0, "data": {
                "flow_id": "f-ok", "authorize_url": "u", "poll_token": "t"}})
        return httpx.Response(200, json={"code": 0, "data": {
            "access_token": "JWT", "user_id": "zai-uid"}})

    patch_httpx(handler)
    started = api.post("/api/oauth/start", json={"realm": "zai"}).json()
    resp = api.post("/api/oauth/poll", json={"state": started["state"]})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["uid"] == "zai-uid"
    assert body["realm"] == "zai"

    accounts = api.get("/api/accounts").json()["accounts"]
    acc = [a for a in accounts if a["uid"] == "zai-uid"][0]
    # z.ai 账号必须落成 Anthropic 原生协议 + 设备档案
    assert acc["fingerprint"]["device_mid"]
    assert acc["kind"] == "oauth"


def test_oauth_start_unknown_realm(api: TestClient):
    assert api.post("/api/oauth/start", json={"realm": "nope"}).status_code == 400
