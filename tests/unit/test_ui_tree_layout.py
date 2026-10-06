"""分支图布局纯函数的单元测试(I-9.1 / I-9.5.1 的判据).

判据全部是**数字**(不启动窗口):

* 父节点居中于它的子节点(取首末孩子中心的中点), 单孩子时不偏;
* 同一层的节点**不重叠**, 且左右顺序 = 兄弟顺序(创建时间升序);
* 一条链的宽度 = 一个框宽(分叉才变宽);
* 剪枝后仍是一棵连续的树(没有指向不存在节点的线);
* 连线端点正好落在父框底边中点与子框顶边中点;
* 字号变大时框跟着变大(布局是像素, 字号是缩放过的);
* **折叠**(I-9.5.1): 不折叠时与旧结果逐字段相同、折叠后仍不重叠且父仍居中、宽度按
  单框算、后代数数得对且只报看得见的那些。
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
    marker_size=12.0,
    marker_inset=3.0,
)


def _tree(*pairs: tuple[str, str | None]) -> list[TreeInput]:
    """按 (节点, 父节点) 造输入, 创建时间按顺序递增(兄弟序 = 参数顺序)."""
    return [
        TreeInput(node_id=node_id, parent_id=parent, created_at=utc_moment(1, index))
        for index, (node_id, parent) in enumerate(pairs)
    ]


def _layout(
    *pairs: tuple[str, str | None], collapsed: tuple[str, ...] = ()
) -> TreeLayout:
    """按 (节点, 父节点) 铺一张图(固定尺寸; ``collapsed`` 给出收起的节点)."""
    return tree_layout(build_tree(_tree(*pairs)), metrics=METRICS, collapsed=collapsed)


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


# -- 折叠(I-9.5.1) -----------------------------------------------------------
#
# 夹具: ``root`` 有两个孩子 ``a`` / ``b``, 而 ``a`` 自己有三个孩子。
# 固定尺寸下(框 100 / 水平间距 10 / 外边距 5)这些数字全是手算得出来的:
#
#   * 不折叠: ``a`` 子树 3x100 + 2x10 = 320, ``root`` 子树 320 + 10 + 100 = 430, 总宽 440;
#   * 折叠 ``a``: 它只占一个框, 于是 100 + 10 + 100 = 210, 总宽 220;
#   * 折叠 ``root``: 只剩它自己, 总宽 110。
_FOLD_TREE: tuple[tuple[str, str | None], ...] = (
    ("root", None),
    ("a", "root"),
    ("a1", "a"),
    ("a2", "a"),
    ("a3", "a"),
    ("b", "root"),
)
#: 折叠 ``a`` 时它藏起来的后代(a1/a2/a3)。
_FOLDED_DESCENDANTS = 3
#: 折叠 ``root`` 时藏起来的后代(a/b/a1/a2/a3)。
_ROOT_DESCENDANTS = 5


def _fingerprint(layout: TreeLayout) -> tuple[object, ...]:
    """一张图里所有能被"折叠集合为空"影响的字段(逐字段对照用)."""
    return (
        layout.boxes,
        layout.edges,
        layout.width,
        layout.height,
        sorted(layout.index),
        layout.collapsed,
        layout.branches,
    )


def test_an_empty_fold_set_lays_out_exactly_like_before() -> None:
    """**最重要的一条**: 不折叠(空集合)时必须与旧行为逐字段相同.

    既有 10 条几何判据与 6 张视觉基线都建在"现在这套铺法"上 —— 折叠若改变了空集合的
    结果, 它们会一起重标。所以这里把三种"等于没折叠"的输入放在一起对照: 不传、
    传空元组、传一个既不存在也不可能有孩子的 id(树里没有的 id 与**叶子**)。
    """
    nodes = build_tree(_tree(*_FOLD_TREE))
    plain = tree_layout(nodes, metrics=METRICS)
    empty = tree_layout(nodes, metrics=METRICS, collapsed=())
    unknown = tree_layout(nodes, metrics=METRICS, collapsed=("没有这个节点",))
    leaf = tree_layout(nodes, metrics=METRICS, collapsed=("a1",))

    assert _fingerprint(empty) == _fingerprint(plain)
    assert _fingerprint(unknown) == _fingerprint(plain)
    assert _fingerprint(leaf) == _fingerprint(plain), "折叠叶子本来就没什么可藏"
    assert plain.collapsed == frozenset()
    assert plain.descendants == {}
    assert plain.width == pytest.approx(440.0), "前提: 这是那棵树的旧宽度"


def test_folding_a_subtree_leaves_its_parent_one_box_wide() -> None:
    """折叠 ``a``: 它的子树不参与布局, 它自己留在原位, 出边一条都不产出."""
    layout = _layout(*_FOLD_TREE, collapsed=("a",))

    assert [box.node_id for box in layout.boxes] == ["root", "a", "b"]
    assert layout.width == pytest.approx(220.0), "折叠后宽度应当只算一个框"
    assert [(edge.parent_id, edge.child_id) for edge in layout.edges] == [
        ("root", "a"),
        ("root", "b"),
    ]
    assert layout.descendants == {"a": _FOLDED_DESCENDANTS}
    assert layout.is_collapsed("a")
    assert layout.can_collapse("a")
    assert not layout.can_collapse("a1"), "没孩子的节点没什么可折叠的"


def test_folding_keeps_the_visible_nodes_centered_and_apart() -> None:
    """折叠之后父节点仍然居中、同级仍然不重叠(折叠只是少了一层, 不是换了铺法)."""
    layout = _layout(*_FOLD_TREE, collapsed=("a",))

    root = layout.box("root")
    left = layout.box("a")
    right = layout.box("b")
    assert root is not None
    assert left is not None
    assert right is not None
    assert root.center_x == pytest.approx((left.center_x + right.center_x) / 2)
    assert left.x + left.width <= right.x
    assert left.y == pytest.approx(root.bottom + METRICS.v_gap)


def test_folding_the_root_leaves_one_box_and_no_edges() -> None:
    """折叠根: 只剩一个框、一条线都没有, 后代数把整棵子树都数进去."""
    layout = _layout(*_FOLD_TREE, collapsed=("root",))

    assert [box.node_id for box in layout.boxes] == ["root"]
    assert layout.edges == ()
    assert layout.descendants == {"root": _ROOT_DESCENDANTS}
    assert layout.width == pytest.approx(110.0)
    assert layout.height == pytest.approx(50.0), "高度只看还剩几层"


def test_the_height_follows_the_visible_rows_only() -> None:
    """高度按**看得见**的最深一层算: 藏起来的那些层不能继续占着可滚高度."""
    deep = _layout(*_FOLD_TREE)
    folded = _layout(*_FOLD_TREE, collapsed=("a",))

    assert deep.height == pytest.approx(170.0), "前提: 不折叠时有 3 层"
    assert deep.height - folded.height == pytest.approx(METRICS.row_height)


def test_a_fold_inside_a_fold_is_ignored() -> None:
    """藏在另一个折叠节点里的折叠不算数: 它连标记都画不出来, 不该报后代数."""
    layout = _layout(*_FOLD_TREE, collapsed=("root", "a"))

    assert layout.descendants == {"root": _ROOT_DESCENDANTS}
    assert not layout.is_collapsed("a")
    assert not layout.can_collapse("a"), "看不见的节点没有折叠入口"


def test_a_branch_has_a_marker_in_the_bottom_right_corner() -> None:
    """折叠标记是**纯函数算出来的**矩形: 在框的右下角、不越出框; 叶子没有标记."""
    layout = _layout(*_FOLD_TREE)

    box = layout.box("a")
    assert box is not None
    rect = layout.marker_rect("a")
    assert rect is not None
    x, y, width, height = rect
    assert (width, height) == (METRICS.marker_size, METRICS.marker_size)
    assert x + width == pytest.approx(box.right - METRICS.marker_inset)
    assert y + height == pytest.approx(box.bottom - METRICS.marker_inset)
    assert box.contains(x, y), "标记的左上角越出了框"
    assert box.contains(x + width, y + height), "标记的右下角越出了框"

    assert layout.marker_rect("a1") is None, "没孩子的节点不该有标记"
    assert layout.marker_rect("没有这个节点") is None


def test_the_marker_wins_the_hit_test_against_its_own_box() -> None:
    """命中的优先级判据: 标记上的点归标记, 框别处归框(免得"想折叠却选中了它")。"""
    layout = _layout(*_FOLD_TREE)

    rect = layout.marker_rect("a")
    box = layout.box("a")
    assert rect is not None
    assert box is not None
    centre = (rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)

    assert layout.marker_at(*centre) == "a"
    assert layout.node_at(*centre) is not None, "标记本来就落在框里"

    elsewhere = (box.x + 2, box.y + 2)
    assert layout.marker_at(*elsewhere) is None
    assert layout.node_at(*elsewhere) is not None
    assert layout.marker_at(-1.0, -1.0) is None


def test_placing_an_empty_forest_gives_no_boxes_and_no_width() -> None:
    """一棵树都没有(全都剪掉了)时给空框表与 0 宽, 而不是负数宽度.

    总宽是"最后一个框的右边"减一个间距得来的: 一个框都没有时那个减法会得出 ``-h_gap``,
    画布的滚动范围跟着变成一个负值。
    """
    from archive_management.ui.tree_layout import _place_forest

    metrics = tree_layout([]).metrics

    boxes, width = _place_forest([], {}, {}, {}, (), metrics)

    assert boxes == {}
    assert width == 0.0
