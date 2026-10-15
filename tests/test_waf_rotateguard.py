"""WAF IP 级 fail-fast 与换号轮转退避。

移植自 sheng-du0521/workbuddy2api 的 `internal/server/wafip.go` 与
`internal/rotateguard/rotateguard.go`。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.config import PoolConfig
from keyport.pool.entry import AccountEntry
from keyport.pool.transitions import apply_error
from keyport.security import hash_api_key
from keyport.upstream.base import AccountAuth, ErrorKind, classify_status, is_waf_blocked
from keyport.upstream.rotateguard import (
    BACKOFF_CAP,
    WAF_IP_THRESHOLD,
    WAF_IP_WINDOW,
    WafIpGate,
    backoff_after,
    jitter_duration,
    sleep_ctx,
)

# ---- WAF 形态判定 ----


@pytest.mark.parametrize("body", [
    "",                                   # 空体
    "<html><body>403 Forbidden</body></html>",
    "Forbidden",
    '{"message":"blocked by waf"}',       # 无 code/msg 信封
    "<head><script>...</script></head><body>blocked</body>",
])
def test_is_waf_blocked_true(body):
    assert is_waf_blocked(403, body) is True


@pytest.mark.parametrize("body", [
    '{"code":11140,"msg":"request illegal"}',
    '{"code":11128,"msg":"x"}',
    '{"msg":"账号存在异常行为"}',
])
def test_business_envelope_403_is_not_waf(body):
    """带业务信封的 403 走既有分类链 —— 宁漏判 WAF 也不误罚业务 403。"""
    assert is_waf_blocked(403, body) is False


def test_is_waf_blocked_only_for_403():
    assert is_waf_blocked(401, "<html>") is False
    assert is_waf_blocked(500, "") is False


def test_classify_403_waf_vs_auth():
    """WAF 拦的是出口 IP、AUTH 是账号失效 —— 判错会把健康号一个个禁用掉。"""
    assert classify_status(403, "<html>Forbidden</html>") == ErrorKind.WAF
    assert classify_status(403, '{"code":11140,"msg":"request illegal"}') == ErrorKind.AUTH
    # 额度耗尽的变体仍优先判 PAYMENT
    assert classify_status(403, '{"code":14051,"msg":"quota"}') == ErrorKind.PAYMENT


def test_waf_does_not_disable_account():
    """WAF 403 不得禁用账号（它指向出口 IP 而非账号）。"""
    cfg = PoolConfig()
    entry = AccountEntry(uid="u1", realm="cn")
    err = type("E", (), {"kind": ErrorKind.WAF, "message": "", "status": 403,
                         "body": "", "headers": {}, "retry_after": 0.0})()
    detail = apply_error(entry, err, cfg, 1_791_000_000)
    assert entry.enabled is True
    assert entry.disabled_reason == ""
    assert detail.startswith("waf_cooldown:")
    assert entry.cooldown_until > 1_791_000_000


def test_auth_403_still_disables():
    cfg = PoolConfig()
    entry = AccountEntry(uid="u1", realm="cn")
    err = type("E", (), {"kind": ErrorKind.AUTH, "message": "", "status": 403,
                         "body": "", "headers": {}, "retry_after": 0.0})()
    apply_error(entry, err, cfg, 1_791_000_000)
    assert entry.enabled is False


# ---- IP 级状态机 ----


def test_gate_single_account_never_triggers():
    """单号反复 403（账号级偶发）永不触发 —— 只数不同号。"""
    gate = WafIpGate()
    for _ in range(10):
        assert gate.note_waf("u1", now=1000.0) is False
    assert gate.active(now=1000.0) is False


def test_gate_triggers_on_two_distinct_accounts():
    """「多号」的最小定义是 2：两个不同号在窗内接连被拦已是 IP 级证据。"""
    gate = WafIpGate()
    assert gate.note_waf("u1", now=1000.0) is False
    assert gate.note_waf("u2", now=1000.5) is True
    assert gate.active(now=1000.5) is True
    assert WAF_IP_THRESHOLD == 2


def test_gate_window_expiry():
    """窗外命中被剪掉，不参与判定。"""
    gate = WafIpGate()
    assert gate.note_waf("u1", now=1000.0) is False
    # 第二个号在窗口之外 → 前一个已被剪掉，只有 1 个不同号
    assert gate.note_waf("u2", now=1000.0 + WAF_IP_WINDOW + 1) is False


def test_gate_active_period_not_renewed():
    """激活期内新命中不续期（保守自然解除，不做主动探测）。"""
    gate = WafIpGate()
    gate.note_waf("u1", now=1000.0)
    gate.note_waf("u2", now=1000.0)
    assert gate.active(now=1000.0) is True
    until = gate.until
    # 激活期内继续命中
    assert gate.note_waf("u3", now=1030.0) is True
    assert gate.until == until          # 不续期
    # 到期后自然解除
    assert gate.active(now=until + 1) is False


def test_gate_clears_window_after_activation():
    """激活后清空判定窗：解除后需全新命中重新判定，不叠旧账。"""
    gate = WafIpGate()
    gate.note_waf("u1", now=1000.0)
    gate.note_waf("u2", now=1000.0)
    assert gate.hits == {}
    # 解除后单号命中不应立刻再次激活
    assert gate.note_waf("u3", now=1000.0 + WAF_IP_WINDOW + 1) is False


def test_gate_same_account_repeat_does_not_accumulate():
    """同号重复命中覆盖不累计（判定口径是「不同号数」）。"""
    gate = WafIpGate()
    for _ in range(5):
        gate.note_waf("u1", now=1000.0)
    assert len(gate.hits) == 1


# ---- 退避 ----


def test_backoff_grows_then_caps():
    # 抖动 ±25%，用区间断言
    assert 0.375 <= backoff_after(0) <= 0.625
    assert 0.75 <= backoff_after(1) <= 1.25
    for n in range(5, 12):
        assert backoff_after(n) <= BACKOFF_CAP * 1.25 + 1e-9


def test_backoff_zero_base_disables():
    """base 置 0 = 关闭退避（测试用）。"""
    for n in range(5):
        assert backoff_after(n, base=0.0) == 0.0


def test_jitter_zero_returns_zero():
    assert jitter_duration(0) == 0
    assert jitter_duration(-1) == -1


def test_jitter_stays_within_bounds():
    for _ in range(50):
        got = jitter_duration(1.0)
        assert 0.75 <= got <= 1.25


@pytest.mark.asyncio
async def test_sleep_ctx_cancellable():
    """取消即返回 False（优雅停机不必等退避睡醒）。"""
    stop = asyncio.Event()

    async def _set():
        await asyncio.sleep(0.02)
        stop.set()

    asyncio.create_task(_set())
    assert await sleep_ctx(5.0, stop) is False


@pytest.mark.asyncio
async def test_sleep_ctx_completes():
    assert await sleep_ctx(0.01) is True
    assert await sleep_ctx(0) is True


# ---- 接线：轮转循环里的 fail-fast ----


WAF_HTML = b"<html><body><h1>403 Forbidden</h1></body></html>"
OK_SSE = (
    'data: {"id":"c1","model":"hy3","choices":'
    '[{"index":0,"delta":{"content":"ok"}}]}\n\n'
    'data: {"id":"c1","model":"hy3","choices":'
    '[{"index":0,"delta":{},"finish_reason":"stop"}],'
    '"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\n'
    "data: [DONE]\n\n"
)


def _build(tmp_path: Path, accounts: int):
    cfg = make_test_config(tmp_path)
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    for i in range(accounts):
        ctx.pool.upsert_account(AccountAuth(
            uid=f"u{i}", realm="cn", access_token=f"T{i}"))
    key = "sk-kp-waf"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("w", hash_api_key(key), key[:12]),
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if not request.url.path.endswith("/chat/completions"):
            return httpx.Response(200, json={"code": 0, "data": {}})
        calls.append(str(request.url.host))
        return httpx.Response(403, content=WAF_HTML,
                              headers={"content-type": "text/html"})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    return app, ctx, calls, {"Authorization": f"Bearer {key}"}


def test_waf_ip_block_stops_rotation(tmp_path: Path):
    """出口 IP 被 WAF 拦时立即止损，不把一次请求放大成 N 次上游请求。"""
    app, ctx, calls, auth = _build(tmp_path, accounts=6)
    with TestClient(app) as tc:
        resp = tc.post("/v1/chat/completions",
                       json={"model": "hy3", "messages": [{"role": "user", "content": "x"}]},
                       headers=auth)
        assert resp.status_code == 403, resp.text
    # 阈值是 2 个不同号：不应把 6 个号全打一遍
    assert len(calls) <= WAF_IP_THRESHOLD + 1, calls
    assert ctx.gateway.waf_gate.active() is True


def test_waf_accounts_not_disabled(tmp_path: Path):
    """WAF 拦的是 IP：账号必须保持健康（禁用它们等于自毁号池）。"""
    app, ctx, _calls, auth = _build(tmp_path, accounts=3)
    with TestClient(app) as tc:
        tc.post("/v1/chat/completions",
                json={"model": "hy3", "messages": [{"role": "user", "content": "x"}]},
                headers=auth)
    for entry in ctx.pool.all_entries():
        assert entry.enabled is True
        assert entry.disabled_reason == ""
