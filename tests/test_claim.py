"""限时套餐领取（billing/preview + billing/claim）。

上游业务码语义、1005 名额退避、验证码一次性、激活上报时序（必须先于 preview）。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_test_config

from keyport.upstream.base import AccountAuth
from keyport.upstream.claim import (
    ClaimError,
    ClaimService,
    billing_block_reason,
    business_code,
    parse_plan,
)
from keyport.upstream.client import UpstreamClient

ZCODE_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"

PLAN_RAW = {
    "plan_id": "plan-abc",
    "name": "限时体验包",
    "description": "新用户专享",
    "priority": 10,
    "entitlements": [
        {"meter": "model_usage", "unit_type": "token", "show_name": "GLM-5.3",
         "grant_units": 3_000_000, "period": "daily"},
        {"meter": "model_usage", "unit_type": "token", "show_name": "GLM-5-Turbo",
         "grant_units": 2_000_000, "period": "daily"},
        # 非 token 权益必须被忽略（上游混有多种 meter）
        {"meter": "something_else", "unit_type": "count", "show_name": "忽略我"},
    ],
}


def _auth(**kw) -> AccountAuth:
    base = {
        "uid": "zai-1", "realm": "zai", "access_token": "PLAN_JWT",
        "kind": "apikey", "base_url": ZCODE_BASE, "protocol": "anthropic",
        "device_token": "dev-mid-1",
    }
    base.update(kw)
    return AccountAuth(**base)


def _service(handler, tmp_path: Path, *, captcha=None) -> ClaimService:
    cfg = make_test_config(tmp_path)
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    client.client = _mock  # type: ignore[method-assign]
    from keyport.db import init_db

    db = init_db(tmp_path / "claim.db")
    return ClaimService(cfg.claim, client, db, captcha=captcha)


class _Captcha:
    def __init__(self, region: str = "cn") -> None:
        from keyport.config import CaptchaConfig

        self.cfg = CaptchaConfig(region=region)
        self.issued: list[str] = []
        self.invalidated = 0

    async def acquire(self, jwt: str = "") -> str:
        param = f"param-{len(self.issued) + 1}"
        self.issued.append(param)
        return param

    def invalidate(self) -> None:
        self.invalidated += 1


# ---- 解析 ----


def test_business_code_variants():
    assert business_code({"code": 0}) == 0
    assert business_code({"code": "3007"}) == 3007
    assert business_code({}) == -1
    assert business_code(None) == -1
    assert business_code({"code": "abc"}) == -1


def test_parse_plan_extracts_token_grants_only():
    plan = parse_plan(PLAN_RAW)
    assert plan["plan_id"] == "plan-abc"
    assert plan["name"] == "限时体验包"
    assert plan["priority"] == 10
    names = [g["name"] for g in plan["grants"]]
    assert names == ["GLM-5.3", "GLM-5-Turbo"]
    assert plan["grants"][0]["units"] == 3_000_000
    assert plan["grants"][0]["period"] == "daily"


def test_parse_plan_requires_plan_id():
    assert parse_plan({"name": "无 id"}) is None
    assert parse_plan(None) is None
    assert parse_plan("not-a-dict") is None


def test_parse_plan_accepts_camel_case():
    plan = parse_plan({"planId": "p1", "entitlements": []})
    assert plan["plan_id"] == "p1"


# ---- billing 门禁 ----


def test_billing_block_reason_allows_coding_plan():
    assert billing_block_reason(_auth()) is None


def test_billing_block_reason_rejects_non_zcode_platform():
    """非 ZCode 平台账号不能打 billing（会 404 且白耗上游流量）。"""
    reason = billing_block_reason(_auth(base_url="https://api.z.ai/api/anthropic"))
    assert reason and "非 ZCode 平台" in reason


def test_billing_block_reason_rejects_workbuddy_account():
    """WorkBuddy / CodeBuddy 账号同样被挡（它们没有 Coding Plan 权益）。"""
    reason = billing_block_reason(_auth(realm="cn", base_url=""))
    assert reason and "非 ZCode 平台" in reason


def test_is_zcode_plan_account_detection():
    """判据是 base_url 落在 Coding Plan 网关下。"""
    from keyport.upstream.claim import is_zcode_plan_account

    assert is_zcode_plan_account(_auth()) is True
    assert is_zcode_plan_account(_auth(base_url="https://api.z.ai/api/anthropic")) is False
    assert is_zcode_plan_account(_auth(base_url="")) is False
    assert is_zcode_plan_account(
        _auth(realm="global", base_url="https://www.workbuddy.ai")) is False


def test_billing_block_reason_rejects_missing_credential():
    reason = billing_block_reason(_auth(access_token=""))
    assert reason and "无凭证" in reason


# ---- preview ----


async def test_preview_sorts_by_priority(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/billing/preview")
        return httpx.Response(200, json={"code": 0, "data": {"plans": [
            {**PLAN_RAW, "plan_id": "low", "priority": 1},
            {**PLAN_RAW, "plan_id": "high", "priority": 99},
        ]}})

    svc = _service(handler, tmp_path)
    plans = await svc.preview_plans(_auth())
    assert [p["plan_id"] for p in plans] == ["high", "low"]


async def test_preview_sends_platform_param(tmp_path: Path):
    """platform 跟账号档案走（上游 preview 接受该参数）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["query"] = dict(request.url.params)
        return httpx.Response(200, json={"code": 0, "data": {"plans": []}})

    svc = _service(handler, tmp_path)
    await svc.preview_plans(_auth())
    assert "platform" in seen["query"]
    assert "-" in seen["query"]["platform"]      # darwin-arm64 / win32-x64 形态
    assert seen["query"]["app_version"]


async def test_preview_uses_desktop_billing_headers(tmp_path: Path):
    """billing 族必须用桌面端头组（缺版本/平台头会被上游拒）。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["headers"] = {k.lower(): v for k, v in request.headers.items()}
        return httpx.Response(200, json={"code": 0, "data": {"plans": []}})

    svc = _service(handler, tmp_path)
    await svc.preview_plans(_auth())
    assert seen["headers"]["x-zcode-app-version"]
    assert seen["headers"]["x-platform"]
    assert seen["headers"]["x-title"] == "Z Code@electron"
    assert seen["headers"]["x-device-mid"] == "dev-mid-1"


async def test_preview_raises_on_business_error(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 1002, "msg": "活动已结束"})

    svc = _service(handler, tmp_path)
    with pytest.raises(ClaimError) as ei:
        await svc.preview_plans(_auth())
    assert ei.value.code == 1002
    assert "活动已结束" in str(ei.value)


# ---- 领取 ----


async def test_claim_success_returns_window_and_server_time(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {
            "server_time": 1_700_000_000,
            "plan": {"starts_at": 1_700_000_000, "ends_at": 1_700_086_400},
        }})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    result = await svc.claim(_auth(), "plan-abc")
    assert result["plan_id"] == "plan-abc"
    # 上游秒值 → 毫秒
    assert result["starts_at"] == 1_700_000_000_000
    assert result["ends_at"] == 1_700_086_400_000
    assert result["server_time"] == 1_700_000_000_000


async def test_claim_1005_carries_next_at(tmp_path: Path):
    """1005 名额用完必须带名额恢复时间（供轮次退避）。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "code": 1005, "msg": "今日名额已用完",
            "data": {"plan": {"ends_at": 1_700_100_000}},
        })

    svc = _service(handler, tmp_path, captcha=_Captcha())
    with pytest.raises(ClaimError) as ei:
        await svc.claim(_auth(), "plan-abc")
    assert ei.value.code == 1005
    assert ei.value.next_at == 1_700_100_000_000


async def test_claim_3007_invalidates_captcha(tmp_path: Path):
    """验证码被拒必须把该参数清出池，避免下轮复用同一个坏码。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 3007, "msg": "验证码校验失败"})

    captcha = _Captcha()
    svc = _service(handler, tmp_path, captcha=captcha)
    with pytest.raises(ClaimError) as ei:
        await svc.claim(_auth(), "plan-abc")
    assert ei.value.code == 3007
    assert captcha.invalidated == 1


async def test_claim_uses_fresh_captcha_param(tmp_path: Path):
    """验证码一次性：每次领取都取新参数。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("x-aliyun-captcha-verify-param") or "")
        return httpx.Response(200, json={"code": 0, "data": {}})

    captcha = _Captcha()
    svc = _service(handler, tmp_path, captcha=captcha)
    await svc.claim(_auth(), "p1")
    await svc.claim(_auth(), "p2")
    assert seen == ["param-1", "param-2"]


async def test_manual_claim_forwards_browser_verify_param(tmp_path: Path):
    """手动领取：浏览器滑块产生的 verifyParam 只做转发，不由本端求解。"""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["param"] = request.headers.get("x-aliyun-captcha-verify-param")
        seen["region"] = request.headers.get("x-aliyun-captcha-verify-region")
        return httpx.Response(200, json={"code": 0, "data": {}})

    captcha = _Captcha()
    svc = _service(handler, tmp_path, captcha=captcha)
    await svc.claim_with_captcha(_auth(), "browser-param", "cn", "p1")
    assert seen["param"] == "browser-param"
    assert seen["region"] == "cn"
    # 手动路径不该消耗农场参数
    assert captcha.issued == []


async def test_manual_claim_requires_verify_param(tmp_path: Path):
    svc = _service(lambda r: httpx.Response(200, json={"code": 0}), tmp_path)
    with pytest.raises(ClaimError) as ei:
        await svc.claim_with_captcha(_auth(), "", "", "p1")
    assert "验证码参数" in str(ei.value)


async def test_claim_auto_picks_highest_priority(tmp_path: Path):
    """plan_id 缺省时先 preview 自动选优先级最高套餐。"""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/billing/preview"):
            return httpx.Response(200, json={"code": 0, "data": {"plans": [
                {**PLAN_RAW, "plan_id": "low", "priority": 1},
                {**PLAN_RAW, "plan_id": "best", "priority": 99},
            ]}})
        import json as _json

        seen.append(_json.loads(request.content)["plan_id"])
        return httpx.Response(200, json={"code": 0, "data": {}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    result = await svc.claim(_auth())
    assert seen == ["best"]
    assert result["plan_id"] == "best"


async def test_claim_raises_when_no_plans(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"code": 0, "data": {"plans": []}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    with pytest.raises(ClaimError) as ei:
        await svc.claim(_auth())
    assert "没有待领取" in str(ei.value)


# ---- 激活上报时序 ----


def _jwt(user_id: str = "user-1") -> str:
    """构造三段点分 JWT（payload 带 user_id）；签名段不校验。"""
    import base64
    import json

    def seg(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    return f"{seg({'alg': 'none'})}.{seg({'user_id': user_id, 'sub': user_id})}.sig"


async def test_activation_report_precedes_preview(tmp_path: Path):
    """激活上报必须先于 preview：反序会形成「preview 恒空 → 永不激活」死锁。"""
    order: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "event/report" in str(request.url):
            order.append("report")
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/billing/preview"):
            order.append("preview")
            return httpx.Response(200, json={"code": 0, "data": {"plans": []}})
        order.append("other")
        return httpx.Response(200, json={"code": 0, "data": {}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    auth = _auth(access_token=_jwt())
    await svc.auto_claim_all(auth)
    assert order[0] == "report"
    assert "preview" in order


async def test_activation_report_deduped_per_day(tmp_path: Path):
    """自动路径每号每天最多成功上报一次（官方 app_launch 节奏）。"""
    reports: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "event/report" in str(request.url):
            reports.append(str(request.url))
            return httpx.Response(200, json={"code": 0})
        if request.url.path.endswith("/billing/preview"):
            return httpx.Response(200, json={"code": 0, "data": {"plans": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    auth = _auth(access_token=_jwt())
    await svc.auto_claim_all(auth)
    await svc.auto_claim_all(auth)
    await svc.auto_claim_all(auth)
    # 两个元素（app_launch + app_daily_active）只上报一次
    assert len(reports) == 2


async def test_activation_report_carries_device_and_user(tmp_path: Path):
    """事件体必须带账号设备档案与 user_id（上游按 device_mid + 日期去重日活）。"""
    import json as _json

    bodies: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "event/report" in str(request.url):
            bodies.append(_json.loads(request.content))
            return httpx.Response(200, json={"code": 0})
        return httpx.Response(200, json={"code": 0, "data": {"plans": []}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    await svc.auto_claim_all(_auth(access_token=_jwt("uid-9")))
    assert {b["element_name"] for b in bodies} == {"app_launch", "app_daily_active"}
    for b in bodies:
        assert b["user_id"] == "uid-9"
        assert b["device_mid"]
        assert b["screen_resolution"]
        assert b["app_version"]
        # 官方 sendReport 字段集固定 16 个
        assert len(b) == 16


async def test_activation_report_skipped_when_no_jwt_user_id(tmp_path: Path):
    """非 JWT 凭证（无 user_id）跳过上报，但 preview 照常执行。"""
    previews: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/billing/preview"):
            previews.append(1)
            return httpx.Response(200, json={"code": 0, "data": {"plans": []}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    auth = _auth(access_token="PLAIN_API_KEY")
    await svc.auto_claim_all(auth)
    assert previews == [1]


# ---- 1005 退避（轮次） ----


async def test_auto_claim_skips_held_plans(tmp_path: Path):
    """1005 等待期该套餐不打 claim（省验证码与上游写流量），preview 仍执行。"""
    claimed: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/billing/preview"):
            return httpx.Response(200, json={"code": 0, "data": {"plans": [
                {**PLAN_RAW, "plan_id": "held", "priority": 99},
                {**PLAN_RAW, "plan_id": "free", "priority": 1},
            ]}})
        import json as _json

        claimed.append(_json.loads(request.content)["plan_id"])
        return httpx.Response(200, json={"code": 0, "data": {}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    outcomes = await svc.auto_claim_all(_auth(), skip_plan_ids={"held"})
    assert claimed == ["free"]
    assert any(o.get("ok") for o in outcomes)


async def test_auto_claim_records_1005_next_at(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/billing/preview"):
            return httpx.Response(200, json={"code": 0, "data": {"plans": [
                {**PLAN_RAW, "plan_id": "p1", "priority": 1}]}})
        return httpx.Response(200, json={
            "code": 1005, "data": {"plan": {"ends_at": 1_700_100_000}}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    outcomes = await svc.auto_claim_all(_auth())
    failed = [o for o in outcomes if not o.get("ok")]
    assert failed and failed[0]["code"] == 1005
    assert failed[0]["next_at"] == 1_700_100_000_000


async def test_auto_claim_never_raises_on_upstream_error(tmp_path: Path):
    """入池链路 fire-and-forget：上游炸了也只能返回 outcome，绝不抛出。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, content=b'{"msg":"boom"}',
                              headers={"content-type": "application/json"})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    outcomes = await svc.auto_claim_all(_auth())
    assert outcomes and all(not o.get("ok") for o in outcomes)


# ---- 历史与状态 ----


async def test_history_records_outcomes(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/billing/preview"):
            return httpx.Response(200, json={"code": 0, "data": {"plans": [
                {**PLAN_RAW, "plan_id": "p1", "priority": 1}]}})
        return httpx.Response(200, json={"code": 0, "data": {}})

    svc = _service(handler, tmp_path, captcha=_Captcha())
    auth = _auth()
    outcomes = await svc.auto_claim_all(auth)

    class _Entry:
        uid = "zai-1"
        nickname = "acc1"

    svc._record(_Entry(), outcomes)
    rows = svc.history()
    assert rows and rows[0]["plan_id"] == "p1"
    assert rows[0]["outcome"] == "claimed"


def test_status_reports_config(tmp_path: Path):
    svc = _service(lambda r: httpx.Response(200, json={}), tmp_path)
    status = svc.status()
    assert "enabled" in status
    assert "round_interval_seconds" in status
    assert status["holds"] == {}
