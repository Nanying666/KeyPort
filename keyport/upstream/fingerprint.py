"""每账号客户端设备档案（指纹）—— 入池生成高可信桌面 SKU，一号一台设备。

## 为什么

网关跑在 Linux 云主机上，若把 N 个账号都标成同一套「云内核 + 1920x1080」，
会在上游聚成「一台机房机器开了 N 个账号」的强关联信号。官方客户端是
Electron 桌面端（darwin-arm64 / win32-x64 为主），账号身份必须长得像用户电脑。

## 生成规则

- 从**成套 SKU 表**抽样（platform × arch × os_version × screen 绑定），
  禁止字段笛卡尔积（`darwin-arm64 + 1366x768` 这类不存在的电脑）。
- 账号池不含 linux：官方桌面主形态是 Mac / Windows。
- 语言 / 时区取真实地区对，避免 `de-DE + Asia/Tokyo` 这类矛盾组合。
- `device_mid` 每次全新 UUIDv4，跨账号不复用。
- 同账号档案一经分配即稳定；`rotate()` 换一整台 SKU（含新 device_mid）。

`host_profile()` 仍采集部署机，只给诊断/测试用，**不作为入池默认源**。
"""

from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass, field, replace

# 成套桌面 SKU：(weight, platform, arch, os_version, screen)
# 分辨率只取官方桌面端常见值，并与平台绑定（Mac 逻辑分辨率不配 Windows）。
_SKUS: tuple[tuple[int, str, str, str, str], ...] = (
    # Apple silicon MacBook Air/Pro 13–14"（darwin 24 = Sequoia，25 = Tahoe）
    (10, "darwin", "arm64", "24.5.0", "1512x982"),
    (10, "darwin", "arm64", "24.6.0", "1512x982"),
    (8, "darwin", "arm64", "24.5.0", "1728x1117"),
    (8, "darwin", "arm64", "24.6.0", "1728x1117"),
    (8, "darwin", "arm64", "25.5.0", "1512x982"),
    (6, "darwin", "arm64", "25.5.0", "1728x1117"),
    (5, "darwin", "arm64", "23.6.0", "1512x982"),
    (4, "darwin", "arm64", "23.6.0", "1728x1117"),
    (4, "darwin", "arm64", "24.5.0", "2560x1440"),
    (3, "darwin", "arm64", "24.6.0", "2560x1600"),
    (2, "darwin", "arm64", "25.5.0", "2560x1440"),
    (2, "darwin", "arm64", "24.5.0", "3840x2160"),
    # Intel Mac 存量（Ventura/Sonoma；darwin 24+ 不再配 x64）
    (2, "darwin", "x64", "23.6.0", "1920x1080"),
    (2, "darwin", "x64", "22.6.0", "1440x900"),
    (1, "darwin", "x64", "23.6.0", "2560x1440"),
    # Windows 11 主流 + 少量 Win10
    (8, "win32", "x64", "10.0.22631", "1920x1080"),
    (7, "win32", "x64", "10.0.26100", "1920x1080"),
    (5, "win32", "x64", "10.0.22631", "2560x1440"),
    (4, "win32", "x64", "10.0.26200", "1920x1080"),
    (3, "win32", "x64", "10.0.26100", "2560x1440"),
    (3, "win32", "x64", "10.0.22621", "1920x1080"),
    (2, "win32", "x64", "10.0.22631", "3840x2160"),
    (2, "win32", "x64", "10.0.19045", "1920x1080"),
    (1, "win32", "x64", "10.0.19045", "1366x768"),
    (1, "win32", "x64", "10.0.26100", "2560x1600"),
    (1, "win32", "x64", "10.0.22000", "1920x1080"),
)

_SKU_POOL: tuple[tuple[str, str, str, str], ...] = tuple(
    (platform, arch, os_version, screen)
    for weight, platform, arch, os_version, screen in _SKUS
    for _ in range(weight)
)
_SKU_COMBOS = frozenset(_SKU_POOL)

# host_real 校验仍认这些平台取值（部署机可能是 linux）
_HOST_PLATFORMS = ("darwin", "win32", "linux")
_HOST_ARCHS = ("arm64", "x64")

# 语言-时区真实地区组合（X-Client-Language ↔ X-Client-Timezone 必须同源）
_LOCALES = (
    ("zh-CN", "Asia/Shanghai"),
    ("en-US", "America/New_York"),
    ("en-US", "America/Los_Angeles"),
    ("en-GB", "Europe/London"),
    ("de-DE", "Europe/Berlin"),
    ("ja-JP", "Asia/Tokyo"),
    ("ko-KR", "Asia/Seoul"),
    ("en-SG", "Asia/Singapore"),
)

_SCREEN_RE = re.compile(r"^\d{3,4}x\d{3,4}$")
# os.release() 形态门（host_real 放行用）：主版本.次版本[.修订] + 可选后缀
_RELEASE_SHAPE = re.compile(r"^\d+\.\d+(\.\d+)?[\w.\-]*$")


@dataclass(frozen=True)
class DeviceProfile:
    """单套客户端设备档案（值直接映射上游身份头 / 事件字段）。"""

    platform: str          # X-Platform 前半：darwin / win32 / linux
    arch: str              # arm64 / x64
    os_version: str        # X-Os-Version（os.release() 语义）
    language: str          # X-Client-Language
    timezone: str          # X-Client-Timezone（IANA）
    screen: str            # 激活事件 screen_resolution
    device_mid: str = field(default_factory=lambda: str(uuid.uuid4()))

    @property
    def platform_full(self) -> str:
        return f"{self.platform}-{self.arch}"

    @property
    def os_category(self) -> str:
        """X-Os-Category：darwin→macos / win32→windows / 其它→linux。"""
        if self.platform in ("darwin", "macos"):
            return "macos"
        if self.platform in ("win32", "windows"):
            return "windows"
        return "linux"

    def to_dict(self) -> dict[str, str]:
        return {
            "platform": self.platform,
            "arch": self.arch,
            "os_version": self.os_version,
            "language": self.language,
            "timezone": self.timezone,
            "screen": self.screen,
            "device_mid": self.device_mid,
        }


def sku_combos() -> frozenset[tuple[str, str, str, str]]:
    """生成器允许的 (platform, arch, os_version, screen) 成套组合。"""
    return _SKU_COMBOS


def is_generated_sku(profile: DeviceProfile) -> bool:
    """档案是否为入池用的高可信桌面 SKU。"""
    return (profile.platform, profile.arch, profile.os_version, profile.screen) in _SKU_COMBOS


def validate(profile: DeviceProfile, *, host_real: bool = False) -> None:
    """合规校验：档案内部自洽（成套 SKU / 真机形态、地区对、分辨率、UUID）。

    `host_real=True` 只用于宿主机采集：平台按取值形态放行（linux/arm64 云主机等
    真实形态），os_version 放宽为内核版本形态。账号生成路径走严格 SKU 门。

    刻意不做成 `DeviceProfile.__post_init__`：宿主机采集必须先构造出 linux /
    云内核形态的档案（`hostinfo`），构造期强校验会把那条诊断路径也挡掉。
    """
    if host_real:
        if profile.platform not in _HOST_PLATFORMS or profile.arch not in _HOST_ARCHS:
            raise ValueError(f"非法平台形态: {profile.platform_full}")
        if not _RELEASE_SHAPE.match(profile.os_version):
            raise ValueError(f"os_version 与平台不符: {profile.platform}/{profile.os_version}")
    else:
        if not is_generated_sku(profile):
            raise ValueError(
                f"非桌面 SKU: {profile.platform_full}/{profile.os_version}/{profile.screen}"
            )
        if (profile.language, profile.timezone) not in _LOCALES:
            raise ValueError(f"语言/时区组合不真实: {profile.language}/{profile.timezone}")
    if not _SCREEN_RE.match(profile.screen):
        raise ValueError(f"分辨率形态非法: {profile.screen}")
    uuid.UUID(profile.device_mid)  # 必须是合法 UUID


# 兼容内部调用点的旧名
_validate = validate


def random_profile() -> DeviceProfile:
    """随机生成一份成套桌面 SKU 档案（生成时自校验）。"""
    platform, arch, os_version, screen = secrets.choice(_SKU_POOL)
    language, timezone = secrets.choice(_LOCALES)
    profile = DeviceProfile(
        platform=platform,
        arch=arch,
        os_version=os_version,
        language=language,
        timezone=timezone,
        screen=screen,
        device_mid=str(uuid.uuid4()),
    )
    _validate(profile)
    return profile


def from_dict(data: dict | None) -> DeviceProfile | None:
    """从 dict 还原档案；缺字段/非法返回 None（调用方据此重新分配）。"""
    if not isinstance(data, dict) or not data.get("device_mid"):
        return None
    try:
        profile = DeviceProfile(
            platform=str(data["platform"]),
            arch=str(data["arch"]),
            os_version=str(data["os_version"]),
            language=str(data["language"]),
            timezone=str(data["timezone"]),
            screen=str(data["screen"]),
            device_mid=str(data["device_mid"]),
        )
    except KeyError:
        return None
    try:
        _validate(profile)
    except (ValueError, TypeError):
        return None
    return profile


def profile_for(auth) -> DeviceProfile:
    """取账号档案；无则按 uid 稳定派生（幂等，不写库）。

    派生而非随机：`auth_for` 每次都新建 AccountAuth 对象，若此处随机生成，
    同一账号每次请求的设备身份都会变 —— 那正是要避免的关联信号。
    持久化由 pool.upsert_account / ensure_fingerprint 负责。
    """
    existing = getattr(auth, "fingerprint", None)
    if isinstance(existing, DeviceProfile):
        return existing
    restored = from_dict(existing) if isinstance(existing, dict) else None
    if restored is not None:
        auth.fingerprint = restored
        return restored
    profile = derive_profile(getattr(auth, "uid", "") or "anonymous")
    auth.fingerprint = profile
    return profile


def derive_profile(seed: str) -> DeviceProfile:
    """按 uid 稳定派生一份成套 SKU 档案（同 uid 幂等，跨重启恒定）。

    用于「档案尚未落库」的兜底路径：保证同一账号在持久化之前也不会
    每次请求换设备，同时不产生随机噪声。
    """
    import hashlib

    digest = hashlib.sha256(f"kp:device:{seed}".encode("utf-8")).digest()
    platform, arch, os_version, screen = _SKU_POOL[int.from_bytes(digest[0:4], "big") % len(_SKU_POOL)]
    language, timezone = _LOCALES[int.from_bytes(digest[4:8], "big") % len(_LOCALES)]
    mid = uuid.UUID(bytes=digest[8:24], version=4)
    profile = DeviceProfile(
        platform=platform, arch=arch, os_version=os_version,
        language=language, timezone=timezone, screen=screen,
        device_mid=str(mid),
    )
    _validate(profile)
    return profile


def assign(auth) -> DeviceProfile:
    """入池分配档案：成套高可信桌面 SKU + 全新 device_mid（一号一台设备）。"""
    profile = random_profile()
    auth.fingerprint = profile
    return profile


def rotate(auth) -> DeviceProfile:
    """换发全新档案（device_mid 必变；风控后换设备语义）。

    换发换一整台成套 SKU（平台/内核/屏幕都可能变）+ 新 device_mid。
    """
    return assign(auth)


def host_profile(device_mid: str | None = None) -> DeviceProfile:
    """宿主机真实档案（诊断/测试用，不是入池默认源）。"""
    from . import hostinfo

    profile = hostinfo.collect_host_profile()
    if device_mid:
        profile = replace(profile, device_mid=device_mid)
    _validate(profile, host_real=True)
    return profile
