"""基于 SQLite 的真实后端(阶段 C/D/E).

把 :mod:`archive_management.domain` 实体与仓库映射为 UI 展示模型,
实现 :class:`~archive_management.ui.backend.ArchiveService`. 本后端是
GUI 的默认数据来源; 备份、分支、恢复与删除原始存档目录都接入真实服务层,
导入导出仍给出明确的阶段提示而非静默忽略.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from archive_management.application import discovery as discovery_cases
from archive_management.application import home as home_cases
from archive_management.application.backup import BackupService
from archive_management.application.locations import (
    LocationRemovalPlan,
    plan_location_removal,
    remove_save_location,
)
from archive_management.application.restore import RestorePlan, RestoreService
from archive_management.domain import (
    DEFAULT_KEEP_AUTO,
    BackupNode,
    CandidateStatus,
    DeletionMode,
    DeletionPlan,
    Game,
    GameCandidate,
    HomeFilter,
    MonitoredDirectory,
    NodeKind,
    PathKind,
    SaveLocation,
    ScheduledJob,
    candidate_sort_key,
)
from archive_management.exceptions import (
    ArchiveManagementError,
    ContentUnchangedError,
    OperationCancelledError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    CandidateRepository,
    GameRepository,
    MonitoredDirectoryRepository,
    SaveLocationRepository,
    ScheduledJobRepository,
)
from archive_management.services.audit import log_action, redacted_path
from archive_management.services.pathcheck import (
    normalize_path,
    probe_path,
    summarize_path,
)
from archive_management.services.platform_scan import path_health
from archive_management.services.scheduler import (
    BackupScheduler,
    format_interval,
    parse_interval,
)
from archive_management.services.snapshot import verify_snapshot
from archive_management.ui.models import (
    BackupItem,
    CandidateItem,
    GameDetail,
    GameSummary,
    HomeBoard,
    LocationItem,
    MonitoredDirItem,
    ScanSummary,
    ScheduleItem,
    TaskStatus,
    home_board,
    size_label,
)

logger = logging.getLogger(__name__)

_TONES = ("orange", "blue", "green")
_DEFAULT_THEME = "dark"


def _tone(name: str) -> str:
    """按名称确定性选择一个头像色调."""
    digest = sum(name.encode("utf-8"))
    return _TONES[digest % len(_TONES)]


def _stamp(moment: datetime) -> str:
    """把时间格式化为本地时区的展示文本."""
    return moment.astimezone().strftime("%Y/%m/%d %H:%M")


def _default_title(node: BackupNode) -> str:
    """按节点类型返回缺省标题."""
    if node.node_kind == "auto":
        return tr("backup.title_auto")
    if node.node_kind == "branch":
        return tr("backup.title_branch")
    return tr("backup.title_manual")


def _interval_label(minutes: int) -> str:
    """把分钟数格式化为与调度输入一致的精简周期文本."""
    return format_interval(minutes)


@dataclass
class _ActiveOperation:
    """一次正在进行中的备份(用于进度展示与取消请求)."""

    game_id: int
    fraction: float = 0.0
    message: str = ""
    cancel_requested: bool = False


class SqlArchiveService:
    """基于 :class:`Database` 的真实 :class:`ArchiveService` 实现."""

    def __init__(
        self,
        database: Database,
        *,
        backup_root: Path,
        scheduler: BackupScheduler | None = None,
    ) -> None:
        """绑定数据库、备份根目录与调度器(默认惰性创建)."""
        self._database = database
        self._backup_root = backup_root
        self._games = GameRepository(database)
        self._locations = SaveLocationRepository(database)
        self._nodes = BackupRepository(database)
        self._jobs = ScheduledJobRepository(database)
        self._monitored = MonitoredDirectoryRepository(database)
        self._candidates = CandidateRepository(database)
        self._backups = BackupService(database, backup_root=backup_root)
        self._restore = RestoreService(
            database, backup_root=backup_root, backups=self._backups
        )
        self._scheduler = scheduler if scheduler is not None else BackupScheduler()
        self._theme = _DEFAULT_THEME
        self._revision = 0
        self._operation_lock = threading.Lock()
        self._active: _ActiveOperation | None = None
        self._verify_cache: dict[str, bool] = {}
        self._restore_schedules()

    def _restore_schedules(self) -> None:
        """启动时从数据库恢复已配置的定时备份.

        没有这一步, 重启后 ``scheduled_jobs`` 里的配置就不会生效: 界面会显示
        "未配置定时备份"、下次运行时间为空, 也不会自动触发备份。
        """
        restored = 0
        for job in self._jobs.list_all():
            if job.game_id is None or not job.schedule.strip():
                continue
            try:
                minutes = parse_interval(job.schedule)
            except ArchiveManagementError as exc:
                logger.warning(
                    "忽略无法解析的定时任务: game=%s schedule=%r (%s)",
                    job.game_id,
                    job.schedule,
                    exc,
                )
                continue
            self._scheduler.schedule(
                job.game_id,
                minutes,
                self._scheduled_backup(job.game_id),
                enabled=job.enabled,
            )
            restored += 1
        if restored:
            log_action("schedule.restore", basic=True, count=restored)

    @property
    def _data_revision(self) -> int:
        """备份数据版本号: UI 据此判断列表是否需要重载."""
        return self._revision

    def _touch(self) -> None:
        """标记备份数据已变化(含定时备份/快捷键触发的后台变更)."""
        self._revision += 1
        self._verify_cache.clear()

    # -- 游戏 CRUD ---------------------------------------------------------

    def list_games(self) -> list[GameSummary]:
        """返回全部游戏摘要(含位置/备份计数)."""
        return [self._summary(game) for game in self._games.list()]

    def get_detail(self, game_id: str) -> GameDetail:
        """返回单个游戏的概要数据."""
        game, gid = self._game_ref(game_id)
        locations = self._locations.list_for_game(gid)
        primary = next(
            (location for location in locations if location.is_primary),
            locations[0] if locations else None,
        )
        if primary is not None:
            probe = probe_path(primary.path, primary.path_kind)
            main = primary.path
            verified = probe.ok
            note = (
                tr("game.primary_location")
                if primary.is_primary
                else tr("game.location_managed")
            )
        else:
            main = ""
            verified = False
            note = tr("game.no_locations_short")
        backups = self._nodes.count(gid)
        latest = self._backups.latest(gid)
        if latest is not None:
            facts = self._backups.facts(latest)
            created = latest.created_at or datetime.now(UTC)
            last_label = _stamp(created)
            last_sub = tr(
                "detail.last_backup_sub",
                kind=(
                    tr("backup.kind_auto")
                    if latest.node_kind == "auto"
                    else tr("backup.kind_manual")
                ),
                size=size_label(facts.total_size),
            )
        else:
            last_label = "—"
            last_sub = ""
        next_run = self._next_run(gid)
        return GameDetail(
            name=game.name,
            subtitle=tr("detail.subtitle"),
            main_location=main,
            location_verified=verified,
            location_note=note,
            last_backup_label=last_label,
            last_backup_sub=last_sub,
            total_backups_label=(
                tr("detail.backups_none")
                if backups == 0
                else tr("detail.backups_count", count=backups)
            ),
            total_backups_sub="",
            next_backup_label=_stamp(next_run) if next_run is not None else "—",
            original_name=game.original_name or game.name,
            storage_folder=game.storage_key,
        )

    def add_game(self, name: str) -> GameSummary:
        """新增游戏, 返回其摘要."""
        clean = self._clean_name(name)
        game = self._games.add(Game(name=clean))
        log_action("game.add", game_id=game.id, name=clean)
        return self._summary(game)

    def update_game(self, game_id: str, name: str) -> GameSummary:
        """重命名游戏, 返回其摘要."""
        clean = self._clean_name(name)
        game = self._game(game_id)
        # 只改名称: 首次录入的原始名称与磁盘目录名保持不变.
        updated = game.model_copy(update={"name": clean})
        self._games.update(updated)
        log_action("game.rename", game_id=game.id, name=clean)
        return self._summary(updated)

    def delete_game(self, game_id: str) -> None:
        """删除游戏记录及其存档位置."""
        game, gid = self._game_ref(game_id)
        self._games.delete(gid)
        log_action("game.delete", game_id=gid, name=game.name)

    def set_game_enabled(self, game_id: str, enabled: bool) -> GameSummary:
        """启用或停用一个游戏."""
        game = self._game(game_id)
        updated = game.model_copy(update={"enabled": enabled})
        self._games.update(updated)
        log_action("game.set_enabled", game_id=game.id, enabled=enabled)
        return self._summary(updated)

    # -- 存档位置管理 ------------------------------------------------------

    def list_locations(self, game_id: str) -> list[LocationItem]:
        """返回某游戏的全部存档位置及校验状态."""
        _game, gid = self._game_ref(game_id)
        return [
            self._location_item(location)
            for location in self._locations.list_for_game(gid)
        ]

    def add_location(self, game_id: str, *, path: str, kind: PathKind) -> LocationItem:
        """新增并校验存档位置; 路径不可用或重复时抛出异常."""
        _game, gid = self._game_ref(game_id)
        normalized = normalize_path(path)
        if self._locations.duplicate_of(gid, normalized) is not None:
            raise ArchiveManagementError(
                tr("error.duplicate_location", path=normalized)
            )
        self._require_path(normalized, kind)
        has_existing = bool(self._locations.list_for_game(gid))
        now = datetime.now(UTC)
        location = self._locations.add(
            SaveLocation(
                game_id=gid,
                path=normalized,
                path_kind=kind,
                source="manual",
                is_primary=not has_existing,
                last_checked_at=now,
                last_check_status="ok",
            )
        )
        log_action(
            "location.add",
            game_id=gid,
            location_id=location.id,
            kind=kind,
            path=redacted_path(normalized),
        )
        return self._location_item(location)

    def update_location(
        self,
        location_id: str,
        *,
        path: str | None = None,
        kind: PathKind | None = None,
    ) -> LocationItem:
        """修改存档位置的路径/类型并重新校验."""
        location = self._location(location_id)
        new_path = location.path if path is None else normalize_path(path)
        new_kind = location.path_kind if kind is None else kind
        if new_path != location.path:
            duplicate = self._locations.duplicate_of(location.game_id, new_path)
            if duplicate is not None and duplicate.id != location.id:
                raise ArchiveManagementError(
                    tr("error.duplicate_location", path=new_path)
                )
            self._require_path(new_path, new_kind)
        changed = SaveLocation(
            id=location.id,
            game_id=location.game_id,
            path=new_path,
            path_kind=new_kind,
            source=location.source,
            is_primary=location.is_primary,
            last_checked_at=datetime.now(UTC),
            last_check_status="ok",
        )
        self._locations.update(changed)
        log_action(
            "location.update",
            game_id=location.game_id,
            location_id=location.id,
            path=redacted_path(new_path),
        )
        return self._location_item(changed)

    def remove_location(self, location_id: str) -> None:
        """删除指定存档位置记录."""
        _location, lid = self._location_ref(location_id)
        self._locations.delete(lid)
        log_action("location.remove", location_id=lid)

    def set_primary_location(self, game_id: str, location_id: str) -> LocationItem:
        """把某位置设为主位置并返回更新后的条目."""
        _game, gid = self._game_ref(game_id)
        location, lid = self._location_ref(location_id)
        if location.game_id != gid:
            raise ArchiveManagementError(
                tr("error.unknown_location", location_id=location_id)
            )
        self._locations.make_primary(gid, lid)
        log_action("location.set_primary", game_id=gid, location_id=lid)
        return self._location_item(location)

    def verify_location(self, location_id: str) -> LocationItem:
        """重新校验存档位置, 把结果写回数据库."""
        location = self._location(location_id)
        probe = probe_path(location.path, location.path_kind)
        changed = SaveLocation(
            id=location.id,
            game_id=location.game_id,
            path=location.path,
            path_kind=location.path_kind,
            source=location.source,
            is_primary=location.is_primary,
            last_checked_at=datetime.now(UTC),
            last_check_status="ok" if probe.ok else probe.reason_code,
        )
        self._locations.update(changed)
        log_action(
            "location.verify",
            basic=True,
            location_id=location.id,
            status=changed.last_check_status,
        )
        return self._location_item(changed)

    def preview_location_removal(self, location_id: str) -> LocationRemovalPlan:
        """删除原始存档目录前的预检(影响范围与路径安全判定)."""
        _location, lid = self._location_ref(location_id)
        return plan_location_removal(self._database, lid, protect=(self._backup_root,))

    def delete_save_location(self, location_id: str, *, confirm_name: str) -> str:
        """把原始存档目录移入系统回收站, 并删除该位置记录."""
        _location, lid = self._location_ref(location_id)
        try:
            result = remove_save_location(
                self._database,
                lid,
                confirm_name=confirm_name,
                protect=(self._backup_root,),
            )
        except ArchiveManagementError as exc:
            raise ArchiveManagementError(
                tr("error.location_delete_failed", reason=str(exc))
            ) from exc
        self._touch()
        return tr("result.location_deleted", path=result.path, count=result.files)

    def list_backups(self, game_id: str) -> list[BackupItem]:
        """返回某游戏的备份节点(含分支树层级与校验状态)."""
        _game, gid = self._game_ref(game_id)
        depths = {node.node_id: node.depth for node in self._backups.tree(gid)}
        # 用"有效当前节点": 指针缺失或指向已删除节点时回退到最新备份,
        # 保证删除当前节点后依然能看出接下来的备份会挂在哪里.
        current_node = self._backups.current_node(gid)
        current = None if current_node is None else current_node.id
        return [
            self._backup_item(node, depths.get(str(node.id), 0), current)
            for node in self._backups.list_nodes(gid)
        ]

    def task_status(self, game_id: str | None = None) -> TaskStatus:
        """返回运行环境、进行中操作与定期备份状态."""
        gid = self._optional_game_id(game_id)
        with self._operation_lock:
            active = self._active
        entry = self._scheduler.get(gid) if gid is not None else None
        next_run = self._next_run(gid) if gid is not None else None
        error = self._scheduler.last_error(gid) if gid is not None else None
        # 暂停不算"未配置": 周期仍然保留, 只是不触发。
        configured = entry is not None
        return TaskStatus(
            running=active is not None,
            task_name=(
                tr(
                    "task.every",
                    interval=_interval_label(entry.interval_minutes),
                    keep=self._keep_auto(gid),
                )
                if configured and entry is not None
                else tr("task.unscheduled")
            ),
            progress=active.fraction if active is not None else 0.0,
            next_run_label=_stamp(next_run) if next_run is not None else "—",
            target_label=str(self._backup_root),
            shortcut_label="—",
            theme_name=self._theme,
            backend_ok=True,
            schedule_text=(
                _interval_label(entry.interval_minutes)
                if configured and entry is not None
                else ""
            ),
            schedule_enabled=(entry.enabled if entry is not None else True),
            cancellable=active is not None,
            progress_label=(active.message if active is not None else (error or "")),
            keep_auto=self._keep_auto(gid),
            revision=self._data_revision,
        )

    def list_schedules(self) -> list[ScheduleItem]:
        """返回全部游戏的定时备份配置(未配置的游戏也会出现, 便于新增).

        任务来自哪个游戏、下次运行时间、当前保留的自动备份份数都在这里
        组装好, 供全局任务窗口直接展示。
        """
        items: list[ScheduleItem] = []
        for game in self._games.list():
            if game.id is None:
                continue
            entry = self._scheduler.get(game.id)
            jobs = self._jobs.for_game(game.id)
            job = jobs[0] if jobs else None
            next_run = (
                entry.next_run_at
                if entry is not None and entry.enabled
                else (job.next_run_at if job is not None else None)
            )
            items.append(
                ScheduleItem(
                    game_id=str(game.id),
                    game_name=game.name,
                    interval_text=(
                        _interval_label(entry.interval_minutes)
                        if entry is not None
                        else (job.schedule if job is not None else "")
                    ),
                    enabled=entry.enabled if entry is not None else True,
                    keep_auto=job.keep_auto if job is not None else DEFAULT_KEEP_AUTO,
                    next_run_label=_stamp(next_run) if next_run is not None else "—",
                    auto_count=self._auto_backup_count(game.id),
                    last_error=(job.last_error if job is not None else "") or "",
                    has_locations=self._games.count_locations(game.id) > 0,
                    tone=_tone(game.name),
                )
            )
        return items

    def _auto_backup_count(self, game_id: int) -> int:
        """统计某游戏当前保留的自动备份(含恢复前安全点之外的自动备份)份数."""
        return sum(
            1 for node in self._nodes.list_for_game(game_id) if node.node_kind == "auto"
        )

    def storage_usage(self) -> int:
        """返回备份存储当前占用的字节数(状态栏显示)."""
        summary = summarize_path(str(self._backup_root))
        return summary.total_size

    def current_theme(self) -> str:
        """返回当前主题名."""
        return self._theme

    def set_theme(self, name: str) -> str:
        """切换主题 (dark/light), 返回生效主题."""
        self._theme = "dark" if name == "dark" else "light"
        log_action("ui.set_theme", basic=True, theme=self._theme)
        return self._theme

    # -- 本地游戏探测与监控目录(阶段 E-1) -------------------------------

    def list_monitored_directories(self) -> list[MonitoredDirItem]:
        """返回全部监控目录及其实时路径状态."""
        return [self._monitored_item(item) for item in self._monitored.list_all()]

    def add_monitored_directory(self, path: str, *, note: str = "") -> MonitoredDirItem:
        """新增监控目录; 路径不可用或重复时抛出异常."""
        created = discovery_cases.add_monitored_directory(
            self._database, path, note=note
        )
        return self._monitored_item(created)

    def update_monitored_directory(
        self, directory_id: str, *, path: str | None = None, note: str | None = None
    ) -> MonitoredDirItem:
        """修改监控目录的路径或备注."""
        updated = discovery_cases.update_monitored_directory(
            self._database, self._monitored_ref(directory_id), path=path, note=note
        )
        return self._monitored_item(updated)

    def set_monitored_enabled(
        self, directory_id: str, enabled: bool
    ) -> MonitoredDirItem:
        """启用/停用一个监控目录."""
        updated = discovery_cases.set_monitored_enabled(
            self._database, self._monitored_ref(directory_id), enabled
        )
        return self._monitored_item(updated)

    def remove_monitored_directory(self, directory_id: str) -> None:
        """删除一个监控目录记录(磁盘内容不动)."""
        discovery_cases.remove_monitored_directory(
            self._database, self._monitored_ref(directory_id)
        )

    def scan_candidates(self) -> ScanSummary:
        """扫描平台安装目录与监控目录, 返回结果摘要.

        扫描会同时刷新监控目录的上次扫描时间与候选的路径健康状态。
        """
        report = discovery_cases.scan_library(self._database)
        return ScanSummary(
            monitored=report.monitored,
            active=report.active,
            total=report.total,
            added=report.added,
            updated=report.updated,
            linked=report.linked,
            unusable=report.unusable,
            errors=report.errors,
        )

    def list_candidates(self, *, status: str | None = None) -> list[CandidateItem]:
        """返回探测到的候选游戏(可按处理进度筛选)."""
        wanted = self._candidate_status(status)
        items = self._candidates.list_all(status=wanted)
        return [
            self._candidate_item(item) for item in sorted(items, key=candidate_sort_key)
        ]

    def import_candidate(
        self, candidate_id: str, *, name: str | None = None
    ) -> GameSummary:
        """把一条探测结果导入为游戏, 返回新游戏的摘要."""
        game = discovery_cases.import_candidate(
            self._database, self._candidate_ref(candidate_id), name=name
        )
        self._touch()
        return self._summary(game)

    def set_candidate_ignored(self, candidate_id: str, ignored: bool) -> CandidateItem:
        """把候选标记为"已忽略"或恢复为"待处理"."""
        reference = self._candidate_ref(candidate_id)
        candidate = (
            discovery_cases.ignore_candidate(self._database, reference)
            if ignored
            else discovery_cases.restore_candidate(self._database, reference)
        )
        return self._candidate_item(candidate)

    def relocate_candidate(self, candidate_id: str, path: str) -> CandidateItem:
        """修正候选的安装路径."""
        candidate = discovery_cases.relocate_candidate(
            self._database, self._candidate_ref(candidate_id), path
        )
        return self._candidate_item(candidate)

    def add_candidate_as_monitored(self, candidate_id: str) -> MonitoredDirItem:
        """把候选所在的上一层目录加入监控列表."""
        created = discovery_cases.add_candidate_as_monitored(
            self._database, self._candidate_ref(candidate_id)
        )
        return self._monitored_item(created)

    @staticmethod
    def _monitored_item(directory: MonitoredDirectory) -> MonitoredDirItem:
        """把监控目录实体映射为展示模型(路径状态实时判定)."""
        return MonitoredDirItem(
            directory_id=str(directory.id),
            path=directory.path,
            enabled=directory.enabled,
            note=directory.note,
            health=path_health(directory.path),
            last_scan_label=(
                _stamp(directory.last_scan_at)
                if directory.last_scan_at is not None
                else ""
            ),
        )

    @staticmethod
    def _candidate_item(candidate: GameCandidate) -> CandidateItem:
        """把候选实体映射为展示模型(路径状态实时判定)."""
        return CandidateItem(
            candidate_id=str(candidate.id),
            name=candidate.name,
            install_dir=candidate.install_dir,
            source=candidate.source,
            confidence=candidate.confidence,
            status=candidate.status,
            health=path_health(candidate.install_dir),
            detail=candidate.detail,
            game_id=None if candidate.game_id is None else str(candidate.game_id),
        )

    # -- 统一游戏主页(阶段 E-2) -------------------------------------------

    def load_home(self) -> HomeBoard:
        """返回主页数据, 沿用上次保存的筛选条件(第一次打开用默认视图)."""
        return self._board(home_cases.load_home(self._database))

    def apply_home_filter(self, active: HomeFilter) -> HomeBoard:
        """按给定筛选条件重算主页并保存该条件."""
        saved = home_cases.save_filter(self._database, active)
        return self._board(home_cases.filter_home(self._database, saved))

    def set_game_archived(self, game_id: str, archived: bool) -> HomeBoard:
        """归档或取消归档一个游戏, 返回重算后的主页数据."""
        _game, gid = self._game_ref(game_id)
        home_cases.set_archived(self._database, gid, archived)
        self._touch()
        return self.load_home()

    def set_game_tags(self, game_id: str, tags: Sequence[str]) -> HomeBoard:
        """覆盖写入游戏的自定义标签, 返回重算后的主页数据."""
        _game, gid = self._game_ref(game_id)
        home_cases.set_tags(self._database, gid, list(tags))
        self._touch()
        return self.load_home()

    def _board(self, report: home_cases.HomeReport) -> HomeBoard:
        """把主页用例结果映射为展示模型(映射逻辑与演示后端共用)."""
        return home_board(report, stamp=_stamp)

    def _monitored_ref(self, directory_id: str) -> int:
        """把界面传入的字符串 id 解析为存在的监控目录 id."""
        reference = self._parse_id(directory_id)
        if reference is None or self._monitored.get(reference) is None:
            raise ArchiveManagementError(
                tr("error.unknown_monitored", directory_id=directory_id)
            )
        return reference

    def _candidate_ref(self, candidate_id: str) -> int:
        """把界面传入的字符串 id 解析为存在的候选 id."""
        reference = self._parse_id(candidate_id)
        if reference is None or self._candidates.get(reference) is None:
            raise ArchiveManagementError(
                tr("error.unknown_candidate", candidate_id=candidate_id)
            )
        return reference

    @staticmethod
    def _parse_id(raw: str) -> int | None:
        """尝试把字符串 id 解析为整数; 非法时返回 None."""
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _candidate_status(status: str | None) -> CandidateStatus | None:
        """把筛选下拉框的取值映射为仓库状态参数."""
        if status in (None, "", "all"):
            return None
        if status in ("new", "imported", "ignored"):
            return cast(CandidateStatus, status)
        raise ArchiveManagementError(
            tr("error.unknown_candidate_status", status=str(status))
        )

    # -- 备份与分支(阶段 D) -----------------------------------------------

    def run_backup_now(self, game_id: str) -> str:
        """立即创建一次备份(向下保存)."""
        game, gid = self._game_ref(game_id)
        if not self._locations.list_for_game(gid):
            raise ArchiveManagementError(tr("error.no_locations_backup"))
        # 新备份带默认名称(直接显示在标题列), 内容摘要留空等用户填写.
        self._perform_backup(gid, title=tr("backup.title_manual"))
        return tr("result.backup_done", name=game.name)

    def run_create_branch(self, game_id: str, backup_id: str, branch_name: str) -> str:
        """从指定节点创建分支(记录当前存档状态)."""
        _game, gid = self._game_ref(game_id)
        clean = branch_name.strip()
        if not clean:
            raise ArchiveManagementError(tr("error.branch_name_empty"))
        self._require_backup(gid, backup_id)
        self._perform_backup(
            gid,
            kind="branch",
            parent_id=int(backup_id),
            branch_name=clean,
            title=clean,
        )
        return tr("result.branch_done", backup_id=backup_id, branch_name=clean)

    def preview_restore(self, game_id: str, backup_id: str) -> RestorePlan:
        """恢复预检: 快照完整性、写回目标、多余文件与游戏进程状态."""
        _game, gid = self._game_ref(game_id)
        self._require_backup(gid, backup_id)
        return self._restore.plan(gid, int(backup_id))

    def run_restore(
        self,
        game_id: str,
        backup_id: str,
        *,
        safety_point: bool = True,
        force: bool = False,
    ) -> str:
        """把备份内容写回原始存档位置, 并把当前节点切到该备份.

        写回前会深度校验快照、检查目标路径与游戏进程; 恢复前默认创建一份
        "恢复前安全点"备份(只在时间线视图展示), 使恢复可逆; 恢复完成后新备份
        与新分支都从这个节点重新开始。
        """
        game, gid = self._game_ref(game_id)
        node = self._require_backup(gid, backup_id)
        if node.id is None:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            )
        with self._operation_lock:
            if self._active is not None:
                raise ArchiveManagementError(tr("error.operation_busy"))
            self._active = _ActiveOperation(game_id=gid)
        try:
            result = self._restore.restore(
                gid,
                node.id,
                safety_point=safety_point,
                force=force,
                safety_title=tr("backup.title_safety_point"),
                safety_note="",
                progress=self._report_progress,
                cancelled=self._cancel_requested,
            )
            self._backups.set_current(gid, node.id)
        except OperationCancelledError as exc:
            raise ArchiveManagementError(tr("result.restore_canceled")) from exc
        finally:
            with self._operation_lock:
                self._active = None
        self._touch()
        return tr(
            "result.restore_written",
            name=game.name,
            title=self._node_title(node),
            files=result.restored_files,
        )

    def rename_backup(
        self, game_id: str, backup_id: str, *, title: str, note: str
    ) -> BackupItem:
        """修改备份的名称与描述."""
        _game, gid = self._game_ref(game_id)
        node = self._require_backup(gid, backup_id)
        try:
            updated = self._backups.update_meta(
                gid, int(backup_id), title=title, note=note
            )
        except ArchiveManagementError as exc:
            raise ArchiveManagementError(
                tr("error.meta_update_failed", reason=str(exc))
            ) from exc
        self._touch()
        current = self._games.current_backup(gid)
        del node
        return self._backup_item(updated, 0, current)

    def plan_delete(self, game_id: str, backup_id: str) -> DeletionPlan:
        """返回删除计划(供 UI 决定是否需要二次确认)."""
        _game, gid = self._game_ref(game_id)
        self._require_backup(gid, backup_id)
        return self._backups.plan_delete(gid, int(backup_id))

    def run_delete_backup(self, game_id: str, backup_id: str) -> str:
        """按分支树删除备份: 同线路节点让后续上移, 分支根节点连带子分支."""
        _game, gid = self._game_ref(game_id)
        self._require_backup(gid, backup_id)
        plan = self._backups.delete_node(gid, int(backup_id), cascade=True)
        self._touch()
        if plan.mode is DeletionMode.CASCADE:
            return tr("result.delete_cascade", count=plan.removed_count)
        if plan.mode is DeletionMode.SHIFT:
            return tr("result.delete_shift")
        return tr("result.delete_single")

    def run_export(self, game_id: str) -> str:
        """导出游戏(阶段 G 接入)."""
        game, _gid = self._game_ref(game_id)
        log_action("export.start", basic=True, game_id=game.id, name=game.name)
        raise ArchiveManagementError(tr("error.not_in_this_phase", phase="G"))

    # -- 调度与生命周期(阶段 D) -------------------------------------------

    def set_schedule(
        self,
        game_id: str,
        interval_text: str,
        *,
        enabled: bool = True,
        keep_auto: int = DEFAULT_KEEP_AUTO,
    ) -> TaskStatus:
        """设置或取消某游戏的定期备份, 并记录自动备份保留份数.

        传空文本表示取消定时备份; 周期文本非法时抛出异常, 由 UI 提示
        用户而不是静默回落默认周期.
        """
        game, gid = self._game_ref(game_id)
        clean = interval_text.strip()
        if clean and not self._locations.list_for_game(gid):
            # 没有存档位置就没有可备份的内容: 提前拒绝并告知原因.
            log_action("schedule.rejected", game_id=gid, reason="no_locations")
            raise ArchiveManagementError(
                tr("error.no_locations_schedule", name=game.name)
            )
        if not clean:
            self._scheduler.unschedule(gid)
            for job in self._jobs.for_game(gid):
                if job.id is not None:
                    self._jobs.delete(job.id)
            log_action("schedule.clear", game_id=gid)
            return self.task_status(game_id)
        minutes = parse_interval(clean)
        entry = self._scheduler.schedule(
            gid, minutes, self._scheduled_backup(gid), enabled=enabled
        )
        self._persist_job(
            gid,
            clean,
            enabled=enabled,
            next_run=entry.next_run_at,
            keep_auto=keep_auto,
        )
        log_action(
            "schedule.set",
            game_id=gid,
            interval=clean,
            enabled=enabled,
            keep_auto=keep_auto,
        )
        return self.task_status(game_id)

    def cancel_active(self) -> bool:
        """请求取消当前备份; 没有进行中的备份时返回 False."""
        with self._operation_lock:
            if self._active is None:
                return False
            self._active.cancel_requested = True
            log_action("task.cancel", game_id=self._active.game_id)
            return True

    def shutdown(self) -> None:
        """释放调度器; 幂等, 供应用退出时调用."""
        self._scheduler.shutdown()

    # -- 内部 ---------------------------------------------------------------

    def _perform_backup(
        self,
        game_id: int,
        *,
        kind: NodeKind = "manual",
        note: str = "",
        parent_id: int | None = None,
        branch_name: str | None = None,
        title: str = "",
    ) -> BackupNode:
        """独占执行一次备份; 已有备份进行中时给出明确错误."""
        with self._operation_lock:
            if self._active is not None:
                raise ArchiveManagementError(tr("error.operation_busy"))
            self._active = _ActiveOperation(game_id=game_id)
        try:
            node = self._backups.create_backup(
                game_id,
                kind=kind,
                note=note,
                parent_id=parent_id,
                branch_name=branch_name,
                title=title,
                keep_auto=self._keep_auto(game_id),
                progress=self._report_progress,
                cancelled=self._cancel_requested,
            )
        except OperationCancelledError as exc:
            raise ArchiveManagementError(tr("result.backup_canceled")) from exc
        finally:
            with self._operation_lock:
                self._active = None
        # 主页的"最近活跃/长期未更新"分类依据这次动作的时间.
        self._games.touch_activity(game_id)
        self._touch()
        return node

    def _scheduled_backup(self, game_id: int) -> Callable[[], None]:
        """构造定时备份回调(在调度线程执行, 不得触碰 Tk).

        存档与当前节点内容一致时本次自动备份静默跳过(create_backup 会写
        DEBUG 级日志), 任务状态仍记为"本次已执行", 不向用户报错。
        """

        def run() -> None:
            try:
                self._perform_backup(
                    game_id, kind="auto", title=tr("backup.title_auto")
                )
            except ContentUnchangedError:
                self._mark_job_run(game_id, error=None)
            except ArchiveManagementError as exc:
                self._mark_job_run(game_id, error=str(exc))
            else:
                self._mark_job_run(game_id, error=None)

        return run

    def _keep_auto(self, game_id: int | None) -> int:
        """返回某游戏的自动备份保留份数(未配置时用默认值)."""
        if game_id is None:
            return DEFAULT_KEEP_AUTO
        jobs = self._jobs.for_game(game_id)
        return jobs[0].keep_auto if jobs else DEFAULT_KEEP_AUTO

    def _mark_job_run(self, game_id: int, *, error: str | None) -> None:
        """记录一次定时备份的执行时间与结果(保留周期配置)."""
        existing = self._jobs.for_game(game_id)
        previous = existing[0] if existing else None
        if previous is None or previous.id is None:
            return
        # 后端已重排下一次触发时间, 刷新缓存后才能显示新的"下次运行".
        self._scheduler.refresh(game_id)
        self._jobs.mark_run(
            previous.id,
            ran_at=datetime.now(UTC),
            next_run_at=self._next_run(game_id),
            error=error,
        )
        # 即使本次因"存档未变化"被跳过, 下次运行时间也变了: 让界面重新读取.
        self._touch()

    def _persist_job(
        self,
        game_id: int,
        interval_text: str,
        *,
        enabled: bool,
        next_run: datetime | None = None,
        keep_auto: int | None = None,
    ) -> None:
        """把任务配置写回数据库, 使调度在重启后可恢复."""
        existing = self._jobs.for_game(game_id)
        previous = existing[0] if existing else None
        self._jobs.upsert(
            ScheduledJob(
                id=None if previous is None else previous.id,
                game_id=game_id,
                schedule=interval_text,
                enabled=enabled,
                last_run_at=None if previous is None else previous.last_run_at,
                next_run_at=(
                    next_run if next_run is not None else self._next_run(game_id)
                ),
                last_error=None,
                keep_auto=(
                    keep_auto
                    if keep_auto is not None
                    else (
                        previous.keep_auto
                        if previous is not None
                        else DEFAULT_KEEP_AUTO
                    )
                ),
            )
        )

    def _node_title(self, node: BackupNode) -> str:
        """返回备份的展示名称(未命名时回退到分支名或类型默认名)."""
        return (
            (node.title or "").strip()
            or (node.branch_name or "").strip()
            or _default_title(node)
        )

    def _report_progress(self, fraction: float, message: str) -> None:
        """记录进度供 UI 轮询(不会阻塞后台线程)."""
        with self._operation_lock:
            if self._active is None:
                return
            self._active.fraction = max(0.0, min(1.0, fraction))
            self._active.message = message

    def _cancel_requested(self) -> bool:
        """返回用户是否请求取消当前备份."""
        with self._operation_lock:
            return self._active is not None and self._active.cancel_requested

    def _require_backup(self, game_id: int, backup_id: str) -> BackupNode:
        """要求备份节点存在且属于该游戏."""
        try:
            node_id = int(backup_id)
        except ValueError as exc:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            ) from exc
        node = self._nodes.get(node_id)
        if node is None or node.game_id != game_id:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            )
        return node

    def _backup_item(
        self, node: BackupNode, depth: int, current_id: int | None = None
    ) -> BackupItem:
        """把领域备份节点映射为列表展示项."""
        facts = self._backups.facts(node)
        created = node.created_at or datetime.now(UTC)
        branch = (node.branch_name or "").strip()
        return BackupItem(
            backup_id=str(node.id),
            title=node.title,
            created_dt=created,
            created_label=_stamp(created),
            auto=node.node_kind == "auto",
            safety=node.is_safety,
            branch_label=(
                tr("backup.branch_label", branch=branch)
                if branch
                else tr("backup.mainline")
            ),
            size_label=size_label(facts.total_size),
            verified=self._is_verified(node),
            sub=node.note,
            parent_id=None if node.parent_id is None else str(node.parent_id),
            depth=depth,
            is_branch=node.node_kind == "branch",
            branch_name=branch,
            is_current=node.id is not None and node.id == current_id,
        )

    def _is_verified(self, node: BackupNode) -> bool:
        """校验快照是否完整(结果按备份缓存, 避免重复扫描)."""
        if node.id is None:
            return False
        key = str(node.id)
        cached = self._verify_cache.get(key)
        if cached is None:
            try:
                cached = verify_snapshot(
                    self._backups.snapshot_root(node), deep=False
                ).ok
            except (ArchiveManagementError, OSError):
                cached = False
            self._verify_cache[key] = cached
        return cached

    def _optional_game_id(self, game_id: str | None) -> int | None:
        """把可选的字符串游戏 id 解析为整型; 非法时返回 None."""
        if game_id is None:
            return None
        try:
            return int(game_id)
        except ValueError:
            return None

    def _next_run(self, game_id: int) -> datetime | None:
        """返回某游戏定期备份的下次运行时间."""
        entry = self._scheduler.get(game_id)
        if entry is None or not entry.enabled:
            return None
        return entry.next_run_at

    # -- 内部 ---------------------------------------------------------------

    def _game(self, game_id: str) -> Game:
        """返回数据库中的游戏实体(不存在时抛错)."""
        game, _id = self._game_ref(game_id)
        return game

    def _game_ref(self, game_id: str) -> tuple[Game, int]:
        """返回游戏实体与稳定的整型 id."""
        try:
            game_id_int = int(game_id)
        except ValueError as exc:
            raise ArchiveManagementError(
                tr("error.unknown_game", game_id=game_id)
            ) from exc
        game = self._games.get(game_id_int)
        if game is None or game.id is None:
            raise ArchiveManagementError(tr("error.unknown_game", game_id=game_id))
        return game, game.id

    def _location(self, location_id: str) -> SaveLocation:
        """返回数据库中的存档位置实体(不存在时抛错)."""
        location, _id = self._location_ref(location_id)
        return location

    def _location_ref(self, location_id: str) -> tuple[SaveLocation, int]:
        """返回存档位置实体与稳定的整型 id."""
        try:
            location_id_int = int(location_id)
        except ValueError as exc:
            raise ArchiveManagementError(
                tr("error.unknown_location", location_id=location_id)
            ) from exc
        location = self._locations.get(location_id_int)
        if location is None or location.id is None:
            raise ArchiveManagementError(
                tr("error.unknown_location", location_id=location_id)
            )
        return location, location.id

    def _summary(self, game: Game) -> GameSummary:
        if game.id is None:
            raise ArchiveManagementError(tr("error.unknown_game", game_id=""))
        location_count = self._games.count_locations(game.id)
        backup_count = self._games.count_backups(game.id)
        return GameSummary(
            game_id=str(game.id),
            name=game.name,
            has_locations=location_count > 0,
            location_count=location_count,
            backup_count=backup_count,
            tone=_tone(game.name),
            enabled=game.enabled,
        )

    def _location_item(self, location: SaveLocation) -> LocationItem:
        probe = probe_path(location.path, location.path_kind)
        note = (
            tr("loc.verified")
            if probe.ok
            else tr(f"loc.err_{probe.reason_code}", path=location.path)
        )
        return LocationItem(
            location_id=str(location.id),
            game_id=str(location.game_id),
            path=location.path,
            path_kind=location.path_kind,
            source=location.source,
            is_primary=location.is_primary,
            ok=probe.ok,
            note=note,
        )

    @staticmethod
    def _clean_name(name: str) -> str:
        """去除首尾空白; 为空时抛出异常."""
        clean = name.strip()
        if not clean:
            raise ArchiveManagementError(tr("error.game_name_empty"))
        return clean

    @staticmethod
    def _require_path(path: str, kind: PathKind) -> None:
        """要求路径可访问; 否则抛出异常."""
        probe = probe_path(path, kind)
        if not probe.ok:
            raise ArchiveManagementError(
                tr(f"error.loc_{probe.reason_code}", path=path)
            )
