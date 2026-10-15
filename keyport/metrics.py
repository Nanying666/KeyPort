"""Prometheus 文本格式指标导出（`GET /metrics`）。

## 设计要点

- **零第三方依赖**：手写 Prometheus 0.0.4 text exposition。
- **零新增累加器**：所有指标在 scrape 时刻**现算** —— 账号池维度走只读快照，
  请求维度读 `request_logs` 聚合，任务维度读既有 `task_runs` 台账。不维护第二份
  状态，因此不存在双写一致性风险。
- **零新增上游请求**：全部读进程内 / 本地库，scrape 不触发任何网络调用。
- **确定性输出**：模型按字典序、realm / state 按固定序 —— 同一状态下两次抓取
  **逐字节相等**（否则看板 diff 会抖）。
- **不暴露高基数 / 敏感维度**：不加 per-uid 标签（泄漏 uid 且基数不可控）；
  逐账号台账由管理台提供。输出不含凭证、token、会话键。
"""

from __future__ import annotations

import math
from typing import Any

# 导出顺序固定（顺序写死而非遍历 dict：随机序会让两次抓取输出不同）
PROM_REALMS = (("cn", "cn"), ("global", "global"), ("all", ""))

# 账号池 state 维度固定顺序。状态是**原因分解而非互斥分类**：熔断/降权号同时
# 计入 cooling；manual_disabled 是 disabled 的子集。
PROM_POOL_STATES = (
    "healthy", "cooling", "disabled", "in_flight_full",
    "breaker", "degraded", "manual_disabled", "model_cooled",
)

# 任务名固定顺序
PROM_TASK_KINDS = ("checkin", "travel", "activity", "keepalive", "cat")


def escape_help(text: str) -> str:
    """HELP 文本转义：反斜杠与换行（Prometheus 规范只要求这两个）。"""
    return text.replace("\\", "\\\\").replace("\n", "\\n")


def escape_label(value: str) -> str:
    """label 值转义：反斜杠、双引号、换行。

    模型名可能来自客户端（未知模型名也会进 metrics 键），含引号/反斜杠时不转义
    会写出非法 exposition（解析失败 → **整个 scrape 被丢弃**）。
    """
    return (value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n"))


def format_value(value: float) -> str:
    """数值格式化。

    非有限值按 Prometheus 约定输出 NaN/+Inf/-Inf —— 虽然上游已做零分母守卫，
    这里仍防御，避免任何未来路径写坏整个 scrape。
    """
    if isinstance(value, bool):
        return "1" if value else "0"
    if math.isnan(value):
        return "NaN"
    if math.isinf(value):
        return "+Inf" if value > 0 else "-Inf"
    if float(value).is_integer():
        return str(int(value))
    return repr(float(value))


class PromWriter:
    """极简 text exposition 写入器。

    `seen` 保证每个指标家族恰好写一次 HELP/TYPE —— Prometheus 解析器对重复的
    HELP/TYPE 会报错，而渲染是「家族 → 逐样本」的循环顺序（池维度在外层、
    state 维度在内层），没有去重就会重复写。
    """

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._seen: set[str] = set()

    def family(self, name: str, help_text: str, metric_type: str) -> None:
        """写 HELP/TYPE；同家族重复调用为空操作（幂等）。"""
        if name in self._seen:
            return
        self._seen.add(name)
        self._lines.append(f"# HELP {name} {escape_help(help_text)}")
        self._lines.append(f"# TYPE {name} {metric_type}")

    def sample(self, name: str, value: float, **labels: str) -> None:
        """写一条样本；labels 为空则不输出花括号。"""
        if labels:
            pairs = ",".join(
                f'{k}="{escape_label(str(v))}"' for k, v in labels.items())
            self._lines.append(f"{name}{{{pairs}}} {format_value(value)}")
        else:
            self._lines.append(f"{name} {format_value(value)}")

    def render(self) -> str:
        return "\n".join(self._lines) + "\n"


def _field(health: Any, name: str) -> int:
    """从健康快照取值：兼容 dict（`pool.metrics_health`）与对象（`alert.Health`）。"""
    if health is None:
        return 0
    if isinstance(health, dict):
        return int(health.get(name, 0) or 0)
    return int(getattr(health, name, 0) or 0)


def _state_value(health: Any, state: str) -> int:
    """把健康快照的字段按 state 标签取值。"""
    return _field(health, state)


def render_metrics(
    *,
    health: dict[str, Any],
    totals: dict[str, int] | None = None,
    models: list[dict] | None = None,
    sticky_sessions: int = 0,
    waf_active: bool = False,
    tasks: dict[str, dict] | None = None,
    since: int = 0,
) -> str:
    """生成完整 exposition 文本。

    纯函数（无 IO、无时间读取），便于测试用固定输入做逐行断言。
    """
    w = PromWriter()
    totals = totals or {}
    models = models or []
    tasks = tasks or {}

    # ---------- 账号池 ----------
    w.family("keyport_pool_accounts_total",
             "账号池内该 realm 的账号总数（realm=all 为全池）。", "gauge")
    for label, _realm in PROM_REALMS:
        w.sample("keyport_pool_accounts_total", _field(health.get(label), "total"),
                 realm=label)

    w.family("keyport_pool_accounts",
             "账号池按 realm 与状态分解的账号数。状态维度是原因分解而非互斥分类："
             "熔断/降权号同时计入 cooling；manual_disabled 是 disabled 的子集；"
             "model_cooled 与账号级健康正交（该号对触发模型不可用、对其他模型仍可选）。",
             "gauge")
    for label, _realm in PROM_REALMS:
        h = health.get(label)
        for state in PROM_POOL_STATES:
            w.sample("keyport_pool_accounts", _state_value(h, state),
                     realm=label, state=state)

    w.family("keyport_pool_in_flight", "该 realm 各账号在途请求数之和。", "gauge")
    for label, _realm in PROM_REALMS:
        w.sample("keyport_pool_in_flight", _field(health.get(label), "in_flight"),
                 realm=label)

    w.family("keyport_sticky_sessions",
             "当前会话粘性绑定数（粘性未启用时恒 0）。", "gauge")
    w.sample("keyport_sticky_sessions", int(sticky_sessions))

    w.family("keyport_waf_ip_block_active",
             "IP 级 WAF 拦截是否处于激活期（1=激活）。进程内状态，重启清零。",
             "gauge")
    w.sample("keyport_waf_ip_block_active", 1 if waf_active else 0)

    # ---------- 定时任务台账 ----------
    #
    # 只为**跑过至少一轮**的任务输出序列，不补 0 时刻的占位序列：从未跑过的任务
    # 没有序列，而不是「时刻为 0」—— 否则服务刚启动 / 台账刚清空时，
    # `time() - keyport_task_last_run_timestamp_seconds > 86400` 这类告警会在每次
    # 部署时误报一轮。反过来，某类任务跑过之后停跑，它的 ts 会一直变旧，告警照常
    # 触发 —— 这正是要抓的情形。
    ran = [k for k in PROM_TASK_KINDS if k in tasks]
    if ran:
        w.family("keyport_task_last_run_timestamp_seconds",
                 "各类定时任务最近一轮的结束时刻（Unix 秒）。序列缺失表示该类任务"
                 "尚未跑过，不是「时刻为 0」。", "gauge")
        for kind in ran:
            w.sample("keyport_task_last_run_timestamp_seconds",
                     int(tasks[kind].get("finished") or 0), kind=kind)

        w.family("keyport_task_last_run_accounts",
                 "各类定时任务最近一轮的计数分解：total=本轮涉及量，ok=做成，"
                 "fail=上游报错，skipped=有意跳过（禁用号/无凭证/global/门槛未达）。",
                 "gauge")
        for kind in ran:
            for key in ("total", "ok", "fail", "skipped"):
                w.sample("keyport_task_last_run_accounts",
                         int(tasks[kind].get(key) or 0), kind=kind, result=key)

        w.family("keyport_task_last_run_all_failed",
                 "各类定时任务最近一轮是否「全灭」（有失败且没有任何账号做成，1=是）。"
                 "判据刻意不是失败率：还有账号成功就说明上游是通的。", "gauge")
        for kind in ran:
            w.sample("keyport_task_last_run_all_failed",
                     1 if tasks[kind].get("all_failed") else 0, kind=kind)

    # ---------- 请求统计 ----------
    w.family("keyport_stats_since_timestamp_seconds",
             "请求统计聚合的起点（进程启动或上次重置）的 Unix 秒。", "gauge")
    w.sample("keyport_stats_since_timestamp_seconds", int(since or 0))

    w.family("keyport_requests_total", "网关累计处理的请求数。", "counter")
    w.sample("keyport_requests_total", int(totals.get("success") or 0),
             status="success")
    w.sample("keyport_requests_total", int(totals.get("failed") or 0),
             status="failed")

    # 模型按字典序（稳定输出）
    ordered = sorted(models, key=lambda m: str(m.get("model", "")))

    w.family("keyport_model_requests_total", "按模型与结果累计的请求数。", "counter")
    for m in ordered:
        model = str(m.get("model", ""))
        w.sample("keyport_model_requests_total", int(m.get("success") or 0),
                 model=model, status="success")
        w.sample("keyport_model_requests_total", int(m.get("failed") or 0),
                 model=model, status="failed")

    w.family("keyport_model_tokens_total",
             "按模型与类型累计的 token 数（仅采信上游 usage，缺失不计）。", "counter")
    for m in ordered:
        model = str(m.get("model", ""))
        w.sample("keyport_model_tokens_total", int(m.get("prompt_tokens") or 0),
                 model=model, type="prompt")
        w.sample("keyport_model_tokens_total",
                 int(m.get("completion_tokens") or 0), model=model, type="completion")

    w.family("keyport_model_credit_total", "按模型累计的上游积分消耗。", "counter")
    for m in ordered:
        w.sample("keyport_model_credit_total", float(m.get("credits") or 0),
                 model=str(m.get("model", "")))

    w.family("keyport_model_avg_ttfb_seconds",
             "按模型的首字节平均耗时（仅成功且有观测的请求计入分母）。", "gauge")
    for m in ordered:
        w.sample("keyport_model_avg_ttfb_seconds",
                 float(m.get("avg_ttfb_ms") or 0) / 1000.0,
                 model=str(m.get("model", "")))

    return w.render()
