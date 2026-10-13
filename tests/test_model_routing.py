"""无域前缀请求按「模型归属域」路由到拥有该模型的账号。

背景：z.ai 的 token 额度（数亿）远大于 workbuddy 积分（数百），三因子权重
（credits_ratio × 10）会让 z.ai 抢走所有请求，包括它并不提供的 workbuddy
免费模型（hy3 / deepseek-v4.1-flash 等），上游因模型不存在而报错。
修复：模型只在一个域提供时，无前缀请求限定该域选号。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import make_test_config
from keyport.app import build_context
from keyport.upstream.base import AccountAuth


@pytest.fixture()
def ctx(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    c = build_context(cfg)
    c.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="G", kind="oauth"))
    # z.ai 走 Coding Plan 权益派生（base_url 含 /zcode-plan）
    c.pool.upsert_account(AccountAuth(
        uid="z1", realm="zai", access_token="Z", kind="apikey",
        base_url="https://zcode.z.ai/api/v1/zcode-plan/anthropic",
        protocol="anthropic"))

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "billing/balance" in url:  # z.ai 权益派生：提供 glm-5.3
            return httpx.Response(200, json={"code": 0, "data": {"plans": [
                {"status": "active", "entitlements": [
                    {"show_name": "GLM-5.3", "capabilities": ["model:glm-5.3"]}]}]}})
        if url.endswith("/v3/config"):  # global：提供 hy3
            return httpx.Response(200, json={"data": {"models": [
                {"id": "hy3", "credits": "x0.00"}]}})
        return httpx.Response(404, json={"code": 404})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    c.client.client = _mock  # type: ignore[method-assign]
    yield c


async def test_realm_for_model_unique_owner(ctx):
    await ctx.models.list_models("", refresh=True)
    assert ctx.gateway.realm_for_model("glm-5.3") == "zai"
    assert ctx.gateway.realm_for_model("hy3") == "global"


async def test_realm_for_model_unknown_is_empty(ctx):
    """未探测到的模型不猜归属（保持不限域）。"""
    await ctx.models.list_models("", refresh=True)
    assert ctx.gateway.realm_for_model("gpt-5.5") == ""


def test_realm_for_model_disabled_returns_empty(tmp_path: Path):
    """关闭 route_by_model 后不再按模型限定域。"""
    cfg = make_test_config(tmp_path)
    cfg.pool.route_by_model = False
    c = build_context(cfg)
    c.models._cache["zai"] = (0.0, [{"id": "glm-5.3"}])
    assert c.gateway.realm_for_model("glm-5.3") == ""


def test_realm_for_model_no_registry_returns_empty():
    """未装配 ModelRegistry 时安全降级（不抛异常）。"""
    cfg = make_test_config(Path("./tmp-nonexistent"))  # noqa: F841
    from keyport.api.gateway import Gateway

    gw = Gateway(cfg, None, None, None, None, None)  # type: ignore[arg-type]
    assert gw.realm_for_model("glm-5.3") == ""


async def test_selection_limited_to_owner_realm(ctx):
    """按归属域选号：hy3 只可能落到 global 账号。"""
    await ctx.models.list_models("", refresh=True)
    for _ in range(10):
        e = ctx.pool.pick(realm=ctx.gateway.realm_for_model("hy3"),
                          model="hy3", conversation_id="", tried=set())
        assert e is not None and e.realm == "global"

    for _ in range(10):
        e = ctx.pool.pick(realm=ctx.gateway.realm_for_model("glm-5.3"),
                          model="glm-5.3", conversation_id="", tried=set())
        assert e is not None and e.realm == "zai"


def test_realm_for_model_cross_realm_duplicate_returns_none(tmp_path: Path):
    """跨域重名模型不强制路由（无法判定归属）。"""
    cfg = make_test_config(tmp_path)
    c = build_context(cfg)
    c.models._cache["zai"] = (0.0, [{"id": "shared-model"}])
    c.models._cache["global"] = (0.0, [{"id": "shared-model"}])
    assert c.models.realm_for_model("shared-model") is None
