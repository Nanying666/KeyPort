"""账号池条目：单账号的运行时状态与健康判定。

状态位（互相独立，全部清空才回到选号池）：
  - enabled         系统级启停（额度耗尽/鉴权失效自动置位）
  - manual_disabled 运维临时摘除（对话流量摘除，签到/保活照常）
  - cooldown_until  错误冷却（429 软冷却 / 404 短冷却 / 402 硬冷却）
  - breaker_until   连续失败熔断
  - model_cooldowns 模型级限流（只冷却触发调用的模型）
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ..config import PoolConfig


@dataclass
class AccountEntry:
    """单账号运行时状态。"""

    uid: str
    realm: str = "cn"
    nickname: str = ""
    enterprise_id: str = ""
    domain: str = ""
    file_path: str = ""
    kind: str = "oauth"

    # 额度
    credits: int = 0
    credits_used: int = 0
    credits_expiring: int = 0
    credits_updated_at: int = 0

    # 计数
    total_calls: int = 0
    failed_calls: int = 0
    consecutive_failures: int = 0
    last_used_at: int = 0
    last_error: str = ""
    last_checkin_at: int = 0

    # 状态位
    enabled: bool = True
    manual_disabled: bool = False
    disabled_reason: str = ""
    cooldown_until: int = 0
    breaker_until: int = 0
    degraded_until: int = 0
    model_cooldowns: dict[str, int] = field(default_factory=dict)

    # 在途租约
    inflight: int = 0
    token_expires_at: int = 0
    refresh_token: str = ""

    # 风控（3012 / unusual activity）：指数退避 + 窗口内累计升级禁用
    risk_strikes: int = 0
    last_risk_at: int = 0

    # 设备档案（fingerprint.DeviceProfile 或其 dict 形态；一号一台）
    fingerprint: dict = field(default_factory=dict)
    # 安装序完成时间（0 = 未完成；幂等门）
    installed_at: int = 0

    # 账号级出站代理（出口线路）：'' = 未指定（跟随全局默认），'direct' = 显式直连，
    # 其余为代理 URL。N 个账号共用同一出口 IP 会在上游聚成「一个 IP 挂 N 个号」的
    # 强关联信号，按账号分散出口是防封的主要手段。
    egress_proxy: str = ""
    # 最近一次出口探活结果（诊断用）
    egress_checked_at: int = 0
    egress_ip: str = ""
    egress_error: str = ""

    @property
    def is_enterprise(self) -> bool:
        """企业版账号（无个人成长体系）。上游打成长端点直接 403，属零收益白跑。"""
        return bool(self.enterprise_id)

    # 账号级限额护栏的当日计数（None = 未知 / 护栏关闭 → 一律放行）
    daily_tokens_today: int | None = None
    daily_credits_today: float | None = None
    model_daily_tokens: dict[str, int] | None = None
    # 最近一次被护栏挡住的原因（诊断用，不参与选号判定）
    guard_blocked_reason: str = ""

    # ---- 健康判定 ----

    def in_pool(self) -> bool:
        """是否参与选号（系统启停 + 运维摘除双开关）。"""
        return self.enabled and not self.manual_disabled

    def healthy(self, now: int) -> bool:
        if not self.in_pool():
            return False
        return not (self.cooldown_until > now or self.breaker_until > now
                    or self.degraded_until > now)

    def healthy_for_model(self, now: int, model: str) -> bool:
        """模型感知健康口径：6004 只冷却该模型，切其他模型立即可用。"""
        if not self.healthy(now):
            return False
        if model and self.model_cooldowns.get(model, 0) > now:
            return False
        return True

    def has_capacity(self, cfg: PoolConfig) -> bool:
        limit = cfg.max_in_flight_global if self.realm == "global" else cfg.max_in_flight
        return self.inflight < max(1, limit)

    def cooldown_remaining(self, now: int) -> int:
        return max(0, max(self.cooldown_until, self.breaker_until, self.degraded_until) - now)

    def cooldown_reason(self, now: int) -> str:
        if self.breaker_until > now:
            return "breaker"
        if self.degraded_until > now:
            return "degraded"
        if self.cooldown_until > now:
            return "cooldown"
        if not self.enabled:
            return "disabled"
        if self.manual_disabled:
            return "manual_disabled"
        return ""

    # ---- 风控退避 ----

    def risk_penalty(
        self, base: int, cap: int, ban_strikes: int, decay_seconds: int, now: int
    ) -> bool:
        """命中风控（3012 / unusual activity）：指数退避冷却，累计升级禁用。

        上游对 messages 端点的 3012 实测是**频道级瞬时频控**：同号同刻 billing
        正常、数小时自愈。硬禁用会让健康的 billing / claim 陪葬，故默认冷却自动
        恢复；窗口内累计 `ban_strikes` 次才升级禁用（需人工确认恢复）。

        strikes 只在 `decay_seconds` 窗口内累计：距上次命中超过窗口视为无关的
        新事件，重新起算 —— 避免跨月偶发命中累积成禁用。

        返回是否已升级为禁用。
        """
        if self.last_risk_at and now - self.last_risk_at > decay_seconds:
            self.risk_strikes = 0
        self.risk_strikes += 1
        self.last_risk_at = now
        if self.risk_strikes >= max(1, ban_strikes):
            self.enabled = False
            self.disabled_reason = "risk_control"
            self.cooldown_until = 0
            return True
        wait = min(base * (2 ** (self.risk_strikes - 1)), cap)
        self.cooldown_until = max(self.cooldown_until, now + wait)
        return False

    # ---- 三因子权重 ----

    def expiring_ratio(self) -> float:
        """快过期积分占比：窗口内有到期的积分优先消耗。"""
        if self.credits <= 0 or self.credits_expiring <= 0:
            return 0.0
        return min(1.0, self.credits_expiring / float(self.credits))

    def idle_weight(self, now: int, cfg: PoolConfig) -> float:
        if not self.last_used_at:
            return cfg.idle_weight_max  # 从未使用给满分
        hours = max(0.0, (now - self.last_used_at) / 3600.0)
        return min(hours * cfg.idle_weight_per_hour, cfg.idle_weight_max)

    def to_state(self) -> dict:
        """序列化运行时状态（供 state.json 落盘）。"""
        return {
            "cooldown_until": self.cooldown_until,
            "breaker_until": self.breaker_until,
            "degraded_until": self.degraded_until,
            "model_cooldowns": dict(self.model_cooldowns),
            "consecutive_failures": self.consecutive_failures,
            "enabled": self.enabled,
            "manual_disabled": self.manual_disabled,
            "disabled_reason": self.disabled_reason,
            "last_used_at": self.last_used_at,
            "last_error": self.last_error,
            "credits": self.credits,
            "credits_used": self.credits_used,
            "credits_expiring": self.credits_expiring,
            "credits_updated_at": self.credits_updated_at,
            "total_calls": self.total_calls,
            "failed_calls": self.failed_calls,
            "last_checkin_at": self.last_checkin_at,
            "risk_strikes": self.risk_strikes,
            "last_risk_at": self.last_risk_at,
            "fingerprint": dict(self.fingerprint or {}),
            "installed_at": self.installed_at,
        }

    def load_state(self, state: dict, now: int) -> None:
        """从 state.json 恢复运行时状态（已过期的冷却不复活）。"""
        self.cooldown_until = int(state.get("cooldown_until", 0) or 0)
        self.breaker_until = int(state.get("breaker_until", 0) or 0)
        self.degraded_until = int(state.get("degraded_until", 0) or 0)
        raw_mc = state.get("model_cooldowns") or {}
        if isinstance(raw_mc, dict):
            self.model_cooldowns = {
                str(k): int(v) for k, v in raw_mc.items() if int(v or 0) > now
            }
        self.consecutive_failures = int(state.get("consecutive_failures", 0) or 0)
        self.enabled = bool(state.get("enabled", True))
        self.manual_disabled = bool(state.get("manual_disabled", False))
        self.disabled_reason = str(state.get("disabled_reason", ""))
        self.last_used_at = int(state.get("last_used_at", 0) or 0)
        self.last_error = str(state.get("last_error", ""))
        self.credits = int(state.get("credits", 0) or 0)
        self.credits_used = int(state.get("credits_used", 0) or 0)
        self.credits_expiring = int(state.get("credits_expiring", 0) or 0)
        self.credits_updated_at = int(state.get("credits_updated_at", 0) or 0)
        self.total_calls = int(state.get("total_calls", 0) or 0)
        self.failed_calls = int(state.get("failed_calls", 0) or 0)
        self.last_checkin_at = int(state.get("last_checkin_at", 0) or 0)
        self.risk_strikes = int(state.get("risk_strikes", 0) or 0)
        self.last_risk_at = int(state.get("last_risk_at", 0) or 0)
        raw_fp = state.get("fingerprint")
        if isinstance(raw_fp, dict):
            self.fingerprint = raw_fp
        self.installed_at = int(state.get("installed_at", 0) or 0)


def next_midnight_four(now: int) -> int:
    """402 硬冷却目标时刻：次日 04:00（本地时区）。"""
    lt = time.localtime(now)
    today4 = int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 4, 0, 0, 0, 0, -1)))
    return today4 if today4 > now else today4 + 86400
