"""下拉浮层(`ui/dropdown.py`)的状态机边界: 不建窗口, 用替身盯着它的判断与收尾.

浮层本身是一个 Tk 窗口, 但它**内部的状态机**只依赖几个属性与几次调用 —— 提交哪一行、
高亮刷给谁、什么时候不再挂绑定、登记簿里留的是哪张。这些判断在真窗口下反而测不实
(鼠标落在哪一行、宿主什么时候发 `<Configure>` 都不由用例决定), 所以这里拿替身替掉
列表/菜单/浮层窗口, 只判"该记的账有没有记对"。

两条实测出来的坑, 顺手记在这里:

* ``DropdownPopup.__init__`` 要为清单建 ``tkfont.Font`` —— **那一步需要 Tk 根**。本组
  判的不是"构造对不对"(那由界面用例看着), 所以用 ``__new__`` 摆好属性绕过构造;
* ``close()`` 第一步是 ``_unbind_outside()``, 得真窗口才算得准 —— 替身里用空实现顶掉。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any, cast

import pytest
from customtkinter.windows.widgets.core_widget_classes import DropdownMenu

from archive_management.ui import dropdown
from archive_management.ui.palette import DARK

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(下拉浮层状态机)"),
    pytest.mark.story("提交/高亮/挂载/登记四处边界"),
    pytest.mark.layer("unit"),
]


class _MenuStub:
    """下拉菜单的替身: 只摆出被测代码会读的那几个属性."""

    def __init__(self, master: object = None, values: list[str] | None = None) -> None:
        self.master = master
        self._values = values if values is not None else []
        self._button_callback: Callable[[str], None] | None = None
        self.picked: list[str] = []

    def pick(self, value: str) -> None:
        """CTk 自己的回调: 把选中值写回输入框并触发业务回调(这里只记账)."""
        self.picked.append(value)


class _ListboxStub:
    """列表控件的替身: 记下每次改底色, 并回答"选中/高亮/鼠标落在第几行"."""

    def __init__(
        self, *, selected: tuple[int, ...] = (), active: int = 0, nearest: int = 0
    ) -> None:
        self.selected = selected
        self.active = active
        self.nearest_row = nearest
        self.paints: list[tuple[int, str]] = []

    def curselection(self) -> tuple[int, ...]:
        """回车时会问"选中了哪几行"."""
        return self.selected

    def index(self, what: str) -> int:
        """没选中任何行时问"高亮的是哪一行"(Tk 的 ``index("active")``)."""
        assert what == "active"
        return self.active

    def nearest(self, y: int) -> int:
        """鼠标落点对应第几行(落在列表外面时 Tk 会给首/末行)."""
        del y
        return self.nearest_row

    def itemconfig(self, index: int, **options: str) -> None:
        """记一条"第几行改成了什么底色"."""
        self.paints.append((index, options["background"]))


class _PopupStub(dropdown.DropdownPopup):
    """登记簿里的替身浮层: 不建窗口, 只答"开着吗"并记"被收了几次"."""

    def __init__(self, menu: object = None, *, open_: bool = True) -> None:
        """不调父类构造(那一步要 Tk 根), 只摆登记与收起要看的那两个属性."""
        self.menu = cast(Any, menu if menu is not None else _MenuStub())
        self.window = cast(Any, object()) if open_ else None
        self.closed = 0

    def is_open(self) -> bool:
        """替身按摆出来的窗口属性回答."""
        return self.window is not None

    def close(self) -> None:
        """记一笔并当作已经收起."""
        self.closed += 1
        self.window = None


class _EventStub:
    """只带 ``y`` 的事件替身(`_index_at` 只读它)."""

    def __init__(self, y: int) -> None:
        self.y = y


def _menu(master: object = None, values: list[str] | None = None) -> DropdownMenu:
    """按菜单的接口把替身交出去(原类缺类型标注, 这里用 cast 对齐签名)."""
    return cast(DropdownMenu, _MenuStub(master, values))


def _widget() -> tk.Misc:
    """一个"是控件、但没有真窗口"的锚点: 只为了过 `isinstance` 那一关."""
    return tk.Misc.__new__(tk.Misc)


def _popup(values: list[str] | None = None, menu: object = None) -> Any:
    """造一张摆好属性的浮层(绕开构造里那次"建清单字体")."""
    # 标成 Any: 下面这些属性是"替身摆出来的", 类型上本来就不完整(父类要 Tk 根才填得满).
    popup: Any = dropdown.DropdownPopup.__new__(dropdown.DropdownPopup)
    popup.menu = menu if menu is not None else _MenuStub()
    popup.anchor = None
    popup.palette = DARK
    popup.values = values if values is not None else ["a", "b", "c"]
    popup.window = None
    popup.listbox = None
    popup.scrollbar = None
    popup.plan = None
    popup._binds = []
    popup._hover = -1
    return popup


# --------------------------------------------------------- 显示失败要当场收尾


def test_show_reports_failure_when_the_popup_cannot_be_built() -> None:
    """建控件就失败(锚点已销毁)时 ``show`` 返回 False, 由调用方收尾."""
    popup = _popup()
    popup._build = lambda: False
    assert popup.show(0, 0) is False


# --------------------------------------------------------- 回车的三条路


def test_return_key_commits_the_selected_row() -> None:
    """回车选中"当前选中的那一行"."""
    menu = _MenuStub()
    menu._button_callback = menu.pick
    popup = _popup(menu=menu)
    popup.listbox = _ListboxStub(selected=(1,))
    assert popup._on_return(_EventStub(0)) == "break"
    assert menu.picked == ["b"]


def test_return_key_falls_back_to_the_highlighted_row() -> None:
    """没选中任何行时按"高亮的那一行"提交(键盘上下键之后就是这个状态)."""
    menu = _MenuStub()
    menu._button_callback = menu.pick
    popup = _popup(menu=menu)
    popup.listbox = _ListboxStub(active=2)
    popup._on_return(_EventStub(0))
    assert menu.picked == ["c"]


def test_return_key_without_a_list_box_commits_nothing() -> None:
    """浮层已经收起(列表没了)时回车什么都不提交, 也不炸."""
    menu = _MenuStub()
    menu._button_callback = menu.pick
    popup = _popup(menu=menu)
    popup._on_return(_EventStub(0))
    assert menu.picked == []


# --------------------------------------------------------- 提交的两处拒绝


def test_commit_ignores_rows_outside_the_value_list() -> None:
    """行号越界(鼠标落在列表外面 / 收起后的 -1)直接丢弃, 不把空值当选中."""
    menu = _MenuStub()
    menu._button_callback = menu.pick
    popup = _popup(menu=menu)
    popup._commit(-1)
    popup._commit(3)
    assert menu.picked == []


def test_commit_skips_the_callback_when_the_menu_has_none() -> None:
    """菜单没挂回调(只读下拉)时照样收起浮层, 只是不回调."""
    popup = _popup(menu=_MenuStub(), values=["a"])
    closes: list[int] = []
    popup.close = lambda: closes.append(1)
    popup._commit(0)
    assert closes == [1]


def test_commit_tolerates_a_menu_without_the_callback_attribute() -> None:
    """连 ``_button_callback`` 属性都没有(别的 CTk 版本)时也不抛."""
    popup = _popup(menu=object(), values=["a"])
    popup.close = lambda: None
    popup._commit(0)


# --------------------------------------------------------- 悬停高亮


def test_motion_over_a_new_row_highlights_it() -> None:
    """鼠标移到某一行 → 那一行刷上悬停色, 并记下它."""
    popup = _popup()
    box = _ListboxStub(nearest=1)
    popup.listbox = box
    popup._on_motion(_EventStub(40))
    assert box.paints == [(1, DARK.item_hover)]
    assert popup._hover == 1


def test_motion_re_paints_the_row_that_was_highlighted_before() -> None:
    """移开时先把上一行恢复成常规底色, 再刷新的那一行."""
    popup = _popup()
    box = _ListboxStub(nearest=2)
    popup.listbox = box
    popup._hover = 0
    popup._on_motion(_EventStub(80))
    assert box.paints == [(0, DARK.panel), (2, DARK.item_hover)]


def test_motion_inside_the_same_row_does_not_repaint() -> None:
    """还在同一行上(高频 ``<Motion>``)时不重复改底色."""
    popup = _popup()
    box = _ListboxStub(nearest=1)
    popup.listbox = box
    popup._hover = 1
    popup._on_motion(_EventStub(41))
    assert box.paints == []


def test_motion_without_a_list_box_does_nothing() -> None:
    """浮层已收起(列表没了)时鼠标事件什么都不做."""
    popup = _popup()
    box = _ListboxStub(nearest=1)
    popup.listbox = None
    popup._on_motion(_EventStub(1))
    assert box.paints == []


def test_motion_below_the_last_row_clears_the_highlight() -> None:
    """移到列表外面(行号算成 -1)时只把原高亮行恢复, 不再刷任何一行."""
    popup = _popup()
    box = _ListboxStub(nearest=9)
    popup.listbox = box
    popup._hover = 1
    popup._on_motion(_EventStub(9999))
    assert box.paints == [(1, DARK.panel)]
    assert popup._hover == -1


# --------------------------------------------------------- 收起之后的延后动作


def test_attaching_the_outside_bindings_after_close_is_a_no_op() -> None:
    """挂绑定是延后那一拍做的, 而浮层已经收起 → 什么都不挂(而不是等它到点再崩)."""
    popup = _popup()
    popup._attach_outside(cast(Any, object()))
    assert popup._binds == []


def test_following_the_anchor_after_close_is_a_no_op() -> None:
    """宿主事后挪动时浮层已收起 → 不再重算位置."""
    popup = _popup()
    calls: list[tuple[int, int]] = []
    popup._place = lambda x, y: calls.append((x, y))
    popup._follow_anchor(_EventStub(0))
    assert calls == []


# --------------------------------------------------------- 登记簿: 同一时刻只留一张


def test_active_dropdown_is_none_without_a_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没人登记时"当前那张"就是没有."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    assert dropdown.active_dropdown() is None


def test_active_dropdown_ignores_a_registered_popup_that_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """登记还在、浮层其实已经收起了 → 当作"没有", 免得守卫量到一张不存在的浮层."""
    monkeypatch.setattr(dropdown, "_ACTIVE", _PopupStub(open_=False))
    assert dropdown.active_dropdown() is None


def test_registering_the_same_popup_twice_keeps_it_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """同一张浮层登记两次(重复显示)不该把它自己收掉."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    popup = _PopupStub()
    dropdown.register(popup)
    dropdown.register(popup)
    assert popup.closed == 0
    assert dropdown.active_dropdown() is popup


def test_registering_another_popup_closes_the_previous_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新的一张登记上来时把上一张收掉 —— 浮层不会两张一起挂在屏幕上."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    first, second = _PopupStub(), _PopupStub()
    dropdown.register(first)
    dropdown.register(second)
    assert first.closed == 1
    assert dropdown.active_dropdown() is second


def test_unregistering_a_popup_clears_the_registration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """正常那条路: 收起的就是当前这张, 登记跟着清掉."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    popup = _PopupStub()
    dropdown.register(popup)
    dropdown.unregister(popup)
    assert dropdown.active_dropdown() is None
    assert popup.closed == 1


def test_unregistering_a_popup_that_is_not_active_still_closes_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锚点销毁那条路上来的可能是"已经不是当前这张"的浮层 —— 也照收, 但不动登记."""
    active, stale = _PopupStub(), _PopupStub()
    monkeypatch.setattr(dropdown, "_ACTIVE", active)
    dropdown.unregister(stale)
    assert dropdown.active_dropdown() is active
    assert stale.closed == 1


def test_is_open_for_compares_the_menu_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """问"这张菜单的浮层开着吗"时只认菜单本体, 别的菜单不算(否则会点不开)."""
    menu, other = _menu(), _menu()
    monkeypatch.setattr(dropdown, "_ACTIVE", _PopupStub(menu=menu))
    assert dropdown.is_open_for(menu) is True
    assert dropdown.is_open_for(other) is False


def test_is_open_for_is_false_without_an_active_popup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有当前那张时"这张菜单开着吗"当然是假."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    assert dropdown.is_open_for(_menu()) is False


# --------------------------------------------------------- open_for 的三种"不做"


def test_open_for_does_nothing_when_the_master_is_not_a_widget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """锚点不是控件(菜单还没挂上/已经销毁)时什么都不做, 也不登记."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    dropdown.open_for(_menu(master=object(), values=["a"]), 0, 0)
    assert dropdown.active_dropdown() is None


def test_open_for_does_nothing_when_there_are_no_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空下拉(没有可选值)不开一个空框出来."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    dropdown.open_for(_menu(master=_widget(), values=[]), 0, 0)
    assert dropdown.active_dropdown() is None


def test_open_for_stops_when_showing_the_popup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显示失败(建控件/定位出问题)时当场收掉, 也不登记成"当前这张"."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    monkeypatch.setattr(dropdown, "palette_for", lambda _anchor: DARK)
    shell: Any = _PopupStub(open_=False)
    shell.show = lambda *_args: False
    monkeypatch.setattr(dropdown, "DropdownPopup", lambda *_args, **_kwargs: shell)
    dropdown.open_for(_menu(master=_widget(), values=["a"]), 0, 0)
    assert shell.closed == 1
    assert dropdown.active_dropdown() is None


def test_open_for_registers_the_popup_it_showed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """显示成功才登记 —— 登记簿正是 CTk"再点一次就收起"那个判断的来源."""
    monkeypatch.setattr(dropdown, "_ACTIVE", None)
    monkeypatch.setattr(dropdown, "palette_for", lambda _anchor: DARK)
    shell: Any = _PopupStub()
    shell.show = lambda *_args: True
    monkeypatch.setattr(dropdown, "DropdownPopup", lambda *_args, **_kwargs: shell)
    dropdown.open_for(_menu(master=_widget(), values=["a"]), 0, 0)
    assert shell.closed == 0
    assert dropdown.active_dropdown() is shell


# --------------------------------------------------------- 行高精算(_refine)


class _LaidOutListbox:
    """按摆好的行位置回答 ``bbox`` 的列表替身.

    ``_refine`` 读的是"第 0 行与第 1 行上沿之差" —— 行距在 Tk 里由 Listbox 自己排,
    桌面环境下打开浮层的那一刻往往还没排出来(``bbox`` 给空), 这里把两种形态都摆得
    出来, 让"量到了/没量到"由用例说了算。
    """

    def __init__(
        self,
        *,
        rows: list[tuple[int, int, int, int] | None] | None = None,
    ) -> None:
        self.rows = rows if rows is not None else []

    def bbox(self, index: int) -> tuple[int, int, int, int] | None:
        """行没排出来(或没这一行)时 Tk 给的就是空."""
        if index >= len(self.rows):
            return None
        return self.rows[index]


def _refine_spy(popup: Any, *, row_height: int) -> dict[str, int]:
    """摆好 ``_refine`` 要读的属性, 并把重排三步换成记账."""
    calls: dict[str, int] = {name: 0 for name in ("_measure", "_fill", "_write")}
    popup.row_height = row_height
    popup.window = object()  # _refine 只要它"不是 None"
    popup.calls = calls

    def _record(name: str, *_args: Any) -> Any:
        """替掉一步重排: 记一笔; ``_measure`` 还要交回三元组."""
        calls[name] += 1
        if name == "_measure":
            return object(), 0, 10
        return None

    for name in ("_measure", "_fill", "_write"):
        setattr(popup, name, lambda *_args, _name=name: _record(_name))
    return calls


def test_refine_keeps_the_estimate_while_rows_are_not_laid_out() -> None:
    """行还没排出来(``bbox`` 给空)时保持估出来的行高, 不触发重排.

    桌面环境下打开浮层后的第一拍常常就是这个形态(映射是异步的), 判据是**别拿着
    半截信息乱摆**: 估的行高虽不精确, 但比拿空数据重算稳。
    """
    popup = _popup()
    calls = _refine_spy(popup, row_height=14)
    popup.listbox = _LaidOutListbox(rows=[(0, 0, 14, 14), None])
    popup._refine(0, 0)
    assert popup.row_height == 14
    assert sum(calls.values()) == 0


def test_refine_adopts_the_listbox_pitch_and_replaces_the_plan() -> None:
    """量到真实行距(与估值对不上)时采纳它, 并按新行高把浮层重摆一遍."""
    popup = _popup()
    calls = _refine_spy(popup, row_height=14)
    popup.listbox = _LaidOutListbox(rows=[(0, 0, 14, 14), (0, 15, 14, 14)])
    popup._refine(0, 0)
    assert popup.row_height == 15
    assert calls == {"_measure": 1, "_fill": 1, "_write": 1}


def test_refine_keeps_the_plan_when_the_pitch_matches_the_estimate() -> None:
    """行距与估值一致时不再重摆一遍(白闪一次没有意义)."""
    popup = _popup()
    calls = _refine_spy(popup, row_height=15)
    popup.listbox = _LaidOutListbox(rows=[(0, 0, 14, 14), (0, 15, 14, 14)])
    popup._refine(0, 0)
    assert popup.row_height == 15
    assert sum(calls.values()) == 0


def test_refine_ignores_a_non_positive_pitch() -> None:
    """两行量出来贴在同一处(非正行距)是坏数据, 不采纳也不重摆."""
    popup = _popup()
    calls = _refine_spy(popup, row_height=14)
    popup.listbox = _LaidOutListbox(rows=[(0, 0, 14, 14), (0, 0, 14, 14)])
    popup._refine(0, 0)
    assert popup.row_height == 14
    assert sum(calls.values()) == 0


# --------------------------------------------------------- 进程级补丁的幂等


def test_the_combo_fix_is_applied_only_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """补丁是进程级的: 第二次调用什么都不做, 免得把包装器套第二层."""
    monkeypatch.setattr(dropdown, "_APPLIED", False)
    for name in ("open", "close", "is_open"):
        monkeypatch.setattr(DropdownMenu, name, getattr(DropdownMenu, name))
    assert dropdown.apply_combo_dropdown_fix() is True
    assert dropdown.apply_combo_dropdown_fix() is False


# --------------------------------------------------------- 行高测量: 两档请求高度相等


class _FakeWindow:
    """浮层窗口的替身: 只记下 topmost 的写入值, 其余什么都不做."""

    def __init__(self) -> None:
        self.topmost: bool | None = None

    def overrideredirect(self, _flag: bool) -> None:
        """无边框(浮层不是普通窗口)."""

    def transient(self, _host: object) -> None:
        """挂成宿主的附属窗口."""

    def attributes(self, _name: str, value: bool) -> None:
        """记下 topmost 写了什么(判据: 要跟随宿主)."""
        self.topmost = value

    def update_idletasks(self) -> None:
        """布局冲刷: 替身里没什么要冲的."""

    def bind(self, sequence: str, _handler: object, add: str = "") -> str:
        """记一条绑定并交回 id."""
        return f"win-{sequence}"


class _FakeFrame:
    """列表的父容器替身."""

    def pack(self, **_options: object) -> None:
        """铺满."""


class _MeasuredListbox:
    """列表替身: 两档请求高度由用例给, 其余只记账."""

    def __init__(self, *, one_row: int, two_rows: int) -> None:
        self.heights = {1: one_row, 2: two_rows}
        self.height = 1
        self.inserted: list[str] = []

    def pack(self, **_options: object) -> None:
        """铺满."""

    def insert(self, _index: str, value: str) -> None:
        """收下候选项."""
        self.inserted.append(value)

    def configure(self, *, height: int) -> None:
        """两档请求高度就是靠改它量出来的."""
        self.height = height

    def winfo_reqheight(self) -> int:
        """按当前 height 报请求高度."""
        return self.heights[self.height]

    def bind(self, sequence: str, _handler: object, add: str = "") -> str:
        """记一条绑定并交回 id."""
        return f"box-{sequence}"


class _FakeAnchor:
    """锚点的替身: ``_build`` 只问它的顶层窗口."""

    def __init__(self, host: object) -> None:
        self.host = host

    def winfo_toplevel(self) -> object:
        """浮层要挂成它的附属窗口."""
        return self.host


def test_build_keeps_the_font_estimate_when_both_request_heights_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两档请求高度量不出差值时**保住字号估的行高**, 而不是把 0 当成一行.

    真 Tk 里有一行内容时 ``height=2`` 一定更高, 所以这条路只有控件异常/内容为空时
    才会走到 —— 而一旦采纳 ``measured = 0``, 后面所有"压到几行"的除法都会崩。
    判据就是那个估值没被 0 覆盖。
    """
    popup = _popup(values=["a"])
    popup.anchor = cast(Any, _FakeAnchor(_FakeWindow()))
    popup.font = cast(Any, "TkDefaultFont")
    popup.row_height = 14
    listbox = _MeasuredListbox(one_row=15, two_rows=15)
    fake_tk = SimpleNamespace(
        Toplevel=lambda _master: _FakeWindow(),
        Frame=lambda _master, **_options: _FakeFrame(),
        Listbox=lambda _master, **_options: listbox,
        TclError=tk.TclError,
    )
    # 只换 dropdown 模块里的 tk 引用: 真 tkinter 一个属性都不动(同一进程里还有 GUI 用例).
    monkeypatch.setattr(dropdown, "tk", fake_tk)
    monkeypatch.setattr(dropdown, "host_is_pinned", lambda _host: False)

    assert popup._build() is True
    assert popup.row_height == 14, "量不出差值时不该把行高改成 0"
    assert listbox.inserted == ["a"], "候选项要真的写进列表"


# --------------------------------------------------------- 选中项与首屏


class _AnchorWithValue:
    """带 ``get()`` 的锚点替身(``_mark_current`` 靠它取当前值)."""

    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        """CTk 下拉读当前值就这一个入口."""
        return self.value


class _ScrollRecordingListbox:
    """``_mark_current`` 要的那三个动作: 选中 / 高亮 / 滚到哪."""

    def __init__(self) -> None:
        self.selected: list[int] = []
        self.activated: list[int] = []
        self.scrolls: list[float] = []

    def selection_set(self, index: int) -> None:
        """标成选中."""
        self.selected.append(index)

    def activate(self, index: int) -> None:
        """高亮到那一行(键盘回车据此提交)."""
        self.activated.append(index)

    def yview_moveto(self, fraction: float) -> None:
        """记下滚到哪(0 = 最上沿)."""
        self.scrolls.append(fraction)


def test_a_selection_below_the_first_screen_scrolls_to_the_top() -> None:
    """选中项在首屏之外时把它顶到视野**上沿**(而不是只保证贴住底边).

    用户 2026-10-30: "选了靠下的选项后, 下拉框自动向下滚动遮住了第一个选项" ——
    ``see()`` 是最小滚动, 会把握手处的几行滚出视野; Windows 原生下拉是"以选中项为
    第一行往下排", 这里对齐它。
    """
    popup = _popup(values=["a", "b", "c", "d"])
    box = _ScrollRecordingListbox()
    popup.listbox = box
    popup.anchor = _AnchorWithValue("d")
    popup.plan = cast(Any, SimpleNamespace(visible_rows=3))

    popup._mark_current()

    assert box.selected == [3], "当前值是第 4 行"
    assert box.activated == [3], "高亮要跟着走"
    assert box.scrolls == [0.75], "3/4: 把第 4 行滚到上沿"
    assert box.scrolls[0] > 0, "要往下滚, 不是钉在最上"


def test_a_selection_inside_the_first_screen_pins_the_view_to_the_top() -> None:
    """首屏装得下时视图钉回最上(估高的旧几何会留下半行偏移)."""
    popup = _popup(values=["a", "b", "c", "d"])
    box = _ScrollRecordingListbox()
    popup.listbox = box
    popup.anchor = _AnchorWithValue("b")
    popup.plan = cast(Any, SimpleNamespace(visible_rows=4))

    popup._mark_current()

    assert box.selected == [1]
    assert box.scrolls == [0.0], "装得下就不要滚"
