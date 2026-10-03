"""分支视图的**图画布**(I-9.2).

卡片做不出那张参考图, 原因是机制上的: 卡片是"一维纵向流水 + 文本前缀缩进", 而图要求
"兄弟横排 + 父子之间有色连线"。所以分支视图换成这里的 ``Canvas`` 绘制, 时间线视图
继续用卡片(它的多行正文在纵向流水里是对的)。

几何全部来自 :mod:`archive_management.ui.tree_layout` 的纯函数, 本模块只负责"把坐标变成
item": 一个框 = **一个 item**(平滑多边形, 圆角)、一条连线 = **一个 item**(``smooth=True``
的曲线)、文字 = 每框两条(标题 + 小字)。item 数是确定性数字, 比计时稳, 因此它的判据是
"``canvas.find_all()`` 的长度 == 框数 + 连线数 + 文字条数"(见
``tests/integration/test_gui_branch_graph.py``)。

实测过的三件事(见 PLAN 的 I-9):

* **曲线不贵**: 400 节点一轮"清空 + 重画"的实测里, 只画方框+文字 7.4ms、加直线段 7.1ms、
  加 Tk 的 ``smooth=True`` 曲线 6.9ms —— 在噪声里, 而且比自己采样贝塞尔便宜, 所以连线走曲线。
  注意 Tk Canvas **不做抗锯齿**(直线也一样), 别指望曲线更"干净"。
* **不要滚动条**: ``CTkScrollableFrame`` 只能选一个方向, 而树一分叉就比视口宽。拖动用
  ``scan_mark``/``scan_dragto``、方向键/WASD 与四边箭头用 ``xview_scroll``/``yview_scroll``、
  四边箭头只在"那个方向真的还有内容"时显示(``xview()``/``yview()`` 的跨度 < 1)。
* **箭头"收起"与"显示"都要真的动到控件**: ``place()`` **无参调用只查询配置** ——
  实测 ``place_forget()`` 之后再 ``place()``, ``winfo_manager()`` 仍是空串, 于是箭头
  一旦被收起就再也露不出来。所以落点每次重新给一遍, 而"现在露着哪几条"只能量控件
  本身(``winfo_manager``), 不能拿几何当判据(几何只说明"那边还有内容")。
* **拖到框上不能变成选中**: 按下时先命中测试 —— 中了就走选中, 没中才进入平移;
  松开时的位移小于阈值才算"点击", 于是"从框上拖走"不会顺手选中它。
"""

from __future__ import annotations

import tkinter
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from typing import Any, Literal

import customtkinter as ctk

from archive_management.i18n import tr
from archive_management.ui import typography
from archive_management.ui.metrics import RADIUS_MD, RADIUS_NONE
from archive_management.ui.models import BackupItem, branch_tree
from archive_management.ui.palette import Palette
from archive_management.ui.textfit import fit_text
from archive_management.ui.tree_layout import TreeLayout, tree_layout
from archive_management.ui.widgets import HoverTip

#: 当前节点的标记(与主窗口 ``_CURRENT_MARK`` 同一个符号, 画在小字那一行里)。
CURRENT_MARK = "●"

#: 折叠标记的两个符号(纯符号, 不进 i18n —— 与 ``●``/箭头符号同一类)。
#: ``-`` = 已经展开着(再点就收起), ``+`` = 收起状态(点一下展开)。
#: 用户 2026-10-03 明确要求换成这两个: 三角箭头(`▾`/`▸`)在这么小的标记里看不出"能不能点",
#: 而加减号是文件树里几十年的一贯表达 —— 配色不变。
_FOLD_GLYPHS: dict[bool, str] = {False: "-", True: "+"}

#: 连线的平滑程度: Tk 自己在两个控制点之间插值, 数字越大越圆(也越像"没拐弯")。
_EDGE_SPLINE_STEPS = 12
#: 圆角框的角半径(与按钮同档, 别用更大的值 —— 框很小, 半径大了会像胶囊)。
_CORNER_RADIUS = RADIUS_MD
#: 画布自己的焦点环宽度。用 Tk 自带的 ``highlightthickness`` 画(见 ``redraw``):
#: 画布是**普通** ``tkinter.Canvas``, 不在 ``keyboard`` 的补丁范围内, 所以焦点提示得自己操办;
#: 实测聚焦时画布边缘像素就是 ``focus_ring``、失焦时是画布底色(看不见环)。
_FOCUS_RING_WIDTH = 3
#: "点击"与"拖动"的分界: 松开时位移小于这个像素数才算点击。
_CLICK_SLOP = 4
#: 方向键/箭头一次移动几"个单位"(单位由 ``xscrollincrement`` 定成一个节点列宽)。
_KEY_STEP = 1

Arrow = Literal["left", "right", "up", "down"]
#: 键盘绑定到方向: 元组写上类型, 免得 ``partial`` 那里被推成 ``str``(mypy)。
#: 方向键与 WASD 都绑(用户要求"顺手"): 左手在 WASD 上时不用离开去看方向键。
#: **大小写各绑一条**: Tk 里 ``Shift+d`` 的 keysym 是 ``D``, ``<d>`` 收不到它(实测)。
_KEY_BINDINGS: tuple[tuple[str, Arrow], ...] = (
    ("<Left>", "left"),
    ("<Right>", "right"),
    ("<Up>", "up"),
    ("<Down>", "down"),
    ("<w>", "up"),
    ("<W>", "up"),
    ("<a>", "left"),
    ("<A>", "left"),
    ("<s>", "down"),
    ("<S>", "down"),
    ("<d>", "right"),
    ("<D>", "right"),
)
#: 箭头按钮上的符号(纯符号, 不进 i18n —— 与 ``●``/``+`` 这类标记同一类)。
#: 四个符号是**故意选的**: 单角引号形的左右箭头与上下两个修饰符箭头, 比 ASCII 的
#: 尖括号 / 脱字符更像"一块可以点的小箭头", 宽度也稳定。
_ARROW_GLYPHS: dict[Arrow, str] = {
    "left": "‹",  # noqa: RUF001 - 上面写了: 故意用单角引号当箭头
    "right": "›",  # noqa: RUF001
    "up": "˄",  # noqa: RUF001
    "down": "˅",
}
#: 四条箭头的落点(画布四条边的中点, 朝外). 放在模块级是为了"收起后再显示"能
#: 原样放回去 —— ``place_forget`` 之后必须**带参**重新 ``place``, 无参不算数。
_ARROW_SPOTS: dict[Arrow, dict[str, Any]] = {
    "left": {"relx": 0.0, "rely": 0.5, "anchor": "w", "x": 2},
    "right": {"relx": 1.0, "rely": 0.5, "anchor": "e", "x": -2},
    "up": {"relx": 0.5, "rely": 0.0, "anchor": "n", "y": 2},
    "down": {"relx": 0.5, "rely": 1.0, "anchor": "s", "y": -2},
}


@dataclass(frozen=True)
class NodeTexts:
    """一个节点的框里那两行字(标题最多两行 + 小字一行)与"被裁掉时能回看的全文"."""

    title: str
    meta: str
    full: str

    @property
    def truncated(self) -> bool:
        """有没有哪一行真的被裁过(悬停提示只在真的被裁时才挂, 见 I-3 那条规则)."""
        return bool(self.full)


@dataclass(frozen=True)
class ItemCounts:
    """一次重画画出来的 item 数(确定性数字, 用来当渲染基准)."""

    boxes: int
    edges: int
    texts: int
    #: 折叠标记: 每个有孩子的框一个(I-9.5.2)。
    markers: int

    @property
    def total(self) -> int:
        """画布上应有的 item 总数."""
        return self.boxes + self.edges + self.texts + self.markers


def _rounded_points(
    x: float, y: float, width: float, height: float, radius: float
) -> list[float]:
    """一个圆角矩形的点列(每个角两个点, 交给 ``smooth=True`` 去倒角).

    为什么不用 4 个点的矩形: ``smooth=True`` 会把**整条边**当曲线, 画出来像叶子;
    每个角给两个点, 平滑只会发生在角上, 直边仍然是直的。
    """
    r = min(radius, width / 2, height / 2)
    return [
        x + r,
        y,
        x + width - r,
        y,
        x + width,
        y,
        x + width,
        y + r,
        x + width,
        y + height - r,
        x + width,
        y + height,
        x + width - r,
        y + height,
        x + r,
        y + height,
        x,
        y + height,
        x,
        y + height - r,
        x,
        y + r,
        x,
        y,
    ]


def _edge_points(start: tuple[float, float], end: tuple[float, float]) -> list[float]:
    """一条组织图式的 S 形连线: 从父框底边中点下到子框顶边中点.

    中间给两个控制点(各占一半高度), 于是"先竖着出来、再横着走一点、再竖着进去",
    这正是参考图里那种连线; 直线也能用, 但曲线读起来更像层级而不是"碰巧对齐"。
    """
    sx, sy = start
    ex, ey = end
    middle = (sy + ey) / 2
    return [sx, sy, sx, middle, ex, middle, ex, ey]


class TreeView:
    """分支视图: 一块画布 + 四条按需出现的边箭头.

    画布**不进 Tab 焦点链里的节点**, 也不做"回车进选中项"(已确认接受的代价: 图要的是
    形状而不是信息密度)。保留的键盘能力是**漫游**(方向键 / WASD 平移) —— 这与将来的
    "方向键在节点间走"冲突, 那时要先改绑定(例如 ``Ctrl+方向键``), 不能直接占用,
    见 PLAN 的 I-9。
    """

    def __init__(
        self,
        parent: ctk.CTkBaseClass,
        *,
        on_select: Callable[[BackupItem], None],
        on_hover: Callable[[str | None], None],
    ) -> None:
        """建好画布与箭头, 接上鼠标/键盘事件(此时还没有内容)."""
        self._on_select = on_select
        self._on_hover = on_hover
        self._items: list[BackupItem] = []
        self._by_id: dict[str, BackupItem] = {}
        self._nodes: list[Any] = []
        self._parent_of: dict[str, str] = {}
        #: 当前收起的节点(**不持久化**: 折叠是看图时的临时收拢, 见 PLAN I-9.5 的口径)。
        self._collapsed: set[str] = set()
        self._texts: dict[str, NodeTexts] = {}
        self._layout: TreeLayout = tree_layout([])
        self._selected: str | None = None
        self._hovered: str | None = None
        self._press_node: str | None = None
        self._press_fold = False
        self._press_at: tuple[int, int] = (0, 0)
        self._box_items: dict[str, int] = {}
        self._text_items: dict[str, tuple[int, int]] = {}
        self._marker_items: dict[str, int] = {}
        self._title_font = typography.font(typography.FONT_STRONG, weight="bold")
        self._meta_font = typography.font(typography.FONT_HINT)
        # 折叠标记的符号字号与框里的小字**分开**: `+`/`-` 是唯一靠字形大小表达状态的图形,
        # 借说明行的 11px 会偏小(用户 2026-10-03 反馈), 用"字形档"13px。
        self._marker_font = typography.font(typography.FONT_GLYPH)
        #: 最近一次重画用的调色板: 鼠标/键盘事件里没有调用方可以问, 而折叠需要重画。
        self._palette: Palette | None = None

        self.frame = ctk.CTkFrame(
            parent, fg_color="transparent", corner_radius=RADIUS_NONE
        )
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(0, weight=1)
        self.canvas = tkinter.Canvas(
            self.frame,
            highlightthickness=_FOCUS_RING_WIDTH,
            borderwidth=0,
            takefocus=1,
        )
        self.canvas.grid(row=0, column=0, sticky="nsew")
        # 自己管显示时机的悬停提示: 框里的文字被裁时可以回看全文(见 widgets.HoverTip).
        self._tip = HoverTip(self.canvas)
        self._arrows = self._build_arrows()
        self._wire_canvas()

    # -- 内容 ---------------------------------------------------------------

    def set_items(
        self, items: Sequence[BackupItem], *, include_safety: bool = False
    ) -> None:
        """换一批节点: 重新剪枝、重算几何、重裁文字(下一次重绘生效).

        折叠集合在这里与现有节点**取交集** —— 换游戏/换筛选之后旧 id 不该继续压着图
        (那些节点已经不在这批数据里了)。
        """
        self._items = list(items)
        self._by_id = {item.backup_id: item for item in self._items}
        self._nodes = list(branch_tree(self._items, include_safety=include_safety))
        self._parent_of = {}
        for node in self._nodes:
            for kid in node.children:
                self._parent_of[kid] = node.node_id
        self._collapsed &= {node.node_id for node in self._nodes}
        self._relayout()
        self._selected = None
        self._hovered = None
        # 数据换了: 悬停提示指向的旧节点可能已经不存在, 直接收起.
        self._tip.hide()

    def _relayout(self) -> None:
        """按当前的折叠集合重算几何与文字(换数据/收起/展开都走这里)."""
        self._layout = tree_layout(self._nodes, collapsed=frozenset(self._collapsed))
        self._texts = self._build_texts(self._nodes)

    def _build_texts(self, nodes: Sequence[Any]) -> dict[str, NodeTexts]:
        """把每个节点的两行字裁进框里(用与列表/海报同一套 ``fit_text``).

        被裁掉的行会把**全文**记在 ``NodeTexts.full`` 里 —— 画布上的文字没有 tooltip 可挂,
        而"截断必须能回看"是 I-3 的硬规则, 所以悬停提示的文案在这里就备好了(每框两行都
        放得下时 ``full`` 是空串, 那时不弹提示).
        """
        texts: dict[str, NodeTexts] = {}
        for node in nodes:
            if node.node_id not in self._by_id:  # pragma: no cover - 只喂剪枝后的节点
                continue
            texts[node.node_id] = self._texts_for(node.node_id)
        return texts

    def _meta_for(self, node_id: str) -> str:
        """说明行的全文(创建时间 · 类型; 折叠时换成"藏了什么").

        折叠口径的落点(PLAN I-9.5): **画面可以藏, 但藏起来的东西必须在框上留下可发现的
        痕迹**。实测框只放得下一件事(框宽 168 - 两侧内衬 = 152px), 而时间/类型在右侧面板
        里还有一份、"藏了谁"只有这里能说 —— 所以折叠时**只讲藏了什么**, 并把最要紧的
        "藏了选中项/当前节点"排在最前面(被裁也要先保住它)。
        """
        item = self._by_id[node_id]
        head = f"{CURRENT_MARK} " if item.is_current else ""
        folded = self._layout.descendants.get(node_id)
        if folded is None:
            return f"{head}{item.created_label} · {item.kind_label}"
        notes: list[str] = []
        if self._hides_something(node_id):
            if self._hidden_under(node_id, self._selected):
                notes.append(tr("graph.contains_selected"))
            if self._hidden_under(node_id, self._current_id()):
                notes.append(tr("graph.contains_current"))
        notes.append(tr("graph.descendants", count=folded))
        return f"{head}{' · '.join(notes)}"

    def _current_id(self) -> str | None:
        """当前节点的 id(画着 ``●`` 的那个; 没有就 ``None``)."""
        for node_id, item in self._by_id.items():
            if item.is_current:
                return node_id
        return None

    def _hidden_under(self, root_id: str, node_id: str | None) -> bool:
        """``node_id`` 是不是藏在 ``root_id`` 的子树里(**不含它自己**).

        沿剪枝**之后**的父子表往上走 —— ``TreeNode.parent_id`` 是剪枝前的原始父节点,
        被剪掉的中间层会让它指向一个不在图上的 id(那正是本函数不能用它当据的原因)。
        """
        if node_id is None:
            return False
        cursor = self._parent_of.get(node_id)
        for _ in range(len(self._nodes) + 1):
            if cursor is None:
                return False
            if cursor == root_id:
                return True
            cursor = self._parent_of.get(cursor)
        return False  # pragma: no cover - 环已在 build_tree 里被截断

    def _texts_for(self, node_id: str) -> NodeTexts:
        """某一个节点现在该显示的两行字(裁过), 以及被裁时的全文."""
        item = self._by_id[node_id]
        metrics = self._layout.metrics
        width = max(1, int(metrics.box_width - metrics.text_inset * 2))
        title = item.display_title
        meta = self._meta_for(node_id)
        shown_title = fit_text(title, self._title_font, width, max_lines=2)
        shown_meta = fit_text(meta, self._meta_font, width)
        clipped = [
            full
            for full, shown in ((title, shown_title), (meta, shown_meta))
            if full != shown
        ]
        return NodeTexts(title=shown_title, meta=shown_meta, full="\n".join(clipped))

    def _refresh_meta(self, node_id: str) -> None:
        """只重算一个框的说明行(选中/折叠变化时用: 别的行没变, 不必整幅重画)."""
        items = self._text_items.get(node_id)
        if items is None:  # pragma: no cover - 只有画过的框会来
            return
        texts = self._texts_for(node_id)
        self._texts[node_id] = texts
        self.canvas.itemconfigure(items[1], text=texts.meta)

    @property
    def layout(self) -> TreeLayout:
        """当前这张图(用例据此断言几何)."""
        return self._layout

    @property
    def collapsed(self) -> frozenset[str]:
        """当前收起的节点(**不持久化**: 只活在这个视图的内存里, 见 PLAN I-9.5)."""
        return frozenset(self._collapsed)

    def can_toggle(self, node_id: str | None) -> bool:
        """这个节点能不能收起/展开(有孩子才有标记, 没孩子就没什么可藏的)."""
        return self._layout.can_collapse(node_id)

    def toggle(self, node_id: str, palette: Palette) -> bool:
        """收起/展开一个节点的子树, 返回"现在是不是收起的".

        两条硬口径(PLAN I-9.5): **折叠不改选中**(偷偷改选会让"恢复/建分支"作用到另一个
        节点上), 以及重算几何之后把视口**对回刚点的那个框**(``scrollregion`` 变小后 Tk
        会把偏移夹回, 不管的话用户会突然看到别处)。
        """
        if self._layout.box(node_id) is None or not self._layout.can_collapse(node_id):
            return False
        if node_id in self._collapsed:
            self._collapsed.discard(node_id)
        else:
            self._collapsed.add(node_id)
        self._relayout()
        self.redraw(palette)
        self._align_to(node_id)
        return node_id in self._collapsed

    def _align_to(self, node_id: str) -> None:
        """把视口挪到刚点的那个框上(它必须仍然看得见, 否则用户会看到"图跑了").

        直接居中到它(再夹进可滚范围): 折叠会把 ``scrollregion`` 改小, Tk 对已越界的偏移
        的处理是"夹回"—— 夹回之后用户看到的是图的另一头, 而刚才点的框不见了。
        """
        box = self._layout.box(node_id)
        if box is None:  # pragma: no cover - 刚点过的节点一定在图上
            return
        self.canvas.update_idletasks()
        width = float(self.canvas.winfo_width())
        height = float(self.canvas.winfo_height())
        total_x = max(1.0, float(self._layout.width))
        total_y = max(1.0, float(self._layout.height))
        offset_x = min(
            max(box.x - (width - box.width) / 2, 0.0), max(0.0, total_x - width)
        )
        offset_y = min(
            max(box.y - (height - box.height) / 2, 0.0), max(0.0, total_y - height)
        )
        self.canvas.xview_moveto(offset_x / total_x)
        self.canvas.yview_moveto(offset_y / total_y)
        self._refresh_arrows()

    @property
    def node_ids(self) -> tuple[str, ...]:
        """画出来的节点 id(按深度优先序)."""
        return tuple(box.node_id for box in self._layout.boxes)

    @property
    def selected(self) -> str | None:
        """当前选中的节点 id."""
        return self._selected

    @property
    def hovered(self) -> str | None:
        """当前悬停的节点 id."""
        return self._hovered

    @property
    def tip_text(self) -> str:
        """悬停提示现在挂着的全文(没挂就空串) —— 守卫按它断言"裁了能不能回看"."""
        return self._tip.text

    def item_counts(self) -> ItemCounts:
        """这次重绘画出来的 item 数(确定性数字, 用来当渲染基准)."""
        boxes = len(self._layout.boxes)
        return ItemCounts(
            boxes=boxes,
            edges=len(self._layout.edges),
            texts=boxes * 2,
            # 折叠标记: 有孩子的框各一个(它们才有收起/展开这个动作)。
            markers=len(self._layout.branches),
        )

    # -- 画 ---------------------------------------------------------------

    def redraw(self, palette: Palette) -> None:
        """用给定调色板整幅重画(主题切换、换字号、换数据都走这里)."""
        self._palette = palette
        self.canvas.delete("all")
        self._box_items.clear()
        self._text_items.clear()
        self._marker_items.clear()
        self.canvas.configure(
            background=palette.well,
            scrollregion=(0, 0, self._layout.width, self._layout.height),
            xscrollincrement=max(1, int(self._layout.metrics.box_width / 2)),
            yscrollincrement=max(1, int(self._layout.metrics.box_height / 2)),
            # 焦点环: 聚焦时是抢眼的 ``focus_ring``, 失焦时与画布底色同色(= 看不见环).
            # 画布是普通 Canvas(不在 keyboard 的补丁范围), 而它 ``takefocus=1`` ——
            # Tab 走到它身上却看不出焦点, 正是 I-6 要拦的事。
            highlightbackground=palette.well,
            highlightcolor=palette.focus_ring,
        )
        for edge in self._layout.edges:
            self.canvas.create_line(
                *_edge_points(edge.start, edge.end),
                fill=palette.border,
                width=2,
                smooth=True,
                splinesteps=_EDGE_SPLINE_STEPS,
            )
        for box in self._layout.boxes:
            self._draw_box(box.node_id, palette)
        self._paint_arrows(palette)
        self._refresh_arrows()

    def _draw_box(self, node_id: str, palette: Palette) -> None:
        """画一个框(一个 item) + 两行字(两个) + 折叠标记(有孩子时一个)."""
        box = self._layout.box(node_id)
        texts = self._texts.get(node_id)
        if box is None or texts is None:  # pragma: no cover - 两者都由同一份节点建出来
            return
        fill, border, title_color = self._box_colors(node_id, palette)
        self._box_items[node_id] = self.canvas.create_polygon(
            _rounded_points(box.x, box.y, box.width, box.height, _CORNER_RADIUS),
            fill=fill,
            outline=border,
            width=2,
            smooth=True,
        )
        inset = self._layout.metrics.text_inset
        center = box.center_x
        title_y = box.y + inset + self._layout.metrics.box_height * 0.3
        meta_y = box.y + self._layout.metrics.box_height - inset - 5
        title_item = self.canvas.create_text(
            center,
            title_y,
            text=texts.title,
            font=self._title_font,
            fill=title_color,
            justify="center",
            anchor="center",
        )
        meta_item = self.canvas.create_text(
            center,
            meta_y,
            text=texts.meta,
            font=self._meta_font,
            fill=palette.text_muted,
            justify="center",
            anchor="center",
        )
        self._text_items[node_id] = (title_item, meta_item)
        self._draw_marker(node_id, palette)

    def _draw_marker(self, node_id: str, palette: Palette) -> None:
        """框右下角的折叠标记(有孩子才有): 折起朝右、展开朝下.

        它是**一个 item**(与框/文字同一套账目), 形状与颜色都在这里定 —— 颜色按"里面藏着
        选中项/当前节点"提亮, 这是"藏起来必须留痕"在视觉上的那一半(另一半是说明行)。
        命中测试在 :meth:`TreeLayout.marker_at` 里(纯函数), 与这里用的是同一个矩形。
        """
        rect = self._layout.marker_rect(node_id)
        if rect is None:
            return
        self._marker_items[node_id] = self.canvas.create_text(
            rect[0] + rect[2] / 2,
            rect[1] + rect[3] / 2,
            text=_FOLD_GLYPHS[self._layout.is_collapsed(node_id)],
            font=self._marker_font,
            fill=self._marker_color(node_id, palette),
            anchor="center",
        )

    def _hides_something(self, node_id: str) -> bool:
        """这个框里藏着选中项或当前节点吗(只有**折叠着**的框才叫藏 —— 展开的什么都看得见).

        少了前半句会把**所有**有后代的框都当成"藏着东西": 子树里总有节点是选中项或当前
        节点, 于是展开状态下每个分支框的标记都提亮 —— 实测就是这么红的。
        """
        if node_id not in self._layout.descendants:
            return False
        return self._hidden_under(node_id, self._selected) or self._hidden_under(
            node_id, self._current_id()
        )

    def _marker_color(self, node_id: str, palette: Palette) -> str:
        """标记的颜色: 藏着选中项/当前节点时用强调色, 否则与说明行同一档弱色.

        选中框自己的底色是 ``accent_soft`` —— 在那上面描强调色只会糊成一块, 所以选中框的
        标记与它的文字一样用 ``accent_soft_text``。
        """
        selected = node_id == self._selected
        if self._hides_something(node_id):
            return palette.accent_soft_text if selected else palette.accent
        return palette.accent_soft_text if selected else palette.text_muted

    def _box_colors(self, node_id: str, palette: Palette) -> tuple[str, str, str]:
        """一个框的(底色, 描边, 标题色): 选中 > 悬停 > 常规(与卡片同一条优先级)."""
        if node_id == self._selected:
            return palette.accent_soft, palette.accent, palette.accent_soft_text
        if node_id == self._hovered:
            return palette.card_hover, palette.card_border, palette.text_primary
        return palette.card, palette.card_border, palette.text_primary

    def repaint_node(self, node_id: str | None, palette: Palette) -> None:
        """只重画一个框(选中/悬停是高频交互, 不做全幅重画).

        实测卡片全量重绘在 40 张时约 380ms, 图这边同理: 悬停只该重画受影响的那两个框。
        """
        if node_id is None:
            return
        item = self._box_items.get(node_id)
        texts = self._text_items.get(node_id)
        if item is None or texts is None:
            return
        fill, border, title_color = self._box_colors(node_id, palette)
        self.canvas.itemconfigure(item, fill=fill, outline=border, width=2)
        self.canvas.itemconfigure(texts[0], fill=title_color)
        self._refresh_marker(node_id, palette)

    def _refresh_marker(self, node_id: str, palette: Palette) -> None:
        """只改一个标记的颜色(没标记的框直接跳过)."""
        marker = self._marker_items.get(node_id)
        if marker is not None:
            self.canvas.itemconfigure(marker, fill=self._marker_color(node_id, palette))

    def _refresh_fold_traces(self, palette: Palette) -> None:
        """折叠痕迹(说明行文字 + 标记色)跟着选中/当前节点变.

        只扫折叠框: 只有它们会写"N 个后代", 也只有它们可能藏着选中项/当前节点。
        """
        for node_id in self._layout.descendants:
            self._refresh_meta(node_id)
            self._refresh_marker(node_id, palette)

    # -- 选中与悬停 ---------------------------------------------------------

    def select(self, node_id: str | None, palette: Palette) -> None:
        """换选中项并只重画受影响的框.

        顺带刷新折叠框的痕迹: "藏着选中项"这句话要当场变(下一拍再说就已经是误导了)。
        """
        if self._selected == node_id:
            return
        previous, self._selected = self._selected, node_id
        self.repaint_node(previous, palette)
        self.repaint_node(node_id, palette)
        self._refresh_fold_traces(palette)

    def set_hovered(self, node_id: str | None, palette: Palette) -> None:
        """换悬停项并只重画受影响的框(要回看的全文没被裁就直接不弹提示)."""
        if self._hovered == node_id:
            return
        previous, self._hovered = self._hovered, node_id
        self.repaint_node(previous, palette)
        self.repaint_node(node_id, palette)
        self._sync_tip(node_id)

    def _sync_tip(self, node_id: str | None) -> None:
        """把悬停提示挂到当前那个框下方(文字没被裁 / 没悬停在任何框上时收起)."""
        texts = None if node_id is None else self._texts.get(node_id)
        box = None if node_id is None else self._layout.box(node_id)
        if texts is None or box is None or not texts.truncated:
            self._tip.hide()
            return
        self._tip.show(
            texts.full,
            root_x=self._screen_x(box.x),
            root_y=self._screen_y(box.bottom),
        )

    def _screen_x(self, canvas_x: float) -> int:
        """画面坐标 -> 屏幕坐标(提示窗口用的是屏幕坐标)."""
        return int(self.canvas.winfo_rootx()) + int(canvas_x - self.canvas.canvasx(0))

    def _screen_y(self, canvas_y: float) -> int:
        """画面坐标 -> 屏幕坐标(见 ``_screen_x``)."""
        return int(self.canvas.winfo_rooty()) + int(canvas_y - self.canvas.canvasy(0))

    def node_at(self, x: int, y: int) -> str | None:
        """画布**控件坐标**落在哪个框里(空白处返回 ``None``).

        必须先用 ``canvasx``/``canvasy`` 换算: 事件给的是控件坐标, 而布局坐标是
        "画面坐标" —— 画面一旦被拖动过, 两者就差出一个滚动偏移量, 直接比较会让
        命中测试整体偏移(实测: 拖过之后点框点不中、拖空白反而选中了别的框)。
        """
        box = self._layout.node_at(
            float(self.canvas.canvasx(x)), float(self.canvas.canvasy(y))
        )
        return None if box is None else box.node_id

    def marker_at(self, x: int, y: int) -> str | None:
        """画布**控件坐标**落在哪个折叠标记里(与 :meth:`node_at` 同一套换算)."""
        return self._layout.marker_at(
            float(self.canvas.canvasx(x)), float(self.canvas.canvasy(y))
        )

    # -- 漫游 ---------------------------------------------------------------

    def can_scroll(self, direction: Arrow) -> bool:
        """那个方向还有没有内容(四边箭头"该不该显示"的唯一判据)."""
        first, last = self.canvas.xview()
        if direction == "left":
            return first > 0.0
        if direction == "right":
            return last < 1.0
        top, bottom = self.canvas.yview()
        if direction == "up":
            return top > 0.0
        return bottom < 1.0

    def visible_arrows(self) -> set[Arrow]:
        """**当前真的在布局里**的箭头(装得下就收起 —— 与"按需显示滚动条"同一条纪律).

        判据落在控件本身(``winfo_manager``)而不是几何上: 几何只回答"那边还有没有
        内容"。曾经拿几何当判据的守卫因此放过了一个真缺陷 —— 箭头收起之后再也放不
        回来, 而用例照样绿。
        """
        return {
            arrow
            for arrow, button in self._arrows.items()
            if button.winfo_manager() == "place"
        }

    def scroll(self, direction: Arrow, steps: int = _KEY_STEP) -> None:
        """朝某个方向挪 ``steps`` 个单位(单位 = 一个节点列/半行)."""
        if direction in ("left", "right"):
            self.canvas.xview_scroll(-steps if direction == "left" else steps, "units")
        else:
            self.canvas.yview_scroll(-steps if direction == "up" else steps, "units")
        self._refresh_arrows()

    def focus_canvas(self) -> None:
        """把键盘焦点交给画布(点一下画布之后方向键才管用)."""
        self.canvas.focus_set()

    # -- 事件接线 -----------------------------------------------------------

    def _wire_canvas(self) -> None:
        """按下/拖动/松开/移动 + 方向键 + 滚轮."""
        self.canvas.bind("<ButtonPress-1>", self._on_press)
        self.canvas.bind("<B1-Motion>", self._on_motion)
        self.canvas.bind("<ButtonRelease-1>", self._on_release)
        self.canvas.bind("<Motion>", self._on_pointer)
        self.canvas.bind("<Leave>", lambda _e: self._hover(None))
        for key, arrow in _KEY_BINDINGS:
            self.canvas.bind(key, partial(self._on_key, arrow))
        # 空格/回车 = 收起/展开**当前选中项**(方向键与 WASD 已经绑给漫游, 节点间导航是
        # 另一件事, 见 PLAN I-9.5 的口径)。
        self.canvas.bind("<space>", self._on_fold_key)
        self.canvas.bind("<Return>", self._on_fold_key)
        # 滚轮: 默认纵向、Shift 横向(触控板用户); Windows 用 MouseWheel, X11 用 Button-4/5。
        self.canvas.bind("<MouseWheel>", self._on_wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._on_shift_wheel)
        self.canvas.bind("<Button-4>", partial(self._on_wheel_delta, 1))
        self.canvas.bind("<Button-5>", partial(self._on_wheel_delta, -1))
        self.canvas.bind("<Shift-Button-4>", partial(self._on_horizontal_wheel, 1))
        self.canvas.bind("<Shift-Button-5>", partial(self._on_horizontal_wheel, -1))
        # 视口大小一变, "装不装得下"就变了 —— 首次布局(画布刚被映射)也走这里, 否则
        # 建图时画布还没量过尺寸, 箭头会按错的判断显示或收起。
        self.canvas.bind("<Configure>", self._on_resize)

    def _on_resize(self, _event: tkinter.Event) -> None:
        """画布尺寸变化后重判四条箭头(移动画布本身不触发, 只有视口变了才会来)."""
        self._refresh_arrows()

    def _on_press(self, event: tkinter.Event) -> None:
        """命中优先级: 标记 → 框 → 空白(PLAN I-9.5 的口径).

        按在**标记**上时既不平移也不选中 —— 否则会出现"想折叠却选中了它", 或者
        "想折叠却把图画拖走了"。
        """
        self.focus_canvas()
        self._press_at = (event.x, event.y)
        self._press_fold = self.marker_at(event.x, event.y) is not None
        self._press_node = None if self._press_fold else self.node_at(event.x, event.y)
        if self._press_node is None and not self._press_fold:
            self.canvas.scan_mark(event.x, event.y)

    def _on_motion(self, event: tkinter.Event) -> None:
        """拖动: 只有"从空白处开始"的拖动才平移画面."""
        if self._press_node is None and not self._press_fold:
            self.canvas.scan_dragto(event.x, event.y, gain=1)
            self._refresh_arrows()

    def _on_release(self, event: tkinter.Event) -> None:
        """松开: 位移小于阈值才算点击(从框上/标记上拖走都不算).

        三种命中各自干一件事: 标记 = 折叠/展开, 框 = 选中, 空白 = 刚才已经平移过了。
        """
        if self._press_fold:
            self._press_fold = False
            node_id = self.marker_at(event.x, event.y)
            if node_id is not None and self._palette is not None:
                self.toggle(node_id, self._palette)
            return
        node_id, self._press_node = self._press_node, None
        if node_id is None:
            return
        start_x, start_y = self._press_at
        if (
            abs(event.x - start_x) <= _CLICK_SLOP
            and abs(event.y - start_y) <= _CLICK_SLOP
        ):
            item = self._by_id.get(node_id)
            if item is not None:
                self._on_select(item)

    def _on_pointer(self, event: tkinter.Event) -> None:
        """鼠标移动: 换悬停的框(拖动时不算, 免得边走边闪)."""
        if self._press_node is not None:
            return
        self._hover(self.node_at(event.x, event.y))

    def _hover(self, node_id: str | None) -> None:
        """把悬停变化交给宿主(宿主负责重画那一个框, 顺带更新状态栏之类)."""
        if self._hovered == node_id:
            return
        self._on_hover(node_id)

    def _on_key(self, arrow: Arrow, _event: tkinter.Event | None = None) -> str:
        """方向键 / WASD 平移(本视图里这些键已经用来漫游, 见类的文档).

        绑定会把事件对象一并传进来(``partial`` 只预填了方向), 所以这里必须收下它 ——
        少这个参数的表现是: 按一下方向键就在 Tk 回调里抛
        ``TypeError: _on_key() takes 2 positional arguments but 3 were given``,
        界面上只是"按了没反应"。
        """
        self.scroll(arrow)
        return "break"

    def _on_fold_key(self, _event: tkinter.Event | None = None) -> str:
        """空格/回车: 收起/展开**当前选中项**(没选中或没孩子时什么也不做).

        键盘入口只做这一件事 —— 方向键/WASD 已经绑给漫游了(见类的文档)。
        """
        if self._selected is not None and self._palette is not None:
            self.toggle(self._selected, self._palette)
        return "break"

    def _on_wheel(self, event: tkinter.Event) -> str:
        """纵向滚轮(与滚动容器一致: 不用按修饰键)."""
        return self._on_wheel_delta(1 if event.delta > 0 else -1)

    def _on_shift_wheel(self, event: tkinter.Event) -> str:
        """Shift+滚轮 = 横向平移(触控板用户的一条退路)."""
        return self._on_horizontal_wheel(1 if event.delta > 0 else -1)

    def _on_wheel_delta(self, steps: int) -> str:
        """纵向滚轮."""
        self.canvas.yview_scroll(-steps, "units")
        self._refresh_arrows()
        return "break"

    def _on_horizontal_wheel(self, steps: int) -> str:
        """横向滚轮(Shift+滚轮)."""
        self.canvas.xview_scroll(-steps, "units")
        self._refresh_arrows()
        return "break"

    # -- 边箭头 -------------------------------------------------------------

    def _build_arrows(self) -> dict[Arrow, ctk.CTkButton]:
        """四条边中点各放一个朝外的箭头(盖在画布之上).

        箭头**在画布之后创建**: Tk 的堆叠顺序就是创建顺序, 后建的才盖在画布上。
        """
        arrows: dict[Arrow, ctk.CTkButton] = {}
        for arrow, spot in _ARROW_SPOTS.items():
            button = ctk.CTkButton(
                self.frame,
                text=_ARROW_GLYPHS[arrow],
                width=22,
                height=22,
                corner_radius=RADIUS_MD,
                font=typography.font(typography.FONT_STRONG, weight="bold"),
                command=lambda a=arrow: self.scroll(a),
            )
            button.place(**spot)
            arrows[arrow] = button
        return arrows

    def _paint_arrows(self, palette: Palette) -> None:
        """箭头只继承两种主题色, 不加动效(它是"那边还有内容"的提示)."""
        for button in self._arrows.values():
            button.configure(
                fg_color=palette.raised,
                hover_color=palette.item_hover,
                text_color=palette.text_body,
                border_width=0,
            )

    def _refresh_arrows(self) -> None:
        """按"那个方向还有没有内容"显示/收起每个箭头."""
        for arrow in self._arrows:
            self._show_arrow(arrow, self.can_scroll(arrow))

    def _show_arrow(self, arrow: Arrow, visible: bool) -> None:
        """真的把箭头放进布局或摘出去(状态没变就什么都不做, 免得每帧重排).

        显示时**必须**把落点再给一遍: ``place()`` 无参调用只是查询当前配置, 摘掉
        的控件靠它放不回来(实测 ``winfo_manager()`` 仍是空串)。
        """
        button = self._arrows[arrow]
        placed = button.winfo_manager() == "place"
        if visible and not placed:
            button.place(**_ARROW_SPOTS[arrow])
        elif not visible and placed:
            button.place_forget()
