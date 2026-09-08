"""模态对话框与确认.

统一高风险操作确认与信息展示方式,全部在主线程调用(后台回调经消息
队列回到主线程后再触发),避免工作线程直接操作 Tk 控件。所有文案经
i18n 配置加载,弹窗一律居中显示在主窗口上。
"""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from archive_management.i18n import tr
from archive_management.ui.palette import Palette


def _center(parent: ctk.CTk, window: ctk.CTkToplevel) -> None:
    """把弹窗移动到主窗口居中位置."""
    window.update_idletasks()
    window.geometry("+0+0")
    window.update()
    offset_x = window.winfo_rootx()
    offset_y = window.winfo_rooty()
    width = window.winfo_width()
    height = window.winfo_height()
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2 - offset_x
    y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2 - offset_y
    window.geometry(f"+{max(x, 0)}+{max(y, 0)}")


def confirm_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    message: str,
    confirm_text: str | None = None,
    cancel_text: str | None = None,
) -> bool:
    """显示居中确认对话框,返回用户选择."""
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    no_text = tr("dialog.cancel") if cancel_text is None else cancel_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=message,
        wraplength=380,
        justify="left",
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    label.pack(padx=24, pady=(22, 6), fill="x")

    result: list[bool] = []

    def choose(value: bool) -> None:
        result.append(value)
        window.destroy()

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(10, 20))
    cancel = ctk.CTkButton(
        buttons,
        text=no_text,
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=lambda: choose(False),
    )
    cancel.pack(side="left", padx=(0, 10))
    ok = ctk.CTkButton(
        buttons,
        text=ok_text,
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=lambda: choose(True),
    )
    ok.pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return bool(result and result[0])


def info_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    message: str,
) -> None:
    """显示居中信息对话框(非模态)."""
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=message,
        wraplength=400,
        justify="left",
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    label.pack(padx=24, pady=(22, 10), fill="x")

    ok = ctk.CTkButton(
        window,
        text=tr("dialog.ok"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=window.destroy,
    )
    ok.pack(pady=(0, 20))
    _center(parent, window)


def ask_branch_name(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    text: str,
) -> str | None:
    """询问分支名称;取消或内容为空返回 None."""
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=text,
        justify="left",
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    label.pack(padx=24, pady=(22, 8))

    entry = ctk.CTkEntry(
        window,
        width=320,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
    )
    entry.pack(padx=24, pady=(0, 10))
    entry.focus_set()

    result: list[str] = []

    def submit() -> None:
        name = entry.get().strip()
        if name:
            result.append(name)
            window.destroy()

    entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 18))
    cancel = ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    )
    cancel.pack(side="left", padx=(0, 10))
    ok = ctk.CTkButton(
        buttons,
        text=tr("dialog.confirm"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    )
    ok.pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def ask_text(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    text: str,
    initial: str = "",
    browse: Callable[[], str | None] | None = None,
    confirm_text: str | None = None,
) -> str | None:
    """询问一段文本(如游戏名或路径); 取消或内容为空返回 None.

    ``browse`` 非 None 时显示"浏览"按钮, 点击后把返回的路径填入输入框.
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=text,
        justify="left",
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    label.pack(padx=24, pady=(22, 8))

    entry = ctk.CTkEntry(
        window,
        width=360,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
    )
    entry.insert(0, initial)
    entry.pack(padx=24, pady=(0, 10))
    entry.focus_set()

    result: list[str] = []

    def submit() -> None:
        value = entry.get().strip()
        if value:
            result.append(value)
            window.destroy()

    entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 18))
    cancel = ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    )
    cancel.pack(side="left", padx=(0, 10))

    def fill_from_browse() -> None:
        if browse is not None:
            picked = browse()
            if picked:
                entry.delete(0, "end")
                entry.insert(0, picked)

    if browse is not None:
        browse_btn = ctk.CTkButton(
            buttons,
            text=tr("dialog.browse"),
            width=96,
            height=32,
            fg_color=palette.raised,
            hover_color=palette.item_hover,
            text_color=palette.text_body,
            command=fill_from_browse,
        )
        browse_btn.pack(side="left", padx=(0, 10))

    ok = ctk.CTkButton(
        buttons,
        text=ok_text,
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    )
    ok.pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result[0] if result else None
