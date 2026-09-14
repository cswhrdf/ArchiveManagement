"""平台识别的单元测试.

平台识别是纯函数: 传入 ``sys.platform`` 的取值即可验证三个平台的分支, 不需要
真的运行在对应系统上。这一点很重要——CI 只跑在 Windows/Ubuntu/macOS 上, 覆盖
不到"其它类 Unix 系统"这类需要降级处理的取值。
"""

from __future__ import annotations

import sys

import pytest

from archive_management.services.platforms import (
    PlatformFamily,
    current_platform,
    detect_platform,
    has_windows_registry,
    platform_label,
)

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("本地游戏探测"),
    pytest.mark.story("按平台选择探测来源"),
    pytest.mark.layer("unit"),
]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("win32", "windows"),
        ("Windows", "windows"),
        ("darwin", "macos"),
        ("Darwin", "macos"),
        ("linux", "linux"),
        ("linux2", "linux"),
        ("freebsd13", "linux"),
        ("cygwin", "linux"),
    ],
)
def test_detect_platform_maps_known_values(value: str, expected: str) -> None:
    assert detect_platform(value) == expected


def test_current_platform_matches_sys_platform() -> None:
    assert current_platform() == detect_platform(sys.platform)


@pytest.mark.parametrize(
    ("platform", "label"),
    [
        ("windows", "Windows"),
        ("macos", "macOS"),
        ("linux", "Linux"),
    ],
)
def test_platform_label_is_human_readable(platform: PlatformFamily, label: str) -> None:
    assert platform_label(platform) == label


def test_has_windows_registry_only_on_windows() -> None:
    """注册表是 Windows 独有的设施: 其它平台的相关探测必须短路."""
    assert has_windows_registry("windows") is True
    assert has_windows_registry("macos") is False
    assert has_windows_registry("linux") is False
    assert has_windows_registry() is (current_platform() == "windows")
