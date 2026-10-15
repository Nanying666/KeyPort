"""国际版每日活跃打卡 —— 网页端云 agent 会话。

## 为什么需要

官方「每日活跃」的判据是**跑完的 agent 会话**。向桌面端点发一次
`chat/completions` 不算数，只建一条网页会话、不接沙箱同样不算数 ——
会话会停在 `CREATING`、零输出。所以

    只建会话 = 白建一条会话 + 拿不到那份积分

必须把会话真正驱动到 `completed`（协议细节见 `webagent`）。

## 四步

1. **建会话**：`POST {web_origin}/console/as/conversations/`
2. **取沙箱**：`GET .../{id}/session` → `link` + `token` + `sessionId` + `cwd`
3. **驱动回合**：`webagent.run_turn()`（initialize → session/load →
   **session/set_model** → session/prompt）
4. **轮询状态**：`GET .../{id}` 直到 `completed`

## 三条「不做就白跑」的硬性约束

1. **必须驱动回合**，不能只建会话（否则永远 `CREATING`）。
2. **必须显式钉模型**（`session/set_model`）：漏了会跑账号默认模型
   （网页端默认 Auto、按付费模型计费）—— 于是**每天的活跃都在扣积分**，
   比不跑更糟。
3. **打卡成功与否以网页腿为准**：桌面腿成功不等于当天打卡完成。
   网页腿失败时**不能**记「今日已完成」，否则当天后续巡检直接跳过该账号，
   那天的积分**静默丢掉且不再重试**。

## 分层

本模块是**业务层**，接收 `UpstreamClient`（与 `upstream/growth.py` 同层）——
需要 HTTP 时经 `upstream.client_for(auth)` 取客户端，账号级出口代理因此自动生效。
纯传输层（SSE + JSON-RPC）在 `webagent`，它只认 `httpx.AsyncClient`。

## 出站头

网页端是**另一套身份**：只有 `Bearer` + `X-User-Id`，加 Origin/Referer（`/app`）
与浏览器 UA —— **不带桌面端指纹头**。混用会两边都不像。
"""

from __future__ import annotations

import time
from typing import Any
from urllib.parse import quote

import httpx

from .base import ChatMeta, ErrorKind, UpstreamError

#: 网页控制台来源（国际版 / 国内版）
WEB_ORIGIN_GLOBAL = "https://www.workbuddy.ai"
WEB_ORIGIN_CN = "https://www.workbuddy.cn"

#: 会话集合端点（注意结尾的斜杠：缺了会被上游 301 掉）
CONVERSATIONS_PATH = "/console/as/conversations/"

#: 网页端 UA：与桌面端 CLI 指纹刻意不同
WEB_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36 Edg/140.0.0.0"
)

#: 默认打卡模型。选一个**免费**模型是刻意的 —— 打卡不该产生积分消耗。
DEFAULT_MODEL = "deepseek-v4.1-flash"

#: 默认提示词。实测一次短问候十几秒就跑完，不需要长文本。
DEFAULT_PROMPT = "Hi"

#: 一轮最多等多久（秒）
DEFAULT_TURN_TIMEOUT = 120.0

#: 轮询间隔（秒）。太快会在沙箱还没起来时白查若干次。
POLL_INTERVAL = 3.0

#: 建会话 / 查询的单请求超时
HTTP_TIMEOUT = 30.0

#: 会话状态
STATUS_COMPLETED = "completed"


def web_origin(realm: str) -> str:
    """按域取网页控制台来源。"""
    return WEB_ORIGIN_CN if str(realm or "").lower() == "cn" else WEB_ORIGIN_GLOBAL


def web_headers(auth: Any, origin: str) -> dict[str, str]:
    """网页端出站头（**不带桌面端指纹**）。"""
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": origin + "/app",
        "User-Agent": WEB_USER_AGENT,
    }
    if getattr(auth, "uid", ""):
        headers["X-User-Id"] = auth.uid
    return headers


async def _http(upstream: Any, auth: Any) -> httpx.AsyncClient:
    """取该账号的 HTTP 客户端（账号级出口代理在此生效）。"""
    return await upstream.client_for(auth)


def _decode(response: httpx.Response) -> dict[str, Any]:
    """解上游信封；非对象 / 非 JSON 一律当失败。"""
    try:
        payload = response.json()
    except ValueError as exc:
        raise UpstreamError(
            ErrorKind.UNKNOWN, response.status_code, "网页接口返回非 JSON"
        ) from exc
    if not isinstance(payload, dict):
        raise UpstreamError(
            ErrorKind.UNKNOWN, response.status_code, "网页接口返回非对象"
        )
    return payload


def _envelope_error(payload: dict[str, Any], what: str) -> str:
    """业务码非 0 时的可读文案（空串 = 成功）。"""
    code = payload.get("code")
    if code in (0, None):
        return ""
    return f"{what} 业务码={code} msg={payload.get('msg')}"


async def create_conversation(
    upstream: Any,
    auth: Any,
    *,
    prompt: str = DEFAULT_PROMPT,
    model: str = DEFAULT_MODEL,
) -> str:
    """建一条网页会话，返回会话 id；失败抛 `UpstreamError`。

    请求体带上网页端抓包所得的两项固定字段（`conversationOrigin` / `plugins`）——
    形态与真实网页端一致，不要裁剪成最小字段集。
    """
    origin = web_origin(auth.realm)
    body = {
        "prompt": prompt,
        "model": model,
        "conversationOrigin": "workbuddy-app",
        "plugins": [{"name": "weixinpay", "marketplace": "codebuddy-builtin"}],
    }
    http = await _http(upstream, auth)
    try:
        response = await http.post(
            origin + CONVERSATIONS_PATH, json=body,
            headers=web_headers(auth, origin), timeout=HTTP_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
    if response.status_code >= 400:
        raise UpstreamError(
            ErrorKind.UNKNOWN, response.status_code,
            f"建会话 HTTP {response.status_code}: {response.text[:120]}",
        )
    payload = _decode(response)
    problem = _envelope_error(payload, "建会话")
    if problem:
        raise UpstreamError(ErrorKind.UNKNOWN, response.status_code, problem)
    data = payload.get("data")
    conversation = data.get("id") if isinstance(data, dict) else None
    if not conversation:
        raise UpstreamError(
            ErrorKind.UNKNOWN, response.status_code, "建会话未返回会话 id"
        )
    return str(conversation)


async def conversation_get(
    upstream: Any, auth: Any, conversation: str, suffix: str = ""
) -> dict[str, Any]:
    """读一条网页会话（`suffix=""` 拿会话本身，`"/session"` 拿沙箱信息）。"""
    origin = web_origin(auth.realm)
    # 会话 id 走 URL 路径段：必须 quote（id 里可能含 `/`、`+` 等）
    url = f"{origin}{CONVERSATIONS_PATH}{quote(str(conversation), safe='')}{suffix}"
    http = await _http(upstream, auth)
    try:
        response = await http.get(
            url, headers=web_headers(auth, origin), timeout=HTTP_TIMEOUT
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
    if response.status_code >= 400:
        raise UpstreamError(
            ErrorKind.UNKNOWN, response.status_code,
            f"查询会话 HTTP {response.status_code}",
        )
    payload = _decode(response)
    data = payload.get("data")
    return data if isinstance(data, dict) else {}


async def conversation_status(upstream: Any, auth: Any, conversation: str) -> str:
    """这条会话现在什么状态（`completed` 就是这一轮真的跑完了）。"""
    try:
        data = await conversation_get(upstream, auth, conversation)
    except UpstreamError:
        return ""
    return str(data.get("status") or "")


async def run_web_leg(
    upstream: Any,
    auth: Any,
    *,
    prompt: str = DEFAULT_PROMPT,
    model: str = DEFAULT_MODEL,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    log: Any = None,
) -> dict[str, Any]:
    """完整跑一次网页腿（建会话 → 取沙箱 → 驱动回合 → 轮询完成）。

    返回 `{"ok", "conversation", "status", "chunks", "events", "elapsed_ms",
    "error"}`；失败时尽量带上会话 id，便于排查停在哪一步。
    """
    from . import webagent

    log = log or (lambda *a, **k: None)
    started = time.monotonic()
    conversation = ""
    try:
        conversation = await create_conversation(
            upstream, auth, prompt=prompt, model=model
        )
        session = await conversation_get(upstream, auth, conversation, "/session")
    except UpstreamError as exc:
        return {"ok": False, "conversation": conversation, "error": str(exc.message)}

    link = str(session.get("link") or session.get("endpoint") or "")
    token = str(session.get("token") or "")
    session_id = str(
        session.get("sessionId") or session.get("session_id") or conversation
    )
    cwd = str(session.get("cwd") or "/workspace")
    if not link or not token:
        # 沙箱没就绪 → 会话永远跑不起来。如实报错，别当成「跑完了」。
        return {
            "ok": False, "conversation": conversation,
            "error": "沙箱未就绪（没有 link/token）",
        }

    async def _poll() -> str:
        return await conversation_status(upstream, auth, conversation)

    http = await _http(upstream, auth)
    result = await webagent.run_turn(
        http,
        link=link,
        token=token,
        session_id=session_id,
        cwd=cwd,
        prompt=prompt,
        user_agent=WEB_USER_AGENT,
        poll_status=_poll,
        wait_seconds=turn_timeout,
        poll_interval=POLL_INTERVAL,
        model=model,
        log=log,
    )
    result["conversation"] = conversation
    result.setdefault("elapsed_ms", int((time.monotonic() - started) * 1000))
    if result.get("ok"):
        result["msg"] = (
            f"网页通道会话跑完：{result.get('chunks') or 0} 段输出，"
            f"{result.get('elapsed_ms') or 0} ms"
        )
    return result


async def daily_chat(
    upstream: Any,
    auth: Any,
    *,
    model: str = DEFAULT_MODEL,
    prompt: str = DEFAULT_PROMPT,
    turn_timeout: float = DEFAULT_TURN_TIMEOUT,
    desktop_leg: bool = True,
    log: Any = None,
) -> dict[str, Any]:
    """国际版每日活跃打卡（桌面腿 + 网页腿）。

    **`ok` 以网页腿为准**：网页腿失败时整次算失败 —— 桌面腿跑过也不算数。
    调用方据此决定是否记「今日已完成」：网页腿失败却记了，当天后续巡检会跳过
    该账号，那天的积分静默丢掉且不再重试。

    `desktop_leg` 只影响「额外发一次桌面端对话」这一动作：它**不计入**官方日活，
    属于保留的兼容行为；网页腿才是算数的那一条。
    """
    log = log or (lambda *a, **k: None)
    result: dict[str, Any] = {"ok": False, "desktop": None, "web": None}

    if str(getattr(auth, "realm", "")) != "global":
        result["error"] = "每日活跃打卡仅适用于国际版账号"
        return result

    if desktop_leg:
        try:
            result["desktop"] = await _desktop_leg(upstream, auth, model=model)
        except Exception as exc:  # noqa: BLE001 - 桌面腿失败不阻断网页腿
            result["desktop"] = {"ok": False, "error": str(exc)}

    web = await run_web_leg(
        upstream, auth, prompt=prompt, model=model, turn_timeout=turn_timeout, log=log
    )
    result["web"] = web
    if web.get("ok"):
        result["ok"] = True
        result["msg"] = f"每日活跃打卡完成（{web.get('msg')}）"
    else:
        # 桌面腿成功不改变结论：计入官方日活的只有网页腿。
        result["error"] = f"网页通道失败：{web.get('error')}"
        result["msg"] = f"每日活跃打卡未完成（{result['error']}）"
    return result


async def _desktop_leg(upstream: Any, auth: Any, *, model: str) -> dict[str, Any]:
    """桌面端最小对话（保留的兼容动作，**不计入**官方日活）。

    极短输出（`max_tokens` 很小、不思考）—— 它只是形态上的「用了一次」，
    不该产生可观的 token 消耗。
    """
    # 延迟导入：本模块被 `client` 惰性导入，模块级反向导入会成环。
    from .client import PATH_CHAT, _chat_base
    from .headers import chat_headers

    body = {
        "model": model,
        "messages": [{"role": "user", "content": "Hi"}],
        "stream": True,
        "max_tokens": 10,
        "reasoning_effort": "none",
    }
    headers = chat_headers(upstream.cfg.upstream, auth, ChatMeta())
    http = await _http(upstream, auth)
    try:
        response = await http.post(
            _chat_base(upstream.cfg, auth) + PATH_CHAT,
            json=body, headers=headers, timeout=HTTP_TIMEOUT,
        )
        # 流式响应只需读掉即可（不解析内容：打卡不关心说了什么）
        await response.aread()
    except httpx.HTTPError as exc:
        return {"ok": False, "error": str(exc)}
    if response.status_code >= 400:
        return {"ok": False, "error": f"HTTP {response.status_code}"}
    return {"ok": True, "msg": "桌面端对话完成"}
