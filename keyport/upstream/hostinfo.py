"""宿主机真实指纹采集 —— 诊断 / 测试用，**不是**账号入池默认源。

账号身份走 `fingerprint.random_profile` 的成套桌面 SKU（一号一台生成设备）。
本模块只描述部署机事实（Linux 云内核、无显示器 1920x1080 等），供运维对照；
**禁止**再把它当作多账号共用的上游身份（那会形成「一台机房机器开了 N 个账号」
的关联信号）。

采集项 ↔ DeviceProfile 字段：

  platform    platform.system()   → darwin / win32 / linux（官方 process.platform 语义）
  arch        platform.machine()  → arm64 / x64（官方 process.arch 语义）
  os_version  os.release() 同源；Windows 上 release() 只给 marketing 版本
              （"10"/"11"），归一为 sys.getwindowsversion 的内核形态 "10.0.xxxxx"
  timezone    /etc/localtime → IANA 名：符号链接反解；实体文件则与
              /usr/share/zoneinfo 字节比对；都失败退 UTC
  language    $LANG（zh_CN.UTF-8 → zh-CN；缺失退 en-US）
  screen      本机无显示器（服务器形态）→ 官方桌面端必有屏幕，取 FALLBACK_SCREEN
  device_mid  本模块不生成 —— DeviceProfile 缺省工厂给每次采集全新 UUID
"""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path

# 服务器无显示器时的兜底分辨率（官方桌面端激活事件必有 screen_resolution）
FALLBACK_SCREEN = "1920x1080"

# $LANG 形态解析：语言主码（2-3 位小写）+ 可选地区码
_LANG_RE = re.compile(r"^([a-z]{2,3})(?:[_-]([A-Za-z]{2,4}))?")

# 单语言标签 → 默认地区（$LANG 无地区段时使用）
_DEFAULT_REGION = {
    "zh": "CN", "en": "US", "ja": "JP", "ko": "KR",
    "de": "DE", "fr": "FR", "es": "ES", "ru": "RU",
}


def _resolve_timezone() -> str:
    """IANA 时区名（官方客户端读系统时区）。

    解析顺序：
      1. /etc/localtime 符号链接路径反解（darwin / 多数 Linux 发行版）
      2. 实体文件时与 /usr/share/zoneinfo 逐文件字节比对取唯一匹配
      3. 都失败退 UTC
    """
    path = Path("/etc/localtime")
    try:
        target = os.path.realpath(path)
        parts = Path(target).parts
        for i, seg in enumerate(parts):
            if seg == "zoneinfo" and i + 2 < len(parts):
                return f"{parts[i + 1]}/{parts[i + 2]}"
    except OSError:
        pass
    try:
        content = path.read_bytes()
    except OSError:
        return "UTC"
    zoneinfo_dir = Path("/usr/share/zoneinfo")
    if not zoneinfo_dir.is_dir():
        return "UTC"
    candidates: list[str] = []
    for f in zoneinfo_dir.rglob("*"):
        if not f.is_file() or f.suffix or f.is_symlink():
            continue  # 二进制 TZif 无后缀；跳过链接（posix/RIGHT 别名）与非数据文件
        rel = f.relative_to(zoneinfo_dir).as_posix()
        if rel.startswith(("Etc/", "posix/", "right/")) or rel in ("posixrules", "leapseconds"):
            continue
        if not rel[0].isupper():
            continue  # 真实地名以大写开头；纯缩写文件（CST 等）不作候选
        try:
            if f.read_bytes() == content:
                candidates.append(rel)
        except OSError:
            continue
    if not candidates:
        return "UTC"
    # Area/City 两段式优先于单段，字典序稳定输出
    candidates.sort(key=lambda r: (0 if "/" in r else 1, r))
    return candidates[0]


def _resolve_language() -> str:
    """语言标签：$LANG 的 zh_CN.UTF-8 形态转 zh-CN；无 Locale 环境退 en-US。

    Linux 容器常为 C / C.UTF-8（无语言信息），此时与官方常见默认 en-US 对齐。
    """
    raw = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").strip()
    m = _LANG_RE.match(raw)
    if not m:
        return "en-US"
    lang, region = m.group(1), m.group(2)
    if not region:
        return f"{lang}-{_DEFAULT_REGION.get(lang, 'US')}"
    return f"{lang}-{region.upper()}"


def _normalize_arch(machine: str) -> str:
    """process.arch 语义：arm64 / x64；其余按 64 位推断为 x64。"""
    m = (machine or "").lower()
    if "arm" in m or "aarch" in m:
        return "arm64"
    return "x64"


def _normalize_platform(system: str) -> str:
    return {"darwin": "darwin", "windows": "win32", "linux": "linux"}.get(
        (system or "").lower(), "linux")


def _windows_kernel_version() -> str | None:
    """Windows 内核形态 os_version（"10.0.22000"）。非 Windows 返回 None。

    platform.release() 在 Windows 只给 marketing 版本号（"10"/"11"），
    通不过 fingerprint 的内核形态门；sys.getwindowsversion 的
    major.minor.build 与官方 Windows 档案（10.0.xxxxx）同形。
    """
    try:
        wv = sys.getwindowsversion()
    except AttributeError:
        return None
    return f"{wv.major}.{wv.minor}.{wv.build}"


def _host_os_version() -> str:
    """os_version（os.release() 同源语义，供 X-Os-Version）。"""
    if platform.system().lower() == "windows":
        kernel = _windows_kernel_version()
        if kernel:
            return kernel
    return platform.release() or "0.0"


def collect_host_profile():
    """采集本机真实设备档案（每次调用重采，落库由调用方负责）。"""
    from .fingerprint import DeviceProfile

    return DeviceProfile(
        platform=_normalize_platform(platform.system()),
        arch=_normalize_arch(platform.machine()),
        os_version=_host_os_version(),
        language=_resolve_language(),
        timezone=_resolve_timezone(),
        screen=FALLBACK_SCREEN,
    )
