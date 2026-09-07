"""UiKit 无头单元测试.

用假控件替换 customtkinter 控件, 验证控件创建、样式映射与主题重绘,
不需要显示环境(ubuntu CI 无头也能覆盖全部按钮样式分支)。
"""

from __future__ import annotations

from typing import Any

import pytest

from archive_management.ui import widgets
from archive_management.ui.palette import DARK


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
        widgets.ctk,
        "CTkFrame",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(
        widgets.ctk,
        "CTkLabel",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(
        widgets.ctk,
        "CTkButton",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(widgets.ctk, "CTkFont", lambda **_kwargs: object())
    return widgets.UiKit()


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
        for style in ("primary", "body", "muted", "h2")
    }
    kit.apply(DARK)
    assert labels["primary"].kwargs["text_color"] == DARK.text_primary
    assert labels["body"].kwargs["text_color"] == DARK.text_body
    assert labels["muted"].kwargs["text_color"] == DARK.text_muted
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
