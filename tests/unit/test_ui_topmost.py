"""host_is_pinned 的无头单元测试.

可无头验证的两件事: 非 Windows 上 Tk 读数位本身(应用内置顶)算数; 无效句柄(窗口可能
正在销毁)安静地按"未置顶"处理。Windows 上的真凭据(Tk 读数**不更新**, 且读一次还会让
Tk 拿缓存样式回写、把外部置上的位抹掉 —— 所以判定走纯 Win32 直查)由
tests/integration/test_gui_dropdown.py 的"外部置顶"一节端到端钉住, 替身没有真窗口查不了。
"""

from __future__ import annotations

import sys
import tkinter as tk
from typing import Any, cast

import pytest

from archive_management.ui.topmost import host_is_pinned

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("弹层置顶跟随"),
    pytest.mark.layer("unit"),
]


class _StubWindow:
    """顶层窗口替身: 只回答 Tk 的置顶读数; 句柄恒给无效值 0."""

    def __init__(self, *, pinned: bool) -> None:
        self.pinned = pinned

    def wm_attributes(self, name: str, _value: Any = None) -> Any:
        assert name == "-topmost", "替身只应被问到置顶读数"
        return self.pinned

    def winfo_id(self) -> int:
        return 0


class _StubHost:
    """宿主控件替身: 自己什么都不会, 只能沿到所属的顶层窗口(2026-10-31 CI 的教训)."""

    def __init__(self, window: _StubWindow) -> None:
        self.window = window

    def winfo_toplevel(self) -> _StubWindow:
        return self.window


def test_a_tk_level_pinned_host_counts_as_pinned_off_windows() -> None:
    """应用内置顶(Tk 写过的位): 非 Windows 上读 Tk 读数就该算数.

    宿主替身是**普通控件**(对话框常把页面框架当 parent): ``attributes`` /
    ``wm_attributes`` 是 ``Wm``(**窗口**)的方法, 控件没有 —— 不先 ``winfo_toplevel``
    就读, 在 Linux/macOS 上当场 ``AttributeError``(2026-10-31 CI)。替身控件**只有**
    ``winfo_toplevel`` 一个方法: 漏了这一步在哪个平台都藏不住。

    Windows 上的真值在窗口样式里(判定走纯 Win32 直查), 替身没有真窗口 —— 同一个替身在那
    条路上按"无效句柄安静退化"处理, 也是这里钉住的行为。
    """
    stub = cast("tk.Misc", _StubHost(_StubWindow(pinned=True)))
    if sys.platform != "win32":
        assert host_is_pinned(stub) is True
    else:
        assert host_is_pinned(stub) is False, "无效句柄要安静退化, 不许抛错"


def test_an_unpinned_host_degrades_quietly() -> None:
    """读数为 False 时安静地判"未置顶": 无效句柄(窗口可能正在销毁)也不许抛错."""
    stub = cast("tk.Misc", _StubHost(_StubWindow(pinned=False)))
    assert host_is_pinned(stub) is False
