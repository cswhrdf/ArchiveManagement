"""游戏与存档位置仓库的单元测试."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archive_management.domain import (
    BackupFileEntry,
    BackupNode,
    Game,
    SaveLocation,
    ScheduledJob,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    OperationRepository,
    SaveLocationRepository,
    ScheduledJobRepository,
)

pytestmark = [pytest.mark.repository, pytest.mark.critical]


def _database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "app.db")
    database.migrate()
    return database


def _add_game(repo: GameRepository, name: str = "Demo") -> Game:
    return repo.add(Game(name=name))


def test_game_add_assigns_id_and_created_at(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repo = GameRepository(database)
    game = _add_game(repo, name="星际拓荒")
    assert game.id is not None
    assert game.created_at is not None
    fetched = repo.get(game.id)
    assert fetched is not None
    assert fetched.name == "星际拓荒"
    assert fetched.enabled is True
    assert fetched.platform == "windows"


def test_game_repository_list_orders_by_creation(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repo = GameRepository(database)
    _add_game(repo, name="A")
    _add_game(repo, name="B")
    games = repo.list()
    assert [game.name for game in games] == ["A", "B"]


def test_game_update_persists_changes(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repo = GameRepository(database)
    game = _add_game(repo)
    assert game.id is not None
    renamed = Game(
        id=game.id,
        name="新名字",
        steam_app_id=480,
        platform="linux",
        enabled=False,
        created_at=game.created_at,
    )
    repo.update(renamed)
    fetched = repo.get(game.id)
    assert fetched is not None
    assert fetched.name == "新名字"
    assert fetched.steam_app_id == 480
    assert fetched.platform == "linux"
    assert fetched.enabled is False


def test_game_update_without_id_raises(tmp_path: Path) -> None:
    repo = GameRepository(_database(tmp_path))
    with pytest.raises(ValueError):
        repo.update(Game(name="无 id"))


def test_game_delete_cascades_locations(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_repo = GameRepository(database)
    location_repo = SaveLocationRepository(database)
    game = _add_game(repo=game_repo)
    assert game.id is not None
    location_repo.add(
        SaveLocation(
            game_id=game.id, path=str(tmp_path / "save"), path_kind="directory"
        )
    )
    game_repo.delete(game.id)
    assert game_repo.get(game.id) is None
    assert location_repo.list_for_game(game.id) == []


def test_game_counts_locations_and_backups(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_repo = GameRepository(database)
    location_repo = SaveLocationRepository(database)
    game = _add_game(repo=game_repo)
    assert game.id is not None
    location_repo.add(
        SaveLocation(
            game_id=game.id, path=str(tmp_path / "save-a"), path_kind="directory"
        )
    )
    location_repo.add(
        SaveLocation(
            game_id=game.id, path=str(tmp_path / "save-b"), path_kind="directory"
        )
    )
    assert game_repo.count_locations(game.id) == 2
    assert game_repo.count_backups(game.id) == 0


def test_location_add_and_list_primary_first(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_repo = GameRepository(database)
    location_repo = SaveLocationRepository(database)
    game = _add_game(repo=game_repo)
    assert game.id is not None
    location_repo.add(
        SaveLocation(
            game_id=game.id, path=str(tmp_path / "plain"), path_kind="directory"
        )
    )
    location_repo.add(
        SaveLocation(
            game_id=game.id,
            path=str(tmp_path / "primary"),
            path_kind="directory",
            is_primary=True,
        )
    )
    locations = location_repo.list_for_game(game.id)
    assert [location.path for location in locations] == [
        str(tmp_path / "primary"),
        str(tmp_path / "plain"),
    ]


def test_make_primary_clears_other_primary_flags(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_repo = GameRepository(database)
    location_repo = SaveLocationRepository(database)
    game = _add_game(repo=game_repo)
    assert game.id is not None
    first = location_repo.add(
        SaveLocation(game_id=game.id, path=str(tmp_path / "a"), path_kind="directory")
    )
    second = location_repo.add(
        SaveLocation(game_id=game.id, path=str(tmp_path / "b"), path_kind="directory")
    )
    assert first.id is not None
    assert second.id is not None
    location_repo.make_primary(game.id, first.id)
    location_repo.make_primary(game.id, second.id)
    locations = location_repo.list_for_game(game.id)
    primary = [location for location in locations if location.is_primary]
    assert len(primary) == 1
    assert primary[0].id == second.id


def test_duplicate_of_is_case_insensitive(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_repo = GameRepository(database)
    location_repo = SaveLocationRepository(database)
    game = _add_game(repo=game_repo)
    assert game.id is not None
    location_repo.add(
        SaveLocation(game_id=game.id, path=r"C:\Games\Save", path_kind="directory")
    )
    assert location_repo.duplicate_of(game.id, r"c:\games\save") is not None
    assert location_repo.duplicate_of(game.id, r"D:\Other") is None


def test_location_update_and_delete(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_repo = GameRepository(database)
    location_repo = SaveLocationRepository(database)
    game = _add_game(repo=game_repo)
    assert game.id is not None
    location = location_repo.add(
        SaveLocation(game_id=game.id, path=str(tmp_path / "a"), path_kind="directory")
    )
    assert location.id is not None
    updated = SaveLocation(
        id=location.id,
        game_id=game.id,
        path=str(tmp_path / "a2"),
        path_kind="file",
        source="steam",
    )
    location_repo.update(updated)
    fetched = location_repo.get(location.id)
    assert fetched is not None
    assert fetched.path == str(tmp_path / "a2")
    assert fetched.path_kind == "file"
    assert fetched.source == "steam"

    location_repo.delete(location.id)
    assert location_repo.get(location.id) is None


def test_location_update_without_id_raises(tmp_path: Path) -> None:
    repo = SaveLocationRepository(_database(tmp_path))
    with pytest.raises(ValueError):
        repo.update(
            SaveLocation(game_id=1, path=str(tmp_path / "x"), path_kind="directory")
        )


# ------------------------------------------------------- 备份节点与文件清单


def _game_id(repo: GameRepository) -> int:
    game = repo.add(Game(name="Demo"))
    assert game.id is not None
    return game.id


def test_backup_add_assigns_id_and_defaults(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = BackupRepository(database)

    node = repo.add(BackupNode(game_id=game_id, node_kind="manual", note="首个"))

    assert node.id is not None
    assert node.created_at is not None
    assert node.parent_id is None
    assert repo.get(node.id) == node
    assert repo.count(game_id) == 1


def test_backup_list_orders_by_creation_and_latest_wins(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = BackupRepository(database)

    first = repo.add(BackupNode(game_id=game_id, note="first"))
    second = repo.add(BackupNode(game_id=game_id, parent_id=first.id, note="second"))

    assert [item.note for item in repo.list_for_game(game_id)] == ["first", "second"]
    assert repo.latest_for_game(game_id) == second


def test_backup_add_with_files_is_atomic(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = BackupRepository(database)

    node = repo.add_with_files(
        BackupNode(game_id=game_id, node_kind="manual"),
        [
            BackupFileEntry(
                backup_id=0,
                relative_path="loc-0/a.dat",
                size=3,
                sha256="hash-a",
            ),
            BackupFileEntry(
                backup_id=0,
                relative_path="loc-0",
                size=0,
                sha256="dir-hash",
                file_kind="directory",
            ),
        ],
    )

    assert node.id is not None
    entries = repo.list_files(node.id)
    assert [entry.relative_path for entry in entries] == ["loc-0", "loc-0/a.dat"]
    assert all(entry.backup_id == node.id for entry in entries)
    assert repo.describe(node.id) == (1, 3)


def test_backup_add_with_files_rolls_back_on_conflict(tmp_path: Path) -> None:
    """清单写入失败时节点也不应落库(同一事务)."""
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = BackupRepository(database)
    duplicate = [
        BackupFileEntry(
            backup_id=0, relative_path="loc-0/a.dat", size=1, sha256="hash"
        ),
        BackupFileEntry(
            backup_id=0, relative_path="loc-0/a.dat", size=1, sha256="hash"
        ),
    ]

    with pytest.raises(sqlite3.IntegrityError):
        repo.add_with_files(BackupNode(game_id=game_id, node_kind="manual"), duplicate)

    assert repo.count(game_id) == 0


def test_backup_files_cascade_on_node_delete(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = BackupRepository(database)
    node = repo.add_with_files(
        BackupNode(game_id=game_id, node_kind="manual"),
        [BackupFileEntry(backup_id=0, relative_path="loc-0/a.dat", size=1, sha256="h")],
    )
    assert node.id is not None

    repo.delete(node.id)

    assert repo.list_files(node.id) == []
    assert repo.get(node.id) is None


# ------------------------------------------------------------------- 操作记录


def test_operation_repository_records_lifecycle(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repo = OperationRepository(database)

    operation_id = repo.start("backup", None, "手动备份")
    repo.finish(operation_id, "succeeded", "1 条文件清单")

    recent = repo.list_recent()
    assert len(recent) == 1
    assert recent[0].op_kind == "backup"
    assert recent[0].status == "succeeded"
    assert recent[0].message == "1 条文件清单"
    assert recent[0].finished_at is not None


def test_operation_repository_limits_and_orders(tmp_path: Path) -> None:
    repo = OperationRepository(_database(tmp_path))
    for index in range(3):
        operation_id = repo.start("backup", None, f"op-{index}")
        repo.finish(operation_id, "failed", f"err-{index}")
    recent = repo.list_recent(limit=2)
    assert len(recent) == 2
    assert {item.message for item in recent} == {"err-1", "err-2"}


# ----------------------------------------------------------------- 定期任务


def test_scheduled_job_insert_update_and_mark_run(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = ScheduledJobRepository(database)

    job = repo.upsert(ScheduledJob(game_id=game_id, schedule="30m", enabled=True))
    assert job.id is not None
    assert repo.for_game(game_id) == [job]

    updated = repo.upsert(
        ScheduledJob(
            id=job.id,
            game_id=game_id,
            schedule="2h",
            enabled=False,
            last_run_at=job.last_run_at,
        )
    )
    assert updated.id == job.id
    assert updated.schedule == "2h"
    assert updated.enabled is False
    assert len(repo.list_all()) == 1

    assert updated.id is not None
    repo.mark_run(
        updated.id,
        ran_at=datetime(2026, 9, 10, 8, 0, tzinfo=UTC),
        next_run_at=datetime(2026, 9, 10, 10, 0, tzinfo=UTC),
        error="磁盘空间不足",
    )
    refetched = repo.get(updated.id)
    assert refetched is not None
    assert refetched.last_error == "磁盘空间不足"
    assert refetched.last_run_at is not None
    assert refetched.next_run_at is not None


def test_scheduled_job_delete(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = ScheduledJobRepository(database)
    job = repo.upsert(ScheduledJob(game_id=game_id, schedule="1d"))
    assert job.id is not None

    repo.delete(job.id)

    assert repo.list_all() == []
    assert repo.get(job.id) is None


def test_scheduled_job_keep_auto_roundtrip(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = ScheduledJobRepository(database)

    job = repo.upsert(ScheduledJob(game_id=game_id, schedule="30m", keep_auto=5))
    assert job.keep_auto == 5
    assert repo.for_game(game_id)[0].keep_auto == 5

    updated = repo.upsert(
        ScheduledJob(id=job.id, game_id=game_id, schedule="30m", keep_auto=2)
    )
    assert updated.keep_auto == 2
    assert repo.get(job.id or 0).keep_auto == 2  # type: ignore[union-attr]


# ----------------------------------------------------------- 当前节点与备份编辑


def _backup(database: Database, game_id: int, **kwargs: object) -> BackupNode:
    return BackupRepository(database).add(BackupNode(game_id=game_id, **kwargs))  # type: ignore[arg-type]


def test_current_backup_pointer_roundtrip(tmp_path: Path) -> None:
    database = _database(tmp_path)
    games = GameRepository(database)
    game_id = _game_id(games)
    node = _backup(database, game_id)

    assert games.current_backup(game_id) is None
    games.set_current_backup(game_id, node.id)
    assert games.current_backup(game_id) == node.id
    games.set_current_backup(game_id, None)
    assert games.current_backup(game_id) is None


def test_current_backup_cleared_when_node_deleted(tmp_path: Path) -> None:
    """删除备份后, 指向它的当前节点指针必须失效."""
    database = _database(tmp_path)
    games = GameRepository(database)
    game_id = _game_id(games)
    node = _backup(database, game_id)
    games.set_current_backup(game_id, node.id)
    assert node.id is not None

    BackupRepository(database).delete(node.id)

    assert games.current_backup(game_id) is None


def test_backup_update_meta_and_reparent(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = BackupRepository(database)
    first = repo.add(BackupNode(game_id=game_id))
    second = repo.add(BackupNode(game_id=game_id, parent_id=first.id))
    assert second.id is not None

    updated = repo.update_meta(second.id, title="新名字", note="描述")
    assert updated is not None
    assert updated.title == "新名字"
    assert updated.note == "描述"

    repo.reparent(second.id, None)
    refetched = repo.get(second.id)
    assert refetched is not None
    assert refetched.parent_id is None


def test_backup_delete_many(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game_id(GameRepository(database))
    repo = BackupRepository(database)
    nodes = [
        repo.add(BackupNode(game_id=game_id, note=str(index))) for index in range(3)
    ]
    ids = [node.id for node in nodes if node.id is not None]

    removed = repo.delete_many(ids[:2])

    assert removed == 2
    assert [node.id for node in repo.list_for_game(game_id)] == [ids[2]]
    assert repo.delete_many([]) == 0
