"""管理台 API 测试：登录、密钥 CRUD、设置热改、IP 管控、账号运维。"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from conftest import make_test_config
from keyport.app import create_app
from keyport.security import hash_password


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("secret-password")
    app = create_app(cfg)
    ctx = app.state.ctx

    from keyport.upstream.base import AccountAuth

    ctx.pool.upsert_account(AccountAuth(
        uid="u1", realm="cn", nickname="alice", access_token="tok",
        refresh_token="r", expires_at=0,
    ))

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"code": 0, "data": {"credits": 500}})
        ))

    ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


def _login(client: TestClient) -> dict:
    resp = client.post("/api/login", json={"username": "admin", "password": "secret-password"})
    assert resp.status_code == 200, resp.text
    return {"x-kp-session": client.cookies.get("kp_session", "")}


def test_login_rejects_bad_password(client: TestClient):
    resp = client.post("/api/login", json={"username": "admin", "password": "nope"})
    assert resp.status_code == 401


def test_login_sets_cookie(client: TestClient):
    resp = client.post("/api/login", json={"username": "admin", "password": "secret-password"})
    assert resp.status_code == 200
    assert client.cookies.get("kp_session")


def test_protected_route_requires_auth(client: TestClient):
    assert client.get("/api/accounts").status_code == 401


def test_accounts_listing(client: TestClient):
    headers = _login(client)
    resp = client.get("/api/accounts", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["accounts"]) == 1
    assert body["accounts"][0]["uid"] == "u1"
    assert body["accounts"][0]["token_state"] in ("healthy", "tight", "soon", "expired")


def test_account_disable_and_revive(client: TestClient):
    headers = _login(client)
    assert client.post("/api/accounts/u1/disable", json={"reason": "test"},
                       headers=headers).status_code == 200
    body = client.get("/api/accounts", headers=headers).json()
    assert body["accounts"][0]["manual_disabled"] is True
    assert body["accounts"][0]["in_pool"] is False

    assert client.post("/api/accounts/u1/revive", headers=headers).status_code == 200
    body = client.get("/api/accounts", headers=headers).json()
    assert body["accounts"][0]["manual_disabled"] is False


def test_account_delete(client: TestClient):
    headers = _login(client)
    assert client.delete("/api/accounts/u1", headers=headers).status_code == 200
    assert client.get("/api/accounts", headers=headers).json()["accounts"] == []


# ---- 密钥 ----


def test_key_lifecycle(client: TestClient):
    headers = _login(client)

    created = client.post("/api/keys", json={
        "name": "prod", "realm": "cn", "model_allow": ["glm-4.6"],
        "quota_credits": 100, "ip_allow": ["10.0.0.0/8"],
    }, headers=headers)
    assert created.status_code == 200
    raw = created.json()["key"]
    assert raw.startswith("sk-kp")
    key_id = created.json()["id"]

    listing = client.get("/api/keys", headers=headers).json()["keys"]
    assert listing[0]["name"] == "prod"
    assert listing[0]["model_allow"] == ["glm-4.6"]
    # 列表只暴露前缀，绝不返回明文
    assert raw not in json.dumps(listing)

    updated = client.patch(f"/api/keys/{key_id}", json={"enabled": False}, headers=headers)
    assert updated.status_code == 200
    assert updated.json()["key"]["enabled"] is False

    assert client.delete(f"/api/keys/{key_id}", headers=headers).status_code == 200
    assert client.get("/api/keys", headers=headers).json()["keys"] == []


def test_key_rejects_bad_cidr(client: TestClient):
    headers = _login(client)
    resp = client.post("/api/keys", json={"name": "x", "ip_allow": ["not-a-cidr"]},
                       headers=headers)
    assert resp.status_code == 400


def test_key_reveal_not_possible(client: TestClient):
    headers = _login(client)
    created = client.post("/api/keys", json={"name": "x"}, headers=headers).json()
    resp = client.post(f"/api/keys/{created['id']}/reveal-check", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["retrievable"] is False


# ---- 设置 ----


def test_settings_never_leak_password_hash(client: TestClient):
    headers = _login(client)
    resp = client.get("/api/settings", headers=headers)
    assert resp.status_code == 200
    assert "admin_password_hash" not in resp.text


def test_settings_hot_update_prompt_mode(client: TestClient):
    headers = _login(client)
    resp = client.patch("/api/settings", json={"prompt": {"mode": "passthrough"}},
                        headers=headers)
    assert resp.status_code == 200
    assert "prompt.mode" in resp.json()["applied"]
    assert client.app.state.ctx.cfg.prompt.mode == "passthrough"


def test_settings_rejects_bad_prompt_mode(client: TestClient):
    headers = _login(client)
    assert client.patch("/api/settings", json={"prompt": {"mode": "bogus"}},
                        headers=headers).status_code == 400


def test_settings_rejects_bad_cidr(client: TestClient):
    headers = _login(client)
    resp = client.patch("/api/settings",
                        json={"security": {"global_allowlist": ["bad"]}}, headers=headers)
    assert resp.status_code == 400


def test_settings_hot_update_does_not_touch_real_config(client: TestClient, tmp_path: Path):
    """回归：管理台热改只写测试临时配置，绝不碰项目根的 config.json。"""
    real_config = Path(__file__).resolve().parent.parent / "config.json"
    before = real_config.read_bytes() if real_config.is_file() else None

    headers = _login(client)
    client.patch("/api/settings", json={"pool": {"top_k": 3}}, headers=headers)

    after = real_config.read_bytes() if real_config.is_file() else None
    assert after == before, "测试写坏了项目根的 config.json"


# ---- 概览与审计 ----


def test_overview(client: TestClient):
    headers = _login(client)
    resp = client.get("/api/overview", headers=headers)
    assert resp.status_code == 200
    assert "pool" in resp.json() and "last_24h" in resp.json()


def test_audit_records_login(client: TestClient):
    headers = _login(client)
    resp = client.get("/api/audit", headers=headers)
    assert resp.status_code == 200
    assert any(row["action"] == "admin_login" for row in resp.json()["audit"])


# ---- 账号导入导出 ----


def test_export_then_import_roundtrip(client: TestClient):
    headers = _login(client)
    exported = client.get("/api/accounts/export", headers=headers).json()
    assert len(exported["accounts"]) == 1

    client.delete("/api/accounts/u1", headers=headers)
    assert client.get("/api/accounts", headers=headers).json()["accounts"] == []

    resp = client.post("/api/accounts/import",
                       json={"accounts": exported["accounts"]}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
    assert len(client.get("/api/accounts", headers=headers).json()["accounts"]) == 1


def test_apikey_account_add(client: TestClient):
    headers = _login(client)
    resp = client.post("/api/accounts/apikey",
                       json={"realm": "zai", "api_key": "zai-secret", "name": "zai-1"},
                       headers=headers)
    assert resp.status_code == 200
    uid = resp.json()["uid"]
    body = client.get("/api/accounts", headers=headers).json()
    assert any(a["uid"] == uid and a["realm"] == "zai" for a in body["accounts"])


# ---- 日志与用量 ----


def test_logs_and_usage_endpoints(client: TestClient):
    headers = _login(client)
    assert client.get("/api/logs", headers=headers).status_code == 200
    usage = client.get("/api/usage?days=7", headers=headers)
    assert usage.status_code == 200
    assert "by_day" in usage.json()


def test_billing_endpoint(client: TestClient):
    headers = _login(client)
    resp = client.get("/api/billing", headers=headers)
    assert resp.status_code == 200
    assert len(resp.json()["accounts"]) == 1


def _seed_logs(client: TestClient) -> None:
    """写入跨域样本：cn 与 zai 各若干条，验证聚合按 realm 拆分。"""
    from keyport.api.logging import RequestRecord

    logger = client.app.state.ctx.request_logger
    # cn：两条（同一天、同密钥）
    for _ in range(2):
        logger.log(RequestRecord(key_id=1, key_name="k1", account_uid="u1", realm="cn",
                                 model="glm-4.6", total_tokens=100, credits=5))
    # zai：一条（额度单位是 token，credits 记的是扣减量）
    logger.log(RequestRecord(key_id=1, key_name="k1", account_uid="z1", realm="zai",
                             model="glm-5.3", total_tokens=300, credits=7))


def test_usage_by_day_grouped_by_realm(client: TestClient):
    """by_day 必须按 (day, realm) 分组，不能把不同域的额度混成一行。"""
    headers = _login(client)
    _seed_logs(client)
    body = client.get("/api/usage?days=7", headers=headers).json()
    rows = body["by_day"]
    # 每个 realm 各一行（同一天）
    realms = {r["realm"] for r in rows}
    assert realms == {"cn", "zai"}
    by_realm = {r["realm"]: r for r in rows}
    assert by_realm["cn"]["calls"] == 2
    assert by_realm["cn"]["credits"] == 10
    assert by_realm["cn"]["total_tokens"] == 200
    assert by_realm["zai"]["calls"] == 1
    assert by_realm["zai"]["credits"] == 7


def test_usage_by_key_grouped_by_realm(client: TestClient):
    """by_key 必须按 (key, realm) 分组，同密钥跨域不能合并成一行。"""
    headers = _login(client)
    _seed_logs(client)
    body = client.get("/api/usage?days=7", headers=headers).json()
    rows = body["by_key"]
    keys = {(r["key_id"], r["realm"]) for r in rows}
    assert keys == {(1, "cn"), (1, "zai")}
    by_realm = {r["realm"]: r for r in rows}
    assert by_realm["cn"]["credits"] == 10
    assert by_realm["cn"]["total_tokens"] == 200
    assert by_realm["zai"]["credits"] == 7
    assert by_realm["zai"]["total_tokens"] == 300


def test_usage_by_model_keeps_realm(client: TestClient):
    """by_model 已按 (model, realm) 分组，回归保护。"""
    headers = _login(client)
    _seed_logs(client)
    body = client.get("/api/usage?days=7", headers=headers).json()
    rows = body["by_model"]
    assert {(r["model"], r["realm"]) for r in rows} == {("glm-4.6", "cn"), ("glm-5.3", "zai")}
