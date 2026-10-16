"""`base_url` 白名单：写入闸 + 运行时闸。

背景：2026-10-16 的入侵通过 `POST /api/accounts/apikey` 植入
`base_url=http://<攻击者IP>:9999/adopt4` 的账号 —— 账号的 base_url 决定网关把它的
请求连同**真实 access token** 发往哪里，等于一条凭证外带通道。

原有的 `manual_disabled` **挡不住它**：设计上「手动摘除」只摘出对话选号，
**签到/保活等定时任务照常执行**，而 `keepalive` 会优先用 `auth.base_url`。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_test_config

from keyport.app import build_context
from keyport.security import base_url_allowed, base_url_violation
from keyport.upstream.base import AccountAuth

ALLOW = ["api.z.ai", "*.z.ai", "zcode.z.ai"]
BAD = "http://23.27.169.22:9999/adopt4"


# ---- 匹配语义 ----


def test_empty_url_is_allowed():
    """空 base_url = 走域默认端点，永远放行。"""
    for allow in (None, [], ALLOW):
        assert base_url_violation("", allow) == ""


def test_no_allowlist_allows_everything():
    """未声明白名单 → 放行（无从判断「异常」），由调用方留告警。"""
    assert base_url_violation(BAD, None) == ""
    assert base_url_violation(BAD, []) == ""
    assert base_url_allowed(BAD, []) is True


@pytest.mark.parametrize("url", [
    "https://api.z.ai/api/coding/paas/v4",
    "https://zcode.z.ai/api/v1/zcode-plan/anthropic",
    "https://code.z.ai/x",          # *.z.ai 通配
    "https://API.Z.AI/x",           # 大小写不敏感
    "https://api.z.ai:443/x",       # 带端口
])
def test_allowlisted_hosts_pass(url):
    assert base_url_violation(url, ALLOW) == ""


@pytest.mark.parametrize("url", [
    "http://23.27.169.22:9999/adopt4",
    "https://evil.example.com/x",
    "https://z.ai.evil.com/x",      # 后缀伪造不该命中 *.z.ai
    "https://notapi.z.ai.evil.com/x",
])
def test_non_allowlisted_hosts_blocked(url):
    assert base_url_violation(url, ALLOW) != ""


@pytest.mark.parametrize("url", [
    "file:///etc/passwd",
    "gopher://127.0.0.1:6379/_x",
    "ftp://example.com/x",
    "data:text/plain,x",
    "javascript:alert(1)",
])
def test_bad_schemes_always_rejected_even_without_allowlist(url):
    """非 http(s) 永远拒绝 —— base_url 只该是上游 API 端点。

    这条**不依赖白名单**，是默认就生效的硬闸。
    """
    for allow in (None, [], ALLOW):
        assert base_url_violation(url, allow) != "", (url, allow)


def test_missing_host_rejected():
    assert base_url_violation("http:///nohost", ALLOW) != ""


def test_rule_with_scheme_and_path_is_tolerated():
    """白名单条目写成完整 URL 也应能匹配（运维常见写法）。"""
    assert base_url_violation("https://api.z.ai/v1", ["https://api.z.ai/x"]) == ""


# ---- 运行时闸 1：选号 ----


def _ctx(tmp_path: Path, **security):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    for k, v in security.items():
        setattr(cfg.security, k, v)
    return build_context(cfg)


def test_guard_blocks_non_allowlisted_base_url(tmp_path: Path):
    """带非白名单 base_url 的账号**不进候选** —— 否则一次选号就把令牌送出去。"""
    ctx = _ctx(tmp_path, base_url_allow=ALLOW)
    ctx.pool.upsert_account(AccountAuth(
        uid="good", realm="zai", access_token="T",
        base_url="https://api.z.ai/api/coding/paas/v4"))
    ctx.pool.upsert_account(AccountAuth(
        uid="evil", realm="zai", access_token="T", base_url=BAD))

    assert ctx.pool.guard_reason(ctx.pool.get("good")) == ""
    reason = ctx.pool.guard_reason(ctx.pool.get("evil"))
    assert "base_url 不受信" in reason
    # 选号走的就是这个 guard（pick 把它当 guard 回调传入），所以被挡的号不会进候选
    blocked = [e.uid for e in ctx.pool.all_entries() if ctx.pool.guard_reason(e)]
    assert blocked == ["evil"]


def test_no_allowlist_does_not_block(tmp_path: Path):
    """白名单为空时保持旧行为（否则会让既有 z.ai 账号全部失效）。"""
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(
        uid="x", realm="zai", access_token="T", base_url=BAD))
    assert ctx.pool.guard_reason(ctx.pool.get("x")) == ""
    assert ctx.pool.guard_reason(ctx.pool.all_entries()[0]) == ""


# ---- 运行时闸 2：定时任务（关键 —— 禁用号也会被任务打到）----


def test_scheduler_gate_blocks_tasks_for_untrusted_base_url(tmp_path: Path):
    """**这是本次修复的关键点**：`manual_disabled` 挡不住任务。

    「手动摘除」的语义是只摘出对话选号，签到/保活照常执行 —— 而保活会带上
    `auth.base_url`。所以必须让任务层也跳过。
    """
    ctx = _ctx(tmp_path, base_url_allow=ALLOW)
    ctx.pool.upsert_account(AccountAuth(
        uid="evil", realm="zai", access_token="T", base_url=BAD))
    entry = ctx.pool.get("evil")

    reason = ctx.scheduler._gate_reason("keepalive", entry)
    assert "base_url 不受信" in reason

    # 对照：即便手动摘除，任务闸依然拦住（说明它不是靠禁用生效的）
    ctx.pool.set_manual_disabled("evil", True, "test")
    assert "base_url 不受信" in ctx.scheduler._gate_reason("keepalive", entry)


def test_scheduler_gate_does_not_block_allowlisted(tmp_path: Path):
    ctx = _ctx(tmp_path, base_url_allow=ALLOW)
    ctx.pool.upsert_account(AccountAuth(
        uid="ok", realm="zai", access_token="T",
        base_url="https://api.z.ai/api/coding/paas/v4"))
    assert ctx.scheduler._gate_reason("keepalive", ctx.pool.get("ok")) == ""


# ---- 写入闸：端点 ----


def _admin_app(tmp_path: Path, **security):
    from keyport.app import create_app
    from keyport.security import hash_password

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    cfg.security.admin_password_hash = hash_password("Correct#Horse9")
    for k, v in security.items():
        setattr(cfg.security, k, v)
    return create_app(cfg)


def test_apikey_endpoint_rejects_untrusted_base_url(tmp_path: Path):
    """注入点直接堵死：非白名单 host 一律 400，并留审计。"""
    from fastapi.testclient import TestClient

    app = _admin_app(tmp_path, base_url_allow=ALLOW)
    with TestClient(app, base_url="https://testserver",
                    client=("127.0.0.1", 50000)) as tc:
        tc.post("/api/login", json={"username": "admin", "password": "Correct#Horse9"})
        resp = tc.post("/api/accounts/apikey",
                       json={"realm": "zai", "api_key": "k", "base_url": BAD})
        assert resp.status_code == 400
        assert "白名单" in resp.json()["detail"]

        # 白名单内的正常写入仍然可用
        ok = tc.post("/api/accounts/apikey",
                     json={"realm": "zai", "api_key": "k",
                           "base_url": "https://api.z.ai/api/coding/paas/v4"})
        assert ok.status_code == 200, ok.text

        # 审计：拒绝的那条要留证（DB 必须在 lifespan 未退出时读）
        rows = app.state.ctx.db.query(
            "SELECT result, detail FROM audit_logs WHERE action='account_apikey_add' "
            "ORDER BY ts")
    assert any(r["result"] == "denied" for r in rows)
    assert any("base_url=" in (r["detail"] or "") for r in rows)


def test_apikey_endpoint_allows_when_no_allowlist(tmp_path: Path):
    """未声明白名单时保持旧行为，但审计里必须留下 base_url。"""
    from fastapi.testclient import TestClient

    app = _admin_app(tmp_path)
    with TestClient(app, base_url="https://testserver",
                    client=("127.0.0.1", 50000)) as tc:
        tc.post("/api/login", json={"username": "admin", "password": "Correct#Horse9"})
        resp = tc.post("/api/accounts/apikey",
                       json={"realm": "zai", "api_key": "k", "base_url": BAD})
        assert resp.status_code == 200, resp.text
        rows = app.state.ctx.db.query(
            "SELECT detail FROM audit_logs WHERE action='account_apikey_add'")
    assert any("23.27.169.22" in (r["detail"] or "") for r in rows)


# ---- 持久化 ----


def test_base_url_persisted_and_reloaded(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(
        uid="z1", realm="zai", access_token="T", base_url="https://api.z.ai/x"))
    assert ctx.pool.get("z1").base_url == "https://api.z.ai/x"
    assert ctx.pool.get("z1").to_state()["base_url"] == "https://api.z.ai/x"
    row = ctx.db.query_one("SELECT base_url FROM accounts WHERE uid=?", ("z1",))
    assert row["base_url"] == "https://api.z.ai/x"


def test_default_allowlist_is_empty():
    """默认空 = 保持旧行为，不因升级而让既有 z.ai 账号失效。"""
    from keyport.config import SecurityConfig

    assert SecurityConfig().base_url_allow == []
