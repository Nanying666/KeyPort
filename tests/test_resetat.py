"""上游「重置时间」三级解析与冷却对齐。

移植自 sheng-du0521/workbuddy2api 的 `internal/upstream/resetat.go`
（`ResolveResetAt` / `parseResetAtJSON` / `parseResetWallClock` / `numericResetAt`）
与 `client.go` 的 `ParseRateReset` / `ParseRetryAfter`。
"""

from __future__ import annotations

import datetime as _dt
import json

import pytest

from keyport.upstream.resetat import (
    BODY_JSON,
    BODY_TEXT,
    EXPIRED,
    HEADER,
    HEADER_SANITY_SECONDS,
    NONE,
    UPSTREAM_TZ,
    parse_rate_reset,
    parse_reset_at_json,
    parse_retry_after_headers,
    parse_wall_clock,
    resolve_reset_at,
)

# 固定的"现在"，让断言不依赖真实时钟
NOW = 1_791_000_000.0  # 2026-10-06 前后


def _epoch(text: str) -> int:
    """按上游 UTC+8 口径把墙钟串转 epoch，供断言构造期望值。"""
    return int(
        _dt.datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=UPSTREAM_TZ)
        .timestamp()
    )


# ---- body 文案（第一级，最权威） ----


def test_parse_rate_reset_cn():
    body = '{"code":6004,"msg":"usage exceeds frequency limit, your usage will reset at 2026-10-15 21:33:13 UTC+8"}'
    assert parse_rate_reset(body) == _epoch("2026-10-15 21:33:13")


def test_parse_rate_reset_cn_human_text():
    body = '{"code":6004,"msg":"该模型用量超限，将在 2026-10-15 21:33:13 重置"}'
    assert parse_rate_reset(body) == _epoch("2026-10-15 21:33:13")


def test_parse_rate_reset_en_without_suffix():
    body = "rate limited; reset at 2026-10-15 21:33:13"
    assert parse_rate_reset(body) == _epoch("2026-10-15 21:33:13")


def test_parse_rate_reset_absent():
    assert parse_rate_reset('{"code":429,"msg":"too many requests"}') == 0
    assert parse_rate_reset("") == 0
    assert parse_rate_reset(None) == 0


def test_parse_rate_reset_accepts_bytes():
    body = b'{"msg":"\xe5\xb0\x86\xe5\x9c\xa8 2026-10-15 21:33:13 \xe9\x87\x8d\xe7\xbd\xae"}'
    assert parse_rate_reset(body) == _epoch("2026-10-15 21:33:13")


# ---- 墙钟时区（最容易错的地方） ----


def test_wall_clock_without_suffix_is_utc8():
    """把「无后缀墙钟」当 UTC 解析会整体偏 8 小时。

    偏早 8 小时 → 冷却几乎立刻到期（窗口内反复撞墙）；
    偏晚 8 小时 → 白扣一个号半天。两种错都比"没有时间信息"更糟。
    """
    got = parse_wall_clock("2026-10-15 21:33:13")
    assert got == _epoch("2026-10-15 21:33:13")
    # 与 UTC 解释必须相差正好 8 小时
    as_utc = int(
        _dt.datetime.strptime("2026-10-15 21:33:13", "%Y-%m-%d %H:%M:%S")
        .replace(tzinfo=_dt.UTC)
        .timestamp()
    )
    assert as_utc - got == 8 * 3600


def test_wall_clock_strips_utc8_suffix():
    assert parse_wall_clock("2026-10-15 21:33:13 UTC+8") == _epoch("2026-10-15 21:33:13")


def test_wall_clock_rfc3339_respects_own_offset():
    """RFC3339 自带偏移时必须尊重它，不能按 UTC+8 二次解释。"""
    got = parse_wall_clock("2026-10-15T21:33:13+08:00")
    assert got == _epoch("2026-10-15 21:33:13")
    # Z 形态是 UTC 时刻
    assert parse_wall_clock("2026-10-15T13:33:13Z") == _epoch("2026-10-15 21:33:13")


def test_wall_clock_iso_without_offset_is_utc8():
    assert parse_wall_clock("2026-10-15T21:33:13") == _epoch("2026-10-15 21:33:13")


def test_wall_clock_invalid():
    assert parse_wall_clock("not a time") == 0
    assert parse_wall_clock("") == 0


# ---- body 结构化字段（第二级） ----


def test_json_resets_at_field():
    body = json.dumps({"code": 6004, "data": {"resets_at": "2026-10-15 21:33:13"}})
    assert parse_reset_at_json(body, NOW) == _epoch("2026-10-15 21:33:13")


def test_json_camel_variants():
    for key in ("resetsAt", "resetAt", "resetTime", "reset_at", "reset_time"):
        body = json.dumps({"data": {key: "2026-10-15 21:33:13"}})
        assert parse_reset_at_json(body, NOW) == _epoch("2026-10-15 21:33:13"), key


def test_json_relative_seconds():
    """reset_in 族语义是「多久以后」→ now + n。"""
    body = json.dumps({"data": {"resets_in": 3600}})
    assert parse_reset_at_json(body, NOW) == int(NOW) + 3600


def test_json_relative_string_number():
    body = json.dumps({"data": {"retry_after": "600"}})
    assert parse_reset_at_json(body, NOW) == int(NOW) + 600


def test_json_relative_rejects_wall_clock():
    """键名说的是"多久以后"，给个墙钟属于上游自相矛盾 → 宁缺毋滥地丢弃。"""
    body = json.dumps({"data": {"resets_in": "2026-10-15 21:33:13"}})
    assert parse_reset_at_json(body, NOW) == 0


def test_json_epoch_seconds_and_millis():
    """数字形态按**量级**分界，不是位数。"""
    sec = _epoch("2026-10-15 21:33:13")
    assert parse_reset_at_json(json.dumps({"data": {"resets_at": sec}}), NOW) == sec
    assert parse_reset_at_json(
        json.dumps({"data": {"resets_at": sec * 1000}}), NOW) == sec
    # 长整型字符串（上游常这么序列化以免 JS 浮点截断）
    assert parse_reset_at_json(
        json.dumps({"data": {"resets_at": str(sec)}}), NOW) == sec


def test_json_small_number_is_relative():
    """< 1e8 视为相对秒数 —— 避开"把 3600 秒当成 1970 年"的荒谬。"""
    assert parse_reset_at_json(json.dumps({"data": {"resets_at": 3600}}), NOW) == int(NOW) + 3600


def test_json_scans_nested_envelopes():
    """信封是 {code,msg,data} / {error:{code,msg,data}} 两种固定形态。"""
    body = json.dumps({"error": {"code": 6004, "data": {"resets_at": "2026-10-15 21:33:13"}}})
    assert parse_reset_at_json(body, NOW) == _epoch("2026-10-15 21:33:13")


def test_json_ignores_unrelated_expiry_keys():
    """刻意不收 expires_at 一类：那些词在包月周期/token 有效期上大量出现，
    收进来会把「套餐到期」误读成「限流恢复」。"""
    body = json.dumps({"data": {"expires_at": "2026-10-15 21:33:13",
                                "expire_time": "2026-10-15 21:33:13"}})
    assert parse_reset_at_json(body, NOW) == 0


def test_json_invalid_body():
    assert parse_reset_at_json("{", NOW) == 0
    assert parse_reset_at_json("[1,2,3]", NOW) == 0
    assert parse_reset_at_json("", NOW) == 0
    assert parse_reset_at_json(None, NOW) == 0


# ---- 响应头（第三级，兜底） ----


def test_header_retry_after_seconds():
    assert parse_retry_after_headers({"retry-after": "60"}) == 60.0


def test_header_retry_after_ms():
    assert parse_retry_after_headers({"retry-after-ms": "1500"}) == 1.5


def test_header_x_ratelimit_reset_is_absolute():
    """绝对时刻型头：折算成"还剩多久"（必须与调用方共用同一个"现在"）。"""
    future = int(NOW) + 300
    got = parse_retry_after_headers({"x-ratelimit-reset": str(future)}, now=NOW)
    assert got == pytest.approx(300.0, abs=1.0)
    # 毫秒口径同样按量级识别
    got = parse_retry_after_headers(
        {"x-ratelimit-reset": str(future * 1000)}, now=NOW)
    assert got == pytest.approx(300.0, abs=1.0)


def test_header_only_plain_digits_accepted():
    """非纯数字（如 HTTP-Date）不解析，宁缺毋滥。"""
    assert parse_retry_after_headers({"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}) == 0.0


def test_header_rejects_nonpositive_and_absurd():
    assert parse_retry_after_headers({"retry-after": "0"}) == 0.0
    assert parse_retry_after_headers({"retry-after": "-5"}) == 0.0
    # 超上限（7 天）视为异常值丢弃
    assert parse_retry_after_headers({"retry-after": str(HEADER_SANITY_SECONDS + 1)}) == 0.0


def test_header_falls_through_candidates():
    """第一个头非法时尝试下一个。"""
    got = parse_retry_after_headers({"retry-after": "abc", "retry-after-ms": "2000"})
    assert got == 2.0


def test_header_empty():
    assert parse_retry_after_headers({}) == 0.0
    assert parse_retry_after_headers(None) == 0.0


# ---- 三级优先级 ----


def test_body_text_beats_json_and_header():
    body = json.dumps({"msg": "将在 2026-10-15 21:33:13 重置",
                       "data": {"resets_at": "2026-10-16 01:00:00"}})
    got = resolve_reset_at(body, {"retry-after": "30"}, NOW)
    assert got.source == BODY_TEXT
    assert got.epoch == _epoch("2026-10-15 21:33:13")


def test_json_beats_header():
    body = json.dumps({"data": {"resets_at": "2026-10-16 01:00:00"}})
    got = resolve_reset_at(body, {"retry-after": "30"}, NOW)
    assert got.source == BODY_JSON


def test_header_used_when_body_silent():
    got = resolve_reset_at('{"code":429,"msg":"slow down"}', {"retry-after": "120"}, NOW)
    assert got.source == HEADER
    assert got.epoch == int(NOW) + 120


def test_none_when_nothing_available():
    got = resolve_reset_at('{"code":429,"msg":"slow down"}', {}, NOW)
    assert got.source == NONE
    assert got.usable is False
    assert got.epoch == 0


def test_expired_is_treated_as_no_information():
    """已过期 → 视同"没有时间信息"，而不是"立刻恢复"。

    立刻恢复会让同秒并发重试立刻再吃一次同样的错误（热循环）；
    有界退避最短也是秒级，足以让频控窗口滑过。
    """
    past = _epoch("2020-01-01 00:00:00")
    got = resolve_reset_at(json.dumps({"data": {"resets_at": past}}), {}, NOW)
    assert got.source == EXPIRED
    assert got.usable is False
    assert got.expired is True


def test_now_is_injectable():
    """now 由调用方传入：一是可测，二是同一次错误处置里所有时间判断共用同一个"现在"。"""
    body = json.dumps({"data": {"resets_in": 60}})
    assert resolve_reset_at(body, {}, 1000.0).epoch == 1060
    assert resolve_reset_at(body, {}, 2000.0).epoch == 2060
