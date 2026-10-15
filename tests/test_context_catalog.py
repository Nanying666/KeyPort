"""context_length / max_output_tokens 静态兜底表。

移植自 sheng-du0521/workbuddy2api 的 `internal/upstream/context_catalog.go`。
"""

from __future__ import annotations

import pytest

from keyport.upstream.client import _normalize_models
from keyport.upstream.context_catalog import (
    CONTEXT_FALLBACK,
    DEFAULT_CONTEXT_WINDOW,
    context_window,
    max_output_tokens,
)

# ---- 查找链优先级 ----


def test_remote_value_is_authoritative():
    """上游给了就用上游的，绝不覆盖。"""
    assert context_window("glm-5.3", 42_000) == 42_000
    assert max_output_tokens("glm-5.3", 8_000) == 8_000


def test_table_fills_when_remote_missing():
    assert context_window("glm-5.3") == 1_000_000
    assert context_window("glm-5.1") == 200_000
    assert max_output_tokens("glm-5.3") == 131_072


@pytest.mark.parametrize("remote", [0, -1, None])
def test_zero_or_negative_remote_falls_back(remote):
    """上游 0 / 负数视为"没给"，不是"真的 0"。"""
    assert context_window("glm-5.3", remote) == 1_000_000


def test_unknown_model_gets_default_context():
    """知识表也未收录 → 1M 兜底（宁可高估不低估）。

    低估会让客户端按 context_length 提前截断、白白丢上下文 —— 用户看不见且不可恢复。
    """
    assert context_window("totally-unknown-model") == DEFAULT_CONTEXT_WINDOW
    assert DEFAULT_CONTEXT_WINDOW == 1_000_000


def test_unknown_output_is_none_not_fabricated():
    """输出上限无「宁可高估」的安全侧 → 未知即 None（调用方省略字段）。"""
    assert max_output_tokens("totally-unknown-model") is None


def test_table_entry_with_zero_output_returns_none():
    """知识表里 max_output 为 0 的条目（输出上限未收录）→ 省略字段。"""
    assert context_window("kimi-k2.8-preview") == 1_048_576
    assert max_output_tokens("kimi-k2.8-preview") is None
    assert max_output_tokens("auto") is None


def test_model_name_case_insensitive():
    """上游模型名大小写敏感，但目录里可能出现大写形态。"""
    assert context_window("GLM-5.3") == context_window("glm-5.3")
    assert context_window(" GLM-5.3 ") == context_window("glm-5.3")


def test_empty_model_name():
    assert context_window("") == DEFAULT_CONTEXT_WINDOW
    assert context_window(None) == DEFAULT_CONTEXT_WINDOW  # type: ignore[arg-type]
    assert max_output_tokens("") is None


# ---- 表本身 ----


def test_table_entries_are_sane():
    """每条条目的 context 必须为正（否则条目无意义），输出上限非负。"""
    for model, cap in CONTEXT_FALLBACK.items():
        assert cap.context > 0, model
        assert cap.max_output >= 0, model


def test_table_covers_the_models_this_deployment_uses():
    """本部署实际在用的模型必须被收录，否则只能吃 1M 兜底。"""
    for model in ("deepseek-v4.1-flash", "hy3", "hy3-preview", "hy4-preview",
                  "glm-5.3", "glm-5.3-flash", "kimi-k3", "auto"):
        assert model in CONTEXT_FALLBACK, model


# ---- 接线到模型目录 ----


def test_normalize_models_fills_missing_fields():
    """上游目录只给 id 时，两个字段都要按知识表补齐/省略。"""
    out = _normalize_models({"models": [
        {"id": "glm-5.3"},
        {"id": "totally-unknown"},
    ]})
    by_id = {m["id"]: m for m in out}
    assert by_id["glm-5.3"]["context_window"] == 1_000_000
    assert by_id["glm-5.3"]["max_output_tokens"] == 131_072
    assert by_id["totally-unknown"]["context_window"] == DEFAULT_CONTEXT_WINDOW
    assert "max_output_tokens" not in by_id["totally-unknown"]


def test_normalize_models_keeps_remote_values():
    """上游给了值时不得被知识表覆盖。"""
    out = _normalize_models({"models": [
        {"id": "glm-5.3", "contextWindow": 200_000, "maxTokens": 50_000},
    ]})
    assert out[0]["context_window"] == 200_000
    assert out[0]["max_output_tokens"] == 50_000


def test_normalize_models_handles_alternate_field_names():
    for field in ("contextWindow", "context_window", "context_length"):
        out = _normalize_models({"models": [{"id": "m", field: 123_456}]})
        assert out[0]["context_window"] == 123_456, field
    for field in ("maxTokens", "max_output_tokens", "maxOutputTokens"):
        out = _normalize_models({"models": [{"id": "m", field: 7_000}]})
        assert out[0]["max_output_tokens"] == 7_000, field
