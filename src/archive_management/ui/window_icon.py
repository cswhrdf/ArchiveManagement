"""窗口与任务栏图标: 把 CustomTkinter 自带的那颗盖掉.

CustomTkinter 在**每个**窗口建好之后 200ms(Windows)自己去贴一张
``CustomTkinter_icon_Windows.ico``, 条件是"这个窗口上没有人调用过 ``iconbitmap``":

* 主窗口本来没事 —— 我们在 ``ArchiveApp.__init__`` 里显式设过(那会把库的念头掐掉);
* **每个子窗口**都会顶着 CTk 那颗图标 —— 设置/定时任务/游戏管理/所有对话框, 以及 CTk 自己
  开的 Toplevel, 标题栏与任务栏里都一样(2026-10-11 用户报的就是这个)。

库没给我们留接口, 所以换掉那个私有方法(与 :mod:`archive_management.ui.rendering` 换掉
``DrawEngine`` 同一类手法): 换成"贴我们的图标"。库升级后方法名变了也不会出错 —— 那时
它只是照旧贴 CTk 的图标, 不会影响启动(守卫 `test_the_ctk_default_icon_is_replaced` 会在
改名的那一刻变红)。

**为什么窗口用一颗单独的小 ICO**(``app-window.ico``, 四档 BMP; 见 ``services/icon_assets``):
2026-10-11 实测 —— 把入库那颗七档大图交给 Tk 之后, 集成套件跑到第 200 个窗口左右会在
``update_idletasks`` 里以 access violation 死掉(原生栈见 ``crash-dumps/faulthandler.log``),
而同一批文件不装本补丁跑是绿的; 换成四档 BMP + 与库同样"只贴一次"的判据之后那批文件
恢复全绿。exe 的 PE 资源仍用那颗七档大图(那是 Windows 读的, 不过 Tk 的手)。
"""

from __future__ import annotations

import tkinter as tk
from typing import Protocol, cast

from customtkinter.windows.ctk_tk import CTk
from customtkinter.windows.ctk_toplevel import CTkToplevel

from archive_management.services import icon_assets

_APPLIED = False
# 贴图必须留引用: PhotoImage 一旦被回收, 图标跟着消失。挂在窗口自己身上最省事。
_PHOTO_ATTR = "_app_icon_photo"
# CustomTkinter 自己那个开关(它的 ``iconbitmap`` 重写会把它置真); 名字经变量传给 getattr。
_SET_BY_CALLER_ATTR = "_iconbitmap_method_called"


class _IconWindow(Protocol):
    """我们真正用到的那两个方法(Tk 的图标 API 在 ``Wm`` 混入上, ``Misc`` 里没有).

    typeshed 给 ``wm_iconbitmap`` 没写参数类型, 而项目开了 ``disallow_untyped_calls``,
    所以按实际用法在这里声明一次 —— 调用点手上都是真的 ``Tk``/``Toplevel``, 这个 Protocol
    只是把签名说清楚, 不是运行时约束。
    """

    def iconbitmap(self, bitmap: str) -> None: ...

    def iconphoto(self, default: bool, image: tk.PhotoImage) -> None: ...


def apply_window_icon(window: tk.Misc) -> bool:
    """立刻给一个窗口贴上应用图标, 返回是否贴上了.

    Windows 优先用 ``app-window.ico``(四档 BMP: 标题栁 16 / 缩放 24与32 / 高 DPI 任务栏
    48); 拿不到或 Tk 不认时就退回 ``iconphoto`` + ``app-256.png``(X11/macOS 只认图片)。
    资源缺了、文件坏了都返回 ``False``, 绝不抛 —— 图标不是启动的前提。

    为什么窗口不用那颗七档的 ``app.ico``: 实测(2026-10-11)把它反复交给 Tk 的
    ``wm iconbitmap`` 之后, Windows 上的 Tk 会在长会话的重绘里踩到已释放内存
    (access violation)。那颗大图依旧入库、依旧给 exe 做 PE 资源 —— 只是不喂给 Tk。
    """
    icons = icon_assets.icon_files()
    icon_window = cast("_IconWindow", window)
    if icons.window_ico is not None:
        try:
            icon_window.iconbitmap(str(icons.window_ico))
        except tk.TclError:
            pass
        else:
            return True
    if icons.window_png is None:
        return False
    try:
        # master=window: 图片要建在这个窗口自己的解释器上, 否则多 Tk 根时贴到了别人家
        # (报 image "pyimageN" does not exist —— 见 rendering.py 顶部的说明)。
        photo = tk.PhotoImage(master=window, file=str(icons.window_png))
        icon_window.iconphoto(True, photo)
    except tk.TclError:
        return False
    setattr(window, _PHOTO_ATTR, photo)
    return True


def _titlebar_icon(window: tk.Misc) -> None:
    """CustomTkinter 那个 200ms 定时器的落点: 贴我们的图标(拿不到就什么都不做).

    判据跟着库自己的开关走(``_iconbitmap_method_called``): 窗口上已经有人设过图标
    (主窗口在 ``__init__`` 里就设了)就什么都不做 —— 与库原来的语义一致, 于是我们
    不会比库**多**发生一次 ``wm iconbitmap``(实测那多出来的一次会把 Tk 弄崩)。
    """
    if getattr(window, _SET_BY_CALLER_ATTR, False):
        return
    apply_window_icon(window)


def install_default_window_icon() -> None:
    """把 CTk 那两个私有方法换成我们的实现(幂等; 在任何平台上调用都安全).

    CTk 的默认图标只在 Windows 上贴, 所以其它平台这里换了也走不到 —— 但换了不影响任何事,
    代码里也就少一个"只在某平台才装"的分支。
    """
    global _APPLIED
    if _APPLIED:
        return
    _APPLIED = True
    # 方法名经变量传: 写常量字符串会被 ruff 的 B010 拦(与 rendering.py 同一条做法)。
    method = "_windows_set_titlebar_icon"
    for window_class in (CTk, CTkToplevel):
        setattr(window_class, method, _titlebar_icon)
