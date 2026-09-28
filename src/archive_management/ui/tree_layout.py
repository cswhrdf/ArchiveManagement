"""分支图的**布局纯函数**: 谁摆在哪、连线从哪连到哪(I-9.1).

为什么单独一个模块: 一张图能不能看, 九成取决于几何 —— "父节点是不是居中于它的子节点"、
"同级会不会挤在一起"、"剪枝之后还是不是一棵连续的树"这些都能**用数字断言**, 不需要启动
窗口。所以这里只做几何: 输入 :class:`~archive_management.domain.tree.TreeNode` 序列,
输出每个框的位置与每条连线的两个端点, 一行 Tkinter 都不碰 —— 画的那一侧在
:mod:`archive_management.ui.tree_view`。

算法是经典的组织图铺法(叶子定位 + 父取子中点), 自底向上算子树宽度、自顶向下铺 x:

1. **子树宽度** ``max(框宽, 各子树宽度之和 + 间距 x (孩子数 - 1))`` —— 一个节点至少要占
   它自己框那么宽, 孩子多就由孩子决定;
2. **x**: 孩子从左到右依次排, 父节点的**中心**取第一个与最后一个孩子中心的**中点**
   (参考图里 ``Encyclopaedia`` 正好落在 ``Science``/``Culture`` 中间), 再夹进"自己那一段"
   里 —— 夹这一下是为了"父框比孩子那一段还宽"时不会压到兄弟的子树;
3. **y** = ``depth x (框高 + 行距)``, 根在最上面;
4. **连线**: 从父框**底边中点**到子框**顶边中点**(曲线由画的那一侧决定, 这里只给端点)。

"剪枝之后重挂"不用在这里处理: 调用方(:func:`~archive_management.ui.models.branch_tree`)
已经用既有的 ``keep_surviving`` 把被过滤节点的子节点挂到了最近存活祖先上, 所以喂进来的一定
是一棵**连续的**树 —— 这也是本模块的一条判据(见 ``tests/unit/test_ui_tree_layout.py``)。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from archive_management.domain.tree import TreeNode
from archive_management.ui.typography import scaled

# -- 设计尺寸(px 口径, 会随"界面字号"等比缩放) -------------------------------
#
# 这几个数是**画出来的**(不是拍的): 框宽要放得下"两行标题 + 一行小字"的典型中文备份名,
# 行距要留得下连线拐弯。它们不进 ``metrics`` 的间距刻度 —— 那张表管的是"控件之间的间距",
# 这里管的是"图自己的坐标", 所以按 ``dialogs._CARD_TEXT_WIDTH`` 的先例写成具名常量。
#
#: 一个节点的框宽。
TREE_BOX_WIDTH = 168
#: 一个节点的框高(标题两行 + 小字一行 + 上下内边距)。
TREE_BOX_HEIGHT = 64
#: 同级两个框之间的水平间距。
TREE_H_GAP = 20
#: 父子之间的竖直间距(连线就走在这段空白里)。
TREE_V_GAP = 34
#: 图的四周留白: 拖到边上时第一个框不贴着边。
TREE_PADDING = 12
#: 文字距离框边的内边距(与 ``metrics.SPACE_8`` 同值, 但它是**图的坐标**而不是控件间距)。
TREE_TEXT_INSET = 8


@dataclass(frozen=True)
class TreeMetrics:
    """一张图的设计尺寸(已按当前界面字号换算)."""

    box_width: float
    box_height: float
    h_gap: float
    v_gap: float
    padding: float
    text_inset: float

    @property
    def row_height(self) -> float:
        """一层占的高度(框高 + 行距)."""
        return self.box_height + self.v_gap


def default_metrics() -> TreeMetrics:
    """按当前界面字号换算出一套尺寸.

    **字号变大时框要跟着变大**: 布局是像素、字号是缩放过的, 两者不一起变的话文字会溢出框
    (见 PLAN 的 I-9"字号缩放走既有 typography")。
    """
    return TreeMetrics(
        box_width=float(scaled(TREE_BOX_WIDTH)),
        box_height=float(scaled(TREE_BOX_HEIGHT)),
        h_gap=float(scaled(TREE_H_GAP)),
        v_gap=float(scaled(TREE_V_GAP)),
        padding=float(scaled(TREE_PADDING)),
        text_inset=float(scaled(TREE_TEXT_INSET)),
    )


@dataclass(frozen=True)
class NodeBox:
    """一个节点在图上占的矩形(左上角坐标 + 尺寸 + 层深)."""

    node_id: str
    x: float
    y: float
    width: float
    height: float
    depth: int

    @property
    def center_x(self) -> float:
        """水平中点(连线的端点取它)."""
        return self.x + self.width / 2

    @property
    def bottom(self) -> float:
        """底边的 y(父节点连线的起点)."""
        return self.y + self.height

    @property
    def top(self) -> float:
        """顶边的 y(子节点连线的终点)."""
        return self.y

    def contains(self, x: float, y: float) -> bool:
        """点是否落在框里(命中测试的纯函数版本, 供无头用例用)."""
        return (
            self.x <= x <= self.x + self.width and self.y <= y <= self.y + self.height
        )


@dataclass(frozen=True)
class Edge:
    """一条父子连线(端点分别是父框底边中点与子框顶边中点)."""

    parent_id: str
    child_id: str
    start: tuple[float, float]
    end: tuple[float, float]


@dataclass(frozen=True)
class TreeLayout:
    """一整张图: 所有框、所有连线, 以及画布的滚动范围."""

    boxes: tuple[NodeBox, ...]
    edges: tuple[Edge, ...]
    width: float
    height: float
    metrics: TreeMetrics
    #: 按 id 找框(悬停与选中是高频交互, 不做线性扫描)。
    index: Mapping[str, NodeBox] = field(default_factory=dict, repr=False)

    def box(self, node_id: str | None) -> NodeBox | None:
        """取某个节点的框(没有就返回 ``None``)."""
        return None if node_id is None else self.index.get(node_id)

    def node_at(self, x: float, y: float) -> NodeBox | None:
        """点在哪个框里(倒序找: 后画的在上, 与画布同一套顺序)."""
        for box in reversed(self.boxes):
            if box.contains(x, y):
                return box
        return None


def _subtree_widths(
    order: Sequence[str],
    children: Mapping[str, tuple[str, ...]],
    size: TreeMetrics,
) -> dict[str, float]:
    """自底向上算每棵子树要占多宽.

    倒着扫一遍深度优先序就够了 —— ``build_tree`` 保证**父节点一定排在它的孩子前面**,
    所以反过来处理时孩子的宽度总是已经算好了(不必递归, 也不会撞上深度上限)。
    """
    widths: dict[str, float] = {}
    for node_id in reversed(order):
        kids = [kid for kid in children.get(node_id, ()) if kid in widths]
        span = size.box_width
        if kids:
            span = sum(widths[kid] for kid in kids) + size.h_gap * (len(kids) - 1)
        widths[node_id] = max(size.box_width, span)
    return widths


def _place_subtree(
    node_id: str,
    left: float,
    *,
    children: Mapping[str, tuple[str, ...]],
    widths: Mapping[str, float],
    depth_of: Mapping[str, int],
    size: TreeMetrics,
    boxes: dict[str, NodeBox],
) -> float:
    """把这棵子树摆在从 ``left`` 开始的那一段里, 返回它的框中心 x."""
    kids = [kid for kid in children.get(node_id, ()) if kid in depth_of]
    width = widths[node_id]
    if kids:
        cursor = left
        centers: list[float] = []
        for kid in kids:
            centers.append(
                _place_subtree(
                    kid,
                    cursor,
                    children=children,
                    widths=widths,
                    depth_of=depth_of,
                    size=size,
                    boxes=boxes,
                )
            )
            cursor += widths[kid] + size.h_gap
        middle = (centers[0] + centers[-1]) / 2
        half = size.box_width / 2
        # 夹进自己那一段: 父框比孩子那一段还宽时, 不能压到兄弟的子树。
        center = min(max(middle, left + half), left + width - half)
    else:
        center = left + size.box_width / 2
    boxes[node_id] = NodeBox(
        node_id=node_id,
        x=center - size.box_width / 2,
        y=depth_of[node_id] * size.row_height + size.padding,
        width=size.box_width,
        height=size.box_height,
        depth=depth_of[node_id],
    )
    return center


def _node_maps(
    nodes: Sequence[TreeNode],
) -> tuple[list[str], dict[str, tuple[str, ...]], dict[str, int]]:
    """把节点序列拆成(深度优先序, 孩子表, 层深表)."""
    return (
        [node.node_id for node in nodes],
        {node.node_id: node.children for node in nodes},
        {node.node_id: node.depth for node in nodes},
    )


def _place_forest(
    order: Sequence[str],
    children: Mapping[str, tuple[str, ...]],
    widths: Mapping[str, float],
    depth_of: Mapping[str, int],
    size: TreeMetrics,
) -> tuple[dict[str, NodeBox], float]:
    """把整片林子从左到右摆好, 返回框表与总宽度.

    根就是 0 层那批(``build_tree`` 只给根 0 层), 可能多于一棵 —— 父节点缺失与被截断
    的环都表现成"又多了一棵树"。拿层深找根比"谁没有父节点"稳: 父节点可能是它自己。
    """
    boxes: dict[str, NodeBox] = {}
    cursor = size.padding
    for node_id in order:
        if depth_of[node_id] != 0:
            continue
        _place_subtree(
            node_id,
            cursor,
            children=children,
            widths=widths,
            depth_of=depth_of,
            size=size,
            boxes=boxes,
        )
        cursor += widths[node_id] + size.h_gap
    if not boxes:
        return boxes, 0.0
    return boxes, cursor - size.h_gap


def _tree_edges(nodes: Sequence[TreeNode], boxes: Mapping[str, NodeBox]) -> list[Edge]:
    """每一对父子的连线端点(父框底边中点 → 子框顶边中点)."""
    edges: list[Edge] = []
    for node in nodes:
        parent = boxes.get(node.node_id)
        if parent is None:  # pragma: no cover - 每个节点都会被摆到自己的位置上
            continue
        for kid in node.children:
            child = boxes.get(kid)
            if child is None:
                continue
            edges.append(
                Edge(
                    parent_id=node.node_id,
                    child_id=kid,
                    start=(parent.center_x, parent.bottom),
                    end=(child.center_x, child.top),
                )
            )
    return edges


def tree_layout(
    nodes: Sequence[TreeNode], *, metrics: TreeMetrics | None = None
) -> TreeLayout:
    """把一棵(已剪枝的)树铺成图.

    ``nodes`` 是 :func:`~archive_management.domain.tree.build_tree` 给出的深度优先序,
    每个节点带着 ``children`` 与 ``depth`` —— 因此这里直接就知道"谁是谁的孩子、同层第几个",
    不必再按 ``parent_id`` 分一次组(那正是本轮要补上的出口)。

    空输入返回一张空图(尺寸为 0), 调用方据此显示空状态。
    """
    size = default_metrics() if metrics is None else metrics
    if not nodes:
        return TreeLayout((), (), 0.0, 0.0, size, {})

    order, children, depth_of = _node_maps(nodes)
    widths = _subtree_widths(order, children, size)
    boxes, forest_width = _place_forest(order, children, widths, depth_of, size)
    edges = _tree_edges(nodes, boxes)

    deepest = max(depth_of.values())
    return TreeLayout(
        boxes=tuple(boxes[node_id] for node_id in order if node_id in boxes),
        edges=tuple(edges),
        width=max(forest_width + size.padding, size.box_width + size.padding * 2),
        height=deepest * size.row_height + size.box_height + size.padding * 2,
        metrics=size,
        index=boxes,
    )
