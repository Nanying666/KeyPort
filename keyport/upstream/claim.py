"""限时套餐领取（ZCode Coding Plan `billing/preview` + `billing/claim`）。

链路（对齐官方客户端与同生态实现）：

  1. `GET  {plan_base}/billing/preview?app_version=&platform=` → `data.plans[]`
  2. 领取需阿里云无痕验证码：`CaptchaManager` 求解 → `X-Aliyun-Captcha-Verify-Param`
  3. `POST {plan_base}/billing/claim` body `{"plan_id": ...}`（+ Verify-Region 头）

上游业务码语义（沿用官方口径，收口在 constants.CLAIM_CODE_MESSAGES）：
  1001 套餐不存在 / 1002 活动结束 / 1003 已领取过 / 1004 不符合条件 /
  1005 今日名额用完 / 3001 参数错误 / 3007 验证码失败 / 401 未登录。

## 两个关键设计

- **激活上报先于 preview**：活动投放疑似以当日激活信号为资格，反序会形成
  「preview 恒空 → 永不激活」的死锁（见 telemetry 模块文档）。
- **1005 按服务端窗口退避**：等待期该套餐不打 claim（省验证码求解与上游写
  流量），preview 照常保留新套餐发现。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from ..config import ClaimConfig
from ..db import Database
from ..db import now as db_now
from . import constants as C
from .base import AccountAuth, ErrorKind, UpstreamError
from .client import UpstreamClient
from .fingerprint import profile_for
from .install import jwt_user_id

AUTH_EXPIRED_MESSAGE = "凭证失效，请重新授权"


class ClaimError(Exception):
    """业务失败（含上游 code 语义），message 面向用户。

    `next_at` 仅 1005（名额用完）携带：上游 `data.plan.ends_at`（秒 → 毫秒），
    即名额恢复时间。
    """

    def __init__(self, message: str, *, code: int = -1, next_at: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.next_at = next_at


def business_code(body: Any) -> int:
    """上游业务码；非对象 / 缺 code / 非数字 → -1（视为失败）。"""
    if not isinstance(body, dict):
        return -1
    try:
        return int(body.get("code", -1))
    except (TypeError, ValueError):
        return -1


def _ms(value: Any) -> int | None:
    """上游秒值 → 毫秒；缺失 / 非正数返回 None（不造数）。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0:
        return int(value * 1000)
    return None


def parse_plan(raw: Any) -> dict | None:
    """提取可领取套餐（plan_id / name / 描述 / 优先级 + model_usage token 授权项）。"""
    if not isinstance(raw, dict):
        return None
    plan_id = str(raw.get("plan_id") or raw.get("planId") or "").strip()
    if not plan_id:
        return None
    grants: list[dict[str, Any]] = []
    for ent in raw.get("entitlements") or []:
        if not isinstance(ent, dict):
            continue
        if ent.get("meter") != "model_usage" or ent.get("unit_type") != "token":
            continue
        name = str(ent.get("show_name") or ent.get("showName") or "").strip()
        if not name:
            continue
        units = ent.get("grant_units", ent.get("grantUnits")) or 0
        try:
            units_f = float(units)
        except (TypeError, ValueError):
            units_f = 0.0
        grants.append({
            "name": name,
            "units": units_f,
            "period": ent.get("period") or "one_time",
        })
    return {
        "plan_id": plan_id,
        "name": str(raw.get("name") or "").strip(),
        "description": str(raw.get("description") or "").strip(),
        "priority": raw.get("priority") or 0,
        "grants": grants,
    }


def _fail_error(code: int, body: dict) -> ClaimError:
    """业务码 → ClaimError；1005 附带名额恢复时间（`data.plan.ends_at` 秒 → 毫秒）。"""
    base = C.CLAIM_CODE_MESSAGES.get(code, "领取失败")
    server = str(body.get("msg") or body.get("message") or "")
    message = f"{base}（{server}）" if server else base
    next_at = None
    if code == C.CLAIM_QUOTA_EXHAUSTED_CODE:
        ends = ((body.get("data") or {}).get("plan") or {}).get("ends_at")
        next_at = _ms(ends)
    return ClaimError(message, code=code, next_at=next_at)


def is_zcode_plan_account(auth: AccountAuth) -> bool:
    """是否为 ZCode 平台账号（z.ai Coding Plan 端点）。

    只有这类账号才具备上游的套餐领取与激活事件资格：

    - **套餐领取**（`billing/preview` / `billing/claim`）是 Coding Plan 专属权益，
      其它域（WorkBuddy / CodeBuddy）打这些端点只会 404 并白耗上游流量。
    - **激活上报**（`event/report`）模拟的是 ZCode 桌面端首启行为，对非 ZCode
      账号上报属于伪造无关信号，不该发生。

    判据是账号出站 base_url 落在 Coding Plan 网关下（`/zcode-plan` 标记）。
    """
    return C.ZCODE_PLAN_MARKER in (getattr(auth, "base_url", "") or "")


def billing_block_reason(auth: AccountAuth, *, action: str = "领取") -> str | None:
    """账号不可打 billing 时的用户文案；可打则返回 None。

    与选号门同源：无凭证 / 非 ZCode 平台账号一律不打 billing，
    保证「冷却期零上游 billing 流量」不变量。
    """
    if not auth.access_token:
        return f"账号无凭证，已跳过{action}"
    if not is_zcode_plan_account(auth):
        return f"非 ZCode 平台账号，已跳过{action}"
    return None


def _claim_outcome(body: dict, plan_id: str, plan_name: str, grants: list) -> dict:
    """领取成功返回集：`server_time` / `starts_at` / `ends_at` 秒值 → 毫秒。

    服务端时钟随成功载荷下发，供前端区分本机时钟漂移；缺失字段保持 None。
    """
    data = body.get("data") if isinstance(body.get("data"), dict) else {}
    plan = data.get("plan") if isinstance(data.get("plan"), dict) else {}
    return {
        "plan_id": plan_id,
        "plan_name": plan_name,
        "grants": grants,
        "starts_at": _ms(plan.get("starts_at")),
        "ends_at": _ms(plan.get("ends_at")),
        "server_time": _ms(data.get("server_time")),
    }


class ClaimService:
    """套餐领取服务：preview / 领取 / 后台自动轮。"""

    def __init__(self, cfg: ClaimConfig, client: UpstreamClient, db: Database, captcha: Any = None):
        self.cfg = cfg
        self.client = client
        self.db = db
        self.captcha = captcha
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        # 账号 uid → {plan_id: 名额恢复时间(ms)}：1005 退避记忆（仅轮次路径使用）
        self._holds: dict[str, dict[str, int]] = {}
        # 激活上报每日去重门：uid → 最近一次**成功**上报的本地日期
        self._activation_reported: dict[str, str] = {}
        self.last_results: list[dict] = []

    # ---- preview / 领取 ----

    async def preview_plans(self, auth: AccountAuth) -> list[dict]:
        """拉取账号当前可领取套餐，按优先级降序。"""
        blocked = billing_block_reason(auth, action="上游查询")
        if blocked:
            raise ClaimError(blocked)
        body = await self.client.fetch_plan_preview(auth)
        code = business_code(body)
        if code != 0:
            raise _fail_error(code, body)
        raw_plans = (body.get("data") or {}).get("plans") or []
        plans = [p for p in (parse_plan(item) for item in raw_plans) if p]
        plans.sort(key=lambda p: (-p["priority"], p["plan_id"]))
        return plans

    async def _auto_pick(self, auth: AccountAuth, plan_id: str) -> tuple[str, str, list]:
        """plan_id 为空时 preview 自动选优先级最高套餐。"""
        if plan_id:
            return plan_id, "", []
        plans = await self.preview_plans(auth)
        if not plans:
            raise ClaimError("没有待领取的套餐")
        best = plans[0]
        return best["plan_id"], best["name"] or best["plan_id"], best["grants"]

    async def claim_with_captcha(
        self, auth: AccountAuth, verify_param: str, verify_region: str = "",
        plan_id: str = "",
    ) -> dict:
        """手动领取：verify_param 由用户浏览器内阿里 SDK 滑块产生，本端只做转发。

        plan_id 缺省时先 preview 自动选优先级最高套餐（无需验证码）。
        """
        blocked = billing_block_reason(auth)
        if blocked:
            raise ClaimError(blocked)
        if not (verify_param or "").strip():
            raise ClaimError("缺少验证码参数，请先完成人机验证")
        plan_id, plan_name, grants = await self._auto_pick(auth, plan_id)
        body = await self.client.claim_plan(
            auth, plan_id, verify_param.strip(), verify_region
        )
        code = business_code(body)
        if code != 0:
            raise _fail_error(code, body)
        return _claim_outcome(body, plan_id, plan_name, grants)

    async def claim(self, auth: AccountAuth, plan_id: str = "") -> dict:
        """自动领取：本端求解验证码后提交。

        验证码被拒（3007）单次即止 —— 拒码本身就是上游风险信号，换码连打只会
        加剧；拒码出池，下一轮自然重试。
        """
        blocked = billing_block_reason(auth)
        if blocked:
            raise ClaimError(blocked)
        plan_id, plan_name, grants = await self._auto_pick(auth, plan_id)
        verify_param, verify_region = await self._solve_captcha(auth)
        body = await self.client.claim_plan(auth, plan_id, verify_param, verify_region)
        code = business_code(body)
        if code == 0:
            return _claim_outcome(body, plan_id, plan_name, grants)
        if code == 3007 and self.captcha is not None:
            self.captcha.invalidate()  # 被拒的码出池，避免下轮复用
        raise _fail_error(code, body)

    async def _solve_captcha(self, auth: AccountAuth) -> tuple[str, str]:
        """求解验证码参数；未启用农场时返回空串（由上游报 3007）。"""
        if self.captcha is None:
            return "", ""
        param = await self.captcha.acquire(auth.access_token)
        region = getattr(getattr(self.captcha, "cfg", None), "region", "") or ""
        return param or "", region

    # ---- 激活上报 ----

    async def report_activation(self, auth: AccountAuth) -> str:
        """上报官方客户端激活事件（app_launch + app_daily_active）；返回错误文案或空串。

        **仅限 ZCode 平台账号**：激活上报模拟的是 ZCode 桌面端首启行为，
        对其它域的账号上报等于伪造无关信号。
        """
        from .telemetry import post_activation_event

        if not is_zcode_plan_account(auth):
            return "非 ZCode 平台账号，跳过激活上报"
        profile = profile_for(auth)
        user_id = jwt_user_id(auth.access_token)
        if not user_id:
            return "JWT 无 user_id，跳过激活上报"
        for element in C.ACTIVATION_ELEMENTS:
            try:
                await post_activation_event(self.client, profile, user_id, element)
            except UpstreamError as err:
                return f"激活事件 {element} 上报失败: {err.message or err.kind.value}"
        return ""

    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    # ---- 自动领取 ----

    async def auto_claim_all(
        self, auth: AccountAuth, *, skip_plan_ids: set[str] | None = None,
        report_activation: bool = True,
    ) -> list[dict]:
        """单账号自动领取：每日一次激活上报（preview 之前）→ preview → 逐个领取。

        **仅限 ZCode 平台账号**（`realm=zai` 且 base_url 指向 Coding Plan 网关）：
        套餐领取是 Coding Plan 专属权益，激活上报模拟的是 ZCode 桌面端行为。
        非 ZCode 账号（WorkBuddy / CodeBuddy）直接跳过 —— 打这些端点只会 404，
        还会白耗上游流量。

        任何失败只记 outcome、绝不抛出（入池 / 轮次链路不受影响）。重复执行安全
        （上游 1003 已领取过幂等）。`skip_plan_ids`：本轮跳过领取的套餐 id
        （1005 名额等待期由轮次记忆传入）；preview 仍照常执行，新套餐发现不受影响。
        """
        if not is_zcode_plan_account(auth):
            return [{"uid": auth.uid, "ok": False, "outcome": "skipped",
                     "message": "非 ZCode 平台账号，跳过自动领取"}]

        outcomes: list[dict] = []

        if report_activation and self._activation_reported.get(auth.uid) != self._today():
            err = await self.report_activation(auth)
            if not err:
                self._activation_reported[auth.uid] = self._today()

        try:
            plans = await self.preview_plans(auth)
        except ClaimError as err:
            return [{"uid": auth.uid, "ok": False, "outcome": "no_plans",
                     "message": str(err)}]
        except UpstreamError as err:
            return [{"uid": auth.uid, "ok": False, "outcome": "error",
                     "message": err.message or err.kind.value}]

        if not plans:
            return [{"uid": auth.uid, "ok": False, "outcome": "no_plans",
                     "message": "上游无投放套餐"}]

        skip = skip_plan_ids or set()
        for plan in plans:
            if plan["plan_id"] in skip:
                continue
            try:
                result = await self.claim(auth, plan["plan_id"])
                outcomes.append({"uid": auth.uid, "ok": True, "outcome": "claimed",
                                 **result})
            except ClaimError as err:
                outcome = {"uid": auth.uid, "ok": False, "outcome": "failed",
                           "plan_id": plan["plan_id"], "message": str(err)}
                if err.code != -1:
                    outcome["code"] = err.code
                if err.next_at:
                    outcome["next_at"] = err.next_at
                outcomes.append(outcome)
            except UpstreamError as err:
                outcomes.append({"uid": auth.uid, "ok": False, "outcome": "error",
                                 "plan_id": plan["plan_id"],
                                 "message": err.message or err.kind.value})
        return outcomes

    # ---- 后台轮 ----

    async def _round(self, pool: Any) -> list[dict]:
        """对全部可打 billing 的 **ZCode 平台**账号执行一轮自动领取。"""
        entries = [e for e in pool.all_entries() if self._claimable(e)]
        if not entries:
            return []
        limit = max(1, self.cfg.max_accounts_per_round)
        entries = entries[:limit]
        now_ms = time.time() * 1000
        # 退避表先清过期项（next_at 已过的恢复本轮重试）
        self._holds = {
            uid: {pid: at for pid, at in plans.items() if at > now_ms}
            for uid, plans in self._holds.items()
        }
        self._holds = {uid: plans for uid, plans in self._holds.items() if plans}

        results: list[dict] = []
        for entry in entries:
            holds = self._holds.get(entry.uid, {})
            try:
                auth = pool.auth_for(entry)
            except Exception:  # noqa: BLE001
                continue
            if not is_zcode_plan_account(auth):
                continue  # 非 ZCode 账号不参与领取轮（billing 端点对它无意义）
            try:
                outcomes = await self.auto_claim_all(auth, skip_plan_ids=set(holds))
            except Exception as err:  # noqa: BLE001 - 单账号异常不中断整轮
                outcomes = [{"uid": entry.uid, "ok": False, "outcome": "error",
                             "message": str(err)}]
            # 按本轮实绩更新退避表：1005+next_at 记忆；成功/其余失败清除
            updated = dict(holds)
            for o in outcomes:
                pid = o.get("plan_id")
                if not pid:
                    continue
                next_at = o.get("next_at") if o.get("code") == C.CLAIM_QUOTA_EXHAUSTED_CODE else None
                if next_at:
                    updated[pid] = int(next_at)
                else:
                    updated.pop(pid, None)
            if updated:
                self._holds[entry.uid] = updated
            else:
                self._holds.pop(entry.uid, None)
            results.extend(outcomes)
            self._record(entry, outcomes)
        self.last_results = results[-50:]
        return results

    def _claimable(self, entry: Any) -> bool:
        """账号是否可打 billing（preview/claim）。

        只排除系统级禁用（额度耗尽 / 鉴权失效 / 风控禁用）—— 那些账号打 billing
        既无意义又会加剧风控。`manual_disabled`（运维临时摘除）**不**排除：
        摘除只针对对话流量，签到 / 保活 / 领取照常。
        """
        return bool(entry.enabled)

    def _record(self, entry: Any, outcomes: list[dict]) -> None:
        """领取结果落 claim_history（供管理台展示与排障）。"""
        for o in outcomes:
            self.db.execute(
                "INSERT INTO claim_history(ts, uid, account, plan_id, plan_name, outcome, "
                "code, starts_at, ends_at, detail) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    db_now(), entry.uid, entry.nickname or "",
                    str(o.get("plan_id") or ""), str(o.get("plan_name") or ""),
                    str(o.get("outcome") or ("claimed" if o.get("ok") else "failed")),
                    int(o.get("code") or 0), int(o.get("starts_at") or 0),
                    int(o.get("ends_at") or 0), str(o.get("message") or "")[:300],
                ),
            )

    async def _loop(self, pool: Any) -> None:
        # 启动先等一段（避开启动安装序 / 入池自动领取的首次流量高峰）
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30)
            return
        except TimeoutError:
            pass
        while not self._stop.is_set():
            interval = max(0, int(self.cfg.round_interval_seconds))
            if self.cfg.enabled and interval > 0:
                try:
                    await self._round(pool)
                except Exception:  # noqa: BLE001 - 后台任务需吞掉异常继续运行
                    pass
            wait = interval if interval > 0 else 30
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=wait)
                return
            except TimeoutError:
                continue

    def start(self, pool: Any) -> None:
        if not self.cfg.enabled or self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(pool))

    async def stop(self) -> None:
        self._stop.set()
        if self._task is None:
            return
        task, self._task = self._task, None
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass

    def status(self) -> dict:
        now_ms = time.time() * 1000
        return {
            "enabled": self.cfg.enabled,
            "round_interval_seconds": self.cfg.round_interval_seconds,
            "auto_on_add": self.cfg.auto_on_add,
            "holds": {
                uid: {pid: at for pid, at in plans.items() if at > now_ms}
                for uid, plans in self._holds.items()
            },
            "last_results": self.last_results[-20:],
        }

    # ---- 手动入口（管理台） ----

    def holds_for(self, uid: str) -> dict[str, int]:
        """该账号当前 1005 退避记忆（plan_id → 恢复时间 ms）。"""
        now_ms = time.time() * 1000
        return {pid: at for pid, at in (self._holds.get(uid) or {}).items() if at > now_ms}

    def history(self, uid: str = "", limit: int = 100) -> list[dict]:
        """领取历史（最近 limit 条）。"""
        sql = "SELECT * FROM claim_history"
        params: list[Any] = []
        if uid:
            sql += " WHERE uid=?"
            params.append(uid)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [dict(r) for r in self.db.query(sql, tuple(params))]


def plan_models_available(body: dict) -> bool:
    """billing/balance 响应里是否含 active 套餐（诊断辅助）。"""
    payload = body.get("data") if isinstance(body.get("data"), dict) else body
    plans = payload.get("plans") if isinstance(payload, dict) else None
    if not isinstance(plans, list):
        return False
    return any(isinstance(p, dict) and p.get("status") == "active" for p in plans)


__all__ = [
    "AUTH_EXPIRED_MESSAGE",
    "ClaimError",
    "ClaimService",
    "ErrorKind",
    "billing_block_reason",
    "business_code",
    "parse_plan",
]
