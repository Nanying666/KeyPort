"""上游 HTTP 客户端：chat 流式、token 刷新、模型目录、额度与任务。

统一把 HTTP 失败翻译为 UpstreamError(kind=...)，由账号池决定处置方式。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator

import httpx

from ..config import Config
from . import constants as C
from . import headers as H
from .base import AccountAuth, ChatMeta, ErrorKind, UpstreamError, classify_status
from .fingerprint import profile_for
from .zcode_system import attach_context_prefix, build_zcode_plan_system

# 端点路径（相对 chat_base / billing_base）
PATH_REFRESH = "/v2/plugin/auth/token/refresh"
PATH_CHAT = "/v2/chat/completions"
PATH_MODELS_CN = "/console/enterprises/personal/models"
PATH_MODELS_GLOBAL = "/v2/enterprises/personal/models"
PATH_V3_CONFIG = "/v3/config"
PATH_USER_RESOURCE = "/v2/billing/meter/get-user-resource"
PATH_USER_RESOURCE_ALT = "/billing/meter/get-user-resource"
PATH_CHECKIN = "/v2/billing/meter/daily-checkin"
PATH_CHECKIN_ALT = "/billing/meter/daily-checkin"

# z.ai 端点
ZAI_CHAT = "/chat/completions"
PATH_ANTHROPIC_MESSAGES = "/v1/messages"

# Coding Plan 标记与套餐权益端点统一收口在 constants.py
ZCODE_PLAN_MARKER = C.ZCODE_PLAN_MARKER
PATH_ZCODE_PLAN_BALANCE = C.PATH_BILLING_BALANCE


def is_zcode_plan(auth: AccountAuth) -> bool:
    """判定账号是否走 zcode.z.ai 的 Coding Plan 网关（该网关要验证码）。"""
    return ZCODE_PLAN_MARKER in (auth.base_url or "")


def _clamp_max_tokens(payload: dict[str, Any]) -> None:
    """把 max_tokens 钳制到上游合法区间 [1, MAX_TOKENS_LIMIT]（就地修改）。

    上游对超限值直接报 400 code 1210「max_tokens参数非法：限制数值范围」。
    客户端（如 auto-compact 续传）可能带更大的值，网关统一钳制而非把请求打回。
    非法值（非数值 / bool）原样保留，由上游给出更准确的错误。
    """
    raw = payload.get("max_tokens")
    if raw is None or isinstance(raw, bool):
        return
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return
    payload["max_tokens"] = max(1, min(value, C.MAX_TOKENS_LIMIT))


def _chat_base(cfg: Config, auth: AccountAuth) -> str:
    # 账号自带 base_url 优先（z.ai Coding Plan 等第三方 Anthropic 端点）
    if auth.base_url:
        return auth.base_url.rstrip("/")
    rc = cfg.realm(auth.realm)
    if rc and rc.chat_base:
        return rc.chat_base.rstrip("/")
    return "https://copilot.tencent.com"


def _billing_base(cfg: Config, auth: AccountAuth) -> str:
    rc = cfg.realm(auth.realm)
    if rc and rc.billing_base:
        return rc.billing_base.rstrip("/")
    return _chat_base(cfg, auth)


def _zcode_plan_billing_url(auth: AccountAuth) -> str:
    """从账号 base_url 推导 Coding Plan 计费端点（套餐权益来源）。

    账号 base_url 形如 `https://zcode.z.ai/api/v1/zcode-plan/anthropic`，
    计费端点在 `/zcode-plan` 前缀下的 `/billing/balance`。非 Coding Plan
    端点（不含该标记）返回空串，调用方据此跳过。
    """
    base = (auth.base_url or "").rstrip("/")
    marker = base.find(ZCODE_PLAN_MARKER)
    if marker < 0:
        return ""
    return base[:marker + len(ZCODE_PLAN_MARKER)] + PATH_ZCODE_PLAN_BALANCE


def _models_from_entitlements(data: Any) -> list[dict[str, Any]]:
    """从 Coding Plan 套餐权益里派生**账号真实可用**的模型列表。

    数据源 `billing/balance` 的 `data.plans[].entitlements[].capabilities`，
    形如 `["model:glm-5.3-flash"]`。只取 `status == "active"` 的套餐，
    按模型聚合授权额度（同模型可能有每日 + 一次性多档权益）。
    上游没有给 display_name / context_window，就不编造（与 _normalize_models 同口径）。
    """
    if not isinstance(data, dict):
        return []
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    plans = payload.get("plans")
    if not isinstance(plans, list):
        return []

    merged: dict[str, dict[str, Any]] = {}
    for plan in plans:
        if not isinstance(plan, dict) or plan.get("status") != "active":
            continue
        entitlements = plan.get("entitlements")
        if not isinstance(entitlements, list):
            continue
        for ent in entitlements:
            if not isinstance(ent, dict):
                continue
            caps = ent.get("capabilities")
            if not isinstance(caps, list):
                continue
            show_name = ent.get("show_name")
            for cap in caps:
                if not isinstance(cap, str) or not cap.startswith("model:"):
                    continue
                model_id = cap[len("model:"):].strip()
                if not model_id:
                    continue
                entry = merged.setdefault(model_id, {"id": model_id})
                if show_name and "display_name" not in entry:
                    entry["display_name"] = str(show_name)
    return list(merged.values())


class UpstreamClient:
    """上游调用封装（复用单个 AsyncClient 连接池）。"""

    def __init__(self, cfg: Config, captcha: Any = None):
        self.cfg = cfg
        self.captcha = captcha
        self._client: httpx.AsyncClient | None = None

    async def client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(
                    self.cfg.upstream.timeout_seconds,
                    connect=15.0,
                    read=self.cfg.upstream.idle_timeout_seconds,
                ),
                follow_redirects=False,
                limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
            )
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def post_url(self, url: str, **kwargs: Any) -> httpx.Response:
        """对绝对 URL 发 POST（激活遥测等不在账号 base 下的端点）。

        复用同一连接池与测试注入点；网络故障统一转 UpstreamError(NETWORK)。
        """
        client = await self.client()
        try:
            return await client.post(url, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc

    async def get_url(self, url: str, **kwargs: Any) -> httpx.Response:
        """对绝对 URL 发 GET（同上，供 client/configs 等免鉴权端点使用）。"""
        client = await self.client()
        try:
            return await client.get(url, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc

    async def _shape_zcode_plan_body(
        self, auth: AccountAuth, body: bytes
    ) -> tuple[bytes, str | None]:
        """把请求体补成官方形态，并返回本次要用的验证码参数。

        三件事缺一不可（实测）：
          1. system 注入 ZCode 身份块 + 首轮 user 挂 currentDate → 否则 3012
          2. max_tokens 钳制到上游合法区间 → 否则 400 code 1210
          3. x-aliyun-captcha-verify-param 每次用新参数 → 否则 3007
        """
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            return body, None

        model = payload.get("model")
        payload["model"] = model.lower() if isinstance(model, str) else model
        _clamp_max_tokens(payload)
        payload["system"] = build_zcode_plan_system(
            existing_system=payload.get("system"), current_model=payload.get("model"))
        payload["messages"] = attach_context_prefix(payload.get("messages"))

        param = await self.captcha.acquire(auth.access_token) if self.captcha else None
        return json.dumps(payload, ensure_ascii=False).encode("utf-8"), param

    # ---- chat ----

    async def open_chat(
        self, auth: AccountAuth, body: bytes, meta: ChatMeta | None = None,
        *, force_fallback: bool = False,
    ) -> httpx.Response:
        """发起上游流式请求，返回已建立的上游响应（调用方负责 aclose）。

        按账号 protocol 分流：
          - openai    → POST {base}/v2/chat/completions（WorkBuddy / CodeBuddy）
          - anthropic → POST {base}/v1/messages（z.ai Coding Plan 等原生 Anthropic）

        `force_fallback`：强制走同账号的 API Key 回退通道（Plan 通道瞬态失败
        如 429 耗尽 / 风控 / 鉴权失效时）。账号状态本身不改 —— 回退是**请求级**
        路由决策，不能靠账号状态推导（Plan 通道 429 时账号仍是健康的）。
        """
        client = await self.client()
        if force_fallback and auth.has_fallback():
            auth = auth.as_fallback()
        base = _chat_base(self.cfg, auth)
        if auth.is_anthropic():
            url = base + PATH_ANTHROPIC_MESSAGES
            if is_zcode_plan(auth):
                # zcode.z.ai 网关：官方头组 + 一次性验证码参数 + 官方 system 形态
                hdrs = H.zcode_plan_headers(auth)
                body, param = await self._shape_zcode_plan_body(auth, body)
                if param:
                    hdrs["x-aliyun-captcha-verify-param"] = param
                    hdrs["x-aliyun-captcha-verify-region"] = self.captcha.cfg.region
            else:
                hdrs = H.anthropic_headers(auth)
        elif auth.is_zai():
            url = base + ZAI_CHAT
            hdrs = H.zai_headers(auth)
        else:
            url = base + PATH_CHAT
            hdrs = H.chat_headers(self.cfg.upstream, auth, meta)
        req = client.build_request("POST", url, content=body, headers=hdrs)
        try:
            resp = await client.send(req, stream=True)
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 400:
            text = await _safe_text(resp)
            await resp.aclose()
            raise _error_from(resp.status_code, text, dict(resp.headers))
        return resp

    async def chat_json(self, auth: AccountAuth, body: bytes) -> dict[str, Any]:
        """非流式 chat（内部仍走流式接口再聚合，见 gateway）。"""
        resp = await self.open_chat(auth, body)
        try:
            text = await _safe_text(resp)
        finally:
            await resp.aclose()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise UpstreamError(ErrorKind.UNKNOWN, 200, "invalid json", text) from exc

    async def iter_sse(self, resp: httpx.Response) -> AsyncIterator[bytes]:
        """逐行产出上游 SSE 行。"""
        async for line in resp.aiter_lines():
            yield line

    # ---- token 刷新 ----

    async def refresh_token(self, auth: AccountAuth) -> dict[str, Any]:
        """刷新 access_token，返回 {access_token, refresh_token, expires_in, domain}。"""
        if not auth.refresh_token:
            raise UpstreamError(ErrorKind.AUTH, 0, "no refresh_token")
        client = await self.client()
        base = _chat_base(self.cfg, auth)
        hdrs = H.refresh_headers(self.cfg.upstream, auth)
        try:
            resp = await client.post(base + PATH_REFRESH, headers=hdrs, json={})
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 400:
            raise _error_from(resp.status_code, resp.text, dict(resp.headers))
        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamError(ErrorKind.UNKNOWN, resp.status_code, "invalid json") from exc
        if not data.get("accessToken") and not data.get("access_token"):
            raise UpstreamError(ErrorKind.AUTH, resp.status_code, "refresh returned no token")
        return data

    # ---- 模型目录 ----

    async def fetch_models(self, auth: AccountAuth) -> list[dict[str, Any]]:
        """拉取账号可用模型列表（直连上游，不编造字段）。

        **两个端点合并**：`/v3/config`（主，权威倍率）+ 企业端点（`/v2` 或
        `/console`，补缺）。v3 独有模型（如 `deepseek-v4.1-flash` x0.00）只在
        v3 面；同 id 冲突以 v3 为准（实测 `hy4-preview` 在 v2 是 x0.00、v3 是
        x0.29，取 v3 才不会把付费模型误判为免费）。任一路失败不影响另一路。
        """
        if auth.is_zai():
            return await self._fetch_zai_models(auth)
        base = _chat_base(self.cfg, auth)
        enterprise_path = PATH_MODELS_GLOBAL if auth.is_global() else PATH_MODELS_CN
        v3_models, enterprise_models = await asyncio.gather(
            self._fetch_v3_models(auth, base),
            self._fetch_enterprise_models(auth, base, enterprise_path),
        )
        if not v3_models and not enterprise_models:
            raise UpstreamError(ErrorKind.UNKNOWN, 0, "models api returned empty list")
        return _merge_models(v3_models, enterprise_models)

    async def _fetch_v3_models(self, auth: AccountAuth, base: str) -> list[dict[str, Any]]:
        """`/v3/config` 模型面（主路）。失败返回空列表，由企业端点兜底。"""
        client = await self.client()
        hdrs = H.common_headers(self.cfg.upstream, auth)
        hdrs["Authorization"] = f"Bearer {auth.access_token}"
        try:
            resp = await client.get(base + PATH_V3_CONFIG, headers=hdrs)
        except httpx.HTTPError:
            return []
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return [m for m in _normalize_models(data) if not _is_non_chat_model(m)]

    async def _fetch_enterprise_models(
        self, auth: AccountAuth, base: str, path: str
    ) -> list[dict[str, Any]]:
        """企业模型端点（补充路）。失败返回空列表，由 v3 兜底。"""
        client = await self.client()
        hdrs = H.common_headers(self.cfg.upstream, auth)
        hdrs["Authorization"] = f"Bearer {auth.access_token}"
        try:
            resp = await client.get(base + path, headers=hdrs)
        except httpx.HTTPError:
            return []
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return [m for m in _normalize_models(data) if not _is_non_chat_model(m)]

    async def _fetch_zai_models(self, auth: AccountAuth) -> list[dict[str, Any]]:
        """z.ai 账号可用模型。

        Coding Plan 账号（base_url 指向 zcode.z.ai 网关）没有独立的模型目录端点，
        其可用模型由套餐权益决定，故从 `billing/balance` 的 entitlement 派生
        （该端点用同一 JWT 即可访问，且不像 /v1/messages 那样要求验证码）。
        其余 z.ai 账号回落到模型目录端点。
        """
        plan_models = await self._fetch_zcode_plan_models(auth)
        if plan_models:
            return plan_models

        rc = self.cfg.realm("zai")
        base = (rc.models_base or rc.chat_base or "").rstrip("/") if rc else ""
        if not base:
            return []
        client = await self.client()
        try:
            resp = await client.get(base + "/v1/models", headers=H.zai_headers(auth))
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 400:
            raise _error_from(resp.status_code, resp.text, dict(resp.headers))
        try:
            data = resp.json()
        except ValueError:
            return []
        return _normalize_models(data)

    async def _fetch_zcode_plan_models(self, auth: AccountAuth) -> list[dict[str, Any]]:
        """从 Coding Plan 套餐权益派生可用模型；非 Coding Plan 账号返回空。

        失败（网络/鉴权）或权益为空时返回空列表，由调用方回落模型目录端点，
        不因单条路径不可用而让整个模型列表请求失败。
        """
        url = _zcode_plan_billing_url(auth)
        if not url:
            return []
        client = await self.client()
        try:
            resp = await client.get(url, headers=H.zcode_billing_headers(auth))
        except httpx.HTTPError:
            return []
        if resp.status_code >= 400:
            return []
        try:
            data = resp.json()
        except ValueError:
            return []
        return _models_from_entitlements(data)

    # ---- 额度 ----

    async def fetch_credits(self, auth: AccountAuth) -> dict[str, Any]:
        """读取账号积分/额度。

        **必须 POST + 请求体**：该端点上游只接受 POST，GET 一律 404
        （实测 `/billing/meter/get-user-resource` 与 `/v2/...` 均如此）。
        global（workbuddy.ai）无 /v2 前缀，优先无前缀路径，404 再回落 /v2；
        CN 只有 /v2 形态。
        """
        if auth.is_zai():
            return await self._fetch_zai_credits(auth)
        client = await self.client()
        base = _billing_base(self.cfg, auth)
        hdrs = H.billing_headers(self.cfg.upstream, auth)
        paths = ([PATH_USER_RESOURCE_ALT, PATH_USER_RESOURCE]
                 if auth.is_global() else [PATH_USER_RESOURCE])
        body = _user_resource_body()
        last_err: UpstreamError | None = None
        for path in paths:
            try:
                resp = await client.post(base + path, headers=hdrs, json=body)
            except httpx.HTTPError as exc:
                last_err = UpstreamError(ErrorKind.NETWORK, 0, str(exc))
                continue
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                last_err = _error_from(resp.status_code, resp.text, dict(resp.headers))
                continue
            try:
                return _normalize_credits(resp.json())
            except ValueError:
                continue
        if last_err is not None:
            raise last_err
        return {}

    async def _fetch_zai_credits(self, auth: AccountAuth) -> dict[str, Any]:
        """z.ai 账号额度。

        Coding Plan 账号（base_url 指向 zcode.z.ai 网关）的额度在
        `/billing/balance` 的 `data.balances[]` 里（单位 token）；
        其余 z.ai 账号无对应端点，返回空。
        """
        url = _zcode_plan_billing_url(auth)
        if not url:
            return {}
        client = await self.client()
        try:
            resp = await client.get(url, headers=H.zcode_billing_headers(auth))
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 400:
            raise _error_from(resp.status_code, resp.text, dict(resp.headers))
        try:
            data = resp.json()
        except ValueError:
            return {}
        return _normalize_zcode_plan_credits(data)

    # ---- Coding Plan 计费族（billing/current|balance|usage|preview|claim）----

    def _plan_billing_base(self, auth: AccountAuth) -> str:
        """Coding Plan 计费基址；非 Coding Plan 账号返回空串（调用方跳过）。

        账号 base_url 形如 `https://zcode.z.ai/api/v1/zcode-plan/anthropic`，
        计费端点在 `/zcode-plan` 前缀下（与 messages 同源，但路径不同）。
        """
        base = (auth.base_url or "").rstrip("/")
        marker = base.find(ZCODE_PLAN_MARKER)
        if marker < 0:
            return ""
        return base[:marker + len(ZCODE_PLAN_MARKER)]

    async def _plan_billing_request(
        self, auth: AccountAuth, method: str, path: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Coding Plan 计费族请求（统一用桌面端 billing 头组）。

        与 messages 通道刻意分离：billing 用 `Z Code@electron` 形态的单段 UA
        与档案化平台头；缺版本 / 平台头时即使验证码有效，claim 也会被拒 3007。
        """
        base = self._plan_billing_base(auth)
        if not base:
            raise UpstreamError(ErrorKind.BAD_REQUEST, 0, "非 Coding Plan 账号")
        client = await self.client()
        headers = H.zcode_billing_headers(auth)
        extra = kwargs.pop("headers", None)
        if isinstance(extra, dict):
            headers.update(extra)
        try:
            resp = await client.request(method, base + path, headers=headers, **kwargs)
        except httpx.HTTPError as exc:
            raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 400:
            raise _error_from(resp.status_code, resp.text, dict(resp.headers))
        try:
            body = resp.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def fetch_plan_preview(self, auth: AccountAuth) -> dict[str, Any]:
        """拉取限时套餐投放（billing/preview，免验证码）。

        `platform` 参数跟账号档案走（实测 client/configs 才拒 platform，preview 宽容）。
        """
        profile = profile_for(auth)
        return await self._plan_billing_request(
            auth, "GET", C.PATH_BILLING_PREVIEW,
            params={"app_version": C.CLIENT_APP_VERSION, "platform": profile.platform_full},
        )

    async def claim_plan(
        self, auth: AccountAuth, plan_id: str,
        verify_param: str = "", verify_region: str = "",
    ) -> dict[str, Any]:
        """提交套餐领取（billing/claim）。验证码参数由调用方求解后传入。"""
        headers: dict[str, str] = {}
        if verify_param:
            headers[C.CAPTCHA_HEADER] = verify_param
        if verify_region:
            headers[C.CAPTCHA_REGION_HEADER] = verify_region
        return await self._plan_billing_request(
            auth, "POST", C.PATH_BILLING_CLAIM,
            headers=headers, json={"plan_id": plan_id},
        )

    async def report_activation_event(
        self, profile: Any, user_id: str, element: str
    ) -> None:
        """上报官方客户端激活事件（app_launch / app_daily_active）。

        无 Authorization（上游该端点不校验登录态）；HTTP >= 400 或业务码非 0
        抛 UpstreamError。事件体字段集收口在 telemetry 模块。
        """
        from . import telemetry

        await telemetry.post_activation_event(self, profile, user_id, element)

    # ---- 自动化任务 ----

    async def daily_checkin(self, auth: AccountAuth) -> dict[str, Any]:
        """每日签到（幂等，重复调用不重复计）。"""
        return await self._billing_post(auth, [PATH_CHECKIN, PATH_CHECKIN_ALT], {})

    async def travel(self, auth: AccountAuth) -> dict[str, Any]:
        """猫猫旅行 / 连续打卡。"""
        return await self._billing_post(
            auth, ["/v2/billing/activity/travel", "/billing/activity/travel"], {}
        )

    async def activity_event(self, auth: AccountAuth, event: str) -> dict[str, Any]:
        """成长任务行为事件上报（点亮任务并领奖）。"""
        return await self._billing_post(
            auth, ["/v2/report/activity/event", "/report/activity/event"], {"event": event}
        )

    async def keepalive(self, auth: AccountAuth) -> dict[str, Any]:
        """Token 保活：读一次额度即刷新活跃（国际版为专属 Token 集中保活）。"""
        data = await self.fetch_credits(auth)
        return {"keepalive": True, "credits": data.get("credits", 0)}

    async def _billing_post(
        self, auth: AccountAuth, paths: list[str], payload: dict[str, Any],
        method: str = "POST",
    ) -> dict[str, Any]:
        client = await self.client()
        base = _billing_base(self.cfg, auth)
        hdrs = H.billing_headers(self.cfg.upstream, auth)
        last_err: UpstreamError | None = None
        for path in paths:
            try:
                resp = await client.request(method, base + path, headers=hdrs, json=payload)
            except httpx.HTTPError as exc:
                last_err = UpstreamError(ErrorKind.NETWORK, 0, str(exc))
                continue
            if resp.status_code == 404:
                continue
            if resp.status_code >= 400:
                last_err = _error_from(resp.status_code, resp.text, dict(resp.headers))
                continue
            try:
                data = resp.json()
            except ValueError:
                return {"ok": True}
            code = str(data.get("code", 0))
            if code not in ("0", ""):
                msg = str(data.get("msg", ""))
                # 幂等场景：已签到 / 已领取不算失败
                if any(k in msg for k in ("已签", "已领", "重复", "already")):
                    return {"ok": True, "already": True, "msg": msg}
                return {"ok": False, "msg": msg, "code": code}
            return {"ok": True, "data": data}
        if last_err is not None:
            raise last_err
        return {"ok": False, "msg": "no endpoint available"}


# ---- 响应归一化 ----


async def _safe_text(resp: httpx.Response) -> str:
    try:
        return (await resp.aread()).decode("utf-8", errors="replace")
    except httpx.HTTPError:
        return ""


def _error_from(status: int, body: str, headers: dict[str, str]) -> UpstreamError:
    kind = classify_status(status, body)
    retry_after = 0.0
    raw_ra = headers.get("retry-after") or headers.get("Retry-After")
    if raw_ra:
        try:
            retry_after = float(raw_ra)
        except ValueError:
            retry_after = 0.0
    msg = ""
    try:
        data = json.loads(body)
        if isinstance(data, dict):
            msg = str(data.get("msg") or data.get("message") or "")
    except (json.JSONDecodeError, TypeError):
        msg = body[:200]
    return UpstreamError(kind, status, msg, body, retry_after)


def _model_items(data: Any) -> list[Any]:
    """从多种上游响应形状里取出模型数组。"""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    for key in ("data", "models", "list", "result"):
        value = data.get(key)
        if isinstance(value, list):
            return value
    nested = data.get("data")
    if isinstance(nested, dict):
        for key in ("models", "list"):
            value = nested.get(key)
            if isinstance(value, list):
                return value
    return []


def _is_non_chat_model(model: dict[str, Any]) -> bool:
    """判定非对话模型（补全 / 图片生成等），应从模型目录剔除。

    上游 v3 面含 `nes-` / `completion-` / `codewise-` 前缀的补全类条目、
    以及 max_output_tokens <= 256 的小条目与 `text-to-image` 标签条目；
    这些被选为模型时上游报 code=11102。
    """
    model_id = str(model.get("id", "")).lower()
    for prefix in ("nes-", "completion-", "codewise-"):
        if model_id.startswith(prefix):
            return True
    max_out = model.get("max_output_tokens")
    if isinstance(max_out, int) and 0 < max_out <= 256:
        return True
    tags = model.get("tags")
    if isinstance(tags, list):
        # 图片/视频生成等非对话能力，选中会被上游拒（code=11102）
        if any(str(t) in ("text-to-image", "image-to-image", "text-to-video",
                          "image-to-video") for t in tags):
            return True
    return False


def _merge_models(primary: list[dict[str, Any]],
                  secondary: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """合并两路模型目录：primary（v3）优先，secondary（企业端点）只补缺失 id。

    同 id 冲突以 primary 为准（倍率等字段以 v3 为权威）。输出顺序稳定：
    primary 原序在前，secondary 补充项在后。
    """
    merged: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source in (primary, secondary):
        for item in source:
            model_id = str(item.get("id", ""))
            if not model_id or model_id in seen:
                continue
            seen.add(model_id)
            merged.append(item)
    return merged


def _parse_credit_factor(raw: Any) -> float | None:
    """解析上游 `credits` 倍率字符串 → 数值。

    上游形态为 `"x0.79 credits"` / `"x0.00"` / `"x3.31"`，`x0.00` 表示免费模型。
    非字符串/无法解析返回 None（未知，不当作免费）。
    """
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    if not isinstance(raw, str):
        return None
    text = raw.strip().lower()
    if not text:
        return None
    if text.startswith("x"):
        text = text[1:].strip()
    # 去掉 "credits"/"credit" 等尾缀
    text = text.replace("credits", "").replace("credit", "").strip()
    try:
        return float(text)
    except ValueError:
        return None


def _normalize_models(data: Any) -> list[dict[str, Any]]:
    """把上游模型目录归一化为统一结构（只透出上游真实字段，不编造）。"""
    out: list[dict[str, Any]] = []
    for item in _model_items(data):
        if not isinstance(item, dict):
            continue
        model_id = item.get("id") or item.get("model") or item.get("name")
        if not model_id:
            continue
        entry: dict[str, Any] = {"id": str(model_id)}
        display = item.get("displayName") or item.get("display_name") or item.get("label")
        if display:
            entry["display_name"] = str(display)
        # 上游倍率（"x0.79 credits"）；0 表示免费模型
        factor = _parse_credit_factor(item.get("credits"))
        if factor is not None:
            entry["credit_factor"] = factor
        ctx = (item.get("contextWindow") or item.get("context_window")
               or item.get("context_length"))
        if isinstance(ctx, (int, float)) and ctx > 0:
            entry["context_window"] = int(ctx)
        max_out = (item.get("maxTokens") or item.get("max_output_tokens")
                   or item.get("maxOutputTokens"))
        if isinstance(max_out, (int, float)) and max_out > 0:
            entry["max_output_tokens"] = int(max_out)
        efforts = item.get("supportedEfforts") or item.get("reasoning_efforts")
        if isinstance(efforts, list) and efforts:
            entry["reasoning_efforts"] = [str(e) for e in efforts]
        default_effort = item.get("defaultEffort") or item.get("reasoning_default_effort")
        if isinstance(default_effort, str) and default_effort:
            entry["reasoning_default_effort"] = default_effort
        series = item.get("series") or item.get("group") or item.get("family")
        if isinstance(series, str) and series:
            entry["series"] = series
        if isinstance(item.get("supportsImages"), bool):
            entry["supports_images"] = item["supportsImages"]
        # 标签保留：用于剔除图片/视频生成等非对话模型（见 _is_non_chat_model）
        tags = item.get("tags")
        if isinstance(tags, list) and tags:
            entry["tags"] = [str(t) for t in tags]
        out.append(entry)
    return out


def _normalize_zcode_plan_credits(data: Any) -> dict[str, Any]:
    """归一化 Coding Plan 额度快照：`data.balances[]` → credits / used / expiring。

    每个 balance 是一条权益桶，字段为 `remaining_units` / `used_units` / `expires_at`。
    credits 取各桶剩余之和；used 取各桶已用之和；expiring 取 7 天内到期桶的剩余之和。

    单位说明：z.ai Coding Plan 的额度单位是 **token**（不同于 CN/global 的积分），
    此处沿用统一的 credits 字段承载「剩余额度」，单位差异由 realm 区分。
    """
    if not isinstance(data, dict):
        return {}
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    buckets = payload.get("balances")
    if not isinstance(buckets, list) or not buckets:
        return {}

    remaining = 0
    used = 0
    expiring = 0
    now = int(time.time())
    window = 7 * 86400
    for bucket in buckets:
        if not isinstance(bucket, dict):
            continue
        rem = bucket.get("remaining_units")
        if rem is None:
            rem = bucket.get("available_units")
        if isinstance(rem, (int, float)):
            remaining += int(rem)
        used_units = bucket.get("used_units")
        if isinstance(used_units, (int, float)):
            used += int(used_units)
        exp = bucket.get("expires_at")
        if isinstance(exp, (int, float)) and isinstance(rem, (int, float)):
            exp_s = int(exp / 1000) if exp > 10_000_000_000 else int(exp)
            if 0 < exp_s - now <= window:
                expiring += int(rem)

    result: dict[str, Any] = {"credits": max(0, remaining), "used": max(0, used)}
    if expiring:
        result["expiring"] = expiring
    return result


def _user_resource_body() -> dict[str, Any]:
    """get-user-resource 请求体（上游只认 POST，缺此体则 404/报错）。

    字段口径与官方客户端一致：ProductCode 固定 `p_tcaca`（CodeBuddy 产品码），
    Status `[0, 3]` 表示有效套餐，时间窗拉长到 101 年以纳入全部套餐。
    """
    from datetime import datetime, timedelta

    now = datetime.now()
    fmt = "%Y-%m-%d %H:%M:%S"
    return {
        "PageNumber": 1,
        "PageSize": 100,
        "ProductCode": "p_tcaca",
        "Status": [0, 3],
        "PackageEndTimeRangeBegin": now.strftime(fmt),
        "PackageEndTimeRangeEnd": (now + timedelta(days=365 * 101)).strftime(fmt),
    }


def _normalize_credits(data: Any) -> dict[str, Any]:
    """归一化额度响应：credits / used / expiring / packs。

    上游真实结构（实测）：
        data.Response.Data.{TotalDosage, Accounts[]}
        每个 Account 是一条套餐：CapacitySize/Remain/Used（总量口径）、
        CycleCapacitySize/Remain/Used（本周期口径）、CycleEndTime（到期时刻）。
    剩余取「周期剩余 + 总量剩余」的较大者口径——单套餐取 CycleCapacityRemain
    优先（周期配额是实际可用额度），缺失回落 CapacityRemain。
    """
    if not isinstance(data, dict):
        return {}
    payload = _credits_payload(data)
    if payload is None:
        return _normalize_credits_flat(data)

    accounts = payload.get("Accounts")
    if not isinstance(accounts, list) or not accounts:
        return _normalize_credits_flat(data)

    remaining = 0
    used = 0
    expiring = 0
    now = int(time.time())
    window = 7 * 86400
    packs = 0
    for acct in accounts:
        if not isinstance(acct, dict):
            continue
        packs += 1
        rem = _first_number(acct, "CycleCapacityRemain", "CapacityRemain")
        usd = _first_number(acct, "CycleCapacityUsed", "CapacityUsed")
        if rem is not None:
            remaining += max(0, int(rem))
        if usd is not None:
            used += max(0, int(usd))
        end = _parse_cycle_end(acct.get("CycleEndTime"))
        if end and rem is not None and 0 < end - now <= window:
            expiring += max(0, int(rem))

    result: dict[str, Any] = {"credits": remaining, "used": used, "packs": packs}
    if expiring:
        result["expiring"] = expiring
    return result


def _credits_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """定位 `data.Response.Data`（上游双层信封）；非该形态返回 None。"""
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    response = payload.get("Response") if isinstance(payload, dict) else None
    if isinstance(response, dict) and isinstance(response.get("Data"), dict):
        return response["Data"]
    return None


def _first_number(source: dict[str, Any], *keys: str) -> float | None:
    """按序取第一个数值字段（排除 bool）。"""
    for key in keys:
        value = source.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _parse_cycle_end(raw: Any) -> int:
    """`CycleEndTime`（"YYYY-MM-DD HH:MM:SS"，上游为 UTC+8 墙钟）→ epoch 秒。"""
    if not isinstance(raw, str) or not raw.strip():
        return 0
    from datetime import datetime, timedelta, timezone

    try:
        dt = datetime.strptime(raw.strip(), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return 0
    return int(dt.replace(tzinfo=timezone(timedelta(hours=8))).timestamp())


def _normalize_credits_flat(data: dict[str, Any]) -> dict[str, Any]:
    """兼容旧/简形态（顶层 credits / remain / used / packs）。"""
    if not isinstance(data, dict):
        return {}
    payload = data.get("data") if isinstance(data.get("data"), dict) else data
    credits = payload.get("credits")
    if credits is None:
        credits = payload.get("remain") if payload.get("remain") is not None \
            else payload.get("remaining")
    used = payload.get("used") or payload.get("usedCredits")
    expiring = payload.get("expiringCredits") or payload.get("expiring_credits") or 0
    packs = payload.get("packs") or payload.get("packages") or []
    result: dict[str, Any] = {}
    if isinstance(credits, (int, float)) and not isinstance(credits, bool):
        result["credits"] = int(credits)
    if isinstance(used, (int, float)) and not isinstance(used, bool):
        result["used"] = int(used)
    if isinstance(expiring, (int, float)) and not isinstance(expiring, bool):
        result["expiring"] = int(expiring)
    if isinstance(packs, list):
        result["packs"] = len(packs)
    # 过期窗口内的积分（上游给出到期明细时按 7 天窗口汇总）
    buckets = payload.get("buckets") or payload.get("details")
    if isinstance(buckets, list):
        soon = 0
        now = int(time.time())
        window = 7 * 86400
        for b in buckets:
            if not isinstance(b, dict):
                continue
            exp = b.get("expireAt") or b.get("expires_at")
            amount = b.get("credits") or b.get("amount") or 0
            if isinstance(exp, (int, float)) and isinstance(amount, (int, float)):
                exp_s = int(exp / 1000) if exp > 10_000_000_000 else int(exp)
                if 0 < exp_s - now <= window:
                    soon += int(amount)
        if soon:
            result["expiring"] = soon
    return result
