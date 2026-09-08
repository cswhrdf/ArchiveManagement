"""基于 SQLite 的真实后端单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.ui.sql_backend import SqlArchiveService

pytestmark = [pytest.mark.backend, pytest.mark.database, pytest.mark.critical]


def _service(tmp_path: Path) -> SqlArchiveService:
    database = Database(tmp_path / "app.db")
    database.migrate()
    return SqlArchiveService(database, backup_root=tmp_path / "backups")


def test_add_game_and_list(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    games = service.list_games()
    assert [game.game_id for game in games] == [summary.game_id]
    assert summary.has_locations is False
    assert summary.backup_count == 0


def test_add_game_rejects_empty_name(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.add_game("   ")


def test_update_game_renames(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("旧名").game_id
    updated = service.update_game(game_id, "新名")
    assert updated.name == "新名"
    assert service.get_detail(game_id).name == "新名"


def test_set_game_enabled_flag(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    disabled = service.set_game_enabled(game_id, False)
    assert disabled.enabled is False
    enabled = service.set_game_enabled(game_id, True)
    assert enabled.enabled is True


def test_delete_game_removes_locations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(game_id, path=str(save), kind="directory")
    service.delete_game(game_id)
    assert service.list_games() == []
    with pytest.raises(ArchiveManagementError):
        service.list_locations(game_id)


def test_add_location_marks_first_as_primary(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    item_a = service.add_location(game_id, path=str(first), kind="directory")
    assert item_a.ok is True
    assert item_a.is_primary is True
    item_b = service.add_location(game_id, path=str(second), kind="directory")
    assert item_b.is_primary is False


def test_add_location_rejects_missing_path(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    with pytest.raises(ArchiveManagementError):
        service.add_location(game_id, path=str(tmp_path / "nope"), kind="directory")


def test_add_location_rejects_duplicate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(game_id, path=str(save), kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.add_location(game_id, path=str(save), kind="directory")


def test_set_primary_location_keeps_single_primary(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    item_a = service.add_location(game_id, path=str(first), kind="directory")
    item_b = service.add_location(game_id, path=str(second), kind="directory")
    service.set_primary_location(game_id, item_b.location_id)
    locations = service.list_locations(game_id)
    primary = [item for item in locations if item.is_primary]
    assert [item.location_id for item in primary] == [item_b.location_id]
    assert item_a.location_id != item_b.location_id


def test_update_location_changes_path(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    item = service.add_location(game_id, path=str(old), kind="directory")
    updated = service.update_location(item.location_id, path=str(new))
    assert updated.path == str(new)


def test_update_location_rejects_duplicate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first = service.add_location(game_id, path=str(one), kind="directory")
    service.add_location(game_id, path=str(two), kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.update_location(first.location_id, path=str(two))


def test_verify_location_reports_status(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    save = tmp_path / "save"
    save.mkdir()
    item = service.add_location(game_id, path=str(save), kind="directory")
    refreshed = service.verify_location(item.location_id)
    assert refreshed.ok is True


def test_no_backups_yet_and_phase_errors(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    assert service.list_backups(game_id) == []
    with pytest.raises(ArchiveManagementError):
        service.run_backup_now(game_id)
    with pytest.raises(ArchiveManagementError):
        service.run_export(game_id)


def test_unknown_game_operations_raise(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.get_detail("999")
    with pytest.raises(ArchiveManagementError):
        service.add_location("abc", path="/x", kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.delete_game("missing")


def test_task_status_reports_backup_root(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = service.task_status()
    assert status.running is False
    assert str(tmp_path / "backups") in status.target_label
