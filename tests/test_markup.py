"""模型原生工具调用标记（DSML）修复层测试。

夹具一律用包内常量拼装，**不在源码里写死真实分隔符字面量** —— 分隔符是全角字符
（U+FF5C），字面量容易被编辑器/复制粘贴静默改坏，而拼装出来的字节与上游实际下发
的一致。

移植自 misakano7545/workbuddy2api-panel 的 `internal/upstream/dsml_test.go`。
"""

from __future__ import annotations

import json

import pytest

from keyport.upstream.markup import (
    MAX_BLOCK_BYTES,
    MarkupRepair,
    repair_aggregated_response,
    tool_name_allowlist,
)

_PIPE = "\uff5c\uff5c"
_OPEN = f"<{_PIPE}DSML{_PIPE} calls>"
_CLOSE = f"</{_PIPE}DSML{_PIPE} calls>"
_INVOKE_CLOSE = f"</{_PIPE}DSML{_PIPE} invoke>"
_PARAM_CLOSE = f"</{_PIPE}DSML{_PIPE} parameter>"

TOOLS = {"exec_command", "write_stdin"}


# ---- 夹具构造 ----


def mk_param(name: str, value: str) -> str:
    """拼一个 string="true" 的 parameter（取值按原文字符串处理）。"""
    return f'<{_PIPE}DSML{_PIPE} parameter name="{name}" string="true">{value}{_PARAM_CLOSE}'


def mk_json_param(name: str, raw: str) -> str:
    """拼一个不带 string 属性的 parameter（取值按 JSON 解析）。"""
    return f'<{_PIPE}DSML{_PIPE} parameter name="{name}">{raw}{_PARAM_CLOSE}'


def mk_invoke(name: str, *params: str) -> str:
    body = "\n" + "\n".join(params) + "\n" if params else ""
    return f'<{_PIPE}DSML{_PIPE} invoke name="{name}">{body}{_INVOKE_CLOSE}'


def mk_block(*invokes: str) -> str:
    return _OPEN + "\n" + "\n".join(invokes) + "\n" + _CLOSE


def repair_all(allowed, chunks: list[str]) -> tuple[str, list]:
    """按给定分片顺序喂完整条正文，返回 (透出文本, 还原出的调用)。"""
    r = MarkupRepair(allowed=allowed)
    out: list[str] = []
    calls: list = []
    for chunk in chunks:
        emit, got = r.feed(chunk)
        out.append(emit)
        calls.extend(got)
    out.append(r.flush())
    return "".join(out), calls


def args_of(raw: str) -> dict:
    parsed = json.loads(raw)
    assert isinstance(parsed, dict), f"arguments 不是 JSON 对象: {raw!r}"
    return parsed


# ---- 基本解析 ----


def test_single_call():
    src = mk_block(mk_invoke("exec_command", mk_param("cmd", "ls -la /tmp")))
    out, calls = repair_all(TOOLS, [src])
    assert out == ""            # 整条正文都是标记 → 透出应为空
    assert len(calls) == 1
    assert calls[0].name == "exec_command"
    assert args_of(calls[0].arguments)["cmd"] == "ls -la /tmp"


def test_multiple_calls_and_order():
    src = mk_block(
        mk_invoke("exec_command", mk_param("cmd", "pwd")),
        mk_invoke("write_stdin", mk_param("session_id", "s1"), mk_param("chars", "y\n")),
    )
    out, calls = repair_all(TOOLS, [src])
    assert out == ""
    assert [c.name for c in calls] == ["exec_command", "write_stdin"]
    assert args_of(calls[1].arguments)["chars"] == "y\n"


def test_keeps_surrounding_prose():
    head = "先摸清接口清单，再落文档。\n\n"
    tail = "\n\n以上是第一批调用。"
    src = head + mk_block(mk_invoke("exec_command", mk_param("cmd", "pwd"))) + tail
    out, calls = repair_all(TOOLS, [src])
    assert len(calls) == 1
    assert out == head + tail


def test_no_parameters():
    src = mk_block(mk_invoke("exec_command"))
    _, calls = repair_all(TOOLS, [src])
    assert len(calls) == 1
    assert calls[0].arguments == "{}"


def test_string_param_escaping():
    """真实场景：apply_patch 的命令里带换行、双引号、反斜杠、中文。"""
    value = ("cd /tmp && apply_patch <<'PATCH'\n*** Begin Patch\n"
             '+  const s = "a\\\\b";\n+  // 中文注释\n*** End Patch\nPATCH')
    src = mk_block(mk_invoke("exec_command", mk_param("cmd", value)))
    _, calls = repair_all(TOOLS, [src])
    assert len(calls) == 1
    assert args_of(calls[0].arguments)["cmd"] == value


def test_json_param():
    src = mk_block(mk_invoke("exec_command", mk_json_param("opts", '{"timeout":30,"force":true}')))
    _, calls = repair_all(TOOLS, [src])
    opts = args_of(calls[0].arguments)["opts"]
    assert opts == {"timeout": 30, "force": True}


def test_json_param_falls_back_to_string():
    """string 属性缺失且取值不是合法 JSON：退化为字符串，绝不丢值。"""
    src = mk_block(mk_invoke("exec_command", mk_json_param("cmd", "ls -la")))
    _, calls = repair_all(TOOLS, [src])
    assert args_of(calls[0].arguments)["cmd"] == "ls -la"


# ---- 不转换（原文透出）的边界 ----


def test_disabled_without_tools():
    src = mk_block(mk_invoke("exec_command", mk_param("cmd", "pwd")))
    out, calls = repair_all(None, [src])
    assert out == src and calls == []


def test_unknown_tool_name_stays_text():
    src = mk_block(mk_invoke("rm_rf_everything", mk_param("cmd", "pwd")))
    out, calls = repair_all(TOOLS, [src])
    assert calls == []
    assert out == src


def test_prose_inside_block_stays_text():
    src = _OPEN + "\n我先说明一下：\n" + mk_invoke(
        "exec_command", mk_param("cmd", "pwd")) + "\n" + _CLOSE
    out, calls = repair_all(TOOLS, [src])
    assert calls == [] and out == src


def test_incomplete_block_flushes_verbatim():
    src = "前置说明\n" + _OPEN + "\n" + mk_invoke("exec_command", mk_param("cmd", "pwd"))
    out, calls = repair_all(TOOLS, [src])
    assert calls == []
    assert out == src


def test_partial_start_token_held_then_flushed():
    """正文恰好以起始标记的前缀结尾（增量边界切断）：不提前吐出半个标记，也不吞掉。"""
    partial = _OPEN[:-3]
    out, calls = repair_all(TOOLS, ["说明" + partial])
    assert calls == [] and out == "说明" + partial


def test_plain_angle_brackets_unaffected():
    """单个全角竖线（半角全角混排）不能触发缓冲。"""
    single = chr(0xFF5C)
    src = f'普通 HTML：<div class="x">a < b</div>，还有 3 < 5 与 <{single} 这种半角全角混排。'
    out, calls = repair_all(TOOLS, [src])
    assert calls == [] and out == src


def test_oversized_unclosed_block_回吐():
    """超长未闭合 → 判定不是标记，原文回吐，不无限缓冲。"""
    src = _OPEN + "x" * (MAX_BLOCK_BYTES + 10)
    out, calls = repair_all(TOOLS, [src])
    assert calls == []
    assert out == src


# ---- 增量边界 ----


def test_any_chunk_boundary():
    src = ("先读一遍控制器。\n\n"
           + mk_block(
               mk_invoke("exec_command", mk_param("cmd", "cd /tmp && ls -la")),
               mk_invoke("exec_command", mk_param("cmd", "pwd")),
           ) + "\n收尾说明。")
    want = "先读一遍控制器。\n\n\n收尾说明。"

    chunkings = {
        "whole": [src],
        "by_char": list(src),
        "step7": [src[i:i + 7] for i in range(0, len(src), 7)],
    }
    for label, chunks in chunkings.items():
        out, calls = repair_all(TOOLS, chunks)
        assert out == want, label
        assert len(calls) == 2, label
        assert args_of(calls[0].arguments)["cmd"] == "cd /tmp && ls -la", label


def test_byte_equivalence():
    """最重要的一条不变量：不转换时逐字节相同；转换时等于「输入去掉被转换的块」。"""
    cases = [
        ("空正文", TOOLS, "", "", 0),
        ("纯正文", TOOLS, "hello 世界", "hello 世界", 0),
        ("未声明工具", None,
         mk_block(mk_invoke("exec_command", mk_param("cmd", "pwd"))),
         mk_block(mk_invoke("exec_command", mk_param("cmd", "pwd"))), 0),
        ("畸形参数", TOOLS,
         _OPEN + mk_invoke("exec_command", "垃圾参数") + _CLOSE,
         _OPEN + mk_invoke("exec_command", "垃圾参数") + _CLOSE, 0),
        ("单块", TOOLS,
         "a" + mk_block(mk_invoke("exec_command", mk_param("cmd", "pwd"))) + "b", "ab", 1),
        ("双块", TOOLS,
         mk_block(mk_invoke("exec_command", mk_param("cmd", "a"))) + "中间"
         + mk_block(mk_invoke("exec_command", mk_param("cmd", "b"))), "中间", 2),
    ]
    for label, allowed, src, want, n in cases:
        out, calls = repair_all(allowed, [src])
        assert out == want, label
        assert len(calls) == n, label


def test_converted_counter():
    r = MarkupRepair(allowed=TOOLS)
    src = mk_block(
        mk_invoke("exec_command", mk_param("cmd", "a")),
        mk_invoke("exec_command", mk_param("cmd", "b")),
    )
    _, calls = r.feed(src)
    assert len(calls) == 2
    assert r.converted == 2
    assert MarkupRepair(allowed=None).converted == 0


def test_counters_seen_and_rejected():
    """排障时要能区分「没识别到」与「识别到但判定不通过」。"""
    r = MarkupRepair(allowed=TOOLS)
    r.feed(_OPEN + mk_invoke("exec_command", "垃圾参数") + _CLOSE)
    assert r.seen == 1 and r.rejected == 1 and r.converted == 0


# ---- 工具名单提取 ----


@pytest.mark.parametrize("body,want", [
    (('{"tools":[{"type":"function","function":{"name":"exec_command"}},'
      '{"type":"function","function":{"name":"write_stdin"}}]}'),
     {"exec_command", "write_stdin"}),
    ('{"tools":[{"type":"function","name":"exec_command"}]}', {"exec_command"}),
    ('{"functions":[{"name":"exec_command"}]}', {"exec_command"}),
    ('{"messages":[{"role":"assistant","tool_calls":[{"function":{"name":"exec_command"}}]}]}',
     {"exec_command"}),
    ('{"messages":[{"role":"assistant","function_call":{"name":"exec_command"}}]}',
     {"exec_command"}),
    ('{"model":"x","messages":[]}', set()),
    ('{"tools":[]}', set()),
    ('{', set()),
    ('{"tools":[{"type":"function","function":{"name":""}}]}', set()),
])
def test_tool_name_allowlist(body, want):
    assert tool_name_allowlist(body) == want


def test_tool_name_allowlist_accepts_dict():
    assert tool_name_allowlist({"tools": [{"function": {"name": "a"}}]}) == {"a"}


# ---- 非流式聚合响应修复 ----


def _aggregated(content: str, finish: str) -> dict:
    return {
        "id": "chatcmpl-test", "object": "chat.completion",
        "model": "global:deepseek-v4.1-flash",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish,
        }],
    }


def test_repair_aggregated_response():
    content = "先读一遍控制器。\n\n" + mk_block(mk_invoke("exec_command", mk_param("cmd", "ls -la")))
    resp = _aggregated(content, "stop")
    assert repair_aggregated_response(resp, TOOLS).converted == 1
    choice = resp["choices"][0]
    assert choice["message"]["content"] == "先读一遍控制器。\n\n"
    tcs = choice["message"]["tool_calls"]
    assert len(tcs) == 1
    assert tcs[0]["type"] == "function" and tcs[0]["id"]
    assert tcs[0]["function"]["name"] == "exec_command"
    assert args_of(tcs[0]["function"]["arguments"])["cmd"] == "ls -la"
    assert choice["finish_reason"] == "tool_calls"


def test_repair_aggregated_response_skips():
    markup = mk_block(mk_invoke("exec_command", mk_param("cmd", "ls -la")))

    # 已有结构化 tool_calls → 不覆盖
    with_calls = _aggregated(markup, "stop")
    with_calls["choices"][0]["message"]["tool_calls"] = [{"id": "real", "type": "function"}]
    assert repair_aggregated_response(with_calls, TOOLS).converted == 0
    assert with_calls["choices"][0]["message"]["tool_calls"] == [{"id": "real", "type": "function"}]

    # finish_reason=length（正文被截断）→ 不解释
    assert repair_aggregated_response(_aggregated(markup, "length"), TOOLS).converted == 0

    # 客户端没声明 tools → 不启用
    assert repair_aggregated_response(_aggregated(markup, "stop"), None,
                                      allow_unknown=False).converted == 0

    # 纯正文 → 零改写，且内容不变
    plain = _aggregated("普通回答，没有任何标记。", "stop")
    assert repair_aggregated_response(plain, TOOLS).converted == 0
    assert plain["choices"][0]["message"]["content"] == "普通回答，没有任何标记。"


def test_repair_aggregated_weak_mode():
    """tools 名单丢失（实测最常见形态）时弱判定仍能还原。"""
    markup = mk_block(mk_invoke("exec_command", mk_param("cmd", "ls -la")))
    resp = _aggregated("前言\n" + markup, "stop")
    assert repair_aggregated_response(resp, None, allow_unknown=True).converted == 1
    assert resp["choices"][0]["message"]["tool_calls"][0]["function"]["name"] == "exec_command"


# ---- 弱判定 ----


@pytest.mark.parametrize("tool,want_ok", [
    ("exec_command", True),
    ("mcp.server:tool", True),
    ("write-stdin", True),
    ("NAME", True),          # 形态合法；拦它的是严格判定那条路
    ("<tool_name>", False),
    ("", False),
    ("exec command", False),
    ("a" * 65, False),
])
def test_weak_mode_name_shape(tool, want_ok):
    src = mk_block(mk_invoke(tool, mk_param("cmd", "ls")))
    r = MarkupRepair(allowed=None, allow_unknown=True)
    out, calls = r.feed(src)
    out += r.flush()
    assert (len(calls) == 1) is want_ok, out


@pytest.mark.parametrize("src", [
    _OPEN + "\n说明文字\n" + mk_invoke("exec_command", mk_param("cmd", "ls")) + "\n" + _CLOSE,
    _OPEN + mk_invoke("exec_command", "垃圾参数") + _CLOSE,
    _OPEN + mk_invoke("exec_command", mk_param("cmd", "ls")),
])
def test_weak_mode_still_rejects_malformed(src):
    """弱判定不放松结构要求：夹带正文 / 参数畸形 / 未闭合，一律原文透出。"""
    r = MarkupRepair(allowed=None, allow_unknown=True)
    out, calls = r.feed(src)
    out += r.flush()
    assert calls == [] and out == src


def test_disabled():
    src = mk_block(mk_invoke("exec_command", mk_param("cmd", "ls")))
    for r in (MarkupRepair(allowed=None), MarkupRepair(allowed=set())):
        assert r.enabled() is False
        out, calls = r.feed(src)
        assert out == src and calls == [] and r.flush() == ""


def test_enabled_with_weak_mode():
    assert MarkupRepair(allowed=None, allow_unknown=True).enabled() is True
    assert MarkupRepair(allowed={"a"}).enabled() is True
