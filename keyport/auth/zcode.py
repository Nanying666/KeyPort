"""ZCode 应用导出的账号包解析（`zcode-accounts-bundle`）。

来源：ZCode 客户端 / zcode-pool 的「导出账号」产物，形如：

  {
    "format": "zcode-accounts-bundle",
    "version": 2,
    "exportedAt": "2026-09-26 16:53",
    "accounts": [
      {
        "name": "iyzcfc",
        "credentials": {
          "oauth:active_provider": "zai",
          "oauth:zai:access_token": "<Z.ai 平台 token>",
          "oauth:zai:user_info": "{\"id\":\"...\",\"displayName\":\"iyzcfc\",...}",
          "zcodejwttoken": "<Coding Plan JWT>"
        },
        "virtualDeviceMid": "78b921b4-...",
        "virtualArmsUid": "uid_ediilp3hfowr1pt9",
        "config": {"provider": {"builtin:zai-start-plan": {...}}}
      }
    ]
  }

语义（对齐 zcode-pool 实现，非推测）：
- `zcodejwttoken` 是 Coding Plan 令牌，**直接当 API Key 用**，端点为
  `https://zcode.z.ai/api/v1/zcode-plan/anthropic`（Anthropic 原生协议）。
- `oauth:<provider>:access_token` 是各平台登录令牌；`oauth:active_provider`
  指明当前启用的是哪家。
- `config.provider` 里 `enabled=true` 且 `kind=="anthropic"` 的条目给出真实
  上游 `baseURL`；`options.apiKey` 有值时优先用它。
- `virtualDeviceMid` 是设备身份，必须随请求发送（`X-Device-Mid`）：服务端按
  「账号 + 设备」计算额度，缺了它额度会查成空。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from ..upstream.base import AccountAuth

BUNDLE_FORMAT = "zcode-accounts-bundle"
KIND_APIKEY = "apikey"

# ZCode Coding Plan 的 Anthropic 原生端点（zcode-pool oauth.rs: START_PLAN_ANTHROPIC_BASE）
ZCODE_START_PLAN_BASE = "https://zcode.z.ai/api/v1/zcode-plan/anthropic"
ZAI_ANTHROPIC_BASE = "https://api.z.ai/api/anthropic"
BIGMODEL_ANTHROPIC_BASE = "https://open.bigmodel.cn/api/anthropic"


def is_zcode_bundle(doc: Any) -> bool:
    """是否为 ZCode 账号包。"""
    return isinstance(doc, dict) and doc.get("format") == BUNDLE_FORMAT


def _active_provider(creds: dict) -> str:
    value = creds.get("oauth:active_provider")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return "zai"


def _enabled_anthropic_provider(config: Any) -> dict | None:
    """取出 config.provider 里启用的 anthropic 条目（含 baseURL / apiKey）。

    多个启用时优先带 apiKey 的（更能直接用于出站）。
    """
    providers = (config or {}).get("provider") if isinstance(config, dict) else None
    if not isinstance(providers, dict):
        return None
    candidates: list[dict] = []
    for pid, entry in providers.items():
        if not isinstance(entry, dict) or not entry.get("enabled"):
            continue
        if entry.get("kind") != "anthropic":
            continue
        options = entry.get("options") if isinstance(entry.get("options"), dict) else {}
        base = str(options.get("baseURL") or "").strip()
        if not base:
            continue
        candidates.append({
            "id": str(pid),
            "base_url": base.rstrip("/"),
            "api_key": str(options.get("apiKey") or "").strip(),
            "name": str(entry.get("name") or ""),
        })
    if not candidates:
        return None
    with_key = [c for c in candidates if c["api_key"]]
    return (with_key or candidates)[0]


def _fallback_base(provider: str) -> str:
    return ZAI_ANTHROPIC_BASE if provider == "zai" else BIGMODEL_ANTHROPIC_BASE


def _platform_token(creds: dict, provider: str) -> str:
    """取平台登录令牌：优先当前 provider，其次 zai / bigmodel。"""
    for key in (
        f"oauth:{provider}:access_token",
        "oauth:zai:access_token",
        "oauth:bigmodel:access_token",
    ):
        value = creds.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _user_info(creds: dict, provider: str) -> dict:
    for key in (f"oauth:{provider}:user_info", "oauth:zai:user_info",
                "oauth:bigmodel:user_info"):
        raw = creds.get(key)
        if isinstance(raw, str) and raw.strip():
            try:
                parsed = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    return {}


@dataclass
class ZCodeCandidate:
    """账号包里的一条候选凭证。"""

    uid: str
    nickname: str = ""
    realm: str = "zai"
    kind: str = KIND_APIKEY
    base_url: str = ""
    protocol: str = "anthropic"
    device_token: str = ""
    token: str = ""
    platform_token: str = ""
    provider_id: str = ""
    arms_uid: str = ""
    # 回退通道：主令牌为 Coding Plan JWT 时，平台令牌 + api.z.ai 端点作为第二把钥匙
    fallback_token: str = ""
    fallback_base_url: str = ""
    raw: dict = field(default_factory=dict)

    def to_auth(self) -> AccountAuth:
        return AccountAuth(
            uid=self.uid,
            realm=self.realm,
            access_token=self.token,
            refresh_token="",
            expires_at=0,
            device_token=self.device_token,
            nickname=self.nickname,
            kind=self.kind,
            base_url=self.base_url,
            protocol=self.protocol,
            fallback_token=self.fallback_token,
            fallback_base_url=self.fallback_base_url,
        )


def _fallback_uid(entry: dict, creds: dict, user_info: dict) -> str:
    """凭证未带 id 时用令牌摘要兜底，保证稳定可入库。"""
    seed = (
        str(creds.get("zcodejwttoken") or "")
        or str(user_info.get("id") or "")
        or str(entry.get("virtualArmsUid") or "")
        or json.dumps(creds, sort_keys=True, ensure_ascii=False)
    )
    return "zcode-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:16]


def parse_zcode_bundle(doc: dict) -> tuple[list[ZCodeCandidate], list[dict]]:
    """解析账号包 → (候选列表, 失败明细)。

    每个账号产出 1 条候选：优先「启用的 anthropic provider + 其 apiKey」，
    没有则回落到 Coding Plan JWT，再没有则用平台登录令牌。
    """
    accounts = doc.get("accounts")
    if not isinstance(accounts, list):
        return [], [{"index": "-", "reason": "账号包缺少 accounts 数组"}]

    out: list[ZCodeCandidate] = []
    failed: list[dict] = []

    for index, entry in enumerate(accounts):
        if not isinstance(entry, dict):
            failed.append({"index": index, "reason": "账号条目必须是对象"})
            continue

        creds = entry.get("credentials")
        if not isinstance(creds, dict):
            failed.append({"index": index, "reason": "账号条目缺少 credentials"})
            continue

        provider = _active_provider(creds)
        user_info = _user_info(creds, provider)
        provider_entry = _enabled_anthropic_provider(entry.get("config"))
        jwt = str(creds.get("zcodejwttoken") or "").strip()
        platform = _platform_token(creds, provider)

        # 令牌优先级：provider 显式 apiKey > Coding Plan JWT > 平台登录令牌
        # base_url 必须与最终采用的令牌配套：令牌取不到就不设 base_url，
        # 否则平台令牌会被发到 Coding Plan 端点（上游 401）。
        token = ""
        base_url = ""
        provider_id = ""
        if provider_entry:
            token = provider_entry["api_key"]
            base_url = provider_entry["base_url"]
            provider_id = provider_entry["id"]
        if not token and jwt:
            token = jwt
            base_url = base_url or ZCODE_START_PLAN_BASE
        if not token and platform:
            token = platform
            base_url = base_url or _fallback_base(provider)

        if not token:
            failed.append({
                "index": index,
                "reason": "凭证里没有可用令牌"
                          "（zcodejwttoken / provider apiKey / oauth access_token 均为空）",
            })
            continue

        # 回退通道：主令牌是 Coding Plan JWT 时，平台登录令牌 + api.z.ai 作为
        # 第二把钥匙。Plan 通道 429 耗尽 / 鉴权失效 / 风控时可无缝切过去，
        # 避免整号被判死（该通道免验证码、不吃 Plan 额度）。
        fallback_token = ""
        fallback_base_url = ""
        if token == jwt and platform and platform != token:
            fallback_token = platform
            fallback_base_url = ZAI_ANTHROPIC_BASE

        uid = (
            str(user_info.get("id") or "").strip()
            or str(entry.get("virtualArmsUid") or "").strip()
            or _fallback_uid(entry, creds, user_info)
        )
        nickname = (
            str(entry.get("name") or "").strip()
            or str(user_info.get("displayName") or "").strip()
            or str(user_info.get("username") or "").strip()
            or uid
        )

        out.append(ZCodeCandidate(
            uid=uid,
            nickname=nickname,
            realm="zai",
            kind=KIND_APIKEY,
            base_url=base_url,
            protocol="anthropic",
            device_token=str(entry.get("virtualDeviceMid") or "").strip(),
            token=token,
            platform_token=platform,
            provider_id=provider_id,
            arms_uid=str(entry.get("virtualArmsUid") or "").strip(),
            fallback_token=fallback_token,
            fallback_base_url=fallback_base_url,
            raw=entry,
        ))

    return out, failed
