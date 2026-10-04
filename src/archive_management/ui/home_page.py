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

from archive_management.application.games import ActivationOutcome
from archive_management.domain import (
    MAX_TAG_LENGTH,
    MAX_TAGS,
    PAGE_SIZES,
    ArtworkKind,
    HomeFilter,
    HomeLayout,
    HomeView,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.services.audit import log_action
from archive_management.ui.activation_page import ActivationPanel
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import ask_text, edit_tags_dialog, info_dialog
from archive_management.ui.discovery_page import DiscoveryPanel
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import (
    HomeBoard,
    HomeGameItem,
    HomeSection,
    poster_columns,
    status_lines,
)
from archive_management.ui.palette import Palette
from archive_management.ui.pickers import pick_directory
from archive_management.ui.rendering import host_image
from archive_management.ui.textfit import fit_text
from archive_management.ui.typography import FONT_GLYPH
from archive_management.ui.widgets import (
    auto_scrollbar,
    card_surface_colors,
    fit_label,
    measured_font,
    paint_button_disabled,
    paint_button_enabled,
    scaled_px,
    sync_scrollbar,
    sync_tooltip,
    track_wraplength,
    window_scaling,
)

_ChangeCallback = Callable[[], None]
_DetailCallback = Callable[[str], None]
_NoticeCallback = Callable[[str], None]
_MonitorCallback = Callable[[str], None]

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
# 数据行右侧再留一点: 滚动条出现时它就贴在画布右边, 最后一列不该顶到那条竖线上.
_SCROLL_INSET = 6
# 表头的上下留白(见 _build_table 的 ``pady=(10, 6)``)。
_HEAD_ROW_PAD = 16
# 状态列最多折几行: **状态一行 + 自定义标签一行**(见 models.status_lines)。
_STATE_MAX_LINES = 2
# 海报卡片各行的上下间距: 与 ``_build_poster`` 里的 grid 共用一套 —— 卡片高度按内容算
# (见 _fit_poster_height), 这些间距是算式的一部分, 不许在两处各写一遍。
_POSTER_COVER_PAD_Y = (8, 6)
_POSTER_META_PAD_Y = (2, 0)
_POSTER_ACTIVITY_PAD_Y = (2, 10)
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
# 用游戏名前两个字代替, 备份数量排在名称下方(不再压在封面上)。
_POSTER_WIDTH = 190
# 卡片高度要容下封面(250 + 上下间距 14)、名称(最多两行, 40)、备份数角标(15)与
# 最近活动(15), 再留出名称与元信息之间的留白; 合计约 350, 取 366 留余量, 否则最后
# 一行文字会被压到底边并盖住卡片的下边框。备份数角标**不再压在封面上**: 它原本
# 盖住了封面里的游戏 logo, 现在与"最近活动"一起排在名称下方(第 5 号评审)。
_POSTER_HEIGHT = 366
_COVER_HEIGHT = 250
# 名称与下方元信息之间的留白: 只靠字号差异区分标题与元信息, 读起来像同一段文字
# 被截断(第 5 号评审)。
_POSTER_TITLE_GAP = 6
# 无封面时的占位字(游戏名前两个字): 34px 比其它卡片的标题大一倍, 一张占位卡因此
# 看起来像另一个组件, 收到与卡片标题同级的 24px。
_POSTER_PLACEHOLDER_SIZE = 24
# 封面宽度 = 卡片宽度去掉左右各 8px 内缩(与 :meth:`HomePage._build_poster` 一致).
_COVER_WIDTH = _POSTER_WIDTH - 16
# 列表行头像里的图标边长(与 :data:`_DOT_COLUMN` 留出内缩).
_ICON_SIZE = 22
# 海报卡片在网格里的占位宽度(卡片 + 左右各 4 的间距).
_POSTER_SLOT_WIDTH = _POSTER_WIDTH + 8
# 海报卡片里名称的可用宽度: 卡片宽减左右各 10 的内边距.
_POSTER_TEXT_WIDTH = _POSTER_WIDTH - 20
_POSTER_NAME_LINES = 2
# 名称块的**首帧**高度(逻辑像素): 设计值, 两行约 40。它只是兜底 —— 真正的值在控件落地之后
# 按**渲染出来的行高**量出来(见 :meth:`HomePage._fit_poster_name_blocks`): 布局之前字体与
# CTkLabel 量到的是默认字号的度量, 2026-10-04 的 Linux CI 因此在 28 与 39 之间对不上。
_POSTER_NAME_BLOCK_FALLBACK = 40


def poster_name_block_height(line: int, default: int) -> int:
    """名称块的高度 = **一行行距 + 标签的默认高** 再加 2px 的取整余量(都是逻辑像素).

    为什么是这两个数: ``CTkLabel`` 有一个与文本无关的**默认高**(默认 28 逻辑像素, 所以
    一行名量出来就是 28、两行名才会长到 39 —— 2026-10-04 的 Linux CI 就是这个差); 一行名
    与两行名要占同样高度, 就要在默认高之外额外留出**一行行距**。

    ``line``(一行行距)与 ``default``(默认高)都由调用方给出**实测值**
    (见 :meth:`HomePage._fit_poster_name_blocks`) —— 这里不做字体度量: 布局之前量不准,
    2026-10-04 的 Linux CI 算出来 28 而两行文本真的要 39。两个数都与**文本内容无关**,
    所以同一字体下各卡片拿到同一个高度。
    """
    return line + default + 2


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


def poster_backing(cover_image: object | None, palette: Palette) -> str:
    """海报标记(备份数角标、启用绿点)的底衬色: 只有**真画了封面图**时才加底衬.

    有图时用面板色: 封面图上是什么颜色都有可能, 标记没有底衬会看不清。回落成名称
    占位时封面底色是纯色, 标记用 ``transparent`` 取到的正是这个底色(与父容器完全
    一致), 于是底衬彻底看不见 —— 这才是真正的"没有背景色"。

    单独抽成纯函数是为了能**不建窗口**就把这条规则钉住: 依赖"真封面图"的界面用例
    在报告里一直是被跳过的那一类(见 tests/tk_guard.py 的说明)。
    """
    return "transparent" if cover_image is None else palette.panel


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
        on_activation_monitor: _MonitorCallback | None = None,
        on_activation_refresh: _ChangeCallback | None = None,
        on_export_batch: _ChangeCallback | None = None,
    ) -> None:
        """在 ``parent`` 内构造主页(含游戏发现与游戏启停分区)."""
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._on_change = on_change
        self._on_open_detail = on_open_detail
        self._on_notice = on_notice
        self._on_activation_monitor = on_activation_monitor
        self._on_activation_refresh = on_activation_refresh
        # 批量导出要弹两个模态框(多选与保存位置), 由主窗口负责, 主页只转交一次点击.
        self._on_export_batch = on_export_batch
        # 最近一次自动启停结果与开关状态: 主题切换会重建整个页面, 重建后要能回填。
        self._last_activation: ActivationOutcome | None = None
        self._activation_enabled = False
        self._board: HomeBoard | None = None
        self._rows: dict[str, ctk.CTkFrame] = {}
        # 鼠标悬停的那一张卡(列表行/海报卡共用): 与详情页的备份卡片同一套反馈.
        self._hover: str | None = None
        # 封面/图标: CTkImage 必须被持有引用, 否则会被垃圾回收成空白.
        self._artwork_images: dict[str, ctk.CTkImage] = {}
        self._selected: str | None = None
        # 主窗口是否正在跑长操作: 主页的长操作入口与它共用同一份事实(见 set_busy)。
        self._busy = False
        # 每行的关键部件: 名称按真实宽度重裁、表头对齐都要用(键是游戏 id).
        self._row_parts: dict[str, _RowParts] = {}
        # 本页创建的按钮与它们的样式: 可用/禁用切换时按这份登记重绘配色.
        self._buttons: dict[ctk.CTkButton, str] = {}
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
        # 名称块(与它里面的名称标签)与已经量出来的块高: 海报卡片重建后按同一个数摆,
        # 从而"一行名与两行名下面那两行"永远对齐(见 _fit_poster_name_blocks)。
        self._poster_name_blocks: list[tuple[ctk.CTkFrame, ctk.CTkLabel]] = []
        self._poster_name_height: int | None = None
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
        # ``CTkFrame.bind`` 把绑定**转发到内层画布**, 所以销毁事件里的 widget 是那块画布而
        # 不是 self.frame —— 把它记下来当判据(见 :meth:`_on_frame_destroyed`)。
        self._frame_host = getattr(self.frame, "_canvas", None)
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
            row=1, column=0, sticky="nsew", padx=24, pady=(0, 16)
        )
        self._discovery.frame.grid_remove()

        self._activation = ActivationPanel(
            self.frame,
            palette=self._palette,
            on_monitor=self._on_activation_monitor,
            on_open_detail=self._on_open_detail,
            on_refresh=self._on_activation_refresh,
        )
        self._activation.frame.grid(
            row=1, column=0, sticky="nsew", padx=24, pady=(0, 16)
        )
        self._activation.frame.grid_remove()
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
        进"支持的最小窗口(主窗口 minsize 1024)下的内容宽度"。否则在比设计尺寸窄的
        屏幕上(例如 CI 的虚拟显示器), 最后一个控件会越出行右边界并盖住描边——
        ``tests/integration/test_gui_layout.py`` 会直接报出来, 改宽度后请同步跑它。

        2026-10-03 最小窗口从 1200 降到 1024 时这一行跟着收窄过一次(页签 112→96、
        平台 112→104、类型 136→124、搜索框 150→120、两颗按钮 58→52): 收窄后合计
        约 **924 逻辑像素**, 而 1024 窗口下的内容区约 949 —— 余量 25px 留给取整。
        这些宽度都是固定的, 所以这条余量在任何字体/平台上都一样。
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
                width=96,
                height=32,
                corner_radius=8,
                border_width=1,
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            tab.grid(row=0, column=index, padx=(8, 4), pady=10)
            self._tabs[view] = tab
        self._origin_box = self._combo(bar, 104, self._on_origin_change)
        self._origin_box.grid(row=0, column=spacer + 1, padx=(0, 8), pady=10)
        self._category_box = self._combo(bar, 124, self._on_category_change)
        self._category_box.grid(row=0, column=spacer + 2, padx=(0, 8), pady=10)

        search = ctk.CTkFrame(bar, fg_color="transparent")
        search.grid(row=0, column=spacer + 3, padx=(0, 12), pady=10)
        self._search_entry = ctk.CTkEntry(
            search,
            width=120,
            height=32,
            placeholder_text=tr("home.search_placeholder"),
            fg_color=palette.input_bg,
            border_color=palette.input_border,
            text_color=palette.text_body,
        )
        self._search_entry.pack(side="left")
        self._search_entry.bind("<Return>", lambda _event: self._submit_search())
        self._search_btn = self._button(
            search, tr("home.search"), self._submit_search, width=52
        )
        self._search_btn.pack(side="left", padx=(6, 0))
        self._clear_btn = self._button(
            search, tr("home.clear"), self._clear_search, width=52
        )
        self._clear_btn.pack(side="left", padx=(6, 0))

    def _build_actions(self) -> None:
        """操作行: 左侧是展示方式切换与库级别的批量导出, 右侧是选中行的动作按钮.

        批量导出与选中的那一行无关(它自己在对话框里多选), 因此放在左边这一组; 中间
        那列只吸收多余宽度, 两头的控件各自贴边。
        """
        bar = ctk.CTkFrame(self._library, fg_color="transparent")
        bar.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 6))
        bar.grid_columnconfigure(2, weight=1)

        self._layout_switch = self._segmented(
            bar, [item.label for item in HomeLayout], self._on_layout_change
        )
        self._layout_switch.grid(row=0, column=0, padx=(0, 10))
        self._export_batch_btn = self._button(
            bar,
            tr("home.action_export_batch"),
            self._request_export_batch,
            width=120,
        )
        self._export_batch_btn.grid(row=0, column=1, padx=(0, 10))

        actions = ctk.CTkFrame(bar, fg_color="transparent")
        actions.grid(row=0, column=3, sticky="e")
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
        # 创建顺序 = Tk 的 Tab 顺序, 所以这里必须与下面 grid 的列顺序一致:
        # 先建"启动"再建"归档", 否则 Tab 会先从归档走到启动(实测反馈, Tk 的
        # tk_focusNext 按窗口的堆叠顺序走, 与 grid 的 row/column 无关)。
        self._enable_btn = self._button(
            actions, tr("home.action_enable"), self._on_toggle_enabled, width=88
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
        # 海报模式会收起表头, 但那一行的高度要留着(见 _reserve_head_row): 行塌成 0 时
        # 整个游戏区会往上跳表头那么高, 与列表模式对不上。
        self._card = card

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
        # 内容装得下就不显示滚动条(空库/三行数据时右侧那条拖不动的滑块会误导用户).
        auto_scrollbar(self._list_box)
        # 宽度变化时重排海报: 启动首屏渲染时控件尺寸还没测量出来
        # (``winfo_width()`` 只有 1), 算出的列数会偏少 —— 4 款游戏会被排成两行。
        self._list_box.bind("<Configure>", self._on_list_box_resize)
        # **滚动条出现/消失只改内层画布的宽度**, 帧本身的宽度不变(它与滚动条同在一个 grid
        # 里) —— 只盯帧的话, 表头对齐就永远停在"差一个滚动条宽度"上。实测 CI 的 Windows
        # runner: 窗口被夹到最小高度 ⇒ 列表需要滚动 ⇒ 六列整体差 22px(就是滚动条宽度;
        # 旧滚动条宽度是 16px, 症状一模一样), 而本机窗口更高、根本不显示滚动条, 所以只在
        # CI 红。表头对齐与名称重裁都是按内容宽度算的, 这里连画布一起盯才跟得上。
        parent_canvas = getattr(self._list_box, "_parent_canvas", None)
        if parent_canvas is not None:
            parent_canvas.bind("<Configure>", self._on_list_box_resize, add="+")

    def _build_footer(self) -> None:
        """底栏: 左侧是计数, 右下角是"每页条数 + 翻页"控件."""
        palette = self._palette
        footer = ctk.CTkFrame(self._library, fg_color="transparent")
        footer.grid(row=3, column=0, sticky="ew", padx=24, pady=(0, 16))
        # 空库时整行收起(见 _render_footer): 一屏空状态里再挂"共 0 款游戏 · 第 1/1 页"
        # 只是噪声, 还要用户去分辨哪几个按钮是不能点的。
        self._footer = footer
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
            border_color=palette.input_border,
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

    def _configure_columns(self, block: ctk.CTkFrame) -> None:
        """给"固定列块"设置列宽: 表头与数据行共用同一套, 因此各列一定上下对齐.

        列宽固定且**不分配权重**: 整块贴右, 宽度由这些列求和而来; 文本再用 fit_text
        封在自己的列宽以内, 于是内容长短也不会把列推动一下。

        ``_COLUMNS`` 里写的是**设计**尺寸(逻辑像素), 而 ``grid`` 的 ``minsize`` 是物理
        像素: 这里先换算。不换算的话 125% 的屏上每一列都比设计窄 25%, 而状态列的
        ``wraplength`` 是逻辑的(=物理 x1.25)——内容比列宽多出 46px, 那一行的整块列因此
        被推左 46px, 与其他行和表头错开(2026-10-02 用户实测: 标签多的那款游戏"没对齐
        内容")。
        """
        for index, (_key, width, _anchor) in enumerate(_COLUMNS):
            block.grid_columnconfigure(index, minsize=scaled_px(block, width))

    def _button(
        self,
        parent: ctk.CTkFrame,
        text: str,
        command: Callable[[], None],
        *,
        style: str = "ghost",
        width: int = 96,
    ) -> ctk.CTkButton:
        """按页面调色板创建一个按钮(样式登记下来, 之后按可用性重绘)."""
        button = ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=32,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
            **self._style_colors(style),
        )
        self._buttons[button] = style
        return button

    def _style_colors(self, style: str) -> dict[str, object]:
        """按钮样式的常规配色(禁用态由 ``paint_button_disabled`` 另行压暗)."""
        palette = self._palette
        colors: dict[str, dict[str, object]] = {
            "accent": {
                "fg_color": palette.accent,
                "hover_color": palette.accent_soft_border,
                "text_color": palette.accent_text,
                "border_width": 0,
            },
            "ghost": {
                "fg_color": palette.raised,
                "hover_color": palette.item_hover,
                "text_color": palette.text_body,
                "border_width": 1,
                "border_color": palette.border,
            },
        }
        return colors[style]

    def _restyle_buttons(self) -> None:
        """按当前可用性重绘本页按钮: 禁用态是更暗的底色 + 更暗的字.

        "几乎一样"的禁用态等于没有禁用态 —— 分页的"上一页/下一页"在空库下曾与可用
        态同色(第 1 号评审)。可用态也在这里重设, 所以按钮从禁用恢复可用时颜色会
        跟着回来。
        """
        palette = self._palette
        for button, style in list(self._buttons.items()):
            if not button.winfo_exists():
                # 渲染重建后留下的旧登记: 摘掉, 别去碰已经不存在的控件。
                self._buttons.pop(button, None)
                continue
            if str(button.cget("state")) == "disabled":
                paint_button_disabled(button, palette)
                continue
            colors = self._style_colors(style)
            paint_button_enabled(
                button,
                palette,
                fg_color=str(colors["fg_color"]),
                text_color=str(colors["text_color"]),
                hover_color=str(colors["hover_color"]),
                border_color=(
                    None
                    if "border_color" not in colors
                    else str(colors["border_color"])
                ),
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
            border_color=palette.input_border,
            text_color=palette.text_body,
            dropdown_fg_color=palette.panel,
            dropdown_text_color=palette.text_body,
            font=ctk.CTkFont(size=12),
            dropdown_font=ctk.CTkFont(size=12),
        )

    # -- 分区切换 -----------------------------------------------------------

    def _show_section(self, section: HomeSection) -> None:
        """切换主页内部的分区(游戏库 / 游戏发现 / 游戏启停)."""
        self._section = section
        if section is HomeSection.LIBRARY:
            self._library.grid()
            self._discovery.frame.grid_remove()
            self._activation.frame.grid_remove()
        elif section is HomeSection.DISCOVERY:
            # 发现分区的数据可能被别处改过(例如手动添加游戏后同名候选会被自动
            # 标记为已入库), 因此每次进入都重新读取一次.
            self._discovery.reload()
            self._discovery.frame.grid()
            self._library.grid_remove()
            self._activation.frame.grid_remove()
        else:
            # 进入启停分页: 页面只展示最近一次轮询结果, 因此进来就立即再探一次
            # (人工动作, 顺带把轮询间隔退回最快档), 结果由主窗口回填。
            self._activation.render(
                self._last_activation, enabled=self._activation_enabled
            )
            self._activation.frame.grid()
            self._library.grid_remove()
            self._discovery.frame.grid_remove()
            if self._on_activation_refresh is not None:
                self._on_activation_refresh()
        self._paint_section_tabs()
        log_action("ui.home_section", basic=True, section=section.value)

    def refresh_activation(
        self,
        outcome: ActivationOutcome | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """把最近一次自动启停结果交给启停分页(由主窗口在轮询结束后调用)."""
        if outcome is not None:
            self._last_activation = outcome
        if enabled is not None:
            self._activation_enabled = enabled
        self._activation.render(self._last_activation, enabled=self._activation_enabled)

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
        self._buttons = {}
        self._build()
        self.reload()
        self._activation.render(self._last_activation, enabled=self._activation_enabled)
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
        self._render_footer(board)

    def _render_footer(self, board: HomeBoard) -> None:
        """底栏: 空库时整行收起, 否则显示统计与翻页控件.

        空库里"共 0 款游戏 · 最近活跃 0 · 待处理 0 · 已归档 0"加"每页 30 / 上一页 /
        第 1/1 页 / 下一页"整行照旧显示, 页面看起来像渲染了一半(第 1/2 号评审)。
        筛选把结果筛空时**不算空库**: 那时用户正需要重新选筛选条件, 计数与页签上的
        数量就是他的参照。
        """
        if board.empty_library:
            self._footer.grid_remove()
            return
        self._footer.grid()
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
        # 列表重建后悬停状态失效: 不清掉会指向已销毁的卡片.
        self._hover = None
        self._align_attempts = 0
        self._refit_attempts = 0
        self._poster_name_blocks = []
        self._apply_poster_columns(0)
        games = self._visible_games()
        board = self._board
        if not games or board is None:  # pragma: no cover - reload 之后才会渲染
            # 空的时候**连表头一起收起来**: 一条数据都没有却摆着七个列头, 看起来
            # 像"表格坏了"; 页面只留一条解释性空状态(含下一步做什么)。
            self._head.grid_remove()
            if board is not None:
                self._render_empty(board)
            self._selected = None
            self._update_actions()
            self._sync_scrollbar()
            return
        if self._selected not in {item.game_id for item in games}:
            self._selected = games[0].game_id
        if self._filter.layout is HomeLayout.LIST:
            self._head.grid()
            self._reserve_head_row(reserve=False)
            for item in games:
                row = self._build_row(item)
                row.pack(fill="x", padx=(4, _SCROLL_INSET), pady=2)
                self._rows[item.game_id] = row
            # 名称按**实际可用宽度**裁剪, 而宽度要等布局完成才知道 —— 这里先排一次
            # (用兜底宽度), 首帧之后再精确重裁一次。
            self._schedule_list_sync()
        else:
            self._head.grid_remove()
            self._reserve_head_row(reserve=True)
            self._render_posters(games)
            # 海报模式没有表头要对齐, 但名称块的高度同样要等落地之后再量一次(那条延后
            # 任务的两种布局都跑, 见 _sync_list_layout / _fit_poster_name_blocks)。
            self._schedule_list_sync()
        self._paint_rows()
        self._update_actions()
        self._sync_scrollbar()

    def _sync_scrollbar(self) -> None:
        """重绘之后再判一次滚动条要不要出现(内容条数刚变过).

        滚动条占的是**滚动区内部**的宽度: 它一出现/收起, 每一行的可用宽度就变了, 而滚动区
        外层的尺寸没变 —— 于是 `<Configure>` 可能一次都不来, 表头就会与数据行整体错开一个
        滚动条宽度(实测格式: 六列全部偏差同一个常量 16px)。这里主动补一次同步。
        """
        sync_scrollbar(self._list_box)
        if self._row_parts:
            self._schedule_list_sync()

    def _reserve_head_row(self, *, reserve: bool) -> None:
        """海报模式收起表头后, 游戏区的四边边距要一致.

        收起表头时那一行会塌成 0 高, 游戏区就贴到了面板上沿 —— 上边距 0, 左右与下
        边却各有 10px。这里把它的上边距补成与其余三边相同(不保留表头那一行, 那样
        会多出一条明显的空白)。
        """
        self._list_box.grid(pady=(10, 10) if reserve else (0, 10))

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
            # 几何真的变了 ⇒ 表头对齐**重新给满轮数**。``_ALIGN_MAX_ATTEMPTS`` 是防打转的,
            # 但它是"控件一辈子"的计数, 只有某一轮量到完全对齐才归零 —— 启动期那几轮(列表
            # 还没显示滚动条、行尺寸还在测)会把轮数用光并停在"右侧内边距被夹到 0"的状态,
            # 之后再来的任何变化都被"已经放弃"挡掉, **永久**差一个滚动条宽度。实测 CI 的
            # Windows runner: 滚动条出现后表头一直差 40px(= 2x滚动条宽度), 本机窗口更高、
            # 不显示滚动条所以一直绿。换了几何就该重新试, 打转的保护仍在(见 _align_table_header)。
            self._align_attempts = 0
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
        """宿主帧被销毁时撤掉挂起的同步任务.

        **判据要连内层画布一起认**: ``CTkFrame.bind`` 把绑定转发到画布上, 销毁事件里的
        ``event.widget`` 是那块画布而不是 ``self.frame``(那个对象不是 Tk 控件) —— 只认
        ``self.frame`` 时这条清理是死代码(2026-09-30 实测: 销毁后 ``_sync_job`` 还挂着,
        ``cancel_list_sync`` 被调用 0 次, 任务随后以
        `invalid command name "..._sync_list_layout"` 报出来)。

        子控件的 Destroy 也会冒泡上来, 所以不能无条件取消: 那些事件与"宿主没了"是两回事。
        """
        if event.widget is not self._frame_host and event.widget is not self.frame:
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
        """把表头对齐到数据行, 并按名称块的**实际宽度**重新裁剪每行的名称.

        海报模式没有表头, 也不重裁名称, 但**名称块的高度**同样要等控件落地之后才量得准
        (见 :meth:`_fit_poster_name_blocks`), 所以那一步在两种布局下都跑。
        """
        self._sync_job = None
        self._fit_poster_name_blocks()
        if self._filter.layout is not HomeLayout.LIST or not self._rows:
            return
        self._align_table_header()
        self._refit_row_names()

    def _fit_poster_name_blocks(self) -> None:
        """按**实测行高**把名称块定成两行高(只在海报模式下有意义).

        为什么必须延后量: 布局之前字体与 CTkLabel 量到的是默认字号的度量, 算出来 28px,
        而两行文本真的要 39px(2026-10-04 的 Linux CI 就是这么红的)。这里量的是**一行**的
        高度: 名称标签自己的请求高度 ÷ 它的行数(行数由文本里的换行数给出, 与用例同一条
        算法), 取各卡片里最大的那个, 乘 :data:`_POSTER_NAME_LINES` 就是块高 —— 同一字体的
        各卡片因此拿到同一个数, 一行名与两行名下面那两行必然对齐。
        """
        if not self._poster_name_blocks:
            return
        scale = window_scaling(self._list_box) or 1
        font = measured_font(self._name_font, self._list_box)
        # 标签的**默认高**与文本无关(实测 28 逻辑像素), 一行的行距取自字体: 两个数加起来
        # 就是"一行名不再矮一截"所需的高度, 而且与具体名称无关(同类卡片必然一致)。
        default = max(
            int(label.cget("height")) for _, label in self._poster_name_blocks
        )
        line = round(int(font.metrics("linespace")) / scale)
        height = poster_name_block_height(line, default)
        if height == self._poster_name_height:
            return
        self._poster_name_height = height
        for block, _ in self._poster_name_blocks:
            block.configure(height=height)
        self._refit_poster_cards()

    def _refit_poster_cards(self) -> None:
        """名称块高度变了之后重算卡片高度(卡片高度是按内容算的, 见 _fit_poster_height)."""
        for card in self._rows.values():
            frames = [
                child
                for child in card.winfo_children()
                if isinstance(child, ctk.CTkFrame)
            ]
            labels = [
                child
                for child in card.winfo_children()
                if isinstance(child, ctk.CTkLabel)
            ]
            if len(frames) < 2 or len(labels) < 2:  # pragma: no cover - 结构变了就该红
                continue
            cover, name_box = frames[0], frames[1]
            badge, activity = labels[-2], labels[-1]
            self._fit_poster_height(
                card, self._poster_rows(cover, name_box, badge, activity)
            )

    @staticmethod
    def _poster_rows(
        cover: ctk.CTkFrame,
        name_box: ctk.CTkFrame,
        badge: ctk.CTkBaseClass,
        activity: ctk.CTkBaseClass,
    ) -> tuple[tuple[ctk.CTkBaseClass, tuple[int, int]], ...]:
        """卡片里四行的"控件 + 上下间距"清单(建卡片与重算高度共用同一份)."""
        return (
            (cover, _POSTER_COVER_PAD_Y),
            (name_box, (_POSTER_TITLE_GAP, 0)),
            (badge, _POSTER_META_PAD_Y),
            (activity, _POSTER_ACTIVITY_PAD_Y),
        )

    def _header_offset(self) -> tuple[int, int] | None:
        """量表头内容与数据行的偏移(正数 = 表头偏右); 量不到时返回 None.

        两个地方共用这一个量法: :meth:`_align_table_header` 拿它算新内边距,
        :meth:`_on_name_resize` 拿它判断"可用宽度变了, 表头是不是真的错开了" —— 只有真的
        错开才值得再排一轮(见那里的说明)。
        """
        parts = next(iter(self._row_parts.values()), None)
        if parts is None or not parts.columns.winfo_ismapped():
            return None
        if not self._head_columns.winfo_ismapped():
            return None
        off_left = self._head_name.winfo_rootx() - parts.name_block.winfo_rootx()
        off_right = (
            self._head_columns.winfo_rootx() + self._head_columns.winfo_width()
        ) - (parts.columns.winfo_rootx() + parts.columns.winfo_width())
        return off_left, off_right

    def _align_table_header(self) -> None:
        """把表头的内容对齐到数据行.

        表头在卡片里, 而数据行在滚动区里(滚动条与内边距还占掉几十像素), 两者宽度
        并不相等; 不补的话左边"名称"列头与右边"贴右"的固定列都会错开。量出两边的
        偏移后**一次算准**新的左右内边距(几何对两边都是线性的), 不会来回抖。
        """
        offset = self._header_offset()
        if offset is None:
            return
        off_left, off_right = offset
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
        """按名称的**稳定预算**重新裁剪名称: 窗口变宽就能多显示几个字.

        名称是唯一长度无法预期的内容, 所以它的可用宽度就是"剩下多少算多少"; 每次宽度变化都
        按可用宽度重裁一次, 长名称才会随窗口变宽而多显示。可用宽度取
        :meth:`_name_available`(从视口推算), **不读**名称标签自己的实测宽度 —— 后者会被
        裁剪结果反过来影响, 是 2026-10-03 那个无限重裁的闭环。

        宽度还没量出来(``winfo_width()`` 是 1: 首帧布局尚未完成)时**安排下一轮重试**,
        而不是直接跳过 —— 各平台的布局时序不同, 有环境下首帧量不到宽度且之后不再有
        尺寸变化, 不重试就会一直停在兜底宽度的短文本上。
        """
        retry = False
        for parts in self._row_parts.values():
            width = self._name_available(parts)
            if width <= 1:
                retry = True
                continue
            self._fit_row_name(parts, width)
        if retry:
            self._retry_refit()

    def _row_viewport(self) -> int:
        """滚动区里**数据行真正能用**的宽度(内层画布; 拿不到时退回滚动区自己的宽度)."""
        canvas = getattr(self._list_box, "_parent_canvas", None)
        if canvas is not None:
            return int(canvas.winfo_width())
        return int(self._list_box.winfo_width())

    def _name_available(self, parts: _RowParts) -> int:
        """名称这一行**稳定的**可用宽度(物理像素).

        不能直接读名称标签的 ``winfo_width()``: 标签的请求宽度会决定滚动区**内层画布**的
        宽度(那里的宽度是 ``max(视口, 最宽子控件)``), 于是"按当前宽度裁一次"又改变了
        "当前宽度" —— 2026-10-03 探针抓到的闭环: 标签宽度 529 ↔ 327 来回翻, 每翻一次都
        重裁一次、又各排一个延后任务, 整个界面卡在 ``update()`` 里出不来。

        取法是"标签宽度再扣掉这一行**超出视口**的那部分": 行比视口宽, 说明内层画布是被
        内容撑出来的(这次裁剪的结果又变成了下一次的输入); 扣掉之后预算只由**视口**决定,
        内容再长也不会把预算带偏 —— 行因此会自己收回到视口宽度, 下一轮预算与标签宽度
        相等, 什么都不用做(闭环断开)。正常(行不比视口宽)时就是标签自己的宽度。
        """
        row = parts.name_block.master
        overflow = max(0, int(row.winfo_width()) - self._row_viewport())
        return max(1, int(parts.label.winfo_width()) - overflow)

    def _fit_row_name(self, parts: _RowParts, width: int) -> None:
        """按给定宽度裁一行名称; 宽度未知或没变就不动.

        "没变就不动"是这条闭环的闸: 同一个宽度不再重写文本, 也就不会再去引出新的
        ``<Configure>``。宽度由 :meth:`_name_available` 从**视口**推算(而不是名称标签的
        实测宽度) —— 名称的裁剪结果不会再反过来影响下一次的输入。
        """
        if width <= 1 or parts.fitted_width == width:
            return
        parts.fitted_width = width
        fit_label(parts.label, parts.full_name, self._name_font, width)

    def _on_name_resize(self, game_id: str, event: tk.Event) -> None:
        """名称标签自己的宽度变了 → 按**稳定预算**重裁这一行.

        滚动区的 ``<Configure>`` 只在**它自己**的尺寸变化时来: 内宽变了而外宽没变
        (滚动条出现/消失、表头内边距被重算)不会触发它, 行刚重建、标签刚量到真实宽度
        时也未必等到下一轮。所以这里直接盯标签自己(与容器事件互补)。

        重裁用的是 :meth:`_name_available` 而不是事件里的宽度: 事件里的那个宽度正是被
        裁剪结果影响的量(见 ``_name_available`` 的说明)。事件只是"该再看一眼"的提示,
        预算没变就什么都不做(实测闭环正是靠这一条断掉的)。
        """
        parts = self._row_parts.get(game_id)
        if parts is None:
            return
        del event
        width = self._name_available(parts)
        if parts.fitted_width != width:
            self._fit_row_name(parts, width)
        # 表头也许得跟着重新对齐, 而滚动区的 `<Configure>` 只在自己尺寸变化时来 —— 所以
        # 这里每次都排一轮(延后合并成一次)。这与裁剪结果**无关**, 不会回到那个闭环:
        # 预算是稳定的(`_name_available`), 名称被裁短就不再改变它自己与行的宽度, 于是
        # 既不会一直发新的 `<Configure>`, 也不会一再排任务。对齐本身另有次数上限。
        self._schedule_list_sync()

    def _retry_refit(self) -> None:
        """为"宽度还没量出来"排下一轮名称重裁(有上限, 免得一直排下去)."""
        if self._refit_attempts >= _REFIT_MAX_ATTEMPTS:
            return
        self._refit_attempts += 1
        self._schedule_list_sync()

    def _build_poster(self, item: HomeGameItem) -> ctk.CTkFrame:
        """一张海报卡片: 竖屏封面(右下角备份数 + 左下角启用标记) + 名称 + 最近活动."""
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
        cover.grid(row=0, column=0, sticky="nsew", padx=8, pady=_POSTER_COVER_PAD_Y)
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
            font=ctk.CTkFont(size=_POSTER_PLACEHOLDER_SIZE, weight="bold"),
        )
        placeholder.grid(row=0, column=0, sticky="nsew")
        # 底衬只用在**真的有封面图**的时候: 图上是什么颜色都有可能, 绿点没有底衬会
        # 看不清; 而回落成名称占位时封面底色就是纯色, 标签的 transparent 取到的正是
        # 这个底色(与父容器完全一致), 于是底衬彻底看不见。
        backing = poster_backing(cover_image, palette)
        name_font = self._name_font
        # 名称块的**高度由这一层容器说了算**(固定两行), 后面那两行的位置因此不随名称
        # 占了几行而变 —— 靠文本"永远两行"在 X11 上不成立(见
        # :func:`poster_name_block_height` 的说明, Linux CI 实测 28 vs 39)。
        name_box = ctk.CTkFrame(
            card,
            fg_color="transparent",
            height=self._poster_name_height or _POSTER_NAME_BLOCK_FALLBACK,
        )
        name_box.grid(
            row=1, column=0, sticky="ew", padx=10, pady=(_POSTER_TITLE_GAP, 0)
        )
        name_box.grid_propagate(False)
        name_box.grid_columnconfigure(0, weight=1)
        name = ctk.CTkLabel(
            name_box,
            text="",
            anchor="w",
            justify="left",
            # 同上: wraplength 兜住测量误差, 最多两行, 不会溢出卡片.
            wraplength=_POSTER_TEXT_WIDTH,
            font=name_font,
            text_color=palette.text_body,
        )
        fit_label(
            name,
            item.name,
            name_font,
            # 预算是**设计**值(逻辑像素), 而 fit_label 里的字体度量是**物理**像素.
            scaled_px(name, _POSTER_TEXT_WIDTH),
            max_lines=_POSTER_NAME_LINES,
        )
        # 名称底对齐容器的上沿: 一行名与两行名的第一个字落在同一行上(比"在两行里垂直居中"
        # 更整齐)。
        name.grid(row=0, column=0, sticky="nw")
        # 登记给 :meth:`_fit_poster_name_blocks`: 控件落地之后要按实测行高把块高改正。
        self._poster_name_blocks.append((name_box, name))
        # 备份数(或"无有效存档路径")排在名称下方而不是压在封面上: 压在封面上的角标
        # 会盖住封面里的游戏 logo, 而它本来就是这个游戏的元信息, 与活动时间同一组。
        meta_font = ctk.CTkFont(size=10, weight="bold")
        badge = ctk.CTkLabel(
            card,
            text="",
            anchor="w",
            font=meta_font,
            text_color=(
                palette.text_body
                if item.location_count and item.backup_count
                else palette.text_muted
            ),
        )
        badge.grid(row=2, column=0, sticky="ew", padx=10, pady=_POSTER_META_PAD_Y)
        fit_label(
            badge,
            self._poster_meta(item),
            meta_font,
            scaled_px(badge, _POSTER_TEXT_WIDTH),
        )
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
        activity.grid(
            row=3, column=0, sticky="ew", padx=10, pady=_POSTER_ACTIVITY_PAD_Y
        )
        self._fit_poster_height(
            card,
            (
                (cover, _POSTER_COVER_PAD_Y),
                (name_box, (_POSTER_TITLE_GAP, 0)),
                (badge, _POSTER_META_PAD_Y),
                (activity, _POSTER_ACTIVITY_PAD_Y),
            ),
        )
        widgets: list[ctk.CTkBaseClass] = [
            card,
            cover,
            placeholder,
            badge,
            name_box,
            name,
            activity,
        ]
        if item.enabled:
            # 左下角的启用标记: 只有启用的那一款才画点 —— 停用是常态(新建游戏默认
            # 停用), 画一片灰点反而让整屏海报都在“报告状态”。
            widgets.append(self._poster_status(cover, backing))
        for widget in widgets:
            widget.bind(
                "<Button-1>", lambda _event, key=item.game_id: self._select(key)
            )
            widget.bind(
                "<Double-Button-1>", lambda _event, key=item.game_id: self._open(key)
            )
            # 悬停在卡片与它的子控件上都要算"在这一张卡片里", 否则鼠标移到文字上时
            # 反馈会闪掉(与详情页的备份卡片同一套做法).
            widget.bind(
                "<Enter>", lambda _event, key=item.game_id: self._set_hover(key)
            )
            widget.bind("<Leave>", lambda _event: self._set_hover(None))
        return card

    @staticmethod
    def _fit_poster_height(
        card: ctk.CTkFrame,
        rows: tuple[tuple[ctk.CTkBaseClass, tuple[int, int]], ...],
    ) -> None:
        """把海报卡片定成**内容需要的高度**(不低于设计值 ``_POSTER_HEIGHT``).

        卡片原来是写死的 ``_POSTER_HEIGHT``, 而里面每一样都随"界面字号"变大 —— 字号调大
        之后名称那两行会把"最近活动"挤出卡片下沿, 看着就是下边框被文字盖住(用户
        2026-10-02 实测)。

        ``rows`` 给的是 (控件, 上下间距): 间距是**设计**值(逻辑像素), 控件请求高度是
        **物理**像素 —— 先换算再相加, 最后再换回逻辑写回 ``height``(CTk 的尺寸单位)。
        """
        scale = window_scaling(card)
        needed = sum(
            int(widget.winfo_reqheight()) + scaled_px(card, sum(pads))
            for widget, pads in rows
        )
        card.configure(height=max(_POSTER_HEIGHT, round(needed / scale)))

    @staticmethod
    def _poster_meta(item: HomeGameItem) -> str:
        """海报卡片名称下方的元信息: 备份数; 没有关联存档位置时说明原因.

        没有存档位置时说"0 备份"是误导(根本没地方备份), 因此直接给原因文案。
        """
        if item.location_count == 0:
            return tr("home.no_save_paths")
        return tr("home.poster_backups", count=item.backup_count)

    def _poster_status(self, cover: ctk.CTkFrame, backing: str) -> ctk.CTkLabel:
        """封面左下角的“启用中”绿点(调用方只在启用时创建它).

        ``backing`` 由调用方按“这一张卡片有没有封面图”给: 有图时用面板色做底衬,
        没有图时给 ``"transparent"``(取到封面自己的底色, 看不见底衬)。
        """
        status = ctk.CTkLabel(
            cover,
            text="●",
            corner_radius=6,
            fg_color=backing,
            text_color=self._palette.success,
            font=ctk.CTkFont(size=10, weight="bold"),
        )
        # 与右下角的备份数角标同一套内缩, 一左一右同高。
        status.grid(row=0, column=0, sticky="sw", padx=6, pady=6)
        return status

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
        picture = host_image(self.frame, light_image=loaded, size=size)
        self._artwork_images[key] = picture
        return picture

    def _render_empty(self, board: HomeBoard) -> None:
        """空状态: 卡片内**居中**摆一句结论 + 一句下一步 + 一个当场能点的动作.

        原先只有左上角两行小字, 却占着整张卡片 —— 密度低到像渲染失败(02 号评审的
        判据: 空状态要在卡片内居中, 且卡片里有一个**可点的主操作**)。于是这里把
        两块文字居中, 并在"库里真的没有游戏"时摆一个主色按钮直接送到「游戏发现」;
        筛选筛空时不摆按钮: 那时要改的是筛选条件, 不是"去发现"。
        """
        palette = self._palette
        holder = ctk.CTkFrame(self._list_box, fg_color="transparent")
        holder.pack(expand=True, fill="both")
        title = ctk.CTkLabel(
            holder,
            text=board.empty_message,
            anchor="center",
            justify="center",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=palette.text_primary,
        )
        title.pack(fill="x", padx=24, pady=(16, 0))
        hint = ctk.CTkLabel(
            holder,
            text=board.empty_hint,
            anchor="center",
            justify="center",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        hint.pack(fill="x", padx=24, pady=(6, 16))
        # 说明文字跟着滚动区宽度换行: 写死的宽度在宽窗口里会提前折行, 像被截断。
        track_wraplength(holder, hint, inset=48)
        self._empty_action = None
        if board.empty_library:
            self._empty_action = self._button(
                holder,
                tr("home.empty_go_discovery"),
                lambda: self._show_section(HomeSection.DISCOVERY),
                style="accent",
                width=132,
            )
            self._empty_action.pack(pady=(0, 20))
            # 空状态每次渲染都重建, 而它进的是一张"按可用性重绘"的登记表 —— 销毁时
            # 必须把登记摘掉, 否则下一轮重绘会去碰一个已经不存在的控件(TclError)。
            self._empty_action.bind(
                "<Destroy>",
                lambda _event, item=self._empty_action: self._buttons.pop(item, None),
                add="+",
            )

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
            font=ctk.CTkFont(size=FONT_GLYPH),
            text_color=self._tone_color(item.tone),
        )
        dot.pack(side="left")
        name = ctk.CTkLabel(
            left,
            # 先用兜底宽度裁一次: 直接放完整名称会让标签请求出上千像素, 把整张表
            # 撑到窗口之外(布局算出来的宽度跟着变, 会来回抖)。真实宽度由
            # _refit_row_names() 在首帧之后补上。兜底宽度是**设计**值(逻辑像素),
            # 而裁剪用的字体度量是**物理**像素。
            text=fit_text(
                item.name,
                self._name_font,
                scaled_px(self._list_box, _NAME_FALLBACK_WIDTH),
            ),
            anchor="w",
            justify="left",
            font=self._name_font,
            text_color=palette.text_body,
        )
        sync_tooltip(name, full=item.name)
        name.pack(side="left", fill="x", expand=True, padx=(_NAME_PAD, 0))
        # 名称标签盯住自己的宽度: 容器事件不一定来(见 _on_name_resize), 而"名称按当前
        # 宽度裁好"是要对用户兑现的。
        name.bind(
            "<Configure>", lambda event: self._on_name_resize(item.game_id, event)
        )
        self._row_parts[item.game_id] = _RowParts(left, columns, name, item.name)
        widgets: list[ctk.CTkBaseClass] = [row, left, columns, dot, name]
        widgets.extend(self._fill_row_columns(columns, item))
        for widget in widgets:
            widget.bind(
                "<Button-1>", lambda _event, key=item.game_id: self._select(key)
            )
            # 双击等同于"打开详情": 与列表类界面的习惯一致.
            widget.bind(
                "<Double-Button-1>", lambda _event, key=item.game_id: self._open(key)
            )
            # 悬停反馈: 与海报卡、详情页的备份卡片同一套(只重绘受影响的两张).
            widget.bind(
                "<Enter>", lambda _event, key=item.game_id: self._set_hover(key)
            )
            widget.bind("<Leave>", lambda _event: self._set_hover(None))
        return row

    def _fill_row_columns(
        self, columns: ctk.CTkFrame, item: HomeGameItem
    ) -> list[ctk.CTkBaseClass]:
        """填右侧固定列块, 返回新加的单元格标签.

        状态列是**唯一长度不受控**的列(标签数量可变): 状态一行、自定义标签一行,
        两行都放不下的部分补省略号(见 :func:`models.status_lines`)。
        """
        palette = self._palette
        # 折行预算要从设计尺寸换成**物理**像素(字体度量就是物理的), 与上面给列宽设的
        # ``minsize`` 用同一个值 —— 两边一致, 状态列才不会被内容顶宽。
        status_room = scaled_px(columns, _COLUMNS[-1][1])
        status = status_lines(
            item.state_chips,
            item.tags,
            # CTk 控件渲染的是**缩放后**的字体: 量文字必须跟着缩放, 否则每行都少算
            # 1.25 倍, Tk 会把它们再折一次(用户 2026-10-02: 状态列变成三行)。
            # 状态列不会因此引发重裁闭环: 它的宽度是列宽给的硬值(minsize), 内容不会
            # 反过来改变列宽 —— 与名称那一列的区别就在这里(见 `_name_available`)。
            measured_font(self._value_font, columns),
            status_room,
        )
        values = (
            (item.platform_label, 0),
            (str(item.location_count), 1),
            (str(item.backup_count), 2),
            (item.last_backup_label or "—", 3),
            (item.activity_label or "—", 4),
            (status, 5),
        )
        labels: list[ctk.CTkBaseClass] = []
        for text, index in values:
            _key, width, anchor = _COLUMNS[index]
            # 文本要按**物理**列宽裁(fit_label / font.measure 都是物理的); 只有
            # ``wraplength`` 是逻辑像素(CTk 自己乘缩放, 正好等于设计里的列宽)。
            room = scaled_px(columns, width)
            label = ctk.CTkLabel(
                columns,
                text="",
                anchor=anchor,
                justify="left" if index == 5 else "center",
                font=self._value_font,
                text_color=(
                    palette.danger if item.risk and index == 5 else palette.text_muted
                ),
            )
            if index == 5:
                # 状态列回到同一行时仍然不许溢出: wraplength 是硬上限, 换行由
                # status_lines 自己算好(状态一行、标签一行, 不切半个标签)。
                label.configure(wraplength=width, text=text)
                # status_lines 已经把两行排好(状态一行、标签一行);
                # " · ".join 是完整内容 —— 挂上悬停提示, 省掉的尾巴才看得到(这里
                # 不能再用 fit_label: 那会把已经排好的状态列重新压回一行)。
                sync_tooltip(label, full=" · ".join(item.chips), shown=text)
            else:
                fit_label(label, text, self._value_font, room)
            label.grid(row=0, column=index, sticky="ew", padx=_cell_pad(index))
            labels.append(label)
        return labels

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
        """选中项用"描边 + 浅底", 其余保持卡片配色.

        规则来自 :meth:`Palette.selection_colors`: 列表与海报共用同一套选中表达, 且
        刻意不用主按钮那种实心强调色 —— "我选中了谁"与"哪里能点"是两件事。
        悬停与选中也是两件事: **选中优先**(悬停不改选中的样子), 未选中悬停才提出
        :attr:`Palette.card_hover`(与详情页的备份卡片同一个 token)。
        """
        for key in self._rows:
            self._paint_row(key)

    def _row_colors(self, game_id: str) -> tuple[str, str]:
        """一张卡片该用的 (底色, 描边色): 选中 > 悬停 > 常规."""
        return card_surface_colors(
            self._palette,
            selected=game_id == self._selected,
            hovered=game_id == self._hover,
        )

    def _paint_row(self, game_id: str) -> None:
        """只重绘一张卡片(悬停是高频交互, 全量重绘开销大)."""
        row = self._rows.get(game_id)
        if row is None:
            return
        background, border = self._row_colors(game_id)
        try:
            row.configure(fg_color=background, border_width=1, border_color=border)
        except tk.TclError:
            # 卡片刚好随重绘被销毁: 忽略这一次局部重绘.
            return

    def _set_hover(self, game_id: str | None) -> None:
        """记录鼠标悬停的那一张卡片, 只重绘受影响的两张."""
        if self._hover == game_id:
            return
        previous, self._hover = self._hover, game_id
        for key in (previous, game_id):
            if key is not None:
                self._paint_row(key)

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
        self._restyle_buttons()

    def _select(self, game_id: str) -> None:
        """选中一款游戏并刷新动作可用性."""
        self._selected = game_id
        self._paint_rows()
        self._update_actions()

    def _request_export_batch(self) -> None:
        """请求主窗口发起批量导出(多选对话框与保存框都必须在主线程里弹).

        主页只负责"用户点了这个按钮": 弹框、后台打包与反馈都在主窗口(它才有忙碌状态
        与消息队列)。没有接线时什么都不做(与其它回调为 None 的处理一致)。
        """
        if self._on_export_batch is not None:
            self._on_export_batch()

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
        # 批量导出与选中的那一行无关(它在对话框里多选), 因此不随选中态变化 ——
        # 只有主窗口在跑长操作时才收起它(那时点了也不会弹框)。
        self._export_batch_btn.configure(state="disabled" if self._busy else "normal")
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
            state="normal" if item.backup_enabled and not self._busy else "disabled"
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
        """按可用性重绘动作按钮(禁用态统一压暗, 可用态回到各自样式)."""
        self._restyle_buttons()

    def set_busy(self, busy: bool) -> None:
        """主窗口报告长操作开始/结束: 忙碌期间收起本页的长操作入口.

        主页的动作是同步执行的(不经主窗口的忙碌通路), 所以只有两个走主窗口或其
        处理器会因 busy 静默返回的入口需要置灰 —— "立即备份"与"批量导出":
        前者会与正在跑的备份抢同一个后端操作槽, 后者点了根本不会弹框.
        """
        self._busy = busy
        self._update_actions()

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
        """编辑选中游戏的自定义标签(一行一个, 与导入时调整存档路径同一个样式)."""
        item = self._item()
        if item is None:
            self._summary_label.configure(text=tr("home.require_game"))
            return
        if not item.allow("tags"):
            self._report_archived(item)
            return
        tags = edit_tags_dialog(
            self.frame,
            self._palette,
            tags=item.tags,
            max_tags=MAX_TAGS,
            max_length=MAX_TAG_LENGTH,
        )
        if tags is None:
            return
        try:
            self._backend.set_game_tags(item.game_id, list(tags))
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
