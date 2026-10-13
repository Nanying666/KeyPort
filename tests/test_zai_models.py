"""z.ai 模型目录测试：Coding Plan 账号从套餐权益派生可用模型。

背景：Coding Plan 账号（base_url 指向 zcode.z.ai 网关）没有独立模型目录端点，
可用模型由套餐权益决定。原先实现用 JWT 打 api.z.ai/api/v1/models，恒定返回空，
导致「导入 z.ai 账号后 /v1/models 无输出」。
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
from keyport.upstream.client import (
    UpstreamClient,
    _models_from_entitlements,
    _zcode_plan_billing_url,
)

ZAI_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
BALANCE_URL = "https://zcode.z.ai/api/v1/zcode-plan/billing/balance"


def _balance_body() -> dict:
    """真实 billing/balance 响应形状（含 active / 非 active 套餐）。"""
    return {
        "code": 0,
        "msg": "",
        "data": {
            "server_time": 1790497426,
            "plans": [
                {
                    "user_plan_id": "upl_1",
                    "plan_id": "zcode-v3-start-plan-0927",
                    "name": "ZCode Global Build",
                    "status": "active",
                    "entitlements": [
                        {
                            "entitlement_id": "ent-1",
                            "show_name": "GLM-5.3-Flash",
                            "meter": "model_usage",
                            "unit_type": "token",
                            "capabilities": ["model:glm-5.3-flash"],
                            "grant_units": 100000000,
                            "period": "one_time",
                        }
                    ],
                },
                {
                    "user_plan_id": "upl_2",
                    "plan_id": "zcode-v3-start-plan-0817",
                    "name": "ZCode Start Plan",
                    "status": "active",
                    "entitlements": [
                        {
                            "entitlement_id": "ent-2",
                            "show_name": "GLM-5.3",
                            "meter": "model_usage",
                            "unit_type": "token",
                            "capabilities": ["model:glm-5.3"],
                            "grant_units": 3000000,
                            "period": "daily",
                        }
                    ],
                },
                {
                    "user_plan_id": "upl_3",
                    "plan_id": "expired-plan",
                    "name": "Expired",
                    "status": "expired",
                    "entitlements": [
                        {
                            "entitlement_id": "ent-3",
                            "show_name": "GLM-4.6",
                            "capabilities": ["model:glm-4.6"],
                        }
                    ],
                },
            ],
        },
    }


# ---- 纯函数：URL 推导 ----

def test_zcode_plan_billing_url_derived_from_base():
    auth = AccountAuth(uid="u", realm="zai", base_url=ZAI_BASE)
    assert _zcode_plan_billing_url(auth) == BALANCE_URL


def test_zcode_plan_billing_url_empty_for_non_coding_plan():
    """非 Coding Plan 端点不含 /zcode-plan 标记，返回空串（调用方跳过）。"""
    auth = AccountAuth(uid="u", realm="zai",
                       base_url="https://api.z.ai/api/anthropic")
    assert _zcode_plan_billing_url(auth) == ""
    assert _zcode_plan_billing_url(AccountAuth(uid="u", realm="zai")) == ""


# ---- 纯函数：权益解析 ----

def test_models_from_entitlements_only_active():
    models = _models_from_entitlements(_balance_body())
    ids = sorted(m["id"] for m in models)
    assert ids == ["glm-5.3", "glm-5.3-flash"]
    # 非 active 套餐的 glm-4.6 不应出现
    assert "glm-4.6" not in ids


def test_models_from_entitlements_keeps_display_name():
    models = {m["id"]: m for m in _models_from_entitlements(_balance_body())}
    assert models["glm-5.3-flash"]["display_name"] == "GLM-5.3-Flash"
    assert models["glm-5.3"]["display_name"] == "GLM-5.3"


def test_models_from_entitlements_tolerates_malformed():
    assert _models_from_entitlements({}) == []
    assert _models_from_entitlements({"data": {"plans": "oops"}}) == []
    assert _models_from_entitlements(
        {"data": {"plans": [{"status": "active", "entitlements": "x"}]}}
    ) == []


def test_models_from_entitlements_ignores_non_model_capabilities():
    """capabilities 里非 model: 前缀的项应被忽略。"""
    out = _models_from_entitlements(
        {"data": {"plans": [{"status": "active", "entitlements": [
            {"capabilities": ["tool:web_search", "model:glm-5.3"]}]}]}}
    )
    assert out == [{"id": "glm-5.3"}]


def test_models_from_entitlements_no_display_name_when_absent():
    """上游没给 show_name 时不编造 display_name。"""
    out = _models_from_entitlements(
        {"data": {"plans": [{"status": "active", "entitlements": [
            {"capabilities": ["model:glm-5.3"]}]}]}}
    )
    assert out == [{"id": "glm-5.3"}]


# ---- 组件级：fetch_models 走权益端点 ----

def _client_with_transport(cfg, handler) -> UpstreamClient:
    client = UpstreamClient(cfg)

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock_client  # type: ignore[method-assign]
    return client


async def test_fetch_models_uses_entitlements_for_coding_plan(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json=_balance_body())

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="u", realm="zai", access_token="JWT", kind="apikey",
                       base_url=ZAI_BASE, protocol="anthropic")
    models = await client.fetch_models(auth)
    await client.aclose()

    assert [m["id"] for m in models] == ["glm-5.3-flash", "glm-5.3"]
    # 只打了权益端点，没打旧的 /v1/models
    assert seen == [BALANCE_URL]


async def test_fetch_models_falls_back_to_catalog_on_empty_entitlements(tmp_path: Path):
    """权益为空时回落到模型目录端点，不返回空。"""
    cfg = make_test_config(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        seen.append(url)
        if "billing/balance" in url:
            return httpx.Response(200, json={"code": 0, "data": {"plans": []}})
        return httpx.Response(200, json={"data": [
            {"id": "glm-4.6", "object": "model", "owned_by": "z-ai"}]})

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="u", realm="zai", access_token="KEY", kind="apikey",
                       base_url=ZAI_BASE, protocol="anthropic")
    models = await client.fetch_models(auth)
    await client.aclose()

    assert [m["id"] for m in models] == ["glm-4.6"]
    assert any("billing/balance" in u for u in seen)
    assert any(u.endswith("/v1/models") for u in seen)


async def test_fetch_models_tolerates_balance_failure(tmp_path: Path):
    """权益端点 5xx 时不抛异常，回落模型目录。"""
    cfg = make_test_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if "billing/balance" in str(request.url):
            return httpx.Response(503, text="upstream down")
        return httpx.Response(200, json={"data": [
            {"id": "glm-4.6", "object": "model", "owned_by": "z-ai"}]})

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="u", realm="zai", access_token="KEY", kind="apikey",
                       base_url=ZAI_BASE, protocol="anthropic")
    models = await client.fetch_models(auth)
    await client.aclose()

    assert [m["id"] for m in models] == ["glm-4.6"]


# ---- 端到端：/v1/models 路由 ----

@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx

    ctx.pool.upsert_account(AccountAuth(
        uid="zai-cp-1", realm="zai", nickname="iyzcfc",
        access_token="CODING_PLAN_JWT", kind="apikey",
        device_token="78b921b4-ef0e-47cf-a333-555a14baa2f1",
        base_url=ZAI_BASE, protocol="anthropic",
    ))

    raw = "sk-kp-zai-models"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("zai", hash_api_key(raw), raw[:12]),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if "billing/balance" in str(request.url):
            return httpx.Response(200, json=_balance_body())
        return httpx.Response(404, text="not found")

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


AUTH = {"Authorization": "Bearer sk-kp-zai-models"}


def test_v1_models_returns_entitled_models(client: TestClient):
    resp = client.get("/v1/models", headers=AUTH)
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    ids = sorted(m["id"] for m in data)
    assert ids == ["glm-5.3", "glm-5.3-flash"]
    by_id = {m["id"]: m for m in data}
    assert by_id["glm-5.3-flash"]["display_name"] == "GLM-5.3-Flash"
    assert by_id["glm-5.3-flash"]["owned_by"] == "zai"

