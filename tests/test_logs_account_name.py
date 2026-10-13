"""调用日志的账号展示：返回昵称而非 uid 前 8 位。

背景：日志表只存 account_uid，前端原先显示 `uid.slice(0, 8)`，无法辨认
是哪个账号（多账号时全是乱码般的前缀）。改为接口按 uid 关联账号昵称。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app
from keyport.security import generate_session_token, hash_session_token
from keyport.upstream.base import AccountAuth

SESSION = generate_session_token()


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(
        uid="8c030511-9296-4e96-a684-fbe7bc348faf", realm="global",
        nickname="alexisbutldp3d", access_token="T", kind="oauth"))
    ctx.pool.upsert_account(AccountAuth(
        uid="fe9be650-56a2-46be-8a50-db8d44e17fb4", realm="zai",
        nickname="iyzcfc", access_token="Z", kind="apikey"))
    ctx.db.execute(
        "INSERT INTO admin_sessions(token_hash, username, created_at, expires_at, ip) "
        "VALUES(?,?,?,?,?)",
        (hash_session_token(SESSION), "admin", 0, 9999999999, "127.0.0.1"))
    with TestClient(app) as tc:
        yield tc


AUTH = {"Cookie": f"kp_session={SESSION}"}


def _insert_log(ctx, uid: str) -> None:
    ctx.db.execute(
        "INSERT INTO request_logs(ts, key_id, key_name, account_uid, realm, protocol, "
        "model, stream, status, ttfb_ms, total_ms, prompt_tokens, completion_tokens, "
        "total_tokens, credits, retries, client_ip, error) "
        "VALUES(0,0,'k',?, 'global','chat','m',1,200,0,0,0,0,0,0,0,'','')",
        (uid,))


def test_logs_return_account_nickname(client: TestClient):
    ctx = client.app.state.ctx
    _insert_log(ctx, "8c030511-9296-4e96-a684-fbe7bc348faf")
    resp = client.get("/api/logs?limit=10", headers=AUTH)
    assert resp.status_code == 200, resp.text
    row = resp.json()["logs"][0]
    assert row["account_name"] == "alexisbutldp3d"


def test_logs_fall_back_to_uid_prefix_for_deleted_account(client: TestClient):
    """账号已删除时回落 uid 前缀，历史日志仍可辨认。"""
    ctx = client.app.state.ctx
    _insert_log(ctx, "deadbeef-0000-0000-0000-000000000000")
    resp = client.get("/api/logs?limit=10", headers=AUTH)
    assert resp.status_code == 200, resp.text
    row = resp.json()["logs"][0]
    assert row["account_name"] == "deadbeef"


def test_logs_without_account_have_empty_name(client: TestClient):
    ctx = client.app.state.ctx
    _insert_log(ctx, "")
    resp = client.get("/api/logs?limit=10", headers=AUTH)
    row = resp.json()["logs"][0]
    assert row["account_name"] == ""
