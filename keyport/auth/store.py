"""凭证读写：auths/workbuddy-<uid>.json（与 login.sh 落盘格式互通）。

嵌套形（官方插件格式，本模块写入与读取）：
  {
    "auth":    {"accessToken","refreshToken","expiresAt","domain","realm"},
    "account": {"uid","enterpriseId","nickname"},
    "device_token": "...",          # 可选
    "kind": "oauth"                 # 可选，apikey 时写入
  }

同时兼容扁平形（顶层直接放 accessToken/access_token/...），便于手工维护。

解析一律容错：字段类型异常（如 expiresAt 写成 ISO 字符串）不抛异常，
缺失/非法的可选字段回落默认值，只有「缺 accessToken」才算不可用。
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..upstream.base import AccountAuth

AUTH_FILE_GLOB = "workbuddy*.json"
KIND_OAUTH = "oauth"
KIND_APIKEY = "apikey"

# 超过该阈值视为毫秒时间戳（≈ 1973 年，秒级时间戳不会这么大）
_MS_THRESHOLD = 100_000_000_000


def _to_epoch(value: Any) -> int:
    """把多种时间表示统一成 Unix 秒；无法识别返回 0（绝不抛异常）。

    支持：int/float 秒或毫秒、纯数字字符串、ISO8601/RFC3339 字符串。
    """
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        num = float(value)
        return int(num / 1000) if num > _MS_THRESHOLD else int(num)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 0
        try:
            num = float(text)
        except ValueError:
            pass
        else:
            return int(num / 1000) if num > _MS_THRESHOLD else int(num)
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return 0
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    return 0


def _to_str(*values: Any) -> str:
    """取第一个非空值的字符串形式。"""
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _doc_fields(doc: dict) -> tuple[dict, dict]:
    """把嵌套形/扁平形统一成 (auth 字段字典, account 字段字典)。

    嵌套形：doc["auth"] / doc["account"]
    扁平形：字段直接在顶层（account 字段也从顶层取）
    """
    nested = doc.get("auth")
    if isinstance(nested, dict):
        account = doc.get("account")
        return nested, account if isinstance(account, dict) else {}
    return doc, doc


def parse_auth_file(raw: bytes | str) -> AccountAuth | None:
    """解析凭证为 AccountAuth；非法 JSON / 缺 accessToken 返回 None。

    字段类型异常（如 expiresAt 写成 ISO 字符串）一律容错，不抛异常。
    """
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        return None
    if not isinstance(doc, dict):
        return None

    auth_fields, acct_fields = _doc_fields(doc)

    access = _to_str(auth_fields.get("accessToken"), auth_fields.get("access_token"))
    if not access:
        return None

    refresh = _to_str(auth_fields.get("refreshToken"), auth_fields.get("refresh_token"))
    expires_at = _to_epoch(auth_fields.get("expiresAt", auth_fields.get("expires_at")))
    domain = _to_str(auth_fields.get("domain"))
    realm = _to_str(auth_fields.get("realm"))
    uid = _to_str(acct_fields.get("uid"), acct_fields.get("userId"))
    enterprise = _to_str(acct_fields.get("enterpriseId"), acct_fields.get("enterprise_id"))
    nickname = _to_str(acct_fields.get("nickname"))
    device_token = _to_str(doc.get("device_token"))
    # base_url / protocol：z.ai Coding Plan 等第三方 Anthropic 端点需要
    base_url = _to_str(doc.get("base_url"), auth_fields.get("base_url"))
    protocol = _to_str(doc.get("protocol"), auth_fields.get("protocol"))
    # 回退通道（同账号第二把钥匙）：Plan 通道 429/鉴权/风控时无缝切换
    fallback_token = _to_str(doc.get("fallback_token"), auth_fields.get("fallback_token"))
    fallback_base_url = _to_str(
        doc.get("fallback_base_url"), auth_fields.get("fallback_base_url")
    )

    # kind 必须往返：apikey 账号若被当成 oauth，会去调 refresh 端点而失败
    raw_kind = _to_str(doc.get("kind"), auth_fields.get("kind"))
    kind = KIND_APIKEY if raw_kind == KIND_APIKEY else KIND_OAUTH

    from .oauth import realm_from_domain

    return AccountAuth(
        uid=uid,
        realm=realm_from_domain(realm, domain),
        access_token=access,
        refresh_token=refresh,
        expires_at=expires_at,
        domain=domain,
        enterprise_id=enterprise,
        device_token=device_token,
        nickname=nickname,
        kind=kind,
        base_url=base_url,
        protocol=protocol or "openai",
        fallback_token=fallback_token,
        fallback_base_url=fallback_base_url,
    )


def parse_auth_file_verbose(raw: bytes | str) -> tuple[AccountAuth | None, str]:
    """同 parse_auth_file，但返回失败原因（供导入接口回报，便于排障）。"""
    try:
        doc = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
        return None, f"JSON 解析失败：{exc}"
    if not isinstance(doc, dict):
        return None, "顶层必须是 JSON 对象"
    auth_fields, _ = _doc_fields(doc)
    if not _to_str(auth_fields.get("accessToken"), auth_fields.get("access_token")):
        return None, "缺少 accessToken"
    parsed = parse_auth_file(raw)
    if parsed is None:
        return None, "解析失败"
    return parsed, ""


def to_auth_doc(auth: AccountAuth, *, device_token: str = "") -> dict:
    """构造嵌套形凭证文档（与官方插件格式一致）。"""
    doc: dict = {
        "auth": {
            "accessToken": auth.access_token,
            "refreshToken": auth.refresh_token,
            "expiresAt": auth.expires_at,
            "domain": auth.domain,
            "realm": auth.realm,
        },
        "account": {
            "uid": auth.uid,
            "enterpriseId": auth.enterprise_id,
            "nickname": auth.nickname,
        },
    }
    token = device_token or auth.device_token
    if token:
        doc["device_token"] = token
    # 仅 apikey 才写 kind：oauth 保持与官方插件格式完全一致（不多写字段）
    if auth.kind == KIND_APIKEY:
        doc["kind"] = KIND_APIKEY
    # 第三方 Anthropic 端点（z.ai Coding Plan）需要 base_url + protocol 才能回灌
    if auth.base_url:
        doc["base_url"] = auth.base_url
    if auth.protocol and auth.protocol != "openai":
        doc["protocol"] = auth.protocol
    # 回退通道凭证（同账号第二把钥匙），迁移时不可丢
    if auth.fallback_token:
        doc["fallback_token"] = auth.fallback_token
    if auth.fallback_base_url:
        doc["fallback_base_url"] = auth.fallback_base_url
    return doc


def write_auth_file(path: Path, auth: AccountAuth, *, device_token: str = "") -> None:
    """原子写回凭证文件（tmp + rename，权限 0600）。"""
    if not auth.access_token:
        raise ValueError("refusing to write empty access_token")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        json.dumps(to_auth_doc(auth, device_token=device_token), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        tmp.chmod(0o600)
    except OSError:
        pass  # Windows 不支持 POSIX 权限位
    tmp.replace(path)


def load_auth_dir(directory: Path) -> tuple[list[AccountAuth], list[str]]:
    """扫描凭证目录；返回 (账号列表, 失败文件名列表)。"""
    out: list[AccountAuth] = []
    failed: list[str] = []
    if not directory.is_dir():
        return out, failed
    for path in sorted(directory.glob(AUTH_FILE_GLOB)):
        try:
            auth = parse_auth_file(path.read_bytes())
        except OSError:
            failed.append(path.name)
            continue
        if auth is None:
            failed.append(path.name)
            continue
        auth.file_path = str(path)
        out.append(auth)
    return out, failed


def default_auth_path(directory: Path, uid: str) -> Path:
    return directory / f"workbuddy-{uid}.json"


def expires_at_from(expires_in: int, now: int | None = None) -> int:
    if expires_in <= 0:
        return 0
    return (now or int(time.time())) + expires_in
