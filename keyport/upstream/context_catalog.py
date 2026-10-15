"""context_length / max_output_tokens 的字段级静态兜底知识表。

## 为什么需要

下游客户端（Codex / ZCode / Claude Code）**按 `context_length` 提前截断**。
上游 `maxInputTokens` 为 0 或缺失时，若网关透出假值（例如硬编码 131072），
大窗口模型会被**白白丢掉上下文**。

取舍原则是「**宁可高估不低估**」：

- 高估的代价 = 客户端不截断、上游报错可重试；
- 低估的代价 = 丢上下文（用户看不见、且不可恢复）。

`max_output_tokens` 未知则**省略字段** —— 输出上限没有「安全侧」，不编造。

## 查找链（优先级从高到低）

1. **上游动态值**（`maxInputTokens` / `maxOutputTokens`）—— 权威，优先；
2. **本文件静态知识表**（远端零值时补齐）；
3. `DefaultContextWindow`（1M）—— 知识表也未收录时的兜底。

知识表**一处定义、两域共用**：`context_length` 是模型固有属性（CN 与国际版是同一套
API 的两次部署，同 id 上下文一致），无按 realm 分表的必要。

## 值的来源

逐条标注来源：`实测` = 直连上游 `/console/enterprises/personal/models` 的
`maxInputTokens`；`models.dev` = models.dev 收录的共识值；`估算` = 同族外推。
**未收录或歧义大的一律不编造**（落到 1M 兜底 / 省略字段）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 知识表也未收录的模型的 context_length 兜底：1M。
# 上游多数大窗口模型的实际量级；高估优于低估（见模块头）。
DEFAULT_CONTEXT_WINDOW = 1_000_000


@dataclass(frozen=True)
class ContextCap:
    """一个模型的上下文能力。

    `context` 必为正；`max_output` 为 0 表示输出上限未知 → 省略该字段（不编造）。
    """

    context: int
    max_output: int = 0


# 知识表（两域共用）。每条标注来源。
CONTEXT_FALLBACK: dict[str, ContextCap] = {
    # ---- GLM 家族（z-ai）----
    "glm-5.2": ContextCap(1_000_000, 131_072),
    "glm-5.1": ContextCap(200_000, 131_072),
    "glm-5.3": ContextCap(1_000_000, 131_072),
    "glm-5.3-flash": ContextCap(1_000_000, 131_072),
    "glm-5v-turbo": ContextCap(200_000, 131_072),

    # ---- Kimi 家族（moonshot）----
    "kimi-k2.7": ContextCap(256_000, 65_536),
    "kimi-k2.6": ContextCap(256_000, 262_144),
    "kimi-k2.5": ContextCap(164_000, 262_144),
    "kimi-k3": ContextCap(1_048_576, 131_072),
    "kimi-k2.8-preview": ContextCap(1_048_576, 0),   # 输出上限未收录 → 省略

    # ---- MiniMax / 混元（tencent）----
    "minimax-m3": ContextCap(512_000, 512_000),
    "hy3": ContextCap(192_000, 64_000),
    "hy3-preview": ContextCap(262_144, 64_000),
    "hy4-preview": ContextCap(1_000_000, 64_000),
    "hy4-preview-x": ContextCap(1_000_000, 64_000),

    # ---- DeepSeek 家族 ----
    "deepseek-v4-pro": ContextCap(1_000_000, 384_000),
    "deepseek-v4-flash": ContextCap(1_000_000, 384_000),
    "deepseek-v4.1-flash": ContextCap(1_000_000, 384_000),

    # ---- OpenAI / Google（国际版家族）----
    "gpt-6-astra": ContextCap(1_050_000, 128_000),
    "gpt-5.6-sol": ContextCap(1_050_000, 128_000),
    "gpt-5.6-terra": ContextCap(1_050_000, 128_000),
    "gpt-5.6-luna": ContextCap(1_050_000, 128_000),
    "gpt-5.5": ContextCap(1_050_000, 128_000),
    "gpt-5.4": ContextCap(1_050_000, 128_000),
    "gpt-5.3-codex": ContextCap(400_000, 128_000),
    "gemini-3.5-flash": ContextCap(1_048_576, 65_536),

    # ---- 国际版路由别名 ----
    "auto": ContextCap(168_000, 0),                   # 输出上限未知 → 省略
}


def context_window(model: str, remote: int | None = None) -> int:
    """模型在 `/v1/models` 的 `context_length`。

    上游值 > 0 时权威；否则查知识表；仍未收录 → `DEFAULT_CONTEXT_WINDOW`（1M）。
    **绝不再透出假值**（例如硬编码 131072）—— 那会让客户端白白截断上下文。
    """
    if isinstance(remote, int) and remote > 0:
        return remote
    cap = CONTEXT_FALLBACK.get(_key(model))
    if cap is not None and cap.context > 0:
        return cap.context
    return DEFAULT_CONTEXT_WINDOW


def max_output_tokens(model: str, remote: int | None = None) -> int | None:
    """模型在 `/v1/models` 的 `max_output_tokens`；未知返回 `None`（调用方省略字段）。

    与 `context_window` 的 1M 兜底**刻意不同**：输出上限没有「宁可高估」的安全侧，
    未知即省略，不编造。
    """
    if isinstance(remote, int) and remote > 0:
        return remote
    cap = CONTEXT_FALLBACK.get(_key(model))
    if cap is not None and cap.max_output > 0:
        return cap.max_output
    return None


def _key(model: str) -> str:
    """知识表键归一化：上游模型名大小写敏感，但目录里可能出现大写形态。"""
    return str(model or "").strip().lower()
