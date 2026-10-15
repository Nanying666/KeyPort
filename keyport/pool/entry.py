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

# ---- 运行态枚举（值稳定，前端按字符串匹配）----

RUN_ONLINE = "online"                    # 正常可用
RUN_COOLING = "cooling"                  # 一般冷却（自动恢复）
RUN_RATE_LIMITED = "rate_limited"        # 限流冷却（账号级：429 / WAF）—— 等一会儿就好
RUN_RISK_COOLING = "risk_cooling"        # 风控冷却（11140 / 11128 / WAF）—— 等下去不会好
RUN_EXHAUSTED = "exhausted"              # 额度/积分耗尽（等到次日或充值）
RUN_RELOGIN = "relogin"                  # 需重新登录 / 会话失效（必须人工）
RUN_EGRESS_ERROR = "egress_error"        # 出口配置不可用（不修会从本机 IP 裸奔）
RUN_DISABLED = "disabled"                # 系统判定不可用
RUN_MANUAL_DISABLED = "manual_disabled"  # 运维手动停用

RUN_STATES = (
    RUN_ONLINE, RUN_COOLING, RUN_RATE_LIMITED, RUN_RISK_COOLING,
    RUN_EXHAUSTED, RUN_RELOGIN, RUN_EGRESS_ERROR, RUN_DISABLED,
    RUN_MANUAL_DISABLED,
)


def _is_relogin_reason(reason: str) -> bool:
    """会话失效 / 试用未激活 —— 都要人工动作，等不来。"""
    return any(k in reason for k in
               ("14017", "12153", "重登", "重新登录", "session", "trial",
                "auth_failed", "no credential"))


def _is_exhausted_reason(reason: str) -> bool:
    """额度/积分耗尽 —— 要么等到次日 04:00，要么充值，不是故障。"""
    return any(k in reason for k in
               ("quota", "余额", "积分不足", "额度耗尽", "402", "exhaust"))


def _is_risk_reason(reason: str) -> bool:
    """风控类 —— **关键区分点**：这类冷却等下去不会好，必须先看出口与请求形态。"""
    return any(k in reason for k in
               ("11140", "11128", "风控", "risk", "request illegal", "waf",
                "指纹", "非法"))


def _is_rate_limit_reason(reason: str) -> bool:
    """限流类 —— 可自动恢复，但应看退避决定要不要换个出口。"""
    return any(k in reason for k in
               ("429", "6004", "rate limit", "限流", "soft"))


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
    # 国际版每日活跃打卡：最近一次**成功**完成的日期时间串（`YYYY-MM-DD HH:MM:SS`）。
    #
    # 只在网页腿真正跑完时写入 —— 桌面腿成功或网页腿失败都不写。写早了会让当天
    # 后续巡检直接跳过该账号，那天的积分静默丢掉且不再重试。
    last_daily_chat: str = ""

    # 状态位
    enabled: bool = True
    manual_disabled: bool = False
    disabled_reason: str = ""
    cooldown_until: int = 0
    breaker_until: int = 0
    degraded_until: int = 0
    model_cooldowns: dict[str, int] = field(default_factory=dict)
    # 模型级冷却的**权威重置时刻**（上游明示）。与 `model_cooldowns`（实际截止，
    # 受本地封顶约束）分开存：两者不等即说明截止被截断过，是自愈的判据来源。
    model_reset_at: dict[str, int] = field(default_factory=dict)

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

    def run_state(self, now: int) -> str:
        """账号运行态（九态枚举，供面板与告警使用）。

        把散在布尔位与自由文案里的处境收敛成一个**显式枚举**。此前「为什么不在池里」
        只能靠三个布尔位加一段自由文案自己拼，运维看到的典型困惑是：

        - 「冷却中」既可能是限流（一分钟后自己好），也可能是风控（说明出口或请求
          形态有问题，**等下去不会好**）—— 两者**处置方向相反**，却显示成同一个东西；
        - 「额度耗尽」与「需要重新登录」都被压成 disabled/cooling，无法一眼分辨
          「等一等 / 充点钱」与「必须人工重登」。

        判定顺序**先「不可自动恢复」，后「可自动恢复」**：

            manual_disabled > disabled > egress_error > relogin > exhausted
            > risk_cooling > rate_limited > cooling > online

        两个顺序上的刻意选择：

        - `manual_disabled` 排第一：那是**人的决定**，任何系统状态都不该把它盖住 ——
          否则运维会看到「冷却中」而去等它自动恢复，实际它永远不会回来。
        - `egress_error` 排在冷却类之前：出口配置坏掉的账号**看起来完全健康**
          （凭证有效、积分充足），但它一被选中就会从本机 IP 出去 —— 这是最该被
          立刻看见的一类。
        """
        if self.manual_disabled:
            return RUN_MANUAL_DISABLED
        if not self.enabled:
            # 系统禁用位之下再细分「等得来」与「等不来」
            reason = (self.disabled_reason or "").lower()
            if _is_relogin_reason(reason):
                return RUN_RELOGIN
            if _is_exhausted_reason(reason):
                return RUN_EXHAUSTED
            if _is_risk_reason(reason):
                return RUN_RISK_COOLING
            return RUN_DISABLED
        if self.egress_error:
            return RUN_EGRESS_ERROR

        reason = (self.last_error or "").lower()
        if self.cooldown_until > now:
            if _is_risk_reason(reason):
                return RUN_RISK_COOLING
            if _is_rate_limit_reason(reason):
                return RUN_RATE_LIMITED
            if _is_relogin_reason(reason):
                return RUN_RELOGIN
            if _is_exhausted_reason(reason):
                return RUN_EXHAUSTED
            return RUN_COOLING
        if self.breaker_until > now or self.degraded_until > now:
            return RUN_COOLING
        # 模型级冷却**不算账号级不可用**：run_state 回答的是"这个号整体能不能用"，
        # 而"某个模型被限"是独立维度（由模型冷却明细表达）。
        return RUN_ONLINE

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
            "model_reset_at": dict(self.model_reset_at),
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
            "last_daily_chat": self.last_daily_chat,
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
        raw_reset = state.get("model_reset_at") or {}
        if isinstance(raw_reset, dict):
            self.model_reset_at = {
                str(k): int(v) for k, v in raw_reset.items() if int(v or 0) > 0
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
        self.last_daily_chat = str(state.get("last_daily_chat", "") or "")
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
