"""安全原语：面板密码 PBKDF2-SHA256、API Key 哈希、CIDR/IP 匹配、会话令牌。

设计要点：
- 面板密码与 API Key 均不落明文；密码用 PBKDF2-SHA256 加盐迭代，API Key 用 SHA-256。
- 比对一律使用 hmac.compare_digest 防时序侧信道。
- 会话令牌为服务端随机 32 字节，落库只存 SHA-256 摘要。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
from dataclasses import dataclass

PBKDF2_ITERATIONS = 210_000
PBKDF2_ALGO = "pbkdf2_sha256"
_SALT_BYTES = 16


# ---- 面板密码 ----


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """返回 `pbkdf2_sha256$iterations$salt_b64$hash_b64` 形式的摘要串。"""
    if not isinstance(password, str) or password == "":
        raise ValueError("password must be a non-empty string")
    salt = os.urandom(_SALT_BYTES)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "{}${}${}${}".format(
        PBKDF2_ALGO,
        iterations,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(dk).decode("ascii"),
    )


def verify_password(password: str, stored: str) -> bool:
    """校验密码与摘要是否匹配；摘要损坏/格式非法一律返回 False。"""
    if not password or not stored:
        return False
    try:
        algo, iters_s, salt_b64, hash_b64 = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        iterations = int(iters_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(dk, expected)


# ---- API Key ----


def generate_api_key(prefix: str = "sk-kp") -> str:
    """生成明文 API Key（仅在创建时展示一次）。"""
    return f"{prefix}-{secrets.token_urlsafe(32)}"


def hash_api_key(key: str) -> str:
    """API Key 摘要：高熵随机串用单轮 SHA-256 足够（无需慢哈希）。"""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(key: str, stored_hash: str) -> bool:
    if not key or not stored_hash:
        return False
    return hmac.compare_digest(hash_api_key(key), stored_hash)


def key_prefix(key: str, keep: int = 12) -> str:
    """取明文前缀用于列表展示（不足以还原完整 Key）。"""
    return key[:keep] if len(key) > keep else key


# ---- 会话令牌 ----


def generate_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_session_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# ---- IP / CIDR ----


def _parse_rule(rule: str) -> ipaddress.IPv4Network | ipaddress.IPv6Network | None:
    rule = (rule or "").strip()
    if not rule:
        return None
    try:
        # 裸 IP 自动补 /32 或 /128
        return ipaddress.ip_network(rule, strict=False)
    except ValueError:
        return None


def ip_matches(ip: str, rules: list[str]) -> bool:
    """ip 是否命中任一 CIDR/单 IP 规则。规则非法则忽略。"""
    try:
        addr = ipaddress.ip_address((ip or "").strip())
    except ValueError:
        return False
    for rule in rules:
        net = _parse_rule(rule)
        if net is not None and addr.version == net.version and addr in net:
            return True
    return False


def validate_cidr_list(rules: list[str]) -> list[str]:
    """返回非法规则清单（供管理台保存前校验）。"""
    return [r for r in rules if _parse_rule(r) is None]


@dataclass
class IpDecision:
    allowed: bool
    reason: str


def evaluate_ip(ip: str, allowlist: list[str], blocklist: list[str]) -> IpDecision:
    """IP 准入判定：黑名单优先，其次白名单（白名单非空即默认拒绝）。"""
    if ip_matches(ip, blocklist):
        return IpDecision(False, "blocklist")
    if allowlist:
        if ip_matches(ip, allowlist):
            return IpDecision(True, "allowlist")
        return IpDecision(False, "not_in_allowlist")
    return IpDecision(True, "default_allow")


def client_ip_from_request(client_host: str | None, headers: dict[str, str], *, trust_proxy: bool) -> str:
    """解析客户端 IP。

    trust_proxy=False（默认）时只用 TCP 对端地址，避免伪造 XFF 绕过 IP 管控。
    trust_proxy=True 时取 X-Forwarded-For 首段，回落 X-Real-IP。
    """
    if trust_proxy:
        xff = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or ""
        if xff:
            first = xff.split(",")[0].strip()
            if first:
                return first
        real = (headers.get("x-real-ip") or headers.get("X-Real-IP") or "").strip()
        if real:
            return real
    return (client_host or "").strip()
