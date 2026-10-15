"""出站代理：格式校验、直连白名单、出口三态解析。

移植自 sheng-du0521/workbuddy2api 的 `internal/egress/{egress,bypass}.go`。
"""

from __future__ import annotations

import pytest

from keyport.upstream.egress import (
    DIRECT,
    build_client_kwargs,
    bypass,
    is_direct,
    resolved_egress,
    should_proxy,
    validate_account_egress,
    validate_proxy,
)

# ---- 格式校验 ----


@pytest.mark.parametrize("raw,want", [
    ("", ""),
    ("   ", ""),
    ("http://1.2.3.4:8080", "http://1.2.3.4:8080"),
    ("https://proxy.example.com:3128", "https://proxy.example.com:3128"),
    ("socks5://127.0.0.1:1080", "socks5://127.0.0.1:1080"),
    ("  http://1.2.3.4:8080  ", "http://1.2.3.4:8080"),
])
def test_validate_proxy_ok(raw, want):
    assert validate_proxy(raw) == want


@pytest.mark.parametrize("raw,needle", [
    ("1.2.3.4:8080", "缺少协议前缀"),
    ("socks5h://1.2.3.4:1080", "不支持 socks5h"),
    ("ftp://1.2.3.4:21", "不支持的代理协议"),
    ("http://1.2.3.4", "缺少端口"),
    ("http://", "缺少主机名"),
    ("http://1.2.3.4:99999", "不合法"),
])
def test_validate_proxy_rejects(raw, needle):
    """错误信息必须能直接指出问题，否则「保存失败」四个字无法指导修复。"""
    with pytest.raises(ValueError, match=needle):
        validate_proxy(raw)


def test_validate_account_egress_accepts_direct_sentinel():
    assert validate_account_egress("direct") == DIRECT
    assert validate_account_egress("  DIRECT  ") == DIRECT
    assert validate_account_egress("") == ""
    assert validate_account_egress("http://1.2.3.4:8080") == "http://1.2.3.4:8080"


@pytest.mark.parametrize("raw", ["none", "null", "nil", "off", "no", "false", "-", "direct://"])
def test_validate_account_egress_rejects_pseudo_direct(raw):
    """想直连却写成 none/null 之类：直接拒绝并给出正确写法。

    默默当成非法代理会让账号被判「出口异常」，反而更难查。
    """
    with pytest.raises(ValueError, match="如需直连请填"):
        validate_account_egress(raw)


# ---- 直连白名单 ----


@pytest.mark.parametrize("host", [
    "localhost", "localhost:8080", "LocalHost",
    "host.docker.internal", "host.docker.internal:7863",
    "gateway.docker.internal",
    "kubernetes.default.svc",
    "metadata.google.internal",
    "instance-data",
    "printer.local", "printer.local.",
    "foo.internal", "bar.lan", "x.home.arpa", "svc.test",
    "127.0.0.1", "127.0.0.1:7863", "::1", "[::1]:7863",
    "10.0.0.5", "192.168.1.3", "172.16.0.1",
    "169.254.1.1",            # link-local
    "100.64.0.1",             # CGNAT（不是 RFC1918，但同样不是公网出口）
    "0.0.0.0",
    "224.0.0.1",              # 组播
    "",
])
def test_bypass_true(host):
    assert bypass(host) is True
    assert should_proxy(host) is False


@pytest.mark.parametrize("host", [
    "www.workbuddy.ai",
    "copilot.tencent.com",
    "api.z.ai",
    "8.8.8.8",
    "1.1.1.1",
    "2001:4860:4860::8888",
    "100.63.255.255",         # CGNAT 边界外
    "100.128.0.1",            # CGNAT 边界外
])
def test_bypass_false(host):
    """普通域名 / 公网 IP 交给代理。

    不把未知一律直连 —— 否则「代理没生效」会变成比原问题更难查的故障。
    """
    assert bypass(host) is False
    assert should_proxy(host) is True


# ---- 出口三态 ----


def test_resolved_egress_three_states():
    # 显式直连覆盖全局默认
    assert resolved_egress(DIRECT, "http://1.2.3.4:8080") == DIRECT
    # 账号自配优先
    assert resolved_egress("http://5.6.7.8:8080", "http://1.2.3.4:8080") == "http://5.6.7.8:8080"
    # 未指定 → 继承全局默认
    assert resolved_egress("", "http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    # 未指定且默认也为空 → 实际直连（语义上是「未指定」）
    assert resolved_egress("", "") == ""
    assert resolved_egress(None, None) == ""


def test_direct_sentinel_matters_when_global_default_set():
    """这正是 direct 哨兵存在的理由。

    出口是两级配置：一旦设了全局默认代理，「留空」的语义就变成「走默认代理」，
    此时没有任何办法让某个账号单独直连 —— 除非有 direct 这个一等状态。
    """
    assert resolved_egress("", "http://1.2.3.4:8080") == "http://1.2.3.4:8080"
    assert resolved_egress(DIRECT, "http://1.2.3.4:8080") == DIRECT


def test_is_direct():
    assert is_direct("direct") and is_direct(" DIRECT ")
    assert not is_direct("") and not is_direct("http://1.2.3.4:8080")


def test_build_client_kwargs():
    assert build_client_kwargs("http://1.2.3.4:8080") == {"proxy": "http://1.2.3.4:8080"}
    assert build_client_kwargs("") == {}
    assert build_client_kwargs(DIRECT) == {}
    assert build_client_kwargs(None) == {}
