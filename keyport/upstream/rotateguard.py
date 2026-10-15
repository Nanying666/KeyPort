"""轮转退避与 WAF IP 级拦截 fail-fast。

## 为什么需要 IP 级 fail-fast

WAF 403 拦的是**网关出口 IP** 而不是账号（实测 3 个账号 1 秒内全部 403）。
账号级软冷却在这种情况下不够：轮转会把一次客户端请求**放大 N 倍**，同一出口 IP
继续打上游只会加重风控。

判定方式：在**短窗内数不同账号**。单号反复 403（账号级偶发）永不触发 —— 只数
不同号。两个不同号在 60s 内接连被拦（同一出口 IP）已是 IP 级证据，更早止损就
少放大一次轮转。

## 为什么轮转要退避

换号零间隔本身就是可被统计的节奏特征；而固定周期退避会让多请求同相位重试**再次
聚团**（WAF 频控按密度判罚），所以要施加抖动打散相位。
"""

from __future__ import annotations

import asyncio
import random
import threading
import time
from dataclasses import dataclass, field

# 轮转退避基数（对齐官方 CLI 的 500ms 形态）
BACKOFF_BASE = 0.5

# 轮转退避封顶（轮转上限默认 3 次，实际等待序列 500ms/1s；封顶只约束极端配置）
BACKOFF_CAP = 8.0

# 抖动比例（±25%，对齐官方 CLI `delay×(1±0.25)` 形态）
JITTER_FRACTION = 0.25

# IP 级判定滑动窗 + 激活时长：窗内不同账号命中 WAF 403 达阈值即判 IP 级拦截，
# 激活同样长（到期自然解除）。
WAF_IP_WINDOW = 60.0

# 判定阈值：窗内不同 UID 数达到该值激活。
#
# 取 2 —— 「多号」的最小定义：单号反复 403 永不触发（账号级偶发归软冷却管），
# 两个不同号在 60s 内接连被拦（同一出口 IP）已是 IP 级证据。
WAF_IP_THRESHOLD = 2


def jitter_duration(seconds: float) -> float:
    """给时长施加 ±JITTER_FRACTION 的均匀抖动。

    `<= 0` 原样返回（零等待不抖动）。抖动目的是打散多请求同相位重试 ——
    WAF 频控按密度判罚，齐步走的固定周期退避会再次聚团。
    """
    if seconds <= 0:
        return seconds
    factor = 1 + (random.random() * 2 - 1) * JITTER_FRACTION
    return max(0.0, seconds * factor)


def backoff_after(n: int, base: float = BACKOFF_BASE, cap: float = BACKOFF_CAP) -> float:
    """第 n 次轮转（0 基：首次失败换号前 n=0）前应等待的秒数。

    `base · 2^n` 封顶 `cap`，再施加 ±25% 抖动。`base <= 0` 时恒 0（关闭退避）。
    """
    value = base
    if value <= 0:
        return 0.0
    for _ in range(max(0, n)):
        if value >= cap:
            break
        value *= 2
    value = min(value, cap)
    return jitter_duration(value)


async def sleep_ctx(seconds: float, stop: asyncio.Event | None = None) -> bool:
    """可取消的等待：取消即返回 False（客户端断连/优雅停机不必等退避睡醒）。

    `seconds <= 0` 立即放行。
    """
    if seconds <= 0:
        return stop is None or not stop.is_set()
    if stop is None:
        await asyncio.sleep(seconds)
        return True
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except TimeoutError:
        return True
    return False


@dataclass
class WafIpGate:
    """WAF IP 级拦截状态机（进程内状态，重启清零 —— 窗口 60s，重建成本极低）。

    与账号级软冷却**协同不叠加**：账号级照常记账，IP 级状态只改变「是否继续轮转」。
    """

    hits: dict[str, float] = field(default_factory=dict)
    until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def note_waf(self, uid: str, now: float | None = None) -> bool:
        """记一次某账号的 WAF 403；返回记账后 IP 级拦截是否激活。

        调用方据此 fail-fast 终止轮转（优先于退避等待）。

        - 已激活：**不续期、不记账**（窗口期内不重置 —— 保守自然解除）；
        - 未激活：记 `hits[uid]`（同号重复命中覆盖不累计，判定口径是「不同号数」），
          剪掉窗外的过期命中；不同 UID 数达阈值 → 激活，并**清空判定窗**
          （解除后需全新命中重新判定，不叠旧账）。
        """
        now = float(now if now is not None else time.time())
        with self._lock:
            if now < self.until:
                return True
            self.hits[uid] = now
            for other, at in list(self.hits.items()):
                if now - at > WAF_IP_WINDOW:
                    self.hits.pop(other, None)
            if len(self.hits) >= WAF_IP_THRESHOLD:
                self.until = now + WAF_IP_WINDOW
                self.hits.clear()
                return True
            return False

    def active(self, now: float | None = None) -> bool:
        """IP 级拦截是否处于激活期（供末端错误文案与告警取值）。"""
        now = float(now if now is not None else time.time())
        with self._lock:
            return now < self.until
