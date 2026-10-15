"""客户端维度滑动窗口限流。

与 IP 白/黑名单互补：那层护「**入口**」（防爆破 / 灌注册），这层护「**用量**」
（防单个 Key 把号池打穿）。

维度：已鉴权的 API Key **按 Key ID 计数**（换 Key 即换桶，符合「客户端」语义）；
无 Key 实体时退化为按客户端 IP。

选滑动窗口而非令牌桶：口径直观（「最近 60 秒内 N 次」），运维一眼能对上日志。
代价是每桶存一份时间戳切片 —— 内网规模（几十 Key）可忽略。

`rpm <= 0` 恒放行（**默认不限速**，升级后行为与引入前完全一致）。
"""

from __future__ import annotations

import math
import threading
import time

WINDOW_SECONDS = 60.0


class ClientLimiter:
    """客户端维度滑动窗口限流器（并发安全）。"""

    def __init__(self, window_seconds: float = WINDOW_SECONDS):
        self._lock = threading.Lock()
        self._hits: dict[str, list[float]] = {}
        self._window = float(window_seconds)
        self._last_gc = 0.0
        self.allowed = 0
        self.blocked = 0

    def allow(self, bucket: str, rpm: int,
              now: float | None = None) -> tuple[bool, float]:
        """记一次访问并返回 `(是否放行, 建议等待秒数)`。

        `rpm <= 0` 恒放行（不限速）。
        """
        if rpm <= 0:
            return True, 0.0
        now = float(now if now is not None else time.time())
        with self._lock:
            self._gc(now)
            cut = now - self._window
            kept = [t for t in self._hits.get(bucket, []) if t > cut]
            if len(kept) >= rpm:
                self._hits[bucket] = kept
                self.blocked += 1
                # 最早一次命中滑出窗口即恢复额度
                wait = max(0.0, kept[0] + self._window - now)
                return False, wait
            kept.append(now)
            self._hits[bucket] = kept
            self.allowed += 1
            return True, 0.0

    def _gc(self, now: float) -> None:
        """清掉完全过期的桶（低频：每窗口最多一次）。

        不做的话，长期运行下「只用过一次就再不出现的 Key/IP」会在 dict 里永久堆积。
        """
        if now - self._last_gc < self._window:
            return
        self._last_gc = now
        cut = now - self._window
        for bucket, stamps in list(self._hits.items()):
            if not any(t > cut for t in stamps):
                self._hits.pop(bucket, None)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "buckets": len(self._hits),
                "allowed": self.allowed,
                "blocked": self.blocked,
                "window_seconds": self._window,
            }


def retry_after_seconds(wait: float) -> int:
    """把建议等待时长折算成 `Retry-After` 秒数（至少 1 秒）。"""
    return max(1, math.ceil(wait))


def bucket_for(key_id: int, client_ip: str) -> str:
    """限流桶键：有 Key ID 用它，否则退化为客户端 IP。"""
    if key_id:
        return f"key:{key_id}"
    return f"ip:{client_ip or 'unknown'}"
