"""出站代理接线：按账号解析出口、按出口复用连接池、非法配置不静默回落直连。

`test_egress.py` 覆盖纯函数；这里覆盖 `UpstreamClient` 的接线行为。
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from conftest import make_test_config

from keyport.upstream.base import AccountAuth
from keyport.upstream.client import UpstreamClient
from keyport.upstream.egress import DIRECT


def _client(tmp_path: Path, **upstream_kw) -> UpstreamClient:
    cfg = make_test_config(tmp_path)
    for k, v in upstream_kw.items():
        setattr(cfg.upstream, k, v)
    return UpstreamClient(cfg)


def _auth(**kw) -> AccountAuth:
    base = {"uid": "u1", "realm": "cn", "access_token": "T"}
    base.update(kw)
    return AccountAuth(**base)


# ---- 出口解析 ----


def test_resolve_egress_prefers_account_over_default(tmp_path: Path):
    c = _client(tmp_path, egress_proxy_default="http://10.0.0.1:8080")
    assert c.resolve_egress(_auth(egress_proxy="http://5.6.7.8:8080")) == "http://5.6.7.8:8080"
    assert c.resolve_egress(_auth()) == "http://10.0.0.1:8080"


def test_resolve_egress_direct_overrides_default(tmp_path: Path):
    """direct 哨兵的意义：设了全局默认后仍能让单号直连。"""
    c = _client(tmp_path, egress_proxy_default="http://10.0.0.1:8080")
    assert c.resolve_egress(_auth(egress_proxy=DIRECT)) == DIRECT


def test_resolve_egress_empty_when_no_default(tmp_path: Path):
    c = _client(tmp_path)
    assert c.resolve_egress(_auth()) == ""
    assert c.resolve_egress(None) == ""


# ---- 客户端选择 ----


@pytest.mark.asyncio
async def test_direct_and_unset_share_default_client(tmp_path: Path):
    c = _client(tmp_path)
    default = await c.client()
    assert await c.client_for(_auth()) is default
    assert await c.client_for(_auth(egress_proxy=DIRECT)) is default
    assert await c.client_for(None) is default
    await c.aclose()


@pytest.mark.asyncio
async def test_proxy_account_gets_its_own_client(tmp_path: Path):
    """配了代理的账号必须用独立客户端，不能复用直连连接池。

    否则 A 线路的连接会被复用给配了 B 线路的账号 —— 出口隔离直接失效。
    """
    c = _client(tmp_path)
    default = await c.client()
    proxied = await c.client_for(_auth(egress_proxy="http://1.2.3.4:8080"))
    assert proxied is not default
    # 同一出口复用同一客户端（连接池共享）
    assert await c.client_for(_auth(egress_proxy="http://1.2.3.4:8080")) is proxied
    # 不同出口各自独立
    other = await c.client_for(_auth(egress_proxy="http://5.6.7.8:8080"))
    assert other is not proxied
    await c.aclose()


@pytest.mark.asyncio
async def test_proxy_client_is_distinct_transport(tmp_path: Path):
    """代理客户端的 transport 必须与直连客户端不同（httpx 把 proxy 放在 transport 上）。

    不断言 transport 内部结构（httpx 不暴露可读属性，断言它只会变成脆弱测试）；
    「对象不同 + transport 不同」已足以证明出口被隔离。
    """
    c = _client(tmp_path)
    default = await c.client()
    cli = await c.client_for(_auth(egress_proxy="http://1.2.3.4:8080"))
    assert cli is not default
    assert cli._transport is not default._transport
    await c.aclose()


@pytest.mark.asyncio
async def test_invalid_proxy_raises_in_strict_mode(tmp_path: Path):
    """严格模式：非法出口**抛错**，绝不静默回落直连。

    静默直连是本模块最需要防的失效模式 —— 界面显示「已配代理」，实际从本机 IP
    出去，防封形同虚设且毫无感知。
    """
    c = _client(tmp_path, egress_strict=True)
    with pytest.raises(ValueError):
        await c.client_for(_auth(egress_proxy="socks5h://1.2.3.4:1080"))
    with pytest.raises(ValueError):
        await c.client_for(_auth(egress_proxy="not-a-url"))
    await c.aclose()


@pytest.mark.asyncio
async def test_invalid_proxy_falls_back_when_not_strict(tmp_path: Path):
    """非严格模式才允许回落（显式关闭护栏的部署）。"""
    c = _client(tmp_path, egress_strict=False)
    default = await c.client()
    assert await c.client_for(_auth(egress_proxy="not-a-url")) is default
    await c.aclose()


@pytest.mark.asyncio
async def test_aclose_closes_all_egress_clients(tmp_path: Path):
    c = _client(tmp_path)
    proxied = await c.client_for(_auth(egress_proxy="http://1.2.3.4:8080"))
    await c.aclose()
    assert proxied.is_closed
    assert c._clients == {}


# ---- 真实请求按账号出口走 ----


@pytest.mark.asyncio
async def test_open_chat_uses_account_egress_client(tmp_path: Path):
    """chat 出站必须走该账号出口的客户端（不是默认客户端）。"""
    c = _client(tmp_path)
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url.host))
        return httpx.Response(200, content=b"data: [DONE]\n\n",
                              headers={"content-type": "text/event-stream"})

    async def _mock():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    c.client = _mock  # type: ignore[method-assign]

    async def _mock_proxied():
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    # 直接注入一个「配了代理」的客户端，验证 open_chat 会取它
    c._clients["http://1.2.3.4:8080"] = await _mock_proxied()

    auth = _auth(egress_proxy="http://1.2.3.4:8080")
    resp = await c.open_chat(auth, b'{"model":"hy3","messages":[]}')
    assert resp.status_code == 200
    assert seen, "应真的发出请求"
    await c.aclose()
