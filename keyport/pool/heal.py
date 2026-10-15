"""存量自愈：启动时纠正历史脏状态。

## 为什么需要

代码修了但磁盘上的状态还是旧形态 —— **升级程序不会让被错误挡住的健康号回来**。
这类缺陷只有通过「用旧形态的状态文件启动」才测得到。

两条纪律：

- **只放长不缩短**：绝不因为「我算出来更短」而提前放出一个可能仍在被限的模型 ——
  那等于制造热循环（窗口内反复打必然失败的请求，每次都在上游留下风控痕迹）。
- **动过就落盘**：修正只在内存生效的话，重启一次又要重算重打日志；自愈本身幂等，
  但「每次启动都刷一屏 WARN」会让真正的告警被淹没。

## 自愈一：模型级限额被误写成账号级冷却

6004 的语义是**模型级**（上游用 `"code":6004` 表达「这个模型用量超限」）。若某个
历史版本把它的冷却写到了账号级 `cooldown_until`，后果不是「多等一会儿」：账号级
冷却期间这个**凭证与余额都完好的号会从池里消失**，用户看到的现象是「某些模型接
不了」——其实是号被整体摘了。

**为什么是「清除」而不是「迁移成模型级」**：旧形态**没有记录是哪个模型**，无从迁移。
清除的代价是「如果那个模型确实被限，下一次请求会再撞一次 6004」；两害相权取前者 ——
**多一次上游请求** vs **一个健康号被整体挡最长 2 小时**。

## 自愈二：模型级冷却被账号级上限截断

上游明说「这个模型 5 小时后恢复」时，若写进磁盘的截止被账号级封顶截成 2h，后果是
**在真实窗口里反复撞墙**：2h 一到该号对这个模型重新进入候选 → 打一次必然失败的
请求 → 再被 6004 打回、再等 2h …… 直到窗口真的滑过。

判据直接来自「字段自相矛盾」，不依赖文案：
`model_reset_at` 非零（有权威时刻）且 `model_cooldowns[m]` 落在它**之前**。
"""

from __future__ import annotations

import logging
from typing import Any

log = logging.getLogger("keyport.pool.heal")

# 旧形态的 reason 前缀：把 6004 模型级限额误写成账号级冷却时留下的标记。
# 精确匹配这个前缀（而不是宽松地找 "6004"）—— 新形态的模型级 reason 里也含
# "6004"，但那写在 model_cooldowns 里，不是账号级。
LEGACY_MODEL_LIMIT_REASON = "6004 model rate limit"


def heal_legacy_model_limit_cooldown(entry: Any, now: int) -> bool:
    """纠正「reason 说是模型级限额、效果却是账号级冷却」的矛盾状态。

    判定条件刻意收紧到**四条全中**，只清「确定自相矛盾」的那一类：

    - reason 以旧形态前缀开头（精确到 6004 模型级限额）
    - 账号级 `cooldown_until` 在未来（确实在挡）
    - 该号**没有**被禁用或手动摘除（那些是**人的决定**或其他病因，不该被顺手清掉）
    - 熔断位为空（熔断是另一条独立病因）
    """
    if entry is None:
        return False
    reason = str(getattr(entry, "last_error", "") or "")
    if not reason.startswith(LEGACY_MODEL_LIMIT_REASON):
        return False
    until = int(getattr(entry, "cooldown_until", 0) or 0)
    if until <= now:
        return False
    if not getattr(entry, "enabled", True) or getattr(entry, "manual_disabled", False):
        return False
    if int(getattr(entry, "breaker_until", 0) or 0) > now:
        return False

    entry.cooldown_until = 0
    entry.last_error = ""
    return True


def heal_truncated_model_cooldowns(entry: Any, now: int, model_max_seconds: int) -> int:
    """把被账号级上限截断过的模型级冷却放长到上游明示的重置时刻。

    返回被修正的条目数（0 表示无需处理）。

    **只放长不放短**：`min(reset_at, now + model_max)` 一定 ≥ 现有截止
    （前提是现有截止 ≤ reset_at），所以本函数只会把冷却放长。
    """
    if entry is None:
        return 0
    cooldowns = getattr(entry, "model_cooldowns", None) or {}
    resets = getattr(entry, "model_reset_at", None) or {}
    if not cooldowns or not resets:
        return 0
    cap = now + max(1, int(model_max_seconds or 86400))
    fixed = 0
    for model, until in list(cooldowns.items()):
        reset_at = int(resets.get(model, 0) or 0)
        if reset_at <= 0:
            continue  # 无权威时刻（无时间信息的退避）：没有「该到什么时候」可对齐
        until = int(until or 0)
        if until <= 0 or until >= reset_at:
            continue  # 已对齐（或本来就更晚）：不动
        want = min(reset_at, cap)
        if want <= until:
            continue
        cooldowns[model] = want
        fixed += 1
    return fixed


def heal_entry(entry: Any, now: int, model_max_seconds: int) -> list[str]:
    """对单个账号执行全部自愈；返回做了什么（供日志）。"""
    notes: list[str] = []
    if heal_legacy_model_limit_cooldown(entry, now):
        notes.append(
            f"已清除账号级冷却（旧形态把 6004 模型级限额误写成账号级，"
            f"会把该号对所有模型一起挡掉）：{entry.uid}"
        )
    fixed = heal_truncated_model_cooldowns(entry, now, model_max_seconds)
    if fixed:
        notes.append(
            f"已按上游重置时刻放长 {fixed} 条模型级冷却：{entry.uid}"
            f"（旧的截止被账号级上限截断过，会在真实窗口里反复撞墙）"
        )
    return notes
