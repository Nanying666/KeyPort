"""账号池：三因子选号、冷却状态机、会话粘性、额度复活。"""

from __future__ import annotations

import time

from keyport.config import PoolConfig, SessionStickyConfig
from keyport.pool.entry import AccountEntry, next_midnight_four
from keyport.pool.ledger import ModelLedger
from keyport.pool.selector import compute_weight, select
from keyport.pool.sticky import StickyStore
from keyport.pool.transitions import (
    apply_error,
    apply_success,
    maybe_revive_on_credit,
    revive,
)
from keyport.upstream.base import ErrorKind, UpstreamError


def test_rate_limit_sets_soft_cooldown_with_backoff():
    cfg = PoolConfig(soft_rate_seconds=600, soft_rate_max_seconds=7200)
    now = int(time.time())
    e = _entry("a")
    err = UpstreamError(ErrorKind.RATE_LIMIT, status=429, message="slow down")
    action = apply_error(e, err, cfg, now)
    assert e.cooldown_until == now + 600
    assert "soft_cooldown" in action
    # 第二次连败翻倍
    e.consecutive_failures = 1
    apply_error(e, err, cfg, now)
    assert e.cooldown_until >= now + 1200


def test_not_found_is_fixed_short_cooldown():
    cfg = PoolConfig(not_found_seconds=60)
    now = int(time.time())
    e = _entry("a")
    apply_error(e, UpstreamError(ErrorKind.NOT_FOUND, status=404), cfg, now)
    assert e.cooldown_until == now + 60


def test_payment_cools_until_next_0400():
    cfg = PoolConfig()
    now = int(time.time())
    e = _entry("a")
    apply_error(e, UpstreamError(ErrorKind.PAYMENT, status=402), cfg, now)
    assert e.cooldown_until == next_midnight_four(now)
    assert e.cooldown_until > now


def test_auth_failure_disables_account():
    cfg = PoolConfig()
    now = int(time.time())
    e = _entry("a")
    apply_error(e, UpstreamError(ErrorKind.AUTH, status=401), cfg, now)
    assert e.enabled is False
    assert e.disabled_reason == "auth_failed"


def test_consecutive_failures_trip_breaker():
    cfg = PoolConfig(breaker_threshold=3, breaker_cooldown_seconds=1800)
    now = int(time.time())
    e = _entry("a")
    err = UpstreamError(ErrorKind.SERVER, status=500)
    for _ in range(3):
        apply_error(e, err, cfg, now)
    assert e.breaker_until > now


def test_bad_request_does_not_penalise_account():
    cfg = PoolConfig()
    now = int(time.time())
    e = _entry("a")
    action = apply_error(e, UpstreamError(ErrorKind.BAD_REQUEST, status=400), cfg, now)
    assert action == "client_error"
    assert e.failed_calls == 0
    assert e.cooldown_until == 0


def test_captcha_does_not_penalise_account():
    """人机验证是端点级限制，非账号过错：不冷却、不熔断、不计连败。"""
    cfg = PoolConfig()
    now = int(time.time())
    e = _entry("a")
    action = apply_error(
        e, UpstreamError(ErrorKind.CAPTCHA, status=400, message="captcha verify failed"),
        cfg, now,
    )
    assert action == "captcha_required"
    assert e.failed_calls == 0
    assert e.consecutive_failures == 0
    assert e.cooldown_until == 0
    assert e.breaker_until == 0


def test_captcha_repeated_never_trips_breaker():
    """反复被验证码拦截也不应把账号熔断掉。"""
    cfg = PoolConfig(breaker_threshold=3)
    now = int(time.time())
    e = _entry("a")
    err = UpstreamError(ErrorKind.CAPTCHA, status=400, message="captcha verify failed")
    for _ in range(10):
        apply_error(e, err, cfg, now)
    assert e.breaker_until == 0
    assert e.consecutive_failures == 0


def test_model_limit_cools_only_that_model():
    """模型名必须由调用方**显式传入**。

    早先用 `err.message`（上游的错误**文案**）当键，而 `healthy_for_model` 查的是
    真实模型名 —— 两边永远对不上，冷却条目写了但**永远不被命中**。
    """
    cfg = PoolConfig()
    now = int(time.time())
    e = _entry("a")
    apply_error(e, UpstreamError(ErrorKind.MODEL_LIMIT, message="usage exceeded"),
                cfg, now, model="glm-5")
    assert e.model_cooldowns["glm-5"] > now
    assert e.cooldown_until == 0
    # 键必须是模型名，不是错误文案
    assert "usage exceeded" not in e.model_cooldowns
    assert e.healthy_for_model(now, "glm-5") is False
    # 切模型立即可用（这正是 6004 的语义）
    assert e.healthy_for_model(now, "hy3") is True


def test_model_limit_without_model_falls_back_to_account_cooldown():
    """取不到模型名时**回落到账号级冷却**。

    与其写一个永远命不中的键（等于完全不冷却、错误无限重放），
    不如挡住整个账号。
    """
    cfg = PoolConfig()
    now = int(time.time())
    e = _entry("a")
    detail = apply_error(e, UpstreamError(ErrorKind.MODEL_LIMIT, message="usage exceeded"),
                         cfg, now)
    assert detail.startswith("cooldown:")
    assert e.model_cooldowns == {}
    assert e.cooldown_until > now


def test_success_resets_consecutive_failures():
    now = int(time.time())
    e = _entry("a", credits=100)
    e.consecutive_failures = 5
    apply_success(e, credits_used=10, now=now)
    assert e.consecutive_failures == 0
    assert e.credits == 90
    assert e.total_calls == 1


def test_quota_recovery_auto_revives():
    now = int(time.time())
    e = _entry("a")
    e.enabled = False
    e.disabled_reason = "quota_exhausted"
    e.credits = 50
    assert maybe_revive_on_credit(e, now) is True
    assert e.enabled is True


def test_revive_clears_everything():
    now = int(time.time())
    e = _entry("a")
    e.cooldown_until = now + 100
    e.breaker_until = now + 200
    e.model_cooldowns["m"] = now + 300
    revive(e, now)
    assert e.healthy(now) is True


# ---- 会话粘性 ----


def test_sticky_bind_and_unbind():
    store = StickyStore(SessionStickyConfig(ttl_seconds=60))
    store.bind("conv1", "uid-a")
    assert store.get("conv1") == "uid-a"
    store.unbind("conv1")
    assert store.get("conv1") == ""


def test_sticky_expires():
    store = StickyStore(SessionStickyConfig(ttl_seconds=0))
    store.bind("conv1", "uid-a")
    assert store.get("conv1") == ""


def test_sticky_gc():
    store = StickyStore(SessionStickyConfig(ttl_seconds=0))
    store.bind("a", "x")
    store.bind("b", "y")
    assert store.gc() == 2


# ---- 账本 ----


def test_ledger_tracks_free_and_paid():
    ledger = ModelLedger()
    ledger.observe("cn", "free-model", 0, 1000)
    ledger.observe("cn", "paid-model", 5, 1000)
    assert ledger.tier_of("cn", "free-model") == 0
    assert ledger.tier_of("cn", "paid-model") == 2
    assert ledger.tier_of("cn", "unknown") == 1


def test_ledger_explore_interval():
    ledger = ModelLedger()
    assert ledger.should_explore("cn", "m", 1800) is True
    assert ledger.should_explore("cn", "m", 1800) is False
    assert ledger.should_explore("cn", "m", 0) is False


def _entry(uid: str, credits: int = 0, **kw) -> AccountEntry:
    return AccountEntry(uid=uid, credits=credits, **kw)


# ---- 三因子权重 ----


def test_weight_prefers_more_credits():
    cfg = PoolConfig()
    now = int(time.time())
    rich = _entry("rich", credits=1000)
    poor = _entry("poor", credits=100)
    assert compute_weight(rich, 1000, now, cfg) > compute_weight(poor, 1000, now, cfg)


def test_weight_prefers_expiring_credits():
    cfg = PoolConfig()
    now = int(time.time())
    expiring = _entry("a", credits=1000, credits_expiring=1000)
    stable = _entry("b", credits=1000, credits_expiring=0)
    assert compute_weight(expiring, 1000, now, cfg) > compute_weight(stable, 1000, now, cfg)


def test_weight_idle_compensation_favours_never_used():
    cfg = PoolConfig()
    now = int(time.time())
    idle = _entry("idle", credits=1000, last_used_at=0)
    busy = _entry("busy", credits=1000, last_used_at=now)
    assert compute_weight(idle, 1000, now, cfg) > compute_weight(busy, 1000, now, cfg)


def test_idle_weight_is_capped():
    cfg = PoolConfig(idle_weight_per_hour=1.0, idle_weight_max=5.0)
    now = int(time.time())
    e = _entry("a", credits=1, last_used_at=now - 10_000 * 3600)
    assert e.idle_weight(now, cfg) == 5.0


# ---- 选号过滤 ----


def test_select_skips_disabled_and_cooled():
    cfg = PoolConfig()
    now = int(time.time())
    ok = _entry("ok", credits=10)
    disabled = _entry("off", credits=1000, enabled=False)
    cooled = _entry("cool", credits=1000, cooldown_until=now + 100)
    picked = select([ok, disabled, cooled], cfg, now=now)
    assert picked is not None and picked.uid == "ok"


def test_select_respects_realm_isolation():
    cfg = PoolConfig()
    now = int(time.time())
    cn = _entry("cn1", credits=10, realm="cn")
    glob = _entry("g1", credits=1000, realm="global")
    picked = select([cn, glob], cfg, now=now, realm="cn")
    assert picked is not None and picked.uid == "cn1"


def test_select_returns_none_when_no_capacity():
    cfg = PoolConfig(max_in_flight=1)
    now = int(time.time())
    e = _entry("a", credits=100, inflight=1)
    assert select([e], cfg, now=now) is None


def test_select_model_cooldown_only_blocks_that_model():
    cfg = PoolConfig()
    now = int(time.time())
    e = _entry("a", credits=100)
    e.model_cooldowns["glm-5"] = now + 600
    assert select([e], cfg, now=now, model="glm-5") is None
    assert select([e], cfg, now=now, model="glm-4.6") is not None


def test_select_skips_recently_picked():
    cfg = PoolConfig(pick_gap_ms=100_000)
    now = int(time.time())
    a = _entry("a", credits=100)
    b = _entry("b", credits=100)
    last = {"a": float(now)}
    picked = select([a, b], cfg, now=now, last_pick=last)
    assert picked is not None and picked.uid == "b"


def test_select_lru_fallback_when_all_recent():
    cfg = PoolConfig(pick_gap_ms=100_000)
    now = int(time.time())
    a = _entry("a", credits=100)
    b = _entry("b", credits=100)
    last = {"a": float(now - 10), "b": float(now - 5)}
    picked = select([a, b], cfg, now=now, last_pick=last)
    assert picked is not None and picked.uid == "a"  # 最旧者
