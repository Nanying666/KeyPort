"""OAuth 设备授权登录（免本地桌面客户端）。

## WorkBuddy / CodeBuddy（无 PKCE，state 由服务端签发）

  1. POST {chat_base}/v2/plugin/auth/state?platform=CLI → {state, authUrl}
  2. 用户在浏览器打开 authUrl 完成登录
  3. GET  {chat_base}/v2/plugin/auth/token?state=     → {accessToken, refreshToken, …}
  4. GET  {chat_base}/v2/plugin/login/account?state=  → {uid, enterpriseId, nickname}

## z.ai（ZCode Coding Plan，官方 CLI 协议）

  1. POST {zcode_origin}/api/v1/oauth/cli/init   → {flow_id, authorize_url, poll_token?}
  2. 用户在浏览器打开 authorize_url 完成登录
  3. GET  {zcode_origin}/api/v1/oauth/cli/poll/{flow_id} → {access_token, …}

**干净请求头规范**：官方 CLI 的 OAuth 请求只带 `Authorization: Bearer <poll_token>`
与 `Content-Type`，不携带任何设备伪装头 —— 多带会让上游对 OAuth 会话产生异常的
设备/上下文绑定限制。

**poll_token 归属**：2026-09 起官方协议由 init 响应服务端下发 `poll_token`，后续
poll 必须采用该值；仅当服务端未返回时回落自造 token（保留旧协议兼容）。
poll 返回的 `code=3004` 明确映射为「会话过期」。
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass
from typing import Any

import httpx

from ..config import Config
from ..upstream import constants as C
from ..upstream.base import AccountAuth, ErrorKind, UpstreamError
from ..upstream.headers import ORIGIN_CN, ORIGIN_GLOBAL, ORIGIN_ZAI

STATE_PATH = "/v2/plugin/auth/state"
TOKEN_PATH = "/v2/plugin/auth/token"
ACCOUNT_PATH = "/v2/plugin/login/account"

# z.ai 官方 CLI OAuth 路径（相对 ZCODE_ORIGIN/api/v1）
ZAI_CLI_INIT_PATH = "/oauth/cli/init"
ZAI_CLI_POLL_PATH = "/oauth/cli/poll"      # + /{flow_id}
# poll 承载的会话过期业务码（官方口径）
ZAI_SESSION_EXPIRED_CODE = 3004


def zai_oauth_base() -> str:
    """z.ai OAuth API 基址（= ZCODE_ORIGIN + /api/v1）。"""
    return f"{C.ZCODE_ORIGIN}/api/v1"


@dataclass
class LoginState:
    state: str
    auth_url: str
    realm: str
    created_at: int


def _base_and_origin(cfg: Config, realm: str) -> tuple[str, str]:
    rc = cfg.realm(realm)
    base = (rc.chat_base if rc and rc.chat_base else "https://copilot.tencent.com").rstrip("/")
    origin = {"global": ORIGIN_GLOBAL, "zai": ORIGIN_ZAI}.get(realm, ORIGIN_CN)
    return base, origin


def _headers(origin: str, token: str = "") -> dict[str, str]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def realm_from_domain(realm: str, domain: str) -> str:
    """realm 归一化：显式 realm 优先，否则按 domain 推断（恒非空）。"""
    if realm:
        return realm
    d = (domain or "").lower()
    if "workbuddy" in d or "z.ai" in d or "zai" in d:
        return "global"
    return "cn"


class OAuthClient:
    """OAuth 设备授权客户端（登录期短生命周期，独立连接池）。"""

    def __init__(self, cfg: Config, timeout: float = 30.0):
        self.cfg = cfg
        self._timeout = timeout

    async def request_state(self, realm: str = "cn") -> LoginState:
        """取授权 URL（state 由服务端签发，无 PKCE）。"""
        base, origin = _base_and_origin(self.cfg, realm)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    base + STATE_PATH + "?platform=CLI",
                    headers=_headers(origin),
                    content=b"{}",
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamError(ErrorKind.NETWORK, resp.status_code, resp.text[:200])
        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamError(ErrorKind.UNKNOWN, resp.status_code, "invalid json") from exc
        state = str(data.get("state") or "")
        auth_url = str(data.get("authUrl") or data.get("auth_url") or "")
        if not state or not auth_url:
            raise UpstreamError(ErrorKind.UNKNOWN, resp.status_code,
                                "state 端点未返回 state/authUrl")
        return LoginState(state=state, auth_url=auth_url, realm=realm,
                          created_at=int(time.time()))

    async def poll_token(self, realm: str, state: str) -> dict[str, Any]:
        """轮询 token（登录完成前上游返回非 0 业务码 → 视为「未完成」）。"""
        base, origin = _base_and_origin(self.cfg, realm)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(base + TOKEN_PATH + f"?state={state}",
                                        headers=_headers(origin))
            except httpx.HTTPError as exc:
                raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 500:
            raise UpstreamError(ErrorKind.NETWORK, resp.status_code, resp.text[:200])
        if resp.status_code >= 400:
            raise UpstreamError(ErrorKind.AUTH, resp.status_code, "登录未完成")
        try:
            data = resp.json()
        except ValueError as exc:
            raise UpstreamError(ErrorKind.AUTH, resp.status_code, "登录未完成") from exc
        if not (data.get("accessToken") or data.get("access_token")):
            raise UpstreamError(ErrorKind.AUTH, resp.status_code, "登录未完成")
        return data

    async def fetch_account(self, realm: str, state: str, access_token: str) -> dict[str, Any]:
        base, origin = _base_and_origin(self.cfg, realm)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(base + ACCOUNT_PATH + f"?state={state}",
                                        headers=_headers(origin, access_token))
            except httpx.HTTPError:
                return {}
        if resp.status_code >= 400:
            return {}
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    async def complete(self, state: str, realm: str = "cn") -> AccountAuth:
        """完整走完 poll + account，产出可直接入库的 AccountAuth。"""
        token = await self.poll_token(realm, state)
        access = str(token.get("accessToken") or token.get("access_token") or "")
        refresh = str(token.get("refreshToken") or token.get("refresh_token") or "")
        expires_in = int(token.get("expiresIn") or token.get("expires_in") or 0)
        domain = str(token.get("domain") or "")
        acct = await self.fetch_account(realm, state, access)
        uid = str(acct.get("uid") or acct.get("userId") or "")
        if not uid:
            # 无 uid 时用 access_token 摘要兜底，保证账号可入库且跨重启稳定
            uid = "oauth-" + hashlib.sha256(access.encode()).hexdigest()[:16]
        return AccountAuth(
            uid=uid,
            realm=realm_from_domain(realm, domain),
            access_token=access,
            refresh_token=refresh,
            expires_at=int(time.time()) + expires_in if expires_in else 0,
            domain=domain,
            enterprise_id=str(acct.get("enterpriseId") or acct.get("enterprise_id") or ""),
            nickname=str(acct.get("nickname") or ""),
            kind="oauth",
        )


def api_key_account(
    realm: str, api_key: str, name: str = "", base_url: str = "",
    protocol: str = "",
) -> AccountAuth:
    """把 API Key 包装成账号（z.ai / 兼容 OpenAI 的第三方端点）。

    kind=apikey：不可 refresh，token 不过期。

    `base_url` 必须落到 `AccountAuth.base_url`（出站端点来源）——历史 bug 把它
    写进了 `file_path`，导致通过管理台「API Key 纳管」添加的 Coding Plan 账号
    既不走 `/zcode-plan` 端点，也无法被识别为 ZCode 平台账号（额度查询与套餐
    领取全部静默失效）。

    `protocol` 留空时按端点推断：Coding Plan / api.z.ai / bigmodel 的 anthropic
    兼容端点走 Anthropic 原生协议，其余走 OpenAI chat。
    """
    uid = f"apikey-{hashlib.sha256((api_key + '|' + realm).encode()).hexdigest()[:16]}"
    resolved_base = (base_url or "").strip().rstrip("/")
    if not protocol:
        protocol = "anthropic" if "/anthropic" in resolved_base else "openai"
    return AccountAuth(
        uid=uid,
        realm=realm,
        access_token=api_key,
        refresh_token="",
        expires_at=0,
        nickname=name or uid,
        kind="apikey",
        base_url=resolved_base,
        protocol=protocol,
    )


@dataclass
class ZaiCliFlow:
    """z.ai 官方 CLI OAuth 会话（init → poll）。"""

    flow_id: str
    authorize_url: str
    poll_token: str
    server_poll_token: bool = False   # poll_token 是否由服务端下发（新协议）


class ZaiOAuthClient:
    """z.ai（ZCode Coding Plan）OAuth CLI 客户端。

    严格遵循官方 CLI 的干净请求头规范：只带 `Authorization: Bearer <poll_token>`、
    `Content-Type` 与一个朴素的 UA。**不携带任何设备指纹头**（X-Device-Mid /
    X-Platform / X-Os-* / X-Title / HTTP-Referer 等）—— 那些是业务通道的伪装，
    用在 OAuth 会话上会让上游把会话与「某台设备」绑定，反而增加异常判定。
    """

    def __init__(self, api_base: str = "", timeout: float = 30.0):
        self.api_base = (api_base or zai_oauth_base()).rstrip("/")
        self._timeout = timeout

    @staticmethod
    def _headers(poll_token: str = "") -> dict[str, str]:
        # UA 显式钉死：httpx 默认 UA 会暴露客户端库与版本，与官方 CLI 不符
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "ZCode-CLI",
        }
        if poll_token:
            headers["Authorization"] = f"Bearer {poll_token}"
        return headers

    async def init(self) -> ZaiCliFlow:
        """发起 OAuth 流程，取授权 URL 与（服务端下发的）poll_token。"""
        # 旧协议回落用的自造 token；服务端下发时以服务端为准
        self_token = secrets.token_hex(32)
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.post(
                    self.api_base + ZAI_CLI_INIT_PATH,
                    headers=self._headers(self_token),
                    json={"provider": "zai"},
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 400:
            raise UpstreamError(ErrorKind.NETWORK, resp.status_code, resp.text[:200])
        try:
            body = resp.json()
        except ValueError as exc:
            raise UpstreamError(ErrorKind.UNKNOWN, resp.status_code, "invalid json") from exc

        code = body.get("code")
        if code is not None and code != 0:
            raise UpstreamError(
                ErrorKind.UNKNOWN, resp.status_code,
                f"OAuth init 被上游拒绝（code={code}）",
            )
        data = body.get("data") or {}
        flow_id = str(data.get("flow_id") or data.get("flowId") or "")
        authorize_url = str(data.get("authorize_url") or data.get("authorizeUrl") or "")
        if not flow_id or not authorize_url:
            raise UpstreamError(ErrorKind.UNKNOWN, resp.status_code,
                                "OAuth init 返回的流程数据不完整")
        server_token = str(data.get("poll_token") or data.get("pollToken") or "").strip()
        return ZaiCliFlow(
            flow_id=flow_id,
            authorize_url=authorize_url,
            poll_token=server_token or self_token,
            server_poll_token=bool(server_token),
        )

    async def poll(self, flow: ZaiCliFlow) -> dict[str, Any]:
        """轮询登录结果；未完成时抛 AUTH（调用方按「等待中」处理）。

        `code=3004` 明确映射为会话过期 —— 与「还没登录」区分开，避免前端
        无限等待一个已经失效的流程。
        """
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            try:
                resp = await client.get(
                    f"{self.api_base}{ZAI_CLI_POLL_PATH}/{flow.flow_id}",
                    headers=self._headers(flow.poll_token),
                )
            except httpx.HTTPError as exc:
                raise UpstreamError(ErrorKind.NETWORK, 0, str(exc)) from exc
        if resp.status_code >= 500:
            raise UpstreamError(ErrorKind.NETWORK, resp.status_code, resp.text[:200])
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}

        code = body.get("code")
        if code == ZAI_SESSION_EXPIRED_CODE:
            # 会话过期必须与「尚未登录」区分开：统一用 401 作为哨兵，
            # 让上层能干净地清掉登录态而不是无限轮询（HTTP 可能仍是 200）。
            raise UpstreamError(ErrorKind.AUTH, 401, "会话已过期，请重新发起登录")
        if resp.status_code >= 400 or (code is not None and code != 0):
            raise UpstreamError(ErrorKind.AUTH, resp.status_code or 200, "登录未完成")

        data = body.get("data") or {}
        if not isinstance(data, dict):
            data = {}
        if not (data.get("access_token") or data.get("accessToken")):
            raise UpstreamError(ErrorKind.AUTH, resp.status_code, "登录未完成")
        return data

    async def complete(self, flow: ZaiCliFlow) -> AccountAuth:
        """轮询成功后产出可直接入库的 zai 账号凭证。"""
        data = await self.poll(flow)
        access = str(data.get("access_token") or data.get("accessToken") or "")
        refresh = str(data.get("refresh_token") or data.get("refreshToken") or "")
        expires_in = int(data.get("expires_in") or data.get("expiresIn") or 0)
        user = data.get("user") if isinstance(data.get("user"), dict) else {}
        uid = str(data.get("user_id") or user.get("id") or "").strip()
        if not uid:
            uid = "zai-" + hashlib.sha256(access.encode()).hexdigest()[:16]
        nickname = str(user.get("displayName") or user.get("name") or "").strip() or uid
        return AccountAuth(
            uid=uid,
            realm="zai",
            access_token=access,
            refresh_token=refresh,
            expires_at=int(time.time()) + expires_in if expires_in else 0,
            nickname=nickname,
            kind="oauth",
            base_url=C.ZCODE_PLAN_ANTHROPIC_BASE,
            protocol="anthropic",
        )
