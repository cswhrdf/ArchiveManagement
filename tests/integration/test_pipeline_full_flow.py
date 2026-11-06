"""监控目录发现 → 导入 → 备份 → 分支 → 恢复 → 导出 → 新库导入 → 识别启动 → 归档 → 回收站删除.

各段单独都有 pipeline 覆盖(导出、导入、恢复边界、存储……), 但**跨段交接**只有串起来
才看得见: 导入来的游戏能不能直接进备份; 探测结果与游戏记录的名字/路径口径是否一致;
归档之后"删除原始目录"是不是还按 `domain/game_rules.py` 走。

CI 上没有安装任何平台客户端, 所以主场景走**监控目录**: 探测用的根目录由 `roots` 注入到
`tmp_path`(否则 `scan_candidates` 会读开发机上真实的 Steam/Epic 清单), 回收站与进程表
各用替身。用例自带数据、不依赖执行顺序, 也不断言平台差异(Windows 会多播种一个 exe
候选, Linux/macOS 只播种目录名)。
"""

from __future__ import annotations

from pathlib import Path
from typing import get_args

import pytest

import archive_management.application.locations as locations_mod
from archive_management.application.imports import ImportInspection
from archive_management.domain import ARCHIVED_ACTIONS, GameAction
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.models import GameSummary
from archive_management.ui.sql_backend import SqlArchiveService
from helpers import migrated_database, scan_roots

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("跨层协作"),
    pytest.mark.story("监控目录发现的游戏走完整流程"),
    pytest.mark.layer("integration"),
]

_GAME = "MyGame"


def _service(
    tmp_path: Path, running: list[str]
) -> tuple[SqlArchiveService, BackupScheduler]:
    """真实 SQLite + 手动调度 + 注入的探测根目录与进程表(不碰本机任何平台目录)."""
    scheduler = BackupScheduler(backend=ManualBackend())
    service = SqlArchiveService(
        migrated_database(tmp_path),
        backup_root=tmp_path / "backups",
        scheduler=scheduler,
        roots=scan_roots(tmp_path),
        process_provider=lambda: list(running),
    )
    return service, scheduler


def _program(tmp_path: Path) -> Path:
    """监控目录里的"一个程序": 安装目录 + 里面的一小份存档, 返回存档目录."""
    install = tmp_path / "Games" / _GAME
    save = install / "saves"
    save.mkdir(parents=True)
    # 真实游戏目录里通常有主程序; 用例只在 Windows 上会把它当进程候选, 这里不断言它.
    (install / f"{_GAME}.exe").write_bytes(b"MZ")
    (save / "slot.dat").write_text("state-v1", encoding="utf-8")
    return save


def _by_name(service: SqlArchiveService, name: str) -> GameSummary:
    """按名字取游戏摘要(``list_games`` 含归档项, 不受主页视图筛选影响)."""
    return next(item for item in service.list_games() if item.name == name)


def _actions(audit_log: list[str]) -> set[str]:
    """把审计日志行读成动作名集合.

    行形如 ``<动作> <字段...>``(见 ``services/audit.py`` 的 ``logger.log(level, "%s %s", ...)``),
    所以动作是第一个词。按整词比而不是子串: 改了动作名(比如加个后缀)也得算回归。
    """
    return {line.split(maxsplit=1)[0] for line in audit_log}


@pytest.mark.profile
def test_a_monitored_game_goes_through_the_whole_flow(
    tmp_path: Path, audit_log: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """一条流程串到底: 发现 → 导入 → 备份 → 分支 → 恢复 → 导出 → 新库导入 → 启动 → 归档 → 删除.

    ``profile`` 标记让它在 Linux 上附一份函数级 trace(见 ``tests/profile_trace.py``);
    只挂这一条 —— 覆盖率最高的那条才值得看耗时分布。
    """
    running: list[str] = []
    service, _scheduler = _service(tmp_path, running)
    save = _program(tmp_path)

    # 1) 监控目录发现: 只有它自己扫出来的那一条, 且监控目录不推断存档位置.
    service.add_monitored_directory(str(tmp_path / "Games"))
    report = service.scan_candidates()
    candidate = next(item for item in service.list_candidates() if item.name == _GAME)
    assert (report.monitored, report.added) == (1, 1), f"扫描结果不对: {report}"
    assert candidate.source == "monitored"
    assert candidate.status == "new"
    assert candidate.health == "ok"
    assert candidate.save_supported is False, "监控目录不推断存档, 路径必须由用户给"

    # 2) 导入建游戏 + 写入用户确认的存档位置(导入对话框里手工给路径).
    summary = service.import_candidate(candidate.candidate_id, save_paths=(str(save),))
    game_id = summary.game_id
    assert summary.name == _GAME
    assert summary.saved_paths == 1
    locations = service.list_locations(game_id)
    assert [(item.path, item.source) for item in locations] == [
        (str(save), "manual")
    ], "监控目录来源的位置在库里的 source 应为 manual"

    # 3) 备份两份(中间改内容, 否则第二份会按"内容未变化"跳过).
    service.run_backup_now(game_id)
    (save / "slot.dat").write_text("state-v2", encoding="utf-8")
    service.run_backup_now(game_id)
    backups = service.list_backups(game_id)
    assert len(backups) == 2
    assert all(item.verified for item in backups), "两份备份都应通过校验"
    older = min(backups, key=lambda item: item.created_dt)

    # 4) 从旧节点开分支: 新节点的 branch_name 与父节点都要对得上.
    service.run_create_branch(game_id, older.backup_id, "experiment")
    branched = service.list_backups(game_id)
    branch_node = next(item for item in branched if item.branch_name)
    assert len(branched) == 3
    assert (branch_node.title, branch_node.parent_id) == ("experiment", older.backup_id)

    # 5) 恢复旧节点: 先造出"尚未备份的新进度", 恢复后内容回退并留下一个安全点.
    (save / "slot.dat").write_text("state-v3", encoding="utf-8")
    plan = service.preview_restore(game_id, older.backup_id)
    assert plan.snapshot_ok is True
    service.run_restore(game_id, older.backup_id, safety_point=True, force=True)
    assert (save / "slot.dat").read_text(encoding="utf-8") == "state-v1"
    assert any(item.safety for item in service.list_backups(game_id))

    # 6) 导出 → 另一个库导入: 节点数与存档位置都要跟着过去.
    package = tmp_path / "out" / f"{_GAME}.zip"
    package.parent.mkdir()
    service.run_export(game_id, str(package))
    assert package.is_file()
    other, _other_scheduler = _service(tmp_path / "other", [])
    inspection = other.inspect_import(str(package))
    assert isinstance(inspection, ImportInspection), "单游戏包应体检成 ImportInspection"
    assert inspection.game_name == _GAME
    assert inspection.backup_count == 4
    target_save = tmp_path / "imported-save"
    target_save.mkdir()
    index = inspection.locations[0].index
    other.run_import(inspection, strategy="new", locations={index: str(target_save)})
    imported = _by_name(other, _GAME)
    assert len(other.list_backups(imported.game_id)) == 4
    assert [item.path for item in other.list_locations(imported.game_id)] == [
        str(target_save)
    ]

    # 7) 关注链路: 进程表里出现它, 就自动把启用态切过去.
    running.append(f"{_GAME}.exe")
    outcome = service.poll_activation()
    assert outcome.monitor is not None, f"没有识别到在跑的游戏: {outcome.reason}"
    assert outcome.monitor.name == _GAME
    assert _by_name(service, _GAME).enabled is True

    # 8) 归档: 只是把游戏移出当前视图, 记录本身照旧可读.
    service.set_game_archived(game_id, True)
    assert _by_name(service, _GAME).archived is True

    # 9) 删除原始存档位置: 走注入的"回收站"(真实现只移动目录, 绝不永久删除).
    moved: list[str] = []
    trash = tmp_path / "trash"
    trash.mkdir()

    def fake_trash(path: str) -> None:
        target = trash / Path(path).name
        Path(path).rename(target)
        moved.append(str(target))

    monkeypatch.setattr(locations_mod, "send_to_trash", fake_trash)
    location = service.list_locations(game_id)[0]
    service.delete_save_location(location.location_id, confirm_name=_GAME)
    assert moved == [str(trash / "saves")]
    assert not save.exists()
    assert service.list_locations(game_id) == []

    # 10) 操作可追溯: 每一段都要在审计日志里留下痕迹.
    actions = _actions(audit_log)
    for action in (
        "discovery.import",
        "backup.create",
        "branch.create",
        "restore.succeeded",
        "export.succeeded",
        "import.succeeded",
        "location.delete",
    ):
        assert action in actions, f"审计日志缺 {action}, 实际有: {sorted(actions)}"


def test_disabling_only_skips_the_run_and_archiving_pauses_the_job(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """停用不改任务本身(到点跳过); 归档才暂停任务, 并把权限收到四项.

    "停用"与"定时任务"是两个独立开关: 停用只是让到点那一次被跳过(`schedule.skipped`),
    任务与周期原样保留; 归档才顺手把任务暂停。归档后的可用动作以
    `domain/game_rules.py` 的 `ARCHIVED_ACTIONS` 为唯一口径。
    """
    service, scheduler = _service(tmp_path, [])
    save = _program(tmp_path)
    game_id = service.add_game(_GAME).game_id
    service.add_location(game_id, path=str(save), kind="directory")
    service.set_game_enabled(game_id, True)
    status = service.set_schedule(game_id, "30m", keep_auto=1)
    assert status.schedule_text == "30m"

    # 停用: 任务与周期原样保留, 只是到点不真的备份.
    service.set_game_enabled(game_id, False)
    job = next(item for item in service.list_schedules() if item.game_id == game_id)
    assert (job.interval_text, job.enabled, job.game_enabled) == ("30m", True, False)
    before = len(service.list_backups(game_id))
    # trigger 返回 True 只说明"任务泵跑过一轮", 被跳过要看审计 —— 返回值不能当判据.
    assert scheduler.trigger(int(game_id)) is True
    assert len(service.list_backups(game_id)) == before, "停用的游戏不该真的备份"
    assert "schedule.skipped" in _actions(audit_log), "停用到点时 要留下跳过记录"

    # 归档: 后端顺手暂停任务(周期仍保留), 游戏也退出启用态.
    service.set_game_archived(game_id, True)
    archived = _by_name(service, _GAME)
    job = next(item for item in service.list_schedules() if item.game_id == game_id)
    assert job.enabled is False
    assert job.interval_text == "30m", "归档只暂停任务, 周期配置要留着"
    assert (archived.archived, archived.enabled) == (True, False)

    # 权限边界: 只剩详情 / 删除 / 导出 / 取消归档 / 管理窗口, 其余一律不可用.
    allowed = {action for action in get_args(GameAction) if archived.allow(action)}
    assert allowed == set(ARCHIVED_ACTIONS)

    # 取消归档后恢复成可备份、可改配置的普通游戏.
    service.set_game_archived(game_id, False)
    reopened = _by_name(service, _GAME)
    assert reopened.archived is False
    assert reopened.allow("backup") is True
    assert reopened.allow("schedule") is True
