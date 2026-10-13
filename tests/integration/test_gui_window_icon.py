"""窗口/任务栏图标: 子窗口不该顶着 CustomTkinter 自带的那颗.

为什么单独一条: CustomTkinter 在**每个**窗口建好 200ms 后, 自己去贴一张它包里的
``CustomTkinter_icon_Windows.ico`` —— 条件是"这个窗口上没人调用过 ``iconbitmap``"。
主窗口逃得过(我们在 ``ArchiveApp.__init__`` 里显式设过), **每个子窗口都中招**: 设置、
定时任务、游戏管理、所有对话框, 标题栏与任务栏里都是 CTk 那颗(2026-10-11 用户报的就是它)。

那个动作在**定时器**里(200ms), 所以"看代码对不对"证明不了 —— 必须真的把事件循环泵到
200ms 之后, 看那次 ``iconbitmap`` 带的是哪个文件。实测(2026-10-11 的临时探针):

- 不装补丁: 主窗口 ``app.ico``、子窗口 ``CustomTkinter_icon_Windows.ico``;
- 装了 ``ui/window_icon`` 的补丁: 子窗口 ``app-window.ico``(我们的那颗)。

只在 Windows 上跑: CTk 那次贴图本身就写着 ``if sys.platform == "win32"``, Linux/macOS 上
没有这个动作 —— 那里跳过是**事实**, 不是环境问题(所以原因里不写 "tk 环境不可用", 免得
被 ``tests/tk_guard.py`` 那条纪律当成静默跳过处理)。
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from customtkinter.windows.ctk_toplevel import CTkToplevel

from archive_management.services import icon_assets
from ui_sharing import SharedUiRegistry, demo_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("窗口与任务栏图标"),
    pytest.mark.layer("e2e"),
]

#: 等那个 200ms 的定时器到期: 给几倍余量, 但都靠泵事件等到, 不睡固定时长。
_DEADLINE_SECONDS = 3.0


@pytest.fixture
def app(ui_shared: SharedUiRegistry) -> Iterator[Any]:
    """共享会话里的主窗口(子窗口挂在它上面; 窗口的收尾交给池)."""
    with ui_shared.test_scope(demo_app) as application:
        yield application


@pytest.fixture
def icons_seen(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, str]]:
    """套一层 ``CTkToplevel.iconbitmap``: 记下 ``(哪个窗口, 哪个路径)``, 退出时撤掉.

    必须记窗口: 共享会话里同时活着好几个 Toplevel, 别把别人的记成子窗口的。
    套的是**函数**(不是实例): 它会被绑上去, 于是第一个参数就是那个窗口。
    """
    seen: list[tuple[Any, str]] = []
    original = CTkToplevel.iconbitmap

    def recorder(window: Any, bitmap: Any = None, default: Any = None) -> None:
        seen.append((window, str(bitmap if bitmap is not None else default)))
        original(window, bitmap, default)

    monkeypatch.setattr(CTkToplevel, "iconbitmap", recorder)
    return seen


def _pump_until(app: Any, done: Callable[[], bool]) -> bool:
    """泵事件直到 ``done()`` 成立(返回是否成立), 超时就如实返回 False."""
    end = time.monotonic() + _DEADLINE_SECONDS
    while True:
        app.update_idletasks()
        app.update()
        if done() or time.monotonic() >= end:
            return done()
        time.sleep(0.01)


def _paths_for(seen: list[tuple[Any, str]], window: Any) -> list[str]:
    """只看这个窗口收到的路径(按文件名, 路径含 tmp/安装目录, 比全路径脆)."""
    return [Path(path).name for owner, path in seen if owner is window]


@pytest.mark.skipif(
    sys.platform != "win32",
    reason="CTk 那次延时贴图只在 Windows 上排(别的平台没有这个动作可测)",
)
def test_a_child_window_ends_up_with_our_icon(
    app: Any, icons_seen: list[tuple[Any, str]]
) -> None:
    """子窗口建好 200ms 后那次 ``iconbitmap``: 带的是我们的 ``app.ico``, 不是 CTk 那颗."""
    expected = icon_assets.icon_files().window_ico
    assert expected is not None, "位图应当随包发布(见 services/icon_assets)"

    child = CTkToplevel(app)
    child.withdraw()  # 与窗口可见性无关: 定时器照样会到点
    try:
        assert _pump_until(app, lambda: bool(_paths_for(icons_seen, child))), (
            "200ms 之后 CTk 本该给每个窗口贴一次图标 —— 没等到说明前提变了, 这条用例"
            "已经量不到东西了(不是产品坏了)"
        )
        seen_names = _paths_for(icons_seen, child)
        assert seen_names == [expected.name], (
            f"子窗口的图标应当是 {expected.name}, 量到 {seen_names}\n"
            "空列表 = 定时器没跑到; CustomTkinter_icon_Windows.ico = CTk 又把它那颗贴"
            "回来了(补丁没装上, 或被库升级盖掉 —— 见 ui/window_icon.py)"
        )
    finally:
        child.destroy()
        app.update_idletasks()
        app.update()
