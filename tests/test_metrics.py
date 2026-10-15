"""Prometheus 文本格式指标导出。

移植自 sheng-du0521/workbuddy2api 的 `internal/server/metrics_prom.go`。
重点是**确定性输出**与**不暴露高基数维度**。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import make_test_config

from keyport.alert import Health
from keyport.app import build_context, create_app
from keyport.metrics import (
    PROM_POOL_STATES,
    PROM_REALMS,
    escape_help,
    escape_label,
    format_value,
    render_metrics,
)
from keyport.upstream.base import AccountAuth


def _health(**kw) -> Health:
    return Health(**kw)


def _render(**kw) -> str:
    base = {"health": {label: Health() for label, _ in PROM_REALMS}}
    base.update(kw)
    return render_metrics(**base)


# ---- 转义与格式化 ----


def test_escape_help():
    assert escape_help("a\\b\nc") == "a\\\\b\\nc"


def test_escape_label():
    """模型名可能来自客户端；不转义会写出非法 exposition → 整个 scrape 被丢弃。"""
    assert escape_label('say "hi"') == 'say \\"hi\\"'
    assert escape_label("a\\b") == "a\\\\b"
    assert escape_label("a\nb") == "a\\nb"


@pytest.mark.parametrize("value,want", [
    (0, "0"), (1, "1"), (3.0, "3"), (-2.0, "-2"),
    (float("nan"), "NaN"), (float("inf"), "+Inf"), (float("-inf"), "-Inf"),
])
def test_format_value(value, want):
    assert format_value(value) == want


def test_format_value_bool():
    assert format_value(True) == "1"
    assert format_value(False) == "0"


# ---- 家族去重 ----


def test_family_written_once():
    """Prometheus 解析器对重复的 HELP/TYPE 会报错。"""
    text = _render()
    assert text.count("# HELP keyport_pool_accounts ") == 1
    assert text.count("# TYPE keyport_pool_accounts ") == 1


def test_every_family_has_help_and_type():
    text = _render()
    helps = {line.split()[2] for line in text.splitlines()
             if line.startswith("# HELP ")}
    types = {line.split()[2] for line in text.splitlines()
             if line.startswith("# TYPE ")}
    assert helps == types
    assert helps, "至少要输出一个家族"


# ---- 池指标 ----


def test_pool_states_emitted_for_every_realm():
    text = _render()
    for label, _realm in PROM_REALMS:
        for state in PROM_POOL_STATES:
            assert f'keyport_pool_accounts{{realm="{label}",state="{state}"}}' in text


def test_pool_totals_and_in_flight():
    health = {
        "cn": _health(total=3, healthy=2, in_flight=1),
        "global": _health(total=1, healthy=0, in_flight=0),
        "all": _health(total=4, healthy=2, in_flight=1),
    }
    text = _render(health=health)
    assert 'keyport_pool_accounts_total{realm="all"} 4' in text
    assert 'keyport_pool_in_flight{realm="all"} 1' in text


def test_sticky_and_waf():
    text = _render(sticky_sessions=7, waf_active=True)
    assert "keyport_sticky_sessions 7" in text
    assert "keyport_waf_ip_block_active 1" in text
    assert "keyport_waf_ip_block_active 0" in _render(waf_active=False)


# ---- 任务台账 ----


def test_task_metrics_omitted_when_never_run():
    """从未跑过的任务**没有序列**，而不是「时刻为 0」。

    否则服务刚启动时按 `time() - last_run > 86400` 的告警会在每次部署误报一轮。
    """
    text = _render(tasks={})
    assert "keyport_task_last_run_timestamp_seconds" not in text
    assert "keyport_task_last_run_accounts" not in text


def test_task_metrics_only_for_ran_kinds():
    text = _render(tasks={"checkin": {"total": 3, "ok": 2, "fail": 1,
                                      "skipped": 0, "finished": 1000,
                                      "all_failed": False}})
    assert 'keyport_task_last_run_timestamp_seconds{kind="checkin"} 1000' in text
    assert 'kind="travel"' not in text


def test_task_all_failed_flag():
    text = _render(tasks={"checkin": {"total": 3, "ok": 0, "fail": 3,
                                      "skipped": 0, "finished": 1000,
                                      "all_failed": True}})
    assert 'keyport_task_last_run_all_failed{kind="checkin"} 1' in text


# ---- 请求 / 模型 ----


def test_request_totals():
    text = _render(totals={"success": 10, "failed": 2})
    assert 'keyport_requests_total{status="success"} 10' in text
    assert 'keyport_requests_total{status="failed"} 2' in text


def test_model_metrics_sorted_and_complete():
    models = [
        {"model": "zeta", "success": 1, "failed": 0, "prompt_tokens": 5,
         "completion_tokens": 7, "credits": 0.5, "avg_ttfb_ms": 1500},
        {"model": "alpha", "success": 2, "failed": 1, "prompt_tokens": 10,
         "completion_tokens": 20, "credits": 1.0, "avg_ttfb_ms": 2000},
    ]
    text = _render(models=models)
    # 字典序（稳定输出）
    assert text.index('model="alpha"') < text.index('model="zeta"')
    assert 'keyport_model_requests_total{model="alpha",status="success"} 2' in text
    assert 'keyport_model_tokens_total{model="alpha",type="prompt"} 10' in text
    assert 'keyport_model_credit_total{model="zeta"} 0.5' in text
    assert 'keyport_model_avg_ttfb_seconds{model="alpha"} 2' in text


def test_output_is_deterministic():
    """同一状态下两次抓取必须**逐字节相等**（否则看板 diff 会抖）。"""
    models = [{"model": "b", "success": 1}, {"model": "a", "success": 2}]
    tasks = {"checkin": {"total": 1, "ok": 1, "finished": 5},
             "travel": {"total": 1, "ok": 0, "fail": 1, "finished": 6}}
    a = _render(models=models, tasks=tasks, totals={"success": 1})
    b = _render(models=list(reversed(models)), tasks=dict(reversed(list(tasks.items()))),
                totals={"success": 1})
    assert a == b


def test_no_high_cardinality_or_secret_labels():
    """不加 per-uid 标签（泄漏 uid 且基数不可控）；输出不含凭证/会话键。"""
    models = [{"model": "m", "success": 1, "credits": 0}]
    text = _render(models=models, totals={"success": 1})
    # 只扫 label 段（指标名里的 tokens_total 是正常业务名，不算泄漏）
    labels = "".join(
        part.split("}")[0] for part in text.splitlines() if "{" in part
    )
    for banned in ("uid", "cookie", "authorization", "session_key", "credential"):
        assert banned not in labels.lower()
    # 整段输出里不得出现凭证形态
    for banned in ("Bearer ", "sk-", "password", "secret"):
        assert banned not in text


def test_trailing_newline():
    assert _render().endswith("\n")


# ---- 端点 ----


def test_metrics_endpoint(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T"))
    ctx.db.execute(
        "INSERT INTO request_logs(ts, key_id, key_name, account_uid, realm, protocol, "
        "model, stream, status, ttfb_ms, total_ms, prompt_tokens, completion_tokens, "
        "total_tokens, credits, retries, client_ip, error) "
        "VALUES(?,0,'','u1','cn','openai','hy3',1,200,100,200,5,7,12,0.0,0,'','')",
        (int(__import__("time").time()),),
    )
    ctx.db.execute(
        "INSERT INTO task_runs(ts, task, realm, account, ok, detail) "
        "VALUES(?, 'checkin', 'cn', 'u1', 1, 'ok')",
        (int(__import__("time").time()),),
    )

    from fastapi.testclient import TestClient

    with TestClient(app) as tc:
        resp = tc.get("/metrics")
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/plain")
        text = resp.text

    assert "# TYPE keyport_pool_accounts gauge" in text
    assert 'keyport_pool_accounts_total{realm="all"} 1' in text
    assert 'keyport_model_requests_total{model="hy3",status="success"} 1' in text
    assert 'keyport_task_last_run_accounts{kind="checkin",result="ok"} 1' in text


def test_task_ledger_aggregates_last_round(tmp_path: Path):
    """台账取「最近一轮」而非全部历史；跳过与失败分开计数。"""
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    now = int(__import__("time").time())
    rows = [
        (now - 100, "checkin", "u1", 1, "ok"),        # 上一轮
        (now, "checkin", "u1", 1, "ok"),              # 最近一轮
        (now, "checkin", "u2", 1, "skipped: 国际版无国内任务体系，跳过"),
        (now, "checkin", "u3", 0, "auth: boom"),
    ]
    for ts, task, acct, ok, detail in rows:
        ctx.db.execute(
            "INSERT INTO task_runs(ts, task, realm, account, ok, detail) "
            "VALUES(?,?,?,?,?,?)",
            (ts, task, "cn", acct, ok, detail),
        )
    from keyport.app import _task_ledger

    ledger = _task_ledger(ctx)
    entry = ledger["checkin"]
    assert entry["total"] == 3          # 只算最近一轮的 3 条
    assert entry["ok"] == 1
    assert entry["skipped"] == 1
    assert entry["fail"] == 1
    assert entry["all_failed"] is False
    assert "travel" not in ledger       # 从未跑过 → 无序列


def test_task_ledger_marks_all_failed(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    now = int(__import__("time").time())
    for acct in ("u1", "u2"):
        ctx.db.execute(
            "INSERT INTO task_runs(ts, task, realm, account, ok, detail) "
            "VALUES(?, 'checkin', 'cn', ?, 0, 'auth: boom')",
            (now, acct),
        )
    from keyport.app import _task_ledger

    assert _task_ledger(ctx)["checkin"]["all_failed"] is True
