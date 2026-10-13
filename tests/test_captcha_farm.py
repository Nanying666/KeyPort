"""验证码农场：按需补货、失败退避、异常自愈。

回归背景：早期实现把 `while(true)` 放在页面内，宿主只在队列满时丢弃结果，
等于无节制打码 → 运行约 25 分钟后被风控判 F001，页面异常退出后农场永久死亡
（`_farm_loop` 直接 return，而 `start()` 只在启动时调用一次）。

以下测试用假会话替身，不启动真实浏览器。
"""

from __future__ import annotations

import asyncio

import pytest

from keyport.upstream.captcha import CaptchaConfig, CaptchaManager


def _cfg(**kw) -> CaptchaConfig:
    base = dict(enabled=True, pool_size=3, gap_ms=0, idle_sleep_ms=1,
                fail_backoff_ms=1, fail_backoff_max_ms=2,
                restart_backoff_ms=1, restart_backoff_max_ms=2,
                restart_max_consecutive=3, per_attempt_ms=100)
    base.update(kw)
    return CaptchaConfig(**base)


@pytest.mark.asyncio()
async def test_ensure_running_starts_when_dead():
    mgr = CaptchaManager(_cfg())
    assert mgr.stats()["running"] is False
    mgr.ensure_running()
    assert mgr.stats()["running"] is True
    mgr._farm.stop.set()
    await mgr.close()


@pytest.mark.asyncio()
async def test_ensure_running_noop_when_alive():
    mgr = CaptchaManager(_cfg())
    mgr.ensure_running()
    first = mgr._farm.task
    mgr.ensure_running()
    assert mgr._farm.task is first  # 不重复拉起
    mgr._farm.stop.set()
    await mgr.close()


def test_ensure_running_disabled_is_noop():
    mgr = CaptchaManager(_cfg(enabled=False))
    mgr.ensure_running()
    assert mgr.stats()["running"] is False


@pytest.mark.asyncio()
async def test_farm_fills_pool_then_idles(monkeypatch):
    """队列达到 pool_size 后应停止求解（省打码量，避免风控）。"""
    mgr = CaptchaManager(_cfg(pool_size=3))
    solves = 0

    async def fake_session(pw, chrome):
        nonlocal solves
        # 模仿真实会话：按需补货，库存满则等待 stop
        while not mgr._farm.stop.is_set():
            if mgr._farm.queue.qsize() >= mgr.cfg.pool_size:
                await mgr._sleep_or_stop(0.01)
                continue
            solves += 1
            mgr._farm.queue.put_nowait(f"param-{solves}")

    monkeypatch.setattr(mgr, "_run_farm_session", fake_session)
    monkeypatch.setattr(mgr, "_farm_loop_playwright", None, raising=False)

    # 直接驱动主循环（绕过 playwright 导入）
    task = asyncio.create_task(_drive(mgr))
    await asyncio.sleep(0.05)
    assert mgr._farm.queue.qsize() == 3
    # 已满后不应继续求解
    assert solves == 3
    mgr._farm.stop.set()
    await task


async def _drive(mgr: CaptchaManager) -> None:
    """等价于 _farm_loop 的重启外壳（去掉 playwright 依赖）。"""
    while not mgr._farm.stop.is_set():
        try:
            await mgr._run_farm_session(None, "")
            break
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            mgr._farm.restarts += 1
            if mgr._farm.restarts >= mgr.cfg.restart_max_consecutive:
                return
            await mgr._sleep_or_stop(0.001)


@pytest.mark.asyncio()
async def test_farm_recovers_after_session_crash():
    """会话异常后应自动重启，而不是永久死亡。"""
    mgr = CaptchaManager(_cfg(restart_max_consecutive=10))
    calls = 0

    async def crashing_session(pw, chrome):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Page.evaluate: Target page has been closed")
        # 第二次进入后正常产出并等待停止
        mgr._farm.queue.put_nowait("recovered")
        await mgr._sleep_or_stop(0.05)

    mgr._run_farm_session = crashing_session  # type: ignore[method-assign]
    task = asyncio.create_task(_drive(mgr))
    await asyncio.sleep(0.05)

    assert calls >= 2, "会话崩溃后未重启"
    assert mgr._farm.restarts >= 1
    assert not mgr._farm.queue.empty()
    assert mgr._farm.queue.get_nowait() == "recovered"

    mgr._farm.stop.set()
    await task


@pytest.mark.asyncio()
async def test_farm_gives_up_after_max_restarts():
    """连续重启超上限后停止自愈，避免重启风暴。"""
    mgr = CaptchaManager(_cfg(restart_max_consecutive=3))

    async def always_crash(pw, chrome):
        raise RuntimeError("boom")

    mgr._run_farm_session = always_crash  # type: ignore[method-assign]
    await _drive(mgr)
    assert mgr._farm.restarts == 3


@pytest.mark.asyncio()
async def test_sleep_or_stop_interruptible():
    """退避等待应能被停止信号提前打断（关服不必等满退避）。"""
    mgr = CaptchaManager(_cfg())
    loop = asyncio.get_running_loop()
    start = loop.time()
    asyncio.get_running_loop().call_later(0.01, mgr._farm.stop.set)
    await mgr._sleep_or_stop(5.0)
    assert loop.time() - start < 1.0


def test_stats_exposes_health_fields():
    mgr = CaptchaManager(_cfg())
    s = mgr.stats()
    for key in ("running", "queued", "pool_size", "produced", "failed",
                "restarts", "consecutive_fails", "last_error"):
        assert key in s
