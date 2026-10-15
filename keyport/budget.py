"""当日积分预算闸（准入控制）。

## 动机

网关烧的是账号积分。一个跑飞的客户端、一个忘了关的脚本挂在网关上时，积分可能在
几小时内见底，而此前**没有任何东西会踩刹车** —— 只能事后从日报里发现。本闸在
当日累计扣费达到阈值后拒掉后续对话请求，把损失截断在阈值附近。

## 设计要点

- **默认关闭**（`limit=0`）：0 是「不限」的哨兵值，不是「未设置」，归一化不把它
  回落成默认。缺省行为与引入前逐字一致。
- **只统计观测到的扣费**：上游 `usage` 缺 `credit` 字段时**不计数**（缺失 ≠ 0）。
  把「没观测到」当 0 会让用量永远涨不上去、闸形同虚设。代价是当日用量是一个
  **下界** —— 真实扣费只会更多，故阈值要按保守值设。
- **按 CST 自然日重置**（与上游增长体系同口径；中国无夏令时，固定 +8 即可）。
- **进程内计数、重启清零**：不落盘。重启会让当日已用归零、闸短暂失效，这是刻意的
  取舍 —— 落盘的计数器要么引入与 state.json 竞争的写路径，要么在崩溃后不可信。
  **宁可重启后少挡一会儿，也不要一个会算错的钱闸。**
- **不改变选号**：本闸只决定「这一枪开不开」，不做「换便宜号」的降级 —— 后者会动到
  选号主路径的语义（粘性 / 分层 / 加权），是另一件事。
"""

from __future__ import annotations

import threading
import time

# CST（Asia/Shanghai）。中国无夏令时，固定 +8 即可。
CST_OFFSET_SECONDS = 8 * 3600


def cst_day(now: float | None = None) -> str:
    """返回该时刻落在哪个 CST 自然日（`YYYY-MM-DD`），用作计数器的归属日。"""
    ts = float(now if now is not None else time.time())
    return time.strftime("%Y-%m-%d", time.gmtime(ts + CST_OFFSET_SECONDS))


class DailyBudget:
    """当日积分预算闸（并发安全：对话请求会并发读它）。"""

    def __init__(self, limit: float = 0.0, *, now: float | None = None):
        self._lock = threading.Lock()
        self.limit = float(limit or 0.0)
        self._day = cst_day(now)
        self._used = 0.0
        self._rejected = 0

    # ---- 判定 ----

    def admit(self, now: float | None = None) -> bool:
        """判定是否放行一次对话请求，并对「拒绝」计数。

        这是个**有副作用**的方法，不是纯查询：闸必须能回答「今天挡了多少次」，
        否则运维只看到服务在拒请求、不知道是不是预算造成的（也可能是账号全挂了）。

        判据是**严格小于** —— `used == limit` 时已经到顶，不再放行。
        """
        with self._lock:
            self._roll(now)
            if self.limit <= 0:
                return True
            if self._used < self.limit:
                return True
            self._rejected += 1
            return False

    def add(self, credit: float, observed: bool = True,
            now: float | None = None) -> None:
        """累加一次请求观测到的扣费。

        `observed=False`（上游未给 credit）时不累加，但**照常滚日** —— 否则跨天后
        第一条无 credit 的请求不会触发重置，计数会一直挂在昨天。
        """
        with self._lock:
            self._roll(now)
            if not observed:
                return
            # 扣费恒 ≥ 0；上游字段异常给负值时宁可少算，也不让它倒扣当日用量
            # （倒扣会把已经关上的闸重新打开）。
            if credit and credit > 0:
                self._used += float(credit)

    def snapshot(self, now: float | None = None) -> dict:
        """返回当日已用、上限与拒绝次数（供管理台与 `/status` 透出）。"""
        with self._lock:
            self._roll(now)
            return {
                "day": self._day,
                "used": round(self._used, 6),
                "limit": self.limit,
                "rejected": self._rejected,
                "enabled": self.limit > 0,
            }

    # ---- 内部 ----

    def _roll(self, now: float | None) -> None:
        """跨日则归零。调用方必须已持锁。

        用**惰性滚动**（下次访问时发现日期变了就归零）而不是起一个 00:00 的定时器：
        定时器在重启后会丢，要么得自己再补一套调度；而「读的时候对一下日期」在任何
        访问路径上都成立，零后台开销、零新增协程。
        """
        today = cst_day(now)
        if today != self._day:
            self._day = today
            self._used = 0.0
            self._rejected = 0
