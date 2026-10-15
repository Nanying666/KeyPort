"""growth 域任务原语与连登管家。

移植自 sheng-du0521/workbuddy2api 的 `internal/upstream/growth_tasks.go`、
`growth_reward.go`、`growth_bonus.go`。
重点验证三条实测教训：**领奖端点是 Web 域路径式**、**accept 必须回读 status**、
**`prerequisite not met` 是正常前置**。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from conftest import make_test_config

from keyport.upstream import growth as G
from keyport.upstream.base import AccountAuth, ErrorKind, UpstreamError
from keyport.upstream.client import UpstreamClient

NOW = 1_792_065_600.0   # 2026-10-15 12:00 UTC = 20:00 CST


def _auth() -> AccountAuth:
    return AccountAuth(uid="u1", realm="cn", access_token="T")


class _Recorder:
    """记录出站请求，并按注册的 handler 返回响应。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.routes: dict[str, httpx.Response] = {}
        self.default = httpx.Response(200, json={"code": 0, "data": {}})

    def on(self, path: str, *, status: int = 200, body: object = None) -> None:
        self.routes[path] = httpx.Response(
            status, json=body if body is not None else {"code": 0, "data": {}})

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            try:
                body = json.loads(request.content)
            except ValueError:
                body = request.content
        self.calls.append((request.method, request.url.path, body))
        return self.routes.get(request.url.path, self.default)


def _client(tmp_path: Path, rec: _Recorder) -> UpstreamClient:
    cfg = make_test_config(tmp_path)
    client = UpstreamClient(cfg)

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(rec.handler))

    client.client = _mock  # type: ignore[method-assign]
    return client


def _paths(rec: _Recorder) -> list[str]:
    return [c[1] for c in rec.calls]


# ---- 端点口径（最容易写错的部分）----


@pytest.mark.asyncio
async def test_claim_uses_path_style_without_v2(tmp_path: Path):
    """领奖是 **Web 域路径式**：`/activity/growth/tasks/<code>/claim`，无 /v2、无 body。

    `/v2/activity/growth/tasks/reward/claim` 是 404 错端点 —— KeyPort 原先走的
    `/v2/report/activity/reward` 属同类错误。
    """
    rec = _Recorder()
    c = _client(tmp_path, rec)
    await c.claim_growth_task(_auth(), "daily_login")

    method, path, body = rec.calls[0]
    assert method == "POST"
    assert path == "/activity/growth/tasks/daily_login/claim"
    assert not path.startswith("/v2")
    assert body is None


@pytest.mark.asyncio
async def test_accept_uses_v2_and_body(tmp_path: Path):
    rec = _Recorder()
    rec.on("/v2/activity/growth/tasks/accept", body={
        "code": 0, "data": {"results": [{"status": "accepted", "message": "OK"}]}})
    c = _client(tmp_path, rec)
    result = await c.accept_growth_task(_auth(), "t1")

    method, path, body = rec.calls[0]
    assert (method, path) == ("POST", "/v2/activity/growth/tasks/accept")
    assert body == {"task_codes": ["t1"]}
    assert result.ok is True


@pytest.mark.asyncio
async def test_tasks_list_is_get_on_v2(tmp_path: Path):
    rec = _Recorder()
    rec.on("/v2/activity/growth/tasks", body={"code": 0, "data": {"tasks": []}})
    c = _client(tmp_path, rec)
    await c.growth_tasks(_auth())
    assert rec.calls[0][:2] == ("GET", "/v2/activity/growth/tasks")


@pytest.mark.asyncio
async def test_streak_redeem_lottery_paths(tmp_path: Path):
    rec = _Recorder()
    rec.on("/activity/growth/streak", body={"code": 0, "data": {"streak": {"days": 8}}})
    rec.on("/activity/growth/redeem", body={"code": 0, "data": {}})
    rec.on("/activity/growth/lottery/chances", body={"code": 0, "data": {"balance": 2}})
    rec.on("/activity/growth/lottery/draw", body={"code": 0, "data": {}})
    rec.on("/activity/growth/heatmap", body={"code": 0, "data": {"cells": []}})
    rec.on("/activity/growth/makeup-cards/use", body={"code": 0, "data": {}})
    c = _client(tmp_path, rec)

    await c.growth_reward_state(_auth())
    await c.growth_redeem(_auth(), "7d")
    assert await c.growth_lottery_chances(_auth()) == 2
    await c.growth_lottery_draw(_auth())
    await c.growth_heatmap(_auth())
    await c.use_makeup_card(_auth(), "2026-10-14")

    assert _paths(rec) == [
        "/activity/growth/streak",
        "/activity/growth/redeem",
        "/activity/growth/lottery/chances",
        "/activity/growth/lottery/draw",
        "/activity/growth/heatmap",
        "/activity/growth/makeup-cards/use",
    ]


# ---- accept 回读 status ----


@pytest.mark.asyncio
async def test_accept_http_200_but_not_accepted_is_detected(tmp_path: Path):
    """实测存在「HTTP 200 + msg=OK 但 status 非 accepted」—— 此时**上报不归账**。

    只看 HTTP 码会把这种静默失败当成功，任务永远点不亮。
    """
    rec = _Recorder()
    rec.on("/v2/activity/growth/tasks/accept", body={
        "code": 0, "data": {"results": [{"status": "error", "message": "internal"}]}})
    c = _client(tmp_path, rec)
    result = await c.accept_growth_task(_auth(), "t1")
    assert result.ok is False
    assert result.status == "error"


@pytest.mark.asyncio
async def test_accept_preserves_message(tmp_path: Path):
    """`message` 必须保留：`prerequisite not met` 是**正常前置**，不是故障。

    丢掉 message 就只能看到一句无信息量的「状态非 accepted」。
    """
    rec = _Recorder()
    rec.on("/v2/activity/growth/tasks/accept", body={
        "code": 0,
        "data": {"results": [{"status": "error",
                              "message": "prerequisite not met: first_buddy"}]}})
    c = _client(tmp_path, rec)
    result = await c.accept_growth_task(_auth(), "black_cat")
    assert "prerequisite not met: first_buddy" in result.message


@pytest.mark.parametrize("status", ["accepted", "completed", "claimed"])
def test_accepted_covers_three_states(status):
    assert G.GrowthTask(task_code="t", accept_status=status).accepted is True


def test_accepted_false_for_other_states():
    for status in ("", "pending", "error", "locked"):
        assert G.GrowthTask(task_code="t", accept_status=status).accepted is False


# ---- 任务模型 ----


def test_task_from_payload():
    task = G.GrowthTask.from_payload({
        "task_code": "t1", "accept_status": "accepted",
        "progress": {"current": 3, "target": 5},
    })
    assert (task.task_code, task.current, task.target) == ("t1", 3, 5)
    assert task.complete is False


def test_task_progress_null_is_tolerated():
    """未 accept 时 progress 为 null（实测）。"""
    task = G.GrowthTask.from_payload({"task_code": "t1", "accept_status": ""})
    assert (task.current, task.target) == (0, 0)
    assert task.complete is False


def test_complete_requires_positive_target():
    """target 为 0 时不判达标（避免假阳性：0>=0 会让一切「达标」）。"""
    assert G.GrowthTask(target=0, current=0).complete is False
    assert G.GrowthTask(target=3, current=3).complete is True
    assert G.GrowthTask(target=3, current=2).complete is False


# ---- 兑换状态 ----


def test_redemption_status_accessors():
    st = G.GrowthRedemptionStatus(tier_7d_status="available",
                                  tier_14d_status="claimed",
                                  tier_28d_status="locked")
    assert st.claimed("14d") is True
    assert st.claimed("7d") is False
    assert st.available("7d") is True
    assert st.status_of("28d") == "locked"
    assert st.status_of("bogus") == ""


@pytest.mark.asyncio
async def test_reward_state_parses_all_sections(tmp_path: Path):
    """一次 GET 同时读出连登天数、三档状态、档位明细、补签卡余额。"""
    rec = _Recorder()
    rec.on("/activity/growth/streak", body={"code": 0, "data": {
        "streak": {"days": 15},
        "redemption_status": {
            "tier_7d_status": "claimed", "tier_14d_status": "available",
            "tier_28d_status": "locked", "remaining_days": 13,
            "tiers": [{"tier": "7d", "days": 7, "credit": 100, "chances": 1}],
        },
        "makeup_cards": {"balance": 2, "max": 5},
    }})
    c = _client(tmp_path, rec)
    state = await c.growth_reward_state(_auth())

    assert state.days == 15
    assert state.redemption.claimed("7d") is True
    assert state.redemption.available("14d") is True
    assert state.redemption.remaining_days == 13
    assert state.redemption.tiers[0].credit == 100
    assert (state.makeup_cards.balance, state.makeup_cards.max) == (2, 5)


@pytest.mark.asyncio
async def test_reward_state_missing_sections_is_zero(tmp_path: Path):
    rec = _Recorder()
    rec.on("/activity/growth/streak", body={"code": 0, "data": {}})
    c = _client(tmp_path, rec)
    state = await c.growth_reward_state(_auth())
    assert state.days == 0
    assert state.redemption.tiers == []
    assert state.makeup_cards.balance == 0


# ---- client_token（幂等键）----


def test_client_token_format_and_uniqueness():
    """每次必须新键 —— 复用可能被上游按幂等键去重吞掉本次领取。"""
    a = G.growth_client_token("redeem-7d")
    b = G.growth_client_token("redeem-7d")
    assert a.startswith("redeem-7d-")
    assert a != b
    assert len(a.split("-")[-1]) == 32


@pytest.mark.asyncio
async def test_redeem_sends_fresh_token(tmp_path: Path):
    rec = _Recorder()
    rec.on("/activity/growth/redeem", body={"code": 0, "data": {}})
    c = _client(tmp_path, rec)
    await c.growth_redeem(_auth(), "7d")
    await c.growth_redeem(_auth(), "7d")
    t1 = rec.calls[0][2]["client_token"]
    t2 = rec.calls[1][2]["client_token"]
    assert t1 != t2
    assert rec.calls[0][2]["tier"] == "7d"


@pytest.mark.asyncio
async def test_redeem_result_fields(tmp_path: Path):
    rec = _Recorder()
    rec.on("/activity/growth/redeem", body={"code": 0, "data": {
        "credit_granted": 100, "energy_granted": 5, "chances_granted": 2,
        "cards_granted": 1, "cards_overflow": 0}})
    c = _client(tmp_path, rec)
    got = await c.growth_redeem(_auth(), "14d")
    assert (got.credit_granted, got.chances_granted, got.cards_granted) == (100, 2, 1)


# ---- 正常态错误识别 ----


def _err(status: int, message: str) -> UpstreamError:
    return UpstreamError(ErrorKind.UNKNOWN, status, message)


def test_idempotent_and_normal_states():
    """这些全是**正常态**，不该重试、不该刷 WARN。"""
    assert G.is_redeem_already_claimed(_err(409, "duplicate: already claimed")) is True
    assert G.is_redeem_already_claimed(_err(409, "本月已领取")) is True
    assert G.is_redeem_not_enough_days(_err(403, "连续登录天数不足")) is True
    assert G.is_lottery_no_chance(_err(400, "insufficient lottery chance balance")) is True
    assert G.is_lottery_disabled(_err(400, "lottery disabled")) is True
    assert G.is_prerequisite_not_met(_err(400, "prerequisite not met")) is True
    assert G.is_growth_unavailable(
        _err(403, "growth system is only available for personal users")) is True


def test_markers_require_matching_status():
    """状态码不匹配不算正常态（防把真故障吞掉）。"""
    assert G.is_redeem_already_claimed(_err(500, "duplicate")) is False
    assert G.is_lottery_no_chance(_err(429, "insufficient")) is False


def test_network_error_is_not_normal_state():
    assert G.is_lottery_no_chance(UpstreamError(ErrorKind.NETWORK, 0, "boom")) is False
    assert G.is_redeem_already_claimed(ValueError("x")) is False


# ---- 业务码信封 ----


@pytest.mark.asyncio
async def test_business_code_idempotent_is_success(tmp_path: Path):
    """业务码非 0 但文案是幂等态 → 视为成功（已领/已签/重复）。"""
    rec = _Recorder()
    rec.on("/activity/growth/redeem",
           body={"code": 409, "msg": "本月已领取", "data": {}})
    c = _client(tmp_path, rec)
    got = await c.growth_redeem(_auth(), "7d")
    assert got.credit_granted == 0     # 幂等：无新到账，但不报错


@pytest.mark.asyncio
async def test_business_code_error_raises(tmp_path: Path):
    rec = _Recorder()
    rec.on("/activity/growth/redeem",
           body={"code": 500, "msg": "internal error"})
    c = _client(tmp_path, rec)
    with pytest.raises(UpstreamError):
        await c.growth_redeem(_auth(), "7d")


@pytest.mark.asyncio
async def test_http_error_carries_status(tmp_path: Path):
    rec = _Recorder()
    rec.on("/activity/growth/lottery/draw", status=400,
           body={"code": 400, "msg": "insufficient lottery chance balance"})
    c = _client(tmp_path, rec)
    with pytest.raises(UpstreamError) as exc:
        await c.growth_lottery_draw(_auth())
    assert G.is_lottery_no_chance(exc.value) is True


# ---- 日期口径 ----


def test_cst_day_and_yesterday():
    """补签判据固定盯**昨日**（连登断档只可能发生在上一个自然日）。"""
    assert G.cst_day(NOW) == "2026-10-15"
    assert G.growth_yesterday_date(NOW) == "2026-10-14"
    # CST 20:00 减一日仍在 10-14
    assert G.growth_yesterday_date(NOW + 4 * 3600) == "2026-10-15"


def test_heatmap_day_score():
    cells = [G.HeatmapCell(date="2026-10-14T00:00:00Z", score=0),
             G.HeatmapCell(date="2026-10-13", score=7)]
    assert G.heatmap_day_score(cells, "2026-10-14") == (0, True)
    assert G.heatmap_day_score(cells, "2026-10-13") == (7, True)
    # 无该日格 → 无判据，调用方不动
    assert G.heatmap_day_score(cells, "2026-10-01") == (0, False)


# ---- 补签 / 礼包 ----


@pytest.mark.asyncio
async def test_makeup_card_body(tmp_path: Path):
    rec = _Recorder()
    rec.on("/activity/growth/makeup-cards/use", body={"code": 0, "data": {}})
    c = _client(tmp_path, rec)
    await c.use_makeup_card(_auth(), "2026-10-14")
    assert rec.calls[0][2] == {"target_date": "2026-10-14"}


@pytest.mark.asyncio
async def test_claim_gift_and_compensation_return_credit(tmp_path: Path):
    rec = _Recorder()
    rec.on("/billing/meter/claim-gift", body={"code": 0, "data": {"credit": 500}})
    rec.on("/billing/meter/claim-compensation", body={"code": 0, "data": {"credit": 200}})
    c = _client(tmp_path, rec)
    assert await c.claim_gift(_auth()) == 500
    assert await c.claim_compensation(_auth()) == 200
    assert _paths(rec) == ["/billing/meter/claim-gift",
                           "/billing/meter/claim-compensation"]
