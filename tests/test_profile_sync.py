"""账号资料昵称同步。

移植自 misakano7545/workbuddy2api-panel 的 `internal/upstream/profile.go`。
重点验证**隐私边界**与**防串号**两条纪律。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_test_config

from keyport.upstream.base import AccountAuth, ErrorKind, UpstreamError
from keyport.upstream.client import UpstreamClient


def _client(tmp_path: Path, handler) -> UpstreamClient:
    cfg = make_test_config(tmp_path)
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock  # type: ignore[method-assign]
    return client


def _auth(**kw) -> AccountAuth:
    base = {"uid": "u-1", "realm": "cn", "access_token": "T"}
    base.update(kw)
    return AccountAuth(**base)


# ---- 正常路径 ----


@pytest.mark.asyncio
async def test_fetch_profile_returns_nickname(tmp_path: Path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"data": {"uid": "u-1", "nickname": "新昵称"}})

    c = _client(tmp_path, handler)
    assert await c.fetch_account_profile(_auth()) == "新昵称"
    assert seen["url"].endswith("/console/account")
    # Web 指纹：上游按它判定调用来源
    assert seen["headers"]["x-client-platform"] == "web"


@pytest.mark.asyncio
async def test_fetch_profile_uses_web_fingerprint(tmp_path: Path):
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"data": {"nickname": "n"}})

    c = _client(tmp_path, handler)
    await c.fetch_account_profile(_auth())
    h = seen["headers"]
    assert h["x-client-platform"] == "web"
    assert h["origin"]
    assert "profile/account-settings" in h["referer"]
    assert h["authorization"] == "Bearer T"


@pytest.mark.asyncio
async def test_fetch_profile_accepts_top_level_shape(tmp_path: Path):
    """上游信封可能是 {data:{...}} 或直接把字段放顶层。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"uid": "u-1", "nickname": "顶层"})

    c = _client(tmp_path, handler)
    assert await c.fetch_account_profile(_auth()) == "顶层"


# ---- 隐私边界 ----


@pytest.mark.asyncio
async def test_sensitive_fields_are_dropped(tmp_path: Path):
    """响应含手机号等敏感信息 —— 只解析昵称，其余字段一概不进入返回路径。

    本方法签名只返回 str，调用方拿不到任何其它字段。
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {
            "uid": "u-1", "nickname": "nick",
            "phoneNumber": "13800138000", "email": "a@b.c", "idCard": "xxx",
        }})

    c = _client(tmp_path, handler)
    got = await c.fetch_account_profile(_auth())
    assert got == "nick"
    assert "13800138000" not in got


# ---- 防串号 ----


@pytest.mark.asyncio
async def test_uid_mismatch_raises(tmp_path: Path):
    """uid 与凭证不一致 → 报错（说明拿到了别人的资料）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"uid": "someone-else", "nickname": "x"}})

    c = _client(tmp_path, handler)
    with pytest.raises(UpstreamError, match="uid mismatch"):
        await c.fetch_account_profile(_auth(uid="u-1"))


@pytest.mark.asyncio
async def test_missing_uid_is_tolerated(tmp_path: Path):
    """上游没给 uid 时不做核对（不编造结论）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"nickname": "x"}})

    c = _client(tmp_path, handler)
    assert await c.fetch_account_profile(_auth()) == "x"


# ---- 错误路径 ----


@pytest.mark.asyncio
async def test_http_error_raises(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text='{"code":403,"msg":"forbidden"}')

    c = _client(tmp_path, handler)
    with pytest.raises(UpstreamError) as exc:
        await c.fetch_account_profile(_auth())
    assert exc.value.status == 403


@pytest.mark.asyncio
async def test_network_error_raises_network_kind(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    c = _client(tmp_path, handler)
    with pytest.raises(UpstreamError) as exc:
        await c.fetch_account_profile(_auth())
    assert exc.value.kind == ErrorKind.NETWORK


@pytest.mark.asyncio
async def test_non_json_body_raises(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>not json</html>")

    c = _client(tmp_path, handler)
    with pytest.raises(UpstreamError):
        await c.fetch_account_profile(_auth())


@pytest.mark.asyncio
async def test_empty_nickname_returns_empty_string(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"uid": "u-1", "nickname": ""}})

    c = _client(tmp_path, handler)
    assert await c.fetch_account_profile(_auth()) == ""


# ---- 池侧与端点 ----


def test_pool_set_nickname(tmp_path: Path):
    from keyport.app import build_context

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T"))
    assert ctx.pool.set_nickname("u1", "改名后") is True
    assert ctx.pool.get("u1").nickname == "改名后"
    # 落库（重启不丢）
    row = ctx.db.query_one("SELECT nickname FROM accounts WHERE uid=?", ("u1",))
    assert row["nickname"] == "改名后"
    assert ctx.pool.set_nickname("nope", "x") is False


def test_sync_endpoint_updates_nicknames(tmp_path: Path, caplog):
    import re

    from fastapi.testclient import TestClient

    from keyport.app import create_app
    from keyport.security import hash_api_key

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T",
                                       nickname="旧名"))
    key = "sk-kp-nick"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("n", hash_api_key(key), key[:12]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/console/account"):
            return httpx.Response(200, json={"data": {"uid": "u1", "nickname": "新名"}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]

    with TestClient(app) as tc:
        logs = "\n".join(r.getMessage() for r in caplog.records)
        match = re.search(r"面板初始密码[^：]*：(\S+)", logs)
        assert match, "未取到初始密码"
        login = tc.post("/api/login",
                        json={"username": "admin", "password": match.group(1)})
        token = login.cookies.get("kp_session")
        assert token, login.text

        resp = tc.post("/api/accounts/sync-nicknames", headers={"x-kp-session": token})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["updated"] == [{"uid": "u1", "nickname": "新名"}]
        assert ctx.pool.get("u1").nickname == "新名"
