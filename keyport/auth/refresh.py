"""Token 保活与自动刷新：在请求前确保 access_token 有效。"""

from __future__ import annotations

import time

from ..config import Config
from ..pool.manager import AccountPool
from ..upstream.base import AccountAuth
from ..upstream.client import UpstreamClient
from .store import default_auth_path, write_auth_file


class TokenRefresher:
    """access_token 自动刷新（带单账号串行锁，避免并发重复刷新）。"""

    def __init__(self, cfg: Config, pool: AccountPool, client: UpstreamClient):
        self.cfg = cfg
        self.pool = pool
        self.client = client

    async def ensure_valid(self, auth: AccountAuth, *, skew: int = 120) -> AccountAuth:
        """过期（含 skew 余量）时刷新并写回；apiKey 账号直接返回。"""
        if auth.kind == "apikey" or not auth.refresh_token:
            return auth
        if not auth.token_expired(skew):
            return auth
        return await self.refresh(auth)

    async def refresh(self, auth: AccountAuth) -> AccountAuth:
        data = await self.client.refresh_token(auth)
        access = str(data.get("accessToken") or data.get("access_token") or "")
        refresh = str(data.get("refreshToken") or data.get("refresh_token") or auth.refresh_token)
        expires_in = int(data.get("expiresIn") or data.get("expires_in") or 0)
        domain = str(data.get("domain") or auth.domain)
        expires_at = int(time.time()) + expires_in if expires_in else auth.expires_at
        self.pool.update_tokens(auth.uid, access, refresh, expires_at, domain)
        auth.access_token = access
        auth.refresh_token = refresh
        auth.expires_at = expires_at
        if domain:
            auth.domain = domain
        # 回写凭证文件（若该账号来自文件）
        if auth.file_path:
            try:
                write_auth_file(
                    default_auth_path(self.cfg.auth_path(), auth.uid), auth
                )
            except (OSError, ValueError):
                pass
        return auth


def token_state(expires_at: int, now: int | None = None) -> str:
    """Token 有效期分档：expired / soon / tight / healthy。

    分档口径（对齐开发文档 §3.3.1）：
      expired 已过期；soon ≤ 24h；tight ≤ 7d；healthy 其余。
    """
    now = now or int(time.time())
    if not expires_at:
        return "healthy"  # apiKey / 无过期信息
    remaining = expires_at - now
    if remaining <= 0:
        return "expired"
    if remaining <= 24 * 3600:
        return "soon"
    if remaining <= 7 * 86400:
        return "tight"
    return "healthy"
