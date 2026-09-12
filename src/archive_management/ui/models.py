"""UI 展示模型与纯逻辑.

这些模型不依赖 Tkinter/CustomTkinter,可在无显示环境单独测试。窗口把
后端返回的数据渲染为这些模型,并据此决定控件可用状态与展示顺序。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from archive_management.domain import (
    PathKind,
    SaveSource,
    TreeInput,
    branch_lineage,
    keep_surviving,
    tree_depths,
)
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
    enabled: bool = True

    @property
    def list_detail(self) -> str:
        """列表项下方的说明文字."""
        if not self.enabled:
            return tr("game.disabled_short")
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
class LocationItem:
    """存档位置管理列表中的单条数据."""

    location_id: str
    game_id: str
    path: str
    path_kind: PathKind
    source: SaveSource
    is_primary: bool
    ok: bool
    note: str  # 状态说明(可用或具体校验错误)


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
    parent_id: str | None = None  # 分支树中的父节点
    depth: int = 0  # 分支树层级(时间线视图恒为 0)
    is_branch: bool = False  # 是否由“创建分支”产生的节点
    branch_name: str = ""  # 本节点开启的分支名(未开启则为空)
    is_current: bool = False  # 是否是“当前节点”(后续备份的起点)

    @property
    def kind_label(self) -> str:
        """节点来源标签(自动/手动)."""
        return tr("backup.kind_auto") if self.auto else tr("backup.kind_manual")

    @property
    def display_title(self) -> str:
        """展示名称: 未命名时回退到分支名或类型默认名."""
        if self.title:
            return self.title
        if self.branch_name:
            return self.branch_name
        return tr("backup.title_auto") if self.auto else tr("backup.title_manual")


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
    schedule_text: str = ""  # 原始周期配置(如 "30m"), 供编辑对话框回填
    cancellable: bool = False  # 当前操作是否可取消
    progress_label: str = ""  # 正在进行的具体步骤
    keep_auto: int = 3  # 自动备份保留份数
    revision: int = 0  # 备份数据版本号: 变化即表示列表需要重载


@dataclass(frozen=True)
class Feedback:
    """一次性提示消息."""

    kind: FeedbackKind
    text: str


def _tree_inputs(items: list[BackupItem]) -> list[TreeInput]:
    """把展示模型转换为树输入(保留分支名以便计算分支归属)."""
    return [
        TreeInput(
            node_id=item.backup_id,
            parent_id=item.parent_id,
            created_at=item.created_dt,
            branch_name=item.branch_name or None,
        )
        for item in items
    ]


def _with_branch_labels(
    items: list[BackupItem], lineage: dict[str, str | None]
) -> list[BackupItem]:
    """用"自身或最近分支祖先"的分支名重置展示用分支标签."""
    labelled: list[BackupItem] = []
    for item in items:
        name = lineage.get(item.backup_id)
        labelled.append(
            replace(
                item,
                branch_label=(
                    tr("backup.branch_label", branch=name)
                    if name
                    else tr("backup.mainline")
                ),
            )
        )
    return labelled


def visible_in_branch_view(items: list[BackupItem]) -> set[str]:
    """返回分支视图中应展示的节点: 自动备份只保留最新的一份.

    自动备份是特殊备份(只保留最近若干份), 在分支树里逐条铺开会让真实
    的分支关系被淹没, 因此只展示最近的那一份。
    """
    keep = {item.backup_id for item in items if not item.auto}
    autos = sorted(
        (item for item in items if item.auto),
        key=lambda item: (item.created_dt, item.backup_id),
    )
    if autos:
        keep.add(autos[-1].backup_id)
    return keep


def timeline_order(items: list[BackupItem]) -> list[BackupItem]:
    """按创建时间倒序排列(时间线视图使用), 并补全所属分支标签."""
    ordered = [
        replace(item, depth=0)
        for item in sorted(items, key=lambda item: item.created_dt, reverse=True)
    ]
    return _with_branch_labels(ordered, branch_lineage(_tree_inputs(items)))


def branch_order(items: list[BackupItem]) -> list[BackupItem]:
    """按分支树深度优先序排列, 并把层级写入 ``depth``(分支视图使用).

    自动备份只保留最新的一份(见 :func:`visible_in_branch_view`); 父节点被
    过滤掉时, 子节点会上移到最近仍可见的祖先, 因此层级始终连续。
    """
    inputs = keep_surviving(_tree_inputs(items), visible_in_branch_view(items))
    depths = tree_depths(inputs)
    lineage = branch_lineage(inputs)
    by_id = {item.backup_id: item for item in items}
    ordered = [
        replace(by_id[node_id], depth=depth)
        for node_id, depth in depths.items()
        if node_id in by_id
    ]
    return _with_branch_labels(ordered, lineage)


def group_by_parent(items: list[BackupItem]) -> list[BackupItem]:
    """分支视图排序(阶段 D 起等于 :func:`branch_order`).

    保留该名字是为了兼容既有调用点; 语义已由"按时间倒序"升级为"按分支树
    深度优先序", 使分支关系在列表中可见.
    """
    return branch_order(items)


def can_backup(game: GameSummary | None) -> bool:
    """没有存档位置的游戏不允许立即备份."""
    return game is not None and game.has_locations
