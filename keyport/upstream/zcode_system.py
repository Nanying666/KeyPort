"""ZCode 官方 system prompt 组装。

为什么必须做这件事：上游网关对请求做**内容检查**——如果 `system` 字段里
看不到 ZCode 身份块，直接返回 `3012 "method not allowed"`（对外文案
"request has been blocked due to unusual activity"）。

参考 a137460387/zcode2api 的实测结论：官方客户端请求体约 8.5KB（含身份块 +
环境信息 + `<system-reminder># currentDate`），而裸请求只有约 100 字节。

组装形态（已按官方核对）：
  1. `cliPrefix` 单独一块
  2. 其余 stable 段以 `\\n\\n` 连接成一块
  3. dynamic 段（含 Environment Info）以 `\\n\\n` 连接，块文本以 `\\n\\n` 开头
  每块都带 `cache_control: {type: "ephemeral"}`。
  用户首轮消息前另挂 `<system-reminder>…# currentDate…</system-reminder>`。

静态文案取自 assets/zcode-system.json。
"""

from __future__ import annotations

import json
import os
import platform
import sys
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

_ASSET = Path(__file__).resolve().parent / "assets" / "zcode-system.json"

EPHEMERAL = {"type": "ephemeral"}


@lru_cache(maxsize=1)
def _assets() -> dict[str, Any]:
    return json.loads(_ASSET.read_text(encoding="utf-8"))


def _format_local_iso_date(now: datetime | None = None) -> str:
    """官方用本地时区的 ISO 日期，不用 UTC。"""
    d = now or datetime.now()
    return f"{d.year:04d}-{d.month:02d}-{d.day:02d}"


def normalize_system_blocks(system: Any) -> list[dict[str, str]]:
    """把调用方传入的 system 归一为文本块列表。"""
    if system is None:
        return []
    if isinstance(system, str):
        t = system.strip()
        return [{"type": "text", "text": t}] if t else []
    if not isinstance(system, list):
        return []
    out: list[dict[str, str]] = []
    for item in system:
        if isinstance(item, str):
            if item.strip():
                out.append({"type": "text", "text": item})
        elif (isinstance(item, dict) and item.get("type") == "text"
              and isinstance(item.get("text"), str) and item["text"].strip()):
            out.append({"type": "text", "text": item["text"]})
    return out


def build_environment_section(
    *, cwd: str | None = None, model: str | None = None,
    provider: str = "bigmodel",
) -> str:
    """Environment Info 段：用真实运行环境，避免与请求头矛盾（会露馅）。"""
    e = _assets()["environment"]
    cwd = cwd or os.getcwd()
    plat = "win32" if sys.platform == "win32" else sys.platform
    shell = "cmd" if sys.platform == "win32" else (os.environ.get("SHELL") or "sh")
    lines = [
        e["heading"],
        f"- {e['cwdLabel']}: {cwd}",
        e["invokedLine"],
        f"- {e['platformLabel']}: {plat}",
        f"- {e['shellLabel']}: {shell}",
        f"- {e['osVersionLabel']}: {platform.release()}",
    ]
    if model:
        lines.append(
            e["poweredByLine"].replace("{provider}", f"{provider}-api").replace("{model}", model)
        )
    if e.get("gitLabel") and e.get("gitNo"):
        lines.append(f"- {e['gitLabel']}: {e['gitNo']}")
    return "\n".join(lines)


def build_zcode_plan_system(
    *, existing_system: Any = None, current_model: str | None = None,
    provider: str = "bigmodel", cwd: str | None = None,
) -> list[dict[str, Any]]:
    """组装官方式 system 块（三块官方身份 + 调用方原有 system 追加在后）。"""
    data = _assets()
    stable = "\n\n".join(data["stableSections"])
    dynamic = "\n\n".join([
        data["dynamicSections"]["beforeEnvironment"],
        build_environment_section(cwd=cwd, model=current_model, provider=provider),
        data["dynamicSections"]["afterEnvironment"],
    ])
    official = [
        {"type": "text", "text": data["cliPrefix"], "cache_control": dict(EPHEMERAL)},
        {"type": "text", "text": stable, "cache_control": dict(EPHEMERAL)},
        {"type": "text", "text": f"\n\n{dynamic}", "cache_control": dict(EPHEMERAL)},
    ]
    return [*official, *normalize_system_blocks(existing_system)]


def build_context_prefix_block(now: datetime | None = None) -> dict[str, str]:
    """首轮 user 消息前挂的 currentDate 上下文（`<system-reminder>` 包裹）。"""
    data = _assets()
    cp = data["contextPrefix"]
    body = "\n".join([
        cp["intro"],
        f"{cp['currentDateHeading']}\n{cp['currentDateLine'].replace('{date}', _format_local_iso_date(now))}",
        "",
        cp["outro"],
    ])
    sr = data["systemReminder"]
    return {"type": "text", "text": f"{sr['open']}{body}{sr['close']}"}


def attach_context_prefix(messages: Any, now: datetime | None = None) -> Any:
    """把上下文前缀块插到首个 user 消息的 content 数组最前面（已存在则不动）。"""
    if not isinstance(messages, list) or not messages:
        return messages
    first = messages[0]
    if not isinstance(first, dict) or first.get("role") != "user":
        return messages
    content = first.get("content")
    content = content if isinstance(content, list) else [
        {"type": "text", "text": str(content or "")}
    ]
    marker = _assets()["systemReminder"]["open"]
    if any(isinstance(c, dict) and c.get("type") == "text"
           and isinstance(c.get("text"), str) and c["text"].startswith(marker)
           for c in content):
        return messages
    return [
        {**first, "content": [build_context_prefix_block(now), *content]},
        *messages[1:],
    ]
