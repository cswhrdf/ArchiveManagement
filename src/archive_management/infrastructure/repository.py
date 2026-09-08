"""游戏与存档位置的 SQLite 数据访问层.

Repository 把 :mod:`archive_management.domain` 中的领域实体映射为
SQLite 行, 供应用用例与后端调用. 所有写操作都封装在
:meth:`Database.session` 事务中, 任一语句失败即整体回滚.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from archive_management.domain import Game, SaveLocation
from archive_management.exceptions import DatabaseError
from archive_management.infrastructure.database import Database, iso_utc_now


def _as_bool(value: object) -> bool:
    """把 SQLite 的 0/1 整数列转换为布尔值."""
    return bool(value)


def _parse_dt(value: str | None) -> datetime | None:
    """把数据库中的 UTC ISO 文本解析为感知时区的 datetime."""
    if value is None:
        return None
    return datetime.fromisoformat(value).astimezone(UTC)


def _dt_text(value: datetime | None) -> str | None:
    """把 datetime 序列化为秒级 UTC ISO 文本; None 原样返回."""
    if value is None:
        return None
    return value.astimezone(UTC).isoformat(timespec="seconds")


def _row_to_game(row: sqlite3.Row) -> Game:
    return Game(
        id=int(row["id"]),
        name=str(row["name"]),
        steam_app_id=None if row["steam_app_id"] is None else int(row["steam_app_id"]),
        platform=str(row["platform"]),
        enabled=_as_bool(row["enabled"]),
        created_at=_parse_dt(row["created_at"]),
    )


def _row_to_location(row: sqlite3.Row) -> SaveLocation:
    return SaveLocation(
        id=int(row["id"]),
        game_id=int(row["game_id"]),
        path=str(row["path"]),
        path_kind=str(row["path_kind"]),  # type: ignore[arg-type]
        source=str(row["source"]),  # type: ignore[arg-type]
        is_primary=_as_bool(row["is_primary"]),
        last_checked_at=_parse_dt(row["last_checked_at"]),
        last_check_status=row["last_check_status"],
    )


class GameRepository:
    """games 表的行级访问."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def list(self) -> list[Game]:
        """返回全部游戏, 按创建时间升序."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, name, steam_app_id, platform, enabled, created_at"
                " FROM games ORDER BY created_at, id"
            ).fetchall()
        return [_row_to_game(row) for row in rows]

    def get(self, game_id: int) -> Game | None:
        """按 id 返回单个游戏; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, steam_app_id, platform, enabled, created_at"
                " FROM games WHERE id = ?",
                (game_id,),
            ).fetchone()
        return _row_to_game(row) if row is not None else None

    def add(self, game: Game) -> Game:
        """插入一条游戏记录, 返回带 id 与创建时间的实体."""
        created_at = _dt_text(game.created_at) or iso_utc_now()
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO games (name, steam_app_id, platform, enabled, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    game.name,
                    game.steam_app_id,
                    game.platform,
                    int(game.enabled),
                    created_at,
                ),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("插入游戏失败: 未返回行 id")
            game_id = int(cursor.lastrowid)
        return Game(
            id=game_id,
            name=game.name,
            steam_app_id=game.steam_app_id,
            platform=game.platform,
            enabled=game.enabled,
            created_at=_parse_dt(created_at),
        )

    def update(self, game: Game) -> Game:
        """按 id 更新可变字段; 缺少 id 时抛错."""
        if game.id is None:
            raise ValueError("更新游戏需要 id")
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET name = ?, steam_app_id = ?, platform = ?,"
                " enabled = ? WHERE id = ?",
                (
                    game.name,
                    game.steam_app_id,
                    game.platform,
                    int(game.enabled),
                    game.id,
                ),
            )
        return game

    def delete(self, game_id: int) -> None:
        """删除游戏记录; 存档位置与备份节点随外键级联删除."""
        with self._database.session() as connection:
            connection.execute("DELETE FROM games WHERE id = ?", (game_id,))

    def count_locations(self, game_id: int) -> int:
        """返回某游戏配置的存档位置数量."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM save_locations WHERE game_id = ?", (game_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def count_backups(self, game_id: int) -> int:
        """返回某游戏的备份节点数量(阶段 D 起才有真实记录)."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM backup_nodes WHERE game_id = ?", (game_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0


class SaveLocationRepository:
    """save_locations 表的行级访问."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def list_for_game(self, game_id: int) -> list[SaveLocation]:
        """返回某游戏的全部存档位置, 主位置优先."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, game_id, path, path_kind, source, is_primary,"
                " last_checked_at, last_check_status FROM save_locations"
                " WHERE game_id = ? ORDER BY is_primary DESC, id",
                (game_id,),
            ).fetchall()
        return [_row_to_location(row) for row in rows]

    def get(self, location_id: int) -> SaveLocation | None:
        """按 id 返回单个存档位置; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, path, path_kind, source, is_primary,"
                " last_checked_at, last_check_status FROM save_locations"
                " WHERE id = ?",
                (location_id,),
            ).fetchone()
        return _row_to_location(row) if row is not None else None

    def add(self, location: SaveLocation) -> SaveLocation:
        """插入一条存档位置记录, 返回带 id 的实体."""
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO save_locations (game_id, path, path_kind, source,"
                " is_primary, last_checked_at, last_check_status)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    location.game_id,
                    location.path,
                    location.path_kind,
                    location.source,
                    int(location.is_primary),
                    _dt_text(location.last_checked_at),
                    location.last_check_status,
                ),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("插入存档位置失败: 未返回行 id")
            location_id = int(cursor.lastrowid)
        return SaveLocation(
            id=location_id,
            game_id=location.game_id,
            path=location.path,
            path_kind=location.path_kind,
            source=location.source,
            is_primary=location.is_primary,
            last_checked_at=location.last_checked_at,
            last_check_status=location.last_check_status,
        )

    def update(self, location: SaveLocation) -> SaveLocation:
        """按 id 更新可变字段; 缺少 id 时抛错."""
        if location.id is None:
            raise ValueError("更新存档位置需要 id")
        with self._database.session() as connection:
            connection.execute(
                "UPDATE save_locations SET path = ?, path_kind = ?, source = ?,"
                " is_primary = ?, last_checked_at = ?, last_check_status = ?"
                " WHERE id = ?",
                (
                    location.path,
                    location.path_kind,
                    location.source,
                    int(location.is_primary),
                    _dt_text(location.last_checked_at),
                    location.last_check_status,
                    location.id,
                ),
            )
        return location

    def delete(self, location_id: int) -> None:
        """删除指定存档位置记录."""
        with self._database.session() as connection:
            connection.execute(
                "DELETE FROM save_locations WHERE id = ?", (location_id,)
            )

    def make_primary(self, game_id: int, location_id: int) -> None:
        """把某位置设为主位置, 并清除该游戏其余位置的主位置标记."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE save_locations SET is_primary = 0"
                " WHERE game_id = ? AND is_primary = 1",
                (game_id,),
            )
            connection.execute(
                "UPDATE save_locations SET is_primary = 1"
                " WHERE game_id = ? AND id = ?",
                (game_id, location_id),
            )

    def duplicate_of(self, game_id: int, path: str) -> SaveLocation | None:
        """返回同游戏内与给定路径(大小写不敏感)重复的存档位置."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, path, path_kind, source, is_primary,"
                " last_checked_at, last_check_status FROM save_locations"
                " WHERE game_id = ? AND LOWER(path) = LOWER(?)",
                (game_id, path),
            ).fetchone()
        return _row_to_location(row) if row is not None else None
