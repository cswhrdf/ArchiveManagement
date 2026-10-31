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


class _StubHost:
    """宿主替身: 只回答 Tk 的置顶读数; 句柄恒给无效值 0."""

    def __init__(self, *, pinned: bool) -> None:
        self.pinned = pinned

    def attributes(self, name: str, _value: Any = None) -> Any:
        assert name == "-topmost", "替身只应被问到置顶读数"
        return self.pinned

    def winfo_id(self) -> int:
        return 0


def test_a_tk_level_pinned_host_counts_as_pinned_off_windows() -> None:
    """应用内置顶(Tk 写过的位): 非 Windows 上读 Tk 读数就该算数.

    Windows 上的真值在窗口样式里(判定走纯 Win32 直查), 替身没有真窗口 —— 同一个替身在那
    条路上按"无效句柄安静退化"处理, 也是这里钉住的行为。
    """
    stub = cast("tk.Misc", _StubHost(pinned=True))
    if sys.platform != "win32":
        assert host_is_pinned(stub) is True
    else:
        assert host_is_pinned(stub) is False, "无效句柄要安静退化, 不许抛错"


def test_an_unpinned_host_degrades_quietly() -> None:
    """读数为 False 时安静地判"未置顶": 无效句柄(窗口可能正在销毁)也不许抛错."""
    assert host_is_pinned(cast("tk.Misc", _StubHost(pinned=False))) is False
