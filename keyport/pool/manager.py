"""账号池管理器：内存状态 + 选号 + 租约 + 持久化 + 账号 CRUD。"""

from __future__ import annotations

import json
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
from .ledger import ModelLedger
from .selector import available_uids, select
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
                    credits=int(row["credits"] or 0),
                    credits_used=int(row["credits_used"] or 0),
                    credits_expiring=int(row["credits_expiring"] or 0),
                    credits_updated_at=int(row["credits_updated_at"] or 0),
                    total_calls=int(row["total_calls"] or 0),
                    failed_calls=int(row["failed_calls"] or 0),
                    last_used_at=int(row["last_used_at"] or 0),
                    last_error=row["last_error"] or "",
                    last_checkin_at=int(row["last_checkin_at"] or 0),
                    enabled=bool(row["enabled"]),
                    manual_disabled=bool(row["manual_disabled"]),
                    disabled_reason=row["disabled_reason"] or "",
                    token_expires_at=int(row["expires_at"] or 0),
                    refresh_token=row["refresh_token"] or "",
                    fingerprint=_load_fingerprint(row["fingerprint"]),
                    installed_at=int(row["installed_at"] or 0),
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
        with self._lock:
            for uid, state in accounts.items():
                entry = self._entries.get(uid)
                if entry is not None and isinstance(state, dict):
                    entry.load_state(state, now)
            ledger_rows = data.get("model_ledger")
            if isinstance(ledger_rows, list):
                self.ledger.load(ledger_rows)

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
        """账号是否被限额护栏挡住；返回原因（空串 = 可服务）。

        「免费模型」判据来自成本账本（tier 0 = 免费）；账本未观测过的模型按
        付费处理 —— 不确定是否免费时不该当免费放行。
        """
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

    def record_error(self, uid: str, err: UpstreamError, conversation_id: str = "") -> str:
        now = int(time.time())
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return ""
            action = apply_error(entry, err, self.cfg.pool, now)
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
                    token_expires_at=auth.expires_at, refresh_token=auth.refresh_token,
                    fingerprint=fingerprint,
                )
                self._entries[auth.uid] = entry
            else:
                entry.realm = auth.realm
                entry.nickname = auth.nickname
                entry.enterprise_id = auth.enterprise_id
                entry.domain = auth.domain
                entry.file_path = auth.file_path
                entry.kind = auth.kind
                entry.token_expires_at = auth.expires_at
                entry.refresh_token = auth.refresh_token
                entry.fingerprint = fingerprint
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

    def summary(self) -> dict:
        """池概览（供 /healthz 与看板）。"""
        now = int(time.time())
        with self._lock:
            entries = list(self._entries.values())
        healthy = [e for e in entries if e.healthy(now)]
        by_realm: dict[str, int] = {}
        for e in entries:
            by_realm[e.realm] = by_realm.get(e.realm, 0) + 1
        return {
            "total": len(entries),
            "healthy": len(healthy),
            "inflight": sum(e.inflight for e in entries),
            "sticky_bindings": self.sticky.size(),
            "by_realm": by_realm,
        }
