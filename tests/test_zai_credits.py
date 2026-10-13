"""z.ai Coding Plan 额度测试：从 billing/balance 的 balances[] 派生剩余额度。

背景：原先 fetch_credits 对 zai 直接返回 {}，额度永远显示为空。
Coding Plan 账号的额度在 `/billing/balance` 的 `data.balances[]`（单位 token）。
"""

from __future__ import annotations

import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app
from keyport.security import hash_password
from keyport.upstream.base import AccountAuth
from keyport.upstream.client import (
    UpstreamClient,
    _normalize_zcode_plan_credits,
)

ZAI_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
BALANCE_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance"

NOW = int(time.time())


def _balance_body() -> dict:
    """真实 billing/balance 形状：三个权益桶（含 7 天内到期与非到期）。"""
    return {
        "code": 0,
        "data": {
            "server_time": NOW,
            "plans": [],
            "balances": [
                {
                    "bucket_id": "b1", "show_name": "GLM-5.3-Flash",
                    "total_units": 300000000, "used_units": 0,
                    "remaining_units": 300000000, "available_units": 300000000,
                    "expires_at": NOW + 3600,
                },
                {
                    "bucket_id": "b2", "show_name": "GLM-5.3",
                    "total_units": 3000000, "used_units": 1000000,
                    "remaining_units": 2000000, "available_units": 2000000,
                    "expires_at": NOW + 3600,
                },
                {
                    "bucket_id": "b3", "show_name": "GLM-5.3-Flash",
                    "total_units": 5000000, "used_units": 0,
                    "remaining_units": 5000000, "available_units": 5000000,
                    # 30 天后到期：不计入 expiring
                    "expires_at": NOW + 30 * 86400,
                },
            ],
        },
    }


# ---- 纯函数：额度归一化 ----

def test_normalize_sums_remaining_and_used():
    out = _normalize_zcode_plan_credits(_balance_body())
    assert out["credits"] == 307000000  # 300M + 2M + 5M
    assert out["used"] == 1000000


def test_normalize_expiring_only_within_window():
    """只有 7 天内到期的桶计入 expiring（b1+b2），b3 不计。"""
    out = _normalize_zcode_plan_credits(_balance_body())
    assert out["expiring"] == 302000000  # 300M + 2M


def test_normalize_tolerates_malformed():
    assert _normalize_zcode_plan_credits({}) == {}
    assert _normalize_zcode_plan_credits({"data": {"balances": "x"}}) == {}
    assert _normalize_zcode_plan_credits({"data": {"balances": []}}) == {}
    # 桶字段类型异常不应抛错
    assert _normalize_zcode_plan_credits(
        {"data": {"balances": [{"remaining_units": "bad", "used_units": None}]}}
    ) == {"credits": 0, "used": 0}


def test_normalize_falls_back_to_available_units():
    out = _normalize_zcode_plan_credits(
        {"data": {"balances": [{"available_units": 123}]}}
    )
    assert out["credits"] == 123


def test_normalize_handles_millisecond_expiry():
    """expires_at 为毫秒时间戳时也应正确判定窗口。"""
    out = _normalize_zcode_plan_credits(
        {"data": {"balances": [{"remaining_units": 50, "expires_at": (NOW + 3600) * 1000}]}}
    )
    assert out["expiring"] == 50


# ---- 组件级：fetch_credits 走 zcode-plan 端点 ----

def _client_with_transport(cfg, handler) -> UpstreamClient:
    client = UpstreamClient(cfg)

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock_client  # type: ignore[method-assign]
    return client


async def test_fetch_credits_uses_balance_for_coding_plan(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_balance_body())

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="u", realm="zai", access_token="JWT", kind="apikey",
                       base_url=ZAI_BASE, protocol="anthropic")
    credits = await client.fetch_credits(auth)
    await client.aclose()

    assert credits["credits"] == 307000000
    assert seen == [BALANCE_URL]


async def test_fetch_credits_empty_for_non_coding_plan_zai(tmp_path: Path):
    """非 Coding Plan 的 zai 账号没有额度端点，返回空且不发请求。"""
    cfg = make_test_config(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={})

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="u", realm="zai", access_token="KEY", kind="apikey",
                       base_url="https://api.z.ai/api/anthropic")
    credits = await client.fetch_credits(auth)
    await client.aclose()

    assert credits == {}
    assert seen == []


async def test_fetch_credits_raises_on_upstream_error(tmp_path: Path):
    """上游 4xx/5xx 应抛 UpstreamError，由调用方决定处置。"""
    from keyport.upstream.base import UpstreamError

    cfg = make_test_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": {"message": "token expired"}})

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="u", realm="zai", access_token="BAD", kind="apikey",
                       base_url=ZAI_BASE, protocol="anthropic")
    with pytest.raises(UpstreamError):
        await client.fetch_credits(auth)
    await client.aclose()


# ---- 端到端：/api/billing/refresh 覆盖 zai apikey 账号 ----

@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("secret-password")
    app = create_app(cfg)
    ctx = app.state.ctx

    # zai Coding Plan 账号（kind=apikey，但额度在 zcode-plan 端点）
    ctx.pool.upsert_account(AccountAuth(
        uid="zai-cp-1", realm="zai", nickname="iyzcfc",
        access_token="CODING_PLAN_JWT", kind="apikey",
        base_url=ZAI_BASE, protocol="anthropic",
    ))
    # 普通 cn oauth 账号：走原 get-user-resource 端点
    ctx.pool.upsert_account(AccountAuth(
        uid="cn-1", realm="cn", nickname="alice", access_token="tok",
        refresh_token="r", expires_at=0,
    ))
    # 普通 cn apikey 账号：无额度端点，应被跳过
    ctx.pool.upsert_account(AccountAuth(
        uid="cn-key", realm="cn", nickname="keyacct", access_token="sk-x",
        kind="apikey",
    ))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "billing/balance" in url:
            return httpx.Response(200, json=_balance_body())
        if "get-user-resource" in url:
            return httpx.Response(200, json={"code": 0, "data": {"credits": 500}})
        return httpx.Response(404, json={"code": 404})

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


def _login(client: TestClient) -> dict:
    resp = client.post("/api/login", json={"username": "admin", "password": "secret-password"})
    assert resp.status_code == 200, resp.text
    return {"x-kp-session": client.cookies.get("kp_session", "")}


def test_billing_refresh_includes_zai_apikey(client: TestClient):
    headers = _login(client)
    resp = client.post("/api/billing/refresh", headers=headers)
    assert resp.status_code == 200, resp.text
    uids = {r["uid"] for r in resp.json()["results"]}
    # zai apikey 与 cn oauth 都刷新；cn 普通 apikey 被跳过
    assert "zai-cp-1" in uids
    assert "cn-1" in uids
    assert "cn-key" not in uids

    entry = client.app.state.ctx.pool.get("zai-cp-1")
    assert entry.credits == 307000000
