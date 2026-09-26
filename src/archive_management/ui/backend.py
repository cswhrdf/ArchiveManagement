"""UI 依赖的后端接口.

界面只依赖这里的接口: 真实实现由 SQLite 后端提供, 演示后端提供同一接口的
内存实现, 两者可互换, 因此 UI 不依赖具体业务实现。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from archive_management.application.games import ActivationOutcome
from archive_management.application.imports import BatchInspection, ImportInspection
from archive_management.application.locations import LocationRemovalPlan
from archive_management.application.restore import RestorePlan
from archive_management.domain import (
    DEFAULT_KEEP_AUTO,
    ArtworkKind,
    DeletionPlan,
    HomeFilter,
    PathKind,
)
from archive_management.ui.models import (
    BackupItem,
    CandidateItem,
    GameDetail,
    GameSummary,
    HomeBoard,
    ImportChoice,
    LocationItem,
    MonitoredDirItem,
    ScanSummary,
    ScheduleItem,
    TaskStatus,
)


@runtime_checkable
class ArchiveService(Protocol):
    """UI 消费的存档管理能力集合."""

    def list_games(self) -> list[GameSummary]:
        """返回所有游戏摘要."""
        ...

    def get_detail(self, game_id: str) -> GameDetail:
        """返回单个游戏的概要数据."""
        ...

    def list_backups(self, game_id: str) -> list[BackupItem]:
        """返回某个游戏的备份节点(含时间线/分支视图所需信息)."""
        ...

    def task_status(self, game_id: str | None = None) -> TaskStatus:
        """返回定时任务与运行环境状态(可按游戏查看周期配置)."""
        ...

    def current_theme(self) -> str:
        """返回当前主题名(dark/light)."""
        ...

    def set_theme(self, name: str) -> str:
        """切换主题并返回最终生效的主题名."""
        ...

    def run_backup_now(self, game_id: str) -> str:
        """立即创建一次备份;成功返回提示,失败抛出异常."""
        ...

    def run_restore(
        self,
        game_id: str,
        backup_id: str,
        *,
        safety_point: bool = True,
        force: bool = False,
    ) -> str:
        """把备份内容写回原始存档位置, 并把当前节点切到该备份.

        ``safety_point`` 为 True 时恢复前先创建一份安全点备份(只在时间线视图
        展示), 使恢复可逆; ``force`` 用于在检测到游戏进程运行时由用户明确
        强制执行。
        """
        ...

    def preview_restore(self, game_id: str, backup_id: str) -> RestorePlan:
        """恢复预检: 快照完整性、写回目标、多余文件与游戏进程状态."""
        ...

    def run_create_branch(self, game_id: str, backup_id: str, branch_name: str) -> str:
        """从指定节点创建分支."""
        ...

    def run_export(self, game_id: str, destination: str) -> str:
        """把该游戏导出为归档包到 ``destination``, 返回本地化的成功提示.

        ``destination`` 是用户在保存对话框里选定的文件路径; 实现负责把包真的是
        写到那里(或明确说明没有写文件), 失败时抛出异常由界面提示。
        """
        ...

    def run_export_batch(self, game_ids: Sequence[str], destination: str) -> str:
        """把若干款游戏一起导出为**批量包**到 ``destination``, 返回本地化提示.

        ``game_ids`` 是用户在多选对话框里勾选的游戏(顺序即包内顺序); 空选择或未知
        游戏都抛异常。提示里带真实的游戏/备份/文件数与体积。**任何一款失败或取消都
        让整批失败**: 目标路径上不留文件(服务层保证), 因此这里的取消不需要回滚。
        """
        ...

    def inspect_import(self, path: str) -> ImportInspection | BatchInspection:
        """只读地体检一个导出包, 返回包内容与冲突项.

        单游戏包与批量包都从这里进(按包清单里的类型分派), 因此调用方拿到结果后要先
        判断类型再决定弹哪个冲突对话框。这一步**不改数据库, 也不在备份根下写任何
        东西**: 界面先把包里的游戏标识、存档位置、备份节点与"疑似同一款"的库内匹配
        结果摆给用户看, 由用户决定导入方式。文件读不动/包损坏时抛异常由界面提示。
        """
        ...

    def run_import(
        self,
        inspection: ImportInspection,
        *,
        strategy: str,
        target_game_id: str | None,
        locations: Mapping[int, str],
    ) -> str:
        """按 ``strategy`` 把体检过的包导入本机, 返回本地化的结果提示.

        这一步会**真的写数据库与备份根**(新建游戏、追加备份节点、登记存档位置),
        但**永不覆盖**: 已存在的节点目录按跳过计数, 因此重复导入同一个包不会产生
        重复数据。``target_game_id`` 只在合并策略下使用; ``locations`` 是“包内序号
        -> 本机路径”的映射, 没有映射的存档位置不导入。失败时抛出异常, 已写的节点
        会被回滚。
        """
        ...

    def run_import_batch(
        self, batch: BatchInspection, choices: Mapping[str, ImportChoice]
    ) -> str:
        """按每款游戏各自的选择导入整批, 返回带真实计数的本地化提示.

        ``choices`` 以**内层条目名**为键(与 :class:`BatchInspection.games` 里的
        ``entry`` 一致); 没有给选择的那一款按默认值处理(新建游戏且不导入任何存档
        位置)。界面只传字符串 id, 换算成整型是后端的事(与 :meth:`run_import` 同一套
        做法)。**失败或取消会让整批停下, 但已经导完的游戏原样保留**(服务层的边界),
        因此界面在取消后仍要刷新一次 —— 用户需要看到已经导进来了哪些。
        """
        ...

    # -- 游戏与存档位置管理 -----------------------------------------------

    def add_game(self, name: str) -> GameSummary:
        """新增一个游戏, 返回其摘要."""
        ...

    def update_game(self, game_id: str, name: str) -> GameSummary:
        """重命名游戏并返回其摘要."""
        ...

    def delete_export_path(self, game_id: str) -> str:
        """算出删除该游戏时"告别包"的默认落点, **只计算路径**.

        默认放在备份目录旁边的 ``exports`` 目录里, 文件名由游戏名与时间戳派生
        (``<slug>-<YYYYMMDD-HHMMSS>.archive.zip``); 同名文件已存在时追加 ``-2``/
        ``-3`` 保证每次自动导出落在不同的名字上, **绝不覆盖**上一份。实现**不建
        目录也不写文件**: 界面要在确认框里先把这条路径给用户看, 真正落盘发生在
        :meth:`delete_game` 里(失败就什么都不删)。
        """
        ...

    def delete_game(self, game_id: str, destination: str) -> None:
        """把游戏导出到 ``destination`` **再**删除它的记录.

        导出的是这款游戏的完整内容(配置 + 每一个备份节点), 与「导出游戏」按钮
        产生的是同一种包。**导出成功之前一个字段都不删**: 导出失败时抛出异常,
        游戏记录、存档位置、探测候选记账与备份节点全部保持原样 —— 用户不能因为
        删除而丢掉一款连告别包都写不出来的游戏。导出成功后才删记录(它的存档位置
        与候选记账随之清除), 磁盘上备份目录里的备份文件保留。
        """
        ...

    def set_game_enabled(self, game_id: str, enabled: bool) -> GameSummary:
        """启用/停用一个游戏, 返回其摘要."""
        ...

    def poll_activation(self, *, enabled: bool = True) -> ActivationOutcome:
        """低频轮询一次按进程自动启停, 返回本次的判断结果.

        ``enabled`` 就是设置里的开关: 关闭时**一次进程表都不枚举**。只有真实后端
        实现自动启停, 演示后端固定返回"无变化"。
        """
        ...

    def list_locations(self, game_id: str) -> list[LocationItem]:
        """返回某游戏的全部存档位置及校验状态."""
        ...

    def add_location(self, game_id: str, *, path: str, kind: PathKind) -> LocationItem:
        """新增并校验一个存档位置; 路径不可用或重复时抛出异常."""
        ...

    def update_location(
        self,
        location_id: str,
        *,
        path: str | None = None,
        kind: PathKind | None = None,
    ) -> LocationItem:
        """修改存档位置的路径/类型并重新校验."""
        ...

    def remove_location(self, location_id: str) -> None:
        """删除一个存档位置记录."""
        ...

    def set_primary_location(self, game_id: str, location_id: str) -> LocationItem:
        """把某位置设为主位置(其余位置清除主标记)."""
        ...

    def verify_location(self, location_id: str) -> LocationItem:
        """重新校验一个存档位置并返回最新状态."""
        ...

    def preview_location_removal(self, location_id: str) -> LocationRemovalPlan:
        """删除原始存档目录前的预检: 影响范围与路径安全判定."""
        ...

    def delete_save_location(self, location_id: str, *, confirm_name: str) -> str:
        """把原始存档目录移入系统回收站并删除该位置记录."""
        ...

    # -- 备份、调度与生命周期 --------------------------------------------

    def set_schedule(
        self,
        game_id: str,
        interval_text: str,
        *,
        enabled: bool = True,
        keep_auto: int = DEFAULT_KEEP_AUTO,
    ) -> TaskStatus:
        """设置或取消某游戏的定期备份, 返回最新任务状态."""
        ...

    def list_schedules(self) -> list[ScheduleItem]:
        """返回全部游戏的定时备份配置(供全局任务窗口展示/编辑)."""
        ...

    def storage_usage(self) -> int:
        """返回备份存储当前占用的字节数(供状态栏显示)."""
        ...

    def plan_delete(self, game_id: str, backup_id: str) -> DeletionPlan:
        """返回删除某备份的计划(是否需要二次确认由 mode 决定)."""
        ...

    def run_delete_backup(self, game_id: str, backup_id: str) -> str:
        """按分支树删除备份: 同线路节点让后续上移, 分支根节点连带子分支."""
        ...

    def rename_backup(
        self, game_id: str, backup_id: str, *, title: str, note: str
    ) -> BackupItem:
        """修改备份的名称与描述, 返回更新后的列表项."""
        ...

    def cancel_active(self) -> bool:
        """请求取消当前正在进行的后台操作; 无进行中操作时返回 False."""
        ...

    # -- 本地游戏探测与监控目录 ------------------------------------------

    def list_monitored_directories(self) -> list[MonitoredDirItem]:
        """返回全部监控目录及其启用状态、路径健康状态."""
        ...

    def add_monitored_directory(self, path: str, *, note: str = "") -> MonitoredDirItem:
        """新增监控目录; 路径不可用或重复时抛出异常."""
        ...

    def update_monitored_directory(
        self, directory_id: str, *, path: str | None = None, note: str | None = None
    ) -> MonitoredDirItem:
        """修改监控目录的路径或备注; 新路径不可用或重复时抛出异常."""
        ...

    def set_monitored_enabled(
        self, directory_id: str, enabled: bool
    ) -> MonitoredDirItem:
        """启用/停用一个监控目录."""
        ...

    def remove_monitored_directory(self, directory_id: str) -> None:
        """删除一个监控目录记录(不删除磁盘内容)."""
        ...

    def scan_candidates(self) -> ScanSummary:
        """扫描平台安装目录与监控目录, 返回结果摘要(失败也不抛异常)."""
        ...

    def list_candidates(self, *, status: str | None = None) -> list[CandidateItem]:
        """返回探测到的候选游戏(可按处理进度筛选)."""
        ...

    def import_candidate(
        self,
        candidate_id: str,
        *,
        name: str | None = None,
        save_paths: Sequence[str] = (),
    ) -> GameSummary:
        """把一条探测结果导入为游戏, 并写入用户确认的存档路径.

        ``save_paths`` 为空表示这次导入不带存档位置(用户把它们都去掉了); 路径里
        的每一项都还会过一遍危险位置校验, 拒绝的会抛错。
        """
        ...

    def set_candidate_ignored(self, candidate_id: str, ignored: bool) -> CandidateItem:
        """把候选标记为"已忽略"或恢复为"待处理"."""
        ...

    def relocate_candidate(self, candidate_id: str, path: str) -> CandidateItem:
        """修正候选的安装路径, 返回更新后的候选."""
        ...

    def add_candidate_as_monitored(self, candidate_id: str) -> MonitoredDirItem:
        """把候选所在的上一层目录加入监控列表."""
        ...

    # -- 图片 ---------------------------------------------------------------

    def artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
        """返回封面/图标的本地路径(只查缓存与平台本地资源, 不联网)."""
        ...

    def prefetch_artwork(self) -> None:
        """后台补齐缺失的封面与图标; 不阻塞界面, 完成后由界面自行重读."""
        ...

    # -- 游戏译名(按当前界面语言) -----------------------------------------

    def prefetch_names(self, *, refresh: bool = False) -> None:
        """后台按当前界面语言补齐游戏译名; 取不到译名的游戏保留探测到的原名.

        只改"名字还是程序写的"那一批(用户改过名的尊重用户); 译名按语言分条缓存,
        切换语言时新语言缺条目的才联网, 取过的直接复用。``refresh`` 是诊断用的
        强制重取口子, 界面里没有入口。
        """
        ...

    # -- 统一游戏主页与分类视图 ------------------------------------------

    def load_home(self) -> HomeBoard:
        """返回主页数据(沿用上次保存的筛选条件)."""
        ...

    def apply_home_filter(self, active: HomeFilter) -> HomeBoard:
        """按给定筛选条件重算主页并保存该条件(下一次打开仍是这个视图)."""
        ...

    def set_game_archived(self, game_id: str, archived: bool) -> HomeBoard:
        """归档或取消归档一个游戏, 返回重算后的主页数据."""
        ...

    def set_game_tags(self, game_id: str, tags: Sequence[str]) -> HomeBoard:
        """覆盖写入游戏的自定义标签, 返回重算后的主页数据."""
        ...

    def shutdown(self) -> None:
        """释放后台资源(调度器、监听器); 幂等, 供应用退出时调用."""
        ...
