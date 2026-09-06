"""CustomTkinter 主窗口.

布局对应 ``docs/archive-management-ui*.svg``:顶部工具栏、左侧游戏/工作区
栏、标题行与概要卡、视图工具条、时间线/分支主面板与右侧 rail、底部反馈
状态条。主题切换不改变布局与操作语义。
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from typing import Literal

import customtkinter as ctk

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

        self._messages: queue.Queue[tuple[Literal["ok", "err"], str]] = queue.Queue()
        self._pending_ok: Callable[[str], None] | None = None
        self._sync_labels: list[ctk.CTkLabel] = []
        self._last_feedback: tuple[FeedbackKind, str] = (FeedbackKind.INFO, "就绪")
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
        sidebar.configure(width=270)
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
            parent, "ARCHIVE / 存档管理", style="primary", size=16, weight="bold"
        )
        brand.grid(row=0, column=1, padx=(0, 8), pady=10)

        self._sync_label = self.kit.label(parent, "", style="muted", size=12)
        self._sync_label.grid(row=0, column=2, sticky="e", padx=8)
        self._sync_labels.append(self._sync_label)

        self.theme_btn = self.kit.button(
            parent,
            "浅色主题",
            style="ghost",
            command=self._on_toggle_theme,
            width=96,
            height=30,
        )
        self.theme_btn.grid(row=0, column=3, padx=(6, 18), pady=10)

    def _build_sidebar(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        section = self.kit.label(
            parent, "我的游戏", style="muted", size=12, weight="bold"
        )
        section.grid(row=0, column=0, padx=22, pady=(18, 6), sticky="w")

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

        add_game = ctk.CTkFrame(parent, corner_radius=8, fg_color="transparent")
        add_game.grid(row=2, column=0, padx=16, pady=(6, 4), sticky="ew")
        self._add_game_label = self.kit.label(
            add_game, "+  添加游戏", style="body", size=13, weight="bold"
        )
        self._add_game_label.pack(fill="x", padx=10, pady=7)
        self._add_game_label.bind("<Button-1>", lambda _e: self._on_add_game())
        self.kit.register(lambda p: add_game.configure(fg_color=p.raised))

        work = self.kit.label(parent, "工作区", style="muted", size=12, weight="bold")
        work.grid(row=3, column=0, padx=22, pady=(14, 4), sticky="w")

        nav = self.kit.frame(parent, bg_key="sidebar", corner_radius=0)
        nav.grid(row=4, column=0, sticky="ew", padx=10, pady=2)
        nav_items = [
            ("全部备份", "查看所有游戏的全部备份时间线"),
            ("定时任务", "查看右侧定时任务状态卡片"),
            ("设置", "打开设置对话框"),
        ]
        for text, message in nav_items:
            row = self.kit.label(nav, text, style="body", size=14)
            row.pack(fill="x", padx=14, pady=4)
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
        self._content.grid_rowconfigure(2, weight=1)

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
            "导出游戏",
            style="accent",
            command=self._on_export,
            width=104,
            height=38,
        )
        self._export_btn.grid(row=0, column=1, rowspan=2, padx=(8, 6))
        self._settings_btn = self.kit.button(
            header,
            "游戏设置",
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
        toolbar.grid(row=2, column=0, sticky="new", padx=24)
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
        self._feedback_label = self.kit.label(statusbar, "就绪", style="muted", size=12)
        self._feedback_label.pack(side="left", padx=18, pady=4)
        self.kit.register(lambda p: self._restyle_feedback(p))

    def _build_hero(self) -> None:
        for col in range(4):
            self._hero.grid_columnconfigure(col, weight=1)
        self._hero_tile = ctk.CTkLabel(
            self._hero,
            text="",
            width=92,
            height=92,
            corner_radius=12,
            fg_color=self._tone_color(None),
            text_color="#ffffff",
            font=ctk.CTkFont(size=30, weight="bold"),
        )
        self._hero_tile.grid(row=0, column=0, rowspan=3, padx=24, pady=18)
        self._hero_name_label = self.kit.label(
            self._hero, "", style="primary", size=20, weight="bold"
        )
        self._hero_name_label.grid(row=0, column=1, sticky="sw", padx=(0, 10))
        self._hero_location_label = self.kit.label(
            self._hero, "", style="body", size=13
        )
        self._hero_location_label.grid(row=1, column=1, sticky="w", padx=(0, 10))
        self._hero_verified_label = self.kit.label(
            self._hero, "", style="primary", size=12, weight="bold"
        )
        self._hero_verified_label.grid(
            row=2, column=1, sticky="w", padx=(0, 10), pady=(4, 14)
        )
        self.kit.register(
            lambda p: self._hero_verified_label.configure(
                text_color=p.success if self._verified else p.danger
            )
        )

        self._stat_recent = self.kit.label(self._hero, "", style="muted", size=12)
        self._stat_recent.grid(row=0, column=2, sticky="sw", padx=10)
        self._stat_recent_value = self.kit.label(
            self._hero, "", style="h2", size=18, weight="bold"
        )
        self._stat_recent_value.grid(row=1, column=2, sticky="w", padx=10)
        self._stat_recent_sub = self.kit.label(self._hero, "", style="muted", size=11)
        self._stat_recent_sub.grid(row=2, column=2, sticky="w", padx=10, pady=(2, 14))

        self._stat_total = self.kit.label(self._hero, "", style="muted", size=12)
        self._stat_total.grid(row=0, column=3, sticky="sw", padx=10)
        self._stat_total_value = self.kit.label(
            self._hero, "", style="h2", size=18, weight="bold"
        )
        self._stat_total_value.grid(row=1, column=3, sticky="w", padx=10)
        self._stat_total_sub = self.kit.label(self._hero, "", style="muted", size=11)
        self._stat_total_sub.grid(row=2, column=3, sticky="w", padx=10, pady=(2, 14))

    def _build_toolbar(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        seg = ctk.CTkSegmentedButton(
            parent,
            values=["时间线", "分支树"],
            command=lambda v: self._on_view_change(str(v)),
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        seg.set("时间线")
        seg.grid(row=0, column=0, padx=14, pady=10, sticky="w")
        self.kit.register(
            lambda p: seg.configure(
                selected_color=p.accent_soft,
                selected_hover_color=p.accent_soft,
                unselected_color=p.raised,
                unselected_hover_color=p.item_hover,
                text_color=p.accent_soft_text,
            )
        )
        self._backup_btn = self.kit.button(
            parent,
            "立即创建备份",
            style="danger",
            command=self._on_backup,
            width=150,
            height=36,
        )
        self._backup_btn.grid(row=0, column=1, padx=14, pady=10)

    def _build_body(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(1, weight=1)

        self._list_panel = self.kit.frame(parent, bg_key="panel", border_key="border")
        self._list_panel.grid(row=0, column=1, sticky="nsew")
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

        rail = self.kit.frame(parent, bg_key="background", corner_radius=0)
        rail.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        rail.grid_columnconfigure(0, weight=1)
        rail.grid_rowconfigure(0, weight=1)
        rail.grid_rowconfigure(1, weight=1)

        self._selected_panel = self.kit.frame(rail, bg_key="panel", border_key="border")
        self._selected_panel.grid(row=0, column=0, sticky="nsew", pady=(0, 8))
        self._selected_panel.grid_columnconfigure(0, weight=1)
        selected_title = self.kit.label(
            self._selected_panel, "选中备份", style="h2", size=15, weight="bold"
        )
        selected_title.grid(row=0, column=0, padx=18, pady=(16, 2), sticky="w")
        self._selected_name = self.kit.label(
            self._selected_panel, "", style="body", size=14, weight="bold"
        )
        self._selected_name.grid(row=1, column=0, padx=18, pady=2, sticky="w")
        self._selected_meta = self.kit.label(
            self._selected_panel, "", style="muted", size=12
        )
        self._selected_meta.grid(row=2, column=0, padx=18, pady=2, sticky="w")
        self._selected_files = self.kit.label(
            self._selected_panel, "", style="body", size=12
        )
        self._selected_files.grid(row=3, column=0, padx=18, pady=(10, 2), sticky="w")
        self._restore_btn = self.kit.button(
            self._selected_panel,
            "恢复到此节点",
            style="danger",
            command=self._on_restore,
            width=150,
        )
        self._restore_btn.grid(row=4, column=0, padx=18, pady=(8, 4), sticky="w")
        self._branch_btn = self.kit.button(
            self._selected_panel,
            "从此处创建分支",
            style="ghost",
            command=self._on_branch,
            width=150,
        )
        self._branch_btn.grid(row=5, column=0, padx=18, pady=(0, 12), sticky="w")

        self._task_panel = self.kit.frame(rail, bg_key="panel", border_key="border")
        self._task_panel.grid(row=1, column=0, sticky="nsew", pady=(8, 0))
        self._task_panel.grid_columnconfigure(0, weight=1)
        task_title = self.kit.label(
            self._task_panel, "定时任务", style="h2", size=15, weight="bold"
        )
        task_title.grid(row=0, column=0, padx=18, pady=(16, 4), sticky="w")
        self._task_name_label = self.kit.label(
            self._task_panel, "", style="body", size=13, weight="bold"
        )
        self._task_name_label.grid(row=1, column=0, padx=18, sticky="w")
        self._task_state_label = self.kit.label(
            self._task_panel, "", style="muted", size=12
        )
        self._task_state_label.grid(row=2, column=0, padx=18, sticky="w")
        self._task_progress = ctk.CTkProgressBar(
            self._task_panel, height=8, corner_radius=4
        )
        self._task_progress.grid(row=3, column=0, padx=18, pady=6, sticky="ew")
        self.kit.register(
            lambda p: self._task_progress.configure(
                fg_color=p.input_bg, progress_color=p.accent
            )
        )
        self._task_next = self.kit.label(self._task_panel, "", style="muted", size=12)
        self._task_next.grid(row=4, column=0, padx=18, sticky="w")
        self._task_shortcut = self.kit.label(
            self._task_panel, "", style="muted", size=12
        )
        self._task_shortcut.grid(row=5, column=0, padx=18, pady=(2, 6), sticky="w")
        edit_btn = self.kit.button(
            self._task_panel,
            "编辑任务设置  →",
            style="ghost",
            command=self._on_edit_task,
            width=130,
            height=28,
        )
        edit_btn.grid(row=6, column=0, padx=18, pady=(4, 14), sticky="w")

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
        row = ctk.CTkFrame(
            self._games_container,
            corner_radius=10,
            fg_color=self.p.item_hover if False else self.p.raised,
        )
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
        icon.grid(row=0, column=0, rowspan=2, padx=(12, 10), pady=10)
        name = ctk.CTkLabel(
            row,
            text=game.name,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=self.p.text_primary,
        )
        name.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=(9, 0))
        detail = ctk.CTkLabel(
            row,
            text=game.list_detail,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self.p.text_muted,
        )
        detail.grid(row=1, column=1, sticky="ew", padx=(0, 8), pady=(0, 8))
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
            row.configure(fg_color=palette.item_active)
            name.configure(text_color=palette.text_primary)
            detail.configure(text_color=palette.text_muted)
        else:
            row.configure(fg_color=palette.raised)
            name.configure(text_color=palette.text_body)
            detail.configure(text_color=palette.text_muted)

    def _refresh_sync(self) -> None:
        for label in self._sync_labels:
            label.configure(text="上次同步  2026/09/06  09:42")

    def _show_empty_list(self) -> None:
        self._list_title.configure(text="备份")
        self._list_sub.configure(text="当前没有可展示的游戏,请先添加游戏。")
        for child in self._list_scroll.winfo_children():
            child.destroy()
        empty = self.kit.label(
            self._list_scroll, "空状态:暂无备份记录", style="muted", size=13
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
            self._feedback(
                FeedbackKind.INFO, "该游戏未配置存档位置,请先手动添加存档路径。"
            )

    def _render_hero(self, detail: GameDetail) -> None:
        self._verified = detail.location_verified
        self._hero_name_label.configure(text=detail.name)
        location = detail.main_location or detail.location_note
        self._hero_location_label.configure(
            text=f"{detail.location_note}  ·  {location}"
        )
        self._hero_verified_label.configure(
            text="路径已验证" if self._verified else "路径未验证/未配置"
        )
        self._stat_recent.configure(text="最近备份")
        self._stat_recent_value.configure(text=detail.last_backup_label)
        self._stat_recent_sub.configure(text=detail.last_backup_sub)
        self._stat_total.configure(text="备份总数")
        self._stat_total_value.configure(text=detail.total_backups_label)
        self._stat_total_sub.configure(text=detail.total_backups_sub)

    def _render_toolbar_header(self, detail: GameDetail) -> None:
        self._title_label.configure(text=detail.name)
        self._subtitle_label.configure(text=detail.subtitle)

    def _render_task(self, task: TaskStatus) -> None:
        state = "运行中" if task.running else "已暂停"
        self._task_name_label.configure(text=f"{task.task_name}    {state}")
        self._task_state_label.configure(text="自动备份服务")
        self._task_progress.set(task.progress)
        self._task_next.configure(
            text=f"下次运行  {task.next_run_label}    目标  {task.target_label}"
        )
        self._task_shortcut.configure(
            text=f"快捷键  {task.shortcut_label}    主题  {self._theme}"
        )
        self._status_title.configure(text="备份服务正常")
        self._status_sub.configure(text=f"磁盘剩余 186 GB · {task.shortcut_label}")

    def _render_list(self) -> None:
        game_id = self._game_id or ""
        self._items = self.backend.list_backups(game_id)
        if self._view == ViewKind.TIMELINE:
            ordered = timeline_order(self._items)
            title, sub = "备份时间线", "按创建时间倒序排列,点击节点查看文件快照"
        else:
            ordered = timeline_order(self._items)
            title, sub = "分支树", "树形关系视图(阶段 D 接入完整分支关系)"
        self._list_title.configure(text=title)
        self._list_sub.configure(text=sub)
        for child in self._list_scroll.winfo_children():
            child.destroy()
        if not ordered:
            empty = self.kit.label(
                self._list_scroll, "空状态:暂无备份记录", style="muted", size=13
            )
            empty.pack(padx=10, pady=16)
            return
        self._cards: dict[str, ctk.CTkFrame] = {}
        for item in ordered:
            card = self._build_backup_card(item)
            card.pack(fill="x", padx=4, pady=3)
            self._cards[item.backup_id] = card
            card.bind(
                "<Button-1>",
                lambda _e, i=item: self._select_backup(i),
            )
        if self._backup_id and self._backup_id in self._cards:
            self._paint_cards()

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
            card.configure(fg_color=palette.accent_soft)
            title.configure(text_color=palette.accent_soft_text)
        else:
            card.configure(fg_color=palette.raised)
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
            self._selected_name.configure(text="未选择节点")
            self._selected_meta.configure(text="在左侧列表点击一个备份节点")
            self._selected_files.configure(text="")
            self._restore_btn.configure(state="disabled")
            self._branch_btn.configure(state="disabled")
            return
        self._selected_name.configure(text=item.title)
        self._selected_meta.configure(
            text=f"{item.created_label}  ·  {item.branch_label}"
        )
        self._selected_files.configure(
            text=f"内容摘要:{item.size_label} · SHA-256 已验证"
        )

    # ---------------------------------------------------------------- 操作

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
        self._feedback(FeedbackKind.PENDING, f"正在创建「{game.name}」的备份…")

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
            title="恢复确认",
            message=(
                f"将把「{game.name}」恢复到备份节点,当前原始目录将被覆盖。"
                "此操作不可逆,是否继续?"
            ),
            confirm_text="确认恢复",
        )
        if not confirmed:
            self._feedback(FeedbackKind.INFO, "已取消恢复")
            return
        self._set_busy(True)
        backup_id = self._backup_id
        self._feedback(FeedbackKind.PENDING, "正在恢复备份…")

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
        branch_name = ask_branch_name(self, title="创建分支", text="请输入分支名称:")
        if not branch_name:
            self._feedback(FeedbackKind.INFO, "已取消创建分支")
            return
        self._set_busy(True)
        backup_id = self._backup_id
        self._feedback(FeedbackKind.PENDING, f"正在创建分支「{branch_name}」…")

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
        self._feedback(FeedbackKind.PENDING, f"正在导出「{game.name}」…")

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
        self.theme_btn.configure(
            text="深色主题" if self._theme == "light" else "浅色主题"
        )
        self.kit.apply(self.p)
        self._feedback(
            FeedbackKind.INFO,
            f"已切换到{('浅色' if self._theme == 'light' else '深色')}主题",
        )

    def _on_view_change(self, label: str) -> None:
        self._view = ViewKind.TIMELINE if label == "时间线" else ViewKind.BRANCH
        self._render_list()

    def _on_add_game(self) -> None:
        self._feedback(FeedbackKind.INFO, "添加游戏将在阶段 C 实现")

    def _on_nav(self, name: str, message: str) -> None:
        if name == "设置":
            self._open_settings()
        else:
            self._feedback(FeedbackKind.INFO, message)

    def _on_game_settings(self) -> None:
        self._open_settings()

    def _on_edit_task(self) -> None:
        self._open_settings()

    def _open_settings(self) -> None:
        task = self.backend.task_status()
        info_dialog(
            self,
            self.p,
            title="设置",
            message=(
                f"主题:{self._theme}\n"
                f"定时任务:{task.task_name}({'运行中' if task.running else '已暂停'})\n"
                f"下次运行:{task.next_run_label}\n"
                f"快捷键:{task.shortcut_label}\n\n"
                "完整设置(主题、快捷键、备份目录、调度器)将在后续阶段接入。"
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
