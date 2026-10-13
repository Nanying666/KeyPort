"""管理台 API：多 API Key 分发（创建/启停/删除、模型白名单、配额、IP 限制）。"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..db import now as db_now
from ..security import generate_api_key, hash_api_key, key_prefix, validate_cidr_list
from .admin_auth import client_ip, require_admin
from .deps import get_ctx
from .logging import record_audit

router = APIRouter(prefix="/api/keys", tags=["keys"])


def _as_list(raw: str) -> list:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _key_view(row) -> dict:
    return {
        "id": int(row["id"]),
        "name": row["name"],
        "key_prefix": row["key_prefix"],
        "enabled": bool(row["enabled"]),
        "realm": row["realm"] or "",
        "model_allow": _as_list(row["model_allow"]),
        "quota_credits": float(row["quota_credits"] or 0),
        "used_credits": float(row["used_credits"] or 0),
        "quota_tokens": int(row["quota_tokens"] or 0),
        "used_tokens": int(row["used_tokens"] or 0),
        "ip_allow": _as_list(row["ip_allow"]),
        "ip_block": _as_list(row["ip_block"]),
        "note": row["note"] or "",
        "created_at": int(row["created_at"] or 0),
        "last_used_at": int(row["last_used_at"] or 0),
    }


@router.get("")
async def list_keys(request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    rows = ctx.db.query("SELECT * FROM api_keys ORDER BY id DESC")
    return {"keys": [_key_view(r) for r in rows]}


@router.post("")
async def create_key(request: Request, _: str = Depends(require_admin)):
    """创建密钥：明文仅此一次返回。"""
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    name = str(payload.get("name") or "default")
    realm = str(payload.get("realm") or "")
    model_allow = payload.get("model_allow") or []
    ip_allow = payload.get("ip_allow") or []
    ip_block = payload.get("ip_block") or []
    for rules, label in ((ip_allow, "ip_allow"), (ip_block, "ip_block")):
        bad = validate_cidr_list(rules)
        if bad:
            raise HTTPException(status_code=400, detail=f"{label} 含非法 CIDR：{bad}")
    if realm and ctx.cfg.realm(realm) is None:
        raise HTTPException(status_code=400, detail=f"未知账号域：{realm}")

    raw = generate_api_key()
    cur = ctx.db.execute(
        """
        INSERT INTO api_keys(name, key_hash, key_prefix, enabled, realm, model_allow,
            quota_credits, quota_tokens, ip_allow, ip_block, note, created_at)
        VALUES(?,?,?,1,?,?,?,?,?,?,?,?)
        """,
        (
            name, hash_api_key(raw), key_prefix(raw), realm,
            json.dumps(model_allow, ensure_ascii=False),
            float(payload.get("quota_credits") or 0),
            int(payload.get("quota_tokens") or 0),
            json.dumps(ip_allow, ensure_ascii=False),
            json.dumps(ip_block, ensure_ascii=False),
            str(payload.get("note") or ""), db_now(),
        ),
    )
    record_audit(ctx.db, client_ip(request), "key_create", name, "ok")
    return {"ok": True, "id": int(cur.lastrowid or 0), "key": raw,
            "notice": "明文仅本次返回，请立即保存"}


@router.patch("/{key_id}")
async def update_key(key_id: int, request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    row = ctx.db.query_one("SELECT * FROM api_keys WHERE id=?", (key_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="密钥不存在")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    fields: dict[str, object] = {}
    if "name" in payload:
        fields["name"] = str(payload["name"])
    if "enabled" in payload:
        fields["enabled"] = 1 if payload["enabled"] else 0
    if "realm" in payload:
        realm = str(payload["realm"])
        if realm and ctx.cfg.realm(realm) is None:
            raise HTTPException(status_code=400, detail=f"未知账号域：{realm}")
        fields["realm"] = realm
    for field_name in ("model_allow", "ip_allow", "ip_block"):
        if field_name in payload:
            value = payload[field_name] or []
            if field_name in ("ip_allow", "ip_block"):
                bad = validate_cidr_list(value)
                if bad:
                    raise HTTPException(status_code=400, detail=f"{field_name} 含非法 CIDR：{bad}")
            fields[field_name] = json.dumps(value, ensure_ascii=False)
    if "quota_credits" in payload:
        fields["quota_credits"] = float(payload["quota_credits"] or 0)
    if "quota_tokens" in payload:
        fields["quota_tokens"] = int(payload["quota_tokens"] or 0)
    if "note" in payload:
        fields["note"] = str(payload["note"])
    if payload.get("reset_usage"):
        fields["used_credits"] = 0.0
        fields["used_tokens"] = 0

    if fields:
        assignments = ", ".join(f"{k}=?" for k in fields)
        ctx.db.execute(f"UPDATE api_keys SET {assignments} WHERE id=?",
                       (*fields.values(), key_id))
    record_audit(ctx.db, client_ip(request), "key_update", str(key_id), "ok")
    row = ctx.db.query_one("SELECT * FROM api_keys WHERE id=?", (key_id,))
    return {"ok": True, "key": _key_view(row)}


@router.delete("/{key_id}")
async def delete_key(key_id: int, request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    cur = ctx.db.execute("DELETE FROM api_keys WHERE id=?", (key_id,))
    if not cur.rowcount:
        raise HTTPException(status_code=404, detail="密钥不存在")
    record_audit(ctx.db, client_ip(request), "key_delete", str(key_id), "ok")
    return {"ok": True, "id": key_id}


@router.post("/{key_id}/reveal-check")
async def reveal_check(key_id: int, request: Request, _: str = Depends(require_admin)):
    """明文不可再取回：明确告知前端（安全设计，非缺陷）。"""
    ctx = get_ctx(request)
    row = ctx.db.query_one("SELECT id FROM api_keys WHERE id=?", (key_id,))
    if row is None:
        raise HTTPException(status_code=404, detail="密钥不存在")
    return JSONResponse(
        status_code=200,
        content={"retrievable": False,
                 "detail": "密钥明文仅在创建时展示一次，服务端只保存 SHA-256 摘要"},
    )
