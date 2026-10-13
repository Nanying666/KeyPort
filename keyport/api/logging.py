"""请求日志与用量落库。"""

from __future__ import annotations

from dataclasses import dataclass

from ..db import Database, now as db_now


@dataclass
class RequestRecord:
    key_id: int = 0
    key_name: str = ""
    account_uid: str = ""
    realm: str = ""
    protocol: str = ""
    model: str = ""
    stream: bool = False
    status: int = 0
    ttfb_ms: int = 0
    total_ms: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    credits: float = 0.0
    retries: int = 0
    client_ip: str = ""
    error: str = ""


class RequestLogger:
    def __init__(self, db: Database):
        self.db = db

    def log(self, rec: RequestRecord) -> int:
        cur = self.db.execute(
            """
            INSERT INTO request_logs(ts, key_id, key_name, account_uid, realm, protocol, model,
                stream, status, ttfb_ms, total_ms, prompt_tokens, completion_tokens,
                total_tokens, credits, retries, client_ip, error)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                db_now(), rec.key_id, rec.key_name, rec.account_uid, rec.realm, rec.protocol,
                rec.model, 1 if rec.stream else 0, rec.status, rec.ttfb_ms, rec.total_ms,
                rec.prompt_tokens, rec.completion_tokens, rec.total_tokens, rec.credits,
                rec.retries, rec.client_ip, rec.error[:500],
            ),
        )
        return int(cur.lastrowid or 0)

    def recent(self, limit: int = 50, key_id: int = 0, model: str = "") -> list[dict]:
        sql = "SELECT * FROM request_logs WHERE 1=1"
        params: list = []
        if key_id:
            sql += " AND key_id=?"
            params.append(key_id)
        if model:
            sql += " AND model=?"
            params.append(model)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(limit, 500)))
        return [dict(r) for r in self.db.query(sql, tuple(params))]

    def prune(self, keep_days: int = 30) -> int:
        cutoff = db_now() - keep_days * 86400
        cur = self.db.execute("DELETE FROM request_logs WHERE ts < ?", (cutoff,))
        return int(cur.rowcount or 0)


def record_audit(db: Database, ip: str, action: str, target: str = "",
                 result: str = "", detail: str = "") -> None:
    db.execute(
        "INSERT INTO audit_logs(ts, ip, action, target, result, detail) VALUES(?,?,?,?,?,?)",
        (db_now(), ip, action, target, result, detail[:500]),
    )
