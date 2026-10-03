"""CustomTkinter 绘制层修补.

CustomTkinter 的绘制引擎会把绘制尺寸向下取整到偶数(``DrawEngine`` 里的
``_round_width_to_even_numbers`` / ``_round_height_to_even_numbers`` 默认为
``True``), 于是宽或高为**奇数**的控件最右一列 / 最下一行不会被涂上边框色:
露出来的是父容器背景, 用户看到的就是右边框、下边框"消失"。库自己在需要精确
拼接的地方也会关掉它(``CTkSegmentedButton`` 给子按钮传
``round_*_to_even_numbers=False``, 否则按钮之间会有 1px 缝隙), 但 ``CTkFrame``
没有暴露该参数, 因此这里在引擎层面统一关掉。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from customtkinter import CTkCanvas, CTkComboBox, DrawEngine

_APPLIED = False
_COMBO_APPLIED = False


def apply_border_rendering_fix() -> bool:
    """关闭绘制引擎的"偶数取整", 返回本次调用是否真的打了补丁(幂等).

    必须在创建任何 CustomTkinter 控件之前调用: 补丁替换的是
    :class:`DrawEngine` 的 ``__init__``, 只对之后创建的控件生效。
    """
    global _APPLIED
    if _APPLIED:
        return False
    original_init = DrawEngine.__init__

    def init(self: DrawEngine, canvas: CTkCanvas) -> None:
        original_init(self, canvas)
        self.set_round_to_even_numbers(False, False)

    DrawEngine.__init__ = init
    _APPLIED = True
    return True


def paint_combo_border(combo: CTkComboBox) -> None:
    """把下拉框右侧那半段边框描回**边框色**(见 :func:`apply_combo_border_fix`)."""
    try:
        color = combo._apply_appearance_mode(combo._border_color)
        combo._canvas.itemconfig("border_parts_right", outline=color, fill=color)
    except tk.TclError:  # pragma: no cover - 控件已销毁
        return


def apply_combo_border_fix() -> bool:
    """让下拉框的**整圈**边框都画出来, 返回本次调用是否真的打了补丁(幂等).

    出处(用户 2026-10-02): "所有的下拉框右侧边框均被下拉符号盖住了, 我希望将这个符号
    包裹进边框中"。CTkComboBox 画的是"左右分段边框"
    (``draw_rounded_rect_with_border_vertical_split``): 整圈边框画好之后, ``_draw``
    又把 ``border_parts_right`` 的描边与填充改成了 ``_button_color``(箭头区底色) ——
    右侧那段边框与底色同色, 看上去就是"没有边框/被箭头盖住"。

    补丁在每次绘制之后把那段描回 ``_border_color``; 箭头本身画在 ``inner_parts_right``
    上、位置不变, 于是它落在边框**里面**。

    **三个入口都要接**: 除了 ``_draw``, ``_on_enter`` / ``_on_leave`` 也会**直接**改
    ``border_parts_right``(悬停时改成 ``_button_hover_color``) —— 只接 ``_draw`` 的话
    鼠标一放上去边框又没了(用户 2026-10-02 追加反馈)。

    与 :func:`apply_border_rendering_fix` 一样是**进程级**补丁, 必须在创建控件之前调用
    (导入 ``ui.widgets`` 时就会打上, 见那里的调用)。
    """
    global _COMBO_APPLIED
    if _COMBO_APPLIED:
        return False

    def repaint(original: Callable[..., None]) -> Callable[..., None]:
        """把原件包一层: 原件跑完之后再把右侧边框描回边框色."""

        def wrapper(self: CTkComboBox, *args: object, **kwargs: object) -> None:
            original(self, *args, **kwargs)
            paint_combo_border(self)

        return wrapper

    for name in ("_draw", "_on_enter", "_on_leave"):
        setattr(CTkComboBox, name, repaint(getattr(CTkComboBox, name)))
    _COMBO_APPLIED = True
    return True
