"""备份 → 恢复 → 回收站删除 → 定时备份的跨层协作.

这里走的是**真实协作链**: 真实 SQLite 数据库 + 真实文件系统 + 真实备份/恢复/
删除用例 + 真实审计日志 + 手动调度后端(不启动线程), 只有"系统回收站"用可注入
替身(真实的目录移动), 避免污染开发者机器的回收站。

覆盖 integration 应覆盖的场景: 成功、重复调用、重启后读取(定时任务
恢复)、部分资源不可用(危险路径被拒绝), 并用审计日志断言"操作可追溯"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import archive_management.application.locations as locations_mod
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    ScheduledJobRepository,
)
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.sql_backend import SqlArchiveService
from helpers import make_save_folder, migrated_database, touch_save

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("跨层协作"),
    pytest.mark.story("备份恢复删除与定时任务"),
    pytest.mark.layer("integration"),
]

_BACKUP_ROOT = "backups"


def _manual_scheduler() -> BackupScheduler:
    """不启动线程、可手动触发的调度器."""
    return BackupScheduler(backend=ManualBackend())


def _service(tmp_path: Path) -> tuple[Database, SqlArchiveService, BackupScheduler]:
    """构造真实后端 + 手动调度后端(不依赖真实时间轴)."""
    database = migrated_database(tmp_path)
    scheduler = _manual_scheduler()
    service = SqlArchiveService(
        database, backup_root=tmp_path / _BACKUP_ROOT, scheduler=scheduler
    )
    return database, service, scheduler


def test_backup_restore_and_schedule_flow(tmp_path: Path, audit_log: list[str]) -> None:
    """完整流程: 备份两份 → 恢复旧节点 → 配置并触发定时备份 → 重启后调度仍生效."""
    database, service, scheduler = _service(tmp_path)
    game_id = service.add_game("管道游戏").game_id
    # 定时备份只对启用的游戏生效(新建游戏默认停用), 这条流水线要验证调度本身,
    # 因此显式启用; 停用/归档的跳过行为另有用例覆盖.
    service.set_game_enabled(game_id, True)
    save = make_save_folder(tmp_path, content="state-v1")
    service.add_location(game_id, path=str(save), kind="directory")

    # 1) 手动备份两次(第二次前改动存档内容, 否则会按"内容未变化"跳过).
    service.run_backup_now(game_id)
    touch_save(tmp_path)
    service.run_backup_now(game_id)
    items = service.list_backups(game_id)
    assert len(items) == 2
    assert all(item.verified for item in items)
    # 按创建时间挑出最早的那一份(列表顺序由仓储决定, 不在这里假设).
    older = min(items, key=lambda item: item.created_dt)

    # 2) 先制造"尚未备份的新进度", 再恢复旧节点: 内容回退 + 生成"恢复前安全点".
    touch_save(tmp_path)
    plan = service.preview_restore(game_id, older.backup_id)
    assert plan.snapshot_ok is True
    assert plan.safety_point_available is True
    assert not plan.blocked_targets
    message = service.run_restore(
        game_id, older.backup_id, safety_point=True, force=True
    )
    assert "管道游戏" in message
    assert (save / "slot.dat").read_text(encoding="utf-8") == "state-v1"
    after_restore = service.list_backups(game_id)
    assert any(item.safety for item in after_restore)

    # 3) 定时备份: 配置 → 落库 → 手动触发 → 写入上次运行时间.
    status = service.set_schedule(game_id, "30m", keep_auto=2)
    assert status.schedule_text == "30m"
    jobs = ScheduledJobRepository(database).for_game(int(game_id))
    assert len(jobs) == 1
    assert jobs[0].schedule == "30m"

    touch_save(tmp_path, text="state-v3")
    assert scheduler.trigger(int(game_id)) is True
    runs = ScheduledJobRepository(database).for_game(int(game_id))
    assert runs[0].last_run_at is not None
    assert len(service.list_backups(game_id)) > len(after_restore)

    # 4) 重启后调度仍生效(新服务实例从数据库恢复定时任务).
    restarted = SqlArchiveService(
        Database(database.path),
        backup_root=tmp_path / _BACKUP_ROOT,
        scheduler=_manual_scheduler(),
    )
    row = next(item for item in restarted.list_schedules() if item.game_id == game_id)
    assert row.interval_text == "30m"
    assert row.has_locations is True
    assert restarted.task_status(game_id).schedule_enabled is True

    joined = " ".join(audit_log)
    assert "backup.create" in joined
    assert "restore.succeeded" in joined
    assert "schedule.set" in joined


def test_delete_save_location_moves_directory_to_injected_trash(
    tmp_path: Path, audit_log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除原始存档位置走"移入回收站"而不是永久删除, 并记入审计日志."""
    moved: list[str] = []
    trash = tmp_path / "trash"
    trash.mkdir()

    def fake_trash(path: str) -> None:
        target = trash / Path(path).name
        Path(path).rename(target)
        moved.append(str(target))

    _database, service, _scheduler = _service(tmp_path)
    game_id = service.add_game("删除游戏").game_id
    save = make_save_folder(tmp_path, content="state-0")
    location = service.add_location(game_id, path=str(save), kind="directory")

    plan = service.preview_location_removal(location.location_id)
    assert plan.blocked is False
    assert plan.files == 1

    monkeypatch.setattr(locations_mod, "send_to_trash", fake_trash)
    service.delete_save_location(location.location_id, confirm_name="删除游戏")

    assert not save.exists()
    assert moved == [str(trash / "save0")]
    assert service.list_locations(game_id) == []
    assert "location.delete" in " ".join(audit_log)


def test_dangerous_save_location_is_refused_without_touching_disk(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """危险路径(应用备份根目录)被拒绝: 既不移动目录也不删除记录."""
    _database, service, _scheduler = _service(tmp_path)
    game_id = service.add_game("危险游戏").game_id
    backups_root = tmp_path / _BACKUP_ROOT
    backups_root.mkdir(parents=True, exist_ok=True)
    location = service.add_location(game_id, path=str(backups_root), kind="directory")

    plan = service.preview_location_removal(location.location_id)
    assert plan.blocked is True

    with pytest.raises(ArchiveManagementError):
        service.delete_save_location(location.location_id, confirm_name="危险游戏")

    assert backups_root.is_dir()
    assert len(service.list_locations(game_id)) == 1
    assert "reason=protected" in " ".join(audit_log)


def test_backup_history_survives_node_delete_and_reopen(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """删除节点后磁盘与数据库一致, 并且重启后列表仍然一致."""
    database, service, _scheduler = _service(tmp_path)
    game_id = service.add_game("节点游戏").game_id
    save = make_save_folder(tmp_path, content="state-0")
    service.add_location(game_id, path=str(save), kind="directory")

    service.run_backup_now(game_id)
    touch_save(tmp_path)
    service.run_backup_now(game_id)
    items = service.list_backups(game_id)
    assert len(items) == 2

    service.run_delete_backup(game_id, items[0].backup_id)
    remaining = service.list_backups(game_id)
    assert len(remaining) == 1

    reopened = SqlArchiveService(
        Database(database.path), backup_root=tmp_path / _BACKUP_ROOT
    )
    assert len(reopened.list_backups(game_id)) == 1
    assert BackupRepository(database).count(int(game_id)) == 1
    assert "backup.delete" in " ".join(audit_log)
