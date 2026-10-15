"""出站指纹文本级净化。

移植自 sheng-du0521/workbuddy2api 的 `internal/upstream/sanitize.go`。
上游内容审核**按逐字精确匹配**拦截（不是语义审核），因此一字改动即可绕过。
"""

from __future__ import annotations

from keyport.upstream.payload import sanitize_fingerprints
from keyport.upstream.sanitize import (
    has_fingerprint,
    sanitize_body,
    sanitize_content,
    sanitize_messages,
    sanitize_text,
    sanitize_tool_calls,
)

IDENTITY = "You are Claude Code, Anthropic's official CLI for Claude"
BRANCH = "Main branch (you will usually use this for PRs)"
CODEX = ("You are a coding agent running in the Codex CLI, "
         "a terminal-based coding assistant.")
FEEDBACK = ("To give feedback, users should report the issue at "
            "https://github.com/anthropics/claude-code/issues")


# ---- 改写层（语义不变，只改一个词） ----


def test_identity_sentence_rewritten():
    out = sanitize_text(f"prefix {IDENTITY} suffix")
    assert "official CLI tool for Claude" in out
    assert "official CLI for Claude" not in out


def test_identity_sentence_without_trailing_punctuation():
    """匹配串刻意不带结尾标点：CLI 版以句号收尾，桌面版以逗号接后继内容。

    带句号的整句只匹配前者，桌面版会漏网、指纹原样发上游。
    """
    cli = f"{IDENTITY}."
    desktop = f"{IDENTITY}, running within the Claude Agent SDK."
    for text in (cli, desktop):
        out = sanitize_text(text)
        assert "official CLI tool for Claude" in out
        assert "official CLI for Claude" not in out
    # 原有标点原样保留
    assert sanitize_text(cli).endswith(".")


def test_branch_sentence_rewritten():
    assert sanitize_text(BRANCH).startswith("Default branch (")


def test_codex_sentence_rewritten():
    out = sanitize_text(CODEX)
    assert "Codex CLI tool," in out


def test_feedback_sentence_rewritten():
    """整句带仓库链接，上游按整句拦截；give→provide 即可绕过。"""
    out = sanitize_text(FEEDBACK)
    assert out.startswith("To provide feedback")
    assert "github.com/anthropics/claude-code/issues" in out


def test_bare_11128_rewritten():
    """上游反探测：请求体里出现裸数字 11128 就整单拦截（与上下文无关）。

    代价：用户对话里任何 11128 都会被改写 —— 但这串数字出现在请求里本身就是
    拦截条件，不改写必然失败。
    """
    for text in ("code=11128", "11128", "错误码 11128", "Code=11128"):
        assert "11-128" in sanitize_text(text), text


def test_adjacent_codes_untouched():
    """相邻错误码均放行，不得误伤。"""
    for text in ("11148", "11101", "11115", "99999"):
        assert sanitize_text(text) == text, text


def test_zero_width_space_does_not_help():
    """零宽空格无效（实测上游会归一化），所以用连字符。"""
    assert "\u200b" not in sanitize_text("11128")


# ---- 剥离层（整段删除，不承载语义） ----


def test_billing_header_kv_stripped():
    text = "x-anthropic-billing-header: cc_version=1.2.3; cc_entrypoint=cli;"
    out = sanitize_text(text)
    assert "x-anthropic-billing-header" not in out
    assert "cc_entrypoint=" not in out


def test_billing_header_case_insensitive():
    out = sanitize_text("X-Anthropic-Billing-Header: abc;")
    assert "Anthropic-Billing-Header" not in out


def test_bare_header_name_abbreviated():
    """裸键名（无冒号无值）同样是指纹 —— 反引号引用即触发。

    键值形态被整段删除后，残留的裸键名做最小缩写（header→hdr）：
    破坏逐字匹配、语义不变、保留可读性。
    """
    out = sanitize_text("see `x-anthropic-billing-header` for details")
    assert "x-anthropic-billing-hdr" in out
    assert "x-anthropic-billing-header" not in out


def test_bare_header_mixed_case_abbreviated():
    """大小写变体 + 裸键名形态必须被兜底正则覆盖。

    `in` 大小写敏感、剥离层要求冒号 —— 两者都会漏掉这种形态。
    """
    assert has_fingerprint("X-Anthropic-Billing-Header")
    out = sanitize_text("X-Anthropic-Billing-Header")
    assert "X-Anthropic-Billing-Header" not in out


def test_cc_kv_stripped_looped():
    """尾随裸键值循环清理直到稳定。"""
    text = "cc_version=1.2.3; cc_entrypoint=cli; cc_foo=bar;"
    out = sanitize_text(text)
    assert "cc_" not in out


# ---- 预检 ----


def test_has_fingerprint_clean_text():
    assert has_fingerprint("普通对话内容，没有任何指纹") is False
    assert sanitize_text("普通对话内容") == "普通对话内容"


def test_has_fingerprint_detects_each_feature():
    for text in (IDENTITY, BRANCH, CODEX, FEEDBACK, "x-anthropic-billing-header",
                 "cc_entrypoint=", "11128"):
        assert has_fingerprint(text) is True, text


def test_plain_text_untouched():
    """预检不中必须原样返回，绝不误伤普通正文。"""
    text = "请帮我写一个快速排序，要求稳定且原地。"
    assert sanitize_text(text) == text


def test_angle_brackets_and_code_untouched():
    for text in ("<div class='x'>a < b</div>", "print('hello')", "SELECT * FROM t;"):
        assert sanitize_text(text) == text, text


# ---- content 形态 ----


def test_sanitize_content_string():
    out, changed = sanitize_content(f"hi {IDENTITY}")
    assert changed is True
    assert "official CLI tool for Claude" in out


def test_sanitize_content_multimodal_only_touches_text_parts():
    """只动 text part，image 等 part 不动。"""
    parts = [
        {"type": "text", "text": IDENTITY},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAA"}},
    ]
    out, changed = sanitize_content(parts)
    assert changed is True
    assert "official CLI tool for Claude" in out[0]["text"]
    assert out[1]["image_url"]["url"] == "data:image/png;base64,AAA"


def test_sanitize_content_untouched():
    out, changed = sanitize_content("clean")
    assert changed is False and out == "clean"
    assert sanitize_content(None) == (None, False)
    assert sanitize_content(123) == (123, False)


# ---- tool_calls 盲区 ----


def test_sanitize_tool_calls_arguments():
    """arguments 是字符串化的 JSON，按文本走。

    这块长期是盲区：工具调用消息的 content 通常是 null，早期实现在 content
    缺失时直接跳过整条消息，于是写进工具参数的被拦字符串会原样漏出。
    """
    calls = [{"function": {"name": "f", "arguments": f'{{"cmd":"{IDENTITY}"}}'}}]
    assert sanitize_tool_calls(calls) is True
    assert "official CLI tool for Claude" in calls[0]["function"]["arguments"]


def test_sanitize_tool_calls_clean():
    calls = [{"function": {"name": "f", "arguments": '{"cmd":"ls"}'}}]
    assert sanitize_tool_calls(calls) is False


def test_tool_calls_sanitized_even_when_content_is_null():
    """content 为 null（工具调用轮）时，tool_calls 仍必须被净化。"""
    msgs = [{"role": "assistant", "content": None,
             "tool_calls": [{"function": {"name": "f",
                                          "arguments": f'{{"x":"{IDENTITY}"}}'}}]}]
    assert sanitize_messages(msgs) is True
    assert "official CLI tool for Claude" in msgs[0]["tool_calls"][0]["function"]["arguments"]


# ---- messages 聚合 ----


def test_sanitize_messages_content_and_reasoning():
    msgs = [
        {"role": "system", "content": IDENTITY},
        {"role": "assistant", "content": "ok", "reasoning_content": BRANCH},
    ]
    assert sanitize_messages(msgs) is True
    assert "official CLI tool for Claude" in msgs[0]["content"]
    assert msgs[1]["reasoning_content"].startswith("Default branch (")


def test_sanitize_messages_clean_returns_false():
    assert sanitize_messages([{"role": "user", "content": "hello"}]) is False
    assert sanitize_messages(None) is False
    assert sanitize_messages("not a list") is False


# ---- body 入口 ----


def test_sanitize_body_messages():
    obj = {"messages": [{"role": "user", "content": FEEDBACK}]}
    assert sanitize_body(obj) is True
    assert obj["messages"][0]["content"].startswith("To provide feedback")


def test_sanitize_body_responses_input_string():
    obj = {"input": f"question about {BRANCH}"}
    assert sanitize_body(obj) is True
    assert "Default branch (" in obj["input"]


def test_sanitize_body_responses_input_list():
    obj = {"input": [{"role": "user", "content": IDENTITY}]}
    assert sanitize_body(obj) is True
    assert "official CLI tool for Claude" in obj["input"][0]["content"]


# ---- 与 payload 的接线 ----


def test_sanitize_fingerprints_keeps_key_stripping_and_adds_text_level():
    """两层互补：键名删除挡结构化字段，文本净化挡写在正文里的模板句。"""
    obj = {
        "machine_id": "should-be-removed",
        "messages": [{"role": "user", "content": IDENTITY}],
    }
    sanitize_fingerprints(obj)
    assert "machine_id" not in obj
    assert "official CLI tool for Claude" in obj["messages"][0]["content"]


def test_sanitize_fingerprints_clean_body_unchanged():
    obj = {"messages": [{"role": "user", "content": "普通问题"}]}
    sanitize_fingerprints(obj)
    assert obj == {"messages": [{"role": "user", "content": "普通问题"}]}
