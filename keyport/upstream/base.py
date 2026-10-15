"""上游适配层公共类型：账号凭证、错误分类、稳定指纹派生。"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from enum import Enum


class ErrorKind(str, Enum):
    """上游错误分类，直接驱动池的冷却 / 熔断策略。"""

    AUTH = "auth"              # 401/403 鉴权失效 → 需刷新或摘除
    RATE_LIMIT = "rate_limit"  # 429 → 软冷却指数退避
    NOT_FOUND = "not_found"    # 404 → 固定短冷却
    PAYMENT = "payment"        # 402 / 余额耗尽 → 硬冷却至次日
    MODEL_LIMIT = "model_limit"  # 该模型用量超限 → 仅冷却该模型
    BAD_REQUEST = "bad_request"  # 400 请求本身不合法 → 不冷却，直接回客户端
    CAPTCHA = "captcha"        # 上游端点要求人机验证（3007）→ 非账号过错，不冷却不换号
    RISK = "risk"              # 风控 3012 / unusual activity → 指数退避，累计升级禁用
    WAF = "waf"                # 403 且无业务信封（WAF 拦截页）→ 指向出口 IP 而非账号
    SERVER = "server"          # 5xx → 计入连败
    NETWORK = "network"        # 连接/超时 → 计入连败
    UNKNOWN = "unknown"


# 上游业务码 → 语义（CodeBuddy / WorkBuddy 口径）
_BIZ_MODEL_LIMIT_CODES = {"6004", "11-6004"}
_BIZ_QUOTA_CODES = {"14051", "11-14051", "14052"}
_BIZ_PROMPT_TOO_LONG = {"11-128", "11-130"}


@dataclass
class UpstreamError(Exception):
    """上游请求失败。kind 决定池侧处置方式。"""

    kind: ErrorKind
    status: int = 0
    message: str = ""
    body: str = ""
    retry_after: float = 0.0
    # 原始响应头：重置时刻的第三级来源（`Retry-After` / `Retry-After-Ms` /
    # `X-Ratelimit-Reset`）。取不到时不臆造 —— 调用方回落本地有界退避。
    headers: dict[str, str] = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - 展示用
        return f"UpstreamError(kind={self.kind.value}, status={self.status}, msg={self.message})"

    def reset_at(self, now: float | None = None):
        """解析上游明示的重置时刻（三级来源，见 `upstream.resetat`）。

        返回 `ResetAt`；`usable` 为假时必须回落本地有界退避。
        """
        from .resetat import resolve_reset_at

        return resolve_reset_at(self.body, self.headers, now)

    @property
    def should_failover(self) -> bool:
        """是否应换号重试。

        请求本身非法（BAD_REQUEST）或上游要求人机验证（CAPTCHA）时都不换号：
        前者换号也没用，后者同域账号共用同一端点、换号同样被拦。
        """
        return self.kind not in (ErrorKind.BAD_REQUEST, ErrorKind.CAPTCHA)


def _is_captcha_challenge(text: str) -> bool:
    """识别上游人机验证挑战（zcode.z.ai Coding Plan 网关业务码 3007）。

    上游返回 400 + `{"code":3007,"msg":"captcha verify failed"}`。
    用「固定短语」或「captcha + 3007 同时出现」判定，避免 logid 里的
    数字巧合触发误判。
    """
    if not text:
        return False
    lowered = text.lower()
    if "captcha verify failed" in lowered:
        return True
    return "captcha" in lowered and "3007" in lowered


def is_risk_control(status: int, body: str = "") -> bool:
    """风控信号判定（3012「unusual activity」/ HTTP 405）。

    与验证码挑战互斥：调用方应先排除挑战形态。命中即账号级风控，
    需指数退避冷却（而非直接回传客户端错误 —— 那会让下次请求立刻重打、加剧风控）。
    """
    from . import constants

    if status in constants.RISK_CONTROL_HTTP_STATUSES:
        return True
    lowered = (body or "").lower()
    return any(m.lower() in lowered for m in constants.RISK_CONTROL_MARKERS)


def has_business_envelope(body: str) -> bool:
    """错误 body 是否携带上游业务信封形态（含 `"code":` 或 `"msg":` 字段）。

    只做**字段名命中**，不做 JSON 解析：畸形 JSON 但含 `"msg":` 字样仍按业务响应
    保守处理 —— **宁漏判 WAF 也不误罚业务 403**（后者各有权威分类）。
    """
    return '"code":' in body or '"msg":' in body


def is_waf_blocked(status: int, body: str = "") -> bool:
    """403 是否为 WAF 拦截形态：**无业务信封**（HTML 拦截页 / 空体 / 纯文本）。

    带业务信封的 403（`11140 request illegal` 等）仍走既有分类链，不受影响。
    这个区分的意义是：WAF 拦的是**网关出口 IP** 而不是账号，处置方向完全不同。
    """
    return status == 403 and not has_business_envelope(body or "")


def classify_status(status: int, body: str = "") -> ErrorKind:
    """按 HTTP 状态 + 响应体业务码分类错误。"""
    text = body or ""
    # 业务码优先：6004 是「该模型使用量超限」，语义不同于账号级 429
    for code in _BIZ_MODEL_LIMIT_CODES:
        if code in text:
            return ErrorKind.MODEL_LIMIT
    # 人机验证挑战同样是 400，但语义是上游拒绝服务而非请求非法
    if _is_captcha_challenge(text):
        return ErrorKind.CAPTCHA
    # 风控先于 405 的通用 4xx 判定：405 在本生态里承载 3012
    if is_risk_control(status, text):
        return ErrorKind.RISK
    if status == 429:
        return ErrorKind.RATE_LIMIT
    if status == 404:
        return ErrorKind.NOT_FOUND
    if status == 402:
        return ErrorKind.PAYMENT
    if status in (401, 403):
        # 402/额度耗尽的变体也可能伪装成 403，业务码兜底
        if any(code in text for code in _BIZ_QUOTA_CODES):
            return ErrorKind.PAYMENT
        # WAF 拦截页（无业务信封）指向出口 IP，不是账号鉴权失效 ——
        # 判成 AUTH 会把健康账号一个个禁用掉
        if status == 403 and is_waf_blocked(status, text):
            return ErrorKind.WAF
        return ErrorKind.AUTH
    if status == 400:
        return ErrorKind.BAD_REQUEST
    if 500 <= status < 600:
        return ErrorKind.SERVER
    if status == 0:
        return ErrorKind.NETWORK
    return ErrorKind.UNKNOWN


@dataclass
class AccountAuth:
    """归一化账号凭证（DB 行 → 出站请求所需的全部字段）。"""

    uid: str
    realm: str = "cn"
    access_token: str = ""
    refresh_token: str = ""
    expires_at: int = 0
    domain: str = ""
    enterprise_id: str = ""
    device_token: str = ""       # 上游设备标识（WorkBuddy 为 X-Device-Token，
                                 # ZCode 为 virtualDeviceMid → X-Device-Mid）
    nickname: str = ""
    kind: str = "oauth"          # oauth / apikey
    file_path: str = ""
    base_url: str = ""           # 上游基址覆盖（z.ai Coding Plan 等第三方端点）
    protocol: str = "openai"     # 上游协议：openai（chat/completions）/ anthropic（messages）
    # 回退通道凭证（ZCode 账号包里的平台 token → api.z.ai/api/anthropic）。
    # Plan 通道（JWT + 验证码）429/鉴权失效/风控时，同账号可无缝切到该通道：
    # 免验证码、不吃 Plan 额度，避免整号被判死。
    fallback_token: str = ""
    fallback_base_url: str = ""
    # 每账号设备档案（fingerprint.DeviceProfile 或其 dict 形态）。
    # 有档案时身份头按档案出值（一号一台设备）；无档案时按 uid 稳定派生，
    # 绝不用宿主机真实形态（那会形成多账号同设备关联信号）。
    fingerprint: object | None = None
    # 账号级出站代理（出口线路）：'' = 未指定（跟随全局默认），'direct' = 显式直连，
    # 其余为代理 URL。N 个账号共用同一出口 IP 会在上游聚成「一个 IP 挂 N 个号」的
    # 强关联信号，按账号分散出口是防封的主要手段。
    egress_proxy: str = ""

    def is_global(self) -> bool:
        return self.realm == "global"

    def is_zai(self) -> bool:
        return self.realm == "zai"

    def is_enterprise(self) -> bool:
        """企业版账号（无个人成长体系）。

        计算属性、不落盘：避免与业务字段漂移。企业版打成长端点上游直接 403
        （`growth system is only available for personal users`），执行任何成长
        任务都是零收益白跑 —— 最危险的是这类「静默无效」在日志里看不出异常。
        """
        return bool(self.enterprise_id)

    def is_anthropic(self) -> bool:
        return self.protocol == "anthropic"

    def has_fallback(self) -> bool:
        """是否有可用的回退通道凭证（同账号的第二把钥匙）。"""
        return bool(self.fallback_token and self.fallback_base_url)

    def as_fallback(self) -> "AccountAuth":
        """派生走回退通道的凭证副本（换令牌与基址，其余身份字段不变）。

        设备档案一并保留：上游按「账号 + 设备」算额度，换通道不该换设备。
        """
        import copy

        clone = copy.copy(self)
        clone.access_token = self.fallback_token
        clone.base_url = self.fallback_base_url
        return clone

    def token_expired(self, skew: int = 60) -> bool:
        """access_token 是否已过期（含提前 skew 秒的余量）。"""
        if not self.expires_at:
            return False
        return self.expires_at <= int(time.time()) + skew

    def token_remaining(self) -> int:
        if not self.expires_at:
            return 0
        return max(0, self.expires_at - int(time.time()))


# ---- 设备指纹隔离 ----


def derive_stable_id(uid: str, purpose: str) -> str:
    """按 uid + 用途稳定派生 36 hex 设备/会话标识。

    跨重启恒定（固定盐）、账号间互异、同 uid 同用途幂等。
    语义：同一账号长期固定在一台虚拟物理设备上，天然防多号关联风控。
    """
    digest = hashlib.sha256(f"kp:{purpose}:{uid}".encode("utf-8")).hexdigest()
    return digest[:36]


def derive_machine_id(uid: str) -> str:
    return derive_stable_id(uid, "machine")


def derive_session_id(uid: str) -> str:
    return derive_stable_id(uid, "session")


def new_hex_id(nbytes: int = 16) -> str:
    """生成 hex 随机 ID（对话/消息级）。"""
    import os

    return os.urandom(nbytes).hex()


@dataclass
class ChatMeta:
    """出站会话头族所需的元信息。"""

    conversation_id: str = ""
    conversation_request_id: str = ""
    trace_id: str = ""
    client_ip: str = ""
    extra_headers: dict[str, str] = field(default_factory=dict)
