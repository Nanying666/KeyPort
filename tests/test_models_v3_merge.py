"""v3/config + 企业端点合并，以及非对话模型剔除。

背景：`deepseek-v4.1-flash`（x0.00 免费）只在 `/v3/config` 的 `data.models` 里，
企业端点（`/v2`、`/console`）的模型数组没有它。KeyPort 原先只打企业端点，
导致该模型从下游 /v1/models 缺失。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from conftest import make_test_config
from keyport.upstream.base import AccountAuth
from keyport.upstream.client import (
    UpstreamClient,
    _is_non_chat_model,
    _merge_models,
)

V3_BODY = {
    "code": 0,
    "data": {"models": [
        {"id": "glm-5.2", "credits": "x0.79"},
        {"id": "hy4-preview", "credits": "x0.29"},          # v3 口径
        {"id": "deepseek-v4.1-flash", "credits": "x0.00"},   # v3 独有，免费
        {"id": "gpt-image-2.5-sunburst", "tags": ["text-to-image"]},
        {"id": "seedance-2.5", "tags": ["text-to-video"]},
        {"id": "completion-1", "maxOutputTokens": 128},
    ]},
}

ENTERPRISE_BODY = {
    "code": 0,
    "data": {"models": [
        {"id": "glm-5.2", "credits": "x0.05"},              # 与 v3 冲突，应取 v3
        {"id": "hy4-preview", "credits": "x0.00"},          # 与 v3 冲突，应取 v3
        {"id": "gpt-5.3-codex", "credits": "x1.25"},        # 企业独有，补充
    ]},
}


def test_merge_models_primary_wins_and_keeps_order():
    primary = [{"id": "a", "credit_factor": 0.29}, {"id": "b", "credit_factor": 0.0}]
    secondary = [{"id": "a", "credit_factor": 0.0}, {"id": "c", "credit_factor": 1.0}]
    merged = _merge_models(primary, secondary)
    assert [m["id"] for m in merged] == ["a", "b", "c"]
    # 冲突 id 取 primary（v3）口径，避免把付费模型误判为免费
    assert merged[0]["credit_factor"] == pytest.approx(0.29)


def test_is_non_chat_model():
    assert _is_non_chat_model({"id": "completion-1"})
    assert _is_non_chat_model({"id": "nes-foo"})
    assert _is_non_chat_model({"id": "codewise-x"})
    assert _is_non_chat_model({"id": "m", "max_output_tokens": 128})
    assert _is_non_chat_model({"id": "m", "tags": ["text-to-image"]})
    assert _is_non_chat_model({"id": "m", "tags": ["text-to-video"]})
    assert not _is_non_chat_model({"id": "glm-5.3", "max_output_tokens": 24000})


def _client_with_transport(cfg, handler) -> UpstreamClient:
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock  # type: ignore[method-assign]
    return client


async def test_fetch_models_merges_v3_and_enterprise(tmp_path: Path):
    cfg = make_test_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/v3/config"):
            return httpx.Response(200, json=V3_BODY)
        return httpx.Response(200, json=ENTERPRISE_BODY)

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="g1", realm="global", access_token="T", kind="oauth")
    models = await client.fetch_models(auth)
    await client.aclose()

    by_id = {m["id"]: m for m in models}
    # v3 独有模型必须进并集（本次修复的核心）
    assert "deepseek-v4.1-flash" in by_id
    assert by_id["deepseek-v4.1-flash"]["credit_factor"] == 0.0
    # 企业独有模型作为补充保留
    assert "gpt-5.3-codex" in by_id
    # 冲突 id 取 v3 口径
    assert by_id["hy4-preview"]["credit_factor"] == pytest.approx(0.29)
    # 非对话模型剔除
    assert "gpt-image-2.5-sunburst" not in by_id
    assert "seedance-2.5" not in by_id
    assert "completion-1" not in by_id


async def test_fetch_models_survives_v3_failure(tmp_path: Path):
    """v3 失败时降级为企业端点结果，不整体报错。"""
    cfg = make_test_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v3/config"):
            return httpx.Response(500, text="boom")
        return httpx.Response(200, json=ENTERPRISE_BODY)

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="g1", realm="global", access_token="T", kind="oauth")
    models = await client.fetch_models(auth)
    await client.aclose()

    assert sorted(m["id"] for m in models) == ["glm-5.2", "gpt-5.3-codex", "hy4-preview"]


async def test_fetch_models_survives_enterprise_failure(tmp_path: Path):
    """企业端点失败时降级为 v3 结果。"""
    cfg = make_test_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/v3/config"):
            return httpx.Response(200, json=V3_BODY)
        return httpx.Response(500, text="boom")

    client = _client_with_transport(cfg, handler)
    auth = AccountAuth(uid="g1", realm="global", access_token="T", kind="oauth")
    models = await client.fetch_models(auth)
    await client.aclose()

    ids = {m["id"] for m in models}
    assert "deepseek-v4.1-flash" in ids
    assert "gpt-5.3-codex" not in ids
