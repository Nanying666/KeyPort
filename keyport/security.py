"""安全原语：面板密码 PBKDF2-SHA256、API Key 哈希、CIDR/IP 匹配、会话令牌。

设计要点：
- 面板密码与 API Key 均不落明文；密码用 PBKDF2-SHA256 加盐迭代，API Key 用 SHA-256。
- 比对一律使用 hmac.compare_digest 防时序侧信道。
- 会话令牌为服务端随机 32 字节，落库只存 SHA-256 摘要。
"""

from __future__ import annotations

import base64
import fnmatch
import hashlib
import hmac
import ipaddress
import os
import secrets
import urllib.parse
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


def ip_in_rules(ip: str, rules: list[str]) -> bool:
    """`ip` 是否落在任一 CIDR / 单 IP 规则内。规则非法则忽略该条。"""
    return ip_matches(ip, rules)


def client_ip_from_request(
    client_host: str | None,
    headers: dict[str, str],
    *,
    trust_proxy: bool,
    trusted_proxies: list[str] | None = None,
) -> str:
    """解析客户端 IP。

    ## 为什么不能取 XFF 首段

    `X-Forwarded-For` 是**客户端自己写的**：`XFF: 1.2.3.4, <代理追加的真实IP>`。
    取首段等于让请求方任意声明自己的 IP —— 审计日志会被污染（查不到真实来源），
    IP 白名单也会被一行请求头绕过。

    正确做法有两道闸：

    1. **对端必须是可信代理**：TCP 对端不在 `trusted_proxies` 里时，XFF 一律
       忽略，只用 TCP 对端地址。公网客户端伪造的 XFF 到不了这里。
    2. **从右往左取第一个不可信地址**：可信代理会把自己看到的对端**追加**到
       XFF 尾部，所以最右侧才是可信信息；左侧全是请求方自己写的。
       逐跳跳过可信代理，取第一个非代理地址即为真实客户端。

    `trusted_proxies` 为空（未声明）时退化为「完全不采信 XFF」—— 这是安全的
    默认：宁可记录代理地址，也不记录一个可伪造的值。
    """
    host = (client_host or "").strip()
    if not trust_proxy:
        return host
    proxies = list(trusted_proxies or [])
    if not proxies:
        return host
    if not ip_in_rules(host, proxies):
        return host  # 对端不是可信代理 → 请求头里的 XFF 一概不采信

    xff = (headers.get("x-forwarded-for") or headers.get("X-Forwarded-For") or "")
    chain = [part.strip() for part in xff.split(",") if part.strip()]
    for addr in reversed(chain):
        if not ip_in_rules(addr, proxies):
            return addr
    # 整条链都是可信代理（或 XFF 为空）→ 回落 X-Real-IP / TCP 对端
    real = (headers.get("x-real-ip") or headers.get("X-Real-IP") or "").strip()
    if real and not ip_in_rules(real, proxies):
        return real
    return host


#: 弱口令黑名单（命中即拒绝设置）。只挡最常见的，不追求完备 ——
#: 真正的强度来自「够长」，所以主判据是长度与字符多样性。
_COMMON_PASSWORDS = frozenset({
    "admin", "administrator", "password", "passw0rd", "p@ssw0rd", "changeme",
    "123456", "1234567", "12345678", "123456789", "1234567890", "qwerty",
    "qwertyuiop", "abc123", "letmein", "welcome", "monkey", "dragon",
    "iloveyou", "trustno1", "sunshine", "master", "secret", "root", "toor",
    "111111", "000000", "666666", "888888", "admin123", "admin888",
    "admin@123", "admin123456", "admin1234", "password1", "password12",
    "password123", "password1234", "passw0rd1", "qwerty123", "1qaz2wsx",
    "keyport", "keyport123", "workbuddy", "codebuddy", "letmein123",
})


def password_strength_error(password: str, username: str = "") -> str:
    """检查新密码强度；返回错误文案（空串 = 通过）。

    判据：长度 ≥ 12、至少两类字符、不在常见弱口令表内、不等于用户名。
    """
    if not isinstance(password, str):
        return "密码必须是字符串"
    if len(password) < 12:
        return "密码至少 12 位"
    if password.lower() in _COMMON_PASSWORDS:
        return "密码过于常见，请更换"
    if username and password.lower() == username.lower():
        return "密码不能与用户名相同"
    kinds = sum((
        any(c.islower() for c in password),
        any(c.isupper() for c in password),
        any(c.isdigit() for c in password),
        any(not c.isalnum() for c in password),
    ))
    if kinds < 2:
        return "密码需包含字母、数字、符号中的至少两类"
    return ""


# ---- 账号自带 base_url 的校验 ----
#
# ## 为什么需要这道闸
#
# 账号的 `base_url` 决定网关把该账号的请求（**连同真实 access token**）发到哪里。
# 一个能写 base_url 的人，等于能把任意账号的令牌导向自己的服务器 —— 2026-10-16
# 的入侵就是这样做的：通过 `/api/accounts/apikey` 植入
# `base_url=http://<攻击者IP>:9999/adopt4` 的账号。
#
# 两道措施：
#   1. **写入时校验**（本函数）：`base_url_allow` 非空时只放行白名单内的 host；
#      为空时放行但由调用方留告警 + 审计。
#   2. **运行时排除**：白名单非空时，带非白名单 base_url 的账号既**不参与选号**、
#      也**不执行定时任务** —— 堵住「静默外带」（定时任务对禁用号照常执行，
#      光靠禁用挡不住它）。

#: 永远拒绝的 URL scheme（base_url 只该是 HTTP(S) 上游端点）
_BAD_SCHEMES = ("file", "gopher", "dict", "ftp", "data", "javascript")


def base_url_violation(url: str, allow: list[str] | None) -> str:
    """校验账号 `base_url`；返回违规原因（空串 = 通过）。

    - `url` 为空（未指定，走域默认端点）→ 通过
    - scheme 非 http/https → 拒绝（这类 URL 不是上游 API 端点）
    - `allow` 为空 → 通过（未声明白名单，无从判断「异常」；调用方应留告警）
    - `allow` 非空 → host / host:port 需命中任一条（支持 `*.example.com` 通配）
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urllib.parse.urlparse(raw)
    except ValueError as exc:
        return f"base_url 无法解析：{exc}"
    scheme = (parsed.scheme or "").lower()
    if scheme in _BAD_SCHEMES:
        return f"base_url 的 scheme 不被允许：{scheme}"
    if scheme not in ("http", "https"):
        return f"base_url 必须是 http/https，当前为 {scheme or '(空)'}"
    host = (parsed.hostname or "").lower()
    if not host:
        return "base_url 缺少 host"

    entries = [str(e).strip().lower() for e in (allow or []) if str(e).strip()]
    if not entries:
        return ""  # 未声明白名单：放行，由调用方告警

    candidates = {host, f"{host}:{parsed.port}" if parsed.port else host}
    for rule in entries:
        rule_host = rule.split("//")[-1].split("/")[0]
        for candidate in candidates:
            if candidate == rule_host or fnmatch.fnmatch(candidate, rule_host):
                return ""
    return f"base_url 的 host 不在白名单内：{host}"


def base_url_allowed(url: str, allow: list[str] | None) -> bool:
    """`base_url` 是否被允许（白名单为空时恒为 True）。"""
    return base_url_violation(url, allow) == ""
