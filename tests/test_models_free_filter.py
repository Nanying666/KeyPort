"""模型倍率解析与「只暴露免费模型」过滤。

背景：WorkBuddy 国际版模型目录含大量付费模型（元数据 `credits` 为 `x3.31 credits`
等倍率字符串），下游 /v1/models 只需看到 0 积分（`x0.00`）的免费模型。
KeyPort 原先完全丢弃该字段，无法区分免费/付费。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import make_test_config
from keyport.upstream.base import AccountAuth
from keyport.upstream.client import UpstreamClient, _parse_credit_factor

# 上游真实形态（workbuddy.ai /v2/enterprises/personal/models）
MODELS_BODY = {
    "code": 0,
    "msg": "OK",
    "data": {
        "models": [
            {"id": "hy3", "name": "Hy3", "credits": "x0.00", "maxOutputTokens": 64000},
            {"id": "hy4-preview", "name": "Hy4 preview", "credits": "x0.00"},
            {"id": "glm-5.3", "name": "GLM-5.3", "credits": "x0.79"},
            {"id": "primary-model", "name": "Primary", "credits": "x3.31 credits"},
            {"id": "no-factor", "name": "NoFactor"},  # 上游未给倍率
        ]
    },
}


def test_parse_credit_factor_forms():
    assert _parse_credit_factor("x0.79 credits") == pytest.approx(0.79)
    assert _parse_credit_factor("x0.00") == 0.0
    assert _parse_credit_factor("x3.31") == pytest.approx(3.31)
    assert _parse_credit_factor("x0.14") == pytest.approx(0.14)
    assert _parse_credit_factor(0.5) == pytest.approx(0.5)


def test_parse_credit_factor_unknown_returns_none():
    """无法解析时返回 None —— 白名单过滤下会被剔除（未知 ≠ 免费）。"""
    for raw in (None, "", "  ", "abc", [], {}):
        assert _parse_credit_factor(raw) is None


def test_filter_excludes_missing_factor(tmp_path: Path):
    """`default-model`（credits 空串，档位别名）必须被过滤掉。"""
    from keyport.api.models import ModelRegistry

    cfg = make_test_config(tmp_path)
    cfg.realms["global"].models_free_only = True
    registry = ModelRegistry(cfg, None, None)  # type: ignore[arg-type]
    models = [
        {"id": "default-model"},                      # 无倍率 → 剔除
        {"id": "hy3", "credit_factor": 0.0},          # 免费 → 保留
        {"id": "glm-5.3", "credit_factor": 0.79},     # 付费 → 剔除
    ]
    assert [m["id"] for m in registry._filter_free_only("global", models)] == ["hy3"]


def _client_with_transport(cfg, handler) -> UpstreamClient:
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock  # type: ignore[method-assign]
    return client


async def test_normalize_models_keeps_credit_factor(tmp_path: Path):
    cfg = make_test_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MODELS_BODY)

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="g1", realm="global", access_token="T", kind="oauth")
    models = await client.fetch_models(auth)
    await client.aclose()

    by_id = {m["id"]: m for m in models}
    assert by_id["hy3"]["credit_factor"] == 0.0
    assert by_id["glm-5.3"]["credit_factor"] == pytest.approx(0.79)
    assert by_id["primary-model"]["credit_factor"] == pytest.approx(3.31)
    # 上游未给倍率 → 不带该字段（未知，不是 0）
    assert "credit_factor" not in by_id["no-factor"]


async def _registry_with_models(tmp_path: Path, *, free_only: bool):
    """构造带 global 账号的真实上下文（mock 上游返回 MODELS_BODY）。"""
    from keyport.app import build_context

    cfg = make_test_config(tmp_path)
    cfg.realms["global"].models_free_only = free_only

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=MODELS_BODY)

    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="T", kind="oauth"))

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    models = await ctx.models._models_for_realm("global", refresh=True)
    await ctx.client.aclose()
    return models


async def test_registry_filters_free_only_for_global(tmp_path: Path):
    """global 配 models_free_only → 只留明确 0 倍率的模型。

    倍率缺失（`default-model` 这类档位别名）不保留 —— 未知不等于免费。
    """
    models = await _registry_with_models(tmp_path, free_only=True)
    ids = sorted(m["id"] for m in models)
    assert ids == ["hy3", "hy4-preview"]


async def test_registry_keeps_all_when_flag_off(tmp_path: Path):
    models = await _registry_with_models(tmp_path, free_only=False)
    assert len(models) == 5
