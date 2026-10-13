"""上游常量收口 —— 与 ZCode(z.ai) / CodeBuddy / WorkBuddy 上游相关的 URL、版本号、
判定关键字、模型名映射集中在此。

约定：模块代码不再硬编码上游 URL / 版本号 / 关键字，一律 import 本模块。
改动任何常量前先补 tests/test_constants.py 的断言，防止无意漂移。

出处标注：值来自对上游接口与官方客户端行为的观测；带日期的条目为实测记录。
"""

from __future__ import annotations

# ── 上游 origin ──────────────────────────────────────────────────────────────

# ZCode Coding Plan 网关（JWT 通道，需阿里云无痕验证参数）
ZCODE_ORIGIN = "https://zcode.z.ai"
# Z.ai 开放平台（API Key 通道：anthropic 兼容 / OpenAI 兼容 / 计费）
ZAI_API_ORIGIN = "https://api.z.ai"
# 智谱开放平台（anthropic 兼容端点）
BIGMODEL_ORIGIN = "https://open.bigmodel.cn"

# ── Coding Plan 端点 ─────────────────────────────────────────────────────────

# 账号 base_url 里出现该标记即判定为 Coding Plan 网关（要验证码）
ZCODE_PLAN_MARKER = "/zcode-plan"
# 官方客户端 z.ai - Coding Plan provider 的 baseURL
ZCODE_PLAN_ANTHROPIC_BASE = f"{ZCODE_ORIGIN}/api/v1/zcode-plan/anthropic"
# api.z.ai 的 anthropic 兼容端点（回退通道 / bigmodel 同形）
ZAI_ANTHROPIC_BASE = f"{ZAI_API_ORIGIN}/api/anthropic"
BIGMODEL_ANTHROPIC_BASE = f"{BIGMODEL_ORIGIN}/api/anthropic"

# Coding Plan 计费端点（套餐权益 / 额度来源）
ZCODE_BILLING_BASE = f"{ZCODE_ORIGIN}/api/v1/zcode-plan"
PATH_BILLING_CURRENT = "/billing/current"
PATH_BILLING_BALANCE = "/billing/balance"
PATH_BILLING_PREVIEW = "/billing/preview"
PATH_BILLING_CLAIM = "/billing/claim"
PATH_USAGE = "/usage"

# 官方客户端运行配置（免鉴权；验证码 scene/region/prefix、功能开关、活动套餐投放）
CLIENT_CONFIGS_URL = f"{ZCODE_ORIGIN}/api/v1/client/configs"
# 激活遥测上报（无 Authorization；日活键由上游按 device_mid + 日期去重）
EVENT_REPORT_URL = f"{ZCODE_ORIGIN}/api/v1/event/report"

# ── 客户端版本（单一真相源）──────────────────────────────────────────────────
# 上游疑似按客户端版本门槛投放活动套餐：钉旧版本会导致 billing/preview 恒空，
# 发现 preview 为空时优先核对该常量是否落后于官方桌面端。
CLIENT_APP_VERSION = "3.14.4"
# messages 端点 UA 的 SDK 段（官方 CLI 真实形态）
CLIENT_SDK_UA_SEGMENT = "ai-sdk/anthropic/3.0.81"
# 服务端固定伪装的平台（官方 asar TH() = process.platform-arch）
CLIENT_PLATFORM = "darwin-arm64"

# ── 身份 / 归属头 ────────────────────────────────────────────────────────────

ANTHROPIC_VERSION = "2023-06-01"
# 无尾斜杠：与 billing 头统一，消除 messages/billing 通道间 Referer 形状抖动
HTTP_REFERER = ZCODE_ORIGIN
X_ZCODE_AGENT = "glm"
# X-Title 形态 "Z Code@{sourceTitle}"：桌面端 electron / CLI 走 cli
TITLE_DESKTOP = "Z Code@electron"
TITLE_CLI = "Z Code@cli"
RELEASE_CHANNEL_DESKTOP = "stable"
RELEASE_CHANNEL_CLI = "production"
# 无账号上下文时的全局兜底身份（诊断 / 进程级安装序使用）
IDENTITY_CLIENT_LANGUAGE = "zh-CN"
IDENTITY_CLIENT_TIMEZONE = "Asia/Shanghai"
IDENTITY_OS_VERSION = "25.5.0"
IDENTITY_SCREEN_RESOLUTION = "2560x1440"

# 验证码头（与 verifyParam 成对下发，缺失易 3007）
CAPTCHA_HEADER = "X-Aliyun-Captcha-Verify-Param"
CAPTCHA_REGION_HEADER = "X-Aliyun-Captcha-Verify-Region"
CAPTCHA_DEFAULTS = {"enabled": True, "prefix": "no8xfe", "region": "cn", "sceneId": "11xygtvd"}

# 激活事件元素名（官方客户端首启与日活各上报一次）
ACTIVATION_ELEMENTS = ("app_launch", "app_daily_active")

# ── 上游被拒信号 → 账号动作 ──────────────────────────────────────────────────

# 额度耗尽：HTTP 402，或 body 命中下列关键字
EXHAUST_HTTP_STATUSES = (402,)
EXHAUST_KEYWORDS = ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足")

# 人机验证挑战：HTTP 400 + body {"code":3007}，或 403 + 文案
CAPTCHA_BODY_MARKERS = ('"code":3007', '"code": 3007')
CAPTCHA_TEXT_MARKERS = ("captcha verify failed", "verify token", "verify failed")

# 风控（3012 "unusual activity"，由 HTTP 405 承载）。高频请求触发，
# 上游定性为临时限制。处置：账号指数退避冷却 + 暂停验证码池预热。
RISK_CONTROL_HTTP_STATUSES = (405,)
RISK_CONTROL_MARKERS = ('"code":3012', '"code": 3012', "unusual activity")

# 429 承载「Key 无资源包」的永久态（原地等待重试无意义，快速换号）
PERMANENT_429_MARKERS = ("1113", "insufficient balance")

# ── 模型 ─────────────────────────────────────────────────────────────────────

# Z.AI 上游模型名大小写敏感；客户端传小写别名时映射到官方名
MODEL_NAME_MAP = {
    "glm-5.3-flash": "GLM-5.3-Flash",
    "glm-5.3": "GLM-5.3",
    "glm-5.2": "GLM-5.2",
    "glm-5-turbo": "GLM-5-Turbo",
    "glm-turbo": "GLM-5-Turbo",
    "glm-5.1": "GLM-5.1",
    "glm-4.7": "GLM-4.7",
    "glm-4.6": "GLM-4.6",
}

# 上游 max_tokens 合法范围（超限报 400 code 1210），出站统一钳制
MAX_TOKENS_LIMIT = 131072

# ── 重试 / 冷却参数（可用环境变量覆盖，见 config.py）─────────────────────────

# 429 频控：账号**不冷却**，按上游 Retry-After 原地等待重试，耗尽后换号
RETRY_429_TIMES = 5
RETRY_429_WAIT = 60
RETRY_429_WAIT_MAX = 120        # Retry-After 采信上限，防把客户端吊死
# 5xx 等一般错误：重试，耗尽后账号冷却并换号
RETRY_5XX_TIMES = 3
RETRY_5XX_WAIT = 5
COOLING_SECONDS = 300
# 风控指数退避：base 起翻倍，封顶 cap；窗口内累计 ban_strikes 次升级禁用
RISK_COOLDOWN_BASE = 900
RISK_COOLDOWN_MAX = 86400
RISK_BAN_STRIKES = 4
RISK_STRIKE_DECAY_SECONDS = 7 * 86400

# 单请求内换号上限（与池选号配合）
MAX_ACCOUNT_ATTEMPTS = 5
# 验证码挑战同账号原地重试上限
MAX_CAPTCHA_RETRIES = 3

# ── 套餐领取业务码语义（沿用官方客户端口径）─────────────────────────────────

CLAIM_CODE_MESSAGES = {
    1001: "套餐不存在",
    1002: "活动已结束或套餐暂不可领取",
    1003: "该套餐已经领取过",
    1004: "不符合领取条件",
    1005: "今日领取名额已用完",
    3001: "领取参数错误，请刷新后重试",
    3007: "验证码校验失败，请重试",
    401: "请先登录后再领取",
}
# 1005 名额用完 → 按上游 data.plan.ends_at 退避（等待期不重试领取、不耗验证码）
CLAIM_QUOTA_EXHAUSTED_CODE = 1005
# 领取轮默认间隔（秒，0 = 关闭；运行期可在管理台热改）
CLAIM_ROUND_INTERVAL = 600
