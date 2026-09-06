"""CustomTkinter 用户界面.

UI 通过线程安全消息队列接收后台任务状态;工作线程禁止直接更新 Tk 控件。
本模块只导出不依赖 Tkinter 的纯层,便于在无显示环境(如 CI)测试;
真正窗口(``main_window``、``widgets``、``dialogs``)按需延迟导入。
"""

from __future__ import annotations

from archive_management.ui import demo_backend, models, palette
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.models import (
    BackupItem,
    FeedbackKind,
    GameDetail,
    GameSummary,
    TaskStatus,
    ViewKind,
)
from archive_management.ui.palette import DEFAULT_THEME, THEME_NAMES, Palette

__all__ = [
    "DEFAULT_THEME",
    "THEME_NAMES",
    "ArchiveService",
    "BackupItem",
    "DemoArchiveService",
    "FeedbackKind",
    "GameDetail",
    "GameSummary",
    "Palette",
    "TaskStatus",
    "ViewKind",
    "demo_backend",
    "models",
    "palette",
]
