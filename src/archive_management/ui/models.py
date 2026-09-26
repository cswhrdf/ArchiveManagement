"""UI 展示模型与纯逻辑.

这些模型不依赖 Tkinter/CustomTkinter,可在无显示环境单独测试。窗口把
后端返回的数据渲染为这些模型,并据此决定控件可用状态与展示顺序。
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum

from archive_management.application.home import HomeReport
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
    BatchGameInspection,
    BatchInspection,
    ImportInspection,
    PackageLocation,
)
from archive_management.domain import (
    GameAction,
    GameFacts,
    HomeFilter,
    HomeStats,
    HomeView,
    PathKind,
    SavePathCandidate,
    SaveSource,
    TreeInput,
    action_allowed,
    branch_lineage,
    keep_surviving,
    tree_depths,
)
from archive_management.i18n import tr
from archive_management.services.export_format import ARCHIVE_SUFFIX
from archive_management.services.pathcheck import dangerous_target_reason
from archive_management.services.platforms import PLATFORM_LABELS


class ViewKind(StrEnum):
    """备份视图类型."""

    TIMELINE = "timeline"
    BRANCH = "branch"


class AppPage(StrEnum):
    """主窗口内容区里的页面.

    两个页面同格叠放, 同一时间只显示一个: **成员顺序即默认页面**, 当前第一位是
    游戏主页 —— 游戏变多以后, 打开软件先看到全局视图比直接进某个游戏的详情更有用。
    """

    HOME = "home"
    DETAIL = "detail"


class HomeSection(StrEnum):
    """游戏主页内部的分区(成员顺序即页签顺序).

    游戏发现原本是独立窗口, 后来合并成主页的一个分区: 两者都在回答"游戏库里有
    什么", 放在同一个页面里切换比开两个窗口更顺手。游戏启停同样讲的是库里的
    游戏(谁在运行、谁在监控), 因此也放在这里而不是另开一个页面。
    """

    LIBRARY = "library"
    DISCOVERY = "discovery"
    ACTIVATION = "activation"

    @property
    def label(self) -> str:
        """返回分区页签文案."""
        return tr(f"page.{self.value}")


# 海报卡片的目标宽度(留出间距后用于计算每行张数).
POSTER_WIDTH = 210


def poster_columns(width: int) -> int:
    """按可用宽度计算海报模式每行放几张卡片(纯函数, 便于单独测试)."""
    return max(2, int(width) // (POSTER_WIDTH + 16))


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
    # 展示模型的默认值不承担业务语义(后端总是显式传入): "新建游戏默认停用"
    # 由 ``domain.entities.Game`` 的默认值决定.
    enabled: bool = True
    archived: bool = False
    # 导入时写进存档位置的路径数(0 表示这次导入没有带存档位置);
    # 只用于现场反馈, 路径本身由用户在导入对话框里确认或修改.
    saved_paths: int = 0
    # 录入时的名称(译名写回或用户改名之前的那个): 与 name 不同时界面会一并显示, 批量
    # 导出的筛选也按它匹配 —— 用户脑子里记的可能还是当初那一个名字.
    original_name: str = ""

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

    def allow(self, action: GameAction) -> bool:
        """该动作在当前状态下是否可用(归档游戏只保留四项)."""
        return action_allowed(action, archived=self.archived)


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
    # 录入时识别到的原始名称(与当前名称不同只说明当前名字不是录入时那个:
    # 可能是用户改的, 也可能是程序写入的译名 —— 区分靠 Game.localized_name).
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
    # 游戏本身的状态: 停用或归档时保留任务配置, 但不允许启用与执行.
    game_enabled: bool = True
    archived: bool = False

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
        if self.archived:
            return tr("schedule.state_archived")
        if not self.interval_text:
            return tr("schedule.state_off")
        return tr("schedule.state_on") if self.enabled else tr("schedule.state_paused")

    @property
    def can_schedule(self) -> bool:
        """是否满足创建定时备份的前置条件(必须有存档位置且未归档)."""
        return self.has_locations and not self.archived

    @property
    def can_enable(self) -> bool:
        """是否允许把任务切到启用态(停用或归档的游戏不允许)."""
        return self.game_enabled and not self.archived

    @property
    def can_toggle(self) -> bool:
        """是否允许暂停/继续: 归档后整体不可用; 停用中的游戏只能暂停, 不能继续."""
        if not self.interval_text or self.archived:
            return False
        return self.enabled or self.game_enabled

    @property
    def editable(self) -> bool:
        """是否可以编辑/删除该任务(归档游戏只保留删除、导出、取消归档与打开详情)."""
        return not self.archived

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
    """分支视图排序(等价于 :func:`branch_order`).

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
    # 命中平台工具排除清单、被默认隐藏(已忽略)的候选数量.
    excluded: int = 0
    errors: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        """主提示文案(强调新增与已知的候选数量)."""
        return tr("discovery.scan_done", added=self.added, total=self.total)

    @property
    def detail(self) -> str:
        """副提示文案: 监控目录数、路径不可用数、自动隐藏数与自动识别数量."""
        parts = [
            tr("discovery.scan_monitored", count=self.monitored, active=self.active),
            tr("discovery.scan_unusable", count=self.unusable),
        ]
        if self.excluded:
            parts.append(tr("discovery.scan_excluded", count=self.excluded))
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
class SavePathSuggestion:
    """探测游戏时顺带推断出的一条存档路径(还没写进存档位置).

    只在界面展示与导入对话框里回填: 用户确认后才变成存档位置, 推断不出来也不
    影响游戏本身导入成功。路径都来自平台清单这类可信渠道, 所以不再标注可信度;
    需要提醒的只剩下"危险目标"(用户主目录、盘符根、游戏安装目录内部)。
    """

    path: str
    path_kind: str  # file/directory
    dangerous: bool = False

    @classmethod
    def from_candidate(cls, item: SavePathCandidate) -> SavePathSuggestion:
        """把平台给出的候选收敛成展示模型(危险路径当场标记)."""
        return cls(
            path=item.path,
            path_kind=item.path_kind,
            dangerous=dangerous_target_reason(item.path) is not None,
        )

    @property
    def risk_label(self) -> str:
        """危险路径的提示文案(安全时为空串)."""
        return tr("discovery.save_risk") if self.dangerous else ""

    @property
    def text(self) -> str:
        """列表行里的一句话: 路径 (危险时补一句提醒)."""
        if not self.dangerous:
            return self.path
        return f"{self.path} · {self.risk_label}"


@dataclass(frozen=True)
class CandidateItem:
    """探测结果列表中的单条数据."""

    candidate_id: str
    name: str
    install_dir: str
    source: str  # steam/epic/gog/ubisoft/monitored/manual
    confidence: str  # high/medium/low
    status: str  # new/imported/ignored
    health: str  # ok/missing/not_directory/unreadable/unsafe
    detail: str = ""
    game_id: str | None = None
    # 当前语言下的译名(空表示没有译名, 界面回落到探测到的名称).
    localized_name: str = ""
    # 探测这款游戏时顺带推断出的存档路径(不落库); 平台不支持时为空.
    save_paths: tuple[SavePathSuggestion, ...] = ()
    save_supported: bool = True

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
    def display_name(self) -> str:
        """界面上显示的名称: 有当前语言的译名就用译名, 没有就用探测到的名称."""
        return self.localized_name or self.name

    @property
    def save_label(self) -> str:
        """存档路径推断结果的提示文案(三态: 不支持 / 没推出来 / 推出来 N 条)."""
        if not self.save_supported:
            return tr("discovery.save_unsupported", platform=self.source_label)
        if not self.save_paths:
            return tr("discovery.save_none")
        return tr("discovery.save_found", count=len(self.save_paths))

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


# --------------------------------------------------------- 统一游戏主页(E-2)


@dataclass(frozen=True)
class HomeOption:
    """主页的一个可选项(视图页签、平台或分类).

    ``key`` 用于回写筛选条件, ``label`` 是人类可读名称, ``count`` 让用户在切换
    之前就知道每个选项里有多少游戏 —— 分类下拉框里的数字为 0 的分类不会出现,
    用户不会点进一个空分类。
    """

    key: str
    label: str
    count: int

    @property
    def text(self) -> str:
        """带数量的展示文案(页签与下拉框共用)."""
        return f"{self.label} ({self.count})"


def _option_text(options: tuple[HomeOption, ...], key: str, fallback: str) -> str:
    """按 key 找选项文案; 找不到(例如该平台暂时没有游戏)时使用回退文案."""
    return next((item.text for item in options if item.key == key), fallback)


@dataclass(frozen=True)
class HomeGameItem:
    """主页列表中的单条游戏摘要(名称、平台、位置/备份数量、时间与风险)."""

    game_id: str
    name: str
    origin: str  # steam/epic/gog/ubisoft/monitored/manual
    location_count: int
    backup_count: int
    last_backup_label: str  # 已格式化时间; 从未备份时为空串
    activity_label: str  # 最近活动时间; 无记录时为空串
    risk: bool
    archived: bool
    enabled: bool = True
    tags: tuple[str, ...] = ()
    tone: str = "blue"  # 头像色块基调, 由窗口映射到调色板

    @property
    def platform_label(self) -> str:
        """来源平台文案(与探测结果使用同一套来源文案)."""
        return tr(f"discovery.source_{self.origin}")

    @property
    def backup_label(self) -> str:
        """备份状态文案: 没有存档位置时说明原因, 而不是笼统地说"未备份"."""
        if self.location_count == 0:
            return tr("home.no_save_paths")
        return (
            tr("home.cat_backup_done")
            if self.backup_count
            else tr("home.cat_backup_none")
        )

    @property
    def risk_label(self) -> str:
        """存档路径状态文案."""
        return tr("home.risk_bad") if self.risk else tr("home.risk_ok")

    @property
    def state_label(self) -> str:
        """状态文案: 归档优先, 其次是停用(都没有时为空串)."""
        if self.archived:
            return tr("home.chip_archived")
        return "" if self.enabled else tr("home.chip_disabled")

    @property
    def meta(self) -> str:
        """存档位置与备份数量的简要说明."""
        return tr("home.meta", locations=self.location_count, backups=self.backup_count)

    @property
    def summary(self) -> str:
        """列表行副标题: 位置/备份数量 + 最近备份 + 最近活动."""
        parts = [
            self.meta,
            (
                tr("home.last_backup", stamp=self.last_backup_label)
                if self.last_backup_label
                else tr("home.last_backup_none")
            ),
        ]
        if self.activity_label:
            parts.append(tr("home.activity", stamp=self.activity_label))
        return " · ".join(parts)

    @property
    def chips(self) -> tuple[str, ...]:
        """列表行的分类标签: 平台、备份、风险、归档/停用与自定义标签."""
        parts = [
            self.platform_label,
            self.backup_label,
        ]
        if self.risk:
            parts.append(tr("home.chip_risk"))
        if self.archived:
            parts.append(tr("home.chip_archived"))
        elif not self.enabled:
            parts.append(tr("home.chip_disabled"))
        parts.extend(self.tags)
        return tuple(parts)

    @property
    def backup_enabled(self) -> bool:
        """没有存档位置或已归档时不允许"立即备份"(与详情页的规则一致)."""
        return self.location_count > 0 and action_allowed(
            "backup", archived=self.archived
        )

    def allow(self, action: GameAction) -> bool:
        """该动作在当前状态下是否可用(归档游戏只保留四项)."""
        return action_allowed(action, archived=self.archived)


@dataclass(frozen=True)
class HomeBoard:
    """主页一次渲染所需的全部数据(列表、页签、筛选项与底部摘要)."""

    filter: HomeFilter
    games: tuple[HomeGameItem, ...]
    views: tuple[HomeOption, ...]
    origins: tuple[HomeOption, ...]
    categories: tuple[HomeOption, ...]
    stats: HomeStats

    @property
    def view_text(self) -> str:
        """当前视图的页签文案(含数量)."""
        return _option_text(self.views, self.filter.view.value, self.filter.view.label)

    @property
    def origin_text(self) -> str:
        """当前平台筛选的文案(含数量); 未选择时用"全部平台"."""
        if not self.filter.origin:
            return tr("home.origin_all")
        fallback = tr(f"discovery.source_{self.filter.origin}")
        return _option_text(self.origins, self.filter.origin, fallback)

    @property
    def category_text(self) -> str:
        """当前分类筛选的文案(含数量); 未选择时用"全部类型"."""
        if not self.filter.category:
            return tr("home.category_all")
        item = self.filter.category_item
        fallback = item.label if item is not None else self.filter.category
        return _option_text(self.categories, self.filter.category, fallback)

    @property
    def summary(self) -> str:
        """底部第一行: 各视图的计数."""
        return tr(
            "home.summary",
            total=self.stats.total,
            recent=self.stats.recent,
            pending=self.stats.pending,
            archived=self.stats.archived,
        )

    @property
    def detail(self) -> str:
        """底部第二行: 备份与风险数量."""
        return tr(
            "home.detail",
            backed_up=self.stats.backed_up,
            risky=self.stats.risky,
        )

    @property
    def narrowing(self) -> bool:
        """当前是否偏离了"默认视图 + 无筛选"的初始状态."""
        active = self.filter
        return (
            bool(active.origin or active.category or active.search)
            or active.view is not HomeView.ALL
        )

    @property
    def empty_message(self) -> str:
        """空状态主文案: 区分"游戏库为空"与"当前筛选没有匹配"."""
        if self.stats.total == 0 and self.stats.archived == 0:
            return tr("home.empty_library")
        if not self.narrowing:
            return tr("home.empty_view", view=self.filter.view.label)
        return tr("home.empty_filtered", total=self.stats.total)

    @property
    def empty_hint(self) -> str:
        """空状态补充说明(始终给出下一步可以做什么)."""
        if self.stats.total == 0 and self.stats.archived == 0:
            return tr("home.empty_library_hint")
        if not self.narrowing:
            return tr("home.empty_hint")
        return tr("home.empty_filtered_hint")


def home_board(report: HomeReport, *, stamp: Callable[[datetime], str]) -> HomeBoard:
    """把主页用例结果映射为展示模型.

    真实后端与演示后端共用这段映射, 保证两个后端的界面行为一致(演示后端只需
    提供事实, 不需要重新实现分类与筛选)。``stamp`` 由后端提供, 用于把时间格式
    化为本地时间文本。
    """
    return HomeBoard(
        filter=report.filter,
        games=tuple(_home_item(facts, stamp=stamp) for facts in report.games),
        views=tuple(
            HomeOption(
                key=view.value,
                label=view.label,
                count=report.stats.count_for(view),
            )
            for view in HomeView
        ),
        origins=tuple(
            HomeOption(key=origin, label=tr(f"discovery.source_{origin}"), count=count)
            for origin, count in report.origins
        ),
        categories=tuple(
            HomeOption(key=category.value, label=category.label, count=count)
            for category, count in report.categories
        ),
        stats=report.stats,
    )


def _home_item(facts: GameFacts, *, stamp: Callable[[datetime], str]) -> HomeGameItem:
    """把单个游戏的事实映射为列表项."""
    activity = facts.activity_at
    return HomeGameItem(
        game_id=facts.game_id,
        name=facts.name,
        origin=facts.origin,
        location_count=facts.location_count,
        backup_count=facts.backup_count,
        last_backup_label=(
            stamp(facts.last_backup_at) if facts.last_backup_at is not None else ""
        ),
        activity_label=stamp(activity) if activity is not None else "",
        risk=facts.risk,
        archived=facts.archived,
        enabled=facts.enabled,
        tags=facts.tags,
    )


# -- 导入归档包 -------------------------------------------------------------


@dataclass(frozen=True)
class ImportLocationRow:
    """导入对话框里的一行存档位置."""

    index: int
    text: str
    default: str


@dataclass(frozen=True)
class ImportTargetOption:
    """可以合并到的目标游戏(对话框里是一个单选项)."""

    game_id: str
    label: str
    selected: bool = False


@dataclass(frozen=True)
class ImportChoice:
    """用户在导入对话框里做出的选择(取消时对话框返回 ``None``)."""

    strategy: str
    target_game_id: str | None
    locations: Mapping[int, str]


@dataclass(frozen=True)
class ImportPrompt:
    """导入对话框需要的文案与选项(纯数据, 无头环境也能构造与断言)."""

    summary: str
    match_text: str
    locations: tuple[ImportLocationRow, ...]
    targets: tuple[ImportTargetOption, ...]


def import_prompt(
    inspection: ImportInspection, games: Sequence[GameSummary]
) -> ImportPrompt:
    """把体检结果与游戏库映射成导入对话框的文案与选项.

    游戏库只用来提供"合并到哪一款"的选项与"疑似同一款"的提示: 真正写库的是
    后端, 界面不替它做匹配判断。存档位置则只在包内路径本机已存在时才预填,
    其余留空(留空的那一条不会被导入)。
    """
    matching_id = (
        None
        if inspection.matching_game_id is None
        else str(inspection.matching_game_id)
    )
    return ImportPrompt(
        summary=_import_summary(inspection),
        match_text=_import_match_text(games, matching_id),
        locations=tuple(_import_location(item) for item in inspection.locations),
        targets=_import_targets(games, matching_id),
    )


def import_strategies(*, has_targets: bool) -> tuple[tuple[str, str], ...]:
    """导入方式选项(策略键 + 文案): 没有可合并的游戏就不给"合并"."""
    options = [(STRATEGY_NEW, tr("dialog.import_strategy_new"))]
    if has_targets:
        options.append((STRATEGY_MERGE, tr("dialog.import_strategy_merge")))
    options.append((STRATEGY_SKIP, tr("dialog.import_strategy_skip")))
    return tuple(options)


def _import_summary(inspection: ImportInspection) -> str:
    """包摘要: 游戏标识 + 备份/文件数与体积(没有 AppID 的包少一段说明)."""
    platform = PLATFORM_LABELS.get(inspection.platform, inspection.platform)
    key = (
        "dialog.import_summary"
        if inspection.steam_app_id is not None
        else "dialog.import_summary_unknown_app"
    )
    return tr(
        key,
        name=inspection.game_name,
        platform=platform,
        app_id=inspection.steam_app_id,
        backups=inspection.backup_count,
        files=inspection.file_count,
        size=size_label(inspection.total_bytes),
    )


def _import_match_text(games: Sequence[GameSummary], matching_id: str | None) -> str:
    """库里疑似同一款游戏的提示(没有匹配时是空串, 对话框就不显示这一行)."""
    game = next((item for item in games if item.game_id == matching_id), None)
    if game is None:
        return ""
    return tr("dialog.import_match", name=game.name)


def _import_targets(
    games: Sequence[GameSummary], matching_id: str | None
) -> tuple[ImportTargetOption, ...]:
    """目标游戏选项: 疑似同一款排在最前并默认选中, 其余保持库里的顺序."""
    if not games:
        return ()
    matching = tuple(game for game in games if game.game_id == matching_id)
    others = tuple(game for game in games if game.game_id != matching_id)
    return tuple(
        ImportTargetOption(game_id=game.game_id, label=game.name, selected=index == 0)
        for index, game in enumerate(matching + others)
    )


def _import_location(item: PackageLocation) -> ImportLocationRow:
    """一行存档位置: 本机已有同名路径时才预填, 否则留空."""
    exists = item.exists_here
    return ImportLocationRow(
        index=item.index,
        text=(
            tr("dialog.import_location_exists", path=item.path) if exists else item.path
        ),
        default=item.path if exists else "",
    )


# -- 批量导出 ---------------------------------------------------------------


@dataclass(frozen=True)
class BatchExportOption:
    """批量导出对话框里的一款游戏(一行复选框)."""

    game_id: str
    name: str
    detail: str  # 存档位置与备份数量的摘要(与主页列表的副标题同一口径)
    selected: bool = False
    #: 录入时的名称(译名探测/用户改名之前的那个): **筛选也按它匹配** —— 库里的名字
    #: 会被译名写回或用户改名换掉, 而用户脑子里记的可能还是当初那一个。
    original_name: str = ""


@dataclass(frozen=True)
class BatchExportPrompt:
    """批量导出对话框需要的文案与候选(纯数据, 无头环境也能构造与断言)."""

    summary: str
    filter_hint: str
    options: tuple[BatchExportOption, ...]


@dataclass(frozen=True)
class BatchExportChoice:
    """批量导出的选择(取消时对话框返回 ``None``)."""

    game_ids: tuple[str, ...]


def exportable_games(games: Sequence[GameSummary]) -> tuple[GameSummary, ...]:
    """批量导出可选的游戏: **已归档的不提供, 停用的照常提供**.

    归档表达"这款游戏我已经收起来了": 主页的默认视图不列它, 只有「已归档」视图才出现,
    所以批量导出也不把它混进来(要单独导出归档游戏仍可去详情页的「导出游戏」, 那条规则
    来自 ``domain.game_rules``, 这里没有改它)。

    **停用不是"收起来"**: 应用里"全局至多一款启用"是硬约束(见
    ``application.games.set_enabled``), 平时绝大多数游戏都是停用态 —— 把它们排除掉
    等于让批量导出一次只能导一款, 与这个功能的用途相悖; 主页动作行对停用游戏也一样
    提供备份/位置/标签等操作。因此这里只按归档过滤。
    """
    return tuple(game for game in games if not game.archived)


def _export_detail(game: GameSummary) -> str:
    """行尾的说明文字: 原名与界面名不同时把原名摆上, 否则只给位置/备份摘要.

    带上原名是为了"按原名搜得到"这件事可解释: 否则搜出来的那行文字里看不出跟输入有
    什么关系(而它确实命中了)。原名与界面名相同时不加, 免得白占宽度。
    """
    if game.original_name and game.original_name != game.name:
        return (
            f"{tr('hero.original_name', name=game.original_name)} · {game.list_detail}"
        )
    return game.list_detail


def export_batch_prompt(games: Sequence[GameSummary]) -> BatchExportPrompt:
    """把游戏库映射成批量导出对话框的文案与候选(默认一个都不勾选).

    默认不勾选是刻意的: 批量导出会写出一个可能很大的包, "点什么都没选"比"以为只导了
    一款却导出了全部"安全; 用户勾了才有下一步。
    """
    options = tuple(
        BatchExportOption(
            game_id=game.game_id,
            name=game.name,
            detail=_export_detail(game),
            selected=False,
            original_name=game.original_name,
        )
        for game in exportable_games(games)
    )
    return BatchExportPrompt(
        summary=tr("dialog.export_batch_summary", count=len(options)),
        filter_hint=tr("dialog.export_batch_filter_hint"),
        options=options,
    )


def filter_export_options(
    options: Sequence[BatchExportOption], query: str
) -> tuple[BatchExportOption, ...]:
    """按名称筛选候选: 空输入返回全部, 一条都不匹配就是空(界面据此给出提示).

    匹配的是**界面上的名称与录入时的原名**两者之一(各算一次包含, 大小写不敏感):
    库里那一个是译名写回或用户改名的结果, 用户可能拿当初的名字来搜.

    与定时任务窗口的选择框的唯一不同是: 过滤**只影响显示**。勾选状态存在按游戏 id
    索引的变量里, 被筛掉的行不会丢掉用户已经做过的选择(界面文案里也这么写)。
    """
    typed = query.strip().casefold()
    if not typed:
        return tuple(options)
    return tuple(option for option in options if _matches_any(option, typed))


def _matches_any(option: BatchExportOption, typed: str) -> bool:
    """界面名或原名任一命中就算匹配(两个名字可能相同, 也可能有个是空的)."""
    return any(
        typed in candidate.casefold()
        for candidate in (option.name, option.original_name)
        if candidate
    )


def batch_export_choice(
    selected: Mapping[str, bool], options: Sequence[BatchExportOption]
) -> BatchExportChoice:
    """把复选框状态整理成选择: 顺序与候选一致, 一个都没勾就是空选择."""
    return BatchExportChoice(
        game_ids=tuple(
            option.game_id for option in options if selected.get(option.game_id, False)
        )
    )


def batch_export_filename(count: int, *, moment: datetime) -> str:
    """批量导出包的默认文件名: 游戏数 + 时间戳(用户仍可改目录与文件名).

    带时间戳是让"同一个库导出两次"默认不互相覆盖; 名字里写明游戏数, 事后在文件管理器
    里也能一眼看出这一批有几款。后缀与单包导出一致(同一种归档包)。
    """
    return f"batch-{count}games-{moment:%Y%m%dT%H%M%S}{ARCHIVE_SUFFIX}"


# -- 批量导入 ---------------------------------------------------------------


@dataclass(frozen=True)
class BatchImportRow:
    """批量导入对话框里的一行(一款游戏的全部选项)."""

    entry: str
    name: str
    meta: str
    match_text: str
    locations: tuple[ImportLocationRow, ...]
    strategies: tuple[tuple[str, str], ...]
    targets: tuple[ImportTargetOption, ...]
    strategy: str
    target_game_id: str | None


@dataclass(frozen=True)
class BatchImportPrompt:
    """批量导入对话框需要的文案与逐游戏选项(纯数据, 无头环境也能断言)."""

    summary: str
    hint: str
    rows: tuple[BatchImportRow, ...]


@dataclass(frozen=True)
class BatchImportSelection:
    """批量导入的选择: 内层条目名 -> 该游戏的导入选择.

    界面**不做 id 换算**: 目标游戏仍是界面用的字符串 id, 由后端在写库前换成整型
    (与单包 :meth:`ArchiveService.run_import` 同一套做法).
    """

    choices: Mapping[str, ImportChoice]


def batch_import_prompt(
    batch: BatchInspection, games: Sequence[GameSummary]
) -> BatchImportPrompt:
    """把批量体检结果与游戏库映射成批量导入对话框的文案与逐行选项.

    每一行的默认值都与单包对话框一致: 策略默认"新建", 目标游戏预选"疑似同一款"
    (没有匹配就是库里的第一款), 存档位置只预填本机已存在的那些。行顺序与包内清单
    一致 —— 用户看到的顺序就是导出时的顺序。
    """
    return BatchImportPrompt(
        summary=tr(
            "dialog.import_batch_summary",
            games=batch.game_count,
            backups=batch.backup_count,
            files=batch.file_count,
            size=size_label(batch.total_bytes),
        ),
        hint=tr("dialog.import_batch_hint"),
        rows=tuple(_batch_row(item, games) for item in batch.games),
    )


def batch_row_choice(
    *, strategy: str, target: str | None, locations: Mapping[int, str]
) -> ImportChoice:
    """把一行控件的取值整理成 :class:`ImportChoice`.

    只有"合并"才带目标游戏; 目标为空时返回 ``None`` 而不是空串 —— 界面不替用户猜
    要合并到哪一款(真导入时后端会明确报"合并导入需要先选定要合并到的游戏")。存档位置
    去掉两端空白, 空串就是"这一条不导入"(与单包对话框同一条语义)。
    """
    return ImportChoice(
        strategy=strategy,
        target_game_id=target if strategy == STRATEGY_MERGE else None,
        locations={
            index: path.strip() for index, path in locations.items() if path.strip()
        },
    )


def target_game_id(targets: Sequence[ImportTargetOption], label: str) -> str | None:
    """把目标下拉框里的文案还原成游戏 id(找不到就是没选)."""
    text = label.strip()
    return next((option.game_id for option in targets if option.label == text), None)


def target_label_for(targets: Sequence[ImportTargetOption], game_id: str | None) -> str:
    """目标下拉框该显示的文案: 该游戏 id 对应的那一条, 找不到就用第一项."""
    for option in targets:
        if option.game_id == game_id:
            return option.label
    return targets[0].label if targets else ""


def unique_targets(
    games: Sequence[GameSummary], matching_id: str | None
) -> tuple[ImportTargetOption, ...]:
    """目标候选: 疑似同一款排最前并预选; **重名的补上 id**, 免得下拉框分不清.

    单包对话框用单选按钮承载游戏 id, 重名不会混淆; 批量对话框里每一行是一个下拉框
    (一款游戏一行, 铺不下整组单选按钮), 而下拉框的取值是文案 —— 于是只有**重名时**
    才加一个 "(游戏 id)" 后缀, 常见情况的文案保持干净。
    """
    options = _import_targets(games, matching_id)
    counts = Counter(option.label for option in options)
    return tuple(
        replace(option, label=f"{option.label} ({option.game_id})")
        if counts[option.label] > 1
        else option
        for option in options
    )


def _batch_row(
    item: BatchGameInspection, games: Sequence[GameSummary]
) -> BatchImportRow:
    """一行: 游戏标识 + 存档位置 + 策略与目标候选(默认值与单包对话框一致)."""
    inspection = item.inspection
    matching = (
        None
        if inspection.matching_game_id is None
        else str(inspection.matching_game_id)
    )
    targets = unique_targets(games, matching)
    return BatchImportRow(
        entry=item.entry,
        name=inspection.game_name,
        meta=_batch_meta(inspection),
        match_text=_import_match_text(games, matching),
        locations=tuple(_import_location(loc) for loc in inspection.locations),
        strategies=import_strategies(has_targets=bool(targets)),
        targets=targets,
        strategy=STRATEGY_NEW,
        target_game_id=_selected_target(targets),
    )


def _selected_target(targets: Sequence[ImportTargetOption]) -> str | None:
    """默认选中的目标: 预选项优先, 否则第一款(与单包对话框的默认一致)."""
    for option in targets:
        if option.selected:
            return option.game_id
    return targets[0].game_id if targets else None


def _batch_meta(inspection: ImportInspection) -> str:
    """一行游戏标识: 平台 + AppID(没有就不提) + 备份与文件数."""
    platform = PLATFORM_LABELS.get(inspection.platform, inspection.platform)
    key = (
        "dialog.import_batch_row_meta"
        if inspection.steam_app_id is not None
        else "dialog.import_batch_row_meta_unknown_app"
    )
    return tr(
        key,
        platform=platform,
        app_id=inspection.steam_app_id,
        backups=inspection.backup_count,
        files=inspection.file_count,
    )
