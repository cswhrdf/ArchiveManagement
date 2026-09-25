"""界面字号单位(px / rem)与缩放换算的单元测试."""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from archive_management.config import DEFAULT_BASE_FONT_PX
from archive_management.ui import typography

pytestmark = [
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("设置窗口"),
    pytest.mark.story("可调基准字号"),
    pytest.mark.layer("unit"),
]


@pytest.fixture(autouse=True)
def _restore_base_font() -> Iterator[None]:
    """每个用例后把基准字号拨回默认值, 避免影响其它用例(它是进程内全局)."""
    yield
    typography.set_base_font_px(DEFAULT_BASE_FONT_PX)


def test_default_scale_is_identity() -> None:
    """默认 16px 时倍数为 1.0: 既有字号一个像素都不会变."""
    scale = typography.set_base_font_px(DEFAULT_BASE_FONT_PX)

    assert scale.ratio == 1.0
    assert typography.scaled(11) == 11
    assert typography.scaled(34) == 34
    assert typography.rem(1) == DEFAULT_BASE_FONT_PX
    assert typography.rem(0.5) == DEFAULT_BASE_FONT_PX // 2


def test_scaling_follows_the_base_size() -> None:
    """基准字号 20px 时一切等比放大(12 → 15), rem 直接等于基准字号."""
    typography.set_base_font_px(20)

    assert typography.scaled(12) == 15
    assert typography.scaled(16) == 20
    assert typography.rem(1) == 20
    assert typography.rem(1.5) == 30


def test_scale_never_produces_a_zero_size() -> None:
    """再小也不给 0: 0 号字在 Tk 里等于用默认字体, 会让"缩小"看起来没生效."""
    typography.set_base_font_px(DEFAULT_BASE_FONT_PX)
    tiny = typography.FontScale(base_px=1)

    assert tiny.px(0) == 1
    assert tiny.px(8) == 1
    assert tiny.rem(0) == 1
    # Tk 里负字号表示"以像素为单位", 缩放后也要保持负数
    assert typography.scaled(-12) == -12
    assert tiny.px(-16) == -1


@pytest.mark.parametrize(
    ("given", "expected"),
    [(5, 12), (99, 28), (16, 16), (18, 18)],
)
def test_base_font_is_clamped_to_the_allowed_range(given: int, expected: int) -> None:
    """越界的基准字号夹到配置允许的区间(最后一道保护)."""
    assert typography.set_base_font_px(given).base_px == expected
    assert typography.current_scale().base_px == expected
