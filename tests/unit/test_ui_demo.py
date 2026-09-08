"""演示后端单元测试(不依赖 Tkinter/文件系统)."""

from __future__ import annotations

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.ui.demo_backend import DemoArchiveService

pytestmark = [pytest.mark.backend, pytest.mark.ui, pytest.mark.critical]


@pytest.fixture
def service() -> DemoArchiveService:
    return DemoArchiveService(delay=0)


def test_list_games(service: DemoArchiveService) -> None:
    games = service.list_games()
    assert len(games) == 3
    assert {game.game_id for game in games} == {
        "outer-wilds",
        "shanhai",
        "endless-space",
    }


def test_backups_exist_for_outer_wilds(service: DemoArchiveService) -> None:
    items = service.list_backups("outer-wilds")
    assert len(items) >= 4
    assert {item.backup_id for item in items} >= {"b1", "b3", "b4"}


def test_endless_space_has_no_backups(service: DemoArchiveService) -> None:
    assert service.list_backups("endless-space") == []


def test_backup_without_locations_raises(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.run_backup_now("endless-space")


def test_restore_of_failing_node_raises(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.run_restore("outer-wilds", "b2")


def test_restore_success(service: DemoArchiveService) -> None:
    message = service.run_restore("outer-wilds", "b1")
    assert "已恢复" in message


def test_backup_creates_new_node(service: DemoArchiveService) -> None:
    before = len(service.list_backups("outer-wilds"))
    service.run_backup_now("outer-wilds")
    after = len(service.list_backups("outer-wilds"))
    assert after == before + 1


def test_theme_cycle(service: DemoArchiveService) -> None:
    assert service.current_theme() == "dark"
    service.set_theme("light")
    assert service.current_theme() == "light"
    assert service.set_theme("anything") == "light"


def test_task_status_reports_theme(service: DemoArchiveService) -> None:
    service.set_theme("light")
    status = service.task_status()
    assert status.running is True
    assert status.theme_name == "light"


def test_get_detail_unknown_game_raises(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.get_detail("missing-game")


def test_backup_unknown_game_raises(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.run_backup_now("missing-game")


def test_restore_unknown_game_raises(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.run_restore("missing-game", "b1")


def test_export_unknown_game_raises(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.run_export("missing-game")


def test_create_branch_message_contains_name(service: DemoArchiveService) -> None:
    message = service.run_create_branch("outer-wilds", "b1", "分支X")
    assert "分支X" in message


def test_export_message_contains_game_name(service: DemoArchiveService) -> None:
    message = service.run_export("outer-wilds")
    assert "星际拓荒" in message


def test_simulate_delay_sleeps_when_configured() -> None:
    slow = DemoArchiveService(delay=0.001)
    slow.run_backup_now("outer-wilds")
    assert slow.list_backups("outer-wilds")


def test_demo_add_game_appears_in_list(service: DemoArchiveService) -> None:
    summary = service.add_game("新游戏")
    games = service.list_games()
    assert any(game.game_id == summary.game_id for game in games)
    assert service.get_detail(summary.game_id).name == "新游戏"
    assert service.list_locations(summary.game_id) == []


def test_demo_add_game_rejects_blank_name(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.add_game("   ")


def test_demo_update_game_renames(service: DemoArchiveService) -> None:
    game_id = service.add_game("旧名").game_id
    assert service.update_game(game_id, "新名").name == "新名"
    assert service.get_detail(game_id).name == "新名"


def test_demo_delete_game_removes(service: DemoArchiveService) -> None:
    game_id = service.add_game("临时").game_id
    service.delete_game(game_id)
    assert game_id not in {game.game_id for game in service.list_games()}
    with pytest.raises(ArchiveManagementError):
        service.get_detail(game_id)


def test_demo_set_game_enabled(service: DemoArchiveService) -> None:
    game_id = service.add_game("Demo").game_id
    assert service.set_game_enabled(game_id, False).enabled is False
    games = service.list_games()
    disabled = next(game for game in games if game.game_id == game_id)
    assert disabled.enabled is False
    assert disabled.list_detail == tr("game.disabled_short")


def test_demo_add_location_first_is_primary(service: DemoArchiveService) -> None:
    game_id = service.add_game("Demo").game_id
    first = service.add_location(game_id, path="/virtual/save-a", kind="directory")
    second = service.add_location(game_id, path="/virtual/save-b", kind="directory")
    assert first.is_primary is True
    assert second.is_primary is False
    assert len(service.list_locations(game_id)) == 2


def test_demo_add_location_rejects_duplicate(service: DemoArchiveService) -> None:
    game_id = service.add_game("Demo").game_id
    service.add_location(game_id, path="/virtual/save", kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.add_location(game_id, path="/virtual/save", kind="directory")


def test_demo_set_primary_location(service: DemoArchiveService) -> None:
    game_id = service.add_game("Demo").game_id
    first = service.add_location(game_id, path="/virtual/a", kind="directory")
    second = service.add_location(game_id, path="/virtual/b", kind="directory")
    service.set_primary_location(game_id, second.location_id)
    primary = [item for item in service.list_locations(game_id) if item.is_primary]
    assert [item.location_id for item in primary] == [second.location_id]
    assert first.location_id != second.location_id


def test_demo_remove_location_promotes_remaining(service: DemoArchiveService) -> None:
    game_id = service.add_game("Demo").game_id
    first = service.add_location(game_id, path="/virtual/a", kind="directory")
    service.add_location(game_id, path="/virtual/b", kind="directory")
    service.remove_location(first.location_id)
    remaining = service.list_locations(game_id)
    assert len(remaining) == 1
    assert remaining[0].is_primary is True


def test_demo_verify_location(service: DemoArchiveService) -> None:
    game_id = service.add_game("Demo").game_id
    item = service.add_location(game_id, path="/virtual/a", kind="directory")
    assert service.verify_location(item.location_id).ok is True


def test_demo_live_summary_tracks_locations(service: DemoArchiveService) -> None:
    game_id = service.add_game("Demo").game_id
    before = next(game for game in service.list_games() if game.game_id == game_id)
    assert before.has_locations is False
    service.add_location(game_id, path="/virtual/a", kind="directory")
    after = next(game for game in service.list_games() if game.game_id == game_id)
    assert after.has_locations is True
    assert after.location_count == 1
