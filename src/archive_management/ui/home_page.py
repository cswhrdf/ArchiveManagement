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

import contextlib
import logging
import sqlite3
import tkinter as tk
from collections.abc import Callable
from dataclasses import dataclass, replace
from math import ceil

import customtkinter as ctk
from PIL import Image

from archive_management.domain import (
    PAGE_SIZES,
    ArtworkKind,
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
from archive_management.ui.textfit import fit_text

_ChangeCallback = Callable[[], None]
_DetailCallback = Callable[[str], None]
_NoticeCallback = Callable[[str], None]

logger = logging.getLogger(__name__)

# 固定列: (表头文案 key, 列宽, 文本对齐). 这些列的内容长短可预期(平台名、数字、
# 时间、状态标签), 因此宽度固定, 并且**整块贴靠界面右侧**; 除它们之外的所有宽度
# 都留给游戏名称 —— 名称的长度无法预期, 而且越长越有价值。状态列取 200px: 标签是
# 应用自己拼的(平台 + 是否已备份 + 是否来自探测 + 风险 + 归档), 够放三个。
_COLUMNS: tuple[tuple[str, int, str], ...] = (
    ("home.col_platform", 108, "w"),
    ("home.col_locations", 92, "center"),
    ("home.col_backups", 88, "center"),
    ("home.col_last_backup", 140, "w"),
    ("home.col_activity", 140, "w"),
    ("home.col_state", 200, "w"),
)
# 列之间的横向间距: 数值列与时间列的标题容易读成一串("备份最近备份"), 因此留出
# 明显的空隙, 再配合数值列居中, 每一列都自成一项。
_COLUMN_GAP = 18
# 头像色块基调: 与详情页的概要卡使用同一套映射.
_TONE_KEYS: dict[str, str] = {"orange": "danger", "green": "success"}
_DOT_COLUMN = 28
# 表头与数据行的左右内边距: 左边给色块、右边给固定列块, 两侧结构一致才能上下对齐.
_TABLE_SIDE_PAD = 10
# 名称块与固定列块之间的最小空隙.
_TABLE_BLOCK_GAP = 12
# 表头左边距 = 滚动区 10 + 数据行自己的 4(pack padx): 表头与数据行落在同一条竖线上.
_HEAD_LEFT_PAD = _TABLE_SIDE_PAD + 4
# 色块与名称之间的空隙(表头与数据行一致).
_NAME_PAD = 8
# 名称的初始宽度: 真正显示多少字由名称块的**实际宽度**决定(见 _refit_row_names),
# 窗口变宽就多显示几个字; 这个值只在控件尺寸还没测量出来时(启动首屏)兜底。
_NAME_FALLBACK_WIDTH = 200
# 拖窗口时把"表头对齐 + 名称重裁"合并成一次: 每个像素都跑一遍会卡.
_SYNC_DELAY_MS = 60
# 表头对齐最多迭代几轮(表头几何变化不会触发滚动区的 Configure, 得主动再量一轮).
_ALIGN_MAX_ATTEMPTS = 4
# 名称重裁在"宽度还没测量出来"时最多重试几轮. 各平台的布局时序不同: Linux/macOS
# 上首次同步时行内标签可能还没被布局(``winfo_width()`` 是 1), 而之后再没有 Configure
# 事件来补救 —— 不重试就会一直停在兜底宽度的短文本上。
_REFIT_MAX_ATTEMPTS = 8
# 海报卡片尺寸: 固定宽高, 保证封面始终是竖屏(高比宽大); 封面暂时没有图片,
# 用游戏名前两个字代替, 备份数量贴在封面右下角.
_POSTER_WIDTH = 190
# 卡片高度要容下封面(250 + 上下间距 14)、名称(28)与活动时间(28 + 上下间距 12),
# 合计 332; 留一点余量, 否则最后一行文字会被压到底边并盖住卡片的下边框。名称放
# 不下时最多折两行(仍在 336 之内, 因此卡片尺寸不变)。
_POSTER_HEIGHT = 336
_COVER_HEIGHT = 250
# 封面宽度 = 卡片宽度去掉左右各 8px 内缩(与 :meth:`HomePage._build_poster` 一致).
_COVER_WIDTH = _POSTER_WIDTH - 16
# 列表行头像里的图标边长(与 :data:`_DOT_COLUMN` 留出内缩).
_ICON_SIZE = 22
# 海报卡片在网格里的占位宽度(卡片 + 左右各 4 的间距).
_POSTER_SLOT_WIDTH = _POSTER_WIDTH + 8
# 海报卡片里名称的可用宽度: 卡片宽减左右各 10 的内边距.
_POSTER_TEXT_WIDTH = _POSTER_WIDTH - 20
_POSTER_NAME_LINES = 2
# 滚动区宽度还没测量出来时的兜底宽度(启动首屏的 winfo_width() 只有 1): 否则首帧
# 会按"很窄的窗口"算列数, 4 款游戏被排成两行 —— 首帧之后 _on_frame_resize 会用
# 真实宽度重排一次。
_POSTER_FALLBACK_WIDTH = 1000


def _sizes_text() -> list[str]:
    """每页条数下拉框的取值."""
    return [str(size) for size in PAGE_SIZES]


def _cell_pad(index: int) -> tuple[int, int] | int:
    """固定列块里第 ``index`` 列的左右间距: 最后一列不留右侧空隙(它贴右边界)."""
    return (0, _COLUMN_GAP) if index < len(_COLUMNS) - 1 else 0


@dataclass
class _RowParts:
    """一行的关键部件: 表头对齐与名称重裁都要用到(不必再从控件树里找)."""

    # 名称块(色块 + 名称)与固定列块(右侧那一排固定列).
    name_block: ctk.CTkFrame
    columns: ctk.CTkFrame
    # 名称标签与它的**完整**名称(标签上只放裁剪后的文本).
    label: ctk.CTkLabel
    full_name: str
    # 上次裁到多少像素(0 = 还没裁过): 宽度没变就不重设文本, 免得反复改文本触发新的
    # Configure 互相追着跑。
    fitted_width: int = 0


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
        on_notice: _NoticeCallback | None = None,
    ) -> None:
        """在 ``parent`` 内构造主页(含游戏发现分区)."""
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._on_change = on_change
        self._on_open_detail = on_open_detail
        self._on_notice = on_notice
        self._board: HomeBoard | None = None
        self._rows: dict[str, ctk.CTkFrame] = {}
        # 封面/图标: CTkImage 必须被持有引用, 否则会被垃圾回收成空白.
        self._artwork_images: dict[str, ctk.CTkImage] = {}
        self._selected: str | None = None
        # 每行的关键部件: 名称按真实宽度重裁、表头对齐都要用(键是游戏 id).
        self._row_parts: dict[str, _RowParts] = {}
        # 表头的左右内边距(会按实测差值调整, 见 _align_table_header).
        self._head_pads: tuple[int, int] = (_HEAD_LEFT_PAD, _TABLE_SIDE_PAD)
        # 表头对齐已经调过几轮(收敛后就归零).
        self._align_attempts = 0
        # 名称重裁因"宽度还没量出来"重试过几轮(每轮渲染重新计数).
        self._refit_attempts = 0
        # 与行内标签同规格的字体对象, 用来量文本宽度(CTkFont 本身就是 Tk 字体).
        self._name_font = ctk.CTkFont(size=13, weight="bold")
        self._value_font = ctk.CTkFont(size=11)
        # 延后的列表同步任务与海报模式当前的每行张数/已配置列数.
        self._sync_job: str | None = None
        self._poster_columns = 0
        self._poster_slots = 0
        self._filter = HomeFilter()
        # 分页当前页(0 基)与每页条数由 HomeFilter.page_size 决定.
        self._page_index = 0
        self._section: HomeSection = HomeSection.LIBRARY
        # 下拉框显示的是"名称 (数量)", 因此需要文案到取值的映射.
        self._origin_keys: dict[str, str] = {}
        self._category_keys: dict[str, str] = {}
        self.frame = ctk.CTkFrame(parent, fg_color=palette.background, corner_radius=0)
        # 帧销毁时撤掉挂起的同步任务: after 回调打到已销毁的控件上会在 stderr 里留下
        # `invalid command name ...` 噪声(报告里会挂到下一个用例的 stderr 附件上)。
        self.frame.bind("<Destroy>", self._on_frame_destroyed, add="+")
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
            on_notice=self._on_notice,
        )
        # 与游戏库保持完全相同的页边距: 发现分区的卡片不能贴着窗口边缘.
        self._discovery.frame.grid(
            row=1, column=0, sticky="nsew", padx=24, pady=(0, 14)
        )
        self._discovery.frame.grid_remove()
        self._paint_section_tabs()

    def _build_sections(self) -> None:
        """分区页签: 游戏库 / 游戏发现.

        这里不再放“本页怎么用”的长说明: 文案在窄窗口里容易被裁掉, 而且页面应当是
        清爽的操作区 —— 相应的说明统一收在 ``docs/ui-notes.md``(待并入 wiki)。
        """
        bar = ctk.CTkFrame(self.frame, fg_color="transparent")
        bar.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 6))
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
        self._enable_btn = self._button(
            actions, tr("home.action_enable"), self._on_toggle_enabled, width=88
        )
        self._action_buttons = (
            self._detail_btn,
            self._backup_btn,
            self._location_btn,
            self._manage_btn,
            self._tags_btn,
            self._enable_btn,
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
        # 左边距要跟数据行一致(滚动区 10 + 行自己的 4); 右边距随后由
        # _align_table_header() 按实测差值补齐 —— 表头在卡片里, 数据行在滚动区里
        # (滚动条还占掉几十像素), 不补齐的话"贴右"的固定列会与表头整体错开。
        self._head.grid(
            row=0,
            column=0,
            sticky="ew",
            padx=self._head_pads,
            pady=(10, 6),
        )
        # 表头与数据行是同一套结构: 左侧"名称块"(自适应宽度) + 右侧"固定列块"(贴右).
        self._head_columns = ctk.CTkFrame(self._head, fg_color="transparent")
        self._head_columns.pack(side="right", padx=(_TABLE_BLOCK_GAP, _TABLE_SIDE_PAD))
        self._configure_columns(self._head_columns)
        for index, (key, _width, anchor) in enumerate(_COLUMNS):
            ctk.CTkLabel(
                self._head_columns,
                text=tr(key),
                anchor=anchor,
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=palette.text_muted,
            ).grid(row=0, column=index, sticky="ew", padx=_cell_pad(index))
        head_name = ctk.CTkFrame(self._head, fg_color="transparent")
        self._head_name = head_name
        head_name.pack(side="left", fill="x", expand=True, padx=(_TABLE_SIDE_PAD, 0))
        # 名称列头要从色块之后开始: 与数据行共用同一个色块宽度, 两边自然对齐.
        ctk.CTkLabel(
            head_name,
            text="",
            width=_DOT_COLUMN,
            font=ctk.CTkFont(size=11, weight="bold"),
        ).pack(side="left")
        ctk.CTkLabel(
            head_name,
            text=tr("home.col_name"),
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=palette.text_muted,
        ).pack(side="left", padx=(_NAME_PAD, 0))

        self._list_box = ctk.CTkScrollableFrame(
            card, fg_color=palette.well, corner_radius=8
        )
        self._list_box.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 10))
        self._list_box.grid_columnconfigure(0, weight=1)
        # 宽度变化时重排海报: 启动首屏渲染时控件尺寸还没测量出来
        # (``winfo_width()`` 只有 1), 算出的列数会偏少 —— 4 款游戏会被排成两行。
        self._list_box.bind("<Configure>", self._on_list_box_resize)

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
    def _configure_columns(block: ctk.CTkFrame) -> None:
        """给"固定列块"设置列宽: 表头与数据行共用同一套, 因此各列一定上下对齐.

        列宽固定且**不分配权重**: 整块贴右, 宽度由这些列求和而来; 文本再用 fit_text
        封在自己的列宽以内, 于是内容长短也不会把列推动一下。
        """
        for index, (_key, width, _anchor) in enumerate(_COLUMNS):
            block.grid_columnconfigure(index, minsize=width)

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
            self._discovery.frame.grid_remove()
        else:
            # 发现分区的数据可能被别处改过(例如手动添加游戏后同名候选会被自动
            # 标记为已入库), 因此每次进入都重新读取一次.
            self._discovery.reload()
            self._discovery.frame.grid()
            self._library.grid_remove()
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
        self._artwork_images = {}
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

    def refresh_artwork(self) -> None:
        """数据版本变化后重绘主页, 让后台补好的封面/图标与译名显示出来.

        不重读筛选条件、页码与选中项, 也不碰底部提示条(它承载着刚做完的操作结果);
        但游戏名可能刚被译名改写, 所以顺手重读一次主页数据。
        """
        self._artwork_images = {}
        try:
            self._board = self._backend.load_home()
        except (ArchiveManagementError, sqlite3.Error) as exc:
            logger.debug("重读主页数据失败: %s", exc)
        if self._board is not None:
            self._render_games()

    def _render_games(self) -> None:
        """重绘游戏列表(列表模式为表格行, 海报模式为卡片网格)."""
        for child in self._list_box.winfo_children():
            child.destroy()
        self._rows = {}
        self._row_parts = {}
        self._align_attempts = 0
        self._refit_attempts = 0
        if self._filter.layout is HomeLayout.LIST:
            # 列表行用 pack 布局: 先把海报模式留下的列宽清干净.
            self._apply_poster_columns(0)
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
            # 名称按**实际可用宽度**裁剪, 而宽度要等布局完成才知道 —— 这里先排一次
            # (用兜底宽度), 首帧之后再精确重裁一次。
            self._schedule_list_sync()
        else:
            self._render_posters(games)
        self._paint_rows()
        self._update_actions()

    def _render_posters(self, games: list[HomeGameItem]) -> None:
        """海报模式: 按可用宽度决定每行张数, 再逐行放置卡片.

        列宽固定且**不分配权重**: 否则第 0 列会吸走全部剩余宽度, 卡片被挤到
        行中间偏右, 看起来就不是左对齐了。
        """
        columns = self._poster_capacity()
        self._poster_columns = columns
        self._apply_poster_columns(columns)
        for item in games:
            self._rows[item.game_id] = self._build_poster(item)
        self._place_cards()

    def _poster_capacity(self) -> int:
        """按滚动区当前宽度算每行能放几张海报(宽度还没测量出来时用兜底值)."""
        width = self._list_box.winfo_width()
        return poster_columns(width if width > 1 else _POSTER_FALLBACK_WIDTH)

    def _apply_poster_columns(self, columns: int) -> None:
        """把前 ``columns`` 列设成固定列宽, 并清掉不再使用的列(收窄窗口时要用)."""
        for index in range(max(columns, self._poster_slots)):
            self._list_box.grid_columnconfigure(
                index,
                weight=0,
                minsize=_POSTER_SLOT_WIDTH if index < columns else 0,
            )
        self._poster_slots = columns

    def _place_cards(self) -> None:
        """按当前每行张数把已建好的卡片重新摆放一遍(启动首屏与窗口缩放都会用到)."""
        columns = self._poster_columns
        for index, card in enumerate(self._rows.values()):
            card.grid(
                row=index // columns,
                column=index % columns,
                sticky="nw",
                padx=4,
                pady=4,
            )

    def _on_list_box_resize(self, event: tk.Event) -> None:
        """滚动区宽度变化时按新宽度重排内容.

        海报: 列数由可用宽度决定, 而**启动首屏**渲染时控件尺寸还没测量出来
        (``winfo_width()`` 只有 1), 于是 4 款游戏会被排成两行; 首帧布局完成后这里
        按真实宽度再排一次。宽度用事件自带的值 —— 事件到达时 ``winfo_width()``
        可能还停在上一轮的尺寸。

        列表: 名称块吸收的宽度变了, 表头也要重新对齐(见 :meth:`_sync_list_layout`)。
        """
        if self._filter.layout is HomeLayout.POSTER:
            if self._rows:
                self._relayout_posters(event.width)
            return
        if self._rows:
            self._schedule_list_sync()

    def _relayout_posters(self, width: int) -> None:
        """按给定宽度重算每行张数, 变了就重新摆放卡片(启动首屏与窗口缩放都会用到)."""
        columns = poster_columns(width)
        if columns == self._poster_columns:
            return
        self._poster_columns = columns
        self._apply_poster_columns(columns)
        self._place_cards()

    def _schedule_list_sync(self) -> None:
        """延后合并一次"表头对齐 + 名称重裁": 已排过队就直接返回.

        **不取消也不重新计时**。改成"每次请求先取消再重新计时"看起来更平滑, 但事件流
        只要足够密集(无窗口管理器的 Xvfb、CustomTkinter 的延迟重绘、滚动区尺寸连续变化),
        任务就会被无限推后 —— CI 上 Linux/macOS 的名称重裁就是这么被饿死的: 现场里
        ``_refit_attempts=1`` 而且任务仍挂在队列里, 名称停在兜底宽度的短文本上。
        合并(而不是推后)能保证它一定在 :data:`_SYNC_DELAY_MS` 内跑一次, 拿到的也是最
        新几何; 拖窗口时约 16 次/秒的重算代价可以接受。
        """
        if self._sync_job is not None:
            return
        self._sync_job = self.frame.after(_SYNC_DELAY_MS, self._sync_list_layout)

    def _on_frame_destroyed(self, event: tk.Event) -> None:
        """宿主帧被销毁时撤掉挂起的同步任务(子控件的 Destroy 事件会冒泡上来, 要过滤)."""
        if event.widget is not self.frame:
            return
        self.cancel_list_sync()

    def cancel_list_sync(self) -> None:
        """撤掉挂起的"表头对齐 + 名称重裁"任务(控件销毁前调用)."""
        if self._sync_job is None:
            return
        with contextlib.suppress(tk.TclError):
            self.frame.after_cancel(self._sync_job)
        self._sync_job = None

    def _sync_list_layout(self) -> None:
        """把表头对齐到数据行, 并按名称块的**实际宽度**重新裁剪每行的名称."""
        self._sync_job = None
        if self._filter.layout is not HomeLayout.LIST or not self._rows:
            return
        self._align_table_header()
        self._refit_row_names()

    def _align_table_header(self) -> None:
        """把表头的内容对齐到数据行.

        表头在卡片里, 而数据行在滚动区里(滚动条与内边距还占掉几十像素), 两者宽度
        并不相等; 不补的话左边"名称"列头与右边"贴右"的固定列都会错开。量出两边的
        偏移后**一次算准**新的左右内边距(几何对两边都是线性的), 不会来回抖。
        """
        parts = next(iter(self._row_parts.values()), None)
        if parts is None or not parts.columns.winfo_ismapped():
            return
        if not self._head_columns.winfo_ismapped():
            return
        # 正数表示表头的内容偏右, 要往左挪.
        off_left = self._head_name.winfo_rootx() - parts.name_block.winfo_rootx()
        off_right = (
            self._head_columns.winfo_rootx() + self._head_columns.winfo_width()
        ) - (parts.columns.winfo_rootx() + parts.columns.winfo_width())
        if off_left == 0 and off_right == 0:
            self._align_attempts = 0
            return
        if self._align_attempts >= _ALIGN_MAX_ATTEMPTS:
            return  # 调了几轮还不齐就不再折腾(窗口极窄等边界情况)
        left, right = self._head_pads
        # 左边距变化会**整体平移**表头内容, 所以右边距要把这部分再补回来.
        pads = (max(0, left - off_left), max(0, right + off_right - off_left))
        if pads == self._head_pads:
            return  # 已经调到边上了(不能再挪), 就此停手
        self._align_attempts += 1
        self._head_pads = pads
        self._head.grid_configure(padx=pads)
        # 表头自己的几何变化不会再触发滚动区的 Configure, 所以主动再量一轮.
        self._schedule_list_sync()

    def _refit_row_names(self) -> None:
        """按名称标签的实际宽度重新裁剪名称: 窗口变宽就能多显示几个字.

        名称是唯一长度无法预期的内容, 所以它的可用宽度就是"剩下多少算多少"; 每次
        宽度变化都按真实宽度重裁一次, 长名称才会随窗口变宽而多显示。

        宽度还没量出来(``winfo_width()`` 是 1: 首帧布局尚未完成)时**安排下一轮重试**,
        而不是直接跳过 —— 各平台的布局时序不同, 有环境下首帧量不到宽度且之后不再有
        尺寸变化, 不重试就会一直停在兜底宽度的短文本上。
        """
        retry = False
        for parts in self._row_parts.values():
            width = int(parts.label.winfo_width())
            if width <= 1:
                retry = True
                continue
            self._fit_row_name(parts, width)
        if retry:
            self._retry_refit()

    def _fit_row_name(self, parts: _RowParts, width: int) -> None:
        """按给定宽度裁一行名称; 宽度未知或没变就不动.

        "没变就不动"有两层作用: 省掉一次无谓的文本重设, 也不会出现"改文本 → 新的
        Configure → 再裁一次"这种来回追。
        """
        if width <= 1 or parts.fitted_width == width:
            return
        parts.fitted_width = width
        parts.label.configure(text=fit_text(parts.full_name, self._name_font, width))

    def _on_name_resize(self, game_id: str, event: tk.Event) -> None:
        """名称标签自己的宽度变了 → **立刻**裁这一行.

        滚动区的 ``<Configure>`` 只在**它自己**的尺寸变化时来: 内宽变了而外宽没变
        (滚动条出现/消失、表头内边距被重算)不会触发它, 行刚重建、标签刚量到真实宽度
        时也未必等到下一轮。所以这里直接盯标签自己: 宽度一量出来就按它裁好, 不用等
        延后的那一轮(与容器事件互补)。只裁这一行, 拖窗口时的开销是一行一次。
        """
        parts = self._row_parts.get(game_id)
        if parts is not None:
            self._fit_row_name(parts, int(event.width))

    def _retry_refit(self) -> None:
        """为"宽度还没量出来"排下一轮名称重裁(有上限, 免得一直排下去)."""
        if self._refit_attempts >= _REFIT_MAX_ATTEMPTS:
            return
        self._refit_attempts += 1
        self._schedule_list_sync()

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
        # 有缓存封面就显示封面, 缺图/解码失败回落游戏名前两个字占位.
        cover_image = self._artwork_image(item, "cover", (_COVER_WIDTH, _COVER_HEIGHT))
        placeholder = ctk.CTkLabel(
            cover,
            text="" if cover_image is not None else item.name[:2],
            image=cover_image,
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
        name_font = self._name_font
        name = ctk.CTkLabel(
            card,
            text=fit_text(
                item.name, name_font, _POSTER_TEXT_WIDTH, max_lines=_POSTER_NAME_LINES
            ),
            anchor="w",
            justify="left",
            # 同上: wraplength 兜住测量误差, 最多两行, 不会溢出卡片.
            wraplength=_POSTER_TEXT_WIDTH,
            font=name_font,
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

    def _artwork_image(
        self, item: HomeGameItem, kind: ArtworkKind, size: tuple[int, int]
    ) -> ctk.CTkImage | None:
        """加载封面/图标(只读本地缓存); 无图或解码失败时返回 None 走文字占位.

        渲染是同步的, 因此这里**不联网**: 下载由后端在导入游戏后于后台完成, 补好后
        刷新数据版本, 下一轮重绘自然就带上图片。
        """
        key = f"{kind}:{item.game_id}"
        cached = self._artwork_images.get(key)
        if cached is not None:
            return cached
        try:
            path = self._backend.artwork_path(item.game_id, kind)
        except ArchiveManagementError as exc:  # 缺图不影响管理功能
            logger.debug("读取图片失败: %s", exc)
            return None
        if not path:
            return None
        try:
            with Image.open(path) as image:
                loaded = image.copy()
        except (OSError, ValueError) as exc:
            logger.warning("图片无法解码(%s): %s", path, exc)
            return None
        picture = ctk.CTkImage(light_image=loaded, size=size)
        self._artwork_images[key] = picture
        return picture

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
        """一行游戏: 左侧"色块 + 名称"(占满剩余宽度) + 右侧贴靠的固定列块."""
        palette = self._palette
        row = ctk.CTkFrame(self._list_box, corner_radius=8)
        # 先放右侧的固定列块, 再把剩下的宽度**全部**给名称块: 名称想显示多长就多长.
        columns = ctk.CTkFrame(row, fg_color="transparent")
        columns.pack(side="right", padx=(_TABLE_BLOCK_GAP, _TABLE_SIDE_PAD), pady=8)
        self._configure_columns(columns)
        left = ctk.CTkFrame(row, fg_color="transparent")
        left.pack(side="left", fill="x", expand=True, padx=(_TABLE_SIDE_PAD, 0), pady=8)
        # 头像优先显示商店图标, 没有图标就回落一个色块圆点(配色由名称推导).
        icon_image = self._artwork_image(item, "icon", (_ICON_SIZE, _ICON_SIZE))
        dot = ctk.CTkLabel(
            left,
            text="" if icon_image is not None else "●",
            image=icon_image,
            width=_DOT_COLUMN,
            anchor="center",
            font=ctk.CTkFont(size=14),
            text_color=self._tone_color(item.tone),
        )
        dot.pack(side="left")
        name = ctk.CTkLabel(
            left,
            # 先用兜底宽度裁一次: 直接放完整名称会让标签请求出上千像素, 把整张表
            # 撑到窗口之外(布局算出来的宽度跟着变, 会来回抖)。真实宽度由
            # _refit_row_names() 在首帧之后补上。
            text=fit_text(item.name, self._name_font, _NAME_FALLBACK_WIDTH),
            anchor="w",
            justify="left",
            font=self._name_font,
            text_color=palette.text_body,
        )
        name.pack(side="left", fill="x", expand=True, padx=(_NAME_PAD, 0))
        # 名称标签盯住自己的宽度: 容器事件不一定来(见 _on_name_resize), 而"名称按当前
        # 宽度裁好"是要对用户兑现的。
        name.bind(
            "<Configure>", lambda event: self._on_name_resize(item.game_id, event)
        )
        self._row_parts[item.game_id] = _RowParts(left, columns, name, item.name)
        # 状态标签也封顶: 标签变多时它会把整行撑宽(横向溢出), 超出部分补省略号.
        chips = fit_text(" · ".join(item.chips), self._value_font, _COLUMNS[-1][1])
        values = (
            (item.platform_label, 0),
            (str(item.location_count), 1),
            (str(item.backup_count), 2),
            (item.last_backup_label or "—", 3),
            (item.activity_label or "—", 4),
            (chips, 5),
        )
        widgets: list[ctk.CTkBaseClass] = [row, left, columns, dot, name]
        for text, index in values:
            _key, width, anchor = _COLUMNS[index]
            label = ctk.CTkLabel(
                columns,
                text=fit_text(text, self._value_font, width),
                anchor=anchor,
                font=self._value_font,
                text_color=(
                    palette.danger if item.risk and index == 5 else palette.text_muted
                ),
            )
            label.grid(row=0, column=index, sticky="ew", padx=_cell_pad(index))
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
        """按选中项及其状态决定按钮可用性与文案.

        归档的游戏只保留"删除游戏、导出游戏、取消归档、打开详情": 规则来自
        ``domain.game_rules``, 界面只负责落位。管理窗口是归档游戏删除自己的唯一
        入口, 因此它仍然可用, 只是按钮改名为"删除游戏"。
        """
        item = self._item()
        if item is None:
            for button in self._action_buttons:
                button.configure(state="disabled")
            self._paint_buttons()
            return
        for button in self._action_buttons:
            button.configure(state="normal")
        self._apply_item_states(item)
        self._paint_buttons()

    def _apply_item_states(self, item: HomeGameItem) -> None:
        """按选中项的归档/启用状态落位按钮状态与文案.

        归档时只有四个动作可用: 打开详情、取消归档、管理窗口(删除入口)与导出
        (导出在详情页); 其余按钮由 ``item.allow`` 统一判掉。
        """
        self._backup_btn.configure(
            state="normal" if item.backup_enabled else "disabled"
        )
        self._location_btn.configure(
            state="normal" if item.allow("locations") else "disabled"
        )
        self._tags_btn.configure(state="normal" if item.allow("tags") else "disabled")
        self._enable_btn.configure(
            state="normal" if item.allow("enable") else "disabled",
            text=tr("home.action_disable" if item.enabled else "home.action_enable"),
        )
        self._manage_btn.configure(
            text=tr("home.action_delete" if item.archived else "home.action_manage")
        )
        self._archive_btn.configure(
            text=tr("home.action_unarchive" if item.archived else "home.action_archive")
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
        if not item.allow("backup"):
            self._report_archived(item)
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
        if not item.allow("locations"):
            self._report_archived(item)
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
            archived=item.archived,
            backup_location=backup_location,
            on_change=self._refresh,
        )

    def _on_edit_tags(self) -> None:
        """编辑选中游戏的自定义标签(逗号分隔)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        if not item.allow("tags"):
            self._report_archived(item)
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

    def _on_toggle_enabled(self) -> None:
        """启用或停用选中的游戏(启用会自动停用其它启用中的游戏)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        if not item.allow("enable"):
            self._report_archived(item)
            return
        enabled = not item.enabled
        replaced = self._enabled_rival(item) if enabled else None
        try:
            self._backend.set_game_enabled(item.game_id, enabled)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        log_action("ui.home_enable", game_id=item.game_id, enabled=enabled)
        self._refresh()
        self._summary_label.configure(
            text=self._enable_message(item, enabled=enabled, replaced=replaced)
        )

    def _enabled_rival(self, item: HomeGameItem) -> HomeGameItem | None:
        """返回当前处于启用态的另一款游戏(启用新的一款会把它自动停用)."""
        board = self._board
        if board is None:
            return None
        return next(
            (
                other
                for other in board.games
                if other.enabled and other.game_id != item.game_id
            ),
            None,
        )

    @staticmethod
    def _enable_message(
        item: HomeGameItem, *, enabled: bool, replaced: HomeGameItem | None
    ) -> str:
        """根据"启用/停用 + 是否顶掉了另一款"给出反馈文案."""
        if not enabled:
            return tr("home.disabled", name=item.name)
        if replaced is None:
            return tr("home.enabled", name=item.name)
        return tr("home.enabled_replaced", name=item.name, other=replaced.name)

    def _report_archived(self, item: HomeGameItem) -> None:
        """归档游戏的动作被拦下时给出原因(不弹窗, 与其它提示一致)."""
        self._summary_label.configure(
            text=tr("home.archived_blocked", name=item.name),
            text_color=self._palette.danger,
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
