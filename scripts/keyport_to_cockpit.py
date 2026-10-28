#!/usr/bin/env python3
"""把 KeyPort 导出的账号包转换成 cockpit tools 可导入的 JSON。

KeyPort /accounts/export 的落盘结构是对象包：

    {"exported_at": ..., "version": 1, "count": N, "accounts": [{"auth": ..., "account": ...}, ...]}

而 cockpit panel 的 /panel/api/import/cockpit 直接
``json.Unmarshal(raw, &[]cockpitAccount)``，顶层必须是数组，否则报
``invalid json: json: cannot unmarshal object into Go value of type []panel.cockpitAccount``。

本脚本把 accounts 数组摊平到顶层，并做字段重命名与单位换算：

    auth.accessToken  -> access_token
    auth.refreshToken -> refresh_token
    auth.expiresAt    -> expires_at（秒 -> 毫秒，cockpit 导入侧会 /1000）
    auth.domain       -> domain（导入侧按 *.workbuddy.ai 推断 realm）
    account.uid       -> uid / id
    account.nickname  -> nickname（缺失时回落 email）
    JWT payload.email -> email（仅作展示，不校验签名）

用法：
    python scripts/keyport_to_cockpit.py keyport-accounts-2026-10-28.json
    python scripts/keyport_to_cockpit.py in.json -o out.json
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Any

UID_ALLOWED = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def jwt_claims(token: str) -> dict[str, Any]:
    """解出 JWT payload，仅用于取 email 等展示字段（不校验签名）。"""
    parts = token.split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        data = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def to_millis(value: Any) -> int:
    """KeyPort 落盘是秒级时间戳，cockpit 导入按毫秒 /1000 还原，已毫秒则原样保留。"""
    try:
        ts = int(value or 0)
    except (TypeError, ValueError):
        return 0
    if ts <= 0:
        return 0
    return ts if ts >= 10**12 else ts * 1000


def valid_uid(uid: str) -> bool:
    """与 panel.validImportUID 同口径：非空、<=64、仅字母数字和 -_。"""
    return bool(uid) and len(uid) <= 64 and set(uid) <= UID_ALLOWED


def is_cockpit_shape(item: dict[str, Any]) -> bool:
    """已经是 cockpit 单条结构（顶层 access_token/uid），无需再摊平。"""
    return ("auth" not in item and "account" not in item
            and any(key in item for key in ("access_token", "refresh_token", "uid")))


def normalize_cockpit(item: dict[str, Any]) -> dict[str, Any]:
    """已摊平的条目只做缺省补齐，保证二次转换幂等。"""
    def text(key: str) -> str:
        return str(item.get(key) or "").strip()

    uid = text("uid") or text("id")
    email = text("email")
    return {
        "id": text("id") or uid,
        "email": email,
        "uid": uid,
        "nickname": text("nickname") or email,
        "access_token": text("access_token"),
        "refresh_token": text("refresh_token"),
        "token_type": text("token_type") or "Bearer",
        "expires_at": to_millis(item.get("expires_at")),
        "domain": text("domain"),
        "dosage_notify_code": text("dosage_notify_code"),
        "payment_type": text("payment_type"),
        "status": text("status") or "active",
        "usage_updated_at": int(item.get("usage_updated_at") or 0),
        "last_checkin_time": int(item.get("last_checkin_time") or 0),
        "checkin_streak": int(item.get("checkin_streak") or 0),
        "created_at": int(item.get("created_at") or 0),
        "last_used": int(item.get("last_used") or 0),
    }


def convert_one(item: dict[str, Any]) -> dict[str, Any]:
    if is_cockpit_shape(item):
        return normalize_cockpit(item)

    auth = item.get("auth") if isinstance(item.get("auth"), dict) else {}
    account = item.get("account") if isinstance(item.get("account"), dict) else {}

    access_token = str(auth.get("accessToken") or "").strip()
    refresh_token = str(auth.get("refreshToken") or "").strip()
    claims = jwt_claims(access_token)

    uid = str(account.get("uid") or claims.get("sub") or "").strip()
    email = str(claims.get("email") or "").strip()
    nickname = str(account.get("nickname") or "").strip() or email

    return {
        "id": uid,
        "email": email,
        "uid": uid,
        "nickname": nickname,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "Bearer",
        "expires_at": to_millis(auth.get("expiresAt")),
        "domain": str(auth.get("domain") or "").strip(),
        "dosage_notify_code": "",
        "payment_type": "",
        "status": "active",
        "usage_updated_at": 0,
        "last_checkin_time": 0,
        "checkin_streak": 0,
        "created_at": 0,
        "last_used": 0,
    }


def load_accounts(path: Path) -> list[Any]:
    """接受 KeyPort 导出包（含 accounts）或已是顶层数组的文件。"""
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        accounts = data.get("accounts")
        if isinstance(accounts, list):
            return accounts
        # 单条账号也放行，方便点位修复
        if "auth" in data or "account" in data:
            return [data]
    raise SystemExit(f"{path}: 未找到 accounts 数组，无法转换")


def default_output(src: Path) -> Path:
    return src.with_name(f"{src.stem}-cockpit.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="KeyPort 导出包 -> cockpit panel 导入数组")
    parser.add_argument("input", type=Path, help="KeyPort 导出的 JSON 文件")
    parser.add_argument("-o", "--output", type=Path,
                        help="输出文件（默认 <input>-cockpit.json）")
    args = parser.parse_args(argv)

    src: Path = args.input
    if not src.is_file():
        raise SystemExit(f"文件不存在: {src}")

    out: list[dict[str, Any]] = []
    skipped: list[str] = []
    for index, item in enumerate(load_accounts(src)):
        if not isinstance(item, dict):
            skipped.append(f"[{index}] 不是对象")
            continue
        doc = convert_one(item)
        missing = [key for key in ("uid", "access_token", "refresh_token")
                   if not doc[key]]
        if missing:
            skipped.append(f"[{index}] 缺少 {','.join(missing)}（uid={doc['uid'] or '-'}）")
            continue
        if not valid_uid(doc["uid"]):
            skipped.append(f"[{index}] uid 非法（{doc['uid']}）")
            continue
        out.append(doc)

    dst = args.output or default_output(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")

    print(f"输入 {src}")
    print(f"输出 {dst}（顶层数组，{len(out)} 条）")
    if skipped:
        print(f"跳过 {len(skipped)} 条：")
        for line in skipped:
            print(f"  - {line}")
    return 0 if out else 1


if __name__ == "__main__":
    sys.exit(main())
