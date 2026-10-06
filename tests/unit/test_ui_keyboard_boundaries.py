"""键盘接线(`ui/keyboard.py`)的边界: 不建真窗口, 用"壳"控件替身盯着它什么时候作罢.

键盘可用性是**增强**: 接不上线的控件不能让接线本身变成别人的前置条件。所以这一层
最该测的不是"有窗口时接得对不对", 而是**每一处"接不上就算了"的退路**, 以及几处
只在特定时序出现的分支(重复 FocusIn、控件事后被禁用、窗口里压根没有主按钮)。

这些退路在带 GUI 的用例里跑不到: 那边控件是真的, 内部 Canvas 一定在, 状态也一直是
可用态。这里的替身是个"是控件但没有真窗口"的壳(``isinstance`` 过得了, 画不出来),
判据落在"改了什么/有没有改"上, 而不是"跑过就算"。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from typing import Any, cast

import customtkinter as ctk
import pytest

from archive_management.ui import keyboard
from archive_management.ui.palette import DARK, Palette

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(键盘接线边界)"),
    pytest.mark.story("接不上线时的退路与几处时序分支"),
    pytest.mark.layer("unit"),
]


class _WidgetStub:
    """控件替身: 只摆出被测代码会读的那几样(状态、内部控件、选项表)."""

    def __init__(self, *, state: str = "normal") -> None:
        self._state = state
        self._parts: dict[str, object] = {}
        self.configured: list[dict[str, object]] = []
        self.invocations = 0

    def cget(self, option: str) -> object:
        """读一个选项(没设过时按 ``state`` 回答, 其余当空串)."""
        if option in self._parts:
            return self._parts[option]
        return self._state if option == "state" else ""

    def configure(self, **kwargs: object) -> None:
        """记下每次改写 —— 焦点环与 takefocus 的判据都在这里."""
        self.configured.append(dict(kwargs))
        self._parts.update(kwargs)

    def invoke(self) -> None:
        """ "按下去"的一种叫法(CTk 按钮); 禁用的是否还会被按到, 就看这个计数."""
        self.invocations += 1

    def disable(self) -> None:
        """模拟"忙碌/不适用"时被禁用."""
        self._state = "disabled"


class _WindowStub:
    """窗口替身: 只记下挂了哪几个键(用例拿它当"按了一下")."""

    def __init__(self) -> None:
        self.handlers: dict[str, Callable[..., object]] = {}

    def bind(self, sequence: str, handler: Callable[..., object]) -> str:
        """记下绑定并给一个 id."""
        self.handlers[sequence] = handler
        return f"{sequence}#{len(self.handlers)}"


class _CanvasStub:
    """内部 Canvas 替身的构造器(必须是真 ``tk.Misc`` 实例 —— `focus_target` 只认控件)."""

    @staticmethod
    def make() -> Any:
        """造一个带记录能力的"内部控件"."""
        canvas = cast(Any, tk.Misc.__new__(tk.Misc))
        canvas.recorded_binds = []
        canvas.options = {}

        def bind(
            sequence: str, handler: Callable[..., object], add: str | None = None
        ) -> str:
            """记下绑定(用例之后可以直接"按一下空格")."""
            del add
            canvas.recorded_binds.append((sequence, handler))
            return f"{sequence}#{len(canvas.recorded_binds)}"

        def configure(**kwargs: object) -> None:
            """记下配置(takefocus 与焦点环都写在这里)."""
            canvas.options.update(kwargs)

        canvas.bind = bind
        canvas.configure = configure
        return canvas


def _widget(
    *, palette: Palette | None = DARK, state: str = "normal", canvas: bool = True
) -> Any:
    """造一个控件替身: 有没有内部控件、什么状态、从哪套调色板取色都可点."""
    widget: Any = _WidgetStub(state=state)
    widget._canvas = _CanvasStub.make() if canvas else None
    if palette is not None:
        # 窗口壳: 只为让 `palette_for` 读到"这个窗口用哪套色".
        window: Any = tk.Misc.__new__(tk.Misc)
        window._palette = palette
        widget.winfo_toplevel = lambda: window
    return widget


def _bound(widget: Any, sequence: str) -> Callable[..., object]:
    """取内部控件上挂的那个处理函数(用例拿它当"按了一下")."""
    for name, handler in widget._canvas.recorded_binds:
        if name == sequence:
            return cast(Callable[..., object], handler)
    raise AssertionError(f"没有挂上 {sequence}")


# --------------------------------------------------------- 找内部控件 / 取动作


def test_focus_target_is_none_without_an_inner_control() -> None:
    """既没有 ``_entry`` 也没有 ``_canvas`` 时给 None(这种控件接不了线)."""
    assert keyboard.focus_target(cast(Any, _WidgetStub())) is None


def test_focus_target_ignores_an_inner_control_that_is_not_a_widget() -> None:
    """内部控件不是控件(别的对象/None)时也当"没有", 免得拿它去 bind."""
    widget: Any = _WidgetStub()
    widget._entry = object()
    assert keyboard.focus_target(widget) is None


def test_default_activate_is_none_for_a_type_without_invoke_or_toggle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """可激活的类型里也可能两种叫法都没有 → 没有动作(而不是抛 AttributeError)."""

    class _Plain:
        """既没有 ``invoke`` 也没有 ``toggle`` 的"可激活"控件."""

    monkeypatch.setattr(keyboard, "ACTIVATABLE_TYPES", (_Plain,))
    assert keyboard._default_activate(cast(Any, _Plain())) is None


def test_ring_color_is_none_without_a_palette(monkeypatch: pytest.MonkeyPatch) -> None:
    """连调色板都取不到时不画环 —— 宁可不画, 也不猜一个颜色."""

    def refuse(_widget: object) -> None:
        return None

    monkeypatch.setattr(keyboard, "palette_for", refuse)
    assert keyboard.ring_color(object()) is None


# --------------------------------------------------------- 聚焦: 什么时候不换


def test_ring_on_does_nothing_without_a_palette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到调色板时一个选项都不改, 也不记原样(没换过就没什么可还原)."""
    monkeypatch.setattr(keyboard, "palette_for", lambda _widget: None)
    widget = _widget()
    saved: dict[str, object] = {}
    keyboard._ring_on(cast(Any, widget), saved)
    assert saved == {}
    assert widget.configured == []


def test_ring_on_does_nothing_for_a_disabled_widget() -> None:
    """禁用控件不该有焦点环(它本来就不该拿到焦点)."""
    widget = _widget(state="disabled")
    saved: dict[str, object] = {}
    keyboard._ring_on(cast(Any, widget), saved)
    assert saved == {}
    assert widget.configured == []


def test_ring_on_skips_when_no_ring_color_is_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调色板里挑不出可用环色时干脆不换(不猜一个看不见的颜色)."""
    monkeypatch.setattr(keyboard, "ring_color", lambda *_args, **_kwargs: None)
    widget = _widget()
    saved: dict[str, object] = {}
    keyboard._ring_on(cast(Any, widget), saved)
    assert saved == {}


def test_ring_on_remembers_the_original_look_only_once() -> None:
    """重复 FocusIn 不能再记原样 —— 否则会把焦点态本身当原样存下来, 失焦就还原不回去."""
    widget = _widget()
    saved: dict[str, object] = {}
    keyboard._ring_on(cast(Any, widget), saved)
    remembered = dict(saved)
    assert remembered
    keyboard._ring_on(cast(Any, widget), saved)
    assert saved == remembered


def test_ring_off_does_nothing_without_a_saved_original() -> None:
    """还没聚焦就失焦(没存过原样)时什么都不放回."""
    widget = _widget()
    keyboard._ring_off(cast(Any, widget), {})
    assert widget.configured == []


# --------------------------------------------------------- takefocus 与接线


def test_set_takefocus_is_a_no_op_without_an_inner_control() -> None:
    """没有内部控件可写时静默作罢(键盘接线是增强, 不能变成别人的前置条件)."""
    keyboard._set_takefocus(cast(Any, _WidgetStub()), 1)


def test_make_reachable_reports_false_when_already_wired() -> None:
    """重复接线没意义: 第二次返回 False(守卫也靠这个判"接上了")."""
    widget = _widget()
    assert keyboard.make_reachable(cast(Any, widget)) is True
    assert keyboard.make_reachable(cast(Any, widget)) is False


def test_make_reachable_reports_false_without_an_inner_control() -> None:
    """没有内部控件可接时返回 False, 也不把控件标成"接过了"."""
    widget = _widget(canvas=False)
    assert keyboard.make_reachable(cast(Any, widget)) is False
    assert keyboard.is_reachable(widget) is False


def test_a_disabled_widget_swallows_the_activation_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """禁用的控件按空格/回车不触发动作, 但事件照样被吃掉(免得冒泡到窗口级回车)."""
    monkeypatch.setattr(keyboard, "ACTIVATABLE_TYPES", (_WidgetStub,))
    widget = _widget()
    assert keyboard.make_reachable(cast(Any, widget)) is True
    activate = _bound(widget, "<space>")
    assert activate() == "break"
    assert widget.invocations == 1
    widget.disable()
    assert activate() == "break"
    assert widget.invocations == 1


def test_leave_tab_chain_is_a_no_op_without_an_inner_control() -> None:
    """没有内部控件可摘时静默作罢."""
    keyboard.leave_tab_chain(cast(Any, _widget(canvas=False)))


def test_enter_tab_chain_restores_the_remembered_takefocus() -> None:
    """输入类放回 Tab 链要还原它**原本**的 takefocus(写死 1 会改变 Tk 对它的处理)."""
    widget = _widget()
    widget._keyboard_takefocus = ""
    keyboard.enter_tab_chain(cast(Any, widget))
    assert widget._canvas.options["takefocus"] == ""


# --------------------------------------------------------- 取色/读选项的退路


def test_focus_style_is_unknown_without_a_palette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到调色板时认不出主色/危险色(登记过样式的仍能认 —— 那一步在它之前)."""
    monkeypatch.setattr(keyboard, "palette_for", lambda _widget: None)
    assert keyboard._focus_style(cast(Any, _WidgetStub())) is None


def test_focused_paint_is_empty_without_a_registered_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没人登记"聚焦换成哪套颜色"时只换焦点环(空表)."""
    monkeypatch.setattr(keyboard, "_FOCUS_PAINT", None)
    assert keyboard._focused_paint(cast(Any, _widget())) == {}


def test_focused_paint_is_empty_without_a_palette(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到调色板时同样只换焦点环."""
    monkeypatch.setattr(keyboard, "palette_for", lambda _widget: None)
    assert keyboard._focused_paint(cast(Any, _widget())) == {}


def test_cget_is_empty_for_a_widget_without_the_option_reader() -> None:
    """替身控件什么都没有时当"读不到"(不是抛 AttributeError)."""
    assert keyboard._cget(object(), "fg_color") == ""


def test_palette_of_gives_none_when_tk_is_broken(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Tk 已经崩掉(连外观模式都取不到)时给不出调色板, 而不是把异常抛给调用方."""

    def refuse() -> str:
        raise tk.TclError("application has been destroyed")

    monkeypatch.setattr(ctk, "get_appearance_mode", refuse)
    assert keyboard._palette_of(cast(Any, _WidgetStub())) is None


# --------------------------------------------------------- 窗口级回车与全局补丁


def test_return_key_in_a_dialog_does_nothing_without_a_primary_button(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """对话框里找不到主按钮时回车什么都不做(而不是随手按第一颗按钮)."""
    monkeypatch.setattr(keyboard, "primary_button", lambda _window: None)
    window = _WindowStub()
    keyboard.bind_window_keys(cast(Any, window))
    assert set(window.handlers) == {"<Escape>", "<Return>"}
    window.handlers["<Return>"]()  # 不该炸, 也不该按到别的按钮


def test_attach_ignores_objects_that_are_not_ctk_widgets() -> None:
    """补丁在构造器里见到的可能是别的东西(不是 CTk 控件)→ 什么都不做."""
    keyboard._attach(object())


def test_install_keyboard_support_is_applied_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补丁是进程级的: 第二次调用什么都不做(否则会把包装器套第二层)."""

    class _Target:
        """补丁目标替身: 只为了看"改了哪两处", 不去动真的 CTk 控件."""

        def __init__(self, *args: object, **kwargs: object) -> None:
            """占位构造器(会被包起来)."""
            del args, kwargs

        def configure(self, **kwargs: object) -> None:
            """占位 configure(会被包起来)."""
            del kwargs

    monkeypatch.setattr(keyboard, "PATCHED_TYPES", (_Target,))
    monkeypatch.setattr(keyboard, "_APPLIED", False)
    assert keyboard.install_keyboard_support() is True
    assert keyboard.install_keyboard_support() is False
