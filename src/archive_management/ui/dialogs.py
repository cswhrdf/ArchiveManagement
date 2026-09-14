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
    initial: str = "",
    confirm_text: str | None = None,
) -> str | None:
    """询问分支名称;取消或内容为空返回 None.

    ``initial`` 用作预填名称(创建分支时默认给一个可直接确认的名字).
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
        width=320,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
    )
    entry.insert(0, initial)
    entry.pack(padx=24, pady=(0, 10))
    entry.focus_set()
    entry.select_range(0, "end")

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


def ask_text(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    text: str,
    initial: str = "",
    browse: Callable[[], str | None] | None = None,
    confirm_text: str | None = None,
    allow_empty: bool = False,
) -> str | None:
    """询问一段文本(如游戏名或路径); 取消或内容为空返回 None.

    ``browse`` 非 None 时显示"浏览"按钮, 点击后把返回的路径填入输入框.
    ``allow_empty`` 为 True 时允许提交空内容(返回 ``""``), 供"清空配置"
    这类需要区分"取消"与"留空"的场景使用.
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
        if value or allow_empty:
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


def edit_backup_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    name_label: str,
    desc_label: str,
    desc_prompt: str,
    initial_name: str = "",
    initial_desc: str = "",
    limit: int = 200,
) -> tuple[str, str] | None:
    """在同一个窗口里编辑备份名称与描述; 取消返回 None.

    ``limit`` 为描述的字数上限: 超限时不会提交并把计数器标红, 避免静默
    截断用户输入(服务层还会再校验一次).
    """
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    section_font = ctk.CTkFont(size=12)
    ctk.CTkLabel(
        window,
        text=title,
        anchor="w",
        font=ctk.CTkFont(size=14, weight="bold"),
        text_color=palette.text_primary,
    ).pack(padx=24, pady=(20, 12), anchor="w")

    ctk.CTkLabel(
        window,
        text=name_label,
        anchor="w",
        font=section_font,
        text_color=palette.text_muted,
    ).pack(padx=24, anchor="w")
    name_entry = ctk.CTkEntry(
        window,
        width=420,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
    )
    name_entry.insert(0, initial_name)
    name_entry.pack(padx=24, pady=(4, 12))
    name_entry.focus_set()

    ctk.CTkLabel(
        window,
        text=desc_label,
        anchor="w",
        font=section_font,
        text_color=palette.text_muted,
    ).pack(padx=24, anchor="w")
    desc_box = ctk.CTkTextbox(
        window,
        width=420,
        height=110,
        wrap="word",
        fg_color=palette.input_bg,
        border_color=palette.border,
        border_width=1,
        text_color=palette.text_body,
        font=ctk.CTkFont(size=12),
    )
    desc_box.insert("1.0", initial_desc)
    desc_box.pack(padx=24, pady=(4, 4))
    counter = ctk.CTkLabel(
        window,
        text="",
        anchor="e",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    )
    counter.pack(padx=24, pady=(0, 10), anchor="e")
    ctk.CTkLabel(
        window,
        text=desc_prompt,
        anchor="w",
        justify="left",
        wraplength=420,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    ).pack(padx=24, pady=(0, 12), anchor="w")

    result: list[tuple[str, str]] = []

    def current_desc() -> str:
        return str(desc_box.get("1.0", "end")).strip()

    def refresh_counter(_event: object = None) -> None:
        length = len(current_desc())
        counter.configure(
            text=tr("dialog.counter", count=length, limit=limit),
            text_color=palette.danger if length > limit else palette.text_muted,
        )

    def submit() -> None:
        if len(current_desc()) > limit:
            refresh_counter()
            return
        result.append((name_entry.get().strip(), current_desc()))
        window.destroy()

    name_entry.bind("<Return>", lambda _event: submit())
    desc_box.bind("<KeyRelease>", refresh_counter)
    refresh_counter()

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 18), anchor="e")
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.schedule_save"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def schedule_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    interval_label: str,
    interval_prompt: str,
    keep_label: str,
    keep_prompt: str,
    initial_interval: str = "",
    initial_keep: str = "3",
) -> tuple[str, str] | None:
    """在同一个窗口里设置定期备份周期与自动备份保留份数; 取消返回 None.

    周期与保留份数由调用方解析校验: 本函数只收集原始文本, 便于沿用既有
    的错误反馈路径。
    """
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    section_font = ctk.CTkFont(size=12)
    hint_font = ctk.CTkFont(size=11)
    ctk.CTkLabel(
        window,
        text=title,
        anchor="w",
        font=ctk.CTkFont(size=14, weight="bold"),
        text_color=palette.text_primary,
    ).pack(padx=24, pady=(20, 12), anchor="w")

    def section(text: str) -> None:
        ctk.CTkLabel(
            window,
            text=text,
            anchor="w",
            font=section_font,
            text_color=palette.text_muted,
        ).pack(padx=24, anchor="w")

    def hint(text: str) -> None:
        ctk.CTkLabel(
            window,
            text=text,
            anchor="w",
            justify="left",
            wraplength=420,
            font=hint_font,
            text_color=palette.text_muted,
        ).pack(padx=24, pady=(0, 12), anchor="w")

    section(interval_label)
    interval_entry = ctk.CTkEntry(
        window,
        width=420,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
    )
    interval_entry.insert(0, initial_interval)
    interval_entry.pack(padx=24, pady=(4, 6))
    interval_entry.focus_set()
    hint(interval_prompt)

    section(keep_label)
    keep_entry = ctk.CTkEntry(
        window,
        width=120,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
    )
    keep_entry.insert(0, initial_keep)
    keep_entry.pack(padx=24, pady=(4, 6), anchor="w")
    hint(keep_prompt)

    result: list[tuple[str, str]] = []

    def submit() -> None:
        result.append((interval_entry.get().strip(), keep_entry.get().strip()))
        window.destroy()

    interval_entry.bind("<Return>", lambda _event: submit())
    keep_entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 18), anchor="e")
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.schedule_save"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def restore_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    summary: str,
    safety_label: str,
    safety_hint: str,
    safety_available: bool,
    safety_default: bool = True,
    danger_note: str = "",
) -> bool | None:
    """在同一个窗口里确认恢复并选择是否先创建安全点; 取消返回 None.

    返回 ``True`` 表示恢复前先创建安全点(默认), ``False`` 表示直接恢复。
    当前没有可备份内容时选项被禁用且强制为 ``False``; 风险提示(例如检测到
    游戏进程在运行)与选项在同一窗口展示, 不再额外弹窗。
    """
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=summary,
        anchor="w",
        justify="left",
        wraplength=420,
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    ).pack(padx=24, pady=(20, 8), anchor="w")

    if danger_note:
        ctk.CTkLabel(
            window,
            text=danger_note,
            anchor="w",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=12),
            text_color=palette.danger,
        ).pack(padx=24, pady=(0, 8), anchor="w")

    safety_var = ctk.BooleanVar(value=safety_default and safety_available)

    def option(
        label: str,
        hint_text: str,
        variable: ctk.BooleanVar,
        *,
        available: bool,
    ) -> None:
        box = ctk.CTkCheckBox(
            window,
            text=label,
            variable=variable,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
            fg_color=palette.accent,
            hover_color=palette.accent_soft_border,
            checkmark_color=palette.accent_text,
        )
        if not available:
            box.configure(state="disabled")
        box.pack(padx=24, pady=(0, 2), anchor="w")
        ctk.CTkLabel(
            window,
            text=hint_text,
            anchor="w",
            justify="left",
            wraplength=400,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(padx=(46, 24), pady=(0, 10), anchor="w")

    option(safety_label, safety_hint, safety_var, available=safety_available)

    result: list[bool] = []

    def submit() -> None:
        result.append(bool(safety_var.get()) and safety_available)
        window.destroy()

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 18), anchor="e")
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.restore_confirm"),
        width=112,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result[0] if result else None
