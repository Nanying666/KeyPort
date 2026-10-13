"""账号级限额护栏：保留积分 / 日 Token / 日积分 / 按模型日 Token。

四条护栏各自作用域不同，混为一谈会误伤账号：

| 护栏 | 触发后 | 关键不变量 |
|---|---|---|
| reserve_credits | 账号整体停用 | 余额用尽前先停，避免触发上游提醒 |
| daily_token_limit | 账号整体停用 | 本地 0 点解封 |
| daily_credit_limit | **只服务免费模型** | 付费模型被挡、免费模型照常 |
| model_daily_token_limit | **仅该模型**停用 | 同账号其他模型照常 |

两条不可动摇的语义：`0` = 关闭；**计数未知时放行**（读库失败绝不能变成
静默的全池停用）。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from keyport.config import GuardConfig, PoolConfig
from keyport.pool.entry import AccountEntry
from keyport.pool.guards import (
    GUARD_KEYS,
    DailyUsage,
    DailyUsageCache,
    GuardLimits,
    apply_usage_to_entry,
    fetch_daily_usage,
    guard_block_reason,
    limits_for,
    midnight_epoch,
)
from keyport.pool.selector import select


def _entry(uid: str = "a", **kw) -> AccountEntry:
    kw.setdefault("credits", 1000)
    return AccountEntry(uid=uid, **kw)


# ---- 阈值解析 ----


def test_all_guards_default_off():
    limits = limits_for(GuardConfig(), "cn")
    assert limits.any_enabled() is False
    for key in GUARD_KEYS:
        assert getattr(limits, key) == 0


def test_realm_override_beats_global():
    guards = GuardConfig(daily_token_limit=1000,
                         realm_overrides={"cn": {"daily_token_limit": 5000}})
    assert limits_for(guards, "cn").daily_token_limit == 5000
    assert limits_for(guards, "global").daily_token_limit == 1000


def test_realm_override_null_inherits_global():
    """显式 null = 继承全局（不是「关掉」）。"""
    guards = GuardConfig(daily_token_limit=1000,
                         realm_overrides={"cn": {"daily_token_limit": None}})
    assert limits_for(guards, "cn").daily_token_limit == 1000


def test_realm_override_partial_inherits_rest():
    guards = GuardConfig(reserve_credits=50, daily_token_limit=1000,
                         realm_overrides={"cn": {"reserve_credits": 200}})
    cn = limits_for(guards, "cn")
    assert cn.reserve_credits == 200
    assert cn.daily_token_limit == 1000      # 未覆盖 → 继承


def test_negative_values_clamped_to_zero():
    """负数一律当关闭处理，绝不让它变成「永远挡住」。"""
    limits = limits_for(GuardConfig(reserve_credits=-5), "cn")
    assert limits.reserve_credits == 0
    assert limits.any_enabled() is False


# ---- 判定 ----


def test_reserve_credits_blocks_at_or_below_threshold():
    limits = GuardLimits(reserve_credits=100)
    assert guard_block_reason(_entry(credits=100), limits)
    assert guard_block_reason(_entry(credits=99), limits)
    assert guard_block_reason(_entry(credits=101), limits) == ""


def test_daily_token_limit_blocks_account_entirely():
    limits = GuardLimits(daily_token_limit=1000)
    entry = _entry(daily_tokens_today=1000)
    assert "daily_token_limit" in guard_block_reason(entry, limits)
    entry.daily_tokens_today = 999
    assert guard_block_reason(entry, limits) == ""


def test_daily_credit_limit_blocks_paid_but_allows_free():
    """护栏的核心价值：付费模型被挡，免费模型继续服务。"""
    limits = GuardLimits(daily_credit_limit=10)
    entry = _entry(daily_credits_today=10.0)
    assert "daily_credit_limit" in guard_block_reason(
        entry, limits, model="paid", is_free_model=False)
    assert guard_block_reason(entry, limits, model="free", is_free_model=True) == ""


def test_model_daily_token_limit_blocks_only_that_model():
    limits = GuardLimits(model_daily_token_limit=500)
    entry = _entry(model_daily_tokens={"glm-5": 500, "hy3": 10})
    assert "glm-5" in guard_block_reason(entry, limits, model="glm-5")
    assert guard_block_reason(entry, limits, model="hy3") == ""


def test_unknown_count_never_blocks():
    """**关键不变量**：计数为 None（读库失败 / 护栏关闭）时一律放行。

    把 None 当成 0 会让数据库故障变成静默的全池停用。
    """
    limits = GuardLimits(reserve_credits=0, daily_token_limit=1,
                         daily_credit_limit=1, model_daily_token_limit=1)
    entry = _entry(daily_tokens_today=None, daily_credits_today=None,
                   model_daily_tokens=None)
    assert guard_block_reason(entry, limits, model="m") == ""


def test_all_guards_off_never_blocks():
    entry = _entry(credits=0, daily_tokens_today=10**9,
                   daily_credits_today=10**9, model_daily_tokens={"m": 10**9})
    assert guard_block_reason(entry, GuardLimits(), model="m") == ""


def test_reserve_takes_precedence_over_daily():
    """多条同时命中时给出确定的原因（reserve 最先判）。"""
    limits = GuardLimits(reserve_credits=100, daily_token_limit=1)
    entry = _entry(credits=50, daily_tokens_today=10**6)
    assert guard_block_reason(entry, limits).startswith("reserve_credits")


# ---- 用量快照 ----


def test_midnight_epoch_is_today_zero():
    now = int(time.time())
    midnight = midnight_epoch(now)
    assert midnight <= now
    assert time.localtime(midnight).tm_hour == 0
    assert time.localtime(midnight).tm_mday == time.localtime(now).tm_mday


def test_daily_usage_accessors():
    usage = DailyUsage(tokens={"a": 100}, credits={"a": 2.5},
                       models={"a": {"m": 60}})
    assert usage.tokens_for("a") == 100
    assert usage.credits_for("a") == 2.5
    assert usage.model_tokens_for("a", "m") == 60
    # 未知账号 / 模型返回 0，不抛异常
    assert usage.tokens_for("nope") == 0
    assert usage.model_tokens_for("a", "nope") == 0


def test_fetch_daily_usage_aggregates_by_account_and_model(tmp_path: Path):
    from keyport.db import init_db

    db = init_db(tmp_path / "g.db")
    now = int(time.time())
    rows = [
        ("a", "glm-5", 100, 1.0),
        ("a", "glm-5", 50, 0.5),
        ("a", "hy3", 30, 0.0),
        ("b", "glm-5", 10, 0.2),
    ]
    for uid, model, tokens, credits in rows:
        db.execute(
            "INSERT INTO request_logs(ts, account_uid, model, total_tokens, credits) "
            "VALUES(?,?,?,?,?)", (now, uid, model, tokens, credits))

    usage = fetch_daily_usage(db)
    assert usage is not None
    assert usage.tokens_for("a") == 180
    assert usage.credits_for("a") == pytest.approx(1.5)
    assert usage.model_tokens_for("a", "glm-5") == 150
    assert usage.model_tokens_for("a", "hy3") == 30
    assert usage.tokens_for("b") == 10


def test_fetch_daily_usage_excludes_yesterday(tmp_path: Path):
    from keyport.db import init_db

    db = init_db(tmp_path / "g.db")
    now = int(time.time())
    db.execute(
        "INSERT INTO request_logs(ts, account_uid, model, total_tokens, credits) "
        "VALUES(?,?,?,?,?)", (midnight_epoch(now) - 60, "a", "m", 9999, 99.0))
    db.execute(
        "INSERT INTO request_logs(ts, account_uid, model, total_tokens, credits) "
        "VALUES(?,?,?,?,?)", (now, "a", "m", 10, 1.0))
    usage = fetch_daily_usage(db)
    assert usage is not None and usage.tokens_for("a") == 10


def test_fetch_daily_usage_ignores_rows_without_account(tmp_path: Path):
    from keyport.db import init_db

    db = init_db(tmp_path / "g.db")
    db.execute(
        "INSERT INTO request_logs(ts, account_uid, model, total_tokens, credits) "
        "VALUES(?,?,?,?,?)", (int(time.time()), "", "m", 500, 5.0))
    usage = fetch_daily_usage(db)
    assert usage is not None and usage.tokens == {}


def test_fetch_daily_usage_returns_none_on_db_error():
    """读库失败必须返回 None（「未知」），而不是空快照（「用量为 0」）。"""
    class _Boom:
        def query(self, *_a, **_kw):
            raise RuntimeError("db down")

    assert fetch_daily_usage(_Boom()) is None


def test_usage_cache_reuses_within_ttl(tmp_path: Path):
    from keyport.db import init_db

    db = init_db(tmp_path / "g.db")
    calls: list[int] = []

    class _Counting:
        def __init__(self, inner):
            self._inner = inner

        def query(self, sql, params=()):
            calls.append(1)
            return self._inner.query(sql, params)

    cache = DailyUsageCache(ttl_seconds=60)
    cache.get(_Counting(db))
    cache.get(_Counting(db))
    assert len(calls) == 1, "TTL 内应复用缓存"


def test_usage_cache_force_refresh_bypasses_ttl(tmp_path: Path):
    from keyport.db import init_db

    db = init_db(tmp_path / "g.db")
    calls: list[int] = []

    class _Counting:
        def __init__(self, inner):
            self._inner = inner

        def query(self, sql, params=()):
            calls.append(1)
            return self._inner.query(sql, params)

    cache = DailyUsageCache(ttl_seconds=60)
    cache.get(_Counting(db))
    cache.get(_Counting(db), refresh=True)
    assert len(calls) == 2


# ---- 计数推送 ----


def test_apply_usage_clears_counts_when_guards_off():
    """护栏全关时不记计数（省内存，也避免看板展示误导性的 0）。"""
    entry = _entry()
    apply_usage_to_entry(entry, DailyUsage(tokens={"a": 100}), GuardLimits())
    assert entry.daily_tokens_today is None
    assert entry.model_daily_tokens is None


def test_apply_usage_sets_counts_when_guard_enabled():
    entry = _entry()
    usage = DailyUsage(tokens={"a": 100}, credits={"a": 3.0},
                       models={"a": {"m": 40}})
    apply_usage_to_entry(entry, usage, GuardLimits(daily_token_limit=1000))
    assert entry.daily_tokens_today == 100
    assert entry.daily_credits_today == 3.0
    assert entry.model_daily_tokens == {"m": 40}


def test_apply_usage_none_marks_unknown():
    entry = _entry()
    apply_usage_to_entry(entry, None, GuardLimits(daily_token_limit=1000))
    assert entry.daily_tokens_today is None
    assert entry.daily_credits_today is None
    assert entry.model_daily_tokens is None


# ---- 选号集成 ----


def test_selector_skips_guard_blocked_account():
    cfg = PoolConfig()
    now = int(time.time())
    blocked = _entry("blocked", credits=10)
    ok = _entry("ok", credits=1000)

    def guard(entry: AccountEntry) -> str:
        return "reserve_credits" if entry.uid == "blocked" else ""

    picked = select([blocked, ok], cfg, now=now, guard=guard)
    assert picked is not None and picked.uid == "ok"


def test_selector_returns_none_when_all_blocked():
    cfg = PoolConfig()
    picked = select([_entry("a"), _entry("b")], cfg, guard=lambda e: "blocked")
    assert picked is None


def test_selector_records_block_reason_for_diagnostics():
    cfg = PoolConfig()
    entry = _entry("a")
    select([entry], cfg, guard=lambda e: "daily_token_limit(1>=1)")
    assert entry.guard_blocked_reason == "daily_token_limit(1>=1)"


def test_selector_clears_block_reason_when_allowed():
    cfg = PoolConfig()
    entry = _entry("a")
    entry.guard_blocked_reason = "stale"
    select([entry], cfg, guard=lambda e: "")
    assert entry.guard_blocked_reason == ""


def test_selector_survives_guard_exception():
    """护栏自身抛异常不该阻断选号（宁可放行也不能让整个网关 500）。"""
    cfg = PoolConfig()

    def boom(_entry: AccountEntry) -> str:
        raise RuntimeError("guard bug")

    picked = select([_entry("a")], cfg, guard=boom)
    assert picked is not None and picked.uid == "a"


def test_guard_blocked_account_is_not_penalised():
    """被护栏挡住只是跳过，不落冷却、不计连败（等 0 点自然解封）。"""
    cfg = PoolConfig()
    entry = _entry("a", credits=0)
    select([entry], cfg, guard=lambda e: "reserve_credits")
    assert entry.cooldown_until == 0
    assert entry.breaker_until == 0
    assert entry.consecutive_failures == 0
    assert entry.enabled is True
