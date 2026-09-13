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

from customtkinter import CTkCanvas, DrawEngine

_APPLIED = False


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
