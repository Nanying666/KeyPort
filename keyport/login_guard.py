"""面板登录节流：按 IP + 全局双维度的失败计数与锁定。

## 为什么必须有

原先 `/api/login` **没有任何速率限制**：可以无限次猜密码。实测这次的入侵
（IP 152.70.234.167）就是在 63 秒内连打多次后拿到会话的。缺了这道闸，
「密码够不够强」就成了唯一防线 —— 而密码泄露是常态（复用、录入日志、被看到）。

## 两道闸，方向不同

- **按 IP**：挡针对单个来源的爆破。失败累计到阈值即锁定，逐次翻倍（封顶）。
- **全局**：挡「分布式换 IP 爆破」。攻击者可以用代理池换源地址绕过按 IP 的闸，
  但**总失败数**绕不过去。

## 为什么锁定期间不验证密码

`verify_password` 是 PBKDF2 210,000 次迭代（约几十毫秒 CPU）。若无节制地调用，
无限次尝试本身就是一次 **CPU 打满的 DoS**。锁定期间直接拒绝，不碰密码摘要。

## 进程内状态

不落盘：重启清零。这是刻意的取舍 —— 落盘要么引入与 state.json 竞争的写路径，
要么崩溃后不可信。宁可重启后少挡一会儿，也不要一个会算错的闸。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

#: 默认阈值：窗口内失败达到该次数即锁定
DEFAULT_MAX_FAILURES = 5
#: 失败计数窗口
DEFAULT_WINDOW_SECONDS = 300.0
#: 首次锁定时长；再犯逐次翻倍
DEFAULT_LOCKOUT_SECONDS = 900.0
#: 锁定时长封顶
DEFAULT_MAX_LOCKOUT_SECONDS = 86400.0
#: 全局维度阈值（防换 IP 绕过）：同一窗口内所有来源的失败总数
DEFAULT_GLOBAL_MAX_FAILURES = 30


@dataclass
class _Bucket:
    """单个维度的失败记录。"""

    failures: list[float] = field(default_factory=list)
    locked_until: float = 0.0
    strikes: int = 0


class LoginGuard:
    """登录失败节流器（并发安全）。"""

    def __init__(
        self,
        *,
        max_failures: int = DEFAULT_MAX_FAILURES,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        lockout_seconds: float = DEFAULT_LOCKOUT_SECONDS,
        max_lockout_seconds: float = DEFAULT_MAX_LOCKOUT_SECONDS,
        global_max_failures: int = DEFAULT_GLOBAL_MAX_FAILURES,
    ):
        self._lock = threading.Lock()
        self.max_failures = max(1, int(max_failures))
        self.window_seconds = float(window_seconds)
        self.lockout_seconds = float(lockout_seconds)
        self.max_lockout_seconds = float(max_lockout_seconds)
        self.global_max_failures = max(1, int(global_max_failures))
        self._per_ip: dict[str, _Bucket] = {}
        self._global = _Bucket()

    # ---- 内部 ----

    def _prune(self, bucket: _Bucket, now: float) -> None:
        cut = now - self.window_seconds
        bucket.failures = [t for t in bucket.failures if t > cut]

    def _retry_after(self, bucket: _Bucket, now: float) -> float:
        return max(0.0, bucket.locked_until - now)

    # ---- 对外 ----

    def retry_after(self, ip: str, now: float | None = None) -> float:
        """还需等待多少秒才能再试；0 = 放行。"""
        now = float(now if now is not None else time.time())
        with self._lock:
            self._prune(self._global, now)
            worst = self._retry_after(self._global, now)
            bucket = self._per_ip.get(ip)
            if bucket is not None:
                self._prune(bucket, now)
                worst = max(worst, self._retry_after(bucket, now))
            return worst

    def allow(self, ip: str, now: float | None = None) -> bool:
        """是否放行这次登录尝试。"""
        return self.retry_after(ip, now) <= 0

    def record_failure(self, ip: str, now: float | None = None) -> float:
        """记一次失败；返回本次触发的锁定时长（0 = 未锁定）。"""
        now = float(now if now is not None else time.time())
        with self._lock:
            self._prune(self._global, now)
            self._global.failures.append(now)
            if len(self._global.failures) >= self.global_max_failures:
                self._global.strikes += 1
                wait = min(
                    self.lockout_seconds * (2 ** (self._global.strikes - 1)),
                    self.max_lockout_seconds,
                )
                self._global.locked_until = max(
                    self._global.locked_until, now + wait)
                self._global.failures.clear()
                return wait

            bucket = self._per_ip.setdefault(ip or "unknown", _Bucket())
            self._prune(bucket, now)
            bucket.failures.append(now)
            if len(bucket.failures) >= self.max_failures:
                bucket.strikes += 1
                wait = min(
                    self.lockout_seconds * (2 ** (bucket.strikes - 1)),
                    self.max_lockout_seconds,
                )
                bucket.locked_until = max(bucket.locked_until, now + wait)
                bucket.failures.clear()
                return wait
            return 0.0

    def record_success(self, ip: str) -> None:
        """登录成功：清掉该 IP 的失败计数。

        不清 strikes —— 锁定过再成功说明口令是对的，但保留 strike 让**再次**
        失败时能立刻进入更长的锁定，避免「试到对了就洗白」。
        """
        with self._lock:
            bucket = self._per_ip.get(ip or "unknown")
            if bucket is not None:
                bucket.failures.clear()

    def snapshot(self, now: float | None = None) -> dict:
        """当前被锁定的来源（供管理台/告警查看）。"""
        now = float(now if now is not None else time.time())
        with self._lock:
            locked = {
                ip: round(self._retry_after(b, now), 1)
                for ip, b in self._per_ip.items()
                if self._retry_after(b, now) > 0
            }
            return {
                "locked_ips": locked,
                "global_locked_for": round(self._retry_after(self._global, now), 1),
                "tracked_ips": len(self._per_ip),
            }
