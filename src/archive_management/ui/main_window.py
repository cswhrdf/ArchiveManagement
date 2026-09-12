"""CustomTkinter 主窗口.

布局对应 ``docs/archive-management-ui*.svg``:顶部工具栏、左侧游戏/工作区
栏、标题行与概要卡、视图工具条、时间线/分支主面板与右侧 rail、底部反馈
状态条。主题切换不改变布局与操作语义;界面文案统一从 i18n 配置加载。
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal

import customtkinter as ctk

from archive_management.application.backup import MAX_NOTE_LENGTH
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import (
    DEFAULT_ACCELERATOR,
    GlobalHotkeyService,
    HotkeyBinding,
)
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import (
    ask_branch_name,
    ask_text,
    confirm_dialog,
    edit_backup_dialog,
    info_dialog,
    schedule_dialog,
)
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import (
    BackupItem,
    FeedbackKind,
    GameDetail,
    GameSummary,
    TaskStatus,
    ViewKind,
    branch_order,
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
# 分支视图中用于标示层级的连接符(与缩进配合).
_BRANCH_MARK = "└ "
# 当前节点标记: 后续备份/分支都从这个节点继续.
_CURRENT_MARK = "●"
# 自动备份保留份数的可配置上限.
MAX_KEEP_AUTO = 20


class ArchiveApp(ctk.CTk):
    """存档管理主窗口."""

    def __init__(
        self,
        backend: ArchiveService,
        *,
        title: str,
        smoke_seconds: float | None = None,
        hotkeys: GlobalHotkeyService | None = None,
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
        # 默认展示分支树(更直观地反映“从哪个节点继续”), 时间线作为第二视图.
        self._view = ViewKind.BRANCH
        self._backup_id: str | None = None
        self._items: list[BackupItem] = []
        self._current_id: str | None = None
        self._hover_id: str | None = None
        self._revision = -1
        self._busy = False
        self._canceled = False
        self._task_running = False
        self._task_ticks = 0
        self._cards: dict[str, ctk.CTkFrame] = {}
        self._card_painters: dict[str, Callable[[Palette], None]] = {}
        self._row_unregisters: list[Callable[[], None]] = []
        self._card_unregisters: list[Callable[[], None]] = []

        self._messages: queue.Queue[tuple[Literal["ok", "err", "hotkey"], str]] = (
            queue.Queue()
        )
        self._pending_ok: Callable[[str], None] | None = None
        self._sync_labels: list[ctk.CTkLabel] = []
        self._last_feedback: tuple[FeedbackKind, str] = (
            FeedbackKind.INFO,
            tr("status.ready"),
        )
        self._verified = True
        self._hotkeys = hotkeys if hotkeys is not None else GlobalHotkeyService()
        self._shortcut_text = "—"
        self.title(title)
        self.minsize(*_WINDOW_MIN)
        self.geometry("1360x860")
        self.configure(fg_color=self.p.background)

        self._build_layout()
        self._register_hotkey()
        self._load_first_game()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(100, self._poll_messages)
        if smoke_seconds is not None:
            # 冒烟自检也走正常关闭路径, 确保调度器/快捷键被释放.
            self.after(int(smoke_seconds * 1000), self._on_close)

    # ---------------------------------------------------------------- 快捷键

    def _register_hotkey(self) -> None:
        """注册"全局保存"快捷键; 注册失败只提示, 不影响应用可用性."""
        state = self._hotkeys.register(
            HotkeyBinding(name="save_now", accelerator=DEFAULT_ACCELERATOR),
            self._request_hotkey_backup,
        )
        if state.registered:
            self._shortcut_text = state.accelerator
        else:
            self._shortcut_text = tr("hotkey.unavailable")
            if state.error:
                self._last_feedback = (
                    FeedbackKind.INFO,
                    tr("hotkey.failed", reason=state.error),
                )

    def _request_hotkey_backup(self) -> None:
        """快捷键回调运行在监听线程: 只投递消息, 由主线程执行备份."""
        self._messages.put(("hotkey", ""))

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

        self._games_container = self.kit.scroll_frame(parent, bg_key="well")
        self._games_container.grid(row=1, column=0, sticky="nsew", padx=10)

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

        branch_btn = self._new_tab(parent, ViewKind.BRANCH, tr("view.branch"))
        branch_btn.grid(row=0, column=0, padx=(16, 2), pady=10)
        self._tab_widgets[ViewKind.BRANCH] = branch_btn
        timeline_btn = self._new_tab(parent, ViewKind.TIMELINE, tr("view.timeline"))
        timeline_btn.grid(row=0, column=1, padx=(0, 2), pady=10)
        self._tab_widgets[ViewKind.TIMELINE] = timeline_btn
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
        self._list_scroll = self.kit.scroll_frame(self._list_panel, bg_key="well")
        self._list_scroll.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))

        # 右侧 rail: 选中备份 + 定时任务.
        # 用可滚动容器承载: 两块面板都按内容高度排布, 窗口变矮时也不会把
        # 底部按钮挤掉(默认尺寸下不出现滚动条, 只是兜底).
        rail = self.kit.frame(parent, bg_key="background", corner_radius=0)
        rail.grid(row=0, column=1, sticky="nsew", padx=(12, 0))
        rail.configure(width=_RAIL_WIDTH)
        rail.grid_propagate(False)
        rail.grid_columnconfigure(0, weight=1)
        rail.grid_rowconfigure(0, weight=1)
        self._rail_scroll = self.kit.scroll_frame(rail, bg_key="background")
        self._rail_scroll.grid(row=0, column=0, sticky="nsew")
        self._rail_scroll.grid_columnconfigure(0, weight=1)
        self._build_selected_panel(self._rail_scroll)
        self._build_task_panel(self._rail_scroll)

    def _build_selected_panel(self, parent: ctk.CTkFrame) -> None:
        panel = self.kit.frame(parent, bg_key="panel", border_key="border")
        panel.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        panel.grid_columnconfigure(0, weight=1)

        self.kit.label(panel, tr("sel.title"), style="h2", size=15, weight="bold").grid(
            row=0, column=0, padx=18, pady=(14, 4), sticky="w"
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
        self._selected_files.grid(row=5, column=0, padx=18, pady=(0, 2), sticky="w")
        self._selected_state = self.kit.label(panel, "", style="muted", size=11)
        self._selected_state.grid(row=6, column=0, padx=18, pady=(0, 4), sticky="w")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=7, column=0, padx=18, pady=(6, 12), sticky="w")
        self._restore_btn = self.kit.button(
            actions,
            tr("action.restore"),
            style="accent",
            command=self._on_restore,
            width=158,
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
        actions_second = ctk.CTkFrame(panel, fg_color="transparent")
        actions_second.grid(row=8, column=0, padx=18, pady=(0, 12), sticky="w")
        self._rename_btn = self.kit.button(
            actions_second,
            tr("action.rename_backup"),
            style="ghost",
            command=self._on_rename_backup,
            width=158,
            height=32,
        )
        self._rename_btn.pack(side="left", padx=(0, 8))
        self._delete_btn = self.kit.button(
            actions_second,
            tr("action.delete_backup"),
            style="danger",
            command=self._on_delete_backup,
            width=150,
            height=32,
        )
        self._delete_btn.pack(side="left")

    def _build_task_panel(self, parent: ctk.CTkFrame) -> None:
        panel = self.kit.frame(parent, bg_key="panel", border_key="border")
        panel.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        panel.grid_columnconfigure(0, weight=1)

        self.kit.label(
            panel, tr("task.title"), style="h2", size=15, weight="bold"
        ).grid(row=0, column=0, padx=18, pady=(14, 8), sticky="w")
        self._task_name_label = self.kit.label(
            panel, "", style="body", size=13, weight="bold"
        )
        self._task_name_label.grid(row=1, column=0, padx=18, sticky="w")
        self._task_state_label = self.kit.label(panel, "", style="muted", size=12)
        self._task_state_label.grid(row=1, column=1, padx=(0, 18), sticky="e")

        self._task_progress = ctk.CTkProgressBar(panel, height=8, corner_radius=4)
        self._task_progress.grid(
            row=2, column=0, columnspan=2, padx=18, pady=(8, 4), sticky="ew"
        )
        self.kit.register(
            lambda p: self._task_progress.configure(
                fg_color=p.input_bg, progress_color=p.accent
            )
        )

        self._task_progress_label = self.kit.label(panel, "", style="muted", size=11)
        self._task_progress_label.grid(
            row=3, column=0, padx=18, pady=(0, 8), sticky="w"
        )
        self._cancel_btn = self.kit.button(
            panel,
            tr("task.cancel"),
            style="ghost",
            command=self._on_cancel,
            width=86,
            height=26,
        )
        self._cancel_btn.grid(row=3, column=1, padx=(0, 18), pady=(0, 8), sticky="e")
        self._cancel_btn.configure(state="disabled")

        self.kit.label(panel, tr("task.next"), style="muted", size=12).grid(
            row=4, column=0, padx=18, sticky="w"
        )
        self._task_next = self.kit.label(panel, "", style="body", size=12)
        self._task_next.grid(row=4, column=1, padx=(0, 18), sticky="e")

        self.kit.label(panel, tr("task.target"), style="muted", size=12).grid(
            row=5, column=0, padx=18, pady=(6, 0), sticky="w"
        )
        self._task_target = self.kit.label(panel, "", style="muted", size=12)
        self._task_target.grid(row=5, column=1, padx=(0, 18), pady=(6, 0), sticky="e")

        self.kit.label(panel, tr("task.shortcut"), style="muted", size=12).grid(
            row=6, column=0, padx=18, pady=(6, 0), sticky="w"
        )
        self._task_shortcut = self.kit.label(panel, "", style="body", size=11)
        self._task_shortcut.grid(row=6, column=1, padx=(0, 18), pady=(6, 0), sticky="e")

        self._task_edit_btn = self.kit.button(
            panel,
            tr("task.edit"),
            style="ghost",
            command=self._on_edit_task,
            width=150,
            height=28,
        )
        self._task_edit_btn.grid(
            row=7, column=0, columnspan=2, padx=18, pady=(10, 12), sticky="w"
        )

    # ---------------------------------------------------------------- 数据装载

    def _load_first_game(self) -> None:
        games = self.backend.list_games()
        self._render_game_list(games)
        if games:
            self._select_game(games[0].game_id)
        else:
            self._show_empty_list()
        self._render_task(self.backend.task_status(self._game_id))
        self._refresh_sync()

    def _render_game_list(self, games: list[GameSummary]) -> None:
        for unsubscribe in self._row_unregisters:
            unsubscribe()
        self._row_unregisters = []
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
        self._row_unregisters.append(
            self.kit.register(
                lambda p, r=row, n=name, d=detail: self._paint_row(r, n, d, p, game)
            )
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
            # 卡片色 + 描边: 浅色主题下白底白卡片也能与列表凹槽区分.
            row.configure(
                fg_color=palette.card,
                border_width=1,
                border_color=palette.card_border,
            )
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
        self._render_task(self.backend.task_status(game_id))
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
        self._task_progress_label.configure(
            text=task.progress_label if task.running else ""
        )
        self._task_next.configure(text=task.next_run_label)
        self._task_target.configure(text=task.target_label)
        self._task_shortcut.configure(text=self._shortcut_text)
        self._cancel_btn.configure(state="normal" if task.cancellable else "disabled")
        self._status_title.configure(text=tr("status.service_ok"))
        self._status_sub.configure(
            text=tr("status.disk", free="186 GB", shortcut=self._shortcut_text)
        )

    def _refresh_task(self) -> None:
        """轮询任务状态: 备份进行中时刷新进度, 数据变化时重载列表.

        备份可能由全局快捷键或定时任务在后台触发, 因此不能只看 ``running``
        (短备份可能在两次轮询之间结束); 这里以数据版本号判断列表是否需要
        重载. 空闲时降低刷新频率, 避免每 100ms 重配一次控件.
        """
        self._task_ticks += 1
        if not (self._busy or self._task_running) and self._task_ticks % 5:
            return
        task = self.backend.task_status(self._game_id)
        self._render_task(task)
        self._task_running = task.running
        if task.revision != self._revision:
            self._reload_data(task)

    def _reload_data(self, task: TaskStatus | None = None) -> None:
        """重载备份列表与概要, 并同步数据版本号与选中项."""
        status = task if task is not None else self.backend.task_status(self._game_id)
        self._revision = status.revision
        self._render_task(status)
        if self._game_id is None:
            return
        self._render_list()
        self._render_hero(self.backend.get_detail(self._game_id))
        self._restore_selection()

    def _restore_selection(self) -> None:
        """按最新数据重绘选中项(选中节点已消失时清空选择)."""
        item = self._selected_item()
        if item is None:
            self._backup_id = None
            self._render_selected(None)
            return
        self._render_selected(item)

    def _render_list(self) -> None:
        if self._game_id is None:
            # 空库(首次启动): 没有可展示的备份, 保持空状态.
            return
        game_id = self._game_id
        self._items = self.backend.list_backups(game_id)
        current = next((item for item in self._items if item.is_current), None)
        self._current_id = None if current is None else current.backup_id
        if self._view == ViewKind.TIMELINE:
            ordered = timeline_order(self._items)
            title, sub = tr("list.timeline_title"), tr("list.timeline_sub")
        else:
            ordered = branch_order(self._items)
            title, sub = tr("list.branch_title"), tr("list.branch_sub")
        self._list_title.configure(text=title)
        self._list_sub.configure(text=sub)

        items = self._apply_filters(ordered)
        if self._backup_id is not None and not any(
            item.backup_id == self._backup_id for item in items
        ):
            self._backup_id = None
            self._render_selected(None)

        for unsubscribe in self._card_unregisters:
            unsubscribe()
        self._card_unregisters = []
        self._card_painters = {}
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

        # 列表重建后悬停状态失效, 清掉避免指向已销毁的卡片.
        self._hover_id = None
        self._cards = {}
        for item in items:
            card = self._build_backup_card(item)
            card.pack(fill="x", padx=4, pady=4)
            self._cards[item.backup_id] = card
            card.bind(
                "<Button-1>",
                lambda _e, i=item: self._select_backup(i),
            )
            for widget in (card, *card.winfo_children()):
                widget.bind(
                    "<Enter>",
                    lambda _e, i=item: self._set_hover(i.backup_id),
                )
                widget.bind("<Leave>", lambda _e: self._set_hover(None))
        # 只有选中项的卡片需要补一次终态着色(其余卡片创建时已是最终颜色).
        self._paint_card_by_id(self._backup_id)

    def _card_title(self, item: BackupItem) -> str:
        """卡片标题: 分支层级缩进 + 当前节点标记."""
        prefix = _BRANCH_MARK * item.depth
        marker = f"{_CURRENT_MARK} " if item.is_current else ""
        return f"{prefix}{marker}{item.display_title}"

    def _card_detail(self, item: BackupItem) -> str:
        """卡片副标题.

        分支树视图里层级已经表达了分支归属, 因此只显示容量与描述; 时间线
        视图需要额外标明该备份处于哪条分支下.
        """
        if self._view == ViewKind.TIMELINE:
            text = f"{item.branch_label}  ·  {item.size_label}"
        else:
            text = item.size_label
        return f"{text}\n{item.sub}" if item.sub else text

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
        card = ctk.CTkFrame(
            self._list_scroll,
            corner_radius=10,
            # 创建时就按调色板着色: 否则会先闪现 CTk 主题默认色再被重绘修正.
            fg_color=self.p.card,
            border_width=1,
            border_color=self.p.card_border,
        )
        card.grid_columnconfigure(1, weight=1)
        indent = 12 + item.depth * 18
        when = ctk.CTkLabel(
            card,
            text=item.created_label,
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self.p.text_body,
        )
        when.grid(row=0, column=0, padx=(indent, 8), pady=(10, 2), sticky="w")
        badge = ctk.CTkLabel(
            card,
            text=f" {item.kind_label} ",
            corner_radius=11,
            font=ctk.CTkFont(size=11),
        )
        badge.grid(row=0, column=2, padx=(0, 12), pady=(10, 2), sticky="e")
        title = ctk.CTkLabel(
            card,
            text=self._card_title(item),
            anchor="w",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=self.p.text_primary,
        )
        title.grid(
            row=1, column=0, columnspan=3, padx=(indent, 12), pady=(2, 0), sticky="w"
        )
        detail_text = self._card_detail(item)
        detail = ctk.CTkLabel(
            card,
            text=detail_text,
            anchor="w",
            justify="left",
            wraplength=560,
            font=ctk.CTkFont(size=11),
            text_color=self.p.text_muted,
        )
        detail.grid(row=2, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="w")

        def paint(
            palette: Palette,
            c: ctk.CTkFrame = card,
            w: ctk.CTkLabel = when,
            t: ctk.CTkLabel = title,
            d: ctk.CTkLabel = detail,
            b: ctk.CTkLabel = badge,
            i: BackupItem = item,
        ) -> None:
            self._paint_card(c, w, t, d, b, palette, i)

        # 单卡重绘函数同时用于主题重绘与悬停高亮: 悬停只重绘受影响的两张卡片,
        # 避免滚轮滚动时鼠标划过卡片触发全量重绘造成卡顿.
        self._card_painters[item.backup_id] = paint
        self._card_unregisters.append(self.kit.register(paint))
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
        hovered = item.backup_id == self._hover_id
        if selected:
            card.configure(
                fg_color=palette.accent_soft,
                border_width=1,
                border_color=palette.accent_soft_border,
            )
            title.configure(text_color=palette.accent_soft_text)
        else:
            # 卡片色 + 描边, 与列表凹槽底色拉开对比(浅色主题下也不至于白上加白).
            card.configure(
                fg_color=palette.card_hover if hovered else palette.card,
                border_width=1,
                border_color=palette.card_border,
            )
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
        """用当前主题全量重绘(主题切换等一次性场景)."""
        self.kit.apply(self.p)

    def _paint_card_by_id(self, backup_id: str | None) -> None:
        """只重绘单张卡片.

        卡片数量可观时全量重绘开销很大(实测 40 张约 380ms), 因此选中与
        悬停这类高频交互只重绘受影响的卡片。
        """
        if backup_id is None:
            return
        paint = self._card_painters.get(backup_id)
        if paint is None:
            return
        try:
            paint(self.p)
        except tk.TclError:
            # 卡片已被销毁(列表刚好刷新): 忽略这一次局部重绘.
            return

    def _set_hover(self, backup_id: str | None) -> None:
        """记录鼠标悬停的卡片并只重绘受影响的两张卡片.

        滚轮滚动时指针下的卡片会不断变化, 若每次都触发全量重绘, 卡片越多
        越卡(拖动滚动条不会有这种开销, 所以看起来“滚轮卡、拖条不卡”).
        """
        if self._hover_id == backup_id:
            return
        previous, self._hover_id = self._hover_id, backup_id
        self._paint_card_by_id(previous)
        self._paint_card_by_id(backup_id)

    def _select_backup(self, item: BackupItem) -> None:
        previous, self._backup_id = self._backup_id, item.backup_id
        self._render_selected(item)
        self._update_actions()
        self._paint_card_by_id(previous)
        self._paint_card_by_id(item.backup_id)

    def _render_selected(self, item: BackupItem | None) -> None:
        if item is None:
            self._selected_name.configure(text=tr("sel.none"))
            self._selected_meta.configure(text=tr("sel.hint"))
            self._selected_files.configure(text="")
            self._selected_state.configure(text="")
            self._restore_btn.configure(state="disabled")
            self._branch_btn.configure(state="disabled")
            self._rename_btn.configure(state="disabled")
            self._delete_btn.configure(state="disabled")
            return
        self._selected_name.configure(text=item.display_title)
        self._selected_meta.configure(
            text=f"{item.created_label}  ·  {item.branch_label}"
        )
        self._selected_files.configure(
            text=item.sub or tr("sel.digest", size=item.size_label)
        )
        self._selected_state.configure(
            text=(
                tr("sel.current")
                if item.is_current
                else tr("sel.not_current", size=item.size_label)
            )
        )
        self._update_actions()

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
        enabled = "normal" if selected and not busy else "disabled"
        self._restore_btn.configure(state=enabled)
        self._branch_btn.configure(state=enabled)
        self._rename_btn.configure(state=enabled)
        self._delete_btn.configure(state=enabled)

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
            self._reload_data()

        self._submit(work, ok)

    def _on_cancel(self) -> None:
        """请求取消正在进行的备份(实际取消由后台线程在安全检查点响应)."""
        if not self.backend.cancel_active():
            self._feedback(FeedbackKind.INFO, tr("action.cancel_none"))
            return
        self._canceled = True
        self._feedback(FeedbackKind.PENDING, tr("action.cancel_pending"))

    def _on_restore(self) -> None:
        """恢复到此节点: 把当前节点移到这里, 之后的备份/分支都从此继续."""
        game = self._game
        if game is None or self._backup_id is None or self._busy:
            return
        item = self._selected_item()
        confirmed = confirm_dialog(
            self,
            self.p,
            title=tr("dialog.restore_title"),
            message=tr(
                "dialog.restore_message",
                name=game.name,
                title=item.display_title if item is not None else "",
            ),
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
            self._reload_data()

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
            initial=tr("dialog.branch_default"),
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
            self._reload_data()

        self._submit(work, ok)

    def _on_rename_backup(self) -> None:
        """在一个窗口内修改选中备份的名称与描述."""
        game = self._game
        item = self._selected_item()
        if game is None or item is None or self._busy:
            return
        edited = edit_backup_dialog(
            self,
            self.p,
            title=tr("dialog.rename_title"),
            name_label=tr("dialog.rename_label"),
            desc_label=tr("dialog.describe_label"),
            desc_prompt=tr("dialog.describe_prompt", limit=MAX_NOTE_LENGTH),
            initial_name=item.display_title,
            initial_desc=item.sub,
            limit=MAX_NOTE_LENGTH,
        )
        if edited is None:
            return
        title, note = edited
        backup_id = item.backup_id

        def work() -> str:
            self.backend.rename_backup(game.game_id, backup_id, title=title, note=note)
            return tr("result.renamed", title=title or item.display_title)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)
            self._reload_data()

        self._set_busy(True)
        self._submit(work, ok)

    def _on_delete_backup(self) -> None:
        """删除备份: 同线路节点让后续上移, 分支根节点需确认后连带子分支删除."""
        game = self._game
        item = self._selected_item()
        if game is None or item is None or self._busy:
            return
        try:
            plan = self.backend.plan_delete(game.game_id, item.backup_id)
        except ArchiveManagementError as exc:
            self._feedback(FeedbackKind.ERROR, str(exc))
            return
        if plan.needs_confirmation:
            confirmed = confirm_dialog(
                self,
                self.p,
                title=tr("dialog.delete_branch_title"),
                message=tr(
                    "dialog.delete_branch_message",
                    title=item.display_title,
                    count=plan.removed_count,
                ),
                confirm_text=tr("dialog.delete_confirm"),
            )
            if not confirmed:
                self._feedback(FeedbackKind.INFO, tr("action.delete_canceled"))
                return
        backup_id = item.backup_id

        def work() -> str:
            return self.backend.run_delete_backup(game.game_id, backup_id)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._backup_id = None
            self._feedback(FeedbackKind.SUCCESS, message)
            self._reload_data()

        self._set_busy(True)
        self._feedback(FeedbackKind.PENDING, tr("action.delete_pending"))
        self._submit(work, ok)

    def _selected_item(self) -> BackupItem | None:
        """返回当前选中的备份项."""
        if self._backup_id is None:
            return None
        return next(
            (item for item in self._items if item.backup_id == self._backup_id), None
        )

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
        """添加游戏: 询问名称后写入后端并选中."""
        if self._busy:
            return
        name = ask_text(
            self,
            self.p,
            title=tr("dialog.add_game_title"),
            text=tr("dialog.add_game_prompt"),
        )
        if not name:
            return
        try:
            summary = self.backend.add_game(name)
        except ArchiveManagementError as exc:
            self._feedback(FeedbackKind.ERROR, str(exc))
            return
        self._feedback(FeedbackKind.SUCCESS, tr("result.game_added", name=summary.name))
        self._refresh_after_manage(select=summary.game_id)

    def _on_nav(self, name: str, message: str) -> None:
        if name == tr("sidebar.nav_settings"):
            self._open_settings()
        else:
            self._feedback(FeedbackKind.INFO, message)

    def _on_game_settings(self) -> None:
        self._open_manage_game()

    def _on_edit_task(self) -> None:
        """在一个窗口内编辑定时备份周期(留空即取消)与自动备份保留份数."""
        game = self._game
        if game is None:
            self._feedback(FeedbackKind.INFO, tr("manage.require_game"))
            return
        task = self.backend.task_status(game.game_id)
        edited = schedule_dialog(
            self,
            self.p,
            title=tr("dialog.schedule_title"),
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
            return
        text, keep_text = edited
        keep_auto = task.keep_auto
        if keep_text:
            try:
                keep_auto = int(keep_text)
            except ValueError:
                self._feedback(FeedbackKind.ERROR, tr("error.keep_auto_invalid"))
                return
            if not 1 <= keep_auto <= MAX_KEEP_AUTO:
                self._feedback(
                    FeedbackKind.ERROR,
                    tr("error.keep_auto_range", max=MAX_KEEP_AUTO),
                )
                return
        try:
            status = self.backend.set_schedule(game.game_id, text, keep_auto=keep_auto)
        except ArchiveManagementError as exc:
            self._feedback(FeedbackKind.ERROR, str(exc))
            return
        self._render_task(status)
        self._render_hero(self.backend.get_detail(game.game_id))
        self._feedback(
            FeedbackKind.SUCCESS,
            (
                tr("result.schedule_saved", interval=status.schedule_text)
                if status.schedule_text
                else tr("result.schedule_cleared")
            ),
        )

    def _open_manage_game(self) -> None:
        """打开当前游戏的管理窗口(重命名/停用/删除/管理存档位置)."""
        game = self._game
        if game is None:
            self._feedback(FeedbackKind.INFO, tr("manage.require_game"))
            return
        backup_path = self.backend.task_status(game.game_id).target_label
        ManageGameWindow(
            self,
            backend=self.backend,
            palette=self.p,
            game_id=game.game_id,
            name=game.name,
            enabled=game.enabled,
            backup_location=backup_path,
            on_change=lambda: self._refresh_after_manage(),
        )

    def _refresh_after_manage(self, *, select: str | None = None) -> None:
        """游戏或存档位置变更后重载列表并保持/恢复选中."""
        games = self.backend.list_games()
        self._render_game_list(games)
        if select is not None:
            self._select_game(select)
            return
        if self._game_id is not None and any(
            game.game_id == self._game_id for game in games
        ):
            self._select_game(self._game_id)
            return
        if games:
            self._select_game(games[0].game_id)
        else:
            self._game_id = None
            self._game = None
            self._show_empty_list()

    def _open_settings(self) -> None:
        task = self.backend.task_status(self._game_id)
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
                shortcut=self._shortcut_text,
                note=tr("dialog.settings_note"),
            ),
        )

    def _on_close(self) -> None:
        """退出前释放调度器与快捷键监听, 避免遗留后台线程."""
        self._hotkeys.shutdown()
        try:
            self.backend.shutdown()
        except Exception as exc:  # pragma: no cover - 退出期异常不阻塞关闭
            print(f"释放后台资源失败: {exc}")
        self.destroy()

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
        self._canceled = False

        def runner() -> None:
            try:
                payload = work()
            except Exception as exc:
                self._messages.put(("err", str(exc)))
            else:
                self._messages.put(("ok", payload))

        threading.Thread(target=runner, daemon=True).start()

    def _poll_messages(self) -> None:
        """主线程轮询队列并分发完成消息与快捷键请求."""
        while True:
            try:
                kind, payload = self._messages.get_nowait()
            except queue.Empty:
                break
            if kind == "hotkey":
                self._on_backup()
                continue
            self._finish_message(kind, payload)
        self._refresh_task()
        self.after(100, self._poll_messages)

    def _finish_message(
        self, kind: Literal["ok", "err", "hotkey"], payload: str
    ) -> None:
        on_ok = self._pending_ok
        self._pending_ok = None
        if kind == "ok":
            if on_ok is not None:
                # 成功回调负责解除忙碌状态并刷新视图
                on_ok(payload)
            else:
                self._set_busy(False)
            return
        self._set_busy(False)
        if self._canceled:
            self._canceled = False
            self._feedback(FeedbackKind.INFO, payload)
        else:
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
    paths: ApplicationPaths | None = None,
) -> int:
    """启动基于 SQLite 的真实后端并进入主循环, 返回退出码."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    paths = ApplicationPaths.default().ensure() if paths is None else paths.ensure()
    database = Database(paths.database_path)
    database.migrate()
    backend: ArchiveService = SqlArchiveService(database, backup_root=paths.backup_root)
    app = ArchiveApp(backend, title=display_name, smoke_seconds=smoke_seconds)
    app.mainloop()
    return 0


def default_theme() -> str:
    """返回默认主题名(供设置显示)."""
    return DEFAULT_THEME
