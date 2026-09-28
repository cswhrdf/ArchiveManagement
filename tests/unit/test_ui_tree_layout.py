"""分支图布局纯函数的单元测试(I-9.1 的判据).

判据全部是**数字**(不启动窗口):

* 父节点居中于它的子节点(取首末孩子中心的中点), 单孩子时不偏;
* 同一层的节点**不重叠**, 且左右顺序 = 兄弟顺序(创建时间升序);
* 一条链的宽度 = 一个框宽(分叉才变宽);
* 剪枝后仍是一棵连续的树(没有指向不存在节点的线);
* 连线端点正好落在父框底边中点与子框顶边中点;
* 字号变大时框跟着变大(布局是像素, 字号是缩放过的)。
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from archive_management.domain import TreeInput, build_tree
from archive_management.ui.tree_layout import (
    TREE_BOX_HEIGHT,
    TREE_BOX_WIDTH,
    TreeLayout,
    TreeMetrics,
    default_metrics,
    tree_layout,
)
from helpers import utc_moment

pytestmark = [
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("分支图"),
    pytest.mark.story("分支图的几何"),
    pytest.mark.layer("unit"),
]

#: 固定尺寸, 让断言不受"界面字号"设置影响(用例跑在默认基准字号上, 但写死更稳)。
METRICS = TreeMetrics(
    box_width=100.0,
    box_height=40.0,
    h_gap=10.0,
    v_gap=20.0,
    padding=5.0,
    text_inset=4.0,
)


def _tree(*pairs: tuple[str, str | None]) -> list[TreeInput]:
    """按 (节点, 父节点) 造输入, 创建时间按顺序递增(兄弟序 = 参数顺序)."""
    return [
        TreeInput(node_id=node_id, parent_id=parent, created_at=utc_moment(1, index))
        for index, (node_id, parent) in enumerate(pairs)
    ]


def _layout(*pairs: tuple[str, str | None]) -> TreeLayout:
    """按 (节点, 父节点) 铺一张图(固定尺寸)."""
    return tree_layout(build_tree(_tree(*pairs)), metrics=METRICS)


def test_a_single_child_sits_directly_under_its_parent() -> None:
    """单孩子居中不偏: 父子中心 x 相同(这是"看起来不像树"的第一处返工)."""
    layout = _layout(("root", None), ("only", "root"))

    root = layout.box("root")
    child = layout.box("only")
    assert root is not None
    assert child is not None
    assert child.center_x == pytest.approx(root.center_x)
    assert child.y == pytest.approx(root.bottom + METRICS.v_gap)


def test_a_parent_is_centered_between_its_first_and_last_child() -> None:
    """父节点落在首末孩子的中间(参考图里 Encyclopaedia 在 Science/Culture 中间)."""
    layout = _layout(
        ("root", None),
        ("a", "root"),
        ("b", "root"),
        ("c", "root"),
    )

    root = layout.box("root")
    first = layout.box("a")
    last = layout.box("c")
    assert root is not None
    assert first is not None
    assert last is not None
    assert root.center_x == pytest.approx((first.center_x + last.center_x) / 2)


def test_siblings_never_overlap_and_keep_the_source_order() -> None:
    """同一层的框互不重叠, 且左右顺序就是兄弟顺序(创建时间升序)."""
    layout = _layout(
        ("root", None),
        ("a", "root"),
        ("b", "root"),
        ("c", "root"),
    )

    boxes = [box for box in layout.boxes if box.depth == 1]
    assert [box.node_id for box in boxes] == ["a", "b", "c"]
    for left, right in pairwise(boxes):
        assert left.x + left.width <= right.x


def test_a_chain_is_only_one_box_wide() -> None:
    """一条链(每层一个孩子)只占一个框宽: 图不会因为深就变成一大片."""
    layout = _layout(("a", None), ("b", "a"), ("c", "b"), ("d", "c"))

    assert layout.width == pytest.approx(METRICS.box_width + METRICS.padding * 2)
    assert [box.depth for box in layout.boxes] == [0, 1, 2, 3]
    # 每层都在同一个 x 上(单链不偏)。
    assert len({box.x for box in layout.boxes}) == 1


def test_a_fan_widens_with_the_leaf_count() -> None:
    """扇形的宽度随叶子数增长 —— 这也是"漫游是必须的而不是可选的"那条."""
    layout = _layout(*[("root", None), *[(f"g{i}", "root") for i in range(4)]])

    expected = 4 * METRICS.box_width + 3 * METRICS.h_gap + METRICS.padding * 2
    assert layout.width == pytest.approx(expected)


def test_edges_join_parent_bottom_to_child_top() -> None:
    """连线的端点: 父框底边中点 → 子框顶边中点."""
    layout = _layout(("root", None), ("a", "root"), ("b", "root"))

    assert len(layout.edges) == 2
    for edge in layout.edges:
        parent = layout.box(edge.parent_id)
        child = layout.box(edge.child_id)
        assert parent is not None
        assert child is not None
        assert edge.start[0] == pytest.approx(parent.center_x)
        assert edge.start[1] == pytest.approx(parent.bottom)
        assert edge.end[0] == pytest.approx(child.center_x)
        assert edge.end[1] == pytest.approx(child.top)


def test_pruning_keeps_the_forest_connected() -> None:
    """剪枝之后仍是一棵连续的树: 每条线两端都真的有框.

    这是布局与 :func:`~archive_management.ui.models.branch_tree` 之间最容易错开的
    契约 —— 只要两边有一边没走 ``keep_surviving``, 就会画出指向不存在节点的线。
    """
    from archive_management.ui.models import BackupItem, branch_tree

    def item(node_id: str, parent: str | None, *, safety: bool = False) -> BackupItem:
        return BackupItem(
            backup_id=node_id,
            title=node_id,
            created_dt=utc_moment(1, 1),
            created_label="今天 09:00",
            auto=False,
            branch_label="主线",
            size_label="1 MB",
            verified=True,
            parent_id=parent,
            safety=safety,
        )

    # 手动 a → 安全点 s(分支视图里默认不展示) → 手动 c: c 必须上挂到 a。
    items = [
        item("a", None),
        item("s", "a", safety=True),
        item("c", "s"),
    ]
    nodes = branch_tree(items)
    assert {node.node_id for node in nodes} == {"a", "c"}
    assert next(node for node in nodes if node.node_id == "a").children == ("c",)

    layout = tree_layout(nodes, metrics=METRICS)
    assert {box.node_id for box in layout.boxes} == {"a", "c"}
    assert [(edge.parent_id, edge.child_id) for edge in layout.edges] == [("a", "c")]


def test_an_empty_tree_lays_out_to_nothing() -> None:
    """空树不给尺寸(调用方据此显示空状态), 也不报错."""
    layout = tree_layout([])

    assert layout.boxes == ()
    assert layout.edges == ()
    assert (layout.width, layout.height) == (0.0, 0.0)


def test_boxes_get_taller_when_the_font_scale_grows() -> None:
    """字号变大时框跟着变大: 布局是像素, 字号是缩放过的, 不一起变文字就会溢出框."""
    from archive_management.ui import typography

    try:
        small = default_metrics()
        typography.set_base_font_px(typography.BASE_FONT_PX + 4)
        large = default_metrics()
    finally:
        typography.set_base_font_px(typography.BASE_FONT_PX)

    assert large.box_height > small.box_height
    assert large.box_width > small.box_width
    assert large.box_height >= TREE_BOX_HEIGHT
    assert small.box_width >= TREE_BOX_WIDTH


def test_node_at_hits_the_box_that_was_drawn_last() -> None:
    """命中测试: 点在框里就命中它, 点在空白处返回 ``None``."""
    layout = _layout(("root", None), ("a", "root"), ("b", "root"))

    box = layout.box("a")
    assert box is not None
    assert layout.node_at(box.center_x, box.y + 1) is not None
    assert layout.node_at(-1.0, -1.0) is None
