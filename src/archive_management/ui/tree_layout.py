"""分支图的**布局纯函数**: 谁摆在哪、连线从哪连到哪.

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

**折叠**: ``collapsed`` 给出"收起子树"的节点 id 集合。折叠一个节点 = 它的整棵子树
**不参与布局**(宽度只算一个框、不产出它的出边), 但它自己留在原位 —— 因此布局只需要先算出
"哪些节点还看得见", 后面每一步都在那个集合上算。口径是: 画面可以藏,
语义不许动(折叠不改选中), 藏起来的东西必须在框上留下可发现的痕迹(后代数 + 标记)。
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
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
#: 折叠标记的边长(`+`/`-` 那个小方块)。按字号缩放 —— 它要跟着框一起变大。
#: 2026-10-03 用户反馈"符号有点小", 14 → 18: 它同时是**命中区**, 放大之后不仅看得清,
#: 也更好点(符号本身的视觉大小由 ``tree_view`` 那边的字号决定, 两个一起调)。
TREE_MARKER_SIZE = 18
#: 折叠标记距框右下角的内边距。
TREE_MARKER_INSET = 4


@dataclass(frozen=True)
class TreeMetrics:
    """一张图的设计尺寸(已按当前界面字号换算)."""

    box_width: float
    box_height: float
    h_gap: float
    v_gap: float
    padding: float
    text_inset: float
    #: 折叠标记的边长与它距框右下角的内边距。
    marker_size: float
    marker_inset: float

    @property
    def row_height(self) -> float:
        """一层占的高度(框高 + 行距)."""
        return self.box_height + self.v_gap


def default_metrics() -> TreeMetrics:
    """按当前界面字号换算出一套尺寸.

    **字号变大时框要跟着变大**: 布局是像素、字号是缩放过的, 两者不一起变的话文字会溢出框
    (字号缩放走既有的 typography)。
    """
    return TreeMetrics(
        box_width=float(scaled(TREE_BOX_WIDTH)),
        box_height=float(scaled(TREE_BOX_HEIGHT)),
        h_gap=float(scaled(TREE_H_GAP)),
        v_gap=float(scaled(TREE_V_GAP)),
        padding=float(scaled(TREE_PADDING)),
        text_inset=float(scaled(TREE_TEXT_INSET)),
        marker_size=float(scaled(TREE_MARKER_SIZE)),
        marker_inset=float(scaled(TREE_MARKER_INSET)),
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
    def right(self) -> float:
        """右边的 x(折叠标记从它往左排)."""
        return self.x + self.width

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
    #: 真的生效的折叠节点(与传入集合取过交集: 树里没有的 id 与叶子都不算)。
    collapsed: frozenset[str] = frozenset()
    #: 每个折叠节点被藏起来的后代总数(说明行上的 "· N 个后代")。
    descendants: Mapping[str, int] = field(default_factory=dict, repr=False)
    #: 有孩子的节点(只有它们才会有折叠标记 —— 没孩子就没什么可折叠的)。
    branches: frozenset[str] = frozenset()

    def box(self, node_id: str | None) -> NodeBox | None:
        """取某个节点的框(没有就返回 ``None``)."""
        return None if node_id is None else self.index.get(node_id)

    def is_collapsed(self, node_id: str | None) -> bool:
        """这个节点当前是不是折叠着的(它的子树没画出来)."""
        return node_id is not None and node_id in self.collapsed

    def can_collapse(self, node_id: str | None) -> bool:
        """这个节点有没有孩子 —— 没有就不给标记(点了也没东西可藏)."""
        return node_id is not None and node_id in self.branches

    def marker_rect(self, node_id: str) -> tuple[float, float, float, float] | None:
        """折叠标记的矩形 ``(x, y, 宽, 高)``, 没孩子的节点没有标记.

        纯函数(只用到框与度量), 因此无头用例可以直接断言"标记在框的右下角、不越出框"。
        """
        box = self.box(node_id)
        if box is None or not self.can_collapse(node_id):
            return None
        size = self.metrics.marker_size
        inset = self.metrics.marker_inset
        return (box.right - size - inset, box.bottom - size - inset, size, size)

    def marker_at(self, x: float, y: float) -> str | None:
        """点落在哪个节点的折叠标记里(倒序找: 后画的在上, 与 ``node_at`` 同一套).

        命中优先级里标记排在框**前面**: 否则"想折叠却选中了它"。
        """
        for box in reversed(self.boxes):
            rect = self.marker_rect(box.node_id)
            if rect is None:
                continue
            mx, my, width, height = rect
            if mx <= x <= mx + width and my <= y <= my + height:
                return box.node_id
        return None

    def node_at(self, x: float, y: float) -> NodeBox | None:
        """点在哪个框里(倒序找: 后画的在上, 与画布同一套顺序)."""
        for box in reversed(self.boxes):
            if box.contains(x, y):
                return box
        return None


def _visible_ids(
    order: Sequence[str],
    children: Mapping[str, tuple[str, ...]],
    collapsed: Collection[str],
) -> list[str]:
    """哪些节点还看得见: 从根往下走, 碰到折叠节点就不再进它的子树.

    ``order`` 是深度优先序(**父一定在子前面**), 所以一趟就够; 被跳过的节点要把它的
    孩子也收进 ``hidden``—— 只跳过那一个的话孙辈会被当成可见的。
    """
    visible: list[str] = []
    hidden: set[str] = set()
    for node_id in order:
        kids = children.get(node_id, ())
        if node_id in hidden:
            hidden.update(kids)
            continue
        visible.append(node_id)
        if node_id in collapsed:
            hidden.update(kids)
    return visible


def _descendant_counts(
    order: Sequence[str], children: Mapping[str, tuple[str, ...]]
) -> dict[str, int]:
    """每个节点的后代总数(含间接后代, 不含自己).

    倒着扫一遍深度优先序: 父在子前, 所以轮到某个节点时它的孩子已经数好了。
    """
    counts: dict[str, int] = {}
    for node_id in reversed(order):
        counts[node_id] = sum(
            counts[kid] + 1 for kid in children.get(node_id, ()) if kid in counts
        )
    return counts


def _subtree_widths(
    visible: Sequence[str],
    children: Mapping[str, tuple[str, ...]],
    size: TreeMetrics,
    shown: Collection[str],
) -> dict[str, float]:
    """自底向上算每棵子树要占多宽.

    倒着扫一遍深度优先序就够了 —— ``build_tree`` 保证**父节点一定排在它的孩子前面**,
    所以反过来处理时孩子的宽度总是已经算好了(不必递归, 也不会撞上深度上限)。

    ``shown`` 是还看得见的节点: 折叠节点的孩子被藏起来了, 于是它的子树宽度就是**一个框宽**
    —— 这正是"折叠后同级兄弟往前排"的来源。
    """
    widths: dict[str, float] = {}
    for node_id in reversed(visible):
        kids = [kid for kid in children.get(node_id, ()) if kid in shown]
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
    shown: Collection[str],
    size: TreeMetrics,
    boxes: dict[str, NodeBox],
) -> float:
    """把这棵子树摆在从 ``left`` 开始的那一段里, 返回它的框中心 x."""
    kids = [kid for kid in children.get(node_id, ()) if kid in shown]
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
                    shown=shown,
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
    visible: Sequence[str],
    children: Mapping[str, tuple[str, ...]],
    widths: Mapping[str, float],
    depth_of: Mapping[str, int],
    shown: Collection[str],
    size: TreeMetrics,
) -> tuple[dict[str, NodeBox], float]:
    """把整片林子从左到右摆好, 返回框表与总宽度.

    根就是 0 层那批(``build_tree`` 只给根 0 层), 可能多于一棵 —— 父节点缺失与被截断
    的环都表现成"又多了一棵树"。拿层深找根比"谁没有父节点"稳: 父节点可能是它自己。
    """
    boxes: dict[str, NodeBox] = {}
    cursor = size.padding
    for node_id in visible:
        if depth_of[node_id] != 0:
            continue
        _place_subtree(
            node_id,
            cursor,
            children=children,
            widths=widths,
            depth_of=depth_of,
            shown=shown,
            size=size,
            boxes=boxes,
        )
        cursor += widths[node_id] + size.h_gap
    if not boxes:
        return boxes, 0.0
    return boxes, cursor - size.h_gap


def _tree_edges(nodes: Sequence[TreeNode], boxes: Mapping[str, NodeBox]) -> list[Edge]:
    """每一对父子的连线端点(父框底边中点 → 子框顶边中点).

    折叠节点的出边不用另外判断: 它的孩子根本没被摆到 ``boxes`` 里, 下面那道
    ``child is None`` 就把它们滤掉了 —— "不产出折叠节点的出边"由此自动成立。
    """
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


@dataclass(frozen=True)
class _Folds:
    """折叠在布局里的三个结论(见 :func:`_resolve_folds`)."""

    folded: frozenset[str]
    visible: list[str]
    branches: frozenset[str]


def _resolve_folds(
    order: Sequence[str],
    children: Mapping[str, tuple[str, ...]],
    collapsed: Collection[str],
) -> _Folds:
    """把调用方给的折叠集合收敛成"真的生效的那几个", 并给出还看得见的节点.

    * ``branches``: 有孩子的节点(只有它们能折叠);
    * ``folded``: 与它**取交集** —— 树里没有的 id 与**叶子**都不算(叶子本来就没什么可藏);
    * ``visible``: 从根往下走, 碰到折叠节点就不再进它的子树。
    """
    branches = frozenset(node_id for node_id in order if children.get(node_id))
    folded = frozenset(node_id for node_id in collapsed if node_id in branches)
    return _Folds(folded, _visible_ids(order, children, folded), branches)


def _build_layout(
    folds: _Folds,
    boxes: Mapping[str, NodeBox],
    edges: Sequence[Edge],
    *,
    forest_width: float,
    counts: Mapping[str, int],
    deepest: int,
    size: TreeMetrics,
) -> TreeLayout:
    """把算好的零件组装成 :class:`TreeLayout`.

    两处"取交集"都在这: 宽度/高度都按**看得见的**算(藏起来的层不该继续占着可滚范围),
    而"报哪些折叠"也只报看得见的 —— 藏在另一个折叠节点肚子里的那些连标记都画不出来。
    """
    visible = folds.visible
    return TreeLayout(
        boxes=tuple(boxes[node_id] for node_id in visible if node_id in boxes),
        edges=tuple(edges),
        width=max(forest_width + size.padding, size.box_width + size.padding * 2),
        height=deepest * size.row_height + size.box_height + size.padding * 2,
        metrics=size,
        index=boxes,
        collapsed=folds.folded.intersection(visible),
        descendants={
            node_id: counts[node_id] for node_id in visible if node_id in folds.folded
        },
        branches=folds.branches.intersection(visible),
    )


def tree_layout(
    nodes: Sequence[TreeNode],
    *,
    metrics: TreeMetrics | None = None,
    collapsed: Collection[str] = (),
) -> TreeLayout:
    """把一棵(已剪枝的)树铺成图.

    ``nodes`` 是 :func:`~archive_management.domain.tree.build_tree` 给出的深度优先序,
    每个节点带着 ``children`` 与 ``depth`` —— 因此这里直接就知道"谁是谁的孩子、同层第几个",
    不必再按 ``parent_id`` 分一次组(那正是本轮要补上的出口)。

    ``collapsed`` 是"收起子树"的节点 id 集合: 折叠一个节点 = 它的整棵子树不参与
    布局, 但它自己留在原位。集合会被**取交集**: 树里不存在的 id、以及**叶子**(没孩子)
    都不算折叠 —— 否则调用方多留一个旧 id 就会让图莫名少一层。

    空输入返回一张空图(尺寸为 0), 调用方据此显示空状态。
    """
    size = default_metrics() if metrics is None else metrics
    if not nodes:
        return TreeLayout((), (), 0.0, 0.0, size, {})

    order, children, depth_of = _node_maps(nodes)
    folds = _resolve_folds(order, children, collapsed)
    shown = set(folds.visible)
    widths = _subtree_widths(folds.visible, children, size, shown)
    boxes, forest_width = _place_forest(
        folds.visible, children, widths, depth_of, shown, size
    )
    return _build_layout(
        folds,
        boxes,
        _tree_edges(nodes, boxes),
        forest_width=forest_width,
        counts=_descendant_counts(order, children),
        deepest=max(depth_of[node_id] for node_id in folds.visible),
        size=size,
    )
