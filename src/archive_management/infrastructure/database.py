"""SQLite 存储: 连接、schema 迁移与会话管理.

使用标准库 ``sqlite3`` 而不引入 ORM, 以降低依赖与迁移复杂度
所有写操作通过 :meth:`Database.session` 在事务中执行,
出现异常即回滚.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from archive_management.exceptions import DatabaseError
from archive_management.infrastructure.schema import (
    MIGRATION_TABLE,
    SCHEMA_MIGRATIONS,
    meta_table_sql,
)

_BUSY_TIMEOUT_MS = 5_000


def iso_utc_now() -> str:
    """返回带毫秒的 UTC ISO 时间, 用于数据库时间戳."""
    return datetime.now(UTC).isoformat(timespec="seconds")


class Database:
    """封装到单个 SQLite 文件的连接、迁移与查询能力."""

    def __init__(self, path: Path) -> None:
        """根据数据库文件路径初始化封装."""
        self._path = path

    @property
    def path(self) -> Path:
        """返回数据库文件路径."""
        return self._path

    def connect(self) -> sqlite3.Connection:
        """建立连接并设置必要的 PRAGMA."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            connection = sqlite3.connect(self._path)
        except sqlite3.Error as exc:
            raise DatabaseError(f"无法打开数据库 {self._path}: {exc}") from exc
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        return connection

    @contextmanager
    def session(self) -> Iterator[sqlite3.Connection]:
        """提供自动提交/回滚的写会话."""
        connection = self.connect()
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def schema_version(self) -> int:
        """返回当前已应用的 schema 版本(0 表示尚未初始化)."""
        with self.connect() as connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master" " WHERE type = 'table' AND name = ?",
                (MIGRATION_TABLE,),
            ).fetchone()
            if exists is None:
                return 0
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def latest_schema_version(self) -> int:
        """返回代码内定义的最终 schema 版本."""
        return SCHEMA_MIGRATIONS[-1][0] if SCHEMA_MIGRATIONS else 0

    def migrate(self, *, target: int | None = None) -> int:
        """在事务中应用所有未执行的迁移, 返回迁移后的版本.

        ``target`` 用于将数据库迁移到某个历史版本(测试场景).
        """
        applied = self.schema_version()
        pending = [(v, stmts) for v, stmts in SCHEMA_MIGRATIONS if v > applied]
        if target is not None:
            pending = [(v, stmts) for v, stmts in pending if v <= target]
        if not pending:
            return self.schema_version()

        with self.session() as connection:
            connection.execute(meta_table_sql())
            for version, statements in pending:
                for statement in statements:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations (version, applied_at)"
                    " VALUES (?, ?)",
                    (version, iso_utc_now()),
                )
        return self.schema_version()
