"""账号池管理器：内存状态 + 选号 + 租约 + 持久化 + 账号 CRUD。"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from ..config import Config
from ..db import Database
from ..db import now as db_now
from ..upstream.base import AccountAuth, UpstreamError
from ..upstream.fingerprint import (
    DeviceProfile,
)
from ..upstream.fingerprint import (
    assign as assign_fingerprint,
)
from ..upstream.fingerprint import (
    rotate as rotate_fingerprint,
)
from .entry import AccountEntry
from .guards import (
    DailyUsageCache,
    apply_usage_to_entry,
    guard_block_reason,
    limits_for,
)
from .heal import heal_entry
from .ledger import ModelLedger
from .selector import available_uids, preview_candidates, select
from .sticky import StickyStore
from .transitions import (
    apply_error,
    apply_success,
    cool_down,
    mark_exhausted,
    maybe_revive_on_credit,
    record_rate_limited,
    revive,
)

log = logging.getLogger("keyport.pool")


def _load_fingerprint(raw: object) -> dict:
    """DB 里的 fingerprint 列（JSON 文本）→ dict；非法一律回落空 dict。

    空 dict 表示「尚未分配」；`fingerprint.profile_for` 会按 uid 稳定派生，
    保证同一账号在持久化之前也不会每次请求换设备。
    """
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


class AccountPool:
    """线程安全的账号池。"""

    def __init__(self, cfg: Config, db: Database):
        self.cfg = cfg
        self.db = db
        self._lock = threading.RLock()
        self._entries: dict[str, AccountEntry] = {}
        self._last_pick: dict[str, float] = {}
        self.sticky = StickyStore(cfg.session_sticky)
        self.ledger = ModelLedger()
        self._usage_cache = DailyUsageCache()
        self.state_path = cfg.data_path() / "state.json"
        self._load_from_db()
        self._load_state()

    # ---- 加载与持久化 ----

    def _load_from_db(self) -> None:
        rows = self.db.query("SELECT * FROM accounts")
        with self._lock:
            for row in rows:
                entry = AccountEntry(
                    uid=row["uid"],
                    realm=row["realm"] or "cn",
                    nickname=row["nickname"] or "",
                    enterprise_id=row["enterprise_id"] or "",
                    domain=row["domain"] or "",
                    file_path=row["file_path"] or "",
                    kind=row["kind"] or "oauth",
                    base_url=row["base_url"] or "",
                    credits=int(row["credits"] or 0),
                    credits_used=int(row["credits_used"] or 0),
                    credits_expiring=int(row["credits_expiring"] or 0),
                    credits_updated_at=int(row["credits_updated_at"] or 0),
                    total_calls=int(row["total_calls"] or 0),
                    failed_calls=int(row["failed_calls"] or 0),
                    last_used_at=int(row["last_used_at"] or 0),
                    last_error=row["last_error"] or "",
                    last_checkin_at=int(row["last_checkin_at"] or 0),
                    last_daily_chat=row["last_daily_chat"] or "",
                    enabled=bool(row["enabled"]),
                    manual_disabled=bool(row["manual_disabled"]),
                    disabled_reason=row["disabled_reason"] or "",
                    token_expires_at=int(row["expires_at"] or 0),
                    refresh_token=row["refresh_token"] or "",
                    fingerprint=_load_fingerprint(row["fingerprint"]),
                    installed_at=int(row["installed_at"] or 0),
                    egress_proxy=row["egress_proxy"] or "",
                    egress_checked_at=int(row["egress_checked_at"] or 0),
                    egress_ip=row["egress_ip"] or "",
                    egress_error=row["egress_error"] or "",
                )
                self._entries[entry.uid] = entry

    def _load_state(self) -> None:
        if not self.state_path.is_file():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        now = int(time.time())
        accounts = data.get("accounts") or {}
        healed = False
        with self._lock:
            for uid, state in accounts.items():
                entry = self._entries.get(uid)
                if entry is not None and isinstance(state, dict):
                    entry.load_state(state, now)
                    # 存量自愈：代码修了但磁盘状态还是旧形态 —— 升级程序不会让
                    # 被错误挡住的健康号回来。两条自愈都在这里跑（唯一的恢复入口）。
                    for note in heal_entry(entry, now, self.cfg.pool.model_rate_max_seconds):
                        log.warning("[pool] %s", note)
                        healed = True
            ledger_rows = data.get("model_ledger")
            if isinstance(ledger_rows, list):
                self.ledger.load(ledger_rows)
        # 动过就立刻物化回磁盘：否则修正只活在内存里，重启一次又要重算重打日志
        if healed:
            self.save_state()

    def save_state(self) -> None:
        """把运行时状态落盘（冷却/熔断/额度/账本）。"""
        with self._lock:
            payload = {
                "saved_at": int(time.time()),
                "accounts": {uid: e.to_state() for uid, e in self._entries.items()},
                "model_ledger": self.ledger.snapshot(),
            }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    # ---- 查询 ----

    def get(self, uid: str) -> AccountEntry | None:
        with self._lock:
            return self._entries.get(uid)

    def all_entries(self) -> list[AccountEntry]:
        with self._lock:
            return list(self._entries.values())

    def auth_for(self, entry: AccountEntry) -> AccountAuth:
        """构造出站凭证（从 DB 取最新 token，并带上该账号的设备档案）。"""
        row = self.db.query_one(
            "SELECT * FROM accounts WHERE uid=?", (entry.uid,)
        )
        if row is None:
            return AccountAuth(uid=entry.uid, realm=entry.realm,
                               fingerprint=dict(entry.fingerprint or {}))
        return AccountAuth(
            uid=row["uid"],
            realm=row["realm"] or "cn",
            access_token=row["access_token"] or "",
            refresh_token=row["refresh_token"] or "",
            expires_at=int(row["expires_at"] or 0),
            domain=row["domain"] or "",
            enterprise_id=row["enterprise_id"] or "",
            device_token=row["device_token"] or "",
            nickname=row["nickname"] or "",
            kind=row["kind"] or "oauth",
            file_path=row["file_path"] or "",
            base_url=row["base_url"] or "",
            protocol=row["protocol"] or "openai",
            fallback_token=row["fallback_token"] or "",
            fallback_base_url=row["fallback_base_url"] or "",
            fingerprint=_load_fingerprint(row["fingerprint"]),
        )

    def available_uids(self, realm: str = "", model: str = "") -> list[str]:
        return available_uids(self.all_entries(), self.cfg.pool, realm=realm, model=model)

    # ---- 选号可观测 ----

    def preview_candidates(self, *, realm: str = "", model: str = "",
                           tried: set[str] | None = None,
                           now: int | None = None) -> list[dict]:
        """候选队列预览（**只读**，不写任何状态）。"""
        return preview_candidates(
            self.all_entries(), self.cfg.pool, realm=realm, model=model,
            tried=tried or set(), last_pick=self._last_pick, now=now,
            guard=lambda e: self.guard_reason(e, model=model),
        )

    def selection_balance(self, hours: int = 24) -> dict:
        """选号均衡度：偏斜倍数与覆盖率。

        选号退化成「额度排序」（少数号吃掉大半流量）是防封上的实质危险，
        且此前只能事后从日报发现。

        告警判据是「绝对占比 ≥ 阈值 **且** 相对偏斜 ≥ 阈值」两条件取与 ——
        单看绝对阈值在池里只有 4 个号时必然误报（完全均匀也每个 25%），
        运维第一天就学会忽略它。
        """
        hours = max(1, min(int(hours) if hours else 24, 24 * 30))
        since = db_now() - hours * 3600
        rows = self.db.query(
            "SELECT account_uid, COUNT(*) AS n FROM request_logs "
            "WHERE ts >= ? AND account_uid <> '' GROUP BY account_uid "
            "ORDER BY n DESC",
            (since,),
        )
        counts = {str(r["account_uid"]): int(r["n"]) for r in rows}
        total = sum(counts.values())

        total_accounts = len(self.all_entries())
        used = len(counts)
        # 理想占比按「本可以轮到的账号数」算 —— 未用到的号要计入分母
        denom = max(1, max(used, total_accounts))
        ideal = 100.0 / denom

        max_share = (max(counts.values()) / total * 100.0) if total else 0.0
        skew = (max_share / ideal) if ideal > 0 else 0.0
        coverage = (used / total_accounts * 100.0) if total_accounts else 0.0

        warn_share = 15.0
        warn_ratio = 2.0
        if total and max_share >= warn_share and skew >= warn_ratio:
            verdict = "skewed"
        elif total_accounts and coverage < 50.0:
            verdict = "low-coverage"
        else:
            verdict = "ok"

        return {
            "hours": hours,
            "calls": total,
            "accounts_total": total_accounts,
            "accounts_used": used,
            "coverage_pct": round(coverage, 2),
            "ideal_share_pct": round(ideal, 2),
            "max_share_pct": round(max_share, 2),
            "skew_ratio": round(skew, 2),
            "verdict": verdict,
            "top": [
                {"uid": uid, "calls": n,
                 "share_pct": round(n / total * 100.0, 2) if total else 0.0}
                for uid, n in list(counts.items())[:10]
            ],
        }

    def set_nickname(self, uid: str, nickname: str) -> bool:
        """更新账号昵称（改名后免重登同步）；返回账号是否存在。"""
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return False
            entry.nickname = nickname or ""
            self.db.execute(
                "UPDATE accounts SET nickname=?, updated_at=? WHERE uid=?",
                (entry.nickname, db_now(), uid),
            )
            return True

    # ---- 选号 + 租约 ----

    def pick(
        self,
        *,
        realm: str = "",
        model: str = "",
        conversation_id: str = "",
        tried: set[str] | None = None,
        exclude_uid: str = "",
    ) -> AccountEntry | None:
        """选号：优先会话粘性绑定，其次三因子加权选号。

        账号级限额护栏（`guards`）在两条路径上都生效：被护栏挡住的账号只是
        跳过，不算失败。
        """
        now = int(time.time())
        tried = tried or set()
        self.refresh_guards()

        def _guard(entry: AccountEntry) -> str:
            return self.guard_reason(entry, model=model)

        # 会话粘性：绑定账号仍健康、有容量且未被护栏挡住时直接复用
        if conversation_id and not exclude_uid:
            bound = self.sticky.get(conversation_id)
            if bound and bound not in tried:
                entry = self.get(bound)
                if entry and (not realm or entry.realm == realm) \
                        and entry.healthy_for_model(now, model) \
                        and entry.has_capacity(self.cfg.pool) \
                        and not _guard(entry):
                    self._last_pick[entry.uid] = now
                    return entry

        entry = select(
            self.all_entries(),
            self.cfg.pool,
            now=now,
            realm=realm,
            model=model,
            tried=tried,
            last_pick=self._last_pick,
            guard=_guard,
        )
        if entry is not None:
            self._last_pick[entry.uid] = now
            if conversation_id:
                self.sticky.bind(conversation_id, entry.uid)
        return entry

    # ---- 账号级限额护栏 ----

    def guard_reason(self, entry: AccountEntry, *, model: str = "") -> str:
        """账号是否被挡住；返回原因（空串 = 可服务）。

        两类判定，**顺序有意**：

        1. **base_url 白名单**（先判）：账号的 base_url 决定网关把它的请求连同
           **真实 token** 发往哪里。白名单非空而该账号的 base_url 不在名单内时，
           它既不该服务对话、也不该执行定时任务 —— 先于限额判定，因为它关乎
           凭证外泄而非额度分配。
        2. **限额护栏**：「免费模型」判据来自成本账本（tier 0 = 免费）；账本未观测
           过的模型按付费处理 —— 不确定是否免费时不该当免费放行。
        """
        allow = list(getattr(getattr(self.cfg, "security", None), "base_url_allow", None) or [])
        if allow:
            from ..security import base_url_violation

            violation = base_url_violation(getattr(entry, "base_url", ""), allow)
            if violation:
                return f"base_url 不受信：{violation}"

        guards = getattr(self.cfg, "guards", None)
        if guards is None:
            return ""
        limits = limits_for(guards, entry.realm)
        if not limits.any_enabled():
            return ""
        is_free = self._is_free_model(entry.realm, model)
        return guard_block_reason(entry, limits, model=model, is_free_model=is_free)

    def _is_free_model(self, realm: str, model: str) -> bool:
        if not model:
            return True
        try:
            return self.ledger.tier_of(realm, model) == 0
        except Exception:  # noqa: BLE001 - 账本异常按付费处理（保守）
            return False

    def refresh_guards(self, *, force: bool = False) -> None:
        """刷新当天用量并推送到账号条目（请求路径每 5 秒最多查一次库）。

        护栏全关时直接清空计数、不查库 —— 默认配置下这条路径零开销。
        """
        guards = getattr(self.cfg, "guards", None)
        if guards is None:
            return
        enabled = any(
            limits_for(guards, realm).any_enabled()
            for realm in (self.cfg.enabled_realms() or ["cn"])
        )
        with self._lock:
            entries = list(self._entries.values())
        if not enabled:
            for entry in entries:
                entry.daily_tokens_today = None
                entry.daily_credits_today = None
                entry.model_daily_tokens = None
            return
        usage = self._usage_cache.get(self.db, refresh=force)
        for entry in entries:
            apply_usage_to_entry(entry, usage, limits_for(guards, entry.realm))

    def guards_snapshot(self) -> dict:
        """护栏当前状态（供管理台展示：阈值 + 当日用量）。"""
        guards = getattr(self.cfg, "guards", None)
        out: dict[str, Any] = {"enabled": False, "accounts": []}
        if guards is None:
            return out
        with self._lock:
            entries = list(self._entries.values())
        for entry in entries:
            limits = limits_for(guards, entry.realm)
            if limits.any_enabled():
                out["enabled"] = True
            out["accounts"].append({
                "uid": entry.uid,
                "realm": entry.realm,
                "nickname": entry.nickname,
                "credits": entry.credits,
                "daily_tokens_today": entry.daily_tokens_today,
                "daily_credits_today": entry.daily_credits_today,
                "model_daily_tokens": dict(entry.model_daily_tokens or {}),
                "blocked_reason": entry.guard_blocked_reason,
                "limits": {
                    "reserve_credits": limits.reserve_credits,
                    "daily_token_limit": limits.daily_token_limit,
                    "daily_credit_limit": limits.daily_credit_limit,
                    "model_daily_token_limit": limits.model_daily_token_limit,
                },
            })
        return out

    def acquire(self, uid: str) -> bool:
        """占用一个在途租约（占满则拒绝）。"""
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None or not entry.has_capacity(self.cfg.pool):
                return False
            entry.inflight += 1
            return True

    def release(self, uid: str) -> None:
        with self._lock:
            entry = self._entries.get(uid)
            if entry is not None and entry.inflight > 0:
                entry.inflight -= 1

    # ---- 结果回写 ----

    def record_success(
        self, uid: str, credits_used: float = 0.0, realm: str = "", model: str = "",
        tokens: int = 0,
    ) -> None:
        now = int(time.time())
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            apply_success(entry, credits_used, now)
            if model and tokens > 0:
                self.ledger.observe(realm or entry.realm, model, credits_used, tokens)
            self._persist_counters(entry)

    def record_success_meta(self, uid: str, *, fallback: bool = False) -> None:
        """建连成功时的轻量回写（正式记账在 finish()）。

        回退通道成功**不得**洗掉 Plan 通道的状态：风控计数、鉴权失效与冷却
        都还在，洗掉会让下一次请求立刻重打 Plan 并累加风控计数。
        只有 Plan 通道自身成功、且冷却已过期，才清掉 last_error。
        """
        now = int(time.time())
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            if not fallback and entry.cooldown_until <= now:
                entry.last_error = ""
            self._persist_counters(entry)

    def record_error(self, uid: str, err: UpstreamError, conversation_id: str = "",
                     model: str = "") -> str:
        """记录一次账号级错误。

        `model` 是本次请求实际使用的模型名 —— 模型级冷却（6004）用它做键。
        不传则 6004 回落到账号级冷却（**不写一个永远命不中的键**）。
        """
        now = int(time.time())
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return ""
            action = apply_error(entry, err, self.cfg.pool, now, model=model)
            self._persist_counters(entry)
        if conversation_id and err.should_failover:
            self.sticky.unbind(conversation_id)  # 失败自动解绑
        return action

    def record_rate_limited(self, uid: str, err: UpstreamError) -> None:
        """429 专用回写：只留痕，**不冷却、不计连败**（账号保持可用）。

        与 `record_error` 分开是有意的：429 是频控而非账号故障，走通用路径会让
        软冷却把健康账号踢出选号池，连败计数还会把它推向熔断。
        """
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            record_rate_limited(entry, err)
            self._persist_counters(entry)

    def cool_down(self, uid: str, seconds: int, reason: str) -> None:
        """显式冷却账号（5xx 重试耗尽 / 连接失败）。"""
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            cool_down(entry, seconds, reason, int(time.time()))
            self._persist_counters(entry)

    def _persist_counters(self, entry: AccountEntry) -> None:
        self.db.execute(
            "UPDATE accounts SET total_calls=?, failed_calls=?, last_used_at=?, last_error=?, "
            "credits=?, credits_used=?, enabled=?, disabled_reason=?, updated_at=? WHERE uid=?",
            (
                entry.total_calls, entry.failed_calls, entry.last_used_at, entry.last_error,
                entry.credits, entry.credits_used, 1 if entry.enabled else 0,
                entry.disabled_reason, db_now(), entry.uid,
            ),
        )

    # ---- 运维操作 ----

    def set_manual_disabled(self, uid: str, disabled: bool, reason: str = "") -> bool:
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return False
            entry.manual_disabled = disabled
            if disabled:
                entry.disabled_reason = reason or "manual"
            self.db.execute(
                "UPDATE accounts SET manual_disabled=?, disabled_reason=?, updated_at=? WHERE uid=?",
                (1 if disabled else 0, entry.disabled_reason, db_now(), uid),
            )
            return True

    def revive_account(self, uid: str) -> bool:
        """运维恢复：清空全部冷却与两个禁用位（系统禁用 + 运维摘除）。"""
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return False
            revive(entry, int(time.time()))
            entry.manual_disabled = False
            self.db.execute(
                "UPDATE accounts SET enabled=1, manual_disabled=0, disabled_reason='', "
                "updated_at=? WHERE uid=?",
                (db_now(), uid),
            )
            return True

    def set_enabled(self, uid: str, enabled: bool, reason: str = "") -> bool:
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return False
            entry.enabled = enabled
            entry.disabled_reason = "" if enabled else (reason or "disabled")
            self.db.execute(
                "UPDATE accounts SET enabled=?, disabled_reason=?, updated_at=? WHERE uid=?",
                (1 if enabled else 0, entry.disabled_reason, db_now(), uid),
            )
            return True

    def mark_exhausted(self, uid: str, reason: str = "quota_exhausted") -> None:
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            mark_exhausted(entry, reason)
            self._persist_counters(entry)

    def update_credits(self, uid: str, credits: int, expiring: int = 0,
                       used: int | None = None) -> None:
        """刷新额度（顺带做额度恢复自动复活）。"""
        now = int(time.time())
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            entry.credits = max(0, int(credits))
            entry.credits_expiring = max(0, int(expiring))
            if used is not None:
                entry.credits_used = max(0, int(used))
            entry.credits_updated_at = now
            maybe_revive_on_credit(entry, now)
            self.db.execute(
                "UPDATE accounts SET credits=?, credits_expiring=?, credits_used=?, "
                "credits_updated_at=?, enabled=?, disabled_reason=?, updated_at=? WHERE uid=?",
                (entry.credits, entry.credits_expiring, entry.credits_used, now,
                 1 if entry.enabled else 0, entry.disabled_reason, db_now(), uid),
            )

    # ---- 出站代理（出口线路）----

    def set_egress(self, uid: str, value: str) -> bool:
        """设置账号级出站代理；返回账号是否存在。

        `value` 已经过 `validate_account_egress` 校验（调用方负责），
        `''` = 未指定（跟随全局默认）、`'direct'` = 显式直连、其余为代理 URL。
        """
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return False
            entry.egress_proxy = value or ""
            self.db.execute(
                "UPDATE accounts SET egress_proxy=?, updated_at=? WHERE uid=?",
                (entry.egress_proxy, db_now(), uid),
            )
            return True

    def record_egress_probe(self, uid: str, *, ip: str = "", error: str = "") -> None:
        """记录一次出口探活结果（诊断用，不参与选号判定）。"""
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            entry.egress_checked_at = int(time.time())
            entry.egress_ip = ip or ""
            entry.egress_error = error or ""
            self.db.execute(
                "UPDATE accounts SET egress_checked_at=?, egress_ip=?, egress_error=?, "
                "updated_at=? WHERE uid=?",
                (entry.egress_checked_at, entry.egress_ip, entry.egress_error, db_now(), uid),
            )

    def mark_checkin(self, uid: str) -> None:
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            entry.last_checkin_at = int(time.time())
            self.db.execute(
                "UPDATE accounts SET last_checkin_at=?, updated_at=? WHERE uid=?",
                (entry.last_checkin_at, db_now(), uid),
            )

    def mark_daily_chat(self, uid: str) -> None:
        """记「国际版每日活跃打卡今天已完成」。

        **只在网页腿真正跑完时调用**：桌面腿成功或网页腿失败都不该记 —— 记早了
        会让当天后续巡检直接跳过该账号，那天的积分静默丢掉且不再重试。
        """
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            entry.last_daily_chat = time.strftime("%Y-%m-%d %H:%M:%S")
            self.db.execute(
                "UPDATE accounts SET last_daily_chat=?, updated_at=? WHERE uid=?",
                (entry.last_daily_chat, db_now(), uid),
            )

    # ---- 账号 CRUD ----

    def upsert_account(self, auth: AccountAuth) -> AccountEntry:
        """新增或更新账号凭证（OAuth 登录 / 扫码纳管 / 导入共用入口）。

        设备档案**只给 ZCode 平台账号分配**（`realm=zai` 且 base_url 指向
        Coding Plan 网关）：

        - **ZCode Coding Plan** 的出站头逐个字段用桌面 SKU
          （`x-platform` / `x-os-version` / `x-os-category` / `x-client-*` /
          `x-device-mid`），官方首启安装序与激活事件也按该档案上报 ——
          没有档案就没有可用的设备身份。
        - **WorkBuddy / CodeBuddy** 认的是 `X-Machine-ID` / `X-Session-ID`
          （按 uid 稳定派生，见 `derive_machine_id`），请求里根本不带 SKU
          字段。给它们分配 SKU 只会让后台展示出「用不上的设备档案」，
          反而误导运维以为它们在走桌面指纹。

        因此非 ZCode 账号的 `fingerprint` 保持空 dict，后台显示「—」。
        """
        from ..upstream.claim import is_zcode_plan_account

        wants_sku = is_zcode_plan_account(auth)
        with self._lock:
            existing = self._entries.get(auth.uid)
            fingerprint = dict(existing.fingerprint or {}) if existing else {}
            if wants_sku and not fingerprint:
                fingerprint = assign_fingerprint(auth).to_dict()
            elif not wants_sku:
                # 非 ZCode 账号不需要档案；顺带清掉历史遗留的（例如账号端点
                # 被改成非 Coding Plan 后，旧 SKU 已无意义）
                fingerprint = {}
            self.db.execute(
                """
                INSERT INTO accounts(uid, realm, nickname, enterprise_id, domain, access_token,
                    refresh_token, expires_at, device_token, file_path, kind, base_url, protocol,
                    fallback_token, fallback_base_url, fingerprint, enabled, created_at, updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,?,?)
                ON CONFLICT(uid) DO UPDATE SET
                    realm=excluded.realm, nickname=excluded.nickname,
                    enterprise_id=excluded.enterprise_id, domain=excluded.domain,
                    access_token=excluded.access_token, refresh_token=excluded.refresh_token,
                    expires_at=excluded.expires_at, device_token=excluded.device_token,
                    file_path=excluded.file_path, kind=excluded.kind,
                    base_url=excluded.base_url, protocol=excluded.protocol,
                    fallback_token=excluded.fallback_token,
                    fallback_base_url=excluded.fallback_base_url,
                    fingerprint=excluded.fingerprint,
                    updated_at=excluded.updated_at
                """,
                (auth.uid, auth.realm, auth.nickname, auth.enterprise_id, auth.domain,
                 auth.access_token, auth.refresh_token, auth.expires_at, auth.device_token,
                 auth.file_path, auth.kind, auth.base_url, auth.protocol,
                 auth.fallback_token, auth.fallback_base_url,
                 json.dumps(fingerprint, ensure_ascii=False), db_now(), db_now()),
            )
            entry = existing
            if entry is None:
                entry = AccountEntry(
                    uid=auth.uid, realm=auth.realm, nickname=auth.nickname,
                    enterprise_id=auth.enterprise_id, domain=auth.domain,
                    file_path=auth.file_path, kind=auth.kind,
                    base_url=getattr(auth, "base_url", "") or "",
                    token_expires_at=auth.expires_at, refresh_token=auth.refresh_token,
                    fingerprint=fingerprint,
                    egress_proxy=getattr(auth, "egress_proxy", "") or "",
                )
                self._entries[auth.uid] = entry
            else:
                entry.realm = auth.realm
                entry.nickname = auth.nickname
                entry.enterprise_id = auth.enterprise_id
                entry.domain = auth.domain
                entry.file_path = auth.file_path
                entry.kind = auth.kind
                entry.base_url = getattr(auth, "base_url", "") or ""
                entry.token_expires_at = auth.expires_at
                entry.refresh_token = auth.refresh_token
                entry.fingerprint = fingerprint
                # 出口配置以 DB 为准：auth 对象可能来自 auths/ 文件扫描（无该字段），
                # 覆盖成空会把管理员在面板里配的线路抹掉
                if auth.egress_proxy:
                    entry.egress_proxy = auth.egress_proxy
            # 回填到传入对象，供调用方立即使用同一份档案（出站头同源）
            auth.fingerprint = fingerprint
            return entry

    def rotate_fingerprint(self, uid: str) -> dict | None:
        """换发全新设备档案（新 SKU + 新 device_mid）；账号不存在返回 None。

        风控升级禁用后调用：账号重新启用时已是全新设备身份。换发同时清空
        `installed_at`，让安装序在新设备上重跑。

        非 ZCode 账号（WorkBuddy / CodeBuddy）不持有设备档案，也不该换发 ——
        它们的身份是 `X-Machine-ID`（按 uid 派生），换 SKU 毫无作用。
        这类账号返回空 dict（调用方据此提示，而不是假装换发成功）。
        """
        from ..upstream.claim import is_zcode_plan_account

        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return None
            auth = self.auth_for(entry)
            if not is_zcode_plan_account(auth):
                return {}
            profile: DeviceProfile = rotate_fingerprint(auth)
            entry.fingerprint = profile.to_dict()
            entry.installed_at = 0
            self.db.execute(
                "UPDATE accounts SET fingerprint=?, installed_at=0, updated_at=? WHERE uid=?",
                (json.dumps(entry.fingerprint, ensure_ascii=False), db_now(), uid),
            )
            return dict(entry.fingerprint)

    def mark_installed(self, uid: str, at: int) -> None:
        """记录按账号安装序完成时间（幂等门）。"""
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return
            entry.installed_at = int(at)
            self.db.execute(
                "UPDATE accounts SET installed_at=?, updated_at=? WHERE uid=?",
                (entry.installed_at, db_now(), uid),
            )

    def fingerprint_view(self, uid: str) -> dict:
        """账号设备档案视图（供管理台展示；无档案返回空 dict）。"""
        entry = self.get(uid)
        if entry is None:
            return {}
        return dict(entry.fingerprint or {})

    def update_tokens(self, uid: str, access_token: str, refresh_token: str,
                      expires_at: int, domain: str = "") -> None:
        """refresh 成功后写回 token。"""
        with self._lock:
            self.db.execute(
                "UPDATE accounts SET access_token=?, refresh_token=?, expires_at=?, "
                "domain=COALESCE(NULLIF(?,''), domain), updated_at=? WHERE uid=?",
                (access_token, refresh_token, expires_at, domain, db_now(), uid),
            )
            entry = self._entries.get(uid)
            if entry is not None:
                entry.token_expires_at = expires_at
                if refresh_token:
                    entry.refresh_token = refresh_token

    def remove_account(self, uid: str) -> bool:
        with self._lock:
            if uid not in self._entries:
                return False
            self._entries.pop(uid, None)
            self._last_pick.pop(uid, None)
            self.db.execute("DELETE FROM accounts WHERE uid=?", (uid,))
            return True

    def alert_health(self, realm: str) -> Any:
        """告警用的单域健康快照（只读，不触碰任何账号状态）。"""
        from ..alert import Health

        h = self.metrics_health(realm)
        return Health(
            total=h["total"], healthy=h["healthy"], cooling=h["cooling"],
            disabled=h["disabled"], breaker=h["breaker"], degraded=h["degraded"],
            in_flight=h["in_flight"],
        )

    def metrics_health(self, realm: str = "") -> dict:
        """指标用的健康快照（只读现算）。

        状态维度是**原因分解而非互斥分类**：熔断/降权号同时计入 cooling；
        manual_disabled 是 disabled 的子集；model_cooled 与账号级健康正交
        （该号对触发模型不可用、对其他模型仍可选）。
        """
        now = int(time.time())
        with self._lock:
            entries = [e for e in self._entries.values()
                       if not realm or e.realm == realm]
        return {
            "total": len(entries),
            "healthy": sum(1 for e in entries if e.healthy(now)),
            "cooling": sum(1 for e in entries if e.cooldown_until > now),
            "disabled": sum(1 for e in entries if not e.enabled),
            "in_flight_full": sum(1 for e in entries
                                  if not e.has_capacity(self.cfg.pool)),
            "breaker": sum(1 for e in entries if e.breaker_until > now),
            "degraded": sum(1 for e in entries if e.degraded_until > now),
            "manual_disabled": sum(1 for e in entries if e.manual_disabled),
            "model_cooled": sum(1 for e in entries
                                if any(v > now for v in e.model_cooldowns.values())),
            "in_flight": sum(e.inflight for e in entries),
        }

    def summary(self) -> dict:
        """池概览（供 /healthz 与看板）。"""
        now = int(time.time())
        with self._lock:
            entries = list(self._entries.values())
        healthy = [e for e in entries if e.healthy(now)]
        by_realm: dict[str, int] = {}
        by_state: dict[str, int] = {}
        for e in entries:
            by_realm[e.realm] = by_realm.get(e.realm, 0) + 1
            state = e.run_state(now)
            by_state[state] = by_state.get(state, 0) + 1
        return {
            "total": len(entries),
            "healthy": len(healthy),
            "inflight": sum(e.inflight for e in entries),
            "sticky_bindings": self.sticky.size(),
            "by_realm": by_realm,
            # 运行态分布：区分「等一会儿就好」（rate_limited）与「等下去不会好」
            # （risk_cooling / relogin）—— 两者处置方向相反
            "by_state": by_state,
        }
