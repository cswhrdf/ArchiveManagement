"""基于 SQLite 的真实后端(阶段 C).

把 :mod:`archive_management.domain` 实体与仓库映射为 UI 展示模型,
实现 :class:`~archive_management.ui.backend.ArchiveService`. 本后端是
GUI 的默认数据来源; 备份/恢复等文件操作在阶段 D/E 接入, 当前抛出
明确的阶段提示而非静默忽略.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from archive_management.domain import Game, PathKind, SaveLocation
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.pathcheck import normalize_path, probe_path
from archive_management.ui.models import (
    BackupItem,
    GameDetail,
    GameSummary,
    LocationItem,
    TaskStatus,
)

_TONES = ("orange", "blue", "green")
_DEFAULT_THEME = "dark"


def _tone(name: str) -> str:
    """按名称确定性选择一个头像色调."""
    digest = sum(name.encode("utf-8"))
    return _TONES[digest % len(_TONES)]


class SqlArchiveService:
    """基于 :class:`Database` 的真实 :class:`ArchiveService` 实现."""

    def __init__(
        self,
        database: Database,
        *,
        backup_root: Path,
    ) -> None:
        """绑定数据库与备份根目录, 供展示与校验使用."""
        self._database = database
        self._backup_root = backup_root
        self._games = GameRepository(database)
        self._locations = SaveLocationRepository(database)
        self._theme = _DEFAULT_THEME

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
        backups = self._games.count_backups(gid)
        return GameDetail(
            name=game.name,
            subtitle=tr("detail.subtitle"),
            main_location=main,
            location_verified=verified,
            location_note=note,
            last_backup_label="—",
            last_backup_sub="",
            total_backups_label=(
                tr("detail.backups_none")
                if backups == 0
                else tr("detail.backups_count", count=backups)
            ),
            total_backups_sub="",
            next_backup_label="—",
        )

    def add_game(self, name: str) -> GameSummary:
        """新增游戏, 返回其摘要."""
        clean = self._clean_name(name)
        game = self._games.add(Game(name=clean))
        return self._summary(game)

    def update_game(self, game_id: str, name: str) -> GameSummary:
        """重命名游戏, 返回其摘要."""
        clean = self._clean_name(name)
        game = self._game(game_id)
        updated = Game(
            id=game.id,
            name=clean,
            steam_app_id=game.steam_app_id,
            platform=game.platform,
            enabled=game.enabled,
            created_at=game.created_at,
        )
        self._games.update(updated)
        return self._summary(updated)

    def delete_game(self, game_id: str) -> None:
        """删除游戏记录及其存档位置."""
        _game, gid = self._game_ref(game_id)
        self._games.delete(gid)

    def set_game_enabled(self, game_id: str, enabled: bool) -> GameSummary:
        """启用或停用一个游戏."""
        game = self._game(game_id)
        updated = Game(
            id=game.id,
            name=game.name,
            steam_app_id=game.steam_app_id,
            platform=game.platform,
            enabled=enabled,
            created_at=game.created_at,
        )
        self._games.update(updated)
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
        return self._location_item(changed)

    def remove_location(self, location_id: str) -> None:
        """删除指定存档位置记录."""
        _location, lid = self._location_ref(location_id)
        self._locations.delete(lid)

    def set_primary_location(self, game_id: str, location_id: str) -> LocationItem:
        """把某位置设为主位置并返回更新后的条目."""
        _game, gid = self._game_ref(game_id)
        location, lid = self._location_ref(location_id)
        if location.game_id != gid:
            raise ArchiveManagementError(
                tr("error.unknown_location", location_id=location_id)
            )
        self._locations.make_primary(gid, lid)
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
        return self._location_item(changed)

    # -- 只读/主题(阶段 D 前占位) ----------------------------------------

    def list_backups(self, game_id: str) -> list[BackupItem]:
        """返回备份节点(阶段 D 接入真实快照前为空)."""
        self._game(game_id)
        return []

    def task_status(self) -> TaskStatus:
        """返回运行环境与任务状态."""
        return TaskStatus(
            running=False,
            task_name=tr("task.unscheduled"),
            progress=0.0,
            next_run_label="—",
            target_label=str(self._backup_root),
            shortcut_label="—",
            theme_name=self._theme,
            backend_ok=True,
        )

    def current_theme(self) -> str:
        """返回当前主题名."""
        return self._theme

    def set_theme(self, name: str) -> str:
        """切换主题 (dark/light), 返回生效主题."""
        self._theme = "dark" if name == "dark" else "light"
        return self._theme

    def run_backup_now(self, game_id: str) -> str:
        """立即备份(阶段 D 接入); 当前给出明确提示."""
        self._game(game_id)
        raise ArchiveManagementError(tr("error.not_in_this_phase", phase="D"))

    def run_restore(self, game_id: str, backup_id: str) -> str:
        """恢复备份(阶段 D/E 接入)."""
        self._game(game_id)
        raise ArchiveManagementError(tr("error.not_in_this_phase", phase="E"))

    def run_create_branch(self, game_id: str, backup_id: str, branch_name: str) -> str:
        """创建分支(阶段 D 接入)."""
        self._game(game_id)
        raise ArchiveManagementError(tr("error.not_in_this_phase", phase="D"))

    def run_export(self, game_id: str) -> str:
        """导出游戏(阶段 G 接入)."""
        self._game(game_id)
        raise ArchiveManagementError(tr("error.not_in_this_phase", phase="G"))

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
