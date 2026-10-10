"""
定时备份: 间隔/保留数持久化、启动恢复、暂停与禁用联动、跳过未变更、失败记录与任务边角静默。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.domain import (
    Game,
    GameCandidate,
    ScheduledJob,
)
from archive_management.exceptions import (
    ArchiveManagementError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
    ScheduledJobRepository,
)
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.sql_backend import SqlArchiveService

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _service,
    _service_with_save,
)

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
    # ManifestBackend 没有时间轴(下次运行时间为 None): 标签本身是空串, 但给用户看的
    # 文案必须落到"未安排"这种能读的说法上, 而不是一个像加载失败的短横线; 真实调度器
    # 会给出具体时间戳(见 test_pause_keeps_interval_configuration).
    assert configured.next_run_label == ""
    assert configured.next_run_text == tr("schedule.next_run_none")
    assert configured.auto_count == 1
    assert configured.auto_count_label
    assert configured.state_label == "已启用"
    # 未配置定时备份的游戏也出现在列表里, 便于直接新增.
    idle = items[other]
    assert idle.interval_text == ""
    assert idle.state_label == "未配置"


def test_detail_marks_a_paused_schedule_as_paused_not_missing(tmp_path: Path) -> None:
    """配了周期但没启用时, 详情页不能说"未配置"(用户反馈: 会产生误解)."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m", enabled=False)

    paused = service.get_detail(game_id)

    assert paused.next_backup_label == ""
    assert paused.next_backup_paused is True
    assert paused.next_backup_text == tr("hero.next_paused")

    # 启用之后回到"有时间戳"的正常路径, 不再报暂停.
    service.set_schedule(game_id, "30m")
    enabled = service.get_detail(game_id)
    assert enabled.next_backup_paused is False
    assert enabled.next_backup_text != tr("hero.next_paused")


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
    # 暂停后没有排期: 标签为空, 展示文案给出"未安排", 而不是一个孤零零的破折号.
    assert item.next_run_label == ""
    assert item.next_run_text == tr("schedule.next_run_none")


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

    service.delete_game(imported.game_id, str(tmp_path / "exports" / "imported.zip"))

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


def test_startup_skips_schedule_rows_without_a_game_or_an_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库里有"没有游戏 id"或"空周期"的任务行时启动照常(只跳过这两行)."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    rows = [
        ScheduledJob(game_id=None, schedule="1h"),
        ScheduledJob(game_id=1, schedule="   "),
    ]
    monkeypatch.setattr(ScheduledJobRepository, "list_all", lambda self: list(rows))

    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    assert service.list_schedules() == []


def test_list_schedules_skips_games_it_cannot_describe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """组装不出任务条目的游戏直接从列表里跳过(不让半截条目进界面)."""
    service = _service(tmp_path)
    service.add_game("甲")
    service.add_game("乙")
    describe = service._schedule_item
    monkeypatch.setattr(
        service,
        "_schedule_item",
        lambda game: None if game.name == "乙" else describe(game),
    )

    assert [item.game_name for item in service.list_schedules()] == ["甲"]


def test_clearing_a_schedule_ignores_jobs_without_an_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库里出现没有 id 的任务行时清周期照常走完(跳过删除而不是崩)."""
    service, game_id, _save = _service_with_save(tmp_path)
    monkeypatch.setattr(
        service._jobs,
        "for_game",
        lambda gid: [ScheduledJob(game_id=gid, schedule="1h")],
    )

    status = service.set_schedule(game_id, "")

    assert status.schedule_text == ""


def test_marking_a_job_run_without_a_stored_job_is_silent(tmp_path: Path) -> None:
    """没有任务行时"记录一次运行"直接返回(自动备份可能在清掉周期之后收尾)."""
    service, game_id, _save = _service_with_save(tmp_path)

    service._mark_job_run(int(game_id), error=None)

    assert service.task_status(game_id).schedule_text == ""
