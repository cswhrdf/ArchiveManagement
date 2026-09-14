"""备份节点、分支树与快照复制在大规模数据下的基准.

数据规模:

- 400 个首尾相连的备份节点(单游戏链式分支), 衡量节点读取、树构建与两种视图排序;
- 300 个 32 KiB 文件(约 9.4 MiB 有效载荷), 衡量快照复制的吞吐与内存峰值。

失败风险: 树/列表操作退化成 O(n²)、快照复制不再流式读取(一次性把文件读进内存)
或深校验变成重复哈希, 都会在这里被拦下。
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from archive_management.domain import BackupNode, build_tree, tree_inputs
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import BackupRepository
from archive_management.services.snapshot import (
    SnapshotSource,
    create_snapshot,
    verify_snapshot,
)
from archive_management.ui.models import BackupItem, branch_order, timeline_order
from archive_management.ui.sql_backend import SqlArchiveService
from helpers import (
    BASE_MOMENT,
    add_game,
    migrated_database,
    seed_backup_chain,
    write_text_files,
)
from reporting import PerformanceRecorder

pytestmark = [
    pytest.mark.performance,
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("备份规模基准"),
    pytest.mark.story("大规模备份节点与快照吞吐"),
    pytest.mark.layer("performance"),
    pytest.mark.timeout(300),
]

# 数据规模: 单个游戏 400 个历史节点, 足以暴露 O(n²) 级别的实现.
NODE_COUNT = 400
NODE_SCALE = f"{NODE_COUNT} 个备份节点"
# 快照规模: 300 个 32 KiB 文件(约 9.4 MiB).
FILE_COUNT = 300
FILE_SIZE = 32 * 1024
SNAPSHOT_SCALE = f"{FILE_COUNT} 个文件 x {FILE_SIZE // 1024} KiB"
SNAPSHOT_BYTES = FILE_COUNT * FILE_SIZE


def _items(nodes: list[BackupNode]) -> list[BackupItem]:
    """把备份节点映射为界面卡片模型(两种排序基准的输入)."""
    items: list[BackupItem] = []
    previous: str | None = None
    for node in nodes:
        backup_id = str(node.id)
        items.append(
            BackupItem(
                backup_id=backup_id,
                title=node.title,
                created_dt=node.created_at or BASE_MOMENT,
                created_label="",
                auto=node.node_kind == "auto",
                branch_label="",
                size_label="",
                verified=True,
                parent_id=previous,
                is_branch=node.node_kind == "branch",
            )
        )
        previous = backup_id
    return items


@pytest.fixture(scope="module")
def chain_database(tmp_path_factory: pytest.TempPathFactory) -> tuple[Database, int]:
    """一个游戏 + 一条 400 节点的备份链."""
    root = tmp_path_factory.mktemp("backup-scale")
    database = migrated_database(root)
    game_id = add_game(database, "规模游戏", path=root / "save")
    seed_backup_chain(database, game_id, NODE_COUNT)
    return database, game_id


def test_backup_node_listing_within_budget(
    chain_database: tuple[Database, int], perf_recorder: PerformanceRecorder
) -> None:
    """读取 400 个节点(含文件计数聚合)的耗时上限."""
    database, game_id = chain_database
    with perf_recorder.duration(
        "backup.list_for_game", scale=NODE_SCALE, budget_seconds=3.0
    ):
        nodes = BackupRepository(database).list_for_game(game_id)

    assert len(nodes) == NODE_COUNT
    assert nodes[0].parent_id is None


def test_tree_and_view_sorting_within_budget(
    chain_database: tuple[Database, int], perf_recorder: PerformanceRecorder
) -> None:
    """分支树构建、时间线与分支视图排序全部在预算内."""
    database, game_id = chain_database
    nodes = BackupRepository(database).list_for_game(game_id)
    items = _items(nodes)

    with perf_recorder.duration(
        "backup.build_tree", scale=NODE_SCALE, budget_seconds=3.0
    ):
        tree = build_tree(tree_inputs(nodes))
    with perf_recorder.duration(
        "ui.timeline_order", scale=NODE_SCALE, budget_seconds=3.0
    ):
        timeline = timeline_order(list(items))
    with perf_recorder.duration(
        "ui.branch_order", scale=NODE_SCALE, budget_seconds=3.0
    ):
        branches = branch_order(list(items))

    assert len(tree) == NODE_COUNT
    assert len(timeline) == NODE_COUNT
    assert branches
    assert max(item.depth for item in branches) > 1


def test_backend_backup_listing_response(
    chain_database: tuple[Database, int],
    tmp_path: Path,
    perf_recorder: PerformanceRecorder,
) -> None:
    """UI 后端的备份列表响应时间(含快照校验结果的缓存路径)."""
    database, game_id = chain_database
    service = SqlArchiveService(database, backup_root=tmp_path / "backups")
    with perf_recorder.duration(
        "backend.list_backups", scale=NODE_SCALE, budget_seconds=5.0
    ):
        items = service.list_backups(str(game_id))

    assert len(items) == NODE_COUNT


def test_snapshot_copy_throughput_and_memory(
    tmp_path: Path, perf_recorder: PerformanceRecorder
) -> None:
    """快照复制: 吞吐下限与内存峰值上限(必须流式复制, 不能整目录读入内存)."""
    payload = tmp_path / "payload"
    write_text_files(payload, FILE_COUNT, size=FILE_SIZE)
    sources = [SnapshotSource(path=str(payload), kind="directory", index=0)]

    with (
        perf_recorder.peak_memory(
            "snapshot.create_memory", scale=SNAPSHOT_SCALE, budget_mib=64.0
        ),
        perf_recorder.throughput(
            "snapshot.create_throughput",
            scale=SNAPSHOT_SCALE,
            total_bytes=SNAPSHOT_BYTES,
            minimum_mib_per_second=4.0,
        ),
        perf_recorder.duration(
            "snapshot.create", scale=SNAPSHOT_SCALE, budget_seconds=30.0
        ),
    ):
        result = create_snapshot(sources, tmp_path / "snap-1")

    assert result.file_count() >= FILE_COUNT


def test_snapshot_deep_verification_throughput(
    tmp_path: Path, perf_recorder: PerformanceRecorder
) -> None:
    """深校验(逐文件重新哈希)的吞吐下限与耗时上限."""
    payload = tmp_path / "payload"
    write_text_files(payload, FILE_COUNT, size=FILE_SIZE)
    destination = tmp_path / "snap-2"
    create_snapshot(
        [SnapshotSource(path=str(payload), kind="directory", index=0)], destination
    )

    with (
        perf_recorder.throughput(
            "snapshot.verify_throughput",
            scale=SNAPSHOT_SCALE,
            total_bytes=SNAPSHOT_BYTES,
            minimum_mib_per_second=4.0,
        ),
        perf_recorder.duration(
            "snapshot.verify", scale=SNAPSHOT_SCALE, budget_seconds=30.0
        ),
    ):
        verification = verify_snapshot(destination)

    assert verification.ok is True
    assert verification.checked >= FILE_COUNT


def test_backup_chain_stays_consistent_under_scale(
    chain_database: tuple[Database, int],
) -> None:
    """规模数据本身的正确性守卫: 节点数、父子关系与时间跨度都应自洽."""
    database, game_id = chain_database
    nodes = BackupRepository(database).list_for_game(game_id)
    assert len(nodes) == NODE_COUNT
    assert nodes[-1].created_at is not None
    assert nodes[0].created_at is not None
    assert nodes[-1].created_at - nodes[0].created_at == timedelta(
        minutes=NODE_COUNT - 1
    )
