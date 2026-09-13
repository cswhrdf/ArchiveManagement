"""全局定时任务窗口.

工作区的"定时任务"入口打开这个窗口: 只列出**已经配置定时备份**的游戏, 每条
记录都会标明任务来自哪个游戏、下次运行时间与该游戏当前的自动备份份数; 在这
里可以新增、编辑、启用/暂停与删除(取消周期)任一游戏的定时任务。

新增时先弹出"选择游戏"窗口(选择框可直接输入关键字筛选), 弹窗里也写明了
推荐入口; 每个游戏的周期与自动备份保留份数都是独立配置的(存放在
``scheduled_jobs``), 更常用的做法是在"游戏设置 → 定时备份"里为单个游戏维护
——两处入口共用 :func:`edit_schedule`, 行为完全一致。本模块采用组合式窗口,
便于无头测试。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

import customtkinter as ctk

from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.services.audit import log_action
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import (
    _center,
    confirm_dialog,
    info_dialog,
    schedule_dialog,
)
from archive_management.ui.models import MAX_KEEP_AUTO, ScheduleItem
from archive_management.ui.palette import Palette

_ChangeCallback = Callable[[], None]


def _has_locations(backend: ArchiveService, game_id: str) -> bool:
    """判断游戏是否已配置至少一个存档位置(没有就不能定时备份)."""
    try:
        return bool(backend.list_locations(game_id))
    except ArchiveManagementError:  # 未知游戏等异常按"不可配置"处理
        return False


def edit_schedule(
    parent: ctk.CTk,
    palette: Palette,
    backend: ArchiveService,
    *,
    game_id: str,
    game_name: str,
) -> bool:
    """为某个游戏编辑定时备份配置; 修改成功返回 True(取消/前置不满足返回 False).

    周期留空表示取消该游戏的定时备份。周期与保留份数的校验同样在这里完成,
    便于两个入口(全局任务窗口与游戏设置)复用同一套规则; 没有配置存档位置
    的游戏会先被拦下并告知原因。
    """
    if not _has_locations(backend, game_id):
        info_dialog(
            parent,
            palette,
            title=tr("dialog.error_title"),
            message=tr("error.no_locations_schedule", name=game_name),
        )
        return False
    task = backend.task_status(game_id)
    edited = schedule_dialog(
        parent,
        palette,
        title=tr("dialog.schedule_title", name=game_name),
        interval_label=tr("dialog.schedule_interval_label"),
        interval_prompt=tr(
            "dialog.schedule_prompt",
            current=task.schedule_text or tr("task.unscheduled"),
        ),
        keep_label=tr("dialog.keep_auto_label"),
        keep_prompt=tr("dialog.keep_auto_prompt", max=MAX_KEEP_AUTO),
        initial_interval=task.schedule_text,
        initial_keep=str(task.keep_auto),
    )
    if edited is None:
        return False
    text, keep_text = edited
    keep_auto = task.keep_auto
    if keep_text:
        try:
            keep_auto = int(keep_text)
        except ValueError:
            info_dialog(
                parent,
                palette,
                title=tr("dialog.error_title"),
                message=tr("error.keep_auto_invalid"),
            )
            return False
        if not 1 <= keep_auto <= MAX_KEEP_AUTO:
            info_dialog(
                parent,
                palette,
                title=tr("dialog.error_title"),
                message=tr("error.keep_auto_range", max=MAX_KEEP_AUTO),
            )
            return False
    try:
        backend.set_schedule(game_id, text, keep_auto=keep_auto)
    except ArchiveManagementError as exc:
        info_dialog(
            parent,
            palette,
            title=tr("dialog.error_title"),
            message=str(exc),
        )
        return False
    log_action(
        "schedule.edit",
        game_id=game_id,
        name=game_name,
        interval=text or None,
        keep_auto=keep_auto,
    )
    return True


def add_schedule_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    candidates: Sequence[ScheduleItem],
    blocked: Sequence[ScheduleItem] = (),
) -> str | None:
    """选择要新增定时任务的游戏, 返回游戏 id; 取消或未选返回 None.

    选择框可以直接输入内容来筛选游戏(输入即过滤下拉候选)。弹窗里写明"主要
    用于统一管理, 为单个游戏配置更推荐去游戏设置", 并列出因"未配置存档位置"
    而无法创建定时任务的游戏及其原因。
    """
    if not candidates:
        return None
    names = [item.game_name for item in candidates]
    window = ctk.CTkToplevel(parent)
    window.title(tr("dialog.schedule_add_title"))
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=tr("dialog.schedule_add_title"),
        anchor="w",
        font=ctk.CTkFont(size=14, weight="bold"),
        text_color=palette.text_primary,
    ).pack(padx=24, pady=(20, 8), anchor="w")
    ctk.CTkLabel(
        window,
        text=tr("dialog.schedule_add_prompt"),
        anchor="w",
        font=ctk.CTkFont(size=12),
        text_color=palette.text_muted,
    ).pack(padx=24, anchor="w")
    ctk.CTkLabel(
        window,
        text=tr("dialog.schedule_add_note"),
        anchor="w",
        justify="left",
        wraplength=420,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    ).pack(padx=24, pady=(8, 10), anchor="w")
    if blocked:
        ctk.CTkLabel(
            window,
            text=tr(
                "dialog.schedule_add_blocked",
                names=", ".join(item.game_name for item in blocked),
            ),
            anchor="w",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=11),
            text_color=palette.danger,
        ).pack(padx=24, pady=(0, 10), anchor="w")

    picker = ctk.CTkComboBox(
        window,
        values=names,
        width=420,
        fg_color=palette.input_bg,
        button_color=palette.raised,
        button_hover_color=palette.raised,
        border_color=palette.border,
        text_color=palette.text_body,
        dropdown_fg_color=palette.panel,
        dropdown_text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
        dropdown_font=ctk.CTkFont(size=13),
    )
    picker.set(names[0])

    def filtered() -> list[str]:
        typed = picker.get().strip().casefold()
        if not typed:
            return list(names)
        return [name for name in names if typed in name.casefold()] or list(names)

    def on_key(_event: object) -> None:
        """输入即筛选: 保留下拉候选与用户已输入的内容."""
        typed = picker.get()
        picker.configure(values=filtered())
        picker.set(typed)

    picker.bind("<KeyRelease>", on_key)
    picker.pack(padx=24, pady=(0, 16))

    result: dict[str, str | None] = {"value": None}

    def submit() -> None:
        typed = picker.get().strip()
        match = next((item for item in candidates if item.game_name == typed), None)
        if match is None:
            match = next(
                (
                    item
                    for item in candidates
                    if typed.casefold() in item.game_name.casefold()
                ),
                None,
            )
        if match is None:
            return
        result["value"] = match.game_id
        window.destroy()

    actions = ctk.CTkFrame(window, fg_color="transparent")
    actions.pack(padx=24, pady=(0, 20), anchor="e")
    ctk.CTkButton(
        actions,
        text=tr("dialog.cancel"),
        command=window.destroy,
        width=88,
        height=30,
        corner_radius=8,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        font=ctk.CTkFont(size=12),
    ).pack(side="left", padx=(0, 8))
    ctk.CTkButton(
        actions,
        text=tr("dialog.schedule_add_confirm"),
        command=submit,
        width=96,
        height=30,
        corner_radius=8,
        fg_color=palette.accent,
        hover_color=palette.accent,
        text_color=palette.accent_text,
        font=ctk.CTkFont(size=12),
    ).pack(side="left")

    _center(parent, window)
    parent.wait_window(window)
    return result["value"]


class ScheduleWindow:
    """展示并管理所有游戏的定时备份配置(不直接子类化 CTkToplevel)."""

    def __init__(
        self,
        parent: ctk.CTk,
        *,
        backend: ArchiveService,
        palette: Palette,
        on_change: _ChangeCallback | None = None,
    ) -> None:
        """构造窗口并装载全部游戏的定时任务."""
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._on_change = on_change
        self._items: list[ScheduleItem] = []
        self._selected: str | None = None
        self._rows: dict[str, list[ctk.CTkBaseClass]] = {}
        self._build()
        self.reload()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        palette = self._palette
        window = ctk.CTkToplevel(self._parent)
        self._window = window
        window.title(tr("schedule.title"))
        window.geometry("720x560")
        window.resizable(False, False)
        window.transient(self._parent)
        window.configure(fg_color=palette.background)
        _center(self._parent, window)

        container = ctk.CTkFrame(window, fg_color=palette.background)
        container.pack(fill="both", expand=True, padx=18, pady=16)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(container, fg_color=palette.panel, corner_radius=10)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        ctk.CTkLabel(
            header,
            text=tr("schedule.title"),
            anchor="w",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=palette.text_primary,
        ).grid(row=0, column=0, padx=16, pady=(14, 0), sticky="w")
        self._summary_label = ctk.CTkLabel(
            header,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._summary_label.grid(row=1, column=0, padx=16, pady=(2, 14), sticky="w")
        ctk.CTkLabel(
            header,
            text=tr("schedule.subtitle"),
            anchor="e",
            justify="right",
            wraplength=300,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).grid(row=0, column=1, rowspan=2, padx=16, sticky="e")

        toolbar = ctk.CTkFrame(container, fg_color="transparent")
        toolbar.grid(row=1, column=0, sticky="ew", pady=(12, 8))
        self._add_btn = self._make_button(
            toolbar, tr("schedule.add"), self._on_add, palette, width=104
        )
        self._add_btn.pack(side="left")
        self._edit_btn = self._make_button(
            toolbar, tr("schedule.edit"), self._on_edit, palette, width=96
        )
        self._edit_btn.pack(side="left", padx=(8, 0))
        self._toggle_btn = self._make_button(
            toolbar, tr("schedule.pause"), self._on_toggle, palette, width=96
        )
        self._toggle_btn.pack(side="left", padx=(8, 0))
        self._remove_btn = self._make_button(
            toolbar, tr("schedule.remove"), self._on_remove, palette, width=96
        )
        self._remove_btn.pack(side="left", padx=(8, 0))

        self._list_scroll = ctk.CTkScrollableFrame(
            container, fg_color=palette.panel, corner_radius=10
        )
        self._list_scroll.grid(row=2, column=0, sticky="nsew")
        self._list_scroll.grid_columnconfigure(0, weight=1)

        footer = ctk.CTkFrame(container, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._status_label = ctk.CTkLabel(
            footer,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._status_label.pack(side="left")
        self._close_btn = self._make_button(
            footer, tr("schedule.close"), self.close, palette, width=96
        )
        self._close_btn.pack(side="right")

    def _make_button(
        self,
        parent: ctk.CTkFrame,
        text: str,
        command: Callable[[], None],
        palette: Palette,
        *,
        width: int,
    ) -> ctk.CTkButton:
        """创建一个与整体风格一致的小按钮."""
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=30,
            corner_radius=8,
            fg_color=palette.raised,
            hover_color=palette.raised,
            text_color=palette.text_body,
            border_width=1,
            border_color=palette.border,
            font=ctk.CTkFont(size=12),
        )

    # -- 数据 ---------------------------------------------------------------

    def reload(self) -> None:
        """重新拉取定时任务并重绘列表(只展示已配置的任务)."""
        scheduled = [
            item for item in self._backend.list_schedules() if item.interval_text
        ]
        self._items = scheduled
        if self._selected not in {item.game_id for item in self._items}:
            self._selected = None
            self._status_label.configure(text="")
        self._render_summary()
        self._render_rows()
        self._update_actions()

    def _render_summary(self) -> None:
        active = sum(1 for item in self._items if item.enabled)
        self._summary_label.configure(
            text=tr(
                "schedule.summary",
                total=len(self._items),
                enabled=active,
            )
        )

    def _render_rows(self) -> None:
        palette = self._palette
        for child in self._list_scroll.winfo_children():
            child.destroy()
        self._rows = {}
        if not self._items:
            ctk.CTkLabel(
                self._list_scroll,
                text=tr("schedule.empty"),
                font=ctk.CTkFont(size=13),
                text_color=palette.text_muted,
            ).pack(padx=10, pady=16)
            return
        for item in self._items:
            self._rows[item.game_id] = self._build_row(item)

    def _build_row(self, item: ScheduleItem) -> list[ctk.CTkBaseClass]:
        palette = self._palette
        row = ctk.CTkFrame(
            self._list_scroll,
            corner_radius=8,
            fg_color=palette.card,
            border_width=1,
            border_color=palette.card_border,
            cursor="hand2",
        )
        row.pack(fill="x", padx=6, pady=4)
        row.grid_columnconfigure(1, weight=1)
        icon = ctk.CTkLabel(
            row,
            text=item.game_name[:1],
            width=32,
            height=32,
            corner_radius=8,
            fg_color=palette.accent,
            text_color=palette.accent_text,
            font=ctk.CTkFont(size=14, weight="bold"),
        )
        icon.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=9)
        name = ctk.CTkLabel(
            row,
            text=item.game_label,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        name.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(9, 0))
        detail = ctk.CTkLabel(
            row,
            text=item.summary,
            anchor="w",
            justify="left",
            wraplength=380,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        detail.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 9))
        state = ctk.CTkLabel(
            row,
            text=item.state_label,
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=palette.accent if item.interval_text else palette.text_muted,
        )
        state.grid(row=0, column=2, rowspan=2, padx=(0, 14))
        for widget in (row, icon, name, detail, state):
            widget.bind(
                "<Button-1>",
                lambda _event, gid=item.game_id: self._select(gid),
            )
        return [row, icon, name, detail, state]

    def _select(self, game_id: str) -> None:
        self._selected = game_id
        self._paint_rows()
        self._update_actions()

    def _paint_rows(self) -> None:
        palette = self._palette
        for game_id, widgets in self._rows.items():
            selected = game_id == self._selected
            widgets[0].configure(
                fg_color=palette.item_active if selected else palette.card,
                border_color=palette.accent if selected else palette.card_border,
            )

    def _update_actions(self) -> None:
        selected = self._selected_item()
        self._remove_btn.configure(state="normal" if selected else "disabled")
        self._edit_btn.configure(state="normal" if selected else "disabled")
        can_toggle = bool(selected and selected.interval_text)
        self._toggle_btn.configure(state="normal" if can_toggle else "disabled")
        if selected is not None and selected.interval_text:
            self._toggle_btn.configure(
                text=(
                    tr("schedule.pause") if selected.enabled else tr("schedule.resume")
                )
            )
        self._add_btn.configure(state="normal" if self._addable() else "disabled")

    def _selected_item(self) -> ScheduleItem | None:
        return next(
            (item for item in self._items if item.game_id == self._selected), None
        )

    def _addable(self) -> list[ScheduleItem]:
        """新任务的候选游戏: 未配置定时备份且已配置存档位置的那些."""
        return [
            item
            for item in self._backend.list_schedules()
            if not item.interval_text and item.can_schedule
        ]

    def _blocked(self) -> list[ScheduleItem]:
        """想配置却缺前置条件的游戏(没有存档位置), 供弹窗提示原因."""
        return [
            item
            for item in self._backend.list_schedules()
            if not item.interval_text and not item.can_schedule
        ]

    # -- 操作 ---------------------------------------------------------------

    def _on_add(self) -> None:
        """弹出选游戏窗口后新增定时任务(推荐先到游戏设置里配置)."""
        candidates = self._addable()
        if not candidates:
            self._status_label.configure(
                text=(
                    tr("schedule.all_configured")
                    if not self._blocked()
                    else tr("schedule.no_eligible")
                )
            )
            return
        game_id = add_schedule_dialog(
            self._window,
            self._palette,
            candidates=candidates,
            blocked=self._blocked(),
        )
        if game_id is None:
            return
        item = next(item for item in candidates if item.game_id == game_id)
        if self._apply(item):
            self._selected = item.game_id
            self._paint_rows()
            self._update_actions()
            self._status_label.configure(text=tr("schedule.added", name=item.game_name))

    def _on_edit(self) -> None:
        """编辑当前选中游戏的定时备份配置."""
        item = self._selected_item()
        if item is None:
            return
        if self._apply(item):
            self._status_label.configure(
                text=tr("schedule.updated", name=item.game_name)
            )

    def _apply(self, item: ScheduleItem) -> bool:
        """打开配置对话框并写回后端; 成功时刷新列表."""
        changed = edit_schedule(
            self._window,
            self._palette,
            self._backend,
            game_id=item.game_id,
            game_name=item.game_name,
        )
        if not changed:
            return False
        self.reload()
        if self._on_change is not None:
            self._on_change()
        return True

    def _on_toggle(self) -> None:
        """暂停或恢复选中游戏的定时备份(保留周期配置)."""
        item = self._selected_item()
        if item is None or not item.interval_text:
            return
        status = self._backend.set_schedule(
            item.game_id,
            item.interval_text,
            enabled=not item.enabled,
            keep_auto=item.keep_auto,
        )
        self._status_label.configure(
            text=(
                tr("result.schedule_saved", interval=status.schedule_text)
                if status.schedule_text
                else tr("result.schedule_cleared")
            )
        )
        self.reload()
        if self._on_change is not None:
            self._on_change()

    def _on_remove(self) -> None:
        """删除选中游戏的定时任务(取消周期, 不影响已有备份)."""
        item = self._selected_item()
        if item is None:
            return
        confirmed = confirm_dialog(
            self._window,
            self._palette,
            title=tr("schedule.remove_title"),
            message=tr("schedule.remove_message", name=item.game_name),
            confirm_text=tr("schedule.remove_confirm"),
        )
        if not confirmed:
            return
        self._backend.set_schedule(item.game_id, "", keep_auto=item.keep_auto)
        log_action("schedule.remove", game_id=item.game_id, name=item.game_name)
        self._status_label.configure(text=tr("schedule.removed", name=item.game_name))
        self.reload()
        if self._on_change is not None:
            self._on_change()

    def close(self) -> None:
        """销毁窗口."""
        self._window.destroy()
