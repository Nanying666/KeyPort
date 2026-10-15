"""截断工具调用丢弃。

## 问题

SSE 流被截断（连接中断 / `finish_reason == length`）时，工具调用的 `arguments`
只剩半截 JSON。此时若把脏参数原样交给客户端，客户端解析会报非法 JSON 并**卡死
会话**。

## 关键区分：只把「非空但无法解析」视为截断

- **空串 / 纯空白** → 合法无参工具（模型可以不带参数调用），**不是**截断
- **非空但 JSON 解析失败** → 截断
- **能解析**（含 `null` / 标量 / 数组等任何合法 JSON）→ 不是截断
  （类型不对属于模型输出错误，交给客户端 schema 校验回传）

## 处置是丢弃，不是补成 `{}`

补一个 `{}` 会**伪造出「参数合法」的外观**，客户端会拿空参数去执行 —— 比报错更危险。
丢弃残缺调用并触发重试才是正确处置。
"""

from __future__ import annotations

import json
from typing import Any


def is_truncated_arguments(raw: Any) -> bool:
    """该 `arguments` 是否是「非空但无法解析」的截断形态。"""
    if not isinstance(raw, str):
        return False
    text = raw.strip()
    if not text:
        return False  # 合法无参工具
    try:
        json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return True
    return False


def drop_truncated_tool_calls(tool_calls: Any) -> tuple[Any, int]:
    """剔除 `arguments` 被截断的工具调用；返回 (保留的调用, 丢弃数)。

    保留的调用**不做任何改动**（正例零改动）。
    """
    if not isinstance(tool_calls, list):
        return tool_calls, 0
    kept: list[Any] = []
    dropped = 0
    for call in tool_calls:
        if not isinstance(call, dict):
            kept.append(call)
            continue
        fn = call.get("function")
        args = fn.get("arguments") if isinstance(fn, dict) else None
        if is_truncated_arguments(args):
            dropped += 1
            continue
        kept.append(call)
    return kept, dropped
