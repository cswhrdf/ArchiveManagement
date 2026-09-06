"""GUI 冒烟测试.

无 tkinter/不可用的图形环境(CI 无显示、Tk 安装不完整)会自动跳过;
可用的环境中验证窗口能构建、切换主题并正常销毁(不进入阻塞式主循环).
"""

from __future__ import annotations

from typing import Any

import pytest

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter  # noqa: F401 - 校验 customtkinter 可导入
except Exception as exc:
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)


def _build_app() -> Any:
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.main_window import ArchiveApp

    try:
        return ArchiveApp(DemoArchiveService(delay=0), title="冒烟")
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")


def test_gui_build_theme_switch_and_destroy() -> None:
    app = _build_app()
    app.update_idletasks()
    app.update()
    app._on_toggle_theme()
    app.update_idletasks()
    app.update()
    app.destroy()
