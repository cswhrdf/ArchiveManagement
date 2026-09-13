"""备份用例(向下保存/创建分支)的单元测试(阶段 D 第 2 条).

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
from archive_management.domain import DeletionMode, Game, SaveLocation
from archive_management.exceptions import (
    ArchiveManagementError,
    DatabaseError,
    OperationCancelledError,
    SnapshotError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    OperationRepository,
    SaveLocationRepository,
)
from archive_management.services.snapshot import SnapshotSource

pytestmark = [
    pytest.mark.backend,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("备份用例服务"),
    pytest.mark.story("创建备份与分支"),
    pytest.mark.layer("unit"),
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
    second = service.create_backup(game_id)

    assert first.id is not None
    assert second.parent_id == first.id
    assert [row.depth for row in service.tree(game_id)] == [0, 1]
    assert service.latest(game_id) == second


def test_create_branch_marks_parent_and_name(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None

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


def test_create_backup_records_successful_operation(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    database = Database(tmp_path / "app.db")
    service.create_backup(game_id)
    operations = OperationRepository(database).list_recent()
    assert [item.status for item in operations] == ["succeeded"]
    assert operations[0].op_kind == "backup"


def test_snapshot_failure_leaves_no_node_or_directory(tmp_path: Path) -> None:
    database = Database(tmp_path / "app.db")
    database.migrate()
    game = GameRepository(database).add(Game(name="Demo"))
    assert game.id is not None
    service = BackupService(database, backup_root=tmp_path / "backups")
    with pytest.raises(ArchiveManagementError):
        # 未配置存档位置 -> 快照阶段失败
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


def test_cancelled_backup_reports_and_cleans_up(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path, saves=2)
    seen = {"checks": 0}

    def cancelled() -> bool:
        seen["checks"] += 1
        return seen["checks"] > 2

    with pytest.raises(OperationCancelledError):
        service.create_backup(game_id, cancelled=cancelled)

    assert service.list_nodes(game_id) == []
    assert not list((tmp_path / "backups").rglob("snapshot.json"))
    operations = OperationRepository(Database(tmp_path / "app.db")).list_recent()
    assert [item.status for item in operations] == ["cancelled"]


def test_snapshot_failure_is_recorded_as_failed_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
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

    operations = OperationRepository(Database(tmp_path / "app.db")).list_recent()
    assert [item.status for item in operations] == ["failed"]
    assert operations[0].message == "磁盘空间不足"


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


# --------------------------------------------- 当前节点 / 继续保存(阶段 D 迭代)


def test_current_node_defaults_to_latest(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    second = service.create_backup(game_id)
    assert service.current_node(game_id) == second
    assert service.current_node(game_id) != first


def test_set_current_makes_new_backups_continue_from_that_node(
    tmp_path: Path,
) -> None:
    """恢复到此节点后, 之后的向下保存从该节点重新开始."""
    service, game_id = _service(tmp_path)
    root = service.create_backup(game_id)
    middle = service.create_backup(game_id)
    tip = service.create_backup(game_id)
    assert root.id is not None
    assert middle.id is not None
    assert tip.id is not None

    service.set_current(game_id, root.id)
    assert service.current_node(game_id) == root

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


# --------------------------------------------------------- 删除(阶段 D 迭代)


def test_delete_leaf_removes_node_and_snapshot(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    service.create_backup(game_id)
    leaf = service.create_backup(game_id)
    root = service.snapshot_root(leaf)

    plan = service.delete_node(game_id, leaf.id or 0)

    assert plan.mode is DeletionMode.SINGLE
    assert not root.exists()
    assert len(service.list_nodes(game_id)) == 1


def test_delete_middle_node_moves_later_backups_up(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    first = service.create_backup(game_id)
    middle = service.create_backup(game_id)
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
    branch = service.create_branch(game_id, base.id, "Branch")
    assert branch.id is not None
    child = service.create_backup(game_id, parent_id=branch.id)

    with pytest.raises(ArchiveManagementError):
        service.delete_node(game_id, branch.id)

    assert len(service.list_nodes(game_id)) == 3
    assert child.parent_id == branch.id


def test_delete_branch_root_cascade_removes_subtree(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    base = service.create_backup(game_id)
    assert base.id is not None
    branch = service.create_branch(game_id, base.id, "Branch")
    assert branch.id is not None
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
    branch = service.create_branch(game_id, base.id, "Branch")
    assert branch.title == "Branch"
    assert branch.branch_name == "Branch"


# --------------------------------------------- 自动备份保留份数(阶段 D 迭代)


def test_auto_backups_are_pruned_to_keep_count(tmp_path: Path) -> None:
    service, game_id = _service(tmp_path)
    for _ in range(3):
        service.create_backup(game_id, kind="auto", keep_auto=2)

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
