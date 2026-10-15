"""选号可观测：候选队列预览（只读）与均衡度指标。

移植自 sheng-du0521/workbuddy2api 的 `internal/pool/preview.go`
（`PreviewCandidates` 的只读契约）与 `cmd/webapp/selection.go`（`selectionBalance`）。
"""

from __future__ import annotations

from pathlib import Path

from conftest import make_test_config

from keyport.app import build_context
from keyport.pool.selector import preview_candidates
from keyport.upstream.base import AccountAuth

NOW = 1_791_000_000


def _ctx(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    return build_context(cfg)


# ---- 候选队列预览 ----


def test_preview_reports_rank_and_weight(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    ctx.pool.upsert_account(AccountAuth(uid="b", realm="cn", access_token="B"))
    ctx.pool.get("a").credits = 1000
    ctx.pool.get("b").credits = 10

    rows = ctx.pool.preview_candidates()
    assert [r["uid"] for r in rows] == ["a", "b"]
    assert rows[0]["rank"] == 1 and rows[1]["rank"] == 2
    assert rows[0]["weight"] > rows[1]["weight"]
    assert rows[0]["in_top"] is True


def test_preview_is_read_only(tmp_path: Path):
    """只读契约：不动 last_pick、不改账号状态位。

    否则它回答的就变成"上次是怎么排的"，而不是"现在会怎么排"。
    """
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    before_last_pick = dict(ctx.pool._last_pick)
    before_entry = ctx.pool.get("a").to_state()

    ctx.pool.preview_candidates()

    assert ctx.pool._last_pick == before_last_pick
    assert ctx.pool.get("a").to_state() == before_entry


def test_preview_explains_skip_reasons(tmp_path: Path):
    """排障的核心问题：「为什么跳过它」。"""
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="ok", realm="cn", access_token="A"))
    ctx.pool.upsert_account(AccountAuth(uid="off", realm="cn", access_token="B"))
    ctx.pool.upsert_account(AccountAuth(uid="cool", realm="cn", access_token="C"))
    ctx.pool.upsert_account(AccountAuth(uid="other", realm="global", access_token="D"))

    ctx.pool.set_manual_disabled("off", True, "运维摘除")
    ctx.pool.get("cool").cooldown_until = NOW + 600

    rows = {r["uid"]: r for r in ctx.pool.preview_candidates(now=NOW, realm="cn")}
    assert rows["ok"]["skip"] == ""
    assert rows["off"]["skip"] == "manual_disabled"
    assert rows["off"]["skip_text"]
    assert rows["cool"]["skip"] == "cooling"
    assert rows["cool"]["cool_until"] == NOW + 600
    assert rows["other"]["skip"] == "realm"


def test_preview_reports_model_cooldown(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    ctx.pool.get("a").model_cooldowns["hy3"] = NOW + 900

    rows = ctx.pool.preview_candidates(model="hy3", now=NOW)
    assert rows[0]["skip"] == "model_cooling"
    assert rows[0]["model_cooldown_until"] == NOW + 900
    # 换一个模型即可用（6004 只挡那一个模型）
    rows = ctx.pool.preview_candidates(model="hy4", now=NOW)
    assert rows[0]["skip"] == ""


def test_preview_reports_tried_and_capacity(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    ctx.pool.upsert_account(AccountAuth(uid="b", realm="cn", access_token="B"))
    ctx.pool.get("b").inflight = ctx.cfg.pool.max_in_flight

    rows = {r["uid"]: r for r in ctx.pool.preview_candidates(tried={"a"}, now=NOW)}
    assert rows["a"]["skip"] == "tried"
    assert rows["b"]["skip"] == "in_flight_full"


def test_preview_reports_recent_pick_and_gap(tmp_path: Path):
    """防惊群窗口与 select() 同口径。"""
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    ctx.pool._last_pick["a"] = NOW - 0.01  # 刚被选中

    rows = ctx.pool.preview_candidates(now=NOW)
    assert rows[0]["recent_pick"] > 0
    assert rows[0]["in_gap"] is True


def test_preview_reports_guard_block(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    ctx.pool.get("a").credits = 0
    ctx.cfg.guards.reserve_credits = 10

    rows = ctx.pool.preview_candidates(model="paid-model", now=NOW)
    assert rows[0]["skip"] == "guard"


def test_preview_empty_pool():
    assert preview_candidates([], make_test_config(Path("./tmp-nonexistent")).pool) == []


def test_preview_sorted_stable(tmp_path: Path):
    """输出稳定可复现：进候选者按排名，落选者按原因再按 uid。"""
    ctx = _ctx(tmp_path)
    for uid in ("z", "a", "m"):
        ctx.pool.upsert_account(AccountAuth(uid=uid, realm="cn", access_token="X"))
    first = [r["uid"] for r in ctx.pool.preview_candidates(now=NOW)]
    second = [r["uid"] for r in ctx.pool.preview_candidates(now=NOW)]
    assert first == second


# ---- 均衡度 ----


def _log(ctx, uid: str, n: int) -> None:
    import time as _t

    for _ in range(n):
        ctx.db.execute(
            "INSERT INTO request_logs(ts, key_id, key_name, account_uid, realm, "
            "protocol, model, stream, status, ttfb_ms, total_ms, prompt_tokens, "
            "completion_tokens, total_tokens, credits, retries, client_ip, error) "
            "VALUES(?,0,'',?,'cn','openai','m',1,200,1,1,1,1,2,0.0,0,'','')",
            (int(_t.time()), uid),
        )


def test_balance_ok_when_even(tmp_path: Path):
    ctx = _ctx(tmp_path)
    for uid in ("a", "b", "c", "d"):
        ctx.pool.upsert_account(AccountAuth(uid=uid, realm="cn", access_token="X"))
        _log(ctx, uid, 25)
    got = ctx.pool.selection_balance(24)
    assert got["accounts_used"] == 4
    assert got["coverage_pct"] == 100.0
    assert got["max_share_pct"] == 25.0
    # 4 个号时完全均匀也是 25% —— 单看绝对阈值必然误报，故必须与相对偏斜取与
    assert got["verdict"] == "ok"


def test_balance_detects_skew(tmp_path: Path):
    ctx = _ctx(tmp_path)
    for uid in ("a", "b", "c", "d"):
        ctx.pool.upsert_account(AccountAuth(uid=uid, realm="cn", access_token="X"))
    _log(ctx, "a", 90)
    for uid in ("b", "c", "d"):
        _log(ctx, uid, 3)
    got = ctx.pool.selection_balance(24)
    assert got["verdict"] == "skewed"
    assert got["skew_ratio"] >= 2.0
    assert got["top"][0]["uid"] == "a"


def test_balance_low_coverage(tmp_path: Path):
    """ideal 分母是「本可以轮到的账号数」（含未用号），因此用号少本身就算偏斜。

    这正是参考实现的刻意口径：单号吃掉大部分流量与「只用了一半账号」在防封上
    是同一类问题（都意味着部分账号被闲置、少数账号承担全部风险）。
    """
    ctx = _ctx(tmp_path)
    for uid in ("a", "b", "c", "d"):
        ctx.pool.upsert_account(AccountAuth(uid=uid, realm="cn", access_token="X"))
    _log(ctx, "a", 10)
    got = ctx.pool.selection_balance(24)
    assert got["coverage_pct"] == 25.0
    assert got["verdict"] == "skewed"

    # 4 个号各 25 次 → 完全均匀，覆盖率 100%
    ctx2 = _ctx(tmp_path / "b")
    for uid in ("a", "b", "c", "d"):
        ctx2.pool.upsert_account(AccountAuth(uid=uid, realm="cn", access_token="X"))
        _log(ctx2, uid, 25)
    got2 = ctx2.pool.selection_balance(24)
    assert got2["coverage_pct"] == 100.0
    assert got2["verdict"] == "ok"


def test_balance_no_traffic(tmp_path: Path):
    ctx = _ctx(tmp_path)
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="X"))
    got = ctx.pool.selection_balance(24)
    assert got["calls"] == 0
    assert got["verdict"] in ("ok", "low-coverage")


def test_balance_hours_clamped(tmp_path: Path):
    """hours 非法值夹到合法区间（0/None → 默认 24，过大 → 上限）。"""
    ctx = _ctx(tmp_path)
    assert ctx.pool.selection_balance(0)["hours"] == 24
    assert ctx.pool.selection_balance(1)["hours"] == 1
    assert ctx.pool.selection_balance(10_000)["hours"] == 24 * 30


# ---- 端点 ----


def test_endpoints_registered(tmp_path: Path, caplog):
    from fastapi.testclient import TestClient

    from keyport.app import create_app
    from keyport.security import hash_api_key

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(uid="a", realm="cn", access_token="A"))
    raw = "sk-kp-sel"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("sel", hash_api_key(raw), raw[:12]),
    )
    with TestClient(app) as tc:
        # 首启会生成随机面板密码并打印到日志，从日志里取回
        import re
        logs = "\n".join(r.getMessage() for r in caplog.records)
        match = re.search(r"面板初始密码[^：]*：(\S+)", logs)
        assert match, "未取到初始密码"
        login = tc.post("/api/login",
                        json={"username": "admin", "password": match.group(1)})
        token = login.cookies.get("kp_session")
        assert token, login.text
        headers = {"x-kp-session": token}

        resp = tc.get("/api/selection/candidates?model=hy3", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["candidates"][0]["uid"] == "a"
        assert "只读" in body["notice"]

        resp = tc.get("/api/selection/balance", headers=headers)
        assert resp.status_code == 200, resp.text
        assert "verdict" in resp.json()
