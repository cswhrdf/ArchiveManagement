"""备份分支树纯计算模型的单元测试."""

from __future__ import annotations

from datetime import datetime

import pytest

from archive_management.domain import BackupNode, TreeInput, build_tree, tree_depths
from helpers import utc_moment

pytestmark = [
    pytest.mark.domain,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("分支树计算"),
    pytest.mark.story("分支树与时间线排序"),
    pytest.mark.layer("unit"),
]


def _dt(day: int, hour: int = 9) -> datetime:
    """返回固定 UTC 时刻(实现见 tests/helpers.py 的 utc_moment)."""
    return utc_moment(day, hour)


def _input(node_id: str, parent_id: str | None = None, day: int = 1) -> TreeInput:
    return TreeInput(node_id=node_id, parent_id=parent_id, created_at=_dt(day))


def test_build_tree_orders_parent_before_children() -> None:
    rows = build_tree(
        [
            _input("c", "b", day=3),
            _input("a", None, day=1),
            _input("b", "a", day=2),
        ]
    )
    assert [row.node_id for row in rows] == ["a", "b", "c"]
    assert [row.depth for row in rows] == [0, 1, 2]
    assert rows[0].children == ("b",)
    assert rows[0].is_root is True
    assert rows[2].is_root is False


def test_build_tree_keeps_branches_as_siblings_ordered_by_time() -> None:
    rows = build_tree(
        [
            _input("root", None, day=1),
            _input("later", "root", day=5),
            _input("earlier", "root", day=2),
        ]
    )
    assert [row.node_id for row in rows] == ["root", "earlier", "later"]
    assert rows[0].children == ("earlier", "later")
    assert [row.depth for row in rows[1:]] == [1, 1]


def test_build_tree_treats_orphans_as_roots() -> None:
    rows = build_tree([_input("orphan", "missing", day=2), _input("root", None, day=1)])
    assert [row.node_id for row in rows] == ["root", "orphan"]
    assert all(row.depth == 0 for row in rows)


def test_build_tree_breaks_cycles_without_losing_nodes() -> None:
    rows = build_tree([_input("a", "b"), _input("b", "a")])
    assert {row.node_id for row in rows} == {"a", "b"}
    assert len(rows) == 2


def test_build_tree_ignores_self_parent() -> None:
    rows = build_tree([_input("a", "a")])
    assert [row.node_id for row in rows] == ["a"]
    assert rows[0].depth == 0


def test_build_tree_handles_multiple_roots() -> None:
    rows = build_tree(
        [
            _input("second", None, day=4),
            _input("first", None, day=2),
            _input("child", "first", day=3),
        ]
    )
    assert [row.node_id for row in rows] == ["first", "child", "second"]


def test_tree_depths_returns_mapping() -> None:
    depths = tree_depths(
        [_input("a", None, day=1), _input("b", "a", day=2), _input("c", "b", day=3)]
    )
    assert depths == {"a": 0, "b": 1, "c": 2}


def test_tree_node_rejects_negative_depth() -> None:
    from archive_management.domain.tree import TreeNode

    with pytest.raises(ValueError):
        TreeNode(node_id="a", parent_id=None, depth=-1, children=(), is_root=False)


def test_tree_inputs_maps_domain_nodes() -> None:
    from archive_management.domain import tree_inputs

    nodes = [
        BackupNode(id=1, game_id=7, node_kind="manual"),
        BackupNode(id=2, game_id=7, parent_id=1, node_kind="branch"),
        BackupNode(game_id=7, node_kind="manual"),  # 缺 id, 应被忽略
    ]
    inputs = tree_inputs(nodes)
    assert [item.node_id for item in inputs] == ["1", "2"]
    assert inputs[1].parent_id == "1"


def test_build_tree_empty_input() -> None:
    assert build_tree([]) == []
