"""基于 SQLite 的真实后端单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest

import archive_management.application.locations as locations_mod
from archive_management.domain import Game, ScheduledJob
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    GameRepository,
    ScheduledJobRepository,
)
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.models import visible_in_branch_view
from archive_management.ui.sql_backend import SqlArchiveService

pytestmark = [
    pytest.mark.backend,
    pytest.mark.database,
    pytest.mark.critical,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("真实 SQLite 后端"),
    pytest.mark.story("备份恢复与删除数据流"),
    pytest.mark.layer("unit"),
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


# ----------------------------------------------------- 阶段 D: 备份/分支/调度


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


# --------------------------------------------- 阶段 D 迭代: 当前节点/删除/改名/保留


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


# ------------------------------------------------- 阶段 E: 恢复与删除原始位置


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
