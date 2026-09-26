"""备份用例(向下保存/创建分支)的单元测试.

重点验证"磁盘与数据库一致":
- 成功时节点、文件清单与快照目录同时存在;
- 失败或取消时不留下可被误认为完整备份的节点或目录;
- 落库失败时回收已经提交的快照目录。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archive_management.application.backup import (
    MAX_NOTE_LENGTH,
    MAX_TITLE_LENGTH,
    BackupService,
)
from archive_management.domain import BackupNode, DeletionMode, Game, SaveLocation
from archive_management.exceptions import (
    ArchiveManagementError,
    ContentUnchangedError,
    DatabaseError,
    OperationCancelledError,
    SnapshotError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.naming import game_folder
from archive_management.services.snapshot import MANIFEST_FILENAME, SnapshotSource
from helpers import touch_save

pytestmark = [
    pytest.mark.backend,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("备份用例服务"),
    pytest.mark.story("创建备份与分支"),
    # 真实数据库 + 文件系统的跨组件协作, 按层定义归入 integration.
    pytest.mark.layer("integration"),
]


def _service(tmp_path: Path, *, saves: int = 1) -> tuple[BackupService, int]:
    """构造带一个游戏的备份服务, 返回服务与游戏 id."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    game = GameRepository(database).add(Game(name="Demo"))
    assert game.id is not None
    locations = SaveLocationRepository(database)
    for index in range(saves):
        save = tmp_path / f"save{index}"
        save.mkdir()
        (save / "slot.dat").write_text(f"state-{index}", encoding="utf-8")
        locations.add(
            SaveLocation(
                game_id=game.id,
                path=str(save),
                path_kind="directory",
                is_primary=index == 0,
                last_checked_at=datetime.now(UTC),
                last_check_status="ok",
            )
        )
    service = BackupService(database, backup_root=tmp_path / "backups")
    return service, game.id


def _touch_save(tmp_path: Path, *, index: int = 0, text: str = "") -> None:
    """改动存档内容, 让下一次备份与当前节点不同(共享实现见 tests/helpers.py).

    "内容没有变化就不产生新备份"是备份服务层的规则(见
    :class:`ArchiveManagementError` 的 ContentUnchangedError), 因此需要连续
    备份的用例必须先推进一次存档状态.
    """
    touch_save(tmp_path, index=index, text=text)


def test_create_backup_writes_node_files_and_snapshot(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id, note="首个节点")

    assert node.id is not None
    assert node.parent_id is None
    assert node.node_kind == "manual"
    assert node.content_hash
    assert node.storage_relpath is not None
    root = service.snapshot_root(node)
    assert root.is_dir()
    assert service.verify(node).ok is True
    facts = service.facts(node)
    assert facts.file_count == 1
    assert facts.total_size > 0
    assert facts.content_hash == node.content_hash


def test_second_backup_chains_onto_previous_node(tmp_path: Path) -> None:
    """向下保存: 新节点挂在当前末端之后."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    second = service.create_backup(game_id)

    assert first.id is not None
    assert second.parent_id == first.id
    assert [row.depth for row in service.tree(game_id)] == [0, 1]
    assert service.latest(game_id) == second


def test_create_backup_skips_unchanged_content(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """存档与当前节点完全一致时不创建新备份, 并留下 DEBUG 级日志."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)

    with pytest.raises(ContentUnchangedError) as excinfo:
        service.create_backup(game_id)

    assert excinfo.value.backup_id == first.id
    assert [node.id for node in service.list_nodes(game_id)] == [first.id]
    skipped = [line for line in audit_log if "skipped_unchanged" in line]
    assert skipped
    # 重复跳过是基础操作: 只在文件日志里留痕.
    assert all("backup.create " in line for line in skipped)


def test_unchanged_backup_leaves_no_snapshot_directory(tmp_path: Path) -> None:
    """被判定没有变化的那一次不能留下快照目录."""
    service, game_id = _service(tmp_path)
    service.create_backup(game_id)

    with pytest.raises(ContentUnchangedError):
        service.create_backup(game_id)

    assert len(list((tmp_path / "backups").rglob(MANIFEST_FILENAME))) == 1


def test_create_branch_requires_changed_content(tmp_path: Path) -> None:
    """分支同样要校验内容: 与分支起点一致时不产生节点."""
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None

    with pytest.raises(ContentUnchangedError):
        service.create_branch(game_id, base.id, "黑棘")

    _touch_save(tmp_path)
    branch = service.create_branch(game_id, base.id, "黑棘")
    assert branch.branch_name == "黑棘"


def test_create_branch_marks_parent_and_name(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None
    _touch_save(tmp_path)

    branch = service.create_branch(game_id, base.id, "  黑棘  ")

    assert branch.node_kind == "branch"
    assert branch.branch_name == "黑棘"
    assert branch.parent_id == base.id
    rows = service.tree(game_id)
    assert [row.node_id for row in rows] == [str(base.id), str(branch.id)]
    assert rows[1].depth == 1


def test_create_branch_rejects_blank_name(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None
    with pytest.raises(ArchiveManagementError):
        service.create_branch(game_id, base.id, "   ")


def test_create_backup_uses_named_folder_instead_of_game_id(tmp_path: Path) -> None:
    """备份目录用"名称 + 名称与路径的短哈希", 不再用数字 id."""
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)

    assert node.storage_relpath is not None
    folder, _, leaf = node.storage_relpath.partition("/")
    assert folder == game_folder("Demo", [str(tmp_path / "save0")])
    assert folder.startswith("Demo-")
    assert folder != str(game_id)
    assert leaf  # 目录内仍然是一次备份的时间前缀文件件

    # 目录名已持久化在 games 表, 用于界面额外信息展示.
    stored = GameRepository(Database(tmp_path / "app.db")).get(game_id)
    assert stored is not None
    assert stored.storage_key == folder
    assert stored.original_name == "Demo"


def test_storage_folder_survives_rename_and_location_change(tmp_path: Path) -> None:
    """改名/增删存档位置不搬动已有备份: 目录名在首次备份时固化."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    games = GameRepository(Database(tmp_path / "app.db"))
    renamed = games.get(game_id)
    assert renamed is not None
    games.update(renamed.model_copy(update={"name": "改名后的游戏"}))

    _touch_save(tmp_path)
    second = service.create_backup(game_id)
    extra = tmp_path / "save-extra"
    extra.mkdir()
    (extra / "slot.dat").write_text("extra", encoding="utf-8")
    SaveLocationRepository(Database(tmp_path / "app.db")).add(
        SaveLocation(game_id=game_id, path=str(extra), path_kind="directory")
    )
    _touch_save(tmp_path)
    third = service.create_backup(game_id)

    def folder_of(relpath: str | None) -> str:
        assert relpath is not None
        return relpath.split("/")[0]

    assert folder_of(first.storage_relpath) == folder_of(second.storage_relpath)
    assert folder_of(second.storage_relpath) == folder_of(third.storage_relpath)
    stored = games.get(game_id)
    assert stored is not None
    assert stored.original_name == "Demo"  # 原始名称保留, 改名不改写
    assert stored.name == "改名后的游戏"


def test_storage_folder_distinguishes_games_with_same_name(tmp_path: Path) -> None:
    """同名游戏靠路径哈希区分, 不会互相覆盖."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    games = GameRepository(database)
    locations = SaveLocationRepository(database)
    service = BackupService(database, backup_root=tmp_path / "backups")
    folders: list[str] = []
    for name in ("Demo", "Demo"):
        save = tmp_path / f"save-{len(folders)}"
        save.mkdir()
        (save / "slot.dat").write_text(name, encoding="utf-8")
        game = games.add(Game(name=name))
        assert game.id is not None
        locations.add(SaveLocation(game_id=game.id, path=str(save)))
        node = service.create_backup(game.id)
        assert node.storage_relpath is not None
        folders.append(node.storage_relpath.split("/")[0])

    assert folders[0] != folders[1]
    assert all(folder.startswith("Demo-") for folder in folders)


def test_create_backup_rejects_unknown_parent(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.create_backup(game_id, parent_id=999)


def test_create_backup_requires_save_locations(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.migrate()
    game = GameRepository(database).add(Game(name="Empty"))
    assert game.id is not None
    service = BackupService(database, backup_root=tmp_path / "backups")
    with pytest.raises(ArchiveManagementError):
        service.create_backup(game.id)


def test_create_backup_rejects_unknown_game(tmp_path: Path) -> None:
    service, _game_id = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.create_backup(4242)


def test_create_backup_records_successful_operation(
    tmp_path: Path, audit_log: list[str]
) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    assert any(
        message.startswith("backup.create ")
        and "result=succeeded" in message
        and f"backup_id={node.id}" in message
        for message in audit_log
    )


def test_snapshot_failure_leaves_no_node_or_directory(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.migrate()
    game = GameRepository(database).add(Game(name="Demo"))
    assert game.id is not None
    service = BackupService(database, backup_root=tmp_path / "backups")
    with pytest.raises(ArchiveManagementError):
        # 未配置存档位置 -> 快照环节失败
        service.create_backup(game.id)
    assert service.list_nodes(game.id) == []
    assert not list((tmp_path / "backups").rglob("snapshot.json"))


def test_database_failure_rolls_back_snapshot_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, game_id = _service(tmp_path)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise DatabaseError("insert failed")

    monkeypatch.setattr(BackupRepository, "add_with_files", boom)
    with pytest.raises(DatabaseError):
        service.create_backup(game_id)

    assert not list((tmp_path / "backups").rglob("snapshot.json"))
    assert service.list_nodes(game_id) == []


def test_cancelled_backup_reports_and_cleans_up(
    tmp_path: Path, audit_log: list[str]
) -> None:
    service, game_id = _service(tmp_path, saves=2)
    seen = {"checks": 0}

    def cancelled() -> bool:
        seen["checks"] += 1
        return seen["checks"] > 2

    with pytest.raises(OperationCancelledError):
        service.create_backup(game_id, cancelled=cancelled)

    assert service.list_nodes(game_id) == []
    assert not list((tmp_path / "backups").rglob("snapshot.json"))
    assert any("result=cancelled" in message for message in audit_log)


def test_snapshot_failure_is_recorded_as_failed_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log: list[str],
) -> None:
    service, game_id = _service(tmp_path)

    def boom(
        _sources: Sequence[SnapshotSource],
        _destination: Path,
        **_kwargs: object,
    ) -> None:
        raise SnapshotError("磁盘空间不足")

    monkeypatch.setattr(service, "_snapshotter", boom)
    with pytest.raises(SnapshotError):
        service.create_backup(game_id)

    assert any(
        message.startswith("backup.create.failed ") and "error=" in message
        for message in audit_log
    )


def test_facts_for_unsaved_node(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    from archive_management.domain import BackupNode

    facts = service.facts(BackupNode(game_id=game_id, node_kind="manual"))
    assert facts.file_count == 0
    assert facts.total_size == 0


def test_snapshot_root_requires_storage_relpath(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    from archive_management.domain import BackupNode

    with pytest.raises(ArchiveManagementError):
        service.snapshot_root(BackupNode(game_id=game_id, node_kind="manual"))


def test_delete_removes_node_and_snapshot(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    root = service.snapshot_root(node)

    service.delete(node)

    assert service.list_nodes(game_id) == []
    assert not root.exists()


def test_multiple_save_locations_are_all_backed_up(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path, saves=2)
    node = service.create_backup(game_id)
    facts = service.facts(node)
    assert facts.file_count == 2
    entries = BackupRepository(Database(tmp_path / "app.db")).list_files(node.id or 0)
    files = [entry.relative_path for entry in entries if entry.file_kind == "file"]
    assert files == ["loc-0/slot.dat", "loc-1/slot.dat"]


# --------------------------------------------- 当前节点 / 继续保存


def test_current_node_defaults_to_latest(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    second = service.create_backup(game_id)
    assert service.current_node(game_id) == second
    assert service.current_node(game_id) != first


def test_set_current_makes_new_backups_continue_from_that_node(
    tmp_path: Path,
) -> None:
    """恢复到此节点后, 之后的向下保存从该节点重新开始."""
    service, game_id = _service(tmp_path)
    root = service.create_backup(game_id)
    _touch_save(tmp_path)
    middle = service.create_backup(game_id)
    _touch_save(tmp_path)
    tip = service.create_backup(game_id)
    assert root.id is not None
    assert middle.id is not None
    assert tip.id is not None

    service.set_current(game_id, root.id)
    assert service.current_node(game_id) == root

    _touch_save(tmp_path)
    continued = service.create_backup(game_id)

    assert continued.parent_id == root.id
    assert continued.parent_id != tip.id
    rows = {row.node_id: row.depth for row in service.tree(game_id)}
    assert rows[str(continued.id)] == 1
    assert rows[str(middle.id)] == 1


def test_set_current_rejects_other_game(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    assert node.id is not None
    with pytest.raises(ArchiveManagementError):
        service.set_current(999, node.id)


def test_set_current_rejects_unknown_backup(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.set_current(game_id, 12345)


# --------------------------------------------------------- 删除


def test_delete_leaf_removes_node_and_snapshot(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    service.create_backup(game_id)
    _touch_save(tmp_path)
    leaf = service.create_backup(game_id)
    root = service.snapshot_root(leaf)

    plan = service.delete_node(game_id, leaf.id or 0)

    assert plan.mode is DeletionMode.SINGLE
    assert not root.exists()
    assert len(service.list_nodes(game_id)) == 1


def test_delete_middle_node_moves_later_backups_up(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    middle = service.create_backup(game_id)
    _touch_save(tmp_path)
    last = service.create_backup(game_id)
    assert first.id is not None
    assert middle.id is not None
    assert last.id is not None

    plan = service.delete_node(game_id, middle.id)

    assert plan.mode is DeletionMode.SHIFT
    remaining = service.list_nodes(game_id)
    assert [node.id for node in remaining] == [first.id, last.id]
    assert remaining[1].parent_id == first.id
    assert not service.snapshot_root(middle).exists()


def test_delete_branch_root_requires_confirmation(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None
    _touch_save(tmp_path)
    branch = service.create_branch(game_id, base.id, "Branch")
    assert branch.id is not None
    _touch_save(tmp_path)
    child = service.create_backup(game_id, parent_id=branch.id)

    with pytest.raises(ArchiveManagementError):
        service.delete_node(game_id, branch.id)

    assert len(service.list_nodes(game_id)) == 3
    assert child.parent_id == branch.id


def test_delete_branch_root_cascade_removes_subtree(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None
    _touch_save(tmp_path)
    branch = service.create_branch(game_id, base.id, "Branch")
    assert branch.id is not None
    _touch_save(tmp_path)
    service.create_backup(game_id, parent_id=branch.id)
    branch_root = service.snapshot_root(branch)

    plan = service.delete_node(game_id, branch.id, cascade=True)

    assert plan.mode is DeletionMode.CASCADE
    assert plan.removed_count == 2
    assert [node.id for node in service.list_nodes(game_id)] == [base.id]
    assert not branch_root.exists()


def test_delete_current_node_falls_back_to_parent(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    last = service.create_backup(game_id)
    assert first.id is not None
    assert last.id is not None
    assert service.current_node(game_id) == last

    service.delete_node(game_id, last.id)

    assert service.current_node(game_id) == first


def test_plan_delete_does_not_change_data(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    plan = service.plan_delete(game_id, node.id or 0)
    assert plan.mode is DeletionMode.SINGLE
    assert len(service.list_nodes(game_id)) == 1


# ------------------------------------------------------------- 命名与描述


def test_update_meta_sets_title_and_note(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    assert node.id is not None

    updated = service.update_meta(
        game_id, node.id, title="  离开量子月亮前  ", note="  第一次通关前  "
    )

    assert updated.title == "离开量子月亮前"
    assert updated.note == "第一次通关前"
    assert service.get(node.id) == updated


def test_update_meta_rejects_long_note(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    assert node.id is not None
    with pytest.raises(ArchiveManagementError):
        service.update_meta(
            game_id, node.id, title="t", note="x" * (MAX_NOTE_LENGTH + 1)
        )


def test_update_meta_rejects_long_title(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    assert node.id is not None
    with pytest.raises(ArchiveManagementError):
        service.update_meta(
            game_id, node.id, title="x" * (MAX_TITLE_LENGTH + 1), note=""
        )


def test_update_meta_rejects_unknown_backup(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.update_meta(game_id, 999, title="a", note="b")


def test_branch_node_uses_branch_name_as_title(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None
    _touch_save(tmp_path)
    branch = service.create_branch(game_id, base.id, "Branch")
    assert branch.title == "Branch"
    assert branch.branch_name == "Branch"


# --------------------------------------------- 自动备份保留份数


def test_auto_backups_are_pruned_to_keep_count(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    for _ in range(3):
        service.create_backup(game_id, kind="auto", keep_auto=2)
        _touch_save(tmp_path)

    autos = [node for node in service.list_nodes(game_id) if node.node_kind == "auto"]
    assert len(autos) == 2
    manifests = list((tmp_path / "backups").rglob("snapshot.json"))
    assert len(manifests) == 2


def test_pruned_auto_keeps_chain_connected(tmp_path: Path) -> None:
    """清理最早的自动备份时, 后续自动备份上移而不是变成孤儿."""
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None
    for _ in range(3):
        _touch_save(tmp_path)
        service.create_backup(game_id, kind="auto", keep_auto=1)

    autos = [node for node in service.list_nodes(game_id) if node.node_kind == "auto"]
    assert len(autos) == 1
    assert autos[0].parent_id == base.id
    rows = {row.node_id: row.depth for row in service.tree(game_id)}
    assert rows[str(autos[0].id)] == 1


def test_auto_backup_becomes_current_node(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    auto = service.create_backup(game_id, kind="auto", keep_auto=3)
    assert service.current_node(game_id) == auto


# -- "当前节点"指针的边界 ---------------------------------------------------


def test_current_node_falls_back_when_the_pointer_dangles(tmp_path: Path) -> None:
    """指针指向已被删掉的行时回退到最新节点(向下保存始终有父节点)."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    second = service.create_backup(game_id)
    assert first.id is not None
    assert second.id is not None
    # 绕过服务直接删行, 造出"指针悬空"的脏库现场.
    service._backups.delete(second.id)

    fallback = service.current_node(game_id)

    assert fallback is not None
    assert fallback.id == first.id


def test_deleting_the_current_leaf_clears_the_pointer(tmp_path: Path) -> None:
    """删掉当前节点(叶子)后指针不再悬空: 之后读到的是剩下最新的那一份."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    second = service.create_backup(game_id)
    assert first.id is not None
    assert second.id is not None

    plan = service.delete_node(game_id, second.id)

    assert plan.mode is DeletionMode.SINGLE
    current = service.current_node(game_id)
    assert current is not None
    assert current.id == first.id


def test_deleting_the_current_root_keeps_the_child_as_the_current_node(
    tmp_path: Path,
) -> None:
    """删掉当前的根节点后仍能向下保存: 当前节点落到上移的子节点."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    second = service.create_backup(game_id)
    assert first.id is not None
    assert second.id is not None
    service.set_current(game_id, first.id)

    plan = service.delete_node(game_id, first.id)

    assert plan.mode is DeletionMode.SHIFT
    current = service.current_node(game_id)
    assert current is not None
    assert current.id == second.id


def test_update_meta_reports_a_row_that_disappeared(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改名时那一行已经不在了: 给出可读错误, 而不是把 None 交给调用方."""
    service, game_id = _service(tmp_path)
    node = service.create_backup(game_id)
    assert node.id is not None
    monkeypatch.setattr(service._backups, "update_meta", lambda *args, **kwargs: None)

    with pytest.raises(ArchiveManagementError, match="未知的备份节点"):
        service.update_meta(game_id, node.id, title="新名字", note="")


# -- 没有存储目录的节点(老库/手工导入的行) ---------------------------------


def _node_without_a_directory(service: BackupService, game_id: int) -> BackupNode:
    """直接写一行没有 storage_relpath 的节点(模拟没有快照目录的历史数据)."""
    node = service._backups.add(
        BackupNode(game_id=game_id, node_kind="manual", title="没有目录")
    )
    assert node.id is not None
    assert node.storage_relpath is None
    return node


def test_deleting_a_node_without_a_directory_only_removes_the_row(
    tmp_path: Path,
) -> None:
    """节点没有存储目录时只删行, 不去删一个不存在的快照目录."""
    service, game_id = _service(tmp_path)
    node = _node_without_a_directory(service, game_id)
    assert node.id is not None

    plan = service.delete_node(game_id, node.id)

    assert plan.removed_count == 1
    assert service._backups.get(node.id) is None


def test_delete_skips_a_node_that_was_never_stored(tmp_path: Path) -> None:
    """删除未落库的节点对象时不碰数据库(没有 id 与没有目录两条都要跳过)."""
    service, game_id = _service(tmp_path)
    unsaved = BackupNode(game_id=game_id, node_kind="manual")

    service.delete(unsaved)

    assert service.list_nodes(game_id) == []

    stored = _node_without_a_directory(service, game_id)
    assert stored.id is not None

    service.delete(stored)

    assert service._backups.get(stored.id) is None


def test_pruning_auto_backups_without_directories_only_removes_rows(
    tmp_path: Path,
) -> None:
    """剪除没有存储目录的自动备份时只删行, 不去删不存在的快照目录."""
    service, game_id = _service(tmp_path)
    for index in range(3):
        service._backups.add(
            BackupNode(
                game_id=game_id,
                node_kind="auto",
                title=f"auto-{index}",
                created_at=datetime(2024, 1, 1 + index, tzinfo=UTC),
            )
        )

    service.create_backup(game_id, kind="auto", keep_auto=1)

    autos = [node for node in service.list_nodes(game_id) if node.node_kind == "auto"]
    assert len(autos) == 1
    assert autos[0].storage_relpath is not None


def _stale_pointer(service: BackupService, removed_id: int) -> None:
    """让服务读到一次"还指着已被删掉的行"的指针(脏库现场).

    正常库里外键(ON DELETE SET NULL)已经把指针清空了, 所以这里用替身把那种
    状态递进去, 钉住 :meth:`BackupService.delete_node` 自带的修正逻辑.
    """
    real = service._games.current_backup
    stale: list[int | None] = [removed_id]

    def still_pointing_at_the_deleted_row(game_id: int) -> int | None:
        if stale:
            return stale.pop()
        return real(game_id)

    service._games.current_backup = still_pointing_at_the_deleted_row  # type: ignore[method-assign]


def test_deleting_the_current_leaf_clears_a_stale_pointer(
    tmp_path: Path,
) -> None:
    """指针还指着被删的行时(脏库)也要被修正, 不能留一个悬空指针."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    second = service.create_backup(game_id)
    assert first.id is not None
    assert second.id is not None
    service.set_current(game_id, second.id)
    _stale_pointer(service, second.id)

    plan = service.delete_node(game_id, second.id)

    assert plan.mode is DeletionMode.SINGLE
    assert service._games.current_backup(game_id) is None


def test_deleting_the_current_root_moves_a_stale_pointer_to_the_child(
    tmp_path: Path,
) -> None:
    """删掉根节点时悬空指针被移到上移的子节点(线路保持连续)."""
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    _touch_save(tmp_path)
    second = service.create_backup(game_id)
    assert first.id is not None
    assert second.id is not None
    service.set_current(game_id, first.id)
    _stale_pointer(service, first.id)

    plan = service.delete_node(game_id, first.id)

    assert plan.mode is DeletionMode.SHIFT
    assert service._games.current_backup(game_id) == second.id
