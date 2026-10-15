"""调度器新增行为：余额后台刷新、排程抖动、企业版/国际版门控、禁用号保号。

移植自 sheng-du0521/workbuddy2api 的 `internal/scheduler/scheduler.go`
（`RunBalanceRefreshNow` / `jitterOffset` / 逐任务 gating）与
misakano7545/workbuddy2api-panel 的 `schedule.balance_refresh_minutes`。
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from conftest import make_test_config

from keyport.app import build_context
from keyport.scheduler import _jitter_offset
from keyport.upstream.base import AccountAuth


def _ctx(tmp_path: Path, **sched):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    for k, v in sched.items():
        setattr(cfg.schedule, k, v)
    return build_context(cfg)


# ---- 排程抖动 ----


def test_jitter_offset_is_deterministic():
    """同一任务 + 同一天 + 同一小时必须恒定。

    `tick()` 每分钟重算一次；若每次得到新偏移，那一小时会被反复重置、
    **永远到不了点**（这正是不能用 `random` 的原因）。
    """
    local = time.localtime(1_792_000_000)
    a = _jitter_offset("checkin", local, 600)
    b = _jitter_offset("checkin", local, 600)
    assert a == b
    assert 0 <= a < 600


def test_jitter_offset_varies_by_task_and_hour():
    """不同任务 / 不同小时要摊开到不同位置，否则抖动没有意义。"""
    local = time.localtime(1_792_000_000)
    offsets = {
        _jitter_offset(task, local, 600)
        for task in ("checkin", "travel", "activity", "keepalive", "cat")
    }
    assert len(offsets) > 1

    next_hour = time.localtime(1_792_000_000 + 3600)
    assert _jitter_offset("checkin", local, 600) != _jitter_offset("checkin", next_hour, 600)


def test_jitter_zero_span():
    assert _jitter_offset("checkin", time.localtime(), 0) == 0


async def test_tick_waits_for_jitter_offset(tmp_path: Path):
    """抖动窗口内不得提前触发。"""
    ctx = _ctx(tmp_path, jitter_minutes=10, checkin_hours=[12])
    sched = ctx.scheduler
    # 造一个「12 点整 + 0 秒」的时刻；偏移 > 0 时应等待
    base = int(time.mktime((2026, 10, 15, 12, 0, 0, 0, 0, -1)))
    local = time.localtime(base)
    offset = _jitter_offset("checkin", local, 600)
    if offset == 0:
        pytest.skip("该时刻偏移恰为 0，无法验证等待行为")
    fired = await sched.tick(base + 1)
    assert fired == []
    # 到点后应触发
    fired = await sched.tick(base + offset + 1)
    assert [f["task"] for f in fired] == ["checkin"]


async def test_tick_without_jitter_fires_at_top_of_hour(tmp_path: Path):
    ctx = _ctx(tmp_path, jitter_minutes=0, checkin_hours=[12])
    base = int(time.mktime((2026, 10, 15, 12, 0, 5, 0, 0, -1)))
    fired = await ctx.scheduler.tick(base)
    assert [f["task"] for f in fired] == ["checkin"]


async def test_tick_fires_once_per_hour(tmp_path: Path):
    ctx = _ctx(tmp_path, jitter_minutes=0, checkin_hours=[12])
    base = int(time.mktime((2026, 10, 15, 12, 0, 5, 0, 0, -1)))
    assert await ctx.scheduler.tick(base)
    assert await ctx.scheduler.tick(base + 60) == []


# ---- 企业版 / 国际版门控 ----


async def test_global_skips_cn_tasks(tmp_path: Path):
    """global 无国内任务体系 → 签到/旅行/夜猫/保活都不该发上游请求。"""
    ctx = _ctx(tmp_path, checkin_hours=[12], travel_hours=[12], cat_hours=[12],
               keepalive_hours=[12])
    ctx.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="G", kind="oauth"))
    for task in ("checkin", "travel", "cat", "keepalive"):
        results = await ctx.scheduler.run_now(task)
        assert len(results) == 1
        assert results[0]["skipped"] is True, task
        assert "国际版" in results[0]["detail"]


async def test_global_activity_is_not_skipped(tmp_path: Path):
    """活跃上报对 global 可用（实测 /v2/report 返回 code=0）。

    跳过会让国际版连登永远点不亮、每日积分拿不到 —— 这个门控方向与其它任务相反。
    """
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="G", kind="oauth"))
    assert ctx.scheduler._gate_reason("activity", ctx.pool.get("g1")) == ""


async def test_enterprise_skips_growth_tasks(tmp_path: Path):
    """企业版无个人成长体系（上游 403）→ 成长任务零收益白跑，显式跳过。

    这类「静默无效」最易被忽略（日志无异常），故不给上游多发一次请求。
    """
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(
        uid="e1", realm="cn", access_token="E", kind="oauth",
        enterprise_id="ent-1"))
    entry = ctx.pool.get("e1")
    assert entry.is_enterprise is True
    assert "企业版" in ctx.scheduler._gate_reason("activity", entry)


async def test_cn_personal_runs_normally(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))
    assert ctx.scheduler._gate_reason("checkin", ctx.pool.get("c1")) == ""


# ---- 禁用号保号 ----


async def test_disabled_accounts_included_by_default(tmp_path: Path):
    """「禁用」只关选号、不停保号 —— 否则禁用号 token 过期后再也救不回来。"""
    ctx = _ctx(tmp_path, include_disabled_in_tasks=True)
    ctx.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))
    ctx.pool.set_manual_disabled("c1", True, "运维摘除")
    results = await ctx.scheduler.run_now("checkin")
    assert len(results) == 1
    # 照常被尝试（会因假 token 失败，但确实发出过调用），不是被门控跳过
    assert results[0].get("skipped") is not True


async def test_disabled_accounts_excluded_when_configured(tmp_path: Path):
    ctx = _ctx(tmp_path, include_disabled_in_tasks=False)
    ctx.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))
    ctx.pool.set_manual_disabled("c1", True, "运维摘除")
    assert await ctx.scheduler.run_now("checkin") == []


async def test_skipped_is_recorded_separately_from_failure(tmp_path: Path):
    """跳过是**预期**的正常态，不能算成失败。

    算成失败会让台账天天报「全灭」并触发毫无意义的当日重试。
    """
    ctx = _ctx(tmp_path, checkin_hours=[12])
    ctx.pool.upsert_account(AccountAuth(
        uid="g1", realm="global", access_token="G", kind="oauth"))
    await ctx.scheduler.run_now("checkin")
    row = ctx.db.query("SELECT ok, detail FROM task_runs ORDER BY id DESC LIMIT 1")[0]
    assert row["ok"] == 1                      # 跳过按成功记账
    assert row["detail"].startswith("skipped:")


# ---- 余额后台刷新 ----


async def test_refresh_balances_updates_credits(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))

    async def _fake_fetch(auth):
        return {"credits": 777, "expiring": 12, "used": 3}

    ctx.client.fetch_credits = _fake_fetch  # type: ignore[method-assign]
    summary = await ctx.scheduler.refresh_balances()
    assert summary["ok"] == 1
    entry = ctx.pool.get("c1")
    assert entry.credits == 777
    assert entry.credits_expiring == 12


async def test_refresh_balances_survives_per_account_failure(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))

    from keyport.upstream.base import ErrorKind, UpstreamError

    async def _boom(auth):
        raise UpstreamError(ErrorKind.NETWORK, 0, "unreachable")

    ctx.client.fetch_credits = _boom  # type: ignore[method-assign]
    summary = await ctx.scheduler.refresh_balances()
    assert summary["ok"] == 0 and summary["failed"] == 1


async def test_refresh_balances_respects_include_disabled(tmp_path: Path):
    ctx = _ctx(tmp_path, include_disabled_in_tasks=False)
    ctx.pool.upsert_account(AccountAuth(
        uid="c1", realm="cn", access_token="C", kind="oauth"))
    ctx.pool.set_manual_disabled("c1", True, "运维摘除")
    summary = await ctx.scheduler.refresh_balances()
    assert summary["total"] == 0


def test_status_exposes_new_schedule_fields(tmp_path: Path):
    ctx = _ctx(tmp_path, jitter_minutes=7, balance_refresh_minutes=9)
    sched = ctx.scheduler.status()["schedule"]
    assert sched["jitter_minutes"] == 7
    assert sched["balance_refresh_minutes"] == 9
    assert sched["include_disabled_in_tasks"] is True
    assert sched["balance_refresh_enabled"] is True
