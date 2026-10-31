"""把一款游戏导出为可移植的归档包.

包里只放"用户在界面上确认过的逻辑信息 + 备份内容":

- ``config.json``: 游戏配置、存档位置、每个备份节点(时间线标识 + 分支归属 + 引用
  的内容目录)、定时任务配置;
- ``timeline/<内容>/``: 备份内容只按时间线存**一份** —— 内容相同的节点(例如恢复前
  安全点与内容未变的下一次备份)共用同一份文件, 由各节点配置里的 ``content`` 字段
  指过去, 不在包里重复装第二遍(内容目录带着它自己的 ``snapshot.json``, 因此每个
  ``loc-<序号>`` 原本对应哪个路径、哪些条目是符号链接都能还原)。

缓存图、译名缓存、探测结果这类可再生数据不进包; 存档位置的绝对路径来自**导出那台
机器**, 在 ``config.json`` 里标记为 ``original_machine``, 导入时由用户确认映射。

一次导出一款游戏用 :meth:`ExportService.export_game`; 一次导出一批游戏用
:meth:`ExportService.export_games` —— 它逐游戏走同一条单包导出, 再把内层包装成
一个批量包(格式见 :mod:`archive_management.services.export_format`)。

打包本身交给 :mod:`archive_management.services.export_format`: 先写同级临时文件、
回读校验后再改名, 因此用户选择的路径上不会出现半成品.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from archive_management.application.backup import BackupService
from archive_management.domain import BackupNode, Game
from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
    ScheduledJobRepository,
)
from archive_management.services.audit import log_action, redacted_path
from archive_management.services.export_format import (
    EXPORT_FORMAT_VERSION,
    TIMELINE_DIR,
    BatchGame,
    CancelProbe,
    PackageFile,
    ProgressCallback,
    archive_entry_name,
    write_batch_package,
    write_package,
)
from archive_management.services.naming import game_slug

logger = logging.getLogger(__name__)

#: 批量导出时逐个内层包落脚的一次性目录名前缀(与目标**同级**, 同盘才能原子改名).
_BATCH_STAGING_PREFIX = ".batch-"


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果(供界面展示"导出了多少")."""

    destination: Path
    game_name: str
    backups: int
    files: int
    total_bytes: int


@dataclass(frozen=True)
class BatchExportResult:
    """一次批量导出的结果(逐游戏计数 + 总量).

    ``results`` 是逐游戏的 :class:`ExportResult`(顺序与传入的 ``game_ids`` 一致);
    ``games``/``backups`` 是它们的个数与备份节点总数, ``files``/``total_bytes``
    是各游戏内容文件的数量与字节数之和(内层包的条数, 不含外层包装).
    """

    destination: Path
    results: tuple[ExportResult, ...]
    files: int
    total_bytes: int

    @property
    def games(self) -> int:
        """包里的游戏数."""
        return len(self.results)

    @property
    def backups(self) -> int:
        """包里的备份节点总数."""
        return sum(item.backups for item in self.results)

    @property
    def game_names(self) -> tuple[str, ...]:
        """包里各游戏的名称(顺序与导入顺序一致)."""
        return tuple(item.game_name for item in self.results)


class ExportService:
    """单游戏导出的用例层."""

    def __init__(self, database: Database, *, backup_root: Path) -> None:
        """绑定数据库与备份根目录(快照内容就放在它下面)."""
        self._backups = BackupService(database, backup_root=backup_root)
        self._games = GameRepository(database)
        self._locations = SaveLocationRepository(database)
        self._nodes = BackupRepository(database)
        self._jobs = ScheduledJobRepository(database)

    def export_game(
        self,
        game_id: int,
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
        cancelled: CancelProbe | None = None,
    ) -> ExportResult:
        """把这款游戏(含全部备份内容)导出到 ``destination``, 返回包摘要.

        目标已存在时会被**原子替换**(用户在保存对话框里已经确认过这个文件名);
        取消或失败都会删掉临时文件, 不动原文件。
        """
        game = self._require_game(game_id)
        nodes = self._nodes.list_for_game(game_id)
        # 先决定"每个节点引用哪份内容": 内容相同的节点共用一份, 包里只装一遍.
        content_of, owners = _content_plan(nodes)
        members = self._collect_members(owners)
        summary = write_package(
            destination,
            game=_manifest_game(game),
            config=self._build_config(game_id, game, nodes, content_of),
            members=members,
            progress=progress,
            cancelled=cancelled,
        )
        log_action(
            "export.succeeded",
            game_id=game.id,
            name=game.name,
            backups=len(nodes),
            files=summary.file_count,
            destination=redacted_path(str(destination)),
        )
        return ExportResult(
            destination=destination,
            game_name=game.name,
            backups=len(nodes),
            files=summary.file_count,
            total_bytes=summary.total_bytes,
        )

    def _require_game(self, game_id: int) -> Game:
        """读取游戏记录, 不存在时给出可读错误."""
        game = self._games.get(game_id)
        if game is None:
            raise ArchiveManagementError(f"未知游戏: {game_id}")
        return game

    def export_games(
        self,
        game_ids: Sequence[int],
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
        cancelled: CancelProbe | None = None,
    ) -> BatchExportResult:
        """把若干款游戏各导成一个单包, 再装成一个批量包, 返回结果摘要.

        每款游戏都走**既有的单包导出**(:meth:`export_game`): 内层包先落在目标
        同级的一个临时目录里, 全部写完才由
        :func:`~archive_management.services.export_format.write_batch_package`
        装成外层包 —— 因此半成品既不会出现在目标路径上, 也不会留在临时目录里。

        未知或空的 ``game_ids`` 与单包导出同样报错; **任何一款失败或取消都让整批
        失败**(目标路径不留文件, 临时目录被删掉): 静默少导一款会让用户以为这一批
        都归档好了, 那是会丢数据的谎报.
        """
        if not game_ids:
            raise ArchiveManagementError("批量导出需要至少 1 款游戏")
        staging = _staging_dir(destination)
        try:
            exported = self._export_each(
                game_ids, staging, progress=progress, cancelled=cancelled
            )
            write_batch_package(
                destination,
                games=[game for _result, game in exported],
                progress=progress,
                cancelled=cancelled,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        result = BatchExportResult(
            destination=destination,
            results=tuple(item for item, _game in exported),
            files=sum(item.files for item, _game in exported),
            total_bytes=sum(item.total_bytes for item, _game in exported),
        )
        log_action(
            "export.batch_succeeded",
            games=result.games,
            files=result.files,
            destination=redacted_path(str(destination)),
        )
        return result

    def _export_each(
        self,
        game_ids: Sequence[int],
        staging: Path,
        *,
        progress: ProgressCallback | None,
        cancelled: CancelProbe | None,
    ) -> list[tuple[ExportResult, BatchGame]]:
        """逐个游戏导出内层包(条目名按游戏名派生, 撞车才加序号)."""
        used: set[str] = set()
        exported: list[tuple[ExportResult, BatchGame]] = []
        for game_id in game_ids:
            _check_cancelled(cancelled)
            entry = _unique_entry(game_slug(self._require_game(game_id).name), used)
            path = staging / entry
            result = self.export_game(
                game_id, path, progress=progress, cancelled=cancelled
            )
            exported.append((result, BatchGame(entry=entry, source=path)))
        return exported

    def _collect_members(self, owners: Sequence[BackupNode]) -> list[PackageFile]:
        """把每份内容的快照目录展开成包内文件(全部落在 ``timeline/`` 这棵树上).

        ``owners`` 是 :func:`_content_plan` 判定需要落盘的节点; 其余节点不在这里
        出现 —— 它们的内容与某个 owner 相同, config.json 用 ``content`` 字段指过去.
        """
        members: list[PackageFile] = []
        for node in owners:
            root = self._backups.snapshot_root(node)
            node_dir = f"{TIMELINE_DIR}/{_node_key(node)}"
            for path in sorted(root.rglob("*")):
                if path.is_file():
                    relative = path.relative_to(root).as_posix()
                    members.append(PackageFile(f"{node_dir}/{relative}", path))
        return members

    def _build_config(
        self,
        game_id: int,
        game: Game,
        nodes: Sequence[BackupNode],
        content_of: Mapping[int | None, str],
    ) -> dict[str, object]:
        """组装 config.json: 游戏、存档位置、备份结构与定时任务.

        每个备份节点同时写明**时间线标识**(``id``, 它自己的目录名)与**分支归属**
        (``parent_id``/``branch_name``/``is_safety``), 再用 ``content`` 指向它引用的
        内容目录 —— 内容在包里只存一份, 两个视图的关系全靠这一份配置表达.
        """
        keys = {node.id: _node_key(node) for node in nodes}
        current = self._games.current_backup(game_id)
        return {
            "version": EXPORT_FORMAT_VERSION,
            "game": _game_config(game),
            "locations": self._location_configs(game_id),
            "backups": [
                _backup_config(
                    node,
                    keys,
                    content=content_of[node.id],
                    current=node.id == current,
                )
                for node in nodes
            ],
            "schedule": self._schedule_config(game_id),
        }

    def _location_configs(self, game_id: int) -> list[dict[str, object]]:
        """存档位置按 ``loc-<序号>`` 的顺序导出, 序号与快照清单里的来源一致."""
        return [
            {
                "index": index,
                "path": location.path,
                "path_kind": location.path_kind,
                "source": location.source,
                "is_primary": location.is_primary,
                # 这是导出那台机器上的路径: 导入时必须由用户确认映射.
                "original_machine": True,
            }
            for index, location in enumerate(self._locations.list_for_game(game_id))
        ]

    def _schedule_config(self, game_id: int) -> Mapping[str, object] | None:
        """定时任务配置(没有配置时给 None, 由导入方决定是否新建)."""
        jobs = self._jobs.for_game(game_id)
        if not jobs:
            return None
        job = jobs[0]
        return {
            "interval": job.schedule,
            "enabled": job.enabled,
            "keep_auto": job.keep_auto,
        }


def _manifest_game(game: Game) -> dict[str, object]:
    """Manifest 里的游戏标识(不含路径与用户数据)."""
    return {
        "name": game.name,
        "original_name": game.original_name,
        "steam_app_id": game.steam_app_id,
        "platform": game.platform,
        "origin": game.origin,
        "archived": game.archived,
    }


def _game_config(game: Game) -> dict[str, object]:
    """config.json 里的游戏配置(名字、标识、标签与本机状态)."""
    config = _manifest_game(game)
    config["tags"] = list(game.tags)
    # 启用态是"这台机器上正在玩哪一款"的本机状态: 导入时不照搬(否则会出现
    # 两款同时启用), 但仍然如实记录下来, 便于用户自己判断.
    config["enabled"] = game.enabled
    config["machine_local"] = True
    return config


def _backup_config(
    node: BackupNode,
    keys: Mapping[int | None, str],
    *,
    content: str,
    current: bool,
) -> dict[str, object]:
    """config.json 里的一条备份节点(用包内稳定的目录名标识, 不泄露本机行 id)."""
    return {
        "id": _node_key(node),
        # 该节点引用的内容目录名(相对 ``timeline/``): 内容相同的节点指向同一份.
        "content": content,
        "parent_id": None if node.parent_id is None else keys.get(node.parent_id),
        "kind": node.node_kind,
        "title": node.title,
        "note": node.note,
        "branch_name": node.branch_name,
        "is_safety": node.is_safety,
        "created_at": None if node.created_at is None else node.created_at.isoformat(),
        "content_hash": node.content_hash,
        # 这份备份当初是按哪套校验数据记的(名称模式下逐文件哈希可能还空着).
        "verify_mode": node.verify_mode,
        "current": current,
    }


def _node_key(node: BackupNode) -> str:
    """包内节点标识: 直接用快照目录名(时间戳 + 短哈希, 天然唯一)."""
    relative = node.storage_relpath
    if not relative:  # pragma: no cover - snapshot_root 已先拒绝缺存储路径的节点
        raise ArchiveManagementError(f"备份节点缺少存储路径, 无法导出: #{node.id}")
    return Path(relative).name


def _dedupe_key(node: BackupNode) -> str | None:
    """返回可安全用于共享内容的去重键; 拿不出可靠键时返回 ``None``.

    只有 ``verify_mode="sha256"`` 且记了整体内容哈希的节点才参与去重: 那份哈希
    覆盖逐文件的路径/大小/哈希/链接目标, 相等即内容相同。名称模式下清单里没有
    逐文件哈希, "路径 + 大小"相同的两份存档字节未必相同 —— 这类节点各存一份,
    宁可多占空间也不冒认错内容的险.
    """
    if node.verify_mode == "sha256" and node.content_hash:
        return node.content_hash
    return None


def _content_plan(
    nodes: Sequence[BackupNode],
) -> tuple[dict[int | None, str], list[BackupNode]]:
    """决定每个节点引用哪份内容, 返回(节点 id -> 内容目录名, 需要落盘的节点).

    相同去重键的节点共用**第一个**节点的目录: 包里只装一份, 其余节点在 config 里
    用 ``content`` 指过去。去重只省包的体积, 不改变节点本身 —— 每个节点在配置里
    仍是独立一条, 各自的时间线位置与分支归属原样保留.
    """
    content_of: dict[int | None, str] = {}
    owners: list[BackupNode] = []
    by_hash: dict[str, str] = {}
    for node in nodes:
        key = _node_key(node)
        dedupe = _dedupe_key(node)
        shared = by_hash.get(dedupe) if dedupe else None
        if shared is not None:
            content_of[node.id] = shared
            continue
        if dedupe:
            by_hash[dedupe] = key
        content_of[node.id] = key
        owners.append(node)
    return content_of, owners


def _staging_dir(destination: Path) -> Path:
    """目标同级的一次性暂存目录(各内层包先落在这里, 整批结束后删除)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = destination.parent / f"{_BATCH_STAGING_PREFIX}{uuid4().hex[:8]}"
    staging.mkdir(parents=True, exist_ok=True)
    return staging


def _unique_entry(slug: str, used: set[str]) -> str:
    """返回不与已用条目名重复的内层包名(撞车时追加 -2/-3 序号)."""
    candidate = archive_entry_name(slug)
    index = 2
    while candidate.casefold() in used:
        candidate = archive_entry_name(f"{slug}-{index}")
        index += 1
    used.add(candidate.casefold())
    return candidate


def _check_cancelled(cancelled: CancelProbe | None) -> None:
    """取消探针为真时立刻中止(整批都不写)."""
    if cancelled is not None and cancelled():
        raise OperationCancelledError("导出已取消")


__all__ = ["BatchExportResult", "ExportResult", "ExportService"]
