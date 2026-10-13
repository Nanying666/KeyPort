"""农场 UA 版本策略：无头必须用固定版本，不能跟随本机真实版本。

回归背景：`_desktop_ua` 曾自动跟随本机 Chrome 真实版本（探测到 153），
而阿里云无痕 SDK 对「声明版本与无头指纹一致」的浏览器直接判 F011。
实测矩阵（本机 Chrome 实际 153）：

    headless + UA 141  → 5/5 成功
    headless + UA 153  → 5/5 F011
    headed   + UA 153  → 1/3 成功，之后 F011

所以无头刻意用固定 `FARM_UA_MAJOR`；有头才用真实版本
（有头写死旧版本会与真实浏览器不符 → 上游 3012）。
"""

from __future__ import annotations

from keyport.upstream.captcha import (
    FARM_UA_MAJOR,
    _desktop_ua,
)

# 本机可能存在的真实浏览器路径（不存在则跳过对应断言）
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"


def test_headless_ua_uses_fixed_major():
    """无头 UA 必须是固定版本，与本机真实版本无关。"""
    ua = _desktop_ua(CHROME, headless=True)
    assert f"Chrome/{FARM_UA_MAJOR}.0.0.0" in ua
    assert "HeadlessChrome" not in ua


def test_headless_ua_ignores_detected_version(monkeypatch):
    """即使能探测到真实版本，无头也不应采用它。"""
    monkeypatch.setattr(
        "keyport.upstream.captcha._chrome_major", lambda _p: "153")
    ua = _desktop_ua(CHROME, headless=True)
    assert "Chrome/153" not in ua
    assert f"Chrome/{FARM_UA_MAJOR}" in ua


def test_headed_ua_prefers_real_version(monkeypatch):
    """有头模式应用真实版本（避免与浏览器不符被判 3012）。"""
    monkeypatch.setattr(
        "keyport.upstream.captcha._chrome_major", lambda _p: "153")
    ua = _desktop_ua(CHROME, headless=False)
    assert "Chrome/153.0.0.0" in ua


def test_headed_ua_falls_back_when_undetectable(monkeypatch):
    """探测不到版本时有头回退到固定值，不抛异常。"""
    monkeypatch.setattr(
        "keyport.upstream.captcha._chrome_major", lambda _p: None)
    ua = _desktop_ua(CHROME, headless=False)
    assert f"Chrome/{FARM_UA_MAJOR}.0.0.0" in ua


def test_ua_is_wellformed():
    ua = _desktop_ua(CHROME, headless=True)
    assert ua.startswith("Mozilla/5.0 (")
    assert "AppleWebKit/537.36" in ua
    assert "Safari/537.36" in ua
