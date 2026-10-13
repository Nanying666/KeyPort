"""会话粘性：同一会话尽量绑定同一账号，保证多轮上下文不跳号。

- TTL 滚动续期：每次命中刷新过期时间
- 失败自动解绑：上游报错换号后清除绑定
- 可选 Redis 镜像：配置 session_sticky.redis_url 时把绑定镜像到 Redis，防重启丢失
"""

from __future__ import annotations

import threading
import time

from ..config import SessionStickyConfig


class StickyStore:
    """进程内会话绑定表（可选 Redis 镜像）。"""

    def __init__(self, cfg: SessionStickyConfig):
        self.cfg = cfg
        self._lock = threading.RLock()
        self._map: dict[str, tuple[str, int]] = {}  # conv_id -> (uid, expire_at)
        self._redis = None
        if cfg.redis_url:
            self._redis = self._connect_redis(cfg.redis_url)

    @staticmethod
    def _connect_redis(url: str):
        """尽力连接 Redis；不可用时静默降级为纯内存（不阻断启动）。"""
        try:
            import redis  # type: ignore

            client = redis.Redis.from_url(url, decode_responses=True)
            client.ping()
            return client
        except Exception:
            return None

    @property
    def redis_available(self) -> bool:
        return self._redis is not None

    def _key(self, conv_id: str) -> str:
        return f"keyport:sticky:{conv_id}"

    def get(self, conv_id: str) -> str:
        """取绑定的账号 uid；过期/未绑定返回空串。"""
        if not conv_id or not self.cfg.enabled:
            return ""
        now = int(time.time())
        if self._redis is not None:
            try:
                uid = self._redis.get(self._key(conv_id))
                if uid:
                    self._redis.expire(self._key(conv_id), self.cfg.ttl_seconds)
                    return str(uid)
            except Exception:
                pass  # 镜像不可用时回落内存表
        with self._lock:
            item = self._map.get(conv_id)
            if not item:
                return ""
            uid, expire_at = item
            if expire_at <= now:
                self._map.pop(conv_id, None)
                return ""
            # TTL 滚动续期
            self._map[conv_id] = (uid, now + self.cfg.ttl_seconds)
            return uid

    def bind(self, conv_id: str, uid: str) -> None:
        if not conv_id or not uid or not self.cfg.enabled:
            return
        now = int(time.time())
        with self._lock:
            self._map[conv_id] = (uid, now + self.cfg.ttl_seconds)
        if self._redis is not None:
            try:
                self._redis.setex(self._key(conv_id), self.cfg.ttl_seconds, uid)
            except Exception:
                pass

    def unbind(self, conv_id: str) -> None:
        """失败自动解绑。"""
        if not conv_id:
            return
        with self._lock:
            self._map.pop(conv_id, None)
        if self._redis is not None:
            try:
                self._redis.delete(self._key(conv_id))
            except Exception:
                pass

    def gc(self) -> int:
        """清理过期绑定；返回清理条数。"""
        now = int(time.time())
        with self._lock:
            stale = [k for k, (_, exp) in self._map.items() if exp <= now]
            for k in stale:
                self._map.pop(k, None)
        return len(stale)

    def size(self) -> int:
        with self._lock:
            return len(self._map)
