"""错误处置接线：冷却截止对齐上游明示的重置墙钟 + 连败降权启用。

参考实现：sheng-du0521/workbuddy2api 的 `internal/pool/cooldown.go`
（`CooldownSoftForModel` 对齐上游重置时刻）与 `internal/pool/degrade.go`（`NoteFailures`）。
"""

from __future__ import annotations

import datetime as _dt
import json

from keyport.config import PoolConfig
from keyport.pool.entry import AccountEntry
from keyport.pool.transitions import apply_error, apply_success
from keyport.upstream.base import ErrorKind, UpstreamError
from keyport.upstream.resetat import UPSTREAM_TZ

NOW = 1_791_000_000


def _entry() -> AccountEntry:
    return AccountEntry(uid="u1", realm="cn")


def _epoch(text: str) -> int:
    return int(
        _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=UPSTREAM_TZ)
        .timestamp()
    )


def _cfg() -> PoolConfig:
    return PoolConfig()


# ---- 6004 对齐上游重置墙钟 ----


def test_model_limit_uses_upstream_reset_wall_clock():
    """核心回归：6004 的冷却截止必须对齐上游明说的时刻，而不是本地猜 600s。

    否则「6004 只冷却该模型、切模型即可用」这个承诺在真实流量里不成立 ——
    一条无 Retry-After、只把时间写在 body 里的 6004 会退化成账号级冷却。
    """
    entry = _entry()
    reset_epoch = NOW + 5400  # 上游说 90 分钟后恢复
    body = json.dumps({"code": 6004, "msg": f"将在 {_dt.datetime.fromtimestamp(reset_epoch, UPSTREAM_TZ).strftime('%Y-%m-%d %H:%M:%S')} 重置"})
    err = UpstreamError(ErrorKind.MODEL_LIMIT, 429, "hy3", body)

    detail = apply_error(entry, err, _cfg(), NOW, model="hy3")

    assert "body-text" in detail
    assert entry.model_cooldowns["hy3"] == reset_epoch
    # 账号级冷却**未被动过** —— 6004 只挡这一个模型
    assert entry.cooldown_until == 0


def test_model_limit_capped_by_local_max():
    """上游明示很长的窗口仍受本地封顶约束（防一条脏数据摘除账号几万年）。

    注意封顶用的是**模型级**上限（比账号级宽）：模型级只挡一个模型、账号仍健康，
    所以可以放长到上游明示时刻；用账号级封顶会截断它，导致在真实窗口里反复撞墙。
    """
    cfg = _cfg()
    entry = _entry()
    far = NOW + 100 * 24 * 3600  # 100 天，远超模型级封顶
    body = json.dumps({"data": {"resets_at": far}})
    err = UpstreamError(ErrorKind.MODEL_LIMIT, 429, "hy3", body)

    apply_error(entry, err, cfg, NOW, model="hy3")
    assert entry.model_cooldowns["hy3"] == NOW + cfg.model_rate_max_seconds


def test_model_limit_not_truncated_by_account_cap():
    """回归：模型级冷却**不得**被账号级封顶截断。

    被截断的后果不是账目错误，而是在真实窗口里反复撞墙：2h 一到该号对这个模型
    重新进入候选 → 打一次必然失败的请求 → 再被 6004 打回、再等 2h……
    """
    cfg = _cfg()
    entry = _entry()
    reset = NOW + 5 * 3600          # 上游说 5 小时后恢复
    assert reset > NOW + cfg.soft_rate_max_seconds   # 超过账号级封顶
    body = json.dumps({"data": {"resets_at": reset}})
    apply_error(entry, UpstreamError(ErrorKind.MODEL_LIMIT, 429, "hy3", body), cfg, NOW,
                model="hy3")

    assert entry.model_cooldowns["hy3"] == reset
    # 权威时刻单独留痕，供自愈判据使用
    assert entry.model_reset_at["hy3"] == reset


def test_model_limit_falls_back_to_local_when_no_time_info():
    entry = _entry()
    err = UpstreamError(ErrorKind.MODEL_LIMIT, 429, "hy3", '{"code":6004}')
    detail = apply_error(entry, err, _cfg(), NOW, model="hy3")
    assert "local" in detail
    assert entry.model_cooldowns["hy3"] == NOW + _cfg().soft_rate_seconds


def test_model_limit_never_shortens_existing_cooldown():
    """并发响应乱序时绝不缩短已有冷却（取更晚的截止）。"""
    entry = _entry()
    entry.model_cooldowns["hy3"] = NOW + 9000
    err = UpstreamError(ErrorKind.MODEL_LIMIT, 429, "hy3",
                        json.dumps({"data": {"resets_at": NOW + 60}}))
    apply_error(entry, err, _cfg(), NOW)
    assert entry.model_cooldowns["hy3"] == NOW + 9000


# ---- 429 对齐 ----


def test_rate_limit_prefers_upstream_reset():
    entry = _entry()
    err = UpstreamError(ErrorKind.RATE_LIMIT, 429, "slow down",
                        json.dumps({"data": {"resets_in": 900}}))
    detail = apply_error(entry, err, _cfg(), NOW)
    assert "body-json" in detail
    assert entry.cooldown_until == NOW + 900


def test_rate_limit_falls_back_to_exp_backoff():
    entry = _entry()
    err = UpstreamError(ErrorKind.RATE_LIMIT, 429, "slow down", '{"code":429}')
    detail = apply_error(entry, err, _cfg(), NOW)
    assert detail.endswith(":local")
    assert entry.cooldown_until > NOW


def test_rate_limit_honours_retry_after_header():
    entry = _entry()
    err = UpstreamError(ErrorKind.RATE_LIMIT, 429, "slow down", '{"code":429}',
                        retry_after=120.0,
                        headers={"retry-after": "120"})
    detail = apply_error(entry, err, _cfg(), NOW)
    assert "header" in detail
    assert entry.cooldown_until == NOW + 120


# ---- 连败降权（启用原本的死字段） ----


def test_bad_request_degrades_after_threshold():
    """BAD_REQUEST 是「不知道原因的失败」：不该熔断（会批量摘除），也不该不管。

    达阈值只**降权**（可逆），等降权期满自动恢复。
    """
    cfg = _cfg()
    entry = _entry()
    err = UpstreamError(ErrorKind.BAD_REQUEST, 400, "bad", "{}")

    for _ in range(cfg.degrade_threshold - 1):
        apply_error(entry, err, cfg, NOW)
    assert entry.degraded_until == 0

    detail = apply_error(entry, err, cfg, NOW)
    assert detail.startswith("degraded:")
    assert entry.degraded_until == NOW + cfg.degrade_cooldown_seconds
    # 计数清零，供下一轮重新累计
    assert entry.consecutive_failures == 0


def test_degrade_does_not_stack_while_active():
    """已在降权期内再次达阈：不延长、不翻倍。

    否则用户重试会把降权越堆越厚。
    """
    cfg = _cfg()
    entry = _entry()
    err = UpstreamError(ErrorKind.BAD_REQUEST, 400, "bad", "{}")
    for _ in range(cfg.degrade_threshold):
        apply_error(entry, err, cfg, NOW)
    first = entry.degraded_until

    for _ in range(cfg.degrade_threshold + 2):
        apply_error(entry, err, cfg, NOW)
    assert entry.degraded_until == first


def test_success_clears_degrade_but_not_breaker():
    """成功即证明账号可用 → 清降权；但熔断位由显式冷却时长表达，不提前清。"""
    entry = _entry()
    entry.degraded_until = NOW + 600
    entry.breaker_until = NOW + 600
    apply_success(entry, 0.0, NOW)
    assert entry.degraded_until == 0
    assert entry.breaker_until == NOW + 600


def test_degrade_makes_entry_unhealthy():
    """降权必须真的影响健康判定，否则等于没做。"""
    cfg = _cfg()
    entry = _entry()
    err = UpstreamError(ErrorKind.BAD_REQUEST, 400, "bad", "{}")
    for _ in range(cfg.degrade_threshold):
        apply_error(entry, err, cfg, NOW)
    assert entry.healthy(NOW) is False
    assert entry.cooldown_reason(NOW) == "degraded"
    # 降权期满后自动恢复
    assert entry.healthy(entry.degraded_until + 1) is True
