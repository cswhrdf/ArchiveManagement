"""CustomTkinter 绘制层修补.

CustomTkinter 的绘制引擎会把绘制尺寸向下取整到偶数(``DrawEngine`` 里的
``_round_width_to_even_numbers`` / ``_round_height_to_even_numbers`` 默认为
``True``), 于是宽或高为**奇数**的控件最右一列 / 最下一行不会被涂上边框色:
露出来的是父容器背景, 用户看到的就是右边框、下边框"消失"。库自己在需要精确
拼接的地方也会关掉它(``CTkSegmentedButton`` 给子按钮传
``round_*_to_even_numbers=False``, 否则按钮之间会有 1px 缝隙), 但 ``CTkFrame``
没有暴露该参数, 因此这里在引擎层面统一关掉。

另一件事是**图片属于哪个解释器**: ``CTkImage`` 贴图时用
``ImageTk.PhotoImage(图片)``(**不带 master**), 不带 master 的 Tk 图片会落到
``tkinter._default_root`` 上 —— 而不是"要用这张图的那个控件"。进程里一旦有第二个
Tk 根(没拆干净的窗口、嵌入的第二个解释器), 图片就被建到**另一个**解释器里, 贴上去时报

    TclError: image "pyimage1" does not exist

它与"图片被回收"(标签的 image 选项还指着已销毁的图片)看起来一样, 根因却完全不同。
所以凡是我们自己建的 ``CTkImage`` 都走 :func:`host_image`, 由它把宿主的解释器记下来。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

from customtkinter import CTkCanvas, CTkComboBox, CTkImage, DrawEngine
from PIL import Image, ImageTk

_APPLIED = False
_COMBO_APPLIED = False


class _HostImage(CTkImage):  # type: ignore[misc]  # customtkinter 没有类型标注
    """把贴图固定到宿主控件那个解释器上的 ``CTkImage``(见本模块顶部说明).

    只覆盖"造贴图"的两个私有方法(``CTkLabel``/``CTkButton`` 都经由
    ``create_scaled_photo_image`` 走到它们), 其余行为与库里的实现一致。
    """

    def __init__(
        self, host: tk.Misc, light_image: Image.Image, size: tuple[int, int]
    ) -> None:
        """记住宿主控件(它的解释器就是这张图该待的地方)."""
        super().__init__(light_image=light_image, size=size)
        self._host = host

    def _photo_image(
        self,
        image: Image.Image,
        cache: dict[tuple[int, int], ImageTk.PhotoImage],
        scaled_size: tuple[int, int],
    ) -> ImageTk.PhotoImage:
        """取缓存里的贴图; 没有就用**宿主**的解释器建一张(而不是默认根)."""
        if scaled_size not in cache:
            cache[scaled_size] = ImageTk.PhotoImage(
                image.resize(scaled_size), master=self._host
            )
        return cache[scaled_size]

    def _get_scaled_light_photo_image(
        self, scaled_size: tuple[int, int]
    ) -> ImageTk.PhotoImage:
        """浅色模式的贴图(与库里的方法同名同签名)."""
        return self._photo_image(
            self._light_image, self._scaled_light_photo_images, scaled_size
        )

    def _get_scaled_dark_photo_image(
        self, scaled_size: tuple[int, int]
    ) -> ImageTk.PhotoImage:
        """深色模式的贴图(与库里的方法同名同签名)."""
        return self._photo_image(
            self._dark_image, self._scaled_dark_photo_images, scaled_size
        )


def host_image(
    host: tk.Misc, *, light_image: Image.Image, size: tuple[int, int]
) -> CTkImage:
    """建一张"绑在 ``host`` 所在解释器上"的 ``CTkImage``(见本模块顶部说明).

    ``host`` 用**将要显示这张图的控件**(或它所在窗口里的任意控件)即可: 图片只需要
    落在同一个解释器里。写成函数而不是让调用方直接用类, 是为了让"我们自己建的图片
    都走这条路"这件事只有一个落点, 也方便守卫按名字检查。
    """
    return _HostImage(host, light_image, size)


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
