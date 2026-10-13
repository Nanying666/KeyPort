"""凭证导入导出 API 测试：多种请求体形态、往返一致性、失败回报。"""

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
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    app = create_app(cfg)

    async def _mock_client():
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"code": 0})
        ))

    app.state.ctx.client.client = _mock_client  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


def _auth(client: TestClient) -> dict:
    resp = client.post("/api/login", json={"username": "admin", "password": "pw-12345678"})
    assert resp.status_code == 200, resp.text
    return {"x-kp-session": client.cookies.get("kp_session", "")}


def _doc(uid: str, token: str, realm: str = "cn", **extra) -> dict:
    doc = {
        "auth": {"accessToken": token, "refreshToken": f"r-{uid}",
                 "expiresAt": 1800000000, "domain": "", "realm": realm},
        "account": {"uid": uid, "enterpriseId": "", "nickname": f"nick-{uid}"},
    }
    doc.update(extra)
    return doc


# ---- 导入 ----


def test_import_accounts_array(client: TestClient):
    headers = _auth(client)
    resp = client.post("/api/accounts/import",
                       json={"accounts": [_doc("u1", "t1"), _doc("u2", "t2")]},
                       headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["imported"] == 2
    assert body["skipped"] == 0
    assert {a["uid"] for a in body["accounts"]} == {"u1", "u2"}

    listed = client.get("/api/accounts", headers=headers).json()["accounts"]
    assert {a["uid"] for a in listed} == {"u1", "u2"}


def test_import_bare_array(client: TestClient):
    headers = _auth(client)
    resp = client.post("/api/accounts/import", json=[_doc("bare", "t")], headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1


def test_import_via_pasted_text_object(client: TestClient):
    """粘贴导出文件内容（含 accounts 包裹层）应可导入。"""
    headers = _auth(client)
    exported = json.dumps({"version": 1, "accounts": [_doc("p1", "t1"), _doc("p2", "t2")]})
    resp = client.post("/api/accounts/import", json={"text": exported}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 2


def test_import_via_pasted_text_jsonl(client: TestClient):
    """每行一个 JSON（JSONL）应可导入。"""
    headers = _auth(client)
    text = "\n".join(json.dumps(_doc(f"l{i}", f"t{i}")) for i in range(3))
    resp = client.post("/api/accounts/import", json={"text": text}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 3


def test_import_flat_form(client: TestClient):
    headers = _auth(client)
    resp = client.post("/api/accounts/import",
                       json={"accounts": [{"accessToken": "flat-tok", "uid": "flat1"}]},
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
    listed = client.get("/api/accounts", headers=headers).json()["accounts"]
    assert any(a["uid"] == "flat1" for a in listed)


def test_import_missing_uid_gets_stable_fallback(client: TestClient):
    headers = _auth(client)
    resp = client.post("/api/accounts/import",
                       json={"accounts": [{"auth": {"accessToken": "no-uid-tok"}}]},
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1
    uid = resp.json()["accounts"][0]["uid"]
    assert uid.startswith("imported-")

    # 同一凭证重复导入应命中同一 uid（幂等，不产生重复账号）
    again = client.post("/api/accounts/import",
                        json={"accounts": [{"auth": {"accessToken": "no-uid-tok"}}]},
                        headers=headers)
    assert again.json()["accounts"][0]["uid"] == uid


def test_import_reports_per_item_failure_reason(client: TestClient):
    """逐条回报失败原因，而不是只给一个计数。"""
    headers = _auth(client)
    resp = client.post("/api/accounts/import",
                       json={"accounts": [
                           _doc("good", "t"),
                           {"auth": {"refreshToken": "no-access"}},
                           "not-an-object",
                       ]},
                       headers=headers)
    body = resp.json()
    assert body["imported"] == 1
    assert body["skipped"] == 2
    reasons = [f["reason"] for f in body["failed"]]
    assert any("accessToken" in r for r in reasons)
    assert any("JSON 对象" in r for r in reasons)


def test_import_expires_at_iso_string_does_not_500(client: TestClient):
    """回归：expiresAt 为 ISO 字符串曾让导入整体 500。"""
    headers = _auth(client)
    resp = client.post("/api/accounts/import",
                       json={"accounts": [{"auth": {"accessToken": "t",
                                                    "expiresAt": "2026-01-01T00:00:00Z"}}]},
                       headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 1


def test_import_empty_payload_reports_reason(client: TestClient):
    headers = _auth(client)
    resp = client.post("/api/accounts/import", json={}, headers=headers)
    assert resp.status_code == 200
    assert resp.json()["imported"] == 0
    assert resp.json()["failed"]


def test_import_writes_credential_file(client: TestClient):
    """导入的凭证应落盘到 auths/，与 login.sh 产物互通。"""
    headers = _auth(client)
    client.post("/api/accounts/import", json={"accounts": [_doc("disk1", "t")]}, headers=headers)
    auth_dir = client.app.state.ctx.cfg.auth_path()
    assert (auth_dir / "workbuddy-disk1.json").is_file()


# ---- 导出 ----


def test_export_contains_credentials_and_metadata(client: TestClient):
    headers = _auth(client)
    client.post("/api/accounts/import",
                json={"accounts": [_doc("u1", "tok-1", realm="global")]}, headers=headers)

    resp = client.get("/api/accounts/export", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 1
    assert body["version"] == 1
    doc = body["accounts"][0]
    assert doc["auth"]["accessToken"] == "tok-1"
    assert doc["auth"]["refreshToken"] == "r-u1"
    assert doc["auth"]["realm"] == "global"
    assert doc["account"]["uid"] == "u1"
    assert doc["account"]["nickname"] == "nick-u1"


def test_export_includes_device_token(client: TestClient):
    """回归：旧导出漏 device_token，迁移后账号会被上游风控降级。"""
    headers = _auth(client)
    client.post("/api/accounts/import",
                json={"accounts": [_doc("u1", "tok", device_token="dev-xyz")]},
                headers=headers)
    doc = client.get("/api/accounts/export", headers=headers).json()["accounts"][0]
    assert doc.get("device_token") == "dev-xyz"


def test_export_keeps_apikey_kind(client: TestClient):
    headers = _auth(client)
    client.post("/api/accounts/import",
                json={"accounts": [_doc("zai1", "sk", realm="zai", kind="apikey")]},
                headers=headers)
    doc = client.get("/api/accounts/export", headers=headers).json()["accounts"][0]
    assert doc.get("kind") == "apikey"


def test_export_realm_filter(client: TestClient):
    headers = _auth(client)
    client.post("/api/accounts/import",
                json={"accounts": [_doc("cn1", "t1", realm="cn"),
                                   _doc("g1", "t2", realm="global")]},
                headers=headers)
    body = client.get("/api/accounts/export?realm=global", headers=headers).json()
    assert body["count"] == 1
    assert body["accounts"][0]["account"]["uid"] == "g1"


def test_export_download_has_attachment_header(client: TestClient):
    headers = _auth(client)
    client.post("/api/accounts/import", json={"accounts": [_doc("u1", "t")]}, headers=headers)
    resp = client.get("/api/accounts/export/download", headers=headers)
    assert resp.status_code == 200
    assert "attachment" in resp.headers.get("content-disposition", "")
    assert resp.json()["count"] == 1


# ---- 往返 ----


def test_export_import_roundtrip_preserves_everything(client: TestClient):
    """核心保证：导出文件必须能原样回灌，字段零丢失。"""
    headers = _auth(client)
    client.post("/api/accounts/import", json={"accounts": [
        _doc("u1", "tok-1", realm="global", device_token="dev-1"),
        _doc("zai1", "sk-zai", realm="zai", kind="apikey"),
        {"accessToken": "flat-tok", "uid": "flat1", "nickname": "flat-nick"},
    ]}, headers=headers)

    exported = client.get("/api/accounts/export", headers=headers).json()

    for uid in ("u1", "zai1", "flat1"):
        client.delete(f"/api/accounts/{uid}", headers=headers)
    assert client.get("/api/accounts", headers=headers).json()["accounts"] == []

    resp = client.post("/api/accounts/import", json=exported, headers=headers)
    assert resp.json()["imported"] == 3
    assert resp.json()["skipped"] == 0

    listed = {a["uid"]: a for a in client.get("/api/accounts", headers=headers).json()["accounts"]}
    assert set(listed) == {"u1", "zai1", "flat1"}
    assert listed["u1"]["realm"] == "global"
    assert listed["zai1"]["kind"] == "apikey"
    assert listed["zai1"]["realm"] == "zai"


def test_import_export_requires_admin(client: TestClient):
    assert client.get("/api/accounts/export").status_code == 401
    assert client.post("/api/accounts/import", json={"accounts": []}).status_code == 401


def test_bootstrap_password_does_not_write_global_config(tmp_path: Path):
    """回归：临时构造的 Config 绝不能把随机密码写进项目根的 config.json。

    根因：bootstrap_admin_password 曾无条件 save_config(cfg)，导致任何用
    Config(...) 直接构造实例的调用方（脚本/测试）都会覆盖生产配置文件。
    """
    from keyport.api.admin_auth import bootstrap_admin_password
    from keyport.config import DEFAULT_CONFIG_PATH
    from keyport.db import init_db

    real = DEFAULT_CONFIG_PATH
    before = real.read_bytes() if real.is_file() else None

    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = ""
    db = init_db(tmp_path / "probe.db")

    password = bootstrap_admin_password(db, cfg)
    assert password  # 生成了随机密码

    # 只写自己的 source_path，不碰项目根配置
    assert cfg.source_path.is_file()
    after = real.read_bytes() if real.is_file() else None
    assert after == before, "bootstrap_admin_password 写坏了项目根的 config.json"
