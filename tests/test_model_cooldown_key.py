"""两个由生产部署暴露的缺陷的回归测试。

两个都是**既存**缺陷（`1f5587b` 就存在），但在真机上跑了真实流量才显现：
服务器上 9 个账号的 `consecutive_failures` 在 3 分钟内涨到 400+，而同期
`request_logs` 里**零失败** —— 因为 6004 被换号重试掩盖了。
"""

from __future__ import annotations

import json

from keyport.config import PoolConfig
from keyport.pool.entry import AccountEntry
from keyport.pool.transitions import apply_error
from keyport.upstream.base import ErrorKind, UpstreamError

NOW = 1_792_065_600


def _entry(**kw) -> AccountEntry:
    base = {"uid": "u1", "realm": "cn"}
    base.update(kw)
    return AccountEntry(**base)


def _model_limit(message: str = "usage exceeds frequency limit") -> UpstreamError:
    return UpstreamError(ErrorKind.MODEL_LIMIT, 429, message,
                         json.dumps({"code": 6004}))


# ---- 缺陷一：模型级冷却的键是错误文案而非模型名 ----


def test_cooldown_key_is_the_model_name_not_the_message():
    """键必须是**模型名**。

    早先 `model = err.message or ""` 把上游的**错误文案**当键，而
    `healthy_for_model` 查的是真实模型名 —— 两边永远对不上，
    冷却条目写了但**永远不被命中**（等于完全不冷却）。
    """
    entry = _entry()
    apply_error(entry, _model_limit(), PoolConfig(), NOW, model="deepseek-v4.1-flash")

    assert "deepseek-v4.1-flash" in entry.model_cooldowns
    # 文案绝不能出现在键里
    for key in entry.model_cooldowns:
        assert "usage exceeds" not in key
        assert "reset at" not in key


def test_model_cooldown_actually_takes_effect():
    """端到端：冷却写入后 `healthy_for_model` 必须真的挡住该模型。

    这是上一个缺陷的**可观测后果** —— 键对不上时这个断言会失败。
    """
    entry = _entry()
    apply_error(entry, _model_limit(), PoolConfig(), NOW, model="hy3")

    assert entry.healthy_for_model(NOW, "hy3") is False
    # 切模型立即可用（6004 的语义）
    assert entry.healthy_for_model(NOW, "hy4-preview") is True


def test_repeated_model_limit_reuses_one_key():
    """真实流量里 6004 是**每请求都撞**的高频事件。

    键若每次都不同（旧行为：文案里带时间戳 → 每次都是一条新键），
    `model_cooldowns` 会无限膨胀且一条都不生效。
    """
    entry = _entry()
    for i in range(50):
        # 每次文案都不同（含不同的重置时刻），模拟真实上游
        msg = f"usage exceeds frequency limit, reset at 2026-10-16 0{i % 10}:00:00"
        apply_error(entry, _model_limit(msg), PoolConfig(), NOW + i, model="hy3")

    assert list(entry.model_cooldowns) == ["hy3"]


def test_missing_model_falls_back_to_account_cooldown():
    """取不到模型名 → 账号级冷却，而不是写一个永远命不中的键。"""
    entry = _entry()
    detail = apply_error(entry, _model_limit(), PoolConfig(), NOW)

    assert entry.model_cooldowns == {}
    assert entry.cooldown_until > NOW
    assert detail.startswith("cooldown:")


# ---- 缺陷二：模型级限额被计入账号连败 ----


def test_model_limit_does_not_accumulate_consecutive_failures():
    """6004 是**模型级**限额、账号本身健康，不该计入账号连败。

    与 CAPTCHA 同类处置（都抵消开头的自增）。计进去会污染账号级判定：
    连败是熔断与降权的输入（阈值只有 3 / 5），而 6004 每请求都撞，
    累积几百次后下一个 5xx 会立刻把健康号熔断。
    """
    cfg = PoolConfig()
    entry = _entry()
    for i in range(200):
        apply_error(entry, _model_limit(), cfg, NOW + i, model="hy3")

    assert entry.consecutive_failures == 0
    assert entry.failed_calls == 0
    # 不该触发熔断
    assert entry.breaker_until == 0


def test_model_limit_then_server_error_does_not_instantly_break():
    """生产实测形态：6004 累积几百次后，一次 5xx 就会熔断健康号。"""
    cfg = PoolConfig()
    entry = _entry()
    for i in range(300):
        apply_error(entry, _model_limit(), cfg, NOW + i, model="hy3")
    assert entry.consecutive_failures == 0

    # 一次 5xx：应只计 1 次连败（阈值 3），不熔断
    apply_error(entry, UpstreamError(ErrorKind.SERVER, 500, "boom", "{}"), cfg, NOW + 400)
    assert entry.consecutive_failures == 1
    assert entry.breaker_until == 0


def test_real_account_failures_still_count():
    """真正的账号级失败仍要计入（别把修复做成了「一律不计数」）。"""
    cfg = PoolConfig()
    entry = _entry()
    for i in range(3):
        apply_error(entry, UpstreamError(ErrorKind.SERVER, 500, "boom", "{}"),
                    cfg, NOW + i)
    assert entry.consecutive_failures == 3
    assert entry.breaker_until > NOW


def test_rate_limit_still_counts():
    """429 走 record_rate_limited 不经此处，但直接调 apply_error 时仍应计数。"""
    entry = _entry()
    apply_error(entry, UpstreamError(ErrorKind.RATE_LIMIT, 429, "slow", "{}"),
                PoolConfig(), NOW)
    assert entry.consecutive_failures == 1


# ---- 两个缺陷的联合复现 ----


def test_production_scenario_no_longer_inflates():
    """复现生产形态：高频 6004 + 冷却必须真的生效。

    修复前：键是文案 → 冷却不命中 → 每请求都重放 6004 → 计数器涨到几百。
    修复后：键是模型名 → 冷却命中 → 该模型被挡，计数器不动。
    """
    cfg = PoolConfig()
    entry = _entry()

    # 第一次 6004 建立冷却
    apply_error(entry, _model_limit(), cfg, NOW, model="deepseek-v4.1-flash")
    assert entry.healthy_for_model(NOW, "deepseek-v4.1-flash") is False

    # 后续请求不会再打这个模型（选号会跳过它），故不会再有 6004
    assert entry.consecutive_failures == 0
    assert entry.failed_calls == 0
    assert len(entry.model_cooldowns) == 1


def test_expired_model_cooldown_allows_reuse():
    entry = _entry()
    apply_error(entry, _model_limit(), PoolConfig(), NOW, model="hy3")
    assert entry.healthy_for_model(NOW, "hy3") is False
    assert entry.healthy_for_model(NOW + 86400 * 2, "hy3") is True
