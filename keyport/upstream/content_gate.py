"""内容策略拦截的进程内降级闸门。

背景（对齐 workbuddy2api-panel `internal/server/degrade.go`）：上游按**逐字精确
匹配**审核 system 提示词，客户端（Claude Code / Codex 等 CLI）注入的固定模板句
会被误判成非授权渠道，表现为 HTTP 400 +
`Illegal API invocation from an unapproved channel`。这是**误报** —— 账号余额、
权限、限流状态都健康，换号同样被拦。

处置：
  1. 首遇拦截 → 同请求内换中性提示词（`prompt.DEGRADED_PROMPT`）重试一次；
  2. 触发后到**次日 00:00 CST** 之前，请求直达中性提示词，不再先撞一次 400；
  3. 状态是进程内存态，重启清零（custom 模式根本不进这条路径）。

为什么以「次日 00:00 CST」为界：上游的内容策略规则集按自然日滚动更新，跨日后
重新探测一次成本极低（最坏多一次 400），却能避免白天被一次误报永久钉在降级态。
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

# 上游规则重置按北京时间；用固定偏移，不依赖宿主机/容器时区
CST = timezone(timedelta(hours=8))


def next_midnight_cst(now: float | None = None) -> float:
    """返回 `now` 之后最近的 Asia/Shanghai 00:00 时间戳。

    边界：23:59 → 次日 00:00；恰好 00:00:00 → 次日 00:00（不返回自身，
    否则刚触发就立刻过期，降级态形同不存在）。
    """
    ts = time.time() if now is None else now
    local = datetime.fromtimestamp(ts, CST)
    midnight = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return midnight.timestamp()


class ContentBlockGate:
    """降级状态机：触发后保持到次日 00:00 CST（不续期）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._until = 0.0

    def active(self, now: float | None = None) -> bool:
        current = time.time() if now is None else now
        with self._lock:
            return current < self._until

    def trigger(self, now: float | None = None) -> float:
        """触发降级；已在降级期内不续期（保持最早触发点的 00:00 重置语义）。"""
        current = time.time() if now is None else now
        with self._lock:
            if current >= self._until:
                self._until = next_midnight_cst(current)
            return self._until

    def reset(self) -> None:
        """清空降级态（测试 / 运维手工恢复用）。"""
        with self._lock:
            self._until = 0.0
