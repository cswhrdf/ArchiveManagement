"""管理游戏与存档位置窗口 (阶段 C).

一个模态浮层: 上半部管理游戏信息(重命名/停用/删除), 下半部管理该
游戏的"原始存档位置"(添加目录/文件、设为主位置、重新验证、编辑路径、
删除). 界面明确区分原始位置与由应用管理的备份目录, 避免误操作
(PLAN 阶段 C 第 1/2/6 条). 本模块采用组合式窗口, 便于无头测试.
"""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from archive_management.domain import PathKind
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import _center, ask_text, confirm_dialog, info_dialog
from archive_management.ui.models import LocationItem
from archive_management.ui.palette import Palette
from archive_management.ui.pickers import pick_directory, pick_file

_ChangeCallback = Callable[[], None]


def _kind_text(kind: PathKind) -> str:
    """返回目录/文件的展示文案."""
    return tr("loc.kind_dir") if kind == "directory" else tr("loc.kind_file")


class ManageGameWindow:
    """管理单个游戏的窗口(不直接子类化 CTkToplevel)."""

    def __init__(
        self,
        parent: ctk.CTk,
        *,
        backend: ArchiveService,
        palette: Palette,
        game_id: str,
        name: str,
        enabled: bool,
        backup_location: str,
        on_change: _ChangeCallback,
    ) -> None:
        """构造管理窗口并装载该游戏的存档位置."""
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._game_id = game_id
        self._name = name
        self._enabled = enabled
        self._backup_location = backup_location
        self._on_change = on_change
        self._items: list[LocationItem] = []
        self._selected: str | None = None
        self._rows: dict[str, list[ctk.CTkBaseClass]] = {}
        self._build()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        palette = self._palette
        window = ctk.CTkToplevel(self._parent)
        self._window = window
        window.title(tr("manage.title"))
        window.geometry("600x560")
        window.resizable(False, False)
        window.transient(self._parent)
        window.grab_set()
        window.configure(fg_color=palette.background)
        _center(self._parent, window)

        container = ctk.CTkFrame(window, fg_color=palette.background)
        container.pack(fill="both", expand=True, padx=18, pady=16)
        container.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(container, fg_color=palette.panel, corner_radius=10)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        self._title_label = ctk.CTkLabel(
            header,
            text=self._name,
            anchor="w",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=palette.text_primary,
        )
        self._title_label.grid(row=0, column=0, padx=16, pady=(14, 0), sticky="w")
        self._state_label = ctk.CTkLabel(
            header,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._state_label.grid(row=1, column=0, padx=16, pady=(2, 4), sticky="w")

        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.grid(row=0, column=1, rowspan=2, padx=12)
        self._rename_btn = self._make_button(
            header_actions, tr("manage.rename"), self._on_rename, width=88
        )
        self._rename_btn.pack(side="left", padx=(0, 6))
        self._toggle_btn = self._make_button(
            header_actions,
            tr("manage.disable") if self._enabled else tr("manage.enable"),
            self._on_toggle_enabled,
            width=88,
        )
        self._toggle_btn.pack(side="left", padx=(0, 6))
        self._delete_btn = self._make_button(
            header_actions, tr("manage.delete_game"), self._on_delete_game, width=92
        )
        self._delete_btn.pack(side="left")

        locations_title = ctk.CTkLabel(
            container,
            text=tr("manage.locations_title"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        locations_title.grid(row=1, column=0, sticky="w", pady=(0, 6))

        self._list_scroll = ctk.CTkScrollableFrame(
            container, fg_color=palette.panel, corner_radius=10
        )
        self._list_scroll.grid(row=2, column=0, sticky="nsew", pady=(0, 8))
        self._list_scroll.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        note = ctk.CTkLabel(
            container,
            text=tr("manage.backup_note", path=self._backup_location),
            anchor="w",
            wraplength=540,
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        note.grid(row=3, column=0, sticky="w", pady=(0, 8))

        action_bar = ctk.CTkFrame(container, fg_color="transparent")
        action_bar.grid(row=4, column=0, sticky="w")
        for text, handler, width in (
            (tr("loc.add_dir"), self._on_add_directory, 96),
            (tr("loc.add_file"), self._on_add_file, 96),
            (tr("loc.set_primary"), self._on_set_primary, 108),
            (tr("loc.verify"), self._on_verify, 92),
            (tr("loc.edit"), self._on_edit_path, 92),
            (tr("loc.remove"), self._on_remove, 80),
        ):
            button = self._make_button(action_bar, text, handler, width=width)
            button.pack(side="left", padx=(0, 6))

        close = self._make_button(
            container, tr("dialog.close"), self.close, width=96, danger=True
        )
        close.grid(row=5, column=0, sticky="e", pady=(10, 0))

        self._render_state()
        self.refresh()

    def _make_button(
        self,
        parent: ctk.CTkBaseClass,
        text: str,
        command: Callable[[], None],
        *,
        width: int,
        danger: bool = False,
    ) -> ctk.CTkButton:
        """创建一个符合当前调色板的按钮."""
        palette = self._palette
        return ctk.CTkButton(
            parent,
            text=text,
            width=width,
            height=30,
            corner_radius=7,
            fg_color=palette.danger if danger else palette.raised,
            hover_color=palette.accent_soft_border if danger else palette.item_hover,
            text_color=palette.danger_text if danger else palette.text_body,
            font=ctk.CTkFont(size=12),
            command=command,
        )

    # -- 状态与列表 ---------------------------------------------------------

    def _render_state(self) -> None:
        state = (
            tr("manage.enabled_label") if self._enabled else tr("manage.disabled_label")
        )
        self._state_label.configure(text=state)

    def refresh(self) -> None:
        """从后端重载存档位置列表并重绘."""
        self._items = self._backend.list_locations(self._game_id)
        if self._selected is not None and not any(
            item.location_id == self._selected for item in self._items
        ):
            self._selected = None
        self._rebuild_rows()

    def _rebuild_rows(self) -> None:
        for child in self._list_scroll.winfo_children():
            child.destroy()
        self._rows = {}
        if not self._items:
            empty = ctk.CTkLabel(
                self._list_scroll,
                text=tr("manage.no_locations"),
                anchor="w",
                font=ctk.CTkFont(size=12),
                text_color=self._palette.text_muted,
            )
            empty.grid(row=0, column=0, padx=12, pady=12, sticky="w")
            return
        for index, item in enumerate(self._items):
            self._build_row(index, item)
        self._paint_rows()

    def _build_row(self, index: int, item: LocationItem) -> None:
        palette = self._palette
        selected = item.location_id == self._selected
        row = ctk.CTkFrame(
            self._list_scroll,
            corner_radius=8,
            fg_color=palette.item_active if selected else palette.raised,
        )
        row.grid(row=index, column=0, sticky="ew", padx=8, pady=4)
        row.grid_columnconfigure(1, weight=1)

        chip = ctk.CTkLabel(
            row,
            text=_kind_text(item.path_kind),
            width=56,
            corner_radius=9,
            font=ctk.CTkFont(size=11),
            fg_color=palette.accent_soft,
            text_color=palette.accent_soft_text,
        )
        chip.grid(row=0, column=0, rowspan=2, padx=(10, 8), pady=8)

        title_text = item.path
        if item.is_primary:
            title_text = f"{tr('loc.primary')}  {title_text}"
        title = ctk.CTkLabel(
            row,
            text=title_text,
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=palette.text_primary if selected else palette.text_body,
        )
        title.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 0))

        note = ctk.CTkLabel(
            row,
            text=item.note,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.success if item.ok else palette.danger,
        )
        note.grid(row=1, column=1, sticky="w", padx=(0, 8), pady=(0, 8))

        for widget in (row, chip, title, note):
            widget.bind(
                "<Button-1>",
                lambda _event, lid=item.location_id: self._select(lid),
            )
        self._rows[item.location_id] = [row, title, note]

    def _paint_rows(self) -> None:
        palette = self._palette
        for item in self._items:
            widgets = self._rows.get(item.location_id)
            if widgets is None:
                continue
            row, title, note = widgets
            selected = item.location_id == self._selected
            row.configure(fg_color=palette.item_active if selected else palette.raised)
            title.configure(
                text_color=palette.text_primary if selected else palette.text_body
            )
            note.configure(text_color=palette.success if item.ok else palette.danger)

    def _select(self, location_id: str) -> None:
        self._selected = location_id
        self._paint_rows()

    # -- 游戏操作 -----------------------------------------------------------

    def _on_rename(self) -> None:
        name = ask_text(
            self._window,
            self._palette,
            title=tr("manage.rename_title"),
            text=tr("manage.rename_prompt"),
            initial=self._name,
        )
        if not name:
            return
        try:
            summary = self._backend.update_game(self._game_id, name)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._name = summary.name
        self._title_label.configure(text=self._name)
        self._on_change()

    def _on_toggle_enabled(self) -> None:
        try:
            summary = self._backend.set_game_enabled(self._game_id, not self._enabled)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._enabled = summary.enabled
        self._toggle_btn.configure(
            text=tr("manage.disable") if self._enabled else tr("manage.enable")
        )
        self._render_state()
        self._on_change()

    def _on_delete_game(self) -> None:
        confirmed = confirm_dialog(
            self._window,
            self._palette,
            title=tr("manage.delete_title"),
            message=tr("manage.delete_message", name=self._name),
            confirm_text=tr("manage.delete_confirm"),
        )
        if not confirmed:
            return
        try:
            self._backend.delete_game(self._game_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.close()  # close() 内部会触发外部刷新回调

    # -- 存档位置操作 -------------------------------------------------------

    def _on_add_directory(self) -> None:
        self._add_location("directory")

    def _on_add_file(self) -> None:
        self._add_location("file")

    def _add_location(self, kind: PathKind) -> None:
        title = (
            tr("loc.add_dir_title") if kind == "directory" else tr("loc.add_file_title")
        )
        pick = (
            (lambda: pick_directory(title=title))
            if kind == "directory"
            else (lambda: pick_file(title=title))
        )
        path = ask_text(
            self._window,
            self._palette,
            title=title,
            text=tr("loc.add_prompt", kind=_kind_text(kind)),
            browse=pick,
        )
        if not path:
            return
        try:
            self._backend.add_location(self._game_id, path=path, kind=kind)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.refresh()
        self._on_change()

    def _selected_item(self) -> LocationItem | None:
        if self._selected is None:
            return None
        for item in self._items:
            if item.location_id == self._selected:
                return item
        return None

    def _on_set_primary(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        try:
            self._backend.set_primary_location(self._game_id, item.location_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.refresh()
        self._on_change()

    def _on_verify(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        try:
            self._backend.verify_location(item.location_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.refresh()

    def _on_edit_path(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        pick = (
            (lambda: pick_directory(title=tr("loc.edit_dir_title")))
            if item.path_kind == "directory"
            else (lambda: pick_file(title=tr("loc.edit_file_title")))
        )
        path = ask_text(
            self._window,
            self._palette,
            title=tr("loc.edit_title"),
            text=tr("loc.edit_prompt"),
            initial=item.path,
            browse=pick,
        )
        if not path or path == item.path:
            return
        try:
            self._backend.update_location(item.location_id, path=path)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.refresh()
        self._on_change()

    def _on_remove(self) -> None:
        item = self._selected_item()
        if item is None:
            return
        confirmed = confirm_dialog(
            self._window,
            self._palette,
            title=tr("loc.remove_title"),
            message=tr("loc.remove_message", path=item.path),
            confirm_text=tr("loc.remove_confirm"),
        )
        if not confirmed:
            return
        try:
            self._backend.remove_location(item.location_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.refresh()
        self._on_change()

    # -- 其它 ---------------------------------------------------------------

    def _show_error(self, exc: ArchiveManagementError) -> None:
        info_dialog(
            self._window,
            self._palette,
            title=tr("dialog.error_title"),
            message=str(exc),
        )

    def close(self) -> None:
        """销毁窗口并触发外部刷新."""
        self._window.destroy()
        self._on_change()
