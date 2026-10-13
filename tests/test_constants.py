"""上游常量收口断言。

目的：常量是网关与上游对接的契约，**任何无意的漂移都会变成线上故障**
（版本号落后 → preview 恒空；Referer 写错 → 3007/3012；判定关键字丢了 → 误杀账号）。
改动上游常量时必须同步更新这里的断言，让「改动」成为一次显式决策。

各值的实测出处见 `keyport/upstream/constants.py` 与 `docs/README.md`。
"""

from __future__ import annotations

from keyport.upstream import constants as C

# ---- origin 与端点 ----


def test_origins():
    assert C.ZCODE_ORIGIN == "https://zcode.z.ai"
    assert C.ZAI_API_ORIGIN == "https://api.z.ai"
    assert C.BIGMODEL_ORIGIN == "https://open.bigmodel.cn"


def test_coding_plan_endpoints_share_the_same_prefix():
    """messages 与 billing 必须同源于 /zcode-plan 前缀。

    两者若漂到不同前缀，账号 base_url 推导（_zcode_plan_billing_url）会取不到
    计费端点，额度与领取全部静默失效。
    """
    marker = C.ZCODE_PLAN_MARKER
    assert marker in C.ZCODE_PLAN_ANTHROPIC_BASE
    assert marker in C.ZCODE_BILLING_BASE
    assert C.ZCODE_PLAN_ANTHROPIC_BASE.endswith("/anthropic")
    assert C.ZCODE_BILLING_BASE.endswith(marker)


def test_fallback_anthropic_endpoints():
    assert C.ZAI_ANTHROPIC_BASE == "https://api.z.ai/api/anthropic"
    assert C.BIGMODEL_ANTHROPIC_BASE == "https://open.bigmodel.cn/api/anthropic"


def test_billing_paths():
    assert C.PATH_BILLING_CURRENT == "/billing/current"
    assert C.PATH_BILLING_BALANCE == "/billing/balance"
    assert C.PATH_BILLING_PREVIEW == "/billing/preview"
    assert C.PATH_BILLING_CLAIM == "/billing/claim"
    assert C.PATH_USAGE == "/usage"


def test_free_endpoints():
    """这两个端点免鉴权，且是「安装序」与「激活上报」的落点。"""
    assert C.CLIENT_CONFIGS_URL.endswith("/api/v1/client/configs")
    assert C.EVENT_REPORT_URL.endswith("/api/v1/event/report")


# ---- 版本号 ----


def test_client_version_is_pinned_and_wellformed():
    """上游按客户端版本门槛投放活动套餐：钉旧版本会让 billing/preview 恒空。

    发现 preview 为空时优先核对这里是否落后于官方桌面端。
    """
    assert C.CLIENT_APP_VERSION
    parts = C.CLIENT_APP_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)
    # 不低于引入 poll_token 新协议的版本区间（3.11.x 时代已知会恒空）
    assert tuple(int(p) for p in parts) >= (3, 12, 0)


def test_sdk_ua_segment_shape():
    assert C.CLIENT_SDK_UA_SEGMENT.startswith("ai-sdk/anthropic/")


def test_identity_defaults():
    assert C.CLIENT_PLATFORM == "darwin-arm64"
    assert C.ANTHROPIC_VERSION == "2023-06-01"
    assert C.X_ZCODE_AGENT == "glm"
    assert C.HTTP_REFERER == C.ZCODE_ORIGIN      # 无尾斜杠
    assert C.TITLE_DESKTOP == "Z Code@electron"
    assert C.TITLE_CLI == "Z Code@cli"


def test_captcha_defaults():
    assert C.CAPTCHA_DEFAULTS["sceneId"] == "11xygtvd"
    assert C.CAPTCHA_DEFAULTS["prefix"] == "no8xfe"
    assert C.CAPTCHA_DEFAULTS["region"] == "cn"


def test_captcha_headers_are_paired():
    """参数头与 region 头必须成对定义（缺失 region 易 3007）。"""
    assert C.CAPTCHA_HEADER == "X-Aliyun-Captcha-Verify-Param"
    assert C.CAPTCHA_REGION_HEADER == "X-Aliyun-Captcha-Verify-Region"


def test_activation_elements_order():
    """app_launch 必须先于 app_daily_active（官方客户端顺序）。"""
    assert C.ACTIVATION_ELEMENTS == ("app_launch", "app_daily_active")


# ---- 判定关键字 ----


def test_exhaust_signals():
    assert C.EXHAUST_HTTP_STATUSES == (402,)
    for kw in ("quota", "insufficient", "balance", "exhaust", "额度", "余额不足"):
        assert kw in C.EXHAUST_KEYWORDS


def test_captcha_body_markers_cover_spacing_variants():
    """上游 JSON 的冒号后可能有空格，两种形态都要认。"""
    assert '"code":3007' in C.CAPTCHA_BODY_MARKERS
    assert '"code": 3007' in C.CAPTCHA_BODY_MARKERS


def test_risk_control_signals():
    """3012 由 HTTP 405 承载；标记与状态码都要认。"""
    assert C.RISK_CONTROL_HTTP_STATUSES == (405,)
    assert '"code":3012' in C.RISK_CONTROL_MARKERS
    assert '"code": 3012' in C.RISK_CONTROL_MARKERS
    assert "unusual activity" in C.RISK_CONTROL_MARKERS


def test_permanent_429_markers():
    """1113「无余额/无资源包」是 Key 永久态，必须能被识别以免白等。"""
    assert "1113" in C.PERMANENT_429_MARKERS
    assert "insufficient balance" in C.PERMANENT_429_MARKERS


# ---- 模型与限额 ----


def test_model_name_map_is_lowercase_keys():
    """客户端传小写别名 → 映射到上游大小写敏感的官方名。"""
    assert all(k == k.lower() for k in C.MODEL_NAME_MAP)
    assert C.MODEL_NAME_MAP["glm-5.3-flash"] == "GLM-5.3-Flash"
    assert C.MODEL_NAME_MAP["glm-turbo"] == "GLM-5-Turbo"


def test_max_tokens_limit():
    """上游对超限报 400 code 1210「限制数值范围[1,131072]」。"""
    assert C.MAX_TOKENS_LIMIT == 131072


# ---- 重试与退避 ----


def test_retry_budgets_are_positive():
    assert C.RETRY_429_TIMES > 0
    assert C.RETRY_429_WAIT > 0
    assert C.RETRY_5XX_TIMES > 0
    assert C.MAX_CAPTCHA_RETRIES > 0
    assert C.MAX_ACCOUNT_ATTEMPTS > 0


def test_retry_after_cap_protects_client():
    """采信上限必须存在且不小于基础等待，否则上游给个巨大值会吊死客户端。"""
    assert 0 < C.RETRY_429_WAIT_MAX
    assert C.RETRY_429_WAIT_MAX >= C.RETRY_429_WAIT


def test_risk_backoff_range_is_sane():
    assert 0 < C.RISK_COOLDOWN_BASE <= C.RISK_COOLDOWN_MAX
    assert C.RISK_BAN_STRIKES >= 1
    assert C.RISK_STRIKE_DECAY_SECONDS > C.RISK_COOLDOWN_MAX


def test_cooling_seconds_positive():
    assert C.COOLING_SECONDS > 0


# ---- 领取业务码 ----


def test_claim_code_messages_cover_documented_codes():
    """官方口径的业务码必须都有面向用户的文案。"""
    for code in (1001, 1002, 1003, 1004, 1005, 3001, 3007, 401):
        assert code in C.CLAIM_CODE_MESSAGES, f"缺少 code {code} 的文案"
        assert C.CLAIM_CODE_MESSAGES[code]


def test_claim_quota_exhausted_code_is_1005():
    """1005 是唯一带「名额恢复时间」的码，轮次退避依赖它。"""
    assert C.CLAIM_QUOTA_EXHAUSTED_CODE == 1005


def test_claim_round_interval_positive():
    assert C.CLAIM_ROUND_INTERVAL > 0
