"""界面字号单位: px(基准) 与 rem(相对基准字号).

浏览器里 ``1rem`` = 根字号; 这里取同一套语义: **1rem = 基准字号, 默认 16px**,
用户可在设置里调整(``config.ui.base_font_px``, 见 PLAN 的界面字号条目)。

代码里的字号仍然按 **px 口径**书写(与既有 137 处 ``CTkFont(size=N)`` 一致),
:func:`scaled` 是唯一的换算入口: 设置里选 16px 时倍数为 1.0(界面与既有尺寸完全一致),
选 20px 时所有字号等比放大 25%。

**不引入其它单位**(评估结论):

- ``px``: 基准单位 —— Tk 最终只认像素/点, 因此换算的落点只能是它;
- ``rem``: 引入 —— 需要"跟随用户设置"的地方都该用它;
- ``em``: 不引入 —— Tk 没有字体级联可依附, 每个控件各自持有一份字体, "相对于父元素
  字号"在桌面控件树里没有确定含义, 引进来只会让人误以为有继承;
- ``pt``: 不引入 —— 它是 Tk 的原生单位, 但会跟着系统 DPI 变, 混用会让"用户设定的
  16px"在不同机器上得到不同的物理大小;
- ``%`` / ``vw`` / ``vh``: 不引入 —— 桌面窗口没有稳定的参照物(视口会随窗口缩放),
  需要"占容器比例"的地方由布局权重(``grid`` 的 ``weight``)表达。

现有调用点不必改动: :func:`install_font_scaling` 把缩放装到 ``ctk.CTkFont`` 上,
``CTkFont(size=N)`` 从此都按当前基准字号走; 新代码可以用 :func:`font` / :func:`rem`
把意图写得更直白。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import customtkinter as ctk

from archive_management.config import BASE_FONT_CHOICES, DEFAULT_BASE_FONT_PX

#: 1rem 的默认像素值(与配置里的默认值同源)。
BASE_FONT_PX = DEFAULT_BASE_FONT_PX
#: 设置窗口列出的可选基准字号。
FONT_CHOICES = BASE_FONT_CHOICES


@dataclass(frozen=True)
class FontScale:
    """一次字号换算(基准字号 → 实际字号)."""

    base_px: int = DEFAULT_BASE_FONT_PX

    @property
    def ratio(self) -> float:
        """相对默认基准的倍数: 默认 16px 时正好是 1.0."""
        return self.base_px / DEFAULT_BASE_FONT_PX

    def px(self, value: float) -> int:
        """把"px 口径的字号"换算成实际字号(负数保持负数: Tk 用负值表示像素)."""
        scaled = round(value * self.ratio)
        return scaled if scaled < 0 else max(1, scaled)

    def rem(self, value: float) -> int:
        """把 rem 换算成实际字号(1rem = 基准字号)."""
        scaled = round(value * self.base_px)
        return max(1, scaled)


_SCALE = FontScale()
# 装上缩放之前的原始类(只换模块属性, 不动第三方包本身).
# customtkinter 没有类型标注, 因此这里的基类在 mypy 眼里就是 Any。
_ORIGINAL_FONT: Any = ctk.CTkFont


def current_scale() -> FontScale:
    """返回当前生效的字号换算."""
    return _SCALE


def set_base_font_px(value: int) -> FontScale:
    """设置基准字号(px), 返回生效的换算.

    取值会被夹进配置允许的区间 —— 这是最后一道保护: 传进来的值已经过配置校验,
    但代码里也可能直接调用, 夹一下比抛异常更适合"界面字号"这种纯外观参数。
    """
    global _SCALE
    lowest, highest = min(FONT_CHOICES), max(FONT_CHOICES)
    _SCALE = FontScale(base_px=min(max(int(value), lowest), highest))
    return _SCALE


def scaled(value: float) -> int:
    """把 px 口径的字号换算成当前实际字号."""
    return _SCALE.px(value)


def rem(value: float) -> int:
    """把 rem 换算成当前实际字号(1rem = 基准字号)."""
    return _SCALE.rem(value)


def font(
    size: float, *, weight: str = "normal", family: str | None = None
) -> ctk.CTkFont:
    """按当前基准字号创建一个字体(px 口径的 ``size``)."""
    return ctk.CTkFont(size=scaled(size), weight=weight, family=family)


class _ScaledFont(_ORIGINAL_FONT):  # type: ignore[misc]  # customtkinter 没有类型标注
    """按当前基准字号缩放的 ``CTkFont``(见 :func:`install_font_scaling`)."""

    def __init__(
        self,
        family: str | None = None,
        size: int | None = None,
        **kwargs: object,
    ) -> None:
        """与 ``CTkFont`` 同签名, 只把 ``size`` 按基准字号换算一次."""
        super().__init__(
            family=family, size=None if size is None else scaled(size), **kwargs
        )


def install_font_scaling() -> None:
    """把字号缩放装到 ``ctk.CTkFont`` 上(幂等).

    界面里有上百处 ``CTkFont(size=N)`` 直接构造字体, 逐个改既容易漏也不值得;
    这里只替换 ``customtkinter`` 模块上的类属性, 之后创建的字体都会经过
    :func:`scaled` —— 与 :func:`archive_management.i18n.set_locale` 一样是
    "进程内全局", 由主窗口在构造界面之前调用一次。
    """
    if ctk.CTkFont is _ScaledFont:  # pragma: no cover - 只有重复调用才会走到
        return
    ctk.CTkFont = _ScaledFont


__all__ = [
    "BASE_FONT_PX",
    "FONT_CHOICES",
    "FontScale",
    "current_scale",
    "font",
    "install_font_scaling",
    "rem",
    "scaled",
    "set_base_font_px",
]
