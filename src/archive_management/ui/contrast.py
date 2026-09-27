"""颜色对比度: 界面"看得清吗"的**唯一算法**.

调色板的取色与守卫的判定都只能依赖数值,不能再靠"看着还行"—— 两类公式都来自
WCAG 2.1:

* 相对亮度: sRGB 通道先线性化,再按 0.2126 / 0.7152 / 0.0722 加权;
* 对比度: ``(L_亮 + 0.05) / (L_暗 + 0.05)``,范围 1.0(同色)到 21.0(黑白)。

本模块只做算术,不碰 Tk,因此既能在守卫里逐对断言,也能在运行期用。
"""

from __future__ import annotations

import math

# 三类判据的下限(见 docs/testing.md「对比度判据」一节):
#   * 正文/次要文字按 WCAG 2.1 AA 的 4.5:1;
#   * 选中描边、状态点这类"非文字"的信息载体按 3:1;
#   * 禁用态文字规范里豁免(禁用控件没有对比度要求),但我们仍然设一条底线,
#     否则"禁用"会退化成看不见 —— 用户分不清是禁用还是没画出来。
TEXT_MINIMUM = 4.5
NON_TEXT_MINIMUM = 3.0
DISABLED_MINIMUM = 2.5

_CHANNEL_STARTS = (1, 3, 5)
_HEX_LENGTH = 7
_HEX_DIGITS = frozenset("0123456789abcdefABCDEF")


def is_hex(color: object) -> bool:
    """判断一个值是不是 ``#rrggbb`` 形式的颜色.

    CTk 的配色选项不保证是字符串: 没显式设置时它会给出**主题对**
    (``["#ffffff", "#1a1a1a"]``), 另外还有 ``"transparent"`` —— 这两类都没法算
    对比度, 所以先问一句再算(调用方因此不用自己写 try)。
    """
    return (
        isinstance(color, str)
        and len(color) == _HEX_LENGTH
        and color.startswith("#")
        and all(char in _HEX_DIGITS for char in color[1:])
    )


def _channel(color: str, start: int) -> float:
    """取 ``#rrggbb`` 里某个通道,归一化到 0..1."""
    return int(color[start : start + 2], 16) / 255


def _linearize(value: float) -> float:
    """把一个 sRGB 通道线性化(WCAG 2.1 的分段函数).

    用 ``math.pow`` 而不是 ``**``: ``float ** 2.4`` 在 mypy 里被算成 ``Any``
    (非整数指数的幂运算没有精确的返回类型), 会让整个函数的返回类型退化成 ``Any``。
    """
    if value <= 0.04045:
        return value / 12.92
    return math.pow((value + 0.055) / 1.055, 2.4)


def relative_luminance(color: str) -> float:
    """返回 ``#rrggbb`` 的相对亮度(0 = 黑,1 = 白)."""
    red = _channel(color, _CHANNEL_STARTS[0])
    green = _channel(color, _CHANNEL_STARTS[1])
    blue = _channel(color, _CHANNEL_STARTS[2])
    luminance: float = (
        0.2126 * _linearize(red)
        + 0.7152 * _linearize(green)
        + 0.0722 * _linearize(blue)
    )
    return luminance


def contrast_ratio(foreground: str, background: str) -> float:
    """返回两色的对比度(与前后顺序无关)."""
    first = relative_luminance(foreground)
    second = relative_luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)
