"""本地游戏发现(已并入游戏主页).

这是主窗口内的一页(不是弹窗): 游戏主页的"游戏发现"分区显示它, 内部分成两个
子页签(默认停在"探测结果"):

- **探测结果**: 从 Steam、Epic、GOG、Ubisoft 的安装清单与注册表, 以及监控
  目录中发现的候选游戏。可按"全部/待处理/已导入/已忽略"筛选, 并对选中项执行
  "导入为游戏 / 忽略(恢复) / 修正路径";
- **监控目录**: 用户自行添加的目录, 用于覆盖平台客户端未安装、未登录或存档路径
  自定义的情况。每行显示实时路径状态(可用/不存在/不是文件夹/不可读/高风险)与
  上次扫描时间, 支持添加、编辑、启用/停用与删除。

"重新扫描"固定放在页签行右侧, 两个子页都能随时触发。这里只做展示与交互编排:
路径校验、重复检测与落库都在后端完成, 失败时把后端给出的原因展示在弹窗与状态
文案里。所有文案来自 i18n, 便于无头测试。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable

import customtkinter as ctk

from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.services.audit import log_action
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import ask_text, confirm_dialog, info_dialog
from archive_management.ui.models import (
    CandidateFilter,
    CandidateItem,
    DiscoveryPage,
    MonitoredDirItem,
)
from archive_management.ui.palette import Palette
from archive_management.ui.pickers import pick_directory

_ChangeCallback = Callable[[], None]

logger = logging.getLogger(__name__)

# 卡片内文字/列表相对卡片边缘的内缩: 与游戏库的卡片保持一致(那里也是 10).
_PANEL_PAD = 10


class DiscoveryPanel:
    """本地游戏发现的页面内容(主窗口的一页, 由游戏主页承载)."""

    def __init__(
        self,
        parent: ctk.CTkFrame,
        *,
        backend: ArchiveService,
        palette: Palette,
        on_change: _ChangeCallback | None = None,
    ) -> None:
        """在 ``parent`` 内构造页面并装载监控目录与探测结果(默认停在"探测结果")."""
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._on_change = on_change
        self._dirs: list[MonitoredDirItem] = []
        self._candidates: list[CandidateItem] = []
        self._dir_rows: dict[str, ctk.CTkFrame] = {}
        self._cand_rows: dict[str, ctk.CTkFrame] = {}
        self._selected_dir: str | None = None
        self._selected_candidate: str | None = None
        # 这个页面的用途是"把发现的游戏加进游戏库", 因此默认只看待处理项.
        self._filter = CandidateFilter.NEW
        self._page: DiscoveryPage = DiscoveryPage.CANDIDATES
        self.frame = ctk.CTkFrame(parent, fg_color=palette.background, corner_radius=0)
        self._build()
        self.reload()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        container = self.frame
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        self._build_tabs(container)
        self._build_pages(container)
        self._build_footer(container)
        self._show_page(self._page)

    def _build_tabs(self, container: ctk.CTkFrame) -> None:
        """构造页签栏: "探测结果" / "监控目录", 右侧固定放"重新扫描".

        页签栏、子页容器与底部状态条都不再加内缩: 外层已按页面边距留白,
        再缩一次会让"游戏发现"比起"游戏库"明显往里挤。
        """
        bar = ctk.CTkFrame(container, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", pady=(4, 8))
        bar.grid_columnconfigure(len(DiscoveryPage), weight=1)
        self._tabs: dict[DiscoveryPage, ctk.CTkButton] = {}
        for index, page in enumerate(DiscoveryPage):
            tab = ctk.CTkButton(
                bar,
                text=page.label,
                command=lambda selected=page: self._show_page(selected),
                width=112,
                height=32,
                corner_radius=8,
                border_width=1,
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            tab.grid(row=0, column=index, padx=(0, 6))
            self._tabs[page] = tab
        self._scan_btn = self._button(
            bar, tr("discovery.scan"), self._on_scan, style="accent", width=104
        )
        self._scan_btn.grid(row=0, column=len(DiscoveryPage), sticky="e")
        self._paint_tabs()

    def _build_pages(self, container: ctk.CTkFrame) -> None:
        """构造两个子页(同格叠放), 由 :meth:`_show_page` 决定显示哪一个."""
        pages = ctk.CTkFrame(container, fg_color="transparent")
        pages.grid(row=2, column=0, sticky="nsew")
        pages.grid_columnconfigure(0, weight=1)
        pages.grid_rowconfigure(0, weight=1)
        self._pages = pages
        self._page_frames = {}
        for page in DiscoveryPage:
            panel = ctk.CTkFrame(
                pages,
                fg_color=self._palette.panel,
                corner_radius=10,
                border_width=1,
                border_color=self._palette.border,
            )
            panel.grid(row=0, column=0, sticky="nsew")
            self._page_frames[page] = panel
        self._build_candidates(self._page_frames[DiscoveryPage.CANDIDATES])
        self._build_dirs(self._page_frames[DiscoveryPage.MONITORED])

    def _build_footer(self, container: ctk.CTkFrame) -> None:
        """底部状态条: 计数/扫描摘要(两个子页共用)."""
        palette = self._palette
        footer = ctk.CTkFrame(container, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        footer.grid_columnconfigure(0, weight=1)
        self._summary_label = ctk.CTkLabel(
            footer,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=palette.text_body,
        )
        self._summary_label.grid(row=0, column=0, sticky="w")
        self._detail_label = ctk.CTkLabel(
            footer,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._detail_label.grid(row=1, column=0, sticky="w")

    def _show_page(self, page: DiscoveryPage) -> None:
        """切换到指定子页并重绘页签(默认页面是"探测结果")."""
        self._page = page
        for kind, frame in self._page_frames.items():
            if kind is page:
                frame.grid()
            else:
                frame.grid_remove()
        self._paint_tabs()

    def _paint_tabs(self) -> None:
        """选中页用强调淡底, 其余页面用普通按钮配色."""
        palette = self._palette
        for page, tab in self._tabs.items():
            if page is self._page:
                tab.configure(
                    fg_color=palette.accent_soft,
                    hover_color=palette.accent_soft,
                    text_color=palette.accent_soft_text,
                    border_color=palette.accent_soft_border,
                )
            else:
                tab.configure(
                    fg_color=palette.raised,
                    hover_color=palette.item_hover,
                    text_color=palette.text_body,
                    border_color=palette.border,
                )

    def _build_dirs(self, panel: ctk.CTkFrame) -> None:
        """监控目录页: 说明 + 增删改按钮 + 整页高度的目录列表."""
        palette = self._palette
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(1, weight=1)

        self._dirs_hint = ctk.CTkLabel(
            panel,
            text=tr("discovery.dirs_hint"),
            anchor="w",
            justify="left",
            wraplength=760,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._dirs_hint.grid(row=0, column=0, padx=_PANEL_PAD, pady=(14, 0), sticky="w")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=0, column=1, padx=_PANEL_PAD, pady=(14, 0), sticky="e")
        self._add_dir_btn = self._button(
            actions, tr("discovery.dir_add"), self._on_add_dir, width=76
        )
        self._edit_dir_btn = self._button(
            actions, tr("discovery.dir_edit"), self._on_edit_dir, width=76
        )
        self._toggle_dir_btn = self._button(
            actions, tr("discovery.dir_disable"), self._on_toggle_dir, width=76
        )
        self._remove_dir_btn = self._button(
            actions,
            tr("discovery.dir_remove"),
            self._on_remove_dir,
            style="danger",
            width=76,
        )
        for index, button in enumerate(
            (
                self._add_dir_btn,
                self._edit_dir_btn,
                self._toggle_dir_btn,
                self._remove_dir_btn,
            )
        ):
            button.grid(row=0, column=index, padx=(0, 6))

        self._dirs_box = ctk.CTkScrollableFrame(
            panel,
            fg_color=palette.well,
            corner_radius=8,
        )
        self._dirs_box.grid(
            row=1, column=0, columnspan=2, padx=_PANEL_PAD, pady=(10, 14), sticky="nsew"
        )
        self._dirs_box.grid_columnconfigure(0, weight=1)

    def _build_candidates(self, panel: ctk.CTkFrame) -> None:
        """探测结果页: 说明 + 筛选 + 导入/忽略/修正路径 + 整页高度的候选列表."""
        palette = self._palette
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        self._hint_label = ctk.CTkLabel(
            panel,
            text=tr("discovery.hint"),
            anchor="w",
            justify="left",
            wraplength=760,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._hint_label.grid(
            row=0, column=0, padx=_PANEL_PAD, pady=(14, 0), sticky="w"
        )

        filters = ctk.CTkFrame(panel, fg_color="transparent")
        filters.grid(row=0, column=1, padx=_PANEL_PAD, pady=(14, 0), sticky="e")
        self._filter_box = ctk.CTkComboBox(
            filters,
            values=[item.label for item in CandidateFilter],
            width=140,
            command=self._on_filter_change,
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
        self._filter_box.set(self._filter.label)
        self._filter_box.pack(side="left")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(
            row=1, column=0, columnspan=2, padx=_PANEL_PAD, pady=(8, 0), sticky="e"
        )
        self._import_btn = self._button(
            actions, tr("discovery.cand_import"), self._on_import, style="accent"
        )
        self._ignore_btn = self._button(
            actions, tr("discovery.cand_ignore"), self._on_ignore
        )
        self._relocate_btn = self._button(
            actions, tr("discovery.cand_relocate"), self._on_relocate
        )
        for index, button in enumerate(
            (self._import_btn, self._ignore_btn, self._relocate_btn)
        ):
            button.grid(row=0, column=index, padx=(6, 0))

        self._cand_box = ctk.CTkScrollableFrame(
            panel,
            fg_color=palette.well,
            corner_radius=8,
        )
        self._cand_box.grid(
            row=2, column=0, columnspan=2, padx=_PANEL_PAD, pady=(10, 14), sticky="nsew"
        )
        self._cand_box.grid_columnconfigure(0, weight=1)

    def _button(
        self,
        parent: ctk.CTkFrame,
        text: str,
        command: Callable[[], None],
        *,
        style: str = "ghost",
        width: int = 88,
    ) -> ctk.CTkButton:
        """按窗口调色板创建一个按钮."""
        palette = self._palette
        colors = {
            "accent": (palette.accent, palette.accent_soft_border, palette.accent_text),
            "danger": (palette.danger, palette.item_hover, palette.danger_text),
            "ghost": (palette.raised, palette.item_hover, palette.text_body),
        }[style]
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=30,
            corner_radius=8,
            fg_color=colors[0],
            hover_color=colors[1],
            text_color=colors[2],
            border_width=1 if style == "ghost" else 0,
            border_color=palette.border,
            font=ctk.CTkFont(size=12, weight="bold"),
        )

    # -- 数据加载与渲染 -----------------------------------------------------

    def reload(self) -> None:
        """重新读取监控目录与探测结果, 并尽量保持选中项.

        读取失败时保留上次内容并记录日志: 本方法在构造过程中也会被调用,
        让异常逃出去等于整个主窗口起不来(磁盘/数据库瞬时不可读时尤其明显)。
        """
        try:
            dirs = self._backend.list_monitored_directories()
            candidates = self._backend.list_candidates()
        except (ArchiveManagementError, sqlite3.Error) as exc:
            self._fail_read(exc)
            return
        self._dirs = dirs
        self._candidates = candidates
        self._render_dirs()
        self._render_candidates()
        self._render_counts()

    def _fail_read(self, exc: Exception) -> None:
        """读取失败: 只记日志与状态文案.

        这里不能弹模态框: 构造阶段与轮询路径都可能走到, 弹窗会挂在事件循环里。
        下次 reload()(切分区/重新扫描)会自行恢复。
        """
        logger.error("读取游戏发现数据失败: %s", exc)
        self._summary_label.configure(
            text=tr("error.read_failed", reason=str(exc)),
            text_color=self._palette.danger,
        )

    def _render_counts(self) -> None:
        """按当前数据刷新底部计数文案(扫描完成后会被结果摘要覆盖).

        第二行按处理进度分列计数: 筛选到"已导入/已忽略"时如果一条都没有, 用
        户可以直接从这行看出原因, 而不会以为筛选坏了。
        """
        counts = self._status_counts()
        self._summary_label.configure(
            text=tr(
                "discovery.counts",
                dirs=len(self._dirs),
                candidates=len(self._candidates),
                pending=counts["new"],
            ),
            text_color=self._palette.text_body,
        )
        self._detail_label.configure(
            text=tr(
                "discovery.counts_detail",
                new=counts["new"],
                imported=counts["imported"],
                ignored=counts["ignored"],
            )
        )

    def _status_counts(self) -> dict[str, int]:
        """按处理进度统计候选数量(空状态文案与底部计数共用)."""
        counts = {"new": 0, "imported": 0, "ignored": 0}
        for item in self._candidates:
            if item.status in counts:
                counts[item.status] += 1
        return counts

    def _render_dirs(self) -> None:
        for child in self._dirs_box.winfo_children():
            child.destroy()
        self._dir_rows = {}
        if not self._dirs:
            empty = ctk.CTkLabel(
                self._dirs_box,
                text=tr("discovery.dirs_empty"),
                anchor="w",
                font=ctk.CTkFont(size=11),
                text_color=self._palette.text_muted,
            )
            empty.pack(fill="x", padx=6, pady=8)
            self._selected_dir = None
            self._update_actions()
            return
        if self._selected_dir not in {item.directory_id for item in self._dirs}:
            self._selected_dir = self._dirs[0].directory_id
        for item in self._dirs:
            row = self._build_dir_row(item)
            row.pack(fill="x", padx=4, pady=3)
            self._dir_rows[item.directory_id] = row
        self._paint_dirs()
        self._update_actions()

    def _build_dir_row(self, item: MonitoredDirItem) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self._dirs_box, corner_radius=8)
        row.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(
            row,
            text=item.path,
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=self._palette.text_body,
        )
        name.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        detail = ctk.CTkLabel(
            row,
            text=item.summary,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self._palette.text_muted,
        )
        detail.grid(row=1, column=0, padx=12, pady=(0, 8), sticky="ew")
        for widget in (row, name, detail):
            widget.bind(
                "<Button-1>",
                lambda _event, key=item.directory_id: self._select_dir(key),
            )
        return row

    def _render_candidates(self) -> None:
        for child in self._cand_box.winfo_children():
            child.destroy()
        self._cand_rows = {}
        visible = [
            item
            for item in self._candidates
            if self._filter is CandidateFilter.ALL or item.status == self._filter.value
        ]
        if not visible:
            self._render_empty_candidates()
            self._selected_candidate = None
            self._update_actions()
            return
        if self._selected_candidate not in {item.candidate_id for item in visible}:
            self._selected_candidate = visible[0].candidate_id
        for item in visible:
            row = self._build_candidate_row(item)
            row.pack(fill="x", padx=4, pady=3)
            self._cand_rows[item.candidate_id] = row
        self._paint_candidates()
        self._update_actions()

    def _render_empty_candidates(self) -> None:
        """候选列表空状态: 区分"还没有候选"与"当前筛选没有匹配项".

        两种情况都给出同一句"暂无探测结果"会让人以为筛选失效(筛选到"已导入"
        而当时一条都没导入时尤其明显), 因此这里分开措辞。
        """
        palette = self._palette
        filtered = bool(self._candidates) and self._filter is not CandidateFilter.ALL
        message = (
            tr("discovery.empty_filtered", filter=self._filter.label)
            if filtered
            else tr("discovery.candidates_empty")
        )
        label = ctk.CTkLabel(
            self._cand_box,
            text=message,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        label.pack(fill="x", padx=6, pady=(8, 0))
        if not filtered:
            return
        counts = self._status_counts()
        hint = ctk.CTkLabel(
            self._cand_box,
            text=tr(
                "discovery.empty_filtered_hint",
                new=counts["new"],
                imported=counts["imported"],
                ignored=counts["ignored"],
            ),
            anchor="w",
            justify="left",
            wraplength=620,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        hint.pack(fill="x", padx=6, pady=(2, 8))

    def _build_candidate_row(self, item: CandidateItem) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self._cand_box, corner_radius=8)
        row.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(
            row,
            text=item.name,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=self._palette.text_body,
        )
        name.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        path = ctk.CTkLabel(
            row,
            text=item.install_dir,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self._palette.text_muted,
        )
        path.grid(row=1, column=0, padx=12, pady=(1, 0), sticky="ew")
        detail = ctk.CTkLabel(
            row,
            text=item.summary,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self._palette.text_muted,
        )
        detail.grid(row=2, column=0, padx=12, pady=(0, 8), sticky="ew")
        for widget in (row, name, path, detail):
            widget.bind(
                "<Button-1>",
                lambda _event, key=item.candidate_id: self._select_candidate(key),
            )
        return row

    def _paint_dirs(self) -> None:
        palette = self._palette
        for key, row in self._dir_rows.items():
            selected = key == self._selected_dir
            row.configure(
                fg_color=palette.item_active if selected else palette.card,
                border_width=1,
                border_color=palette.accent if selected else palette.card_border,
            )

    def _paint_candidates(self) -> None:
        palette = self._palette
        for key, row in self._cand_rows.items():
            selected = key == self._selected_candidate
            row.configure(
                fg_color=palette.item_active if selected else palette.card,
                border_width=1,
                border_color=palette.accent if selected else palette.card_border,
            )

    def _select_dir(self, directory_id: str) -> None:
        self._selected_dir = directory_id
        self._paint_dirs()
        self._update_actions()

    def _select_candidate(self, candidate_id: str) -> None:
        self._selected_candidate = candidate_id
        self._paint_candidates()
        self._update_actions()

    def _on_filter_change(self, value: str) -> None:
        """下拉框选中的是文案, 这里把它映射回枚举后重绘列表."""
        log_action("ui.filter_candidates", basic=True, filter=value)
        for item in CandidateFilter:
            if item.label == value:
                self._filter = item
                break
        self._render_candidates()

    def _dir_item(self) -> MonitoredDirItem | None:
        return next(
            (item for item in self._dirs if item.directory_id == self._selected_dir),
            None,
        )

    def _candidate_item(self) -> CandidateItem | None:
        return next(
            (
                item
                for item in self._candidates
                if item.candidate_id == self._selected_candidate
            ),
            None,
        )

    def _update_actions(self) -> None:
        """按选中项与状态决定按钮可用性与文案."""
        palette = self._palette
        directory = self._dir_item()
        # 添加按钮不依赖选中项; 其余三个按钮需要先选中一个监控目录.
        self._add_dir_btn.configure(state="normal")
        for button in (
            self._edit_dir_btn,
            self._toggle_dir_btn,
            self._remove_dir_btn,
        ):
            button.configure(state="normal" if directory is not None else "disabled")
        if directory is not None:
            self._toggle_dir_btn.configure(
                text=(
                    tr("discovery.dir_disable")
                    if directory.enabled
                    else tr("discovery.dir_enable")
                )
            )
        candidate = self._candidate_item()
        for button in (
            self._import_btn,
            self._ignore_btn,
            self._relocate_btn,
        ):
            button.configure(state="normal" if candidate is not None else "disabled")
        if candidate is not None:
            self._import_btn.configure(
                state="normal" if candidate.importable else "disabled"
            )
            self._ignore_btn.configure(
                text=(
                    tr("discovery.cand_restore")
                    if candidate.status == "ignored"
                    else tr("discovery.cand_ignore")
                )
            )
        self._paint_buttons(palette)

    def _paint_buttons(self, palette: Palette) -> None:
        """禁用态按钮统一用弱化配色, 避免看起来仍可点击."""
        for button in (
            self._add_dir_btn,
            self._edit_dir_btn,
            self._toggle_dir_btn,
            self._remove_dir_btn,
            self._import_btn,
            self._ignore_btn,
            self._relocate_btn,
        ):
            if str(button.cget("state")) == "disabled":
                button.configure(fg_color=palette.raised, text_color=palette.text_muted)
        self._import_btn.configure(
            fg_color=(
                palette.accent
                if str(self._import_btn.cget("state")) == "normal"
                else palette.raised
            )
        )

    # -- 交互 ---------------------------------------------------------------

    def _on_scan(self) -> None:
        """扫描平台安装目录与监控目录(同步执行, 先给出"扫描中"提示)."""
        log_action("ui.scan_candidates", basic=True)
        self._summary_label.configure(
            text=tr("discovery.scanning"), text_color=self._palette.accent
        )
        self._detail_label.configure(text="")
        self.frame.update_idletasks()
        try:
            report = self._backend.scan_candidates()
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._summary_label.configure(text_color=self._palette.text_body)
        self.reload()
        self._summary_label.configure(text=report.label)
        self._detail_label.configure(text=report.detail)

    def _on_add_dir(self) -> None:
        """添加监控目录(可选择"浏览"按钮挑目录)."""
        path = ask_text(
            self.frame,
            self._palette,
            title=tr("dialog.monitor_add_title"),
            text=tr("dialog.monitor_add_prompt"),
            browse=lambda: pick_directory(title=tr("dialog.monitor_add_title")),
        )
        if not path:
            log_action("monitor.add", basic=True, result="cancelled")
            return
        try:
            created = self._backend.add_monitored_directory(path)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._selected_dir = created.directory_id
        self.reload()
        self._summary_label.configure(text=tr("discovery.dir_added", path=created.path))

    def _on_edit_dir(self) -> None:
        """修改监控目录的路径与备注."""
        directory = self._dir_item()
        if directory is None:
            return
        path = ask_text(
            self.frame,
            self._palette,
            title=tr("dialog.monitor_edit_title"),
            text=tr("dialog.monitor_edit_prompt"),
            initial=directory.path,
            browse=lambda: pick_directory(title=tr("dialog.monitor_edit_title")),
        )
        if not path:
            log_action("monitor.update", basic=True, result="cancelled")
            return
        note = ask_text(
            self.frame,
            self._palette,
            title=tr("dialog.monitor_note_title"),
            text=tr("dialog.monitor_note_prompt"),
            initial=directory.note,
            allow_empty=True,
        )
        if note is None:
            return
        try:
            self._backend.update_monitored_directory(
                directory.directory_id, path=path, note=note
            )
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()

    def _on_toggle_dir(self) -> None:
        """启用或停用选中的监控目录."""
        directory = self._dir_item()
        if directory is None:
            return
        try:
            self._backend.set_monitored_enabled(
                directory.directory_id, not directory.enabled
            )
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()

    def _on_remove_dir(self) -> None:
        """删除选中的监控目录(需要确认)."""
        directory = self._dir_item()
        if directory is None:
            return
        if not confirm_dialog(
            self.frame,
            self._palette,
            title=tr("dialog.monitor_remove_title"),
            message=tr("dialog.monitor_remove_message", path=directory.path),
            confirm_text=tr("discovery.dir_remove"),
        ):
            log_action("monitor.remove", basic=True, result="cancelled")
            return
        try:
            self._backend.remove_monitored_directory(directory.directory_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()

    def _on_import(self) -> None:
        """把选中的探测结果导入为游戏, 同步刷新左侧游戏列表."""
        candidate = self._candidate_item()
        if candidate is None or not candidate.importable:
            return
        name = ask_text(
            self.frame,
            self._palette,
            title=tr("dialog.candidate_import_title"),
            text=tr("dialog.candidate_import_prompt"),
            initial=candidate.name,
        )
        if not name:
            log_action("discovery.import", basic=True, result="cancelled")
            return
        try:
            summary = self._backend.import_candidate(candidate.candidate_id, name=name)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()
        self._summary_label.configure(text=tr("result.game_added", name=summary.name))
        if self._on_change is not None:
            self._on_change()

    def _on_ignore(self) -> None:
        """把选中的探测结果标记为已忽略, 或把已忽略的恢复为待处理."""
        candidate = self._candidate_item()
        if candidate is None:
            return
        ignored = candidate.status != "ignored"
        try:
            self._backend.set_candidate_ignored(candidate.candidate_id, ignored)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()

    def _on_relocate(self) -> None:
        """修正选中探测结果的安装路径."""
        candidate = self._candidate_item()
        if candidate is None:
            return
        path = ask_text(
            self.frame,
            self._palette,
            title=tr("dialog.candidate_relocate_title"),
            text=tr("dialog.candidate_relocate_prompt"),
            initial=candidate.install_dir,
            browse=lambda: pick_directory(title=tr("dialog.candidate_relocate_title")),
        )
        if not path:
            log_action("discovery.relocate", basic=True, result="cancelled")
            return
        try:
            self._backend.relocate_candidate(candidate.candidate_id, path)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()

    def _show_error(self, exc: ArchiveManagementError) -> None:
        """把后端给出的原因展示在弹窗与状态文案里."""
        message = str(exc)
        self._summary_label.configure(text=message, text_color=self._palette.danger)
        info_dialog(
            self.frame,
            self._palette,
            title=tr("dialog.error_title"),
            message=message,
        )
