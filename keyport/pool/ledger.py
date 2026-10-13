"""账本择优：按 (realm, model) 记录每千 token 单价，免费 / 便宜的账号优先。

- 每次成功请求按 usage.credit 折算单价，EMA 平滑
- 6 小时未更新即失效（陈旧价格不复活）
- 成本分层：tier 0 免费 > tier 1 未知 > tier 2 收费
- 成本分层条件探索：tier 0 垄断且 tier 1 有成员时，按间隔把选号改道给未知号，
  承接真实请求零新增上游调用（搭车改道）
"""

from __future__ import annotations

import threading
import time

EMA_ALPHA = 0.3
STALE_SECONDS = 6 * 3600

TIER_FREE = 0
TIER_UNKNOWN = 1
TIER_PAID = 2


class ModelLedger:
    """(realm, model) → 单价 / 分层 / 样本数 的内存账本。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._rows: dict[tuple[str, str], dict] = {}
        self._explore: dict[tuple[str, str], float] = {}

    def observe(self, realm: str, model: str, credits: float, tokens: int) -> None:
        """记录一次成功请求的实测单价（每千 token 积分）。"""
        if not model or tokens <= 0:
            return
        cost_per_1k = (credits / tokens) * 1000.0
        key = (realm, model)
        now = time.time()
        with self._lock:
            row = self._rows.get(key)
            if row is None or (now - row["observed_at"]) > STALE_SECONDS:
                row = {"cost_per_1k": cost_per_1k, "samples": 1, "observed_at": now}
            else:
                row["cost_per_1k"] = (
                    EMA_ALPHA * cost_per_1k + (1 - EMA_ALPHA) * row["cost_per_1k"]
                )
                row["samples"] += 1
                row["observed_at"] = now
            row["tier"] = TIER_FREE if row["cost_per_1k"] <= 0 else TIER_PAID
            self._rows[key] = row

    def cost_of(self, realm: str, model: str) -> float | None:
        """返回实测单价；无记录或已陈旧返回 None。"""
        with self._lock:
            row = self._rows.get((realm, model))
            if not row:
                return None
            if (time.time() - row["observed_at"]) > STALE_SECONDS:
                return None
            return float(row["cost_per_1k"])

    def tier_of(self, realm: str, model: str) -> int:
        """成本分层：0 免费 / 1 未知 / 2 收费。"""
        cost = self.cost_of(realm, model)
        if cost is None:
            return TIER_UNKNOWN
        return TIER_FREE if cost <= 0 else TIER_PAID

    def should_explore(self, realm: str, model: str, interval: int) -> bool:
        """是否该做一次成本探索（搭车改道给未知账号）。"""
        if interval <= 0 or not model:
            return False
        key = (realm, model)
        now = time.time()
        with self._lock:
            last = self._explore.get(key, 0.0)
            if now - last < interval:
                return False
            self._explore[key] = now
            return True

    def snapshot(self) -> list[dict]:
        """导出账本台账（供 /status 与管理台透出）。"""
        now = time.time()
        with self._lock:
            out = []
            for (realm, model), row in self._rows.items():
                out.append({
                    "realm": realm,
                    "model": model,
                    "cost_per_1k": round(float(row["cost_per_1k"]), 6),
                    "tier": int(row["tier"]),
                    "samples": int(row["samples"]),
                    "observed_at": int(row["observed_at"]),
                    "stale": (now - row["observed_at"]) > STALE_SECONDS,
                })
            out.sort(key=lambda r: (r["realm"], r["model"]))
            return out

    def explore_snapshot(self) -> list[dict]:
        with self._lock:
            return [
                {"realm": k[0], "model": k[1], "last_explore": int(v)}
                for k, v in self._explore.items()
            ]

    def load(self, rows: list[dict]) -> None:
        """从持久化数据恢复账本。"""
        with self._lock:
            for row in rows:
                try:
                    self._rows[(str(row["realm"]), str(row["model"]))] = {
                        "cost_per_1k": float(row["cost_per_1k"]),
                        "tier": int(row.get("tier", TIER_UNKNOWN)),
                        "samples": int(row.get("samples", 0)),
                        "observed_at": float(row.get("observed_at", 0)),
                    }
                except (KeyError, TypeError, ValueError):
                    continue
