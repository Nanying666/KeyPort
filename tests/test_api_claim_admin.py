"""管理台新增能力：设备档案、安装序、限时套餐领取、配置热改。

覆盖本轮新增的 `/api/accounts/{uid}/fingerprint|rotate-fingerprint|install`
与 `/api/claim/*`，以及 pool.risk_* / claim.* / install.* 的设置热改。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.security import hash_password

ZCODE_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"


def _handler(request: httpx.Request) -> httpx.Response:
    # 必须用 path 而非完整 URL 判断：preview 带 app_version/platform 查询串，
    # 用 str(request.url) 做 endswith 会永远不匹配。
    path = request.url.path
    if "client/configs" in path or "event/report" in path:
        return httpx.Response(200, json={"code": 0, "data": {"configs": {}}})
    if path.endswith("/billing/preview"):
        return httpx.Response(200, json={"code": 0, "data": {"plans": [
            {"plan_id": "p1", "name": "限时包", "priority": 5, "entitlements": [
                {"meter": "model_usage", "unit_type": "token",
                 "show_name": "GLM-5.3", "grant_units": 1000, "period": "daily"}]},
        ]}})
    if path.endswith("/billing/claim"):
        return httpx.Response(200, json={"code": 0, "data": {
            "server_time": 1_700_000_000, "plan": {"ends_at": 1_700_086_400}}})
    if path.endswith("/billing/balance"):
        return httpx.Response(200, json={"code": 0, "data": {"balances": [
            {"remaining_units": 500, "used_units": 10, "expires_at": 0}]}})
    return httpx.Response(200, json={"code": 0, "data": {}})


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    cfg.install.auto_on_add = False      # 由用例显式触发
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx

    from keyport.upstream.base import AccountAuth

    ctx.pool.upsert_account(AccountAuth(
        uid="zai-1", realm="zai", nickname="acc1", access_token="PLAN_JWT",
        kind="apikey", device_token="dev-mid-1",
        base_url=ZCODE_BASE, protocol="anthropic",
    ))

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        yield tc


def _auth(client: TestClient) -> dict:
    resp = client.post("/api/login", json={"username": "admin", "password": "pw-12345678"})
    assert resp.status_code == 200, resp.text
    return {"x-kp-session": client.cookies.get("kp_session", "")}


# ---- 设备档案 ----


def test_account_view_exposes_fingerprint(client: TestClient):
    """账号列表必须暴露设备档案（一号一台设备的可视化依据）。"""
    headers = _auth(client)
    acc = client.get("/api/accounts", headers=headers).json()["accounts"][0]
    fp = acc["fingerprint"]
    assert fp["device_mid"]
    assert fp["platform"] in ("darwin", "win32")
    assert fp["screen"]
    assert acc["installed_at"] == 0
    assert acc["risk_strikes"] == 0


def test_fingerprint_endpoint(client: TestClient):
    headers = _auth(client)
    resp = client.get("/api/accounts/zai-1/fingerprint", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["fingerprint"]["device_mid"]
    assert body["installed_at"] == 0


def test_rotate_fingerprint_changes_device_mid(client: TestClient):
    """换发必换 device_mid（风控后换设备语义）。"""
    headers = _auth(client)
    before = client.get("/api/accounts/zai-1/fingerprint", headers=headers).json()
    resp = client.post("/api/accounts/zai-1/rotate-fingerprint", headers=headers)
    assert resp.status_code == 200
    after = resp.json()["fingerprint"]
    assert after["device_mid"] != before["fingerprint"]["device_mid"]
    # 换发后安装序幂等位必须清空（新设备要重跑安装）
    listed = client.get("/api/accounts", headers=headers).json()["accounts"][0]
    assert listed["installed_at"] == 0


def test_rotate_fingerprint_unknown_account(client: TestClient):
    headers = _auth(client)
    assert client.post("/api/accounts/nope/rotate-fingerprint",
                       headers=headers).status_code == 404


def test_fingerprint_endpoints_require_admin(client: TestClient):
    assert client.get("/api/accounts/zai-1/fingerprint").status_code == 401
    assert client.post("/api/accounts/zai-1/rotate-fingerprint").status_code == 401


# ---- 安装序 ----


def test_install_endpoint_runs_and_marks_installed(client: TestClient):
    headers = _auth(client)
    resp = client.post("/api/accounts/zai-1/install", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["installed"] is True
    assert body["configs_fetched"] is True
    assert set(body["events_reported"]) == {"app_launch", "app_daily_active"}
    listed = client.get("/api/accounts", headers=headers).json()["accounts"][0]
    assert listed["installed_at"] > 0


def test_install_endpoint_is_idempotent(client: TestClient):
    headers = _auth(client)
    client.post("/api/accounts/zai-1/install", headers=headers)
    again = client.post("/api/accounts/zai-1/install", headers=headers).json()
    assert again["skipped"] is True


def test_install_requires_admin(client: TestClient):
    assert client.post("/api/accounts/zai-1/install").status_code == 401


# ---- 套餐领取 ----


def test_claim_preview_lists_plans(client: TestClient):
    headers = _auth(client)
    resp = client.get("/api/claim/preview", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    account = body["accounts"][0]
    assert account["ok"] is True
    assert account["plans"][0]["plan_id"] == "p1"
    assert account["plans"][0]["grants"][0]["name"] == "GLM-5.3"


def test_claim_preview_scoped_to_uid(client: TestClient):
    headers = _auth(client)
    body = client.get("/api/claim/preview?uid=zai-1", headers=headers).json()
    assert len(body["accounts"]) == 1


def test_claim_run_for_uid(client: TestClient):
    """指定账号领取：本端农场求解验证码后提交。"""
    headers = _auth(client)
    resp = client.post("/api/claim/run", json={"uid": "zai-1"}, headers=headers)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["scope"] == "zai-1"
    assert body["result"]["plan_id"] == "p1"
    assert body["result"]["ends_at"] == 1_700_086_400_000


def test_claim_run_records_history(client: TestClient):
    headers = _auth(client)
    client.post("/api/claim/run", json={"uid": "zai-1"}, headers=headers)
    hist = client.get("/api/claim/history", headers=headers).json()["history"]
    assert hist and hist[0]["outcome"] == "claimed"
    assert hist[0]["plan_id"] == "p1"


def test_claim_history_filtered_by_uid(client: TestClient):
    headers = _auth(client)
    client.post("/api/claim/run", json={"uid": "zai-1"}, headers=headers)
    assert len(client.get("/api/claim/history?uid=zai-1", headers=headers).json()["history"]) == 1
    assert client.get("/api/claim/history?uid=nope", headers=headers).json()["history"] == []


def test_claim_run_unknown_account(client: TestClient):
    headers = _auth(client)
    assert client.post("/api/claim/run", json={"uid": "nope"},
                       headers=headers).status_code == 404


def test_claim_endpoints_require_admin(client: TestClient):
    assert client.get("/api/claim/preview").status_code == 401
    assert client.post("/api/claim/run", json={"uid": "zai-1"}).status_code == 401
    assert client.get("/api/claim/history").status_code == 401


def test_claim_run_reports_business_error(tmp_path: Path):
    """上游业务码失败必须以 400 + code 回报（前端可展示原因）。"""
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    cfg.install.auto_on_add = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    from keyport.upstream.base import AccountAuth

    ctx.pool.upsert_account(AccountAuth(
        uid="zai-1", realm="zai", access_token="JWT", kind="apikey",
        base_url=ZCODE_BASE, protocol="anthropic"))

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/billing/preview"):
            return httpx.Response(200, json={"code": 0, "data": {"plans": [
                {"plan_id": "p1", "priority": 1, "entitlements": []}]}})
        if request.url.path.endswith("/billing/claim"):
            return httpx.Response(200, json={"code": 1005, "msg": "名额用完",
                                             "data": {"plan": {"ends_at": 1_700_100_000}}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        headers = _auth(tc)
        resp = tc.post("/api/claim/run", json={"uid": "zai-1"}, headers=headers)
        assert resp.status_code == 400
        body = resp.json()
        assert body["code"] == 1005
        assert body["next_at"] == 1_700_100_000_000


# ---- 任务状态 ----


def test_task_status_includes_claim_state(client: TestClient):
    headers = _auth(client)
    body = client.get("/api/tasks", headers=headers).json()
    assert body["claims"] is not None
    assert "round_interval_seconds" in body["claims"]


# ---- 设置热改 ----


def test_settings_hot_update_risk_params(client: TestClient):
    headers = _auth(client)
    resp = client.patch("/api/settings", json={"pool": {
        "risk_cooldown_seconds": 1200, "risk_ban_strikes": 6,
        "risk_auto_rotate": False, "retry_429_times": 7,
    }}, headers=headers)
    assert resp.status_code == 200
    applied = resp.json()["applied"]
    assert "pool.risk_cooldown_seconds" in applied
    assert "pool.risk_ban_strikes" in applied
    assert "pool.risk_auto_rotate" in applied
    cfg = client.app.state.ctx.cfg
    assert cfg.pool.risk_cooldown_seconds == 1200
    assert cfg.pool.risk_ban_strikes == 6
    assert cfg.pool.risk_auto_rotate is False
    assert cfg.pool.retry_429_times == 7


def test_settings_hot_update_claim_params(client: TestClient):
    headers = _auth(client)
    resp = client.patch("/api/settings", json={"claim": {
        "enabled": False, "round_interval_seconds": 900,
    }}, headers=headers)
    assert resp.status_code == 200
    cfg = client.app.state.ctx.cfg
    assert cfg.claim.enabled is False
    assert cfg.claim.round_interval_seconds == 900


def test_settings_hot_update_install_params(client: TestClient):
    headers = _auth(client)
    resp = client.patch("/api/settings", json={"install": {
        "enabled": False, "auto_on_add": False,
    }}, headers=headers)
    assert resp.status_code == 200
    assert client.app.state.ctx.cfg.install.enabled is False


def test_settings_exposes_new_sections(client: TestClient):
    """新配置段必须出现在设置视图里（管理台据此渲染表单）。"""
    headers = _auth(client)
    cfg = client.get("/api/settings", headers=headers).json()["config"]
    assert "claim" in cfg and "install" in cfg
    assert "risk_cooldown_seconds" in cfg["pool"]


def test_settings_hot_update_does_not_touch_real_config(client: TestClient):
    """回归：新增配置段的热改同样只能写测试临时文件。"""
    real_config = Path(__file__).resolve().parent.parent / "config.json"
    before = real_config.read_bytes() if real_config.is_file() else None

    headers = _auth(client)
    client.patch("/api/settings", json={
        "pool": {"risk_ban_strikes": 9},
        "claim": {"round_interval_seconds": 120},
    }, headers=headers)

    after = real_config.read_bytes() if real_config.is_file() else None
    assert after == before, "测试写坏了项目根的 config.json"


# ---- 账号新增后的自动收尾（仅 ZCode 平台账号）----


def _auto_cfg(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    cfg.install.enabled = True
    cfg.install.auto_on_add = True
    cfg.claim.enabled = True
    cfg.claim.auto_on_add = True
    return cfg


def test_zcode_apikey_add_runs_install_and_claim(tmp_path: Path):
    """ZCode 平台账号（base_url 指向 Coding Plan）：自动跑安装序 + 领取。"""
    cfg = _auto_cfg(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        headers = _auth(tc)
        resp = tc.post("/api/accounts/apikey", json={
            "realm": "zai", "api_key": "JWT", "name": "auto",
            "base_url": ZCODE_BASE,
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["zcode_platform"] is True
        assert body["install"]["installed"] is True
        assert body["claim"]
        acc = [a for a in tc.get("/api/accounts", headers=headers).json()["accounts"]
               if a["uid"] == body["uid"]][0]
        assert acc["installed_at"] > 0


def test_workbuddy_apikey_add_skips_install_and_claim(tmp_path: Path):
    """非 ZCode 账号（WorkBuddy / CodeBuddy）：整体跳过安装序与领取。

    它们没有 Coding Plan 权益，打上游 billing 只会 404，激活上报更是伪造
    无关信号 —— 必须零上游请求。
    """
    cfg = _auto_cfg(tmp_path)
    app = create_app(cfg)
    ctx = app.state.ctx
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(200, json={"code": 0, "data": {}})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        headers = _auth(tc)
        resp = tc.post("/api/accounts/apikey", json={
            "realm": "cn", "api_key": "wb-key", "name": "wb",
        }, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["zcode_platform"] is False
        assert "install" not in body
        assert "claim" not in body
        assert "skipped_reason" in body
        acc = [a for a in tc.get("/api/accounts", headers=headers).json()["accounts"]
               if a["uid"] == body["uid"]][0]
        assert acc["installed_at"] == 0
    # 零上游请求：没有 event/report、没有 billing/preview
    assert not any("event/report" in p or "billing" in p for p in seen), seen


def test_apikey_account_keeps_base_url_and_protocol(tmp_path: Path):
    """回归：API Key 纳管的 base_url 曾写进 file_path，导致端点丢失。

    丢失 base_url 的后果不只是走错端点 —— 该账号也不再被识别为 ZCode 平台账号，
    额度查询与套餐领取会静默失效。
    """
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    cfg.install.auto_on_add = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(_handler))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        headers = _auth(tc)
        body = tc.post("/api/accounts/apikey", json={
            "realm": "zai", "api_key": "JWT", "name": "z",
            "base_url": ZCODE_BASE,
        }, headers=headers).json()
        auth = ctx.pool.auth_for(ctx.pool.get(body["uid"]))
        assert auth.base_url == ZCODE_BASE
        assert auth.protocol == "anthropic"
        assert auth.file_path == ""      # 不再被误写
        from keyport.upstream.claim import is_zcode_plan_account

        assert is_zcode_plan_account(auth) is True


def test_apikey_account_infers_openai_protocol_for_non_anthropic(tmp_path: Path):
    """非 anthropic 端点按 OpenAI chat 协议出站（不误判为 Anthropic）。"""
    from keyport.auth.oauth import api_key_account

    auth = api_key_account("zai", "k", base_url="https://api.z.ai/api/coding/paas/v4")
    assert auth.protocol == "openai"
    assert auth.base_url == "https://api.z.ai/api/coding/paas/v4"
