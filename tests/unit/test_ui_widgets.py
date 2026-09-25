"""UiKit 无头单元测试.

用假控件替换 customtkinter 控件, 验证控件创建、样式映射与主题重绘,
不需要显示环境(ubuntu CI 无头也能覆盖全部按钮样式分支)。
"""

from __future__ import annotations

import tkinter as tk
from typing import Any

import customtkinter as ctk
import pytest

import archive_management.ui.widgets as widgets
from archive_management.ui.palette import DARK

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("控件主题重绘"),
    pytest.mark.layer("unit"),
]


class _FakeCtkWidget:
    """记录 configure 调用的假 customtkinter 控件."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        self.master = master
        self.kwargs = dict(kwargs)

    def configure(self, **kwargs: Any) -> None:
        self.kwargs.update(kwargs)


@pytest.fixture
def kit(monkeypatch: pytest.MonkeyPatch) -> widgets.UiKit:
    """把 ctk 控件替换为假实现, 返回空的 UiKit."""
    monkeypatch.setattr(
        ctk,
        "CTkFrame",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(
        ctk,
        "CTkLabel",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(
        ctk,
        "CTkButton",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(ctk, "CTkFont", lambda **_kwargs: object())
    return widgets.UiKit()


class _FakeScrollableFrame(_FakeCtkWidget):
    """假滚动容器: 记录主题重绘时对内部 canvas 背景的更新."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.canvas_bg: str | None = None


@pytest.fixture
def scroll_kit(monkeypatch: pytest.MonkeyPatch) -> widgets.UiKit:
    """把 CTkScrollableFrame 替换为假实现, 返回空的 UiKit."""
    monkeypatch.setattr(
        ctk,
        "CTkScrollableFrame",
        lambda master=None, **kwargs: _FakeScrollableFrame(master=master, **kwargs),
    )
    return widgets.UiKit()


def test_scroll_frame_repaints_background_on_theme_change(
    scroll_kit: widgets.UiKit,
) -> None:
    """回归: 滚动列表的背景必须跟随主题, 否则浅/深色切换后颜色错位.

    CustomTkinter 只在构造时把内层 canvas 的背景取为父容器当时的颜色,
    因此 ``scroll_frame`` 必须在每次重绘时用调色板重设 ``fg_color``。
    """
    frame = scroll_kit.scroll_frame(scroll_kit, bg_key="panel")
    scroll_kit.apply(DARK)

    assert frame.kwargs["fg_color"] == DARK.panel
    assert frame.kwargs["scrollbar_button_color"] == DARK.border


def test_scroll_frame_ignores_destroyed_widget(
    scroll_kit: widgets.UiKit,
) -> None:
    """回归: 已销毁的滚动容器不应让重绘抛出 TclError."""
    frame = scroll_kit.scroll_frame(scroll_kit, bg_key="sidebar")

    def boom(**_kwargs: Any) -> None:
        raise tk.TclError("bad window path name")

    frame.configure = boom
    scroll_kit.apply(DARK)
    scroll_kit.apply(DARK)


def test_frame_recolored_with_border(kit: widgets.UiKit) -> None:
    frame = kit.frame(kit, bg_key="panel", border_key="border")
    kit.apply(DARK)
    assert frame.kwargs["fg_color"] == DARK.panel
    assert frame.kwargs["border_color"] == DARK.border


def test_frame_without_border_key_has_no_border_color(kit: widgets.UiKit) -> None:
    frame = kit.frame(kit, bg_key="raised")
    kit.apply(DARK)
    assert frame.kwargs["fg_color"] == DARK.raised
    assert "border_color" not in frame.kwargs


def test_label_maps_each_style_color(kit: widgets.UiKit) -> None:
    labels = {
        style: kit.label(kit, style, style=style)
        for style in ("primary", "body", "muted", "hint", "h2")
    }
    kit.apply(DARK)
    assert labels["primary"].kwargs["text_color"] == DARK.text_primary
    assert labels["body"].kwargs["text_color"] == DARK.text_body
    assert labels["muted"].kwargs["text_color"] == DARK.text_muted
    assert labels["hint"].kwargs["text_color"] == DARK.text_hint
    assert labels["h2"].kwargs["text_color"] == DARK.text_primary


def test_buttons_painted_by_each_style(kit: widgets.UiKit) -> None:
    buttons = {
        style: kit.button(kit, style, style=style)
        for style in ("accent", "danger", "soft", "ghost")
    }
    kit.apply(DARK)
    assert buttons["accent"].kwargs["fg_color"] == DARK.accent
    assert buttons["danger"].kwargs["fg_color"] == DARK.danger
    assert buttons["soft"].kwargs["fg_color"] == DARK.accent_soft
    ghost = buttons["ghost"]
    assert ghost.kwargs["fg_color"] == DARK.raised
    assert ghost.kwargs["border_color"] == DARK.border


def test_button_registers_and_repaints(kit: widgets.UiKit) -> None:
    button = kit.button(kit, "动作", style="accent", command=None, width=88, height=30)
    kit.apply(DARK)
    assert button.kwargs["width"] == 88
    assert button.kwargs["height"] == 30
    assert button.kwargs["fg_color"] == DARK.accent


def test_register_returns_unsubscribe_skips_inactive(kit: widgets.UiKit) -> None:
    calls: list[Any] = []
    kit.register(lambda p: calls.append("kept"))
    unsubscribe = kit.register(lambda p: calls.append("removed"))
    kit.apply(DARK)
    assert calls == ["kept", "removed"]
    unsubscribe()
    kit.apply(DARK)
    assert calls == ["kept", "removed", "kept"]


def test_register_unsubscribe_is_idempotent(kit: widgets.UiKit) -> None:
    calls: list[Any] = []
    unsubscribe = kit.register(lambda p: calls.append("x"))
    unsubscribe()
    unsubscribe()
    kit.apply(DARK)
    assert calls == []


def test_apply_skips_repaint_raising_tcl_error(kit: widgets.UiKit) -> None:
    """重绘已销毁控件抛 TclError 时应被跳过而非中断整批重绘."""
    painted: list[str] = []
    kit.register(lambda p: painted.append("before"))
    kit.register(lambda _p: (_ for _ in ()).throw(tk.TclError("bad window path")))
    kit.register(lambda p: painted.append("after"))
    kit.apply(DARK)
    assert painted == ["before", "after"]


def test_poster_backing_uses_the_panel_colour_only_over_a_real_cover() -> None:
    """海报标记的底衬: 有封面图才加底衬, 回落名称占位时必须是彻底透明.

    这条规则原先只在"真有一张封面图"的界面用例里验过, 而那条用例在 CI 上一直被
    跳过(见 tests/tk_guard.py) —— 抽成纯函数后不建窗口也能钉住两个方向。
    """
    from archive_management.ui.home_page import poster_backing

    assert poster_backing(None, DARK) == "transparent", "没有封面时不该有背景色"
    assert poster_backing(object(), DARK) == DARK.panel, "有封面时用面板色做底衬"
