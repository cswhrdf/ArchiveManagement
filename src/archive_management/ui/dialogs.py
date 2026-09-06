"""模态对话框与确认.

统一高风险操作确认与信息展示方式,全部在主线程调用(后台回调经消息
队列回到主线程后再触发),避免工作线程直接操作 Tk 控件。
"""

from __future__ import annotations

import customtkinter as ctk

from archive_management.ui.palette import Palette


def confirm_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    message: str,
    confirm_text: str = "确认",
    cancel_text: str = "取消",
) -> bool:
    """显示确认对话框,返回用户选择."""
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
        text=cancel_text,
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
        text=confirm_text,
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=lambda: choose(True),
    )
    ok.pack(side="left")

    window.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_width()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_height()) // 2
    window.geometry(f"+{x}+{y}")
    parent.wait_window(window)
    return bool(result and result[0])


def info_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    message: str,
) -> None:
    """显示一次性信息对话框."""
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
        text="知道了",
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=window.destroy,
    )
    ok.pack(pady=(0, 20))


def ask_branch_name(parent: ctk.CTk, *, title: str, text: str) -> str | None:
    """询问分支名称;用户取消返回 None."""
    dialog = ctk.CTkInputDialog(text=text, title=title)
    value = dialog.get_input()
    if value is None:
        return None
    name = str(value).strip()
    return name or None
