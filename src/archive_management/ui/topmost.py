"""宿主窗口"强制置顶"状态的判定: 浮层与对话框跟随宿主置顶用.

背景(用户 2026-10-30): "当将窗口强制置顶时, 自定义的下拉框就看不到了"。宿主进了置顶层
(Windows 的 ``HWND_TOPMOST``)后, **自己非置顶**的附属(``transient``)窗口反而排到宿主
之下 —— 浮层被主窗口整块盖住。而 Tk 的 ``attributes("-topmost")`` 读数是它**自己写的
缓存位**: 应用内没置过顶就恒为 False, 外部工具(PowerToys 的 Win+Ctrl+T、DeskPins 这类
走 ``SetWindowPos`` 的)把窗口置顶后读数**不会**变 —— 探针实测: 置顶后 Tk 仍读到 0,
``wm stackorder`` 里浮层已经翻到宿主之下。所以 Windows 上要越过 Tk 直查真实窗口样式。
"""

from __future__ import annotations

import ctypes
import sys
import tkinter as tk
from ctypes import c_uint, c_void_p

# Win32 的几个常量(只在 Windows 分支用到): 取顶层句柄 / 读扩展样式 / "总在最前"位。
_GA_ROOT = 2
_GWL_EXSTYLE = -20
_WS_EX_TOPMOST = 0x00000008


def host_is_pinned(host: tk.Misc) -> bool:
    """宿主(或它所属的顶层窗口)现在是不是被系统级置顶(强制置顶).

    宿主可以是**任意控件**, 不一定是窗口: ``dialogs._present`` 收到的 parent 常是页面
    框架 —— 而 ``attributes`` / ``wm_attributes`` 是 ``Wm``(**窗口**)的方法, 普通控件
    (``CTkFrame``)根本没有(2026-10-31 CI: Linux/macOS 上就这样炸过)。置顶位长在窗口上,
    所以先沿 ``winfo_toplevel`` 到顶层窗口再问; Windows 分支的句柄同样取顶层窗口的。

    Windows 上**只用**直查窗口样式这一条路, 连 ``attributes("-topmost")`` 的读都不能碰:
    那次读会触发 Tk 拿自己缓存的窗口样式**回写**操作系统 —— 2026-10-30 探针实测, 外部
    ``SetWindowPos`` 置顶后(``GWL_EXSTYLE``=0x108)读一次 Tk 读数, ``WS_EX_TOPMOST`` 位
    就没了(0x100)。于是"先读 Tk 缓存、再查真值"的写法永远查不到外部置顶: 缓存读把证据
    销毁了。其它平台没有这条回写路径, "强制置顶"也主要是应用内置顶一种来源, 直接信 Tk
    的读数。
    """
    window = host.winfo_toplevel()
    if sys.platform != "win32":  # platform: linux macos - 没有 Win32 可查, 信 Tk 读数
        pinned = window.wm_attributes("-topmost")  # platform: linux macos - 窗口的读数
        return bool(pinned)  # platform: linux macos - 读数即判定
    user32 = ctypes.windll.user32  # platform: windows - 直查窗口样式只有 Win32 能做
    ga = user32.GetAncestor  # platform: windows - 取顶层句柄的接口
    ga.argtypes = [c_void_p, c_uint]  # platform: windows - 句柄按指针宽度传
    ga.restype = c_void_p  # platform: windows - 64 位句柄不截断
    handle = c_void_p(window.winfo_id())  # platform: windows - 顶层窗口自己的句柄
    root = ga(handle, _GA_ROOT)  # platform: windows - 由内层取到顶层窗口
    if not root:  # platform: windows - 句柄取不到按未置顶处理
        return False  # platform: windows - 句柄取不到就当未置顶
    get_long = user32.GetWindowLongW  # platform: windows - 读窗口样式
    root_ptr = c_void_p(root)  # platform: windows - 把顶层句柄按指针传
    style = get_long(root_ptr, _GWL_EXSTYLE)  # platform: windows - 读它的扩展样式
    return bool(style & _WS_EX_TOPMOST)  # platform: windows - WS_EX_TOPMOST 位
