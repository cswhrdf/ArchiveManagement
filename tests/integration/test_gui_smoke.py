"""GUI 冒烟测试.

无 tkinter/不可用的图形环境(CI 无显示、Tk 安装不完整)会自动跳过;
可用的环境中验证窗口能构建、切换主题并正常销毁(不进入阻塞式主循环).
"""

from __future__ import annotations

import pytest

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter  # noqa: F401 - 校验 customtkinter 可导入
except Exception as exc:
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.smoke,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("窗口启动与主题"),
    pytest.mark.story("窗口构建冒烟"),
    pytest.mark.layer("integration"),
]


def test_gui_smoke_build_theme_manage_and_destroy() -> None:
    """单窗口内验证构建、主题切换、管理窗口并销毁(避免同进程多 Tk 根)."""
    from archive_management.services.hotkeys import (
        GlobalHotkeyService,
        UnavailableBackend,
    )
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.main_window import ArchiveApp
    from archive_management.ui.manage_window import ManageGameWindow
    from archive_management.ui.palette import Palette

    try:
        app = ArchiveApp(
            DemoArchiveService(delay=0),
            title="冒烟",
            # 冒烟测试不注册系统级快捷键, 避免遗留键盘钩子.
            hotkeys=GlobalHotkeyService(backend=UnavailableBackend("冒烟测试禁用")),
        )
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")

    def _pump() -> None:
        app.update_idletasks()
        app.update()

    _pump()
    app._on_toggle_theme()
    _pump()
    palette = Palette.for_theme(app._theme)
    manager = ManageGameWindow(
        app,
        backend=DemoArchiveService(delay=0),
        palette=palette,
        game_id="outer-wilds",
        name="星际拓荒",
        enabled=True,
        backup_location=r"C:\fake\backups",
        on_change=lambda: None,
    )
    manager.refresh()
    _pump()
    manager.close()
    app.destroy()
