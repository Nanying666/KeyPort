"""设备档案（桌面 SKU）只分配给 ZCode 平台账号。

## 为什么这样分

两套上游身份体系完全不同：

| 体系 | 身份机制 | 出站头 |
|---|---|---|
| WorkBuddy / CodeBuddy | 按 uid 稳定派生 | `X-Machine-ID` / `X-Session-ID`（36 hex） |
| ZCode Coding Plan | 成套桌面 SKU | `x-platform` / `x-os-version` / `x-os-category` / `x-client-*` / `x-device-mid` |

WorkBuddy / CodeBuddy 的请求里**根本不带 SKU 字段**，所以给它们分配档案毫无作用，
只会让管理台显示出一份用不上的「设备档案」，误导运维以为它们在走桌面指纹。

管理台因此分两个列表（`zcode_platform` 标记由后端判定）：
- **WorkBuddy / CodeBuddy 池**：无设备档案列、无安装序 / 换设备操作
- **ZCode 池**：带设备档案列 + 安装序 + 换发设备
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_test_config
from fastapi.testclient import TestClient

from keyport.app import create_app
from keyport.config import Config
from keyport.db import init_db
from keyport.pool.manager import AccountPool
from keyport.security import hash_password
from keyport.upstream.base import AccountAuth

ZCODE_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
ZAI_KEY_BASE = "https://api.z.ai/api/anthropic"


def _pool(tmp_path: Path) -> AccountPool:
    cfg = Config(data_dir=str(tmp_path / "d"), auth_dir=str(tmp_path / "a"),
                 static_dir=str(tmp_path / "s"))
    cfg.source_path = tmp_path / "config.json"
    db = init_db(tmp_path / "pool.db")
    return AccountPool(cfg, db)


def _wb(uid: str, realm: str = "global") -> AccountAuth:
    return AccountAuth(uid=uid, realm=realm, access_token="T", kind="oauth")


def _zcode(uid: str) -> AccountAuth:
    return AccountAuth(uid=uid, realm="zai", access_token="JWT", kind="apikey",
                       base_url=ZCODE_BASE, protocol="anthropic")


# ---- 分配口径 ----


def test_workbuddy_global_gets_no_sku(tmp_path: Path):
    entry = _pool(tmp_path).upsert_account(_wb("wb-g", "global"))
    assert entry.fingerprint == {}


def test_codebuddy_cn_gets_no_sku(tmp_path: Path):
    entry = _pool(tmp_path).upsert_account(_wb("wb-c", "cn"))
    assert entry.fingerprint == {}


def test_zai_apikey_without_coding_plan_gets_no_sku(tmp_path: Path):
    """z.ai API Key 走 api.z.ai（openai 协议），不是 Coding Plan，不需要 SKU。"""
    entry = _pool(tmp_path).upsert_account(
        AccountAuth(uid="zai-k", realm="zai", access_token="K", kind="apikey"))
    assert entry.fingerprint == {}


def test_zcode_coding_plan_gets_sku(tmp_path: Path):
    entry = _pool(tmp_path).upsert_account(_zcode("zai-p"))
    fp = entry.fingerprint
    assert fp.get("device_mid")
    assert fp.get("platform") in ("darwin", "win32")
    assert fp.get("screen")
    # 成套 SKU 自洽（不是字段笛卡尔积）
    from keyport.upstream.fingerprint import from_dict, is_generated_sku

    assert is_generated_sku(from_dict(fp)) is True


def test_sku_is_stable_across_reupsert(tmp_path: Path):
    """同一 ZCode 账号重复入池不换设备（档案已存在就沿用）。"""
    pool = _pool(tmp_path)
    first = pool.upsert_account(_zcode("zai-p")).fingerprint
    second = pool.upsert_account(_zcode("zai-p")).fingerprint
    assert first["device_mid"] == second["device_mid"]


def test_legacy_sku_cleared_when_account_is_not_zcode(tmp_path: Path):
    """历史遗留档案会被清掉：账号端点改成非 Coding Plan 后旧 SKU 已无意义。"""
    pool = _pool(tmp_path)
    entry = pool.upsert_account(_zcode("zai-x"))
    assert entry.fingerprint  # 先是 ZCode，拿到档案

    # 同一 uid 改指 api.z.ai（不再是 Coding Plan）
    pool.upsert_account(AccountAuth(
        uid="zai-x", realm="zai", access_token="K", kind="apikey",
        base_url=ZAI_KEY_BASE, protocol="anthropic"))
    assert pool.get("zai-x").fingerprint == {}


def test_rotate_refuses_for_non_zcode(tmp_path: Path):
    """非 ZCode 账号不持有档案，换发返回空 dict（不假装成功）。"""
    pool = _pool(tmp_path)
    pool.upsert_account(_wb("wb-g"))
    assert pool.rotate_fingerprint("wb-g") == {}


def test_rotate_works_for_zcode(tmp_path: Path):
    pool = _pool(tmp_path)
    pool.upsert_account(_zcode("zai-p"))
    before = pool.get("zai-p").fingerprint["device_mid"]
    after = pool.rotate_fingerprint("zai-p")
    assert after.get("device_mid") and after["device_mid"] != before


def test_rotate_unknown_account_returns_none(tmp_path: Path):
    """不存在的账号返回 None（与「不适用」的空 dict 区分开）。"""
    assert _pool(tmp_path).rotate_fingerprint("nope") is None


def test_fingerprint_persists_across_reload(tmp_path: Path):
    """档案落库：重建池子后仍在（跨重启恒定）。"""
    pool = _pool(tmp_path)
    pool.upsert_account(_zcode("zai-p"))
    mid = pool.get("zai-p").fingerprint["device_mid"]
    pool.save_state()

    fresh = AccountPool(pool.cfg, pool.db)
    assert fresh.get("zai-p").fingerprint["device_mid"] == mid


# ---- 管理台 API：zcode_platform 标记与门控 ----


@pytest.fixture()
def client(tmp_path: Path) -> TestClient:
    cfg = make_test_config(tmp_path)
    cfg.security.admin_password_hash = hash_password("pw-12345678")
    cfg.install.auto_on_add = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx

    ctx.pool.upsert_account(_wb("wb-1", "global"))
    ctx.pool.upsert_account(_wb("wb-2", "cn"))
    ctx.pool.upsert_account(_zcode("zai-1"))

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json={"code": 0, "data": {}})))

    ctx.client.client = _mock  # type: ignore[method-assign]
    with TestClient(app) as tc:
        resp = tc.post("/api/login",
                       json={"username": "admin", "password": "pw-12345678"})
        assert resp.status_code == 200
        tc.headers.update({"x-kp-session": tc.cookies.get("kp_session", "")})
        yield tc


def _by_uid(client: TestClient, uid: str) -> dict:
    rows = client.get("/api/accounts").json()["accounts"]
    return next(r for r in rows if r["uid"] == uid)


def test_account_view_exposes_zcode_flag(client: TestClient):
    """前端靠这个标记分池，后端必须给准。"""
    assert _by_uid(client, "wb-1")["zcode_platform"] is False
    assert _by_uid(client, "wb-2")["zcode_platform"] is False
    assert _by_uid(client, "zai-1")["zcode_platform"] is True


def test_wb_accounts_report_empty_fingerprint(client: TestClient):
    for uid in ("wb-1", "wb-2"):
        acc = _by_uid(client, uid)
        assert acc["fingerprint"] == {}
        assert acc["installed_at"] == 0


def test_zcode_account_reports_fingerprint(client: TestClient):
    acc = _by_uid(client, "zai-1")
    assert acc["fingerprint"]["device_mid"]
    assert acc["fingerprint"]["platform"] in ("darwin", "win32")


def test_rotate_fingerprint_rejects_non_zcode(client: TestClient):
    """非 ZCode 换发 → 400 说明原因，而不是假装成功。"""
    resp = client.post("/api/accounts/wb-1/rotate-fingerprint")
    assert resp.status_code == 400
    assert "非 ZCode 平台账号" in resp.json()["detail"]


def test_rotate_fingerprint_ok_for_zcode(client: TestClient):
    before = _by_uid(client, "zai-1")["fingerprint"]["device_mid"]
    resp = client.post("/api/accounts/zai-1/rotate-fingerprint")
    assert resp.status_code == 200
    assert resp.json()["fingerprint"]["device_mid"] != before


def test_install_skipped_for_non_zcode(client: TestClient):
    """非 ZCode 补跑安装序 → skipped，且零上游请求。"""
    body = client.post("/api/accounts/wb-1/install").json()
    assert body["skipped"] is True
    assert body.get("reason")
    assert body["configs_fetched"] is False


def test_accounts_list_supports_two_pool_split(client: TestClient):
    """两个池的划分（前端据此渲染两个列表）。"""
    rows = client.get("/api/accounts").json()["accounts"]
    wb = [r for r in rows if not r["zcode_platform"]]
    zcode = [r for r in rows if r["zcode_platform"]]
    assert {r["uid"] for r in wb} == {"wb-1", "wb-2"}
    assert {r["uid"] for r in zcode} == {"zai-1"}
