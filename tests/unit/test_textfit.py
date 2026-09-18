"""按像素裁剪文本的单元测试.

用一个"汉字 2 单位、其余 1 单位"的假字体测量, 覆盖: 放得下时原样返回、单行截断补
省略号、限定行数时逐行填满、英文在空格处断行、容器极窄或参数非法时不崩。

真实字体与真实控件的效果由 ``tests/integration/test_gui_layout.py`` 的长名称用例
把关(那边需要真的建窗口)。
"""

from __future__ import annotations

import pytest

from archive_management.ui.textfit import ELLIPSIS, fit_text

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("文本裁剪"),
    pytest.mark.story("长文本按宽度折行与截断"),
    pytest.mark.layer("unit"),
]

# 假字号: 一个"单位"= 10px, 汉字算 2 个单位(接近真实中文字体的宽高比).
_UNIT = 10


class _FakeFont:
    """只实现 ``measure`` 的字体替身(不需要 Tk 窗口)."""

    def measure(self, text: str) -> int:
        """汉字按 2 个单位、其余按 1 个单位累加."""
        return sum(2 if ord(char) > 0x2E80 else 1 for char in text) * _UNIT


def test_text_that_fits_is_returned_unchanged() -> None:
    """放得下时原样返回: 不引入换行, 也不补省略号."""
    assert fit_text("abc", _FakeFont(), 30) == "abc"
    assert fit_text("abc", _FakeFont(), 3000) == "abc"


def test_single_line_is_truncated_with_an_ellipsis() -> None:
    """单行放不下时截断, 用省略号占掉它自己的宽度."""
    assert fit_text("abcdefghijklmnop", _FakeFont(), 100) == f"abcdefghi{ELLIPSIS}"


def test_lines_fill_greedily_and_only_the_last_one_is_truncated() -> None:
    """限定行数时逐行填满, 只有最后一行带省略号."""
    text = "abcdefghijklmnopqrstuvwxyz"
    fitted = fit_text(text, _FakeFont(), 100, max_lines=2)

    assert fitted == f"abcdefghij\nklmnopqrs{ELLIPSIS}"
    lines = fitted.splitlines()
    assert len(lines) == 2
    assert all(_FakeFont().measure(line) <= 100 for line in lines)


def test_wide_characters_are_measured_by_pixels_not_by_char_count() -> None:
    """中文按宽度算: 10 个汉字(20 单位)在 5 单位的宽度里只放得下 2 个字加省略号."""
    fitted = fit_text("超级长的游戏名称示例", _FakeFont(), 50, max_lines=1)

    assert fitted == f"超级{ELLIPSIS}"
    assert _FakeFont().measure(fitted) <= 50


def test_english_text_breaks_at_spaces_when_wrapping() -> None:
    """折行时尽量保持单词完整."""
    assert fit_text("hello world again", _FakeFont(), 110, max_lines=2) == (
        "hello world\nagain"
    )


def test_ellipsis_only_when_truncated_in_a_single_line() -> None:
    """刚好放得下(不截断)时不能多出省略号."""
    assert fit_text("hello", _FakeFont(), 50) == "hello"


def test_a_width_too_small_for_the_ellipsis_returns_nothing() -> None:
    """宽度连省略号都放不下时返回空串, 而不是把容器撑宽."""
    assert fit_text("abc", _FakeFont(), 5) == ""


@pytest.mark.parametrize(
    ("width", "max_lines"), [(0, 1), (-10, 1), (100, 0), (100, -1)]
)
def test_invalid_budgets_return_the_text_unchanged(width: int, max_lines: int) -> None:
    """非法预算(0/负数)不裁剪: 上层给错值时不要静默吞掉文本."""
    assert fit_text("abc", _FakeFont(), width, max_lines=max_lines) == "abc"


def test_empty_text_stays_empty() -> None:
    assert fit_text("", _FakeFont(), 10) == ""
