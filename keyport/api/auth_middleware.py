"""鉴权中间件：API Key 校验、模型白名单、配额、IP 管控。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from ..db import Database, now as db_now
from ..security import evaluate_ip, ip_matches, verify_api_key


@dataclass
class KeyContext:
    """一次下游请求的密钥上下文。"""

    key_id: int
    key_name: str
    realm: str = ""
    model_allow: list[str] = None  # type: ignore[assignment]
    ip_allow: list[str] = None  # type: ignore[assignment]
    ip_block: list[str] = None  # type: ignore[assignment]
    quota_credits: float = 0.0
    used_credits: float = 0.0
    quota_tokens: int = 0
    used_tokens: int = 0

    def __post_init__(self) -> None:
        self.model_allow = self.model_allow or []
        self.ip_allow = self.ip_allow or []
        self.ip_block = self.ip_block or []

    def model_allowed(self, model: str) -> bool:
        if not self.model_allow:
            return True
        base = model.split(":", 1)[-1] if ":" in model else model
        return model in self.model_allow or base in self.model_allow

    def credits_exhausted(self) -> bool:
        return self.quota_credits > 0 and self.used_credits >= self.quota_credits

    def tokens_exhausted(self) -> bool:
        return self.quota_tokens > 0 and self.used_tokens >= self.quota_tokens


def _json_list(raw: str) -> list[str]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return [str(v) for v in value] if isinstance(value, list) else []


class KeyStore:
    """API Key 的增删改查与鉴权（明文仅在创建时返回一次）。"""

    def __init__(self, db: Database):
        self.db = db

    def authenticate(self, raw_key: str) -> KeyContext | None:
        """按明文 Key 鉴权；禁用 / 不存在返回 None。"""
        if not raw_key:
            return None
        # 用前缀缩小候选集，再逐一 compare_digest
        prefix = raw_key[:12]
        rows = self.db.query(
            "SELECT * FROM api_keys WHERE enabled=1 AND (key_prefix=? OR key_prefix='')",
            (prefix,),
        )
        for row in rows:
            if verify_api_key(raw_key, row["key_hash"]):
                self.db.execute(
                    "UPDATE api_keys SET last_used_at=? WHERE id=?", (db_now(), row["id"])
                )
                return self._context(row)
        return None

    def _context(self, row: Any) -> KeyContext:
        return KeyContext(
            key_id=int(row["id"]),
            key_name=row["name"] or f"key-{row['id']}",
            realm=row["realm"] or "",
            model_allow=_json_list(row["model_allow"]),
            ip_allow=_json_list(row["ip_allow"]),
            ip_block=_json_list(row["ip_block"]),
            quota_credits=float(row["quota_credits"] or 0),
            used_credits=float(row["used_credits"] or 0),
            quota_tokens=int(row["quota_tokens"] or 0),
            used_tokens=int(row["used_tokens"] or 0),
        )

    def check_ip(self, ctx: KeyContext, ip: str) -> tuple[bool, str]:
        """密钥级 IP 管控：先黑名单，再白名单（白名单非空即默认拒绝）。"""
        if ip and ip_matches(ip, ctx.ip_block):
            return False, "key_blocklist"
        if ctx.ip_allow:
            if ip and ip_matches(ip, ctx.ip_allow):
                return True, "key_allowlist"
            return False, "key_not_in_allowlist"
        return True, "ok"

    def add_usage(self, key_id: int, credits: float, tokens: int) -> None:
        if key_id <= 0:
            return
        self.db.execute(
            "UPDATE api_keys SET used_credits=used_credits+?, used_tokens=used_tokens+?, "
            "last_used_at=? WHERE id=?",
            (float(credits), int(tokens), db_now(), key_id),
        )


def global_ip_allowed(cfg_allow: list[str], cfg_block: list[str], ip: str) -> tuple[bool, str]:
    decision = evaluate_ip(ip, cfg_allow, cfg_block)
    return decision.allowed, decision.reason
