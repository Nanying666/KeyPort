"""出站代理（出口线路）：格式校验、直连白名单、真实出口 IP 探测。

## 为什么需要单独一层

出站代理如果只是一个自由文本输入框、保存时不校验、使用时不验证，就会出现一个
**很隐蔽的失效模式**：代理 URL 填错一个字符，账号就静默回落直连，而界面上仍显示
「已配代理」。管理员以为在做 IP 隔离，实际所有账号都从本机 IP 出去，防封形同虚设
且毫无感知。

本模块提供三道闸：

1. `validate_proxy` —— 保存前的格式校验（协议 / 主机 / 端口），把错误挡在配置阶段。
2. `bypass` —— 本机 / 内网目标**不经代理**。代理一旦接管所有出站请求，健康探测、
   本机回调、局域网端点都会被打进代理，表现成「本地端点不可达」，且探测结论会
   与真实出口不一致，排障时被自己的探测结果误导。
3. `probe` —— 用该代理实际发一次请求，取回上游看到的**真实出口 IP**。

## 出口解析是三态，别误判「裸奔」

- `"direct"`  显式直连（覆盖全局默认）
- `""`        未指定，且全局默认也为空 → 实际是直连（语义上是「未指定」）
- `"http://…"` 生效的代理 URL

`"direct"` 哨兵的存在是因为出口是**两级配置**（账号级优先，为空则继承全局默认）。
没有它的话，「留空」的真实语义是「跟随全局默认」—— 一旦设了全局默认代理，就再也
没有办法让某个账号单独直连。
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import time
from dataclasses import dataclass
from urllib.parse import urlparse

# 直连哨兵：显式要求不走代理（覆盖全局默认）
DIRECT = "direct"

# 支持的代理协议。刻意不支持 socks5h —— httpx 的 socks 支持需要额外依赖，
# 且「由本地解析域名」与远程解析的语义差异容易踩坑，提前拒绝并给出可操作提示。
SUPPORTED_SCHEMES = ("http", "https", "socks5")

# 出口 IP 探测端点候选（按「国内可达性优先」排序）。
# 多个候选 + 逐个尝试：这些第三方回显服务没有 SLA，随时可能被墙或下线；单端点会让
# 「端点挂了」看起来像「代理坏了」，进而导致无意义的排查。
PROBE_ENDPOINTS = (
    "https://myip.ipip.net/s",
    "https://api.ip.sb/ip",
    "https://api.ipify.org",
    "https://ifconfig.me/ip",
    "https://ipinfo.io/ip",
)

_IPV4_RE = re.compile(r"\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\b")

# 单次探测请求超时。代理链路多一次握手，过短会误判失败；过长会让「保存时校验」
# 这种交互路径卡住。
PROBE_TIMEOUT = 6.0

# 单次探活的网络总预算。探活要先把「探测端点本身是否可达」试出来（逐个试候选），
# 不做总量限制的话，受限网络下 5 个端点各卡满超时就是 30 秒以上。超预算即收敛为
# 「无法验证」—— 这比继续干等更有用。
PROBE_BUDGET = 12.0

# 内网 / 保留命名空间后缀（RFC 6761 / 6762）
_BYPASS_SUFFIXES = (
    ".localhost", ".local", ".internal", ".lan", ".home", ".home.arpa", ".test", ".example",
)

# 容器 / 云环境里的内网别名（常见写法，非 IP 故不能靠网段判定）
_BYPASS_HOSTS = frozenset({
    "localhost",
    "host.docker.internal",
    "gateway.docker.internal",
    "host.containers.internal",
    "kubernetes.default",
    "kubernetes.default.svc",
    "metadata.google.internal",
    "instance-data",
    "host.internal",
})

# 运营商级 NAT 段 100.64.0.0/10：不是 RFC1918，但同样不是公网出口
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def is_direct(value: str | None) -> bool:
    """账号级出口配置是否显式要求直连。"""
    return (value or "").strip().lower() == DIRECT


def validate_proxy(raw: str | None) -> str:
    """校验并规范化代理 URL；非法抛 ValueError，空串合法（= 有意直连）。

    错误信息面向管理员，要能直接指出问题（是协议不认、还是漏了端口），
    否则「保存失败」四个字无法指导修复。
    """
    s = (raw or "").strip()
    if not s:
        return ""

    # 先挡最常见的笔误：只写了 host:port 没写协议。
    # urlparse 会把 "1.2.3.4:8080" 解析成 scheme="1.2.3.4"，报错会变得莫名其妙。
    if "://" not in s:
        return _raise(
            f"代理地址缺少协议前缀，请写成 http://{s} 或 socks5://{s}"
        )

    try:
        u = urlparse(s)
    except ValueError as exc:
        raise ValueError(f"代理 URL 无法解析：{exc}") from exc

    scheme = (u.scheme or "").lower()
    if scheme == "socks5h":
        raise ValueError("不支持 socks5h，请改用 socks5（由本地解析域名）")
    if scheme not in SUPPORTED_SCHEMES:
        raise ValueError(
            f"不支持的代理协议 {u.scheme!r}（支持 {' / '.join(SUPPORTED_SCHEMES)}）"
        )
    if not u.hostname:
        raise ValueError("代理 URL 缺少主机名")
    try:
        port = u.port
    except ValueError as exc:
        # urlparse 对超范围端口直接抛 ValueError（且是英文文案），这里统一成
        # 面向管理员的中文提示 —— 「Port out of range 0-65535」对站长没有指导性
        raise ValueError(f"代理 URL 的端口不合法（应为 1-65535）：{exc}") from exc
    if port is None:
        raise ValueError(f"代理 URL 缺少端口（如 {scheme}://{u.hostname}:8080）")
    if not (1 <= port <= 65535):
        raise ValueError(f"端口 {port!r} 不合法（应为 1-65535）")
    return s


def validate_account_egress(raw: str | None) -> str:
    """校验**账号级**出口配置，返回规范化值。

    与 `validate_proxy` 的区别：额外接受 `direct` 哨兵，空串仍合法（= 未指定，
    跟随全局默认）。单开一个函数而不是放宽 `validate_proxy`：后者也用于代理池
    录入，池里存 `direct` 毫无意义。
    """
    if is_direct(raw):
        return DIRECT
    # 常见误写：想直连却写成 none/null/- 之类。直接拒绝并给出正确写法，
    # 比默默当成非法代理（然后账号被判出口异常）更友好。
    lowered = (raw or "").strip().lower()
    if lowered in ("none", "null", "nil", "off", "no", "false", "-", "direct://"):
        raise ValueError(
            f"如需直连请填 {DIRECT!r}（当前填的 {(raw or '').strip()!r} 不是合法代理，"
            f"也不是直连标记）"
        )
    return validate_proxy(raw)


def _raise(msg: str) -> str:
    raise ValueError(msg)


def bypass(host: str | None) -> bool:
    """该主机是否**不应**走出口代理（True = 直连）。纯函数，可单测。

    host 可带端口、IPv6 可带方括号，都会被归一后再判定。判定不了的普通域名返回
    False —— **不**把未知一律直连，否则「代理没生效」会变成一个比原问题更难查的故障。
    """
    h = (host or "").strip()
    if not h:
        # 空主机名没有可用信息；宁可不加代理，避免把畸形请求丢给代理
        return True

    # 去端口：有端口则取主机部分
    if h.startswith("["):            # IPv6 字面量 [::1]:8080
        end = h.find("]")
        if end > 0:
            h = h[1:end]
    elif h.count(":") == 1:          # host:port
        h = h.split(":", 1)[0]

    # 去尾点：`printer.local.` 是 FQDN 的根形式，与 `printer.local` 等价
    low = h.lower().rstrip(".")

    if low in _BYPASS_HOSTS:
        return True
    if any(low.endswith(s) for s in _BYPASS_SUFFIXES):
        return True

    try:
        ip = ipaddress.ip_address(low)
    except ValueError:
        return False  # 普通域名：交给代理

    if (ip.is_loopback or ip.is_private or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified or ip.is_reserved):
        return True
    return ip.version == 4 and ip in _CGNAT


def should_proxy(host: str | None) -> bool:
    """与 `bypass` 互反，供调用方按语义取用（避免到处写 not bypass）。"""
    return not bypass(host)


@dataclass
class ProbeResult:
    """一次出口探测的结果。失败本身是有信息量的结论，不是异常。"""

    ok: bool = False
    ip: str = ""
    error: str = ""
    endpoint: str = ""
    elapsed_ms: int = 0
    # 直连也无法访问任何探测端点 → 无法判定代理好坏。
    # 调用方应显示「未能验证」而不是「代理不可用」（容器/受限网络下很常见）。
    endpoint_unreachable: bool = False


# 记住「哪个端点是直连可达的」，避免每次探测都先直连试一轮。
# 只缓存成功结果：一旦确认某端点可用就直用它；若它后来失效，代理探测会失败并
# 回落到重新挑选。
_reachable_cache: dict[str, object] = {"url": "", "at": 0.0}
_REACHABLE_TTL = 600.0


async def _client_for(proxy_url: str):
    import httpx

    kwargs: dict[str, object] = {
        "timeout": httpx.Timeout(PROBE_TIMEOUT),
        "follow_redirects": False,
    }
    if (proxy_url or "").strip():
        norm = validate_proxy(proxy_url)
        kwargs["proxy"] = norm
    return httpx.AsyncClient(**kwargs)


async def _fetch_ip(client, endpoint: str) -> str:
    resp = await client.get(endpoint, headers={"User-Agent": "curl/8.0"})
    if resp.status_code != 200:
        raise RuntimeError(f"端点返回 HTTP {resp.status_code}")
    # 限 4KB：回显服务响应都很小，超出的必然是异常内容
    body = resp.text[:4096]
    m = _IPV4_RE.search(body)
    if m is None:
        raise RuntimeError("端点响应中未找到 IP")
    return m.group(1)


async def _endpoint_reachable(client, endpoint: str) -> bool:
    """该端点是否可达（失败只表示这个端点不可用，继续试下一个）。"""
    try:
        await _fetch_ip(client, endpoint)
    except Exception:  # noqa: BLE001 - 第三方回显服务没有 SLA，随时可能被墙/下线
        return False
    return True


async def _pick_endpoint(start: float) -> str:
    """选一个直连可达的探测端点；全部不可达（或超总预算）返回空串。"""
    now = time.monotonic()
    if _reachable_cache["url"] and (now - float(_reachable_cache["at"])) < _REACHABLE_TTL:
        return str(_reachable_cache["url"])

    try:
        direct = await _client_for("")
    except ValueError:
        return ""
    try:
        for ep in PROBE_ENDPOINTS:
            # 预算耗尽就收手：继续试只会让调用方干等，而结论（无法验证）已经确定
            if time.monotonic() - start > PROBE_BUDGET:
                return ""
            if not await _endpoint_reachable(direct, ep):
                continue
            _reachable_cache["url"] = ep
            _reachable_cache["at"] = time.monotonic()
            return ep
    finally:
        await direct.aclose()
    return ""


async def probe(proxy_url: str = "") -> ProbeResult:
    """通过 `proxy_url` 探测真实出口 IP；空串探测本机直连出口。

    关键设计是区分两类失败（否则会把「探测端点不可达」误判成「代理坏了」）：

    1. 先直连访问一次探测端点。连直连都不通 → 端点不可达，结论是「无法验证」，
       不归咎代理（容器 / 受限网络下这是常见情形）。
    2. 直连通、走代理不通 → 才是代理真的有问题。
    """
    start = time.monotonic()

    def _finish(endpoint: str, ip: str, err: Exception | None) -> ProbeResult:
        r = ProbeResult(endpoint=endpoint, ip=(ip or "").strip(),
                        elapsed_ms=int((time.monotonic() - start) * 1000))
        if err is not None:
            r.error = str(err)
            return r
        r.ok = True
        return r

    try:
        client = await _client_for(proxy_url)
    except ValueError as exc:
        # 格式非法：不必浪费网络请求
        return ProbeResult(error=str(exc),
                           elapsed_ms=int((time.monotonic() - start) * 1000))

    try:
        endpoint = await _pick_endpoint(start)
        if not endpoint:
            return ProbeResult(
                endpoint_unreachable=True,
                error="探测端点均不可达（本机出网受限），无法验证出口 IP",
                elapsed_ms=int((time.monotonic() - start) * 1000),
            )
        if not (proxy_url or "").strip():
            # 直连探测时该端点就是最终结果，无需再绕一圈
            try:
                return _finish(endpoint, await _fetch_ip(client, endpoint), None)
            except Exception as exc:  # noqa: BLE001
                return _finish(endpoint, "", exc)
        # 走代理探测。刻意**不**在失败后换端点重试：_pick_endpoint 已证明该端点
        # 直连可达，那么「走代理不通」的原因只可能在代理侧；换端点既改变不了结论，
        # 又会让单次探活耗时成倍增长。
        try:
            return _finish(endpoint, await _fetch_ip(client, endpoint), None)
        except Exception as exc:  # noqa: BLE001
            return _finish(endpoint, "", exc)
    finally:
        await client.aclose()


async def probe_many(urls: list[str], concurrency: int = 6) -> dict[str, ProbeResult]:
    """并发探活多条线路（供面板批量探活用）。同一条 URL 只探一次。"""
    uniq = list(dict.fromkeys(u for u in urls if (u or "").strip()))
    out: dict[str, ProbeResult] = {}
    if not uniq:
        return out
    sem = asyncio.Semaphore(max(1, concurrency))

    async def _one(u: str) -> None:
        async with sem:
            out[u] = await probe(u)

    await asyncio.gather(*(_one(u) for u in uniq))
    return out


def resolved_egress(own: str | None, default: str | None) -> str:
    """解析账号**最终生效**的出口配置（见模块头「三态」）。

    - 显式 `direct` → `"direct"`
    - 账号自配代理 → 该 URL
    - 未指定 → 全局默认（可能为空 = 直连）
    """
    if is_direct(own):
        return DIRECT
    own_s = (own or "").strip()
    if own_s:
        return own_s
    return (default or "").strip()


def build_client_kwargs(egress_value: str | None) -> dict[str, object]:
    """把生效出口配置转成 httpx 的构造参数。

    `direct` 与空串都产出**不带 proxy** 的参数；区别在于 `direct` 是显式的强保证
    （调用方据此绕开环境代理），空串表示「未指定」。
    """
    value = (egress_value or "").strip()
    if not value or is_direct(value):
        return {}
    return {"proxy": value}
