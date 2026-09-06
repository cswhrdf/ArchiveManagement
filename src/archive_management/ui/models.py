"""UI 展示模型与纯逻辑.

这些模型不依赖 Tkinter/CustomTkinter,可在无显示环境单独测试。窗口把
后端返回的数据渲染为这些模型,并据此决定控件可用状态与展示顺序。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from archive_management.i18n import tr


class ViewKind(StrEnum):
    """备份视图类型."""

    TIMELINE = "timeline"
    BRANCH = "branch"


class FeedbackKind(StrEnum):
    """反馈消息严重度."""

    INFO = "info"
    SUCCESS = "success"
    ERROR = "error"
    PENDING = "pending"


@dataclass(frozen=True)
class GameSummary:
    """左侧游戏列表单条摘要."""

    game_id: str
    name: str
    has_locations: bool
    location_count: int
    backup_count: int
    tone: str = "blue"  # 头像色块基调,由窗口映射到调色板

    @property
    def list_detail(self) -> str:
        """列表项下方的说明文字."""
        if not self.has_locations:
            return tr("game.no_locations_short")
        return tr(
            "game.locations_detail",
            count=self.location_count,
            backups=self.backup_count,
        )


@dataclass(frozen=True)
class GameDetail:
    """标题行与概要卡展示数据."""

    name: str
    subtitle: str
    main_location: str
    location_verified: bool
    location_note: str
    last_backup_label: str
    last_backup_sub: str
    total_backups_label: str
    total_backups_sub: str
    next_backup_label: str


@dataclass(frozen=True)
class BackupItem:
    """时间线/分支中的单个备份节点."""

    backup_id: str
    title: str
    created_dt: datetime
    created_label: str
    auto: bool  # True=自动, False=手动
    branch_label: str  # 分支描述,如“主线”
    size_label: str
    verified: bool
    sub: str = ""  # 节点正文说明

    @property
    def kind_label(self) -> str:
        """节点来源标签(自动/手动)."""
        return tr("backup.kind_auto") if self.auto else tr("backup.kind_manual")


@dataclass(frozen=True)
class SelectedBackup:
    """右侧“选中备份”面板数据."""

    title: str
    meta: str
    files_label: str
    size_label: str
    can_restore: bool
    can_branch: bool


@dataclass(frozen=True)
class TaskStatus:
    """定时任务与运行环境状态."""

    running: bool
    task_name: str
    progress: float  # 0..1
    next_run_label: str
    target_label: str
    shortcut_label: str
    theme_name: str
    backend_ok: bool = True


@dataclass(frozen=True)
class Feedback:
    """一次性提示消息."""

    kind: FeedbackKind
    text: str


def timeline_order(items: list[BackupItem]) -> list[BackupItem]:
    """按创建时间倒序排列(时间线视图使用)."""
    return sorted(items, key=lambda item: item.created_dt, reverse=True)


def group_by_parent(items: list[BackupItem]) -> list[BackupItem]:
    """分支视图的简单呈现顺序:当前优先,其后按时间倒序.

    阶段 B 只使用演示数据与顺序语义,为后续真实分支树预留接口.
    """
    return sorted(items, key=lambda item: item.created_dt, reverse=True)


def can_backup(game: GameSummary | None) -> bool:
    """没有存档位置的游戏不允许立即备份."""
    return game is not None and game.has_locations
