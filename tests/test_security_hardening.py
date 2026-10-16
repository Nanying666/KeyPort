"""安全加固回归：对应 2026-10-16 入侵排查发现的问题。

排查结论（服务器实测证据）：
- IP 152.70.234.167 在 63 秒内连打登录，6 次失败后于 12:58:34 拿到 admin 会话
- 随后 `account_export` 导出 count=11（全部账号明文凭证）、建了 `pentest-key`、
  改动 57 次设置、调用 106 次模型

据此修掉的问题：登录无限爆破、XFF 可伪造、管理台来源不受限、Cookie 缺 Secure、
密码强度形同虚设、改密不吊销会话。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_test_config

from keyport.login_guard import LoginGuard
from keyport.security import (
    client_ip_from_request,
    password_strength_error,
)

TRUSTED = ["127.0.0.1/32", "172.16.0.0/12"]


# ---- X-Forwarded-For 伪造 ----


def test_xff_ignored_when_peer_not_trusted_proxy():
    """**核心修复**：对端不是可信代理时，XFF 一律不采信。

    原先取 XFF **首段**（`xff.split(",")[0]`）—— 那是请求方自己写的，
    等于让任何人声明自己的 IP：审计日志被污染、IP 白名单一行头就绕过。
    """
    got = client_ip_from_request(
        "203.0.113.50", {"x-forwarded-for": "1.2.3.4"},  # 公网直连
        trust_proxy=True, trusted_proxies=TRUSTED,
    )
    assert got == "203.0.113.50"  # 用 TCP 对端，不用伪造值


def test_xff_rightmost_untrusted_is_the_real_client():
    """对端是可信代理时，从**右往左**取第一个非代理地址。

    可信代理把自己看到的对端**追加**到 XFF 尾部，所以最右侧才可信；
    左侧全是请求方自己写的（可以塞任意多个假地址）。
    """
    got = client_ip_from_request(
        "127.0.0.1",  # 可信代理
        {"x-forwarded-for": "9.9.9.9, 8.8.8.8, 203.0.113.7"},
        trust_proxy=True, trusted_proxies=TRUSTED,
    )
    assert got == "203.0.113.7"  # 不是最左的 9.9.9.9


def test_xff_all_hops_trusted_falls_back_to_peer():
    """整条链都是可信代理 → 没有真实客户端可提取，回落 TCP 对端。"""
    got = client_ip_from_request(
        "127.0.0.1", {"x-forwarded-for": "172.18.0.5, 172.18.0.9"},
        trust_proxy=True, trusted_proxies=TRUSTED,
    )
    assert got == "127.0.0.1"


def test_xff_ignored_when_no_trusted_proxies_declared():
    """未声明可信代理 → 完全不采信 XFF（安全默认）。"""
    got = client_ip_from_request(
        "127.0.0.1", {"x-forwarded-for": "1.2.3.4"},
        trust_proxy=True, trusted_proxies=[],
    )
    assert got == "127.0.0.1"


def test_xff_ignored_when_trust_proxy_disabled():
    got = client_ip_from_request(
        "10.0.0.1", {"x-forwarded-for": "1.2.3.4"},
        trust_proxy=False, trusted_proxies=TRUSTED,
    )
    assert got == "10.0.0.1"


def test_real_ip_header_also_gated():
    """`X-Real-IP` 同样只在可信代理下采信（它是同一个信任问题的另一张皮）。"""
    got = client_ip_from_request(
        "203.0.113.50", {"x-real-ip": "1.2.3.4"},
        trust_proxy=True, trusted_proxies=TRUSTED,
    )
    assert got == "203.0.113.50"


# ---- 登录节流 ----


def test_login_locks_after_threshold():
    """失败达阈值即锁定 —— 原先可无限猜密码。"""
    g = LoginGuard(max_failures=3, lockout_seconds=60)
    assert g.allow("1.1.1.1", now=1000) is True
    for i in range(2):
        assert g.record_failure("1.1.1.1", now=1000 + i) == 0
    wait = g.record_failure("1.1.1.1", now=1002)
    assert wait == 60
    assert g.allow("1.1.1.1", now=1003) is False
    assert g.retry_after("1.1.1.1", now=1003) == pytest.approx(59)


def test_lockout_expires():
    g = LoginGuard(max_failures=2, lockout_seconds=60)
    g.record_failure("1.1.1.1", now=1000)
    g.record_failure("1.1.1.1", now=1001)
    assert g.allow("1.1.1.1", now=1030) is False
    assert g.allow("1.1.1.1", now=1065) is True


def test_lockout_doubles_on_repeat():
    """再犯翻倍 —— 反复爆破的代价要递增。"""
    g = LoginGuard(max_failures=1, lockout_seconds=60, max_lockout_seconds=600)
    assert g.record_failure("1.1.1.1", now=1000) == 60
    assert g.record_failure("1.1.1.1", now=2000) == 120
    assert g.record_failure("1.1.1.1", now=3000) == 240
    # 封顶
    for t in (4000, 5000, 6000):
        g.record_failure("1.1.1.1", now=t)
    assert g.record_failure("1.1.1.1", now=7000) == 600


def test_global_lockout_blocks_ip_rotation():
    """全局闸：换 IP 也绕不过 —— 总失败数封顶。"""
    g = LoginGuard(max_failures=100, global_max_failures=4, lockout_seconds=60)
    for i in range(3):
        assert g.record_failure(f"10.0.0.{i}", now=1000 + i) == 0
    wait = g.record_failure("10.0.0.9", now=1003)
    assert wait == 60
    # 一个全新的 IP 同样被挡（负载均衡/代理池换源没用）
    assert g.allow("8.8.8.8", now=1004) is False


def test_success_clears_failures():
    g = LoginGuard(max_failures=3, lockout_seconds=60)
    g.record_failure("1.1.1.1", now=1000)
    g.record_failure("1.1.1.1", now=1001)
    g.record_success("1.1.1.1")
    # 计数已清零：再失败两次不该锁定
    assert g.record_failure("1.1.1.1", now=1002) == 0
    assert g.record_failure("1.1.1.1", now=1003) == 0


def test_failures_outside_window_do_not_accumulate():
    g = LoginGuard(max_failures=3, window_seconds=10, lockout_seconds=60)
    for t in (1000, 1100, 1200):  # 间隔远超窗口
        assert g.record_failure("1.1.1.1", now=t) == 0
    assert g.allow("1.1.1.1", now=1201) is True


def test_snapshot_reports_locked_ips():
    g = LoginGuard(max_failures=1, lockout_seconds=60)
    g.record_failure("1.1.1.1", now=1000)
    snap = g.snapshot(now=1000)
    assert "1.1.1.1" in snap["locked_ips"]


# ---- 密码强度 ----


@pytest.mark.parametrize("pw", [
    "short",              # 太短
    "12345678901",        # 纯数字
    "aaaaaaaaaaaa",       # 单一字符类
    "admin",              # 常见弱口令
    "password1234",       # 常见弱口令
])
def test_weak_passwords_rejected(pw):
    assert password_strength_error(pw) != ""


def test_strong_password_accepted():
    assert password_strength_error("Kx7#mQ2!pLw9") == ""


def test_password_cannot_equal_username():
    assert password_strength_error("AdminAdmin12", "adminadmin12") != ""


# ---- 端点级验证 ----
#
# 注意两点测试约束：
#   1. 一个 app 只开一次 TestClient —— 退出 lifespan 会关掉 DB，再开就报
#      「Cannot operate on a closed database」；
#   2. base_url 用 https —— `Secure` Cookie 在 HTTP 下不会被客户端回传
#      （这正是加它的意义），用 http 测会得到「登录成功但后续 401」。

GOOD_PW = "Correct#Horse9"


def _app(tmp_path: Path, **security):
    from keyport.app import create_app
    from keyport.security import hash_password

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    cfg.security.admin_password_hash = hash_password(GOOD_PW)
    for k, v in security.items():
        setattr(cfg.security, k, v)
    return create_app(cfg)


def _tc(app, ip: str = "203.0.113.9"):
    from fastapi.testclient import TestClient

    return TestClient(app, base_url="https://testserver", client=(ip, 50000))


def test_login_endpoint_locks_out(tmp_path: Path):
    """连打失败后返回 429（而不是继续让他猜）。"""
    app = _app(tmp_path)
    with _tc(app) as tc:
        for _ in range(5):
            assert tc.post("/api/login",
                           json={"username": "admin", "password": "wrong"}
                           ).status_code == 401
        resp = tc.post("/api/login", json={"username": "admin", "password": "wrong"})
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        # 锁定期间即便密码正确也拒绝（不泄露「猜对了」）
        assert tc.post("/api/login",
                       json={"username": "admin", "password": GOOD_PW}).status_code == 429


def test_login_endpoint_accepts_correct_password(tmp_path: Path):
    app = _app(tmp_path)
    with _tc(app) as tc:
        resp = tc.post("/api/login", json={"username": "admin", "password": GOOD_PW})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # 会话可用（HTTPS + Secure Cookie）
        assert tc.get("/api/accounts").status_code == 200


def test_login_cookie_is_secure(tmp_path: Path):
    """HTTPS 连接必须带 Secure，否则同域明文请求会把会话带出去。"""
    app = _app(tmp_path)
    with _tc(app) as tc:
        resp = tc.post("/api/login", json={"username": "admin", "password": GOOD_PW})
    cookie = resp.headers["set-cookie"].lower()
    assert "secure" in cookie
    assert "httponly" in cookie


def test_login_cookie_not_secure_on_plain_http(tmp_path: Path):
    """纯 HTTP 连接**不加** Secure —— 否则浏览器干脆不回传，登录形同失效。"""
    from fastapi.testclient import TestClient

    app = _app(tmp_path)
    with TestClient(app, base_url="http://testserver",
                    client=("127.0.0.1", 50000)) as tc:
        resp = tc.post("/api/login", json={"username": "admin", "password": GOOD_PW})
        assert resp.status_code == 200
        assert "secure" not in resp.headers["set-cookie"].lower()
        # 会话仍然可用（这正是「不加」的目的）
        assert tc.get("/api/accounts").status_code == 200


def test_admin_ip_allowlist_blocks_other_sources(tmp_path: Path):
    """`admin_ip_allow` 非空时，非白名单来源连登录都不给。"""
    app = _app(tmp_path, admin_ip_allow=["10.0.0.0/8"])
    with _tc(app, ip="203.0.113.9") as tc:
        assert tc.post("/api/login",
                       json={"username": "admin", "password": GOOD_PW}
                       ).status_code == 403
        # 未登录也拦：来源闸在任何鉴权之前
        assert tc.get("/api/accounts").status_code == 403


def test_admin_ip_allowlist_permits_listed_source(tmp_path: Path):
    app = _app(tmp_path, admin_ip_allow=["10.0.0.0/8"])
    with _tc(app, ip="10.1.2.3") as tc:
        assert tc.post("/api/login",
                       json={"username": "admin", "password": GOOD_PW}
                       ).status_code == 200
        assert tc.get("/api/accounts").status_code == 200


def test_admin_ip_allowed_helper():
    from keyport.api.admin_auth import admin_ip_allowed
    from keyport.config import Config

    cfg = Config()
    assert admin_ip_allowed("1.2.3.4", cfg) is True       # 空 = 不限制
    cfg.security.admin_ip_allow = ["10.0.0.0/8"]
    assert admin_ip_allowed("10.1.2.3", cfg) is True
    assert admin_ip_allowed("203.0.113.9", cfg) is False


def test_change_password_rejects_weak(tmp_path: Path):
    app = _app(tmp_path)
    with _tc(app, ip="127.0.0.1") as tc:
        tc.post("/api/login", json={"username": "admin", "password": GOOD_PW})
        resp = tc.post("/api/password",
                       json={"old_password": GOOD_PW, "new_password": "abc123"})
        assert resp.status_code == 400
        assert "12" in resp.json()["detail"]


def test_change_password_revokes_all_sessions(tmp_path: Path):
    """改密即吊销全部会话 —— 口令泄露后改密是标准处置，留着旧会话等于没改。"""
    app = _app(tmp_path)
    with _tc(app, ip="127.0.0.1") as tc:
        tc.post("/api/login", json={"username": "admin", "password": GOOD_PW})
        # 另造一个「他人」会话（模拟被泄露的那一个）
        other = tc.post("/api/login", json={"username": "admin", "password": GOOD_PW})
        other_token = other.cookies.get("kp_session")
        assert other_token

        resp = tc.post("/api/password",
                       json={"old_password": GOOD_PW,
                             "new_password": "Fresh#Secret42"})
        assert resp.status_code == 200
        assert resp.json()["revoked_sessions"] >= 2

        # 旧令牌全部失效
        assert tc.get("/api/accounts",
                      headers={"x-kp-session": other_token}).status_code == 401
        # 新密码可登录
        assert tc.post("/api/login",
                       json={"username": "admin",
                             "password": "Fresh#Secret42"}).status_code == 200


# ---- 默认值 ----


def test_security_defaults_are_safe():
    from keyport.config import SecurityConfig

    s = SecurityConfig()
    # 可信代理默认含回环与容器网段（生产形态：代理在本机/Docker 网络内）
    assert "127.0.0.1/32" in s.trusted_proxies
    assert s.login_max_failures > 0, "登录节流必须默认开启"
    assert s.cookie_secure is True
