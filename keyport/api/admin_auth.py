"""管理台会话鉴权：面板密码独立于 API Key，会话令牌落库只存摘要。"""

from __future__ import annotations

import time
from typing import Any

from fastapi import Header, Request

from ..db import Database, now as db_now
from ..security import (
    generate_session_token,
    hash_session_token,
    verify_password,
)

SESSION_COOKIE = "kp_session"
SESSION_HEADER = "x-kp-session"


class AdminAuth:
    def __init__(self, db: Database, cfg=None):
        self.db = db
        self.cfg = cfg

    def _config(self):
        if self.cfg is not None:
            return self.cfg
        from ..config import get_config

        return get_config()

    # ---- 登录 ----

    def login(self, username: str, password: str, ip: str = "") -> str | None:
        """校验面板密码，成功返回会话令牌（明文，仅此一次）。"""
        cfg = self._config()
        expected_user = cfg.security.admin_username or "admin"
        stored = cfg.security.admin_password_hash
        if not stored:
            return None  # 未设置密码 → 拒绝登录（由首启初始化）
        if username and username != expected_user:
            return None
        if not verify_password(password, stored):
            return None
        token = generate_session_token()
        ttl = cfg.security.session_ttl_seconds
        self.db.execute(
            "INSERT INTO admin_sessions(token_hash, username, created_at, expires_at, ip) "
            "VALUES(?,?,?,?,?)",
            (hash_session_token(token), expected_user, db_now(), db_now() + ttl, ip),
        )
        return token

    def verify(self, token: str) -> bool:
        """校验会话令牌（过期即清理）。"""
        if not token:
            return False
        row = self.db.query_one(
            "SELECT expires_at FROM admin_sessions WHERE token_hash=?",
            (hash_session_token(token),),
        )
        if row is None:
            return False
        if int(row["expires_at"]) <= db_now():
            self.logout(token)
            return False
        return True

    def logout(self, token: str) -> None:
        if token:
            self.db.execute(
                "DELETE FROM admin_sessions WHERE token_hash=?",
                (hash_session_token(token),),
            )

    def gc(self) -> int:
        cur = self.db.execute("DELETE FROM admin_sessions WHERE expires_at <= ?", (db_now(),))
        return int(cur.rowcount or 0)


def session_token(request: Request, x_kp_session: str = Header(default="")) -> str:
    """从请求提取会话令牌（Header 优先，回落 Cookie）。"""
    if x_kp_session:
        return x_kp_session.strip()
    cookie = request.cookies.get(SESSION_COOKIE)
    return (cookie or "").strip()


def require_admin(request: Request) -> Any:
    """FastAPI 依赖：未登录抛 401。"""
    from fastapi import HTTPException

    ctx = request.app.state.ctx
    token = session_token(request, request.headers.get(SESSION_HEADER, ""))
    if not ctx.admin_auth.verify(token):
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return token


def client_ip(request: Request) -> str:
    ctx = request.app.state.ctx
    from ..security import client_ip_from_request

    host = request.client.host if request.client else ""
    return client_ip_from_request(
        host, dict(request.headers), trust_proxy=ctx.cfg.security.trust_proxy_headers
    )


def bootstrap_admin_password(db: Database, cfg) -> str | None:
    """首启初始化面板密码：未设置时生成随机密码并打印到启动日志。

    返回生成的明文密码（仅在本次启动返回一次），已设置则返回 None。
    仅当配置确实来自磁盘文件时才落盘；临时构造的配置（测试）只改内存，
    避免把随机密码写进生产 config.json。
    """
    if cfg.security.admin_password_hash:
        return None
    import secrets

    from ..config import save_config
    from ..security import hash_password

    password = secrets.token_urlsafe(12)
    cfg.security.admin_password_hash = hash_password(password)
    if getattr(cfg, "source_path", None) is not None:
        save_config(cfg)
    return password


def is_expired(token_created: int, ttl: int, now: int | None = None) -> bool:
    return (now or int(time.time())) - token_created > ttl
