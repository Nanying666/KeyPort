"""风控（3012 / unusual activity）隔离与 429 处置。

上游对 messages 端点的 3012 实测是**频道级瞬时频控**：同号同刻 billing 正常、
数小时自愈。硬禁用会让健康的 billing / 领取链路陪葬，故默认指数退避冷却自动
恢复；窗口内累计若干次才升级禁用（需人工确认恢复）。

429 则相反：它不是账号故障，账号**不冷却**，原地按 Retry-After 等待重试，
耗尽后换号且账号保持可用。
"""

from __future__ import annotations

import time

from keyport.config import PoolConfig
from keyport.pool.entry import AccountEntry
from keyport.pool.transitions import apply_error, apply_success, maybe_revive_on_credit
from keyport.upstream.base import ErrorKind, UpstreamError, classify_status, is_risk_control

# 真实上游响应体形态（zcode.z.ai messages 端点 3012 风控）
RISK_BODY = ('{"code":3012,"msg":"unusual activity detected",'
             '"logid":"20261001003028e1991f398fc7e5005c57"}')


def _entry(uid: str = "a", **kw) -> AccountEntry:
    return AccountEntry(uid=uid, **kw)


def _cfg(**kw) -> PoolConfig:
    defaults = {
        "risk_cooldown_seconds": 900,
        "risk_cooldown_max_seconds": 86400,
        "risk_ban_strikes": 4,
        "risk_strike_decay_seconds": 7 * 86400,
        "soft_rate_seconds": 600,
        "soft_rate_max_seconds": 7200,
    }
    defaults.update(kw)
    return PoolConfig(**defaults)


# ---- 分类 ----


def test_classify_risk_by_body_code():
    assert classify_status(405, RISK_BODY) == ErrorKind.RISK


def test_classify_risk_by_http_status():
    """3012 由 HTTP 405 承载，405 本身即风控信号。"""
    assert classify_status(405, "") == ErrorKind.RISK


def test_classify_risk_by_marker_only():
    assert classify_status(400, "unusual activity") == ErrorKind.RISK


def test_is_risk_control_helpers():
    assert is_risk_control(405, "") is True
    assert is_risk_control(400, '"code":3012') is True
    assert is_risk_control(400, '{"code":3007}') is False


def test_risk_takes_precedence_over_captcha_check():
    """风控与验证码互斥：3007 挑战优先判 CAPTCHA，不应误判成风控。"""
    assert classify_status(400, '{"code":3007,"msg":"captcha verify failed"}') \
        == ErrorKind.CAPTCHA


# ---- 指数退避 ----


def test_risk_first_strike_cools_but_keeps_account_enabled():
    """首次命中只冷却，不禁用 —— 3012 多为瞬时频控，数小时自愈。"""
    cfg = _cfg()
    now = int(time.time())
    e = _entry()
    action = apply_error(e, UpstreamError(ErrorKind.RISK, 405, "unusual activity"), cfg, now)
    assert e.enabled is True
    assert e.cooldown_until == now + 900
    assert e.risk_strikes == 1
    assert action.startswith("risk_cooldown:")


def test_risk_backoff_doubles_each_strike():
    cfg = _cfg()
    now = int(time.time())
    e = _entry()
    err = UpstreamError(ErrorKind.RISK, 405, "unusual activity")
    waits = []
    for _ in range(3):
        e.cooldown_until = 0
        apply_error(e, err, cfg, now)
        waits.append(e.cooldown_until - now)
    assert waits == [900, 1800, 3600]


def test_risk_backoff_is_capped():
    cfg = _cfg(risk_cooldown_max_seconds=2000, risk_ban_strikes=99)
    now = int(time.time())
    e = _entry()
    err = UpstreamError(ErrorKind.RISK, 405, "unusual activity")
    for _ in range(10):
        e.cooldown_until = 0
        apply_error(e, err, cfg, now)
    assert e.cooldown_until - now == 2000


def test_risk_escalates_to_disabled_at_ban_strikes():
    """累计到阈值才升级禁用（人工确认恢复）。"""
    cfg = _cfg(risk_ban_strikes=3)
    now = int(time.time())
    e = _entry()
    err = UpstreamError(ErrorKind.RISK, 405, "unusual activity")
    for i in range(3):
        action = apply_error(e, err, cfg, now)
    assert e.enabled is False
    assert e.disabled_reason == "risk_control"
    assert action.startswith("disabled:risk_control")


def test_risk_strikes_decay_after_window():
    """距上次命中超过衰减窗口视为无关新事件，重新起算。

    避免跨月偶发命中累积成禁用。
    """
    cfg = _cfg(risk_ban_strikes=3, risk_strike_decay_seconds=3600)
    now = int(time.time())
    e = _entry()
    err = UpstreamError(ErrorKind.RISK, 405, "unusual activity")
    apply_error(e, err, cfg, now)
    apply_error(e, err, cfg, now)
    assert e.risk_strikes == 2
    # 过了衰减窗口后再命中：重新从 1 起算
    later = now + 7200
    apply_error(e, err, cfg, later)
    assert e.risk_strikes == 1
    assert e.enabled is True


def test_risk_disabled_account_never_revived_by_credit():
    """风控禁用绝不能因额度数字复活 —— 那会让已封禁账号立刻重打上游。"""
    cfg = _cfg(risk_ban_strikes=1)
    now = int(time.time())
    e = _entry(credits=1000)
    apply_error(e, UpstreamError(ErrorKind.RISK, 405, "unusual activity"), cfg, now)
    assert e.enabled is False
    assert e.disabled_reason == "risk_control"
    assert maybe_revive_on_credit(e, now) is False
    assert e.enabled is False


def test_quota_exhausted_still_revives_on_credit():
    """额度耗尽仍可自动复活（只有风控禁用被排除）。"""
    now = int(time.time())
    e = _entry()
    e.enabled = False
    e.disabled_reason = "quota_exhausted"
    e.credits = 50
    assert maybe_revive_on_credit(e, now) is True


# ---- 成功回写 ----


def test_success_clears_risk_strikes_when_not_cooling():
    now = int(time.time())
    e = _entry()
    e.risk_strikes = 2
    e.last_risk_at = now - 10
    apply_success(e, 0.0, now)
    assert e.risk_strikes == 0
    assert e.last_risk_at == 0


def test_success_does_not_clear_strikes_during_cooldown():
    """风控冷却未到期时成功不得洗计数（回退通道成功不代表 Plan 已解除）。"""
    now = int(time.time())
    e = _entry()
    e.risk_strikes = 2
    e.last_risk_at = now - 10
    e.cooldown_until = now + 500
    apply_success(e, 0.0, now)
    assert e.risk_strikes == 2


# ---- 429：不冷却账号 ----


def test_rate_limit_cools_account_at_pool_level():
    """池侧对 429 仍会软冷却（这是熔断保护）；「不冷却」是网关**请求级**的
    原地重试策略 —— 两者不冲突：重试期间不落状态，耗尽后才落。"""
    cfg = _cfg()
    now = int(time.time())
    e = _entry()
    action = apply_error(e, UpstreamError(ErrorKind.RATE_LIMIT, 429), cfg, now)
    assert action.startswith("soft_cooldown:")
    assert e.enabled is True
