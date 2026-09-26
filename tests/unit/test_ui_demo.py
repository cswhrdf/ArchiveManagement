"""演示后端单元测试(不依赖 Tkinter/文件系统)."""

from __future__ import annotations

import posixpath
from pathlib import Path, PurePosixPath

import pytest

import archive_management.ui.demo_backend as demo_mod
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
    BatchGameInspection,
    BatchInspection,
    ImportInspection,
)
from archive_management.domain import HomeFilter, HomeView
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.services.export_format import ARCHIVE_SUFFIX
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.models import ImportChoice

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


def test_export_unknown_game_raises(
    service: DemoArchiveService, tmp_path: Path
) -> None:
    with pytest.raises(ArchiveManagementError):
        service.run_export("missing-game", str(tmp_path / "Demo.archive.zip"))


def test_create_branch_message_contains_name(service: DemoArchiveService) -> None:
    message = service.run_create_branch("outer-wilds", "b1", "分支X")
    assert "分支X" in message


def test_export_message_admits_it_wrote_nothing(
    service: DemoArchiveService, tmp_path: Path
) -> None:
    """演示后端不碰文件系统: 提示要说清楚没写文件, 而不是假装导出了包."""
    destination = tmp_path / "Demo.archive.zip"

    message = service.run_export("outer-wilds", str(destination))

    assert "星际拓荒" in message
    assert not destination.exists(), "演示后端不该真的写出导出包"


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


def test_demo_storage_folder_is_remembered_only_once(
    service: DemoArchiveService,
) -> None:
    """备份目录名在首次备份时确定: 第二次备份不该把它算成新目录."""
    service.run_backup_now("outer-wilds")
    first = service.get_detail("outer-wilds").storage_folder
    service.run_backup_now("outer-wilds")

    assert service.get_detail("outer-wilds").storage_folder == first


def test_demo_enable_is_refused_for_an_archived_game(
    service: DemoArchiveService,
) -> None:
    """归档会连带停用: 想再启用必须先取消归档(与真实后端同一口径)."""
    service.set_game_archived("outer-wilds", True)

    with pytest.raises(ArchiveManagementError):
        service.set_game_enabled("outer-wilds", True)


def test_demo_update_location_keeps_the_path_when_only_the_kind_changes(
    service: DemoArchiveService,
) -> None:
    """不传 ``path`` 时保持原路径(不能把 None 当成"新路径"去比重复)."""
    location = service.add_location(
        "outer-wilds", path="C:/demo/keep", kind="directory"
    )

    updated = service.update_location(location.location_id, kind="file")

    assert updated.path == location.path
    assert updated.path_kind == "file"


def test_demo_update_location_rejects_a_path_used_by_another_location(
    service: DemoArchiveService,
) -> None:
    """改成另一个位置已经在用的路径要被拦下, 否则两个位置指向同一份存档."""
    taken = service.list_locations("outer-wilds")[0]
    second = service.add_location(
        "outer-wilds", path="C:/demo/second", kind="directory"
    )

    with pytest.raises(ArchiveManagementError):
        service.update_location(second.location_id, path=taken.path)


def _posix_normalize(raw: str) -> str:
    """模拟 POSIX 上 ``normalize_path`` 的行为: ``C:/…`` 不是绝对路径, 会被拼上工作目录.

    用它把"只规范化一侧"的漏判搬到 Windows 上重现 —— 不必等 Linux 分片去发现。
    """
    base = PurePosixPath(raw)
    if not raw.startswith("/"):
        base = PurePosixPath("/work/ArchiveManagement") / raw
    return posixpath.normpath(str(base))


def test_demo_duplicate_locations_are_caught_for_unusual_path_forms(
    service: DemoArchiveService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """判重要按"两侧都规范化"比较: 只规范化输入侧会在 POSIX 上漏判(CI 实测过一次).

    实测(CI 的 Linux 分片): ``test_demo_update_location_rejects_a_path_used_by_another_location``
    报 ``DID NOT RAISE`` —— 演示数据里的路径是原样保存的展示字符串, 而输入侧会过一遍
    ``normalize_path``: 在 POSIX 上 ``D:\\Games\\…`` 属于相对路径, 会被拼上工作目录,
    两侧形态不同就永远比不出重复。Windows 上恰好因为 normpath 对这类路径幂等而看不出来,
    所以这里换成 POSIX 风格的替身, 让同一条规则在每个平台都跑一遍。
    """
    monkeypatch.setattr(demo_mod, "normalize_path", _posix_normalize)
    taken = service.list_locations("outer-wilds")[0]
    second = service.add_location(
        "outer-wilds", path="C:/demo/second", kind="directory"
    )

    # 新增与修改两条路径都要拦下: 否则两个位置会指向同一份存档.
    with pytest.raises(ArchiveManagementError):
        service.add_location("outer-wilds", path=taken.path, kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.update_location(second.location_id, path=taken.path)


def test_demo_removing_the_primary_location_promotes_the_next_one(
    service: DemoArchiveService,
) -> None:
    """删掉主位置后主标记交给下一个位置, 不能留下"没有主位置"的状态."""
    primary = service.list_locations("outer-wilds")[0]
    service.add_location("outer-wilds", path="C:/demo/promoted", kind="directory")
    assert primary.is_primary is True

    service.remove_location(primary.location_id)

    remaining = service.list_locations("outer-wilds")
    assert [item.is_primary for item in remaining] == [True]


def test_demo_deleting_the_primary_location_promotes_the_next_one(
    service: DemoArchiveService,
) -> None:
    """删除存档位置(移入回收站)这条路径同样要交接主标记."""
    primary = service.list_locations("outer-wilds")[0]
    service.add_location("outer-wilds", path="C:/demo/after-delete", kind="directory")

    service.delete_save_location(primary.location_id, confirm_name="星际拓荒")

    remaining = service.list_locations("outer-wilds")
    assert remaining[0].is_primary is True


def test_demo_primary_location_must_belong_to_the_game(
    service: DemoArchiveService,
) -> None:
    """把别的游戏的位置设成本游戏的主位置: 必须拒绝(否则会串改两款游戏)."""
    foreign = service.list_locations("shanhai")[0]

    with pytest.raises(ArchiveManagementError):
        service.set_primary_location("outer-wilds", foreign.location_id)


def test_demo_location_operations_reject_unknown_ids(
    service: DemoArchiveService,
) -> None:
    with pytest.raises(ArchiveManagementError):
        service.remove_location("missing-loc")

    with pytest.raises(ArchiveManagementError):
        service.delete_save_location("missing-loc", confirm_name="x")


def test_demo_delete_of_a_leaf_keeps_the_rest_of_the_tree(
    service: DemoArchiveService,
) -> None:
    """叶子节点没有"被顶替的子分支": 删除走到提前返回, 其余节点原样保留."""
    items = service.list_backups("outer-wilds")
    parents = {item.parent_id for item in items}
    leaf = next(item for item in items if item.backup_id not in parents)

    service.run_delete_backup("outer-wilds", leaf.backup_id)

    remaining = service.list_backups("outer-wilds")
    assert leaf.backup_id not in {item.backup_id for item in remaining}
    assert len(remaining) == len(items) - 1


def test_demo_restore_rejects_an_unknown_node(service: DemoArchiveService) -> None:
    with pytest.raises(ArchiveManagementError):
        service.run_restore("outer-wilds", "nope")


def test_demo_monitored_directory_must_be_unique_and_non_empty(
    service: DemoArchiveService,
) -> None:
    service.add_monitored_directory("C:/demo/monitored")

    with pytest.raises(ArchiveManagementError):
        service.add_monitored_directory("C:/demo/monitored")

    with pytest.raises(ArchiveManagementError):
        service.add_monitored_directory("   ")


def test_demo_candidate_operations_reject_unknown_ids(
    service: DemoArchiveService,
) -> None:
    with pytest.raises(ArchiveManagementError):
        service.set_candidate_ignored("missing-candidate", True)

    with pytest.raises(ArchiveManagementError):
        service.relocate_candidate("missing-candidate", "C:/demo/nope")


def test_demo_update_game_renames(service: DemoArchiveService) -> None:
    game_id = service.add_game("旧名").game_id
    assert service.update_game(game_id, "新名").name == "新名"
    assert service.get_detail(game_id).name == "新名"


def test_demo_delete_game_removes(service: DemoArchiveService, tmp_path: Path) -> None:
    game_id = service.add_game("临时").game_id
    destination = tmp_path / "Demo.archive.zip"

    service.delete_game(game_id, str(destination))

    assert game_id not in {game.game_id for game in service.list_games()}
    with pytest.raises(ArchiveManagementError):
        service.get_detail(game_id)
    assert not destination.exists(), "演示后端不该真的写出告别包"


def test_demo_delete_export_path_is_a_plausible_relative_path(
    service: DemoArchiveService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """演示后端只算出一条像样的路径(游戏名 + 时间戳 + 包后缀), 不碰文件系统."""
    monkeypatch.chdir(tmp_path)
    game_id = service.add_game("临时").game_id

    destination = Path(service.delete_export_path(game_id))

    assert destination.parent == Path("exports")
    assert destination.name.startswith("临时-")
    assert destination.name.endswith(ARCHIVE_SUFFIX)
    assert not destination.exists()
    assert not Path("exports").exists(), "只算路径的入口不该建目录"

    with pytest.raises(ArchiveManagementError):
        service.delete_export_path("missing-game")


def test_demo_delete_game_writes_nothing_to_disk(
    service: DemoArchiveService,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log: list[str],
) -> None:
    """删除时演示后端如实记一条"模拟导出", 目标路径上什么都不写(与 run_export 同口径)."""
    monkeypatch.chdir(tmp_path)
    game_id = service.add_game("临时").game_id
    destination = Path(service.delete_export_path(game_id))

    service.delete_game(game_id, str(destination))

    assert game_id not in {game.game_id for game in service.list_games()}
    assert not destination.exists(), "演示后端不该真的写出告别包"
    assert list(tmp_path.rglob("*")) == []
    assert any("game.delete_export" in line for line in audit_log)


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


# ---------------------------------------------------------------- 导入归档包


def test_demo_inspect_import_marks_the_fabricated_package(
    service: DemoArchiveService,
    tmp_path: Path,
) -> None:
    """演示后端不读磁盘: 编出来的"包"没有存档位置与节点, 名字也标成演示包."""
    inspection = service.inspect_import(str(tmp_path / "Demo.archive.zip"))

    assert inspection.path == tmp_path / "Demo.archive.zip"
    assert inspection.game_name == tr("demo.import_name", file="Demo.archive")
    assert inspection.backup_count == 0
    assert inspection.file_count == 0
    assert inspection.locations == ()
    assert inspection.matching_game_id is None


def test_demo_run_import_admits_it_wrote_nothing(
    service: DemoArchiveService,
    tmp_path: Path,
) -> None:
    """演示后端不碰数据库: 提示要说清什么都没写, 游戏与备份也不许变多."""
    inspection = service.inspect_import(str(tmp_path / "Demo.archive.zip"))
    before_games = [game.game_id for game in service.list_games()]
    before_backups = len(service.list_backups("outer-wilds"))

    created = service.run_import(
        inspection,
        strategy=STRATEGY_NEW,
        target_game_id=None,
        locations={0: "D:/saves"},
    )
    merged = service.run_import(
        inspection,
        strategy=STRATEGY_MERGE,
        target_game_id="outer-wilds",
        locations={0: "D:/saves"},
    )

    assert created == tr("result.import_simulated", name=inspection.game_name)
    assert merged == tr("result.import_simulated", name=inspection.game_name)
    assert [game.game_id for game in service.list_games()] == before_games
    assert len(service.list_backups("outer-wilds")) == before_backups


def test_demo_run_import_skip_reports_nothing_imported(
    service: DemoArchiveService,
    tmp_path: Path,
) -> None:
    """跳过: 与真实后端同一句话(跳过了多少份), 而不是"已模拟导入"."""
    inspection = service.inspect_import(str(tmp_path / "Demo.archive.zip"))

    message = service.run_import(
        inspection,
        strategy=STRATEGY_SKIP,
        target_game_id=None,
        locations={},
    )

    assert message == tr("result.import_skipped", name=inspection.game_name, skipped=0)


def test_demo_run_import_rejects_a_target_that_does_not_exist(
    service: DemoArchiveService,
    tmp_path: Path,
) -> None:
    """合并到一个不存在的游戏: 与真实后端一样报错, 不假装成功."""
    inspection = service.inspect_import(str(tmp_path / "Demo.archive.zip"))

    with pytest.raises(ArchiveManagementError):
        service.run_import(
            inspection,
            strategy=STRATEGY_MERGE,
            target_game_id="missing-game",
            locations={},
        )


def test_demo_run_import_rejects_an_unknown_strategy(
    service: DemoArchiveService,
    tmp_path: Path,
) -> None:
    """未知策略: 报错而不是默默按"模拟导入"收场."""
    inspection = service.inspect_import(str(tmp_path / "Demo.archive.zip"))

    with pytest.raises(ArchiveManagementError):
        service.run_import(
            inspection,
            strategy="nope",
            target_game_id=None,
            locations={},
        )


# ---------------------------------------------------------------- 批量导出/导入


def _batch_inspection(*names: str) -> BatchInspection:
    """一个演示用的批量体检结果(演示后端只用得上游戏数与条目名)."""
    return BatchInspection(
        path=Path("demo-batch.archive.zip"),
        games=tuple(
            BatchGameInspection(
                entry=f"demo-{index}.archive.zip",
                inspection=ImportInspection(
                    path=Path(f"demo-{index}.archive.zip"),
                    game_name=name,
                    steam_app_id=None,
                    platform="windows",
                    origin="manual",
                    tags=(),
                    locations=(),
                    nodes=(),
                    schedule=None,
                    matching_game_id=None,
                ),
            )
            for index, name in enumerate(names)
        ),
    )


def test_demo_run_export_batch_admits_it_wrote_nothing(
    service: DemoArchiveService,
    tmp_path: Path,
) -> None:
    """演示后端不写文件: 提示要说清这一批没有文件产生, 目标路径也不许出现."""
    game_ids = [game.game_id for game in service.list_games()][:2]
    destination = tmp_path / "batch.archive.zip"

    message = service.run_export_batch(game_ids, str(destination))

    assert message == tr("result.export_batch_simulated", count=2)
    assert destination.exists() is False


def test_demo_run_export_batch_rejects_an_empty_selection_and_unknown_games(
    service: DemoArchiveService,
    tmp_path: Path,
) -> None:
    """空选择与不存在的游戏: 与真实后端一样报错(不是"已模拟导出 0 款")."""
    with pytest.raises(ArchiveManagementError):
        service.run_export_batch([], str(tmp_path / "empty.archive.zip"))
    with pytest.raises(ArchiveManagementError):
        service.run_export_batch(
            ["missing-game"], str(tmp_path / "unknown.archive.zip")
        )


def test_demo_run_import_batch_admits_it_wrote_nothing(
    service: DemoArchiveService,
) -> None:
    """批量导入: 只数非跳过的款数, 游戏与备份一个都不许多."""
    batch = _batch_inspection("演示甲", "演示乙")
    before_games = [game.game_id for game in service.list_games()]
    before_backups = len(service.list_backups("outer-wilds"))

    message = service.run_import_batch(
        batch,
        {
            "demo-0.archive.zip": ImportChoice(
                strategy=STRATEGY_NEW, target_game_id=None, locations={0: "D:/saves"}
            ),
            "demo-1.archive.zip": ImportChoice(
                strategy=STRATEGY_SKIP, target_game_id=None, locations={}
            ),
        },
    )

    assert message == tr("result.import_batch_simulated", count=1)
    assert [game.game_id for game in service.list_games()] == before_games
    assert len(service.list_backups("outer-wilds")) == before_backups


def test_demo_run_import_batch_skip_reports_nothing_imported(
    service: DemoArchiveService,
) -> None:
    """整批都选"跳过": 一句话说清什么都没导入, 而不是"已模拟导入 2 款"."""
    batch = _batch_inspection("演示甲", "演示乙")
    skips = {
        item.entry: ImportChoice(
            strategy=STRATEGY_SKIP, target_game_id=None, locations={}
        )
        for item in batch.games
    }

    message = service.run_import_batch(batch, skips)

    assert message == tr("result.import_batch_all_skipped", games=2)


def test_demo_run_import_batch_rejects_unknown_strategy_target_and_empty_batch(
    service: DemoArchiveService,
) -> None:
    """未知策略/不存在的目标游戏/空包都要报错, 不假装成功."""
    batch = _batch_inspection("演示甲")

    with pytest.raises(ArchiveManagementError):
        service.run_import_batch(
            batch,
            {
                "demo-0.archive.zip": ImportChoice(
                    strategy="nope", target_game_id=None, locations={}
                )
            },
        )
    with pytest.raises(ArchiveManagementError):
        service.run_import_batch(
            batch,
            {
                "demo-0.archive.zip": ImportChoice(
                    strategy=STRATEGY_MERGE,
                    target_game_id="missing-game",
                    locations={},
                )
            },
        )
    with pytest.raises(ArchiveManagementError):
        service.run_import_batch(_batch_inspection(), {})
