"""
备份节点与分支树: 默认标题/子节点/分支标记/改名/删除级联与提示、快照校验与取消、哈希回填后台任务。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

import archive_management.ui.sql_backend as sql_mod
from archive_management.config import AppConfig, VerificationSettings, save_config
from archive_management.domain import (
    BackupNode,
    Game,
)
from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
    SnapshotError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
)

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _advance,
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


def test_backup_uses_named_folder_and_hides_the_key(tmp_path: Path) -> None:
    """备份目录用"名称 + 哈希", 但详情正文只说明它由应用自动命名."""
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)

    detail = service.get_detail(game_id)
    assert detail.storage_folder.startswith("Demo-")
    assert detail.storage_folder != game_id
    # 内部存储键只在悬停提示里, 不上正文(评审时定的).
    assert detail.origin_label == tr("hero.storage_folder")
    assert detail.storage_folder not in detail.origin_label
    assert detail.storage_hint == tr(
        "hero.storage_folder_tip", folder=detail.storage_folder
    )
    snapshots = list(
        (tmp_path / "backups" / detail.storage_folder).glob("*/loc-0/*.dat")
    )
    assert [item.name for item in snapshots] == ["slot1.dat"]
    assert str(save) not in detail.storage_folder


def test_no_backups_yet_and_backup_without_locations_raise(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    assert service.list_backups(game_id) == []
    with pytest.raises(ArchiveManagementError):
        service.run_backup_now(game_id)


def test_task_status_reports_backup_root(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = service.task_status()
    assert status.running is False
    assert str(tmp_path / "backups") in status.target_label


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


def test_task_status_without_game_reports_paused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = service.task_status(None)
    assert status.running is False
    assert status.schedule_text == ""
    assert status.cancellable is False


def test_backup_marks_current_node_and_bumps_revision(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    before = service.task_status(game_id).revision

    service.run_backup_now(game_id)

    items = service.list_backups(game_id)
    assert [item.is_current for item in items] == [True]
    assert service.task_status(game_id).revision != before


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


def test_storage_usage_counts_backup_storage(tmp_path: Path) -> None:
    """状态栏展示的"当前占用"来自备份存储的实际大小."""
    service, game_id, _save = _service_with_save(tmp_path)
    before = service.storage_usage()

    service.run_backup_now(game_id)

    after = service.storage_usage()
    assert after > before
    assert after > 0


def test_branch_backups_get_their_own_label(tmp_path: Path) -> None:
    """分支节点与主线节点的展示文案不同(节点的种类要映射对)."""
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot.dat").write_text("v1", encoding="utf-8")
    service.add_location(summary.game_id, path=str(save), kind="directory")
    service.run_backup_now(summary.game_id)
    first = service.list_backups(summary.game_id)[0]
    # 内容没变的分支会被跳过, 因此先动一下存档再分支。
    (save / "slot.dat").write_text("v2", encoding="utf-8")

    service.run_create_branch(summary.game_id, first.backup_id, "测试分支")

    labels = {item.branch_label for item in service.list_backups(summary.game_id)}
    assert len(labels) >= 2, f"分支与主线的标签应当不同: {labels}"


def test_automatic_backups_get_their_own_label(tmp_path: Path) -> None:
    """定时触发的备份是"自动备份"这一种类, 与手动备份区分开."""
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot.dat").write_text("v1", encoding="utf-8")
    service.add_location(summary.game_id, path=str(save), kind="directory")
    service.set_game_enabled(summary.game_id, True)
    assert service.set_schedule(summary.game_id, "30m")
    service.run_backup_now(summary.game_id)
    # 定时备份同样遵守"内容未变化就跳过", 因此先动一下存档。
    (save / "slot.dat").write_text("v2", encoding="utf-8")

    assert service._scheduler.trigger(int(summary.game_id)) is True

    items = service.list_backups(summary.game_id)
    assert len(items) == 2
    # 两种节点的展示文案不同: 定时那份叫"自动备份", 手动那份叫"手动备份"
    # (两者都在主线上, 因此区分它们的是标题而不是分支标签)。
    titles = {item.title for item in items}
    assert titles == {tr("backup.title_manual"), tr("backup.title_auto")}


def test_backup_of_an_empty_save_folder_records_no_file_entries(
    tmp_path: Path,
) -> None:
    """空存档目录也能备份: 快照清单里一条文件记录都没有, 但节点照样已验证."""
    service = _service(tmp_path)
    summary = service.add_game("空目录游戏")
    empty = tmp_path / "empty"
    empty.mkdir()
    service.add_location(summary.game_id, path=str(empty), kind="directory")

    service.run_backup_now(summary.game_id)

    items = service.list_backups(summary.game_id)
    assert len(items) == 1
    assert items[0].verified is True


def test_node_title_falls_back_to_the_kind_default(tmp_path: Path) -> None:
    """既没有标题也没有分支名时按节点类型给默认名(手动/自动/分支三种)."""
    service, game_id, save = _service_with_save(tmp_path)
    manual = service._backups.create_backup(int(game_id), kind="manual", title="")
    _advance(save)
    auto = service._backups.create_backup(int(game_id), kind="auto", title="")
    _advance(save)
    branch = service._backups.create_backup(
        int(game_id), kind="branch", title="", branch_name=""
    )

    assert service._node_title(manual) == tr("backup.title_manual")
    assert service._node_title(auto) == tr("backup.title_auto")
    assert service._node_title(branch) == tr("backup.title_branch")


def test_delete_backup_reports_cascade_for_a_branch_root(tmp_path: Path) -> None:
    """删掉带子节点的分支根节点: 提示要说清连带删掉了整条分支."""
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    first = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, first.backup_id, "分支")
    # 分支节点下面再挂一个: 这样删分支根节点就是"连带整条分支"(否则只是删一个节点).
    _advance(save)
    service.run_backup_now(game_id)
    branch_root = next(item for item in service.list_backups(game_id) if item.is_branch)

    message = service.run_delete_backup(game_id, branch_root.backup_id)

    assert message == tr("result.delete_cascade", count=2)


def test_verified_flag_is_false_for_a_node_without_an_id(tmp_path: Path) -> None:
    """节点还没落库(没有 id)时校验一律 False(不给缓存留下错的键)."""
    from archive_management.domain import BackupNode

    service = _service(tmp_path)

    assert service._is_verified(BackupNode(game_id=1, node_kind="manual")) is False


def test_half_written_rows_are_treated_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """查到实体但没有 id(半截数据)时按"未知"处理, 不拿 None 继续往下走."""
    from archive_management.domain import SaveLocation

    service = _service(tmp_path)
    monkeypatch.setattr(service._games, "get", lambda _id: Game(name="半截"))
    monkeypatch.setattr(
        service._locations,
        "get",
        lambda _id: SaveLocation(game_id=1, path="/x", path_kind="directory"),
    )

    with pytest.raises(ArchiveManagementError):
        service._game_ref("1")
    with pytest.raises(ArchiveManagementError):
        service._location_ref("1")


def test_name_mode_backup_is_hash_backfilled_in_the_background(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """名称模式: 备份立刻返回, 后台线程把缺的 sha256 补齐(界面不必等)."""
    config_path = tmp_path / "config.json"
    save_config(AppConfig(verification=VerificationSettings(mode="name")), config_path)
    service, game_id, _save = _service_with_save(tmp_path, config_path=config_path)
    done = threading.Event()
    real_backfill = service._backups.backfill_hashes

    def _backfill(node: BackupNode) -> int:
        try:
            return real_backfill(node)
        finally:
            done.set()

    monkeypatch.setattr(service._backups, "backfill_hashes", _backfill)

    service.run_backup_now(game_id)

    assert done.wait(timeout=10) is True
    backup_id = int(service.list_backups(game_id)[0].backup_id)
    files = service._nodes.list_files(backup_id)
    assert [entry.sha256 for entry in files if entry.file_kind == "file"] != [""]
    node = service._backups.get(backup_id)
    assert node is not None
    assert service._backups.needs_hash_backfill(node) is False


def test_a_failed_hash_backfill_only_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补齐失败不能让"备份成功"变成错误: 只记一条日志, 哈希留着下次再补."""
    service, _game_id, _save = _service_with_save(tmp_path)
    node = BackupNode(game_id=1, verify_mode="name")
    calls: list[BackupNode] = []

    def _boom(target: BackupNode) -> int:
        calls.append(target)
        raise SnapshotError("读不出来了")

    monkeypatch.setattr(service._backups, "backfill_hashes", _boom)

    service._run_hash_backfill([node])

    assert calls == [node]


def test_a_backfill_that_found_nothing_stays_quiet(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个文件都没补上(文件都不在了)时不留日志噪音, 也不算失败."""
    service, _game_id, _save = _service_with_save(tmp_path)
    node = BackupNode(game_id=1, verify_mode="name")
    monkeypatch.setattr(service._backups, "backfill_hashes", lambda _node: 0)

    service._run_hash_backfill([node])


def test_a_game_with_nothing_to_backfill_spawns_no_thread(
    tmp_path: Path,
) -> None:
    """严格模式下没有待补的备份: 不白开一个后台线程."""
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    before = threading.active_count()

    service._spawn_hash_backfill(int(game_id))

    assert threading.active_count() == before


def test_ids_that_are_not_numbers_are_treated_as_unknown(tmp_path: Path) -> None:
    """外部来的 id 可能是任意字符串: 可选的那处给 None, 必需的那处报"不认识"."""
    service = _service(tmp_path)

    assert service._optional_game_id(None) is None
    assert service._optional_game_id("不是 id") is None
    with pytest.raises(ArchiveManagementError):
        service._location_ref("不是 id")


def test_require_backup_rejects_a_non_numeric_id(tmp_path: Path) -> None:
    """备份 id 不是数字时给"不认识这个备份", 而不是把 ValueError 漏给界面."""
    service, game_id, _save = _service_with_save(tmp_path)

    with pytest.raises(ArchiveManagementError):
        service._require_backup(int(game_id), "不是 id")


def test_an_unreadable_snapshot_counts_as_unverified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快照读不出来(被删/权限不对)时按"没验证过"处理, 并把结论缓存下来(不反复重扫)."""
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    backup_id = service.list_backups(game_id)[0].backup_id
    node = service._require_backup(int(game_id), backup_id)
    service._verify_cache.clear()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("快照读不出来")

    monkeypatch.setattr(sql_mod, "verify_snapshot", refuse)

    assert service._is_verified(node) is False
    assert service._is_verified(node) is False, "第二次该直接读缓存, 不再去扫快照"
    assert service._verify_cache[str(node.id)] is False


def test_a_cancelled_backup_reports_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """备份中途被取消: 换成可展示的"已取消"说明, 并放掉进行中操作的槽位."""
    service, game_id, _save = _service_with_save(tmp_path)

    def stop(*_args: object, **_kwargs: object) -> None:
        raise OperationCancelledError("用户按了取消")

    monkeypatch.setattr(service._backups, "create_backup", stop)

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.run_backup_now(game_id)

    assert str(excinfo.value) == tr("result.backup_canceled")
    assert service._active is None, "取消之后不能一直占着「进行中」的槽位"
