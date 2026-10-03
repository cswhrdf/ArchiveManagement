"""管理游戏与存档位置窗口.

一个模态浮层: 上半部管理游戏信息(重命名/停用/删除), 下半部管理该
游戏的"原始存档位置"(添加目录/文件、设为主位置、重新验证、编辑路径、
删除记录), 并提供"删除原始存档位置": 把磁盘上的原目录移入
系统回收站(需输入游戏名称确认, 不会永久删除)。界面明确区分原始位置与
由应用管理的备份目录, 避免误操作。
本模块采用组合式窗口, 便于无头测试。
"""

from __future__ import annotations

from collections.abc import Callable

import customtkinter as ctk

from archive_management.domain import GameAction, PathKind, action_allowed
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import (
    _present,
    ask_text,
    confirm_dialog,
    info_dialog,
)
from archive_management.ui.metrics import RADIUS_LG, RADIUS_MD
from archive_management.ui.models import LocationItem, size_label
from archive_management.ui.palette import Palette
from archive_management.ui.pickers import pick_directory, pick_file
from archive_management.ui.schedule_window import edit_schedule
from archive_management.ui.widgets import (
    ButtonStyle,
    attach_tooltip,
    auto_scrollbar,
    card_surface_colors,
    fit_label,
    paint_button_state,
    paint_button_style,
    track_fit,
    window_scaling,
)

_ChangeCallback = Callable[[], None]


def _kind_text(kind: PathKind) -> str:
    """返回目录/文件的展示文案."""
    return tr("loc.kind_dir") if kind == "directory" else tr("loc.kind_file")


def _chip_text(kind: PathKind, *, primary: bool) -> str:
    r"""位置行左侧那枚标签: 主位置写"主目录/主文件", 其余只写类型.

    出处(2026-10-02 用户反馈): 主位置原来是在**路径前面加一个字**("主  C:\..."), 看起来
    像路径自己的一部分(容易被读成别的字), 所以改成由这枚标签表达 —— 路径原样显示。
    标签宽 56px 装得下最长的英文标签(实测 11 号字下 "Main file" = 44px)。
    """
    if not primary:
        return _kind_text(kind)
    return tr("loc.primary_dir" if kind == "directory" else "loc.primary_file")


# 窗口是固定的 600x600, 标题左边只腾得下 150px(右边四个动作按钮占 414px): 长名称
# 如果不受限, 头部请求宽度会到 1202px —— 整个头部被挤到窗口之外, 按钮也看不到。
# 上限取下实测值再去掉标签自身的内边距, 最多两行, 超出补省略号(完整名称在
# 重命名对话框与游戏主页里都能看到)。
_TITLE_TEXT_WIDTH = 118
_TITLE_TEXT_LINES = 2

_WINDOW_WIDTH = 600
_WINDOW_PAD_Y = 16
# 位置行里“路径那一列”要让开的宽度: 类型标签 56 + 左侧 10 + 右侧 8 + 路径自己的右内边距 8.
_ROW_TEXT_INSET = 56 + 10 + 8 + 8
# 窗口打开时的初始高度: 实际高度随后按内容算(见 _fit_window_height), 所以它只是一个
# 合理的起点 —— 不再当内容的下限用(否则位置少的时候底部会空出一大块)。
_WINDOW_MIN_HEIGHT = 440
# 位置列表的高度跟着内容走: 一条位置时只占一行的高度(不在卡片里空出一大块,
# 15 号评审), 超过上限则由列表自己滚动。
_LIST_MIN_HEIGHT = 88
_LIST_MAX_HEIGHT = 200


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
        archived: bool = False,
    ) -> None:
        """构造管理窗口并装载该游戏的存档位置."""
        self._parent = parent
        self._backend = backend
        self._palette = palette
        self._game_id = game_id
        self._name = name
        self._enabled = enabled
        self._archived = archived
        self._backup_location = backup_location
        self._on_change = on_change
        self._items: list[LocationItem] = []
        self._selected: str | None = None
        self._rows: dict[str, list[ctk.CTkBaseClass]] = {}
        # 鼠标悬停的那一行位置: 与主页卡片/详情页备份卡片同一套反馈.
        self._hover: str | None = None
        # 本窗口按钮与它们的样式: 归档置灰(以及恢复可用)时要按 state 重绘配色。
        self._buttons: dict[ctk.CTkButton, ButtonStyle] = {}
        self._build()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        palette = self._palette
        window = ctk.CTkToplevel(self._parent)
        self._window = window
        window.title(tr("manage.title"))
        window.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_MIN_HEIGHT}")
        window.resizable(False, False)
        window.transient(self._parent)
        window.grab_set()
        window.configure(fg_color=palette.background)
        _present(self._parent, window)

        container = ctk.CTkFrame(window, fg_color=palette.background)
        container.pack(fill="both", expand=True, padx=16, pady=16)
        container.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(container, fg_color=palette.panel, corner_radius=10)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.grid_columnconfigure(0, weight=1)
        self._title_font = ctk.CTkFont(size=16, weight="bold")
        self._title_label = ctk.CTkLabel(
            header,
            text="",
            anchor="w",
            justify="left",
            wraplength=_TITLE_TEXT_WIDTH,
            font=self._title_font,
            text_color=palette.text_primary,
        )
        self._title_label.grid(row=0, column=0, padx=16, pady=(16, 0), sticky="w")
        self._state_label = ctk.CTkLabel(
            header,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        # 标题与状态两行是**一块**文字: 两端留白要一样, 否则整块在页头里偏下 ——
        # 2026-10-02 用户实测"标记启停状态的文字没有上下居中, 而是在区域中居下":
        # 那时标题上面留 16、状态行下面只留 4, 于是状态那行看着贴在页头下沿。
        # 右边那组按钮是 ``rowspan=2`` 且自己居中(实测 y=30、高 37, 正好落在页头中线),
        # 所以只要这两行对称, 三者在页头里的中线就一致了。
        self._state_label.grid(row=1, column=0, padx=16, pady=(2, 16), sticky="w")

        header_actions = ctk.CTkFrame(header, fg_color="transparent")
        header_actions.grid(row=0, column=1, rowspan=2, padx=12)
        self._schedule_btn = self._make_button(
            header_actions, tr("manage.schedule"), self._on_schedule, width=104
        )
        self._schedule_btn.pack(side="left", padx=(0, 6))
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
        # 危险动作穿危险色: 与下方的"删除原始存档位置"一致(15 号评审)。
        self._delete_btn = self._make_button(
            header_actions,
            tr("manage.delete_game"),
            self._on_delete_game,
            width=92,
            danger=True,
        )
        self._delete_btn.pack(side="left")

        # 「定时备份」是一节小标题, 值单独一行用次要色: 它与"原始存档位置"不能
        # 长得一模一样, 否则整窗自上而下没有层级(15 号评审)。
        self._schedule_heading = ctk.CTkLabel(
            container,
            text=tr("manage.schedule_heading"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        self._schedule_heading.grid(row=1, column=0, sticky="w", pady=(10, 0))
        self._schedule_state = ctk.CTkLabel(
            container,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._schedule_state.grid(row=2, column=0, sticky="w", pady=(2, 6))

        locations_title = ctk.CTkLabel(
            container,
            text=tr("manage.locations_title"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        locations_title.grid(row=3, column=0, sticky="w", pady=(0, 6))

        self._list_scroll = ctk.CTkScrollableFrame(
            container,
            fg_color=palette.panel,
            corner_radius=10,
            height=_LIST_MIN_HEIGHT,
        )
        self._list_scroll.grid(row=4, column=0, sticky="ew", pady=(0, 8))
        self._list_scroll.grid_columnconfigure(0, weight=1)
        auto_scrollbar(self._list_scroll)

        # 备份目录是应用自己管理的目录, 正文只说"不用在这里配", 真实路径收进悬停提示:
        # 把脚本/临时路径整条摊在正文里还会折行, 用户看不懂也用不上(15 号评审)。
        note = ctk.CTkLabel(
            container,
            text=tr("manage.backup_note"),
            anchor="w",
            wraplength=540,
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        note.grid(row=5, column=0, sticky="w", pady=(0, 8))
        attach_tooltip(note, tr("manage.backup_note_tip", path=self._backup_location))

        action_bar = ctk.CTkFrame(container, fg_color="transparent")
        action_bar.grid(row=6, column=0, sticky="w")
        self._location_buttons: list[ctk.CTkButton] = []
        # 两行两组: 第一行是"新增(主操作, 强调色) + 管理现有位置", 第二行只放破坏性的
        # "删除"。六个按钮挤一行时总宽(96+96+108+92+92+80 = 564)已经等于容器可用宽度,
        # 再加间距就必然溢出 —— 最右边的"删除"会被窗口边缘裁掉半颗(15 号评审的回归)。
        for text, handler, width, gap, primary in (
            (tr("loc.add_dir"), self._on_add_directory, 96, 0, True),
            (tr("loc.add_file"), self._on_add_file, 96, 6, False),
            (tr("loc.set_primary"), self._on_set_primary, 108, 20, False),
            (tr("loc.verify"), self._on_verify, 92, 6, False),
            (tr("loc.edit"), self._on_edit_path, 92, 6, False),
        ):
            button = self._make_button(
                action_bar, text, handler, width=width, primary=primary
            )
            button.grid(row=0, column=len(self._location_buttons), padx=(gap, 0))
            self._location_buttons.append(button)
        remove = self._make_button(
            action_bar,
            tr("loc.remove"),
            self._on_remove,
            width=80,
            danger=True,
        )
        remove.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._location_buttons.append(remove)

        danger_bar = ctk.CTkFrame(container, fg_color="transparent")
        danger_bar.grid(row=7, column=0, sticky="w", pady=(8, 0))
        self._delete_origin_btn = self._make_button(
            danger_bar,
            tr("loc.delete_origin"),
            self._on_delete_origin,
            width=168,
            danger=True,
        )
        self._delete_origin_btn.pack(side="left")

        # 「关闭」是中性动作: 不穿危险色(15 号评审)。
        self._close_btn = self._make_button(
            container, tr("dialog.close"), self.close, width=96
        )
        self._close_btn.grid(row=8, column=0, sticky="e", pady=(10, 0))

        self._apply_archived_rules()
        self._render_state()
        self._render_schedule_state()
        self.refresh()
        # 建窗阶段窗口还没映射: 要完整跑一轮事件循环, 位置列表的新高度才会传播到
        # 窗口的请求尺寸上(只跑 idle 时量到的还是画布的默认高度)。
        self._fit_window_height(settle=True)

    def _make_button(
        self,
        parent: ctk.CTkBaseClass,
        text: str,
        command: Callable[[], None],
        *,
        width: int,
        danger: bool = False,
        primary: bool = False,
    ) -> ctk.CTkButton:
        """创建一个符合当前调色板的按钮.

        ``danger``/``primary`` 只是调用处的说法, 配色统一取自
        :func:`archive_management.ui.widgets.button_colors` —— 危险色只有那一份
        定义(48 号评审: 同一窗口里三个删除按钮的配色必须一模一样)。
        """
        style: ButtonStyle = "danger" if danger else "accent" if primary else "ghost"
        button = ctk.CTkButton(
            parent,
            text=text,
            width=width,
            height=30,
            corner_radius=RADIUS_MD,
            font=ctk.CTkFont(size=12),
            command=command,
        )
        paint_button_style(button, self._palette, style)
        # 登记样式: 可用性变了要按当前 state 重画(归档后那一批置灰的按钮).
        self._buttons[button] = style
        return button

    def _paint_buttons(self) -> None:
        """按每个按钮的 state 重绘: 置灰的必须真的看起来置灰.

        只 ``configure(state="disabled")`` 的话, 主色/危险色的底与描边会留在那里 ——
        归档后整个窗口的按钮看起来都还可点(49 号评审)。
        """
        for button, style in self._buttons.items():
            paint_button_state(button, self._palette, style)

    # -- 状态与列表 ---------------------------------------------------------

    def _render_state(self) -> None:
        """右上角的状态文字.

        它是一枚"状态"而不是标题: 用颜色与标题拉开层级(已启用=强调色, 已停用/
        已归档=次要色), 与定时任务窗口里同类状态的取色口径一致。
        """
        palette = self._palette
        if self._archived:
            self._state_label.configure(
                text=tr("manage.archived_label"), text_color=palette.text_muted
            )
            return
        enabled = self._enabled
        self._state_label.configure(
            text=(
                tr("manage.enabled_label") if enabled else tr("manage.disabled_label")
            ),
            text_color=palette.accent if enabled else palette.text_muted,
        )

    def _apply_archived_rules(self) -> None:
        """归档游戏在管理窗口里只保留"删除游戏".

        归档后允许的动作只有删除、导出、取消归档与打开详情: 导出在详情页、取消
        归档在主页, 所以这里除了删除以外的按钮一律置灰。
        """
        if not self._archived:
            return
        for button in (*self._location_buttons, self._delete_origin_btn):
            button.configure(state="disabled")
        for button in (self._rename_btn, self._schedule_btn, self._toggle_btn):
            button.configure(state="disabled")
        self._paint_buttons()

    def _blocked(self, action: GameAction) -> bool:
        """归档游戏被禁用的动作: 按钮已置灰, 这里兜住直接调用."""
        if action_allowed(action, archived=self._archived):
            return False
        self._state_label.configure(
            text=tr("manage.archived_label"),
            text_color=self._palette.text_muted,
        )
        return True

    def _render_schedule_state(self) -> None:
        """展示该游戏当前的定时备份配置(每个游戏独立配置)."""
        task = self._backend.task_status(self._game_id)
        self._schedule_state.configure(
            text=tr(
                "manage.schedule_state",
                state=task.schedule_text or tr("task.unscheduled"),
                keep=task.keep_auto,
            )
        )

    def refresh(self) -> None:
        """从后端重载存档位置列表并重绘."""
        self._items = self._backend.list_locations(self._game_id)
        if self._selected is not None and not any(
            item.location_id == self._selected for item in self._items
        ):
            self._selected = None
        self._rebuild_rows()
        self._fit_list_height()
        self._fit_window_height()

    def _fit_list_height(self) -> None:
        """位置列表的高度跟着内容走(一条不空、多了滚动).

        列表高度固定成两行时, 只有一条位置的窗口里就空出一大块(15 号评审); 这里按
        内容请求夹到 [_LIST_MIN_HEIGHT, _LIST_MAX_HEIGHT]。
        """
        self._window.update_idletasks()
        canvas = getattr(self._list_scroll, "_parent_canvas", None)
        box = None if canvas is None else canvas.bbox("all")
        content = 0 if box is None else int(box[3]) - int(box[1])
        self._list_scroll.configure(
            height=min(max(_LIST_MIN_HEIGHT, content), _LIST_MAX_HEIGHT)
        )

    def _fit_window_height(self, *, settle: bool = False) -> None:
        """窗口高度按内容算: 位置列表定高之后, 多余的空白不再留在窗口里.

        出处(2026-10-02 用户反馈): 底部按钮与窗口下沿之间空出一大块。两个原因叠加:
        ① 这里又对内容高度取了一次 ``_WINDOW_MIN_HEIGHT`` 的下限; ② ``winfo_reqheight()``
        是**物理**像素, 而 ``CTk.geometry()`` 吃**逻辑**像素(会乘窗口缩放, 125% 的屏上就是
        1.25 倍) —— 直接拿物理值去定高, 窗口永远比内容高一截。所以现在**只按内容**, 而且
        除回窗口缩放(见 ``widgets.window_scaling``)。
        """
        if settle:
            self._window.update()
        else:
            self._window.update_idletasks()
        scale = window_scaling(self._window)
        height = round(int(self._window.winfo_reqheight()) / scale)
        self._window.geometry(f"{_WINDOW_WIDTH}x{height}")
        _present(self._parent, self._window)

    def _rebuild_rows(self) -> None:
        for child in self._list_scroll.winfo_children():
            child.destroy()
        self._rows = {}
        # 行重建后悬停状态失效: 不清掉会指向已销毁的行.
        self._hover = None
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
        row = ctk.CTkFrame(
            self._list_scroll,
            corner_radius=RADIUS_MD,
            fg_color=palette.raised,
        )
        row.grid(row=index, column=0, sticky="ew", padx=8, pady=4)
        row.grid_columnconfigure(1, weight=1)

        chip = ctk.CTkLabel(
            row,
            text=_chip_text(item.path_kind, primary=item.is_primary),
            width=56,
            corner_radius=RADIUS_LG,
            font=ctk.CTkFont(size=11),
            fg_color=palette.accent_soft,
            text_color=palette.accent_soft_text,
        )
        chip.grid(row=0, column=0, rowspan=2, padx=(10, 8), pady=8)

        title_text = item.path
        title = ctk.CTkLabel(
            row,
            text=title_text,
            anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=palette.text_body,
        )
        title.grid(row=0, column=1, sticky="w", padx=(0, 8), pady=(8, 0))
        # 路径可以很长: 按行内剩下的宽度**中间省略**(尾部是目录名, 比盘符值得留),
        # 否则会被行硬切 —— 它自己是 pack/grid 到左边、宽度随文字变的, 不能拿来当依据。
        track_fit(row, title, (title_text,), path=True, inset=_ROW_TEXT_INSET)

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
            # 悬停反馈: 与主页的卡片、详情页的备份卡片同一条规则(只重绘受影响的两行).
            widget.bind(
                "<Enter>",
                lambda _event, lid=item.location_id: self._set_hover(lid),
            )
            widget.bind("<Leave>", lambda _event: self._set_hover(None))
        self._rows[item.location_id] = [row, title, note]

    def _paint_rows(self) -> None:
        for item in self._items:
            self._paint_row(item.location_id)

    def _row_colors(self, location_id: str) -> tuple[str, str]:
        """一行位置该用的 (底色, 描边色): 选中 > 悬停 > 常规."""
        return card_surface_colors(
            self._palette,
            selected=location_id == self._selected,
            hovered=location_id == self._hover,
        )

    def _paint_row(self, location_id: str) -> None:
        """只重绘一行位置(主题/选中/悬停都走它)."""
        widgets = self._rows.get(location_id)
        item = next(
            (entry for entry in self._items if entry.location_id == location_id), None
        )
        if widgets is None or item is None:
            return
        row, _title, note = widgets
        background, border = self._row_colors(location_id)
        row.configure(fg_color=background, border_width=1, border_color=border)
        note.configure(
            text_color=self._palette.success if item.ok else self._palette.danger
        )

    def _set_hover(self, location_id: str | None) -> None:
        """记录鼠标悬停的那一行, 只重绘受影响的两行."""
        if self._hover == location_id:
            return
        previous, self._hover = self._hover, location_id
        for key in (previous, location_id):
            if key is not None:
                self._paint_row(key)

    def _select(self, location_id: str) -> None:
        self._selected = location_id
        self._paint_rows()

    # -- 游戏操作 -----------------------------------------------------------

    def _on_schedule(self) -> None:
        """配置该游戏的定时备份(每个游戏独立配置)."""
        if self._blocked("schedule"):
            return
        if edit_schedule(
            self._window,
            self._palette,
            self._backend,
            game_id=self._game_id,
            game_name=self._name,
            game_enabled=self._enabled,
        ):
            self._render_schedule_state()
            self._on_change()

    def _on_rename(self) -> None:
        if self._blocked("rename"):
            return
        name = ask_text(
            self._window,
            self._palette,
            title=tr("manage.rename_title"),
            text=tr("manage.rename_prompt"),
            initial=self._name,
            # 改名字这个弹窗与"新增游戏"长得一样, 不写清在改谁就只能靠猜(20 号评审)。
            context=tr("dialog.rename_context", name=self._name),
        )
        if not name:
            return
        try:
            summary = self._backend.update_game(self._game_id, name)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._name = summary.name
        # 重命名后同样要重新裁剪: 新名字可能比原来的长.
        fit_label(
            self._title_label,
            self._name,
            self._title_font,
            _TITLE_TEXT_WIDTH,
            max_lines=_TITLE_TEXT_LINES,
        )
        self._on_change()

    def _on_toggle_enabled(self) -> None:
        if self._blocked("enable"):
            return
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
        """删除游戏: 先把配置与全部备份导出到默认位置, 再删记录.

        导出路径在弹确认框**之前**就算好并写进提示里 —— 用户要清楚告别包会落在哪里,
        以及"先导出、后删除"的顺序。确认后按同一条路径真的导出; 导出失败时后端什么都
        不删, 这里只负责把原因显示出来, 窗口保持打开。
        """
        try:
            destination = self._backend.delete_export_path(self._game_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        confirmed = confirm_dialog(
            self._window,
            self._palette,
            title=tr("manage.delete_title"),
            message=tr("manage.delete_message", name=self._name),
            # 导出路径单独一行(带底色): 夹在句子里时它会把末句的句号挤到孤行,
            # 折行后也不知道到哪里结束(24 号评审)。
            detail=destination,
            confirm_text=tr("manage.delete_confirm"),
            danger=True,
        )
        if not confirmed:
            return
        try:
            self._backend.delete_game(self._game_id, destination)
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
        if self._blocked("locations"):
            return
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
        if self._blocked("locations"):
            return
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
        if self._blocked("locations"):
            return
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
        if self._blocked("locations"):
            return
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
        if self._blocked("locations"):
            return
        item = self._selected_item()
        if item is None:
            return
        confirmed = confirm_dialog(
            self._window,
            self._palette,
            title=tr("loc.remove_title"),
            message=tr("loc.remove_message", path=item.path),
            confirm_text=tr("loc.remove_confirm"),
            danger=True,
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

    def _on_delete_origin(self) -> None:
        """把原始存档目录移入回收站(需输入游戏名称确认).

        这里只做"预检 -> 确认 -> 调用后端"三步; 路径边界与回收站失败等
        判断都在用例层完成, 界面只负责展示与收集确认文本。
        """
        if self._blocked("locations"):
            return
        item = self._selected_item()
        if item is None:
            return
        try:
            plan = self._backend.preview_location_removal(item.location_id)
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        if plan.blocked:
            self._show_error(
                ArchiveManagementError(
                    tr(
                        f"loc.delete_blocked_{plan.blocked_reason}",
                        path=plan.path,
                    )
                )
            )
            return
        typed = ask_text(
            self._window,
            self._palette,
            title=tr("loc.delete_origin_title"),
            text=tr(
                "loc.delete_origin_prompt",
                path=plan.path,
                files=plan.files,
                size=size_label(plan.total_size),
                name=plan.game_name,
            ),
            confirm_text=tr("loc.delete_origin_confirm"),
        )
        if typed is None:
            return
        try:
            message = self._backend.delete_save_location(
                item.location_id, confirm_name=typed
            )
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self.refresh()
        self._on_change()
        info_dialog(
            self._window,
            self._palette,
            title=tr("loc.delete_origin_done"),
            message=message,
        )

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
