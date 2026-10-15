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
import hashlib
import logging
import time
from typing import Any

from .config import Config
from .db import Database, now as db_now
from .pool.manager import AccountPool
from .upstream.base import UpstreamError
from .upstream.client import UpstreamClient

log = logging.getLogger("keyport.scheduler")

TASKS = ("checkin", "travel", "activity", "keepalive", "cat")

# 成长任务行为事件（上报即点亮）
GROWTH_EVENTS = (
    "open_app", "send_message", "use_tool", "view_profile",
    "daily_active", "share_content", "complete_profile", "invite_friend",
)


def _jitter_offset(task: str, local: time.struct_time, span_seconds: int) -> int:
    """按 (任务, 日期, 小时) 派生确定性抖动偏移，落在 `[0, span_seconds)`。

    用哈希而不是 `random`：`tick()` 每分钟重算一次，若每次得到新偏移，
    那一小时会被反复重置、**永远到不了点**。确定性偏移保证同一小时只算一次。
    """
    if span_seconds <= 0:
        return 0
    seed = f"{task}|{local.tm_year}-{local.tm_mon}-{local.tm_mday}T{local.tm_hour}"
    digest = hashlib.sha256(seed.encode()).digest()
    return int.from_bytes(digest[:4], "big") % span_seconds


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
        self._balance_task: asyncio.Task | None = None
        self._alert_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._last_run: dict[tuple[str, str], int] = {}  # (task,realm) -> hour stamp
        self.last_results: list[dict] = []

    # ---- 生命周期 ----

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._loop())
        if self._balance_task is None or self._balance_task.done():
            self._balance_task = asyncio.create_task(self._balance_loop())
        if self._alert_task is None or self._alert_task.done():
            self._alert_task = asyncio.create_task(self._alert_loop())

    async def stop(self) -> None:
        self._stop.set()
        for attr in ("_task", "_balance_task", "_alert_task"):
            task = getattr(self, attr)
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                setattr(self, attr, None)

    # ---- 可用性告警 ----

    async def _alert_loop(self) -> None:
        """周期评估池健康度并在越界时投递 webhook（默认关闭）。"""
        from .alert import Monitor

        monitor = Monitor(self.cfg.alert)
        if not monitor.enabled:
            return
        while not self._stop.is_set():
            interval = max(5.0, float(self.cfg.alert.interval_seconds or 30))
            try:
                for record in monitor.tick(self):
                    await self._deliver(record)
            except Exception as exc:  # noqa: BLE001 - 后台循环不允许被单次失败打断
                log.warning("告警评估失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval)
            except TimeoutError:
                continue

    async def _deliver(self, record: dict) -> None:
        """投递一条告警；失败只记日志、不重试（通知是旁路）。"""
        import httpx

        timeout = float(self.cfg.alert.timeout_seconds or 5)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.post(record["url"], content=record["body"],
                                         headers=record["headers"])
            if resp.status_code >= 300:
                log.warning("告警投递返回 HTTP %s", resp.status_code)
        except Exception as exc:  # noqa: BLE001 - 投递失败不影响主流程
            log.warning("告警投递失败：%s", exc)

    # ---- 告警数据源（Source 协议实现）----

    def health(self, realm: str) -> Any:
        return self.pool.alert_health(realm)

    def waf_active(self) -> bool:
        gw = getattr(self, "gateway", None)
        gate = getattr(gw, "waf_gate", None)
        return bool(gate and gate.active())

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
        """检查当前整点是否命中排程；命中则执行。

        抖动（`schedule.jitter_minutes`）把触发时刻摊在该小时内的确定性偏移上：
        **同一任务 + 同一天 + 同一小时永远同一偏移**（用哈希而非随机 —— 每轮重算
        随机偏移会让那一小时永远不到点）。0 = 精确整点。
        """
        now = now or int(time.time())
        local = time.localtime(now)
        hour = local.tm_hour
        stamp = now // 3600  # 同一小时只触发一次
        sched = self.cfg.schedule
        jitter = max(0, int(getattr(sched, "jitter_minutes", 0) or 0)) * 60
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
            if jitter:
                offset = _jitter_offset(task, local, jitter)
                # 该小时起点 + 偏移；未到点则等下一轮 tick（分钟级精度足够）
                if now < stamp * 3600 + offset:
                    continue
            self._last_run[task] = stamp
            result = await self.run_now(task)
            fired.append({"task": task, "hour": hour, "result": result})
        return fired

    # ---- 余额后台刷新 ----

    async def _balance_loop(self) -> None:
        """分钟级周期刷新余额（独立于整点排程）。

        余额是**分钟级观测量**，但只在签到/请求时才更新 —— 积分冷却（余额耗尽）的
        账号要等到下一个签到点（最坏约 11.5 小时）才可能解冻。独立周期刷新让
        「余额恢复 → 自动解冻」在几分钟内生效，也是面板额度展示的新鲜度保障。

        刻意**不做签到、不刷 token** —— 只让「积分」这个观测量保持新鲜。
        """
        while not self._stop.is_set():
            minutes = max(1, int(self.cfg.schedule.balance_refresh_minutes or 5))
            try:
                if self.cfg.schedule.balance_refresh_enabled:
                    await self.refresh_balances()
            except Exception as exc:  # noqa: BLE001 - 后台循环不允许被单次失败打断
                log.warning("余额后台刷新失败：%s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=minutes * 60)
            except asyncio.TimeoutError:  # noqa: UP041 - 与既有 tick 循环同一写法
                continue

    async def refresh_balances(self) -> dict:
        """刷新全部账号余额；余额恢复的冷却账号自动解冻。"""
        include_disabled = self.cfg.schedule.include_disabled_in_tasks
        entries = [
            e for e in self.pool.all_entries()
            if e.kind != "apikey" and (include_disabled or e.in_pool())
        ]
        ok = failed = 0
        for entry in entries:
            try:
                auth = await self.refresher.ensure_valid(self.pool.auth_for(entry))
            except UpstreamError:
                failed += 1
                continue
            if not auth.access_token:
                failed += 1
                continue
            try:
                data = await self.client.fetch_credits(auth)
            except UpstreamError:
                failed += 1
                continue
            self.pool.update_credits(
                entry.uid, int(data.get("credits") or 0),
                expiring=int(data.get("expiring") or 0),
                used=data.get("used"),
            )
            ok += 1
        self.pool.save_state()
        return {"ok": ok, "failed": failed, "total": len(entries)}

    # ---- 任务执行 ----

    async def run_now(self, task: str, realm: str = "") -> list[dict]:
        """立即执行某任务（全账号或指定域）。

        门控（方向**逐任务不同**，不能一刀切）：

        - **企业版**：无个人成长体系，成长类端点上游直接 403
          （`growth system is only available for personal users`）——
          执行任何成长任务都是零收益白跑，且这类「静默无效」在日志里看不出异常。
        - **global（国际版）**：无 CN 任务体系 → 签到 / 旅行 / 夜猫跳过；
          但**活跃上报不跳过**（实测可用），跳过会让国际版连登永远点不亮。
        """
        include_disabled = self.cfg.schedule.include_disabled_in_tasks
        entries = [
            e for e in self.pool.all_entries()
            if e.kind != "apikey"
            and (not realm or e.realm == realm)
            and (include_disabled or e.in_pool())
        ]
        results: list[dict] = []
        for entry in entries:
            skip = self._gate_reason(task, entry)
            if skip:
                outcome = {"uid": entry.uid, "realm": entry.realm, "ok": True,
                           "skipped": True, "detail": skip}
            else:
                outcome = await self._run_for(task, entry)
            results.append(outcome)
            self._record(task, entry, outcome)
        self.last_results = results
        return results

    def _gate_reason(self, task: str, entry: Any) -> str:
        """该账号是否应跳过此任务；返回原因（空串 = 照常执行）。"""
        # 活跃上报对 global 可用（实测 workbuddy.ai 的 /v2/report 返回 code=0），
        # 跳过会让国际版连登永远点不亮 —— 这里刻意不按 realm 跳过。
        if task == "activity":
            if getattr(entry, "is_enterprise", False):
                return "企业版无个人成长体系（上游 403），跳过"
            return ""
        if getattr(entry, "realm", "") == "global":
            return "国际版无国内任务体系，跳过"
        if getattr(entry, "is_enterprise", False):
            return "企业版无个人成长体系（上游 403），跳过"
        return ""

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
        """签到 → 末尾运行**连登管家**（补签保连登 → 兑换已解锁档位 → 抽完次数）。"""
        data = await self.client.daily_checkin(auth)
        ok = bool(data.get("ok"))
        detail = data.get("msg", "checkin")
        if ok:
            self.pool.mark_checkin(entry.uid)
            detail += await self._streak_steward(entry, auth)
        return {"uid": entry.uid, "realm": entry.realm, "ok": ok, "detail": detail}

    async def _streak_steward(self, entry, auth) -> str:
        """连登管家：补签 → 兑换 → 抽奖（各步独立容错，绝不阻断签到）。

        顺序刻意如此：

        1. **先补签**：连登天数一断就要重攒 7 天，而补签卡的代价远小于此；
           且补签会改变 `streak.days`，必须在读兑换状态**之前**做。
        2. **再兑换**：按天数闸挑最高可领档位，同月每档各一次（409 幂等）。
        3. **最后抽奖**：兑换送的 `chances` 是抽奖次数来源，顺序反了就抽不到。
        """
        from .upstream import growth as G

        notes: list[str] = []
        try:
            state = await self.client.growth_reward_state(auth)
        except UpstreamError as exc:
            if G.is_growth_unavailable(exc):
                return " + 连登管家跳过（企业版）"
            return ""

        # ---- 1. 补签保连登 ----
        try:
            cards = state.makeup_cards.balance
            if cards > 0:
                yesterday = G.growth_yesterday_date()
                cells = await self.client.growth_heatmap(auth)
                score, known = G.heatmap_day_score(cells, yesterday)
                # 只在**确认昨日漏签**时补（无该日格 = 无判据，不动）
                if known and score == 0:
                    await self.client.use_makeup_card(auth, yesterday)
                    notes.append("补签+1")
        except UpstreamError:
            pass

        # ---- 2. 兑换连登奖励（挑最高可领档位）----
        for tier in ("28d", "14d", "7d"):
            if state.redemption.claimed(tier):
                continue
            if not (state.redemption.available(tier)
                    or state.redemption.status_of(tier) in ("", "locked")):
                continue
            try:
                result = await self.client.growth_redeem(auth, tier)
                notes.append(f"兑换{tier}(+{result.credit_granted}分"
                             f"+{result.chances_granted}次)")
                break
            except UpstreamError as exc:
                if G.is_redeem_already_claimed(exc) or G.is_redeem_not_enough_days(exc):
                    continue
                continue

        # ---- 3. 抽完抽奖次数（有上限，防上游返回异常值时死循环）----
        try:
            chances = await self.client.growth_lottery_chances(auth)
            drawn = 0
            credit = 0
            for _ in range(min(max(chances, 0), 20)):
                try:
                    prize = await self.client.growth_lottery_draw(auth)
                except UpstreamError as exc:
                    if G.is_lottery_no_chance(exc) or G.is_lottery_disabled(exc):
                        break
                    break
                drawn += 1
                credit += prize.credit_amount
            if drawn:
                notes.append(f"抽奖{drawn}次(+{credit}分)")
        except UpstreamError:
            pass

        return (" + " + " ".join(notes)) if notes else ""

    async def _activity(self, entry, auth) -> dict:
        """成长任务全自动完成：拉列表 → 逐个 accept（**回读确认**）→ 达标即领奖。

        ## 三条实测教训（照抄参考实现，别改）

        1. **领奖端点是 Web 域路径式**：`POST /activity/growth/tasks/<code>/claim`，
           任务码在路径、**无 body**。原先走的 `/v2/report/activity/reward` 是错端点，
           「端点对了才有归账可言」。
        2. **accept 必须回读 status**：实测存在「HTTP 200 + msg=OK 但 status 非
           accepted」的形态 —— 此时 accept 未真正登记，**后续上报全部不归账**，
           任务永远点不亮。只看 HTTP 码会把这种静默失败当成功。
        3. **`prerequisite not met` 是正常前置未满足**，不是故障（如 `black_cat`
           需要先领养 Buddy）—— 要把它记成 skipped 而不是 fail。
        """
        from .upstream import growth as G

        try:
            tasks = await self.client.growth_tasks(auth)
        except UpstreamError as exc:
            if G.is_growth_unavailable(exc):
                return {"uid": entry.uid, "realm": entry.realm, "ok": True,
                        "skipped": True, "detail": "成长体系不可用（企业版）"}
            return {"uid": entry.uid, "realm": entry.realm, "ok": False,
                    "detail": f"{exc.kind.value}: {exc.message}"}

        if not tasks:
            return {"uid": entry.uid, "realm": entry.realm, "ok": True,
                    "detail": "无任务"}

        accepted = prereq = claimed = failed = 0
        for task in tasks:
            code = task.task_code
            if not code:
                continue
            # accept：已登记则跳过（幂等），否则接受并**回读确认**
            if not task.accepted:
                try:
                    result = await self.client.accept_growth_task(auth, code)
                except UpstreamError as exc:
                    if G.is_prerequisite_not_met(exc):
                        prereq += 1
                        continue
                    failed += 1
                    continue
                if not result.ok:
                    # HTTP 200 但未登记 —— 上报不会归账，如实记为失败
                    if "prerequisite" in (result.message or "").lower():
                        prereq += 1
                    else:
                        failed += 1
                    continue
                accepted += 1
            # 达标即领奖（进度未满时领奖会被上游拒，不必试）
            if task.complete or task.accept_status == "completed":
                try:
                    await self.client.claim_growth_task(auth, code)
                    claimed += 1
                except UpstreamError:
                    # 已领过 / 未达标都是正常态
                    pass

        ok = accepted > 0 or claimed > 0 or (failed == 0)
        detail = (f"tasks={len(tasks)} accepted={accepted} claimed={claimed}"
                  f" prereq={prereq} failed={failed}")
        return {"uid": entry.uid, "realm": entry.realm, "ok": ok, "detail": detail}

    def _record(self, task: str, entry, outcome: dict) -> None:
        # 跳过（门控）与失败必须分开：跳过是**预期**的正常态（global / 企业版 /
        # 无凭证），算成失败会让台账天天报「全灭」并触发毫无意义的当日重试。
        if outcome.get("skipped"):
            detail = f"skipped: {outcome.get('detail', '')}"
        else:
            detail = str(outcome.get("detail", ""))
        self.db.execute(
            "INSERT INTO task_runs(ts, task, realm, account, ok, detail) VALUES(?,?,?,?,?,?)",
            (db_now(), task, entry.realm, entry.uid,
             1 if outcome.get("ok") else 0, detail[:300]),
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
                "balance_refresh_enabled": sched.balance_refresh_enabled,
                "balance_refresh_minutes": sched.balance_refresh_minutes,
                "include_disabled_in_tasks": sched.include_disabled_in_tasks,
                "jitter_minutes": sched.jitter_minutes,
            },
            "last_results": self.last_results[-20:],
        }
