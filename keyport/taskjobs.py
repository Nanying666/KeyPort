"""后台任务作业：账号级全量自动任务的异步执行与可查询状态。

## 为什么需要

调度器是**同步串行**的：一个账号卡在上游调用上，整轮任务都要等它。而「给某个账号
跑一遍全量自动任务」本质是个**长耗时**操作 —— 它要读服务端状态、逐项 accept、
逐项领奖、补签、抽奖。让 HTTP 请求等它做完既超时又浪费连接。

本模块把它变成**作业**：创建即返回，执行在后台，状态可随时查询。

## 生命周期

    pending → running → finished
                      ↘ interrupted（服务关闭 / 凭证变更 / 账号被移除）
                      ↘ error（执行器异常）

## 四条必须守住的纪律

1. **启动时把 pending/running 判为 interrupted**：进程上次退出时任务没跑完，
   磁盘上的「运行中」是**陈旧状态** —— 留着会让面板永远显示一个不动的进度条。
2. **同一账号同一时刻只有一个作业**：重复启动返回现有作业而不是再起一个。
   两个作业并发跑同一账号会**重复领奖**（上游的幂等键在每次调用时是新生成的，
   挡不住这个）。
3. **账号锁**：后台作业与手动的单项动作互斥，避免同账号被两条路径同时操作。
4. **取消在执行步骤之间观测**：正在飞的上游调用会跑完并**如实记录结果**，
   不能假装没发生（否则会漏记一次已发生的扣费/领奖）。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger("keyport.taskjobs")

MAX_JOBS = 256
MAX_JOB_RESULTS = 64

# 作业状态
PENDING = "pending"
RUNNING = "running"
FINISHED = "finished"
INTERRUPTED = "interrupted"
ERROR = "error"

LIVE_STATUSES = (PENDING, RUNNING)


class TaskJobError(Exception):
    """作业服务错误（带 HTTP 语义，供端点直接映射）。"""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.message = message
        self.status = status


class JobServiceNotStarted(TaskJobError):
    def __init__(self) -> None:
        super().__init__("后台任务服务尚未启动", 503)


class JobServiceStopped(TaskJobError):
    def __init__(self) -> None:
        super().__init__("后台任务服务正在关闭", 503)


class JobAccountNotFound(TaskJobError):
    def __init__(self) -> None:
        super().__init__("account not found", 404)


class JobAccountBusy(TaskJobError):
    def __init__(self) -> None:
        super().__init__("该账号有任务动作正在执行中，请等本轮结束后再试", 409)


class JobDisabled(TaskJobError):
    def __init__(self) -> None:
        super().__init__("自动成长任务未启用", 403)


class JobLimitReached(TaskJobError):
    def __init__(self) -> None:
        super().__init__("后台任务记录已满，且当前没有可清理的已完成记录", 429)


@dataclass
class TaskJob:
    """账号全量自动任务的可查询状态。

    `results` 沿用单项执行器的结果字段；`status` 只描述**后台作业生命周期**，
    不替代逐项执行结果 —— 两者是不同的维度。
    """

    id: str = ""
    uid: str = ""
    nickname: str = ""
    realm: str = ""
    trigger: str = "manual"
    status: str = PENDING
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: float | None = None
    current_task: str = ""
    completed: int = 0
    total: int = 0
    results: list[dict] = field(default_factory=list)
    message: str = ""
    error: str = ""

    @property
    def live(self) -> bool:
        return self.status in LIVE_STATUSES

    def clone(self) -> TaskJob:
        return TaskJob(
            **{**asdict(self),
               "results": [dict(r) for r in self.results]}
        )

    def to_dict(self) -> dict:
        out = asdict(self)
        return out


def upsert_result(job: TaskJob, task_code: str, result: dict | None) -> None:
    """按 task_code 就地更新结果（同 code 覆盖，不重复追加）。"""
    if job is None or not result:
        return
    code = task_code or str(result.get("task_code") or "")
    for i, existing in enumerate(job.results):
        if str(existing.get("task_code") or "") == code:
            job.results[i] = dict(result)
            return
    if len(job.results) >= MAX_JOB_RESULTS:
        return
    job.results.append(dict(result))


class TaskJobManager:
    """后台任务作业管理器。

    `runner` 由调用方注入：`runner(ctx, uid, report)`，其中 `report(kind, code, result)`
    用于上报进度。这样执行细节（走哪些上游端点）与本模块的生命周期管理解耦，
    测试可以注入假执行器。
    """

    def __init__(
        self,
        path: str | Path,
        runner: Callable[..., Any],
        *,
        total_actions: int = 0,
        concurrency: int = 2,
        max_jobs: int = MAX_JOBS,
    ):
        self.path = Path(path)
        self.runner = runner
        self.total_actions = max(0, int(total_actions))
        self.concurrency = max(1, int(concurrency))
        self.max_jobs = max(1, int(max_jobs))

        self._lock = threading.Lock()
        self._jobs: dict[str, TaskJob] = {}
        self._active: dict[str, str] = {}       # uid -> active job id
        self._locked_accounts: set[str] = set()  # 账号锁（后台与手动动作互斥）
        self._tasks: dict[str, asyncio.Task] = {}
        self._sem: asyncio.Semaphore | None = None
        self._closed = False
        self._started = False
        self._stop_event: asyncio.Event | None = None

    # ---- 生命周期 ----

    def start(self) -> None:
        """加载磁盘状态，并把陈旧的 pending/running 判为 interrupted。"""
        self._jobs = _read_jobs(self.path, self.max_jobs)
        now = time.time()
        changed = False
        for job in self._jobs.values():
            if job.live:
                job.status = INTERRUPTED
                job.message = "进程上次退出时任务未完成，等待恢复"
                job.error = ""
                job.current_task = ""
                job.updated_at = now
                job.finished_at = now
                changed = True
        if changed:
            self._persist()
        self._closed = False
        self._started = True

    async def stop(self) -> None:
        """取消待执行作业并等待当前（不可取消的）上游调用返回。

        返回后不再有后台任务协程写状态文件。
        """
        with self._lock:
            if self._closed:
                return
            self._closed = True
            tasks = list(self._tasks.values())

        for task in tasks:
            task.cancel()
        if tasks:
            # 正在飞的上游调用会跑完并**如实记录结果**，不能假装没发生
            await asyncio.gather(*tasks, return_exceptions=True)

        with self._lock:
            now = time.time()
            for job in self._jobs.values():
                if job.live:
                    job.status = INTERRUPTED
                    job.message = "服务关闭，任务已中断"
                    job.error = ""
                    job.current_task = ""
                    job.updated_at = now
                    job.finished_at = now
            self._tasks.clear()
            self._persist()

    # ---- 账号锁 ----

    def try_lock_account(self, uid: str) -> bool:
        """尝试独占账号；已被占用返回 False。"""
        with self._lock:
            if uid in self._locked_accounts:
                return False
            self._locked_accounts.add(uid)
            return True

    def unlock_account(self, uid: str) -> None:
        with self._lock:
            self._locked_accounts.discard(uid)

    def account_locked(self, uid: str) -> bool:
        with self._lock:
            return uid in self._locked_accounts

    # ---- 启动作业 ----

    def start_job(
        self, uid: str, *, trigger: str = "manual", nickname: str = "",
        realm: str = "", auto_enabled: bool = True,
    ) -> tuple[TaskJob, bool]:
        """创建作业并立即返回 `(作业, 是否新建)`。

        - 同一账号已有活动作业 → 返回现有作业（`started=False`），**不再起一个**
        - 账号被其它动作锁住 → 抛 `JobAccountBusy`
        - `trigger != manual` 且自动任务未启用 → 抛 `JobDisabled`
        """
        if not uid:
            raise JobAccountNotFound()
        if trigger != "manual" and not auto_enabled:
            raise JobDisabled()

        with self._lock:
            if not self._started:
                raise JobServiceNotStarted()
            if self._closed:
                raise JobServiceStopped()

            if uid in self._active:
                existing = self._jobs.get(uid)
                if existing is not None:
                    return existing.clone(), False

            existing = self._jobs.get(uid)
            if existing is not None and existing.live:
                return existing.clone(), False

            if not self._try_lock_locked(uid):
                raise JobAccountBusy()

            evicted: tuple[str, TaskJob] | None = None
            if uid not in self._jobs and len(self._jobs) >= self.max_jobs:
                evicted = self._evict_oldest_locked()
                if evicted is None:
                    self._locked_accounts.discard(uid)
                    raise JobLimitReached()

            now = time.time()
            job = TaskJob(
                id=uuid.uuid4().hex,
                uid=uid, nickname=nickname, realm=realm, trigger=trigger,
                status=PENDING, started_at=now, updated_at=now,
                current_task="等待后台执行", total=self.total_actions,
                results=[], message="任务已排队",
            )
            previous = self._jobs.get(uid)
            self._jobs[uid] = job
            self._active[uid] = job.id
            try:
                self._persist()
            except OSError as exc:
                self._active.pop(uid, None)
                if previous is not None:
                    self._jobs[uid] = previous
                else:
                    self._jobs.pop(uid, None)
                if evicted is not None:
                    self._jobs[evicted[0]] = evicted[1]
                self._locked_accounts.discard(uid)
                raise TaskJobError(f"persist task job: {exc}") from exc

        task = asyncio.create_task(self._run(uid, job.id))
        with self._lock:
            self._tasks[job.id] = task
        task.add_done_callback(lambda _t: self._forget(job.id))
        return job.clone(), True

    def _forget(self, job_id: str) -> None:
        with self._lock:
            self._tasks.pop(job_id, None)

    def resume(self) -> int:
        """恢复上次退出时未完成的作业；返回恢复数量。

        执行器会**先回读服务端状态**，已领奖的项目在内部跳过动作，不会重复领奖。
        """
        with self._lock:
            if not self._started or self._closed:
                return 0
            pending = [job for job in self._jobs.values() if job.status == INTERRUPTED]
            pending.sort(key=lambda j: j.updated_at)
            resumed = 0
            now = time.time()
            for job in pending:
                uid = job.uid
                if not self._try_lock_locked(uid):
                    # 其他任务当前持有账号锁；保持 interrupted，后续重启仍可恢复
                    continue
                job.status = PENDING
                job.current_task = "等待后台恢复"
                job.message = "正在恢复未完成任务"
                job.error = ""
                job.updated_at = now
                job.finished_at = None
                self._active[uid] = job.id
                self._persist()
                task = asyncio.create_task(self._run(uid, job.id))
                self._tasks[job.id] = task
                task.add_done_callback(lambda _t, jid=job.id: self._forget(jid))
                resumed += 1
        return resumed

    def _try_lock_locked(self, uid: str) -> bool:
        """已持锁版本的账号锁。"""
        if uid in self._locked_accounts:
            return False
        self._locked_accounts.add(uid)
        return True

    # ---- 查询 ----

    def list_jobs(self) -> list[TaskJob]:
        """返回每个账号最新的作业，按更新时间从新到旧。"""
        with self._lock:
            jobs = [j.clone() for j in self._jobs.values()]
        jobs.sort(key=lambda j: j.updated_at, reverse=True)
        return jobs

    def get_job(self, uid: str) -> TaskJob | None:
        with self._lock:
            job = self._jobs.get(uid)
            return job.clone() if job is not None else None

    # ---- 执行 ----

    async def _run(self, uid: str, job_id: str) -> None:
        try:
            if self._sem is None:
                self._sem = asyncio.Semaphore(self.concurrency)
            async with self._sem:
                if self._closed:
                    self._finish(uid, job_id, INTERRUPTED, "服务关闭，任务已中断")
                    return
                with self._lock:
                    job = self._jobs.get(uid)
                    if job is None or job.id != job_id:
                        return
                    job.status = RUNNING
                    job.current_task = "读取服务端任务状态"
                    job.message = "后台任务正在执行"
                    job.updated_at = time.time()
                    self._persist()

                def report(kind: str, code: str = "",
                           result: dict | None = None, completed: bool = False) -> None:
                    self._observe(uid, job_id, kind, code, result, completed)

                await self.runner(uid, report)

                if self._closed:
                    self._finish(uid, job_id, INTERRUPTED, "服务关闭，任务已中断")
                else:
                    self._finish(uid, job_id, FINISHED, "全部可自动任务已处理")
        except asyncio.CancelledError:
            # 正在飞的上游调用已跑完并记录；这里只标记状态
            self._finish(uid, job_id, INTERRUPTED, "服务关闭，任务已中断")
            raise
        except Exception as exc:  # noqa: BLE001 - 执行器异常不该打穿后台协程
            log.warning("后台任务异常 uid=%s: %s", uid, exc)
            self._finish(uid, job_id, ERROR, f"后台任务异常: {exc}")
        finally:
            self.unlock_account(uid)
            with self._lock:
                self._active.pop(uid, None)

    def _observe(self, uid: str, job_id: str, kind: str, code: str,
                 result: dict | None, completed: bool) -> None:
        with self._lock:
            job = self._jobs.get(uid)
            if job is None or job.id != job_id:
                return
            if kind in ("phase", "task_start"):
                job.current_task = code
                job.message = "后台任务正在执行"
            elif kind == "result":
                if result:
                    upsert_result(job, code, result)
                if completed:
                    job.completed = _count_completed(job.results)
                job.current_task = ""
            job.updated_at = time.time()
            self._persist()

    def _finish(self, uid: str, job_id: str, status: str, message: str) -> None:
        with self._lock:
            job = self._jobs.get(uid)
            if job is None or job.id != job_id:
                return
            job.status = status
            job.message = message
            job.current_task = ""
            job.updated_at = time.time()
            job.finished_at = job.updated_at
            job.error = message if status == ERROR else ""
            self._active.pop(uid, None)
            self._persist()

    # ---- 持久化 ----

    def _evict_oldest_locked(self) -> tuple[str, TaskJob] | None:
        """淘汰最旧的已完成作业；没有可淘汰的返回 None。"""
        candidate: str | None = None
        oldest = 0.0
        for uid, job in self._jobs.items():
            if uid in self._active or job.live:
                continue
            if candidate is None or job.updated_at < oldest:
                candidate, oldest = uid, job.updated_at
        if candidate is None:
            return None
        return candidate, self._jobs.pop(candidate)

    def _persist(self) -> None:
        """原子落盘（tmp + rename）—— 崩溃不会留下半截文件。"""
        jobs = sorted(self._jobs.values(), key=lambda j: j.updated_at, reverse=True)
        jobs = jobs[: self.max_jobs]
        payload = {"version": 1, "jobs": [j.to_dict() for j in jobs]}
        raw = json.dumps(payload, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".task-jobs-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(raw)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def _count_completed(results: list[dict]) -> int:
    """已完成的**去重**任务数（同 code 只算一次）。"""
    codes = {str(r.get("task_code") or "") for r in results}
    codes.discard("")
    return len(codes)


def _read_jobs(path: Path, max_jobs: int) -> dict[str, TaskJob]:
    """读回作业状态；文件不存在或损坏时返回空（零状态启动，不崩溃）。"""
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, FileNotFoundError):
        return {}
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(payload, dict):
        return {}

    jobs: dict[str, TaskJob] = {}
    for item in payload.get("jobs") or []:
        if not isinstance(item, dict):
            continue
        uid = str(item.get("uid") or "")
        job_id = str(item.get("id") or "")
        if not uid or not job_id:
            continue
        results = [r for r in (item.get("results") or []) if isinstance(r, dict)]
        if len(results) > MAX_JOB_RESULTS:
            results = results[-MAX_JOB_RESULTS:]
        jobs[uid] = TaskJob(
            id=job_id, uid=uid,
            nickname=str(item.get("nickname") or ""),
            realm=str(item.get("realm") or ""),
            trigger=str(item.get("trigger") or "manual"),
            status=str(item.get("status") or FINISHED),
            started_at=float(item.get("started_at") or 0),
            updated_at=float(item.get("updated_at") or 0),
            finished_at=(float(item["finished_at"])
                         if item.get("finished_at") is not None else None),
            current_task=str(item.get("current_task") or ""),
            completed=int(item.get("completed") or 0),
            total=int(item.get("total") or 0),
            results=results,
            message=str(item.get("message") or ""),
            error=str(item.get("error") or ""),
        )
    if len(jobs) > max_jobs:
        ordered = sorted(jobs.values(), key=lambda j: j.updated_at, reverse=True)
        jobs = {j.uid: j for j in ordered[:max_jobs]}
    return jobs
