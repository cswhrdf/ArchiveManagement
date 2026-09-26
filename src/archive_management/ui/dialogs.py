"""模态对话框与确认.

统一高风险操作确认与信息展示方式,全部在主线程调用(后台回调经消息
队列回到主线程后再触发),避免工作线程直接操作 Tk 控件。所有文案经
i18n 配置加载,弹窗一律居中显示在主窗口上。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

import customtkinter as ctk

from archive_management.application.imports import STRATEGY_MERGE
from archive_management.domain import (
    MAX_TAG_LENGTH,
    MAX_TAGS,
    normalize_tags,
)
from archive_management.i18n import tr
from archive_management.ui.models import (
    BatchExportChoice,
    BatchExportOption,
    BatchExportPrompt,
    BatchImportPrompt,
    BatchImportRow,
    BatchImportSelection,
    ImportChoice,
    ImportLocationRow,
    ImportPrompt,
    ImportTargetOption,
    batch_export_choice,
    batch_row_choice,
    filter_export_options,
    target_game_id,
    target_label_for,
)
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


def import_game_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    name_label: str,
    initial_name: str,
    paths_label: str,
    paths_hint: str,
    initial_paths: Sequence[str] = (),
    add_text: str = "",
    confirm_text: str | None = None,
    browse: Callable[[], str | None] | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """询问游戏名与要写进库的存档路径; 取消或名称为空时返回 ``None``.

    路径区预填探测到的候选: 每条都能改, 取消勾选就不写库(用户可以只留其中几条),
    也能自己再加一条。``browse`` 非 None 时每条路径后面多一个"浏览…"按钮(与游戏
    详情里添加/修改存档位置同一个目录选择框), 点它就能可视化挑目录。返回
    ``(名称, 勾选且非空的路径)``。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    name_text = ctk.CTkLabel(
        window,
        text=name_label,
        justify="left",
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    )
    name_text.pack(padx=24, pady=(22, 4), anchor="w")
    name_entry = ctk.CTkEntry(
        window,
        width=440,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
    )
    name_entry.insert(0, initial_name)
    name_entry.pack(padx=24, pady=(0, 10))
    name_entry.focus_set()
    name_entry.select_range(0, "end")

    paths_text = ctk.CTkLabel(
        window,
        text=paths_label,
        justify="left",
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    )
    paths_text.pack(padx=24, pady=(4, 2), anchor="w")
    hint = ctk.CTkLabel(
        window,
        text=paths_hint,
        justify="left",
        wraplength=440,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    )
    hint.pack(padx=24, anchor="w")

    rows = ctk.CTkScrollableFrame(
        window, width=440, height=150, fg_color=palette.well, corner_radius=8
    )
    rows.pack(padx=24, pady=(6, 4), fill="x")
    entries: list[tuple[ctk.CTkCheckBox, ctk.CTkEntry]] = []

    def fill_from_browse(field: ctk.CTkEntry) -> None:
        """把系统目录选择框里挑到的路径填进这一行(用户取消时保持原样)."""
        if browse is None:  # pragma: no cover - 按钮只在 browse 非空时创建
            return
        picked = browse()
        if picked:
            field.delete(0, "end")
            field.insert(0, picked)

    def add_row(value: str) -> None:
        row = ctk.CTkFrame(rows, fg_color="transparent")
        row.pack(fill="x", pady=2)
        box = ctk.CTkCheckBox(
            row,
            text="",
            variable=ctk.BooleanVar(value=True),
            width=20,
            checkbox_width=18,
            checkbox_height=18,
            fg_color=palette.accent,
            hover_color=palette.accent_soft_border,
            border_color=palette.border,
        )
        box.pack(side="left", padx=(4, 6))
        entry = ctk.CTkEntry(
            row,
            fg_color=palette.input_bg,
            border_color=palette.border,
            text_color=palette.text_body,
        )
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        if browse is not None:
            browse_btn = ctk.CTkButton(
                row,
                text=tr("dialog.browse"),
                width=64,
                height=28,
                fg_color=palette.raised,
                hover_color=palette.item_hover,
                text_color=palette.text_body,
                command=lambda field=entry: fill_from_browse(field),
            )
            browse_btn.pack(side="left", padx=(0, 4))
        entries.append((box, entry))

    for path in initial_paths:
        add_row(path)
    if not initial_paths:
        add_row("")

    result: list[tuple[str, tuple[str, ...]]] = []

    def submit() -> None:
        name = name_entry.get().strip()
        if not name:
            return
        chosen = tuple(
            entry.get().strip()
            for box, entry in entries
            if box.get() and entry.get().strip()
        )
        result.append((name, chosen))
        window.destroy()

    name_entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(6, 18))
    add = ctk.CTkButton(
        buttons,
        text=add_text,
        width=116,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=lambda: add_row(""),
    )
    add.pack(side="left", padx=(0, 10))
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


def edit_tags_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    tags: Sequence[str] = (),
    max_tags: int = MAX_TAGS,
    max_length: int = MAX_TAG_LENGTH,
) -> tuple[str, ...] | None:
    """编辑一款游戏的自定义标签: 一行一个, 与导入时调整存档路径同一个样式.

    每行一个输入框加一个"删除", 底部是"添加标签 / 取消 / 保存"; 行数达到上限后
    "添加标签"不可用(与 :func:`~archive_management.domain.home.normalize_tags` 的
    上限一致)。回车等同于保存; 空行与重复项直接丢掉, 中英文逗号都会被剔除。返回
    清理后的标签, 取消时返回 ``None``。
    """
    window = ctk.CTkToplevel(parent)
    window.title(tr("dialog.home_tags_title"))
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=tr("dialog.tags_label", max=max_tags, length=max_length),
        justify="left",
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    )
    label.pack(padx=24, pady=(22, 2), anchor="w")
    hint = ctk.CTkLabel(
        window,
        text=tr("dialog.tags_hint"),
        justify="left",
        wraplength=440,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    )
    hint.pack(padx=24, anchor="w")

    # 按钮先建好(回调里要按行数切"添加标签"的可用状态), 打包留到最后 —— Tk 的布局
    # 按 pack 的顺序, 与创建顺序无关。
    buttons = ctk.CTkFrame(window, fg_color="transparent")
    add = ctk.CTkButton(
        buttons,
        text=tr("dialog.tags_add"),
        width=116,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=lambda: add_row(""),
    )
    add.pack(side="left", padx=(0, 10))
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
        text=tr("dialog.tags_save"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=lambda: submit(),
    )
    ok.pack(side="left")

    rows = ctk.CTkScrollableFrame(
        window, width=440, height=150, fg_color=palette.well, corner_radius=8
    )
    entries: list[ctk.CTkEntry] = []
    result: list[tuple[str, ...]] = []

    def refresh_add_state() -> None:
        """行数到上限后不允许再加(上限与 normalize_tags 一致)."""
        add.configure(state="normal" if len(entries) < max_tags else "disabled")

    def remove_row(row: ctk.CTkFrame, entry: ctk.CTkEntry) -> None:
        """删掉一行; 删空了补一个空行, 窗户里总有一个输入框可用."""
        if entry in entries:
            entries.remove(entry)
        row.destroy()
        if not entries:
            add_row("")
        refresh_add_state()

    def add_row(value: str) -> None:
        """追加一行(输入框 + "删除"); 到达上限就什么也不做.

        "添加标签"按钮那时已经是禁用状态, 这里再加一道兜底: 行的数量永远不会
        超过 ``normalize_tags`` 能保留下来的上限。
        """
        if len(entries) >= max_tags:
            return
        row = ctk.CTkFrame(rows, fg_color="transparent")
        row.pack(fill="x", pady=2)
        entry = ctk.CTkEntry(
            row,
            fg_color=palette.input_bg,
            border_color=palette.border,
            text_color=palette.text_body,
        )
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        entry.bind("<Return>", lambda _event: submit())
        remove = ctk.CTkButton(
            row,
            text=tr("dialog.tags_remove"),
            width=64,
            height=28,
            fg_color=palette.raised,
            hover_color=palette.item_hover,
            text_color=palette.text_body,
            command=lambda: remove_row(row, entry),
        )
        remove.pack(side="left")
        entries.append(entry)
        refresh_add_state()

    def submit() -> None:
        """收集非空行, 清理(去空白/去重/截断)后交给调用方."""
        result.append(normalize_tags([entry.get() for entry in entries]))
        window.destroy()

    for tag in tags[:max_tags]:
        add_row(tag)
    if len(entries) < max_tags:
        add_row("")

    rows.pack(padx=24, pady=(6, 4), fill="x")
    buttons.pack(padx=24, pady=(6, 18))

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


def import_package_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    prompt: ImportPrompt,
    locations_label: str,
    locations_hint: str,
    strategy_label: str,
    strategies: Sequence[tuple[str, str]],
    target_label: str,
    target_hint: str,
    confirm_text: str | None = None,
) -> ImportChoice | None:
    """展示包内容与冲突项, 让用户选导入方式并映射存档位置; 取消返回 ``None``.

    ``strategies`` 是 ``(策略键, 文案)`` 列表(调用方按"有没有可合并的游戏"决定
    是否提供"合并"); ``prompt`` 里的每个存档位置一行输入框, **留空表示这一条不
    导入**, 只有本就存在于本机的包内路径才会被预填。目标游戏只在"合并"时返回。

    与其它对话框一致: 只在主线程调用, 窗口居中于主窗口, 用户点"取消"或直接关窗
    都返回 ``None``(调用方据此什么都不做)。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=prompt.summary,
        anchor="w",
        justify="left",
        wraplength=460,
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    ).pack(padx=24, pady=(20, 6), anchor="w")

    if prompt.match_text:
        ctk.CTkLabel(
            window,
            text=prompt.match_text,
            anchor="w",
            justify="left",
            wraplength=460,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        ).pack(padx=24, pady=(0, 6), anchor="w")

    _dialog_section(window, palette, locations_label)
    _dialog_hint(window, palette, locations_hint)
    rows = ctk.CTkScrollableFrame(
        window, width=460, height=120, fg_color=palette.well, corner_radius=8
    )
    rows.pack(padx=24, pady=(6, 10), fill="x")
    entries: list[tuple[ImportLocationRow, ctk.CTkEntry]] = []
    for row in prompt.locations:
        frame = ctk.CTkFrame(rows, fg_color="transparent")
        frame.pack(fill="x", pady=2)
        ctk.CTkLabel(
            frame,
            text=row.text,
            anchor="w",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(fill="x")
        entry = ctk.CTkEntry(
            frame,
            fg_color=palette.input_bg,
            border_color=palette.border,
            text_color=palette.text_body,
        )
        entry.insert(0, row.default)
        entry.pack(fill="x", pady=(0, 2))
        entries.append((row, entry))

    _dialog_section(window, palette, strategy_label)
    strategy_var = ctk.StringVar(value=strategies[0][0])
    for key, text in strategies:
        ctk.CTkRadioButton(
            window,
            text=text,
            value=key,
            variable=strategy_var,
            command=lambda: _paint_targets(),
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
            fg_color=palette.accent,
            hover_color=palette.accent_soft_border,
            border_color=palette.border,
        ).pack(padx=24, pady=(2, 0), anchor="w")

    target_var = ctk.StringVar(value=_default_target(prompt.targets))
    # 只登记"能接受 state 参数"的控件: 滚动容器的 configure 不吃 state(会报未知选项).
    target_parts: list[ctk.CTkBaseClass] = []
    if prompt.targets:
        target_parts.append(_dialog_section(window, palette, target_label))
        target_parts.append(_dialog_hint(window, palette, target_hint))
        target_rows = ctk.CTkScrollableFrame(
            window, width=460, height=110, fg_color=palette.well, corner_radius=8
        )
        target_rows.pack(padx=24, pady=(6, 10), fill="x")
        for option in prompt.targets:
            radio = ctk.CTkRadioButton(
                target_rows,
                text=option.label,
                value=option.game_id,
                variable=target_var,
                font=ctk.CTkFont(size=12),
                text_color=palette.text_body,
                fg_color=palette.accent,
                hover_color=palette.accent_soft_border,
                border_color=palette.border,
            )
            radio.pack(padx=4, pady=2, anchor="w")
            target_parts.append(radio)

    def _paint_targets() -> None:
        """只有"合并"才需要目标游戏: 其余方式把目标区置灰(避免误以为会合并)."""
        state = "normal" if strategy_var.get() == STRATEGY_MERGE else "disabled"
        for part in target_parts:
            part.configure(state=state)

    _paint_targets()

    result: list[ImportChoice] = []

    def submit() -> None:
        strategy = strategy_var.get()
        chosen = {row.index: entry.get().strip() for row, entry in entries}
        result.append(
            ImportChoice(
                strategy=strategy,
                target_game_id=_chosen_target(strategy, target_var.get()),
                locations={index: path for index, path in chosen.items() if path},
            )
        )
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
        text=ok_text,
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


@dataclass(frozen=True)
class _BatchTicks:
    """批量导出对话框的勾选状态: 全选框 + 逐行变量(两者共用同一份变量表)."""

    master: ctk.CTkCheckBox
    variables: dict[str, ctk.BooleanVar]

    def selected(self) -> dict[str, bool]:
        """逐行取值(提交时用)."""
        return {game_id: bool(var.get()) for game_id, var in self.variables.items()}


def _build_batch_game_list(
    window: ctk.CTk,
    palette: Palette,
    *,
    prompt: BatchExportPrompt,
    list_label: str,
    select_all_label: str,
    no_match_text: str,
    filter_box: ctk.CTkEntry,
) -> tuple[_BatchTicks, Callable[[object], None]]:
    """建"全选框 + 可滚动勾选列表", 返回勾选状态与"按筛选刷新显示"的回调.

    筛选只影响显示: 勾选状态存在按游戏 id 索引的变量里, 被筛掉的行不会丢掉用户做过的
    选择。全选框作用于**当前筛选后的行**(筛掉的行保持用户原来的勾选), 自己的状态则始终
    反映"可见行是否已全部勾上", 没有可见行时置灰。
    """
    _dialog_section(window, palette, list_label)
    master_var = ctk.BooleanVar(value=all(option.selected for option in prompt.options))
    master = ctk.CTkCheckBox(
        window,
        text=select_all_label,
        variable=master_var,
        font=ctk.CTkFont(size=12),
        text_color=palette.text_body,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        border_color=palette.border,
    )
    master.pack(padx=24, pady=(6, 0), anchor="w")
    rows = ctk.CTkScrollableFrame(
        window, width=460, height=220, fg_color=palette.well, corner_radius=8
    )
    rows.pack(padx=24, pady=(6, 10), fill="x")
    no_match = ctk.CTkLabel(
        rows,
        text=no_match_text,
        anchor="w",
        font=ctk.CTkFont(size=12),
        text_color=palette.text_muted,
    )
    variables: dict[str, ctk.BooleanVar] = {}
    lines: list[tuple[BatchExportOption, ctk.CTkFrame]] = []

    def visible_ids() -> set[str]:
        """当前筛选后仍然显示的游戏 id(筛选只影响显示)."""
        return {
            option.game_id
            for option in filter_export_options(prompt.options, str(filter_box.get()))
        }

    def refresh_master() -> None:
        """刷新全选框: 没有可见行时置灰, 否则反映"可见行是否已全部勾上"."""
        visible = visible_ids()
        master.configure(state="normal" if visible else "disabled")
        master_var.set(bool(visible) and all(variables[gid].get() for gid in visible))

    def toggle_all() -> None:
        """点全选: 只改**当前筛选后的**行, 筛掉的行保持用户原来的勾选."""
        wanted = bool(master_var.get())
        for game_id in visible_ids():
            variables[game_id].set(wanted)
        refresh_master()

    master.configure(command=toggle_all)

    for option in prompt.options:
        row = ctk.CTkFrame(rows, fg_color="transparent")
        row.pack(fill="x", pady=2)
        variables[option.game_id] = ctk.BooleanVar(value=option.selected)
        ctk.CTkCheckBox(
            row,
            text=option.name,
            variable=variables[option.game_id],
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
            fg_color=palette.accent,
            hover_color=palette.accent_soft_border,
            border_color=palette.border,
            command=refresh_master,
        ).pack(side="left", padx=(6, 8), pady=2)
        ctk.CTkLabel(
            row,
            text=option.detail,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(side="left")
        lines.append((option, row))

    def apply_filter(_event: object = None) -> None:
        """按输入内容显示/隐藏行; 一条都不匹配时给出提示(勾选状态不受影响)."""
        visible = visible_ids()
        for option, row in lines:
            if option.game_id in visible:
                row.pack(fill="x", pady=2)
            else:
                row.pack_forget()
        if visible:
            no_match.pack_forget()
        else:
            no_match.pack(padx=6, pady=8, anchor="w")
        refresh_master()

    refresh_master()
    return _BatchTicks(master=master, variables=variables), apply_filter


def export_batch_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    prompt: BatchExportPrompt,
    filter_label: str,
    list_label: str,
    no_match_text: str,
    select_all_label: str,
    confirm_text: str | None = None,
) -> BatchExportChoice | None:
    """让用户勾选要批量导出的游戏; 取消返回 ``None``.

    列表上方是一个**可输入的筛选框**(与定时任务窗口的选择框同一套做法: 输入即过滤),
    但过滤只影响显示 —— 勾选状态存在按游戏 id 索引的变量里, 随后被筛掉的行不会丢掉
    用户已经做过的选择(勾选是用户明确表达过的意思, 不该被一次输入悄悄丢掉)。

    筛选框下面还有一个**全选框**: 它作用于**当前筛选后的行**(筛掉的行保持用户原来的
    勾选 —— 与上面同一条语义), 自己的状态则始终反映"可见行是否已全部勾上", 没有
    可见行时置灰。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=prompt.summary,
        anchor="w",
        justify="left",
        wraplength=460,
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    ).pack(padx=24, pady=(20, 6), anchor="w")

    _dialog_section(window, palette, filter_label)
    _dialog_hint(window, palette, prompt.filter_hint)
    # 普通输入框(不是下拉选框): 这里用户要做的就是打字筛选, 下拉列表只会挡住下面的
    # 勾选列表; 输入即筛选的接线与下面 apply_filter 一致。
    filter_box = ctk.CTkEntry(
        window,
        width=460,
        placeholder_text=filter_label,
        fg_color=palette.input_bg,
        border_color=palette.border,
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    filter_box.pack(padx=24, pady=(6, 10))
    ticks, apply_filter = _build_batch_game_list(
        window,
        palette,
        prompt=prompt,
        list_label=list_label,
        select_all_label=select_all_label,
        no_match_text=no_match_text,
        filter_box=filter_box,
    )
    filter_box.bind("<KeyRelease>", apply_filter)

    result: list[BatchExportChoice] = []

    def submit() -> None:
        result.append(batch_export_choice(ticks.selected(), prompt.options))
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
        text=ok_text,
        width=104,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


@dataclass(frozen=True)
class _BatchRowWidgets:
    """批量导入对话框里一行的控件(提交时逐行取值)."""

    row: BatchImportRow
    strategy: ctk.StringVar
    target: ctk.CTkComboBox
    entries: tuple[tuple[ImportLocationRow, ctk.CTkEntry], ...]
    target_parts: tuple[ctk.CTkBaseClass, ...]


def _paint_batch_row(parts: _BatchRowWidgets) -> None:
    """只有"合并"才启用目标选择(与单包对话框同一套语义)."""
    state = "normal" if parts.strategy.get() == STRATEGY_MERGE else "disabled"
    for part in parts.target_parts:
        part.configure(state=state)


def batch_import_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    prompt: BatchImportPrompt,
    locations_label: str,
    locations_hint: str,
    strategy_label: str,
    target_label: str,
    confirm_text: str | None = None,
) -> BatchImportSelection | None:
    """逐款确认批量导入的方式; 取消返回 ``None``.

    一款游戏一张小卡片: 名称与标识、"疑似同一款"的提示、存档位置(留空 = 不导入)、
    策略单选按钮与目标游戏下拉框。语义与单包 :func:`import_package_dialog` 完全一致;
    这里的目标选择用下拉框而不是单选按钮, 因为一批里有好几款游戏, 每款铺一组单选按钮
    排不下 —— 下拉框的取值是**去重后的文案**(重名时带游戏 id, 见
    :func:`~archive_management.ui.models.unique_targets`)。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=prompt.summary,
        anchor="w",
        justify="left",
        wraplength=460,
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    ).pack(padx=24, pady=(20, 0), anchor="w")
    _dialog_hint(window, palette, prompt.hint)

    cards = ctk.CTkScrollableFrame(
        window, width=460, height=320, fg_color=palette.well, corner_radius=8
    )
    cards.pack(padx=24, pady=(8, 10), fill="x")

    def build_row(row: BatchImportRow) -> _BatchRowWidgets:
        """一款游戏的一张卡片(控件在这里建, 显示顺序由 pack 决定)."""
        card = ctk.CTkFrame(cards, fg_color=palette.card, corner_radius=8)
        card.pack(fill="x", pady=4)
        ctk.CTkLabel(
            card,
            text=row.name,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        ).pack(padx=12, pady=(10, 0), anchor="w")
        ctk.CTkLabel(
            card,
            text=row.meta,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(padx=12, anchor="w")
        if row.match_text:
            ctk.CTkLabel(
                card,
                text=row.match_text,
                anchor="w",
                font=ctk.CTkFont(size=11),
                text_color=palette.text_hint,
            ).pack(padx=12, anchor="w")
        entries = _batch_location_entries(
            card, palette, row, locations_label, locations_hint
        )
        strategy = ctk.StringVar(value=row.strategy)
        target = ctk.CTkComboBox(
            card,
            values=[option.label for option in row.targets],
            fg_color=palette.input_bg,
            button_color=palette.raised,
            button_hover_color=palette.raised,
            border_color=palette.border,
            text_color=palette.text_body,
            dropdown_fg_color=palette.panel,
            dropdown_text_color=palette.text_body,
            font=ctk.CTkFont(size=12),
            dropdown_font=ctk.CTkFont(size=12),
        )
        heading = ctk.CTkLabel(
            card,
            text=target_label,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        parts = _BatchRowWidgets(
            row=row,
            strategy=strategy,
            target=target,
            entries=entries,
            target_parts=(heading, target),
        )

        def paint() -> None:
            _paint_batch_row(parts)

        radio_row = ctk.CTkFrame(card, fg_color="transparent")
        radio_row.pack(padx=12, pady=(8, 0), anchor="w")
        ctk.CTkLabel(
            radio_row,
            text=strategy_label,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(side="left", padx=(0, 8))
        for key, text in row.strategies:
            ctk.CTkRadioButton(
                radio_row,
                text=text,
                value=key,
                variable=strategy,
                command=paint,
                font=ctk.CTkFont(size=12),
                text_color=palette.text_body,
                fg_color=palette.accent,
                hover_color=palette.accent_soft_border,
                border_color=palette.border,
            ).pack(side="left", padx=(0, 8))
        if row.targets:
            heading.pack(padx=12, pady=(6, 0), anchor="w")
            target.pack(padx=12, pady=(0, 10), fill="x")
            target.set(target_label_for(row.targets, row.target_game_id))
        paint()
        return parts

    built = [build_row(row) for row in prompt.rows]

    result: list[BatchImportSelection] = []

    def submit() -> None:
        result.append(
            BatchImportSelection(
                choices={
                    parts.row.entry: batch_row_choice(
                        strategy=str(parts.strategy.get()),
                        target=target_game_id(
                            parts.row.targets, str(parts.target.get())
                        ),
                        locations={
                            item.index: str(entry.get())
                            for item, entry in parts.entries
                        },
                    )
                    for parts in built
                }
            )
        )
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
        text=ok_text,
        width=104,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def _batch_location_entries(
    card: ctk.CTkFrame,
    palette: Palette,
    row: BatchImportRow,
    label: str,
    hint: str,
) -> tuple[tuple[ImportLocationRow, ctk.CTkEntry], ...]:
    """一行游戏卡片里的存档位置区(没有位置时整块不显示)."""
    if not row.locations:
        return ()
    ctk.CTkLabel(
        card,
        text=label,
        anchor="w",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    ).pack(padx=12, pady=(8, 0), anchor="w")
    ctk.CTkLabel(
        card,
        text=hint,
        anchor="w",
        justify="left",
        wraplength=420,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_hint,
    ).pack(padx=12, anchor="w")
    entries: list[tuple[ImportLocationRow, ctk.CTkEntry]] = []
    for item in row.locations:
        ctk.CTkLabel(
            card,
            text=item.text,
            anchor="w",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(padx=12, pady=(4, 0), anchor="w")
        entry = ctk.CTkEntry(
            card,
            fg_color=palette.input_bg,
            border_color=palette.border,
            text_color=palette.text_body,
        )
        entry.insert(0, item.default)
        entry.pack(padx=12, pady=(0, 2), fill="x")
        entries.append((item, entry))
    return tuple(entries)


def _dialog_section(
    window: ctk.CTkToplevel, palette: Palette, text: str
) -> ctk.CTkBaseClass:
    """对话框里的小节标题(返回控件, 便于按策略置灰整块)."""
    label = ctk.CTkLabel(
        window,
        text=text,
        anchor="w",
        font=ctk.CTkFont(size=12),
        text_color=palette.text_muted,
    )
    label.pack(padx=24, pady=(4, 0), anchor="w")
    return label


def _dialog_hint(
    window: ctk.CTkToplevel, palette: Palette, text: str
) -> ctk.CTkBaseClass:
    """对话框里的补充说明(成段文字, 用更好读的 text_hint)."""
    label = ctk.CTkLabel(
        window,
        text=text,
        anchor="w",
        justify="left",
        wraplength=460,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_hint,
    )
    label.pack(padx=24, pady=(0, 2), anchor="w")
    return label


def _default_target(targets: Sequence[ImportTargetOption]) -> str:
    """目标游戏的默认选中项: "疑似同一款"(没有就是列表里的第一款)."""
    for option in targets:
        if option.selected:
            return option.game_id
    return targets[0].game_id if targets else ""


def _chosen_target(strategy: str, selected: str) -> str | None:
    """只有"合并"方式才返回目标游戏; 其余方式一律不带.

    目标区在"合并"时一定有预选项(没有可选游戏时界面根本不提供合并), 所以这里
    不做"没选中"的兜底判断: 界面不替用户猜要合并到哪一款。
    """
    return selected if strategy == STRATEGY_MERGE else None
