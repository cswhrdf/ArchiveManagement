"""系统文件/目录选择封装.

仅在用户点击"浏览"等按钮时弹出系统对话框; 独立成模块便于在无头
单元测试中用假实现替换, 避免依赖真实图形环境.
"""

from __future__ import annotations

from tkinter import filedialog


def pick_directory(*, title: str) -> str | None:
    """弹出系统目录选择框; 用户取消时返回 None."""
    chosen = filedialog.askdirectory(title=title)
    return chosen or None


def pick_file(*, title: str) -> str | None:
    """弹出系统文件选择框; 用户取消时返回 None."""
    chosen = filedialog.askopenfilename(title=title)
    return chosen or None


def pick_save_file(*, title: str, initialfile: str | None = None) -> str | None:
    """弹出系统保存文件对话框; 用户取消时返回 None.

    ``initialfile`` 是预填的文件名(通常是按游戏名派生的目录/文件名), 用户仍可
    改成别的名字或目录; 传 None 或空串表示不预填。
    """
    chosen = filedialog.asksaveasfilename(title=title, initialfile=initialfile or "")
    return chosen or None
