"""游戏与存档位置的 SQLite 数据访问层.

Repository 把 :mod:`archive_management.domain` 中的领域实体映射为
SQLite 行, 供应用用例与后端调用. 所有写操作都封装在
:meth:`Database.session` 事务中, 任一语句失败即整体回滚.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from archive_management.domain import (
    ACTIVATION_STATE_VERSION,
    HOME_STATE_VERSION,
    TAG_SEPARATORS,
    ActivationState,
    BackupFileEntry,
    BackupNode,
    CandidateStatus,
    Game,
    GameCandidate,
    HomeFilter,
    HomeLayout,
    HomeView,
    MonitoredDirectory,
    PathHealth,
    RunEntry,
    SaveCandidate,
    SaveCandidateStatus,
    SaveLocation,
    ScheduledJob,
)
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


def _split_tags(raw: object) -> tuple[str, ...]:
    """把数据库中逗号拼接的标签文本还原为元组(中英文逗号都算分隔符)."""
    text = str(raw or "")
    for separator in TAG_SEPARATORS[1:]:
        text = text.replace(separator, TAG_SEPARATORS[0])
    parts = (part.strip() for part in text.split(TAG_SEPARATORS[0]))
    return tuple(tag for tag in parts if tag)


def _optional_int(value: object) -> int | None:
    """把可空的外键列读成 int; 为空时返回 ``None``."""
    return None if value is None else int(str(value))


def _join_tags(tags: Sequence[str]) -> str:
    """把标签元组拼成数据库存储形式."""
    return ",".join(tag.strip() for tag in tags if tag.strip())


def _row_to_game(row: sqlite3.Row) -> Game:
    name = str(row["name"])
    return Game(
        id=int(row["id"]),
        name=name,
        steam_app_id=None if row["steam_app_id"] is None else int(row["steam_app_id"]),
        platform=str(row["platform"]),
        enabled=_as_bool(row["enabled"]),
        created_at=_parse_dt(row["created_at"]),
        original_name=str(row["original_name"] or "") or name,
        localized_name=str(row["localized_name"] or ""),
        storage_key=str(row["storage_key"] or ""),
        origin=str(row["origin"] or "manual"),
        tags=_split_tags(row["tags"]),
        archived=_as_bool(row["archived"]),
        last_activity_at=_parse_dt(row["last_activity_at"]),
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


def _row_to_backup_node(row: sqlite3.Row) -> BackupNode:
    return BackupNode(
        id=int(row["id"]),
        game_id=int(row["game_id"]),
        parent_id=None if row["parent_id"] is None else int(row["parent_id"]),
        node_kind=str(row["node_kind"]),  # type: ignore[arg-type]
        branch_name=row["branch_name"],
        note=str(row["note"] or ""),
        title=str(row["title"] or ""),
        content_hash=row["content_hash"],
        storage_relpath=row["storage_relpath"],
        created_at=_parse_dt(row["created_at"]),
        is_safety=_as_bool(row["is_safety"]),
    )


def _row_to_backup_file(row: sqlite3.Row) -> BackupFileEntry:
    return BackupFileEntry(
        id=int(row["id"]),
        backup_id=int(row["backup_id"]),
        relative_path=str(row["relative_path"]),
        size=int(row["size"]),
        sha256=str(row["sha256"]),
        file_kind=str(row["file_kind"]),  # type: ignore[arg-type]
    )


def _row_to_job(row: sqlite3.Row) -> ScheduledJob:
    return ScheduledJob(
        id=int(row["id"]),
        game_id=None if row["game_id"] is None else int(row["game_id"]),
        schedule=str(row["schedule"]),
        enabled=_as_bool(row["enabled"]),
        last_run_at=_parse_dt(row["last_run_at"]),
        next_run_at=_parse_dt(row["next_run_at"]),
        last_error=row["last_error"],
        keep_auto=max(1, int(row["keep_auto"])),
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
                "SELECT id, name, steam_app_id, platform, enabled, created_at,"
                " original_name, localized_name, storage_key, origin, tags, archived,"
                " last_activity_at FROM games ORDER BY created_at, id"
            ).fetchall()
        return [_row_to_game(row) for row in rows]

    def get(self, game_id: int) -> Game | None:
        """按 id 返回单个游戏; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, steam_app_id, platform, enabled, created_at,"
                " original_name, localized_name, storage_key, origin, tags, archived,"
                " last_activity_at FROM games WHERE id = ?",
                (game_id,),
            ).fetchone()
        return _row_to_game(row) if row is not None else None

    def add(self, game: Game) -> Game:
        """插入一条游戏记录, 返回带 id 与创建时间的实体."""
        created_at = _dt_text(game.created_at) or iso_utc_now()
        original_name = game.original_name or game.name
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO games (name, steam_app_id, platform, enabled, created_at,"
                " original_name, localized_name, storage_key, origin, tags, archived,"
                " last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    game.name,
                    game.steam_app_id,
                    game.platform,
                    int(game.enabled),
                    created_at,
                    original_name,
                    game.localized_name,
                    game.storage_key,
                    game.origin,
                    _join_tags(game.tags),
                    int(game.archived),
                    _dt_text(game.last_activity_at) or created_at,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - 插入必然返回行 id
                raise DatabaseError("插入游戏失败: 未返回行 id")
            game_id = int(cursor.lastrowid)
        return Game(
            id=game_id,
            name=game.name,
            steam_app_id=game.steam_app_id,
            platform=game.platform,
            enabled=game.enabled,
            created_at=_parse_dt(created_at),
            original_name=original_name,
            localized_name=game.localized_name,
            storage_key=game.storage_key,
            origin=game.origin,
            tags=game.tags,
            archived=game.archived,
            last_activity_at=_parse_dt(_dt_text(game.last_activity_at) or created_at),
        )

    def update(self, game: Game) -> Game:
        """按 id 更新可变字段; 缺少 id 时抛错.

        只更新名称/Steam/平台/启用状态/译名记录: ``original_name`` 与
        ``storage_key`` 记录的是"首次录入的名称"与"磁盘上实际使用的目录",
        重命名不得改写; ``localized_name`` 跟着一起写, 让"名字是谁写的"能跟着
        名字一起变(用户改名时调用方负责清空它)。
        """
        if game.id is None:
            raise ValueError("更新游戏需要 id")
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET name = ?, steam_app_id = ?, platform = ?,"
                " enabled = ?, localized_name = ? WHERE id = ?",
                (
                    game.name,
                    game.steam_app_id,
                    game.platform,
                    int(game.enabled),
                    game.localized_name,
                    game.id,
                ),
            )
        return game

    def ensure_storage_key(self, game_id: int, key: str) -> str:
        """写入并返回该游戏在备份根下的目录名(已有取值时不再改写).

        目录名只在第一次备份时确定: 之后改名或增删存档位置都不会搬动已有
        备份, 因此这里用"仅在为空时写入"的方式保证稳定性。
        """
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET storage_key = ? WHERE id = ?"
                " AND (storage_key IS NULL OR storage_key = '')",
                (key, game_id),
            )
            row = connection.execute(
                "SELECT storage_key FROM games WHERE id = ?", (game_id,)
            ).fetchone()
        if row is None:
            raise DatabaseError(f"未知游戏: {game_id}")
        return str(row["storage_key"] or "") or key

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
        """返回某游戏的备份节点数量."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM backup_nodes WHERE game_id = ?", (game_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def current_backup(self, game_id: int) -> int | None:
        """返回某游戏"当前节点"的 id; 未设置或节点已删除时返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT current_backup_id FROM games WHERE id = ?", (game_id,)
            ).fetchone()
        if row is None or row["current_backup_id"] is None:
            return None
        return int(row["current_backup_id"])

    def set_current_backup(self, game_id: int, backup_id: int | None) -> None:
        """设置或清除某游戏的"当前节点"指针."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET current_backup_id = ? WHERE id = ?",
                (backup_id, game_id),
            )

    def clear_current_backup(self, backup_id: int) -> None:
        """清除引用了该备份的"当前节点"指针(删除备份前调用)."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET current_backup_id = NULL WHERE current_backup_id = ?",
                (backup_id,),
            )

    def set_archived(self, game_id: int, archived: bool) -> None:
        """设置或取消归档(归档只是从主页收起来, 不删除任何数据)."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET archived = ?, last_activity_at = ? WHERE id = ?",
                (int(archived), iso_utc_now(), game_id),
            )

    def set_enabled(self, game_id: int, enabled: bool) -> tuple[int, ...]:
        """启用/停用一款游戏, 返回被自动停用的其它游戏 id.

        同一时刻只允许一款游戏启用, 因此"启用"必须在**同一个事务**里先停用其它
        游戏再启用目标 —— 分两次写会在中途失败时留下两个启用态。
        返回元组而不是列表: 类里已有名为 ``list`` 的方法, 注解里的 ``list[...]``
        会被 mypy 当成那个方法(method 遮蔽内置名)。
        """
        replaced: tuple[int, ...] = ()
        with self._database.session() as connection:
            if enabled:
                rows = connection.execute(
                    "SELECT id FROM games WHERE enabled = 1 AND id != ?", (game_id,)
                ).fetchall()
                replaced = tuple(int(row["id"]) for row in rows)
                connection.execute(
                    "UPDATE games SET enabled = 0 WHERE enabled = 1 AND id != ?",
                    (game_id,),
                )
            connection.execute(
                "UPDATE games SET enabled = ? WHERE id = ?", (int(enabled), game_id)
            )
        return replaced

    def enabled_game_id(self) -> int | None:
        """返回当前启用的游戏 id; 没有启用任何游戏时返回 None.

        按定义至多一个, 这里仍然加 ``ORDER BY`` + ``LIMIT``: 旧库或手工改库可能
        留下多个启用态, 界面需要一个确定的答案。
        """
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id FROM games WHERE enabled = 1 ORDER BY created_at, id LIMIT 1"
            ).fetchone()
        return None if row is None else int(row["id"])

    def set_tags(self, game_id: int, tags: Sequence[str]) -> None:
        """覆盖写入游戏的自定义标签(调用方负责清理与限额)."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET tags = ?, last_activity_at = ? WHERE id = ?",
                (_join_tags(tags), iso_utc_now(), game_id),
            )

    def set_origin(self, game_id: int, origin: str) -> None:
        """记录游戏的来源平台(导入探测结果或手动录入时使用)."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET origin = ? WHERE id = ?", (origin, game_id)
            )

    def touch_activity(self, game_id: int, *, when: datetime | None = None) -> None:
        """记录一次与该游戏相关的动作, 供主页的"最近活跃"排序使用."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE games SET last_activity_at = ? WHERE id = ?",
                (_dt_text(when) or iso_utc_now(), game_id),
            )


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

    def game_ids_with_locations(self) -> set[int]:
        """返回至少关联了一个存档位置的游戏 id(自动启停的监控范围).

        没有存档位置的游戏是"待处理"状态: 它没有可备份的内容, 接管它既不能定时
        备份也没有意义。这里用一条聚合查询一次取完, 与启停轮询的开销无关。
        """
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT game_id FROM save_locations"
            ).fetchall()
        return {int(row["game_id"]) for row in rows}

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
            if cursor.lastrowid is None:  # pragma: no cover - 插入必然返回行 id
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
                "UPDATE save_locations SET is_primary = 1 WHERE game_id = ? AND id = ?",
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


class BackupRepository:
    """backup_nodes / backup_files 表的行级访问."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def add(self, node: BackupNode) -> BackupNode:
        """插入一个备份节点, 返回带 id 的实体."""
        created_at = _dt_text(node.created_at) or iso_utc_now()
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO backup_nodes (game_id, parent_id, node_kind,"
                " branch_name, note, content_hash, storage_relpath, created_at,"
                " is_safety) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node.game_id,
                    node.parent_id,
                    node.node_kind,
                    node.branch_name,
                    node.note,
                    node.content_hash,
                    node.storage_relpath,
                    created_at,
                    int(node.is_safety),
                ),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("插入备份节点失败: 未返回行 id")
            node_id = int(cursor.lastrowid)
        return BackupNode(
            id=node_id,
            game_id=node.game_id,
            parent_id=node.parent_id,
            node_kind=node.node_kind,
            branch_name=node.branch_name,
            note=node.note,
            content_hash=node.content_hash,
            storage_relpath=node.storage_relpath,
            created_at=_parse_dt(created_at),
            is_safety=node.is_safety,
        )

    def get(self, backup_id: int) -> BackupNode | None:
        """按 id 返回单个备份节点; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, parent_id, node_kind, branch_name, note, title,"
                " content_hash, storage_relpath, created_at, is_safety"
                " FROM backup_nodes WHERE id = ?",
                (backup_id,),
            ).fetchone()
        return _row_to_backup_node(row) if row is not None else None

    def list_for_game(self, game_id: int) -> list[BackupNode]:
        """返回某游戏的全部备份节点, 按创建时间升序(便于建树)."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, game_id, parent_id, node_kind, branch_name, note, title,"
                " content_hash, storage_relpath, created_at, is_safety"
                " FROM backup_nodes WHERE game_id = ? ORDER BY created_at, id",
                (game_id,),
            ).fetchall()
        return [_row_to_backup_node(row) for row in rows]

    def latest_for_game(self, game_id: int) -> BackupNode | None:
        """返回某游戏最新的备份节点(即分支树的当前末端)."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, parent_id, node_kind, branch_name, note, title,"
                " content_hash, storage_relpath, created_at, is_safety"
                " FROM backup_nodes WHERE game_id = ?"
                " ORDER BY created_at DESC, id DESC LIMIT 1",
                (game_id,),
            ).fetchone()
        return _row_to_backup_node(row) if row is not None else None

    def delete(self, backup_id: int) -> None:
        """删除备份节点; 其文件清单随外键级联删除."""
        with self._database.session() as connection:
            connection.execute("DELETE FROM backup_nodes WHERE id = ?", (backup_id,))

    def delete_many(self, backup_ids: Sequence[int]) -> int:
        """在一个事务中删除多个备份节点, 返回删除条数."""
        if not backup_ids:
            return 0
        with self._database.session() as connection:
            connection.executemany(
                "DELETE FROM backup_nodes WHERE id = ?",
                [(backup_id,) for backup_id in backup_ids],
            )
        return len(backup_ids)

    def update_meta(
        self, backup_id: int, *, title: str, note: str
    ) -> BackupNode | None:
        """更新备份的名称与描述, 返回更新后的节点."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE backup_nodes SET title = ?, note = ? WHERE id = ?",
                (title, note, backup_id),
            )
        return self.get(backup_id)

    def reparent(self, backup_id: int, parent_id: int | None) -> None:
        """把节点重新挂到另一个父节点下(删除同线路节点时的"上移")."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE backup_nodes SET parent_id = ? WHERE id = ?",
                (parent_id, backup_id),
            )

    def add_with_files(
        self,
        node: BackupNode,
        entries: Sequence[BackupFileEntry],
    ) -> BackupNode:
        """在同一事务中写入备份节点与文件清单.

        备份节点与清单必须同时存在才是有效备份, 因此两写操作共用一个
        事务: 任一失败则整体回滚, 不会留下只有节点或只有清单的半成品.
        """
        created_at = _dt_text(node.created_at) or iso_utc_now()
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO backup_nodes (game_id, parent_id, node_kind,"
                " branch_name, note, title, content_hash, storage_relpath,"
                " created_at, is_safety) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node.game_id,
                    node.parent_id,
                    node.node_kind,
                    node.branch_name,
                    node.note,
                    node.title,
                    node.content_hash,
                    node.storage_relpath,
                    created_at,
                    int(node.is_safety),
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - 插入必然返回行 id
                raise DatabaseError("插入备份节点失败: 未返回行 id")
            backup_id = int(cursor.lastrowid)
            if entries:
                connection.executemany(
                    "INSERT INTO backup_files (backup_id, relative_path, size,"
                    " sha256, file_kind) VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            backup_id,
                            entry.relative_path,
                            entry.size,
                            entry.sha256,
                            entry.file_kind,
                        )
                        for entry in entries
                    ],
                )
        return BackupNode(
            id=backup_id,
            game_id=node.game_id,
            parent_id=node.parent_id,
            node_kind=node.node_kind,
            branch_name=node.branch_name,
            note=node.note,
            title=node.title,
            content_hash=node.content_hash,
            storage_relpath=node.storage_relpath,
            created_at=_parse_dt(created_at),
            is_safety=node.is_safety,
        )

    def add_files(self, backup_id: int, entries: Sequence[BackupFileEntry]) -> int:
        """批量写入备份文件清单, 返回写入条数."""
        if not entries:
            return 0
        with self._database.session() as connection:
            connection.executemany(
                "INSERT INTO backup_files (backup_id, relative_path, size, sha256,"
                " file_kind) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        backup_id,
                        entry.relative_path,
                        entry.size,
                        entry.sha256,
                        entry.file_kind,
                    )
                    for entry in entries
                ],
            )
        return len(entries)

    def list_files(self, backup_id: int) -> list[BackupFileEntry]:
        """返回某个备份的文件清单, 按相对路径排序."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, backup_id, relative_path, size, sha256, file_kind"
                " FROM backup_files WHERE backup_id = ? ORDER BY relative_path",
                (backup_id,),
            ).fetchall()
        return [_row_to_backup_file(row) for row in rows]

    def describe(self, backup_id: int) -> tuple[int, int]:
        """返回 ``(文件数, 总字节数)``; 只统计真实文件."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM backup_files"
                " WHERE backup_id = ? AND file_kind = 'file'",
                (backup_id,),
            ).fetchone()
        if row is None:
            return (0, 0)
        return (int(row[0]), int(row[1]))

    def count(self, game_id: int) -> int:
        """返回某游戏的备份节点数量."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM backup_nodes WHERE game_id = ?", (game_id,)
            ).fetchone()
        return int(row[0]) if row is not None else 0


class ScheduledJobRepository:
    """scheduled_jobs 表的行级访问(定期备份任务)."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def upsert(self, job: ScheduledJob) -> ScheduledJob:
        """插入或更新任务配置, 返回带 id 的实体."""
        if job.id is None:
            with self._database.session() as connection:
                cursor = connection.execute(
                    "INSERT INTO scheduled_jobs (game_id, schedule, enabled,"
                    " last_run_at, next_run_at, last_error, keep_auto)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        job.game_id,
                        job.schedule,
                        int(job.enabled),
                        _dt_text(job.last_run_at),
                        _dt_text(job.next_run_at),
                        job.last_error,
                        job.keep_auto,
                    ),
                )
                if cursor.lastrowid is None:  # pragma: no cover - 插入必然返回行 id
                    raise DatabaseError("插入定时任务失败: 未返回行 id")
                job_id = int(cursor.lastrowid)
        else:
            with self._database.session() as connection:
                connection.execute(
                    "UPDATE scheduled_jobs SET game_id = ?, schedule = ?,"
                    " enabled = ?, last_run_at = ?, next_run_at = ?, last_error = ?,"
                    " keep_auto = ? WHERE id = ?",
                    (
                        job.game_id,
                        job.schedule,
                        int(job.enabled),
                        _dt_text(job.last_run_at),
                        _dt_text(job.next_run_at),
                        job.last_error,
                        job.keep_auto,
                        job.id,
                    ),
                )
            job_id = job.id
        return ScheduledJob(
            id=job_id,
            game_id=job.game_id,
            schedule=job.schedule,
            enabled=job.enabled,
            last_run_at=job.last_run_at,
            next_run_at=job.next_run_at,
            last_error=job.last_error,
            keep_auto=job.keep_auto,
        )

    def list_all(self) -> list[ScheduledJob]:
        """返回全部定时任务, 按 id 排序."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, game_id, schedule, enabled, last_run_at, next_run_at,"
                " last_error, keep_auto, created_at FROM scheduled_jobs ORDER BY id"
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def get(self, job_id: int) -> ScheduledJob | None:
        """按 id 返回任务; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, schedule, enabled, last_run_at, next_run_at,"
                " last_error, keep_auto, created_at FROM scheduled_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
        return _row_to_job(row) if row is not None else None

    def for_game(self, game_id: int) -> list[ScheduledJob]:
        """返回某游戏的定时任务."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, game_id, schedule, enabled, last_run_at, next_run_at,"
                " last_error, keep_auto, created_at FROM scheduled_jobs"
                " WHERE game_id = ? ORDER BY id",
                (game_id,),
            ).fetchall()
        return [_row_to_job(row) for row in rows]

    def delete(self, job_id: int) -> None:
        """删除一个定时任务."""
        with self._database.session() as connection:
            connection.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))

    def mark_run(
        self,
        job_id: int,
        *,
        ran_at: datetime,
        next_run_at: datetime | None,
        error: str | None = None,
    ) -> None:
        """记录一次执行结果(成功清空错误, 失败写入摘要)."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE scheduled_jobs SET last_run_at = ?, next_run_at = ?,"
                " last_error = ? WHERE id = ?",
                (_dt_text(ran_at), _dt_text(next_run_at), error, job_id),
            )


def _row_to_monitored(row: sqlite3.Row) -> MonitoredDirectory:
    return MonitoredDirectory(
        id=int(row["id"]),
        path=str(row["path"]),
        enabled=_as_bool(row["enabled"]),
        note=str(row["note"] or ""),
        created_at=_parse_dt(row["created_at"]),
        last_scan_at=_parse_dt(row["last_scan_at"]),
        last_scan_status=row["last_scan_status"],
    )


def _row_to_candidate(row: sqlite3.Row) -> GameCandidate:
    return GameCandidate(
        id=int(row["id"]),
        name=str(row["name"]),
        install_dir=str(row["install_dir"]),
        source=str(row["source"]),  # type: ignore[arg-type]
        confidence=str(row["confidence"]),  # type: ignore[arg-type]
        reason_code=str(row["reason_code"] or ""),
        detail=str(row["detail"] or ""),
        found_at=_parse_dt(row["found_at"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        health=str(row["health"]),  # type: ignore[arg-type]
        game_id=None if row["game_id"] is None else int(row["game_id"]),
    )


def _row_to_save_candidate(row: sqlite3.Row) -> SaveCandidate:
    return SaveCandidate(
        id=int(row["id"]),
        game_id=int(row["game_id"]),
        platform=str(row["platform"] or ""),
        platform_game_id=str(row["platform_game_id"] or ""),
        path=str(row["path"]),
        path_kind=str(row["path_kind"]),  # type: ignore[arg-type]
        reason_code=str(row["reason_code"] or ""),
        detail=str(row["detail"] or ""),
        relative_path=str(row["relative_path"] or ""),
        confidence=str(row["confidence"]),  # type: ignore[arg-type]
        health=str(row["health"]),  # type: ignore[arg-type]
        risk_reason=str(row["risk_reason"] or ""),
        status=str(row["status"]),  # type: ignore[arg-type]
        found_at=_parse_dt(row["found_at"]),
        decided_at=_parse_dt(row["decided_at"]),
    )


class MonitoredDirectoryRepository:
    """monitored_directories 表的行级访问."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def list_all(self) -> list[MonitoredDirectory]:
        """返回全部监控目录, 按路径排序(方法名不用 ``list`` 以免屏蔽内置类型)."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, path, enabled, note, created_at, last_scan_at,"
                " last_scan_status FROM monitored_directories ORDER BY path"
            ).fetchall()
        return [_row_to_monitored(row) for row in rows]

    def get(self, directory_id: int) -> MonitoredDirectory | None:
        """按 id 返回监控目录; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, path, enabled, note, created_at, last_scan_at,"
                " last_scan_status FROM monitored_directories WHERE id = ?",
                (directory_id,),
            ).fetchone()
        return _row_to_monitored(row) if row is not None else None

    def add(self, directory: MonitoredDirectory) -> MonitoredDirectory:
        """插入一条监控目录记录, 返回带 id 与创建时间的实体."""
        created_at = _dt_text(directory.created_at) or iso_utc_now()
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO monitored_directories (path, enabled, note, created_at,"
                " last_scan_at, last_scan_status) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    directory.path,
                    int(directory.enabled),
                    directory.note,
                    created_at,
                    _dt_text(directory.last_scan_at),
                    directory.last_scan_status,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - 插入必然返回行 id
                raise DatabaseError("插入监控目录失败: 未返回行 id")
            directory_id = int(cursor.lastrowid)
        return MonitoredDirectory(
            id=directory_id,
            path=directory.path,
            enabled=directory.enabled,
            note=directory.note,
            created_at=_parse_dt(created_at),
            last_scan_at=directory.last_scan_at,
            last_scan_status=directory.last_scan_status,
        )

    def update(self, directory: MonitoredDirectory) -> MonitoredDirectory:
        """按 id 更新路径/启用状态/备注; 缺少 id 时抛错."""
        if directory.id is None:
            raise ValueError("更新监控目录需要 id")
        with self._database.session() as connection:
            connection.execute(
                "UPDATE monitored_directories SET path = ?, enabled = ?, note = ?"
                " WHERE id = ?",
                (
                    directory.path,
                    int(directory.enabled),
                    directory.note,
                    directory.id,
                ),
            )
        return directory

    def delete(self, directory_id: int) -> None:
        """删除一个监控目录(不删除磁盘内容, 也不删除已发现的候选)."""
        with self._database.session() as connection:
            connection.execute(
                "DELETE FROM monitored_directories WHERE id = ?", (directory_id,)
            )

    def duplicate_of(
        self, path: str, *, exclude_id: int | None = None
    ) -> MonitoredDirectory | None:
        """返回与给定路径(大小写不敏感)重复的监控目录.

        编辑已存在的记录时用 ``exclude_id`` 排除自身, 避免把自己判为重复。
        """
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, path, enabled, note, created_at, last_scan_at,"
                " last_scan_status FROM monitored_directories"
                " WHERE LOWER(RTRIM(path, '\\/')) = LOWER(RTRIM(?, '\\/'))"
                " AND (? IS NULL OR id <> ?)",
                (path, exclude_id, exclude_id),
            ).fetchone()
        return _row_to_monitored(row) if row is not None else None

    def mark_scan(
        self,
        directory_id: int,
        *,
        status: str,
        when: datetime,
    ) -> None:
        """记录一次扫描时间与结果状态."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE monitored_directories SET last_scan_at = ?,"
                " last_scan_status = ? WHERE id = ?",
                (_dt_text(when), status, directory_id),
            )

    def enabled_paths(self) -> list[str]:
        """返回全部已启用监控目录的路径(参与扫描)."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT path FROM monitored_directories WHERE enabled = 1 ORDER BY path"
            ).fetchall()
        return [str(row["path"]) for row in rows]


class CandidateRepository:
    """game_candidates 表的行级访问."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def list_all(self, *, status: CandidateStatus | None = None) -> list[GameCandidate]:
        """返回候选列表; 给定 ``status`` 时只返回该进度的候选."""
        with self._database.connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT id, name, install_dir, source, confidence, reason_code,"
                    " detail, found_at, status, health, game_id"
                    " FROM game_candidates ORDER BY name, id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT id, name, install_dir, source, confidence, reason_code,"
                    " detail, found_at, status, health, game_id"
                    " FROM game_candidates WHERE status = ? ORDER BY name, id",
                    (status,),
                ).fetchall()
        return [_row_to_candidate(row) for row in rows]

    def get(self, candidate_id: int) -> GameCandidate | None:
        """按 id 返回候选; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, install_dir, source, confidence, reason_code,"
                " detail, found_at, status, health, game_id"
                " FROM game_candidates WHERE id = ?",
                (candidate_id,),
            ).fetchone()
        return _row_to_candidate(row) if row is not None else None

    def find_by_dir(self, install_dir: str) -> GameCandidate | None:
        """按安装路径(大小写不敏感)查找候选, 用于去重."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, name, install_dir, source, confidence, reason_code,"
                " detail, found_at, status, health, game_id"
                " FROM game_candidates"
                " WHERE LOWER(RTRIM(install_dir, '\\/')) = LOWER(RTRIM(?, '\\/'))",
                (install_dir,),
            ).fetchone()
        return _row_to_candidate(row) if row is not None else None

    def upsert(self, candidate: GameCandidate) -> tuple[GameCandidate, bool]:
        """按安装路径插入或刷新候选, 返回 ``(实体, 是否新建)``.

        刷新只覆盖探测得到的字段(名称/来源/可信度/说明/路径状态); 用户的处理
        进度(status)与已关联的游戏(game_id)保持不变, 否则每次扫描都会把用户
        的"已忽略"决定撤销掉。
        """
        existing = self.find_by_dir(candidate.install_dir)
        if existing is not None and existing.id is not None:
            with self._database.session() as connection:
                connection.execute(
                    "UPDATE game_candidates SET name = ?, source = ?, confidence = ?,"
                    " reason_code = ?, detail = ?, health = ? WHERE id = ?",
                    (
                        candidate.name,
                        candidate.source,
                        candidate.confidence,
                        candidate.reason_code,
                        candidate.detail,
                        candidate.health,
                        existing.id,
                    ),
                )
            refreshed = candidate.model_copy(
                update={
                    "id": existing.id,
                    "status": existing.status,
                    "game_id": existing.game_id,
                    "found_at": existing.found_at,
                }
            )
            return (refreshed, False)
        found_at = _dt_text(candidate.found_at) or iso_utc_now()
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO game_candidates (name, install_dir, source, confidence,"
                " reason_code, detail, found_at, status, health, game_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.name,
                    candidate.install_dir,
                    candidate.source,
                    candidate.confidence,
                    candidate.reason_code,
                    candidate.detail,
                    found_at,
                    candidate.status,
                    candidate.health,
                    candidate.game_id,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - 插入必然返回行 id
                raise DatabaseError("插入候选游戏失败: 未返回行 id")
            candidate_id = int(cursor.lastrowid)
        return (
            candidate.model_copy(
                update={"id": candidate_id, "found_at": _parse_dt(found_at)}
            ),
            True,
        )

    def set_status(
        self,
        candidate_id: int,
        status: CandidateStatus,
        *,
        game_id: int | None,
    ) -> GameCandidate:
        """更新候选的处理进度与关联的游戏.

        ``game_id`` 是必填的关键字参数: 忽略候选时要传回原值, 否则会把"该候选
        对应哪个游戏"这条信息清掉, 恢复时就无法再自动识别为已入库。
        """
        with self._database.session() as connection:
            connection.execute(
                "UPDATE game_candidates SET status = ?, game_id = ? WHERE id = ?",
                (status, game_id, candidate_id),
            )
        candidate = self.get(candidate_id)
        if candidate is None:
            raise DatabaseError(f"未知候选: {candidate_id}")
        return candidate

    def set_install_dir(self, candidate_id: int, install_dir: str) -> GameCandidate:
        """修正候选的安装路径(用户手动纠正探测结果)."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE game_candidates SET install_dir = ? WHERE id = ?",
                (install_dir, candidate_id),
            )
        candidate = self.get(candidate_id)
        if candidate is None:
            raise DatabaseError(f"未知候选: {candidate_id}")
        return candidate

    def set_health(self, candidate_id: int, health: PathHealth) -> None:
        """更新候选路径的健康状态."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE game_candidates SET health = ? WHERE id = ?",
                (health, candidate_id),
            )

    def delete(self, candidate_id: int) -> None:
        """删除一条候选记录."""
        with self._database.session() as connection:
            connection.execute(
                "DELETE FROM game_candidates WHERE id = ?", (candidate_id,)
            )

    def release_imported(self, game_id: int) -> int:
        """把挂在该游戏上的"已导入"候选退回待处理, 返回改动条数.

        删除游戏之前必须先调用它: ``game_candidates.game_id`` 是
        ``ON DELETE SET NULL``, 游戏一旦删掉就再也找不到这批候选, 它们会永远停在
        "已导入"却指向任何游戏(界面上既不在待处理里, 也导不进来)。
        """
        with self._database.session() as connection:
            cursor = connection.execute(
                "UPDATE game_candidates SET status = 'new', game_id = NULL"
                " WHERE game_id = ? AND status = 'imported'",
                (game_id,),
            )
        return int(cursor.rowcount)

    def count_by_status(self) -> dict[str, int]:
        """返回各处理进度下的候选数量(未出现的进度计 0)."""
        counts = {"new": 0, "imported": 0, "ignored": 0}
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM game_candidates GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[str(row[0])] = int(row[1])
        return counts


class SaveCandidateRepository:
    """save_path_candidates 表的行级访问.

    候选在用户确认前只存在于这张表: 探测结果可能指向主目录或游戏安装目录,
    静默写进 ``save_locations`` 会把整盘内容卷进备份, 所以确认这一步必须由
    用户触发(见 :mod:`archive_management.application.candidates`)。
    """

    def __init__(self, database: Database) -> None:
        """绑定数据库连接工厂."""
        self._database = database

    def list_all(
        self, *, status: SaveCandidateStatus | None = None
    ) -> list[SaveCandidate]:
        """返回全部候选, 待确认的排在前面(与按游戏查询同一套排序)."""
        if status is None:
            sql = (
                "SELECT * FROM save_path_candidates"
                " ORDER BY CASE status WHEN 'suggested' THEN 0 ELSE 1 END,"
                " game_id, path"
            )
            params: tuple[object, ...] = ()
        else:
            sql = (
                "SELECT * FROM save_path_candidates WHERE status = ?"
                " ORDER BY CASE status WHEN 'suggested' THEN 0 ELSE 1 END,"
                " game_id, path"
            )
            params = (status,)
        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_save_candidate(row) for row in rows]

    def list_for_game(
        self, game_id: int, *, status: SaveCandidateStatus | None = None
    ) -> list[SaveCandidate]:
        """返回某个游戏的候选, 待确认的排在前面(按路径)."""
        order = (
            " FROM save_path_candidates WHERE game_id = ?"
            " ORDER BY CASE status WHEN 'suggested' THEN 0 ELSE 1 END, path"
        )
        if status is None:
            sql = "SELECT *" + order
            params: tuple[object, ...] = (game_id,)
        else:
            sql = (
                "SELECT * FROM save_path_candidates WHERE game_id = ?"
                " AND status = ?"
                " ORDER BY CASE status WHEN 'suggested' THEN 0 ELSE 1 END, path"
            )
            params = (game_id, status)
        with self._database.connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [_row_to_save_candidate(row) for row in rows]

    def get(self, candidate_id: int) -> SaveCandidate | None:
        """按 id 读取一条候选, 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM save_path_candidates WHERE id = ?", (candidate_id,)
            ).fetchone()
        return None if row is None else _row_to_save_candidate(row)

    def upsert(self, candidate: SaveCandidate) -> tuple[SaveCandidate, bool]:
        """写入一条候选, 返回 ``(实体, 是否新建)``.

        用户已经确认或忽略过的候选不会被下一次探测覆盖, 否则界面上的决定会
        被扫描结果抹掉; 待确认的候选则刷新探测字段(路径健康、危险标记等)。
        """
        with self._database.session() as connection:
            row = connection.execute(
                "SELECT id, status FROM save_path_candidates"
                " WHERE game_id = ? AND path = ?",
                (candidate.game_id, candidate.path),
            ).fetchone()
            if row is not None and str(row["status"]) != "suggested":
                candidate_id = int(row["id"])
                created = False
            else:
                candidate_id, created = self._write(connection, candidate, row)
        stored = self.get(candidate_id)
        if stored is None:  # pragma: no cover - 刚写入的行必然可读
            raise DatabaseError(f"写入存档候选失败: {candidate.path}")
        return (stored, created)

    def _write(
        self,
        connection: sqlite3.Connection,
        candidate: SaveCandidate,
        row: sqlite3.Row | None,
    ) -> tuple[int, bool]:
        """在事务内插入新候选或刷新已有候选的探测字段."""
        found_at = _dt_text(candidate.found_at) or iso_utc_now()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO save_path_candidates (game_id, platform,"
                " platform_game_id, path, path_kind, reason_code, detail,"
                " relative_path, confidence, health, risk_reason, status,"
                " found_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate.game_id,
                    candidate.platform,
                    candidate.platform_game_id,
                    candidate.path,
                    candidate.path_kind,
                    candidate.reason_code,
                    candidate.detail,
                    candidate.relative_path,
                    candidate.confidence,
                    candidate.health,
                    candidate.risk_reason,
                    candidate.status,
                    found_at,
                ),
            )
            if cursor.lastrowid is None:  # pragma: no cover - 插入必然返回 id
                raise DatabaseError("插入存档候选失败: 未返回行 id")
            return (int(cursor.lastrowid), True)
        candidate_id = int(row["id"])
        connection.execute(
            "UPDATE save_path_candidates SET platform = ?, platform_game_id = ?,"
            " path_kind = ?, reason_code = ?, detail = ?, relative_path = ?,"
            " confidence = ?, health = ?, risk_reason = ?, status = ?, found_at = ?"
            " WHERE id = ?",
            (
                candidate.platform,
                candidate.platform_game_id,
                candidate.path_kind,
                candidate.reason_code,
                candidate.detail,
                candidate.relative_path,
                candidate.confidence,
                candidate.health,
                candidate.risk_reason,
                candidate.status,
                found_at,
                candidate_id,
            ),
        )
        return (candidate_id, False)

    def set_status(
        self,
        candidate_id: int,
        status: SaveCandidateStatus,
        *,
        decided_at: datetime | None = None,
    ) -> SaveCandidate:
        """更新候选的处理进度; 回到 ``suggested`` 会清掉决定时间."""
        decided = None
        if status != "suggested":
            decided = _dt_text(decided_at) or iso_utc_now()
        with self._database.session() as connection:
            connection.execute(
                "UPDATE save_path_candidates SET status = ?, decided_at = ?"
                " WHERE id = ?",
                (status, decided, candidate_id),
            )
        candidate = self.get(candidate_id)
        if candidate is None:
            raise DatabaseError(f"未知存档候选: {candidate_id}")
        return candidate

    def count_by_status(self) -> dict[str, int]:
        """返回各处理进度下的候选数量(未出现的进度计 0)."""
        counts = {"suggested": 0, "confirmed": 0, "ignored": 0}
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) FROM save_path_candidates GROUP BY status"
            ).fetchall()
        for row in rows:
            counts[str(row[0])] = int(row[1])
        return counts


@dataclass(frozen=True)
class HomeRow:
    """主页聚合查询的一行: 游戏 + 存档位置 + 备份计数 + 是否来自探测流程.

    ``locations`` 是 ``(路径, 路径类型)`` 元组, 只在应用层判定风险时使用, 因此
    不构造完整的 :class:`SaveLocation`(主页不需要校验时间等字段)。
    """

    game: Game
    locations: tuple[tuple[str, str], ...]
    backup_count: int
    last_backup_at: datetime | None


class HomeRepository:
    """主页所需的聚合查询与筛选条件持久化."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def list_rows(self) -> list[HomeRow]:
        """一次性取出全部游戏及其计数, 避免按游戏逐个查询.

        共 3 条查询(游戏、存档位置、备份计数), 与游戏数量无关。
        """
        with self._database.connect() as connection:
            games = [
                _row_to_game(row)
                for row in connection.execute(
                    "SELECT id, name, steam_app_id, platform, enabled, created_at,"
                    " original_name, localized_name, storage_key, origin, tags,"
                    " archived, last_activity_at FROM games ORDER BY name, id"
                ).fetchall()
            ]
            paths: dict[int, list[tuple[str, str]]] = {}
            for row in connection.execute(
                "SELECT game_id, path, path_kind FROM save_locations ORDER BY id"
            ).fetchall():
                paths.setdefault(int(row["game_id"]), []).append(
                    (str(row["path"]), str(row["path_kind"]))
                )
            counts: dict[int, tuple[int, datetime | None]] = {}
            for row in connection.execute(
                "SELECT game_id, COUNT(*) AS total, MAX(created_at) AS latest"
                " FROM backup_nodes GROUP BY game_id"
            ).fetchall():
                counts[int(row["game_id"])] = (
                    int(row["total"]),
                    _parse_dt(row["latest"]),
                )
        rows: list[HomeRow] = []
        for game in games:
            if game.id is None:  # pragma: no cover - 查询总是带 id
                continue
            total, latest = counts.get(game.id, (0, None))
            rows.append(
                HomeRow(
                    game=game,
                    locations=tuple(paths.get(game.id, ())),
                    backup_count=total,
                    last_backup_at=latest,
                )
            )
        return rows

    def load_state(self) -> HomeFilter | None:
        """读取主页状态; 无记录或版本不符时返回 None(调用方用默认值)."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT view, origin, category, search, layout, page_size, version"
                " FROM home_state WHERE id = 1"
            ).fetchone()
        if row is None or int(row["version"]) != HOME_STATE_VERSION:
            return None
        try:
            view = HomeView(str(row["view"]))
            layout = HomeLayout(str(row["layout"]))
        except ValueError:  # pragma: no cover - 版本内取值被手工改坏
            return None
        return HomeFilter(
            view=view,
            origin=str(row["origin"] or ""),
            category=str(row["category"] or ""),
            search=str(row["search"] or ""),
            layout=layout,
            page_size=int(row["page_size"]),
            version=HOME_STATE_VERSION,
        ).normalized()

    def save_state(self, active: HomeFilter) -> HomeFilter:
        """写入主页状态(单行表), 返回规范化的取值."""
        clean = active.normalized()
        with self._database.session() as connection:
            connection.execute(
                "INSERT INTO home_state (id, view, origin, category, search, layout,"
                " page_size, version, updated_at) VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET view = excluded.view,"
                " origin = excluded.origin, category = excluded.category,"
                " search = excluded.search, layout = excluded.layout,"
                " page_size = excluded.page_size,"
                " version = excluded.version, updated_at = excluded.updated_at",
                (
                    clean.view.value,
                    clean.origin,
                    clean.category,
                    clean.search,
                    clean.layout.value,
                    clean.page_size,
                    clean.version,
                    iso_utc_now(),
                ),
            )
        return clean


class ActivationStateRepository:
    """自动启停状态(单行表 ``activation_state``).

    读不到记录或版本不符时返回默认状态而不是报错: 这份状态只是"自动启停的观察
    记录", 丢了顶多让它重新等一次"游戏在运行"的观察, 不该阻断任何操作。
    """

    def __init__(self, database: Database) -> None:
        """绑定数据库."""
        self._database = database

    def load(self) -> ActivationState:
        """读取状态; 没有记录或版本不符时返回默认状态(不建行)."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT monitor_game_id, armed, paused, version"
                " FROM activation_state WHERE id = 1"
            ).fetchone()
        if row is None or int(row["version"]) != ACTIVATION_STATE_VERSION:
            return ActivationState()
        return ActivationState(
            monitor_game_id=_optional_int(row["monitor_game_id"]),
            armed=_as_bool(row["armed"]),
            paused=_as_bool(row["paused"]),
            version=ACTIVATION_STATE_VERSION,
        )

    def save(self, state: ActivationState) -> ActivationState:
        """写入状态(单行表), 返回落库后的取值."""
        clean = replace(state, version=ACTIVATION_STATE_VERSION)
        with self._database.session() as connection:
            connection.execute(
                "INSERT INTO activation_state (id, monitor_game_id, armed,"
                " paused, version, updated_at)"
                " VALUES (1, ?, ?, ?, ?, ?)"
                " ON CONFLICT (id) DO UPDATE SET"
                " monitor_game_id = excluded.monitor_game_id,"
                " armed = excluded.armed,"
                " paused = excluded.paused,"
                " version = excluded.version, updated_at = excluded.updated_at",
                (
                    clean.monitor_game_id,
                    int(clean.armed),
                    int(clean.paused),
                    clean.version,
                    iso_utc_now(),
                ),
            )
        return clean


class ActivationQueueRepository:
    """启动顺序队列(表 ``activation_runs``).

    这里存的是"上一次探测看到的运行集合 + 顺序", 因此每次变化都整表重写:
    ``position`` 必须连续, 而删一条、插一条很容易把位置号写乱。整表重写在一次
    事务里完成, 不会出现"删完了还没插入"的中间状态。
    """

    def __init__(self, database: Database) -> None:
        """绑定数据库."""
        self._database = database

    def load(self) -> tuple[RunEntry, ...]:
        """按位置顺序读取队列(表不存在或为空时返回空队列)."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT game_id, position, suppressed, first_seen_at, last_seen_at"
                " FROM activation_runs ORDER BY position, game_id"
            ).fetchall()
        return tuple(
            RunEntry(
                game_id=int(row["game_id"]),
                position=int(row["position"]),
                suppressed=_as_bool(row["suppressed"]),
                first_seen_at=str(row["first_seen_at"]),
                last_seen_at=str(row["last_seen_at"]),
            )
            for row in rows
        )

    def save(self, entries: Sequence[RunEntry]) -> tuple[RunEntry, ...]:
        """整表重写队列, 返回落库后的取值."""
        clean = tuple(entries)
        with self._database.session() as connection:
            connection.execute("DELETE FROM activation_runs")
            connection.executemany(
                "INSERT INTO activation_runs (game_id, position, suppressed,"
                " first_seen_at, last_seen_at) VALUES (?, ?, ?, ?, ?)",
                [
                    (
                        entry.game_id,
                        entry.position,
                        int(entry.suppressed),
                        entry.first_seen_at,
                        entry.last_seen_at,
                    )
                    for entry in clean
                ],
            )
        return clean
