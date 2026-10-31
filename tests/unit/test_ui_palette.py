"""UI 调色板单元测试."""

from __future__ import annotations

import pytest

from archive_management.ui.palette import (
    DARK,
    DEFAULT_THEME,
    LIGHT,
    THEME_NAMES,
    Palette,
)

pytestmark = [
    pytest.mark.ui,
    pytest.mark.trivial,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("主题调色板"),
    pytest.mark.story("深浅色主题切换"),
    pytest.mark.layer("unit"),
]

_COLOR_FIELDS = (
    "background",
    "accent",
    "text_primary",
    "danger",
    "border",
    "accent_soft",
    "accent_soft_text",
)


def test_default_theme_is_dark() -> None:
    assert DEFAULT_THEME == "dark"


def test_theme_names_contain_both() -> None:
    assert set(THEME_NAMES) == {"dark", "light"}


@pytest.mark.parametrize(
    ("theme", "expected"),
    [("dark", DARK), ("light", LIGHT), ("neon", DARK)],
    ids=["dark", "light", "unknown-falls-back-to-dark"],
)
def test_for_theme_returns_the_named_palette(theme: str, expected: Palette) -> None:
    """命名主题取对应的调色板; 未知名不抛异常, 回落到深色."""
    assert Palette.for_theme(theme) is expected


def test_palettes_define_required_colors() -> None:
    for palette in (DARK, LIGHT):
        for field in _COLOR_FIELDS:
            value = getattr(palette, field)
            assert value.startswith("#"), (palette, field)


def test_palettes_differ_in_background() -> None:
    assert DARK.background != LIGHT.background
