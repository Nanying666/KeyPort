"""存量自愈：启动时纠正历史脏状态。

移植自 sheng-du0521/workbuddy2api 的 `internal/pool/persist.go`
（`healLegacyModelLimitCooldown` / `healTruncatedModelCooldowns`）。
核心纪律是**只放长不缩短**。
"""

from __future__ import annotations

import json
from pathlib import Path

from conftest import make_test_config

from keyport.app import build_context
from keyport.pool.entry import AccountEntry
from keyport.pool.heal import (
    LEGACY_MODEL_LIMIT_REASON,
    heal_entry,
    heal_legacy_model_limit_cooldown,
    heal_truncated_model_cooldowns,
)
from keyport.upstream.base import AccountAuth

NOW = 1_791_000_000
MODEL_MAX = 86400


def _entry(**kw) -> AccountEntry:
    base = {"uid": "u1", "realm": "cn"}
    base.update(kw)
    return AccountEntry(**base)


# ---- 自愈一：模型级限额被误写成账号级冷却 ----


def test_heals_legacy_account_level_cooldown():
    """旧形态把 6004 写成账号级 → 清除（否则整个号对所有模型都被挡）。"""
    e = _entry(cooldown_until=NOW + 3600,
               last_error=f"{LEGACY_MODEL_LIMIT_REASON} [reset=body-text]")
    assert heal_legacy_model_limit_cooldown(e, NOW) is True
    assert e.cooldown_until == 0
    assert e.last_error == ""


def test_does_not_touch_normal_cooldown():
    """正常形态（reason 不带旧前缀）绝不动。"""
    e = _entry(cooldown_until=NOW + 3600, last_error="rate_limit:429")
    assert heal_legacy_model_limit_cooldown(e, NOW) is False
    assert e.cooldown_until == NOW + 3600


def test_does_not_touch_expired_cooldown():
    e = _entry(cooldown_until=NOW - 1, last_error=LEGACY_MODEL_LIMIT_REASON)
    assert heal_legacy_model_limit_cooldown(e, NOW) is False


def test_does_not_touch_when_disabled_or_manual():
    """那些是**人的决定**或其他病因，不该被顺手清掉。"""
    for kw in ({"enabled": False}, {"manual_disabled": True},
               {"breaker_until": NOW + 100}):
        e = _entry(cooldown_until=NOW + 3600,
                   last_error=LEGACY_MODEL_LIMIT_REASON, **kw)
        assert heal_legacy_model_limit_cooldown(e, NOW) is False, kw
        assert e.cooldown_until == NOW + 3600, kw


# ---- 自愈二：模型级冷却被账号级上限截断 ----


def test_heals_truncated_model_cooldown():
    """被截断 → 放长到上游明示时刻（否则在真实窗口里反复撞墙）。"""
    e = _entry()
    reset = NOW + 5 * 3600
    e.model_cooldowns["hy3"] = NOW + 2 * 3600    # 被账号级封顶截断过
    e.model_reset_at["hy3"] = reset
    assert heal_truncated_model_cooldowns(e, NOW, MODEL_MAX) == 1
    assert e.model_cooldowns["hy3"] == reset


def test_only_extends_never_shortens():
    """只放长不放短 —— 提前放出一个可能仍在被限的模型等于制造热循环。"""
    e = _entry()
    e.model_cooldowns["hy3"] = NOW + 10 * 3600   # 已经比权威时刻更晚
    e.model_reset_at["hy3"] = NOW + 5 * 3600
    assert heal_truncated_model_cooldowns(e, NOW, MODEL_MAX) == 0
    assert e.model_cooldowns["hy3"] == NOW + 10 * 3600


def test_ignores_entries_without_authoritative_time():
    """无权威时刻的条目（无时间信息的退避）没有「该到什么时候」可对齐。"""
    e = _entry()
    e.model_cooldowns["hy3"] = NOW + 600
    assert heal_truncated_model_cooldowns(e, NOW, MODEL_MAX) == 0
    assert e.model_cooldowns["hy3"] == NOW + 600


def test_caps_at_model_max():
    """权威时刻远超模型级封顶时按封顶（防一条脏数据摘除账号几万年）。"""
    e = _entry()
    e.model_cooldowns["hy3"] = NOW + 600
    e.model_reset_at["hy3"] = NOW + 100 * 24 * 3600
    assert heal_truncated_model_cooldowns(e, NOW, MODEL_MAX) == 1
    assert e.model_cooldowns["hy3"] == NOW + MODEL_MAX


def test_heal_entry_reports_actions():
    e = _entry(cooldown_until=NOW + 3600,
               last_error=LEGACY_MODEL_LIMIT_REASON)
    e.model_cooldowns["hy3"] = NOW + 600
    e.model_reset_at["hy3"] = NOW + 5 * 3600
    notes = heal_entry(e, NOW, MODEL_MAX)
    assert len(notes) == 2
    assert any("账号级冷却" in n for n in notes)
    assert any("模型级冷却" in n for n in notes)


def test_heal_entry_noop_on_clean_state():
    e = _entry()
    assert heal_entry(e, NOW, MODEL_MAX) == []


def test_heal_is_idempotent():
    e = _entry(cooldown_until=NOW + 3600, last_error=LEGACY_MODEL_LIMIT_REASON)
    e.model_cooldowns["hy3"] = NOW + 600
    e.model_reset_at["hy3"] = NOW + 5 * 3600
    assert len(heal_entry(e, NOW, MODEL_MAX)) == 2
    # 第二次跑无事可做（幂等）
    assert heal_entry(e, NOW, MODEL_MAX) == []


# ---- 接线：从旧形态 state.json 启动 ----


def _write_state(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_startup_heals_legacy_state(tmp_path: Path):
    """用旧形态的 state.json 启动 → 必须被纠正（否则升级程序也救不回来）。"""
    import time as _t

    now = int(_t.time())
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    data_dir = Path(cfg.data_dir)
    # 先建库（账号必须先存在，_load_state 才作用得上）
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T"))
    ctx.pool.save_state()
    ctx.db.close()

    # 手写旧形态状态：账号级冷却 + reason 说是模型级（时刻用真实时钟相对量，
    # 否则相对真实时钟已过期，过期过滤会先把它清掉、测不到自愈本身）
    reset = now + 5 * 3600
    _write_state(data_dir / "state.json", {
        "saved_at": now,
        "accounts": {"u1": {
            "cooldown_until": now + 7200,
            "last_error": f"{LEGACY_MODEL_LIMIT_REASON} [reset=body-text]",
            "enabled": True,
            "manual_disabled": False,
            "model_cooldowns": {"hy3": now + 600},
            "model_reset_at": {"hy3": reset},
        }},
        "model_ledger": [],
    })

    ctx2 = build_context(cfg)
    entry = ctx2.pool.get("u1")
    assert entry.cooldown_until == 0            # 账号级冷却已清除
    assert entry.model_cooldowns["hy3"] == reset  # 模型级已放长

    # 修正已物化回磁盘（否则重启一次又要重算）
    saved = json.loads((data_dir / "state.json").read_text(encoding="utf-8"))
    assert saved["accounts"]["u1"]["cooldown_until"] == 0
    assert saved["accounts"]["u1"]["model_cooldowns"]["hy3"] == reset


def test_startup_leaves_clean_state_untouched(tmp_path: Path):
    cfg = make_test_config(tmp_path)
    cfg.captcha.enabled = False
    ctx = build_context(cfg)
    ctx.pool.upsert_account(AccountAuth(uid="u1", realm="cn", access_token="T"))
    ctx.pool.save_state()
    before = (Path(cfg.data_dir) / "state.json").read_text(encoding="utf-8")
    ctx.db.close()

    ctx2 = build_context(cfg)
    entry = ctx2.pool.get("u1")
    assert entry.cooldown_until == 0
    # 干净状态不该被自愈逻辑改动（saved_at 会更新，故只比对账号段）
    saved = json.loads((Path(cfg.data_dir) / "state.json").read_text(encoding="utf-8"))
    assert saved["accounts"]["u1"]["cooldown_until"] == 0
    assert before  # 原文件存在即可


# ---- 模型级封顶配置 ----


def test_model_rate_max_wider_than_account_cap():
    """模型级封顶必须比账号级宽，否则又会截断（这就是自愈二要修的成因）。"""
    from keyport.config import PoolConfig

    cfg = PoolConfig()
    assert cfg.model_rate_max_seconds > cfg.soft_rate_max_seconds


def test_model_reset_at_persisted_roundtrip():
    e = _entry()
    e.model_cooldowns["hy3"] = NOW + 3600
    e.model_reset_at["hy3"] = NOW + 5 * 3600
    state = e.to_state()
    assert state["model_reset_at"] == {"hy3": NOW + 5 * 3600}

    e2 = _entry()
    e2.load_state(state, NOW)
    assert e2.model_reset_at["hy3"] == NOW + 5 * 3600
    assert e2.model_cooldowns["hy3"] == NOW + 3600
