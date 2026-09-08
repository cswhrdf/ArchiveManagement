"""游戏与存档位置仓库的单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.domain import Game, SaveLocation
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    GameRepository,
    SaveLocationRepository,
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
