"""后台任务作业：异步执行与可查询状态。

移植自 misakano7545/workbuddy2api-panel 的 `internal/panel/taskjobs.go`。
"""

from __future__ import annotations

import asyncio
import itertools
import json
from pathlib import Path

import pytest
from conftest import make_test_config

from keyport.app import build_context, create_app
from keyport.taskjobs import (
    ERROR,
    FINISHED,
    INTERRUPTED,
    MAX_JOB_RESULTS,
    PENDING,
    RUNNING,
    JobAccountBusy,
    JobDisabled,
    JobLimitReached,
    JobServiceNotStarted,
    TaskJob,
    TaskJobManager,
    upsert_result,
)
from keyport.upstream.base import AccountAuth


class _Runner:
    """可控的假执行器：记录调用，按需阻塞 / 抛错 / 上报结果。"""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.gate: asyncio.Event | None = None
        self.raise_exc: Exception | None = None
        self.results: list[tuple[str, dict]] = []

    async def __call__(self, uid: str, report) -> None:
        self.calls.append(uid)
        report("task_start", "checkin")
        if self.gate is not None:
            await self.gate.wait()
        if self.raise_exc is not None:
            raise self.raise_exc
        for code, result in self.results:
            report("result", code, result, True)


def _manager(tmp_path: Path, runner, **kw) -> TaskJobManager:
    m = TaskJobManager(tmp_path / "task-jobs.json", runner, total_actions=5, **kw)
    m.start()
    return m


# ---- 生命周期 ----


@pytest.mark.asyncio
async def test_start_job_runs_to_finished(tmp_path: Path):
    runner = _Runner()
    runner.results = [("checkin", {"task_code": "checkin", "ok": True})]
    m = _manager(tmp_path, runner)
    job, started = m.start_job("u1")
    assert started is True
    assert job.status == PENDING
    assert job.total == 5

    await asyncio.sleep(0.05)
    got = m.get_job("u1")
    assert got.status == FINISHED
    assert got.completed == 1
    assert got.message == "全部可自动任务已处理"
    assert got.finished_at is not None


@pytest.mark.asyncio
async def test_duplicate_start_returns_existing(tmp_path: Path):
    """同一账号同一时刻只有一个作业 —— 两个作业并发会**重复领奖**。

    上游的幂等键在每次调用时是新生成的，挡不住这个。
    """
    runner = _Runner()
    runner.gate = asyncio.Event()
    m = _manager(tmp_path, runner)

    first, started1 = m.start_job("u1")
    second, started2 = m.start_job("u1")
    assert started1 is True
    assert started2 is False
    assert first.id == second.id
    assert len(runner.calls) <= 1

    runner.gate.set()
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_busy_when_account_locked(tmp_path: Path):
    """后台作业与手动单项动作互斥。"""
    m = _manager(tmp_path, _Runner())
    assert m.try_lock_account("u1") is True
    with pytest.raises(JobAccountBusy):
        m.start_job("u1")
    m.unlock_account("u1")
    _job, started = m.start_job("u1")
    assert started is True
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_runner_exception_marks_error(tmp_path: Path):
    runner = _Runner()
    runner.raise_exc = RuntimeError("boom")
    m = _manager(tmp_path, runner)
    m.start_job("u1")
    await asyncio.sleep(0.05)
    got = m.get_job("u1")
    assert got.status == ERROR
    assert "boom" in got.error


@pytest.mark.asyncio
async def test_stop_marks_interrupted_and_waits(tmp_path: Path):
    """停止时等正在飞的上游调用返回并把结果记完，不假装没发生。"""
    runner = _Runner()
    runner.gate = asyncio.Event()
    m = _manager(tmp_path, runner)
    m.start_job("u1")
    await asyncio.sleep(0.02)
    assert m.get_job("u1").status == RUNNING

    async def _release():
        await asyncio.sleep(0.02)
        runner.gate.set()

    asyncio.create_task(_release())
    await m.stop()

    got = m.get_job("u1")
    assert got.status == INTERRUPTED
    assert "服务关闭" in got.message


@pytest.mark.asyncio
async def test_stop_is_idempotent(tmp_path: Path):
    m = _manager(tmp_path, _Runner())
    await m.stop()
    await m.stop()


@pytest.mark.asyncio
async def test_start_after_stop_rejected(tmp_path: Path):
    m = _manager(tmp_path, _Runner())
    await m.stop()
    with pytest.raises(Exception, match="正在关闭"):
        m.start_job("u1")


def test_start_before_start_raises(tmp_path: Path):
    m = TaskJobManager(tmp_path / "j.json", _Runner())
    with pytest.raises(JobServiceNotStarted):
        m.start_job("u1")


# ---- 触发方式门控 ----


@pytest.mark.asyncio
async def test_non_manual_trigger_requires_auto_enabled(tmp_path: Path):
    m = _manager(tmp_path, _Runner())
    with pytest.raises(JobDisabled):
        m.start_job("u1", trigger="new_account", auto_enabled=False)
    _job, started = m.start_job("u1", trigger="new_account", auto_enabled=True)
    assert started is True
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_manual_trigger_ignores_auto_flag(tmp_path: Path):
    """手动触发不受自动任务开关限制。"""
    m = _manager(tmp_path, _Runner())
    _job, started = m.start_job("u1", trigger="manual", auto_enabled=False)
    assert started is True
    await asyncio.sleep(0.05)


# ---- 启动恢复 ----


def test_start_marks_stale_live_as_interrupted(tmp_path: Path):
    """进程上次退出时任务没跑完 —— 磁盘上的「运行中」是**陈旧状态**。

    留着会让面板永远显示一个不动的进度条。
    """
    path = tmp_path / "task-jobs.json"
    path.write_text(json.dumps({"version": 1, "jobs": [
        {"id": "a" * 32, "uid": "u1", "status": "running",
         "started_at": 100, "updated_at": 200, "message": "x"},
        {"id": "b" * 32, "uid": "u2", "status": "pending",
         "started_at": 100, "updated_at": 150},
        {"id": "c" * 32, "uid": "u3", "status": "finished",
         "started_at": 100, "updated_at": 120, "message": "done"},
    ]}), encoding="utf-8")

    m = TaskJobManager(path, _Runner())
    m.start()
    assert m.get_job("u1").status == INTERRUPTED
    assert m.get_job("u2").status == INTERRUPTED
    # 已完成的作业不动
    assert m.get_job("u3").status == FINISHED
    assert m.get_job("u3").message == "done"


def test_start_persists_healed_status(tmp_path: Path):
    """修正要立刻物化回磁盘，否则每次启动都重打一遍日志。"""
    path = tmp_path / "task-jobs.json"
    path.write_text(json.dumps({"version": 1, "jobs": [
        {"id": "a" * 32, "uid": "u1", "status": "running",
         "started_at": 100, "updated_at": 200},
    ]}), encoding="utf-8")
    TaskJobManager(path, _Runner()).start()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["jobs"][0]["status"] == INTERRUPTED


@pytest.mark.asyncio
async def test_resume_restarts_interrupted(tmp_path: Path):
    path = tmp_path / "task-jobs.json"
    path.write_text(json.dumps({"version": 1, "jobs": [
        {"id": "a" * 32, "uid": "u1", "status": "interrupted",
         "started_at": 100, "updated_at": 200},
    ]}), encoding="utf-8")
    runner = _Runner()
    m = TaskJobManager(path, runner, total_actions=5)
    m.start()
    assert m.resume() == 1
    await asyncio.sleep(0.05)
    assert runner.calls == ["u1"]
    assert m.get_job("u1").status == FINISHED


@pytest.mark.asyncio
async def test_resume_skips_locked_account(tmp_path: Path):
    """账号被别的动作锁住 → 保持 interrupted，后续重启仍可恢复。"""
    path = tmp_path / "task-jobs.json"
    path.write_text(json.dumps({"version": 1, "jobs": [
        {"id": "a" * 32, "uid": "u1", "status": "interrupted",
         "started_at": 100, "updated_at": 200},
    ]}), encoding="utf-8")
    m = TaskJobManager(path, _Runner())
    m.start()
    m.try_lock_account("u1")
    assert m.resume() == 0
    assert m.get_job("u1").status == INTERRUPTED


def test_read_corrupt_file_is_empty(tmp_path: Path):
    """文件损坏 → 零状态启动，不崩溃。"""
    path = tmp_path / "task-jobs.json"
    path.write_text("{not json", encoding="utf-8")
    m = TaskJobManager(path, _Runner())
    m.start()
    assert m.list_jobs() == []


def test_read_missing_file_is_empty(tmp_path: Path):
    m = TaskJobManager(tmp_path / "nope.json", _Runner())
    m.start()
    assert m.list_jobs() == []


def test_read_skips_entries_without_id_or_uid(tmp_path: Path):
    path = tmp_path / "task-jobs.json"
    path.write_text(json.dumps({"version": 1, "jobs": [
        {"id": "", "uid": "u1"},
        {"id": "x" * 32, "uid": ""},
        {"id": "y" * 32, "uid": "u2", "status": "finished"},
    ]}), encoding="utf-8")
    m = TaskJobManager(path, _Runner())
    m.start()
    assert [j.uid for j in m.list_jobs()] == ["u2"]


# ---- 结果累积 ----


def test_upsert_result_replaces_same_code():
    """同 task_code 覆盖而非追加 —— 否则重跑会把结果列表刷爆。"""
    job = TaskJob()
    upsert_result(job, "checkin", {"task_code": "checkin", "ok": True})
    upsert_result(job, "checkin", {"task_code": "checkin", "ok": False})
    assert len(job.results) == 1
    assert job.results[0]["ok"] is False


def test_upsert_result_caps_length():
    job = TaskJob()
    for i in range(MAX_JOB_RESULTS + 10):
        upsert_result(job, f"t{i}", {"task_code": f"t{i}"})
    assert len(job.results) == MAX_JOB_RESULTS


def test_upsert_result_falls_back_to_payload_code():
    job = TaskJob()
    upsert_result(job, "", {"task_code": "travel", "ok": True})
    assert job.results[0]["task_code"] == "travel"


def test_upsert_result_ignores_empty():
    job = TaskJob()
    upsert_result(job, "x", None)
    upsert_result(job, "x", {})
    assert job.results == []


@pytest.mark.asyncio
async def test_completed_counts_deduped_codes(tmp_path: Path):
    runner = _Runner()
    runner.results = [("checkin", {"task_code": "checkin"}),
                      ("travel", {"task_code": "travel"})]
    m = _manager(tmp_path, runner)
    m.start_job("u1")
    await asyncio.sleep(0.05)
    assert m.get_job("u1").completed == 2


# ---- 淘汰 ----


@pytest.mark.asyncio
async def test_evicts_oldest_finished_when_full(tmp_path: Path):
    m = _manager(tmp_path, _Runner(), max_jobs=2)
    for uid in ("u1", "u2"):
        m.start_job(uid)
        await asyncio.sleep(0.03)
    assert len(m.list_jobs()) == 2

    m.start_job("u3")
    await asyncio.sleep(0.03)
    assert len(m.list_jobs()) == 2
    assert m.get_job("u3") is not None


@pytest.mark.asyncio
async def test_raises_when_no_evictable(tmp_path: Path):
    """全在跑且已达上限 → 拒绝（而不是静默丢掉一个正在跑的）。"""
    runner = _Runner()
    runner.gate = asyncio.Event()
    m = _manager(tmp_path, runner, max_jobs=1)
    m.start_job("u1")
    await asyncio.sleep(0.02)
    with pytest.raises(JobLimitReached):
        m.start_job("u2")
    runner.gate.set()
    await asyncio.sleep(0.05)


# ---- 查询 ----


@pytest.mark.asyncio
async def test_list_sorted_newest_first(tmp_path: Path):
    m = _manager(tmp_path, _Runner())
    for uid in ("u1", "u2", "u3"):
        m.start_job(uid)
        await asyncio.sleep(0.03)
    jobs = m.list_jobs()
    assert [j.uid for j in jobs] == ["u3", "u2", "u1"]
    assert all(j.updated_at >= k.updated_at
               for j, k in itertools.pairwise(jobs))


@pytest.mark.asyncio
async def test_get_job_returns_clone(tmp_path: Path):
    """返回副本 —— 调用方改动不该污染管理器内部状态。"""
    m = _manager(tmp_path, _Runner())
    m.start_job("u1", nickname="nick")
    got = m.get_job("u1")
    got.nickname = "tampered"
    got.results.append({"x": 1})
    assert m.get_job("u1").nickname == "nick"
    assert m.get_job("u1").results == []
    await asyncio.sleep(0.05)


# ---- 并发闸 ----


@pytest.mark.asyncio
async def test_concurrency_limit(tmp_path: Path):
    runner = _Runner()
    runner.gate = asyncio.Event()
    m = _manager(tmp_path, runner, concurrency=1)
    m.start_job("u1")
    m.start_job("u2")
    await asyncio.sleep(0.05)
    # 只有一个能进入执行
    assert len(runner.calls) == 1
    runner.gate.set()
    await asyncio.sleep(0.05)


# ---- 持久化原子性 ----


def test_persist_is_atomic(tmp_path: Path):
    """tmp + rename —— 崩溃不会留下半截文件。"""
    import asyncio

    async def _run() -> None:
        m = _manager(tmp_path, _Runner())
        m.start_job("u1")
        await asyncio.sleep(0.05)
        path = tmp_path / "task-jobs.json"
        assert path.is_file()
        # 目录里不该留下临时文件
        leftovers = [p.name for p in tmp_path.iterdir()
                     if p.name.startswith(".task-jobs-")]
        assert leftovers == []
        json.loads(path.read_text(encoding="utf-8"))
        await m.stop()

    asyncio.run(_run())


# ---- 端点 ----


def _app(tmp_path: Path):
    from keyport.security import hash_api_key

    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    cfg.install.enabled = False
    cfg.claim.auto_on_add = False
    app = create_app(cfg)
    ctx = app.state.ctx
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T",
                                       nickname="nick"))
    key = "sk-kp-jobs"
    ctx.db.execute(
        "INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow, "
        "quota_credits, quota_tokens, ip_allow, ip_block, note, created_at) "
        "VALUES(?,?,?,1,'','[]',0,0,'[]','[]','',0)",
        ("j", hash_api_key(key), key[:12]),
    )
    return app, ctx


def test_endpoints(tmp_path: Path, caplog):
    import re

    from fastapi.testclient import TestClient

    app, _ctx = _app(tmp_path)
    with TestClient(app) as tc:
        logs = "\n".join(r.getMessage() for r in caplog.records)
        match = re.search(r"面板初始密码[^：]*：(\S+)", logs)
        assert match, "未取到初始密码"
        login = tc.post("/api/login",
                        json={"username": "admin", "password": match.group(1)})
        token = login.cookies.get("kp_session")
        assert token, login.text
        headers = {"x-kp-session": token}

        resp = tc.get("/api/task-jobs", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["jobs"] == []

        resp = tc.post("/api/task-jobs/u1", headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["started"] is True
        assert body["job"]["uid"] == "u1"
        assert body["job"]["nickname"] == "nick"

        # 重复启动返回现有作业
        resp = tc.post("/api/task-jobs/u1", headers=headers)
        assert resp.json()["started"] is False

        resp = tc.get("/api/task-jobs/u1", headers=headers)
        assert resp.status_code == 200
        assert resp.json()["job"]["uid"] == "u1"


def test_endpoint_unknown_account(tmp_path: Path, caplog):
    import re

    from fastapi.testclient import TestClient

    app, _ctx = _app(tmp_path)
    with TestClient(app) as tc:
        logs = "\n".join(r.getMessage() for r in caplog.records)
        match = re.search(r"面板初始密码[^：]*：(\S+)", logs)
        login = tc.post("/api/login",
                        json={"username": "admin", "password": match.group(1)})
        token = login.cookies.get("kp_session")
        resp = tc.post("/api/task-jobs/nope", headers={"x-kp-session": token})
        assert resp.status_code == 404


@pytest.mark.asyncio
@pytest.mark.asyncio
async def test_scheduler_runner_reports_progress(tmp_path: Path):
    """调度器的全量执行器按任务上报进度（含门控跳过）。"""
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="global",
                                       access_token="T"))

    from keyport.scheduler import TASKS

    events: list[tuple] = []

    async def _fake_run_for(task, entry):
        return {"uid": entry.uid, "ok": True, "detail": f"{task}-ok"}

    ctx.scheduler._run_for = _fake_run_for  # type: ignore[method-assign]
    await ctx.scheduler.run_account_tasks(
        "u1", lambda kind, code="", result=None, completed=False:
        events.append((kind, code, result, completed)))

    # 每个任务都上报一次结果（global 账号的国内任务被门控跳过，但 skipped 也是正常态）
    results = [e for e in events if e[0] == "result"]
    assert len(results) == len(TASKS)
    assert all(r[3] is True for r in results)          # completed=True
    assert [r[1] for r in results] == list(TASKS)      # 任务码顺序一致
    assert any("skipped" in str(r[2]) for r in results)
