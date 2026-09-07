"""CustomTkinter 主窗口.

布局对应 ``docs/archive-management-ui*.svg``:顶部工具栏、左侧游戏/工作区
栏、标题行与概要卡、视图工具条、时间线/分支主面板与右侧 rail、底部反馈
状态条。主题切换不改变布局与操作语义;界面文案统一从 i18n 配置加载。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import customtkinter as ctk

from archive_management.i18n import tr
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.dialogs import (
    ask_branch_name,
    confirm_dialog,
    info_dialog,
)
from archive_management.ui.models import (
    BackupItem,
    FeedbackKind,
    GameDetail,
    GameSummary,
    TaskStatus,
    ViewKind,
    timeline_order,
)
from archive_management.ui.palette import DEFAULT_THEME, Palette
from archive_management.ui.widgets import UiKit

_TONE_COLORS: dict[str, str] = {
    "orange": "#d15b3e",
    "blue": "#405685",
    "green": "#3b806e",
    "default": "#405685",
}
_WINDOW_MIN = (1080, 720)
_RAIL_WIDTH = 350


class ArchiveApp(ctk.CTk):
    """存档管理主窗口."""

    def __init__(
        self,
        backend: ArchiveService,
        *,
        title: str,
        smoke_seconds: float | None = None,
    ) -> None:
        """构造主窗口并加载演示数据."""
        super().__init__()
        self.backend = backend
        self.kit = UiKit()

        self._theme = backend.current_theme()
        ctk.set_appearance_mode(self._theme)
        self.p = Palette.for_theme(self._theme)

        self._game_id: str | None = None
        self._game: GameSummary | None = None
        self._view = ViewKind.TIMELINE
        self._backup_id: str | None = None
        self._items: list[BackupItem] = []
        self._busy = False
        self._cards: dict[str, ctk.CTkFrame] = {}

        self._messages: queue.Queue[tuple[Literal["ok", "err"], str]] = queue.Queue()
        self._pending_ok: Callable[[str], None] | None = None
        self._sync_labels: list[ctk.CTkLabel] = []
        self._last_feedback: tuple[FeedbackKind, str] = (
            FeedbackKind.INFO,
            tr("status.ready"),
        )
        self._verified = True
        self.title(title)
        self.minsize(*_WINDOW_MIN)
        self.geometry("1360x860")
        self.configure(fg_color=self.p.background)

        self._build_layout()
        self._load_first_game()

        self.after(100, self._poll_messages)
        if smoke_seconds is not None:
            self.after(int(smoke_seconds * 1000), self.destroy)

    # ------------------------------------------------------------------ 布局

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        topbar = self.kit.frame(
            self, bg_key="topbar", border_key="border", corner_radius=0
        )
        topbar.grid(row=0, column=0, sticky="nsew")
        self._build_topbar(topbar)

        self._main = self.kit.frame(self, bg_key="background", corner_radius=0)
        self._main.grid(row=1, column=0, sticky="nsew")
        self._main.grid_columnconfigure(1, weight=1)
        self._main.grid_rowconfigure(0, weight=1)

        sidebar = self.kit.frame(
            self._main, bg_key="sidebar", border_key="border", corner_radius=0
        )
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.configure(width=272)
        sidebar.grid_propagate(False)
        self._build_sidebar(sidebar)

        self._content = self.kit.frame(self._main, bg_key="background", corner_radius=0)
        self._content.grid(row=0, column=1, sticky="nsew", padx=(0, 0))
        self._build_content()

    def _build_topbar(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(2, weight=1)
        logo = ctk.CTkFrame(
            parent, width=30, height=30, corner_radius=8, fg_color=self.p.accent
        )
        logo.grid(row=0, column=0, padx=(18, 12), pady=11)
        self.kit.register(lambda p: logo.configure(fg_color=p.accent))
        brand = self.kit.label(
            parent, tr("topbar.brand"), style="primary", size=16, weight="bold"
        )
        brand.grid(row=0, column=1, padx=(0, 8), pady=10)

        self._sync_label = self.kit.label(parent, "", style="muted", size=12)
        self._sync_label.grid(row=0, column=2, sticky="e", padx=8)
        self._sync_labels.append(self._sync_label)

        self.theme_btn = self.kit.button(
            parent,
            tr("theme.to_light"),
            style="ghost",
            command=self._on_toggle_theme,
            width=96,
            height=30,
        )
        self.theme_btn.grid(row=0, column=3, padx=(6, 18), pady=10)

    def _build_sidebar(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(1, weight=1)

        section = self.kit.label(
            parent, tr("sidebar.my_games"), style="muted", size=12, weight="bold"
        )
        section.grid(row=0, column=0, padx=22, pady=(18, 8), sticky="w")

        self._games_container = ctk.CTkScrollableFrame(
            parent, fg_color="transparent", scrollbar_button_color="#314765"
        )
        self._games_container.grid(row=1, column=0, sticky="nsew", padx=10)
        self.kit.register(
            lambda p: self._games_container.configure(
                fg_color="transparent",
                scrollbar_button_color=p.border,
            )
        )

        add_game = ctk.CTkFrame(parent, corner_radius=8, cursor="hand2")
        add_game.grid(row=2, column=0, padx=22, pady=(10, 2), sticky="ew")
        add_game.grid_columnconfigure(0, weight=1)
        self.kit.register(
            lambda p: add_game.configure(
                fg_color=p.raised, border_width=1, border_color=p.border
            )
        )
        self._add_game_label = self.kit.label(
            add_game,
            tr("sidebar.add_game"),
            style="body",
            size=13,
            weight="bold",
            anchor="center",
        )
        self._add_game_label.grid(row=0, column=0, pady=9)
        self._add_game_label.bind("<Button-1>", lambda _e: self._on_add_game())

        work = self.kit.label(
            parent, tr("sidebar.workspace"), style="muted", size=12, weight="bold"
        )
        work.grid(row=3, column=0, padx=22, pady=(14, 4), sticky="w")

        nav = ctk.CTkFrame(parent, fg_color="transparent")
        nav.grid(row=4, column=0, sticky="ew", padx=16, pady=2)
        self.kit.register(lambda p: nav.configure(fg_color="transparent"))
        nav_items = [
            (tr("sidebar.nav_all"), tr("sidebar.nav_all_hint")),
            (tr("sidebar.nav_scheduled"), tr("sidebar.nav_scheduled_hint")),
            (tr("sidebar.nav_settings"), tr("sidebar.nav_settings_hint")),
        ]
        for text, message in nav_items:
            row = self.kit.label(nav, text, style="body", size=14)
            row.pack(fill="x", padx=12, pady=4)
            row.bind("<Button-1>", lambda _e, m=message, t=text: self._on_nav(t, m))

        self._status_card = self.kit.frame(parent, bg_key="raised", border_key="border")
        self._status_card.grid(row=5, column=0, padx=16, pady=(8, 14), sticky="ew")
        self._status_card.grid_columnconfigure(1, weight=1)
        self._status_title = self.kit.label(
            self._status_card, "", style="primary", size=12, weight="bold"
        )
        self._status_title.grid(row=0, column=1, padx=(8, 10), pady=(10, 0), sticky="w")
        self._status_sub = self.kit.label(self._status_card, "", style="muted", size=11)
        self._status_sub.grid(row=1, column=1, padx=(8, 10), pady=(0, 10), sticky="w")
        dot = ctk.CTkLabel(self._status_card, text="●", text_color=self.p.success)
        dot.grid(row=0, column=0, rowspan=2, padx=(14, 0))
        self.kit.register(lambda p: dot.configure(text_color=p.success))

    def _build_content(self) -> None:
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(3, weight=1)

        header = self.kit.frame(self._content, bg_key="background", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 4))
        header.grid_columnconfigure(0, weight=1)
        self._title_label = self.kit.label(
            header, "", style="primary", size=26, weight="bold"
        )
        self._title_label.grid(row=0, column=0, sticky="w")
        self._subtitle_label = self.kit.label(header, "", style="muted", size=13)
        self._subtitle_label.grid(row=1, column=0, sticky="w", pady=(2, 0))
        self._export_btn = self.kit.button(
            header,
            tr("action.export"),
            style="accent",
            command=self._on_export,
            width=104,
            height=38,
        )
        self._export_btn.grid(row=0, column=1, rowspan=2, padx=(8, 6))
        self._settings_btn = self.kit.button(
            header,
            tr("action.game_settings"),
            style="ghost",
            command=self._on_game_settings,
            width=112,
            height=38,
        )
        self._settings_btn.grid(row=0, column=2, rowspan=2)

        self._hero = self.kit.frame(
            self._content, bg_key="hero_bg", border_key="border"
        )
        self._hero.grid(row=1, column=0, sticky="ew", padx=24, pady=8)
        self._build_hero()

        toolbar = self.kit.frame(self._content, bg_key="raised", border_key="border")
        toolbar.grid(row=2, column=0, sticky="ew", padx=24)
        self._build_toolbar(toolbar)

        body = self.kit.frame(self._content, bg_key="background", corner_radius=0)
        body.grid(row=3, column=0, sticky="nsew", padx=24, pady=(8, 8))
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)
        self._build_body(body)

        statusbar = self.kit.frame(
            self, bg_key="raised", border_key="border", corner_radius=0
        )
        statusbar.grid(row=2, column=0, sticky="ew")
        self._feedback_label = self.kit.label(
            statusbar, tr("status.ready"), style="muted", size=12
        )
        self._feedback_label.pack(side="left", padx=18, pady=4)
        self.kit.register(lambda p: self._restyle_feedback(p))

    def _build_hero(self) -> None:
        self._hero.grid_columnconfigure(0, weight=1)
        self._hero.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(self._hero, fg_color="transparent")
        left.grid(row=0, column=0, sticky="nsew", padx=(24, 8), pady=14)

        self._hero_tile = ctk.CTkLabel(
            left,
            text="",
            width=84,
            height=84,
            corner_radius=12,
            fg_color=self._tone_color(None),
            text_color="#ffffff",
            font=ctk.CTkFont(size=28, weight="bold"),
        )
        self._hero_tile.pack(side="left")

        info = ctk.CTkFrame(left, fg_color="transparent")
        info.pack(side="left", fill="y", padx=(18, 0))
        self.kit.label(info, tr("hero.current_game"), style="muted", size=11).pack(
            anchor="w", pady=(2, 0)
        )
        self._hero_name_label = self.kit.label(
            info, "", style="primary", size=20, weight="bold"
        )
        self._hero_name_label.pack(anchor="w", pady=(2, 0))
        self._hero_location_label = self.kit.label(info, "", style="body", size=13)
        self._hero_location_label.pack(anchor="w", pady=(3, 0))
        self._hero_verified_label = ctk.CTkLabel(
            info, text="", font=ctk.CTkFont(size=12, weight="bold")
        )
        self._hero_verified_label.pack(anchor="w", pady=(6, 0))
        self.kit.register(
            lambda p: self._hero_verified_label.configure(
                text_color=p.success if self._verified else p.danger
            )
        )

        stats = ctk.CTkFrame(self._hero, fg_color="transparent")
        stats.grid(row=0, column=1, sticky="e", padx=(8, 24), pady=14)

        recent = ctk.CTkFrame(stats, fg_color="transparent")
        recent.pack(side="left", padx=(0, 24))
        self.kit.label(recent, tr("hero.recent"), style="muted", size=11).pack(
            anchor="w"
        )
        self._stat_recent_value = self.kit.label(
            recent, "", style="primary", size=17, weight="bold"
        )
        self._stat_recent_value.pack(anchor="w", pady=(2, 0))
        self._stat_recent_sub = self.kit.label(recent, "", style="muted", size=11)
        self._stat_recent_sub.pack(anchor="w", pady=(2, 0))

        total = ctk.CTkFrame(stats, fg_color="transparent")
        total.pack(side="left", padx=(0, 24))
        self.kit.label(total, tr("hero.total"), style="muted", size=11).pack(anchor="w")
        self._stat_total_value = self.kit.label(
            total, "", style="primary", size=17, weight="bold"
        )
        self._stat_total_value.pack(anchor="w", pady=(2, 0))
        self._stat_total_sub = self.kit.label(total, "", style="muted", size=11)
        self._stat_total_sub.pack(anchor="w", pady=(2, 0))

        chip = ctk.CTkFrame(stats, corner_radius=10)
        chip.pack(side="left", padx=(4, 0))
        self.kit.register(
            lambda p: chip.configure(
                fg_color=p.accent_soft,
                border_color=p.accent_soft_border,
                border_width=1,
            )
        )
        self._stat_next_caption = ctk.CTkLabel(
            chip, text=tr("hero.next_auto"), font=ctk.CTkFont(size=11), anchor="w"
        )
        self._stat_next_caption.pack(anchor="w", padx=12, pady=(10, 0))
        self.kit.register(
            lambda p: self._stat_next_caption.configure(text_color=p.success)
        )
        self._stat_next_value = ctk.CTkLabel(
            chip, text="", font=ctk.CTkFont(size=16, weight="bold"), anchor="w"
        )
        self._stat_next_value.pack(anchor="w", padx=12, pady=(2, 10))
        self.kit.register(
            lambda p: self._stat_next_value.configure(text_color=p.accent_soft_text)
        )

    def _build_toolbar(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(5, weight=1)
        self._tab_widgets: dict[ViewKind, ctk.CTkButton] = {}

        timeline_btn = self._new_tab(parent, ViewKind.TIMELINE, tr("view.timeline"))
        timeline_btn.grid(row=0, column=0, padx=(16, 2), pady=10)
        self._tab_widgets[ViewKind.TIMELINE] = timeline_btn
        branch_btn = self._new_tab(parent, ViewKind.BRANCH, tr("view.branch"))
        branch_btn.grid(row=0, column=1, padx=(0, 2), pady=10)
        self._tab_widgets[ViewKind.BRANCH] = branch_btn
        self.kit.register(lambda p: self._paint_tabs(p))

        self.kit.label(parent, tr("filter.label"), style="muted", size=12).grid(
            row=0, column=2, padx=(16, 8), pady=10, sticky="w"
        )
        self._filter_source = self._new_combo(
            parent,
            [tr("filter.all_sources"), tr("filter.manual"), tr("filter.auto")],
            tr("filter.all_sources"),
        )
        self._filter_source.grid(row=0, column=3, padx=(0, 8), pady=10)
        self._filter_period = self._new_combo(
            parent,
            [
                tr("filter.last_week"),
                tr("filter.last_month"),
                tr("filter.all_time"),
            ],
            tr("filter.last_month"),
        )
        self._filter_period.grid(row=0, column=4, pady=10)

        self._backup_btn = self.kit.button(
            parent,
            tr("action.backup_now"),
            style="danger",
            command=self._on_backup,
            width=150,
            height=36,
        )
        self._backup_btn.grid(row=0, column=6, padx=14, pady=10, sticky="e")

    def _new_tab(
        self, parent: ctk.CTkFrame, view: ViewKind, text: str
    ) -> ctk.CTkButton:
        """创建一个视图切换按钮."""
        return ctk.CTkButton(
            parent,
            text=text,
            width=120,
            height=36,
            corner_radius=7,
            command=lambda: self._switch_view(view),
            font=ctk.CTkFont(size=13, weight="bold"),
        )

    def _new_combo(
        self, parent: ctk.CTkFrame, values: list[str], initial: str
    ) -> ctk.CTkComboBox:
        """创建一个带主题重绘的下拉框."""
        combo = ctk.CTkComboBox(
            parent,
            values=values,
            state="readonly",
            width=176,
            height=36,
            corner_radius=7,
            command=self._on_filter_change,
            font=ctk.CTkFont(size=12),
        )
        combo.set(initial)
        self.kit.register(lambda p: self._paint_combo(combo, p))
        return combo

    def _paint_tabs(self, palette: Palette) -> None:
        for view, button in self._tab_widgets.items():
            if view == self._view:
                button.configure(
                    fg_color=palette.accent_soft,
                    hover_color=palette.accent_soft,
                    text_color=palette.accent_soft_text,
                    border_width=0,
                )
            else:
                button.configure(
                    fg_color=palette.raised,
                    hover_color=palette.item_hover,
                    text_color=palette.text_body,
                    border_width=0,
                )

    def _paint_combo(self, combo: ctk.CTkComboBox, palette: Palette) -> None:
        combo.configure(
            fg_color=palette.input_bg,
            border_color=palette.border,
            button_color=palette.raised,
            button_hover_color=palette.item_hover,
            text_color=palette.text_body,
            dropdown_fg_color=palette.panel,
            dropdown_hover_color=palette.item_hover,
            dropdown_text_color=palette.text_body,
        )

    def _build_body(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(0, weight=1)

        # 左侧: 备份时间线 / 分支树 主面板
        self._list_panel = self.kit.frame(parent, bg_key="panel", border_key="border")
        self._list_panel.grid(row=0, column=0, sticky="nsew")
        self._list_panel.grid_columnconfigure(0, weight=1)
        self._list_panel.grid_rowconfigure(2, weight=1)
        self._list_title = self.kit.label(
            self._list_panel, "", style="h2", size=16, weight="bold"
        )
        self._list_title.grid(row=0, column=0, padx=18, pady=(16, 0), sticky="w")
        self._list_sub = self.kit.label(self._list_panel, "", style="muted", size=12)
        self._list_sub.grid(row=1, column=0, padx=18, pady=(2, 8), sticky="w")
        self._list_scroll = ctk.CTkScrollableFrame(
            self._list_panel, fg_color="transparent", scrollbar_button_color="#314765"
        )
        self._list_scroll.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self.kit.register(
            lambda p: self._list_scroll.configure(scrollbar_button_color=p.border)
        )

        # 右侧 rail: 选中备份 + 定时任务
        rail = self.kit.frame(parent, bg_key="background", corner_radius=0)
        rail.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        rail.configure(width=_RAIL_WIDTH)
        rail.grid_propagate(False)
        rail.grid_columnconfigure(0, weight=1)
        rail.grid_rowconfigure(0, weight=1)
        rail.grid_rowconfigure(1, weight=1)
        self._build_selected_panel(rail)
        self._build_task_panel(rail)

    def _build_selected_panel(self, parent: ctk.CTkFrame) -> None:
        panel = self.kit.frame(parent, bg_key="panel", border_key="border")
        panel.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(7, weight=1)

        self.kit.label(panel, tr("sel.title"), style="h2", size=15, weight="bold").grid(
            row=0, column=0, padx=18, pady=(16, 4), sticky="w"
        )
        self._selected_name = self.kit.label(
            panel, "", style="body", size=14, weight="bold"
        )
        self._selected_name.grid(row=1, column=0, padx=18, pady=(0, 2), sticky="w")
        self._selected_meta = self.kit.label(panel, "", style="muted", size=12)
        self._selected_meta.grid(row=2, column=0, padx=18, pady=(0, 4), sticky="w")

        line = ctk.CTkFrame(panel, height=1, fg_color="transparent")
        line.grid(row=3, column=0, sticky="ew", padx=18, pady=(4, 6))
        self.kit.register(lambda p: line.configure(fg_color=p.border))

        self.kit.label(panel, tr("sel.summary"), style="muted", size=11).grid(
            row=4, column=0, padx=18, pady=(0, 2), sticky="w"
        )
        self._selected_files = self.kit.label(panel, "", style="body", size=12)
        self._selected_files.grid(row=5, column=0, padx=18, pady=(0, 4), sticky="w")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=6, column=0, padx=18, pady=(6, 0), sticky="w")
        self._restore_btn = self.kit.button(
            actions,
            tr("action.restore"),
            style="danger",
            command=self._on_restore,
            width=138,
            height=32,
        )
        self._restore_btn.pack(side="left", padx=(0, 8))
        self._branch_btn = self.kit.button(
            actions,
            tr("action.branch"),
            style="ghost",
            command=self._on_branch,
            width=150,
            height=32,
        )
        self._branch_btn.pack(side="left")

    def _build_task_panel(self, parent: ctk.CTkFrame) -> None:
        panel = self.kit.frame(parent, bg_key="panel", border_key="border")
        panel.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(7, weight=1)

        self.kit.label(
            panel, tr("task.title"), style="h2", size=15, weight="bold"
        ).grid(row=0, column=0, padx=18, pady=(16, 8), sticky="w")
        self._task_name_label = self.kit.label(
            panel, "", style="body", size=13, weight="bold"
        )
        self._task_name_label.grid(row=1, column=0, padx=18, sticky="w")
        self._task_state_label = self.kit.label(panel, "", style="muted", size=12)
        self._task_state_label.grid(row=1, column=1, padx=(0, 18), sticky="e")

        self._task_progress = ctk.CTkProgressBar(panel, height=8, corner_radius=4)
        self._task_progress.grid(
            row=2, column=0, columnspan=2, padx=18, pady=(8, 12), sticky="ew"
        )
        self.kit.register(
            lambda p: self._task_progress.configure(
                fg_color=p.input_bg, progress_color=p.accent
            )
        )

        self.kit.label(panel, tr("task.next"), style="muted", size=12).grid(
            row=3, column=0, padx=18, sticky="w"
        )
        self._task_next = self.kit.label(panel, "", style="body", size=12)
        self._task_next.grid(row=3, column=1, padx=(0, 18), sticky="e")

        self.kit.label(panel, tr("task.target"), style="muted", size=12).grid(
            row=4, column=0, padx=18, pady=(6, 0), sticky="w"
        )
        self._task_target = self.kit.label(panel, "", style="muted", size=12)
        self._task_target.grid(row=4, column=1, padx=(0, 18), pady=(6, 0), sticky="e")

        self.kit.label(panel, tr("task.shortcut"), style="muted", size=12).grid(
            row=5, column=0, padx=18, pady=(6, 0), sticky="w"
        )
        self._task_shortcut = self.kit.label(panel, "", style="body", size=11)
        self._task_shortcut.grid(row=5, column=1, padx=(0, 18), pady=(6, 0), sticky="e")

        edit_btn = self.kit.button(
            panel,
            tr("task.edit"),
            style="ghost",
            command=self._on_edit_task,
            width=150,
            height=28,
        )
        edit_btn.grid(row=6, column=0, columnspan=2, padx=18, pady=(10, 0), sticky="w")

    # ---------------------------------------------------------------- 数据装载

    def _load_first_game(self) -> None:
        games = self.backend.list_games()
        self._render_game_list(games)
        if games:
            self._select_game(games[0].game_id)
        else:
            self._show_empty_list()
        task = self.backend.task_status()
        self._render_task(task)
        self._refresh_sync()

    def _render_game_list(self, games: list[GameSummary]) -> None:
        for child in self._games_container.winfo_children():
            child.destroy()
        for game in games:
            row = self._build_game_row(game)
            row.pack(fill="x", pady=3)
            self._bind_game_row(row, game)

    def _bind_game_row(self, row: ctk.CTkFrame, game: GameSummary) -> None:
        for widget in (row, *row.winfo_children()):
            widget.bind(
                "<Button-1>",
                lambda _event, gid=game.game_id: self._select_game(gid),
            )

    def _build_game_row(self, game: GameSummary) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self._games_container, corner_radius=10)
        row.grid_columnconfigure(1, weight=1)
        icon = ctk.CTkLabel(
            row,
            text=game.name[:1],
            width=34,
            height=34,
            corner_radius=8,
            fg_color=self._tone_color(game.tone),
            text_color="#ffffff",
            font=ctk.CTkFont(size=15, weight="bold"),
        )
        icon.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=8)
        name = ctk.CTkLabel(
            row,
            text=game.name,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=self.p.text_primary,
        )
        name.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(8, 0))
        detail = ctk.CTkLabel(
            row,
            text=game.list_detail,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self.p.text_muted,
        )
        detail.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 7))
        self.kit.register(
            lambda p, r=row, n=name, d=detail: self._paint_row(r, n, d, p, game)
        )
        return row

    def _paint_row(
        self,
        row: ctk.CTkFrame,
        name: ctk.CTkLabel,
        detail: ctk.CTkLabel,
        palette: Palette,
        game: GameSummary,
    ) -> None:
        selected = game.game_id == self._game_id
        if selected:
            row.configure(
                fg_color=palette.item_active,
                border_width=1,
                border_color=palette.accent,
            )
            name.configure(text_color=palette.text_primary)
        else:
            row.configure(fg_color=palette.raised, border_width=0)
            name.configure(text_color=palette.text_body)
        detail.configure(text_color=palette.text_muted)

    def _refresh_sync(self) -> None:
        for label in self._sync_labels:
            label.configure(text=tr("topbar.sync", stamp="2026/09/06  09:42"))

    def _show_empty_list(self) -> None:
        self._list_title.configure(text=tr("list.fallback_title"))
        self._list_sub.configure(text=tr("list.no_games"))
        for child in self._list_scroll.winfo_children():
            child.destroy()
        empty = self.kit.label(
            self._list_scroll, tr("list.empty_all"), style="muted", size=13
        )
        empty.pack(padx=10, pady=16)

    # ---------------------------------------------------------------- 渲染

    def _select_game(self, game_id: str) -> None:
        self._game_id = game_id
        games = self.backend.list_games()
        self._game = next((g for g in games if g.game_id == game_id), None)
        if self._game is None:
            return
        detail = self.backend.get_detail(game_id)
        self._backup_id = None
        self._hero_tile.configure(
            text=detail.name[:1], fg_color=self._tone_color(self._game.tone)
        )
        self._render_hero(detail)
        self._render_toolbar_header(detail)
        self._render_list()
        self._render_selected(None)
        self._update_actions()
        self.kit.apply(self.p)
        if self._game is not None and not self._game.has_locations:
            self._feedback(FeedbackKind.INFO, tr("game.no_locations_hint"))

    def _render_hero(self, detail: GameDetail) -> None:
        self._verified = detail.location_verified
        self._hero_name_label.configure(text=detail.name)
        if detail.main_location:
            location = f"{detail.location_note}  ·  {detail.main_location}"
        else:
            location = detail.location_note
        self._hero_location_label.configure(text=location)
        self._hero_verified_label.configure(
            text=tr("hero.verified") if self._verified else tr("hero.unverified")
        )
        self._stat_recent_value.configure(text=detail.last_backup_label)
        self._stat_recent_sub.configure(text=detail.last_backup_sub)
        self._stat_total_value.configure(text=detail.total_backups_label)
        self._stat_total_sub.configure(text=detail.total_backups_sub)
        self._stat_next_value.configure(text=detail.next_backup_label)

    def _render_toolbar_header(self, detail: GameDetail) -> None:
        self._title_label.configure(text=detail.name)
        self._subtitle_label.configure(text=detail.subtitle)

    def _render_task(self, task: TaskStatus) -> None:
        state = tr("task.running") if task.running else tr("task.paused")
        self._task_name_label.configure(text=task.task_name)
        self._task_state_label.configure(text=state)
        self._task_progress.set(task.progress)
        self._task_next.configure(text=task.next_run_label)
        self._task_target.configure(text=task.target_label)
        self._task_shortcut.configure(text=task.shortcut_label)
        self._status_title.configure(text=tr("status.service_ok"))
        self._status_sub.configure(
            text=tr("status.disk", free="186 GB", shortcut=task.shortcut_label)
        )

    def _render_list(self) -> None:
        game_id = self._game_id or ""
        self._items = self.backend.list_backups(game_id)
        ordered = timeline_order(self._items)
        if self._view == ViewKind.TIMELINE:
            title, sub = tr("list.timeline_title"), tr("list.timeline_sub")
        else:
            title, sub = tr("list.branch_title"), tr("list.branch_sub")
        self._list_title.configure(text=title)
        self._list_sub.configure(text=sub)

        items = self._apply_filters(ordered)
        if self._backup_id is not None and not any(
            item.backup_id == self._backup_id for item in items
        ):
            self._backup_id = None
            self._render_selected(None)

        for child in self._list_scroll.winfo_children():
            child.destroy()
        if not items:
            empty = self.kit.label(
                self._list_scroll,
                tr("list.empty_filtered"),
                style="muted",
                size=13,
            )
            empty.pack(padx=10, pady=16)
            return

        self._cards = {}
        for item in items:
            card = self._build_backup_card(item)
            card.pack(fill="x", padx=4, pady=3)
            self._cards[item.backup_id] = card
            card.bind(
                "<Button-1>",
                lambda _e, i=item: self._select_backup(i),
            )
        if self._backup_id and self._backup_id in self._cards:
            self._paint_cards()

    def _apply_filters(self, items: list[BackupItem]) -> list[BackupItem]:
        """按来源与时间范围筛选备份节点."""
        source = self._filter_source.get()
        if source == tr("filter.auto"):
            items = [item for item in items if item.auto]
        elif source == tr("filter.manual"):
            items = [item for item in items if not item.auto]

        period = self._filter_period.get()
        if period != tr("filter.all_time"):
            days = 7 if period == tr("filter.last_week") else 30
            threshold = datetime.now(UTC) - timedelta(days=days)
            items = [item for item in items if item.created_dt >= threshold]
        return items

    def _build_backup_card(self, item: BackupItem) -> ctk.CTkFrame:
        card = ctk.CTkFrame(self._list_scroll, corner_radius=10)
        card.grid_columnconfigure(1, weight=1)
        when = ctk.CTkLabel(
            card,
            text=item.created_label,
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self.p.text_body,
        )
        when.grid(row=0, column=0, padx=(12, 8), pady=(10, 2), sticky="w")
        badge = ctk.CTkLabel(
            card,
            text=f" {item.kind_label} ",
            corner_radius=11,
            font=ctk.CTkFont(size=11),
        )
        badge.grid(row=0, column=2, padx=(0, 12), pady=(10, 2), sticky="e")
        title = ctk.CTkLabel(
            card,
            text=item.title,
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=self.p.text_primary,
        )
        title.grid(row=1, column=0, columnspan=3, padx=12, pady=(2, 0), sticky="w")
        detail = ctk.CTkLabel(
            card,
            text=f"{item.branch_label}  ·  {item.size_label}",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self.p.text_muted,
        )
        detail.grid(row=2, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="w")
        self.kit.register(
            lambda p, c=card, w=when, t=title, d=detail, b=badge, i=item: self._paint_card(
                c, w, t, d, b, p, i
            )
        )
        return card

    def _paint_card(
        self,
        card: ctk.CTkFrame,
        when: ctk.CTkLabel,
        title: ctk.CTkLabel,
        detail: ctk.CTkLabel,
        badge: ctk.CTkLabel,
        palette: Palette,
        item: BackupItem,
    ) -> None:
        selected = item.backup_id == self._backup_id
        if selected:
            card.configure(
                fg_color=palette.accent_soft,
                border_width=1,
                border_color=palette.accent_soft_border,
            )
            title.configure(text_color=palette.accent_soft_text)
        else:
            card.configure(fg_color=palette.raised, border_width=0)
            title.configure(text_color=palette.text_primary)
        when.configure(text_color=palette.text_body)
        detail.configure(text_color=palette.text_muted)
        if item.auto:
            badge.configure(
                fg_color=palette.badge_auto_bg, text_color=palette.badge_auto_text
            )
        else:
            badge.configure(
                fg_color=palette.badge_manual_bg, text_color=palette.badge_manual_text
            )

    def _paint_cards(self) -> None:
        self.kit.apply(self.p)

    def _select_backup(self, item: BackupItem) -> None:
        self._backup_id = item.backup_id
        self._render_selected(item)
        self._update_actions()
        self._paint_cards()

    def _render_selected(self, item: BackupItem | None) -> None:
        if item is None:
            self._selected_name.configure(text=tr("sel.none"))
            self._selected_meta.configure(text=tr("sel.hint"))
            self._selected_files.configure(text="")
            self._restore_btn.configure(state="disabled")
            self._branch_btn.configure(state="disabled")
            return
        self._selected_name.configure(text=item.title)
        self._selected_meta.configure(
            text=f"{item.created_label}  ·  {item.branch_label}"
        )
        self._selected_files.configure(text=tr("sel.digest", size=item.size_label))

    # ---------------------------------------------------------------- 操作

    def _switch_view(self, view: ViewKind) -> None:
        """切换时间线/分支树视图并刷新列表."""
        if view == self._view:
            return
        self._view = view
        self._render_list()
        self.kit.apply(self.p)

    def _on_filter_change(self, _value: str) -> None:
        """筛选条件变化时刷新列表."""
        self._render_list()
        self._update_actions()

    def _update_actions(self) -> None:
        busy = self._busy
        game = self._game
        can_do_backup = game is not None and game.has_locations and not busy
        self._backup_btn.configure(state="normal" if can_do_backup else "disabled")
        self._export_btn.configure(state="normal" if not busy else "disabled")
        selected = self._backup_id is not None
        self._restore_btn.configure(
            state="normal" if selected and not busy else "disabled"
        )
        self._branch_btn.configure(
            state="normal" if selected and not busy else "disabled"
        )

    def _on_backup(self) -> None:
        game = self._game
        if game is None or self._busy:
            return
        self._set_busy(True)
        self._feedback(
            FeedbackKind.PENDING, tr("action.backup_pending", name=game.name)
        )

        def work() -> str:
            return self.backend.run_backup_now(game.game_id)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)
            self._render_list()

        self._submit(work, ok)

    def _on_restore(self) -> None:
        game = self._game
        if game is None or self._backup_id is None or self._busy:
            return
        confirmed = confirm_dialog(
            self,
            self.p,
            title=tr("dialog.restore_title"),
            message=tr("dialog.restore_message", name=game.name),
            confirm_text=tr("dialog.restore_confirm"),
        )
        if not confirmed:
            self._feedback(FeedbackKind.INFO, tr("action.restore_canceled"))
            return
        self._set_busy(True)
        backup_id = self._backup_id
        self._feedback(FeedbackKind.PENDING, tr("action.restore_pending"))

        def work() -> str:
            return self.backend.run_restore(game.game_id, backup_id)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)

        self._submit(work, ok)

    def _on_branch(self) -> None:
        game = self._game
        if game is None or self._backup_id is None or self._busy:
            return
        branch_name = ask_branch_name(
            self,
            self.p,
            title=tr("dialog.branch_title"),
            text=tr("dialog.branch_prompt"),
        )
        if not branch_name:
            self._feedback(FeedbackKind.INFO, tr("action.branch_canceled"))
            return
        self._set_busy(True)
        backup_id = self._backup_id
        self._feedback(
            FeedbackKind.PENDING, tr("action.branch_pending", branch=branch_name)
        )

        def work() -> str:
            return self.backend.run_create_branch(game.game_id, backup_id, branch_name)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)

        self._submit(work, ok)

    def _on_export(self) -> None:
        game = self._game
        if game is None or self._busy:
            return
        self._set_busy(True)
        self._feedback(
            FeedbackKind.PENDING, tr("action.export_pending", name=game.name)
        )

        def work() -> str:
            return self.backend.run_export(game.game_id)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)

        self._submit(work, ok)

    def _on_toggle_theme(self) -> None:
        next_theme = "light" if self._theme == "dark" else "dark"
        self._theme = self.backend.set_theme(next_theme)
        ctk.set_appearance_mode(self._theme)
        self.p = Palette.for_theme(self._theme)
        self.configure(fg_color=self.p.background)
        theme_text = (
            tr("theme.to_dark") if self._theme == "light" else tr("theme.to_light")
        )
        self.theme_btn.configure(text=theme_text)
        self.kit.apply(self.p)
        self._feedback(FeedbackKind.INFO, tr("theme.switched", theme=theme_text))

    def _on_add_game(self) -> None:
        self._feedback(FeedbackKind.INFO, tr("action.add_game_soon"))

    def _on_nav(self, name: str, message: str) -> None:
        if name == tr("sidebar.nav_settings"):
            self._open_settings()
        else:
            self._feedback(FeedbackKind.INFO, message)

    def _on_game_settings(self) -> None:
        self._open_settings()

    def _on_edit_task(self) -> None:
        self._open_settings()

    def _open_settings(self) -> None:
        task = self.backend.task_status()
        state = tr("task.running") if task.running else tr("task.paused")
        info_dialog(
            self,
            self.p,
            title=tr("dialog.settings_title"),
            message=tr(
                "dialog.settings_message",
                theme=self._theme,
                task=task.task_name,
                state=state,
                next_run=task.next_run_label,
                shortcut=task.shortcut_label,
                note=tr("dialog.settings_note"),
            ),
        )

    # ---------------------------------------------------------------- 反馈与后台

    def _feedback(self, kind: FeedbackKind, text: str) -> None:
        self._last_feedback = (kind, text)
        self._restyle_feedback(self.p)

    def _restyle_feedback(self, palette: Palette) -> None:
        kind, text = self._last_feedback
        prefix = {
            FeedbackKind.SUCCESS: "✓ ",
            FeedbackKind.ERROR: "✕ ",
            FeedbackKind.PENDING: "… ",
            FeedbackKind.INFO: "",
        }
        self._feedback_label.configure(
            text=f"{prefix[kind]}{text}",
            text_color=colors_by(kind, palette),
        )

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._update_actions()

    def _submit(
        self,
        work: Callable[[], str],
        on_ok: Callable[[str], None],
    ) -> None:
        """在后台线程执行阻塞操作, 结果经队列回到主线程."""
        self._pending_ok = on_ok

        def runner() -> None:
            try:
                payload = work()
            except Exception as exc:
                self._messages.put(("err", str(exc)))
            else:
                self._messages.put(("ok", payload))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_messages(self) -> None:
        """主线程轮询队列并分发完成消息."""
        while True:
            try:
                kind, payload = self._messages.get_nowait()
            except queue.Empty:
                break
            self._finish_message(kind, payload)
        self.after(100, self._poll_messages)

    def _finish_message(self, kind: Literal["ok", "err"], payload: str) -> None:
        on_ok = self._pending_ok
        self._pending_ok = None
        if kind == "ok":
            if on_ok is not None:
                # 成功回调负责解除忙碌状态并刷新视图
                on_ok(payload)
            else:
                self._set_busy(False)
        else:
            self._set_busy(False)
            self._feedback(FeedbackKind.ERROR, payload)

    def _tone_color(self, tone: str | None) -> str:
        return _TONE_COLORS.get(tone or "", _TONE_COLORS["default"])


def colors_by(kind: FeedbackKind, palette: Palette) -> str:
    """返回反馈等级对应颜色."""
    mapping = {
        FeedbackKind.INFO: palette.text_muted,
        FeedbackKind.SUCCESS: palette.success,
        FeedbackKind.ERROR: palette.danger,
        FeedbackKind.PENDING: palette.accent,
    }
    return mapping[kind]


def run_gui(
    *,
    smoke_seconds: float | None = None,
    display_name: str = "ArchiveManagement",
) -> int:
    """启动演示后端并进入主循环,返回退出码."""
    backend: ArchiveService = DemoArchiveService(delay=0.4)
    app = ArchiveApp(backend, title=display_name, smoke_seconds=smoke_seconds)
    app.mainloop()
    return 0


def default_theme() -> str:
    """返回默认主题名(供设置显示)."""
    return DEFAULT_THEME
