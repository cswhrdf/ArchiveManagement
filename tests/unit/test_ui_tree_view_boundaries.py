"""分支视图(`ui/tree_view.py`)的边界: 折叠说明行与鼠标/滚轮事件里的几处"什么都不做".

这一层的事件处理全是"按下/拖动/松开"与"滚轮"这种**没有调用方可以问**的入口 ——
里面的退路(没按过就松手、按下之后节点没了、拖着的时候不换悬停、没选中就按空格、
布局里没有那个框)在带 GUI 的用例里很难凑出来, 因为它们要靠时序, 而时序不是用例
能安排的。这里用不建画布窗口的替身把这些入口直接叫一遍, 判据是各处的返回值与
"通知了宿主几次"。

说明行那一条是**折叠口径**的落点: 画面可以藏, 但藏起来的东西必须在框
上留下痕迹。要测的是"只提真被藏起来的那一项" —— 提多了同样是撒谎。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, cast

import pytest

from archive_management.i18n import tr
from archive_management.ui.palette import DARK, Palette
from archive_management.ui.tree_layout import tree_layout
from archive_management.ui.tree_view import TreeView

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(分支视图事件边界)"),
    pytest.mark.story("折叠说明行与鼠标/滚轮入口的退路"),
    pytest.mark.layer("unit"),
]


class _ItemStub:
    """备份项替身: 说明行只读"是不是当前节点"与两个标签."""

    def __init__(self, *, is_current: bool = False) -> None:
        self.is_current = is_current
        self.created_label = "2026-10-01 10:00"
        self.kind_label = "手动"


class _CanvasStub:
    """画布替身: 记下滚动与拖动(真画布要 Tk 根)."""

    def __init__(self) -> None:
        self.scrolled: list[tuple[str, int, str]] = []
        self.scans: list[tuple[str, int, int]] = []

    def canvasx(self, x: float) -> float:
        """画面坐标 = 控件坐标(这条替身没有滚动偏移, 于是命中测试算得出来)."""
        return x

    def canvasy(self, y: float) -> float:
        """见 :meth:`canvasx`."""
        return y

    def yview_scroll(self, steps: int, what: str) -> None:
        """记一次纵向滚动."""
        self.scrolled.append(("y", steps, what))

    def xview_scroll(self, steps: int, what: str) -> None:
        """记一次横向滚动."""
        self.scrolled.append(("x", steps, what))

    def scan_mark(self, x: int, y: int) -> None:
        """记一次"从这里开始拖"."""
        self.scans.append(("mark", x, y))

    def scan_dragto(self, x: int, y: int, *, gain: int = 1) -> None:
        """记一次"拖到这里"(gain 与真实现一致)."""
        del gain
        self.scans.append(("dragto", x, y))


class _EventStub:
    """只带 ``x``/``y``/``delta`` 的事件替身."""

    def __init__(self, *, x: int = 0, y: int = 0, delta: int = 0) -> None:
        self.x = x
        self.y = y
        self.delta = delta


def _view() -> Any:
    """造一个"没有画布窗口"的视图: 只摆出被测代码会用的那几样."""
    view: Any = TreeView.__new__(TreeView)
    view._layout = tree_layout([])
    view._by_id = {}
    view._nodes = []
    view._parent_of = {}
    view._collapsed = set()
    view._texts = {}
    view._selected = None
    view._hovered = None
    view._press_node = None
    view._press_fold = False
    view._press_at = (0, 0)
    view._palette = None
    view._arrows = {}
    view._on_select = lambda _item: None
    view._on_hover = lambda _node_id: None
    view.canvas = _CanvasStub()
    return view


# --------------------------------------------------------- 折叠说明行


def test_a_folded_box_only_mentions_what_is_actually_hidden() -> None:
    """折叠框只提**真被藏起来**的那一项: 选中项在里面、当前节点不在 → 只提选中项.

    提多了同样是撒谎(框上只有一行字的位置), 所以"当前节点不在里面"这一半必须走对。
    """
    view = _view()
    view._by_id = {"a": _ItemStub(is_current=True), "b": _ItemStub()}
    view._parent_of = {"b": "a"}
    view._layout = replace(tree_layout([]), descendants={"a": 1})
    view._selected = "b"
    meta = cast(str, view._meta_for("a"))
    assert tr("graph.contains_selected") in meta
    assert tr("graph.contains_current") not in meta
    assert tr("graph.descendants", count=1) in meta


def test_can_toggle_follows_the_layout() -> None:
    """能不能收起/展开只看布局里它是不是分支(没孩子就没什么可藏的)."""
    view = _view()
    assert view.can_toggle("a") is False
    assert view.can_toggle(None) is False
    view._layout = replace(tree_layout([]), branches=frozenset({"a"}))
    assert view.can_toggle("a") is True


def test_toggle_refuses_a_node_that_is_not_in_the_layout() -> None:
    """布局里没有这个框(或它不是分支)时什么都不做: 不改折叠, 也不重画."""
    view = _view()
    assert view.toggle("missing", DARK) is False
    assert view.collapsed == frozenset()


# --------------------------------------------------------- 鼠标: 按下/拖动/松开


def test_releasing_without_a_press_does_nothing() -> None:
    """没按过就松手(例如拖进来再松手)时什么都不做."""
    view = _view()
    view._on_release(_EventStub(x=10, y=10))
    assert view._press_node is None


def test_releasing_over_a_node_that_left_the_view_does_not_select() -> None:
    """按下之后图重画过、那个节点已经不在了 → 不选中(不能拿旧 id 当命中)."""
    view = _view()
    picks: list[object] = []
    view._on_select = picks.append
    view._press_node = "gone"
    view._press_at = (10, 10)
    view._on_release(_EventStub(x=12, y=11))
    assert picks == []


def test_pointer_motion_during_a_drag_does_not_hover() -> None:
    """正拖着画面时不换悬停框(免得边走边闪)."""
    view = _view()
    hops: list[object] = []
    view._on_hover = hops.append
    view._press_node = "a"
    view._on_pointer(_EventStub(x=1, y=1))
    assert hops == []


def test_pointer_motion_updates_the_hover_when_not_dragging() -> None:
    """没在拖动时悬停跟着鼠标走(落在空白处 = 取消悬停)."""
    view = _view()
    hops: list[str | None] = []
    view._on_hover = hops.append
    view._hovered = "a"
    view._on_pointer(_EventStub(x=1, y=1))
    assert hops == [None]


def test_hover_only_reports_a_real_change() -> None:
    """同一个框上重复移动(高频 ``<Motion>``)不再通知宿主."""
    view = _view()
    hops: list[str | None] = []
    view._on_hover = hops.append
    view._hovered = "a"
    view._hover("a")
    assert hops == []
    view._hover("b")
    assert hops == ["b"]


# --------------------------------------------------------- 键盘与滚轮


def test_folding_without_a_selection_does_nothing() -> None:
    """空格/回车时没选中任何框(或还没画过)就什么都不折叠, 但事件照样被吃掉."""
    view = _view()
    folded: list[str] = []

    def remember(node_id: str, _palette: Palette) -> bool:
        """记下被折叠的那个节点."""
        folded.append(node_id)
        return True

    view.toggle = remember
    assert view._on_fold_key() == "break"
    assert folded == []
    view._selected = "a"
    view._palette = DARK
    assert view._on_fold_key() == "break"
    assert folded == ["a"]


def test_wheel_scrolls_and_re_judges_the_arrows() -> None:
    """滚轮纵向滚动并重判四条箭头(向上与向下各一次, delta 的符号不能反过来)."""
    view = _view()
    assert view._on_wheel(_EventStub(delta=120)) == "break"
    assert view._on_wheel(_EventStub(delta=-120)) == "break"
    assert view.canvas.scrolled == [("y", -1, "units"), ("y", 1, "units")]


def test_shift_wheel_scrolls_sideways() -> None:
    """Shift+滚轮横向平移(触控板用户的一条退路)."""
    view = _view()
    assert view._on_shift_wheel(_EventStub(delta=120)) == "break"
    assert view._on_shift_wheel(_EventStub(delta=-120)) == "break"
    assert view.canvas.scrolled == [("x", -1, "units"), ("x", 1, "units")]
