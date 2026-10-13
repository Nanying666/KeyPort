"""官方首启安装序仿真与激活遥测。

官方桌面端首启序：`client/configs`（免鉴权，只带 app_version）→
`event/report` 激活遥测（无 Authorization）。安装先于登录，未登录设备同样
上报 app_launch（user_id 空串）。
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
from conftest import make_test_config

from keyport.upstream.base import AccountAuth, UpstreamError
from keyport.upstream.client import UpstreamClient
from keyport.upstream.install import (
    fetch_client_configs,
    is_jwt,
    jwt_user_id,
    run_install_sequence_for_account,
)
from keyport.upstream.telemetry import (
    build_activation_event_body,
    business_code,
    post_activation_event,
)

ZCODE_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"


def _jwt(payload: dict) -> str:
    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg(payload)}.sig"


def _auth(**kw) -> AccountAuth:
    base = {
        "uid": "zai-1", "realm": "zai", "access_token": _jwt({"user_id": "u-1"}),
        "kind": "apikey", "base_url": ZCODE_BASE, "protocol": "anthropic",
        "device_token": "dev-mid-1",
    }
    base.update(kw)
    return AccountAuth(**base)


def _client(handler, tmp_path: Path) -> UpstreamClient:
    cfg = make_test_config(tmp_path)
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock  # type: ignore[method-assign]
    return client


# ---- JWT 解析 ----


def test_jwt_user_id_prefers_user_id():
    assert jwt_user_id(_jwt({"user_id": "abc"})) == "abc"


def test_jwt_user_id_falls_back_to_sub():
    assert jwt_user_id(_jwt({"sub": "xyz"})) == "xyz"


def test_jwt_user_id_rejects_malformed():
    assert jwt_user_id("") == ""
    assert jwt_user_id("not-a-jwt") == ""
    assert jwt_user_id("a.b") == ""
    assert jwt_user_id("a.!!!not-base64!!!.c") == ""


def test_is_jwt_requires_three_segments():
    assert is_jwt("a.b.c") is True
    assert is_jwt("a.b") is False
    assert is_jwt("plain-api-key") is False
    assert is_jwt(None) is False


# ---- 事件体 ----


def test_activation_event_body_has_official_field_set():
    """字段集必须与官方 sendReport 一致（固定 16 个）。"""
    from keyport.upstream.fingerprint import random_profile

    body = build_activation_event_body("app_launch", random_profile(), "u-1")
    assert set(body) == {
        "event_id", "client_timezone", "client_language", "element_name",
        "event_region", "event_type", "event_text", "event_extra_detail",
        "user_id", "screen_resolution", "app_version", "device_os_category",
        "device_os_version", "device_mid", "mac_id", "marketing_params",
    }
    assert body["event_region"] == "app"
    assert body["event_type"] == "view"
    assert body["mac_id"] == ""
    assert body["marketing_params"] == "{}"


def test_activation_event_body_uses_profile_identity():
    """事件体身份字段取自账号设备档案（一号一台设备）。"""
    from keyport.upstream.fingerprint import random_profile

    profile = random_profile()
    body = build_activation_event_body("app_daily_active", profile, "u-9")
    assert body["device_mid"] == profile.device_mid
    assert body["screen_resolution"] == profile.screen
    assert body["device_os_category"] == profile.os_category
    assert body["device_os_version"] == profile.os_version
    assert body["client_language"] == profile.language
    assert body["client_timezone"] == profile.timezone


def test_business_code_variants():
    assert business_code({"code": 0}) == 0
    assert business_code({"code": "0"}) == 0
    assert business_code({}) == -1
    assert business_code(None) == -1
    assert business_code("nope") == -1


# ---- 上报 ----


async def test_post_activation_event_sends_no_authorization(tmp_path: Path):
    """官方事件端点不校验登录态，且官方客户端不带 Authorization。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"code": 0})

    from keyport.upstream.fingerprint import random_profile

    client = _client(handler, tmp_path)
    await post_activation_event(client, random_profile(), "", "app_launch")
    assert "authorization" not in seen["headers"]
    assert seen["url"].endswith("/api/v1/event/report")
    assert seen["body"]["user_id"] == ""


async def test_post_activation_event_raises_on_http_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    from keyport.upstream.fingerprint import random_profile

    client = _client(handler, tmp_path)
    with pytest.raises(UpstreamError):
        await post_activation_event(client, random_profile(), "u", "app_launch")


async def test_post_activation_event_raises_on_business_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 3001, "msg": "parameter error"})

    from keyport.upstream.fingerprint import random_profile

    client = _client(handler, tmp_path)
    with pytest.raises(UpstreamError) as ei:
        await post_activation_event(client, random_profile(), "u", "app_launch")
    assert "3001" in ei.value.message


# ---- client/configs ----


async def test_fetch_client_configs_sends_only_app_version(tmp_path: Path):
    """实测带 platform 参数会被拒（3001），故只带 app_version。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"code": 0, "data": {"configs": {}}})

    client = _client(handler, tmp_path)
    await fetch_client_configs(client)
    assert set(seen["query"]) == {"app_version"}


async def test_fetch_client_configs_raises_on_business_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 3001, "msg": "parameter error"})

    client = _client(handler, tmp_path)
    with pytest.raises(UpstreamError):
        await fetch_client_configs(client)


# ---- 安装序 ----


async def test_install_sequence_runs_configs_then_events(tmp_path: Path):
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "client/configs" in str(request.url):
            order.append("configs")
            return httpx.Response(200, json={"code": 0, "data": {"configs": {
                "captcha": {"enabled": True}}}})
        order.append("event")
        return httpx.Response(200, json={"code": 0})

    client = _client(handler, tmp_path)
    result = await run_install_sequence_for_account(client, _auth())
    assert order[0] == "configs"
    assert result["configs_fetched"] is True
    assert result["captcha_enabled"] is True
    assert result["events_reported"] == ["app_launch", "app_daily_active"]
    assert result["installed"] is True
    assert result["errors"] == []


async def test_install_sequence_is_idempotent(tmp_path: Path):
    """installed_at 非空直接跳过（零上游请求）。"""
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = _client(handler, tmp_path)
    result = await run_install_sequence_for_account(client, _auth(), installed_at=12345)
    assert result["skipped"] is True
    assert result["installed"] is False
    assert calls == []


async def test_install_sequence_never_raises(tmp_path: Path):
    """安装仿真不影响主服务：上游全炸也只留痕在 errors。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b"boom")

    client = _client(handler, tmp_path)
    result = await run_install_sequence_for_account(client, _auth())
    assert result["installed"] is False
    assert result["configs_fetched"] is False
    assert result["errors"]


async def test_install_sequence_uses_account_device_profile(tmp_path: Path):
    """安装序绑定账号自身设备档案（每账号独立 device_mid）。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "event/report" in str(request.url):
            bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {}})

    from keyport.upstream.fingerprint import profile_for

    auth = _auth()
    expected = profile_for(auth).device_mid
    client = _client(handler, tmp_path)
    await run_install_sequence_for_account(client, auth)
    assert bodies and all(b["device_mid"] == expected for b in bodies)


async def test_install_sequence_reports_user_id_when_logged_in(tmp_path: Path):
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "event/report" in str(request.url):
            bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = _client(handler, tmp_path)
    await run_install_sequence_for_account(
        client, _auth(access_token=_jwt({"user_id": "logged-in"})))
    assert bodies and all(b["user_id"] == "logged-in" for b in bodies)


async def test_install_sequence_empty_user_id_for_apikey(tmp_path: Path):
    """apiKey 账号无 JWT → user_id 空串（官方未登录安装形态）。"""
    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "event/report" in str(request.url):
            bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"code": 0, "data": {}})

    client = _client(handler, tmp_path)
    await run_install_sequence_for_account(
        client, _auth(access_token="PLAIN_KEY"))
    assert bodies and all(b["user_id"] == "" for b in bodies)
