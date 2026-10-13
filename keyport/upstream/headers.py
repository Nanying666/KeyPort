"""出站请求头构造：按 realm 切换品牌段、Origin/Referer、身份归属头。

对齐官方桌面端 / CLI 指纹（详见开发文档 §3.4 设备指纹隔离）。
所有头构造集中在 build_* 函数，便于测试断言。

两条身份线的差异（按错会触发上游 3007 / 3012）：

- **messages 通道**（Coding Plan）：UA 带 `ai-sdk/anthropic/...` SDK 段，
  `http-referer` 指向 zcode.z.ai，`X-Title` = `Z Code@cli`，
  同时携带 `authorization` 与 `x-api-key`。
- **billing 通道**（billing/current|balance|preview|claim）：UA 单段
  `ZCode/<version>`，`X-Title` = `Z Code@electron`，平台/语言/时区/设备 ID
  按**账号设备档案**出值。
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ..config import UpstreamConfig
from . import constants as C
from .base import AccountAuth, ChatMeta, derive_machine_id, derive_session_id, new_hex_id
from .fingerprint import profile_for

# 官方桌面端版本段默认值（config 可覆盖）
DEFAULT_CLIENT_VERSION = "5.5.4"
DEFAULT_CLI_VERSION = "2.137.1"

ORIGIN_CN = "https://www.codebuddy.cn"
ORIGIN_GLOBAL = "https://www.workbuddy.ai"
ORIGIN_ZAI = C.ZAI_API_ORIGIN


def origin_referer(auth: AccountAuth) -> str:
    if auth.is_zai():
        return ORIGIN_ZAI
    return ORIGIN_GLOBAL if auth.is_global() else ORIGIN_CN


def accept_language(auth: AccountAuth) -> str:
    return "en-US" if auth.is_global() or auth.is_zai() else "zh-CN"


def client_version(cfg: UpstreamConfig) -> str:
    return cfg.client_version or DEFAULT_CLIENT_VERSION


def cli_version(cfg: UpstreamConfig) -> str:
    return cfg.cli_version or DEFAULT_CLI_VERSION


def default_user_agent(cfg: UpstreamConfig, auth: AccountAuth) -> str:
    """官方桌面端三段式 UA；global 平台段用 `WorkBuddy AI`（送错会触发风控 403）。"""
    ver = client_version(cfg)
    platform = "WorkBuddy AI" if auth.is_global() else "WorkBuddy"
    return f"WorkBuddy/{ver} {platform}/{ver} CLI/{cli_version(cfg)}"


def user_agent(cfg: UpstreamConfig, auth: AccountAuth) -> str:
    return cfg.user_agent or default_user_agent(cfg, auth)


def billing_user_agent(cfg: UpstreamConfig) -> str:
    """白名单类接口（billing/checkin）用单段 UA，不带 CLI 段。"""
    if cfg.client_name == "SaaS":
        return ""
    return f"WorkBuddy/{client_version(cfg)}"


def attribution_name(cfg: UpstreamConfig) -> str:
    return cfg.client_name or "WorkBuddy"


def resolve_device_token(cfg: UpstreamConfig, auth: AccountAuth) -> str:
    """设备风控 Token：账号级 > 全局配置 > 文件兜底。"""
    if auth.device_token:
        return auth.device_token
    if cfg.device_token:
        return cfg.device_token
    if cfg.device_token_file:
        try:
            return Path(cfg.device_token_file).read_text(encoding="utf-8").strip()
        except OSError:
            return ""
    return ""


def common_headers(cfg: UpstreamConfig, auth: AccountAuth) -> dict[str, str]:
    """所有上游 API 共享的请求头。"""
    origin = origin_referer(auth)
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "X-Requested-With": "XMLHttpRequest",
        "Origin": origin,
        "Referer": origin + "/",
        "User-Agent": user_agent(cfg, auth),
        "X-CodeBuddy-Request": "1",
        "Accept-Language": accept_language(auth),
    }
    # 账号级稳定设备头（跨重启恒定、账号间互异）
    if auth.uid:
        headers["X-Machine-ID"] = derive_machine_id(auth.uid)
        headers["X-Session-ID"] = derive_session_id(auth.uid)
    return headers


def _inject_attribution(cfg: UpstreamConfig, headers: dict[str, str]) -> None:
    """用量归属头：伪造官方 WorkBuddy 桌面端头组。"""
    name = attribution_name(cfg)
    if name == "SaaS":
        headers["X-Product"] = "SaaS"
        return
    headers["X-Agent-Purpose"] = "conversation"
    headers["X-IDE-Name"] = name
    headers["X-IDE-Type"] = name
    headers["X-IDE-Version"] = client_version(cfg)
    headers["X-Product"] = name


def _valid_trace_id(value: str) -> bool:
    if len(value) not in (16, 32):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value)


def _inject_conversation(headers: dict[str, str], meta: ChatMeta) -> str:
    """会话头族：对话/请求/消息/B3 链路。返回本次 message_id。"""
    conv_req_id = meta.conversation_request_id or new_hex_id(16)
    message_id = new_hex_id(16)
    if meta.conversation_id:
        headers["X-Conversation-ID"] = meta.conversation_id
    headers["X-Conversation-Request-ID"] = conv_req_id
    headers["X-Conversation-Message-ID"] = message_id
    headers["X-Request-ID"] = message_id
    headers["X-Root-Request-ID"] = conv_req_id
    headers["X-Trace-ID"] = meta.trace_id or conv_req_id
    b3_trace = conv_req_id if _valid_trace_id(conv_req_id) else message_id
    headers["X-B3-TraceId"] = b3_trace
    headers["X-B3-SpanId"] = message_id[:16]
    headers["X-B3-Sampled"] = "1"
    return message_id


def _inject_global_chat_headers(headers: dict[str, str], auth: AccountAuth) -> None:
    headers["X-User-Id"] = auth.uid
    if auth.enterprise_id:
        headers["X-Enterprise-Id"] = auth.enterprise_id
        headers["X-Tenant-Id"] = auth.enterprise_id
    if auth.domain:
        headers["X-Domain"] = auth.domain


def chat_headers(
    cfg: UpstreamConfig, auth: AccountAuth, meta: ChatMeta | None = None
) -> dict[str, str]:
    """chat/completions 出站头（流式）。"""
    meta = meta or ChatMeta()
    headers = common_headers(cfg, auth)
    headers["Accept"] = "text/event-stream"
    headers["Authorization"] = f"Bearer {auth.access_token}"
    # CN 域租户/部门标识：缺失时显式发「无」标记，对齐官方客户端
    if auth.is_global() or auth.is_zai():
        _inject_global_chat_headers(headers, auth)
    else:
        if auth.enterprise_id:
            headers["X-Enterprise-Id"] = auth.enterprise_id
        else:
            headers["X-No-Enterprise-Id"] = "1"
        if auth.domain:
            headers["X-Domain"] = auth.domain
        else:
            headers["X-No-Department-Info"] = "1"
    _inject_attribution(cfg, headers)
    if cfg.passthrough_ip and meta.client_ip:
        for h in ("X-Forwarded-For", "X-Real-IP", "X-Client-IP"):
            headers[h] = meta.client_ip
    token = resolve_device_token(cfg, auth)
    if token:
        headers["X-Device-Token"] = token
    _inject_conversation(headers, meta)
    return {k: v for k, v in headers.items() if v != ""}


def billing_headers(cfg: UpstreamConfig, auth: AccountAuth) -> dict[str, str]:
    """billing / checkin / 余额查询接口头。"""
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "X-CodeBuddy-Request": "1",
        "Accept-Language": accept_language(auth),
    }
    ua = billing_user_agent(cfg)
    if ua:
        headers["User-Agent"] = ua
    if auth.uid:
        headers["X-User-Id"] = auth.uid
        headers["X-Machine-ID"] = derive_machine_id(auth.uid)
        headers["X-Session-ID"] = derive_session_id(auth.uid)
    if auth.enterprise_id:
        headers["X-Enterprise-Id"] = auth.enterprise_id
        headers["X-Tenant-Id"] = auth.enterprise_id
    if auth.domain:
        headers["X-Domain"] = auth.domain
    token = resolve_device_token(cfg, auth)
    if token:
        headers["X-Device-Token"] = token
    return headers


def refresh_headers(cfg: UpstreamConfig, auth: AccountAuth) -> dict[str, str]:
    """refresh 端点专属头（X-Refresh-Token 只出现在这里）。"""
    headers = common_headers(cfg, auth)
    headers["X-Refresh-Token"] = auth.refresh_token
    if auth.enterprise_id:
        headers["X-Enterprise-Id"] = auth.enterprise_id
    headers["X-Auth-Refresh-Source"] = "plugin"
    return headers


def zai_headers(auth: AccountAuth) -> dict[str, str]:
    """z.ai 出站头（Bearer API Key / JWT，OpenAI 与 Anthropic 双协议共用）。"""
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Accept-Language": "en-US,en",
        "User-Agent": user_agent(UpstreamConfig(), auth),
    }
    if auth.uid:
        headers["X-Machine-ID"] = derive_machine_id(auth.uid)
    # ZCode 的设备身份：服务端按「账号 + 设备」计算额度，缺了会查成空
    if auth.device_token:
        headers["X-Device-Mid"] = auth.device_token
    return headers


def _device_mid(auth: AccountAuth) -> str:
    """账号设备 ID：凭证显式携带的 virtualDeviceMid 优先，其次设备档案。

    凭证里的 MID 必须优先 —— 上游按「账号 + 设备」算额度，换掉它会查成空。
    """
    return auth.device_token or profile_for(auth).device_mid


def zcode_plan_headers(auth: AccountAuth) -> dict[str, str]:
    """zcode.z.ai Coding Plan messages 端点头组（按官方 CLI 真实请求复刻）。

    与通用 anthropic_headers 的关键差异（按错实测导致 3007 / 3012）：
      - `http-referer` 是 `https://zcode.z.ai`（不是 api.z.ai）
      - UA 的 SDK 段是 `ai-sdk/anthropic/...`
      - 必须带 `x-zcode-app-version` / `x-zcode-agent`
      - 同时带 `authorization` 与 `x-api-key`
    验证码参数由调用方注入 `x-aliyun-captcha-verify-param`。

    平台/语言/时区取自账号设备档案（一号一台），消灭「多账号同一设备」信号。
    """
    profile = profile_for(auth)
    headers = {
        "accept-encoding": "gzip",
        "anthropic-version": C.ANTHROPIC_VERSION,
        "authorization": f"Bearer {auth.access_token}",
        "content-type": "application/json",
        "http-referer": C.HTTP_REFERER,
        "user-agent": f"ZCode/{C.CLIENT_APP_VERSION} {C.CLIENT_SDK_UA_SEGMENT}",
        "x-api-key": auth.access_token,
        "x-client-language": profile.language,
        "x-client-timezone": profile.timezone,
        "x-os-category": profile.os_category,
        "x-os-version": profile.os_version,
        "x-platform": profile.platform_full,
        "x-release-channel": C.RELEASE_CHANNEL_CLI,
        "x-request-id": str(uuid.uuid4()),
        "x-title": C.TITLE_CLI,
        "x-zcode-agent": C.X_ZCODE_AGENT,
        "x-zcode-app-version": C.CLIENT_APP_VERSION,
        "x-zcode-session-type": "main",
        "x-zcode-trace-id": str(uuid.uuid4()),
        "x-device-mid": _device_mid(auth),
    }
    return headers


def zcode_billing_headers(auth: AccountAuth) -> dict[str, str]:
    """Coding Plan billing 族头组（current / balance / usage / preview / claim）。

    与 messages 通道刻意分离（对齐官方桌面端而非 CLI）：
      - UA 单段 `ZCode/<version>`，`X-Title` = `Z Code@electron`
      - 平台/语言/时区/设备 ID 按账号设备档案出值
      - 每请求全新 x-request-id
    缺版本 / 平台头时，即使验证码有效，claim 也会被拒 3007。
    """
    profile = profile_for(auth)
    headers = {
        "Content-Type": "application/json",
        "User-Agent": f"ZCode/{C.CLIENT_APP_VERSION}",
        "HTTP-Referer": C.HTTP_REFERER,
        "X-Title": C.TITLE_DESKTOP,
        "X-ZCode-App-Version": C.CLIENT_APP_VERSION,
        "X-Platform": profile.platform_full,
        "X-Release-Channel": C.RELEASE_CHANNEL_DESKTOP,
        "X-Client-Language": profile.language,
        "X-Client-Timezone": profile.timezone,
        "X-Os-Category": profile.os_category,
        "X-Os-Version": profile.os_version,
        "X-Device-Mid": _device_mid(auth),
        "x-request-id": str(uuid.uuid4()),
    }
    headers["Authorization"] = f"Bearer {auth.access_token}"
    return headers


def anthropic_headers(auth: AccountAuth) -> dict[str, str]:
    """Anthropic 原生上游出站头（api.z.ai API Key 回退 / bigmodel）。

    令牌走 `Authorization: Bearer`，并携带设备身份与客户端标识头。
    身份字段按账号设备档案出值（不再写死 windows-x64）。
    """
    profile = profile_for(auth)
    headers = {
        "Authorization": f"Bearer {auth.access_token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "anthropic-version": C.ANTHROPIC_VERSION,
        "anthropic-beta": "prompt-caching-2024-07-31",
        "User-Agent": f"ZCode/{C.CLIENT_APP_VERSION}",
        "HTTP-Referer": ORIGIN_ZAI,
        "X-Title": C.TITLE_DESKTOP,
        "X-ZCode-App-Version": C.CLIENT_APP_VERSION,
        "X-Platform": profile.platform_full,
        "X-Release-Channel": C.RELEASE_CHANNEL_DESKTOP,
        "X-Client-Language": profile.language,
        "X-Client-Timezone": profile.timezone,
        "X-Os-Category": profile.os_category,
        "X-Os-Version": profile.os_version,
    }
    if auth.device_token:
        headers["X-Device-Mid"] = auth.device_token
    return headers
