"""备份删除计划与自动备份清理的单元测试."""

from __future__ import annotations

from datetime import datetime

import pytest

from archive_management.domain import (
    BackupNode,
    DeletionMode,
    NodeKind,
    auto_prune_ids,
    descendant_ids,
    plan_deletion,
)
from helpers import utc_moment

pytestmark = [
    pytest.mark.domain,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("删除策略"),
    pytest.mark.story("删除备份节点"),
    pytest.mark.layer("unit"),
]


def _dt(day: int, hour: int = 9) -> datetime:
    """返回固定 UTC 时刻(实现见 tests/helpers.py 的 utc_moment)."""
    return utc_moment(day, hour)


def _node(
    node_id: int,
    parent_id: int | None = None,
    *,
    day: int = 1,
    kind: NodeKind = "manual",
) -> BackupNode:
    return BackupNode(
        id=node_id,
        game_id=1,
        parent_id=parent_id,
        node_kind=kind,
        created_at=_dt(day),
    )


def test_single_delete_for_leaf_node() -> None:
    nodes = [_node(1), _node(2, 1, day=2)]
    plan = plan_deletion(nodes, 2)
    assert plan.mode is DeletionMode.SINGLE
    assert plan.removed_ids == (2,)
    assert plan.needs_confirmation is False
    assert plan.shifted_child_id is None


def test_shift_delete_moves_later_backups_up() -> None:
    """同一线路上的节点被删除时, 后续节点上移到被删节点的父节点."""
    nodes = [_node(1), _node(2, 1, day=2), _node(3, 2, day=3)]
    plan = plan_deletion(nodes, 2)
    assert plan.mode is DeletionMode.SHIFT
    assert plan.removed_ids == (2,)
    assert plan.shifted_child_id == 3
    assert plan.new_parent_id == 1
    assert plan.needs_confirmation is False


def test_shift_delete_of_first_node_reparents_to_root() -> None:
    nodes = [_node(1), _node(2, 1, day=2)]
    plan = plan_deletion(nodes, 1)
    assert plan.mode is DeletionMode.SHIFT
    assert plan.shifted_child_id == 2
    assert plan.new_parent_id is None


def test_cascade_delete_for_fork_node() -> None:
    """有两个子节点 = 真正的分叉, 删除需连带整个子树."""
    nodes = [
        _node(1),
        _node(2, 1, day=2),
        _node(3, 1, day=3, kind="branch"),
        _node(4, 3, day=4),
    ]
    plan = plan_deletion(nodes, 1)
    assert plan.mode is DeletionMode.CASCADE
    assert set(plan.removed_ids) == {1, 2, 3, 4}
    assert plan.removed_ids[0] == 1
    assert plan.removed_count == 4
    assert plan.needs_confirmation is True


def test_cascade_delete_for_branch_root_with_single_child() -> None:
    """分支根节点即使只有一个子节点, 删除也意味着整条分支消失."""
    nodes = [_node(1), _node(2, 1, day=2, kind="branch"), _node(3, 2, day=3)]
    plan = plan_deletion(nodes, 2)
    assert plan.mode is DeletionMode.CASCADE
    assert plan.removed_ids == (2, 3)


def test_descendants_are_depth_first() -> None:
    nodes = [
        _node(1),
        _node(2, 1, day=2),
        _node(3, 2, day=3),
        _node(4, 1, day=4),
    ]
    assert sorted(descendant_ids(nodes, 1)) == [2, 3, 4]


def test_plan_delete_unknown_node_raises() -> None:
    with pytest.raises(KeyError):
        plan_deletion([_node(1)], 99)


def test_auto_prune_keeps_newest_entries() -> None:
    nodes = [
        _node(1, kind="auto", day=1),
        _node(2, kind="auto", day=2),
        _node(3, kind="auto", day=3),
        _node(4, kind="auto", day=4),
        _node(5, day=5),
    ]
    assert auto_prune_ids(nodes, 3) == [1]
    assert auto_prune_ids(nodes, 2) == [1, 2]
    assert auto_prune_ids(nodes, 4) == []


def test_auto_prune_ignores_manual_and_protected_nodes() -> None:
    nodes = [
        _node(1, kind="auto", day=1),
        _node(2, kind="auto", day=2),
        _node(3, day=3),
    ]
    assert auto_prune_ids(nodes, 1, protected={1}) == []
    assert auto_prune_ids(nodes, 0) == [1, 2]


def test_auto_prune_respects_keep_zero() -> None:
    nodes = [_node(1, kind="auto", day=1), _node(2, kind="auto", day=2)]
    assert auto_prune_ids(nodes, 0) == [1, 2]
