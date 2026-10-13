"""管理台 API：日志、用量统计、上游账单、任务调度、设置、IP 管控、聊天测试台。"""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from ..config import config_to_dict, save_config
from ..pool.guards import GUARD_KEYS
from ..security import validate_cidr_list
from ..upstream.base import UpstreamError
from .admin_auth import client_ip, require_admin
from .deps import get_ctx
from .gateway import ChatParams, openai_collect
from .logging import record_audit

router = APIRouter(prefix="/api", tags=["ops"])


# ---- 调用日志 ----


@router.get("/logs")
async def list_logs(
    request: Request,
    limit: int = 50,
    key_id: int = 0,
    model: str = "",
    _: str = Depends(require_admin),
):
    ctx = get_ctx(request)
    logs = ctx.request_logger.recent(limit=limit, key_id=key_id, model=model)
    # 补账号昵称：日志只存 uid，展示层要可读名。按 uid 实时关联（昵称可改），
    # 账号已删除时回落 uid 前缀，保证历史日志仍可辨认。
    names = {e.uid: e.nickname for e in ctx.pool.all_entries()}
    for row in logs:
        uid = row.get("account_uid") or ""
        row["account_name"] = names.get(uid) or (uid[:8] if uid else "")
    return {"logs": logs}


@router.delete("/logs")
async def prune_logs(request: Request, keep_days: int = 30, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    removed = ctx.request_logger.prune(keep_days=keep_days)
    record_audit(ctx.db, client_ip(request), "logs_prune", "", "ok", f"removed={removed}")
    return {"ok": True, "removed": removed}


# ---- 用量统计 ----


@router.get("/usage")
async def usage_stats(request: Request, days: int = 7, _: str = Depends(require_admin)):
    """按天 / 按模型 / 按密钥多维统计 Token 消耗。"""
    ctx = get_ctx(request)
    since = int(time.time()) - max(1, min(days, 90)) * 86400

    by_day = ctx.db.query(
        "SELECT date(ts,'unixepoch','localtime') AS day, realm, COUNT(*) AS calls, "
        "COALESCE(SUM(prompt_tokens),0) AS prompt_tokens, "
        "COALESCE(SUM(completion_tokens),0) AS completion_tokens, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(credits),0) AS credits, "
        "COALESCE(AVG(NULLIF(ttfb_ms,0)),0) AS avg_ttfb "
        "FROM request_logs WHERE ts >= ? GROUP BY day, realm ORDER BY day, realm",
        (since,),
    )
    by_model = ctx.db.query(
        "SELECT model, realm, COUNT(*) AS calls, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(credits),0) AS credits, "
        "COALESCE(AVG(NULLIF(ttfb_ms,0)),0) AS avg_ttfb "
        "FROM request_logs WHERE ts >= ? GROUP BY model, realm ORDER BY total_tokens DESC",
        (since,),
    )
    by_key = ctx.db.query(
        "SELECT key_id, key_name, realm, COUNT(*) AS calls, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(credits),0) AS credits "
        "FROM request_logs WHERE ts >= ? "
        "GROUP BY key_id, key_name, realm ORDER BY total_tokens DESC",
        (since,),
    )
    return {
        "days": days,
        "by_day": [dict(r) for r in by_day],
        "by_model": [dict(r) for r in by_model],
        "by_key": [dict(r) for r in by_key],
    }


# ---- 上游真实账单 ----


@router.get("/billing")
async def upstream_billing(
    request: Request, days: int = 30, realm: str = "", _: str = Depends(require_admin)
):
    """直读官方用量接口，任意区间按天/按模型/分账号汇总。

    上游仅提供「当前余额/剩余积分」快照，不提供历史明细接口，
    因此这里给出的是各账号实时快照 + 本地记账的联合视图（不编造历史）。
    """
    ctx = get_ctx(request)
    accounts = []
    for entry in ctx.pool.all_entries():
        if realm and entry.realm != realm:
            continue
        accounts.append({
            "uid": entry.uid, "realm": entry.realm, "nickname": entry.nickname,
            "credits": entry.credits, "credits_used": entry.credits_used,
            "credits_expiring": entry.credits_expiring,
            "credits_updated_at": entry.credits_updated_at,
        })
    since = int(time.time()) - max(1, min(days, 365)) * 86400
    local = ctx.db.query(
        "SELECT account_uid, realm, COUNT(*) AS calls, "
        "COALESCE(SUM(total_tokens),0) AS total_tokens, "
        "COALESCE(SUM(credits),0) AS credits "
        "FROM request_logs WHERE ts >= ? GROUP BY account_uid, realm",
        (since,),
    )
    return {
        "days": days,
        "accounts": accounts,
        "local_usage": [dict(r) for r in local],
        "note": "上游仅提供实时余额快照；历史明细由本地记账提供",
    }


@router.post("/billing/refresh")
async def refresh_all_credits(request: Request, _: str = Depends(require_admin)):
    """批量刷新全部账号额度（直连上游）。"""
    ctx = get_ctx(request)
    results = []
    for entry in ctx.pool.all_entries():
        # 跳过没有额度端点的账号：普通 apikey 账号无法查额度，
        # 但 z.ai Coding Plan 账号（kind=apikey）额度在 zcode-plan 计费端点，需刷新。
        if entry.kind == "apikey" and entry.realm != "zai":
            continue
        try:
            auth = await ctx.refresher.ensure_valid(ctx.pool.auth_for(entry))
            data = await ctx.client.fetch_credits(auth)
            ctx.pool.update_credits(
                entry.uid, int(data.get("credits", entry.credits)),
                int(data.get("expiring", entry.credits_expiring)), data.get("used"),
            )
            results.append({"uid": entry.uid, "ok": True, "credits": data.get("credits")})
        except UpstreamError as exc:
            results.append({"uid": entry.uid, "ok": False,
                            "error": exc.message or exc.kind.value})
    ctx.pool.save_state()
    record_audit(ctx.db, client_ip(request), "billing_refresh", "", "ok")
    return {"ok": True, "results": results}


# ---- 任务调度 ----


@router.get("/tasks")
async def task_status(request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    runs = ctx.db.query("SELECT * FROM task_runs ORDER BY id DESC LIMIT 100")
    return {
        "scheduler": ctx.scheduler.status() if ctx.scheduler else None,
        "claims": ctx.claims.status() if ctx.claims else None,
        "runs": [dict(r) for r in runs],
    }


@router.post("/tasks/run")
async def run_task(request: Request, _: str = Depends(require_admin)):
    """手动触发一次任务（checkin / travel / activity / keepalive / cat）。"""
    ctx = get_ctx(request)
    if ctx.scheduler is None:
        raise HTTPException(status_code=503, detail="调度器未启动")
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    task = str(payload.get("task", ""))
    realm = str(payload.get("realm") or "")
    if task not in ("checkin", "travel", "activity", "keepalive", "cat"):
        raise HTTPException(status_code=400, detail=f"未知任务：{task}")
    result = await ctx.scheduler.run_now(task, realm=realm)
    record_audit(ctx.db, client_ip(request), "task_run", task, "ok")
    return {"ok": True, "task": task, "result": result}


@router.patch("/tasks/schedule")
async def update_schedule(request: Request, _: str = Depends(require_admin)):
    """在线修改排程（热生效并落盘）。"""
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")
    sched = ctx.cfg.schedule
    for field_name in ("checkin_hours", "travel_hours", "activity_hours",
                       "keepalive_hours", "cat_hours"):
        if field_name in payload and isinstance(payload[field_name], list):
            hours = sorted({int(h) for h in payload[field_name] if 0 <= int(h) <= 23})
            setattr(sched, field_name, hours)
    for flag in ("checkin_enabled", "travel_enabled", "activity_enabled",
                 "keepalive_enabled", "cat_enabled"):
        if flag in payload:
            setattr(sched, flag, bool(payload[flag]))
    save_config(ctx.cfg)
    record_audit(ctx.db, client_ip(request), "schedule_update", "", "ok")
    return {"ok": True, "schedule": config_to_dict(ctx.cfg)["schedule"]}


# ---- 设置 ----


def _public_config(cfg) -> dict:
    """可热改的配置视图（脱敏，不外泄任何密钥与摘要）。"""
    data = config_to_dict(cfg)
    data.get("security", {}).pop("admin_password_hash", None)
    data["upstream"].pop("device_token", None)
    return data


@router.get("/settings")
async def get_settings(request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    return {"config": _public_config(ctx.cfg)}


@router.patch("/settings")
async def update_settings(request: Request, _: str = Depends(require_admin)):
    """在线改配置（白名单字段，热生效并落盘）。"""
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    applied: list[str] = []

    prompt = payload.get("prompt")
    if isinstance(prompt, dict):
        if "mode" in prompt:
            mode = str(prompt["mode"])
            if mode not in ("custom", "passthrough", "append"):
                raise HTTPException(status_code=400, detail=f"非法提示词模式：{mode}")
            ctx.cfg.prompt.mode = mode
            applied.append("prompt.mode")
        for field_name in ("file", "default_text"):
            if field_name in prompt:
                setattr(ctx.cfg.prompt, field_name, str(prompt[field_name]))
                applied.append(f"prompt.{field_name}")

    features = payload.get("features")
    if isinstance(features, dict):
        for flag in ("sanitize_blacklist_fingerprints", "compress_long_context",
                     "reasoning_injection"):
            if flag in features:
                setattr(ctx.cfg.features, flag, bool(features[flag]))
                applied.append(f"features.{flag}")

    security = payload.get("security")
    if isinstance(security, dict):
        for list_field in ("global_allowlist", "global_blocklist"):
            if list_field in security:
                rules = security[list_field] or []
                bad = validate_cidr_list(rules)
                if bad:
                    raise HTTPException(status_code=400, detail=f"{list_field} 含非法 CIDR：{bad}")
                setattr(ctx.cfg.security, list_field, rules)
                applied.append(f"security.{list_field}")
        for flag in ("trust_proxy_headers", "audit_enabled"):
            if flag in security:
                setattr(ctx.cfg.security, flag, bool(security[flag]))
                applied.append(f"security.{flag}")

    pool = payload.get("pool")
    if isinstance(pool, dict):
        for int_field in ("max_in_flight", "max_in_flight_global", "breaker_threshold",
                          "top_k", "pick_gap_ms", "soft_rate_seconds", "not_found_seconds",
                          "retry_429_times", "retry_429_wait", "retry_429_wait_max",
                          "retry_5xx_times", "retry_5xx_wait", "cooling_seconds",
                          "risk_cooldown_seconds", "risk_cooldown_max_seconds",
                          "risk_ban_strikes", "risk_strike_decay_seconds"):
            if int_field in pool:
                setattr(ctx.cfg.pool, int_field, int(pool[int_field]))
                applied.append(f"pool.{int_field}")
        if "risk_auto_rotate" in pool:
            ctx.cfg.pool.risk_auto_rotate = bool(pool["risk_auto_rotate"])
            applied.append("pool.risk_auto_rotate")

    claim = payload.get("claim")
    if isinstance(claim, dict):
        for flag in ("enabled", "auto_on_add"):
            if flag in claim:
                setattr(ctx.cfg.claim, flag, bool(claim[flag]))
                applied.append(f"claim.{flag}")
        for int_field in ("round_interval_seconds", "cooldown_seconds",
                          "max_accounts_per_round"):
            if int_field in claim:
                setattr(ctx.cfg.claim, int_field, int(claim[int_field]))
                applied.append(f"claim.{int_field}")

    install = payload.get("install")
    if isinstance(install, dict):
        for flag in ("enabled", "auto_on_add"):
            if flag in install:
                setattr(ctx.cfg.install, flag, bool(install[flag]))
                applied.append(f"install.{flag}")

    guards = payload.get("guards")
    if isinstance(guards, dict):
        for int_field in GUARD_KEYS:
            if int_field not in guards:
                continue
            try:
                value = int(guards[int_field] or 0)
            except (TypeError, ValueError):
                raise HTTPException(
                    status_code=400, detail=f"guards.{int_field} 必须是整数") from None
            if value < 0:
                raise HTTPException(status_code=400, detail=f"guards.{int_field} 不能为负数")
            setattr(ctx.cfg.guards, int_field, value)
            applied.append(f"guards.{int_field}")
        overrides = guards.get("realm_overrides")
        if isinstance(overrides, dict):
            cleaned: dict[str, dict[str, int]] = {}
            for realm, values in overrides.items():
                if not isinstance(values, dict):
                    continue
                entry: dict[str, int] = {}
                for key, raw_value in values.items():
                    if key not in GUARD_KEYS or raw_value is None:
                        continue  # 未知键忽略；null = 继承全局
                    try:
                        entry[key] = max(0, int(raw_value))
                    except (TypeError, ValueError):
                        raise HTTPException(
                            status_code=400,
                            detail=f"guards.realm_overrides.{realm}.{key} 非法",
                        ) from None
                cleaned[str(realm)] = entry
            ctx.cfg.guards.realm_overrides = cleaned
            applied.append("guards.realm_overrides")
        # 阈值一改立刻重算计数，管理台不必等下一个请求才看到效果
        ctx.pool.refresh_guards(force=True)

    models = payload.get("models")
    if isinstance(models, dict) and "aliases" in models:
        raw_aliases = models["aliases"]
        if not isinstance(raw_aliases, dict):
            raise HTTPException(status_code=400, detail="models.aliases 必须是对象")
        ctx.cfg.models.aliases = {
            str(k).strip(): str(v).strip()
            for k, v in raw_aliases.items()
            if str(k).strip() and str(v).strip()
        }
        applied.append("models.aliases")

    upstream = payload.get("upstream")
    if isinstance(upstream, dict):
        for str_field in ("user_agent", "client_version", "cli_version", "client_name"):
            if str_field in upstream:
                setattr(ctx.cfg.upstream, str_field, str(upstream[str_field]))
                applied.append(f"upstream.{str_field}")
        if "passthrough_ip" in upstream:
            ctx.cfg.upstream.passthrough_ip = bool(upstream["passthrough_ip"])
            applied.append("upstream.passthrough_ip")

    realms = payload.get("realms")
    if isinstance(realms, dict):
        for name, values in realms.items():
            rc = ctx.cfg.realm(name)
            if rc is None or not isinstance(values, dict):
                continue
            for field_name in ("enabled", "chat_base", "billing_base", "models_base"):
                if field_name in values:
                    setattr(rc, field_name, values[field_name])
                    applied.append(f"realms.{name}.{field_name}")

    save_config(ctx.cfg)
    record_audit(ctx.db, client_ip(request), "settings_update", "", "ok", ",".join(applied))
    return {"ok": True, "applied": applied, "config": _public_config(ctx.cfg)}


# ---- 审计与 IP 管控 ----


@router.get("/audit")
async def list_audit(request: Request, limit: int = 100, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    rows = ctx.db.query(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 500)),)
    )
    return {"audit": [dict(r) for r in rows]}


@router.get("/ip-rules")
async def get_ip_rules(request: Request, _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    return {
        "allowlist": ctx.cfg.security.global_allowlist,
        "blocklist": ctx.cfg.security.global_blocklist,
        "trust_proxy_headers": ctx.cfg.security.trust_proxy_headers,
    }


# ---- 账号级限额护栏 ----


@router.get("/guards")
async def get_guards(request: Request, _: str = Depends(require_admin)):
    """护栏状态：阈值（含分域覆盖）+ 各账号当日用量与是否被挡。

    只读本地记账（`request_logs`），不产生上游调用。
    """
    ctx = get_ctx(request)
    ctx.pool.refresh_guards(force=True)
    guards = ctx.cfg.guards
    return {
        "limits": {k: getattr(guards, k) for k in GUARD_KEYS},
        "realm_overrides": guards.realm_overrides,
        "pool": ctx.pool.guards_snapshot(),
    }


# ---- 聊天测试台（不建密钥直接试调，仅管理员可用，真实扣积分） ----


@router.get("/playground/models")
async def playground_models(request: Request, realm: str = "", _: str = Depends(require_admin)):
    ctx = get_ctx(request)
    models = await ctx.models.list_models(realm, refresh=False)
    return {"models": models, "realms": ctx.cfg.enabled_realms()}


@router.post("/playground/chat")
async def playground_chat(request: Request, _: str = Depends(require_admin)):
    """经管理端登录态转发到上游（与下游同一账号池），试调会真实扣积分。"""
    ctx = get_ctx(request)
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="请求体不是合法 JSON")

    messages = payload.get("messages") or []
    if not isinstance(messages, list) or not messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")
    model = str(payload.get("model", ""))
    if not model:
        raise HTTPException(status_code=400, detail="model 不能为空")

    body: dict = {"model": model, "messages": messages}
    if payload.get("reasoning_effort"):
        body["reasoning_effort"] = payload["reasoning_effort"]
    if payload.get("max_tokens"):
        body["max_tokens"] = int(payload["max_tokens"])
    if payload.get("temperature") is not None:
        body["temperature"] = payload["temperature"]

    params = ChatParams(
        body=body, model=model, realm=str(payload.get("realm") or ""),
        protocol="chat", client_ip=client_ip(request), stream=False,
        key_id=0, key_name="playground",
    )
    session = ctx.gateway.session(params)
    try:
        await session.start()
        result, usage = await openai_collect(session)
    except UpstreamError as exc:
        return JSONResponse(status_code=exc.status or 502,
                            content={"detail": exc.message or "上游请求失败"})

    from .gateway import extract_credits

    choice = result["choices"][0]
    return {
        "content": choice["message"].get("content") or "",
        "reasoning": choice["message"].get("reasoning_content") or "",
        "tool_calls": choice["message"].get("tool_calls") or [],
        "finish_reason": choice.get("finish_reason"),
        "usage": usage,
        "credits": extract_credits(usage),
        "model": session.params.model,
        "realm": session.params.realm,
        "account_uid": session.uid,
    }
