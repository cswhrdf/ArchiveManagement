"""管理游戏与存档位置窗口.

一个模态浮层: 上半部管理游戏信息(重命名/停用/删除), 下半部管理该
游戏的"原始存档位置"(添加目录/文件、设为主位置、重新验证、编辑路径、
删除记录), 并提供"删除原始存档位置": 把磁盘上的原目录移入
系统回收站(需输入游戏名称确认, 不会永久删除)。界面明确区分原始位置与
由应用管理的备份目录, 避免误操作。
本模块采用组合式窗口, 便于无头测试。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from functools import partial
from pathlib import Path

import customtkinter as ctk
from PIL import Image

from archive_management.domain import ArtworkKind, GameAction, PathKind, action_allowed
from archive_management.exceptions import ArchiveManagementError, ArtworkImageError
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

logger = logging.getLogger(__name__)


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
# 屏幕安全边距与页脚间距: 与设置窗口同一套口径(窗口绝不能比屏幕还高, 见 _fit_window_height).
_SCREEN_MARGIN = 120
_FOOTER_GAP = 6
# 正文滚动区的高度下限(逻辑像素): 再矮也得留出能滚动的一块。
_BODY_MIN_HEIGHT = 200
# 位置列表的高度跟着内容走: 一条位置时只占一行的高度(不在卡片里空出一大块,
# 15 号评审), 超过上限则由列表自己滚动。
_LIST_MIN_HEIGHT = 88
_LIST_MAX_HEIGHT = 200

# 外观那一节里两个预览的固定尺寸(逻辑像素): 封面给竖版比例, 图标给方形。
_ARTWORK_KINDS: tuple[ArtworkKind, ...] = ("cover", "icon")
_ARTWORK_PREVIEW_SIZE: dict[ArtworkKind, tuple[int, int]] = {
    "cover": (48, 72),
    "icon": (24, 24),
}
_ARTWORK_LABEL_WIDTH = 44
_ARTWORK_BUTTON_WIDTH = 96
_ARTWORK_RESET_WIDTH = 88


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
        # 外观一节: 两个预览 + 状态文字 + 各自的“选择/恢复默认”按钮(按 kind 索引).
        self._artwork_preview: dict[ArtworkKind, ctk.CTkLabel] = {}
        self._artwork_state: dict[ArtworkKind, ctk.CTkLabel] = {}
        self._artwork_buttons: dict[
            ArtworkKind, tuple[ctk.CTkButton, ctk.CTkButton]
        ] = {}
        # 预览图缓存: 每次改图/恢复默认就清掉(键是路径, 换了图自然换键).
        self._artwork_images: dict[str, ctk.CTkImage | None] = {}
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

        container = ctk.CTkScrollableFrame(window, fg_color=palette.background)
        self._body = container
        container.pack(fill="both", expand=True, padx=16, pady=(16, 0))
        container.grid_columnconfigure(0, weight=1)
        # 内容装得下就不立滚动条(与主页列表/设置窗口同一条规则).
        auto_scrollbar(container)

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

        # 外观一节放在页头之后: 它管的是“这款游戏长什么样”, 在定时备份与位置之前。
        self._build_artwork_section(container, row=1)

        # 「定时备份」是一节小标题, 值单独一行用次要色: 它与"原始存档位置"不能
        # 长得一模一样, 否则整窗自上而下没有层级(15 号评审)。
        self._schedule_heading = ctk.CTkLabel(
            container,
            text=tr("manage.schedule_heading"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        self._schedule_heading.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self._schedule_state = ctk.CTkLabel(
            container,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        self._schedule_state.grid(row=3, column=0, sticky="w", pady=(2, 6))

        locations_title = ctk.CTkLabel(
            container,
            text=tr("manage.locations_title"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        locations_title.grid(row=4, column=0, sticky="w", pady=(0, 6))

        self._list_scroll = ctk.CTkScrollableFrame(
            container,
            fg_color=palette.panel,
            corner_radius=10,
            height=_LIST_MIN_HEIGHT,
        )
        self._list_scroll.grid(row=5, column=0, sticky="ew", pady=(0, 8))
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
        note.grid(row=6, column=0, sticky="w", pady=(0, 8))
        attach_tooltip(note, tr("manage.backup_note_tip", path=self._backup_location))

        action_bar = ctk.CTkFrame(container, fg_color="transparent")
        action_bar.grid(row=7, column=0, sticky="w")
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
        danger_bar.grid(row=8, column=0, sticky="w", pady=(8, 0))
        self._delete_origin_btn = self._make_button(
            danger_bar,
            tr("loc.delete_origin"),
            self._on_delete_origin,
            width=168,
            danger=True,
        )
        self._delete_origin_btn.pack(side="left")

        # 「关闭」是中性动作: 不穿危险色(15 号评审)。它放在**固定页脚**里: 正文装不下要
        # 滚动时, 它既不该跟着跑, 也不该被屏幕下沿切掉(与设置窗口同一套)。
        footer = ctk.CTkFrame(window, fg_color=palette.background)
        self._footer = footer
        footer.pack(fill="x", padx=16, pady=(_FOOTER_GAP, 16))
        self._close_btn = self._make_button(
            footer, tr("dialog.close"), self.close, width=96
        )
        self._close_btn.pack(side="right")

        self._apply_archived_rules()
        self._render_state()
        self._render_schedule_state()
        self._render_artwork()
        self.refresh()
        # 建窗阶段窗口还没映射: 要完整跑一轮事件循环, 位置列表的新高度才会传播到
        # 窗口的请求尺寸上(只跑 idle 时量到的还是画布的默认高度)。
        self._fit_window_height(settle=True)

    def _build_artwork_section(self, container: ctk.CTkFrame, *, row: int) -> None:
        """建出"外观"那一节: 封面与图标各一行(预览 + 状态 + 选择/恢复默认).

        为什么放在这里而不是别处: 平台取图由适配器与 CDN 决定, **手动添加的游戏压根
        没有平台图** —— 用户想给自己加的游戏贴一张图, 只能从界面上给一个入口(用户
        2026-10-03 的要求)。用户挑的图存在数据目录(在缓存之外), 所以清理缓存不会把它
        带走(见 ``services.artwork.UserArtworkStore``)。
        """
        palette = self._palette
        section = ctk.CTkFrame(container, fg_color=palette.panel, corner_radius=10)
        section.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        section.grid_columnconfigure(2, weight=1)
        heading = ctk.CTkLabel(
            section,
            text=tr("manage.appearance_title"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        heading.grid(row=0, column=0, columnspan=4, sticky="w", padx=12, pady=(12, 6))
        for index, kind in enumerate(_ARTWORK_KINDS, start=1):
            self._build_artwork_row(section, kind, row=index)

    def _build_artwork_row(
        self, section: ctk.CTkFrame, kind: ArtworkKind, *, row: int
    ) -> None:
        """一行的外观控件: 名称 + 预览 + 状态文字 + 选择 + 恢复默认."""
        palette = self._palette
        name = ctk.CTkLabel(
            section,
            text=tr(
                "manage.artwork_cover" if kind == "cover" else "manage.artwork_icon"
            ),
            anchor="w",
            width=_ARTWORK_LABEL_WIDTH,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
        )
        name.grid(row=row, column=0, sticky="w", padx=(12, 8), pady=(0, 10))
        preview = ctk.CTkLabel(section, text="", width=_ARTWORK_PREVIEW_SIZE[kind][0])
        preview.grid(row=row, column=1, sticky="w", pady=(0, 10))
        self._artwork_preview[kind] = preview
        state = ctk.CTkLabel(
            section,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_muted,
        )
        state.grid(row=row, column=2, sticky="w", padx=(10, 8), pady=(0, 10))
        self._artwork_state[kind] = state
        choose = self._make_button(
            section,
            tr("manage.choose_cover" if kind == "cover" else "manage.choose_icon"),
            partial(self._choose_artwork, kind),
            width=_ARTWORK_BUTTON_WIDTH,
        )
        choose.grid(row=row, column=3, sticky="e", padx=(0, 6), pady=(0, 10))
        reset = self._make_button(
            section,
            tr("manage.artwork_reset"),
            partial(self._reset_artwork, kind),
            width=_ARTWORK_RESET_WIDTH,
        )
        reset.grid(row=row, column=4, sticky="e", padx=(0, 12), pady=(0, 10))
        self._artwork_buttons[kind] = (choose, reset)

    def _render_artwork(self) -> None:
        """按后端当前状态重画两行: 预览、状态文字与"恢复默认"的可用性.

        状态文字区分"自定义"与"内置": 用户改过图之后得能看出来当前用的是哪一份, 否则
        想换回平台图时只能靠猜。
        """
        for kind in _ARTWORK_KINDS:
            custom = self._backend_text(self._backend.user_artwork_path, kind)
            try:
                effective = self._backend.artwork_path(self._game_id, kind)
            except ArchiveManagementError:  # 缺图不影响管理功能
                effective = ""
            preview = self._artwork_preview[kind]
            picture = self._load_artwork(effective, kind)
            if picture is None:
                # 没有图: 显示名称首字占位(与主页海报卡片同一套做法: 别留一块空白).
                preview.configure(
                    text=self._name[:1]
                    if kind == "icon"
                    else tr("manage.artwork_none"),
                    image=None,
                    font=ctk.CTkFont(size=12),
                    text_color=self._palette.text_muted,
                )
            else:
                preview.configure(text="", image=picture)
            self._artwork_state[kind].configure(
                text=tr(
                    "manage.artwork_custom" if custom else "manage.artwork_builtin"
                ),
                text_color=(
                    self._palette.accent if custom else self._palette.text_muted
                ),
            )
            _choose, reset = self._artwork_buttons[kind]
            reset.configure(state="normal" if custom else "disabled")
        self._paint_buttons()

    def _backend_text(
        self, reader: Callable[[str, ArtworkKind], str], kind: ArtworkKind
    ) -> str:
        """读一次后端状态; 失败(游戏已删/参数不对)按"没有"处理."""
        try:
            return reader(self._game_id, kind)
        except ArchiveManagementError as exc:  # pragma: no cover - 渲染期不该抛
            logger.debug("读取图片状态失败: %s", exc)
            return ""

    def _load_artwork(self, path: str, kind: ArtworkKind) -> ctk.CTkImage | None:
        """把本地图片读成预览图(读不到/解码失败时返回 None 走文字占位).

        与主页海报卡片同一条约定: 渲染是同步的, 这里**不联网**; 图坏了只记一行日志,
        绝不让窗口建不出来。

        缓存**只增不减**(键里带 mtime 与大小, 换了图自然是新键): Tk 的图片被 Python
        回收之后控件里还留着那个名字, 下一次 ``configure(image=None)`` 会以
        ``image "pyimage1" doesn't exist`` 直接抛下来 —— 恢复默认走的正是那一条(实测)。
        一个窗口里换不了几次图, 多留几个对象无所谓。
        """
        if not path:
            return None
        try:
            info = Path(path).stat()
        except OSError:  # 图被移走了: 当"没有图"
            return None
        key = f"{path}:{info.st_mtime_ns}:{info.st_size}:{kind}"
        if key in self._artwork_images:
            return self._artwork_images[key]
        picture: ctk.CTkImage | None = None
        try:
            with Image.open(path) as image:
                loaded = image.copy()
        except (OSError, ValueError) as exc:
            logger.warning("预览图无法解码(%s): %s", path, exc)
        else:
            picture = ctk.CTkImage(light_image=loaded, size=_ARTWORK_PREVIEW_SIZE[kind])
        self._artwork_images[key] = picture
        return picture

    def _choose_artwork(self, kind: ArtworkKind) -> None:
        """选一张本地图片并设成这款游戏的封面/图标.

        归档后不允许改: 与重命名/启停同一档(归档只留删除/导出/取消归档/打开详情),
        所以这里借 ``rename`` 那条归档规则。
        """
        if self._blocked("rename"):
            return
        title = tr(
            "manage.choose_cover_title"
            if kind == "cover"
            else "manage.choose_icon_title"
        )
        chosen = pick_file(title=title)
        if not chosen:  # 用户取消
            return
        try:
            self._backend.set_game_artwork(self._game_id, kind, chosen)
        except ArtworkImageError as exc:
            # 图片不可用: 按原因代码取文案(服务层只给代码与原始细节, 见 ArtworkImageError)。
            info_dialog(
                self._window,
                self._palette,
                title=tr("manage.artwork_problem"),
                message=tr(f"artwork.error.{exc.code}"),
            )
            return
        except ArchiveManagementError as exc:
            self._show_error(exc)
            return
        self._render_artwork()
        self._fit_window_height()
        self._on_change()

    def _reset_artwork(self, kind: ArtworkKind) -> None:
        """恢复默认: 删掉用户指定的那份图(没有就什么都不做)."""
        if not self._backend.clear_game_artwork(self._game_id, kind):
            return
        self._render_artwork()
        self._fit_window_height()
        self._on_change()

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
        # 「外观」那一节也归在“改游戏信息”里: 归档后与重命名/启停同一档置灰。
        for choose, _reset in self._artwork_buttons.values():
            choose.configure(state="disabled")
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
        """窗口高度按内容算, 但**绝不出屏**: 装不下就把正文交给滚动条.

        出处(2026-10-02 用户反馈): 底部按钮与窗口下沿之间空出一大块。原因是这里又对内容
        高度取了一次 ``_WINDOW_MIN_HEIGHT`` 的下限, 而且 ``winfo_reqheight()`` 是**物理**
        像素而 ``CTk.geometry()`` 吃**逻辑**像素(125% 的屏上就是 1.25 倍)—— 现在只按内容,
        且除回窗口缩放(见 ``widgets.window_scaling``)。

        2026-10-03 加"外观"一节之后多一条: 内容高过屏幕时窗口不能比屏幕还高(矮屏用例
        ``test_workspace_windows_fit_the_screen`` 实测报过 676 > 576)。所以先把正文区的高度
        定成"内容想要的高度", 再夹到"屏高 - 安全边距 - 页脚": 夹住的那部分由正文自己滚,
        页脚与"关闭"始终看得见。
        """
        if settle:
            self._window.update()
        else:
            self._window.update_idletasks()
        scale = window_scaling(self._window)
        chrome = (
            int(self._footer.winfo_reqheight())
            + round(_FOOTER_GAP * scale)
            + round(_WINDOW_PAD_Y * 2 * scale)
        )
        available = round(
            (int(self._window.winfo_screenheight()) - _SCREEN_MARGIN) / scale
        )
        room = max(_BODY_MIN_HEIGHT, available - round(chrome / scale))
        wanted = max(_BODY_MIN_HEIGHT, round(self._body_content_height() / scale))
        self._body.configure(height=min(wanted, room))
        self._window.update_idletasks()
        # 高度取窗口**自己的**请求高度, 不再自己拼算式: 实测把算式(正文 + 页脚 + 外边距)直接
        # 当成几何值会让页脚掉出窗口 —— 本机上关闭按钮跑到窗口下沿以下 148 像素
        # (case: test_the_manage_window_hugs_its_content 的 below < 0)。Tk 算出来的请求里
        # 还含一些我们没建模的间距, 自己拼就少给了一块。
        height = max(
            _WINDOW_MIN_HEIGHT,
            min(round(int(self._window.winfo_reqheight()) / scale), available),
        )
        self._window.geometry(f"{_WINDOW_WIDTH}x{height}")
        _present(self._parent, self._window)

    def _body_content_height(self) -> int:
        """正文内容想要多高(**物理**像素): 数滚动区画布里排出来的那一块.

        不能读正文自己的 ``winfo_reqheight()``: 滚动区的高度是**我们配置**的值(默认 200),
        与内容无关 —— 拿它定高等于把窗口钉死在 200 像素上。
        """
        canvas = getattr(self._body, "_parent_canvas", None)
        box = None if canvas is None else canvas.bbox("all")
        return 0 if box is None else int(box[3]) - int(box[1])

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
