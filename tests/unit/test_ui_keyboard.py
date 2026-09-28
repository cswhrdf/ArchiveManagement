"""键盘可用性的无头单元测试(取色与类型边界).

焦点环的颜色是**算术**问题: "在这颗控件自己的底色上, 焦点环看不看得见"由
:mod:`archive_management.ui.contrast` 给出数值, 因此这一层不需要 Tk 环境就能穷举
(两套主题, 五种按钮样式, 五个界面底色)。真控件上的聚焦行为在
``tests/integration/test_gui_keyboard.py`` 里量。
"""

from __future__ import annotations

from typing import Any

import pytest

from archive_management.ui import contrast, keyboard
from archive_management.ui.palette import DARK, LIGHT, Palette
from archive_management.ui.widgets import BUTTON_STYLES, button_colors

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("纯键盘可用"),
    pytest.mark.layer("unit"),
]

# 界面里出现过的"控件底色": 按钮各样式、输入框、面板、窗口底、卡片。
BACKGROUND_TOKENS = ("input_bg", "panel", "raised", "background", "card")
PALETTES = (LIGHT, DARK)


class _FakeWidget:
    """只有 ``cget``/``configure`` 的假控件(焦点环取色与描边读写只需要这两个).

    ``cget`` 对不支持的选项抛 ``ValueError`` —— 这是 CTk 的**实测**行为
    (``CTkRadioButton`` 没有可读的 ``border_width``), 而 :func:`_paint_options`
    正是为它而写的。
    """

    def __init__(self, palette: Palette, colors: dict[str, Any]) -> None:
        self._palette = palette
        self._colors = dict(colors)

    def cget(self, option: str) -> Any:
        """返回该控件在这个用例里的选项值(没有这个选项就报错, 同 CTk)."""
        if option not in self._colors:
            raise ValueError(f"unknown option {option!r}")
        return self._colors[option]

    def configure(self, **options: Any) -> None:
        """写入选项(未支持的选项同样报错)."""
        for key in options:
            if key not in self._colors:
                raise ValueError(f"unknown option {key!r}")
        self._colors.update(options)

    def winfo_toplevel(self) -> Any:
        """返回自己所在的"窗口"(与控件共用同一套调色板)."""
        return self


def _widget(palette: Palette, **colors: Any) -> _FakeWidget:
    """造一个带指定选项值的假控件."""
    return _FakeWidget(palette, colors)


@pytest.mark.parametrize("palette", PALETTES, ids=("light", "dark"))
@pytest.mark.parametrize("style", BUTTON_STYLES)
def test_focus_ring_is_visible_on_every_button_style(
    palette: Palette, style: str
) -> None:
    """每颗按钮的底色上都挑得出一个达标的焦点环(主色实底按钮也不例外)."""
    colors = button_colors(palette, style)  # type: ignore[arg-type]
    widget = _widget(palette, fg_color=colors.fg)
    ring = keyboard.ring_color(widget)

    assert ring is not None
    ratio = contrast.contrast_ratio(ring, colors.fg)
    assert ratio >= keyboard.FOCUS_RING_MINIMUM, (
        f"{style} 的底色 {colors.fg} 上, 焦点环 {ring} 只有 {ratio:.2f}:1"
    )


@pytest.mark.parametrize("palette", PALETTES, ids=("light", "dark"))
@pytest.mark.parametrize("token", BACKGROUND_TOKENS)
def test_focus_ring_is_visible_on_every_surface(palette: Palette, token: str) -> None:
    """输入框/开关/面板上的焦点环同样要达标(它们不是按钮)."""
    fill = str(getattr(palette, token))
    ring = keyboard.ring_color(_widget(palette, fg_color=fill))

    assert ring is not None
    ratio = contrast.contrast_ratio(ring, fill)
    assert ratio >= keyboard.FOCUS_RING_MINIMUM, (
        f"{token}={fill} 上的焦点环 {ring} 只有 {ratio:.2f}:1"
    )


@pytest.mark.parametrize("palette", PALETTES, ids=("light", "dark"))
def test_the_ring_is_one_of_the_two_dedicated_tokens(palette: Palette) -> None:
    """焦点环只用两个**专用于焦点**的 token, 不再复用 accent / text_primary(用户要求).

    两档的理由是量出来的: "一套主题一个颜色"时, 普通底色与主色/危险色实底要求的明度
    刚好相反, 合并后最优也只有 1.87:1(深色) / 3.67:1(浅色) —— 达不到 3:1。
    """
    dedicated = {palette.focus_ring, palette.focus_ring_on_fill}
    for token in (*BACKGROUND_TOKENS, "accent", "danger", "accent_soft"):
        fill = str(getattr(palette, token))
        ring = keyboard.ring_color(_widget(palette, fg_color=fill))
        assert ring in dedicated, f"{token}={fill} 上的焦点环 {ring} 不是专用色"


@pytest.mark.parametrize("palette", PALETTES, ids=("light", "dark"))
def test_plain_surfaces_use_the_plain_ring_token(palette: Palette) -> None:
    """普通底色用 ``focus_ring``(不用实底那一档)."""
    for token in BACKGROUND_TOKENS:
        fill = str(getattr(palette, token))
        widget = _widget(palette, fg_color=fill)
        assert keyboard.ring_color(widget) == palette.focus_ring


@pytest.mark.parametrize("palette", PALETTES, ids=("light", "dark"))
def test_solid_fills_use_the_on_fill_token(palette: Palette) -> None:
    """主色/危险色实底按钮用 ``focus_ring_on_fill`` —— 12 号反馈的那颗按钮就在这里."""
    for token in ("accent", "danger"):
        fill = str(getattr(palette, token))
        widget = _widget(palette, fg_color=fill)
        ring = keyboard.ring_color(widget)

        assert ring == palette.focus_ring_on_fill
        assert ring != fill
        assert contrast.contrast_ratio(ring, fill) >= keyboard.FOCUS_RING_MINIMUM


def test_ring_falls_back_to_the_plain_token_when_the_fill_is_unknown() -> None:
    """底色读不到(透明/主题对)时取普通底色那一档, 而不是不画."""
    assert keyboard.ring_color(_widget(DARK, fg_color="transparent")) == DARK.focus_ring
    assert keyboard.ring_color(_widget(DARK)) == DARK.focus_ring


def test_the_ring_is_thick_enough_to_read_as_a_frame() -> None:
    """环宽至少 3px: 用户实测 2px 在主色/危险色实底按钮上"看起来只是按钮缩小了一圈"."""
    assert keyboard.FOCUS_RING_WIDTH >= 3


def test_is_hex_rejects_what_cannot_be_computed() -> None:
    """``is_hex`` 是"能不能算对比度"的唯一判据, 因此它自己的边界也要钉住."""
    assert contrast.is_hex("#55d6be") is True
    assert contrast.is_hex("#55D6BE") is True
    # CTk 的"主题对"、透明、缺位都不是颜色.
    assert contrast.is_hex(["#ffffff", "#1a1a1a"]) is False
    assert contrast.is_hex("transparent") is False
    assert contrast.is_hex("#55d6b") is False
    assert contrast.is_hex("#55d6bez") is False
    assert contrast.is_hex(None) is False


def test_every_patched_type_is_either_operable_or_excluded() -> None:
    """类型边界: 接键盘线的与"键盘操作不了"的合起来正好是要打补丁的那些."""
    assert set(keyboard.PATCHED_TYPES) == {
        *keyboard.REACHABLE_TYPES,
        *(keyboard.UNOPERABLE_TYPES),
    }
    assert not set(keyboard.REACHABLE_TYPES) & set(keyboard.UNOPERABLE_TYPES)


def test_lists_and_combos_are_not_treated_as_keyboard_operable() -> None:
    """下拉框的值只能用鼠标选, 因此不许被当成"可聚焦/可输入"的控件."""
    for cls in keyboard.UNOPERABLE_TYPES:
        assert not issubclass(cls, keyboard.REACHABLE_TYPES)
        assert not issubclass(cls, keyboard.INPUT_TYPES)
        assert not issubclass(cls, keyboard.ACTIVATABLE_TYPES)


def test_focusing_a_solid_button_swaps_it_to_the_soft_colours() -> None:
    """**聚焦时实底按钮换成软底配色** —— 用户要的"显眼"就落在这里.

    实底(主色/危险色)底色上任何亮色环都看不出(实测 1.27:1), 换成软底之后那一档
    亮色/深色专用环立刻 10:1 以上; 文字色跟着换成软底上的文字色, 所以依然读得清。
    """
    widget = _widget(
        DARK,
        fg_color=DARK.accent,
        border_color="#253a55",
        border_width=0,
        hover_color="#000000",
        text_color=DARK.accent_text,
    )
    saved: dict[str, object] = {}
    keyboard._ring_on(widget, saved)

    assert widget.cget("fg_color") == DARK.accent_soft
    assert widget.cget("text_color") == DARK.accent_soft_text
    assert widget.cget("border_color") == DARK.focus_ring
    assert widget.cget("border_width") == keyboard.FOCUS_RING_WIDTH
    assert saved["fg_color"] == DARK.accent  # 原样都记下来了

    keyboard._ring_off(widget, saved)
    assert widget.cget("fg_color") == DARK.accent
    assert widget.cget("text_color") == DARK.accent_text
    assert widget.cget("border_color") == "#253a55"
    assert widget.cget("border_width") == 0
    assert saved == {}


def test_the_swapped_fill_keeps_the_ring_and_the_text_readable() -> None:
    """换色之后的两条对比度: 环对**新**底色 ≥3:1、文字对**新**底色 ≥4.5:1."""
    for palette in PALETTES:
        for token in ("accent", "danger"):
            fill = str(getattr(palette, token))
            widget = _widget(palette, fg_color=fill)
            paint = keyboard._focused_paint(widget)
            new_fill = paint["fg_color"]

            ring = keyboard.ring_color(widget, fill=new_fill)
            assert ring == palette.focus_ring, f"{token} 换色后应该用抢眼的那一档"
            ring_ratio = contrast.contrast_ratio(ring, new_fill)
            assert ring_ratio >= keyboard.FOCUS_RING_MINIMUM, (
                f"{token} 换色成 {new_fill} 后, 环 {ring} 只有 {ring_ratio:.2f}:1"
            )
            text_ratio = contrast.contrast_ratio(paint["text_color"], new_fill)
            assert text_ratio >= contrast.TEXT_MINIMUM, (
                f"{token} 换色成 {new_fill} 后, 文字只有 {text_ratio:.2f}:1"
            )


def test_plain_buttons_only_get_the_ring() -> None:
    """普通底色的按钮只换环, 不动底色与文字色(不要为了显眼把整屏按钮都改色)."""
    widget = _widget(
        DARK,
        fg_color=DARK.raised,
        border_color="#253a55",
        border_width=1,
        text_color=DARK.text_body,
    )
    saved: dict[str, object] = {}
    keyboard._ring_on(widget, saved)

    assert widget.cget("fg_color") == DARK.raised
    assert widget.cget("text_color") == DARK.text_body
    assert widget.cget("border_color") == DARK.focus_ring


def test_ring_reads_and_restores_paint_without_border_width() -> None:
    """单选按钮没有 ``border_width``(实测): 只画颜色也要能画、也要能还原.

    这条是 12 号实测的那颗雷: 原实现把两样原值塞进同一个 ``suppress``, 读到一半
    就抛异常, 于是存下了 ``border_color`` 而没存 ``border_width`` —— 失焦时
    ``pop`` 一个不存在的键直接 ``KeyError``, 描边永远停在环色上。
    """
    widget = _widget(DARK, fg_color=DARK.raised, border_color="#253a55")
    saved: dict[str, object] = {}
    keyboard._ring_on(widget, saved)

    ring = keyboard.ring_color(widget)
    assert widget.cget("border_color") == ring
    assert saved == {"border_color": "#253a55", "fg_color": DARK.raised}

    keyboard._ring_off(widget, saved)  # 不报错才算还原
    assert widget.cget("border_color") == "#253a55"
    assert saved == {}


def test_paint_options_reports_nothing_when_the_control_has_no_border() -> None:
    """连描边都没有的控件(OptionMenu)画不了环: ``_ring_on`` 什么也不写."""
    widget = _widget(DARK, fg_color=DARK.input_bg)
    options = keyboard._paint_options(widget)

    assert "border_color" not in options

    saved: dict[str, object] = {}
    keyboard._ring_on(widget, saved)  # 应该什么也不做
    assert saved == {}
    assert widget.cget("fg_color") == DARK.input_bg


def test_a_focused_button_is_still_recognised_as_the_primary_action() -> None:
    """聚焦换色之后, 这颗按钮还得认得出是"主按钮"(否则回车确认会失效).

    实底按钮聚焦时会被换成软底配色 —— 那是**焦点态**, 不是"这颗按钮是什么按钮"。
    按实测底色认主按钮的机制(``primary_button``/配色守卫)如果不看原样, 焦点一落上去
    就认不出来了: CI 上实测"回车不确认"(找不到主按钮)与"危险色按钮少了一颗"都是这个。

    ``_focus_saved`` 由 :func:`make_reachable` 挂在控件上, 所以这里也照它的样子先挂一个。
    """
    widget = _widget(DARK, fg_color=DARK.accent, border_color="#253a55", border_width=0)
    saved: dict[str, object] = {}
    widget._focus_saved = saved  # type: ignore[attr-defined]

    keyboard._ring_on(widget, saved)
    assert widget.cget("fg_color") == DARK.accent_soft, "夹具要先造出已被换色的样子"
    assert keyboard.resting_fill(widget) == DARK.accent
    assert keyboard._focus_style(widget) == "accent"

    keyboard._ring_off(widget, saved)
    assert keyboard.resting_fill(widget) == DARK.accent
    assert keyboard._focus_style(widget) == "accent"


def test_resting_fill_falls_back_to_the_live_colour() -> None:
    """没接过键盘线(或已经失焦还原)的控件读活值; 记过原样但没记底色时也读活值."""
    widget = _widget(DARK, fg_color=DARK.raised)
    assert keyboard.resting_fill(widget) == DARK.raised

    widget._focus_saved = {"border_color": "#253a55"}  # type: ignore[attr-defined]
    assert keyboard.resting_fill(widget) == DARK.raised
