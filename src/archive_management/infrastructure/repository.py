"""游戏与存档位置的 SQLite 数据访问层.

Repository 把 :mod:`archive_management.domain` 中的领域实体映射为
SQLite 行, 供应用用例与后端调用. 所有写操作都封装在
:meth:`Database.session` 事务中, 任一语句失败即整体回滚.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime

from archive_management.domain import (
    BackupFileEntry,
    BackupNode,
    Game,
    Operation,
    OperationKind,
    OperationStatus,
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


def _row_to_operation(row: sqlite3.Row) -> Operation:
    return Operation(
        id=int(row["id"]),
        op_kind=str(row["op_kind"]),  # type: ignore[arg-type]
        game_id=None if row["game_id"] is None else int(row["game_id"]),
        status=str(row["status"]),  # type: ignore[arg-type]
        message=row["message"],
        started_at=_parse_dt(row["started_at"]),
        finished_at=_parse_dt(row["finished_at"]),
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
                "UPDATE games SET current_backup_id = NULL"
                " WHERE current_backup_id = ?",
                (backup_id,),
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


class BackupRepository:
    """backup_nodes / backup_files 表的行级访问(阶段 D)."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def add(self, node: BackupNode) -> BackupNode:
        """插入一个备份节点, 返回带 id 的实体."""
        created_at = _dt_text(node.created_at) or iso_utc_now()
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO backup_nodes (game_id, parent_id, node_kind,"
                " branch_name, note, content_hash, storage_relpath, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    node.game_id,
                    node.parent_id,
                    node.node_kind,
                    node.branch_name,
                    node.note,
                    node.content_hash,
                    node.storage_relpath,
                    created_at,
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
        )

    def get(self, backup_id: int) -> BackupNode | None:
        """按 id 返回单个备份节点; 不存在返回 None."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, parent_id, node_kind, branch_name, note, title,"
                " content_hash, storage_relpath, created_at FROM backup_nodes"
                " WHERE id = ?",
                (backup_id,),
            ).fetchone()
        return _row_to_backup_node(row) if row is not None else None

    def list_for_game(self, game_id: int) -> list[BackupNode]:
        """返回某游戏的全部备份节点, 按创建时间升序(便于建树)."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, game_id, parent_id, node_kind, branch_name, note, title,"
                " content_hash, storage_relpath, created_at FROM backup_nodes"
                " WHERE game_id = ? ORDER BY created_at, id",
                (game_id,),
            ).fetchall()
        return [_row_to_backup_node(row) for row in rows]

    def latest_for_game(self, game_id: int) -> BackupNode | None:
        """返回某游戏最新的备份节点(即分支树的当前末端)."""
        with self._database.connect() as connection:
            row = connection.execute(
                "SELECT id, game_id, parent_id, node_kind, branch_name, note, title,"
                " content_hash, storage_relpath, created_at FROM backup_nodes"
                " WHERE game_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
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
                " branch_name, note, title, content_hash, storage_relpath, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                ),
            )
            if cursor.lastrowid is None:
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


class OperationRepository:
    """operations 表的行级访问: 记录备份/恢复等操作的状态与错误摘要."""

    def __init__(self, database: Database) -> None:
        """绑定到指定的数据库封装."""
        self._database = database

    def start(
        self,
        op_kind: OperationKind,
        game_id: int | None = None,
        message: str | None = None,
    ) -> int:
        """开始一次操作, 返回操作 id."""
        with self._database.session() as connection:
            cursor = connection.execute(
                "INSERT INTO operations (op_kind, game_id, status, message,"
                " started_at) VALUES (?, ?, 'started', ?, ?)",
                (op_kind, game_id, message, iso_utc_now()),
            )
            if cursor.lastrowid is None:
                raise DatabaseError("插入操作记录失败: 未返回行 id")
            return int(cursor.lastrowid)

    def finish(
        self,
        operation_id: int,
        status: OperationStatus,
        message: str | None = None,
    ) -> None:
        """结束一次操作, 写入结果状态与摘要."""
        with self._database.session() as connection:
            connection.execute(
                "UPDATE operations SET status = ?, message = ?, finished_at = ?"
                " WHERE id = ?",
                (status, message, iso_utc_now(), operation_id),
            )

    def list_recent(self, *, limit: int = 20) -> list[Operation]:
        """返回最近的操作记录(新的在前)."""
        with self._database.connect() as connection:
            rows = connection.execute(
                "SELECT id, op_kind, game_id, status, message, started_at,"
                " finished_at FROM operations ORDER BY started_at DESC, id DESC"
                " LIMIT ?",
                (limit,),
            ).fetchall()
        return [_row_to_operation(row) for row in rows]


class ScheduledJobRepository:
    """scheduled_jobs 表的行级访问(阶段 D 定期备份)."""

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
                if cursor.lastrowid is None:
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
