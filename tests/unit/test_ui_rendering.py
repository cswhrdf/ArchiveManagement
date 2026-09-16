"""绘制层修补单元测试.

CustomTkinter 的绘制引擎默认把绘制尺寸向下取整到偶数, 奇数尺寸的控件因此
会丢掉 1px 右边框/下边框(露出的其实是父容器背景)。这里验证修补函数确实关掉
了该取整并且可以重复调用。不需要显示环境: ``DrawEngine`` 只保存 canvas 引用。
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from customtkinter import CTkCanvas, DrawEngine

import archive_management.ui.rendering as rendering
from archive_management.ui.rendering import apply_border_rendering_fix

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
