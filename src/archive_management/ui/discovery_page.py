"""本地游戏发现(已并入游戏主页).

这是主窗口内的一页(不是弹窗): 游戏主页的"游戏发现"分区显示它, 内部分成两个
子页签(默认停在"探测结果"):

- **探测结果**: 从 Steam、Epic、GOG、Ubisoft 的安装清单与注册表, 以及监控
  目录中发现的候选游戏。可按"全部/待处理/已导入/已忽略"筛选, 并对选中项执行
  "导入为游戏 / 忽略(恢复) / 修正路径"; 每行还会展示**探测这款游戏时顺带推断
  出的存档路径**(平台模块支持才会推理), 导入时在对话框里可以改;
- **监控目录**: 用户自行添加的目录, 用于覆盖平台客户端未安装、未登录或存档路径
  自定义的情况。每行显示实时路径状态(可用/不存在/不是文件夹/不可读/高风险)与
  上次扫描时间, 支持添加、编辑、启用/停用与删除。

"重新扫描"固定放在页签行右侧, 两个子页都能随时触发。这里只做展示与交互编排:
路径校验、重复检测与落库都在后端完成, 失败时把后端给出的原因展示在弹窗与状态
文案里。所有文案来自 i18n, 便于无头测试。

三处界面规则(来自逐页评审):

- 说明文字与筛选控件**分栏**: 说明在左列(按列宽换行), 控件在右列, 两者不互挤;
- 动作按钮与列表的选中状态绑定: 没选中任何一项时按钮置灰, 并在按钮左边写明
  "这一排按钮作用于谁";
- 长路径**中间省略**, 卡片上下留出内边距, 滚到底时不会顶着一行被裁掉的字。
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
from archive_management.ui.dialogs import (
    ask_text,
    confirm_dialog,
    import_game_dialog,
    info_dialog,
)
from archive_management.ui.models import (
    CandidateFilter,
    CandidateItem,
    DiscoveryPage,
    GameSummary,
    MonitoredDirItem,
    listable_candidates,
)
from archive_management.ui.palette import Palette
from archive_management.ui.pickers import pick_directory
from archive_management.ui.textfit import fit_path
from archive_management.ui.typography import FONT_STRONG
from archive_management.ui.widgets import (
    ButtonStyle,
    auto_scrollbar,
    measured_font,
    paint_button_disabled,
    paint_button_style,
    track_fit,
    track_wraplength,
)

_ChangeCallback = Callable[[], None]
_NoticeCallback = Callable[[str], None]

logger = logging.getLogger(__name__)

# 卡片内文字/列表相对卡片边缘的内缩: 与游戏库的卡片保持一致(那里也是 10).
_PANEL_PAD = 10
# 数据行右侧再留一点: 滚动条一出现就贴在画布右边, 最后一行文字不该顶上去.
_SCROLL_INSET = 6
# 说明文字的右侧预留: 同一行的筛选控件/按钮大概占这么宽, 说明不许压过去.
_HINT_INSET = 190
# 卡片正文的兜底宽度: 真实宽度要等布局完成(容器的 Configure)才知道.
_CARD_TEXT_WIDTH = 680
# 卡片正文左右各 12 的内边距: 按容器宽度算可用宽度时要减掉.
_CARD_TEXT_INSET = 24


class DiscoveryPanel:
    """本地游戏发现的页面内容(主窗口的一页, 由游戏主页承载)."""

    def __init__(
        self,
        parent: ctk.CTkFrame,
        *,
        backend: ArchiveService,
        palette: Palette,
        on_change: _ChangeCallback | None = None,
        on_notice: _NoticeCallback | None = None,
    ) -> None:
        """在 ``parent`` 内构造页面并装载监控目录与探测结果(默认停在"探测结果").

        ``on_notice`` 用于把需要用户看到的一句话送进主窗口的底部状态栏(例如
        "开始尝试探测封面"), 页面自身不弹窗。
        """
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._on_change = on_change
        self._on_notice = on_notice
        self._dirs: list[MonitoredDirItem] = []
        self._candidates: list[CandidateItem] = []
        self._dir_rows: dict[str, ctk.CTkFrame] = {}
        self._cand_rows: dict[str, ctk.CTkFrame] = {}
        self._selected_dir: str | None = None
        self._selected_candidate: str | None = None
        # 按钮与它们的样式: 可用/禁用切换时按这份登记重绘配色.
        self._styles: dict[ctk.CTkButton, ButtonStyle] = {}
        # 与卡片正文同规格的字体, 用来量文本宽度.
        self._path_font = ctk.CTkFont(size=12)
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
        """构造三个子页(同格叠放), 由 :meth:`_show_page` 决定显示哪一个."""
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
        """切换到指定子页并重绘页签(默认页面是"探测结果").

        底栏计数也随页面切换: 两个子页的计数口径不同, 沿用上一个子页的数字会让人
        误以为当前页在统计别的东西。
        """
        self._page = page
        for kind, frame in self._page_frames.items():
            if kind is page:
                frame.grid()
            else:
                frame.grid_remove()
        self._paint_tabs()
        self._render_counts()

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
            anchor="nw",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._dirs_hint.grid(
            row=0, column=0, padx=(_PANEL_PAD, 16), pady=(16, 0), sticky="ew"
        )
        # 说明按可用宽度换行: 固定宽度在宽窗口里会提前折行, 看起来像被截断。
        track_wraplength(panel, self._dirs_hint, inset=_HINT_INSET)

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=0, column=1, padx=_PANEL_PAD, pady=(16, 0), sticky="ne")
        self._add_dir_btn = self._button(
            actions,
            tr("discovery.dir_add"),
            self._on_add_dir,
            style="accent",
            width=76,
        )
        self._edit_dir_btn = self._button(
            actions, tr("discovery.dir_edit"), self._on_edit_dir, width=76
        )
        self._toggle_dir_btn = self._button(
            actions, tr("discovery.dir_disable"), self._on_toggle_dir, width=76
        )
        # 删除保持危险色, 但改成"描边 + 危险色文字": 破坏性动作不该比"添加"更抢眼.
        self._remove_dir_btn = self._button(
            actions,
            tr("discovery.dir_remove"),
            self._on_remove_dir,
            style="danger_soft",
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
        self._order_dir_buttons()

        self._dirs_box = ctk.CTkScrollableFrame(
            panel,
            fg_color=palette.well,
            corner_radius=8,
        )
        self._dirs_box.grid(
            row=1, column=0, columnspan=2, padx=_PANEL_PAD, pady=(10, 16), sticky="nsew"
        )
        self._dirs_box.grid_columnconfigure(0, weight=1)
        auto_scrollbar(self._dirs_box)

    def _order_dir_buttons(self) -> None:
        """把"添加"放在最左边并贴近编辑/启停, "删除"拉开一段距离(避免误点)."""
        self._remove_dir_btn.grid_configure(padx=(16, 0))

    def _build_candidates(self, panel: ctk.CTkFrame) -> None:
        """探测结果页: 说明 + 筛选 + 导入/忽略/修正路径 + 整页高度的候选列表."""
        palette = self._palette
        panel.grid_columnconfigure(0, weight=1)
        panel.grid_rowconfigure(2, weight=1)

        self._hint_label = ctk.CTkLabel(
            panel,
            text=tr("discovery.hint"),
            anchor="nw",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._hint_label.grid(
            row=0, column=0, padx=(_PANEL_PAD, 16), pady=(16, 0), sticky="ew"
        )
        # 与筛选下拉分栏: 说明只占左列, 右列留给控件(两者不再互相挤压).
        track_wraplength(panel, self._hint_label, inset=_HINT_INSET)

        filters = ctk.CTkFrame(panel, fg_color="transparent")
        filters.grid(row=0, column=1, padx=_PANEL_PAD, pady=(16, 0), sticky="ne")
        self._filter_box = ctk.CTkComboBox(
            filters,
            values=[item.label for item in CandidateFilter],
            width=140,
            command=self._on_filter_change,
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
        self._filter_box.set(self._filter.label)
        self._filter_box.pack(side="left")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(
            row=1, column=0, columnspan=2, padx=_PANEL_PAD, pady=(10, 0), sticky="ew"
        )
        # 左边写明这一排按钮作用于谁: 列表里没有选中项时按钮置灰并说明原因,
        # 而不是让三个按钮"悬"在筛选下方看不出作用对象(03/08 号评审).
        actions.grid_columnconfigure(0, weight=1)
        self._acting_label = ctk.CTkLabel(
            actions,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._acting_label.grid(row=0, column=0, sticky="w")
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
            button.grid(row=0, column=index + 1, padx=(6, 0))
        # 取消忽略/忽略是逐条动作, 而"清空扫描结果"是整页动作(用户 2026-10-03):
        # 它一次清掉所有非已导入的候选(已忽略的先按名字记下), 之后重新扫一遍就是干净的。
        # 它是**破坏性**动作: 按其风格走 danger, 而且要点确认。
        self._clear_btn = self._button(
            actions, tr("discovery.clear_scan"), self._on_clear_scan, style="danger"
        )
        self._clear_btn.grid(row=0, column=4, padx=(6, 0))

        self._cand_box = ctk.CTkScrollableFrame(
            panel,
            fg_color=palette.well,
            corner_radius=8,
        )
        self._cand_box.grid(
            row=2, column=0, columnspan=2, padx=_PANEL_PAD, pady=(10, 16), sticky="nsew"
        )
        self._cand_box.grid_columnconfigure(0, weight=1)
        auto_scrollbar(self._cand_box)

    def _button(
        self,
        parent: ctk.CTkFrame,
        text: str,
        command: Callable[[], None],
        *,
        style: ButtonStyle = "ghost",
        width: int = 88,
    ) -> ctk.CTkButton:
        """按窗口调色板创建一个按钮(样式登记下来, 之后按可用性重绘)."""
        button = ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=30,
            corner_radius=8,
            font=ctk.CTkFont(size=12, weight="bold"),
        )
        paint_button_style(button, self._palette, style)
        self._styles[button] = style
        return button

    def _restyle_buttons(self) -> None:
        """按可用性重绘本页所有按钮: 禁用态统一压暗, 可用态回到各自样式.

        强调项只有两处(页签行右侧的"重新扫描"与监控目录页的"添加"); 删除是危险色
        描边, 破坏性动作不再比主操作更抢眼(10 号评审)。
        """
        palette = self._palette
        for button, style in self._styles.items():
            if str(button.cget("state")) == "disabled":
                paint_button_disabled(button, palette)
                continue
            paint_button_style(button, palette, style)

    # -- 数据加载与渲染 -----------------------------------------------------

    def reload(self) -> None:
        """重新读取监控目录与探测结果, 并尽量保持选中项.

        读取失败时保留上次内容并记录日志: 本方法在构造过程中也会被调用,
        让异常逃出去等于整个主窗口起不来(磁盘/数据库瞬时不可读时尤其明显)。

        候选先过一遍 :func:`listable_candidates`: **已导入的不进这一页**(它就是
        游戏库里的一款游戏, 在库里有完整动作), 因此底下的筛选/计数/空状态都不用
        再关心它。
        """
        try:
            dirs = self._backend.list_monitored_directories()
            candidates = listable_candidates(self._backend.list_candidates())
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

        **每个子页只报自己的数字, 而且只占一行**: 在监控目录页显示"候选 23 项 · 待处理
        13 项"会让人以为这一页也在统计别的东西; 两行计数(第一行总数/待处理、第二行
        待处理/已忽略)又是在说同一批数(第 3/10 号评审)。因此探测结果页把三种状态**并列
        在同一行**里 —— 筛选到"已忽略"时一条都没有时, 用户仍能从这一行看出原因, 而
        不用先找第二行。

        分列里**只有未导入的两种状态**: 已导入的候选不进这一页(它们就是游戏库里的
        游戏), 所以分母也不会把它们算进去。
        第二行留给扫描结果(``_on_scan`` 会把"扫描了…"写进去)。
        """
        counts = self._status_counts()
        if self._page is DiscoveryPage.MONITORED:
            self._summary_label.configure(
                text=tr("discovery.counts", dirs=len(self._dirs)),
                text_color=self._palette.text_body,
            )
            self._detail_label.configure(text="")
            return
        self._summary_label.configure(
            text=tr(
                "discovery.counts_candidates",
                candidates=len(self._candidates),
                pending=counts["new"],
                ignored=counts["ignored"],
            ),
            text_color=self._palette.text_body,
        )
        self._detail_label.configure(text="")

    def _status_counts(self) -> dict[str, int]:
        """按处理进度统计候选数量(空状态文案与底部计数共用)."""
        counts = {"new": 0, "ignored": 0}
        for item in self._candidates:
            if item.status in counts:
                counts[item.status] += 1
        return counts

    def _render_dirs(self) -> None:
        for child in self._dirs_box.winfo_children():
            child.destroy()
        self._dir_rows = {}
        if not self._dirs:
            self._render_empty(
                self._dirs_box,
                title=tr("discovery.dirs_empty"),
                hint=tr("discovery.dirs_empty_hint"),
            )
            self._selected_dir = None
            self._update_actions()
            return
        if self._selected_dir not in {item.directory_id for item in self._dirs}:
            self._selected_dir = self._dirs[0].directory_id
        for item in self._dirs:
            row = self._build_dir_row(item)
            row.pack(fill="x", padx=(4, _SCROLL_INSET), pady=2)
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
            text_color=self._palette.text_primary,
        )
        name.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        # 启用状态单独一个标签: "已停用"是要被注意的状态, 不该与"路径可用"同色.
        detail = ctk.CTkFrame(row, fg_color="transparent")
        detail.grid(row=1, column=0, padx=12, pady=(2, 8), sticky="ew")
        state = ctk.CTkLabel(
            detail,
            text=item.state_label,
            anchor="w",
            font=ctk.CTkFont(size=11, weight="bold" if item.enabled else "normal"),
            text_color=(
                self._palette.text_muted
                if item.state_tone == "ok"
                else self._palette.danger
            ),
        )
        state.pack(side="left")
        rest = ctk.CTkLabel(
            detail,
            text=item.detail,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self._palette.text_muted,
        )
        rest.pack(side="left", padx=(6, 0))
        for widget in (row, name, detail, state, rest):
            widget.bind(
                "<Button-1>",
                lambda _event, key=item.directory_id: self._select_dir(key),
            )
        return row

    def _render_empty(
        self, box: ctk.CTkScrollableFrame, *, title: str, hint: str
    ) -> None:
        """列表空状态: 一句结论 + 一句下一步(表头/表格骨架都不显示时它就是这一页的内容)."""
        headline = ctk.CTkLabel(
            box,
            text=title,
            anchor="w",
            font=ctk.CTkFont(size=FONT_STRONG, weight="bold"),
            text_color=self._palette.text_primary,
        )
        headline.pack(fill="x", padx=6, pady=(12, 0))
        detail = ctk.CTkLabel(
            box,
            text=hint,
            anchor="nw",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=self._palette.text_hint,
        )
        detail.pack(fill="x", padx=6, pady=(6, 12))
        track_wraplength(box, detail)

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
        for index, item in enumerate(visible):
            row = self._build_candidate_row(item)
            # 上下都留出内边距: 滚到底时顶部那张卡片不会被贴着边缘裁掉(8/9 号评审).
            row.pack(
                fill="x", padx=(4, _SCROLL_INSET), pady=(6 if index == 0 else 3, 6)
            )
            self._cand_rows[item.candidate_id] = row
        self._paint_candidates()
        self._update_actions()

    def _render_empty_candidates(self) -> None:
        """候选列表空状态: 区分"还没有候选"与"当前筛选没有匹配项".

        两种情况都给出同一句"暂无探测结果"会让人以为筛选失效(筛选到"已导入"
        而当时一条都没导入时尤其明显), 因此这里分开措辞; 而且它往往是这一页唯一
        的行动指引, 所以用结论 + 说明两层, 不再是最弱的一行灰字。
        """
        filtered = bool(self._candidates) and self._filter is not CandidateFilter.ALL
        if filtered:
            self._render_empty(
                self._cand_box,
                title=tr("discovery.empty_filtered", filter=self._filter.label),
                hint=tr("discovery.empty_filtered_hint", **self._status_counts()),
            )
            return
        self._render_empty(
            self._cand_box,
            title=tr("discovery.candidates_empty"),
            hint=tr("discovery.candidates_empty_hint"),
        )

    def _build_candidate_row(self, item: CandidateItem) -> ctk.CTkFrame:
        row = ctk.CTkFrame(self._cand_box, corner_radius=8)
        row.grid_columnconfigure(0, weight=1)
        title_text = self._candidate_title(item)
        name = ctk.CTkLabel(
            row,
            text=title_text,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=self._palette.text_primary,
        )
        name.grid(row=0, column=0, padx=12, pady=(8, 0), sticky="ew")
        # 标题也要按真实宽度裁: 译名 + 括号里的原名可以很长, 不裁会被卡片硬切.
        track_fit(row, name, (title_text,), inset=_CARD_TEXT_INSET)
        # 安装路径: 长路径中间省略, 宽度变化时重裁(与主页列表的名称同一套做法).
        path_text = self._clip_path(item.install_dir, _CARD_TEXT_WIDTH)
        path = ctk.CTkLabel(
            row,
            text=path_text,
            anchor="w",
            font=self._path_font,
            text_color=self._palette.text_muted,
        )
        path.grid(row=1, column=0, padx=12, pady=(2, 0), sticky="ew")
        track_fit(row, path, (item.install_dir,), path=True, inset=_CARD_TEXT_INSET)
        detail = ctk.CTkLabel(
            row,
            text=item.summary,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self._palette.text_muted,
        )
        detail.grid(row=2, column=0, padx=12, pady=(2, 0), sticky="ew")
        track_fit(row, detail, (item.summary,), inset=_CARD_TEXT_INSET)
        # 存档路径区分两层: 结论(一行)与路径(每条一行), 路径同样中间省略.
        conclusion = ctk.CTkLabel(
            row,
            text=item.save_label,
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=self._save_color(item),
        )
        conclusion.grid(row=3, column=0, padx=12, pady=(4, 0), sticky="ew")
        lines = tuple(f"· {path_.text}" for path_ in item.save_paths)
        saves = ctk.CTkLabel(
            row,
            text=self._clip_lines(lines, _CARD_TEXT_WIDTH),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=(
                self._palette.danger
                if any(path_.dangerous for path_ in item.save_paths)
                else self._palette.text_muted
            ),
        )
        saves.grid(row=4, column=0, padx=12, pady=(2, 10), sticky="ew")
        if lines:
            track_fit(row, saves, lines, path=True, inset=_CARD_TEXT_INSET)
        for widget in (row, name, path, detail, conclusion, saves):
            widget.bind(
                "<Button-1>",
                lambda _event, key=item.candidate_id: self._select_candidate(key),
            )
        return row

    def _clip_path(self, text: str, width: int) -> str:
        """按宽度裁剪一行路径: **中间省略**, 头尾都留.

        尾部是目录名(最有用的一段), 因此不能用从头截断的 :func:`fit_text`。
        """
        return fit_path(text, measured_font(self._path_font, self.frame), width)

    def _clip_lines(self, lines: tuple[str, ...], width: int) -> str:
        """多行文本逐行裁剪后拼起来(每行都不许溢出)."""
        return "\n".join(self._clip_path(line, width) for line in lines)

    @staticmethod
    def _candidate_title(item: CandidateItem) -> str:
        """行标题: 有当前语言译名就显示译名, 并把探测到的原名放进括号里."""
        if item.localized_name and item.localized_name != item.name:
            return f"{item.localized_name}  ({item.name})"
        return item.name

    @staticmethod
    def _save_lines(item: CandidateItem) -> str:
        """候选行里的存档路径区: 结论一行, 之后每条路径一行.

        没有存档路径时只剩结论那一行(调用方按行数决定要不要登记重裁)。
        """
        if not item.save_paths:
            return item.save_label
        lines = [item.save_label]
        lines.extend(f"· {path.text}" for path in item.save_paths)
        return "\n".join(lines)

    def _save_color(self, item: CandidateItem) -> str:
        """存档路径区的颜色: 危险路径用警告色, 有结果用成功色, 其余弱化."""
        palette = self._palette
        if any(path.dangerous for path in item.save_paths):
            return palette.danger
        if item.save_paths:
            return palette.success
        return palette.text_muted

    def _paint_dirs(self) -> None:
        for key, row in self._dir_rows.items():
            background, border = self._palette.selection_colors(
                key == self._selected_dir
            )
            row.configure(fg_color=background, border_width=1, border_color=border)

    def _paint_candidates(self) -> None:
        for key, row in self._cand_rows.items():
            background, border = self._palette.selection_colors(
                key == self._selected_candidate
            )
            row.configure(fg_color=background, border_width=1, border_color=border)

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
        """按选中项与状态决定按钮可用性与文案.

        三个候选动作挂在"列表里选中的那一条"上: 没有选中项时一起置灰, 并在按钮
        左边写明这一点 —— 否则按钮悬在筛选下方, 看不出作用对象(03/08 号评审)。
        """
        directory = self._dir_item()
        candidate = self._candidate_item()
        self._update_dir_actions(directory)
        self._update_candidate_actions(candidate)
        self._acting_label.configure(
            text=(
                tr("discovery.acting_on", name=candidate.display_name)
                if candidate is not None
                else tr("discovery.no_selection")
            ),
            text_color=(
                self._palette.text_hint
                if candidate is not None
                else self._palette.text_muted
            ),
        )
        self._restyle_buttons()

    def _update_dir_actions(self, directory: MonitoredDirItem | None) -> None:
        """监控目录页的四个按钮: 除"添加"外都要求先选中一条."""
        self._add_dir_btn.configure(state="normal")
        for button in (
            self._edit_dir_btn,
            self._toggle_dir_btn,
            self._remove_dir_btn,
        ):
            button.configure(state="normal" if directory is not None else "disabled")
        if directory is None:
            return
        self._toggle_dir_btn.configure(
            text=(
                tr("discovery.dir_disable")
                if directory.enabled
                else tr("discovery.dir_enable")
            )
        )

    def _update_candidate_actions(self, candidate: CandidateItem | None) -> None:
        """探测结果页的三个按钮: 选中后才能用, 且不可导入的候选只能忽略/修正."""
        for button in (
            self._import_btn,
            self._ignore_btn,
            self._relocate_btn,
        ):
            button.configure(state="normal" if candidate is not None else "disabled")
        if candidate is None:
            return
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

    def _on_clear_scan(self) -> None:
        """清空探测结果(需要确认): 已导入的候选与按名字记住的忽略都会保留."""
        if not confirm_dialog(
            self.frame,
            self._palette,
            title=tr("dialog.clear_scan_title"),
            message=tr("dialog.clear_scan_message"),
            confirm_text=tr("discovery.clear_scan"),
            danger=True,
        ):
            log_action("ui.clear_scan_results", basic=True, result="cancelled")
            return
        log_action("ui.clear_scan_results", basic=True)
        try:
            removed = self._backend.clear_scan_results()
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()
        self._summary_label.configure(text=tr("discovery.clear_done", count=removed))

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
            danger=True,
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
        """导入选中的探测结果: 确认名称与存档路径, 再开始探测封面与图标."""
        candidate = self._candidate_item()
        if candidate is None or not candidate.importable:
            return
        chosen = import_game_dialog(
            self.frame,
            self._palette,
            title=tr("dialog.candidate_import_title"),
            name_label=tr("dialog.candidate_import_prompt"),
            initial_name=candidate.name,
            paths_label=self._paths_label(candidate),
            paths_hint=candidate.save_label,
            initial_paths=tuple(item.path for item in candidate.save_paths),
            add_text=tr("dialog.import_add_path"),
            confirm_text=tr("dialog.import_confirm"),
            browse=lambda: pick_directory(title=tr("dialog.import_browse_title")),
        )
        if chosen is None:
            log_action("discovery.import", basic=True, result="cancelled")
            return
        name, paths = chosen
        try:
            summary = self._backend.import_candidate(
                candidate.candidate_id, name=name, save_paths=paths
            )
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.reload()
        self._summary_label.configure(text=self._import_message(summary))
        self._start_probes(summary)
        if self._on_change is not None:
            self._on_change()

    @staticmethod
    def _import_message(summary: GameSummary) -> str:
        """导入后的页面提示: 写了几条存档位置就说几条."""
        if summary.saved_paths:
            return tr(
                "result.game_added_with_paths",
                name=summary.name,
                count=summary.saved_paths,
            )
        return tr("result.game_added", name=summary.name)

    def _paths_label(self, candidate: CandidateItem) -> str:
        """导入对话框里路径区的小标题(平台不支持时说明原因)."""
        if not candidate.save_supported:
            return tr("dialog.import_paths_unsupported")
        return tr("dialog.import_paths")

    def _start_probes(self, summary: GameSummary) -> None:
        """导入成功后开始后台探测封面/图标与译名, 并把封面这件事写到状态栏.

        两者都要平台支持: 不支持的平台什么都不做, 也不该弹错误(导入本身已经成功)。
        译名探测完成后会改写游戏名, 数据版本一变界面就会重绘。
        """
        try:
            self._backend.prefetch_artwork()
            self._backend.prefetch_names()
        except ArchiveManagementError as exc:  # pragma: no cover - 探测失败不影响使用
            logger.debug("请求后台探测失败: %s", exc)
            return
        if self._on_notice is not None:
            self._on_notice(tr("discovery.artwork_started", name=summary.name))

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
