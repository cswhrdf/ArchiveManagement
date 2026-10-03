"""绘制层修补单元测试.

CustomTkinter 的绘制引擎默认把绘制尺寸向下取整到偶数, 奇数尺寸的控件因此
会丢掉 1px 右边框/下边框(露出的其实是父容器背景)。这里验证修补函数确实关掉
了该取整并且可以重复调用。不需要显示环境: ``DrawEngine`` 只保存 canvas 引用。

另有一条: 下拉框右侧那半段边框的修补(CTk 把它涂成了箭头区底色, 用户 2026-10-02
反馈"右侧边框被下拉符号盖住了") —— 同样用假控件验证, 不需要显示环境。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from customtkinter import CTkCanvas, CTkComboBox, DrawEngine

import archive_management.ui.rendering as rendering
from archive_management.ui.rendering import (
    apply_border_rendering_fix,
    apply_combo_border_fix,
    paint_combo_border,
)

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("控件边框渲染"),
    pytest.mark.layer("unit"),
]


class _FakeCanvas:
    """占位 canvas: 引擎构造只保存引用."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def test_apply_border_rendering_fix_only_patches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一次调用执行修补, 重复调用直接返回, 不会反复包装 ``__init__``."""
    monkeypatch.setattr(rendering, "_APPLIED", False)
    monkeypatch.setattr(DrawEngine, "__init__", DrawEngine.__init__)

    assert apply_border_rendering_fix() is True
    assert apply_border_rendering_fix() is False


def test_draw_engine_rounds_to_actual_size() -> None:
    """回归: 绘制引擎必须按真实尺寸绘制, 否则奇数尺寸控件会丢边框."""
    apply_border_rendering_fix()

    engine = DrawEngine(cast("CTkCanvas", _FakeCanvas()))

    assert getattr(engine, "_round_width_to_even_numbers", False) is False
    assert getattr(engine, "_round_height_to_even_numbers", False) is False


class _RecordingCanvas:
    """只把 ``itemconfig`` 记下来的假画布."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def itemconfig(self, item: str, **kwargs: Any) -> None:
        self.calls.append((item, kwargs))


class _FakeCombo:
    """假下拉框: 只保存边框色与画布引用(颜色对按浅色/深色给第一个)."""

    def __init__(self, border_color: str = "#6a7681") -> None:
        self._border_color = border_color
        self._canvas = _RecordingCanvas()

    @staticmethod
    def _apply_appearance_mode(value: Any) -> Any:
        return value[0] if isinstance(value, tuple) else value


def test_paint_combo_border_uses_the_border_color_for_the_right_side() -> None:
    """右侧那半段边框要描回**边框色**(而不是箭头区底色) —— 箭头因此落在边框里面."""
    combo = _FakeCombo()

    paint_combo_border(cast("CTkComboBox", combo))

    assert combo._canvas.calls == [
        ("border_parts_right", {"outline": "#6a7681", "fill": "#6a7681"})
    ]


def test_apply_combo_border_fix_paints_after_every_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补丁只打一次(幂等); 打上之后每次 ``_draw`` 都会把右侧边框描回边框色."""
    monkeypatch.setattr(CTkComboBox, "_draw", lambda _self: None)  # 假的原始实现
    monkeypatch.setattr(rendering, "_COMBO_APPLIED", False)

    assert apply_combo_border_fix() is True
    assert apply_combo_border_fix() is False

    combo = _FakeCombo()
    CTkComboBox._draw(cast("CTkComboBox", combo))

    assert combo._canvas.calls == [
        ("border_parts_right", {"outline": "#6a7681", "fill": "#6a7681"})
    ]
