"""管理台 API：登录、账号管理、OAuth 纳管、导入导出。"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..auth.oauth import OAuthClient, api_key_account
from ..auth.refresh import token_state
from ..auth.store import (
    default_auth_path,
    load_auth_dir,
    parse_auth_file_verbose,
    to_auth_doc,
    write_auth_file,
)
from ..auth.zcode import is_zcode_bundle
from ..config import save_config
from ..db import now as db_now
from ..upstream.base import UpstreamError
from ..upstream.claim import ClaimError, is_zcode_plan_account
from .admin_auth import SESSION_COOKIE, bootstrap_admin_password, client_ip, require_admin
from .deps import get_ctx
from .logging import record_audit

router = APIRouter(prefix="/api", tags=["admin"])

# OAuth 待完成登录态（state → LoginState），内存即可（重启丢失只需重试）
_LOGIN_TTL = 600


def _login_states(ctx) -> dict[str, Any]:
    now = time.time()
    for key in [k for k, v in ctx.login_states.items() if now - v.get("created_at", 0) > _LOGIN_TTL]:
        ctx.login_states.pop(key, None)
    return ctx.login_states


# ---- 登录 ----


@router.post("/login")
async def login(request: Request):
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    username = str(payload.get("username", "admin"))
    password = str(payload.get("password", ""))
    ip = client_ip(request)

    # 首启未设置密码：生成随机密码并在响应中提示一次
    generated = bootstrap_admin_password(ctx.db, ctx.cfg)
    token = ctx.admin_auth.login(username, password, ip)
    record_audit(ctx.db, ip, "admin_login", username, "ok" if token else "fail")
    if token is None:
        if generated:
            return JSONResponse(
                status_code=401,
                content={"detail": "面板密码尚未设置，已生成初始密码，请查看服务启动日志",
                         "initial_password_issued": True},
            )
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    resp = JSONResponse(content={"ok": True, "username": username})
    resp.set_cookie(
        SESSION_COOKIE, token, httponly=True, samesite="lax",
        max_age=ctx.cfg.security.session_ttl_seconds,
    )
    return resp


@router.post("/logout")
async def logout(request: Request):
    ctx = get_ctx(request)
    token = request.headers.get("x-kp-session") or request.cookies.get(SESSION_COOKIE) or ""
    ctx.admin_auth.logout(token)
    resp = JSONResponse(content={"ok": True})
    resp.delete_cookie(SESSION_COOKIE)
    return resp


@router.get("/session")
async def session_info(request: Request):
    ctx = get_ctx(request)
    token = request.headers.get("x-kp-session") or request.cookies.get(SESSION_COOKIE) or ""
    return {"authenticated": ctx.admin_auth.verify(token)}


@router.post("/password")
async def change_password(request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    from ..security import hash_password, verify_password

    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    old = str(payload.get("old_password", ""))
    new = str(payload.get("new_password", ""))
    if len(new) < 8:
        raise HTTPException(status_code=400, detail="新密码至少 8 位")
    if not verify_password(old, ctx.cfg.security.admin_password_hash):
        raise HTTPException(status_code=400, detail="原密码错误")
    ctx.cfg.security.admin_password_hash = hash_password(new)
    save_config(ctx.cfg)
    record_audit(ctx.db, client_ip(request), "change_password", "", "ok")
    return {"ok": True}


# ---- 账号：列表 / 概览 ----


def _account_view(ctx, entry) -> dict[str, Any]:
    now = int(time.time())
    return {
        "uid": entry.uid,
        "realm": entry.realm,
        "nickname": entry.nickname,
        "kind": entry.kind,
        "enabled": entry.enabled,
        "manual_disabled": entry.manual_disabled,
        "in_pool": entry.in_pool(),
        "healthy": entry.healthy(now),
        "status": entry.cooldown_reason(now) or "ok",
        "cooldown_remaining": entry.cooldown_remaining(now),
        "disabled_reason": entry.disabled_reason,
        "credits": entry.credits,
        "credits_used": entry.credits_used,
        "credits_expiring": entry.credits_expiring,
        "credits_updated_at": entry.credits_updated_at,
        "total_calls": entry.total_calls,
        "failed_calls": entry.failed_calls,
        "consecutive_failures": entry.consecutive_failures,
        "inflight": entry.inflight,
        "last_used_at": entry.last_used_at,
        "last_error": entry.last_error,
        "last_checkin_at": entry.last_checkin_at,
        "token_expires_at": entry.token_expires_at,
        "token_remaining": max(0, entry.token_expires_at - now) if entry.token_expires_at else 0,
        "token_state": token_state(entry.token_expires_at, now),
        "model_cooldowns": entry.model_cooldowns,
        "risk_strikes": entry.risk_strikes,
        "last_risk_at": entry.last_risk_at,
        "fingerprint": dict(entry.fingerprint or {}),
        "installed_at": entry.installed_at,
        # ZCode 平台标记：管理台据此把账号分到两个列表
        # （WorkBuddy / CodeBuddy 池 vs ZCode 池，后者才带设备档案与安装序）
        "zcode_platform": is_zcode_plan_account(ctx.pool.auth_for(entry)),
    }


async def _post_add(ctx, auth, *, label: str) -> dict[str, Any]:
    """新账号入池后的统一收尾：安装序 + 自动领取（均不阻断入池）。

    **仅 ZCode 平台账号**（`realm=zai` 且 base_url 指向 Coding Plan 网关）才执行：
    套餐领取是 Coding Plan 专属权益，激活上报与安装序模拟的是 ZCode 桌面端行为。
    WorkBuddy / CodeBuddy 账号加入时这两步整体跳过 —— 它们打上游 billing 只会 404，
    激活上报更是伪造无关信号。

    两件事都必须跑在「已落库」之后：安装序要按该账号自己的设备档案上报，
    自动领取要先有可打 billing 的凭证。
    """
    from ..upstream.claim import is_zcode_plan_account

    out: dict[str, Any] = {}
    if not is_zcode_plan_account(auth):
        out["zcode_platform"] = False
        out["skipped_reason"] = "非 ZCode 平台账号，不执行安装序与套餐领取"
        return out

    out["zcode_platform"] = True
    cfg = ctx.cfg
    if cfg.install.enabled and cfg.install.auto_on_add:
        try:
            from ..upstream.install import run_install_sequence_for_account

            result = await run_install_sequence_for_account(
                ctx.client, auth, installed_at=0,
                timeout=cfg.install.timeout_seconds,
            )
            if result.get("installed"):
                ctx.pool.mark_installed(auth.uid, int(time.time()))
            out["install"] = {
                "installed": bool(result.get("installed")),
                "configs_fetched": bool(result.get("configs_fetched")),
                "events": result.get("events_reported") or [],
                "errors": result.get("errors") or [],
            }
        except Exception as exc:  # noqa: BLE001 - 安装序失败不影响入池
            out["install"] = {"installed": False, "errors": [str(exc)]}

    if cfg.claim.enabled and cfg.claim.auto_on_add and ctx.claims is not None:
        try:
            outcomes = await ctx.claims.auto_claim_all(auth)
            out["claim"] = outcomes
        except Exception as exc:  # noqa: BLE001 - 领取失败不影响入池
            out["claim"] = [{"uid": auth.uid, "ok": False, "message": str(exc)}]

    record_audit(ctx.db, "", f"{label}_post_add", auth.uid, "ok")
    return out


@router.get("/accounts")
async def list_accounts(request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    entries = sorted(ctx.pool.all_entries(), key=lambda e: (e.realm, e.uid))
    return {
        "accounts": [_account_view(ctx, e) for e in entries],
        "summary": ctx.pool.summary(),
        "realms": ctx.cfg.enabled_realms(),
    }


@router.get("/selection/candidates")
async def selection_candidates(
    request: Request, realm: str = "", model: str = "",
    _: str = Depends(require_admin),
):
    """候选队列预览：**只读**，复现「如果现在发一个请求，会怎么排」。

    排障时最常见的问题不是"选号坏了"，而是"为什么总选它 / 为什么跳过它" ——
    此前只能靠读代码猜。
    """
    ctx = get_ctx(request)
    rows = ctx.pool.preview_candidates(realm=realm, model=model)
    return {
        "realm": realm,
        "model": model,
        "candidates": rows,
        "notice": "只读预览，不写入任何状态；条件探索会写状态故不体现",
    }


@router.get("/selection/balance")
async def selection_balance(
    request: Request, hours: int = 24, _: str = Depends(require_admin),
):
    """选号均衡度：偏斜倍数 / 覆盖率 / 判定与排查建议。"""
    ctx = get_ctx(request)
    return ctx.pool.selection_balance(hours)


@router.get("/overview")
async def overview(request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    db = ctx.db
    day_ago = db_now() - 86400
    stats = db.query_one(
        "SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS tokens, "
        "COALESCE(SUM(credits),0) AS credits, "
        "COALESCE(AVG(NULLIF(ttfb_ms,0)),0) AS ttfb "
        "FROM request_logs WHERE ts >= ?",
        (day_ago,),
    )
    errs = db.query_one(
        "SELECT COUNT(*) AS n FROM request_logs WHERE ts >= ? AND status >= 400", (day_ago,)
    )
    by_realm = db.query(
        "SELECT realm, COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS tokens "
        "FROM request_logs WHERE ts >= ? GROUP BY realm",
        (day_ago,),
    )
    return {
        "pool": ctx.pool.summary(),
        "last_24h": {
            "calls": int(stats["calls"] or 0),
            "tokens": int(stats["tokens"] or 0),
            "credits": float(stats["credits"] or 0),
            "avg_ttfb_ms": int(stats["ttfb"] or 0),
            "errors": int(errs["n"] or 0),
        },
        "by_realm": [dict(r) for r in by_realm],
        "realms": ctx.cfg.enabled_realms(),
        "scheduler": ctx.scheduler.status() if ctx.scheduler else None,
    }


# ---- 账号：运维操作 ----


@router.post("/accounts/{uid}/disable")
async def disable_account(uid: str, request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    reason = "manual"
    try:
        payload = await request.json()
        reason = str(payload.get("reason") or "manual")
    except ValueError:
        pass
    if not ctx.pool.set_manual_disabled(uid, True, reason):
        raise HTTPException(status_code=404, detail="账号不存在")
    record_audit(ctx.db, client_ip(request), "account_disable", uid, "ok", reason)
    return {"ok": True, "uid": uid, "manual_disabled": True}


@router.post("/accounts/{uid}/enable")
async def enable_account(uid: str, request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    if not ctx.pool.set_manual_disabled(uid, False):
        raise HTTPException(status_code=404, detail="账号不存在")
    record_audit(ctx.db, client_ip(request), "account_enable", uid, "ok")
    return {"ok": True, "uid": uid, "manual_disabled": False}


@router.post("/accounts/{uid}/revive")
async def revive_account(uid: str, request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    if not ctx.pool.revive_account(uid):
        raise HTTPException(status_code=404, detail="账号不存在")
    record_audit(ctx.db, client_ip(request), "account_revive", uid, "ok")
    return {"ok": True, "uid": uid}


@router.delete("/accounts/{uid}")
async def delete_account(uid: str, request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    if not ctx.pool.remove_account(uid):
        raise HTTPException(status_code=404, detail="账号不存在")
    record_audit(ctx.db, client_ip(request), "account_delete", uid, "ok")
    return {"ok": True, "uid": uid}


@router.post("/accounts/{uid}/refresh-credits")
async def refresh_credits(uid: str, request: Request, _: str = Depends(require_admin)):
    """直连上游刷新单个账号额度。"""
    ctx = get_ctx(request)
    entry = ctx.pool.get(uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    auth = await ctx.refresher.ensure_valid(ctx.pool.auth_for(entry))
    try:
        data = await ctx.client.fetch_credits(auth)
    except UpstreamError as exc:
        return JSONResponse(status_code=502, content={"detail": exc.message or "刷新失败"})
    ctx.pool.update_credits(
        uid, int(data.get("credits", entry.credits)),
        int(data.get("expiring", entry.credits_expiring)),
        data.get("used"),
    )
    return {"ok": True, "uid": uid, "credits": data}


# ---- OAuth 一键登录（免本地桌面客户端） ----


@router.post("/oauth/start")
async def oauth_start(request: Request, _: str = Depends(require_admin)):
    """取授权 URL，返回给前端打开；登录态保存在内存待轮询。

    `realm=zai` 走官方 CLI OAuth 协议（init → poll，服务端下发 poll_token）；
    其余域走 WorkBuddy / CodeBuddy 的设备授权流程。
    """
    ctx = get_ctx(request)
    realm = "cn"
    try:
        payload = await request.json()
        realm = str(payload.get("realm") or "cn")
    except ValueError:
        pass
    if ctx.cfg.realm(realm) is None:
        raise HTTPException(status_code=400, detail=f"未知账号域：{realm}")

    states = _login_states(ctx)

    if realm == "zai":
        from ..auth.oauth import ZaiOAuthClient

        try:
            flow = await ZaiOAuthClient().init()
        except UpstreamError as exc:
            return JSONResponse(status_code=502,
                                content={"detail": exc.message or "取授权链接失败"})
        states[flow.flow_id] = {
            "realm": realm, "created_at": time.time(),
            "auth_url": flow.authorize_url,
            "poll_token": flow.poll_token,
            "server_poll_token": flow.server_poll_token,
        }
        record_audit(ctx.db, client_ip(request), "oauth_start", realm, "ok")
        return {"state": flow.flow_id, "auth_url": flow.authorize_url, "realm": realm,
                "protocol": "zai-cli"}

    client = OAuthClient(ctx.cfg)
    try:
        state = await client.request_state(realm)
    except UpstreamError as exc:
        return JSONResponse(status_code=502, content={"detail": exc.message or "取授权链接失败"})
    states[state.state] = {
        "realm": realm, "created_at": time.time(), "auth_url": state.auth_url,
    }
    record_audit(ctx.db, client_ip(request), "oauth_start", realm, "ok")
    return {"state": state.state, "auth_url": state.auth_url, "realm": realm,
            "protocol": "device"}


@router.post("/oauth/poll")
async def oauth_poll(request: Request, _: str = Depends(require_admin)):
    """轮询登录结果；成功后自动入库（并按配置补跑安装序 + 领取）。"""
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    state = str(payload.get("state", ""))
    record = _login_states(ctx).get(state)
    if record is None:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期，请重新获取授权链接")

    if record["realm"] == "zai":
        from ..auth.oauth import ZaiCliFlow, ZaiOAuthClient

        flow = ZaiCliFlow(
            flow_id=state,
            authorize_url=str(record.get("auth_url") or ""),
            poll_token=str(record.get("poll_token") or ""),
            server_poll_token=bool(record.get("server_poll_token")),
        )
        try:
            auth = await ZaiOAuthClient().complete(flow)
        except UpstreamError as exc:
            if exc.status == 401:
                # 会话过期 ≠ 等待中：清掉记录，让前端重新发起而不是无限轮询
                _login_states(ctx).pop(state, None)
                return JSONResponse(status_code=410,
                                    content={"expired": True, "detail": exc.message})
            # 未完成登录是正常轮询状态，返回 202 让前端继续等待
            return JSONResponse(status_code=202, content={"pending": True, "detail": exc.message})
    else:
        client = OAuthClient(ctx.cfg)
        try:
            auth = await client.complete(state, record["realm"])
        except UpstreamError as exc:
            return JSONResponse(status_code=202, content={"pending": True, "detail": exc.message})

    ctx.pool.upsert_account(auth)
    # 落盘凭证文件，便于与 login.sh 互通
    try:
        write_auth_file(default_auth_path(ctx.cfg.auth_path(), auth.uid), auth)
    except (OSError, ValueError):
        pass
    _login_states(ctx).pop(state, None)
    ctx.pool.save_state()
    post = await _post_add(ctx, auth, label="oauth_complete")
    record_audit(ctx.db, client_ip(request), "oauth_complete", auth.uid, "ok")
    return {"ok": True, "uid": auth.uid, "nickname": auth.nickname,
            "realm": auth.realm, **post}


@router.post("/accounts/apikey")
async def add_apikey_account(request: Request, _: str = Depends(require_admin)):
    """以 API Key 纳管账号（z.ai / 兼容 OpenAI 的第三方端点）。"""
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    api_key = str(payload.get("api_key", "")).strip()
    realm = str(payload.get("realm") or "zai")
    name = str(payload.get("name") or "")
    base_url = str(payload.get("base_url") or "")
    if not api_key:
        raise HTTPException(status_code=400, detail="api_key 不能为空")
    auth = api_key_account(realm, api_key, name, base_url)
    ctx.pool.upsert_account(auth)
    ctx.pool.save_state()
    post = await _post_add(ctx, auth, label="account_apikey_add")
    record_audit(ctx.db, client_ip(request), "account_apikey_add", auth.uid, "ok")
    return {"ok": True, "uid": auth.uid, "realm": realm, **post}


@router.post("/accounts/import")
async def import_accounts(request: Request, _: str = Depends(require_admin)):
    """导入账号凭证。

    接受的请求体（任一）：
      1. {"accounts": [<凭证对象>, ...]}       —— 导出文件直接回灌
      2. {"text": "<粘贴的多份 JSON>"}          —— 粘贴导出的文件内容
      3. {"text": "<每行一个 JSON>"}            —— JSONL
      4. [<凭证对象>, ...]                      —— 裸数组

    每份凭证支持嵌套形（auth/account）与扁平形；`kind` 决定 oauth / apikey。
    逐条返回失败原因，不再只给一个 skipped 计数。
    """
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    docs, parse_errors, bundle = _collect_import_docs(payload)

    imported: list[dict] = []
    failed: list[dict] = list(parse_errors)

    # ZCode 账号包（format=zcode-accounts-bundle）走专用解析
    if bundle is not None:
        from ..auth.zcode import parse_zcode_bundle

        candidates, bundle_errors = parse_zcode_bundle(bundle)
        failed.extend(bundle_errors)
        for cand in candidates:
            auth = cand.to_auth()
            try:
                ctx.pool.upsert_account(auth)
                write_auth_file(default_auth_path(ctx.cfg.auth_path(), auth.uid), auth)
            except (OSError, ValueError) as exc:
                failed.append({"index": cand.uid, "reason": f"落盘失败：{exc}"})
                continue
            imported.append({
                "uid": auth.uid, "realm": auth.realm, "nickname": auth.nickname,
                "kind": auth.kind, "base_url": auth.base_url, "protocol": auth.protocol,
            })

    for index, doc in enumerate(docs):
        auth, reason = parse_auth_file_verbose(json.dumps(doc))
        if auth is None:
            failed.append({"index": index, "reason": reason})
            continue
        if not auth.uid:
            # 无 uid 的凭证用 accessToken 摘要兜底，保证可入库且跨重启稳定
            auth.uid = "imported-" + hashlib.sha256(
                auth.access_token.encode("utf-8")
            ).hexdigest()[:16]
        try:
            ctx.pool.upsert_account(auth)
            write_auth_file(default_auth_path(ctx.cfg.auth_path(), auth.uid), auth)
        except (OSError, ValueError) as exc:
            failed.append({"index": index, "uid": auth.uid, "reason": f"落盘失败：{exc}"})
            continue
        imported.append({"uid": auth.uid, "realm": auth.realm,
                         "nickname": auth.nickname, "kind": auth.kind})

    ctx.pool.save_state()
    record_audit(ctx.db, client_ip(request), "account_import", "", "ok",
                 f"imported={len(imported)} failed={len(failed)}")
    return {
        "ok": True,
        "imported": len(imported),
        "skipped": len(failed),
        "accounts": imported,
        "failed": failed,
    }


def _collect_import_docs(payload: object) -> tuple[list[dict], list[dict], dict | None]:
    """把各种导入请求体归一化成 (凭证文档列表, 解析错误列表, zcode 账号包)。

    识别到 ZCode 账号包（format=zcode-accounts-bundle）时单独返回，由调用方
    走专用解析路径；其余按通用凭证文档处理。
    """
    docs: list[dict] = []
    errors: list[dict] = []
    bundle: dict | None = None

    def add(obj: object, label: str) -> None:
        """加入一份凭证；识别包裹层与 ZCode 账号包。"""
        nonlocal bundle
        if not isinstance(obj, dict):
            errors.append({"index": label, "reason": "条目必须是 JSON 对象"})
            return
        if is_zcode_bundle(obj):
            bundle = obj
            return
        nested = obj.get("accounts")
        if isinstance(nested, list) and "credentials" not in obj:
            for i, item in enumerate(nested):
                add(item, f"{label}.accounts[{i}]")
            return
        docs.append(obj)

    if isinstance(payload, list):
        for i, item in enumerate(payload):
            add(item, str(i))
        return docs, errors, bundle

    if not isinstance(payload, dict):
        return docs, [{"index": "-", "reason": "请求体必须是对象或数组"}], bundle

    if "accounts" in payload or is_zcode_bundle(payload):
        add(payload, "body")

    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        stripped = text.strip()
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if parsed is not None:
            add(parsed, "text")
        else:
            # 退化为 JSONL / 多份 JSON 拼接
            for lineno, line in enumerate(stripped.splitlines(), start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    errors.append({"index": f"line {lineno}",
                                   "reason": f"JSON 解析失败：{exc}"})
                    continue
                add(obj, f"line {lineno}")

    if not docs and not errors and bundle is None:
        errors.append({"index": "-", "reason": "未提供 accounts 数组或 text 内容"})
    return docs, errors, bundle


@router.get("/accounts/export")
async def export_accounts(
    request: Request,
    realm: str = "",
    include_disabled: int = 1,
    _: str = Depends(require_admin),
):
    """导出账号凭证（含 accessToken / refreshToken / device_token）。

    参数：
      realm            仅导出该域（留空 = 全部）
      include_disabled 是否包含已禁用账号（默认包含，便于完整备份）

    导出文件可直接用 /accounts/import 回灌。
    """
    ctx = get_ctx(request)
    out = []
    for entry in ctx.pool.all_entries():
        if realm and entry.realm != realm:
            continue
        if not include_disabled and not entry.enabled:
            continue
        auth = ctx.pool.auth_for(entry)
        if not auth.access_token:
            continue  # 无凭证的空壳不导出，避免产生无法回灌的条目
        doc = to_auth_doc(auth)
        doc["account"]["uid"] = auth.uid
        out.append(doc)
    record_audit(ctx.db, client_ip(request), "account_export", realm, "ok", f"count={len(out)}")
    return {
        "exported_at": db_now(),
        "version": 1,
        "count": len(out),
        "accounts": out,
    }


@router.get("/accounts/export/download")
async def export_accounts_download(
    request: Request, realm: str = "", _: str = Depends(require_admin)
):
    """导出为可下载的 JSON 文件。"""
    data = await export_accounts(request, realm=realm, include_disabled=1, _=_)
    filename = f"keyport-accounts-{time.strftime('%Y%m%d-%H%M%S')}.json"
    return JSONResponse(
        content=data,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/accounts/reload-files")
async def reload_auth_files(request: Request, _: str = Depends(require_admin)):
    """重新扫描 auths/ 目录，纳管 login.sh 落盘的凭证。"""
    ctx = get_ctx(request)
    auths, failed = load_auth_dir(ctx.cfg.auth_path())
    for auth in auths:
        ctx.pool.upsert_account(auth)
    ctx.pool.save_state()
    record_audit(ctx.db, client_ip(request), "account_reload_files", "", "ok",
                 f"loaded={len(auths)} failed={len(failed)}")
    return {"ok": True, "loaded": len(auths), "failed": failed}


# ---- 设备档案（指纹） ----


@router.get("/accounts/{uid}/fingerprint")
async def get_fingerprint(uid: str, request: Request, _: str = Depends(require_admin)):
    """查看账号设备档案（成套桌面 SKU + device_mid）。

    出站身份头按该档案出值，一号一台设备 —— 多账号共用宿主机真实形态会在
    上游形成「一台机房机器开了 N 个账号」的关联信号。
    """
    ctx = get_ctx(request)
    entry = ctx.pool.get(uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    return {"ok": True, "uid": uid, "fingerprint": ctx.pool.fingerprint_view(uid),
            "installed_at": entry.installed_at}


@router.post("/accounts/{uid}/rotate-fingerprint")
async def rotate_fingerprint(uid: str, request: Request, _: str = Depends(require_admin)):
    """换发全新设备档案（新 SKU + 新 device_mid），并清空安装序幂等位。

    风控命中后使用：账号重新启用时已是全新设备身份。
    **仅 ZCode 平台账号持有设备档案**；WorkBuddy / CodeBuddy 返回 400 说明原因
    （它们的身份是 `X-Machine-ID`，换 SKU 毫无作用），而不是假装成功。
    """
    ctx = get_ctx(request)
    profile = ctx.pool.rotate_fingerprint(uid)
    if profile is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not profile:
        raise HTTPException(
            status_code=400,
            detail="非 ZCode 平台账号，不持有设备档案（其身份为 X-Machine-ID，无需换发）",
        )
    record_audit(ctx.db, client_ip(request), "account_rotate_fingerprint", uid, "ok")
    return {"ok": True, "uid": uid, "fingerprint": profile}


@router.post("/accounts/{uid}/install")
async def run_install(uid: str, request: Request, _: str = Depends(require_admin)):
    """手动补跑官方首启安装序（client/configs + 激活事件上报）。

    **仅 ZCode 平台账号**：安装序模拟的是 ZCode 桌面端首启行为。非 ZCode 账号
    返回 `skipped=True`（零上游请求），前端据此提示而不是显示「成功」。
    """
    ctx = get_ctx(request)
    entry = ctx.pool.get(uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    from ..upstream.install import run_install_sequence_for_account

    auth = ctx.pool.auth_for(entry)
    result = await run_install_sequence_for_account(
        ctx.client, auth, installed_at=entry.installed_at,
        timeout=ctx.cfg.install.timeout_seconds,
    )
    if result.get("installed") and not entry.installed_at:
        ctx.pool.mark_installed(uid, int(time.time()))
    record_audit(ctx.db, client_ip(request), "account_install", uid,
                 "ok" if result.get("installed") else "fail",
                 "; ".join(result.get("errors") or [])[:200])
    return {"ok": True, "uid": uid, **result}


# ---- 限时套餐领取 ----


def _require_claims(ctx):
    if ctx.claims is None:
        raise HTTPException(status_code=503, detail="领取服务未启用")
    return ctx.claims


def _claim_error(exc) -> JSONResponse:
    """ClaimError → 响应；1005 附带名额恢复时间，供前端展示倒计时。"""
    content: dict[str, Any] = {"detail": str(exc), "code": getattr(exc, "code", -1)}
    if getattr(exc, "next_at", None):
        content["next_at"] = exc.next_at
    return JSONResponse(status_code=400, content=content)


@router.get("/claim/preview")
async def claim_preview(
    request: Request, uid: str = "", _: str = Depends(require_admin)
):
    """预览可领取套餐（免验证码）。

    `uid` 缺省时对全部可打 billing 的账号各查一次；否则只查指定账号。
    """
    ctx = get_ctx(request)
    claims = _require_claims(ctx)
    entries = [ctx.pool.get(uid)] if uid else list(ctx.pool.all_entries())
    out: list[dict[str, Any]] = []
    for entry in entries:
        if entry is None:
            continue
        auth = ctx.pool.auth_for(entry)
        try:
            plans = await claims.preview_plans(auth)
        except UpstreamError as exc:
            out.append({"uid": entry.uid, "nickname": entry.nickname, "ok": False,
                        "error": exc.message or exc.kind.value})
            continue
        except Exception as exc:  # noqa: BLE001 - 单账号失败不阻断整体预览
            out.append({"uid": entry.uid, "nickname": entry.nickname, "ok": False,
                        "error": str(exc)})
            continue
        out.append({
            "uid": entry.uid, "nickname": entry.nickname, "ok": True,
            "plans": plans,
            "holds": claims.holds_for(entry.uid),
        })
    record_audit(ctx.db, client_ip(request), "claim_preview", uid, "ok")
    return {"ok": True, "accounts": out, "status": claims.status()}


@router.post("/claim/run")
async def claim_run(request: Request, _: str = Depends(require_admin)):
    """手动触发一次领取。

    请求体：
      - `{"uid": "...", "plan_id": "..."}`        指定账号（+ 可选套餐）
      - `{"verify_param": "...", "region": "..."}` 浏览器滑块产生的验证码参数
        （仅指定 uid 时有效；不传则由本端农场求解）
      - `{}` / `{"uid": ""}`                      对全部可领取账号跑一轮
    """
    ctx = get_ctx(request)
    claims = _require_claims(ctx)
    try:
        payload = await request.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    uid = str(payload.get("uid") or "")
    plan_id = str(payload.get("plan_id") or "")
    verify_param = str(payload.get("verify_param") or "")
    region = str(payload.get("region") or "")

    if not uid:
        results = await claims._round(ctx.pool)
        record_audit(ctx.db, client_ip(request), "claim_run", "all", "ok")
        return {"ok": True, "scope": "all", "results": results}

    entry = ctx.pool.get(uid)
    if entry is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    auth = ctx.pool.auth_for(entry)
    try:
        if verify_param:
            outcome = await claims.claim_with_captcha(auth, verify_param, region, plan_id)
        else:
            outcome = await claims.claim(auth, plan_id)
    except ClaimError as exc:
        claims._record(entry, [{"ok": False, "outcome": "failed", "plan_id": plan_id,
                                "code": exc.code, "next_at": exc.next_at,
                                "message": str(exc)}])
        record_audit(ctx.db, client_ip(request), "claim_run", uid, "fail", str(exc)[:200])
        return _claim_error(exc)
    except UpstreamError as exc:
        record_audit(ctx.db, client_ip(request), "claim_run", uid, "fail",
                     (exc.message or exc.kind.value)[:200])
        return JSONResponse(status_code=502,
                            content={"detail": exc.message or "上游请求失败"})
    claims._record(entry, [{"ok": True, "outcome": "claimed", **outcome}])
    record_audit(ctx.db, client_ip(request), "claim_run", uid, "ok")
    return {"ok": True, "scope": uid, "result": outcome}


@router.get("/claim/history")
async def claim_history(
    request: Request, uid: str = "", limit: int = 100, _: str = Depends(require_admin)
):
    """领取历史（含失败原因与业务码）。"""
    ctx = get_ctx(request)
    claims = _require_claims(ctx)
    return {"ok": True, "history": claims.history(uid=uid, limit=limit)}
