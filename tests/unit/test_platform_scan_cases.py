"""注册表读取器的枚举与失败收敛.

``WinRegistry`` 是探测来源里唯一真正接触系统 API 的地方, 约定是"任何失败都当作
没有": 键不存在、权限不足、值类型不对都不能让整次扫描失败。这里用假的
``winreg`` 模块验证枚举循环与错误收敛, 因此在三个平台上都能跑。
"""

from __future__ import annotations

from typing import Any

import pytest

from archive_management.services.platform_scan import WinRegistry

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("游戏探测"),
    pytest.mark.story("注册表读取"),
    pytest.mark.layer("unit"),
]


class _FakeKey:
    """可作上下文管理器使用的假键句柄."""

    def __enter__(self) -> str:
        """进入上下文时返回占位句柄."""
        return "key"

    def __exit__(self, *_exc: object) -> None:
        """退出上下文不吞异常."""


class _FakeWinReg:
    """只实现 ``WinRegistry`` 用到的最小 ``winreg`` 表面."""

    HKEY_CURRENT_USER = 1
    HKEY_LOCAL_MACHINE = 2

    def __init__(self, keys: list[object]) -> None:
        self.keys = keys
        self.opened: list[tuple[int, str]] = []

    def OpenKey(self, root: int, subkey: str) -> _FakeKey:  # noqa: N802 - 对齐 winreg
        """打开键; 特定子键模拟"键不存在"."""
        if subkey == "missing":
            raise OSError("没有这个键")
        self.opened.append((root, subkey))
        return _FakeKey()

    def EnumKey(self, _key: object, index: int) -> object:  # noqa: N802 - 对齐 winreg
        """按序返回子键名, 越界时抛 ``OSError``(真实 winreg 的行为)."""
        if index >= len(self.keys):
            raise OSError("枚举结束")
        return self.keys[index]

    def EnumValue(  # noqa: N802 - 对齐 winreg 的命名
        self, _key: object, index: int
    ) -> tuple[object, object, int]:
        """按序返回值三元组, 越界时抛 ``OSError``."""
        if index >= len(self.keys):
            raise OSError("枚举结束")
        name = self.keys[index]
        return name, f"value-{index}", 1


def _use(monkeypatch: pytest.MonkeyPatch, module: Any) -> None:
    """把读取器内部的 ``winreg`` 模块替换成替身."""
    monkeypatch.setattr(WinRegistry, "_module", staticmethod(lambda: module))


def test_subkeys_enumerates_until_oserror(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐个枚举子键, 遇到越界(``OSError``)停止, 非字符串条目跳过."""
    module = _FakeWinReg(["A", "B", 3])
    _use(monkeypatch, module)

    assert WinRegistry().subkeys("HKCU", "Software\\Vendor") == ["A", "B"]
    assert module.opened == [(module.HKEY_CURRENT_USER, "Software\\Vendor")]


def test_subkeys_returns_empty_for_unknown_hive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hive 名字不认识时直接返回空, 不尝试打开任何键."""
    module = _FakeWinReg([])
    _use(monkeypatch, module)

    assert WinRegistry().subkeys("NOPE", "Software\\Vendor") == []
    assert module.opened == []


def test_subkeys_and_values_degrade_on_missing_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """键不存在(权限/未安装)时返回空字典/空列表, 而不是抛出异常."""
    module = _FakeWinReg([])
    _use(monkeypatch, module)
    registry = WinRegistry()

    assert registry.subkeys("HKCU", "missing") == []
    assert registry.values("HKCU", "missing") == {}


def test_values_keeps_only_string_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """值列表里混入非字符串项时只保留字符串对."""
    module = _FakeWinReg(["InstallDir", 42])
    _use(monkeypatch, module)

    values = WinRegistry().values("HKCU", "Software\\Vendor")

    assert values["InstallDir"] == "value-0"
    assert set(values) == {"InstallDir"}


def test_values_returns_empty_for_unknown_hive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hive 名字不认识时 ``values`` 直接返回空字典, 不尝试打开任何键."""
    module = _FakeWinReg([])
    _use(monkeypatch, module)

    assert WinRegistry().values("NOPE", "Software\\Vendor") == {}
    assert module.opened == []
