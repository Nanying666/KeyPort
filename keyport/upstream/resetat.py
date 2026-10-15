"""上游「重置时间」的统一解析（三级来源，一个实现）。

## 为什么需要

限流类错误（6004 模型级限额 / 429 / WAF 403）的上游响应会**明说**恢复时刻，而
「冷却多久」有两个口径：上游给的时间（权威）与本地猜的（有界退避）。本地猜必然
不准 —— 猜短了会在窗口内反复撞墙（每次都是一次失败请求 + 一次风控痕迹），猜长了
会把健康号白扣几小时。

历史问题：一条**无 `Retry-After` 头、只把重置时间写在 body 里**的 6004，会被当成
「没有时间信息的普通软冷却」处理，而那是**账号级**冷却 —— 于是「6004 只冷却该
模型、切模型即可用」这个承诺在真实流量里根本不成立。

## 三级来源与优先级（顺序即权威性）

1. **body 文案** —— 上游在人话 msg 里直接写的恢复时刻（`将在 2026-10-15 10:00:00 重置`）。
   这是上游写给用户看的，最权威，且最便宜（一次正则）。
2. **body 结构化字段** —— `data.resets_at` 等。上游不保证发，但发了比文案更精确。
   放在文案之后是刻意的：文案是**已知存在**的形态，字段是**可能存在**的形态，
   而字段解析要扫全 body，代价更高 —— 把便宜的权威来源放前面。
3. **响应头** —— `Retry-After` / `Retry-After-Ms` / `X-Ratelimit-Reset`。语义是
   「多久后可以再试」，不带绝对时刻，精度最低，属兜底。

三级全不可用 → `NONE`：调用方回落本地有界退避，**绝不臆造时间**（臆造一个"看起来
正常"的时间会让排障时无法区分"上游说的"与"我们猜的"）。

## 时区（最容易错的地方）

上游文案里的墙钟是**上游本地时区 UTC+8**。把「无后缀墙钟」当 UTC 解析会整体偏
8 小时 —— 偏早 8 小时等于冷却几乎立刻到期（窗口内反复撞墙），偏晚 8 小时等于白扣
一个号半天。两种错都比"没有时间信息"更糟，所以显式固定 UTC+8，不交给本地时区。

    形态                              处理
    --------------------------------  ----------------------------------------
    2026-10-15 10:00:00（无后缀）      按 UTC+8 解释（上游文案形态）
    2026-10-15 10:00:00 UTC+8（有后缀） 剥掉后缀后同上
    2026-10-15T10:00:00+08:00          RFC3339：按自带偏移解析（最精确）
    2026-10-15T10:00:00Z               同上（UTC 时刻）
    1791972000 / "1791972000"          epoch 秒（时区无关）
    1791972000000                      epoch 毫秒（>=1e12）
    3600 / "3600"（< 1e8）              相对秒数 → now + 3600s

## 边界情况（三种，均有明确策略）

- **缺失**（`NONE`）：回落本地有界退避。不延长、不缩短。
- **已过期**（`EXPIRED`）：解析到了但不在未来（上游文案过期 / 本地时钟偏移）。
  **按"没有时间信息"处理**，而不是"立刻恢复" —— 立刻恢复会让同秒并发重试立刻再吃
  一次同样的错误（热循环），而有界退避最短也是秒级，足以让频控窗口滑过。
- **并发**：由 pool 侧收敛（取更晚的截止），绝不因并发响应乱序而缩短已有冷却。
"""

from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from typing import Any

# 上游 429 / 6004 文案里的重置时间固定按 UTC+8 解释（上游文案如此，与容器时区无关）
UPSTREAM_TZ = _dt.timezone(_dt.timedelta(hours=8))

# 上游重置时间的文案格式（无时区后缀；时区固定 UTC+8）
WALL_CLOCK_FORMAT = "%Y-%m-%d %H:%M:%S"

# 「将在 … 重置」的人话文案；捕获中间的时间串
_RE_RESET_CN = re.compile(r"将在 (.+?) 重置")
_RE_RESET_EN = re.compile(r"(?i)reset at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# ---- 来源标记（用于日志与台账，让排障时一眼看出"这个时长哪来的"）----

NONE = "none"              # 上游没给任何重置信息 → 回落本地有界退避
BODY_TEXT = "body-text"    # 来自 body 里「将在 … 重置」的人话文案（最权威）
BODY_JSON = "body-json"    # 来自 body 的结构化字段
HEADER = "header"          # 来自响应头（兜底）
EXPIRED = "expired"        # 解析到了但不在未来 → 视同"没有时间信息"

# 结构化字段候选键（snake / camel 双形态，与仓库其余解析器「snake 优先」一致）。
# 只收 reset 语义的键，刻意**不收** expires_at / expire_time 一类：那些词在上游
# 其它业务（包月周期、token 有效期）上大量出现，收进来会把「套餐到期」误读成
# 「限流恢复」。宁缺毋滥。
RESET_AT_FIELD_KEYS = (
    "resets_at", "reset_at", "reset_time",
    "resetsAt", "resetAt", "resetTime",
)

# 相对秒数的候选键（"多久以后"而不是"什么时刻"）。同样只收 reset 语义：
# retry_after 因为是 HTTP 头的同名形态而收；wait / sleep / delay 一类
# 并发/调度语义的词一律不收。
RESET_IN_FIELD_KEYS = (
    "resets_in", "reset_in", "reset_after", "retry_after",
    "resetsIn", "resetIn", "resetAfter", "retryAfter",
)

# 结构化字段的最大下钻层数（含顶层，即覆盖 root / root.data / root.error.data）。
# 取 3 而不是不限深度：上游信封是 {code,msg,data} 与 {error:{code,msg,data}} 两种
# 固定形态，三层足够且确定性可控；再深就是畸形体（偶尔夹带整段 HTML 拦截页）。
JSON_SCAN_DEPTH = 3

# 数字形态的语义分界（判定依据是**量级**而不是位数）：
#   < 1e8（≈1973 年）  相对秒数（"多久以后"）
#   < 1e12            绝对 epoch 秒
#   >= 1e12           绝对 epoch 毫秒
# 两个界既避开"把 3600 秒当成 1970 年"的荒谬，也避开"把毫秒当秒"（那会算出
# 一个 5 万年后才到期的冷却，等于永久摘除）。
EPOCH_SEC_THRESHOLD = 1e8
EPOCH_MS_THRESHOLD = 1e12

# 响应头形态的等待时长上限。取值 7 天：它的职责只是拒绝"明显不是时长"的荒谬值，
# 而不是截断 —— 截断由 pool 的软冷却封顶统一负责。不能沿用 2h：x-ratelimit-reset
# 是**绝对时刻**，一处 5 小时后的真实重置被丢掉就会退化成 60 秒冷却（反复撞墙）。
HEADER_SANITY_SECONDS = 7 * 24 * 3600

# 冷却时长优先解析的响应头候选（大小写不敏感）
RETRY_AFTER_HEADERS = ("retry-after", "retry-after-ms", "x-ratelimit-reset")


@dataclass(frozen=True)
class ResetAt:
    """解析结果。`epoch` 为 0 表示"没有可用的绝对时刻"。"""

    epoch: int = 0
    source: str = NONE

    @property
    def usable(self) -> bool:
        return self.epoch > 0

    @property
    def expired(self) -> bool:
        return self.source == EXPIRED


def _is_all_digits(s: str) -> bool:
    return bool(s) and all("0" <= ch <= "9" for ch in s)


def parse_wall_clock(text: str) -> int:
    """字符串墙钟 → epoch 秒；解析不了返回 0（时区规则见模块头）。"""
    s = (text or "").strip()
    if not s:
        return 0

    # 先试 RFC3339（自带偏移/Z，最精确）。上游若发这种形态，必须尊重它自带的偏移，
    # 不能按 UTC+8 二次解释。
    candidate = s.replace("Z", "+00:00")
    try:
        dt = _dt.datetime.fromisoformat(candidate)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UPSTREAM_TZ)
        return int(dt.timestamp())
    except ValueError:
        pass

    # 再试「无偏移但有 T 分隔」的 ISO 形态：上游只给本地墙钟而漏了偏移时，
    # 与文案同口径按 UTC+8 解释。
    try:
        dt = _dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=UPSTREAM_TZ)
        return int(dt.timestamp())
    except ValueError:
        pass

    # 最后是文案形态：剥掉可选的 " UTC+8" 后缀后按 UTC+8 解释
    ts = s.removesuffix(" UTC+8").strip()
    try:
        dt = _dt.datetime.strptime(ts, WALL_CLOCK_FORMAT).replace(tzinfo=UPSTREAM_TZ)
        return int(dt.timestamp())
    except ValueError:
        return 0


def parse_rate_reset(body: str | bytes | None) -> int:
    """从限流响应 body 里解析「将在 … 重置」时刻（上游 UTC+8 文案）；失败返回 0。

    上游发的是**原始 UTF-8**（中文不转义），所以先直接搜原文。但有些中间层会把
    JSON 重新序列化成 ASCII 转义形态（`\\u5c06\\u5728 ...`）—— 那时原文搜不到，
    再解码一次 JSON 后用未转义形态搜一遍。两步都失败才算"没有文案"。
    """
    if body is None:
        return 0
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    epoch = _match_reset_text(text)
    if epoch:
        return epoch
    # 回退：ASCII 转义形态
    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(decoded, (dict, list)):
        return 0
    return _match_reset_text(json.dumps(decoded, ensure_ascii=False))


def _match_reset_text(text: str) -> int:
    m = _RE_RESET_CN.search(text)
    if m is None:
        m = _RE_RESET_EN.search(text)
    if m is None:
        return 0
    return parse_wall_clock(m.group(1))


def parse_retry_after_headers(
    headers: Any, max_seconds: float = HEADER_SANITY_SECONDS, now: float | None = None,
) -> float:
    """从响应头解析上游明示的等待秒数；不可用返回 0。

    依次尝试 `Retry-After`（整数秒）→ `retry-after-ms`（整数毫秒）→
    `x-ratelimit-reset`（epoch 秒/毫秒）。**只认纯数字** —— 非纯数字（如
    HTTP-Date）不解析，宁缺毋滥。任一头缺失/非法/非正/超上限则尝试下一头。

    `now` 由调用方传入：`x-ratelimit-reset` 是绝对时刻，折算"还剩多久"必须与
    调用方其余时间判断共用同一个"现在"，否则同一份响应在不同时刻解析出不同结果。
    """
    if not headers:
        return 0.0

    def _get(name: str) -> str:
        for key in (name, name.title(), name.upper()):
            try:
                value = headers.get(key)
            except AttributeError:
                return ""
            if value:
                return str(value).strip()
        return ""

    if now is None:
        now = _dt.datetime.now(_dt.UTC).timestamp()
    for name in RETRY_AFTER_HEADERS:
        raw = _get(name)
        if not raw or not _is_all_digits(raw):
            continue
        try:
            number = int(raw)
        except ValueError:
            continue
        if number <= 0:
            continue
        if name == "retry-after-ms":
            seconds = number / 1000.0
        elif name == "x-ratelimit-reset":
            # 绝对时刻：按量级推断秒 / 毫秒口径，折算成"还剩多久"
            epoch = number / 1000.0 if number >= EPOCH_MS_THRESHOLD else float(number)
            seconds = max(0.0, epoch - now)
        else:
            seconds = float(number)
        if seconds <= 0 or (max_seconds > 0 and seconds > max_seconds):
            continue
        return seconds
    return 0.0


def _numeric_reset_at(value: float, now: float, relative: bool) -> int:
    """数字形态 → epoch 秒；不可用返回 0。"""
    if value <= 0:
        return 0
    if relative or value < EPOCH_SEC_THRESHOLD:
        # 相对秒数。上限 1e8 秒（≈3.17 年）纯属防呆：真出现这种值说明上游发的
        # 东西不是我们理解的那个语义，宁可丢弃回退有界退避。
        if value >= EPOCH_SEC_THRESHOLD:
            return 0
        return int(now + value)
    if value >= EPOCH_MS_THRESHOLD:
        return int(value / 1000.0)
    return int(value)


def _parse_reset_value(value: Any, now: float, relative: bool) -> int:
    """单个字段值 → epoch 秒；不可用返回 0。

    `relative` 表示该键的语义是「相对秒数」；此时数字一律按 now+n 解释，
    字符串只接受纯数字 —— 键名说的是"多久以后"，给个墙钟属于上游自相矛盾，
    宁缺毋滥地丢弃。
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, (int, float)):
        return _numeric_reset_at(float(value), now, relative)
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return 0
        if _is_all_digits(s):
            try:
                return _numeric_reset_at(float(int(s)), now, relative)
            except ValueError:
                return 0
        if relative:
            return 0
        return parse_wall_clock(s)
    return 0


def _scan_envelopes(root: dict[str, Any], depth: int) -> list[dict[str, Any]]:
    """产出「顶层 + 逐层下钻」的信封对象，供字段扫描。

    有界广度优先而不是递归：上游信封固定是 `{code,msg,data}` /
    `{error:{code,msg,data}}` 形态，且错误体偶尔夹带整段 HTML 拦截页。有界既覆盖
    真实形态，又不会在畸形/巨型 body 上失控。

    顺序必须**确定**（含子对象的键名排序）：不排序会让同一份 body 每次解析出不同
    的键 —— 表现是"偶发取不到重置时间"，最难查的一类缺陷。
    """
    out: list[dict[str, Any]] = []
    level = [root]
    for _ in range(depth):
        if not level:
            break
        out.extend(level)
        nxt: list[dict[str, Any]] = []
        for node in level:
            nxt.extend(_child_envelopes(node))
        level = nxt
    return out


def _child_envelopes(node: dict[str, Any]) -> list[dict[str, Any]]:
    """一个信封对象的直接子对象，按**确定顺序**返回。

    `data` / `error` / `result` 先（上游信封的既有三处），其余子对象按键名升序补足。
    """
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for key in ("data", "error", "result"):
        value = node.get(key)
        if isinstance(value, dict):
            out.append(value)
            seen.add(key)
    rest = sorted(
        k for k, v in node.items() if isinstance(v, dict) and k not in seen
    )
    out.extend(node[k] for k in rest)
    return out


def parse_reset_at_json(body: str | bytes | None, now: float) -> int:
    """从 body 的结构化字段里取重置时刻（epoch 秒）；取不到返回 0。"""
    if body is None:
        return 0
    text = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
    text = text.strip()
    if not text:
        return 0
    try:
        root = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(root, dict):
        return 0

    for node in _scan_envelopes(root, JSON_SCAN_DEPTH):
        for key in RESET_AT_FIELD_KEYS:
            if key not in node:
                continue
            epoch = _parse_reset_value(node[key], now, relative=False)
            if epoch:
                return epoch
        for key in RESET_IN_FIELD_KEYS:
            if key not in node:
                continue
            epoch = _parse_reset_value(node[key], now, relative=True)
            if epoch:
                return epoch
    return 0


def _usable(epoch: int, now: float, source: str) -> ResetAt:
    """把「解析到的时刻」收敛成对外契约：不在未来的一律判为过期。

    抽出来是因为三级来源都走这一关 —— 否则每级各写一遍"是否在未来"的判断，
    迟早有一级漏掉，而漏掉的那一级就是热循环的来源。
    """
    if epoch <= 0 or epoch <= now:
        return ResetAt(0, EXPIRED)
    return ResetAt(int(epoch), source)


def resolve_reset_at(
    body: str | bytes | None,
    headers: Any = None,
    now: float | None = None,
) -> ResetAt:
    """解析上游给出的重置时刻（三级来源，见模块头）。

    `epoch` 为 0 时（`NONE` / `EXPIRED`）调用方**必须**回落本地有界退避，
    不要拿零值去做时间运算。
    """
    import time as _time

    now = float(now if now is not None else _time.time())

    # 第 1 级：body 文案（最权威，且最便宜）
    epoch = parse_rate_reset(body)
    if epoch:
        return _usable(epoch, now, BODY_TEXT)

    # 第 2 级：body 结构化字段
    epoch = parse_reset_at_json(body, now)
    if epoch:
        return _usable(epoch, now, BODY_JSON)

    # 第 3 级：响应头（兜底）。上限的职责是拒绝荒谬值，截断由 pool 统一负责。
    wait = parse_retry_after_headers(headers, HEADER_SANITY_SECONDS, now)
    if wait > 0:
        return _usable(int(now + wait), now, HEADER)

    return ResetAt(0, NONE)
