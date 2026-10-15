"""账号运行态九态。

移植自 sheng-du0521/workbuddy2api 的 `cmd/webapp/runstate.go`。
核心价值是**区分「等一会儿就好」与「等下去不会好」** —— 两者处置方向相反，
此前却显示成同一个「冷却中」。
"""

from __future__ import annotations

import pytest

from keyport.pool.entry import (
    RUN_COOLING,
    RUN_DISABLED,
    RUN_EGRESS_ERROR,
    RUN_EXHAUSTED,
    RUN_MANUAL_DISABLED,
    RUN_ONLINE,
    RUN_RATE_LIMITED,
    RUN_RELOGIN,
    RUN_RISK_COOLING,
    RUN_STATES,
    AccountEntry,
)

NOW = 1_791_000_000


def _entry(**kw) -> AccountEntry:
    base = {"uid": "u1", "realm": "cn"}
    base.update(kw)
    return AccountEntry(**base)


# ---- 基本态 ----


def test_online():
    assert _entry().run_state(NOW) == RUN_ONLINE


def test_generic_cooling():
    e = _entry(cooldown_until=NOW + 60)
    assert e.run_state(NOW) == RUN_COOLING


def test_breaker_and_degrade_map_to_cooling():
    """熔断与降权都是可自动恢复的一般冷却。"""
    assert _entry(breaker_until=NOW + 60).run_state(NOW) == RUN_COOLING
    assert _entry(degraded_until=NOW + 60).run_state(NOW) == RUN_COOLING


def test_cooling_expired_is_online():
    assert _entry(cooldown_until=NOW - 1).run_state(NOW) == RUN_ONLINE


# ---- 关键区分：限流 vs 风控 ----


def test_rate_limited_vs_risk_cooling():
    """核心价值：同样在冷却，处置方向相反。

    - 限流（429）：等一会儿就好；
    - 风控（11140 / WAF）：等下去不会好，必须先看出口与请求形态。
    """
    rate = _entry(cooldown_until=NOW + 60, last_error="rate_limit:429 too many")
    risk = _entry(cooldown_until=NOW + 60, last_error="risk:11140 request illegal")
    assert rate.run_state(NOW) == RUN_RATE_LIMITED
    assert risk.run_state(NOW) == RUN_RISK_COOLING


def test_waf_reason_is_risk():
    e = _entry(cooldown_until=NOW + 60, last_error="waf:<html>403</html>")
    assert e.run_state(NOW) == RUN_RISK_COOLING


def test_6004_is_rate_limit_not_risk():
    e = _entry(cooldown_until=NOW + 60, last_error="model_limit:6004")
    assert e.run_state(NOW) == RUN_RATE_LIMITED


def test_model_cooldown_alone_keeps_online():
    """模型级冷却**不算账号级不可用**。

    run_state 回答的是「这个号整体能不能用」；「某个模型被限」是独立维度，
    由模型冷却明细表达。判成冷却会让人误以为整号不可用。
    """
    e = _entry()
    e.model_cooldowns["hy3"] = NOW + 3600
    assert e.run_state(NOW) == RUN_ONLINE


# ---- 等不来的两类 ----


def test_exhausted_from_reason():
    for reason in ("quota_exhausted", "余额不足", "积分不足", "402 payment"):
        e = _entry(enabled=False, disabled_reason=reason)
        assert e.run_state(NOW) == RUN_EXHAUSTED, reason


def test_relogin_from_reason():
    for reason in ("auth_failed", "14017", "12153", "session expired"):
        e = _entry(enabled=False, disabled_reason=reason)
        assert e.run_state(NOW) == RUN_RELOGIN, reason


def test_disabled_generic():
    e = _entry(enabled=False, disabled_reason="something else")
    assert e.run_state(NOW) == RUN_DISABLED


# ---- 顺序纪律 ----


def test_manual_disabled_beats_everything():
    """运维手动摘除排第一：那是**人的决定**，任何系统状态都不该把它盖住。

    否则运维会看到「冷却中」而去等它自动恢复，实际它永远不会回来。
    """
    e = _entry(manual_disabled=True, enabled=False, disabled_reason="quota_exhausted",
               cooldown_until=NOW + 9999, breaker_until=NOW + 9999)
    assert e.run_state(NOW) == RUN_MANUAL_DISABLED


def test_egress_error_beats_cooling():
    """出口配置坏掉的账号**看起来完全健康**（凭证有效、积分充足），
    但它一被选中就会从本机 IP 出去 —— 最该被立刻看见的一类。"""
    e = _entry(cooldown_until=NOW + 60, egress_error="proxy unreachable")
    assert e.run_state(NOW) == RUN_EGRESS_ERROR


def test_egress_error_ignored_when_manual_disabled():
    e = _entry(manual_disabled=True, egress_error="boom")
    assert e.run_state(NOW) == RUN_MANUAL_DISABLED


def test_disabled_beats_egress_error():
    """系统禁用比出口异常更根本（前者根本不会参与选号）。"""
    e = _entry(enabled=False, disabled_reason="x", egress_error="boom")
    assert e.run_state(NOW) == RUN_DISABLED


# ---- 枚举稳定性 ----


def test_state_values_are_stable():
    """值稳定 —— 前端按字符串匹配，改动会静默破坏界面。"""
    assert RUN_STATES == (
        "online", "cooling", "rate_limited", "risk_cooling", "exhausted",
        "relogin", "egress_error", "disabled", "manual_disabled",
    )
    assert len(RUN_STATES) == 9
    assert len(set(RUN_STATES)) == 9


def test_every_entry_maps_to_a_known_state():
    """任何状态位组合都必须落在九态之一（不返回空串）。"""
    combos = [
        {},
        {"enabled": False},
        {"manual_disabled": True},
        {"cooldown_until": NOW + 1},
        {"breaker_until": NOW + 1},
        {"degraded_until": NOW + 1},
        {"egress_error": "x"},
        {"enabled": False, "cooldown_until": NOW + 1, "egress_error": "x"},
    ]
    for kw in combos:
        assert _entry(**kw).run_state(NOW) in RUN_STATES, kw


# ---- 汇总接线 ----


def test_summary_includes_state_distribution(tmp_path):
    from conftest import make_test_config

    from keyport.app import build_context
    from keyport.upstream.base import AccountAuth

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    for uid in ("ok", "cool", "risk", "manual"):
        ctx.pool.upsert_account(AccountAuth(uid=uid, realm="cn", access_token="T"))
    ctx.pool.get("cool").cooldown_until = int(__import__("time").time()) + 600
    ctx.pool.get("cool").last_error = "rate_limit:429"
    ctx.pool.get("risk").cooldown_until = int(__import__("time").time()) + 600
    ctx.pool.get("risk").last_error = "risk:11140"
    ctx.pool.set_manual_disabled("manual", True, "运维摘除")

    by_state = ctx.pool.summary()["by_state"]
    assert by_state.get("online") == 1
    assert by_state.get("rate_limited") == 1
    assert by_state.get("risk_cooling") == 1
    assert by_state.get("manual_disabled") == 1


def test_account_view_exposes_run_state(tmp_path, caplog):
    """端点的账号视图必须真的透出 run_state 字段。"""
    import re

    from conftest import make_test_config
    from fastapi.testclient import TestClient

    from keyport.app import create_app
    from keyport.upstream.base import AccountAuth

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T"))

    with TestClient(app) as tc:
        logs = "\n".join(r.getMessage() for r in caplog.records)
        match = re.search(r"面板初始密码[^：]*：(\S+)", logs)
        assert match, "未取到初始密码"
        login = tc.post("/api/login",
                        json={"username": "admin", "password": match.group(1)})
        token = login.cookies.get("kp_session")
        assert token, login.text

        resp = tc.get("/api/accounts", headers={"x-kp-session": token})
        assert resp.status_code == 200, resp.text
        accounts = resp.json()["accounts"]
        assert accounts[0]["uid"] == "u1"
        assert accounts[0]["run_state"] == RUN_ONLINE
        assert "by_state" in resp.json()["summary"]


@pytest.mark.parametrize("reason,want", [
    ("rate_limit:429", RUN_RATE_LIMITED),
    ("risk:11140 request illegal", RUN_RISK_COOLING),
    ("auth_failed", RUN_RELOGIN),
    ("quota_exhausted", RUN_EXHAUSTED),
    ("something unknown", RUN_COOLING),
])
def test_reason_classification_table(reason, want):
    e = _entry(cooldown_until=NOW + 60, last_error=reason)
    assert e.run_state(NOW) == want
