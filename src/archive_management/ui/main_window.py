"""CustomTkinter 主窗口.

布局对应 ``docs/archive-management-ui*.svg``:顶部工具栏、左侧游戏/工作区
栏、标题行与概要卡、视图工具条、时间线/分支主面板与右侧 rail、底部反馈
状态条。主题切换不改变布局与操作语义;界面文案统一从 i18n 配置加载。
"""

from __future__ import annotations

import contextlib
import logging
import queue
import sqlite3
import threading
import tkinter as tk
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import customtkinter as ctk
from PIL import Image

from archive_management.application.backup import MAX_NOTE_LENGTH
from archive_management.application.restore import RestorePlan
from archive_management.config import (
    AppConfig,
    ConfigLoad,
    HotkeySettings,
    load_or_reset_config,
    save_config,
)
from archive_management.domain import GameAction, action_allowed
from archive_management.exceptions import (
    ArchiveManagementError,
    ContentUnchangedError,
)
from archive_management.i18n import DEFAULT_LOCALE, set_locale, tr
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.logging_config import apply_debug
from archive_management.services.audit import log_action
from archive_management.services.hotkeys import (
    ACTION_CREATE_BRANCH,
    ACTION_SAVE_NOW,
    DEFAULT_ACCELERATORS,
    GlobalHotkeyService,
    HotkeyBinding,
    combo_error,
    format_accelerator,
    parse_accelerator,
)
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import (
    ask_branch_name,
    ask_text,
    confirm_dialog,
    edit_backup_dialog,
    info_dialog,
    restore_dialog,
)
from archive_management.ui.home_page import HomePage
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import (
    AppPage,
    BackupItem,
    FeedbackKind,
    GameDetail,
    GameSummary,
    SourceFilter,
    TaskStatus,
    ViewKind,
    branch_order,
    filter_by_source_label,
    size_label,
    timeline_order,
)
from archive_management.ui.palette import DEFAULT_THEME, Palette
from archive_management.ui.schedule_window import ScheduleWindow
from archive_management.ui.settings_window import SettingsWindow
from archive_management.ui.textfit import fit_text
from archive_management.ui.widgets import UiKit

logger = logging.getLogger(__name__)

_TONE_COLORS: dict[str, str] = {
    "orange": "#d15b3e",
    "blue": "#405685",
    "green": "#3b806e",
    "default": "#405685",
}
# 支持的最小窗口尺寸: 主页里那些固定宽度的行必须能放进"最小窗口下的内容区",
# 否则在更窄的屏幕(窗口被窗口管理器再压小)上会越界并盖住描边。
WINDOW_MIN_SIZE = (1200, 720)
# 头部标题与概要卡里的游戏名**按控件实际宽度**裁剪: 窗口变宽就能多显示几个字。
# 两个常量只是"控件尺寸还没测量出来时"的落位预算(取自最小窗口下的实测可用宽度:
# 头部标题区 922px、概要卡信息区约 720px), 之后由 _refit_detail_names() 按真实
# 宽度重裁。名称最多显示两行, 超出补省略号, 否则会把下面的内容整排推下去。
_HEADER_TEXT_WIDTH = 900
_HERO_TEXT_WIDTH = 640
# 名称最多显示的行数(超出补省略号): 完整名称仍可在游戏设置/重命名对话框里看到。
_HEADER_NAME_LINES = 2
_HERO_NAME_LINES = 2
# 拖窗口时把名称重裁合并成一次(每个像素都跑一遍会卡).
_REFIT_DELAY_MS = 60
_RAIL_WIDTH = 350
# 分支视图中用于标示层级的连接符(与缩进配合).
_BRANCH_MARK = "└ "
# 当前节点标记: 后续备份/分支都从这个节点继续.
_CURRENT_MARK = "●"


def _schedule_state(task: TaskStatus) -> str:
    """返回定时备份的启用状态文案(未配置/已启用/已暂停)."""
    if not task.schedule_text:
        return tr("schedule.state_off")
    return (
        tr("schedule.state_on")
        if task.schedule_enabled
        else tr("schedule.state_paused")
    )


class _WorkspaceWindow(Protocol):
    """工作区窗口的最小接口: 主窗口据此保证同一时间只开一个窗口.

    游戏发现、定时任务与设置都是配置面板, 同时开多个会互相遮挡, 也容易在已经
    过期的数据上操作; 因此它们共用同一个"窗口位置"。
    """

    def focus(self) -> bool:
        """把窗口提到前台; 窗口已关闭时返回 False."""
        ...

    def close(self) -> None:
        """关闭窗口."""
        ...

    _window: ctk.CTkToplevel


class ArchiveApp(ctk.CTk):
    """存档管理主窗口."""

    def __init__(
        self,
        backend: ArchiveService,
        *,
        title: str,
        smoke_seconds: float | None = None,
        hotkeys: GlobalHotkeyService | None = None,
        paths: ApplicationPaths | None = None,
    ) -> None:
        """构造主窗口并加载演示数据.

        ``paths`` 用于读写应用配置(快捷键); 省略时只在内存里维护, 便于测试。
        """
        super().__init__()
        self.backend = backend
        self.kit = UiKit()

        self._theme = backend.current_theme()
        ctk.set_appearance_mode(self._theme)
        self.p = Palette.for_theme(self._theme)

        self._game_id: str | None = None
        self._game: GameSummary | None = None
        # 主窗口内的页面: 游戏主页(默认)与游戏详情页同格叠放, 同一时间只显示一个.
        self._page: AppPage = AppPage.HOME
        # 当前打开的工作区窗口(游戏发现/定时任务/设置): 三个入口共用一个位置.
        self._active_window: _WorkspaceWindow | None = None
        self._active_nav: str | None = None
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
        self._card_unregisters: list[Callable[[], None]] = []
        # 详情页当前显示的**完整**名称与副标题: 窗口宽度变化时要按新的可用宽度重裁。
        self._detail_name = ""
        self._detail_subtitle = ""
        self._refit_job: str | None = None
        # 消息轮询任务的 id(destroy() 里要撤掉, 见那里的说明)。
        self._poll_job: str | None = None

        self._messages: queue.Queue[
            tuple[Literal["ok", "err", "unchanged", "hotkey"], str]
        ] = queue.Queue()
        self._pending_ok: Callable[[str], None] | None = None
        self._last_feedback: tuple[FeedbackKind, str] = (
            FeedbackKind.INFO,
            tr("status.ready"),
        )
        self._verified = True
        self._usage_text = tr("status.usage", used="—")
        self._hotkeys = hotkeys if hotkeys is not None else GlobalHotkeyService()
        self._paths = paths
        # 只读一次配置: 语言、快捷键与"配置被还原过"的提示都从同一份结果出发。
        loaded = self._load_config()
        # 语言要在构造界面之前生效: 所有文案都是构建时取的.
        self._language = self._apply_language(loaded.config.language)
        self._shortcuts = self._shortcuts_from(loaded)
        # 调试日志开关来自同一份配置(默认关闭): 设置窗口展示的与生效的要是同一个值.
        self._debug = loaded.config.logging.debug
        self.title(title)
        self.minsize(*WINDOW_MIN_SIZE)
        self.geometry("1360x860")
        self.configure(fg_color=self.p.background)

        self._build_layout()
        self._register_hotkeys()
        self._load_first_game()
        # 软件打开后默认停在游戏主页(游戏很多时它比单个游戏的详情更有用).
        self._show_page(AppPage.HOME)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._poll_job = self.after(100, self._poll_messages)
        if smoke_seconds is None:
            # 启动时补一次译名探测: 后台线程、只对当前语言缺条目的游戏联网(有缓存就
            # 直接复用), 探到新名字会抬高数据版本, 界面自动重绘。--smoke 不触发,
            # 冻结包的冒烟自检保持不发网络请求。
            self.backend.prefetch_names()
        if smoke_seconds is not None:
            # 冒烟自检也走正常关闭路径, 确保调度器/快捷键被释放.
            self.after(int(smoke_seconds * 1000), self._on_close)

    # ---------------------------------------------------------------- 快捷键

    def _register_hotkeys(self) -> None:
        """注册全局快捷键(保存/创建分支); 注册失败只提示, 不影响应用可用性."""
        for action, callback in (
            (ACTION_SAVE_NOW, self._request_hotkey_backup),
            (ACTION_CREATE_BRANCH, self._request_hotkey_branch),
        ):
            state = self._hotkeys.register(
                HotkeyBinding(name=action, accelerator=self._shortcuts[action]),
                callback,
            )
            if not state.registered and state.error:
                self._last_feedback = (
                    FeedbackKind.INFO,
                    tr("hotkey.failed", reason=state.error),
                )

    def _load_config(self) -> ConfigLoad:
        """读一次配置(没有配置路径时用默认值).

        内容非法时 ``load_or_reset_config`` 已经把文件还原为默认值, 这里再把这件事反馈
        到界面与审计日志, 避免用户以为自己改的语言/快捷键"没生效"。
        """
        if self._paths is None:
            return ConfigLoad(config=AppConfig())
        loaded = load_or_reset_config(self._paths.config_path)
        if loaded.reset:
            log_action("config.reset", basic=True, backup=str(loaded.backup or ""))
            self._last_feedback = (FeedbackKind.INFO, tr("config.reset"))
        return loaded

    @staticmethod
    def _apply_language(language: str) -> str:
        """让 :mod:`archive_management.i18n` 用上配置里的语言, 返回生效的语言名."""
        try:
            set_locale(language)
        except ValueError:  # pragma: no cover - 配置校验已经拦过不支持的语言
            set_locale(DEFAULT_LOCALE)
            return DEFAULT_LOCALE
        return language

    @staticmethod
    def _shortcuts_from(loaded: ConfigLoad) -> dict[str, str]:
        """按配置拼出快捷键表(配置缺失或内容非法时是默认组合)."""
        shortcuts = dict(DEFAULT_ACCELERATORS)
        shortcuts[ACTION_SAVE_NOW] = loaded.config.hotkeys.save
        shortcuts[ACTION_CREATE_BRANCH] = loaded.config.hotkeys.branch
        return shortcuts

    def _save_language(self, locale: str) -> None:
        """把界面语言写回配置文件(没有配置路径时只保存在内存里)."""
        if self._paths is None:
            return
        config = load_or_reset_config(self._paths.config_path).config
        config.language = locale
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存语言失败: %s", exc)

    def _on_language_change(self, locale: str) -> str | None:
        """切换界面语言: 按新语言重探译名 + 整体重建界面; 返回 None 表示成功.

        译名重新探测在后台线程里跑(``refresh=True`` 忽略缓存), 探测完抬高的数据
        版本会触发重绘, 所以这里不必等网络。
        """
        try:
            set_locale(locale)
        except ValueError as exc:
            return str(exc)
        self._language = locale
        self._save_language(locale)
        log_action("ui.switch_language", basic=True, language=locale)
        self.backend.prefetch_names(refresh=True)
        self._rebuild_ui()
        self._feedback(
            FeedbackKind.INFO,
            tr("settings.language_switched", language=tr(f"locale.{locale}")),
        )
        return None

    def _save_debug(self, enabled: bool) -> None:
        """把调试开关写回配置文件(没有配置路径时只保存在内存里)."""
        if self._paths is None:
            return
        # 内容非法时 load_or_reset_config 已经还原过, 因此这里总能拿到一份可用配置。
        config = load_or_reset_config(self._paths.config_path).config
        config.logging.debug = enabled
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存调试开关失败: %s", exc)

    def _on_debug_change(self, enabled: bool) -> str | None:
        """开关调试日志: 立即生效 + 写回配置; 返回 None 表示成功.

        关掉后日志里不再出现 DEBUG 级记录(平时不必把日志写满), 打开才会把基础操作
        一起记进去 —— 因此应用后立刻记一条 INFO 审计: 这个改动本身总看得见。
        """
        apply_debug(enabled)
        self._debug = enabled
        self._save_debug(enabled)
        log_action("ui.switch_debug_log", enabled=enabled)
        self._feedback(
            FeedbackKind.INFO,
            tr(
                "settings.debug_switched_on"
                if enabled
                else "settings.debug_switched_off"
            ),
        )
        return None

    def _rebuild_ui(self) -> None:
        """按当前语言重建整个窗口(文案在构建时就已定稿, 只能重建).

        重建会丢掉所有控件与它们登记的主题回调, 因此同时换一个干净的 ``UiKit``;
        定时器、消息队列、快捷键与后台服务都不受影响。
        """
        self._active_window = None
        self._active_nav = None
        for child in self.winfo_children():
            child.destroy()
        self.kit = UiKit()
        self._cards = {}
        self._card_painters = {}
        self._card_unregisters = []
        self._revision = -1
        self._build_layout()
        self._load_first_game()
        self._show_page(AppPage.HOME)

    def _save_shortcuts(self) -> None:
        """把当前快捷键写回配置文件(没有配置路径时只保存在内存里)."""
        if self._paths is None:
            return
        # 内容非法时 load_or_reset_config 已经还原过, 因此这里总能拿到一份可用配置。
        config = load_or_reset_config(self._paths.config_path).config
        config.hotkeys = HotkeySettings(
            save=self._shortcuts[ACTION_SAVE_NOW],
            branch=self._shortcuts[ACTION_CREATE_BRANCH],
        )
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存快捷键失败: %s", exc)

    def _request_hotkey_backup(self) -> None:
        """快捷键回调运行在监听线程: 只投递消息, 由主线程执行备份."""
        self._messages.put(("hotkey", ACTION_SAVE_NOW))

    def _request_hotkey_branch(self) -> None:
        """创建分支的快捷键回调: 同样只投递消息, 由主线程执行."""
        self._messages.put(("hotkey", ACTION_CREATE_BRANCH))

    # ------------------------------------------------------------------ 布局

    def _build_layout(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        topbar = self.kit.frame(
            self, bg_key="topbar", border_key="border", corner_radius=0
        )
        topbar.grid(row=0, column=0, sticky="nsew")
        self._build_topbar(topbar)

        # 内容区: 游戏主页(默认)与游戏详情页同格叠放, 由 _show_page 切换.
        # 侧边栏已移除, 页面因此获得整窗宽度(游戏列表与海报网格受益最明显).
        self._pages = self.kit.frame(self, bg_key="background", corner_radius=0)
        self._pages.grid(row=1, column=0, sticky="nsew")
        self._pages.grid_columnconfigure(0, weight=1)
        self._pages.grid_rowconfigure(0, weight=1)

        self._content = self.kit.frame(
            self._pages, bg_key="background", corner_radius=0
        )
        self._content.grid(row=0, column=0, sticky="nsew")
        self._build_content()

        self._home_page = HomePage(
            self._pages,
            backend=self.backend,
            palette=self.p,
            on_change=self._refresh_after_manage,
            on_open_detail=self._open_game_detail,
            on_notice=self._notice,
        )
        self._home_page.frame.grid(row=0, column=0, sticky="nsew")
        self._home_page.frame.grid_remove()

    def _show_page(self, page: AppPage) -> None:
        """切换主窗口内的页面: 游戏主页(默认)与游戏详情页.

        进入主页时重新读取数据(其它窗口与后台任务都可能改过游戏库), 因此不需要
        在其他地方额外刷新主页。"← 游戏主页"只在详情页有意义, 因此跟着页面显隐。
        """
        self._page = page
        if page is AppPage.HOME:
            self._home_page.reload()
            self._home_page.frame.grid()
            self._content.grid_remove()
            # 已经在主页时"← 游戏主页"没有意义, 只有详情页才显示.
            self._back_btn.grid_remove()
        else:
            self._content.grid()
            self._home_page.frame.grid_remove()
            self._back_btn.grid()

    def _open_game_detail(self, game_id: str) -> None:
        """打开某款游戏的详情页(主页"打开详情"与左侧列表点击都走这里)."""
        self._select_game(game_id)
        if self._game is not None:
            self._show_page(AppPage.DETAIL)

    def _build_topbar(self, parent: ctk.CTkFrame) -> None:
        """顶栏: 左侧是品牌与"← 游戏主页", 右侧是全局入口(添加游戏/定时任务/设置).

        侧边栏去掉后, 全局动作统一收在顶栏右上角: 这些入口与当前看的是哪个页面
        无关, 放在固定的位置最容易找到。
        """
        parent.grid_columnconfigure(2, weight=1)
        logo = ctk.CTkFrame(
            parent, width=30, height=30, corner_radius=8, fg_color=self.p.accent
        )
        logo.grid(row=0, column=0, padx=(18, 12), pady=11)
        self.kit.register(lambda p: logo.configure(fg_color=p.accent))
        brand = self.kit.label(
            parent, tr("topbar.brand"), style="primary", size=16, weight="bold"
        )
        brand.grid(row=0, column=1, padx=(0, 14), pady=10)

        # 返回主页只在详情页显示(在主页时它没有意义).
        self._back_btn = self.kit.button(
            parent,
            tr("page.back_home"),
            style="ghost",
            command=self._on_back_home,
            width=118,
            height=34,
        )
        self._back_btn.grid(row=0, column=2, sticky="w")

        actions = ctk.CTkFrame(parent, fg_color="transparent")
        actions.grid(row=0, column=3, sticky="e", padx=(0, 18))
        self._add_game_btn = self.kit.button(
            actions,
            tr("topbar.add_game"),
            style="accent",
            command=self._on_add_game,
            width=104,
            height=34,
        )
        self._add_game_btn.pack(side="left", padx=(0, 8))
        self._schedule_btn = self.kit.button(
            actions,
            tr("topbar.nav_scheduled"),
            style="ghost",
            command=self._on_open_schedules,
            width=104,
            height=34,
        )
        self._schedule_btn.pack(side="left", padx=(0, 8))
        self._settings_btn = self.kit.button(
            actions,
            tr("topbar.nav_settings"),
            style="ghost",
            command=self._on_open_settings,
            width=88,
            height=34,
        )
        self._settings_btn.pack(side="left")

    def _build_content(self) -> None:
        self._content.grid_columnconfigure(0, weight=1)
        self._content.grid_rowconfigure(3, weight=1)
        # 窗口宽度变了, 名称的可用宽度也变了: 重新裁一次(不是所有控件都能自动重排).
        self._content.bind("<Configure>", self._on_content_resize)

        header = self.kit.frame(self._content, bg_key="background", corner_radius=0)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(16, 4))
        header.grid_columnconfigure(0, weight=1)
        self._title_label = self.kit.label(
            header, "", style="primary", size=26, weight="bold"
        )
        # 与 _title_label 同规格的字体对象: 用来把名称量进固定的行数与宽度.
        self._title_font = ctk.CTkFont(size=26, weight="bold")
        self._title_label.configure(wraplength=_HEADER_TEXT_WIDTH, justify="left")
        # sticky="ew" 让标签占满整格: 它的宽度就是名称可用的宽度(据此重裁文本).
        self._title_label.grid(row=0, column=0, sticky="ew")
        self._subtitle_label = self.kit.label(header, "", style="muted", size=13)
        self._subtitle_font = ctk.CTkFont(size=13)
        self._subtitle_label.configure(wraplength=_HEADER_TEXT_WIDTH, justify="left")
        self._subtitle_label.grid(row=1, column=0, sticky="ew", pady=(2, 0))
        self._export_btn = self.kit.button(
            header,
            tr("action.export"),
            style="accent",
            command=self._on_export,
            width=104,
            height=38,
        )
        self._export_btn.grid(row=0, column=1, rowspan=2, padx=(8, 6))
        self._settings_game_btn = self.kit.button(
            header,
            tr("action.game_settings"),
            style="ghost",
            command=self._on_game_settings,
            width=112,
            height=38,
        )
        self._settings_game_btn.grid(row=0, column=2, rowspan=2)

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
        # 左侧是操作反馈, 右下角是备份服务状态(原本在侧边栏的状态卡里).
        self._feedback_label = self.kit.label(
            statusbar, tr("status.ready"), style="muted", size=12
        )
        self._feedback_label.pack(side="left", padx=18, pady=4)

        self._service_card = ctk.CTkFrame(statusbar, fg_color="transparent")
        self._service_card.pack(side="right", padx=18, pady=4)
        self._service_dot = ctk.CTkLabel(
            self._service_card,
            text="●",
            text_color=self.p.success,
            font=ctk.CTkFont(size=12),
        )
        self._service_dot.pack(side="left", padx=(0, 6))
        self.kit.register(lambda p: self._service_dot.configure(text_color=p.success))
        self._service_label = self.kit.label(
            self._service_card, "", style="muted", size=12
        )
        self._service_label.pack(side="left")
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
        # 图标位: 有缓存图标就换成图片, 没有才回落名称首字(CTkImage 要持引用).
        self._hero_icon: ctk.CTkImage | None = None

        info = ctk.CTkFrame(left, fg_color="transparent")
        # expand=True: 信息块占满色块右边的全部宽度, 名称因此能"撑满一行".
        info.pack(side="left", fill="both", expand=True, padx=(18, 0))
        self.kit.label(info, tr("hero.current_game"), style="muted", size=11).pack(
            anchor="w", pady=(2, 0)
        )
        self._hero_name_label = self.kit.label(
            info, "", style="primary", size=20, weight="bold"
        )
        self._hero_name_font = ctk.CTkFont(size=20, weight="bold")
        self._hero_name_label.configure(wraplength=_HERO_TEXT_WIDTH, justify="left")
        self._hero_name_label.pack(fill="x", anchor="w", pady=(2, 0))
        self._hero_location_label = self.kit.label(info, "", style="body", size=13)
        self._hero_location_label.pack(anchor="w", pady=(3, 0))
        self._hero_verified_label = ctk.CTkLabel(
            info, text="", font=ctk.CTkFont(size=12, weight="bold")
        )
        self._hero_verified_label.pack(anchor="w", pady=(6, 0))
        # 额外信息: 原始名称(改过名时)与备份根下的目录名.
        self._hero_origin_label = self.kit.label(info, "", style="muted", size=11)
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
            [
                SourceFilter.ALL.label,
                SourceFilter.MANUAL.label,
                SourceFilter.AUTO.label,
                SourceFilter.SAFETY.label,
            ],
            SourceFilter.ALL.label,
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
        self._task_name_label.configure(wraplength=180, justify="left")
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

        self._task_progress_label = self.kit.label(panel, "", style="hint", size=12)
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
        self._task_next.configure(wraplength=180, justify="left")
        self._task_next.grid(row=4, column=1, padx=(0, 18), sticky="e")

        self.kit.label(panel, tr("task.target"), style="muted", size=12).grid(
            row=5, column=0, padx=18, pady=(6, 0), sticky="nw"
        )
        # 备份目标是完整路径, 必须换行显示, 否则会被卡片裁掉.
        self._task_target = self.kit.label(panel, "", style="hint", size=12)
        self._task_target.configure(wraplength=190, justify="left")
        self._task_target.grid(row=5, column=1, padx=(0, 18), pady=(6, 0), sticky="ne")

        # 快捷键不在任务状态卡里展示: 它属于全局设置, 统一在"设置"窗口里查看与修改。
        self._task_hint = self.kit.label(panel, "", style="hint", size=12)
        self._task_hint.configure(wraplength=250, justify="left")
        self._task_hint.grid(
            row=6, column=0, columnspan=2, padx=18, pady=(12, 12), sticky="w"
        )

    # ---------------------------------------------------------------- 数据装载

    def _refresh_usage(self) -> None:
        """刷新状态栏的存储占用文本.

        目录扫描成本不低, 因此只在启动与数据版本变化时重算一次, 其余时候
        复用缓存文本(``_render_task`` 会高频调用)。
        """
        self._usage_text = tr(
            "status.usage", used=size_label(self.backend.storage_usage())
        )

    def _load_first_game(self) -> None:
        """启动时选中第一款游戏(详情页有内容), 默认页面仍是游戏主页.

        启动阶段的读取失败只记日志: 这里抛异常等于应用根本起不来。
        """
        try:
            self._load_first_game_now()
        except (ArchiveManagementError, sqlite3.Error) as exc:
            self._report_read_failure(exc)

    def _load_first_game_now(self) -> None:
        """执行一次真实的启动加载(异常由 :meth:`_load_first_game` 统一兜住)."""
        games = self.backend.list_games()
        self._refresh_usage()
        if games:
            self._select_game(games[0].game_id)
        else:
            self._show_empty_list()
        self._render_task(self.backend.task_status(self._game_id))

    def _show_empty_list(self) -> None:
        """没有游戏时的空状态: 概要区、选中面板与备份列表一起复位.

        删除最后一个游戏后, 概要区(名称/存档位置/统计)、标题行与选中面板都
        还停留在被删的那款游戏上, 因此这里把它们全部复位。
        """
        self._game_id = None
        self._game = None
        self._backup_id = None
        self._current_id = None
        self._items = []
        self._cards = {}
        self._hover_id = None
        # 没有选中游戏时不要把名称留在状态里, 否则缩放窗口会盖掉占位文案.
        self._detail_name = ""
        self._detail_subtitle = ""
        self._list_title.configure(text=tr("list.fallback_title"))
        self._list_sub.configure(text=tr("list.no_games"))
        self._title_label.configure(text=tr("hero.no_game"))
        self._subtitle_label.configure(text=tr("list.no_games"))
        # 清图标用空串而不是 None: 用 None 时 Tk 侧的 image 选项不会变, 会出现
        # "图标位缺一块颜色"(旧图残留在标签上), 而且旧图一旦被回收标签就报错.
        self._hero_tile.configure(image="", text="", fg_color=self._tone_color(None))
        self._hero_icon = None
        self._hero_name_label.configure(text=tr("hero.no_game"))
        self._hero_location_label.configure(text="")
        self._hero_verified_label.configure(text="")
        self._hero_origin_label.pack_forget()
        self._stat_recent_value.configure(text="—")
        self._stat_recent_sub.configure(text="")
        self._stat_total_value.configure(text=tr("detail.backups_none"))
        self._stat_total_sub.configure(text="")
        self._stat_next_value.configure(text="—")
        self._render_selected(None)
        self._render_task(self.backend.task_status(None))
        self._update_actions()
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
            # 游戏已被删除: 不要留着指向上一次选中项的“半选中”状态.
            self._show_empty_list()
            return
        log_action("ui.select_game", basic=True, game_id=game_id, name=self._game.name)
        detail = self.backend.get_detail(game_id)
        self._backup_id = None
        self._render_hero_tile(game_id, detail.name, self._game.tone)
        self._render_hero(detail)
        self._render_toolbar_header(detail)
        self._render_list()
        self._render_selected(None)
        self._render_task(self.backend.task_status(game_id))
        self._update_actions()
        self.kit.apply(self.p)
        if self._game is not None and not self._game.has_locations:
            self._feedback(FeedbackKind.INFO, tr("game.no_locations_hint"))

    def _render_origin(self, detail: GameDetail) -> None:
        """展示"原始名称 / 备份目录"补充信息(无内容时不占位)."""
        text = detail.origin_label
        if not text:
            self._hero_origin_label.pack_forget()
            return
        self._hero_origin_label.configure(text=text)
        self._hero_origin_label.pack(anchor="w", pady=(4, 0))

    def _render_hero(self, detail: GameDetail) -> None:
        self._verified = detail.location_verified
        self._detail_name = detail.name
        self._set_detail_names()
        self._render_origin(detail)
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

    def _notice(self, text: str) -> None:
        """把发现分区的提示(如"开始尝试探测封面")写到左下角状态栏."""
        self._feedback(FeedbackKind.INFO, text)

    def _render_hero_tile(self, game_id: str, name: str, tone: str | None) -> None:
        """概要区的图标位: 有缓存图标就显示图标, 否则回落首字色块.

        两个坑写在这里: ① 没有图标时要传 ``image=""`` 而不是 ``None`` —— ``CTkLabel``
        在 image 为 None 时**不会**清掉 Tk 侧已有的 image 选项, 旧图会残留在色块位置;
        ② 先 configure 再放掉旧的 ``CTkImage`` —— 一旦它在标签还指着图片名时被回收,
        这个标签就彻底坏了(之后 configure 任何选项都报 image "pyimageN" doesn't exist,
        表现为"打开过一次带图标的游戏后返回主页, 再点任何游戏都没反应")。
        """
        previous = self._hero_icon
        picture = self._icon_image(game_id)
        self._hero_icon = picture
        self._hero_tile.configure(
            image=picture if picture is not None else "",
            text="" if picture is not None else name[:1],
            fg_color="transparent" if picture is not None else self._tone_color(tone),
        )
        del previous  # 标签已经换上新图/清空, 现在才能安全地放掉旧图

    def _icon_image(self, game_id: str) -> ctk.CTkImage | None:
        """读取缓存里的方形图标; 没有图片或解码失败时返回 None(走首字回落)."""
        try:
            path = self.backend.artwork_path(game_id, "icon")
        except ArchiveManagementError as exc:
            logger.debug("读取图标失败: %s", exc)
            return None
        if not path:
            return None
        try:
            with Image.open(path) as image:
                loaded = image.copy()
        except (OSError, ValueError) as exc:
            logger.warning("图标无法解码(%s): %s", path, exc)
            return None
        return ctk.CTkImage(light_image=loaded, size=(84, 84))

    def _render_toolbar_header(self, detail: GameDetail) -> None:
        """写头部标题/副标题(按控件实际宽度裁剪, 见 _refit_detail_names)."""
        self._detail_name = detail.name
        self._detail_subtitle = detail.subtitle
        self._set_detail_names()

    def _name_budget(self, label: ctk.CTkLabel, fallback: int) -> int:
        """控件当前的可用宽度(还没测量出来时用设计预算兜底)."""
        width = label.winfo_width()
        return width if width > 1 else fallback

    def _set_detail_names(self) -> None:
        """按各控件**当前宽度**裁剪名称: 窗口变宽就多显示几个字.

        超出部分补省略号并限制行数 —— 否则超长名称会把下面的内容整排推下去。
        """
        if not self._detail_name:
            return
        title_budget = self._name_budget(self._title_label, _HEADER_TEXT_WIDTH)
        self._title_label.configure(
            wraplength=title_budget,
            text=fit_text(
                self._detail_name,
                self._title_font,
                title_budget,
                max_lines=_HEADER_NAME_LINES,
            ),
        )
        # 副标题里带存档位置(可能是很长的路径): 同样封顶两行, 头部高度有上限.
        subtitle_budget = self._name_budget(self._subtitle_label, _HEADER_TEXT_WIDTH)
        self._subtitle_label.configure(
            wraplength=subtitle_budget,
            text=fit_text(
                self._detail_subtitle,
                self._subtitle_font,
                subtitle_budget,
                max_lines=_HEADER_NAME_LINES,
            ),
        )
        hero_budget = self._name_budget(self._hero_name_label, _HERO_TEXT_WIDTH)
        self._hero_name_label.configure(
            wraplength=hero_budget,
            text=fit_text(
                self._detail_name,
                self._hero_name_font,
                hero_budget,
                max_lines=_HERO_NAME_LINES,
            ),
        )

    def _refit_detail_names(self) -> None:
        """窗口宽度变化后按新宽度重裁详情页里的名称(延后合并成一次)."""
        self._refit_job = None
        if self._page is AppPage.DETAIL:
            self._set_detail_names()

    def _on_content_resize(self, _event: tk.Event) -> None:
        """内容区尺寸变化: 名称的可用宽度变了, 延后重新裁一次.

        只合并、不推后(已有任务就不另排): 取消重排会让密集的尺寸事件把任务无限拖延,
        名称就一直按旧宽度裁着 —— 与 ``HomePage._schedule_list_sync`` 同一条规则。
        """
        if self._page is not AppPage.DETAIL or not self._detail_name:
            return
        if self._refit_job is not None:
            return
        self._refit_job = self._content.after(_REFIT_DELAY_MS, self._refit_detail_names)

    def _render_task(self, task: TaskStatus) -> None:
        """刷新任务状态卡: 运行中的操作优先, 否则显示定时任务的启用状态."""
        state = tr("task.running") if task.running else _schedule_state(task)
        self._task_name_label.configure(text=task.task_name)
        self._task_state_label.configure(text=state)
        self._task_progress.set(task.progress)
        self._task_progress_label.configure(
            text=task.progress_label if task.running else ""
        )
        self._task_next.configure(text=task.next_run_label)
        self._task_target.configure(text=task.target_label)
        self._task_hint.configure(text=tr("task.hint"))
        self._cancel_btn.configure(state="normal" if task.cancellable else "disabled")
        # 右下角的服务状态: 服务正常 + 备份占用(原本显示在侧边栏的状态卡里).
        self._service_label.configure(
            text=f"{tr('status.service_ok')} · {self._usage_text}"
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
        try:
            task = self.backend.task_status(self._game_id)
        except (ArchiveManagementError, sqlite3.Error) as exc:
            self._report_read_failure(exc)
            return
        self._render_task(task)
        self._task_running = task.running
        if task.revision != self._revision:
            self._reload_data(task)

    def _reload_data(self, task: TaskStatus | None = None) -> None:
        """重载备份列表与概要; 读取失败只记日志并保留上次画面.

        轮询与界面回调都会走到这里: 异常冒到 Tk 回调里会让整个窗口报错, 而数据层
        这类失败(磁盘/文件被外部短暂占用)通常下一秒就会恢复。
        """
        try:
            self._reload_data_now(task)
        except (ArchiveManagementError, sqlite3.Error) as exc:
            self._report_read_failure(exc)

    def _reload_data_now(self, task: TaskStatus | None) -> None:
        """执行一次实际重载(业务/数据库异常由 :meth:`_reload_data` 统一兜住)."""
        status = task if task is not None else self.backend.task_status(self._game_id)
        if status.revision != self._revision:
            self._refresh_usage()
        self._revision = status.revision
        self._render_task(status)
        if self._page is AppPage.HOME:
            # 主页可见时重绘一次: 封面与图标是导入后由后台补的, 补好只会抬高数据
            # 版本, 主页不重绘就还是旧的那一屏(图标一直不出现).
            self._home_page.refresh_artwork()
        if self._game_id is None:
            return
        games = self.backend.list_games()
        if not any(game.game_id == self._game_id for game in games):
            # 当前游戏已被删除(例如在别处删除后由轮询触发的重载): 整体回到空状态.
            self._show_empty_list()
            return
        self._render_list()
        detail = self.backend.get_detail(self._game_id)
        self._render_hero(detail)
        # 封面探测在后台跑完会抬高数据版本, 这里顺手把图标位也重读一次.
        tone = None if self._game is None else self._game.tone
        self._render_hero_tile(self._game_id, detail.name, tone)
        self._restore_selection()

    def _report_read_failure(self, exc: Exception) -> None:
        """读取失败: 记日志 + 状态栏提示(同样文案重复设置不会闪烁)."""
        logger.error("读取数据失败: %s", exc)
        self._feedback(FeedbackKind.ERROR, tr("error.read_failed", reason=str(exc)))

    def _restore_selection(self) -> None:
        """按最新数据重绘选中项(选中节点已消失时清空选择)."""
        item = self._selected_item()
        if item is None:
            self._backup_id = None
            self._render_selected(None)
            return
        self._render_selected(item)

    def _render_list(self) -> None:
        """重建左侧备份列表(时间线/分支树两种视图共用一条流水线)."""
        if self._game_id is None:
            # 空库(首次启动): 没有可展示的备份, 保持空状态.
            return
        game_id = self._game_id
        self._items = self.backend.list_backups(game_id)
        current = next((item for item in self._items if item.is_current), None)
        self._current_id = None if current is None else current.backup_id
        # 先按来源筛选再排序: 显式筛选"安全点"时, 分支视图也应把它们显示出来.
        selected = filter_by_source_label(self._items, self._filter_source.get())
        ordered, title, sub = self._ordered_for_view(
            selected,
            include_safety=(self._filter_source.get() == SourceFilter.SAFETY.label),
        )
        self._list_title.configure(text=title)
        self._list_sub.configure(text=sub)

        items = self._apply_period_filter(ordered)
        if self._backup_id is not None and not any(
            item.backup_id == self._backup_id for item in items
        ):
            self._backup_id = None
            self._render_selected(None)

        self._clear_cards()
        if not items:
            empty = self.kit.label(
                self._list_scroll,
                tr("list.empty_filtered"),
                style="muted",
                size=13,
            )
            empty.pack(padx=10, pady=16)
            return
        self._build_cards(items)

    def _ordered_for_view(
        self, selected: list[BackupItem], *, include_safety: bool
    ) -> tuple[list[BackupItem], str, str]:
        """按当前视图排序并给出列表标题与说明(时间线按时间, 分支树按线路)."""
        if self._view == ViewKind.TIMELINE:
            return (
                timeline_order(selected),
                tr("list.timeline_title"),
                tr("list.timeline_sub"),
            )
        return (
            branch_order(selected, include_safety=include_safety),
            tr("list.branch_title"),
            tr("list.branch_sub"),
        )

    def _clear_cards(self) -> None:
        """卸载卡片上登记的主题回调并清空列表区."""
        for unsubscribe in self._card_unregisters:
            unsubscribe()
        self._card_unregisters = []
        self._card_painters = {}
        for child in self._list_scroll.winfo_children():
            child.destroy()

    def _build_cards(self, items: list[BackupItem]) -> None:
        """逐张重建备份卡片, 并接上选中与悬停交互."""
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

    def _apply_period_filter(self, items: list[BackupItem]) -> list[BackupItem]:
        """按时间范围筛选备份节点(来源筛选已在排序前完成)."""
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
        log_action(
            "ui.select_backup",
            basic=True,
            game_id=self._game_id,
            backup_id=item.backup_id,
            title=item.display_title,
        )
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
        log_action("ui.switch_view", basic=True, view=view.value, game_id=self._game_id)
        self._render_list()
        self.kit.apply(self.p)

    def _on_filter_change(self, value: str) -> None:
        """筛选条件变化时刷新列表."""
        log_action(
            "ui.filter",
            basic=True,
            filter=value,
            period=self._filter_period.get(),
            game_id=self._game_id,
        )
        self._render_list()
        self._update_actions()

    def _update_actions(self) -> None:
        busy = self._busy
        game = self._game
        can_do_backup = (
            game is not None
            and game.has_locations
            and not busy
            and game.allow("backup")
        )
        self._backup_btn.configure(state="normal" if can_do_backup else "disabled")
        can_export = game is not None and not busy and game.allow("export")
        self._export_btn.configure(state="normal" if can_export else "disabled")
        selected = self._backup_id is not None
        nodes_ready = selected and not busy and game is not None
        # 恢复/分支/重命名/删除备份都属于"备份管理": 归档的游戏不允许其中任何一项.
        self._restore_btn.configure(
            state=_node_state(game, "restore", ready=nodes_ready)
        )
        self._branch_btn.configure(state=_node_state(game, "branch", ready=nodes_ready))
        self._rename_btn.configure(
            state=_node_state(game, "backup_edit", ready=nodes_ready)
        )
        self._delete_btn.configure(
            state=_node_state(game, "backup_delete", ready=nodes_ready)
        )

    def _on_backup(self) -> None:
        game = self._game
        if game is None or self._busy:
            return
        if not action_allowed("backup", archived=game.archived):
            self._feedback(
                FeedbackKind.INFO, tr("home.archived_blocked", name=game.name)
            )
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
        """恢复到此节点: 把备份内容写回原始存档, 之后的备份/分支都从此继续."""
        game = self._game
        item = self._selected_item()
        if game is None or item is None or self._busy:
            return
        if not action_allowed("restore", archived=game.archived):
            self._feedback(
                FeedbackKind.INFO, tr("home.archived_blocked", name=game.name)
            )
            return
        try:
            plan = self.backend.preview_restore(game.game_id, item.backup_id)
        except ArchiveManagementError as exc:
            self._feedback(FeedbackKind.ERROR, str(exc))
            return
        if not plan.snapshot_ok:
            self._feedback(
                FeedbackKind.ERROR,
                plan.snapshot_reason or tr("dialog.restore_invalid"),
            )
            return
        blocked = plan.blocked_targets
        if blocked:
            self._feedback(
                FeedbackKind.ERROR,
                tr(
                    "dialog.restore_blocked",
                    path=blocked[0].path,
                    reason=self._restore_problem_text(blocked[0].problem),
                ),
            )
            return
        options = restore_dialog(
            self,
            self.p,
            title=tr("dialog.restore_title"),
            summary=self._restore_summary(game.name, item, plan),
            safety_label=tr("dialog.restore_safety"),
            safety_hint=tr("dialog.restore_safety_hint"),
            safety_available=plan.safety_point_available,
            danger_note=self._restore_danger_note(plan),
        )
        if options is None:
            log_action(
                "restore",
                basic=True,
                result="cancelled",
                game_id=game.game_id,
                backup_id=item.backup_id,
            )
            self._feedback(FeedbackKind.INFO, tr("action.restore_canceled"))
            return
        safety_point = options
        # 预检已经确认游戏在运行, 这里代表用户看过提示后选择强制执行.
        force = plan.process.running
        backup_id = item.backup_id
        self._set_busy(True)
        self._feedback(FeedbackKind.PENDING, tr("action.restore_pending"))

        def work() -> str:
            return self.backend.run_restore(
                game.game_id,
                backup_id,
                safety_point=safety_point,
                force=force,
            )

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)
            self._reload_data()

        self._submit(work, ok)

    def _restore_summary(self, name: str, item: BackupItem, plan: RestorePlan) -> str:
        """拼装恢复对话框的摘要文本(快照规模与写回目标)."""
        targets = "\n".join(f"· {target.path}" for target in plan.targets)
        return tr(
            "dialog.restore_summary",
            name=name,
            title=item.display_title,
            files=plan.file_count,
            size=item.size_label,
            targets=targets,
        )

    def _restore_danger_note(self, plan: RestorePlan) -> str:
        """拼装需要用户额外确认的风险提示."""
        notes: list[str] = []
        if plan.process.running:
            notes.append(
                tr(
                    "dialog.restore_process",
                    matches=", ".join(plan.process.matches[:3]),
                )
            )
        notes.extend(tr(f"restore.warn_{code}") for code in plan.warnings)
        return "\n".join(notes)

    def _restore_problem_text(self, code: str | None) -> str:
        """把恢复预检的原因代码映射为文案."""
        if code is None:
            return ""
        return tr(f"restore.problem_{code}")

    def _on_branch(self, *, quick: bool = False) -> None:
        """创建分支; ``quick`` 为 True 时使用默认分支名且不弹窗(全局快捷键)."""
        game = self._game
        if game is None or self._backup_id is None or self._busy:
            return
        if not action_allowed("branch", archived=game.archived):
            self._feedback(
                FeedbackKind.INFO, tr("home.archived_blocked", name=game.name)
            )
            return
        branch_name: str | None = tr("dialog.branch_default")
        if not quick:
            branch_name = ask_branch_name(
                self,
                self.p,
                title=tr("dialog.branch_title"),
                text=tr("dialog.branch_prompt"),
                initial=branch_name or "",
            )
        if not branch_name:
            log_action(
                "branch.cancel",
                basic=True,
                game_id=game.game_id,
                parent_id=self._backup_id,
            )
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
            log_action(
                "backup.update_meta",
                basic=True,
                game_id=game.game_id,
                backup_id=item.backup_id,
                result="cancelled",
            )
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
                log_action(
                    "backup.delete",
                    basic=True,
                    game_id=game.game_id,
                    backup_id=item.backup_id,
                    result="cancelled",
                    removed=plan.removed_count,
                )
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

    def _on_toggle_theme(self) -> str:
        """切换浅/深主题并返回生效主题名(供设置窗口刷新按钮文案)."""
        next_theme = "light" if self._theme == "dark" else "dark"
        self._theme = self.backend.set_theme(next_theme)
        ctk.set_appearance_mode(self._theme)
        self.p = Palette.for_theme(self._theme)
        self.configure(fg_color=self.p.background)
        theme_text = (
            tr("theme.to_dark") if self._theme == "light" else tr("theme.to_light")
        )
        self.kit.apply(self.p)
        # 主页里的控件是按调色板逐一定色的, 换主题后要用新调色板重建一次.
        self._home_page.apply_palette(self.p)
        log_action("ui.toggle_theme", basic=True, theme=self._theme)
        self._feedback(FeedbackKind.INFO, tr("theme.switched", theme=theme_text))
        return self._theme

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
            log_action("game.add", basic=True, result="cancelled")
            return
        try:
            summary = self.backend.add_game(name)
        except ArchiveManagementError as exc:
            log_action("game.add", result="failed", error=str(exc))
            self._feedback(FeedbackKind.ERROR, str(exc))
            return
        log_action("game.add", game_id=summary.game_id, name=summary.name)
        self._feedback(
            FeedbackKind.SUCCESS,
            tr("result.game_added_disabled", name=summary.name),
        )
        self._refresh_after_manage(select=summary.game_id)

    def _on_open_schedules(self) -> None:
        """顶栏"定时任务"入口: 打开全局任务窗口(与设置共用一个窗口位置)."""
        self._open_workspace_window(
            tr("topbar.nav_scheduled"), self._open_schedule_window
        )

    def _on_open_settings(self) -> None:
        """顶栏"设置"入口: 打开设置窗口(与定时任务共用一个窗口位置)."""
        self._open_workspace_window(tr("topbar.nav_settings"), self._open_settings)

    def _open_workspace_window(
        self, nav: str, opener: Callable[[], _WorkspaceWindow]
    ) -> None:
        """打开工作区窗口; 已有窗口时只聚焦或给出提示, 不会重复开窗.

        重复点击同一个入口会把已打开的窗口提到前台; 点击另一个入口则提示先关闭
        当前窗口——同时开多个配置面板会互相遮挡, 也容易在过期的列表上操作。
        窗口被用户关闭后(``focus()`` 返回 False)再次点击就正常开新窗口。
        """
        active = self._active_window
        if active is not None and active.focus():
            if self._active_nav != nav:
                self._feedback(FeedbackKind.INFO, tr("topbar.busy"))
            return
        self._active_window = None
        self._active_nav = None
        created = opener()
        if created is None:  # pragma: no cover - 替身或异常路径
            return
        self._active_window = created
        self._active_nav = nav

    def _open_schedule_window(self) -> ScheduleWindow:
        """打开全局定时任务窗口(可新增/编辑/删除每个游戏的定时备份)."""
        log_action("ui.open_schedules", basic=True)
        return ScheduleWindow(
            self,
            backend=self.backend,
            palette=self.p,
            on_change=self._after_schedule_change,
        )

    def _after_schedule_change(self) -> None:
        """定时配置变化后刷新任务卡(概要区的下次运行时间也随之一同更新)."""
        self._render_task(self.backend.task_status(self._game_id))
        if self._game_id is not None:
            self._render_hero(self.backend.get_detail(self._game_id))

    def _on_game_settings(self) -> None:
        self._open_manage_game()

    def _on_back_home(self) -> None:
        """返回游戏主页(主窗口内的默认页面)."""
        log_action("ui.show_home", basic=True, source="detail")
        self._show_page(AppPage.HOME)

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
            archived=game.archived,
            backup_location=backup_path,
            on_change=lambda: self._refresh_after_manage(),
        )

    def _refresh_after_manage(self, *, select: str | None = None) -> None:
        """游戏或存档位置变更后重载详情页并保持/恢复选中.

        侧边栏已经去掉, 游戏列表由游戏主页承载, 因此这里只维护"当前游戏"与详情页;
        主页可见时顺手刷新它(不可见时进入页面会重新读取, 无需在这里重算)。
        """
        games = self.backend.list_games()
        if self._page is AppPage.HOME:
            self._home_page.reload()
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
            # 最后一个游戏被删掉: 连概要区一起回到空状态.
            self._show_empty_list()

    def _open_settings(self) -> SettingsWindow:
        """打开设置窗口(主题切换与快捷键录制; 不包含定时任务配置)."""
        log_action("ui.open_settings", basic=True, theme=self._theme)
        return SettingsWindow(
            self,
            palette=self.p,
            theme=self._theme,
            language=self._language,
            debug=self._debug,
            shortcuts=self._shortcuts,
            on_toggle_theme=self._on_toggle_theme,
            on_apply_language=self._on_language_change,
            on_apply_debug=self._on_debug_change,
            on_apply_shortcut=self._apply_shortcut,
            on_capture_start=self._hotkeys.suspend,
            on_capture_end=self._hotkeys.resume,
        )

    def _apply_shortcut(self, action: str, accelerator: str) -> str | None:
        """应用设置窗口录制到的组合键: 校验 → 重新注册 → 写回配置.

        返回 None 表示成功; 否则返回可直接展示给用户的失败说明(注册失败时
        会把上一个可用的组合恢复回去, 避免用户无声地失去快捷键)。
        """
        reason = combo_error(parse_accelerator(accelerator))
        if reason is not None:
            return tr(f"hotkey.err_{reason}")
        previous = self._shortcuts.get(action, accelerator)
        callback = (
            self._request_hotkey_branch
            if action == ACTION_CREATE_BRANCH
            else self._request_hotkey_backup
        )
        self._hotkeys.unregister(action)
        state = self._hotkeys.register(
            HotkeyBinding(name=action, accelerator=accelerator), callback
        )
        if not state.registered:
            self._hotkeys.register(
                HotkeyBinding(name=action, accelerator=previous), callback
            )
            log_action("hotkey.apply_failed", hotkey=action, accelerator=accelerator)
            return state.error or tr("hotkey.unavailable")
        self._shortcuts[action] = accelerator
        self._save_shortcuts()
        log_action("hotkey.apply", hotkey=action, accelerator=accelerator)
        self._feedback(
            FeedbackKind.INFO,
            tr("hotkey.updated", combo=format_accelerator(accelerator)),
        )
        return None

    def _on_close(self) -> None:
        """退出前释放调度器与快捷键监听, 避免遗留后台线程."""
        self._hotkeys.shutdown()
        try:
            self.backend.shutdown()
        except Exception as exc:  # pragma: no cover - 退出期异常不阻塞关闭
            # GUI 不使用 print: 退出期的问题只写日志, 不干扰界面.
            logger.warning("释放后台资源失败: %s", exc)
        self.destroy()

    def destroy(self) -> None:
        """销毁前撤掉挂在自己身上的定时任务.

        消息轮询(每 100ms)与详情名称重裁都是 ``after`` 任务; 控件销毁后它们仍在 Tk 的
        队列里, 下一次事件循环会以 ``invalid command name "..._poll_messages"`` 报错 ——
        输出落到 stderr, 在 CI 里会挂到**下一个用例**的 stderr 附件上掩盖真问题(实测报告
        里的 stderr 附件就是这么来的)。

        任务 id 从 ``self.__dict__`` 里取而不是 ``getattr``: 构造中途失败时这些字段还没建好,
        而 Tk 控件的 ``__getattr__`` 会把未知名字转发给 ``self.tk``(连 ``tk`` 都还没有时
        会无限递归成 ``RecursionError``)。销毁函数自己不能因为"属性没建好"再抛一个异常,
        把真正的失败现场搅乱。
        """
        for name in ("_poll_job", "_refit_job"):
            job = self.__dict__.get(name)
            if job is not None:
                with contextlib.suppress(tk.TclError):
                    self.after_cancel(job)
            setattr(self, name, None)
        super().destroy()

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
            except ContentUnchangedError as exc:
                self._messages.put(("unchanged", exc.target_label))
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
                self._run_hotkey(payload)
                continue
            self._finish_message(kind, payload)
        self._refresh_task()
        self._poll_job = self.after(100, self._poll_messages)

    def _run_hotkey(self, payload: str) -> None:
        """执行快捷键请求; 停用或归档的游戏不触发(全局只有一款游戏启用)."""
        game = self._game
        if game is None:
            return
        if not (game.enabled and not game.archived):
            log_action("hotkey.skipped", game_id=game.game_id, reason="inactive")
            self._feedback(FeedbackKind.INFO, tr("hotkey.game_inactive"))
            return
        if payload == ACTION_CREATE_BRANCH:
            self._on_branch(quick=True)
        else:
            self._on_backup()

    def _finish_message(
        self, kind: Literal["ok", "err", "unchanged", "hotkey"], payload: str
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
        if kind == "unchanged":
            # 存档与参照备份完全一致: 弹窗告知, 不当作错误.
            message = tr("dialog.unchanged_backup", backup=payload)
            info_dialog(
                self,
                self.p,
                title=tr("dialog.unchanged_title"),
                message=message,
            )
            self._feedback(FeedbackKind.INFO, message)
            return
        if self._canceled:
            self._canceled = False
            self._feedback(FeedbackKind.INFO, payload)
        else:
            self._feedback(FeedbackKind.ERROR, payload)

    def _tone_color(self, tone: str | None) -> str:
        return _TONE_COLORS.get(tone or "", _TONE_COLORS["default"])


def _node_state(game: GameSummary | None, action: GameAction, *, ready: bool) -> str:
    """备份管理类按钮的状态: 未选中节点或动作被规则禁用时置灰."""
    if not ready or game is None:
        return "disabled"
    return "normal" if game.allow(action) else "disabled"


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
    verbose: bool = False,
) -> int:
    """启动基于 SQLite 的真实后端并进入主循环, 返回退出码.

    日志会在进入主循环前装配: 默认只记录 INFO 及以上的高风险操作,
    ``verbose=True`` 或配置里的“启用调试日志”打开时才把 DEBUG 也记进去;
    单文件上限与保留份数取自配置里的 ``logging`` 段。
    """
    from archive_management.config import load_or_reset_config
    from archive_management.infrastructure.database import Database
    from archive_management.logging_config import configure_from_settings
    from archive_management.ui.sql_backend import SqlArchiveService

    paths = ApplicationPaths.default().ensure() if paths is None else paths.ensure()
    loaded = load_or_reset_config(paths.config_path)
    configure_from_settings(paths.log_dir, loaded.config.logging, verbose=verbose)
    if loaded.reset:
        logger.warning("配置文件内容非法, 已还原为默认值: %s", loaded.backup)
    logger.info("界面启动(verbose=%s)", verbose)
    database = Database(paths.database_path)
    database.migrate()
    backend: ArchiveService = SqlArchiveService(
        database, backup_root=paths.backup_root, cache_dir=paths.cache_dir
    )
    app = ArchiveApp(
        backend,
        title=display_name,
        smoke_seconds=smoke_seconds,
        paths=paths,
    )
    app.mainloop()
    return 0


def default_theme() -> str:
    """返回默认主题名(供设置显示)."""
    return DEFAULT_THEME
