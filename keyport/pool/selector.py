"""三因子加权选号。

权重公式：`credits 比例 ×10 + 快过期积分占比 ×8 + 闲置补偿`

流程：
  1. 过滤出健康 + 有容量 + 未被本次请求试过的账号（realm 匹配）
  2. 按权重降序取 Top-K 短名单（K = pool.top_k）
  3. 短名单内加权抽签（防惊群：跳过 100ms 内刚被选中的账号；
     全部刚被选过时用 LRU 兜底，在全体候选中选最旧者）
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable

from ..config import PoolConfig
from .entry import AccountEntry

CREDITS_WEIGHT = 10.0
EXPIRING_WEIGHT = 8.0


@dataclass
class Candidate:
    entry: AccountEntry
    weight: float


def compute_weight(entry: AccountEntry, max_credits: int, now: int, cfg: PoolConfig) -> float:
    """三因子加权（credits 比例以全集最大值为基准，避免量纲爆炸）。"""
    credits_ratio = (entry.credits / max_credits) if max_credits > 0 else 0.0
    return (
        credits_ratio * CREDITS_WEIGHT
        + entry.expiring_ratio() * EXPIRING_WEIGHT
        + entry.idle_weight(now, cfg)
    )


def _shuffle_ties(cands: list[Candidate]) -> list[Candidate]:
    """等权重候选随机打乱：防止字典序截断把靠后账号饿死。"""
    out = list(cands)
    i = 0
    n = len(out)
    while i < n:
        j = i
        while j + 1 < n and abs(out[j + 1].weight - out[i].weight) < 1e-9:
            j += 1
        if j > i:
            block = out[i:j + 1]
            random.shuffle(block)
            out[i:j + 1] = block
        i = j + 1
    return out


def select(
    entries: list[AccountEntry],
    cfg: PoolConfig,
    *,
    now: int | None = None,
    realm: str = "",
    model: str = "",
    tried: set[str] | None = None,
    last_pick: dict[str, float] | None = None,
    rand: random.Random | None = None,
    guard: Callable[[AccountEntry], str] | None = None,
) -> AccountEntry | None:
    """选出一个账号；无可用账号返回 None。

    `guard`：账号级限额护栏判定，返回非空原因即视为不可服务。被护栏挡住的
    账号只是**跳过**，不算失败 —— 不落冷却、不计连败，等本地 0 点自然解封。
    """
    now = now or int(time.time())
    tried = tried or set()
    last_pick = last_pick if last_pick is not None else {}
    rng = rand or random

    def _guarded(entry: AccountEntry) -> bool:
        if guard is None:
            return False
        try:
            reason = guard(entry)
        except Exception:  # noqa: BLE001 - 护栏异常不该阻断选号
            return False
        entry.guard_blocked_reason = reason or ""
        return bool(reason)

    pool = [
        e for e in entries
        if (not realm or e.realm == realm)
        and e.uid not in tried
        and e.healthy_for_model(now, model)
        and e.has_capacity(cfg)
        and not _guarded(e)
    ]
    if not pool:
        return None

    max_credits = max((e.credits for e in pool), default=0)
    cands = [Candidate(e, compute_weight(e, max_credits, now, cfg)) for e in pool]
    # 权重降序（等权重块先洗牌，避免稳定排序造成的固定偏好）
    cands.sort(key=lambda c: c.weight, reverse=True)
    cands = _shuffle_ties(cands)

    shortlist = cands[: max(1, cfg.top_k)]
    gap = max(0, cfg.pick_gap_ms) / 1000.0

    def too_recent(c: Candidate) -> bool:
        return gap > 0 and (now - last_pick.get(c.entry.uid, 0.0)) < gap

    eligible = [c for c in shortlist if not too_recent(c)]
    if not eligible:
        # Top-K 全部刚被用过 → LRU 兜底：全体候选中选最旧者
        return min(pool, key=lambda e: last_pick.get(e.uid, 0.0))

    total = sum(c.weight for c in eligible)
    if total <= 0:
        chosen = rng.choice(eligible)
        return chosen.entry
    threshold = rng.random() * total
    acc = 0.0
    for c in eligible:
        acc += c.weight
        if acc >= threshold:
            return c.entry
    return eligible[-1].entry


def available_uids(
    entries: list[AccountEntry],
    cfg: PoolConfig,
    *,
    now: int | None = None,
    realm: str = "",
    model: str = "",
) -> list[str]:
    """当前可用（健康 + 有容量）账号 uid 清单。"""
    now = now or int(time.time())
    return [
        e.uid for e in entries
        if (not realm or e.realm == realm)
        and e.healthy_for_model(now, model)
        and e.has_capacity(cfg)
    ]
