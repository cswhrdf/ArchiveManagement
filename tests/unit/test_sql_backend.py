"""基于 SQLite 的真实后端单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest

import archive_management.application.locations as locations_mod
import helpers
from archive_management.domain import (
    ArtworkRef,
    Game,
    GameCandidate,
    HomeFilter,
    HomeView,
    PlatformGame,
    PlatformId,
    SavePathCandidate,
    ScheduledJob,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
    ScheduledJobRepository,
)
from archive_management.services.artwork import (
    ICON_SIZE,
    ICON_VERSION,
    STEAM_COVER_ASSET,
    artwork_cache_at,
    steam_cover,
    steam_icon,
)
from archive_management.services.game_names import NameFetcher, name_cache_at
from archive_management.services.platform_adapters import SaveCandidateSource
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.models import visible_in_branch_view
from archive_management.ui.sql_backend import SqlArchiveService

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

# 一款游戏的官方图标哈希(appinfo.vdf 的 clienticon).
_ICON_HASH = "b2f863a4c63bc1c5667a8a7e3e9355ef260ce6d2"

pytestmark = [
    pytest.mark.backend,
    pytest.mark.database,
    pytest.mark.critical,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("真实 SQLite 后端"),
    pytest.mark.story("备份恢复与删除数据流"),
    # 真实 SQLite + 文件系统 + 调度后端, 按层定义归入 integration.
    pytest.mark.layer("integration"),
]


def _service(tmp_path: Path) -> SqlArchiveService:
    database = Database(tmp_path / "app.db")
    database.migrate()
    # 用不启动线程的手动调度后端, 让测试不依赖真实时间轴.
    return SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )


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
    detail = service.get_detail(game_id)
    assert detail.name == "新名"
    # 重命名不改写"首次录入的名称", 界面据此展示额外的原始名称.
    assert detail.original_name == "旧名"
    assert detail.origin_label == "原始名称: 旧名"


def test_backup_uses_named_folder_and_shows_it(tmp_path: Path) -> None:
    """备份目录改用"名称 + 哈希", 并在详情里作为额外信息展示."""
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)

    detail = service.get_detail(game_id)
    assert detail.storage_folder.startswith("Demo-")
    assert detail.storage_folder != game_id
    assert detail.origin_label == f"备份目录: {detail.storage_folder}"
    snapshots = list(
        (tmp_path / "backups" / detail.storage_folder).glob("*/loc-0/*.dat")
    )
    assert [item.name for item in snapshots] == ["slot1.dat"]
    assert str(save) not in detail.storage_folder


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


def test_no_backups_yet_and_unavailable_actions_raise(tmp_path: Path) -> None:
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


# ----------------------------------------------------- 备份/分支/调度


def _advance(save: Path, text: str = "") -> None:
    """改动存档内容, 让下一次备份与当前节点不同(否则会被判为"未变化")."""
    target = save / "slot1.dat"
    previous = target.read_text(encoding="utf-8")
    target.write_text(text or f"{previous}+", encoding="utf-8")


def _service_with_save(
    tmp_path: Path, name: str = "Demo"
) -> tuple[SqlArchiveService, str, Path]:
    """构造带一个可用存档位置的游戏, 返回服务、游戏 id 与存档目录."""
    service = _service(tmp_path)
    game_id = service.add_game(name).game_id
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game_id, path=str(save), kind="directory")
    return service, game_id, save


def test_backup_now_creates_listable_node(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)

    message = service.run_backup_now(game_id)

    assert message
    items = service.list_backups(game_id)
    assert len(items) == 1
    assert items[0].verified is True
    assert items[0].depth == 0
    assert items[0].auto is False
    assert items[0].size_label != ""
    assert service.get_detail(game_id).last_backup_label != "—"
    assert service.list_games()[0].backup_count == 1


def test_backup_now_requires_locations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Empty").game_id
    with pytest.raises(ArchiveManagementError):
        service.run_backup_now(game_id)


def test_new_backup_gets_default_title_and_empty_summary(tmp_path: Path) -> None:
    """新建备份的标题列直接给出默认名称, 内容摘要默认为空."""
    service, game_id, _save = _service_with_save(tmp_path)

    service.run_backup_now(game_id)

    item = service.list_backups(game_id)[0]
    assert item.title == tr("backup.title_manual")
    assert item.display_title == tr("backup.title_manual")
    assert item.sub == ""


def test_created_backup_title_is_persisted(tmp_path: Path) -> None:
    """默认名称写进数据库, 而不是只在界面回退展示."""
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.repository import BackupRepository

    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)

    nodes = BackupRepository(Database(tmp_path / "app.db")).list_for_game(int(game_id))
    assert [node.title for node in nodes] == [tr("backup.title_manual")]
    assert [node.note for node in nodes] == [""]


def test_second_backup_becomes_child_node(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    _advance(save)
    service.run_backup_now(game_id)

    items = service.list_backups(game_id)
    assert len(items) == 2
    assert items[0].parent_id is None
    assert items[1].parent_id == items[0].backup_id
    assert items[1].depth == 1


def test_create_branch_marks_node(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)

    message = service.run_create_branch(game_id, base.backup_id, "黑棘")

    assert "黑棘" in message
    items = service.list_backups(game_id)
    assert len(items) == 2
    branch = items[-1]
    assert branch.is_branch is True
    assert branch.parent_id == base.backup_id
    assert "黑棘" in branch.branch_label


def test_create_branch_rejects_unknown_backup(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.run_create_branch(game_id, "9999", "分支")


def test_create_branch_rejects_blank_name(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    with pytest.raises(ArchiveManagementError):
        service.run_create_branch(game_id, base.backup_id, "   ")


def test_set_schedule_persists_and_reports(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)

    status = service.set_schedule(game_id, "30m")

    assert status.schedule_text == "30m"
    assert service.task_status(game_id).schedule_text == "30m"
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert [job.schedule for job in jobs] == ["30m"]


def test_set_schedule_rejects_invalid_interval(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.set_schedule(game_id, "abc")


def test_set_schedule_blank_clears_schedule(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_schedule(game_id, "1h")

    status = service.set_schedule(game_id, "")

    assert status.schedule_text == ""
    assert (
        ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
        == []
    )


def test_cancel_active_returns_false_when_idle(tmp_path: Path) -> None:
    service, _game_id, _save = _service_with_save(tmp_path)
    assert service.cancel_active() is False


def test_shutdown_releases_scheduler_idempotently(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_schedule(game_id, "30m")

    service.shutdown()
    service.shutdown()

    assert service.task_status(game_id).schedule_text == ""


def test_restore_requires_known_backup(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.run_restore(game_id, "9999")


def test_task_status_without_game_reports_paused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = service.task_status(None)
    assert status.running is False
    assert status.schedule_text == ""
    assert status.cancellable is False


# --------------------------------------------- 当前节点/删除/改名/保留份数


def _service_with_scheduler(
    tmp_path: Path,
) -> tuple[SqlArchiveService, str, ManualBackend]:
    """构造可手动触发的调度器, 返回服务、游戏 id 与手动调度后端."""
    backend = ManualBackend()
    database = Database(tmp_path / "app.db")
    database.migrate()
    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=backend),
    )
    game_id = service.add_game("Demo").game_id
    # 定时备份只对启用的游戏生效: 这些用例验证的是调度本身, 因此显式启用.
    service.set_game_enabled(game_id, True)
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game_id, path=str(save), kind="directory")
    return service, game_id, backend


def test_backup_marks_current_node_and_bumps_revision(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    before = service.task_status(game_id).revision

    service.run_backup_now(game_id)

    items = service.list_backups(game_id)
    assert [item.is_current for item in items] == [True]
    assert service.task_status(game_id).revision != before


def test_restore_moves_current_node_and_keeps_branch_label(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    first = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, first.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]

    service.run_restore(game_id, first.backup_id)

    items = {item.backup_id: item for item in service.list_backups(game_id)}
    assert items[first.backup_id].is_current is True
    assert items[branch.backup_id].is_current is False
    # 新备份从当前节点(而非末尾)继续.
    _advance(save, "restored")
    service.run_backup_now(game_id)
    created = [item for item in service.list_backups(game_id) if not item.is_branch]
    assert created[-1].parent_id == first.backup_id


def test_branch_node_carries_branch_name_for_inheritance(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)

    service.run_create_branch(game_id, base.backup_id, "黑棘")

    branch = service.list_backups(game_id)[-1]
    assert branch.branch_name == "黑棘"
    assert branch.title == "黑棘"


def test_rename_backup_updates_title_and_note(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    item = service.list_backups(game_id)[0]

    updated = service.rename_backup(
        game_id, item.backup_id, title="通关前", note="第一次通关前的存档"
    )

    assert updated.title == "通关前"
    assert updated.sub == "第一次通关前的存档"
    assert service.list_backups(game_id)[0].title == "通关前"


def test_rename_backup_rejects_too_long_note(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    item = service.list_backups(game_id)[0]
    with pytest.raises(ArchiveManagementError):
        service.rename_backup(game_id, item.backup_id, title="t", note="x" * 201)


def test_plan_delete_signals_confirmation_for_branch_root(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, base.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]
    # 分支上继续保存, 形成"分支根 + 其备份"的结构.
    _advance(save)
    service.run_backup_now(game_id)

    plan = service.plan_delete(game_id, branch.backup_id)

    assert plan.needs_confirmation is True
    assert plan.removed_count == 2
    assert service.plan_delete(game_id, base.backup_id).needs_confirmation is False


def test_plan_delete_branch_tip_is_plain_single_delete(tmp_path: Path) -> None:
    """分支末端没有后续备份时, 删除它不涉及其它节点."""
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, base.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]

    plan = service.plan_delete(game_id, branch.backup_id)

    assert plan.needs_confirmation is False
    assert plan.removed_count == 1


def test_run_delete_backup_shifts_later_nodes(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    _advance(save)
    service.run_backup_now(game_id)
    _advance(save)
    service.run_backup_now(game_id)
    middle = service.list_backups(game_id)[1]

    message = service.run_delete_backup(game_id, middle.backup_id)

    assert message
    remaining = service.list_backups(game_id)
    assert len(remaining) == 2
    assert remaining[1].parent_id == remaining[0].backup_id


def test_run_delete_backup_removes_branch_subtree(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, base.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]

    service.run_delete_backup(game_id, branch.backup_id)

    remaining = service.list_backups(game_id)
    assert [item.backup_id for item in remaining] == [base.backup_id]


def test_set_schedule_persists_keep_auto(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)

    status = service.set_schedule(game_id, "30m", keep_auto=5)

    assert status.keep_auto == 5
    assert "5" in status.task_name
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].keep_auto == 5


def test_scheduled_backup_creates_auto_node_and_prunes(
    tmp_path: Path,
) -> None:
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m", keep_auto=1)

    backend.trigger(f"backup-{game_id}")
    backend.trigger(f"backup-{game_id}")
    backend.trigger(f"backup-{game_id}")

    autos = [item for item in service.list_backups(game_id) if item.auto]
    assert len(autos) == 1
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_run_at is not None
    assert jobs[0].last_error is None


def test_schedules_are_restored_on_startup(tmp_path: Path) -> None:
    """重启后要能从数据库恢复已配置的定时任务(否则界面显示"未配置")."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m", keep_auto=4)

    # 模拟重启: 同一个数据库重新构造服务(新的调度器实例).
    restarted = SqlArchiveService(
        Database(tmp_path / "app.db"),
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    status = restarted.task_status(game_id)
    assert status.schedule_text == "30m"
    assert status.keep_auto == 4
    assert status.schedule_enabled is True
    item = next(i for i in restarted.list_schedules() if i.game_id == game_id)
    assert item.interval_text == "30m"
    assert item.state_label == "已启用"


def test_paused_schedule_survives_restart(tmp_path: Path) -> None:
    """暂停状态与周期配置也要在重启后保持(不会变成未配置)."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    assert service.set_schedule(game_id, "2h", enabled=False)

    restarted = SqlArchiveService(
        Database(tmp_path / "app.db"),
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    status = restarted.task_status(game_id)
    assert status.schedule_text == "2h"
    assert status.schedule_enabled is False


def test_unparsable_stored_schedule_is_ignored(tmp_path: Path) -> None:
    """数据库里的脏数据不应该阻止服务启动."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    game = GameRepository(database).add(Game(name="Demo"))
    assert game.id is not None
    ScheduledJobRepository(database).upsert(
        ScheduledJob(id=None, game_id=game.id, schedule="每个星期")
    )

    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    assert service.task_status(str(game.id)).schedule_text == ""
    assert service.list_schedules()


def test_schedule_requires_save_locations(tmp_path: Path) -> None:
    """未配置存档位置的游戏不能创建定时备份, 错误信息要说明原因."""
    service = _service(tmp_path)
    game_id = service.add_game("无位置").game_id

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_schedule(game_id, "30m")

    assert "存档位置" in str(excinfo.value)
    assert service.task_status(game_id).schedule_text == ""
    item = next(i for i in service.list_schedules() if i.game_id == game_id)
    assert item.can_schedule is False


def test_skipped_run_refreshes_next_run_and_revision(tmp_path: Path) -> None:
    """存档未变化被跳过时也要刷新下次运行时间与数据版本(界面需重读)."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    service.run_backup_now(game_id)
    before = service.task_status(game_id)

    backend.trigger(f"backup-{game_id}")

    after = service.task_status(game_id)
    assert after.revision != before.revision
    assert len(service.list_backups(game_id)) == 1
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_run_at is not None


def test_scheduled_backup_failure_is_recorded(tmp_path: Path) -> None:
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    save_dir = service.list_locations(game_id)[0].path
    path = Path(save_dir)
    for child in path.iterdir():
        child.unlink()
    path.rmdir()

    backend.trigger(f"backup-{game_id}")

    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_error is not None


def test_scheduled_backup_skips_unchanged_save(tmp_path: Path) -> None:
    """自动备份遇到"存档未变化"时静默跳过: 不新增节点, 也不报错."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    service.run_backup_now(game_id)
    before = len(service.list_backups(game_id))

    backend.trigger(f"backup-{game_id}")

    assert len(service.list_backups(game_id)) == before
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_error is None
    status = service.task_status(game_id)
    assert status.progress_label == ""


def test_storage_usage_counts_backup_storage(tmp_path: Path) -> None:
    """状态栏展示的"当前占用"来自备份存储的实际大小."""
    service, game_id, _save = _service_with_save(tmp_path)
    before = service.storage_usage()

    service.run_backup_now(game_id)

    after = service.storage_usage()
    assert after > before
    assert after > 0


def test_list_schedules_reports_all_games(tmp_path: Path) -> None:
    """全局任务列表包含每个游戏: 来源游戏、下次运行与已保留的自动备份份数."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    other = service.add_game("另一位玩家").game_id
    service.set_schedule(game_id, "30m", keep_auto=2)
    backend.trigger(f"backup-{game_id}")

    items = {item.game_id: item for item in service.list_schedules()}

    assert set(items) == {game_id, other}
    configured = items[game_id]
    assert configured.game_name == "Demo"
    assert configured.interval_text == "30m"
    assert configured.keep_auto == 2
    # ManifestBackend 没有时间轴(下次运行时间为 None), 因此文案回退到占位符;
    # 真实调度器会给出具体时间戳(见 test_pause_keeps_interval_configuration).
    assert configured.next_run_label
    assert configured.auto_count == 1
    assert configured.auto_count_label
    assert configured.state_label == "已启用"
    # 未配置定时备份的游戏也出现在列表里, 便于直接新增.
    idle = items[other]
    assert idle.interval_text == ""
    assert idle.state_label == "未配置"


def test_pause_keeps_interval_configuration(tmp_path: Path) -> None:
    """暂停只停触发, 不清空周期配置(否则再编辑会丢掉周期)."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)

    paused = service.set_schedule(game_id, "30m", enabled=False)

    assert paused.schedule_text == "30m"
    assert paused.schedule_enabled is False
    item = next(i for i in service.list_schedules() if i.game_id == game_id)
    assert item.interval_text == "30m"
    assert item.enabled is False
    assert item.state_label == "已暂停"
    assert item.next_run_label == "—"


def test_schedule_for_a_disabled_game_is_created_paused(tmp_path: Path) -> None:
    """停用中的游戏允许先配好周期, 但任务只能是暂停态."""
    service, game_id, _save = _service_with_save(tmp_path)

    status = service.set_schedule(game_id, "30m")

    assert status.schedule_text == "30m"
    assert status.schedule_enabled is False
    item = next(i for i in service.list_schedules() if i.game_id == game_id)
    assert item.state_label == "已暂停"
    assert item.can_enable is False
    assert item.can_toggle is False


def test_resuming_a_schedule_of_a_disabled_game_is_rejected(
    tmp_path: Path,
) -> None:
    """停用中的游戏不允许把已有任务切到启用态."""
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_schedule(game_id, "30m")

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_schedule(game_id, "30m", enabled=True)

    assert "停用" in str(excinfo.value)
    assert service.task_status(game_id).schedule_enabled is False


def test_disabling_a_game_pauses_its_schedule(tmp_path: Path) -> None:
    """停用游戏时把它已启用的定时任务置为暂停."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    assert service.task_status(game_id).schedule_enabled is True

    service.set_game_enabled(game_id, False)

    status = service.task_status(game_id)
    assert status.schedule_text == "30m"
    assert status.schedule_enabled is False


def test_scheduled_backup_skips_a_disabled_game(tmp_path: Path) -> None:
    """停用期间不执行自动备份(任务配置保留)."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    service.set_game_enabled(game_id, False)

    backend.trigger(f"backup-{game_id}")

    assert service.list_backups(game_id) == []
    assert service.task_status(game_id).schedule_text == "30m"


def test_archiving_disables_the_game_and_pauses_its_schedule(
    tmp_path: Path,
) -> None:
    """归档只保留删除/导出/取消归档/打开详情: 同时停用游戏并暂停定时备份."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")

    board = service.set_game_archived(game_id, True)

    assert board.stats.archived == 1
    # 归档游戏不出现在默认视图里, 因此摘要直接按 id 取完整列表.
    summary = next(game for game in service.list_games() if game.game_id == game_id)
    assert summary.archived is True
    assert summary.enabled is False
    assert service.task_status(game_id).schedule_enabled is False
    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_schedule(game_id, "30m", enabled=True)
    assert "已归档" in str(excinfo.value)


def test_delete_game_returns_its_candidate_to_pending(tmp_path: Path) -> None:
    """后端删除游戏时也要把探测候选退回待处理(界面入口的兼底)."""
    service = _service(tmp_path)
    install = tmp_path / "steam" / "Demo"
    install.mkdir(parents=True)
    database = Database(tmp_path / "app.db")
    candidate, _created = CandidateRepository(database).upsert(
        GameCandidate(name="Demo", install_dir=str(install), source="steam")
    )
    assert candidate.id is not None
    imported = service.import_candidate(str(candidate.id))

    service.delete_game(imported.game_id)

    released = CandidateRepository(database).get(candidate.id)
    assert released is not None
    assert released.status == "new"
    assert released.game_id is None


def test_enabling_a_game_disables_the_other_one(tmp_path: Path) -> None:
    """全局只允许一款游戏启用: 启用它时自动停用另一款."""
    service = _service(tmp_path)
    first = service.add_game("第一位").game_id
    second = service.add_game("第二位").game_id
    service.set_game_enabled(first, True)

    service.set_game_enabled(second, True)

    games = {game.game_id: game for game in service.list_games()}
    assert games[second].enabled is True
    assert games[first].enabled is False


def test_enabling_an_archived_game_is_rejected(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_game_archived(game_id, True)

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_game_enabled(game_id, True)

    assert "已归档" in str(excinfo.value)


class _FakeSaveSource:
    """固定的存档候选来源(避免用例去读真实 Steam 目录)."""

    def __init__(self, *paths: str) -> None:
        self._paths = paths

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        """返回构造时给定的候选路径."""
        return [
            SavePathCandidate(
                path=path, reason_code="steam_remotecache", detail="remotecache.vdf"
            )
            for path in self._paths
        ]


class _FakeAdapter:
    """测试替身适配器: 存档候选来自固定来源, 图片引用由调用方指定."""

    platform: PlatformId = "steam"
    supported: bool = True
    unsupported_reason: str = ""
    supports_save_paths: bool = True
    supports_artwork: bool = True

    def __init__(
        self, cloud: SaveCandidateSource, *, icon: ArtworkRef | None = None
    ) -> None:
        self._cloud = cloud
        self._icon = icon

    def list_games(self) -> list[PlatformGame]:
        """替身不提供游戏列表(用例自己造游戏记录)."""
        return []

    def save_candidates(self, game: PlatformGame) -> list[SavePathCandidate]:
        """存档候选来自注入的固定来源."""
        return self._cloud.candidates(game.game_id, install_dir=None)

    def artwork_refs(self, game: PlatformGame) -> tuple[ArtworkRef, ...]:
        """封面按公开 CDN 规则构造; 另可注入一份官方图标引用(与真实适配器一致)."""
        if not game.game_id:
            return ()
        refs = [steam_cover(game.game_id)]
        if self._icon is not None:
            refs.append(self._icon)
        return tuple(refs)


class _StubNames:
    """按 AppID 返回固定译名的替身(不联网)."""

    def __init__(self, names: dict[str, str]) -> None:
        """绑定 AppID 到译名的映射."""
        self._names = names

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """返回预设译名(没有映射时返回 None, 走原名回落)."""
        del language, timeout, max_bytes
        return self._names.get(app_id)


def _steam_service(
    tmp_path: Path,
    *paths: str,
    cache_dir: Path | None = None,
    name_fetcher: NameFetcher | None = None,
    icon: ArtworkRef | None = None,
) -> tuple[SqlArchiveService, str, Database]:
    """一个带 Steam AppID 的游戏 + 固定候选来源与替身适配器的服务实例."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    source = _FakeSaveSource(*paths)
    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        cache_dir=cache_dir,
        save_source=source,
        adapters={"steam": _FakeAdapter(source, icon=icon)},
        name_fetcher=name_fetcher,
    )
    game_id = service.add_game("Demo").game_id
    game = GameRepository(database).get(int(game_id))
    assert game is not None
    GameRepository(database).update(game.model_copy(update={"steam_app_id": 730}))
    # ``update`` 只写名称/Steam/平台/启用态, 来源要单独设(set_origin).
    GameRepository(database).set_origin(int(game_id), "steam")
    return service, game_id, database


def test_discovery_rows_carry_probed_save_paths(tmp_path: Path) -> None:
    """探测结果行里就带着该游戏的存档路径建议(只探测, 不写库)."""
    save = tmp_path / "saves"
    save.mkdir()
    (save / "slot.dat").write_text("x", encoding="utf-8")
    service, _game_id, database = _steam_service(tmp_path, str(save))
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    item = next(row for row in service.list_candidates() if row.name == "哈迪斯")

    assert item.save_supported is True
    assert [path.path for path in item.save_paths] == [str(save)]
    assert item.save_label == tr("discovery.save_found", count=1)
    # 只是建议: 存档位置仍然要等用户在导入对话框里确认.
    assert service.list_locations("1") == []


def test_discovery_rows_say_when_the_platform_is_unsupported(tmp_path: Path) -> None:
    """监控目录这类非平台来源不猜测存档路径, 行里明说需要手动添加."""
    service, _game_id, database = _steam_service(tmp_path)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(name="哈迪斯", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "哈迪斯")

    assert item.save_supported is False
    assert item.save_paths == ()
    assert item.save_label == tr(
        "discovery.save_unsupported", platform=item.source_label
    )


def test_artwork_path_reads_only_the_cache(tmp_path: Path) -> None:
    """封面/图标接口只查缓存: 预置缓存图能拿到路径, 未配置缓存目录时返回空."""
    cache = artwork_cache_at(tmp_path / "cache")
    cover = cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    icon = cache.store(
        "steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png"
    )
    service, game_id, _database = _steam_service(tmp_path, cache_dir=tmp_path / "cache")
    plain, plain_id, _other = _steam_service(tmp_path / "plain")

    assert service.artwork_path(game_id, "cover") == str(cover)
    assert service.artwork_path(game_id, "icon") == str(icon)
    assert plain.artwork_path(plain_id, "cover") == ""


def test_prefetch_names_localizes_only_unrenamed_games(tmp_path: Path) -> None:
    """译名只写到"没改过名"的游戏上: 用户改过的名字优先, 取不到就保留原名."""
    fetcher = _StubNames({"730": "无尽塔防 2", "1": "无名游戏"})
    service, game_id, database = _steam_service(tmp_path, name_fetcher=fetcher)
    service._localize_names(refresh=False)

    game = GameRepository(database).get(int(game_id))
    assert game is not None
    assert game.name == "无尽塔防 2"

    # 用户改过名(与首次录入的名称不同)的游戏不会再被译名覆盖.
    renamed = game.model_copy(update={"name": "我给它起的名字"})
    GameRepository(database).update(renamed)
    service._localize_names(refresh=False)

    stored = GameRepository(database).get(int(game_id))
    assert stored is not None
    assert stored.name == "我给它起的名字"


def test_prefetch_names_keeps_the_detected_name_without_a_translation(
    tmp_path: Path,
) -> None:
    """该语言没有译文(或取不到)时保留探测到的原名, 不猜也不翻."""
    service, game_id, database = _steam_service(
        tmp_path, name_fetcher=_StubNames({"999": "别的游戏"})
    )

    service._localize_names(refresh=False)

    game = GameRepository(database).get(int(game_id))
    assert game is not None
    assert game.name == "Demo"


def test_discovery_rows_read_the_localized_name_from_the_cache(
    tmp_path: Path,
) -> None:
    """探测结果行显示缓存里的当前语言译名(扫描时已补好), 没缓存就回落原名."""
    cache_dir = tmp_path / "cache"
    name_cache_at(cache_dir).put("1145360", "zh-CN", "哈迪斯")
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Hades",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    item = next(row for row in service.list_candidates() if row.name == "Hades")

    assert item.localized_name == "哈迪斯"
    assert item.display_name == "哈迪斯"


def test_discovery_rows_keep_the_detected_name_without_a_translation(
    tmp_path: Path,
) -> None:
    """没有译名(或不是平台清单来源)的候选直接显示探测到的名称."""
    service, _game_id, database = _steam_service(tmp_path)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(name="Hades", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "Hades")

    assert item.localized_name == ""
    assert item.display_name == "Hades"


def test_prefetch_names_fills_candidate_names_without_touching_games(
    tmp_path: Path,
) -> None:
    """探测结果还不是游戏: 译名只写进缓存, 不会凭空多出一条游戏记录."""
    cache_dir = tmp_path / "cache"
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        name_fetcher=_StubNames({"1145360": "哈迪斯"}),
    )
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Hades",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    service._localize_names(refresh=False)

    assert name_cache_at(cache_dir).get("1145360", "zh-CN") == "哈迪斯"
    assert [game.name for game in service.list_games()] == ["Demo"]


def test_delete_game_removes_its_artwork_cache(tmp_path: Path) -> None:
    """删游戏时把它探测到的封面/图标缓存一起删掉(缓存是可再生的派生数据)."""
    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    cache.store("steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png")
    service, game_id, _database = _steam_service(tmp_path, cache_dir=cache_dir)

    service.delete_game(game_id)

    assert cache.lookup_any("steam", "730", "cover") is None
    assert cache.lookup_any("steam", "730", "icon") is None


def test_icon_is_derived_from_the_cover(tmp_path: Path) -> None:
    """封面兜底: 平台给不出官方图标时, 从封面裁出方形图标."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=cover.read_bytes(),
        extension="png",
    )
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    # 封面已有(不计入), 这一份是刚裁出来的图标.
    assert service._fetch_artwork(cache, referenced) == 1

    icon = service.artwork_path("1", "icon")
    assert icon != ""
    with Image.open(icon) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)


def test_official_icon_wins_over_the_cover_crop(tmp_path: Path) -> None:
    """有官方图标时用它: 缓存里的像素来自 ico, 而不是封面(两者颜色不同)."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=cover.read_bytes(),
        extension="png",
    )
    official = helpers.write_steam_icon(tmp_path, _ICON_HASH)
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        icon=steam_icon("730", _ICON_HASH, local_path=official),
    )
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._fetch_artwork(cache, referenced) == 1

    stored = cache.lookup_any("steam", "730", "icon")
    assert stored is not None
    # 文件名说明这份图是怎么来的: 官方图标用哈希, 且已经归一化成方形 PNG.
    assert stored.name == f"icon-{_ICON_HASH}.png"
    with Image.open(stored) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)
        # 官方图标带透明通道: 颜色来自 ico(红)而不是封面(深蓝), 且透明度保留.
        assert made.getpixel((ICON_SIZE // 2, ICON_SIZE // 2)) == (200, 30, 30, 255)
    # 界面拿到的是缓存里那份成品(而不是平台目录里的原始 .ico).
    assert service.artwork_path("1", "icon") == str(stored)


def test_a_cover_crop_icon_is_replaced_by_the_official_one(tmp_path: Path) -> None:
    """已有裁出来的封面图标时, 拿到官方图标要覆盖它(否则升级后仍是旧图)."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    crop = tmp_path / "crop.png"
    Image.new("RGB", (ICON_SIZE, ICON_SIZE), (10, 20, 30)).save(crop)
    cache.store(
        "steam", "730", "icon", ICON_VERSION, content=crop.read_bytes(), extension="png"
    )
    # 封面也预置好: 这样这一轮要补的只有图标(返回值只数图标那 1 份).
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=crop.read_bytes(),
        extension="png",
    )
    official = helpers.write_steam_icon(tmp_path, _ICON_HASH)
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        icon=steam_icon("730", _ICON_HASH, local_path=official),
    )
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._fetch_artwork(cache, referenced) == 1

    # 只剩官方图标那一版: 旧的封面裁剪已按“同类型过期文件”清掉.
    assert cache.lookup("steam", "730", "icon", ICON_VERSION) is None
    stored = cache.lookup("steam", "730", "icon", _ICON_HASH)
    assert stored is not None
    with Image.open(stored) as made:
        assert made.getpixel((4, 4)) == (200, 30, 30, 255)


def test_import_candidate_writes_the_confirmed_save_paths(tmp_path: Path) -> None:
    """导入时把用户确认的路径写成存档位置: 平台候选保留来源, 新增的记手动."""
    save = tmp_path / "saves"
    save.mkdir()
    (save / "slot.dat").write_text("x", encoding="utf-8")
    extra = tmp_path / "extra"
    extra.mkdir()
    service, _game_id, database = _steam_service(tmp_path, str(save))
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    candidate, _created = CandidateRepository(database).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )
    assert candidate.id is not None

    summary = service.import_candidate(
        str(candidate.id), save_paths=(str(save), str(extra))
    )

    assert summary.saved_paths == 2
    game = GameRepository(database).get(int(summary.game_id))
    assert game is not None
    assert game.steam_app_id == 1145360
    sources = {
        item.path: item.source for item in service.list_locations(summary.game_id)
    }
    assert sources == {str(save): "steam", str(extra): "manual"}


# ------------------------------------------------- 恢复与删除原始位置


def test_preview_restore_reports_targets_and_extra_files(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "notes.txt").write_text("extra", encoding="utf-8")

    plan = service.preview_restore(game_id, node.backup_id)

    assert plan.snapshot_ok is True
    assert plan.file_count == 1
    assert [target.path for target in plan.targets] == [str(save)]
    assert plan.blocked_reason is None
    assert plan.safety_point_available is True


def test_preview_restore_rejects_unknown_backup(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.preview_restore(game_id, "9999")


def test_run_restore_writes_files_and_keeps_current_pointer(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "slot1.dat").write_text("changed", encoding="utf-8")

    message = service.run_restore(game_id, node.backup_id, safety_point=False)

    assert (save / "slot1.dat").read_text(encoding="utf-8") == "progress"
    assert "当前节点" in message
    items = {item.backup_id: item for item in service.list_backups(game_id)}
    assert items[node.backup_id].is_current is True


def test_run_restore_keeps_extra_files(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "notes.txt").write_text("extra", encoding="utf-8")

    message = service.run_restore(game_id, node.backup_id, safety_point=False)

    # 恢复是覆盖而不是镜像: 快照之外的文件保持原样.
    assert (save / "notes.txt").read_text(encoding="utf-8") == "extra"
    assert "1 个文件" in message


def test_run_restore_creates_safety_point_by_default(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "slot1.dat").write_text("changed", encoding="utf-8")

    service.run_restore(game_id, node.backup_id)

    items = service.list_backups(game_id)
    safety = [item for item in items if item.safety]
    assert [item.display_title for item in safety] == ["恢复前安全点"]
    # 安全点带 safety 标记: 只出现在时间线, 不占分支树的位置.
    assert safety[0].backup_id not in visible_in_branch_view(items)


def test_preview_location_removal_reports_impact(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]

    plan = service.preview_location_removal(location.location_id)

    assert plan.game_name == "Demo"
    assert plan.path == str(save)
    assert plan.files == 1
    assert plan.blocked_reason is None


def test_delete_save_location_requires_matching_name(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]

    with pytest.raises(ArchiveManagementError):
        service.delete_save_location(location.location_id, confirm_name="别的游戏")

    assert save.exists()
    assert len(service.list_locations(game_id)) == 1


def test_delete_save_location_uses_injected_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]
    moved: list[str] = []
    monkeypatch.setattr(locations_mod, "send_to_trash", moved.append)

    message = service.delete_save_location(location.location_id, confirm_name="demo")

    assert moved == [str(save)]
    assert str(save) in message
    assert service.list_locations(game_id) == []


# ------------------------------------------------- 本地游戏探测


def test_monitored_directories_crud(tmp_path: Path) -> None:
    """监控目录的增删改查都经后端暴露给界面, 并带实时路径状态."""
    service = _service(tmp_path)
    games = tmp_path / "Games"
    games.mkdir()

    created = service.add_monitored_directory(str(games), note="自定义")
    assert created.path == str(games)
    assert created.health == "ok"
    assert [item.directory_id for item in service.list_monitored_directories()] == [
        created.directory_id
    ]

    disabled = service.set_monitored_enabled(created.directory_id, False)
    assert disabled.enabled is False
    assert disabled.state_label == tr("discovery.dir_off")

    updated = service.update_monitored_directory(created.directory_id, note="改过")
    assert updated.note == "改过"

    service.remove_monitored_directory(created.directory_id)
    assert service.list_monitored_directories() == []


def test_scan_finds_monitored_games_and_imports_them(tmp_path: Path) -> None:
    """扫描会把监控目录里的游戏写进候选表, 导入后成为游戏记录."""
    service = _service(tmp_path)
    games = tmp_path / "Games"
    (games / "Hades").mkdir(parents=True)
    service.add_monitored_directory(str(games))

    report = service.scan_candidates()

    assert report.monitored == 1
    assert report.active == 1
    ours = next(
        item
        for item in service.list_candidates()
        if item.install_dir == str(games / "Hades")
    )
    assert ours.source == "monitored"
    assert ours.status == "new"
    assert ours.importable is True

    summary = service.import_candidate(ours.candidate_id, name="哈迪斯")

    assert summary.name == "哈迪斯"
    assert [game.game_id for game in service.list_games()] == [summary.game_id]
    imported = service.list_candidates(status="imported")
    assert [item.candidate_id for item in imported] == [ours.candidate_id]
    assert imported[0].game_id == summary.game_id
    # 已导入的候选不能再导入一次.
    with pytest.raises(ArchiveManagementError):
        service.import_candidate(ours.candidate_id)


def test_candidate_ignore_restore_and_relocate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    games = tmp_path / "Games"
    (games / "Hades").mkdir(parents=True)
    service.add_monitored_directory(str(games))
    service.scan_candidates()
    candidate = next(
        item
        for item in service.list_candidates()
        if item.install_dir == str(games / "Hades")
    )

    ignored = service.set_candidate_ignored(candidate.candidate_id, True)
    assert ignored.status == "ignored"
    # 用集合判断而不是取第一条: 扫描本机时也可能会带出别的已忽略候选
    # (例如平台官方工具被默认隐藏).
    ignored_ids = {
        item.candidate_id for item in service.list_candidates(status="ignored")
    }
    assert candidate.candidate_id in ignored_ids

    restored = service.set_candidate_ignored(candidate.candidate_id, False)
    assert restored.status == "new"

    moved = tmp_path / "Elsewhere" / "Hades"
    moved.mkdir(parents=True)
    relocated = service.relocate_candidate(candidate.candidate_id, str(moved))
    assert relocated.install_dir == str(moved)
    assert relocated.health == "ok"

    watched = service.add_candidate_as_monitored(candidate.candidate_id)
    assert watched.path == str(tmp_path / "Elsewhere")


def test_backend_rejects_unknown_discovery_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ArchiveManagementError, match="未知监控目录"):
        service.set_monitored_enabled("not-a-number", False)
    with pytest.raises(ArchiveManagementError, match="未知监控目录"):
        service.remove_monitored_directory("42")
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        service.import_candidate("42")
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        service.set_candidate_ignored("42", True)
    with pytest.raises(ArchiveManagementError, match="不支持的筛选条件"):
        service.list_candidates(status="bogus")


def test_home_board_lists_games_and_persists_filter(tmp_path: Path) -> None:
    """主页列表与筛选条件都要持久化: 重新打开仍是上次的视图."""
    service = _service(tmp_path)
    service.add_game("星际拓荒")
    service.add_game("空洞骑士")

    board = service.load_home()
    assert {item.name for item in board.games} == {"星际拓荒", "空洞骑士"}
    assert board.filter.view is HomeView.ALL
    # 刚录入的游戏算“最近活跃”, 但都还没有存档位置, 因此都是待处理.
    assert board.summary == tr("home.summary", total=2, recent=2, pending=2, archived=0)

    service.apply_home_filter(HomeFilter(view=HomeView.PENDING, search="拓荒"))

    reloaded = service.load_home()
    assert reloaded.filter.view is HomeView.PENDING
    assert reloaded.filter.search == "拓荒"
    assert [item.name for item in reloaded.games] == ["星际拓荒"]
    assert reloaded.origin_text == tr("home.origin_all")
    assert reloaded.category_text == tr("home.category_all")


def test_home_filter_options_carry_counts(tmp_path: Path) -> None:
    """平台与分类下拉框的每一项都要带数量, 避免点进空分类."""
    service = _service(tmp_path)
    service.add_game("星际拓荒")

    board = service.load_home()
    assert [option.key for option in board.views] == [view.value for view in HomeView]
    origins = {option.key: option.count for option in board.origins}
    assert origins == {"manual": 1}
    categories = {option.key: option.count for option in board.categories}
    assert categories["origin:manual"] == 1
    assert categories["backup:none"] == 1

    narrowed = service.apply_home_filter(HomeFilter(origin="manual"))
    assert narrowed.origin_text == f"{tr('discovery.source_manual')} (1)"
    assert narrowed.narrowing is True
    # 平台筛选不参与自身的计数: 否则下拉框里只会剩下当前平台.
    assert {option.key for option in narrowed.origins} == {"manual"}


def test_home_archive_and_tags_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")

    archived = service.set_game_archived(summary.game_id, True)
    assert archived.stats.archived == 1
    assert archived.games == ()

    restored = service.set_game_archived(summary.game_id, False)
    assert [item.name for item in restored.games] == ["星际拓荒"]

    tagged = service.set_game_tags(summary.game_id, [" 探索 ", "探索", "解谜"])
    assert tagged.games[0].tags == ("探索", "解谜")
    assert "探索" in tagged.games[0].chips

    # 中英文逗号一视同仁: 两种逗号都不会留在标签里, 也不会在往返后被拆成两个
    comma_ed = service.set_game_tags(
        summary.game_id,
        ["探索,解谜", "动作，冒险"],  # noqa: RUF001 - 全角逗号正是被测输入
    )
    assert comma_ed.games[0].tags == ("探索解谜", "动作冒险")
    assert service.load_home().games[0].tags == ("探索解谜", "动作冒险")


def test_home_reflects_backups_and_locations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(summary.game_id, path=str(save), kind="directory")

    board = service.load_home()
    item = board.games[0]
    assert item.location_count == 1
    assert item.backup_enabled is True
    assert item.last_backup_label == ""
    assert tr("home.last_backup_none") in item.summary
    assert tr("home.cat_backup_none") in item.chips
    assert board.detail == tr("home.detail", backed_up=0, risky=0)

    service.run_backup_now(summary.game_id)

    refreshed = service.load_home()
    assert refreshed.games[0].backup_count == 1
    assert refreshed.games[0].last_backup_label != ""
    assert tr("home.cat_backup_done") in refreshed.games[0].chips
    assert refreshed.stats.pending == 0
    assert refreshed.detail == tr("home.detail", backed_up=1, risky=0)


def test_home_marks_games_without_locations_as_pending(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add_game("还没配置")

    board = service.load_home()
    assert board.stats.pending == 1
    assert board.games[0].backup_enabled is False

    only_pending = service.apply_home_filter(HomeFilter(view=HomeView.PENDING))
    assert [item.name for item in only_pending.games] == ["还没配置"]
    # 刚录入的游戏同时属于“最近活跃”: 录入本身就算一次活动.
    recent = service.apply_home_filter(HomeFilter(view=HomeView.RECENT))
    assert [item.name for item in recent.games] == ["还没配置"]

    empty = service.apply_home_filter(HomeFilter(search="zzz"))
    assert empty.games == ()
    assert empty.narrowing is True
    assert empty.empty_message == tr("home.empty_filtered", total=1)
    assert empty.empty_hint == tr("home.empty_filtered_hint")


def test_home_rejects_unknown_game_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.set_game_archived("999", True)
    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.set_game_tags("not-a-number", ["x"])
