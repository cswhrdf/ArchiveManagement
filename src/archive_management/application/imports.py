"""把导出包导入本机: 先体检, 再按策略落库(永不覆盖).

两步分开是为了让界面能先把"包里有什么、与库里的东西怎么冲突"摆给用户看:

- :meth:`ImportService.inspect`: 只读清单与配置(不校验内容哈希, 快), 给出游戏标识、
  存档位置(本机是否已存在同名路径)、备份节点与定时任务, 以及"疑似同一个游戏"的
  库内匹配(同平台 + 同 AppID);
- :meth:`ImportService.import_package`: 先逐条校验内容哈希, 再解到备份根下的暂存目录、
  逐个节点原子改名到位, **内容都到位之后**才写数据库; 任何一步失败都会把本次已建
  的节点(数据库行 + 目录)一并回收, 不留半截备份.

三种策略: 新建游戏 / 合并到现有游戏(把备份节点追加到该游戏的分支树上) / 跳过。
**导入永不覆盖**: 节点目录已存在时跳过该节点并计数, 存档位置走既有的"新增位置"
用例(规范化 + 判重 + 可访问性), 因此重复导入同一个包不会产生重复数据.

一次导入一个包用 :meth:`ImportService.import_package`; 一次导入一批(批量包)用
:meth:`ImportService.inspect_batch` 体检、:meth:`ImportService.import_batch` 按每个
游戏各自的选择导入(格式见 :mod:`archive_management.services.export_format`)。批量
导入复用同一条落库与回滚路径, 所以"永不覆盖"与"失败只回收自己那一款"这两条保证
对两种导入同样成立.
"""

from __future__ import annotations

import hashlib
import logging
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from uuid import uuid4

from archive_management.application.locations import add_save_location
from archive_management.domain import (
    DEFAULT_KEEP_AUTO,
    BackupFileEntry,
    BackupNode,
    Game,
    NodeKind,
    PathKind,
    SaveSource,
    ScheduledJob,
    VerificationMode,
    normalize_verification_mode,
)
from archive_management.exceptions import ArchiveManagementError, PackageError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
    ScheduledJobRepository,
)
from archive_management.services.audit import log_action, log_failure, redacted_path
from archive_management.services.export_format import (
    BATCH_KIND,
    BRANCHES_DIR,
    TIMELINE_DIR,
    CancelProbe,
    PackageContents,
    ProgressCallback,
    extract_package,
    read_batch_package,
    read_package,
    read_package_kind,
)
from archive_management.services.naming import game_folder
from archive_management.services.pathcheck import probe_path
from archive_management.services.snapshot import read_manifest

logger = logging.getLogger(__name__)

#: 导入策略: 新建游戏 / 合并到现有游戏 / 跳过这个包.
STRATEGY_NEW = "new"
STRATEGY_MERGE = "merge"
STRATEGY_SKIP = "skip"
STRATEGIES = (STRATEGY_NEW, STRATEGY_MERGE, STRATEGY_SKIP)


@dataclass(frozen=True)
class PackageLocation:
    """包里的一个存档位置(路径来自导出那台机器)."""

    index: int
    path: str
    path_kind: PathKind
    source: SaveSource
    is_primary: bool
    exists_here: bool


@dataclass(frozen=True)
class PackageNode:
    """包里的一个备份节点(含内容条数与体积, 供界面展示)."""

    key: str
    parent_key: str | None
    is_safety: bool
    kind: NodeKind
    title: str
    note: str
    branch_name: str | None
    created_at: str | None
    content_hash: str | None
    # 导出那台机器上这份备份用的校验方式; 老包没有这个字段 —— 那时只有 sha256 一种,
    # 因此缺失一律按 sha256 看待(见 domain.entities.normalize_verification_mode)。
    verify_mode: VerificationMode
    current: bool
    files: int
    total_bytes: int

    @property
    def directory(self) -> str:
        """该节点在包内的目录前缀."""
        return f"{TIMELINE_DIR if self.is_safety else BRANCHES_DIR}/{self.key}"


@dataclass(frozen=True)
class ImportInspection:
    """体检结果: 包里有什么、与库里怎么冲突."""

    path: Path
    game_name: str
    steam_app_id: int | None
    platform: str
    origin: str
    tags: tuple[str, ...]
    locations: tuple[PackageLocation, ...]
    nodes: tuple[PackageNode, ...]
    schedule: Mapping[str, object] | None
    matching_game_id: int | None

    @property
    def backup_count(self) -> int:
        """包里的备份节点数."""
        return len(self.nodes)

    @property
    def file_count(self) -> int:
        """包里的内容文件数(不含各节点自己的快照清单)."""
        return sum(node.files for node in self.nodes)

    @property
    def total_bytes(self) -> int:
        """包里内容文件的总字节数."""
        return sum(node.total_bytes for node in self.nodes)


@dataclass(frozen=True)
class ImportResult:
    """一次导入的结果摘要."""

    game_id: int
    game_name: str
    strategy: str
    nodes: int
    files: int
    total_bytes: int
    skipped_nodes: int
    locations: int


@dataclass(frozen=True)
class BatchGameInspection:
    """批量包里一款游戏的体检结果(单包体检 + 它在包里的条目名)."""

    entry: str
    inspection: ImportInspection

    @property
    def name(self) -> str:
        """游戏名称(与单包体检一致)."""
        return self.inspection.game_name


@dataclass(frozen=True)
class BatchInspection:
    """批量包的体检结果: 逐游戏体检 + 汇总.

    ``games`` 的顺序与包内清单一致(也就是导出时的顺序)。逐游戏体检里的
    ``path`` 指向包**内部**的临时文件, 只在体检那一刻有效 —— 要导入请交给
    :meth:`ImportService.import_batch`, 它会自己把内层包重新读出来.
    """

    path: Path
    games: tuple[BatchGameInspection, ...]

    @property
    def game_count(self) -> int:
        """包里的游戏数."""
        return len(self.games)

    @property
    def backup_count(self) -> int:
        """包里的备份节点总数."""
        return sum(item.inspection.backup_count for item in self.games)

    @property
    def file_count(self) -> int:
        """包里的内容文件总数."""
        return sum(item.inspection.file_count for item in self.games)

    @property
    def total_bytes(self) -> int:
        """包里的内容总字节数."""
        return sum(item.inspection.total_bytes for item in self.games)


@dataclass(frozen=True)
class BatchImportChoice:
    """批量导入里一款游戏的选择(与单包 :meth:`ImportService.import_package` 一一对应)."""

    strategy: str = STRATEGY_NEW
    target_game_id: int | None = None
    locations: Mapping[int, str] | None = None


@dataclass(frozen=True)
class BatchImportResult:
    """一次批量导入的结果摘要(逐游戏结果 + 汇总)."""

    results: tuple[ImportResult, ...]
    nodes: int
    files: int
    total_bytes: int
    skipped_nodes: int
    skipped_games: int

    @property
    def games(self) -> int:
        """本次处理过的游戏数(含按"跳过"策略处理的那些)."""
        return len(self.results)


class ImportService:
    """导出包导入的用例层."""

    def __init__(self, database: Database, *, backup_root: Path) -> None:
        """绑定数据库与备份根目录(导入的内容就落在这里)."""
        self._database = database
        self._backup_root = backup_root
        self._games = GameRepository(database)
        self._locations = SaveLocationRepository(database)
        self._backups = BackupRepository(database)
        self._jobs = ScheduledJobRepository(database)

    # -- 体检 ---------------------------------------------------------------

    def inspect(self, path: Path) -> ImportInspection:
        """只读地解析包结构与配置, 并把冲突项一并算出来."""
        return self._inspection_from(read_package(path))

    def _inspection_from(self, contents: PackageContents) -> ImportInspection:
        """由已经读出的单包内容组装体检结果(单包与批量包共用这一条路径)."""
        game = _mapping(contents.config.get("game"), "game")
        name = _text(game, "name")
        if not name:
            raise PackageError("导出包里的游戏名称为空")
        nodes = tuple(_node(contents, item) for item in contents.config_list("backups"))
        return ImportInspection(
            path=contents.path,
            game_name=name,
            steam_app_id=_int_or_none(game.get("steam_app_id")),
            platform=_text(game, "platform", "windows"),
            origin=_text(game, "origin", "manual"),
            tags=tuple(_text(item, "value") for item in _tags(game)),
            locations=tuple(
                _location(item) for item in contents.config_list("locations")
            ),
            nodes=nodes,
            schedule=_mapping_or_none(contents.config.get("schedule")),
            matching_game_id=self._matching_game(game),
        )

    def _matching_game(self, game: Mapping[str, object]) -> int | None:
        """库里"疑似同一个游戏"的 id: 同平台 + 同 AppID 才算."""
        app_id = _int_or_none(game.get("steam_app_id"))
        platform = _text(game, "platform", "windows")
        if app_id is None:
            return None
        for existing in self._games.list():
            if existing.steam_app_id == app_id and existing.platform == platform:
                return existing.id
        return None

    def inspect_any(self, path: Path) -> ImportInspection | BatchInspection:
        """按清单里的包类型分派: 单游戏包走 :meth:`inspect`, 批量包走 :meth:`inspect_batch`.

        调用方(界面)不该自己拆 zip 判断类型, 所以分派收在这里; 判断只用
        :func:`read_package_kind` 读一眼清单, 真正的体检仍是上面两条路径, 因此
        "两种包各自一套口径"这条不变量不受影响。
        """
        if read_package_kind(path) == BATCH_KIND:
            return self.inspect_batch(path)
        return self.inspect(path)

    def inspect_batch(self, path: Path) -> BatchInspection:
        """批量包体检: 逐游戏给出单包体检的全部内容 + 它在包里的条目名.

        内层包由 :func:`read_batch_package` 解到临时目录, 这里在 ``with`` 块内把
        每个内层包交给与单包同一条体检路径(:meth:`_inspection_from`), 因此批量
        体检不会出现第二套口径。临时目录在方法返回时就删掉了, 所以逐游戏体检里的
        ``path`` 只在展示上有意义 —— 要导入请用 :meth:`import_batch`.
        """
        with read_batch_package(path) as contents:
            games = tuple(
                BatchGameInspection(
                    entry=item.entry,
                    inspection=self._inspection_from(item.package),
                )
                for item in contents.games
            )
        return BatchInspection(path=path, games=games)

    # -- 导入 ---------------------------------------------------------------

    def import_package(
        self,
        inspection: ImportInspection,
        *,
        strategy: str = STRATEGY_NEW,
        target_game_id: int | None = None,
        locations: Mapping[int, str] | None = None,
        progress: ProgressCallback | None = None,
        cancelled: CancelProbe | None = None,
    ) -> ImportResult:
        """按策略导入这个包, 返回导入结果.

        ``locations`` 是"包内序号 -> 本机路径"的映射: 没有映射的存档位置不导入
        (用户选择的目录不猜), 映射里的路径仍要走既有的规范化与判重.
        """
        self._validate_strategy(strategy)
        if strategy == STRATEGY_SKIP:
            return _skipped(inspection, target_game_id)
        contents = read_package(inspection.path, verify_hashes=True)
        return self._import_contents(
            contents,
            inspection,
            strategy=strategy,
            target_game_id=target_game_id,
            locations=locations,
            progress=progress,
            cancelled=cancelled,
        )

    def _validate_strategy(self, strategy: str) -> None:
        """导入策略必须是已知取值(未知策略直接拒绝, 不猜)."""
        if strategy not in STRATEGIES:
            raise ArchiveManagementError(f"未知的导入策略: {strategy}")

    def _import_contents(
        self,
        contents: PackageContents,
        inspection: ImportInspection,
        *,
        strategy: str,
        target_game_id: int | None,
        locations: Mapping[int, str] | None,
        progress: ProgressCallback | None = None,
        cancelled: CancelProbe | None = None,
        storage_suffix: str = "",
    ) -> ImportResult:
        """把一个**已经读出**的单包按策略落到本机(单包与批量导入共用这一条路径).

        ``storage_suffix`` 只给批量导入用: 同一个批量包里两款同名同路径的游戏必须
        落在不同的备份目录下, 见 :func:`_storage_suffix`. 失败时只回收本次已落位的
        备份节点(目录 + 行), 游戏行与已经写入的存档位置保持不动 —— 与单包导入
        完全同一条路径, 因此两处的回滚边界天然一致.
        """
        game_id = self._resolve_game(strategy, inspection, target_game_id)
        key = self._storage_key(game_id, inspection, locations, suffix=storage_suffix)
        written_locations = self._write_locations(game_id, inspection, locations)
        created: list[tuple[int, Path]] = []
        try:
            nodes, files, total, skipped = self._place_nodes(
                contents, game_id, key, inspection, created, progress, cancelled
            )
        except BaseException:
            self._rollback(created)
            raise
        self._create_schedule(game_id, inspection)
        log_action(
            "import.succeeded",
            game_id=game_id,
            strategy=strategy,
            nodes=nodes,
            files=files,
            skipped_nodes=skipped,
            source=redacted_path(str(contents.path)),
        )
        return ImportResult(
            game_id=game_id,
            game_name=inspection.game_name,
            strategy=strategy,
            nodes=nodes,
            files=files,
            total_bytes=total,
            skipped_nodes=skipped,
            locations=written_locations,
        )

    def _resolve_game(
        self, strategy: str, inspection: ImportInspection, target_game_id: int | None
    ) -> int:
        """确定内容落到哪个游戏上(新建一条或复用给定的那条)."""
        if strategy == STRATEGY_NEW:
            game = self._games.add(
                Game(
                    name=inspection.game_name,
                    original_name=inspection.game_name,
                    steam_app_id=inspection.steam_app_id,
                    platform=inspection.platform,
                    origin=inspection.origin,
                    tags=inspection.tags,
                    # 启用态是本机状态: 导入一律新建为停用, 由用户自己启用一款.
                    enabled=False,
                )
            )
            if game.id is None:  # pragma: no cover - add() 总是返回 id
                raise ArchiveManagementError("导入失败: 未返回游戏 id")
            log_action("import.game_created", game_id=game.id, name=game.name)
            return game.id
        if target_game_id is None:
            raise ArchiveManagementError("合并导入需要先选定要合并到的游戏")
        if self._games.get(target_game_id) is None:
            raise ArchiveManagementError(f"未知游戏: {target_game_id}")
        return target_game_id

    def _storage_key(
        self,
        game_id: int,
        inspection: ImportInspection,
        locations: Mapping[int, str] | None,
        *,
        suffix: str = "",
    ) -> str:
        """冻结本机的备份目录名(按"名称 + 映射后的路径"推导).

        ``suffix`` 只给批量导入用(见 :func:`_storage_suffix`); 单包导入保持默认值,
        因此同一个包重复导入仍然落在同一个目录里(已存在的节点会被跳过).
        """
        paths = [
            locations[item.index]
            for item in inspection.locations
            if locations and item.index in locations
        ]
        folder = game_folder(inspection.game_name, paths) + suffix
        return self._games.ensure_storage_key(game_id, folder)

    def _write_locations(
        self,
        game_id: int,
        inspection: ImportInspection,
        locations: Mapping[int, str] | None,
    ) -> int:
        """按映射写存档位置: 没映射的不导入, 已存在的不重复写."""
        written = 0
        for item in inspection.locations:
            target = None if locations is None else locations.get(item.index)
            if target is None:
                continue
            probe = probe_path(target, item.path_kind)
            if not probe.ok:
                raise ArchiveManagementError(f"导入的存档位置不可用: {target}")
            if self._locations.duplicate_of(game_id, probe.normalized) is not None:
                continue
            add_save_location(
                self._database,
                game_id,
                path=probe.normalized,
                kind=item.path_kind,
                source=item.source,
            )
            written += 1
        return written

    def _place_nodes(
        self,
        contents: PackageContents,
        game_id: int,
        key: str,
        inspection: ImportInspection,
        created: list[tuple[int, Path]],
        progress: ProgressCallback | None,
        cancelled: CancelProbe | None,
    ) -> tuple[int, int, int, int]:
        """把每个节点解到暂存目录、原子改名到位、再写库; 返回统计."""
        staging = self._staging_dir()
        ids: dict[str, int] = {}
        parent_of_root = self._games.current_backup(game_id)
        files = total = skipped = 0
        try:
            for index, node in enumerate(inspection.nodes):
                if cancelled is not None and cancelled():
                    raise ArchiveManagementError("导入已取消")
                _report(progress, index / max(1, len(inspection.nodes)), node.key)
                if self._node_exists(key, node.key):
                    skipped += 1
                    continue
                parent_id = (
                    parent_of_root
                    if node.parent_key is None
                    else ids.get(node.parent_key)
                )
                entries = self._extract_node(contents, staging, node)
                target = self._backup_root / key / node.key
                # 先写库再改名到位: 这样"目录已就位"永远不会早于"这一份进了
                # 回收账本"(created), 落库失败时目录还没搬过去, 不会留下一个
                # 会被下次导入当成"已存在"而跳过、内容却进不了库的孤儿目录.
                created_id = self._insert_node(game_id, key, node, parent_id, entries)
                created.append((created_id, target))
                target.parent.mkdir(parents=True, exist_ok=True)
                (staging / node.directory).replace(target)
                ids[node.key] = created_id
                if node.current:
                    self._games.set_current_backup(game_id, created_id)
                files += len(entries)
                total += sum(entry.size for entry in entries)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        return len(ids), files, total, skipped

    def _extract_node(
        self, contents: PackageContents, staging: Path, node: PackageNode
    ) -> list[BackupFileEntry]:
        """解出一个节点并返回它自己的快照清单(清单里的条目就是要写库的文件清单)."""
        members = contents.member_paths(node.directory)
        if not members:
            raise PackageError(f"导出包里缺少节点内容: {node.key}")
        extract_package(contents, staging, members=members)
        manifest = read_manifest(staging / node.directory)
        return [
            BackupFileEntry(
                backup_id=0,
                relative_path=item.relative_path,
                size=item.size,
                sha256=item.sha256,
                file_kind=item.file_kind,
            )
            for item in manifest.entries
        ]

    def _insert_node(
        self,
        game_id: int,
        key: str,
        node: PackageNode,
        parent_id: int | None,
        entries: Sequence[BackupFileEntry],
    ) -> int:
        """写入备份节点与文件清单, 返回新节点 id."""
        created = self._backups.add_with_files(
            BackupNode(
                game_id=game_id,
                parent_id=parent_id,
                node_kind=node.kind,
                branch_name=node.branch_name,
                note=node.note,
                title=node.title,
                content_hash=node.content_hash,
                storage_relpath=f"{key}/{node.key}",
                created_at=_moment(node.created_at),
                is_safety=node.is_safety,
                verify_mode=node.verify_mode,
            ),
            entries,
        )
        if created.id is None:  # pragma: no cover - add_with_files 总是返回 id
            raise ArchiveManagementError("导入失败: 未返回备份节点 id")
        return created.id

    def _node_exists(self, key: str, node_key: str) -> bool:
        """目标节点目录是否已存在(存在就跳过, 不覆盖)."""
        return (self._backup_root / key / node_key).exists()

    def _staging_dir(self) -> Path:
        """一个本次导入专用的暂存目录(与备份根同盘, 改名才是原子的)."""
        staging = self._backup_root / f".import-{uuid4().hex[:8]}"
        staging.mkdir(parents=True, exist_ok=True)
        return staging

    def _rollback(self, created: Sequence[tuple[int, Path]]) -> None:
        """回收本次已导入的节点: 先删目录再删行, 失败只记日志."""
        for node_id, directory in reversed(list(created)):
            try:
                shutil.rmtree(directory, ignore_errors=True)
                self._backups.delete(node_id)
            except Exception as exc:
                logger.warning("导入回滚失败: 节点 %s (%s)", node_id, exc)
        log_failure("import.failed", nodes=len(created), result="rolled_back")

    def _create_schedule(self, game_id: int, inspection: ImportInspection) -> None:
        """包里有定时任务且本机还没有时补上(已有的不动, 免得覆盖本机配置)."""
        if inspection.schedule is None or self._jobs.for_game(game_id):
            return
        interval = _text(inspection.schedule, "interval")
        if not interval:
            return
        self._jobs.upsert(
            ScheduledJob(
                game_id=game_id,
                schedule=interval,
                enabled=_flag(inspection.schedule, "enabled", True),
                keep_auto=_int_or_none(inspection.schedule.get("keep_auto"))
                or DEFAULT_KEEP_AUTO,
            )
        )

    # -- 批量包 -------------------------------------------------------------

    def import_batch(
        self,
        batch: BatchInspection,
        choices: Mapping[str, BatchImportChoice],
        *,
        progress: ProgressCallback | None = None,
        cancelled: CancelProbe | None = None,
    ) -> BatchImportResult:
        """按每个游戏各自的选择导入整批, 返回逐游戏结果与汇总.

        ``choices`` 以**内层条目名**为键, 值与单包 :meth:`import_package` 的参数
        一一对应; 没有给选择的那一款按默认策略(新建, 且不导入任何存档位置)处理,
        与单包导入的默认值一致。策略为 :data:`STRATEGY_SKIP` 的那一款什么都不写,
        只计入 :attr:`BatchImportResult.skipped_games`.

        **回滚边界与单包完全一致**: 某一款失败时只回收它自己已落位的备份节点
        (目录 + 数据库行, 见 :meth:`_place_nodes`), 它新建的游戏行与已写入的存档
        位置保留, 前面导完的游戏也原样保留。**失败会让整批停下**: 失败通常来自
        磁盘或数据库这类系统性原因, 继续导只会在未知状态上再压几笔; 停下来之后
        用户修好重跑即可, 已经存在的节点会被跳过。``cancelled`` 在游戏之间与节点
        之间生效, 边界与失败一致.
        """
        # 逐条复核内层包的哈希(与单包导入同一条纪律): 体检为了快可以不校验,
        # 但真正落库之前必须核对过, 否则"只落已校验的内容"这条就不成立了.
        with read_batch_package(batch.path, verify_hashes=True) as contents:
            packages = {item.entry: item.package for item in contents.games}
            results = self._import_games(batch, packages, choices, progress, cancelled)
        log_action(
            "import.batch_succeeded",
            games=len(results),
            nodes=sum(item.nodes for item in results),
            source=redacted_path(str(batch.path)),
        )
        return _batch_result(results)

    def _import_games(
        self,
        batch: BatchInspection,
        packages: Mapping[str, PackageContents],
        choices: Mapping[str, BatchImportChoice],
        progress: ProgressCallback | None,
        cancelled: CancelProbe | None,
    ) -> list[ImportResult]:
        """按顺序逐游戏导入(哪一款失败就立刻上抛, 后面的不再处理)."""
        results: list[ImportResult] = []
        count = max(1, len(batch.games))
        for index, item in enumerate(batch.games):
            _check_cancelled(cancelled)
            results.append(
                self._import_one(
                    item,
                    packages[item.entry],
                    choices.get(item.entry, BatchImportChoice()),
                    _slice_progress(progress, index, count),
                    cancelled,
                )
            )
        return results

    def _import_one(
        self,
        item: BatchGameInspection,
        package: PackageContents,
        choice: BatchImportChoice,
        progress: ProgressCallback | None,
        cancelled: CancelProbe | None,
    ) -> ImportResult:
        """导入批量包里的一款游戏(复用单包的全部落库与回滚逻辑)."""
        self._validate_strategy(choice.strategy)
        if choice.strategy == STRATEGY_SKIP:
            return _skipped(item.inspection, choice.target_game_id)
        return self._import_contents(
            package,
            item.inspection,
            strategy=choice.strategy,
            target_game_id=choice.target_game_id,
            locations=choice.locations,
            progress=progress,
            cancelled=cancelled,
            storage_suffix=_storage_suffix(item.entry),
        )


def _check_cancelled(cancelled: CancelProbe | None) -> None:
    """取消探针为真时立刻中止(当前这一款会回滚)."""
    if cancelled is not None and cancelled():
        raise ArchiveManagementError("导入已取消")


def _slice_progress(
    progress: ProgressCallback | None, index: int, count: int
) -> ProgressCallback | None:
    """把一款游戏的 0..1 进度映射到整批的对应区间(整批进度仍然单调)."""
    if progress is None:
        return None
    callback: ProgressCallback = progress

    def _report(ratio: float, message: str) -> None:
        callback((index + ratio) / count, message)

    return _report


def _storage_suffix(entry: str) -> str:
    """批量导入时给备份目录名加一个由条目名派生的短后缀.

    同一个批量包里两款**同名同路径**的游戏不能共用备份目录: 否则第二款会看到
    第一款写下的节点目录, 整批被当成"已存在"而跳过。后缀只由条目名决定, 因此
    同一个批量包重导时后缀不变, "已存在的节点被跳过"这条语义仍然成立.
    """
    digest = hashlib.sha256(entry.encode("utf-8")).hexdigest()
    return f"-{digest[:8]}"


def _batch_result(results: Sequence[ImportResult]) -> BatchImportResult:
    """把逐游戏结果汇总成批量结果."""
    return BatchImportResult(
        results=tuple(results),
        nodes=sum(item.nodes for item in results),
        files=sum(item.files for item in results),
        total_bytes=sum(item.total_bytes for item in results),
        skipped_nodes=sum(item.skipped_nodes for item in results),
        skipped_games=sum(1 for item in results if item.strategy == STRATEGY_SKIP),
    )


def _skipped(inspection: ImportInspection, target_game_id: int | None) -> ImportResult:
    """跳过这个包时的结果(什么都不做, 也要给出可展示的说明)."""
    return ImportResult(
        game_id=0 if target_game_id is None else target_game_id,
        game_name=inspection.game_name,
        strategy=STRATEGY_SKIP,
        nodes=0,
        files=0,
        total_bytes=0,
        skipped_nodes=inspection.backup_count,
        locations=0,
    )


def _report(progress: ProgressCallback | None, ratio: float, key: str) -> None:
    """上报进度(与快照/导出同一套回调约定)."""
    if progress is not None:
        progress(ratio, f"正在导入 {key}")


def _node(contents: PackageContents, raw: Mapping[str, object]) -> PackageNode:
    """解析配置里的一条备份节点, 并统计它带来的内容文件数与体积."""
    key = _text(raw, "id")
    if not key:
        raise PackageError("导出包里的备份节点缺少 id")
    prefix = f"{TIMELINE_DIR if _flag(raw, 'is_safety') else BRANCHES_DIR}/{key}/"
    members = [
        entry
        for entry in contents.entries
        if entry.path.startswith(prefix)
        and entry.path.rsplit("/", 1)[-1] != "snapshot.json"
    ]
    return PackageNode(
        key=key,
        parent_key=_optional_text(raw, "parent_id"),
        is_safety=_flag(raw, "is_safety"),
        kind=_kind(raw),
        title=_text(raw, "title"),
        note=_text(raw, "note"),
        branch_name=_optional_text(raw, "branch_name"),
        created_at=_optional_text(raw, "created_at"),
        content_hash=_optional_text(raw, "content_hash"),
        verify_mode=normalize_verification_mode(_optional_text(raw, "verify_mode")),
        current=_flag(raw, "current"),
        files=len(members),
        total_bytes=sum(entry.size for entry in members),
    )


def _location(raw: Mapping[str, object]) -> PackageLocation:
    """解析配置里的一个存档位置(并探测本机有没有同名路径)."""
    index = _int_or_none(raw.get("index"))
    path = _text(raw, "path")
    if index is None or not path:
        raise PackageError("导出包里的存档位置缺少序号或路径")
    kind: PathKind = "file" if _text(raw, "path_kind") == "file" else "directory"
    source: SaveSource = "steam" if _text(raw, "source") == "steam" else "manual"
    return PackageLocation(
        index=index,
        path=path,
        path_kind=kind,
        source=source,
        is_primary=_flag(raw, "is_primary"),
        exists_here=probe_path(path, kind).ok,
    )


def _kind(raw: Mapping[str, object]) -> NodeKind:
    """节点类型, 未知取值按手动备份处理."""
    value = _text(raw, "kind")
    if value in ("branch", "auto"):
        return "branch" if value == "branch" else "auto"
    return "manual"


def _moment(value: str | None) -> datetime | None:
    """把包里的时间戳字符串解析成时间(不合法就留空, 由数据库补当前时间)."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _mapping(value: object, key: str) -> Mapping[str, object]:
    """要求配置里的某个字段是对象."""
    if not isinstance(value, Mapping):
        raise PackageError(f"config.json 里的 {key} 不是对象")
    return {str(name): item for name, item in value.items()}


def _mapping_or_none(value: object) -> Mapping[str, object] | None:
    """可选的对象字段."""
    if isinstance(value, Mapping):
        return {str(name): item for name, item in value.items()}
    return None


def _tags(game: Mapping[str, object]) -> list[Mapping[str, object]]:
    """游戏标签(包里是字符串数组, 这里统一成"对象列表"的形状便于复用取值)."""
    raw = game.get("tags")
    if not isinstance(raw, list):
        return []
    return [{"value": item} for item in raw if isinstance(item, str)]


def _text(raw: Mapping[str, object], key: str, default: str = "") -> str:
    """取字符串字段(不是字符串时按默认值处理)."""
    value = raw.get(key)
    return value if isinstance(value, str) else default


def _optional_text(raw: Mapping[str, object], key: str) -> str | None:
    """取可空字符串字段."""
    value = raw.get(key)
    return value if isinstance(value, str) and value else None


def _int_or_none(value: object) -> int | None:
    """取整数字段(布尔值不算整数)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _flag(raw: Mapping[str, object], key: str, default: bool = False) -> bool:
    """取布尔字段."""
    value = raw.get(key)
    return value if isinstance(value, bool) else default
