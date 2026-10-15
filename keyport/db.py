"""SQLite 持久层（标准库 sqlite3，无重依赖）。

- WAL 模式 + 单连接 + 进程内锁：管理台读写量低，简单可靠优先。
- 所有时间戳统一 Unix 秒（整数）。
- schema 通过 _SCHEMA 幂等创建。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable, Sequence

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    uid                TEXT PRIMARY KEY,
    realm              TEXT NOT NULL DEFAULT 'cn',
    nickname           TEXT NOT NULL DEFAULT '',
    enterprise_id      TEXT NOT NULL DEFAULT '',
    domain             TEXT NOT NULL DEFAULT '',
    access_token       TEXT NOT NULL DEFAULT '',
    refresh_token      TEXT NOT NULL DEFAULT '',
    expires_at         INTEGER NOT NULL DEFAULT 0,
    device_token       TEXT NOT NULL DEFAULT '',
    file_path          TEXT NOT NULL DEFAULT '',
    kind               TEXT NOT NULL DEFAULT 'oauth',
    base_url           TEXT NOT NULL DEFAULT '',
    protocol           TEXT NOT NULL DEFAULT 'openai',
    fallback_token     TEXT NOT NULL DEFAULT '',
    fallback_base_url  TEXT NOT NULL DEFAULT '',
    fingerprint        TEXT NOT NULL DEFAULT '{}',
    installed_at       INTEGER NOT NULL DEFAULT 0,
    enabled            INTEGER NOT NULL DEFAULT 1,
    manual_disabled    INTEGER NOT NULL DEFAULT 0,
    disabled_reason    TEXT NOT NULL DEFAULT '',
    credits            INTEGER NOT NULL DEFAULT 0,
    credits_used       INTEGER NOT NULL DEFAULT 0,
    credits_expiring   INTEGER NOT NULL DEFAULT 0,
    credits_updated_at INTEGER NOT NULL DEFAULT 0,
    total_calls        INTEGER NOT NULL DEFAULT 0,
    failed_calls       INTEGER NOT NULL DEFAULT 0,
    last_used_at       INTEGER NOT NULL DEFAULT 0,
    last_error         TEXT NOT NULL DEFAULT '',
    last_checkin_at    INTEGER NOT NULL DEFAULT 0,
    created_at         INTEGER NOT NULL DEFAULT 0,
    updated_at         INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS api_keys (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL DEFAULT '',
    key_hash      TEXT NOT NULL UNIQUE,
    key_prefix    TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 1,
    realm         TEXT NOT NULL DEFAULT '',
    model_allow   TEXT NOT NULL DEFAULT '[]',
    quota_credits INTEGER NOT NULL DEFAULT 0,
    used_credits  REAL NOT NULL DEFAULT 0,
    quota_tokens  INTEGER NOT NULL DEFAULT 0,
    used_tokens   INTEGER NOT NULL DEFAULT 0,
    ip_allow      TEXT NOT NULL DEFAULT '[]',
    ip_block      TEXT NOT NULL DEFAULT '[]',
    note          TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL DEFAULT 0,
    last_used_at  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS request_logs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            INTEGER NOT NULL,
    key_id        INTEGER NOT NULL DEFAULT 0,
    key_name      TEXT NOT NULL DEFAULT '',
    account_uid   TEXT NOT NULL DEFAULT '',
    realm         TEXT NOT NULL DEFAULT '',
    protocol      TEXT NOT NULL DEFAULT '',
    model         TEXT NOT NULL DEFAULT '',
    stream        INTEGER NOT NULL DEFAULT 0,
    status        INTEGER NOT NULL DEFAULT 0,
    ttfb_ms       INTEGER NOT NULL DEFAULT 0,
    total_ms      INTEGER NOT NULL DEFAULT 0,
    prompt_tokens INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    credits       REAL NOT NULL DEFAULT 0,
    retries       INTEGER NOT NULL DEFAULT 0,
    client_ip     TEXT NOT NULL DEFAULT '',
    error         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_key ON request_logs(key_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_logs_model ON request_logs(model, ts DESC);

CREATE TABLE IF NOT EXISTS admin_sessions (
    token_hash TEXT PRIMARY KEY,
    username   TEXT NOT NULL DEFAULT 'admin',
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    ip         TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    ip        TEXT NOT NULL DEFAULT '',
    action    TEXT NOT NULL DEFAULT '',
    target    TEXT NOT NULL DEFAULT '',
    result    TEXT NOT NULL DEFAULT '',
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts DESC);

CREATE TABLE IF NOT EXISTS task_runs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        INTEGER NOT NULL,
    task      TEXT NOT NULL DEFAULT '',
    realm     TEXT NOT NULL DEFAULT '',
    account   TEXT NOT NULL DEFAULT '',
    ok        INTEGER NOT NULL DEFAULT 0,
    detail    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_task_ts ON task_runs(ts DESC);

CREATE TABLE IF NOT EXISTS model_ledger (
    realm       TEXT NOT NULL DEFAULT '',
    model       TEXT NOT NULL DEFAULT '',
    cost_per_1k REAL NOT NULL DEFAULT 0,
    tier        INTEGER NOT NULL DEFAULT 1,
    samples     INTEGER NOT NULL DEFAULT 0,
    observed_at INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (realm, model)
);

CREATE TABLE IF NOT EXISTS claim_history (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         INTEGER NOT NULL,
    uid        TEXT NOT NULL DEFAULT '',
    account    TEXT NOT NULL DEFAULT '',
    plan_id    TEXT NOT NULL DEFAULT '',
    plan_name  TEXT NOT NULL DEFAULT '',
    outcome    TEXT NOT NULL DEFAULT '',
    code       INTEGER NOT NULL DEFAULT 0,
    starts_at  INTEGER NOT NULL DEFAULT 0,
    ends_at    INTEGER NOT NULL DEFAULT 0,
    detail     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_claim_ts ON claim_history(ts DESC);
CREATE INDEX IF NOT EXISTS idx_claim_uid ON claim_history(uid, ts DESC);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL DEFAULT ''
);
"""

# 增量迁移：(表, 列, 列定义)。老库缺列时按此补齐，无需手工迁移。
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("accounts", "base_url", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "protocol", "TEXT NOT NULL DEFAULT 'openai'"),
    ("accounts", "fallback_token", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "fallback_base_url", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "fingerprint", "TEXT NOT NULL DEFAULT '{}'"),
    ("accounts", "installed_at", "INTEGER NOT NULL DEFAULT 0"),
    # 账号级出站代理：'' = 未指定（跟随全局默认），'direct' = 显式直连，
    # 其余为代理 URL（http/https/socks5）
    ("accounts", "egress_proxy", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "egress_checked_at", "INTEGER NOT NULL DEFAULT 0"),
    ("accounts", "egress_ip", "TEXT NOT NULL DEFAULT ''"),
    ("accounts", "egress_error", "TEXT NOT NULL DEFAULT ''"),
]


class Database:
    """线程安全的 SQLite 封装。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=30.0)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")
            self._conn.executescript(_SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """补齐老库缺失的列（幂等：已存在的列跳过）。"""
        for table, column, ddl in _MIGRATIONS:
            existing = {
                row["name"] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column in existing:
                continue
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # ---- settings kv ----

    def get_setting(self, key: str, default: str = "") -> str:
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_json_setting(self, key: str, default: Any = None) -> Any:
        raw = self.get_setting(key, "")
        if not raw:
            return default
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return default

    def set_json_setting(self, key: str, value: Any) -> None:
        self.set_setting(key, json.dumps(value, ensure_ascii=False))


# ---- 单例 ----


_db: Database | None = None
_db_lock = threading.Lock()


def init_db(path: str | Path) -> Database:
    """初始化数据库单例。

    路径变化或既有连接已关闭时重建（测试与热切换配置都依赖此语义）。
    """
    global _db
    with _db_lock:
        if _db is not None:
            same_path = str(path) == _db.path
            if same_path and _is_open(_db):
                return _db
            try:
                _db.close()
            except Exception:  # noqa: BLE001 - 已关闭/不可用时忽略
                pass
            _db = None
        _db = Database(path)
        return _db


def _is_open(db: Database) -> bool:
    try:
        db.query_one("SELECT 1")
        return True
    except sqlite3.ProgrammingError:
        return False


def reset_db() -> None:
    """关闭并清空单例（测试用）。"""
    global _db
    with _db_lock:
        if _db is not None:
            try:
                _db.close()
            except Exception:  # noqa: BLE001
                pass
        _db = None


def get_db() -> Database:
    if _db is None:
        raise RuntimeError("database not initialised; call init_db() first")
    return _db


def now() -> int:
    return int(time.time())
