"""错误 → 冷却 / 熔断 / 降权状态机。

分级处置（对应开发文档 §3.2）：
  429 软冷却 600s 起指数退避、封顶 soft_rate_max
  404 固定 60s 短冷却
  402 硬冷却至次日 04:00
  6004 模型级限流：只冷却触发调用的模型
  风控（3012 / unusual activity）→ 指数退避，窗口内累计升级禁用
  连续失败达阈值 → 熔断（指数退避封顶 breaker_cooldown_max）
  401/403 → 系统级禁用（需刷新或重新登录）
"""

from __future__ import annotations

from ..config import PoolConfig
from ..upstream.base import ErrorKind, UpstreamError
from .entry import AccountEntry, next_midnight_four


def _exp_backoff(base: int, attempt: int, cap: int) -> int:
    """指数退避：base * 2^(attempt-1)，封顶 cap。"""
    attempt = max(1, attempt)
    value = base * (2 ** (attempt - 1))
    return min(value, cap)


def apply_error(
    entry: AccountEntry,
    err: UpstreamError,
    cfg: PoolConfig,
    now: int,
) -> str:
    """按错误类型更新账号状态；返回人类可读的处置说明。"""
    entry.failed_calls += 1
    entry.consecutive_failures += 1
    entry.last_error = f"{err.kind.value}:{err.message or err.status}"
    kind = err.kind

    if kind == ErrorKind.MODEL_LIMIT:
        # 只冷却触发调用的模型
        model = err.message or ""
        if model:
            entry.model_cooldowns[model] = now + cfg.soft_rate_seconds
            return f"model_cooldown:{model}:{cfg.soft_rate_seconds}s"
        entry.cooldown_until = max(entry.cooldown_until, now + cfg.soft_rate_seconds)
        return f"cooldown:{cfg.soft_rate_seconds}s"

    if kind == ErrorKind.RATE_LIMIT:
        wait = _exp_backoff(cfg.soft_rate_seconds, entry.consecutive_failures,
                            cfg.soft_rate_max_seconds)
        if err.retry_after > 0:
            wait = max(wait, int(err.retry_after))
        entry.cooldown_until = max(entry.cooldown_until, now + wait)
        return f"soft_cooldown:{wait}s"

    if kind == ErrorKind.NOT_FOUND:
        entry.cooldown_until = max(entry.cooldown_until, now + cfg.not_found_seconds)
        return f"not_found_cooldown:{cfg.not_found_seconds}s"

    if kind == ErrorKind.PAYMENT:
        target = next_midnight_four(now)
        entry.cooldown_until = max(entry.cooldown_until, target)
        return f"hard_cooldown_until:{target}"

    if kind == ErrorKind.AUTH:
        entry.enabled = False
        entry.disabled_reason = "auth_failed"
        return "disabled:auth_failed"

    if kind == ErrorKind.RISK:
        banned = entry.risk_penalty(
            cfg.risk_cooldown_seconds, cfg.risk_cooldown_max_seconds,
            cfg.risk_ban_strikes, cfg.risk_strike_decay_seconds, now,
        )
        if banned:
            return f"disabled:risk_control:{entry.risk_strikes}"
        remaining = max(0, entry.cooldown_until - now)
        return f"risk_cooldown:{remaining}s:strike{entry.risk_strikes}"

    if kind in (ErrorKind.SERVER, ErrorKind.NETWORK, ErrorKind.UNKNOWN):
        # 连续失败达阈值 → 熔断
        if entry.consecutive_failures >= cfg.breaker_threshold:
            wait = _exp_backoff(cfg.breaker_cooldown_seconds,
                                entry.consecutive_failures - cfg.breaker_threshold + 1,
                                cfg.breaker_cooldown_max_seconds)
            entry.breaker_until = max(entry.breaker_until, now + wait)
            return f"breaker:{wait}s"
        return "counted"

    if kind == ErrorKind.CAPTCHA:
        # 上游端点要求人机验证：同域所有账号都会被拦，非单个账号的过错。
        # 不冷却、不熔断、不换号，也不计入连败（抵消开头的自增）。
        entry.consecutive_failures = max(0, entry.consecutive_failures - 1)
        entry.failed_calls = max(0, entry.failed_calls - 1)
        return "captcha_required"

    # BAD_REQUEST：请求本身问题，不该换号，也不该惩罚账号
    entry.consecutive_failures = max(0, entry.consecutive_failures - 1)
    entry.failed_calls = max(0, entry.failed_calls - 1)
    return "client_error"


def apply_success(entry: AccountEntry, credits_used: float, now: int) -> None:
    """成功请求：清零连败与风控计数，记录用量与时间。

    风控冷却**未到期**时不洗状态：上游风控标记为小时级粘性，回退通道成功
    不代表 Plan 通道已解除，洗掉会立刻重打 Plan 并累加计数。
    """
    entry.total_calls += 1
    entry.consecutive_failures = 0
    entry.last_used_at = now
    entry.last_error = ""
    if entry.cooldown_until <= now:
        entry.risk_strikes = 0
        entry.last_risk_at = 0
    if credits_used > 0:
        entry.credits_used += int(credits_used)
        entry.credits = max(0, entry.credits - int(credits_used))


def mark_exhausted(entry: AccountEntry, reason: str = "quota_exhausted") -> None:
    """额度耗尽 → 系统级禁用（恢复时由额度刷新自动复活）。"""
    entry.enabled = False
    entry.disabled_reason = reason


def record_rate_limited(entry: AccountEntry, err: UpstreamError) -> None:
    """429 专用：只留痕，**不施加任何冷却**。

    429 是频控而非账号故障 —— 账号必须保持可用（耗尽后换号，账号仍能被
    后续请求选中）。连败计数同样不动：让它参与熔断阈值会让频控把健康账号
    熔断掉。冷却只由真正表示账号故障的错误类型（5xx / 连接失败）施加。
    """
    entry.failed_calls += 1
    entry.last_error = f"{err.kind.value}:{err.message or err.status}"


def cool_down(entry: AccountEntry, seconds: int, reason: str, now: int) -> None:
    """显式冷却账号（5xx 重试耗尽 / 连接失败）。

    与 `apply_error` 的熔断路径分开：熔断要连续失败达阈值才触发，而「重试
    N 次仍然 5xx」本身就是充分的冷却理由，不该再等连败累积。
    """
    entry.failed_calls += 1
    entry.consecutive_failures += 1
    entry.last_error = reason
    entry.cooldown_until = max(entry.cooldown_until, now + max(0, seconds))


def revive(entry: AccountEntry, now: int) -> None:
    """恢复账号：清空所有冷却与禁用位（运维手动恢复 / 额度恢复自动复活）。"""
    entry.enabled = True
    entry.disabled_reason = ""
    entry.cooldown_until = 0
    entry.breaker_until = 0
    entry.degraded_until = 0
    entry.model_cooldowns.clear()
    entry.consecutive_failures = 0
    entry.risk_strikes = 0
    entry.last_risk_at = 0


def maybe_revive_on_credit(entry: AccountEntry, now: int) -> bool:
    """额度恢复自动复活：额度耗尽被禁用的账号一旦有可用额度即回到池内。

    只认 `quota_exhausted`：风控禁用（risk_control）绝不能因额度数字复活，
    那会让已封禁账号立刻重新打上游。
    """
    if entry.enabled or entry.disabled_reason != "quota_exhausted":
        return False
    if entry.credits > 0:
        revive(entry, now)
        return True
    return False
