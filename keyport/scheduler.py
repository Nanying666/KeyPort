"""后台常驻定时调度器。

每日整点排程（默认，可在管理台热改并落盘）：
  09:00 / 21:00 签到 + 末尾运行连登管家（兑换已解锁档位 + 抽完抽奖次数）
  09:00 / 21:00 猫猫旅行 / 连续打卡
  10:00         成长任务全自动完成（接取任务 → 行为事件上报 → 领奖）
  22:00         Token 保活（国际版自适应为专属 Token 集中保活）
  01:00         夜猫任务

每个任务独立排程、独立开关，互不影响；每分钟 tick 一次，命中整点即触发。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from .config import Config
from .db import Database, now as db_now
from .pool.manager import AccountPool
from .upstream.base import UpstreamError
from .upstream.client import UpstreamClient

TASKS = ("checkin", "travel", "activity", "keepalive", "cat")

# 成长任务行为事件（上报即点亮）
GROWTH_EVENTS = (
    "open_app", "send_message", "use_tool", "view_profile",
    "daily_active", "share_content", "complete_profile", "invite_friend",
)


class Scheduler:
    """分钟级 tick 的整点排程器。"""

    def __init__(
        self,
        cfg: Config,
        db: Database,
        pool: AccountPool,
        client: UpstreamClient,
        refresher: Any,
    ):
        self.cfg = cfg
        self.db = db
        self.pool = pool
        self.client = client
        self.refresher = refresher
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_run: dict[tuple[str, str], int] = {}  # (task,realm) -> hour stamp
        self.last_results: list[dict] = []

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - 调度循环不允许被单次失败打断
                pass
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except asyncio.TimeoutError:
                continue

    async def tick(self, now: int | None = None) -> list[dict]:
        """检查当前整点是否命中排程；命中则执行。"""
        now = now or int(time.time())
        hour = time.localtime(now).tm_hour
        stamp = now // 3600  # 同一小时只触发一次
        sched = self.cfg.schedule
        fired: list[dict] = []

        mapping = {
            "checkin": (sched.checkin_enabled, sched.checkin_hours),
            "travel": (sched.travel_enabled, sched.travel_hours),
            "activity": (sched.activity_enabled, sched.activity_hours),
            "keepalive": (sched.keepalive_enabled, sched.keepalive_hours),
            "cat": (sched.cat_enabled, sched.cat_hours),
        }
        for task, (enabled, hours) in mapping.items():
            if not enabled or hour not in hours:
                continue
            if self._last_run.get(task) == stamp:
                continue
            self._last_run[task] = stamp
            result = await self.run_now(task)
            fired.append({"task": task, "hour": hour, "result": result})
        return fired

    # ---- 任务执行 ----

    async def run_now(self, task: str, realm: str = "") -> list[dict]:
        """立即执行某任务（全账号或指定域）。"""
        entries = [
            e for e in self.pool.all_entries()
            if e.kind != "apikey" and (not realm or e.realm == realm)
        ]
        results: list[dict] = []
        for entry in entries:
            outcome = await self._run_for(task, entry)
            results.append(outcome)
            self._record(task, entry, outcome)
        self.last_results = results
        return results

    async def _run_for(self, task: str, entry) -> dict:
        try:
            auth = await self.refresher.ensure_valid(self.pool.auth_for(entry))
        except UpstreamError as exc:
            return {"uid": entry.uid, "realm": entry.realm, "ok": False,
                    "detail": f"token: {exc.message or exc.kind.value}"}
        if not auth.access_token:
            return {"uid": entry.uid, "realm": entry.realm, "ok": False,
                    "detail": "no credential"}

        try:
            if task == "checkin":
                return await self._checkin(entry, auth)
            if task == "travel":
                data = await self.client.travel(auth)
                return {"uid": entry.uid, "realm": entry.realm,
                        "ok": bool(data.get("ok")), "detail": data.get("msg", "")}
            if task == "activity":
                return await self._activity(entry, auth)
            if task == "keepalive":
                data = await self.client.keepalive(auth)
                return {"uid": entry.uid, "realm": entry.realm, "ok": True,
                        "detail": f"credits={data.get('credits')}"}
            if task == "cat":
                # 夜猫任务复用旅行端点（独立排程，互不影响）
                data = await self.client.travel(auth)
                return {"uid": entry.uid, "realm": entry.realm,
                        "ok": bool(data.get("ok")), "detail": "night-cat"}
        except UpstreamError as exc:
            return {"uid": entry.uid, "realm": entry.realm, "ok": False,
                    "detail": f"{exc.kind.value}: {exc.message}"}
        return {"uid": entry.uid, "realm": entry.realm, "ok": False, "detail": "unknown task"}

    async def _checkin(self, entry, auth) -> dict:
        """签到 → 末尾运行连登管家（兑换已解锁档位 + 抽完抽奖次数）。"""
        data = await self.client.daily_checkin(auth)
        ok = bool(data.get("ok"))
        detail = data.get("msg", "checkin")
        if ok:
            self.pool.mark_checkin(entry.uid)
            # 连登管家：兑换与抽奖（端点不可用时静默跳过，不阻断签到）
            try:
                steward = await self.client._billing_post(
                    auth, ["/v2/billing/streak/redeem", "/billing/streak/redeem"], {}
                )
                if steward.get("ok"):
                    detail += " + steward"
            except UpstreamError:
                pass
        return {"uid": entry.uid, "realm": entry.realm, "ok": ok, "detail": detail}

    async def _activity(self, entry, auth) -> dict:
        """成长任务全自动完成：接取任务 → 行为事件上报点亮 → 自动领取奖励。"""
        lit = 0
        try:
            await self.client._billing_post(
                auth, ["/v2/report/activity/tasks", "/report/activity/tasks"], {},
                method="GET",
            )
        except UpstreamError:
            pass
        for event in GROWTH_EVENTS:
            try:
                result = await self.client.activity_event(auth, event)
            except UpstreamError:
                continue
            if result.get("ok"):
                lit += 1
        claimed = False
        try:
            claim = await self.client._billing_post(
                auth, ["/v2/report/activity/reward", "/report/activity/reward"], {}
            )
            claimed = bool(claim.get("ok"))
        except UpstreamError:
            pass
        return {"uid": entry.uid, "realm": entry.realm, "ok": lit > 0,
                "detail": f"events={lit} claimed={claimed}"}

    def _record(self, task: str, entry, outcome: dict) -> None:
        self.db.execute(
            "INSERT INTO task_runs(ts, task, realm, account, ok, detail) VALUES(?,?,?,?,?,?)",
            (db_now(), task, entry.realm, entry.uid,
             1 if outcome.get("ok") else 0, str(outcome.get("detail", ""))[:300]),
        )

    # ---- 状态 ----

    def status(self) -> dict:
        sched = self.cfg.schedule
        return {
            "running": self._task is not None and not self._task.done(),
            "schedule": {
                "checkin_hours": sched.checkin_hours,
                "travel_hours": sched.travel_hours,
                "activity_hours": sched.activity_hours,
                "keepalive_hours": sched.keepalive_hours,
                "cat_hours": sched.cat_hours,
                "checkin_enabled": sched.checkin_enabled,
                "travel_enabled": sched.travel_enabled,
                "activity_enabled": sched.activity_enabled,
                "keepalive_enabled": sched.keepalive_enabled,
                "cat_enabled": sched.cat_enabled,
            },
            "last_results": self.last_results[-20:],
        }
