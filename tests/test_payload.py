"""出站请求体改写管线测试。"""

from __future__ import annotations

import json

from keyport.upstream import payload as P
from keyport.upstream import prompt, thinking


def _obj(**kw) -> dict:
    base = {"model": "glm-4.6", "messages": [{"role": "user", "content": "hi"}]}
    base.update(kw)
    return base


# ---- 强制流式与字段翻译 ----


def test_prepare_body_forces_stream_and_usage():
    out = P.prepare_body(json.dumps(_obj()).encode(), prompt_mode="passthrough")
    obj = json.loads(out)
    assert obj["stream"] is True
    assert obj["stream_options"] == {"include_usage": True}


def test_translate_max_completion_tokens():
    obj = _obj(max_completion_tokens=500)
    P.translate_max_completion_tokens(obj)
    assert obj["max_tokens"] == 500
    assert "max_completion_tokens" not in obj


def test_translate_skips_when_max_tokens_present():
    obj = _obj(max_tokens=100, max_completion_tokens=500)
    P.translate_max_completion_tokens(obj)
    assert obj["max_tokens"] == 100


def test_translate_skips_zero_alias():
    obj = _obj(max_completion_tokens=0)
    P.translate_max_completion_tokens(obj)
    assert "max_tokens" not in obj


# ---- tool_choice 归一化 ----


def test_tool_choice_none_drops_tools():
    obj = _obj(tool_choice="none", tools=[{"type": "function"}])
    P.normalize_tool_choice(obj)
    assert "tool_choice" not in obj and "tools" not in obj


def test_tool_choice_object_becomes_string():
    obj = _obj(tool_choice={"type": "auto"})
    P.normalize_tool_choice(obj)
    assert obj["tool_choice"] == "auto"


def test_tool_choice_named_function():
    obj = _obj(tool_choice={"type": "function", "function": {"name": "f"}})
    P.normalize_tool_choice(obj)
    assert obj["tool_choice"] == "f"


# ---- role 归一化 ----


def test_developer_role_becomes_system():
    obj = {"messages": [{"role": "developer", "content": "x"}]}
    P.normalize_roles(obj)
    assert obj["messages"][0]["role"] == "system"


# ---- tool 配对 ----


def test_orphan_tool_call_removed():
    msgs = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "next"},
    ]
    out, changed = P.cleanup_orphan_tool_calls(msgs)
    assert changed is True
    assert out[0]["role"] == "user"  # 只剩 role 的 assistant 被丢弃


def test_complete_tool_pair_kept():
    msgs = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    out, changed = P.cleanup_orphan_tool_calls(msgs)
    assert changed is False
    assert len(out) == 2


def test_repack_moves_interleaved_message():
    msgs = [
        {"role": "assistant", "content": None,
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "user", "content": "image_resize_notice"},
        {"role": "tool", "tool_call_id": "c1", "content": "ok"},
    ]
    out, changed = P.repack_tool_result_blocks(msgs)
    assert changed is True
    assert out[0]["role"] == "assistant"
    assert out[1]["role"] == "tool"      # tool 结果紧跟其后
    assert out[2]["role"] == "user"      # 插队消息被挪后


# ---- 指纹脱敏 ----


def test_sanitize_removes_fingerprint_keys():
    obj = {"messages": [{"role": "user", "content": "x", "machine_id": "abc"}],
           "client_version": "1.0", "model": "m"}
    P.sanitize_fingerprints(obj)
    assert "client_version" not in obj
    assert "machine_id" not in obj["messages"][0]
    assert obj["model"] == "m"


# ---- 压缩投影 ----


def test_compress_long_context_folds_middle():
    msgs = [{"role": "user", "content": "x" * 100} for _ in range(40)]
    obj = {"messages": msgs}
    changed = P.compress_long_context(obj, max_tokens=10, keep_recent=5)
    assert changed is True
    assert len(obj["messages"]) < len(msgs)
    assert any("已压缩" in str(m.get("content")) for m in obj["messages"])


# ---- 思维链 ----


def test_thinking_injection_for_reasoning_model():
    obj = {"model": "deepseek-reasoner"}
    thinking.inject_thinking(obj, None)
    assert obj["thinking"]["type"] == "enabled"
    assert obj["reasoning_effort"] == "high"


def test_effort_downgrade_to_supported():
    obj = {"model": "deepseek-reasoner", "reasoning_effort": "high"}
    thinking.normalize_reasoning_effort(obj, ["low", "medium"])
    assert obj["reasoning_effort"] == "medium"


def test_effort_kept_when_supported():
    obj = {"model": "m", "reasoning_effort": "low"}
    thinking.normalize_reasoning_effort(obj, ["low", "medium", "high"])
    assert obj["reasoning_effort"] == "low"


def test_reasoning_content_backfill():
    obj = {"messages": [{"role": "assistant", "content": "a", "reasoning": "trace"}]}
    thinking.backfill_reasoning_content(obj)
    assert obj["messages"][0]["reasoning_content"] == "trace"
    assert "reasoning" not in obj["messages"][0]


# ---- 系统提示词 ----


def test_custom_mode_replaces_system():
    obj = {"messages": [{"role": "system", "content": "client sys"},
                        {"role": "user", "content": "hi"}]}
    prompt.apply_system_prompt(obj, "custom", "GATEWAY PROMPT")
    assert obj["messages"][0] == {"role": "system", "content": "GATEWAY PROMPT"}
    assert obj["messages"][1] == {"role": "user", "content": "hi"}


def test_passthrough_mode_keeps_system():
    obj = {"messages": [{"role": "system", "content": "client sys"}]}
    prompt.apply_system_prompt(obj, "passthrough", "GATEWAY PROMPT")
    assert obj["messages"][0]["content"] == "client sys"


def test_append_mode_extends_system():
    obj = {"messages": [{"role": "system", "content": "base"}]}
    prompt.apply_system_prompt(obj, "append", "extra")
    assert obj["messages"][0]["content"] == "base\n\nextra"


def test_blocked_detection():
    assert prompt.looks_blocked(400, "内容安全策略拦截") is True
    assert prompt.looks_blocked(200, "ok") is False
