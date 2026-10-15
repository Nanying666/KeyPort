"""会话键提取（含内容派生回退）与上游前缀缓存键注入。

移植自 misakano7545/workbuddy2api-panel 的 `internal/session/session.go`（派生回退）
与 `internal/upstream/cache_key.go`（cache key 注入）。
"""

from __future__ import annotations

import pytest

from keyport.upstream.cache_key import (
    CACHE_KEY_PREFIX,
    DERIVED_PREFIX,
    build_prompt_cache_key,
    cache_key_source,
    derive_conversation_key,
    extract_session_key,
    inject_prompt_cache_key,
)

# ---- 会话键提取优先级 ----


@pytest.mark.parametrize("header", ["x-conversation-id", "x-session-id", "x-trace-id"])
def test_header_wins(header):
    assert extract_session_key({}, {header: "h-1"}) == "h-1"


def test_metadata_beats_toplevel():
    body = {"metadata": {"conversation_id": "m-1"}, "conversation_id": "t-1"}
    assert extract_session_key(body, {}) == "m-1"


def test_metadata_camel_variant():
    assert extract_session_key({"metadata": {"conversationId": "m-2"}}, {}) == "m-2"


@pytest.mark.parametrize("key", ["conversation_id", "conversationId", "session_id", "sessionId"])
def test_toplevel_variants(key):
    assert extract_session_key({key: "t-2"}, {}) == "t-2"


def test_prompt_cache_key_is_a_session_source():
    """pi-ai 驱动的客户端把会话 ID 放在 prompt_cache_key 里。"""
    assert extract_session_key({"prompt_cache_key": "pck-1"}, {}) == "pck-1"


def test_user_key_is_not_a_session_key():
    """顶层 `user` 刻意不再作会话键。

    user 维度粒度过粗，一个用户的全部并行对话会被钉到同一账号，
    粘性范围远超上游 prompt cache 的对话级边界。
    """
    assert extract_session_key({"user": "u-1"}, {}) == ""


def test_empty_when_nothing_usable():
    assert extract_session_key({}, {}) == ""
    assert extract_session_key(None, None) == ""


# ---- 内容派生回退 ----


def test_derive_uses_system_and_first_user():
    body = {"messages": [
        {"role": "system", "content": "你是助手"},
        {"role": "user", "content": "第一条"},
        {"role": "user", "content": "第二条"},
    ]}
    key = derive_conversation_key(body)
    assert key.startswith(DERIVED_PREFIX)
    assert len(key) == len(DERIVED_PREFIX) + 16


def test_derive_stable_across_appended_history():
    """多轮对话里历史每轮追加，但 system + 首条 user 恒定 → 键必须稳定。

    若用全量消息哈希，粘性会每轮失效。
    """
    first = {"messages": [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U1"},
    ]}
    later = {"messages": [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "U1"},
        {"role": "assistant", "content": "A1"},
        {"role": "user", "content": "U2"},
    ]}
    assert derive_conversation_key(first) == derive_conversation_key(later)


def test_derive_differs_across_conversations():
    a = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "A"}]}
    b = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "B"}]}
    assert derive_conversation_key(a) != derive_conversation_key(b)


def test_derive_handles_multimodal_first_user():
    """纯图片轮（无 text part）在纯文本提取下恒空串 → 粘性盲区。

    签名口径下图片 part 以 `[type:摘要]` 入键，故仍能派生出稳定键。
    """
    body = {"messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]}]}
    key = derive_conversation_key(body)
    assert key.startswith(DERIVED_PREFIX)


def test_derive_empty_when_no_usable_text():
    assert derive_conversation_key({"messages": []}) == ""
    assert derive_conversation_key({"messages": [{"role": "assistant", "content": "x"}]}) == ""
    assert derive_conversation_key({}) == ""


def test_derive_falls_back_through_extract():
    body = {"messages": [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]}
    assert extract_session_key(body, {}).startswith(DERIVED_PREFIX)


def test_user_id_gate_blocks_derivation():
    """body 带 user_id 时不派生、回落轮换。

    否则「只发 user_id」的请求会借内容哈希重新获得粘性，
    让「user 不作粘性键」的契约在回退路径上失效。
    """
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "U"}]
    assert extract_session_key({"messages": msgs}, {}) != ""
    assert extract_session_key({"messages": msgs, "user_id": "u1"}, {}) == ""
    assert extract_session_key({"messages": msgs, "metadata": {"user_id": "u1"}}, {}) == ""


# ---- 上游前缀缓存键 ----


def test_cache_key_shape_and_isolation():
    k1 = build_prompt_cache_key("uid-aaaa1111", "conv-1")
    k2 = build_prompt_cache_key("uid-bbbb2222", "conv-1")
    assert k1.startswith(f"{CACHE_KEY_PREFIX}-uid-aaaa")
    # 同一会话源 + 不同账号 → 绝不碰撞（否则会命中别人的前缀缓存并泄露对话）
    assert k1 != k2


def test_cache_key_stable_for_same_account_and_conversation():
    assert (build_prompt_cache_key("u1", "c1") == build_prompt_cache_key("u1", "c1"))


def test_cache_key_without_conversation_still_isolated():
    """空会话源时仍由 uid 单独哈希：跨账号不碰撞，同账号同空会话复用。"""
    assert build_prompt_cache_key("u1", "") != build_prompt_cache_key("u2", "")
    assert build_prompt_cache_key("u1", "") == build_prompt_cache_key("u1", "")


def test_cache_key_empty_uid_uses_dash():
    assert build_prompt_cache_key("", "c1").startswith(f"{CACHE_KEY_PREFIX}--")


def test_inject_sets_field():
    body = {"model": "hy3", "messages": []}
    assert inject_prompt_cache_key(body, "u1", "c1") is True
    assert body["prompt_cache_key"] == build_prompt_cache_key("u1", "c1")


def test_inject_never_overwrites_client_value():
    """客户端已带该字段时原值保留 —— 它比我们更清楚该复用哪个键。"""
    body = {"prompt_cache_key": "client-owned"}
    assert inject_prompt_cache_key(body, "u1", "c1") is False
    assert body["prompt_cache_key"] == "client-owned"


def test_inject_uses_body_conversation_over_argument():
    body = {"conversation_id": "from-body"}
    inject_prompt_cache_key(body, "u1", "from-arg")
    assert body["prompt_cache_key"] == build_prompt_cache_key("u1", "from-body")


def test_inject_ignores_non_dict():
    assert inject_prompt_cache_key(None, "u1") is False
    assert inject_prompt_cache_key([], "u1") is False  # type: ignore[arg-type]


def test_cache_key_source():
    assert cache_key_source({"conversation_id": "b"}, "a") == "b"
    assert cache_key_source({}, "a") == "a"
    assert cache_key_source(None, "") == ""
