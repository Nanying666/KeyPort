"""验证 captcha 参数时效（usable_ms）：过期参数不得递给上游。

对应实测结论：陈参数会被上游判 3007，故取出前必须丢弃。
这部分是纯逻辑（不碰浏览器），可确定性验证。
"""

from __future__ import annotations

import asyncio
import time

from keyport.upstream.captcha import CaptchaConfig, _Farm


def test_fresh_param_returned():
    f = _Farm()
    f.push("p1")
    assert f.take_fresh(40_000) == "p1"


def test_expired_param_discarded():
    """过期参数必须丢弃并返回 None，而不是把它递给上游换 3007。"""
    f = _Farm()
    f.push("stale")
    f.born[0] = time.monotonic() - 60  # 60s 前入队，超出 40s 时效
    assert f.take_fresh(40_000) is None
    assert f.discarded == 1
    assert f.queue.qsize() == 0


def test_fresh_count_excludes_stale():
    """fresh_count 是补货判据：池满但全陈时必须是 0，否则永不补货。"""
    f = _Farm()
    for p in ("a", "b", "c"):
        f.push(p)
    assert f.fresh_count(40_000) == 3

    # 全部变陈：数量仍是 3，但新鲜数是 0
    f.born.clear()
    f.born.extend([time.monotonic() - 120] * 3)
    assert f.queue.qsize() == 3
    assert f.fresh_count(40_000) == 0


def test_partial_stale_takes_fresh_one():
    """混合场景：队首陈、队尾新 —— 陈的被丢，新的能取到。"""
    f = _Farm()
    f.push("old")
    f.push("new")
    f.born[0] = time.monotonic() - 60
    assert f.take_fresh(40_000) == "new"
    assert f.discarded == 1
    assert f.queue.qsize() == 0


def test_push_rejects_when_full():
    """队列满时 push 返回 False，不阻塞、不覆盖已有参数。"""
    f = _Farm()
    f.queue = asyncio.Queue(maxsize=2)
    assert f.push("a") is True
    assert f.push("b") is True
    assert f.push("c") is False
    assert f.queue.qsize() == 2
    assert len(f.born) == 2


def test_born_stays_in_sync_with_queue():
    """born 与 queue 必须严格同步，否则时效判定会错位。"""
    f = _Farm()
    for p in ("a", "b", "c", "d"):
        f.push(p)
    assert len(f.born) == f.queue.qsize()

    f.take_fresh(40_000)
    assert len(f.born) == f.queue.qsize()

    f.born[0] = time.monotonic() - 99
    f.take_fresh(40_000)
    assert len(f.born) == f.queue.qsize()


def test_push_prunes_stale_but_keeps_fresh():
    """push 清理过期项，但绝不能吞掉新鲜参数（曾误用 take_fresh 实现）。"""
    f = _Farm()
    f.push("fresh1")
    f.push("fresh2")
    # 只让第一个变陈
    f.born[0] = time.monotonic() - 90

    f.push("fresh3", 40_000)

    # fresh1 被清掉，fresh2/fresh3 必须都还在
    assert f.queue.qsize() == 2
    assert f.discarded == 1
    assert f.take_fresh(40_000) == "fresh2"
    assert f.take_fresh(40_000) == "fresh3"


def test_push_does_not_grow_unbounded_when_idle():
    """农场停产后反复 push：队列不应膨胀（陈参数须被清掉）。"""
    f = _Farm()
    f.push("a")
    for _ in range(5):
        f.born.clear()
        f.born.append(time.monotonic() - 120)  # 全部变陈
        f.push("x", 40_000)
    # 每次都清掉旧的，故最终只剩最新那个
    assert f.queue.qsize() == 1
    assert f.discarded == 5


def test_usable_ms_default_and_override():
    assert CaptchaConfig().usable_ms == 40_000
    cfg = CaptchaConfig.from_dict({"usable_ms": 15_000, "unknown": 1})
    assert cfg.usable_ms == 15_000
