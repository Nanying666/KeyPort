"""推理模型兼容：DeepSeek 思维链注入、effort 档位降级、reasoning_content 回填。"""

from __future__ import annotations

from typing import Any

# 模型 → 支持的 reasoning_effort 档位（升序）。空/未知 = 不降级。
EFFORT_LADDER = ["minimal", "low", "medium", "high"]
DEFAULT_EFFORT = "high"


def supports_thinking(model: str) -> bool:
    """是否为需要注入 thinking 开关的推理模型。"""
    m = (model or "").lower()
    return any(tag in m for tag in ("deepseek", "reasoner", "thinking", "glm-5", "glm-4.6", "o1", "o3"))


def supported_efforts(model: str) -> list[str] | None:
    """返回模型支持的 effort 档位；None = 未知（不降级）。"""
    m = (model or "").lower()
    if not supports_thinking(m):
        return None
    if "minimal" in m or "flash" in m:
        return ["minimal", "low"]
    if "reasoner" in m or "deepseek" in m:
        return ["low", "medium", "high"]
    return ["low", "medium", "high"]


def inject_thinking(obj: dict[str, Any], default_effort: str | None = None) -> None:
    """注入 thinking.type=enabled 并在缺档时补默认档（先于 effort 降级执行）。"""
    model = str(obj.get("model", ""))
    if not supports_thinking(model):
        return
    thinking = obj.get("thinking")
    if not isinstance(thinking, dict):
        thinking = {}
    if "type" not in thinking:
        thinking["type"] = "enabled"
    obj["thinking"] = thinking
    if not obj.get("reasoning_effort"):
        obj["reasoning_effort"] = default_effort or DEFAULT_EFFORT


def normalize_reasoning_effort(obj: dict[str, Any], efforts: list[str] | None) -> None:
    """按模型支持档位降级 reasoning_effort（就地修改）。"""
    if not efforts:
        return
    req = obj.get("reasoning_effort")
    if not isinstance(req, str):
        return
    req_norm = req.strip().lower()
    if req_norm in efforts:
        return
    order = [e for e in EFFORT_LADDER if e in efforts]
    if not order:
        return
    req_rank = EFFORT_LADDER.index(req_norm) if req_norm in EFFORT_LADDER else len(EFFORT_LADDER)
    # 取 ≤ 请求档位的最高支持档；全部高于请求档时取最低档
    lower = [e for e in order if EFFORT_LADDER.index(e) <= req_rank]
    obj["reasoning_effort"] = lower[-1] if lower else order[0]


def backfill_reasoning_content(obj: dict[str, Any]) -> None:
    """多轮一致性：assistant 消息带推理痕迹时回填 reasoning_content。"""
    msgs = obj.get("messages")
    if not isinstance(msgs, list):
        return
    for msg in msgs:
        if not isinstance(msg, dict):
            continue
        if str(msg.get("role", "")) != "assistant":
            continue
        if msg.get("reasoning_content"):
            continue
        # 仅当确有推理痕迹（thinking 块或 reasoning 字段）才回填，避免凭空造内容
        trace = msg.pop("reasoning", None)
        if isinstance(trace, str) and trace:
            msg["reasoning_content"] = trace
        elif isinstance(msg.get("thinking"), dict):
            content = msg["thinking"].get("content") or msg["thinking"].get("text")
            if isinstance(content, str) and content:
                msg["reasoning_content"] = content
