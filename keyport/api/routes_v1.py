"""`/v1` 对外网关路由：chat/completions、responses、messages、models。

鉴权：Authorization: Bearer <API Key>（或 x-api-key，兼容 Anthropic 客户端）。
"""

from __future__ import annotations

import json
import time
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..protocol import anthropic
from ..protocol import responses as responses_proto
from ..upstream.base import ErrorKind, UpstreamError
from ..upstream.sse import ensure_usage_total
from .auth_middleware import global_ip_allowed
from .deps import get_ctx
from .gateway import ChatParams, openai_collect, openai_stream, resolve_model

router = APIRouter(prefix="/v1", tags=["gateway"])


# ---- 鉴权与准入 ----


class AuthFailure(Exception):
    def __init__(self, status: int, message: str, err_type: str = "invalid_request_error",
                 headers: dict[str, str] | None = None):
        self.status = status
        self.message = message
        self.err_type = err_type
        # 附加响应头（如 429 的 `Retry-After`）—— 下游据此退避，别让客户端硬撞
        self.headers = headers or {}


def _extract_key(authorization: str, x_api_key: str) -> str:
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    if x_api_key:
        return x_api_key.strip()
    return ""


def _client_ip(request: Request, cfg) -> str:
    from ..security import client_ip_from_request

    host = request.client.host if request.client else ""
    return client_ip_from_request(
        host, dict(request.headers),
        trust_proxy=cfg.security.trust_proxy_headers,
        trusted_proxies=cfg.security.trusted_proxies,
    )


def _authorize(request: Request, authorization: str, x_api_key: str):
    ctx = get_ctx(request)
    cfg = ctx.cfg
    ip = _client_ip(request, cfg)

    # 全局 IP 管控
    allowed, reason = global_ip_allowed(
        cfg.security.global_allowlist, cfg.security.global_blocklist, ip
    )
    if not allowed:
        raise AuthFailure(403, f"IP 被拒绝（{reason}）", "permission_error")

    raw_key = _extract_key(authorization, x_api_key)
    if not raw_key:
        raise AuthFailure(401, "缺少 API Key", "authentication_error")
    ctx_key = ctx.key_store.authenticate(raw_key)
    if ctx_key is None:
        raise AuthFailure(401, "API Key 无效或已停用", "authentication_error")

    # 密钥级 IP 管控
    ok, kreason = ctx.key_store.check_ip(ctx_key, ip)
    if not ok:
        raise AuthFailure(403, f"IP 被密钥策略拒绝（{kreason}）", "permission_error")

    if ctx_key.credits_exhausted():
        raise AuthFailure(429, "该密钥积分配额已用尽", "rate_limit_error")
    if ctx_key.tokens_exhausted():
        raise AuthFailure(429, "该密钥 Token 配额已用尽", "rate_limit_error")

    # 客户端维度限流：按 Key ID 计数（换 Key 即换桶，符合「客户端」语义）。
    # 放在鉴权之后 —— 那时才拿得到 Key ID；无 Key 实体时退化为按客户端 IP。
    rpm = int(getattr(cfg.pool, "rate_limit_rpm", 0) or 0)
    if rpm > 0 and ctx.limiter is not None:
        from ..ratelimit import bucket_for, retry_after_seconds

        ok_rl, wait = ctx.limiter.allow(bucket_for(ctx_key.key_id, ip), rpm)
        if not ok_rl:
            secs = retry_after_seconds(wait)
            raise AuthFailure(
                429,
                f"请求过于频繁：每分钟上限 {rpm} 次，请 {secs}s 后重试",
                "rate_limit_error",
                headers={"Retry-After": str(secs)},
            )

    # 当日积分预算闸：**全局**准入控制，达到当日累计扣费上限即停收。
    # 放在这里（读 body 之前）—— 被拒的请求不该先把几十 MB 请求体读进内存再丢掉。
    if ctx.budget is not None and not ctx.budget.admit():
        snap = ctx.budget.snapshot()
        raise AuthFailure(
            429,
            f"网关当日积分预算已用尽（{snap['used']:.2f}/{snap['limit']:.2f}），"
            f"按 CST 自然日重置",
            "rate_limit_error",
        )

    return ctx, ctx_key, ip


def _check_model(ctx_key, model: str) -> None:
    if not ctx_key.model_allowed(model):
        raise AuthFailure(400, f"密钥未授权模型：{model}", "invalid_request_error")


def _error_body(message: str, err_type: str = "invalid_request_error") -> dict:
    return {"error": {"message": message, "type": err_type, "code": None, "param": None}}


# ---- 对话 ID 提取（会话粘性键） ----


def _conversation_id(body: dict[str, Any], headers: dict[str, str]) -> str:
    """提取会话键（含内容派生回退，见 `upstream.cache_key`）。

    刻意**不再**把顶层 `user` 当会话键：user 维度粒度过粗，一个用户的全部并行
    对话会被钉到同一账号，粘性范围远超上游 prompt cache 的对话级边界。
    """
    from ..upstream.cache_key import extract_session_key

    return extract_session_key(body, headers)


def _fail_status(exc: UpstreamError) -> int:
    """把上游错误映射为下游 HTTP 状态码（OpenAI / Anthropic 两条路径共用）。"""
    status = exc.status or 502
    if exc.kind == ErrorKind.RATE_LIMIT:
        # 429 原样透出：这是给客户端「退避重试」的有效信号
        return 429
    if exc.kind == ErrorKind.AUTH:
        return 401
    if exc.kind == ErrorKind.CAPTCHA:
        # 上游网关要求人机验证，客户端重试无意义，用 502 表明是上游侧限制
        return 502
    if exc.kind == ErrorKind.RISK:
        # 风控由上游 HTTP 405 承载，但 405 在下游语义是「方法不允许」，
        # 会误导客户端去改请求方法。风控本质是服务端侧限制 → 502。
        return 502
    if exc.kind in (ErrorKind.NETWORK, ErrorKind.SERVER, ErrorKind.UNKNOWN):
        return 503 if status < 500 else status
    return status


def _fail(exc: UpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=_fail_status(exc),
        content=_error_body(_error_message(exc), f"upstream_{exc.kind.value}"),
    )


def _anthropic_fail(exc: UpstreamError) -> JSONResponse:
    """Anthropic 风格错误体（/v1/messages 用）。"""
    return JSONResponse(
        status_code=_fail_status(exc),
        content={"type": "error", "error": {
            "type": f"upstream_{exc.kind.value}",
            "message": _error_message(exc),
        }},
    )


def _error_message(exc: UpstreamError) -> str:
    """把上游错误转成可诊断的下游提示。

    CAPTCHA 需要解释：上游端点的验证码是 KeyPort 无法代答的，
    直接透传 "captcha verify failed" 会让调用方误以为是自己请求有误。
    """
    if exc.kind == ErrorKind.CAPTCHA:
        return ("上游端点要求人机验证（captcha verify failed），"
                "KeyPort 无法代为通过。该账号域需改用无需验证码的端点或密钥。")
    return exc.message or "上游请求失败"


# ---- /v1/models ----


@router.get("/models")
async def list_models(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="x-api-key"),
    refresh: int = 0,
):
    try:
        ctx, ctx_key, _ = _authorize(request, authorization, x_api_key)
    except AuthFailure as exc:
        return JSONResponse(status_code=exc.status,
                            content=_error_body(exc.message, exc.err_type),
                            headers=exc.headers)
    realm = ctx_key.realm or ""
    models = await ctx.models.list_models(realm, refresh=bool(refresh))
    data = []
    for model in models:
        if not ctx_key.model_allowed(model["id"]):
            continue
        entry = {
            "id": model["id"],
            "object": "model",
            "created": 0,
            "owned_by": model.get("realm", "keyport"),
        }
        # 直连上游的真实字段原样透出，缺失即省略（不编造）
        for src, dst in (
            ("display_name", "display_name"),
            ("context_window", "context_window"),
            ("max_output_tokens", "max_output_tokens"),
            ("reasoning_efforts", "reasoning_supported_efforts"),
            ("reasoning_default_effort", "reasoning_default_effort"),
            ("series", "series"),
            ("supports_images", "supports_images"),
            ("credit_factor", "credit_factor"),
        ):
            if src in model:
                entry[dst] = model[src]
        data.append(entry)
    return {"object": "list", "data": data}


# ---- /v1/usage（密钥自查，不产生上游调用） ----


@router.get("/usage")
async def key_usage(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="x-api-key"),
):
    """密钥自查：已用 / 剩余配额、有效期与模型范围。

    **只读本地记账，不产生任何上游调用** —— 下游可以用它做预算告警，
    而不必担心查询本身消耗额度。鉴权口径与 `/v1/models` 一致。
    """
    try:
        ctx, ctx_key, _ = _authorize(request, authorization, x_api_key)
    except AuthFailure as exc:
        return JSONResponse(status_code=exc.status,
                            content=_error_body(exc.message, exc.err_type),
                            headers=exc.headers)

    def _remaining(quota: float, used: float) -> float | None:
        """剩余额度；配额为 0（不限）时返回 None（不编造数字）。"""
        return max(0.0, quota - used) if quota > 0 else None

    return {
        "object": "key.usage",
        "key": {
            "id": ctx_key.key_id,
            "name": ctx_key.key_name,
            "realm": ctx_key.realm or "",
        },
        "credits": {
            "quota": ctx_key.quota_credits,
            "used": ctx_key.used_credits,
            "remaining": _remaining(ctx_key.quota_credits, ctx_key.used_credits),
            "exhausted": ctx_key.credits_exhausted(),
        },
        "tokens": {
            "quota": ctx_key.quota_tokens,
            "used": ctx_key.used_tokens,
            "remaining": _remaining(ctx_key.quota_tokens, ctx_key.used_tokens),
            "exhausted": ctx_key.tokens_exhausted(),
        },
        "models": {
            "allow": list(ctx_key.model_allow),
            "restricted": bool(ctx_key.model_allow),
        },
        "note": "只读本地记账；quota=0 表示不限（remaining 为 null）",
    }


# ---- /v1/chat/completions ----


@router.post("/chat/completions")
async def chat_completions(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="x-api-key"),
):
    try:
        ctx, ctx_key, ip = _authorize(request, authorization, x_api_key)
    except AuthFailure as exc:
        return JSONResponse(status_code=exc.status,
                            content=_error_body(exc.message, exc.err_type),
                            headers=exc.headers)

    try:
        body = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content=_error_body("请求体不是合法 JSON"))

    model = str(body.get("model", ""))
    _realm, bare = resolve_model(model, ctx_key.realm)
    try:
        _check_model(ctx_key, bare)
    except AuthFailure as exc:
        return JSONResponse(status_code=exc.status,
                            content=_error_body(exc.message, exc.err_type),
                            headers=exc.headers)

    stream = bool(body.get("stream", False))
    params = ChatParams(
        body=body, model=model, realm=ctx_key.realm, protocol="chat",
        client_ip=ip, conversation_id=_conversation_id(body, dict(request.headers)),
        stream=stream, key_id=ctx_key.key_id, key_name=ctx_key.key_name,
    )
    session = ctx.gateway.session(params)
    try:
        await session.start()
    except UpstreamError as exc:
        return _fail(exc)

    if stream:
        async def _gen():
            async for chunk in openai_stream(session):
                yield chunk

        return StreamingResponse(
            _gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        payload, usage = await openai_collect(session)
    except UpstreamError as exc:
        return _fail(exc)
    payload["usage"] = ensure_usage_total(usage)
    return JSONResponse(content=payload)


# ---- /v1/responses（Codex CLI） ----


@router.post("/responses")
async def responses_api(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="x-api-key"),
):
    try:
        ctx, ctx_key, ip = _authorize(request, authorization, x_api_key)
    except AuthFailure as exc:
        return JSONResponse(status_code=exc.status,
                            content=_error_body(exc.message, exc.err_type),
                            headers=exc.headers)

    try:
        raw = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(status_code=400, content=_error_body("请求体不是合法 JSON"))

    model = str(raw.get("model", ""))
    _realm, bare = resolve_model(model, ctx_key.realm)
    try:
        _check_model(ctx_key, bare)
    except AuthFailure as exc:
        return JSONResponse(status_code=exc.status,
                            content=_error_body(exc.message, exc.err_type),
                            headers=exc.headers)

    openai_body = responses_proto.request_to_openai(raw)
    stream = bool(raw.get("stream", False))
    params = ChatParams(
        body=openai_body, model=model, realm=ctx_key.realm, protocol="responses",
        client_ip=ip, conversation_id=_conversation_id(raw, dict(request.headers)),
        stream=stream, key_id=ctx_key.key_id, key_name=ctx_key.key_name,
    )
    session = ctx.gateway.session(params)
    try:
        await session.start()
    except UpstreamError as exc:
        return _fail(exc)

    response_id = f"resp_{int(time.time() * 1000)}"

    if stream:
        async def _gen():
            try:
                async for event in responses_proto.stream_events(
                    session.iter_lines(), session.params.model, response_id,
                    repair=session.markup_repair,
                ):
                    yield event
            finally:
                session.finish(None)
                await session.close()

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        payload, usage = await openai_collect(session)
    except UpstreamError as exc:
        return _fail(exc)
    choice = payload["choices"][0]
    message = choice["message"]
    result = responses_proto.response_from_openai(
        content_text=message.get("content") or "",
        tool_calls=message.get("tool_calls") or [],
        finish_reason=choice.get("finish_reason", "stop"),
        usage=ensure_usage_total(usage),
        model=session.params.model,
        response_id=response_id,
    )
    return JSONResponse(content=result)


# ---- /v1/messages（Anthropic Messages / Claude Code） ----


@router.post("/messages")
async def messages_api(
    request: Request,
    authorization: str = Header(default=""),
    x_api_key: str = Header(default="", alias="x-api-key"),
):
    try:
        ctx, ctx_key, ip = _authorize(request, authorization, x_api_key)
    except AuthFailure as exc:
        return JSONResponse(
            status_code=exc.status,
            content={"type": "error", "error": {"type": exc.err_type, "message": exc.message}},
            headers=exc.headers,
        )

    try:
        raw = await request.json()
    except (json.JSONDecodeError, ValueError):
        return JSONResponse(
            status_code=400,
            content={"type": "error", "error": {"type": "invalid_request_error",
                                                "message": "请求体不是合法 JSON"}},
        )

    model = str(raw.get("model", ""))
    _realm, bare = resolve_model(model, ctx_key.realm)
    try:
        _check_model(ctx_key, bare)
    except AuthFailure as exc:
        return JSONResponse(
            status_code=exc.status,
            content={"type": "error", "error": {"type": exc.err_type, "message": exc.message}},
            headers=exc.headers,
        )

    openai_body = anthropic.request_to_openai(raw)
    stream = bool(raw.get("stream", False))
    params = ChatParams(
        body=openai_body, model=model, realm=ctx_key.realm, protocol="messages",
        client_ip=ip, conversation_id=_conversation_id(raw, dict(request.headers)),
        stream=stream, key_id=ctx_key.key_id, key_name=ctx_key.key_name,
    )
    session = ctx.gateway.session(params)
    try:
        await session.start()
    except UpstreamError as exc:
        return _anthropic_fail(exc)

    msg_id = f"msg_{int(time.time() * 1000)}"

    if stream:
        async def _gen():
            try:
                async for event in anthropic.stream_events(
                    session.iter_lines(), session.params.model, msg_id,
                    repair=session.markup_repair,
                ):
                    yield event
            finally:
                session.finish(None)
                await session.close()

        return StreamingResponse(
            _gen(), media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    try:
        payload, usage = await openai_collect(session)
    except UpstreamError as exc:
        return _anthropic_fail(exc)
    choice = payload["choices"][0]
    message = choice["message"]
    result = anthropic.response_from_openai(
        content_text=message.get("content") or "",
        tool_calls=message.get("tool_calls") or [],
        finish_reason=choice.get("finish_reason", "stop"),
        usage=ensure_usage_total(usage),
        model=session.params.model,
        msg_id=msg_id,
    )
    return JSONResponse(content=result)
