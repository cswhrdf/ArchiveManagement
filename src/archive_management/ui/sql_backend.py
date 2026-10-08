"""基于 SQLite 的真实后端.

把 :mod:`archive_management.domain` 实体与仓库映射为 UI 展示模型,
实现 :class:`~archive_management.ui.backend.ArchiveService`. 本后端是
GUI 的默认数据来源; 备份、分支、恢复、导出与删除原始存档目录都接入真实
服务层, 导入也是: 先只读体检, 再按用户选的策略真的写入。
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from archive_management.application import activation as activation_cases
from archive_management.application import candidates as candidate_cases
from archive_management.application import discovery as discovery_cases
from archive_management.application import games as games_cases
from archive_management.application import home as home_cases
from archive_management.application import locations as locations_cases
from archive_management.application.backup import BackupService
from archive_management.application.export import ExportService
from archive_management.application.imports import (
    STRATEGY_SKIP,
    BatchImportChoice,
    BatchImportResult,
    BatchInspection,
    ImportInspection,
    ImportResult,
    ImportService,
)
from archive_management.application.locations import (
    LocationRemovalPlan,
    plan_location_removal,
    remove_save_location,
)
from archive_management.application.restore import RestorePlan, RestoreService
from archive_management.config import load_or_repair_config
from archive_management.domain import (
    DEFAULT_KEEP_AUTO,
    PLATFORM_IDS,
    ArtworkKind,
    BackupNode,
    CandidateStatus,
    DeletionPlan,
    Game,
    GameCandidate,
    HomeFilter,
    MonitoredDirectory,
    NodeKind,
    PathKind,
    PlatformGame,
    PlatformId,
    SaveLocation,
    ScheduledJob,
    candidate_sort_key,
    is_active,
)
from archive_management.exceptions import (
    ArchiveManagementError,
    ContentUnchangedError,
    OperationCancelledError,
)
from archive_management.i18n import current_locale, tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    CandidateRepository,
    GameRepository,
    MonitoredDirectoryRepository,
    SaveLocationRepository,
    ScheduledJobRepository,
)
from archive_management.services.artwork import (
    ArtworkCache,
    UserArtworkStore,
    artwork_cache_at,
    cached_artwork,
    icon_version,
    platform_game,
    resolve_artwork,
    square_icon,
)
from archive_management.services.audit import log_action, redacted_path
from archive_management.services.exclusions import load_exclusions
from archive_management.services.export_format import ARCHIVE_SUFFIX
from archive_management.services.game_names import (
    NameCache,
    NameFetcher,
    name_cache_at,
    resolve_name,
)
from archive_management.services.naming import game_slug
from archive_management.services.pathcheck import (
    normalize_path,
    probe_path,
    summarize_path,
)
from archive_management.services.platform_adapters import (
    PlatformAdapter,
    SaveCandidateSource,
    adapter_for,
    default_adapters,
)
from archive_management.services.platform_scan import default_roots, path_health
from archive_management.services.processes import ProcessNameProvider
from archive_management.services.scheduler import (
    BackupScheduler,
    ScheduledEntry,
    format_interval,
    parse_interval,
)
from archive_management.services.snapshot import verify_snapshot
from archive_management.services.steam_cloud import SteamCloudSource
from archive_management.services.verification import VerificationPolicy
from archive_management.ui.models import (
    BackupItem,
    CandidateItem,
    GameDetail,
    GameSummary,
    HomeBoard,
    ImportChoice,
    LocationItem,
    MonitoredDirItem,
    SavePathSuggestion,
    ScanSummary,
    ScheduleItem,
    TaskStatus,
    clean_game_name,
    delete_result_text,
    format_moment,
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


def _stamp_or_empty(moment: datetime | None) -> str:
    """时间戳文案: 没有排期时返回空串.

    界面据此给出"未配置/未安排"这种能读的说法(见 ``ui.models`` 的
    ``next_backup_text`` / ``next_run_text``), 而不是一个像加载失败的短横线。
    """
    return format_moment(moment) if moment is not None else ""


def _schedule_next_run(
    entry: ScheduledEntry | None, job: ScheduledJob | None
) -> datetime | None:
    """下次运行时间: 启用的调度器优先, 否则回退到最近一次登记的任务."""
    if entry is not None and entry.enabled:
        return entry.next_run_at
    if job is None:
        return None
    return job.next_run_at


def _schedule_fields(
    entry: ScheduledEntry | None, job: ScheduledJob | None
) -> tuple[str, bool, int]:
    """周期文案 / 是否启用 / 保留份数: 调度器里有就以它为准, 否则看登记的任务."""
    if entry is not None:
        keep = job.keep_auto if job is not None else DEFAULT_KEEP_AUTO
        return _interval_label(entry.interval_minutes), entry.enabled, keep
    if job is not None:
        return job.schedule, True, job.keep_auto
    return "", True, DEFAULT_KEEP_AUTO


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


def _import_message(result: ImportResult) -> str:
    """导入结果提示: 计数一律取服务层返回的导入结果, 不另算一遍.

    “跳过”策略什么都没写, 所以它的提示与真的导入了多少份是两回事; 真的导入时
    如果碰上包里已有同名的节点目录(重复导入同一个包), 也要把跳过数说出来。
    """
    if result.strategy == STRATEGY_SKIP:
        return tr(
            "result.import_skipped",
            name=result.game_name,
            skipped=result.skipped_nodes,
        )
    fields = {
        "name": result.game_name,
        "nodes": result.nodes,
        "files": result.files,
        "size": size_label(result.total_bytes),
    }
    if result.skipped_nodes:
        return tr("result.import_done_skipped", skipped=result.skipped_nodes, **fields)
    return tr("result.import_done", **fields)


def _batch_import_message(result: BatchImportResult) -> str:
    """批量导入提示: 逐游戏计数一律取服务层的结果, 界面不另算一遍.

    "跳过"在批量导入里有两层含义, 因此分开说: **整款跳过**(用户选了"跳过", 那款的
    备份一份都没试)与**跳过已存在的备份**(重复导入时那个节点目录已经在位)。服务层的
    ``skipped_nodes`` 把两者算在一起, 所以这里只从**没被跳过**的那些游戏上取后者 ——
    否则"跳过已存在的 0 份"会变成"1 份", 用户去找一个并不存在的旧节点。全批都跳过时
    给一句更直白的说明。
    """
    if result.games and result.skipped_games == result.games:
        return tr("result.import_batch_all_skipped", games=result.games)
    existing = sum(
        item.skipped_nodes for item in result.results if item.strategy != STRATEGY_SKIP
    )
    return tr(
        "result.import_batch_done",
        games=result.games,
        nodes=result.nodes,
        files=result.files,
        size=size_label(result.total_bytes),
        skipped=existing,
        skipped_games=result.skipped_games,
    )


def _platform_game(candidate: GameCandidate) -> PlatformGame | None:
    """把探测结果包装成平台数据模型(解析不出平台游戏标识时返回 ``None``)."""
    if candidate.source not in PLATFORM_IDS:
        return None
    app_id = discovery_cases.candidate_app_id(candidate)
    if app_id is None:
        return None
    return PlatformGame(
        platform=candidate.source,
        game_id=str(app_id),
        name=candidate.name,
        install_dir=candidate.install_dir,
    )


def _library_game(game: Game) -> PlatformGame | None:
    """把库里的游戏包装成平台数据模型(来源不是平台或没有标识时返回 ``None``)."""
    if game.origin not in PLATFORM_IDS or game.steam_app_id is None:
        return None
    return platform_game(
        store=game.origin,
        game_id=str(game.steam_app_id),
        name=game.name,
    )


def _free_destination(directory: Path, stem: str) -> Path:
    """在 ``directory`` 里挑一个还没被占用的归档包名(同名已存在时追加 -2/-3…).

    自动导出**绝不覆盖**已有的包: 时间戳只精确到秒, 同一秒里连着删第二款游戏时
    必须落到不同的名字上, 否则后一款会把前一款的告别包顶掉。
    """
    candidate = directory / f"{stem}{ARCHIVE_SUFFIX}"
    counter = 2
    while candidate.exists():
        candidate = directory / f"{stem}-{counter}{ARCHIVE_SUFFIX}"
        counter += 1
    return candidate


def policy_from_config(config_path: Path) -> VerificationPolicy:
    """按配置文件读出这次要用的校验方式与并发上限.

    每次操作前现读一次(而不是启动时定死): 用户可能刚在设置窗口里把校验方式改掉,
    下一次备份/还原就该按新的走 —— 而**正在跑的那一次**用的是开始时的取值。
    """
    config = load_or_repair_config(config_path).config
    return VerificationPolicy(
        mode=config.verification.mode,
        max_parallel=config.verification.max_parallel,
    )


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
        exports_dir: Path | None = None,
        artwork_dir: Path | None = None,
        scheduler: BackupScheduler | None = None,
        cache_dir: Path | None = None,
        save_source: SaveCandidateSource | None = None,
        adapters: Mapping[PlatformId, PlatformAdapter] | None = None,
        name_fetcher: NameFetcher | None = None,
        process_provider: ProcessNameProvider | None = None,
        config_path: Path | None = None,
    ) -> None:
        """绑定数据库、备份根目录、调度器与缓存目录(后两者默认惰性创建).

        ``cache_dir`` 为空时封面接口一律返回空: 测试与未配置应用路径的场景直接走
        占位图, 不去碰真实用户缓存目录。``process_provider`` 是自动启停用的进程名
        提供者, 省略时用 psutil(测试注入假进程表)。``exports_dir`` 是自动导出的
        落点, 省略时取备份目录旁边(生产里就是 :attr:`ApplicationPaths.exports_dir`);
        它**不在这里创建**, 只在真的要写出告别包时创建。``artwork_dir`` 是**用户自己
        指定**的封面/图标落点, 同样省略时取备份目录旁边(生产里是
        :attr:`ApplicationPaths.artwork_dir`) —— 它与 ``cache_dir`` 分开: 缓存可以
        整体清理, 用户挑的图不能。
        """
        self._database = database
        self._backup_root = backup_root
        self._exports_dir = (
            exports_dir if exports_dir is not None else backup_root.parent / "exports"
        )
        self._artwork_dir = (
            artwork_dir if artwork_dir is not None else backup_root.parent / "artwork"
        )
        self._games = GameRepository(database)
        self._locations = SaveLocationRepository(database)
        self._nodes = BackupRepository(database)
        self._jobs = ScheduledJobRepository(database)
        self._monitored = MonitoredDirectoryRepository(database)
        self._candidates = CandidateRepository(database)
        # 校验方式与并发上限**每次操作前现读配置**(用户可能刚在设置窗口里改过); 没有
        # 配置路径(测试与演示)时交给服务用默认策略(sha256 + 保守并发数)。
        self._verification_policy = (
            (lambda: policy_from_config(config_path))
            if config_path is not None
            else None
        )
        self._backups = BackupService(
            database, backup_root=backup_root, policy=self._verification_policy
        )
        self._restore = RestoreService(
            database,
            backup_root=backup_root,
            backups=self._backups,
        )
        self._export = ExportService(database, backup_root=backup_root)
        self._import = ImportService(database, backup_root=backup_root)
        self._scheduler = scheduler if scheduler is not None else BackupScheduler()
        self._theme = _DEFAULT_THEME
        self._revision = 0
        self._operation_lock = threading.Lock()
        self._active: _ActiveOperation | None = None
        self._verify_cache: dict[str, bool] = {}
        self._cache_dir = cache_dir
        self._save_source = save_source
        self._adapters = adapters
        self._name_fetcher = name_fetcher
        self._process_provider = process_provider
        self._names_lock = threading.Lock()
        self._names_running = False
        self._artwork_lock = threading.Lock()
        self._artwork_running = False
        self._artwork_attempts: set[str] = set()
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
            game = self._games.get(job.game_id)
            # 停用或归档的游戏保留任务配置, 但一律以暂停状态登记(不执行).
            active = game is not None and is_active(game)
            self._scheduler.schedule(
                job.game_id,
                minutes,
                self._scheduled_backup(job.game_id),
                enabled=job.enabled and active,
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
        main, verified, note = self._primary_location_view(gid)
        backups = self._nodes.count(gid)
        last_label, last_sub = self._last_backup_labels(self._backups.latest(gid))
        next_run, next_paused = self._next_run_state(gid)
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
            next_backup_label=_stamp_or_empty(next_run),
            next_backup_paused=next_paused,
            original_name=game.original_name or game.name,
            storage_folder=game.storage_key,
        )

    def _primary_location_view(self, gid: int) -> tuple[str, bool, str]:
        """主位置的路径、校验结果与说明文案(没有位置时是空串/False/"未配置")."""
        locations = self._locations.list_for_game(gid)
        primary = next(
            (location for location in locations if location.is_primary),
            locations[0] if locations else None,
        )
        if primary is None:
            return "", False, tr("game.no_locations_short")
        probe = probe_path(primary.path, primary.path_kind)
        note = (
            tr("game.primary_location")
            if primary.is_primary
            else tr("game.location_managed")
        )
        return primary.path, probe.ok, note

    def _last_backup_labels(self, latest: BackupNode | None) -> tuple[str, str]:
        """最近一次备份的时间文案与副标题(没有备份时是破折号与空串)."""
        if latest is None:
            return "—", ""
        facts = self._backups.facts(latest)
        created = latest.created_at or datetime.now(UTC)
        kind = (
            tr("backup.kind_auto")
            if latest.node_kind == "auto"
            else tr("backup.kind_manual")
        )
        return format_moment(created), tr(
            "detail.last_backup_sub",
            kind=kind,
            size=size_label(facts.total_size),
        )

    def add_game(self, name: str) -> GameSummary:
        """新增游戏, 返回其摘要."""
        clean = clean_game_name(name)
        game = self._games.add(Game(name=clean))
        log_action("game.add", game_id=game.id, name=clean)
        return self._summary(game)

    def update_game(self, game_id: str, name: str) -> GameSummary:
        """重命名游戏, 返回其摘要."""
        clean = clean_game_name(name)
        game = self._game(game_id)
        # 只改名称: 首次录入的原始名称与磁盘目录名保持不变. 同时清掉"程序写入的
        # 译名"记录: 从这一刻起这个名字是用户起的, 译名探测不得再覆盖它。
        updated = game.model_copy(update={"name": clean, "localized_name": ""})
        self._games.update(updated)
        log_action("game.rename", game_id=game.id, name=clean)
        return self._summary(updated)

    def delete_export_path(self, game_id: str) -> str:
        """算出这款游戏"告别包"的默认落点(**只计算路径, 不创建任何东西**).

        默认落在备份目录旁边的 ``exports`` 目录, 文件名是
        ``<游戏名 slug>-<YYYYMMDD-HHMMSS>.archive.zip``; 同一秒内已经有一份同名包时
        追加 ``-2``/``-3``, 因此自动导出永远不会覆盖上一份。这里不建目录也不写文件,
        真正落盘发生在 :meth:`delete_game` 里(失败就什么都不删)。
        """
        game, _gid = self._game_ref(game_id)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        stem = f"{game_slug(game.name)}-{stamp}"
        return str(_free_destination(self._exports_dir, stem))

    def delete_game(self, game_id: str, destination: str) -> None:
        """先把这款游戏(配置 + 全部备份)导出成告别包, 再删除它的记录.

        顺序是这次改动的全部要点: **导出成功之前一个字段都不删**。导出用的是与
        「导出游戏」按钮同一条服务路径, 因此包里是完整内容; 失败时异常原样抛出,
        游戏记录、存档位置、探测候选记账与备份节点全部原样保留。导出成功后按原有
        行为删除记录(其存档位置与候选记账随之清除), 并清掉封面/图标与译名缓存
        这类可再生的派生数据 —— 备份目录里的备份文件始终保留。
        """
        game, gid = self._game_ref(game_id)
        # 目录只在真的要写出告别包时才创建(界面里的确认框不该顺手建目录).
        self._exports_dir.mkdir(parents=True, exist_ok=True)
        result = self._export.export_game(gid, Path(destination))
        # 与导出按钮同一套"路径脱敏"记法: 审计里能同时看到"导出过"与"删除过"。
        log_action(
            "game.delete_export",
            game_id=game.id,
            name=game.name,
            backups=result.backups,
            files=result.files,
            path=redacted_path(str(destination)),
        )
        games_cases.delete_game(self._database, gid)
        self._forget_artwork(game)
        self._forget_names(game)
        # 这个游戏已经不存在了: 把它从"本次会话已尝试过"的集合里拿掉, 免得再建同名
        # 游戏时被当成"试过了"而不再取图。
        self._artwork_attempts.discard(str(gid))

    def _forget_artwork(self, game: Game) -> None:
        """把这款游戏探测到的封面与图标缓存一起删掉, 连用户指定的那份一起清.

        游戏已经不存在了, 留着只会让下次同名/同 AppID 的游戏误用旧图。
        """
        for path in self._user_artwork().clear(str(game.id)):
            logger.debug("已清理自定义图片: %s", path)
        cache = self._artwork_cache()
        referenced = None if cache is None else _library_game(game)
        if cache is None or referenced is None:
            return
        for path in cache.forget(referenced.platform, referenced.game_id):
            logger.debug("已清理缓存图片: %s", path)

    def _forget_names(self, game: Game) -> None:
        """把这款游戏缓存的译名一起删掉(游戏不在了, 留着只会误用)."""
        if self._cache_dir is None or game.steam_app_id is None:
            return
        name_cache_at(self._cache_dir).forget(str(game.steam_app_id))

    def set_game_enabled(self, game_id: str, enabled: bool) -> GameSummary:
        """启用或停用一个游戏(启用时会自动停用其它启用中的游戏).

        这是用户自己的选择, 因此带 ``manual=True``: 自动启停据此让手动决定优先
        (见 :func:`~archive_management.application.games.set_enabled`)。
        """
        _game, gid = self._game_ref(game_id)
        result = games_cases.set_enabled(self._database, gid, enabled, manual=True)
        if not enabled:
            # 停用后定时备份不允许启用: 先把它置为暂停, 界面才能如实展示状态.
            self._pause_schedule(gid)
        self._touch()
        return self._summary(result.game)

    def poll_activation(self, *, enabled: bool = True) -> games_cases.ActivationOutcome:
        """低频轮询一次自动启停: 按被跟踪游戏的进程决定启用态.

        由界面定时器在后台线程调用; 开关关闭时什么都不做(连状态都不读)。
        """
        outcome = activation_cases.poll_activation(
            self._database, enabled=enabled, provider=self._process_provider
        )
        if outcome.changed:
            self._touch()
        return outcome

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
        # 写入规则(规范化/判重/可访问性)在应用层: 导入归档包走的是同一个入口.
        location = locations_cases.add_save_location(
            self._database, gid, path=path, kind=kind
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
        task_name, schedule_text, schedule_enabled = self._schedule_summary(gid, entry)
        return TaskStatus(
            running=active is not None,
            task_name=task_name,
            progress=active.fraction if active is not None else 0.0,
            next_run_label=_stamp_or_empty(next_run),
            target_label=str(self._backup_root),
            theme_name=self._theme,
            backend_ok=True,
            schedule_text=schedule_text,
            schedule_enabled=schedule_enabled,
            cancellable=active is not None,
            progress_label=(active.message if active is not None else (error or "")),
            keep_auto=self._keep_auto(gid),
            revision=self._data_revision,
        )

    def _schedule_summary(
        self, gid: int | None, entry: ScheduledEntry | None
    ) -> tuple[str, str, bool]:
        """定时任务的三段文案: 任务名、周期、是否启用(未配置时是"未定时"/空/启用态)."""
        if entry is None:
            return tr("task.unscheduled"), "", True
        # 暂停不算"未配置": 周期仍然保留, 只是不触发。
        return (
            tr(
                "task.every",
                interval=_interval_label(entry.interval_minutes),
                keep=self._keep_auto(gid),
            ),
            _interval_label(entry.interval_minutes),
            entry.enabled,
        )

    def list_schedules(self) -> list[ScheduleItem]:
        """返回全部游戏的定时备份配置(未配置的游戏也会出现, 便于新增).

        任务来自哪个游戏、下次运行时间、当前保留的自动备份份数都在这里
        组装好, 供全局任务窗口直接展示。
        """
        items: list[ScheduleItem] = []
        for game in self._games.list():
            item = self._schedule_item(game)
            if item is not None:
                items.append(item)
        return items

    def _schedule_item(self, game: Game) -> ScheduleItem | None:
        """把一款游戏组装成任务条目(还没有落到库里的游戏返回 None)."""
        if game.id is None:  # pragma: no cover - 查询总是带 id
            return None
        entry = self._scheduler.get(game.id)
        jobs = self._jobs.for_game(game.id)
        job = jobs[0] if jobs else None
        interval_text, enabled, keep_auto = _schedule_fields(entry, job)
        return ScheduleItem(
            game_id=str(game.id),
            game_name=game.name,
            interval_text=interval_text,
            enabled=enabled,
            keep_auto=keep_auto,
            next_run_label=_stamp_or_empty(_schedule_next_run(entry, job)),
            auto_count=self._auto_backup_count(game.id),
            last_error=(job.last_error if job is not None else "") or "",
            has_locations=self._games.count_locations(game.id) > 0,
            game_enabled=game.enabled,
            archived=game.archived,
            tone=_tone(game.name),
        )

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

    # -- 本地游戏探测与监控目录 -------------------------------------------

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

        扫描会同时刷新监控目录的上次扫描时间与候选的路径健康状态; 命中平台工具
        排除清单(`resources/excluded-games.json`)的候选会被默认标记为"已忽略"。
        """
        report = discovery_cases.scan_library(
            self._database, exclusions=load_exclusions()
        )
        # 扫描完就顺手补一次译名(后台线程 + 走缓存): 探测结果页要直接显示译名.
        self.prefetch_names()
        return ScanSummary(
            monitored=report.monitored,
            active=report.active,
            total=report.total,
            added=report.added,
            updated=report.updated,
            linked=report.linked,
            unusable=report.unusable,
            excluded=report.excluded,
            pruned=report.pruned,
            errors=report.errors,
        )

    def clear_scan_results(self) -> int:
        """清空探测结果(保留已导入的候选与按名字记住的忽略)."""
        removed = discovery_cases.clear_scan_results(self._database)
        self._touch()
        return removed

    def list_candidates(self, *, status: str | None = None) -> list[CandidateItem]:
        """返回探测到的候选游戏(可按处理进度筛选)."""
        wanted = self._candidate_status(status)
        items = self._candidates.list_all(status=wanted)
        return [
            self._candidate_item(item) for item in sorted(items, key=candidate_sort_key)
        ]

    def import_candidate(
        self,
        candidate_id: str,
        *,
        name: str | None = None,
        save_paths: Sequence[str] = (),
    ) -> GameSummary:
        """把一条探测结果导入为游戏, 并写入用户确认的存档路径.

        路径来自导入对话框(预填值就是扫描阶段推出来的候选, 用户可以改也可以去掉):
        与平台候选一致的走 ``confirm_candidate``(保留"来自哪份清单"的可追溯性),
        用户改过或新增的走 ``add_location``(记成手动添加)。两者都会再走一遍危险
        位置校验, 被拒的路径直接报错, 不写一半。
        """
        candidate = self._candidate_ref(candidate_id)
        game = discovery_cases.import_candidate(self._database, candidate, name=name)
        written = self._apply_save_paths(game, save_paths)
        self._touch()
        return replace(self._summary(game), saved_paths=written)

    def _apply_save_paths(self, game: Game, paths: Sequence[str]) -> int:
        """把导入对话框里确认的存档路径写进库, 返回写入条数."""
        wanted = [normalize_path(path) for path in paths if path.strip()]
        if game.id is None or not wanted:
            return 0
        known = self._suggest_for_game(game)
        for path in wanted:
            reference = known.get(path)
            if reference is None:
                self.add_location(
                    str(game.id),
                    path=path,
                    kind="directory" if Path(path).is_dir() else "file",
                )
            else:
                candidate_cases.confirm_candidate(self._database, reference)
        return len(wanted)

    def _suggest_for_game(self, game: Game) -> dict[str, int]:
        """探测该游戏的存档候选并落库为待确认, 返回 ``{规范化路径: 候选 id}``.

        落库是为了保留"这条路径来自哪个平台的哪份清单"的来源信息; 平台不支持
        存档探测(或没有可信来源)时返回空字典, 导入照常进行。
        """
        if game.id is None:  # pragma: no cover - 查询总是带 id
            return {}
        adapter = self._adapter_for(game.origin)
        if adapter is None or not adapter.supports_save_paths:
            return {}
        report = candidate_cases.suggest_candidates(
            self._database, game.id, self._save_candidate_source(), platform=game.origin
        )
        stored: dict[str, int] = {}
        for item in report.candidates:
            if item.id is not None:
                stored[normalize_path(item.path)] = item.id
        return stored

    def _save_candidate_source(self) -> SaveCandidateSource:
        """存档候选来源: 未注入时读本机公开的 Steam 云同步清单."""
        if self._save_source is not None:
            return self._save_source
        return SteamCloudSource(default_roots())

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
                format_moment(directory.last_scan_at)
                if directory.last_scan_at is not None
                else ""
            ),
        )

    def _candidate_item(self, candidate: GameCandidate) -> CandidateItem:
        """把候选实体映射为展示模型(路径状态实时判定, 顺带带上存档路径建议)."""
        suggestions = self._save_suggestions(candidate)
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
            localized_name=self._cached_name(candidate),
            save_paths=() if suggestions is None else suggestions,
            save_supported=suggestions is not None,
        )

    def _save_suggestions(
        self, candidate: GameCandidate
    ) -> tuple[SavePathSuggestion, ...] | None:
        """探测这款游戏的存档路径(只探测不落库); 平台不支持时返回 ``None``.

        这是"扫描发现游戏时就顺手推断存档目录"的入口: 结果直接展示在探测结果页,
        真正写入要等用户在导入对话框里确认。拿不到平台标识(或平台没实现存档
        探测)时返回 ``None``, 界面据此显示"需手动添加"而不是"探测过了但没有"。
        """
        game = _platform_game(candidate)
        adapter = None if game is None else self._adapter_for(game.platform)
        if game is None or adapter is None or not adapter.supports_save_paths:
            return None
        try:
            found = adapter.save_candidates(game)
        except Exception as exc:  # 适配器边界: 意外错误只降级, 不阻断扫描
            logger.debug("推断存档路径失败(%s): %s", candidate.name, exc)
            return ()
        return tuple(SavePathSuggestion.from_candidate(item) for item in found)

    def _adapter_for(self, platform: str) -> PlatformAdapter | None:
        """按来源取平台适配器; 非平台来源(监控目录/手动添加)返回 ``None``.

        适配器表在第一次真正需要时才构造(要读本机注册表与安装目录)。
        """
        if self._adapters is None:
            self._adapters = default_adapters(default_roots())
        return adapter_for(self._adapters, platform)

    # -- 游戏译名 -----------------------------------------------------------

    def prefetch_names(self, *, refresh: bool = False) -> None:
        """后台按当前界面语言补齐译名(取不到就保留探测到的原名).

        同一个实例同时只跑一个线程; 译名会直接改写到游戏记录里, 因此完成后抬高
        数据版本, 界面重读就能看到新名字。缓存按 ``<AppID>:<语言>`` 分条, 因此
        切换语言**不需要**忽略缓存: 新语言没记录才联网, 已有记录直接复用。
        ``refresh`` 只是诊断用的口子(强制重取一次), 界面里没有入口。
        """
        if self._cache_dir is None:
            return
        with self._names_lock:
            if self._names_running:
                return
            self._names_running = True
        threading.Thread(
            target=self._localize_names, args=(refresh,), daemon=True
        ).start()

    def _localize_names(self, refresh: bool) -> None:
        """在工作线程里补译名: 游戏改写记录, 探测结果只填缓存.

        用户改过名的游戏跳过(见 :meth:`_name_is_ours`), 没有平台标识的也跳过 ——
        拿不到 AppID 就没有可信的译文来源。
        """
        try:
            # 没有缓存目录时仍可以改写游戏译名(候选译名必须落缓存, 那种情况跳过).
            cache = None if self._cache_dir is None else name_cache_at(self._cache_dir)
            locale = current_locale()
            changed = self._rename_games(cache, locale, refresh)
            if cache is not None:
                changed += self._name_candidates(cache, locale, refresh)
            if changed:
                self._touch()
        finally:
            with self._names_lock:
                self._names_running = False

    def _rename_games(self, cache: NameCache | None, locale: str, refresh: bool) -> int:
        """把"名字还是程序写的"游戏换成当前语言的名称, 返回改写条数."""
        renamed = 0
        for game in self._games.list():
            if game.id is None or game.steam_app_id is None:
                continue
            if not self._name_is_ours(game):
                continue
            found = resolve_name(
                str(game.steam_app_id),
                locale,
                cache,
                fetcher=self._name_fetcher,
                refresh=refresh,
            )
            if found is None or found == game.name:
                continue
            self._games.update(
                game.model_copy(update={"name": found, "localized_name": found})
            )
            log_action("game.localize_name", game_id=game.id, locale=locale, name=found)
            renamed += 1
        return renamed

    @staticmethod
    def _name_is_ours(game: Game) -> bool:
        """判断当前名称是"程序写上去的译名"还是"用户自己起的名字".

        名称等于录入时的原名(还没探测过)或上次程序写入的译名(``localized_name``)
        时都算我们的, 切换语言时应该跟着换; 用户改过名之后 ``update_game`` 会清空
        ``localized_name``, 从此这个名字不再被译名覆盖。
        """
        return game.name == game.original_name or game.name == game.localized_name

    def _name_candidates(self, cache: NameCache, locale: str, refresh: bool) -> int:
        """给"待处理"的探测结果补一次译名(只填缓存, 不写游戏库), 返回新取的条数.

        探测结果页要直接看到译名, 但它还不是游戏, 无处可存 —— 因此只把结果写进译名
        缓存, 界面从缓存里取。已经导入/已忽略的候选不再联网。
        """
        added = 0
        for candidate in self._candidates.list_all(status="new"):
            app_id = discovery_cases.candidate_app_id(candidate)
            if app_id is None:
                continue
            if not refresh and cache.get(str(app_id), locale) is not None:
                continue
            found = resolve_name(
                str(app_id), locale, cache, fetcher=self._name_fetcher, refresh=refresh
            )
            if found is not None:
                added += 1
        return added

    def _cached_name(self, candidate: GameCandidate) -> str:
        """探测结果在当前语言下的译名(只读缓存不联网); 没有就返回空串."""
        if self._cache_dir is None:
            return ""
        app_id = discovery_cases.candidate_app_id(candidate)
        if app_id is None:
            return ""
        found = name_cache_at(self._cache_dir).get(str(app_id), current_locale())
        return "" if found is None else found

    # -- 图片 ---------------------------------------------------------------

    def artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
        """返回封面/图标的本地路径(用户指定的优先, 其次缓存与平台本地资源, 不联网).

        用户自己挑的图排在最前面: 它是明确的意图, 井且手动添加的游戏压根没有平台图
        (``_artwork_game`` 返回 None), 只有这一条路能给出封面。
        """
        user = self._user_artwork_path(game_id, kind)
        if user:
            return user
        cache = self._artwork_cache()
        game, _gid = self._game_ref(game_id)
        referenced = self._artwork_game(game)
        if cache is None or referenced is None:
            return ""
        found = cached_artwork(referenced, kind, cache)
        return "" if found is None else str(found)

    def user_artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
        """只看用户自己指定过的那份图(没有则空串)."""
        return self._user_artwork_path(game_id, kind)

    def set_game_artwork(
        self, game_id: str, kind: ArtworkKind, source_path: str
    ) -> str:
        """把用户选的文件存成这款游戏的封面/图标, 返回落点.

        图片不可用时抛 :class:`~archive_management.exceptions.ArtworkImageError`
        (带原因代码): 界面临摹不到"为什么这张图不能用"就只能静默失败。
        """
        game, gid = self._game_ref(game_id)
        path = self._user_artwork().save(str(gid), kind, Path(source_path))
        log_action(
            "game.artwork_set",
            game_id=game.id,
            kind=kind,
            source=redacted_path(str(source_path)),
        )
        return str(path)

    def clear_game_artwork(self, game_id: str, kind: ArtworkKind) -> bool:
        """恢复默认: 删掉用户指定过的那份图."""
        game, gid = self._game_ref(game_id)
        removed = self._user_artwork().clear(str(gid), kind)
        if removed:
            log_action("game.artwork_clear", game_id=game.id, kind=kind)
        return bool(removed)

    def _user_artwork(self) -> UserArtworkStore:
        """用户指定的封面/图标存储(绑定数据目录下的画像目录)."""
        return UserArtworkStore(self._artwork_dir)

    def _user_artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
        """用户指定的那份图的绝对路径(没给过/文件不可用/游戏不存在时都返回空串)."""
        try:
            _game, gid = self._game_ref(game_id)
        except ArchiveManagementError as exc:  # 游戏刚被删/参数不对: 缺图不影响渲染
            logger.debug("读取自定义图片失败: %s", exc)
            return ""
        found = self._user_artwork().find(str(gid), kind)
        return "" if found is None else str(found)

    def prefetch_artwork(self) -> None:
        """后台补齐缺失的封面与图标(不阻塞界面, 完成后刷新数据版本让界面重读).

        同一个实例同时只跑一个下载线程, 且每款游戏本次会话只尝试一次(失败不反复
        轰炸 CDN); 下载失败不影响任何管理功能, 界面直接回落占位图。
        """
        if self._artwork_cache() is None:
            return
        with self._artwork_lock:
            if self._artwork_running:
                return
            self._artwork_running = True
        threading.Thread(target=self._download_artwork, daemon=True).start()

    def _download_artwork(self) -> None:
        """在工作线程里补齐缓存里还没有的封面与图标."""
        try:
            cache = self._artwork_cache()
            if cache is None:  # pragma: no cover - 调用前已判过一次
                return
            downloaded = 0
            for game in self._games.list():
                referenced = self._artwork_game(game)
                if referenced is None or str(game.id) in self._artwork_attempts:
                    continue
                self._artwork_attempts.add(str(game.id))
                downloaded += self._fetch_artwork(cache, referenced)
            if downloaded:
                self._touch()
        finally:
            with self._artwork_lock:
                self._artwork_running = False

    def _fetch_artwork(self, cache: ArtworkCache, referenced: PlatformGame) -> int:
        """补齐一款游戏的封面与图标, 返回本次成功的份数."""
        fetched = 0
        if cache.lookup_any(referenced.platform, referenced.game_id, "cover") is None:
            fetched += int(resolve_artwork(referenced, "cover", cache).found)
        return fetched + int(self._store_icon(cache, referenced) is not None)

    def _store_icon(self, cache: ArtworkCache, referenced: PlatformGame) -> Path | None:
        """把图标写进缓存: **官方图标优先**, 平台给不出官方图标才裁封面.

        官方图标由适配器给出(本机 ``steam/games/<哈希>.ico`` 或 CDN 同哈希资源)。
        无论哪种来源都统一转成方形 PNG: 界面与缓存校验只认位图, 而且两类来源的
        尺寸跨度很大(16x16 到 256x256), 统一到固定边长后头像位不会忽大忽小。
        裁不开(文件损坏)时返回 ``None``, 界面回落名称首字位图。
        """
        platform, game_id = referenced.platform, referenced.game_id
        version = icon_version(referenced)
        # 只认“当前来源对应的那一版”: 以前裁出来的封面图标(版本名不同)会被下面
        # 这次覆盖掉 —— 否则拿到官方图标之后仍会一直显示旧的裁剪图。
        if cache.lookup(platform, game_id, "icon", version) is not None:
            return None
        source = self._icon_source(cache, referenced)
        if source is None:
            return None
        content = square_icon(source)
        if content is None:
            return None
        try:
            return cache.store(
                platform,
                game_id,
                "icon",
                version,
                content=content,
                extension="png",
            )
        except OSError as exc:
            logger.warning("写入图标缓存失败: %s", exc)
            return None

    def _icon_source(
        self, cache: ArtworkCache, referenced: PlatformGame
    ) -> Path | None:
        """图标原图: 官方图标(本机 ico → CDN ico)优先, 都没有才用封面."""
        official = resolve_artwork(referenced, "icon", cache)
        if official.path is not None:
            return official.path
        return cache.lookup_any(referenced.platform, referenced.game_id, "cover")

    def _artwork_cache(self) -> ArtworkCache | None:
        """图片缓存; 未配置缓存目录时返回 None(界面走占位图)."""
        return None if self._cache_dir is None else artwork_cache_at(self._cache_dir)

    def _artwork_game(self, game: Game) -> PlatformGame | None:
        """把库里的游戏包装成取图需要的平台数据(平台不支持取图时返回 ``None``).

        能力由平台适配器声明: 拿不到适配器、适配器不支持图片、或它给不出资源引用
        的游戏都不去下载, 界面直接显示占位。
        """
        base = _library_game(game)
        adapter = None if base is None else self._adapter_for(base.platform)
        if base is None or adapter is None or not adapter.supports_artwork:
            return None
        refs = adapter.artwork_refs(base)
        if not refs:
            return None
        return base.model_copy(update={"artwork": list(refs)})

    # -- 统一游戏主页 ------------------------------------------------------

    def load_home(self) -> HomeBoard:
        """返回主页数据, 沿用上次保存的筛选条件(第一次打开用默认视图)."""
        return self._board(home_cases.load_home(self._database))

    def apply_home_filter(self, active: HomeFilter) -> HomeBoard:
        """按给定筛选条件重算主页并保存该条件."""
        saved = home_cases.save_filter(self._database, active)
        return self._board(home_cases.filter_home(self._database, saved))

    def set_game_archived(self, game_id: str, archived: bool) -> HomeBoard:
        """归档或取消归档一个游戏, 返回重算后的主页数据.

        归档只保留"删除/导出/取消归档/打开详情": 因此先把游戏停用、把它的定时
        备份置为暂停, 再写归档标记(取消归档不顺手启用, 由用户明确选择)。
        """
        _game, gid = self._game_ref(game_id)
        if archived:
            games_cases.set_enabled(self._database, gid, False)
            self._pause_schedule(gid)
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
        return home_board(report, stamp=format_moment)

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

    # -- 备份与分支 --------------------------------------------------------

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
        if node.id is None:  # pragma: no cover - 查询总是带 id
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
        # 恢复前的安全点也是备份: 名称模式下它同样先没哈希, 在这里一并排上补齐.
        self._spawn_hash_backfill(gid)
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
        node = self._require_backup(gid, backup_id)
        # 名字要在删之前取: 结果文案要说清"删的是哪一份".
        title = self._node_title(node)
        plan = self._backups.delete_node(gid, int(backup_id), cascade=True)
        self._touch()
        return delete_result_text(plan, title)

    def run_export(self, game_id: str, destination: str) -> str:
        """把这款游戏(含全部备份内容)导出到 ``destination``, 返回本地化提示.

        提示里带真实的备份数/文件数与包大小; 失败时记审计并原样抛出, 由界面
        提示原因(用户选定的路径不会留下半成品: 包由服务层原子写入).
        """
        game, gid = self._game_ref(game_id)
        log_action("export.start", basic=True, game_id=game.id, name=game.name)
        try:
            result = self._export.export_game(gid, Path(destination))
        except ArchiveManagementError as exc:
            log_action(
                "export.failed",
                game_id=game.id,
                name=game.name,
                error=str(exc),
            )
            raise
        return tr(
            "result.export_done",
            name=result.game_name,
            file=Path(destination).name,
            backups=result.backups,
            files=result.files,
            size=size_label(result.total_bytes),
        )

    def run_export_batch(self, game_ids: Sequence[str], destination: str) -> str:
        """把若干款游戏导成一个批量包, 返回带真实计数的本地化提示.

        与单包导出同一套口径: 空选择与未知游戏直接报错; 提示里的计数一律取服务层的
        结果, 界面不另算一遍。**任何一款失败或取消都让整批失败**(目标路径不留文件, 由
        服务层保证), 因此这里只补审计、不做回滚。导出期间占用"进行中操作"槽位, 用户
        因此可以像备份那样请求取消(取消落在游戏之间与节点之间)。
        """
        selected = [self._game_ref(game_id)[1] for game_id in game_ids]
        if not selected:
            # 与用例层同一句话: 这里提前拦是为了给"进行中操作"留一个真实的游戏 id.
            raise ArchiveManagementError("批量导出需要至少 1 款游戏")
        log_action("export.batch_start", basic=True, games=len(selected))
        with self._operation_lock:
            if self._active is not None:
                raise ArchiveManagementError(tr("error.operation_busy"))
            self._active = _ActiveOperation(game_id=selected[0])
        try:
            result = self._export.export_games(
                selected,
                Path(destination),
                progress=self._report_progress,
                cancelled=self._cancel_requested,
            )
        except OperationCancelledError as exc:
            raise ArchiveManagementError(tr("action.export_batch_canceled")) from exc
        except ArchiveManagementError as exc:
            log_action("export.batch_failed", games=len(selected), error=str(exc))
            raise
        finally:
            with self._operation_lock:
                self._active = None
        return tr(
            "result.export_batch_done",
            games=result.games,
            file=Path(destination).name,
            backups=result.backups,
            files=result.files,
            size=size_label(result.total_bytes),
        )

    def inspect_import(self, path: str) -> ImportInspection | BatchInspection:
        """只读体检一个导出包: 单游戏包或批量包, 由包清单里的类型决定.

        不改数据库, 也不在备份根下写任何东西; 分派(读一眼清单的类型)在用例层, 界面不
        自己拆 zip。失败(文件不存在/不是归档包/清单损坏)直接抛给界面提示: 体检没成功
        就不存在"写了一半"的问题, 所以这里不写回滚日志, 只把体检结果记进审计。
        """
        inspection = self._import.inspect_any(Path(path))
        self._log_inspection(inspection, path)
        return inspection

    def _log_inspection(
        self, inspection: ImportInspection | BatchInspection, source: str
    ) -> None:
        """体检结果的审计: 两类包的字段口径不同, 因此各记一条动作."""
        if isinstance(inspection, BatchInspection):
            log_action(
                "import.batch_inspected",
                games=inspection.game_count,
                backups=inspection.backup_count,
                files=inspection.file_count,
                source=redacted_path(source),
            )
            return
        log_action(
            "import.inspected",
            game=inspection.game_name,
            backups=inspection.backup_count,
            files=inspection.file_count,
            locations=len(inspection.locations),
            source=redacted_path(source),
        )

    def run_import(
        self,
        inspection: ImportInspection,
        *,
        strategy: str,
        target_game_id: str | None,
        locations: Mapping[int, str],
    ) -> str:
        """按策略真的导入这个包, 返回带真实计数的本地化提示.

        内容与数据库都由 :class:`ImportService` 处理(失败时它会回滚本次已建的节点);
        这里只负责把界面的字符串 id 换成整型、记审计与拼提示。“跳过”策略什么都不
        写, 因此提示里只说明跳过了多少份。
        """
        source = redacted_path(str(inspection.path))
        log_action("import.start", basic=True, strategy=strategy, source=source)
        try:
            result = self._import.import_package(
                inspection,
                strategy=strategy,
                target_game_id=self._import_target(target_game_id),
                locations=locations,
            )
        except ArchiveManagementError as exc:
            log_action(
                "import.failed", strategy=strategy, error=str(exc), source=source
            )
            raise
        self._touch()
        return _import_message(result)

    def run_import_batch(
        self, batch: BatchInspection, choices: Mapping[str, ImportChoice]
    ) -> str:
        """按每款游戏各自的选择导入整批, 返回带真实计数的本地化提示.

        ``choices`` 以**内层条目名**为键; 没给选择的那一款由服务层按默认值处理(新建,
        且不导入任何存档位置)。目标游戏的字符串 id 在这里换成整型并顺带校验存在
        (与 :meth:`run_import` 同一个 :meth:`_import_target`)。失败与取消的回滚边界都
        由服务层决定: 当前这一款只回收自己, **已经导完的保留**。

        用例层用同一个异常表达"失败"与"取消", 因此这里按真实请求过取消来区分: 只有
        用户真的按了取消才把它换成"已取消"那句可展示的说明, 真正的失败照常上抛。
        """
        source = redacted_path(str(batch.path))
        log_action(
            "import.batch_start", basic=True, games=batch.game_count, source=source
        )
        with self._operation_lock:
            if self._active is not None:
                raise ArchiveManagementError(tr("error.operation_busy"))
            # 整批一起导, 没有哪一款是"主角": 占位 id 只出现在取消审计里.
            self._active = _ActiveOperation(game_id=0)
        try:
            result = self._import.import_batch(
                batch,
                self._batch_choices(choices),
                progress=self._report_progress,
                cancelled=self._cancel_requested,
            )
        except ArchiveManagementError as exc:
            if not self._cancel_requested():
                log_action("import.batch_failed", error=str(exc), source=source)
                raise
            raise OperationCancelledError(tr("result.import_batch_canceled")) from exc
        finally:
            with self._operation_lock:
                self._active = None
        self._touch()
        return _batch_import_message(result)

    def _batch_choices(
        self, choices: Mapping[str, ImportChoice]
    ) -> dict[str, BatchImportChoice]:
        """把界面选择翻译成用例层的选择: 目标游戏顺带校验存在(未知目标报错)."""
        return {
            entry: BatchImportChoice(
                strategy=choice.strategy,
                target_game_id=self._import_target(choice.target_game_id),
                locations=choice.locations,
            )
            for entry, choice in choices.items()
        }

    # -- 调度与生命周期 ----------------------------------------------------

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
        self._reject_unusable_schedule(game, gid, clean)
        if not clean:
            self._scheduler.unschedule(gid)
            for job in self._jobs.for_game(gid):
                if job.id is not None:
                    self._jobs.delete(job.id)
            log_action("schedule.clear", game_id=gid)
            return self.task_status(game_id)
        active = self._requested_state(game, gid, enabled)
        minutes = parse_interval(clean)
        entry = self._scheduler.schedule(
            gid, minutes, self._scheduled_backup(gid), enabled=active
        )
        self._persist_job(
            gid,
            clean,
            enabled=active,
            next_run=entry.next_run_at,
            keep_auto=keep_auto,
        )
        log_action(
            "schedule.set",
            game_id=gid,
            interval=clean,
            enabled=active,
            keep_auto=keep_auto,
        )
        return self.task_status(game_id)

    def _reject_unusable_schedule(self, game: Game, gid: int, interval: str) -> None:
        """归档或没有存档位置时拒绝配置定时备份(并记下原因).

        ``interval`` 为空表示"取消定时备份", 这条路径永远放行。
        """
        if not interval:
            return
        if game.archived:
            # 归档之后只保留"删除/导出/取消归档/打开详情": 定时备份也归入不可用.
            log_action("schedule.rejected", game_id=gid, reason="archived")
            raise ArchiveManagementError(
                tr("error.archived_game_schedule", name=game.name)
            )
        if not self._locations.list_for_game(gid):
            # 没有存档位置就没有可备份的内容: 提前拒绝并告知原因.
            log_action("schedule.rejected", game_id=gid, reason="no_locations")
            raise ArchiveManagementError(
                tr("error.no_locations_schedule", name=game.name)
            )

    def _requested_state(self, game: Game, gid: int, requested: bool) -> bool:
        """把"想启用"折算成实际可用的启用态: 停用中的游戏只能暂停.

        已经有任务时说明用户在"继续", 这种情况明确拒绝而不是静默改成暂停;
        新建任务时则降级为暂停并记一条日志(界面会随之提示原因)。
        """
        if not requested or game.enabled:
            return requested
        if self._scheduler.get(gid) is not None:
            log_action("schedule.rejected", game_id=gid, reason="game_disabled")
            raise ArchiveManagementError(
                tr("error.disabled_game_schedule", name=game.name)
            )
        log_action("schedule.paused_disabled", game_id=gid)
        return False

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
        self._spawn_hash_backfill(game_id)
        return node

    def _spawn_hash_backfill(self, game_id: int) -> None:
        """把这款游戏里还缺哈希的备份交给一个后台线程补齐.

        **不在本次备份里等它**: 备份已经落盘并入库, 补齐只是把逐文件 sha256 补上
        (名称模式跳过哈希就是为了这会儿快) —— 补齐期间这些备份按名称校验, 界面上
        看得出来(见 ui.models 的校验状态)。

        以**游戏**为单位而不是单个节点: 恢复前的安全点走的是 ``RestoreService``
        (不经过这里), 它们在恢复完成后由 :meth:`run_restore` 一并排进来; 攒了十几份
        缺哈希的旧备份也只开一个线程, 顺序补完。
        """
        pending = [
            node
            for node in self._backups.list_nodes(game_id)
            if self._backups.needs_hash_backfill(node)
        ]
        if not pending:
            return
        threading.Thread(
            target=self._run_hash_backfill,
            args=(pending,),
            daemon=True,
            name="hash-backfill",
        ).start()

    def _run_hash_backfill(self, nodes: Sequence[BackupNode]) -> None:
        """后台逐个补齐哈希: 失败只记日志 —— 备份本身已经成功, 哈希缺着不影响使用."""
        for node in nodes:
            try:
                filled = self._backups.backfill_hashes(node)
            except (ArchiveManagementError, OSError) as exc:
                logger.warning("补齐备份哈希失败(%s): %s", node.storage_relpath, exc)
                continue
            logger.debug("补齐备份哈希 %d 个文件: %s", filled, node.storage_relpath)

    def _pause_schedule(self, gid: int) -> None:
        """把某游戏的定时备份置为暂停(停用或归档时调用; 没有任务就不做)."""
        jobs = self._jobs.for_game(gid)
        job = jobs[0] if jobs else None
        if job is None or not job.schedule.strip() or not job.enabled:
            return
        self.set_schedule(
            str(gid), job.schedule, enabled=False, keep_auto=job.keep_auto
        )

    def _scheduled_backup(self, game_id: int) -> Callable[[], None]:
        """构造定时备份回调(在调度线程执行, 不得触碰 Tk).

        存档与当前节点内容一致时本次自动备份静默跳过(create_backup 会写
        DEBUG 级日志), 任务状态仍记为"本次已执行", 不向用户报错。
        """

        def run() -> None:
            game = self._games.get(game_id)
            if game is None or not is_active(game):
                # 停用或归档期间不执行自动备份: 任务配置保留, 恢复启用后照常触发.
                log_action(
                    "schedule.skipped", basic=True, game_id=game_id, reason="inactive"
                )
                return
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
            created_label=format_moment(created),
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
        return self._next_run_state(game_id)[0]

    def _next_run_state(self, game_id: int) -> tuple[datetime | None, bool]:
        """返回(下次运行时间, 是否"配了周期但没启用").

        两者都返回 ``None`` / False 会让"未配置"与"配置了但暂停"在界面上变成同一句
        话 —— 用户看到"未配置"会以为自己没配过(用户反馈)。
        """
        entry = self._scheduler.get(game_id)
        if entry is None:
            return None, False
        if not entry.enabled:
            return None, True
        return entry.next_run_at, False

    # -- 内部 ---------------------------------------------------------------

    def _game(self, game_id: str) -> Game:
        """返回数据库中的游戏实体(不存在时抛错)."""
        game, _id = self._game_ref(game_id)
        return game

    def _import_target(self, game_id: str | None) -> int | None:
        """合并导入的目标游戏 id(str -> int); 没有目标时是 ``None``.

        顺带校验目标真的存在: 否则用户选中的游戏已经被删掉时, 错误会变成难懂的
        数据库异常而不是"未知游戏"。
        """
        if game_id is None:
            return None
        _game, gid = self._game_ref(game_id)
        return gid

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
        if game.id is None:  # pragma: no branch - 游戏行来自仓储, id 必然非空
            # 与上一行同一条判据: 仓储出来的行一定有 id, 这一支只是给类型检查一个出口。
            raise ArchiveManagementError(
                tr("error.unknown_game", game_id="")
            )  # pragma: no cover - 仓储出来的行一定有 id
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
            archived=game.archived,
            original_name=game.original_name,
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
    def _require_path(path: str, kind: PathKind) -> None:
        """要求路径可访问; 否则抛出异常."""
        probe = probe_path(path, kind)
        if not probe.ok:
            raise ArchiveManagementError(
                tr(f"error.loc_{probe.reason_code}", path=path)
            )
