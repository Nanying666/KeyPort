"""growth 域「任务列表 / 接受 / 领奖」与「连登兑换 / 抽奖 / 补签」原语。

## 端点口径（照抄参考实现实测，勿改）

    列表  : GET  {chatBase}/v2/activity/growth/tasks
    接受  : POST {chatBase}/v2/activity/growth/tasks/accept   body {"task_codes":[...]}
    领奖  : POST {chatBase}/activity/growth/tasks/{code}/claim   ← **无 /v2、无 body**

领奖路径不含 `/v2` 是**刻意的**：`/v2/activity/growth/tasks/reward/claim` 是 404 错端点。
KeyPort 原先走的 `/v2/report/activity/reward` 属同类错误 —— 端点对了才有归账可言。

连登奖励 / 抽奖 / 补签：

    GET  {chatBase}/activity/growth/streak            → streak.days + redemption_status + makeup_cards
    POST {chatBase}/activity/growth/redeem            → {"tier":"7d","client_token":...}
    GET  {chatBase}/activity/growth/lottery/chances   → data.balance
    POST {chatBase}/activity/growth/lottery/draw      → {"client_token":...}
    GET  {chatBase}/activity/growth/heatmap           → data.cells[]
    POST {chatBase}/activity/growth/makeup-cards/use  → {"target_date":"YYYY-MM-DD"}

本模块**只做单个任务的原语** —— 批量编排（时段判定、上限、回读确认）由 scheduler 负责，
保持上游客户端无业务状态。
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from .base import AccountAuth, ErrorKind, UpstreamError

# ---- 路径 ----

GROWTH_TASKS_PATH = "/v2/activity/growth/tasks"
GROWTH_ACCEPT_PATH = "/v2/activity/growth/tasks/accept"
GROWTH_CLAIM_PREFIX = "/activity/growth/tasks/"
GROWTH_CLAIM_SUFFIX = "/claim"

STREAK_PATH = "/activity/growth/streak"
REDEEM_PATH = "/activity/growth/redeem"
LOTTERY_CHANCES_PATH = "/activity/growth/lottery/chances"
LOTTERY_DRAW_PATH = "/activity/growth/lottery/draw"
HEATMAP_PATH = "/activity/growth/heatmap"
MAKEUP_CARD_USE_PATH = "/activity/growth/makeup-cards/use"
CLAIM_GIFT_PATH = "/billing/meter/claim-gift"
CLAIM_COMPENSATION_PATH = "/billing/meter/claim-compensation"

# ---- Buddy 旅行（原「猫猫旅行」改版后的链路）----
#
# 旧的 `POST /v2/billing/activity/travel` 已下线（恒 404）。旅行改版为
# 「Buddy 旅行」，四步状态机（照官方前端 / 参考实现实测口径）：
#
#     状态 : GET  {base}/activity/growth/buddy/travel/status
#     地点 : GET  {base}/v2/activity/growth/buddy/travel/config
#     出发 : POST {base}/v2/activity/growth/buddy/travel/depart   body {"location_id":<id>}
#     领奖 : POST {base}/activity/growth/buddy/travel/claim        ← **无 /v2、无 body**
#
# `state` 取 `idle`（未出发）/ `traveling`（在路上）/ `arrived`（可领奖）。
# 每个自然日只能**派出一次**；派出会立即置 `daily_limit_reached=true`。
# 一天的正确节奏：早上 `idle → depart`，后续排程点 `arrived → claim`。
BUDDY_TRAVEL_STATUS_PATH = "/activity/growth/buddy/travel/status"
BUDDY_TRAVEL_CONFIG_PATH = "/v2/activity/growth/buddy/travel/config"
BUDDY_TRAVEL_DEPART_PATH = "/v2/activity/growth/buddy/travel/depart"
BUDDY_TRAVEL_CLAIM_PATH = "/activity/growth/buddy/travel/claim"

# CST（Asia/Shanghai）：上游自然日口径，固定 +8，不依赖容器 tzdata
CST_OFFSET_SECONDS = 8 * 3600


# ---- 数据模型 ----


@dataclass
class GrowthTask:
    """单个 growth 任务的状态。

    `progress` 在未 accept 时为 null（实测），故 current/target 默认 0，由调用方兜底。
    """

    task_code: str = ""
    accept_status: str = ""
    current: int = 0
    target: int = 0

    @property
    def accepted(self) -> bool:
        """accept 是否已生效（三态都算已登记）。"""
        return self.accept_status in ("accepted", "completed", "claimed")

    @property
    def complete(self) -> bool:
        """进度是否已达标（target 为 0 时不判达标，避免假阳性）。"""
        return bool(self.target > 0 and self.current >= self.target)

    @classmethod
    def from_payload(cls, item: dict[str, Any]) -> GrowthTask:
        progress = item.get("progress") if isinstance(item.get("progress"), dict) else {}
        return cls(
            task_code=str(item.get("task_code") or ""),
            accept_status=str(item.get("accept_status") or ""),
            current=int(progress.get("current") or 0),
            target=int(progress.get("target") or 0),
        )


@dataclass
class GrowthAcceptResult:
    """accept 回执。`message` 必须保留 —— 见 `accept_growth_task` 的说明。"""

    status: str = ""
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("accepted", "completed", "claimed")


@dataclass
class GrowthTierSpec:
    """连登奖励档位（7d 入门 / 14d 进阶 / 28d 巅峰）。"""

    tier: str = ""
    days: int = 0
    credit: int = 0
    energy: int = 0
    cards: int = 0
    chances: int = 0


@dataclass
class GrowthRedemptionStatus:
    """连登奖励兑换状态。档位状态取值：available 可领 / claimed 已领 / locked 未达标。"""

    tier_7d_status: str = ""
    tier_14d_status: str = ""
    tier_28d_status: str = ""
    tiers: list[GrowthTierSpec] = field(default_factory=list)
    remaining_days: int = 0

    def claimed(self, tier: str) -> bool:
        """指定档位本月是否已领。"""
        mapping = {
            "7d": self.tier_7d_status,
            "14d": self.tier_14d_status,
            "28d": self.tier_28d_status,
        }
        return mapping.get(tier, "") == "claimed"

    def status_of(self, tier: str) -> str:
        mapping = {
            "7d": self.tier_7d_status,
            "14d": self.tier_14d_status,
            "28d": self.tier_28d_status,
        }
        return mapping.get(tier, "")

    def available(self, tier: str) -> bool:
        return self.status_of(tier) == "available"


@dataclass
class GrowthMakeupCards:
    """补签卡余额。"""

    balance: int = 0
    max: int = 0


@dataclass
class GrowthRewardState:
    """连登天数 + 兑换状态 + 补签卡（一次 `GET /activity/growth/streak` 读完）。"""

    days: int = 0
    redemption: GrowthRedemptionStatus = field(default_factory=GrowthRedemptionStatus)
    makeup_cards: GrowthMakeupCards = field(default_factory=GrowthMakeupCards)


@dataclass
class GrowthRedeemResult:
    """兑换回执。"""

    cards_granted: int = 0
    cards_overflow: int = 0
    credit_granted: int = 0
    energy_granted: int = 0
    chances_granted: int = 0


@dataclass
class GrowthLotteryDrawResult:
    """单次抽奖结果。`prize_type` credit=积分 / physical=实物。"""

    prize_code: str = ""
    prize_name: str = ""
    prize_type: str = ""
    credit_amount: int = 0


@dataclass
class HeatmapCell:
    """活跃地图热力格（一日一格）；`score == 0` 判漏签。"""

    date: str = ""
    score: int = 0


# ---- token / 日期 ----


def growth_client_token(prefix: str) -> str:
    """生成 SPA 同款 `client_token`：`<prefix>-<uuid>`。

    上游只把它当**每次独立的幂等键**，不校验格式。**每次调用必须新键** ——
    复用旧键可能被上游按幂等键去重，把本次领取吞掉。
    """
    return f"{prefix}-{uuid.uuid4().hex}"


def cst_day(now: float | None = None) -> str:
    """CST 自然日（`YYYY-MM-DD`）。"""
    ts = float(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", time.gmtime(ts + CST_OFFSET_SECONDS))


def growth_yesterday_date(now: float | None = None) -> str:
    """昨日的 CST 自然日。

    补签判据固定盯**昨日**：连登断档只可能发生在「上一个自然日」（今日尚未结算）。

    先归一到 CST 再减日 —— 直接按本地时区减一日，在含夏令时的容器里切换日的
    23h/25h 会把瞬时点挪 1 小时、CST 日期错位一天（补签漏掉真实断档或补错日期）。
    """
    ts = float(now if now is not None else time.time())
    return cst_day(ts - 86400)


def heatmap_day_score(cells: list[HeatmapCell], date: str) -> tuple[int, bool]:
    """`cells` 中该日的 score；无该日格返回 `(0, False)`（无判据，调用方不动）。"""
    for cell in cells:
        if len(cell.date) >= 10 and cell.date[:10] == date:
            return cell.score, True
    return 0, False


# ---- 错误分类（正常态识别）----


def _is_marker(exc: Exception, status: int, *markers: str) -> bool:
    """`exc` 是否 `UpstreamError` 且状态码与任一 marker 命中（大小写不敏感）。

    网络层 / 解析层错误返回 False（不当作幂等正常态）。
    """
    if not isinstance(exc, UpstreamError) or exc.status != status:
        return False
    message = (exc.message or "").lower()
    return any(m.lower() in message for m in markers)


def is_redeem_already_claimed(exc: Exception) -> bool:
    """「本月已领取」幂等态（409 + duplicate/已领取）—— 正常，不重试不刷 WARN。"""
    return _is_marker(exc, 409, "duplicate", "已领取")


def is_redeem_not_enough_days(exc: Exception) -> bool:
    """「连续登录天数不足」（403）—— 正常：本次连登天数 < 该档门槛。"""
    return _is_marker(exc, 403, "连续登录天数不足", "not enough")


def is_lottery_no_chance(exc: Exception) -> bool:
    """「无抽奖次数」（400）—— 正常：无次数不消耗。"""
    return _is_marker(exc, 400, "insufficient lottery chance balance", "insufficient")


def is_lottery_disabled(exc: Exception) -> bool:
    """「抽奖未开启」（400）—— 正常。"""
    return _is_marker(exc, 400, "lottery disabled")


def is_growth_unavailable(exc: Exception) -> bool:
    """成长体系不可用（企业版账号）—— 正常：该系统只对个人用户开放。"""
    return _is_marker(exc, 403, "personal user", "not available", "enterprise")


def is_prerequisite_not_met(exc: Exception) -> bool:
    """任务前置未满足（如 `first_buddy`）—— **正常前置**，不是故障。"""
    return _is_marker(exc, 400, "prerequisite not met")


# ---- 请求层 ----

# 业务错误码 → 语义（上游信封里的 `code`）
_IDEMPOTENT_MARKERS = ("已领", "已签", "重复", "already", "duplicate")


async def _growth_json(
    client: Any, auth: AccountAuth, method: str, path: str,
    body: dict[str, Any] | None = None,
) -> Any:
    """growth / billing 域的 JSON 调用：返回信封里的 `data` 段。

    统一收口三件事：
    - HTTP ≥ 400 → `UpstreamError`（带 status 与 msg，供上面的正常态判定使用）
    - 业务码非 0 → 幂等文案视为成功；其余抛 `UpstreamError`
    - 信封缺 `data` → 返回 `{}`（字段缺失不视为失败）
    """
    import httpx

    from .client import _billing_base, _error_from  # 延迟导入避免循环
    from .headers import billing_headers

    base = _billing_base(client.cfg, auth)
    headers = billing_headers(client.cfg.upstream, auth)
    http = await client.client_for(auth)
    try:
        resp = await http.request(method, base + path, headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc

    if resp.status_code >= 400:
        raise _error_from(resp.status_code, resp.text, dict(resp.headers))

    try:
        payload = resp.json()
    except ValueError:
        return {}
    if not isinstance(payload, dict):
        return payload
    code = str(payload.get("code", 0) or 0)
    if code not in ("0", ""):
        message = str(payload.get("msg", "") or "")
        if any(k in message for k in _IDEMPOTENT_MARKERS):
            return {"_idempotent": True, "msg": message}
        # 业务错误也带上 HTTP 状态，让正常态判定（is_*）能按同一口径识别
        raise UpstreamError(ErrorKind.UNKNOWN, resp.status_code, message,
                            json.dumps(payload, ensure_ascii=False))
    data = payload.get("data")
    return data if data is not None else {}


# ---- 任务原语 ----


async def growth_tasks_raw(client: Any, auth: AccountAuth) -> Any:
    """任务列表的**原始** `data` 段（不解析）。

    上游字段会增删，运维需要一个「照原样看」的出口，而不是被本地结构体过滤后的视图。
    """
    return await _growth_json(client, auth, "GET", GROWTH_TASKS_PATH)


async def growth_tasks(client: Any, auth: AccountAuth) -> list[GrowthTask]:
    """拉取 growth 任务列表。"""
    data = await growth_tasks_raw(client, auth)
    items = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [GrowthTask.from_payload(i) for i in items if isinstance(i, dict)]


async def growth_task_status(client: Any, auth: AccountAuth,
                             code: str) -> GrowthTask | None:
    """查询单个任务；任务不在列表中返回 `None`。"""
    for task in await growth_tasks(client, auth):
        if task.task_code == code:
            return task
    return None


async def accept_growth_task_raw(client: Any, auth: AccountAuth,
                                 codes: list[str]) -> Any:
    """accept 的原始 `data` 段，供运维排查登记失败原因。"""
    return await _growth_json(client, auth, "POST", GROWTH_ACCEPT_PATH,
                              {"task_codes": list(codes)})


async def accept_growth_task(client: Any, auth: AccountAuth,
                             code: str) -> GrowthAcceptResult:
    """接受任务，返回上游回执（含 status 与 message）。

    **为什么必须回读 status 而不是只看 HTTP 200**：实测存在「HTTP 200 + `msg=OK`
    但 `status` 非 accepted」的形态 —— 此时 accept **未真正登记**，后续上报
    **全部不归账**，任务永远点不亮。

    `message` 同样必须保留：实测 `black_cat` 在未领养 Buddy 时返回
    `status=error + message="prerequisite not met: first_buddy"` —— 这是**正常前置
    未满足**，不是故障；丢掉 message 就只能看到一句无信息量的「状态非 accepted」。
    """
    data = await accept_growth_task_raw(client, auth, [code])
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list) or not results:
        return GrowthAcceptResult()
    first = results[0] if isinstance(results[0], dict) else {}
    return GrowthAcceptResult(
        status=str(first.get("status") or ""),
        message=str(first.get("message") or ""),
    )


async def claim_growth_task(client: Any, auth: AccountAuth, code: str) -> Any:
    """领取任务奖励。

    **路径不含 `/v2`、不带 body** —— 这是实测口径，`/v2/.../reward/claim` 是 404
    错端点（KeyPort 原先走的就是那个形态）。
    """
    path = f"{GROWTH_CLAIM_PREFIX}{code}{GROWTH_CLAIM_SUFFIX}"
    return await _growth_json(client, auth, "POST", path)


# ---- 连登 / 抽奖 / 补签 ----


async def growth_reward_state(client: Any, auth: AccountAuth) -> GrowthRewardState:
    """读取连登天数 + 各档兑换状态 + 补签卡余额（一次 GET，免二次请求）。"""
    data = await _growth_json(client, auth, "GET", STREAK_PATH)
    if not isinstance(data, dict):
        return GrowthRewardState()

    streak = data.get("streak") if isinstance(data.get("streak"), dict) else {}
    raw_redemption = (data.get("redemption_status")
                      if isinstance(data.get("redemption_status"), dict) else {})
    raw_cards = (data.get("makeup_cards")
                 if isinstance(data.get("makeup_cards"), dict) else {})

    tiers: list[GrowthTierSpec] = []
    raw_tiers = raw_redemption.get("tiers")
    if isinstance(raw_tiers, list):
        for item in raw_tiers:
            if not isinstance(item, dict):
                continue
            tiers.append(GrowthTierSpec(
                tier=str(item.get("tier") or ""),
                days=int(item.get("days") or 0),
                credit=int(item.get("credit") or 0),
                energy=int(item.get("energy") or 0),
                cards=int(item.get("cards") or 0),
                chances=int(item.get("chances") or 0),
            ))

    return GrowthRewardState(
        days=int(streak.get("days") or 0),
        redemption=GrowthRedemptionStatus(
            tier_7d_status=str(raw_redemption.get("tier_7d_status") or ""),
            tier_14d_status=str(raw_redemption.get("tier_14d_status") or ""),
            tier_28d_status=str(raw_redemption.get("tier_28d_status") or ""),
            tiers=tiers,
            remaining_days=int(raw_redemption.get("remaining_days") or 0),
        ),
        makeup_cards=GrowthMakeupCards(
            balance=int(raw_cards.get("balance") or 0),
            max=int(raw_cards.get("max") or 0),
        ),
    )


async def growth_redeem(client: Any, auth: AccountAuth, tier: str,
                        client_token: str = "") -> GrowthRedeemResult:
    """兑换指定档位连登奖励（幂等：409 本月已领、403 天数不足）。

    `client_token` 为幂等键，**空时自动生成新键** —— 每次调用必须新键，不应复用，
    否则上游可能按幂等键去重吞掉本次领取。
    """
    token = client_token or growth_client_token(f"redeem-{tier}")
    data = await _growth_json(client, auth, "POST", REDEEM_PATH,
                              {"tier": tier, "client_token": token})
    if not isinstance(data, dict):
        return GrowthRedeemResult()
    return GrowthRedeemResult(
        cards_granted=int(data.get("cards_granted") or 0),
        cards_overflow=int(data.get("cards_overflow") or 0),
        credit_granted=int(data.get("credit_granted") or 0),
        energy_granted=int(data.get("energy_granted") or 0),
        chances_granted=int(data.get("chances_granted") or 0),
    )


async def growth_lottery_chances(client: Any, auth: AccountAuth) -> int:
    """当前抽奖次数余额。0 = 无次数（正常态、无副作用）。"""
    data = await _growth_json(client, auth, "GET", LOTTERY_CHANCES_PATH)
    if not isinstance(data, dict):
        return 0
    return int(data.get("balance") or 0)


async def growth_lottery_draw(client: Any, auth: AccountAuth,
                              client_token: str = "") -> GrowthLotteryDrawResult:
    """抽一次奖。`client_token` 为空时自动生成新键（抽奖对它敏感，复用可能被幂等吞掉）。"""
    token = client_token or growth_client_token("draw")
    data = await _growth_json(client, auth, "POST", LOTTERY_DRAW_PATH,
                              {"client_token": token})
    if not isinstance(data, dict):
        return GrowthLotteryDrawResult()
    return GrowthLotteryDrawResult(
        prize_code=str(data.get("prize_code") or ""),
        prize_name=str(data.get("prize_name") or ""),
        prize_type=str(data.get("prize_type") or ""),
        credit_amount=int(data.get("credit_amount") or 0),
    )


async def growth_heatmap(client: Any, auth: AccountAuth) -> list[HeatmapCell]:
    """活跃地图热力格列表（`score == 0` 判漏签）。"""
    data = await _growth_json(client, auth, "GET", HEATMAP_PATH)
    cells = data.get("cells") if isinstance(data, dict) else None
    if not isinstance(cells, list):
        return []
    return [HeatmapCell(date=str(c.get("date") or ""), score=int(c.get("score") or 0))
            for c in cells if isinstance(c, dict)]


async def use_makeup_card(client: Any, auth: AccountAuth, target_date: str) -> Any:
    """对指定日期使用补签卡（`target_date` 为 CST 自然日 `YYYY-MM-DD`）。

    无卡 / 该日无漏签 / 已补过 → 上游业务错误，调用方静默跳过。

    补签的收益是保住**连续天数**（断一次就要重攒 7 天），代价只是一张卡。
    """
    return await _growth_json(client, auth, "POST", MAKEUP_CARD_USE_PATH,
                              {"target_date": target_date})


async def claim_gift(client: Any, auth: AccountAuth) -> int:
    """领取新手礼包（每号一次）。已领返回业务错误，调用方静默跳过。返回到账 credit。"""
    data = await _growth_json(client, auth, "POST", CLAIM_GIFT_PATH, {})
    if not isinstance(data, dict):
        return 0
    return int(data.get("credit") or 0)


async def claim_compensation(client: Any, auth: AccountAuth) -> int:
    """领取活动补偿（有则领）。无可领返回业务错误，调用方静默跳过。返回到账 credit。"""
    data = await _growth_json(client, auth, "POST", CLAIM_COMPENSATION_PATH, {})
    if not isinstance(data, dict):
        return 0
    return int(data.get("credit") or 0)


# ---- Buddy 旅行原语 ----


async def buddy_travel_status(client: Any, auth: AccountAuth) -> dict[str, Any]:
    """Buddy 旅行当前状态（`state` / `daily_limit_reached` / `reward_credit`）。

    `state`：`idle` 未出发 / `traveling` 在路上 / `arrived` 已到达可领奖。
    """
    data = await _growth_json(client, auth, "GET", BUDDY_TRAVEL_STATUS_PATH)
    return data if isinstance(data, dict) else {}


async def buddy_travel_config(client: Any, auth: AccountAuth) -> dict[str, Any]:
    """可选旅行地点列表（含 id 与时长范围）。"""
    data = await _growth_json(client, auth, "GET", BUDDY_TRAVEL_CONFIG_PATH)
    return data if isinstance(data, dict) else {}


async def buddy_travel_depart(client: Any, auth: AccountAuth,
                              location_id: Any) -> dict[str, Any]:
    """派 Buddy 出发去某地点（每自然日一次，重复派发会被上游拒）。"""
    data = await _growth_json(client, auth, "POST", BUDDY_TRAVEL_DEPART_PATH,
                              {"location_id": location_id})
    return data if isinstance(data, dict) else {}


async def buddy_travel_claim(client: Any, auth: AccountAuth) -> dict[str, Any]:
    """领取 Buddy 旅行奖励（仅在 `state == arrived` 时有效）。

    **路径无 `/v2`、无 body** —— 与 growth 领奖同一口径。
    """
    data = await _growth_json(client, auth, "POST", BUDDY_TRAVEL_CLAIM_PATH, {})
    return data if isinstance(data, dict) else {}
