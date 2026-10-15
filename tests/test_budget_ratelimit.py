"""当日积分预算闸与客户端维度限流。

移植自 sheng-du0521/workbuddy2api 的 `internal/server/budget.go` 与
`cmd/webapp/ratelimit.go`。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_test_config

from keyport.budget import CST_OFFSET_SECONDS, DailyBudget, cst_day
from keyport.ratelimit import (
    ClientLimiter,
    bucket_for,
    retry_after_seconds,
)

# 固定时刻：2026-10-15 12:00 UTC = 2026-10-15 20:00 CST
NOW = 1_792_065_600.0


# ---- CST 自然日 ----


def test_cst_day_uses_utc8():
    """按 CST 自然日重置（上游增长体系同口径；中国无夏令时，固定 +8）。"""
    assert cst_day(NOW) == "2026-10-15"
    # UTC 16:00 已是 CST 次日 00:00
    assert cst_day(NOW + 4 * 3600) == "2026-10-16"
    assert CST_OFFSET_SECONDS == 8 * 3600


# ---- 预算闸 ----


def test_disabled_by_default():
    """0 是「不限」的哨兵值，不是「未设置」。"""
    b = DailyBudget(0)
    assert b.admit(NOW) is True
    assert b.snapshot(NOW)["enabled"] is False
    b.add(99999, True, NOW)
    assert b.admit(NOW) is True


def test_admits_until_limit():
    """判据是**严格小于** —— used == limit 时已经到顶。"""
    b = DailyBudget(10.0)
    assert b.admit(NOW) is True
    b.add(9.99, True, NOW)
    assert b.admit(NOW) is True
    b.add(0.01, True, NOW)
    assert b.admit(NOW) is False      # 已到顶
    assert b.admit(NOW) is False


def test_rejected_counter():
    """闸必须能回答「今天挡了多少次」，否则运维不知道是不是预算造成的。"""
    b = DailyBudget(1.0)
    b.add(2.0, True, NOW)
    for _ in range(3):
        b.admit(NOW)
    assert b.snapshot(NOW)["rejected"] == 3


def test_unobserved_credit_not_counted():
    """上游缺 credit 时不计数 —— 把「没观测到」当 0 会让闸形同虚设。"""
    b = DailyBudget(10.0)
    b.add(999.0, False, NOW)
    assert b.snapshot(NOW)["used"] == 0.0
    assert b.admit(NOW) is True


def test_unobserved_still_rolls_day():
    """未观测也要照常滚日，否则跨天后第一条无 credit 的请求不会触发重置。"""
    b = DailyBudget(10.0)
    b.add(20.0, True, NOW)
    assert b.admit(NOW) is False
    b.add(0.0, False, NOW + 86400)      # 次日，未观测
    assert b.snapshot(NOW + 86400)["used"] == 0.0
    assert b.admit(NOW + 86400) is True


def test_negative_credit_ignored():
    """负值宁可少算，也不让它倒扣当日用量（倒扣会把关上的闸重新打开）。"""
    b = DailyBudget(10.0)
    b.add(5.0, True, NOW)
    b.add(-100.0, True, NOW)
    assert b.snapshot(NOW)["used"] == 5.0


def test_resets_on_next_cst_day():
    b = DailyBudget(10.0)
    b.add(20.0, True, NOW)
    assert b.admit(NOW) is False
    # 次日（惰性滚动，无定时器）
    nxt = NOW + 86400
    assert b.admit(nxt) is True
    snap = b.snapshot(nxt)
    assert snap["used"] == 0.0 and snap["rejected"] == 0
    assert snap["day"] == "2026-10-16"


def test_snapshot_shape():
    b = DailyBudget(100.0)
    b.add(1.5, True, NOW)
    snap = b.snapshot(NOW)
    assert snap == {"day": "2026-10-15", "used": 1.5, "limit": 100.0,
                    "rejected": 0, "enabled": True}


# ---- 限流器 ----


def test_rpm_zero_means_unlimited():
    """0 = 不限（默认，升级后行为与引入前一致）。"""
    lim = ClientLimiter()
    for _ in range(100):
        ok, _ = lim.allow("k", 0, NOW)
        assert ok is True


def test_sliding_window_blocks_at_limit():
    lim = ClientLimiter()
    for i in range(3):
        ok, _ = lim.allow("k", 3, NOW + i)
        assert ok is True, i
    ok, wait = lim.allow("k", 3, NOW + 3)
    assert ok is False
    assert wait > 0


def test_window_slides_and_recovers():
    lim = ClientLimiter()
    for i in range(3):
        lim.allow("k", 3, NOW + i)
    assert lim.allow("k", 3, NOW + 3)[0] is False
    # 最早一次滑出窗口（60s）后恢复额度
    assert lim.allow("k", 3, NOW + 61)[0] is True


def test_buckets_are_independent():
    """换 Key 即换桶（符合「客户端」语义）。"""
    lim = ClientLimiter()
    for i in range(3):
        lim.allow("key:1", 3, NOW + i)
    assert lim.allow("key:1", 3, NOW + 3)[0] is False
    assert lim.allow("key:2", 3, NOW + 3)[0] is True


def test_bucket_for_prefers_key_id():
    assert bucket_for(7, "1.2.3.4") == "key:7"
    assert bucket_for(0, "1.2.3.4") == "ip:1.2.3.4"
    assert bucket_for(0, "") == "ip:unknown"


def test_retry_after_at_least_one_second():
    assert retry_after_seconds(0.1) == 1
    assert retry_after_seconds(0) == 1
    assert retry_after_seconds(2.3) == 3


def test_limiter_snapshot_and_gc():
    lim = ClientLimiter()
    lim.allow("a", 5, NOW)
    lim.allow("b", 5, NOW)
    assert lim.snapshot()["buckets"] == 2
    # 窗口外再访问触发 GC，过期桶被清掉
    lim.allow("c", 5, NOW + 120)
    assert lim.snapshot()["buckets"] == 1
    assert lim.snapshot()["allowed"] == 3


def test_limiter_counts():
    lim = ClientLimiter()
    for i in range(2):
        lim.allow("k", 2, NOW + i)
    lim.allow("k", 2, NOW + 2)
    snap = lim.snapshot()
    assert snap["allowed"] == 2 and snap["blocked"] == 1


# ---- 接线 ----


def _app(tmp_path: Path, **pool):
    from keyport.app import create_app
    from keyport.security import hash_api_key
    from keyport.upstream.base import AccountAuth

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    for k, v in pool.items():
        setattr(cfg.pool, k, v)
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T"))
    key = "sk-kp-budget"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("b", hash_api_key(key), key[:12]),
    )
    return app, ctx, {"Authorization": f"Bearer {key}"}


def test_budget_gate_returns_429(tmp_path: Path):
    """当日预算用尽 → 429（且**在读 body 之前**就拒掉）。"""
    from fastapi.testclient import TestClient

    app, ctx, auth = _app(tmp_path, budget_daily_credit_limit=1.0)
    # 先记一笔超限的扣费
    ctx.budget.add(5.0, True)

    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={"model": "hy3", "messages": [{"role": "user", "content": "x"}]},
                       headers=auth)
        assert resp.status_code == 429, resp.text
        assert "预算" in resp.json()["error"]["message"]


def test_budget_disabled_by_default(tmp_path: Path):
    """默认 0 = 不限，请求照常（会因假凭证失败，但不是 429 预算拒）。"""
    from fastapi.testclient import TestClient

    app, ctx, auth = _app(tmp_path)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={"model": "hy3", "messages": [{"role": "user", "content": "x"}]},
                       headers=auth)
        assert resp.status_code != 429
        assert ctx.budget.snapshot()["enabled"] is False


def test_rpm_limit_returns_429_with_retry_after(tmp_path: Path):
    from fastapi.testclient import TestClient

    app, _ctx, auth = _app(tmp_path, rate_limit_rpm=2)
    payload = {"model": "hy3", "messages": [{"role": "user", "content": "x"}]}
    with TestClient(app) as tc:
        codes = []
        for _ in range(3):
            resp = tc.post("/v1/chat/completions", json=payload, headers=auth)
            codes.append(resp.status_code)
            if resp.status_code == 429:
                assert resp.headers.get("Retry-After")
                assert "请求过于频繁" in resp.json()["error"]["message"]
        assert 429 in codes


def test_rpm_disabled_by_default(tmp_path: Path):
    from fastapi.testclient import TestClient

    app, _ctx, auth = _app(tmp_path)
    payload = {"model": "hy3", "messages": [{"role": "user", "content": "x"}]}
    with TestClient(app) as tc:
        for _ in range(5):
            resp = tc.post("/v1/chat/completions", json=payload, headers=auth)
            assert resp.status_code != 429


def test_anthropic_path_also_limited(tmp_path: Path):
    """三条协议共用同一个准入点，故 Anthropic 路径同样受限。"""
    from fastapi.testclient import TestClient

    app, _ctx, auth = _app(tmp_path, rate_limit_rpm=1)
    payload = {"model": "hy3", "max_tokens": 16,
               "messages": [{"role": "user", "content": "x"}]}
    headers = {**auth, "anthropic-version": "2023-06-01"}
    with TestClient(app) as tc:
        tc.post("/v1/messages", json=payload, headers=headers)
        resp = tc.post("/v1/messages", json=payload, headers=headers)
        assert resp.status_code == 429
        assert resp.json()["type"] == "error"


@pytest.mark.parametrize("field,default", [
    ("budget_daily_credit_limit", 0.0),
    ("rate_limit_rpm", 0),
])
def test_config_defaults_are_disabled(field, default):
    from keyport.config import PoolConfig

    assert getattr(PoolConfig(), field) == default
