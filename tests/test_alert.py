"""可用性告警：边沿触发 + 迟滞 + 重臂 + HMAC 签名。

移植自 sheng-du0521/workbuddy2api 的 `internal/alert/alert.go`。
重点是**不刷屏**：只在 pending→firing 跃迁发一次。
"""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from keyport.alert import (
    DEFAULT_SERVICE_NAME,
    AlertConfig,
    Health,
    Monitor,
    build_payload,
    build_rules,
    sign_body,
    snapshot_of,
    step,
)


class _Src:
    """可控的告警数据源。"""

    def __init__(self) -> None:
        self.data = {"cn": Health(), "global": Health()}
        self.waf = False

    def health(self, realm: str) -> Health:
        return self.data.get(realm, Health())

    def waf_active(self) -> bool:
        return self.waf


def _t(offset: float = 0.0) -> float:
    """基于真实时钟的评估时刻。

    启动宽限用的是真实 `time.time()`（生产语义），所以测试时刻必须相对真实时钟
    取偏移，而不是用一个任意小的常数。默认 +1s 足以越过测试用的极短宽限。
    """
    import time

    return time.time() + 1.0 + offset


def _cfg(**kw) -> AlertConfig:
    base = {"enabled": True, "webhook_url": "https://ops.example/hook",
            "startup_grace_seconds": 0.001, "min_healthy_cn": 1}
    base.update(kw)
    return AlertConfig(**base)


def _monitor(**kw) -> Monitor:
    return Monitor(_cfg(**kw))


# ---- 默认关闭 ----


def test_disabled_by_default():
    m = Monitor(AlertConfig())
    assert m.enabled is False
    assert m.tick(_Src(), now=_t(0)) == []


def test_enabled_requires_url():
    m = Monitor(AlertConfig(enabled=True, webhook_url=""))
    assert m.enabled is False
    assert m.tick(_Src(), now=_t(0)) == []


# ---- 规则构建 ----


def test_zero_threshold_disables_rule():
    """0 = 关闭该规则（纯 cn 部署默认关 global，否则常驻误报）。"""
    rules = build_rules(AlertConfig(min_healthy_cn=1, min_healthy_global=0))
    ids = [(r.id, r.realm) for r in rules]
    assert ("realm_healthy_low", "cn") in ids
    assert ("realm_healthy_low", "global") not in ids
    # WAF 规则始终在（它无阈值）
    assert ("waf_ip_block", "") in ids


def test_breaker_rule_only_when_threshold_set():
    assert not [r for r in build_rules(AlertConfig()) if r.id == "breaker_high"]
    assert [r for r in build_rules(AlertConfig(breaker_threshold=3))
            if r.id == "breaker_high"]


# ---- 边沿触发 ----


def test_alert_fires_once_then_silent():
    """核心：只在跃迁时发一次；firing 期间每拍只更新不发送（不刷屏）。"""
    m = _monitor(for_ticks=1, clear_ticks=2)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)   # 低于阈值

    first = m.tick(src, now=_t(0))
    assert len(first) == 1
    assert first[0]["payload"]["event"] == "alert"

    # 持续越界：不应再发
    for t in (10_030, 10_060, 10_090):
        assert m.tick(src, now=t) == []


def test_for_ticks_requires_consecutive():
    """连续 for_ticks 拍才触发；抖动回落不告警。"""
    m = _monitor(for_ticks=3)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)

    assert m.tick(src, now=_t(0)) == []   # badTicks=1
    assert m.tick(src, now=_t(30)) == []   # badTicks=2
    fired = m.tick(src, now=_t(60))        # badTicks=3 → 触发
    assert len(fired) == 1


def test_for_ticks_one_fires_immediately():
    """for_ticks=1 时本拍就触发（不能退化成需要两拍）。"""
    m = _monitor(for_ticks=1)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)
    assert len(m.tick(src, now=_t(0))) == 1


def test_flap_does_not_alert():
    """抖动（未达 for_ticks 就回落）不告警。"""
    m = _monitor(for_ticks=3)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)
    assert m.tick(src, now=_t(0)) == []
    src.data["cn"] = Health(total=3, healthy=5)   # 回落
    assert m.tick(src, now=_t(30)) == []
    src.data["cn"] = Health(total=3, healthy=0)
    assert m.tick(src, now=_t(60)) == []
    assert m.tick(src, now=_t(90)) == []


def test_rearm_after_recovery():
    """恢复后再次越界才会重新触发（重臂）。"""
    m = _monitor(for_ticks=1, clear_ticks=1, send_resolve=False)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)
    assert len(m.tick(src, now=_t(0))) == 1

    src.data["cn"] = Health(total=3, healthy=5)   # 恢复
    assert m.tick(src, now=_t(30)) == []          # resolve 默认不发

    src.data["cn"] = Health(total=3, healthy=0)   # 再次越界
    fired = m.tick(src, now=_t(60))
    assert len(fired) == 1
    assert fired[0]["payload"]["event"] == "alert"


def test_clear_ticks_hysteresis():
    """恢复需连续 clear_ticks 拍；中途回到告警区则清零恢复计数。"""
    m = _monitor(for_ticks=1, clear_ticks=3, send_resolve=True)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)
    m.tick(src, now=_t(0))

    src.data["cn"] = Health(total=3, healthy=5)
    assert m.tick(src, now=_t(30)) == []   # clear=1
    src.data["cn"] = Health(total=3, healthy=0)
    assert m.tick(src, now=_t(60)) == []   # 回到告警区 → clear 清零
    src.data["cn"] = Health(total=3, healthy=5)
    assert m.tick(src, now=_t(90)) == []   # clear=1
    assert m.tick(src, now=_t(120)) == []   # clear=2
    resolved = m.tick(src, now=_t(150))     # clear=3 → resolve
    assert len(resolved) == 1
    assert resolved[0]["payload"]["event"] == "resolve"


def test_resolve_not_sent_by_default():
    m = _monitor(for_ticks=1, clear_ticks=1, send_resolve=False)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)
    m.tick(src, now=_t(0))
    src.data["cn"] = Health(total=3, healthy=5)
    assert m.tick(src, now=_t(30)) == []


# ---- 阈值语义 ----


def test_strictly_less_than_threshold():
    """严格小于：min=1 表示「健康数为 0 才告警」。"""
    m = _monitor(min_healthy_cn=1, for_ticks=1)
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=1)
    assert m.tick(src, now=_t(0)) == []   # healthy==1 不告警
    src.data["cn"] = Health(total=3, healthy=0)
    assert len(m.tick(src, now=_t(30))) == 1


def test_startup_grace_suppresses_early_alerts():
    """启动宽限：避开启动瞬间账号还没加载完的「0 健康态」误报。"""
    m = Monitor(_cfg(startup_grace_seconds=30, for_ticks=1))
    src = _Src()
    src.data["cn"] = Health(total=0, healthy=0)
    assert m.tick(src, now=m.started_at + 5) == []
    assert len(m.tick(src, now=m.started_at + 31)) == 1


def test_breaker_rule_aggregates_both_realms():
    m = _monitor(min_healthy_cn=0, breaker_threshold=2, for_ticks=1)
    src = _Src()
    src.data["cn"] = Health(breaker=1)
    src.data["global"] = Health(breaker=1)
    fired = m.tick(src, now=_t(0))
    assert len(fired) == 1
    assert fired[0]["payload"]["rule"] == "breaker_high"
    assert fired[0]["payload"]["value"] == 2


def test_waf_rule_fires_when_active():
    m = _monitor(min_healthy_cn=0, for_ticks=1)
    src = _Src()
    src.waf = True
    fired = m.tick(src, now=_t(0))
    assert len(fired) == 1
    assert fired[0]["payload"]["rule"] == "waf_ip_block"


# ---- 载荷与签名 ----


def test_payload_contains_no_secrets():
    """载荷只含池快照，**不含任何凭证、token、uid、会话键**。"""
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=1)
    snap = snapshot_of(src)
    assert set(snap) == {"cn", "global", "waf_active"}
    assert set(snap["cn"]) == {"total", "healthy", "cooling", "disabled",
                              "breaker", "in_flight"}
    blob = json.dumps(snap)
    for banned in ("token", "uid", "cookie", "secret", "password"):
        assert banned not in blob.lower()


def test_signature_is_hmac_sha256():
    body = b'{"x":1}'
    got = sign_body("s3cret", body)
    expected = hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()
    assert got == f"sha256={expected}"


def test_headers_include_signature_only_with_secret():
    m = _monitor(min_healthy_cn=1, for_ticks=1, secret="s3cret")
    src = _Src()
    src.data["cn"] = Health(total=3, healthy=0)
    rec = m.tick(src, now=_t(0))[0]
    assert rec["headers"]["X-KP-Signature"].startswith("sha256=")
    assert rec["headers"]["Content-Type"] == "application/json"
    assert DEFAULT_SERVICE_NAME in rec["headers"]["User-Agent"]

    m2 = _monitor(min_healthy_cn=1, for_ticks=1, secret="")
    m2.started_at = 0
    rec2 = m2.tick(src, now=_t(0))[0]
    assert "X-KP-Signature" not in rec2["headers"]


def test_payload_shape():
    rule = build_rules(AlertConfig(min_healthy_cn=1))[0]
    payload = build_payload(cfg=AlertConfig(service_name="kp"), rule=rule,
                            event="alert", value=0, threshold=1, message="m",
                            since=1000, snapshot={"cn": {}, "global": {},
                                                  "waf_active": False})
    assert payload["version"] == 1
    assert payload["rule"] == "realm_healthy_low"
    assert payload["severity"] == "critical"
    assert payload["realm"] == "cn"
    assert payload["service"] == "kp"
    assert payload["since"] == 1000
    assert "fired_at" in payload


# ---- 归一化 ----


@pytest.mark.parametrize("field,default", [
    ("interval_seconds", 30.0),
    ("timeout_seconds", 5.0),
    ("startup_grace_seconds", 30.0),
    ("for_ticks", 2),
    ("clear_ticks", 2),
    ("recover_healthy", 1),
])
def test_normalize_fills_defaults(field, default):
    m = Monitor(AlertConfig(**{field: 0}))
    assert getattr(m.cfg, field) == default


def test_normalize_keeps_explicit_values():
    m = Monitor(AlertConfig(for_ticks=5, clear_ticks=7, interval_seconds=60))
    assert m.cfg.for_ticks == 5
    assert m.cfg.clear_ticks == 7
    assert m.cfg.interval_seconds == 60


def test_step_returns_empty_when_no_event():
    rule = build_rules(AlertConfig(min_healthy_cn=1))[0]
    event, value, threshold, message = step(rule, _Src(), 10_000, 2, 2)
    assert event == "" and value == 0 and threshold == 0 and message == ""
