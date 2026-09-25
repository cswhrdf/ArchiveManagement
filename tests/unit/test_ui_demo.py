"""演示后端单元测试(不依赖 Tkinter/文件系统)."""

from __future__ import annotations

import pytest

from archive_management.domain import HomeFilter, HomeView
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.ui.demo_backend import DemoArchiveService

pytestmark = [
    pytest.mark.backend,
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("演示后端"),
    pytest.mark.story("演示数据后端"),
    pytest.mark.layer("unit"),
]


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


def test_restore_success_moves_current_node(service: DemoArchiveService) -> None:
    message = service.run_restore("outer-wilds", "b1")
    assert "当前节点" in message
    current = [item for item in service.list_backups("outer-wilds") if item.is_current]
    assert [item.backup_id for item in current] == ["b1"]


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
    status = service.task_status("outer-wilds")
    assert status.running is False
    assert status.theme_name == "light"
    assert status.schedule_text == "1d"


def test_schedules_are_configured_per_game(service: DemoArchiveService) -> None:
    """每个游戏的定时备份配置互不影响."""
    service.set_schedule("shanhai", "5m", keep_auto=2)

    changed = service.task_status("shanhai")
    untouched = service.task_status("outer-wilds")

    assert changed.schedule_text == "5m"
    assert changed.keep_auto == 2
    assert untouched.schedule_text == "1d"


def test_list_schedules_covers_every_game(service: DemoArchiveService) -> None:
    items = service.list_schedules()

    assert [item.game_name for item in items] == ["星际拓荒", "山海旅人", "无尽太空"]
    first = items[0]
    assert first.interval_text == "1d"
    assert first.enabled is True
    assert first.auto_count_label
    assert "来自游戏" in first.game_label
    # 未配置的游戏中显示为未配置.
    assert items[-1].state_label == "未配置"


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


def test_home_board_lists_demo_games_with_filters(
    service: DemoArchiveService,
) -> None:
    """演示主页也要给出视图、平台、分类与统计(界面在演示模式下同样可用)."""
    board = service.load_home()

    assert {item.game_id for item in board.games} == {
        "outer-wilds",
        "shanhai",
        "endless-space",
    }
    assert board.stats.total == 3
    assert [option.key for option in board.views] == [view.value for view in HomeView]
    origins = {option.key for option in board.origins}
    # 星际拓荒来自 Steam 探测, 另外两款是手动录入.
    assert origins == {"steam", "manual"}
    assert "backup:done" in {option.key for option in board.categories}

    narrowed = service.apply_home_filter(HomeFilter(search="山海"))
    assert [item.name for item in narrowed.games] == ["山海旅人"]
    assert narrowed.filter.search == "山海"


def test_home_board_archive_tags_and_backup(
    service: DemoArchiveService,
) -> None:
    archived = service.set_game_archived("endless-space", True)
    assert archived.stats.archived == 1
    assert "endless-space" not in {item.game_id for item in archived.games}

    restored = service.set_game_archived("endless-space", False)
    assert "endless-space" in {item.game_id for item in restored.games}

    tagged = service.set_game_tags("shanhai", [" 解谜 ", "解谜"])
    item = next(entry for entry in tagged.games if entry.game_id == "shanhai")
    assert item.tags == ("解谜",)
    assert "解谜" in item.chips

    before = item.backup_count
    after = service.run_backup_now("shanhai")
    assert "山海旅人" in after
    refreshed = service.load_home()
    updated = next(entry for entry in refreshed.games if entry.game_id == "shanhai")
    assert updated.backup_count == before + 1
    assert updated.last_backup_label != ""


def test_home_board_rejects_unknown_game(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.set_game_archived("missing-game", True)
    with pytest.raises(ArchiveManagementError):
        service.set_game_tags("missing-game", ["x"])


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


def test_demo_schedule_on_archived_game_is_refused(service: DemoArchiveService) -> None:
    """归档等价于"不再配置备份": 连定时周期都不许设(与真实后端同一口径)."""
    service.set_game_archived("outer-wilds", True)

    with pytest.raises(ArchiveManagementError):
        service.set_schedule("outer-wilds", "1h")


def test_demo_schedule_on_a_disabled_game_stays_paused(
    service: DemoArchiveService,
) -> None:
    """停用中的游戏可以先把周期配好, 但任务只能是暂停状态."""
    # 演示数据里这款游戏本来就配了定时任务(已存在的任务不允许被"继续"), 先清掉。
    service.set_schedule("outer-wilds", "")
    service.set_game_enabled("outer-wilds", False)

    status = service.set_schedule("outer-wilds", "1h")

    assert status.schedule_text == "1h"
    assert status.schedule_enabled is False
    # 已经有任务时再"配置"一次就等于要它跑起来 —— 停用中的游戏不允许, 必须明确拒绝.
    with pytest.raises(ArchiveManagementError):
        service.set_schedule("outer-wilds", "1h")


def test_demo_empty_schedule_removes_the_task(service: DemoArchiveService) -> None:
    """周期留空 = 删除任务(不只是暂停)."""
    service.set_schedule("outer-wilds", "1h")

    status = service.set_schedule("outer-wilds", "   ")

    assert status.schedule_text == ""


def test_demo_rename_backup_rejects_unknown_and_long_note(
    service: DemoArchiveService,
) -> None:
    """改备份信息: 未知节点与超长描述都要明确报错, 而不是静默写库."""
    with pytest.raises(ArchiveManagementError):
        service.rename_backup("outer-wilds", "nope", title="x", note="")

    with pytest.raises(ArchiveManagementError):
        service.rename_backup("outer-wilds", "b1", title="x", note="字" * 400)


def test_demo_preview_restore_rejects_unknown_backup(
    service: DemoArchiveService,
) -> None:
    with pytest.raises(ArchiveManagementError):
        service.preview_restore("outer-wilds", "nope")


def test_demo_theme_cancel_and_shutdown_are_harmless(
    service: DemoArchiveService,
) -> None:
    """演示后端没有后台任务: 取消与释放都不做事, 主题只归一到深/浅两档."""
    assert service.cancel_active() is False
    service.shutdown()
    assert service.set_theme("dark") == "dark"
    assert service.set_theme("light") == "light"
    assert service.set_theme("莫名其妙") == "light"


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


# ------------------------------------------------- 恢复与删除原始位置


def test_demo_preview_restore_describes_plan(service: DemoArchiveService) -> None:
    plan = service.preview_restore("outer-wilds", "b1")

    assert plan.snapshot_ok is True
    assert plan.title == "离开量子月亮前"
    assert [target.path for target in plan.targets] == [r"D:\Games\OuterWilds\save"]
    assert plan.safety_point_available is True


def test_demo_preview_restore_flags_running_process(
    service: DemoArchiveService,
) -> None:
    plan = service.preview_restore("outer-wilds", "b2")

    assert plan.process.running is True


def test_demo_run_restore_accepts_restore_options(
    service: DemoArchiveService,
) -> None:
    message = service.run_restore("outer-wilds", "b1", safety_point=False, force=True)

    assert "当前节点" in message


def test_demo_run_restore_adds_safety_point_to_timeline(
    service: DemoArchiveService,
) -> None:
    from archive_management.ui.models import visible_in_branch_view

    service.run_restore("outer-wilds", "b1", safety_point=True)

    items = service.list_backups("outer-wilds")
    safety = [item for item in items if item.safety]
    assert len(safety) == 1
    assert safety[0].kind_label == tr("backup.kind_safety")
    # 安全点不进入分支树.
    assert safety[0].backup_id not in visible_in_branch_view(items)


def test_demo_preview_location_removal_reports_impact(
    service: DemoArchiveService,
) -> None:
    location = service.list_locations("outer-wilds")[0]

    plan = service.preview_location_removal(location.location_id)

    assert plan.game_name == "星际拓荒"
    assert plan.path == location.path
    assert plan.files > 0
    assert plan.blocked is False


def test_demo_delete_save_location_requires_confirm_name(
    service: DemoArchiveService,
) -> None:
    location = service.list_locations("outer-wilds")[0]

    with pytest.raises(ArchiveManagementError):
        service.delete_save_location(location.location_id, confirm_name="错的")

    assert [item.location_id for item in service.list_locations("outer-wilds")] == [
        location.location_id
    ]


def test_demo_delete_save_location_removes_location(
    service: DemoArchiveService,
) -> None:
    location = service.list_locations("outer-wilds")[0]

    message = service.delete_save_location(
        location.location_id, confirm_name="星际拓荒"
    )

    assert location.path in message
    assert service.list_locations("outer-wilds") == []
    assert service.list_games()[0].has_locations is False
