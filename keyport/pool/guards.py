"""账号级限额护栏 —— 在**本地**拦住会把账号烧穿的请求。

上游只告诉你「余额还剩多少」，不会阻止你把余额用尽。四条护栏补上这一层，
各自作用域与后果都不同（对齐 workbuddy2api-hub 的护栏语义）：

| 护栏 | 触发后行为 | 防的是什么 |
|---|---|---|
| `reserve_credits` | 账号整体不再接单 | 余额被用尽后触发上游的提醒 / 风控 |
| `daily_token_limit` | 账号整体不再接单 | 单账号一天吃掉过多 token |
| `daily_credit_limit` | 账号**只服务免费模型** | 付费模型把当天积分额度烧光 |
| `model_daily_token_limit` | 该账号**仅该模型**不再接单 | 单账号单模型撞上游 24h 窗口配额 |

## 三条不可动摇的语义

1. **`0` = 关闭**。默认全关，装上护栏前的老部署行为逐字不变。
2. **计数未知时绝不停用**。用量读不出来（DB 异常）时计数保持 `None`，
   护栏**放行**而不是停用 —— 宁可放过一次请求，也不能因为读不到数据把
   整个账号池判死。
3. **按本地 0 点解封**。计数窗口 = 本地当天零点起算，与上游的日窗口一致；
   护栏只在**请求路径**生效，定时任务（签到 / 保活 / 领取）不受影响 ——
   任务本身不消耗对话额度，被护栏挡住只会白白丢功能。

免费 / 付费的判据是**各域自己的模型目录**（账本 tier 0 = 免费）；未知模型
按付费处理 —— 「不确定是否免费」不该被当成免费放行。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

# 护栏键名（与 GuardConfig 字段一一对应）
GUARD_KEYS = (
    "reserve_credits",
    "daily_token_limit",
    "daily_credit_limit",
    "model_daily_token_limit",
)

# 用量快照缓存时长（秒）：请求路径上每次选号都查库太重，且护栏本身是
# 分钟级语义，几秒的陈旧度完全可接受。
USAGE_TTL_SECONDS = 5


@dataclass(frozen=True)
class GuardLimits:
    """单个域解析后的四条护栏阈值（已把「按域覆盖」折叠进来）。"""

    reserve_credits: int = 0
    daily_token_limit: int = 0
    daily_credit_limit: int = 0
    model_daily_token_limit: int = 0

    def any_enabled(self) -> bool:
        return any(getattr(self, k) > 0 for k in GUARD_KEYS)


def limits_for(guards: Any, realm: str) -> GuardLimits:
    """解析某域的护栏阈值：按域覆盖优先，缺省继承全局值。

    `guards` 是 `config.GuardConfig`（duck-typed，避免循环导入）。
    """
    base = {k: max(0, int(getattr(guards, k, 0) or 0)) for k in GUARD_KEYS}
    overrides = getattr(guards, "realm_overrides", None) or {}
    per_realm = overrides.get(realm) if isinstance(overrides, dict) else None
    if isinstance(per_realm, dict):
        for key in GUARD_KEYS:
            value = per_realm.get(key)
            if value is None:
                continue  # 显式 null = 继承全局
            try:
                base[key] = max(0, int(value))
            except (TypeError, ValueError):
                continue
    return GuardLimits(**base)


@dataclass
class DailyUsage:
    """本地当天（0 点起算）的账号用量快照。"""

    tokens: dict[str, int] = field(default_factory=dict)
    credits: dict[str, float] = field(default_factory=dict)
    models: dict[str, dict[str, int]] = field(default_factory=dict)

    def tokens_for(self, uid: str) -> int:
        return int(self.tokens.get(uid, 0))

    def credits_for(self, uid: str) -> float:
        return float(self.credits.get(uid, 0.0))

    def model_tokens_for(self, uid: str, model: str) -> int:
        return int((self.models.get(uid) or {}).get(model, 0))


def midnight_epoch(now: int | None = None) -> int:
    """本地当天 0 点的 epoch 秒。"""
    lt = time.localtime(now or int(time.time()))
    return int(time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)))


def fetch_daily_usage(db: Any, now: int | None = None) -> DailyUsage | None:
    """从 `request_logs` 汇总当天用量；读库失败返回 `None`。

    返回 `None` 是**有意义的信号**：调用方必须把它当作「计数未知」而放行，
    绝不能当成「用量为 0」—— 那会让护栏在数据库故障时变成静默的全池停用。
    """
    since = midnight_epoch(now)
    try:
        rows = db.query(
            "SELECT account_uid, model, COALESCE(SUM(total_tokens),0) AS tokens, "
            "COALESCE(SUM(credits),0) AS credits "
            "FROM request_logs WHERE ts >= ? AND account_uid != '' "
            "GROUP BY account_uid, model",
            (since,),
        )
    except Exception:  # noqa: BLE001 - 读不到就是「未知」，由调用方放行
        return None

    usage = DailyUsage()
    for row in rows:
        uid = str(row["account_uid"] or "")
        if not uid:
            continue
        tokens = int(row["tokens"] or 0)
        credits = float(row["credits"] or 0.0)
        usage.tokens[uid] = usage.tokens.get(uid, 0) + tokens
        usage.credits[uid] = usage.credits.get(uid, 0.0) + credits
        if row["model"]:
            bucket = usage.models.setdefault(uid, {})
            bucket[str(row["model"])] = bucket.get(str(row["model"]), 0) + tokens
    return usage


class DailyUsageCache:
    """带 TTL 的当天用量缓存（请求路径每 5 秒最多查一次库）。"""

    def __init__(self, ttl_seconds: int = USAGE_TTL_SECONDS):
        self.ttl_seconds = max(0, ttl_seconds)
        self._lock = threading.Lock()
        self._at = 0.0
        self._usage: DailyUsage | None = None
        self._loaded = False

    def get(self, db: Any, *, refresh: bool = False) -> DailyUsage | None:
        now = time.monotonic()
        with self._lock:
            if not refresh and self._loaded and (now - self._at) < self.ttl_seconds:
                return self._usage
        usage = fetch_daily_usage(db)
        with self._lock:
            self._at = now
            self._usage = usage
            self._loaded = True
            return self._usage

    def invalidate(self) -> None:
        with self._lock:
            self._loaded = False
            self._usage = None


def guard_block_reason(
    entry: Any,
    limits: GuardLimits,
    *,
    model: str = "",
    is_free_model: bool = True,
) -> str:
    """账号是否被护栏挡住；返回原因（空串 = 可服务）。

    `entry` 需带 `credits` / `daily_tokens_today` / `daily_credits_today` /
    `model_daily_tokens`（由 `AccountPool.apply_guards` 推送）。

    计数为 `None` 一律放行（见模块文档第 2 条）。
    """
    if not limits.any_enabled():
        return ""

    reserve = limits.reserve_credits
    if reserve > 0 and int(getattr(entry, "credits", 0) or 0) <= reserve:
        return f"reserve_credits({entry.credits}<={reserve})"

    daily_tokens = getattr(entry, "daily_tokens_today", None)
    if limits.daily_token_limit > 0 and daily_tokens is not None \
            and int(daily_tokens) >= limits.daily_token_limit:
        return f"daily_token_limit({daily_tokens}>={limits.daily_token_limit})"

    daily_credits = getattr(entry, "daily_credits_today", None)
    if limits.daily_credit_limit > 0 and daily_credits is not None \
            and float(daily_credits) >= limits.daily_credit_limit and not is_free_model:
        return (f"daily_credit_limit({daily_credits:g}>={limits.daily_credit_limit})"
                f"，仅免费模型可用")

    per_model = getattr(entry, "model_daily_tokens", None)
    if limits.model_daily_token_limit > 0 and model and isinstance(per_model, dict):
        used = int(per_model.get(model, 0) or 0)
        if used >= limits.model_daily_token_limit:
            return (f"model_daily_token_limit({model} {used}"
                    f">={limits.model_daily_token_limit})")
    return ""


def apply_usage_to_entry(entry: Any, usage: DailyUsage | None, limits: GuardLimits) -> None:
    """把用量快照推送到账号条目（护栏关闭时清空，避免残留旧计数）。

    `usage is None`（读库失败）时把计数置 `None` —— 明确表达「未知」，
    护栏据此放行。
    """
    if usage is None:
        entry.daily_tokens_today = None
        entry.daily_credits_today = None
        entry.model_daily_tokens = None
        return
    if not limits.any_enabled():
        # 护栏全关：不记计数（省内存，也避免看板展示误导性的 0）
        entry.daily_tokens_today = None
        entry.daily_credits_today = None
        entry.model_daily_tokens = None
        return
    entry.daily_tokens_today = usage.tokens_for(entry.uid)
    entry.daily_credits_today = usage.credits_for(entry.uid)
    entry.model_daily_tokens = dict(usage.models.get(entry.uid) or {})


def make_free_model_predicate(ledger: Any) -> Callable[[str, str], bool]:
    """免费模型判据：账本 tier 0 即免费（按 (域, 模型) 记录实测单价）。

    账本未观测过的模型返回 `False`（按付费处理）—— 「不确定是否免费」
    不该被当作免费放行，否则护栏形同虚设。
    """

    def _is_free(realm: str, model: str) -> bool:
        if not model:
            return True
        try:
            return ledger.tier_of(realm, model) == 0
        except Exception:  # noqa: BLE001 - 账本异常按付费处理（保守）
            return False

    return _is_free
