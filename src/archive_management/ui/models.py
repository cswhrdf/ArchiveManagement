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


# 自动备份保留份数的可配置上限(调度配置对话框与校验共用).
MAX_KEEP_AUTO = 60


def size_label(total: int) -> str:
    """把字节数格式化为紧凑的容量文本(列表与对话框共用)."""
    if total >= 1024**3:
        return f"{total / 1024**3:.1f} GB"
    if total >= 1024**2:
        return f"{total / 1024**2:.1f} MB"
    if total >= 1024:
        return f"{total / 1024:.1f} KB"
    return f"{total} B"


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
    # 录入时识别到的原始名称(与当前名称不同则表示用户改过名).
    original_name: str = ""
    # 备份根目录下实际使用的目录名; 尚未备份过时为空.
    storage_folder: str = ""

    @property
    def origin_label(self) -> str:
        """返回"原始名称 / 备份目录"补充信息(无内容可展示时返回空串)."""
        parts: list[str] = []
        if self.original_name and self.original_name != self.name:
            parts.append(tr("hero.original_name", name=self.original_name))
        if self.storage_folder:
            parts.append(tr("hero.storage_folder", folder=self.storage_folder))
        return " · ".join(parts)


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
    safety: bool = False  # 是否是"恢复前安全点"(只在时间线展示)

    @property
    def kind_label(self) -> str:
        """节点来源标签(自动/安全点/手动)."""
        if self.safety:
            return tr("backup.kind_safety")
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
    schedule_enabled: bool = True  # 周期配置是否启用(暂停时仍保留周期)
    cancellable: bool = False  # 当前操作是否可取消
    progress_label: str = ""  # 正在进行的具体步骤
    keep_auto: int = 3  # 自动备份保留份数
    revision: int = 0  # 备份数据版本号: 变化即表示列表需要重载


@dataclass(frozen=True)
class ScheduleItem:
    """全局定时任务列表中的一条记录(每个游戏至多一条)."""

    game_id: str
    game_name: str
    interval_text: str  # 原始周期配置(如 "60m"); 为空表示未配置
    enabled: bool
    keep_auto: int
    next_run_label: str
    auto_count: int  # 该游戏当前保留的自动备份份数
    last_error: str = ""
    tone: str = "blue"
    has_locations: bool = True  # 未配置存档位置的游戏无法创建定时备份

    @property
    def game_label(self) -> str:
        """返回任务来自哪个游戏的展示文案."""
        return tr("schedule.game_label", name=self.game_name)

    @property
    def interval_label(self) -> str:
        """周期文案(未配置时给出明确提示)."""
        if not self.interval_text:
            return tr("task.unscheduled")
        return tr(
            "task.every",
            interval=self.interval_text,
            keep=self.keep_auto,
        )

    @property
    def state_label(self) -> str:
        """启用状态文案."""
        if not self.interval_text:
            return tr("schedule.state_off")
        return tr("schedule.state_on") if self.enabled else tr("schedule.state_paused")

    @property
    def can_schedule(self) -> bool:
        """是否满足创建定时备份的前置条件(必须有存档位置)."""
        return self.has_locations

    @property
    def auto_count_label(self) -> str:
        """当前自动备份存档数量文案."""
        return tr("schedule.auto_count", count=self.auto_count)

    @property
    def summary(self) -> str:
        """列表行副标题: 周期 + 下次运行 + 自动备份份数."""
        return " · ".join(
            (
                self.interval_label,
                tr("schedule.next_run", stamp=self.next_run_label),
                self.auto_count_label,
            )
        )


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


def visible_in_branch_view(
    items: list[BackupItem], *, include_safety: bool = False
) -> set[str]:
    """返回分支视图中应展示的节点: 自动备份只保留最新一份, 安全点不展示.

    自动备份与恢复前安全点都是"特殊备份": 自动备份只保留最近若干份, 安全点
    是恢复操作的副产品, 它们逐条铺开会让真实的分支关系被淹没。自动备份保留
    最近的一份以体现"最近同步过"; 安全点默认完全不进入分支树, 只在时间线里
    查看——只有用户显式筛选"恢复前安全点"时才例外(``include_safety=True``)。
    """
    keep = {
        item.backup_id
        for item in items
        if not item.auto and (include_safety or not item.safety)
    }
    autos = sorted(
        (item for item in items if item.auto),
        key=lambda item: (item.created_dt, item.backup_id),
    )
    if autos:
        keep.add(autos[-1].backup_id)
    return keep


class SourceFilter(StrEnum):
    """备份来源筛选(与工具栏下拉选项一一对应)."""

    ALL = "all"
    MANUAL = "manual"
    AUTO = "auto"
    SAFETY = "safety"

    @property
    def label(self) -> str:
        """返回下拉框与日志中使用的展示文案."""
        return tr(f"filter.{self.value}")


def filter_by_source(items: list[BackupItem], source: SourceFilter) -> list[BackupItem]:
    """按来源筛选备份节点.

    "手动"会同时包含安全点(它也走手动保存链路): 想只看安全点时用
    :attr:`SourceFilter.SAFETY`。显式按安全点筛选时, 分支视图也会把安全点
    显示出来(筛选是用户明确的要求, 优先于默认的隐藏规则)。
    """
    if source is SourceFilter.AUTO:
        return [item for item in items if item.auto]
    if source is SourceFilter.SAFETY:
        return [item for item in items if item.safety]
    if source is SourceFilter.MANUAL:
        return [item for item in items if not item.auto]
    return list(items)


def filter_by_source_label(items: list[BackupItem], label: str) -> list[BackupItem]:
    """按下拉框文案筛选(界面里保存的就是文案)."""
    for source in SourceFilter:
        if source.label == label:
            return filter_by_source(items, source)
    return list(items)


def timeline_order(items: list[BackupItem]) -> list[BackupItem]:
    """按创建时间倒序排列(时间线视图使用), 并补全所属分支标签."""
    ordered = [
        replace(item, depth=0)
        for item in sorted(items, key=lambda item: item.created_dt, reverse=True)
    ]
    return _with_branch_labels(ordered, branch_lineage(_tree_inputs(items)))


def branch_order(
    items: list[BackupItem], *, include_safety: bool = False
) -> list[BackupItem]:
    """按分支树深度优先序排列, 并把层级写入 ``depth``(分支视图使用).

    自动备份只保留最新的一份、安全点默认不展示(见
    :func:`visible_in_branch_view`); 父节点被过滤掉时, 子节点会上移到最近仍
    可见的祖先, 因此层级始终连续。
    """
    inputs = keep_surviving(
        _tree_inputs(items),
        visible_in_branch_view(items, include_safety=include_safety),
    )
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


# --------------------------------------------------------- 本地游戏探测(E-1)


@dataclass(frozen=True)
class ScanSummary:
    """一次本机探测的结果摘要(探测窗口据此显示提示与计数)."""

    monitored: int
    active: int
    total: int
    added: int
    updated: int
    linked: int
    unusable: int
    errors: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """主提示文案(强调新增与已知的候选数量)."""
        return tr("discovery.scan_done", added=self.added, total=self.total)

    @property
    def detail(self) -> str:
        """副提示文案: 监控目录数、路径不可用数与自动识别数量."""
        parts = [
            tr("discovery.scan_monitored", count=self.monitored, active=self.active),
            tr("discovery.scan_unusable", count=self.unusable),
        ]
        if self.linked:
            parts.append(tr("discovery.scan_linked", count=self.linked))
        if self.errors:
            parts.append(tr("discovery.scan_errors", count=len(self.errors)))
        return " · ".join(parts)


class DiscoveryPage(StrEnum):
    """游戏发现窗口的页面(与顶部页签一一对应, 成员顺序即页签顺序)."""

    CANDIDATES = "candidates"
    MONITORED = "monitored"

    @property
    def label(self) -> str:
        """返回页签文案."""
        return tr(f"discovery.page_{self.value}")


@dataclass(frozen=True)
class MonitoredDirItem:
    """监控目录列表中的单条数据."""

    directory_id: str
    path: str
    enabled: bool
    note: str
    health: str  # ok/missing/not_directory/unreadable/unsafe
    last_scan_label: str = ""

    @property
    def health_label(self) -> str:
        """路径健康状态文案(扫描结果与列表共用)."""
        return tr(f"discovery.health_{self.health}")

    @property
    def state_label(self) -> str:
        """启用状态文案."""
        return tr("discovery.dir_on") if self.enabled else tr("discovery.dir_off")

    @property
    def summary(self) -> str:
        """列表行副标题: 路径 + 状态 + 上次扫描时间."""
        parts = [self.state_label, self.health_label]
        if self.note:
            parts.append(self.note)
        if self.last_scan_label:
            parts.append(tr("discovery.dir_last_scan", stamp=self.last_scan_label))
        return " · ".join(parts)


class CandidateFilter(StrEnum):
    """探测结果筛选(与下拉选项一一对应)."""

    ALL = "all"
    NEW = "new"
    IMPORTED = "imported"
    IGNORED = "ignored"

    @property
    def label(self) -> str:
        """返回下拉框与日志中使用的展示文案."""
        return tr(f"discovery.filter_{self.value}")


@dataclass(frozen=True)
class CandidateItem:
    """探测结果列表中的单条数据."""

    candidate_id: str
    name: str
    install_dir: str
    source: str  # steam/epic/gog/battle_net/monitored/manual
    confidence: str  # high/medium/low
    status: str  # new/imported/ignored
    health: str  # ok/missing/not_directory/unreadable/unsafe
    detail: str = ""
    game_id: str | None = None

    @property
    def source_label(self) -> str:
        """来源平台文案."""
        return tr(f"discovery.source_{self.source}")

    @property
    def confidence_label(self) -> str:
        """可信度文案."""
        return tr(f"discovery.confidence_{self.confidence}")

    @property
    def status_label(self) -> str:
        """处理进度文案."""
        return tr(f"discovery.status_{self.status}")

    @property
    def health_label(self) -> str:
        """路径健康状态文案."""
        return tr(f"discovery.health_{self.health}")

    @property
    def importable(self) -> bool:
        """是否可以直接导入为游戏(路径可用且尚未处理)."""
        return self.status == "new" and self.health == "ok"

    @property
    def summary(self) -> str:
        """列表行副标题: 来源 + 可信度 + 状态 + 路径状态."""
        return " · ".join(
            (
                self.source_label,
                self.confidence_label,
                self.status_label,
                self.health_label,
            )
        )
