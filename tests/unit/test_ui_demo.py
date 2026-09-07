"""演示后端单元测试(不依赖 Tkinter/文件系统)."""

from __future__ import annotations

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.ui.demo_backend import DemoArchiveService


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
