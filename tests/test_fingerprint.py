"""每账号设备档案（成套桌面 SKU 指纹）测试。

背景：网关跑在 Linux 云主机上，若把 N 个账号都标成同一套「云内核 + 1920x1080」，
会在上游聚成「一台机房机器开了 N 个账号」的强关联信号。故入池时按账号分配
成套桌面 SKU（platform × arch × os_version × screen 绑定），一号一台设备。
"""

from __future__ import annotations

import uuid

import pytest

from keyport.upstream.base import AccountAuth
from keyport.upstream.fingerprint import (
    DeviceProfile,
    assign,
    derive_profile,
    from_dict,
    is_generated_sku,
    profile_for,
    random_profile,
    rotate,
    sku_combos,
    validate,
)


def _auth(uid: str = "u1", **kw) -> AccountAuth:
    return AccountAuth(uid=uid, realm="zai", access_token="tok", **kw)


# ---- 生成与自洽 ----


def test_random_profile_is_valid_sku():
    for _ in range(50):
        profile = random_profile()
        assert is_generated_sku(profile) is True
        assert profile.platform in ("darwin", "win32")
        uuid.UUID(profile.device_mid)


def test_generated_profiles_never_use_linux():
    """账号池不含 linux：官方桌面主形态是 Mac / Windows。"""
    platforms = {random_profile().platform for _ in range(200)}
    assert platforms <= {"darwin", "win32"}
    assert "linux" not in platforms


def test_screen_is_bound_to_platform():
    """分辨率必须与平台成套，禁止字段笛卡尔积产生的假电脑。

    `darwin-arm64 + 1366x768` 这类组合不存在于真实设备池。
    """
    for _ in range(200):
        profile = random_profile()
        assert (profile.platform, profile.arch, profile.os_version,
                profile.screen) in sku_combos()


def test_intel_mac_never_gets_new_darwin_kernel():
    """darwin 24+ 不再配 Intel x64（Sequoia/Tahoe 已不支持 Intel Mac）。"""
    for _ in range(300):
        profile = random_profile()
        if profile.arch == "x64" and profile.platform == "darwin":
            assert profile.os_version.startswith(("22.", "23."))


def test_language_timezone_pair_is_real():
    """语言 / 时区必须同源（禁止 de-DE + Asia/Tokyo 这类矛盾组合）。"""
    for _ in range(200):
        profile = random_profile()
        assert (profile.language, profile.timezone) in (
            ("zh-CN", "Asia/Shanghai"),
            ("en-US", "America/New_York"),
            ("en-US", "America/Los_Angeles"),
            ("en-GB", "Europe/London"),
            ("de-DE", "Europe/Berlin"),
            ("ja-JP", "Asia/Tokyo"),
            ("ko-KR", "Asia/Seoul"),
            ("en-SG", "Asia/Singapore"),
        )


def test_device_mid_is_fresh_uuid_v4():
    """device_mid 每次全新 UUIDv4，跨账号不复用。"""
    mids = {random_profile().device_mid for _ in range(100)}
    assert len(mids) == 100
    for mid in mids:
        assert uuid.UUID(mid).version == 4


def test_os_category_mapping():
    assert DeviceProfile("darwin", "arm64", "24.5.0", "zh-CN",
                         "Asia/Shanghai", "1512x982").os_category == "macos"
    assert DeviceProfile("win32", "x64", "10.0.22631", "zh-CN",
                         "Asia/Shanghai", "1920x1080").os_category == "windows"


def test_platform_full_shape():
    profile = DeviceProfile("darwin", "arm64", "24.5.0", "zh-CN",
                            "Asia/Shanghai", "1512x982")
    assert profile.platform_full == "darwin-arm64"


# ---- 稳定派生（profile_for 兜底路径）----


def test_derive_profile_is_stable_per_uid():
    """同 uid 派生必须幂等：auth_for 每次都新建 AccountAuth，随机生成会
    让同一账号每次请求换设备 —— 那正是要避免的关联信号。"""
    a = derive_profile("user-1")
    b = derive_profile("user-1")
    assert a == b
    assert a.device_mid == b.device_mid


def test_derive_profile_differs_across_uids():
    assert derive_profile("user-1").device_mid != derive_profile("user-2").device_mid


def test_derive_profile_is_valid_sku():
    for i in range(50):
        assert is_generated_sku(derive_profile(f"u{i}")) is True


def test_profile_for_is_stable_without_persisted_fingerprint():
    """未落库时按 uid 稳定派生（跨调用恒定）。"""
    auth = _auth("stable-uid")
    first = profile_for(auth)
    second = profile_for(_auth("stable-uid"))
    assert first.device_mid == second.device_mid


def test_profile_for_uses_existing_dict_fingerprint():
    """已落库的档案必须被采用，而不是重新派生。"""
    profile = random_profile()
    auth = _auth("u1", fingerprint=profile.to_dict())
    got = profile_for(auth)
    assert got == profile


def test_profile_for_ignores_corrupt_fingerprint():
    """损坏档案回落派生，绝不抛异常。"""
    auth = _auth("u1", fingerprint={"device_mid": "not-a-uuid"})
    got = profile_for(auth)
    assert is_generated_sku(got) is True


def test_from_dict_rejects_incomplete():
    assert from_dict(None) is None
    assert from_dict({}) is None
    assert from_dict({"device_mid": "x"}) is None


def test_from_dict_roundtrips():
    profile = random_profile()
    assert from_dict(profile.to_dict()) == profile


# ---- 入池分配与换发 ----


def test_assign_sets_fingerprint_on_auth():
    auth = _auth("u1")
    profile = assign(auth)
    assert auth.fingerprint is profile
    assert is_generated_sku(profile) is True


def test_rotate_changes_device_mid():
    """换发必换 device_mid（风控后换设备语义）。"""
    auth = _auth("u1")
    first = assign(auth)
    second = rotate(auth)
    assert first.device_mid != second.device_mid
    assert is_generated_sku(second) is True


def test_rotate_keeps_profile_valid_across_many_times():
    auth = _auth("u1")
    mids = set()
    for _ in range(30):
        profile = rotate(auth)
        assert is_generated_sku(profile) is True
        mids.add(profile.device_mid)
    assert len(mids) == 30


# ---- 校验门 ----


def test_invalid_screen_rejected():
    profile = DeviceProfile("darwin", "arm64", "24.5.0", "zh-CN", "Asia/Shanghai", "abc")
    with pytest.raises(ValueError):
        validate(profile)


def test_invalid_device_mid_rejected():
    profile = DeviceProfile("darwin", "arm64", "24.5.0", "zh-CN", "Asia/Shanghai",
                            "1512x982", device_mid="not-a-uuid")
    with pytest.raises(ValueError):
        validate(profile)


def test_bogus_locale_rejected():
    profile = DeviceProfile("darwin", "arm64", "24.5.0", "de-DE", "Asia/Tokyo", "1512x982")
    with pytest.raises(ValueError):
        validate(profile)


def test_non_sku_combo_rejected():
    """字段笛卡尔积产生的假电脑必须被校验门挡掉。"""
    profile = DeviceProfile("darwin", "arm64", "24.5.0", "zh-CN", "Asia/Shanghai", "1366x768")
    with pytest.raises(ValueError):
        validate(profile)


def test_host_real_allows_linux_cloud_shape():
    """宿主机采集路径必须放行 linux 云内核形态（诊断用，非入池源）。"""
    profile = DeviceProfile("linux", "x64", "6.8.0-45-generic", "en-US", "UTC", "1920x1080")
    validate(profile, host_real=True)
