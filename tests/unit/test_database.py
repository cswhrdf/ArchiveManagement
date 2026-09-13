"""数据库迁移与会话单元测试."""

from __future__ import annotations

import sqlite3
from contextlib import AbstractContextManager
from pathlib import Path

import pytest

from archive_management.infrastructure.database import Database

pytestmark = [
    pytest.mark.database,
    pytest.mark.critical,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("数据库迁移"),
    pytest.mark.story("数据库结构与版本"),
    pytest.mark.layer("unit"),
]

EXPECTED_TABLES = {
    "games",
    "save_locations",
    "backup_nodes",
    "backup_files",
    "scheduled_jobs",
    "operations",
    "schema_migrations",
}


def _insert_then_raise(
    session: AbstractContextManager[sqlite3.Connection],
) -> None:
    """在会话内写入一行后抛出异常, 用于验证事务回滚."""
    with session as connection:
        connection.execute("INSERT INTO games (name) VALUES (?)", ("Demo",))
        raise RuntimeError("boom")


def _table_names(database: Database) -> set[str]:
    with database.connect() as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {str(row[0]) for row in rows}


def test_fresh_database_has_no_schema(tmp_path: Path) -> None:
    database = Database(tmp_path / "nested" / "app.db")
    assert database.schema_version() == 0


def test_migrate_creates_all_tables(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    version = database.migrate()
    assert version == database.latest_schema_version()
    assert database.latest_schema_version() >= 1
    assert database.schema_version() == database.latest_schema_version()
    assert _table_names(database) >= EXPECTED_TABLES


def test_migrate_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.migrate()
    database.migrate()
    assert database.schema_version() == database.latest_schema_version()
    with database.connect() as connection:
        count = connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()
    assert count is not None
    assert int(count[0]) == database.latest_schema_version()


def test_session_commits_changes(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.migrate()
    with database.session() as connection:
        connection.execute("INSERT INTO games (name) VALUES (?)", ("Demo",))
    with database.connect() as connection:
        row = connection.execute("SELECT COUNT(*) FROM games").fetchone()
    assert row is not None
    assert int(row[0]) == 1


def test_session_rolls_back_on_error(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.migrate()
    with pytest.raises(RuntimeError):
        _insert_then_raise(database.session())
    with database.connect() as connection:
        row = connection.execute("SELECT COUNT(*) FROM games").fetchone()
    assert row is not None
    assert int(row[0]) == 0


def test_foreign_keys_are_enforced(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.migrate()
    with pytest.raises(sqlite3.IntegrityError), database.session() as connection:
        connection.execute(
            "INSERT INTO save_locations (game_id, path) VALUES (?, ?)",
            (9999, "/nowhere"),
        )
