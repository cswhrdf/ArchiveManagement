"""主窗口内的游戏主页.

主页不是弹窗, 而是主窗口内容区里的一页, 并且是**软件打开后默认显示的页面**:
游戏变多以后"哪些游戏需要处理"比"某个游戏有哪些备份"更常用, 因此首页先给全局
视图, 选中游戏再进入详情页(详情页顶栏左侧的"← 游戏主页"可以随时返回)。

页面分两个分区(顶部页签切换):

- **游戏库**(默认): 全部游戏 / 最近活跃 / 待处理 / 已归档四个视图(带数量)加平台、
  类型与名称筛选, 支持**列表**与**海报**两种展示方式, 以及**分页**与**无限滚动**
  两种翻页方式(分页每页条数可调, 默认 30);
- **游戏发现**: 本地游戏探测(探测结果 + 监控目录), 把发现的游戏收进游戏库。

列表模式按列对齐展示每款游戏的核心摘要(名称/平台/存档位置/备份/最近备份/最近
活动/状态标签); 海报模式用封面占位文字 + 名称 + 最近活动时间展示, 适合游戏很多
时快速浏览。两种模式都支持双击某款游戏直接打开详情页。

展示方式、翻页方式与每页条数同筛选条件一起经后端持久化(``home_state``), 因此
重启后仍是上次看到的样子。分类与筛选规则在 :mod:`archive_management.domain.home`,
本页面只负责布局与交互编排。
"""

from __future__ import annotations

import logging
import sqlite3
from collections.abc import Callable
from dataclasses import replace
from math import ceil

import customtkinter as ctk

from archive_management.domain import (
    PAGE_SIZES,
    HomeFilter,
    HomeLayout,
    HomeView,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.services.audit import log_action
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import ask_text, info_dialog
from archive_management.ui.discovery_page import DiscoveryPanel
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import (
    HomeBoard,
    HomeGameItem,
    HomeSection,
    poster_columns,
)
from archive_management.ui.palette import Palette
from archive_management.ui.pickers import pick_directory

_ChangeCallback = Callable[[], None]
_DetailCallback = Callable[[str], None]

logger = logging.getLogger(__name__)

# 列表列: (表头文案 key, 列宽, 文本对齐). 名称列与状态列自适应(最小宽度仅供收窄
# 窗口时兜底), 其余列固定宽度; 所有行与表头使用同一套 grid 列配置, 因此各列上下
# 对齐。总最小宽度 840px, 对应主窗口最小宽度 1200。
_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("home.col_platform", 108, "w"),
    ("home.col_locations", 92, "center"),
    ("home.col_backups", 88, "center"),
    ("home.col_last_backup", 140, "w"),
    ("home.col_activity", 140, "w"),
    ("home.col_state", 150, "w"),
)
# 列之间的横向间距: 数值列与时间列的标题容易读成一串("备份最近备份"), 因此留出
# 明显的空隙, 再配合数值列居中, 每一列都自成一项。
_COLUMN_GAP = 18
# 头像色块基调: 与详情页的概要卡使用同一套映射.
_TONE_KEYS: dict[str, str] = {"orange": "danger", "green": "success"}
_DOT_COLUMN = 28
_NAME_MIN_WIDTH = 130
# 海报卡片尺寸: 固定宽高, 保证封面始终是竖屏(高比宽大); 封面暂时没有图片,
# 用游戏名前两个字代替, 备份数量贴在封面右下角.
_POSTER_WIDTH = 190
# 卡片高度要容下封面(250 + 上下间距 14)、名称(28)与活动时间(28 + 上下间距 12),
# 合计 332; 留一点余量, 否则最后一行文字会被压到底边并盖住卡片的下边框。
_POSTER_HEIGHT = 336
_COVER_HEIGHT = 250


def _sizes_text() -> list[str]:
    """每页条数下拉框的取值."""
    return [str(size) for size in PAGE_SIZES]


class HomePage:
    """游戏主页(主窗口内容区的一页, 不是独立窗口)."""

    def __init__(
        self,
        parent: ctk.CTkFrame,
        *,
        backend: ArchiveService,
        palette: Palette,
        on_change: _ChangeCallback | None = None,
        on_open_detail: _DetailCallback | None = None,
    ) -> None:
        """在 ``parent`` 内构造主页(含游戏发现分区)."""
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._on_change = on_change
        self._on_open_detail = on_open_detail
        self._board: HomeBoard | None = None
        self._rows: dict[str, ctk.CTkFrame] = {}
        self._selected: str | None = None
        self._filter = HomeFilter()
        # 分页当前页(0 基)与每页条数由 HomeFilter.page_size 决定.
        self._page_index = 0
        self._section: HomeSection = HomeSection.LIBRARY
        # 下拉框显示的是"名称 (数量)", 因此需要文案到取值的映射.
        self._origin_keys: dict[str, str] = {}
        self._category_keys: dict[str, str] = {}
        self.frame = ctk.CTkFrame(parent, fg_color=palette.background, corner_radius=0)
        self._build()
        self.reload()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        self.frame.grid_columnconfigure(0, weight=1)
        self.frame.grid_rowconfigure(1, weight=1)
        self._build_sections()

        self._library = ctk.CTkFrame(
            self.frame, fg_color=self._palette.background, corner_radius=0
        )
        self._library.grid(row=1, column=0, sticky="nsew")
        self._library.grid_columnconfigure(0, weight=1)
        self._library.grid_rowconfigure(2, weight=1)
        self._build_toolbar()
        self._build_actions()
        self._build_table()
        self._build_footer()

        self._discovery = DiscoveryPanel(
            self.frame,
            backend=self._backend,
            palette=self._palette,
            on_change=self._after_discovery_change,
        )
        # 与游戏库保持完全相同的页边距: 发现分区的卡片不能贴着窗口边缘.
        self._discovery.frame.grid(
            row=1, column=0, sticky="nsew", padx=24, pady=(0, 14)
        )
        self._discovery.frame.grid_remove()
        self._paint_section_tabs()

    def _build_sections(self) -> None:
        """分区页签: 游戏库 / 游戏发现, 右侧是游戏库的一句话说明."""
        palette = self._palette
        bar = ctk.CTkFrame(self.frame, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 6))
        bar.grid_columnconfigure(len(HomeSection), weight=1)
        self._section_tabs: dict[HomeSection, ctk.CTkButton] = {}
        for index, section in enumerate(HomeSection):
            tab = ctk.CTkButton(
                bar,
                text=section.label,
                command=lambda selected=section: self._show_section(selected),
                width=132,
                height=34,
                corner_radius=8,
                border_width=1,
                font=ctk.CTkFont(size=13, weight="bold"),
            )
            tab.grid(row=0, column=index, padx=(0, 8))
            self._section_tabs[section] = tab
        self._library_hint = ctk.CTkLabel(
            bar,
            text=tr("home.hint"),
            anchor="e",
            justify="right",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._library_hint.grid(row=0, column=len(HomeSection), sticky="e")

    def _build_toolbar(self) -> None:
        """筛选行: 视图页签(带数量) + 平台/类型下拉框 + 名称搜索.

        这一行的控件宽度是**固定的**, 它们的总和就是主页能容纳的最小宽度: 必须放得
        进"支持的最小窗口(主窗口 minsize 1200)下的内容宽度"。否则在比设计尺寸窄的
        屏幕上(例如 CI 的虚拟显示器), 最后一个控件会越出行右边界并盖住描边——
        ``tests/integration/test_gui_layout.py`` 会直接报出来, 改宽度后请同步跑它。
        """
        palette = self._palette
        bar = ctk.CTkFrame(
            self._library,
            fg_color=palette.raised,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(0, 6))
        # 多余宽度全部落在"页签与筛选控件之间"的空白列上: 页签保持左对齐, 平台/
        # 类型下拉与搜索框在右侧紧挨着对齐。把 weight 给筛选控件所在的列会让它在很宽
        # 的单元格里居中(看起来像没对齐), 因此这里留一列不放控件专门吸收空白。
        spacer = len(HomeView)
        bar.grid_columnconfigure(spacer, weight=1)
        self._tabs: dict[HomeView, ctk.CTkButton] = {}
        for index, view in enumerate(HomeView):
            tab = ctk.CTkButton(
                bar,
                text=view.label,
                command=lambda selected=view: self._on_view(selected),
                width=112,
                height=32,
                corner_radius=8,
                border_width=1,
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            tab.grid(row=0, column=index, padx=(10, 6), pady=10)
            self._tabs[view] = tab
        self._origin_box = self._combo(bar, 112, self._on_origin_change)
        self._origin_box.grid(row=0, column=spacer + 1, padx=(0, 8), pady=10)
        self._category_box = self._combo(bar, 136, self._on_category_change)
        self._category_box.grid(row=0, column=spacer + 2, padx=(0, 8), pady=10)

        search = ctk.CTkFrame(bar, fg_color="transparent")
        search.grid(row=0, column=spacer + 3, padx=(0, 12), pady=10)
        self._search_entry = ctk.CTkEntry(
            search,
            width=150,
            height=32,
            placeholder_text=tr("home.search_placeholder"),
            fg_color=palette.input_bg,
            border_color=palette.border,
            text_color=palette.text_body,
        )
        self._search_entry.pack(side="left")
        self._search_entry.bind("<Return>", lambda _event: self._submit_search())
        self._search_btn = self._button(
            search, tr("home.search"), self._submit_search, width=58
        )
        self._search_btn.pack(side="left", padx=(6, 0))
        self._clear_btn = self._button(
            search, tr("home.clear"), self._clear_search, width=58
        )
        self._clear_btn.pack(side="left", padx=(6, 0))

    def _build_actions(self) -> None:
        """操作行: 左侧是展示方式切换, 右侧是选中行的动作按钮."""
        bar = ctk.CTkFrame(self._library, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 6))
        bar.grid_columnconfigure(1, weight=1)

        self._layout_switch = self._segmented(
            bar, [item.label for item in HomeLayout], self._on_layout_change
        )
        self._layout_switch.grid(row=0, column=0, padx=(0, 10))

        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.grid(row=0, column=2, sticky="e")
        self._detail_btn = self._button(
            actions, tr("home.action_detail"), self._on_detail, style="accent", width=96
        )
        self._backup_btn = self._button(
            actions, tr("home.action_backup"), self._on_backup, width=96
        )
        self._location_btn = self._button(
            actions, tr("home.action_location"), self._on_add_location, width=108
        )
        self._manage_btn = self._button(
            actions, tr("home.action_manage"), self._on_manage, width=96
        )
        self._tags_btn = self._button(
            actions, tr("home.action_tags"), self._on_edit_tags, width=88
        )
        self._archive_btn = self._button(
            actions, tr("home.action_archive"), self._on_archive, width=96
        )
        self._action_buttons = (
            self._detail_btn,
            self._backup_btn,
            self._location_btn,
            self._manage_btn,
            self._tags_btn,
            self._archive_btn,
        )
        for index, button in enumerate(self._action_buttons):
            button.grid(row=0, column=index, padx=(6, 0))

    def _build_table(self) -> None:
        """游戏库主体: 固定表头的列表卡片(海报模式复用同一个滚动区)."""
        palette = self._palette
        card = ctk.CTkFrame(
            self._library,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        card.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 6))
        card.grid_columnconfigure(0, weight=1)
        card.grid_rowconfigure(1, weight=1)

        self._head = ctk.CTkFrame(card, fg_color="transparent")
        self._head.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        self._configure_columns(self._head)
        ctk.CTkLabel(
            self._head,
            text=tr("home.col_name"),
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=palette.text_muted,
        ).grid(row=0, column=1, sticky="w")
        for index, (key, _width, anchor) in enumerate(_COLUMNS, start=2):
            ctk.CTkLabel(
                self._head,
                text=tr(key),
                anchor=anchor,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=palette.text_muted,
            ).grid(row=0, column=index, sticky="ew", padx=(0, _COLUMN_GAP))

        self._list_box = ctk.CTkScrollableFrame(
            card, fg_color=palette.well, corner_radius=8
        )
        self._list_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._list_box.grid_columnconfigure(0, weight=1)

    def _build_footer(self) -> None:
        """底栏: 左侧是计数, 右下角是"每页条数 + 翻页"控件."""
        palette = self._palette
        footer = ctk.CTkFrame(self._library, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 14))
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

        self._pager = ctk.CTkFrame(footer, fg_color="transparent")
        self._pager.grid(row=0, column=1, rowspan=2, sticky="e")
        # "每页 N 条"放在翻页按钮左侧: 它只影响分页, 放在一起才读得通.
        self._page_size_label = ctk.CTkLabel(
            self._pager,
            text=tr("home.page_size"),
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._page_size_label.pack(side="left")
        self._page_size_box = ctk.CTkComboBox(
            self._pager,
            values=_sizes_text(),
            width=76,
            height=32,
            command=self._on_page_size_change,
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
        self._page_size_box.pack(side="left", padx=(6, 16))
        self._prev_btn = self._button(
            self._pager, tr("home.prev_page"), self._on_prev_page, width=92
        )
        self._prev_btn.pack(side="left")
        self._page_label = ctk.CTkLabel(
            self._pager,
            text="",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
        )
        self._page_label.pack(side="left", padx=12)
        self._next_btn = self._button(
            self._pager, tr("home.next_page"), self._on_next_page, width=92
        )
        self._next_btn.pack(side="left")

    @staticmethod
    def _configure_columns(frame: ctk.CTkFrame) -> None:
        """给表头与数据行设置同一套列宽, 保证各列上下对齐.

        名称列与最后一列(状态标签)分配剩余宽度: 窗口变宽时表格跟着变宽, 收窄时
        先压缩这两列, 固定列(数量与时间)不会被挤掉。
        """
        frame.grid_columnconfigure(0, minsize=_DOT_COLUMN)
        frame.grid_columnconfigure(1, weight=1, minsize=_NAME_MIN_WIDTH)
        last = len(_COLUMNS) + 1
        for index, (_key, width, _anchor) in enumerate(_COLUMNS, start=2):
            frame.grid_columnconfigure(index, minsize=width)
            if index == last:
                frame.grid_columnconfigure(index, weight=1)

    def _button(
        self,
        parent: ctk.CTkFrame,
        text: str,
        command: Callable[[], None],
        *,
        style: str = "ghost",
        width: int = 96,
    ) -> ctk.CTkButton:
        """按页面调色板创建一个按钮."""
        palette = self._palette
        colors = {
            "accent": (palette.accent, palette.accent_soft_border, palette.accent_text),
            "ghost": (palette.raised, palette.item_hover, palette.text_body),
        }[style]
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=32,
            corner_radius=8,
            fg_color=colors[0],
            hover_color=colors[1],
            text_color=colors[2],
            border_width=1 if style == "ghost" else 0,
            border_color=palette.border,
            font=ctk.CTkFont(size=12, weight="bold"),
        )

    def _segmented(
        self, parent: ctk.CTkFrame, values: list[str], command: Callable[[str], None]
    ) -> ctk.CTkSegmentedButton:
        """创建二选一的切换控件(当前只用于列表/海报展示方式).

        特意不设描边: CustomTkinter 的分段按钮把各段铺满整个控件区域, 库画的
        1px 描边会被段按钮盖住, 只剩下左边/上边可见, 看着像"边框缺了一角"。
        """
        palette = self._palette
        return ctk.CTkSegmentedButton(
            parent,
            values=values,
            command=command,
            width=150,
            height=32,
            corner_radius=8,
            border_width=0,
            fg_color=palette.raised,
            selected_color=palette.accent_soft,
            selected_hover_color=palette.accent_soft,
            unselected_color=palette.raised,
            unselected_hover_color=palette.item_hover,
            text_color=palette.text_body,
            font=ctk.CTkFont(size=12, weight="bold"),
        )

    def _combo(
        self, parent: ctk.CTkFrame, width: int, command: Callable[[str], None]
    ) -> ctk.CTkComboBox:
        """创建筛选下拉框."""
        palette = self._palette
        return ctk.CTkComboBox(
            parent,
            values=[""],
            width=width,
            height=32,
            command=command,
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

    # -- 分区切换 -----------------------------------------------------------

    def _show_section(self, section: HomeSection) -> None:
        """切换主页内部的分区(游戏库 / 游戏发现)."""
        self._section = section
        if section is HomeSection.LIBRARY:
            self._library.grid()
            self._library_hint.grid()
            self._discovery.frame.grid_remove()
        else:
            # 发现分区的数据可能被别处改过(例如手动添加游戏后同名候选会被自动
            # 标记为已入库), 因此每次进入都重新读取一次.
            self._discovery.reload()
            self._discovery.frame.grid()
            self._library.grid_remove()
            self._library_hint.grid_remove()
        self._paint_section_tabs()
        log_action("ui.home_section", basic=True, section=section.value)

    def _paint_section_tabs(self) -> None:
        """选中分区用强调淡底, 其余用普通按钮配色."""
        palette = self._palette
        for section, tab in self._section_tabs.items():
            if section is self._section:
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

    def _after_discovery_change(self) -> None:
        """发现分区改动游戏库后: 重载游戏库并通知主窗口刷新详情页."""
        self.reload()
        if self._on_change is not None:
            self._on_change()

    # -- 主题 ---------------------------------------------------------------

    def apply_palette(self, palette: Palette) -> None:
        """主题切换后按新调色板重建页面(自绘控件不会跟着 UiKit 重绘).

        页面里的控件都是按当前调色板逐一定色的, 换主题时只能重建; 重建后会保持
        当前分区、筛选条件、页码与选中项, 用户的观感与切换前一致。
        """
        if palette is self._palette:
            return
        section = self._section
        selected = self._selected
        page_index = self._page_index
        self._palette = palette
        self.frame.configure(fg_color=palette.background)
        for child in self.frame.winfo_children():
            child.destroy()
        self._rows = {}
        self._build()
        self.reload()
        self._selected = selected
        self._page_index = min(page_index, self._page_count() - 1)
        self._show_section(section)
        self._render()

    # -- 数据加载与渲染 -----------------------------------------------------

    def reload(self) -> None:
        """重新读取主页数据并重绘(进入页面或数据变化后调用).

        读取失败时保留上次画面并记录日志: 主页在窗口构造时就会加载, 异常冒到
        Tk 回调里会把整个窗口带崩, 而这类失败通常是瞬时的(下一次轮询就好了)。
        """
        try:
            board = self._backend.load_home()
        except (ArchiveManagementError, sqlite3.Error) as exc:
            logger.error("读取游戏主页数据失败: %s", exc)
            self._summary_label.configure(
                text=tr("error.read_failed", reason=str(exc)),
                text_color=self._palette.danger,
            )
            return
        self._board = board
        self._filter = board.filter
        self._page_index = 0
        self._render()

    def _apply(self, active: HomeFilter) -> None:
        """应用新的筛选/展示设置(由后端持久化)并重绘."""
        self._board = self._backend.apply_home_filter(active)
        self._filter = self._board.filter
        self._page_index = 0
        self._render()

    def _visible_games(self) -> list[HomeGameItem]:
        """取出当前页要渲染的游戏."""
        board = self._board
        if board is None:  # pragma: no cover - reload 之前不会渲染
            return []
        games = list(board.games)
        start = self._page_index * self._filter.page_size
        return games[start : start + self._filter.page_size]

    def _page_count(self) -> int:
        """分页模式下的总页数(至少 1 页, 便于显示"第 1/1 页")."""
        board = self._board
        total = len(board.games) if board is not None else 0
        return max(1, ceil(total / self._filter.page_size))

    def _render(self) -> None:
        """按当前数据重绘页签、筛选项、游戏列表与底栏."""
        board = self._board
        if board is None:  # pragma: no cover - reload 之前不会渲染
            return
        self._render_tabs(board)
        self._render_filters(board)
        self._render_games()
        self._summary_label.configure(
            text=board.summary, text_color=self._palette.text_body
        )
        self._detail_label.configure(text=board.detail)
        self._update_pager()

    def _render_tabs(self, board: HomeBoard) -> None:
        """视图页签显示"名称 (数量)", 当前视图用强调淡底."""
        palette = self._palette
        current = board.filter.view
        for option, tab in zip(board.views, self._tabs.values(), strict=True):
            tab.configure(text=option.text)
        for view, tab in self._tabs.items():
            if view is current:
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

    def _render_filters(self, board: HomeBoard) -> None:
        """下拉框列出"全部"与各取值(带数量), 并回显当前选择与展示偏好."""
        self._origin_keys = {tr("home.origin_all"): ""}
        self._origin_keys.update({item.text: item.key for item in board.origins})
        self._origin_box.configure(values=list(self._origin_keys))
        self._origin_box.set(board.origin_text)

        self._category_keys = {tr("home.category_all"): ""}
        self._category_keys.update({item.text: item.key for item in board.categories})
        self._category_box.configure(values=list(self._category_keys))
        self._category_box.set(board.category_text)

        if board.filter.search:
            self._search_entry.delete(0, "end")
            self._search_entry.insert(0, board.filter.search)
        self._page_size_box.set(str(board.filter.page_size))
        self._layout_switch.set(board.filter.layout.label)

    def _render_games(self) -> None:
        """重绘游戏列表(列表模式为表格行, 海报模式为卡片网格)."""
        for child in self._list_box.winfo_children():
            child.destroy()
        self._rows = {}
        if self._filter.layout is HomeLayout.LIST:
            # 列表行用 pack 布局, 这里把海报模式关掉的列权重恢复回来.
            self._list_box.grid_columnconfigure(0, weight=1)
            self._head.grid()
        else:
            self._head.grid_remove()
        games = self._visible_games()
        board = self._board
        if not games or board is None:  # pragma: no cover - reload 之后才会渲染
            if board is not None:
                self._render_empty(board)
            self._selected = None
            self._update_actions()
            return
        if self._selected not in {item.game_id for item in games}:
            self._selected = games[0].game_id
        if self._filter.layout is HomeLayout.LIST:
            for item in games:
                row = self._build_row(item)
                row.pack(fill="x", padx=4, pady=3)
                self._rows[item.game_id] = row
        else:
            self._render_posters(games)
        self._paint_rows()
        self._update_actions()

    def _render_posters(self, games: list[HomeGameItem]) -> None:
        """海报模式: 按可用宽度决定每行张数, 再逐行放置卡片.

        列宽固定且**不分配权重**: 否则第 0 列会吸走全部剩余宽度, 卡片被挤到
        行中间偏右, 看起来就不是左对齐了。
        """
        columns = poster_columns(self._list_box.winfo_width() or 1000)
        for index in range(columns):
            self._list_box.grid_columnconfigure(
                index, weight=0, minsize=_POSTER_WIDTH + 8
            )
        for index, item in enumerate(games):
            card = self._build_poster(item)
            card.grid(
                row=index // columns,
                column=index % columns,
                sticky="nw",
                padx=4,
                pady=4,
            )
            self._rows[item.game_id] = card

    def _build_poster(self, item: HomeGameItem) -> ctk.CTkFrame:
        """一张海报卡片: 竖屏封面(文字占位 + 右下角备份数) + 名称 + 最近活动时间."""
        palette = self._palette
        card = ctk.CTkFrame(
            self._list_box,
            width=_POSTER_WIDTH,
            height=_POSTER_HEIGHT,
            corner_radius=8,
        )
        card.grid_propagate(False)
        card.grid_columnconfigure(0, weight=1)
        cover = ctk.CTkFrame(
            card,
            height=_COVER_HEIGHT,
            corner_radius=6,
            fg_color=palette.item_hover,
        )
        cover.grid(row=0, column=0, sticky="nsew", padx=8, pady=(8, 6))
        cover.grid_propagate(False)
        cover.grid_columnconfigure(0, weight=1)
        cover.grid_rowconfigure(0, weight=1)
        # 封面暂时没有图片: 用游戏名前两个字占位.
        placeholder = ctk.CTkLabel(
            cover,
            text=item.name[:2],
            fg_color="transparent",
            text_color=palette.text_primary,
            font=ctk.CTkFont(size=34, weight="bold"),
        )
        placeholder.grid(row=0, column=0, sticky="nsew")
        # 右下角的备份数量角标(与封面同格叠放, 靠右下角).
        badge = ctk.CTkLabel(
            cover,
            text=tr("home.poster_backups", count=item.backup_count),
            corner_radius=6,
            fg_color=palette.panel,
            text_color=(palette.text_body if item.backup_count else palette.text_muted),
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        badge.grid(row=0, column=0, sticky="se", padx=6, pady=6)
        name = ctk.CTkLabel(
            card,
            text=item.name,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        name.grid(row=1, column=0, sticky="ew", padx=10)
        activity = ctk.CTkLabel(
            card,
            text=(
                tr("home.activity", stamp=item.activity_label)
                if item.activity_label
                else tr("home.activity_none")
            ),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        activity.grid(row=2, column=0, sticky="ew", padx=10, pady=(2, 10))
        for widget in (card, cover, placeholder, badge, name, activity):
            widget.bind(
                "<Button-1>", lambda _event, key=item.game_id: self._select(key)
            )
            widget.bind(
                "<Double-Button-1>", lambda _event, key=item.game_id: self._open(key)
            )
        return card

    def _render_empty(self, board: HomeBoard) -> None:
        """空状态: 说明"库里没有游戏"还是"当前筛选没有匹配", 并给出下一步建议."""
        palette = self._palette
        ctk.CTkLabel(
            self._list_box,
            text=board.empty_message,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        ).pack(fill="x", padx=8, pady=(12, 0))
        ctk.CTkLabel(
            self._list_box,
            text=board.empty_hint,
            anchor="w",
            justify="left",
            wraplength=700,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(fill="x", padx=8, pady=(2, 10))

    def _build_row(self, item: HomeGameItem) -> ctk.CTkFrame:
        """一行游戏: 头像点 + 名称 + 各数据列(与表头使用同一套列宽)."""
        palette = self._palette
        row = ctk.CTkFrame(self._list_box, corner_radius=8)
        self._configure_columns(row)
        dot = ctk.CTkLabel(
            row,
            text="●",
            font=ctk.CTkFont(size=14),
            text_color=self._tone_color(item.tone),
        )
        dot.grid(row=0, column=0, padx=(10, 0), pady=8)
        name = ctk.CTkLabel(
            row,
            text=item.name,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        name.grid(row=0, column=1, sticky="ew", padx=(0, 8), pady=8)
        values = (
            (item.platform_label, 2),
            (str(item.location_count), 3),
            (str(item.backup_count), 4),
            (item.last_backup_label or "—", 5),
            (item.activity_label or "—", 6),
            (" · ".join(item.chips), 7),
        )
        widgets: list[ctk.CTkBaseClass] = [row, dot, name]
        for text, column in values:
            anchor = _COLUMNS[column - 2][2]
            label = ctk.CTkLabel(
                row,
                text=text,
                anchor=anchor,
                font=ctk.CTkFont(size=11),
                text_color=(
                    palette.danger if item.risk and column == 7 else palette.text_muted
                ),
            )
            label.grid(row=0, column=column, sticky="ew", padx=(0, _COLUMN_GAP), pady=8)
            widgets.append(label)
        for widget in widgets:
            widget.bind(
                "<Button-1>", lambda _event, key=item.game_id: self._select(key)
            )
            # 双击等同于"打开详情": 与列表类界面的习惯一致.
            widget.bind(
                "<Double-Button-1>", lambda _event, key=item.game_id: self._open(key)
            )
        return row

    def _tone_color(self, tone: str) -> str:
        """把头像基调映射到调色板颜色."""
        palette = self._palette
        key = _TONE_KEYS.get(tone, "accent")
        colors = {
            "accent": palette.accent,
            "danger": palette.danger,
            "success": palette.success,
        }
        return colors[key]

    def _paint_rows(self) -> None:
        """选中项用强调色描边, 其余保持卡片配色."""
        palette = self._palette
        for key, row in self._rows.items():
            selected = key == self._selected
            row.configure(
                fg_color=palette.item_active if selected else palette.card,
                border_width=1,
                border_color=palette.accent if selected else palette.card_border,
            )

    def _update_pager(self) -> None:
        """刷新分页条(页码与上一页/下一页的可用性)."""
        pages = self._page_count()
        self._page_label.configure(
            text=tr("home.page_indicator", page=self._page_index + 1, pages=pages)
        )
        self._prev_btn.configure(state="normal" if self._page_index > 0 else "disabled")
        self._next_btn.configure(
            state="normal" if self._page_index + 1 < pages else "disabled"
        )
        self._paint_disabled()

    def _paint_disabled(self) -> None:
        """禁用态按钮用弱化配色, 避免看起来仍可点击."""
        palette = self._palette
        for button in (self._prev_btn, self._next_btn):
            if str(button.cget("state")) == "disabled":
                button.configure(fg_color=palette.raised, text_color=palette.text_muted)

    def _select(self, game_id: str) -> None:
        """选中一款游戏并刷新动作可用性."""
        self._selected = game_id
        self._paint_rows()
        self._update_actions()

    # -- 状态与动作 ---------------------------------------------------------

    def _item(self) -> HomeGameItem | None:
        """返回当前选中的游戏项."""
        board = self._board
        if board is None or self._selected is None:
            return None
        return next(
            (item for item in board.games if item.game_id == self._selected), None
        )

    def _update_actions(self) -> None:
        """按选中项决定按钮可用性与文案."""
        item = self._item()
        for button in self._action_buttons:
            button.configure(state="normal" if item is not None else "disabled")
        if item is not None:
            # 没有存档位置的游戏无法备份(与详情页的规则保持一致).
            self._backup_btn.configure(
                state="normal" if item.backup_enabled else "disabled"
            )
            self._archive_btn.configure(
                text=tr(
                    "home.action_unarchive" if item.archived else "home.action_archive"
                )
            )
        self._paint_buttons()

    def _paint_buttons(self) -> None:
        """禁用态按钮统一用弱化配色."""
        palette = self._palette
        for button in self._action_buttons:
            if str(button.cget("state")) == "disabled":
                button.configure(fg_color=palette.raised, text_color=palette.text_muted)
        if str(self._detail_btn.cget("state")) == "normal":
            self._detail_btn.configure(
                fg_color=palette.accent, text_color=palette.accent_text
            )

    def _on_view(self, view: HomeView) -> None:
        """切换统一视图."""
        log_action("ui.home_view", basic=True, view=view.value)
        self._apply(replace(self._filter, view=view))

    def _on_layout_change(self, value: str) -> None:
        """切换列表/海报展示方式."""
        for layout in HomeLayout:
            if layout.label == value:
                log_action("ui.home_layout", basic=True, layout=layout.value)
                self._apply(replace(self._filter, layout=layout))
                return

    def _on_page_size_change(self, value: str) -> None:
        """调整每页条数(分页与滚动都从第一页重新开始)."""
        try:
            size = int(value)
        except ValueError:  # pragma: no cover - 下拉框只会给出数字
            return
        self._apply(replace(self._filter, page_size=size))

    def _on_prev_page(self) -> None:
        """上一页."""
        if self._page_index > 0:
            self._page_index -= 1
            self._render_games()
            self._update_pager()

    def _on_next_page(self) -> None:
        """下一页."""
        if self._page_index + 1 < self._page_count():
            self._page_index += 1
            self._render_games()
            self._update_pager()

    def _on_origin_change(self, value: str) -> None:
        """平台下拉框: 把文案映射回取值后重新筛选."""
        key = self._origin_keys.get(value, "")
        if key == self._filter.origin:
            return
        self._selected = None
        self._apply(replace(self._filter, origin=key))

    def _on_category_change(self, value: str) -> None:
        """类型下拉框: 把文案映射回分类取值后重新筛选."""
        key = self._category_keys.get(value, "")
        if key == self._filter.category:
            return
        self._selected = None
        self._apply(replace(self._filter, category=key))

    def _submit_search(self) -> None:
        """按名称搜索(清空输入即恢复全部)."""
        text = self._search_entry.get().strip()
        log_action("ui.home_search", basic=True, length=len(text))
        self._selected = None
        self._apply(replace(self._filter, search=text))

    def _clear_search(self) -> None:
        """清除搜索与筛选条件, 回到默认视图(保留展示方式与每页条数)."""
        self._search_entry.delete(0, "end")
        self._selected = None
        self._apply(
            HomeFilter(
                layout=self._filter.layout,
                page_size=self._filter.page_size,
            )
        )

    def _open(self, game_id: str) -> None:
        """打开某款游戏的详情页(由主窗口切换页面)."""
        log_action("ui.home_open_detail", basic=True, game_id=game_id)
        if self._on_open_detail is not None:
            self._on_open_detail(game_id)

    def _on_detail(self) -> None:
        """打开选中游戏的详情页."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        self._open(item.game_id)

    def _on_backup(self) -> None:
        """立即备份选中的游戏(同步执行, 先给出进行中提示)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        if not item.backup_enabled:
            self._show_error(ArchiveManagementError(tr("error.home_location_required")))
            return
        self._summary_label.configure(
            text=tr("action.backup_pending", name=item.name),
            text_color=self._palette.accent,
        )
        self.frame.update_idletasks()
        try:
            self._backend.run_backup_now(item.game_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        log_action("ui.home_backup", game_id=item.game_id)
        self._refresh()
        self._summary_label.configure(text=tr("home.backed_up", name=item.name))

    def _on_add_location(self) -> None:
        """为选中的游戏添加一个存档位置(目录)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        path = ask_text(
            self.frame,
            self._palette,
            title=tr("dialog.home_location_title"),
            text=tr("dialog.home_location_prompt"),
            browse=lambda: pick_directory(title=tr("dialog.home_location_title")),
        )
        if not path:
            log_action("location.add", basic=True, result="cancelled")
            return
        try:
            self._backend.add_location(item.game_id, path=path, kind="directory")
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        log_action("ui.home_add_location", game_id=item.game_id)
        self._refresh()
        self._summary_label.configure(text=tr("home.location_added", name=item.name))

    def _on_manage(self) -> None:
        """打开游戏管理窗口(存档位置与"修正路径"都在那里)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        backup_location = self._backend.task_status(item.game_id).target_label
        ManageGameWindow(
            self.frame,
            backend=self._backend,
            palette=self._palette,
            game_id=item.game_id,
            name=item.name,
            enabled=item.enabled,
            backup_location=backup_location,
            on_change=self._refresh,
        )

    def _on_edit_tags(self) -> None:
        """编辑选中游戏的自定义标签(逗号分隔)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        value = ask_text(
            self.frame,
            self._palette,
            title=tr("dialog.home_tags_title"),
            text=tr("dialog.home_tags_prompt"),
            initial=", ".join(item.tags),
            allow_empty=True,
        )
        if value is None:
            return
        try:
            self._backend.set_game_tags(
                item.game_id, [part for part in value.split(",")]
            )
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._refresh()
        self._summary_label.configure(text=tr("home.tags_saved", name=item.name))

    def _on_archive(self) -> None:
        """归档或取消归档选中的游戏(不删除任何数据)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        archived = not item.archived
        try:
            self._backend.set_game_archived(item.game_id, archived)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._selected = None if archived else item.game_id
        self._refresh()
        self._summary_label.configure(
            text=tr("home.archived" if archived else "home.unarchived", name=item.name)
        )

    def _refresh(self) -> None:
        """动作完成后同步数据.

        主窗口在场时由它统一刷新(详情页 + 本页), 避免同一份数据重算两次;
        独立使用时(无回调)则自己重绘。
        """
        if self._on_change is not None:
            self._on_change()
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
