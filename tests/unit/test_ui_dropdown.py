"""下拉浮层的落点决策单元测试.

用户 2026-10-02 的两条要求 —— "下拉框要限高, 内容超出就出滚动条, 不要一直撑开顶出
软件界面" 与 "位于软件下方的下拉框默认向上展示" —— 都落在 :func:`plan_dropdown`
这一支纯函数上(算术, 不需要显示环境): 位置、行数、要不要滚动条全在这里定, 浮层那边
只负责把算出来的数写进 ``geometry``。所以判据也钉在这里: 四条规则各咬一条用例,
改坏了立刻能看出是哪一条。

单位是**物理**像素的屏幕坐标(调用方已经把设计值换算过了), 用例里的数字取整方便手算:
行高 20、上下各留 100 的空间。
"""

from __future__ import annotations

import pytest

from archive_management.ui import contrast
from archive_management.ui.dropdown import (
    plan_dropdown,
    plan_dropdown_width,
    scrollbar_colors,
)
from archive_management.ui.palette import Palette

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("下拉框弹出层"),
    pytest.mark.layer("unit"),
]

# 用例里统一的行高与窗口(手算方便: 10 行 = 200px)。
_ROW = 20
_WINDOW_TOP = 0
_WINDOW_BOTTOM = 800


def test_room_below_keeps_the_list_under_the_box() -> None:
    """下方放得下就往下展开 —— 原生下拉的默认方向, 不改。"""
    plan = plan_dropdown(
        rows=4,
        row_height=_ROW,
        anchor_top=100,
        anchor_bottom=128,
        window_top=_WINDOW_TOP,
        window_bottom=_WINDOW_BOTTOM,
        screen_top=0,
        screen_bottom=1080,
        gap=2,
    )
    assert not plan.flipped
    assert plan.visible_rows == 4
    assert not plan.scrolls
    assert plan.top == 130
    assert plan.height == 4 * _ROW


def test_a_box_near_the_bottom_opens_upwards() -> None:
    """用户 2026-10-02: 位于软件下方的下拉框默认向上展示.

    锚点上沿 700、20 行(理想高度 200)在下方只剩 60px 可用 —— 上方有 698px, 于是翻上去,
    底边贴着锚点上沿减掉那道缝。
    """
    plan = plan_dropdown(
        rows=20,
        row_height=_ROW,
        anchor_top=700,
        anchor_bottom=728,
        window_top=_WINDOW_TOP,
        window_bottom=_WINDOW_BOTTOM,
        screen_top=0,
        screen_bottom=1080,
        gap=2,
    )
    assert plan.flipped
    assert plan.top + plan.height == 698
    assert plan.visible_rows == 10, "超过上限的部分交给滚动条, 不再往上撑"
    assert plan.scrolls


def test_too_many_rows_stop_at_the_cap_and_scroll() -> None:
    """内容超过上限就出滚动条(而不是一直撑开): 显示行数锁在上限。"""
    plan = plan_dropdown(
        rows=40,
        row_height=_ROW,
        anchor_top=100,
        anchor_bottom=128,
        window_top=_WINDOW_TOP,
        window_bottom=_WINDOW_BOTTOM,
        screen_top=0,
        screen_bottom=1080,
        gap=2,
    )
    assert plan.visible_rows == 10
    assert plan.scrolls
    assert not plan.flipped


def test_a_short_window_squeezes_the_rows_instead_of_overflowing() -> None:
    """两边都放不下时: 取空间大的一边, 并把行数压到放得下的行数.

    窗口只有 200px 高, 锚点几乎居中(下方 60px、上方 104px): 向上更宽裕, 于是翻上去,
    能放 5 行(104 / 20 = 5.2 → 5), 剩下的走滚动条 —— 关键是**不越出窗口**。
    """
    plan = plan_dropdown(
        rows=20,
        row_height=_ROW,
        anchor_top=110,
        anchor_bottom=140,
        window_top=_WINDOW_TOP,
        window_bottom=200,
        screen_top=0,
        screen_bottom=1080,
        gap=2,
    )
    assert plan.flipped
    assert plan.visible_rows == 5
    assert plan.scrolls
    assert plan.top >= _WINDOW_TOP
    assert plan.top + plan.height <= 200


def test_the_screen_clamp_beats_everything() -> None:
    """窗口本身比屏幕还高时, 至少保证浮层在屏幕上看得见(按屏幕夹一次)."""
    plan = plan_dropdown(
        rows=20,
        row_height=_ROW,
        anchor_top=900,
        anchor_bottom=928,
        window_top=0,
        window_bottom=2000,
        screen_top=0,
        screen_bottom=1080,
        gap=2,
    )
    assert plan.top + plan.height <= 1080 - 8
    assert plan.top >= 8


def test_the_scrollbar_slider_stands_out_from_the_trough() -> None:
    """滚动条滑块与轨道必须一眼分得开.

    出处(用户 2026-10-03): "显示内容超出上限变为可滚动区域但是看不到滚动条, 容易产生
    误判"。滑块用 CTk 滚动条那一档暗色时会与轨道几乎同色 —— 看着就像没有滚动条。
    """
    for theme in ("dark", "light"):
        palette = Palette.for_theme(theme)
        trough, slider = scrollbar_colors(palette)
        ratio = contrast.contrast_ratio(slider, trough)
        assert ratio >= contrast.NON_TEXT_MINIMUM, (
            f"{theme} 主题下滑块与轨道只有 {ratio:.2f}:1, 看不出有滚动条"
        )


def test_the_width_grows_for_long_values_but_stays_inside_the_window() -> None:
    """内容比控件宽就放宽(文字不被裁), 但右边界是硬的: 越界就夹回来。"""
    left, width = plan_dropdown_width(
        anchor_left=100,
        anchor_width=140,
        widest=300,
        window_right=1000,
        screen_right=1920,
        text_pad=8,
    )
    assert (left, width) == (100, 316), "300 + 左右各 8"

    left, width = plan_dropdown_width(
        anchor_left=900,
        anchor_width=140,
        widest=300,
        window_right=1000,
        screen_right=1920,
        text_pad=8,
    )
    assert left + width <= 1000 - 4, "窗口右边只剩 96px, 宁可裁字也不飘出窗口"
    assert width >= 1
