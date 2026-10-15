"""账号池可用性阈值告警：周期评估只读健康快照，越界时 POST 到运维自有 webhook。

## 设计纪律（与网关主流程隔离）

- **默认关闭**：`enabled=false` 时不起循环、不发任何 HTTP，行为零变化。
- **不碰请求热路径**：只经只读访问器取值，评估在独立循环里跑。
- **零新增上游请求**：webhook 目标是运维自有端点，从不调用上游。
- **边沿触发 + 迟滞 + 重臂**：只在 `pending → firing` 跃迁发一次；`firing` 期间
  每拍只更新不发送（天然去重，不刷屏）；恢复需连续 `clear_ticks` 次不满足才回
  `ok`，之后再次越界才会重新触发。抖动（未达 `for_ticks` 就回落）不告警。
- **零第三方依赖**：httpx 已在依赖里，HMAC 用标准库 `hmac`/`hashlib`。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("keyport.alert")

# 默认值（零值/负值回落）
DEFAULT_INTERVAL = 30.0
DEFAULT_TIMEOUT = 5.0
DEFAULT_FOR_TICKS = 2
DEFAULT_CLEAR_TICKS = 2
DEFAULT_STARTUP_GRACE = 30.0
DEFAULT_RECOVER = 1
DEFAULT_SERVICE_NAME = "keyport"

# 状态机
STATE_OK = "ok"
STATE_PENDING = "pending"
STATE_FIRING = "firing"


@dataclass
class Health:
    """单个域的池健康快照。"""

    total: int = 0
    healthy: int = 0
    cooling: int = 0
    disabled: int = 0
    breaker: int = 0
    degraded: int = 0
    in_flight: int = 0


@dataclass
class AlertConfig:
    """告警配置。零值即「全部关闭」。"""

    enabled: bool = False
    webhook_url: str = ""
    # 非空时对 body 做 HMAC-SHA256 签名，写入 `X-KP-Signature` 头
    secret: str = ""
    interval_seconds: float = DEFAULT_INTERVAL
    timeout_seconds: float = DEFAULT_TIMEOUT
    startup_grace_seconds: float = DEFAULT_STARTUP_GRACE
    # 各域最低可接受健康账号数：**低于**该值才告警（min=1 即「健康数为 0 时告警」）。
    # **0 = 关闭该规则** —— 纯 cn 部署没有 global 账号，默认开启会常驻误报。
    min_healthy_cn: int = 0
    min_healthy_global: int = 0
    # 恢复阈值（迟滞上沿），应 > min_healthy_*
    recover_healthy: int = DEFAULT_RECOVER
    # 熔断中账号数阈值；0 = 关闭
    breaker_threshold: int = 0
    # 连续满足 N 拍才触发 / 连续不满足 N 拍才解除
    for_ticks: int = DEFAULT_FOR_TICKS
    clear_ticks: int = DEFAULT_CLEAR_TICKS
    # 恢复时是否也发一条 `event=resolve`
    send_resolve: bool = False
    service_name: str = DEFAULT_SERVICE_NAME


def _normalize(cfg: AlertConfig) -> AlertConfig:
    """补默认值（不改变显式配置的语义）。"""
    if cfg.interval_seconds <= 0:
        cfg.interval_seconds = DEFAULT_INTERVAL
    if cfg.timeout_seconds <= 0:
        cfg.timeout_seconds = DEFAULT_TIMEOUT
    if cfg.startup_grace_seconds <= 0:
        cfg.startup_grace_seconds = DEFAULT_STARTUP_GRACE
    if cfg.for_ticks <= 0:
        cfg.for_ticks = DEFAULT_FOR_TICKS
    if cfg.clear_ticks <= 0:
        cfg.clear_ticks = DEFAULT_CLEAR_TICKS
    if cfg.recover_healthy <= 0:
        cfg.recover_healthy = DEFAULT_RECOVER
    if not cfg.service_name:
        cfg.service_name = DEFAULT_SERVICE_NAME
    return cfg


@dataclass
class Rule:
    """一条告警规则 + 其状态机。

    事件只在 `pending → firing` 的跃迁上产生一次；`firing` 期间每拍只更新
    `clear_count`、不产生事件 —— 这就是「不刷屏」的全部机制，无需额外的冷却计时器。
    """

    id: str
    severity: str
    realm: str
    sample: Callable[[Any], tuple[float, bool, bool, float, str]]
    state: str = STATE_OK
    bad_ticks: int = 0
    clear_count: int = 0
    since: float = 0.0


def build_rules(cfg: AlertConfig) -> list[Rule]:
    """按配置构建规则集。阈值为 0 的规则不构建（= 关闭）。"""
    rules: list[Rule] = []

    for realm, minimum in (("cn", cfg.min_healthy_cn),
                           ("global", cfg.min_healthy_global)):
        if minimum <= 0:
            continue  # 0 = 关闭

        def _make(realm: str = realm, minimum: int = minimum):
            def _sample(src: Any) -> tuple[float, bool, bool, float, str]:
                value = float(src.health(realm).healthy)
                # **严格小于**：min=1 表示「健康数低于 1（即 0）才告警」。
                # 不能用 <= —— 那会让 healthy==1 也告警，与「最低可接受健康数」
                # 的语义不符，且 min=1（推荐默认）会常驻误报。
                bad = value < float(minimum)
                recovered = value >= float(cfg.recover_healthy)
                return (value, bad, recovered, float(minimum),
                        f"{realm} 域健康账号数 {int(value)} 低于阈值 {minimum}")
            return _sample

        rules.append(Rule(id="realm_healthy_low", severity="critical",
                          realm=realm, sample=_make()))

    if cfg.breaker_threshold > 0:
        threshold = cfg.breaker_threshold

        def _breaker(src: Any) -> tuple[float, bool, bool, float, str]:
            value = float(src.health("cn").breaker + src.health("global").breaker)
            return (value, value >= float(threshold), value < float(threshold),
                    float(threshold),
                    f"熔断中账号数 {int(value)} 达到阈值 {threshold}")

        rules.append(Rule(id="breaker_high", severity="warning",
                          realm="", sample=_breaker))

    def _waf(src: Any) -> tuple[float, bool, bool, float, str]:
        active = bool(src.waf_active())
        return (1.0 if active else 0.0, active, not active, 1.0,
                "IP 级 WAF 拦截生效中（对话轮转正在快速失败）")

    rules.append(Rule(id="waf_ip_block", severity="warning", realm="", sample=_waf))
    return rules


def step(rule: Rule, src: Any, now: float, for_ticks: int,
         clear_ticks: int) -> tuple[str, float, float, str]:
    """推进一拍；返回 `(事件, 值, 阈值, 文案)`，无事件时事件为空串。"""
    value, bad, recovered, threshold, message = rule.sample(src)

    if rule.state == STATE_OK:
        if not bad:
            return "", 0.0, 0.0, ""
        # 首次越界 → 进入 pending。**必须继续往下走**（不能 return）：
        # for_ticks=1 时本拍就应满足阈值并触发；在此 return 会让 for_ticks=1
        # 退化成「需要连续两拍」。
        rule.state = STATE_PENDING
        rule.bad_ticks = 0

    if rule.state == STATE_PENDING:
        if not bad:
            # 抖动回落：未达 for_ticks 就不算「持续」，直接回 ok（不告警）
            rule.state = STATE_OK
            rule.bad_ticks = 0
            return "", 0.0, 0.0, ""
        rule.bad_ticks += 1
        if rule.bad_ticks >= for_ticks:
            rule.state = STATE_FIRING
            rule.since = now
            rule.clear_count = 0
            return "alert", value, threshold, message
        return "", 0.0, 0.0, ""

    # STATE_FIRING
    if recovered:
        rule.clear_count += 1
        if rule.clear_count >= clear_ticks:
            rule.state = STATE_OK
            rule.bad_ticks = 0
            rule.clear_count = 0
            return "resolve", value, threshold, message
    else:
        # 仍在告警区：清零恢复计数并**不发送**（去重的唯一实现点）
        rule.clear_count = 0
    return "", 0.0, 0.0, ""


def build_payload(*, cfg: AlertConfig, rule: Rule, event: str, value: float,
                  threshold: float, message: str, since: float,
                  snapshot: dict) -> dict:
    """构造 webhook 请求体。

    接收端幂等键 = `rule + realm + since`。**不含任何凭证、token、uid、会话键**。
    """
    return {
        "version": 1,
        "event": event,
        "service": cfg.service_name,
        "rule": rule.id,
        "severity": rule.severity,
        "realm": rule.realm,
        "value": value,
        "threshold": threshold,
        "message": message,
        "since": int(since),
        "fired_at": int(time.time()),
        "snapshot": snapshot,
    }


def sign_body(secret: str, body: bytes) -> str:
    """HMAC-SHA256 签名（十六进制），写入 `X-KP-Signature` 头。"""
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@dataclass
class Monitor:
    """告警监控器（纯状态机，投递由调用方注入，便于测试）。"""

    cfg: AlertConfig
    rules: list[Rule] = field(default_factory=list)
    started_at: float = field(default_factory=time.time)
    events: list[dict] = field(default_factory=list)
    _seen_alert: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.cfg = _normalize(self.cfg)
        if not self.rules:
            self.rules = build_rules(self.cfg)

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.enabled and self.cfg.webhook_url)

    def tick(self, src: Any, now: float | None = None) -> list[dict]:
        """评估一拍；返回本拍产生的事件（含已签名的 body）。"""
        if not self.enabled:
            return []
        now = float(now if now is not None else time.time())
        # 启动宽限：避开启动瞬间账号还没加载完的「0 健康态」误报
        if now - self.started_at < self.cfg.startup_grace_seconds:
            return []

        out: list[dict] = []
        for rule in self.rules:
            event, value, threshold, message = step(
                rule, src, now, self.cfg.for_ticks, self.cfg.clear_ticks)
            if not event:
                continue
            if event == "resolve" and not self.cfg.send_resolve:
                continue
            payload = build_payload(
                cfg=self.cfg, rule=rule, event=event, value=value,
                threshold=threshold, message=message, since=rule.since,
                snapshot=snapshot_of(src),
            )
            body = json.dumps(payload, ensure_ascii=False).encode()
            record = {
                "payload": payload,
                "body": body,
                "headers": self._headers(body),
                "url": self.cfg.webhook_url,
            }
            out.append(record)
            self.events.append(record)
        return out

    def _headers(self, body: bytes) -> dict[str, str]:
        headers = {"Content-Type": "application/json",
                   "User-Agent": f"{self.cfg.service_name}-alert/1"}
        if self.cfg.secret:
            headers["X-KP-Signature"] = sign_body(self.cfg.secret, body)
        return headers


def snapshot_of(src: Any) -> dict:
    """池快照（不含任何凭证、token、uid、会话键）。"""
    def _realm(name: str) -> dict:
        h = src.health(name)
        return {"total": h.total, "healthy": h.healthy, "cooling": h.cooling,
                "disabled": h.disabled, "breaker": h.breaker,
                "in_flight": h.in_flight}

    return {"cn": _realm("cn"), "global": _realm("global"),
            "waf_active": bool(src.waf_active())}
