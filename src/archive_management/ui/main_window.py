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
import time
import tkinter as tk
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol

import customtkinter as ctk
from PIL import Image

from archive_management.application.backup import MAX_NOTE_LENGTH
from archive_management.application.games import ActivationOutcome
from archive_management.application.imports import BatchInspection, ImportInspection
from archive_management.application.restore import RestorePlan
from archive_management.config import (
    AppConfig,
    ConfigLoad,
    HotkeySettings,
    WindowSettings,
    load_or_repair_config,
    save_config,
)
from archive_management.domain import (
    ACTIVATION_DELAY_LADDER,
    REASON_DISABLED,
    REASON_ENABLED,
    REASON_FALLBACK,
    GameAction,
    VerificationMode,
    action_allowed,
    activation_delay,
    normalize_verification_mode,
)
from archive_management.exceptions import (
    ArchiveManagementError,
    ContentUnchangedError,
    OperationCancelledError,
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
from archive_management.services.naming import game_slug
from archive_management.ui.backend import ArchiveService
from archive_management.ui.dialogs import (
    ask_branch_name,
    ask_text,
    batch_import_dialog,
    confirm_dialog,
    edit_backup_dialog,
    export_batch_dialog,
    import_package_dialog,
    info_dialog,
    restore_dialog,
)
from archive_management.ui.home_page import HomePage
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.metrics import (
    RADIUS_LG,
    RADIUS_MD,
    RADIUS_NONE,
    RADIUS_PILL,
)
from archive_management.ui.models import (
    AppPage,
    BackupItem,
    BatchImportSelection,
    FeedbackKind,
    GameDetail,
    GameSummary,
    ImportChoice,
    SourceFilter,
    TaskStatus,
    ViewKind,
    backup_card_detail,
    batch_export_filename,
    batch_import_prompt,
    branch_order,
    export_batch_prompt,
    exportable_games,
    filter_by_source_label,
    import_prompt,
    import_strategies,
    size_label,
    timeline_order,
)
from archive_management.ui.palette import DEFAULT_THEME, Palette
from archive_management.ui.pickers import pick_file, pick_save_file
from archive_management.ui.rendering import host_image
from archive_management.ui.schedule_window import ScheduleWindow
from archive_management.ui.settings_window import SettingsWindow
from archive_management.ui.tree_view import TreeView
from archive_management.ui.typography import (
    FONT_STRONG,
    drain_deferred_fonts,
    install_font_scaling,
    set_base_font_px,
)
from archive_management.ui.widgets import (
    UiKit,
    attach_tooltip,
    card_surface_colors,
    fit_label,
    scaled_px,
    track_fit,
    window_scaling,
    wrap_budget,
)

logger = logging.getLogger(__name__)

# 首字占位块的文字色: 它坐在**固定色调**的色块上(见下面的 _TONE_COLORS, 这批颜色故意不跟
# 主题走), 所以文字固定用白 —— 写成具名常量而不是调用里的字面量, 让"这个白是故意的"一眼可见。
# (原先拦这类字面量的静态检查 C9101 已撤, 理由见 PLAN §17.3 —— 现在由 test_gui_theme_repaint
# 在运行期整屏核对配色。)
_HERO_TILE_TEXT = "#ffffff"

_TONE_COLORS: dict[str, str] = {
    "orange": "#d15b3e",
    "blue": "#405685",
    "green": "#3b806e",
    "default": "#405685",
}
# 支持的最小窗口尺寸: 主页里那些固定宽度的行必须能放进"最小窗口下的内容区",
# 否则在更窄的屏幕(窗口被窗口管理器再压小)上会越界并盖住描边。
# 2026-10-03 从 1200x720 降到 **1024x720**: CI 的 Windows/macOS runner 桌面只有约
# 1024x768, 而旧下限比桌面还宽 —— 窗口管理器会把窗口夹回 1024, 于是界面用例跑在应用
# **自己声明不支持**的尺寸下(两条 macOS 尺寸用例正是这么红的, 见 PLAN §42.2/§44)。
# 1024 落在 runner 与老式笔记本屏之内, 并且仍然装得下固定宽度的行 —— 后半句由
# ``test_gui_layout.test_home_widgets_fit_the_supported_minimum_window`` 守着。
# 高度 720 不变: 它是 768 高的屏去掉标题栏后的可用高度, 再矮就真的放不下内容。
WINDOW_MIN_SIZE = (1024, 720)
# 打开时的默认尺寸(设计尺寸): 屏幕装不下就按屏幕夹(见 initial_window_size)。
WINDOW_DEFAULT_SIZE = (1360, 860)
# 按屏幕夹尺寸时留出的余量: 标题栏 + 任务栏 + 一点呼吸空间。屏幕尺寸是整块屏幕
# (不含任务栏信息), 所以只看 winfo_screenheight 会算出"贴到屏幕最下沿"的窗口。
SCREEN_MARGIN = 96


def fit_window_size(size: tuple[int, int], screen: tuple[int, int]) -> tuple[int, int]:
    """把窗口尺寸夹进屏幕: 不超出 ``屏幕 - SCREEN_MARGIN``, 但不低于最小尺寸.

    最小尺寸是布局的硬下限(主页那些固定宽度的列需要它), 屏幕比它更小时只能不夹 ——
    再往下压会把内容挤出窗口。
    """
    return (
        max(WINDOW_MIN_SIZE[0], min(size[0], screen[0] - SCREEN_MARGIN)),
        max(WINDOW_MIN_SIZE[1], min(size[1], screen[1] - SCREEN_MARGIN)),
    )


def initial_window_size(window: tk.Misc, *, scale: float = 1.0) -> tuple[int, int]:
    """主窗口打开时的默认尺寸: 设计尺寸, 但不超出屏幕(逻辑像素).

    固定 1360x860 在 1366x768 / 1920x900 这类屏幕上比屏幕还高, 窗口下沿(底部状态
    条)会落到屏幕外面去(与评审时"设置窗口比屏幕高"同一类问题)。宽度同理:
    1366 宽的屏幕上 1360 会把窗口右沿顶到屏幕边上。

    尺寸的夹法是 :func:`fit_window_size`(记住的几何也走同一条)。

    ``scale`` 是窗口缩放(见 ``widgets.window_scaling``): 屏幕尺寸是**物理**的、设计尺寸与
    ``CTk.geometry()`` 是**逻辑**的, 所以先把屏幕换到逻辑像素再夹 —— 不换算的话 125% 的屏上
    "不超出屏幕"这条根本不成立(窗口实际会比夹出来的值再大 1.25 倍)。
    """
    factor = scale if scale > 0 else 1.0
    return fit_window_size(
        WINDOW_DEFAULT_SIZE,
        (
            round(int(window.winfo_screenwidth()) / factor),
            round(int(window.winfo_screenheight()) / factor),
        ),
    )


def open_window_geometry(
    saved: WindowSettings, *, screen: tuple[int, int], scale: float = 1.0
) -> str:
    """算出主窗口打开时的 geometry 串: 有记住的整套几何就用它, 否则用设计尺寸.

    尺寸走 :func:`fit_window_size`(换了更小的屏幕也不会出屏); 位置**夹回屏幕内** ——
    上次摆在另一台显示器上、这次那台屏幕不在了(或分辨率变小)时, 记下来的 x/y 会落在
    屏幕外, 直接把窗口摆到屏幕外等于"打开就找不到窗口"。

    ``scale`` 是窗口缩放(见 ``widgets.window_scaling``): 屏幕尺寸是物理的, 而记住的尺寸与
    ``CTk.geometry()`` 都是**逻辑**的 —— 所以先把屏幕换到逻辑像素再夹取, 位置则用**物理**尺寸
    去夹(位置本身是屏幕坐标, CTk 不动它)。
    """
    factor = scale if scale > 0 else 1.0
    geometry = saved.geometry()
    logical_size = geometry[:2] if geometry is not None else WINDOW_DEFAULT_SIZE
    logical_screen = (round(screen[0] / factor), round(screen[1] / factor))
    width, height = fit_window_size(logical_size, logical_screen)
    size = f"{width}x{height}"
    if geometry is None:
        return size
    x = min(max(0, geometry[2]), max(0, screen[0] - round(width * factor)))
    y = min(max(0, geometry[3]), max(0, screen[1] - round(height * factor)))
    return f"{size}+{x}+{y}"


def _window_state(window: tk.Wm) -> str:
    """``wm state`` 的取值; 读不到(窗口已销毁)时返回 ``"unknown"``, 调用方按"不记"处理."""
    try:
        return str(window.state())
    except tk.TclError:  # pragma: no cover - 窗口已经销毁
        return "unknown"


def _window_flag(window: tk.Wm, name: str) -> bool:
    """读一个布尔窗口属性; 该平台没有这个属性(或读不到)时按 ``False`` 处理."""
    try:
        return bool(window.attributes(name))
    except tk.TclError:
        return False


def current_window_geometry(
    window: tk.Tk, *, scale: float = 1.0
) -> WindowSettings | None:
    """读窗口当前的尺寸与位置; 最大化/最小化/全屏时返回 ``None``(这次跳过这一项).

    三种状态都不是"用户摆出来的那一套", 所以都不记(跳过 = 保留上一次记下的值):

    * ``state() != "normal"`` —— Windows 上最大化是 ``zoomed``、最小化是 ``iconic``;
      实测最大化时读到的是 **3440x1369+-8+-8**(整块屏幕), 记下来下次打开就不是用户
      摆的那个大小了(而且下次打开也不该直接最大化);
    * ``-zoomed`` —— X11 上的最大化**不改 state**(仍是 ``normal``), 只有这个属性为 1;
      Windows 没有这个属性(实测报 ``bad attribute``), 读不到就当没最大化;
    * ``-fullscreen`` —— 全屏时 state 同样不变(实测仍是 ``normal``), 不判这条会把
      "整块屏幕"当成用户摆的尺寸记下来。

    位置读的是 ``winfo_x/y`` 而不是 ``winfo_rootx/rooty``: 前者与 ``geometry("+x+y")``
    是**同一套坐标**(实测在这台 Windows 上 ``root`` 比 ``x`` 大 8/31 —— 那是窗口边框
    与标题栏), 拿 root 坐标去复原会让窗口每开一次就往下右漂一格。

    ``scale`` 是窗口缩放(见 :func:`window_scaling`): 尺寸要**除回逻辑像素**再记
    (与 ``CTk.geometry()`` 同一套单位), 位置是屏幕坐标, 不参与换算。
    """
    if _window_state(window) != "normal":
        return None
    if _window_flag(window, "-zoomed") or _window_flag(window, "-fullscreen"):
        return None
    factor = scale if scale > 0 else 1.0
    return WindowSettings(
        width=round(int(window.winfo_width()) / factor),
        height=round(int(window.winfo_height()) / factor),
        x=int(window.winfo_x()),
        y=int(window.winfo_y()),
    )


# 头部标题(左上角那一处游戏名)**按控件实际宽度**裁剪: 窗口变宽就能多显示几个字。
# 这个常量只是"控件尺寸还没测量出来时"的落位预算(取自最小窗口下的实测可用宽度:
# 头部标题区 922px), 之后由 _refit_detail_names() 按真实宽度重裁。名称最多显示两行,
# 超出补省略号, 否则会把下面的内容整排推下去。
# 概要卡里**不再重复**印一遍游戏名(用户 2026-10-03: 名称只留左上角那一处), 所以这里
# 也没有 _HERO_TEXT_WIDTH / _HERO_NAME_LINES 了。
_HEADER_TEXT_WIDTH = 900
# 名称最多显示的行数(超出补省略号): 完整名称仍可在游戏设置/重命名对话框里看到。
_HEADER_NAME_LINES = 2
# 拖窗口时把名称重裁合并成一次(每个像素都跑一遍会卡).
_REFIT_DELAY_MS = 60
# 任务卡里的名称/值: 控件还没测量出来时的落位预算(实测侧栏内容宽约 314px, 去掉
# 两侧 18px 内边距)。名称最多两行, 放不下补省略号。
_TASK_NAME_WIDTH = 260
_TASK_NAME_LINES = 2
_TASK_VALUE_WIDTH = 260
_RAIL_WIDTH = 350
# 右侧「选中备份」面板里的文字宽度: 面板宽不跟窗口走(轨道固定 ``_RAIL_WIDTH``, 去掉
# 滚动条约 17px —— 实测内宽 333px), 两侧各 16px 的 padx。所以这里可以拿常量兜底 ——
# "还没量出来"也算得对, 不像头部只能拿设计预算先顶着。
_SELECTED_PANEL_WIDTH = 333
_SELECTED_TEXT_INSET = 32
# 面板里"文件摘要"那一行的上限行数(它可能是一条很长的备注, 见 sel.digest)。
_SELECTED_FILES_LINES = 2
# 卡片副标题的左右内边距(见 _build_backup_card 的 padx=12).
_CARD_DETAIL_INSET = 24
# 当前节点标记: 后续备份/分支都从这个节点继续(图里画在小字那一行, 卡片上写在标题前).
_CURRENT_MARK = "●"
# 导出包默认的文件名后缀: 保存对话框里预填的名字由游戏名派生(`<slug>.archive.zip`),
# 用户仍可改成别的名字或目录.
EXPORT_FILE_SUFFIX = ".archive.zip"
# 自动启停的轮询间隔(见 domain.activation.activation_delay): 队列为空时用最快档
# (尽快发现"游戏启动了"), 有游戏在运行时逐档放慢到上限。
_ACTIVATION_FAST_SECONDS = ACTIVATION_DELAY_LADDER[0]


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
        # 体检完成的包(读包在后台线程, 结果先放这里再经消息队列通知主线程弹框);
        # 单游戏包与批量包共用这一个位置, 弹框时按类型分派。
        self._inspection: ImportInspection | BatchInspection | None = None
        # 批量导入是不是因为用户取消而停下的(服务层用异常表达取消, 但这不是失败).
        self._batch_cancelled = False
        self._task_ticks = 0
        self._cards: dict[str, ctk.CTkFrame] = {}
        self._card_painters: dict[str, Callable[[Palette], None]] = {}
        self._card_unregisters: list[Callable[[], None]] = []
        # 详情页当前显示的**完整**名称与副标题: 窗口宽度变化时要按新的可用宽度重裁。
        self._detail_name = ""
        self._detail_subtitle = ""
        self._refit_job: str | None = None
        # 右侧面板三行的**完整**文本(名称/元信息/摘要): 同样要在宽度变化后重裁。
        self._selected_texts: tuple[str, str, str] = ("", "", "")
        # 消息轮询任务的 id(destroy() 里要撤掉, 见那里的说明)。
        self._poll_job: str | None = None

        self._messages: queue.Queue[
            tuple[
                Literal["ok", "err", "unchanged", "hotkey", "activation", "inspected"],
                str,
            ]
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
        # 界面字号(px 口径的基准字号, 1rem): 要在建界面**之前**生效, 因此先把缩放
        # 装到 CTkFont 上 —— 之后所有字体都按它换算。
        install_font_scaling()
        self._base_font_px = set_base_font_px(loaded.config.ui.base_font_px).base_px
        # 记住窗口几何的开关(默认开): 关掉时关窗不写, 且把已经记下的那套删掉。
        self._remember_window = loaded.config.ui.remember_window
        # 备份校验方式与并发校验数(设置窗口只读展示后者; 数值由本机情况算出后落盘)。
        self._verification_mode: VerificationMode = loaded.config.verification.mode
        self._verification_parallel = loaded.config.verification.max_parallel
        # 自动启停开关同样取自这份配置(默认关闭); 轮询结果、"是否忙"、队列是否非空
        # 与已经连续跑了几档(自适应间隔)都在下面维护。
        self._activation = loaded.config.activation.auto
        self._activation_busy = False
        self._activation_due = 0.0
        self._activation_run: ActivationOutcome | None = None
        self._activation_running = False
        self._activation_steps = 0
        self.title(title)
        self.minsize(*WINDOW_MIN_SIZE)
        self._apply_window_geometry(loaded.config.window)
        self.configure(fg_color=self.p.background)

        self._build_layout()
        self._register_hotkeys()
        self._load_first_game()
        # 软件打开后默认停在游戏主页(游戏很多时它比单个游戏的详情更有用).
        self._show_page(AppPage.HOME)
        # 界面构建完就把调色板刷一遍. 不刷的话, 顶栏/状态栏/页面容器会一直是
        # CustomTkinter 的默认灰, 顶栏那几个按钮还会是默认蓝 —— 而 "有游戏" 的
        # 情况下会误打误撞被 _load_first_game 里的选中动作刷上色, 于是**空库首次
        # 启动**才看得出问题(2026-09-27 实测: 顶栏与状态栏 #2b2b2b, 三个按钮 #1F6AA5).
        self.kit.apply(self.p)

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        #: 销毁牌子: destroy() 立起来, 延后任务(轮询/名称重裁)看到它就什么都不做 ——
        #: 已经到点、被 Tk 取走的那个任务撤不掉, 只能靠它兜住(见 destroy 的说明)。
        self._destroyed = False
        #: 后台资源(调度器/快捷键监听)是否已释放: :meth:`_release_background` 幂等靠它。
        self._background_released = False
        self._poll_job = self.after(100, self._poll_messages)
        if smoke_seconds is None:
            # 启动时补一次译名探测: 后台线程、只对当前语言缺条目的游戏联网(有缓存就
            # 直接复用), 探到新名字会抬高数据版本, 界面自动重绘。--smoke 不触发,
            # 冻结包的冒烟自检保持不发网络请求。
            self.backend.prefetch_names()
        if smoke_seconds is not None:
            # 冒烟自检也走正常关闭路径, 确保调度器/快捷键被释放.
            self.after(int(smoke_seconds * 1000), self._on_close)

    def _apply_window_geometry(self, saved: WindowSettings) -> None:
        """按记住的尺寸与位置打开窗口(没记住就用设计尺寸, 见 :func:`open_window_geometry`)."""
        screen = (int(self.winfo_screenwidth()), int(self.winfo_screenheight()))
        self.geometry(
            open_window_geometry(saved, screen=screen, scale=window_scaling(self))
        )

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

        内容有问题时 ``load_or_repair_config`` 已经处理过: 非法值与不认识的键被剔除并
        还原为默认值(合法的部分保留), 整份读不出来时才会全体还原。这里把这件事反馈
        到界面与审计日志, 避免用户以为自己改的语言/快捷键"没生效"。
        """
        if self._paths is None:
            return ConfigLoad(config=AppConfig())
        loaded = load_or_repair_config(self._paths.config_path)
        if loaded.reset:
            log_action("config.reset", basic=True, backup=str(loaded.backup or ""))
            self._last_feedback = (FeedbackKind.INFO, tr("config.reset"))
        elif loaded.repaired:
            fields = ", ".join(loaded.repaired)
            log_action(
                "config.repaired",
                basic=True,
                fields=fields,
                backup=str(loaded.backup or ""),
            )
            self._last_feedback = (
                FeedbackKind.INFO,
                tr("config.repaired", count=len(loaded.repaired), fields=fields),
            )
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
        config = load_or_repair_config(self._paths.config_path).config
        config.language = locale
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存语言失败: %s", exc)

    def _save_font_size(self, size: int) -> None:
        """把界面字号写回配置文件(没有配置路径时只保存在内存里)."""
        if self._paths is None:
            return
        config = load_or_repair_config(self._paths.config_path).config
        config.ui.base_font_px = size
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存界面字号失败: %s", exc)

    def _on_font_size_change(self, size: int) -> str | None:
        """调整界面字号: 立即生效 + 写回配置; 返回 None 表示成功.

        字号在构造界面时就已用上(见 :mod:`archive_management.ui.typography`),
        因此和切语言一样只能重建整个窗口。
        """
        self._base_font_px = set_base_font_px(size).base_px
        self._save_font_size(self._base_font_px)
        log_action("ui.switch_font_size", basic=True, size=self._base_font_px)
        self._rebuild_ui()
        self._feedback(
            FeedbackKind.INFO,
            tr("settings.font_switched", size=self._base_font_px),
        )
        return None

    def _on_language_change(self, locale: str) -> str | None:
        """切换界面语言: 按新语言重探译名 + 整体重建界面; 返回 None 表示成功.

        译名按 ``<AppID>:<语言>`` 分条缓存, 因此这里**不忽略缓存**: 新语言还没记录
        的游戏才联网, 取过的直接用缓存。探测在后台线程里跑, 完成后抬高的数据版本
        会触发重绘, 所以不必等网络。
        """
        try:
            set_locale(locale)
        except ValueError as exc:
            return str(exc)
        self._language = locale
        self._save_language(locale)
        log_action("ui.switch_language", basic=True, language=locale)
        self.backend.prefetch_names()
        self._rebuild_ui()
        self._feedback(
            FeedbackKind.INFO,
            tr("settings.language_switched", language=tr(f"locale.{locale}")),
        )
        return None

    def _save_remember_window(self, enabled: bool) -> None:
        """把"记住窗口大小与位置"写回配置; 关掉时顺手清掉已记下的几何.

        清掉是用户 2026-10-02 明确要求的一半: "关闭该功能时如果配置中有记录了位置大小
        信息要一并删除"。留着它既与开关的说法矛盾, 也会在重新打开开关时突然生效一个
        很久以前的位置。
        """
        if self._paths is None:
            return
        config = load_or_repair_config(self._paths.config_path).config
        config.ui.remember_window = enabled
        if not enabled:
            config.window = WindowSettings()
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存窗口几何开关失败: %s", exc)

    def _on_remember_window_change(self, enabled: bool) -> str | None:
        """开关"记住窗口大小与位置": 立即生效 + 写回配置; 返回 None 表示成功."""
        self._remember_window = enabled
        self._save_remember_window(enabled)
        log_action("ui.switch_remember_window", enabled=enabled)
        self._feedback(
            FeedbackKind.INFO,
            tr(
                "settings.remember_switched_on"
                if enabled
                else "settings.remember_switched_off"
            ),
        )
        return None

    def _save_verification(self, mode: VerificationMode) -> None:
        """把备份校验方式写回配置文件(没有配置路径时只保存在内存里)."""
        if self._paths is None:
            return
        # 内容有问题时 load_or_repair_config 已经剔除过非法部分, 这里总能拿到可用配置。
        config = load_or_repair_config(self._paths.config_path).config
        config.verification.mode = mode
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存校验方式失败: %s", exc)

    def _on_verification_change(self, mode: str) -> str | None:
        """切换备份校验方式: 写回配置; 返回 None 表示成功.

        只改配置, 不通知后端: 备份与还原都在**每次操作前**现读一次配置
        (见 ui.sql_backend.policy_from_config), 因此下一次备份就会按新方式走, 而正在
        跑的那一次仍用开始时的取值 —— 中途换掉的校验方式不该影响半途的操作。
        """
        self._verification_mode = normalize_verification_mode(mode)
        self._save_verification(self._verification_mode)
        log_action("ui.switch_verification", mode=mode)
        self._feedback(
            FeedbackKind.INFO,
            tr(
                "settings.verification_switched",
                mode=tr(f"settings.verification_{mode}"),
            ),
        )
        return None

    def _save_debug(self, enabled: bool) -> None:
        """把调试开关写回配置文件(没有配置路径时只保存在内存里)."""
        if self._paths is None:
            return
        # 内容有问题时 load_or_repair_config 已经剔除过非法部分, 这里总能拿到可用配置。
        config = load_or_repair_config(self._paths.config_path).config
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

    def _save_activation(self, enabled: bool) -> None:
        """把自动启停开关写回配置文件(没有配置路径时只保存在内存里)."""
        if self._paths is None:
            return
        config = load_or_repair_config(self._paths.config_path).config
        config.activation.auto = enabled
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存自动启停开关失败: %s", exc)

    def _on_activation_change(self, enabled: bool) -> str | None:
        """开关自动启停: 立即生效 + 写回配置; 返回 None 表示成功.

        开启时把下一次轮询提前到当下, 否则用户要等一整个轮询间隔才看得到效果
        (而"刚打开开关却没反应"看起来就像坏了)。开关变化也算人工动作, 因此把
        自适应间隔退回最快档。
        """
        self._activation = enabled
        self._reset_activation_ladder()
        self._save_activation(enabled)
        log_action("ui.switch_activation", enabled=enabled)
        self._home_page.refresh_activation(enabled=enabled)
        self._feedback(
            FeedbackKind.INFO,
            tr(
                "settings.activation_switched_on"
                if enabled
                else "settings.activation_switched_off"
            ),
        )
        return None

    def _reset_activation_ladder(self) -> None:
        """人工动作后把轮询退回最快档并立即探测一次."""
        self._activation_steps = 0
        self._activation_due = 0.0

    def _poll_activation_now(self) -> None:
        """启停分页的"刷新"与"进入分页": 立即再探一次(人工动作)."""
        if not self._activation:
            self._home_page.refresh_activation(enabled=False)
            return
        self._reset_activation_ladder()

    def _on_set_monitor(self, game_id: str) -> None:
        """把分页里选中的那一款设为监控对象(等同于手动启用它)."""
        try:
            summary = self.backend.set_game_enabled(game_id, True)
        except ArchiveManagementError as exc:
            self._notice(tr("activation.monitor_failed", reason=str(exc)))
            return
        log_action("ui.activation_monitor", game_id=game_id)
        self._reset_activation_ladder()
        self._notice(tr("activation.monitor_set", name=summary.name))
        self._refresh_after_manage()
        self._home_page.refresh_activation(enabled=self._activation)

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
        # 内容有问题时 load_or_repair_config 已经剔除过非法部分, 这里总能拿到可用配置。
        config = load_or_repair_config(self._paths.config_path).config
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
            on_activation_monitor=self._on_set_monitor,
            on_activation_refresh=self._poll_activation_now,
            on_export_batch=self._on_export_batch,
        )
        self._home_page.frame.grid(row=0, column=0, sticky="nsew")
        self._home_page.frame.grid_remove()
        # 可用性会变的按钮: 状态一改就要按新 state 重画(禁用必须看起来禁用).
        # 不在这个表里的按钮(各 tab、搜索、上一页/下一页…)的可用性从不变化。
        self._stateful_buttons = (
            self._add_game_btn,
            self._import_btn,
            self._backup_btn,
            self._export_btn,
            self._settings_game_btn,
            self._cancel_btn,
            self._restore_btn,
            self._branch_btn,
            self._rename_btn,
            self._delete_btn,
        )
        # 分页在第一次轮询之前就要显示开关状态(否则会先把"关闭"写出来再改口).
        self._home_page.refresh_activation(enabled=self._activation)

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
        logo.grid(row=0, column=0, padx=(16, 12), pady=10)
        self.kit.register(lambda p: logo.configure(fg_color=p.accent))
        brand = self.kit.label(
            parent, tr("topbar.brand"), style="primary", size=16, weight="bold"
        )
        brand.grid(row=0, column=1, padx=(0, 16), pady=10)

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
        actions.grid(row=0, column=3, sticky="e", padx=(0, 16))
        self._add_game_btn = self.kit.button(
            actions,
            tr("topbar.add_game"),
            kind="primary",
            command=self._on_add_game,
            width=104,
            height=34,
        )
        self._add_game_btn.pack(side="left", padx=(0, 8))
        self._import_btn = self.kit.button(
            actions,
            tr("topbar.import_package"),
            style="ghost",
            command=self._on_import_package,
            width=124,
            height=34,
        )
        self._import_btn.pack(side="left", padx=(0, 8))
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
            kind="primary",
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
        self._feedback_label.pack(side="left", padx=16, pady=4)

        self._service_card = ctk.CTkFrame(statusbar, fg_color="transparent")
        self._service_card.pack(side="right", padx=16, pady=4)
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
        left.grid(row=0, column=0, sticky="nsew", padx=(24, 8), pady=16)

        self._hero_tile = ctk.CTkLabel(
            left,
            text="",
            width=84,
            height=84,
            corner_radius=RADIUS_LG,
            fg_color=self._tone_color(None),
            text_color=_HERO_TILE_TEXT,
            font=ctk.CTkFont(size=28, weight="bold"),
        )
        self._hero_tile.pack(side="left")
        # 图标位: 有缓存图标就换成图片, 没有才回落名称首字(CTkImage 要持引用).
        self._hero_icon: ctk.CTkImage | None = None

        info = ctk.CTkFrame(left, fg_color="transparent")
        # expand=True: 信息块占满色块右边的全部宽度, 位置/来源那几行因此能"撑满一行".
        info.pack(side="left", fill="both", expand=True, padx=(16, 0))
        # 名称**只印在左上角那一处**(头部标题): 概要卡里不再重复一遍(用户 2026-10-03)。
        # 这张卡留下的是"当前游戏在哪、验没验过、备份统计", 卡上那句 "当前游戏" 就是标题。
        self.kit.label(info, tr("hero.current_game"), style="muted", size=11).pack(
            anchor="w", pady=(2, 0)
        )
        self._hero_location_label = self.kit.label(info, "", style="body", size=13)
        self._hero_location_label.pack(anchor="w", pady=(2, 0))
        self._hero_verified_label = ctk.CTkLabel(
            info, text="", font=ctk.CTkFont(size=12, weight="bold")
        )
        self._hero_verified_label.pack(anchor="w", pady=(6, 0))
        # 额外信息: 原始名称(改过名时)与备份目录. 目录名是 slug + 短哈希这种内部
        # 存储键, 放在正文里用户既改不了也用不上 —— 正文只说明"由应用自动命名",
        # 真实目录名挂在悬停提示上(技术信息不丢, 也不占正文)。
        self._hero_origin_label = self.kit.label(info, "", style="muted", size=11)
        self._origin_tip = ""
        attach_tooltip(self._hero_origin_label, lambda: self._origin_tip)
        self.kit.register(
            lambda p: self._hero_verified_label.configure(
                text_color=p.success if self._verified else p.danger
            )
        )

        stats = ctk.CTkFrame(self._hero, fg_color="transparent")
        stats.grid(row=0, column=1, sticky="e", padx=(8, 24), pady=16)

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
        self._next_chip = chip
        # 没有排期时不能穿"正向"的强调色: 一眼看去会像"已安排好了".
        self._next_scheduled = False
        self.kit.register(lambda p: self._paint_next_chip(p))
        self._stat_next_caption = ctk.CTkLabel(
            chip, text=tr("hero.next_auto"), font=ctk.CTkFont(size=11), anchor="w"
        )
        self._stat_next_caption.pack(anchor="w", padx=12, pady=(10, 0))
        self._stat_next_value = ctk.CTkLabel(
            chip, text="", font=ctk.CTkFont(size=16, weight="bold"), anchor="w"
        )
        self._stat_next_value.pack(anchor="w", padx=12, pady=(2, 10))

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
            kind="primary",
            command=self._on_backup,
            width=150,
            height=36,
        )
        self._backup_btn.grid(row=0, column=6, padx=16, pady=10, sticky="e")

    def _new_tab(
        self, parent: ctk.CTkFrame, view: ViewKind, text: str
    ) -> ctk.CTkButton:
        """创建一个视图切换按钮."""
        return ctk.CTkButton(
            parent,
            text=text,
            width=120,
            height=36,
            corner_radius=RADIUS_MD,
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
            corner_radius=RADIUS_MD,
            command=self._on_filter_change,
            # 构造时就给输入边界: 下面的 _paint_combo 也会写一次(主题重绘), 但控件
            # **建成那一瞬**就得是它 —— 否则注册表生效前会闪一下无描边的样子。
            border_color=self.p.input_border,
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
            border_color=palette.input_border,
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
        self._list_title.grid(row=0, column=0, padx=16, pady=(16, 0), sticky="w")
        self._list_sub = self.kit.label(self._list_panel, "", style="muted", size=12)
        self._list_sub.grid(row=1, column=0, padx=16, pady=(2, 8), sticky="w")
        self._list_scroll = self.kit.scroll_frame(self._list_panel, bg_key="well")
        self._list_scroll.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        # 分支视图不用滚动容器而是图画布(见 I-9): 两块同格叠放, 同一时间只显示一个。
        # 图画布自己拿 ``well`` 当底色(与滚动区一致), 所以切视图时那块区域的底色不变。
        self._branch_host = self.kit.frame(
            self._list_panel, bg_key="well", corner_radius=RADIUS_NONE
        )
        self._branch_host.grid(row=2, column=0, sticky="nsew", padx=8, pady=(0, 8))
        self._branch_host.grid_remove()
        self._tree_view = TreeView(
            self._branch_host,
            on_select=self._select_backup,
            on_hover=self._on_graph_hover,
        )
        self._tree_view.frame.pack(fill="both", expand=True)
        self.kit.register(self._tree_view.redraw)

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
        # 面板宽度是这三行文字的"可用宽度"来源(见 _fit_selected_text)。
        self._selected_panel = panel

        self.kit.label(panel, tr("sel.title"), style="h2", size=15, weight="bold").grid(
            row=0, column=0, padx=16, pady=(16, 4), sticky="w"
        )
        self._selected_name = self.kit.label(
            panel, "", style="body", size=14, weight="bold"
        )
        self._selected_name.grid(row=1, column=0, padx=16, pady=(0, 2), sticky="w")
        self._selected_meta = self.kit.label(panel, "", style="muted", size=12)
        self._selected_meta.grid(row=2, column=0, padx=16, pady=(0, 4), sticky="w")

        line = ctk.CTkFrame(panel, height=1, fg_color="transparent")
        line.grid(row=3, column=0, sticky="ew", padx=16, pady=(4, 6))
        self.kit.register(lambda p: line.configure(fg_color=p.border))

        self.kit.label(panel, tr("sel.summary"), style="muted", size=11).grid(
            row=4, column=0, padx=16, pady=(0, 2), sticky="w"
        )
        self._selected_files = self.kit.label(panel, "", style="body", size=12)
        self._selected_files.grid(row=5, column=0, padx=16, pady=(0, 2), sticky="w")
        self._selected_state = self.kit.label(panel, "", style="muted", size=11)
        self._selected_state.grid(row=6, column=0, padx=16, pady=(0, 4), sticky="w")

        actions = ctk.CTkFrame(panel, fg_color="transparent")
        actions.grid(row=7, column=0, padx=16, pady=(6, 12), sticky="w")
        self._restore_btn = self.kit.button(
            actions,
            tr("action.restore"),
            kind="primary",
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
        actions_second.grid(row=8, column=0, padx=16, pady=(0, 12), sticky="w")
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
            kind="destructive",
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
        ).grid(row=0, column=0, padx=16, pady=(16, 8), sticky="w")
        # 任务名自己占满一整行(右边只留状态): 早先它和"备份目标"那条长路径挤同两列,
        # 亏空从名称列扣, "每 5m 备份 · 保留 3 份"被静默截成"每 5m 备份 · 保"。
        self._task_name_label = self.kit.label(
            panel, "", style="body", size=13, weight="bold"
        )
        self._task_name_label.configure(wraplength=_TASK_NAME_WIDTH, justify="left")
        self._task_name_label.grid(row=1, column=0, padx=16, sticky="ew")
        self._task_state_label = self.kit.label(panel, "", style="muted", size=12)
        self._task_state_label.grid(row=1, column=1, padx=(8, 16), sticky="e")

        self._task_progress = ctk.CTkProgressBar(
            panel, height=8, corner_radius=RADIUS_PILL
        )
        self._task_progress.grid(
            row=2, column=0, columnspan=2, padx=16, pady=(8, 4), sticky="ew"
        )
        self.kit.register(
            lambda p: self._task_progress.configure(
                fg_color=p.input_bg, progress_color=p.accent
            )
        )

        self._task_progress_label = self.kit.label(panel, "", style="hint", size=12)
        self._task_progress_label.grid(
            row=3, column=0, padx=16, pady=(0, 8), sticky="w"
        )
        self._cancel_btn = self.kit.button(
            panel,
            tr("task.cancel"),
            style="ghost",
            command=self._on_cancel,
            width=86,
            height=26,
        )
        self._cancel_btn.grid(row=3, column=1, padx=(8, 16), pady=(0, 8), sticky="e")
        self._cancel_btn.configure(state="disabled")

        # 说明与它的值各占一行(值拿满宽度): 长路径不再需要和别的列抢位置。
        self.kit.label(panel, tr("task.next"), style="muted", size=12).grid(
            row=4, column=0, columnspan=2, padx=16, sticky="w"
        )
        self._task_next = self.kit.label(panel, "", style="body", size=12)
        self._task_next.configure(wraplength=_TASK_VALUE_WIDTH, justify="left")
        self._task_next.grid(
            row=5, column=0, columnspan=2, padx=16, pady=(2, 0), sticky="ew"
        )

        self.kit.label(panel, tr("task.target"), style="muted", size=12).grid(
            row=6, column=0, columnspan=2, padx=16, pady=(8, 0), sticky="w"
        )
        # 备份目标是完整路径, 必须换行显示, 否则会被卡片裁掉.
        self._task_target = self.kit.label(panel, "", style="hint", size=12)
        self._task_target.configure(wraplength=_TASK_VALUE_WIDTH, justify="left")
        self._task_target.grid(
            row=7, column=0, columnspan=2, padx=16, pady=(2, 0), sticky="ew"
        )

        # 快捷键不在任务状态卡里展示: 它属于全局设置, 统一在"设置"窗口里查看与修改。
        self._task_hint = self.kit.label(panel, "", style="hint", size=12)
        self._task_hint.configure(wraplength=250, justify="left")
        self._task_hint.grid(
            row=8, column=0, columnspan=2, padx=16, pady=(12, 12), sticky="w"
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
        self._hero_location_label.configure(text="")
        self._hero_verified_label.configure(text="")
        self._hero_origin_label.pack_forget()
        self._stat_recent_value.configure(text="—")
        self._stat_recent_sub.configure(text="")
        self._stat_total_value.configure(text=tr("detail.backups_none"))
        self._stat_total_sub.configure(text="")
        self._next_scheduled = False
        self._stat_next_value.configure(text=tr("hero.next_none"))
        self._paint_next_chip(self.p)
        self._render_selected(None)
        self._render_task(self.backend.task_status(None))
        self._update_actions()
        self._show_area(graph=False)
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
        """展示"原始名称 / 备份目录"补充信息(无内容时不占位).

        备份目录那一句只说"由应用自动命名", 真实目录名(内部存储键)进悬停提示。
        """
        text = detail.origin_label
        self._origin_tip = detail.storage_hint
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
        self._next_scheduled = bool(detail.next_backup_label)
        self._stat_next_value.configure(text=detail.next_backup_text)
        self._paint_next_chip(self.p)

    def _paint_next_chip(self, palette: Palette) -> None:
        """下次自动备份那个胶囊的配色跟着有没有排期走.

        有排期 = 强调色的正向提示; 没有排期 = 中性底色 + 中性字(未配置), 不再是
        一个穿着正向色、里面只有一条短横线的面板。
        """
        if self._next_scheduled:
            self._next_chip.configure(
                fg_color=palette.accent_soft,
                border_color=palette.accent_soft_border,
                border_width=1,
            )
            self._stat_next_caption.configure(text_color=palette.success)
            self._stat_next_value.configure(text_color=palette.accent_soft_text)
            return
        self._next_chip.configure(
            fg_color=palette.panel,
            border_color=palette.border,
            border_width=1,
        )
        self._stat_next_caption.configure(text_color=palette.text_muted)
        self._stat_next_value.configure(text_color=palette.text_muted)

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
        return host_image(self, light_image=loaded, size=(84, 84))

    def _render_toolbar_header(self, detail: GameDetail) -> None:
        """写头部标题/副标题(按控件实际宽度裁剪, 见 _refit_detail_names)."""
        self._detail_name = detail.name
        self._detail_subtitle = detail.subtitle
        self._set_detail_names()

    def _name_budget(self, label: ctk.CTkLabel, fallback: int) -> int:
        """控件当前的可用宽度(还没测量出来时用设计预算兜底).

        ``fallback`` 是**设计**值(逻辑像素), 而这里返回的宽度要拿给 ``fit_label`` 当
        **物理**预算用: 两者差一个窗口缩放(见 ``widgets.scaled_px``)。
        """
        width = label.winfo_width()
        return width if width > 1 else scaled_px(label, fallback)

    def _set_selected_texts(self, name: str, meta: str, files: str) -> None:
        """写下右侧面板三行的**完整**文本, 再按当前宽度裁一次."""
        self._selected_texts = (name, meta, files)
        self._fit_selected_text()

    def _fit_selected_text(self) -> None:
        """把右侧面板的三行文字裁进面板宽度里(与详情页头部同一条纪律).

        面板宽度固定(所以这里不像头部那样必须等 ``<Configure>``), 但**超长备份名只有
        这一处能读全** —— 从前它直接被 Tk 硬裁且不补省略号, 用户看到的是半句话;
        现在裁掉的部分补省略号, 完整内容挂在悬停提示上(``fit_label`` 一并做了)。
        """
        name, meta, files = self._selected_texts
        rows = (
            (self._selected_name, name, 1),
            (self._selected_meta, meta, 1),
            (self._selected_files, files, _SELECTED_FILES_LINES),
        )
        for label, text, lines in rows:
            # 预算取**标签自己**的宽度(与详情页头部 :meth:`_set_detail_names` 同一条纪律):
            # 拿"面板宽度 - 一个写死的内衬"去裁总是差那么几像素 —— 2026-10-03 实测右侧
            # 面板差 **4px**(标题与说明都被 Tk 硬裁掉一点, 且不补省略号): 面板内宽比卡片里
            # 那个标签还宽一点。标签的宽度就是它真正能写的宽度。
            # 宽度还没量出来时(首帧)才退回设计值。
            budget = self._name_budget(
                label, _SELECTED_PANEL_WIDTH - _SELECTED_TEXT_INSET
            )
            # ``budget`` 是物理像素(fit_label / font.measure 用的就是它), 而 wraplength
            # 是逻辑像素: 过一层 wrap_budget, 否则 125% 的屏上标签按 1.25 倍的宽度折行.
            label.configure(wraplength=wrap_budget(label, budget))
            fit_label(label, text, label.cget("font"), budget, max_lines=lines)

    def _set_detail_names(self) -> None:
        """按各控件**当前宽度**裁剪名称: 窗口变宽就多显示几个字.

        超出部分补省略号并限制行数 —— 否则超长名称会把下面的内容整排推下去。
        """
        if not self._detail_name:
            return
        title_budget = self._name_budget(self._title_label, _HEADER_TEXT_WIDTH)
        self._title_label.configure(
            wraplength=wrap_budget(self._title_label, title_budget)
        )
        fit_label(
            self._title_label,
            self._detail_name,
            self._title_font,
            title_budget,
            max_lines=_HEADER_NAME_LINES,
        )
        # 副标题里带存档位置(可能是很长的路径): 同样封顶两行, 头部高度有上限.
        subtitle_budget = self._name_budget(self._subtitle_label, _HEADER_TEXT_WIDTH)
        self._subtitle_label.configure(
            wraplength=wrap_budget(self._subtitle_label, subtitle_budget)
        )
        fit_label(
            self._subtitle_label,
            self._detail_subtitle,
            self._subtitle_font,
            subtitle_budget,
            max_lines=_HEADER_NAME_LINES,
        )

    def _refit_detail_names(self) -> None:
        """窗口宽度变化后按新宽度重裁详情页里的名称(延后合并成一次)."""
        self._refit_job = None
        if self.__dict__.get("_destroyed", False):
            return  # 与 _poll_messages 同一条理由: 到点的任务可能在被销毁后才轮到
        if self._page is AppPage.DETAIL:
            self._set_detail_names()
            self._fit_selected_text()

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
        self._fit_task_name(task.task_name)
        self._show_task_progress(task.running)
        self._task_state_label.configure(text=state)
        self._task_progress.set(task.progress)
        self._task_progress_label.configure(
            text=task.progress_label if task.running else ""
        )
        self._task_next.configure(text=task.next_run_text)
        self._task_target.configure(text=task.target_label)
        self._task_hint.configure(text=tr("task.hint"))
        self._cancel_btn.configure(state="normal" if task.cancellable else "disabled")
        self.kit.repaint_button(self._cancel_btn, self.p)
        # 右下角的服务状态: 服务正常 + 备份占用(原本显示在侧边栏的状态卡里).
        self._service_label.configure(
            text=f"{tr('status.service_ok')} · {self._usage_text}"
        )

    def _fit_task_name(self, text: str) -> None:
        """按标签的**当前宽度**裁剪任务名.

        宽度不够时先换行, 两行还放不下才补省略号 —— 绝不允许出现"每 5m 备份 · 保"
        这种半句话(评审时定的)。控件还没量出宽度时用设计预算兜底。
        """
        width = int(self._task_name_label.winfo_width())
        budget = width if width > 1 else _TASK_NAME_WIDTH
        self._task_name_label.configure(wraplength=budget)
        fit_label(
            self._task_name_label,
            text,
            self._task_name_label.cget("font"),
            budget,
            max_lines=_TASK_NAME_LINES,
        )

    def _show_task_progress(self, running: bool) -> None:
        """进度条与取消入口只在真的有操作时占面积.

        空闲时那条约 8px 的进度槽在深色底上就是一个没有说明的小绿点(评审时定的),
        所以整行收起, 而不是留在那里"占位"。
        """
        widgets = (
            self._task_progress,
            self._task_progress_label,
            self._cancel_btn,
        )
        if running:
            for widget in widgets:
                widget.grid()
            return
        for widget in widgets:
            widget.grid_remove()

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
        include_safety = self._filter_source.get() == SourceFilter.SAFETY.label
        ordered, title, sub = self._ordered_for_view(
            selected,
            include_safety=include_safety,
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
            # 空状态回到滚动容器里显示; 同时把图清空, 否则隐藏的画布会留着上一批框
            # ("切视图不残留"这条判据量的就是它)。
            self._tree_view.set_items([])
            self._show_area(graph=False)
            empty = self.kit.label(
                self._list_scroll,
                tr("list.empty_filtered"),
                style="muted",
                size=13,
            )
            empty.pack(padx=10, pady=16)
            return
        if self._view == ViewKind.BRANCH:
            # 分支视图: 喂**剪枝后**的树, 由画布铺图(见 ui/tree_view.py).
            self._tree_view.set_items(items, include_safety=include_safety)
            self._tree_view.redraw(self.p)
            self._tree_view.select(self._backup_id, self.p)
            self._show_area(graph=True)
            return
        self._show_area(graph=False)
        self._build_cards(items)

    def _show_area(self, *, graph: bool) -> None:
        """切左侧主体显示的是图画布还是滚动容器(空状态与时间线用后者)."""
        if graph:
            self._list_scroll.grid_remove()
            self._branch_host.grid()
            return
        self._branch_host.grid_remove()
        self._list_scroll.grid()

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
        """卡片标题: 当前节点标记 + 名称.

        层级前缀(``└`` 重复)已经退场 —— 分支视图改用图画布表达层级, 而时间线里
        ``depth`` 恒为 0, 那个前缀本来就一直是空的。
        """
        marker = f"{_CURRENT_MARK} " if item.is_current else ""
        return f"{marker}{item.display_title}"

    def _card_detail(self, item: BackupItem) -> str:
        """卡片副标题.

        两种视图共用同一份结构(``backup_card_detail``): 时间线早先多一段"主线 · ",
        分支树只有容量, 同一张卡片切视图就"变了形状"。
        """
        return backup_card_detail(item)

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
            corner_radius=RADIUS_LG,
            font=ctk.CTkFont(size=11),
        )
        badge.grid(row=0, column=2, padx=(0, 12), pady=(10, 2), sticky="e")
        title = ctk.CTkLabel(
            card,
            text=self._card_title(item),
            anchor="w",
            font=ctk.CTkFont(size=FONT_STRONG, weight="bold"),
            text_color=self.p.text_primary,
        )
        title.grid(
            row=1, column=0, columnspan=3, padx=(indent, 12), pady=(2, 0), sticky="w"
        )
        # 标题是单行的: 卡片宽度不够时补省略号, 而不是让文字被卡片硬切(也免得它把
        # 卡片的请求宽度撞大、把列表撑出横向滚动); 完整的标题在悬停提示里看.
        full_title = self._card_title(item)
        track_fit(card, title, (full_title,), inset=indent + 12)
        attach_tooltip(title, full_title)
        detail_text = self._card_detail(item)
        detail = ctk.CTkLabel(
            card,
            text=detail_text,
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=self.p.text_muted,
        )
        detail.grid(row=2, column=0, columnspan=3, padx=12, pady=(0, 10), sticky="w")
        # 副标题自己断行到两行 + 省略号, 并挂完整文本的悬停提示: 写死的 wraplength 在窄
        # 窗口里会把行顶出卡片, 而一整段没空格的中日韩文字 Tk 根本断不开(实测 1038px/815px)。
        track_fit(card, detail, (detail_text,), inset=_CARD_DETAIL_INSET, max_lines=2)
        attach_tooltip(detail, detail_text)

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
        background, border = card_surface_colors(
            palette, selected=selected, hovered=hovered
        )
        card.configure(fg_color=background, border_width=1, border_color=border)
        if selected:
            title.configure(text_color=palette.accent_soft_text)
        else:
            title.configure(text_color=palette.text_primary)
        when.configure(text_color=palette.text_body)
        detail.configure(text_color=palette.text_muted)
        if item.safety:
            # 安全点与"手动/自动"的语义不同(一个是种类、两个是来源): 三者必须
            # 是三种可区分的颜色, 否则同一张卡片上两个胶囊看着一样。
            badge.configure(
                fg_color=palette.badge_safety_bg, text_color=palette.badge_safety_text
            )
        elif item.auto:
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

        分支视图里对应的对象是"图上那两个框": 画布自己算命中, 这里只负责把
        id 交给它(同一套优先级: 选中 > 悬停 > 常规)。
        """
        if self._hover_id == backup_id:
            return
        previous, self._hover_id = self._hover_id, backup_id
        if self._view == ViewKind.BRANCH:
            self._tree_view.set_hovered(backup_id, self.p)
            return
        self._paint_card_by_id(previous)
        self._paint_card_by_id(backup_id)

    def _on_graph_hover(self, node_id: str | None) -> None:
        """图上的指针换了框: 走与卡片同一条悬停路径(状态只有一份)."""
        self._set_hover(node_id)

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
        if self._view == ViewKind.BRANCH:
            self._tree_view.select(item.backup_id, self.p)
            return
        self._paint_card_by_id(previous)
        self._paint_card_by_id(item.backup_id)

    def _render_selected(self, item: BackupItem | None) -> None:
        if item is None:
            self._set_selected_texts(tr("sel.none"), tr("sel.hint"), "")
            self._selected_state.configure(text="")
            self._restore_btn.configure(state="disabled")
            self._branch_btn.configure(state="disabled")
            self._rename_btn.configure(state="disabled")
            self._delete_btn.configure(state="disabled")
            self._paint_button_states()
            return
        self._set_selected_texts(
            item.display_title,
            f"{item.created_label}  ·  {item.branch_label}",
            item.sub or tr("sel.digest", size=item.size_label),
        )
        self._selected_state.configure(
            text=(
                tr("sel.current")
                if item.is_current
                else tr(
                    "sel.not_current",
                    size=item.size_label,
                    button=tr("action.restore"),
                )
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
        self._apply_entry_states(busy)
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
        self._paint_button_states()

    def _apply_entry_states(self, busy: bool) -> None:
        """忙碌期间收起这两个"点了没反应"的入口.

        它们的处理器第一句就是"正在忙就直接返回", 所以忙的时候必须置灰: 无效控件
        要看起来无效, 而不是等用户点了再什么都不发生。
        """
        state = "disabled" if busy else "normal"
        self._add_game_btn.configure(state=state)
        self._import_btn.configure(state=state)

    def _paint_button_states(self) -> None:
        """按每个按钮**当前的 state**重画可用性会变的那一批.

        与 state 分开做一次是因为两者必须同时成立: 只改 state 的话, 主色/危险色的
        按钮被禁用后仍是亮的(评审时定的)。
        """
        for button in self._stateful_buttons:
            self.kit.repaint_button(button, self.p)

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
        # 进行中要说清"正在把哪一份恢复到哪里": 只有"正在恢复备份…"的话, 用户看不出
        # 这一条属于哪一款游戏(I-7: 进行中的状态必须可辨识).
        self._feedback(
            FeedbackKind.PENDING,
            tr("action.restore_pending", name=game.name, title=item.title),
        )

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
                danger=True,
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
        # 进行中说清\"删的是哪一份\": 分支根节点连带的删除量由结果文案给出(I-7).
        self._feedback(
            FeedbackKind.PENDING,
            tr("action.delete_pending", title=item.display_title),
        )
        self._submit(work, ok)

    def _selected_item(self) -> BackupItem | None:
        """返回当前选中的备份项."""
        if self._backup_id is None:
            return None
        return next(
            (item for item in self._items if item.backup_id == self._backup_id), None
        )

    def _on_export(self) -> None:
        """导出选中游戏: 先让用户选好保存位置, 再在后台线程打包.

        选择文件这一步必须在主线程完成(会阻塞事件循环), 因此后台线程只负责真正
        的打包; 用户取消时什么都不写, 只给一条提示并记审计。
        """
        game = self._game
        if game is None or self._busy:
            return
        destination = pick_save_file(
            title=tr("dialog.export_title"),
            initialfile=f"{game_slug(game.name)}{EXPORT_FILE_SUFFIX}",
        )
        if not destination:
            log_action(
                "export.start",
                basic=True,
                game_id=game.game_id,
                result="cancelled",
            )
            self._feedback(FeedbackKind.INFO, tr("action.export_canceled"))
            return
        self._set_busy(True)
        self._feedback(
            FeedbackKind.PENDING, tr("action.export_pending", name=game.name)
        )

        def work() -> str:
            return self.backend.run_export(game.game_id, destination)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)

        self._submit(work, ok)

    def _on_export_batch(self) -> None:
        """批量导出: 多选对话框 → 保存位置 → 后台打包.

        前两步都在主线程(都会阻塞事件循环), 后台线程只负责真正的打包。任何一步取消都
        不写文件、不进忙碌态; 一份都没勾选时也不导出。候选里只有"未停用且未归档"的
        游戏(规则在 :func:`exportable_games` 里, 库里一款都没有时连对话框都不弹)。
        """
        if self._busy:
            return
        candidates = exportable_games(self.backend.list_games())
        if not candidates:
            log_action("export.batch_start", basic=True, result="empty")
            self._feedback(FeedbackKind.INFO, tr("action.export_batch_empty"))
            return
        choice = export_batch_dialog(
            self,
            self.p,
            title=tr("dialog.export_batch_title"),
            prompt=export_batch_prompt(candidates),
            filter_label=tr("dialog.export_batch_filter"),
            list_label=tr("dialog.export_batch_list"),
            no_match_text=tr("dialog.export_batch_no_match"),
            select_all_label=tr("dialog.export_batch_select_all"),
            select_all_scope=tr("dialog.export_batch_select_all_scope"),
            confirm_text=tr("dialog.export_batch_confirm"),
        )
        if choice is None:
            self._cancel_batch_export()
            return
        if not choice.game_ids:
            log_action("export.batch_start", basic=True, result="none_selected")
            self._feedback(FeedbackKind.INFO, tr("action.export_batch_none"))
            return
        destination = pick_save_file(
            title=tr("dialog.export_batch_save_title"),
            initialfile=batch_export_filename(
                len(choice.game_ids), moment=datetime.now(UTC)
            ),
        )
        if not destination:
            self._cancel_batch_export()
            return
        self._start_batch_export(choice.game_ids, destination)

    def _cancel_batch_export(self) -> None:
        """用户在对话框或保存框里取消: 记审计 + 提示, 不写文件也不进忙碌态."""
        log_action("export.batch_start", basic=True, result="cancelled")
        self._feedback(FeedbackKind.INFO, tr("action.export_batch_canceled"))

    def _start_batch_export(self, game_ids: Sequence[str], destination: str) -> None:
        """在后台线程里把这几款游戏打成一个批量包, 完成后给出带真实计数的提示."""
        self._set_busy(True)
        self._feedback(
            FeedbackKind.PENDING,
            tr("action.export_batch_pending", count=len(game_ids)),
        )

        def work() -> str:
            return self.backend.run_export_batch(list(game_ids), destination)

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)

        self._submit(work, ok)

    def _on_import_package(self) -> None:
        """导入归档包: 先选文件(主线程), 再后台体检, 最后在主线程让用户确认冲突项.

        选文件与弹窗必须在主线程完成(前者会阻塞事件循环, 后者是模态窗口), 只有
        "读包"这一步放到后台线程 —— 大包的清单解析不该卡住界面。用户没选文件就
        什么都不做, 也不进忙碌态。
        """
        if self._busy:
            return
        path = pick_file(title=tr("dialog.import_pick_title"))
        if not path:
            log_action("import.start", basic=True, result="cancelled")
            self._feedback(FeedbackKind.INFO, tr("action.import_canceled"))
            return
        self._set_busy(True)
        self._feedback(FeedbackKind.PENDING, tr("action.import_pending"))

        def runner() -> None:
            try:
                self._inspection = self.backend.inspect_import(path)
            except Exception as exc:
                self._messages.put(("err", str(exc)))
            else:
                self._messages.put(("inspected", ""))

        threading.Thread(target=runner, daemon=True).start()

    def _finish_inspection(self) -> None:
        """体检完成: 按包的类型弹对应的冲突对话框(取消就什么都不做).

        弹框前先解除忙碌状态: 模态窗口会一直占用主线程, 这期间状态栏不该还停在
        "正在读取"上, 用户取消后也不会留下一个转不停的忙碌态。两类包的对话框差异很大
        (批量包要逐款选方式与映射), 因此各自一个方法, 这里只分派。
        """
        inspection, self._inspection = self._inspection, None
        self._set_busy(False)
        if inspection is None:  # pragma: no cover - 只有体检成功才会投递该消息
            return
        if isinstance(inspection, BatchInspection):
            self._finish_batch_inspection(inspection)
            return
        self._finish_single_inspection(inspection)

    def _finish_single_inspection(self, inspection: ImportInspection) -> None:
        """单游戏包: 原有的冲突对话框(语义一字未改)."""
        prompt = import_prompt(inspection, self.backend.list_games())
        choice = import_package_dialog(
            self,
            self.p,
            title=tr("dialog.import_title"),
            prompt=prompt,
            locations_label=tr("dialog.import_locations"),
            locations_hint=tr("dialog.import_locations_hint"),
            strategy_label=tr("dialog.import_strategy"),
            strategies=import_strategies(has_targets=bool(prompt.targets)),
            target_label=tr("dialog.import_target"),
            target_hint=tr("dialog.import_target_hint"),
            target_locked_hint=tr("dialog.import_target_locked"),
            confirm_text=tr("dialog.import_confirm"),
        )
        if choice is None:
            log_action("import.start", basic=True, result="cancelled")
            self._feedback(FeedbackKind.INFO, tr("action.import_canceled"))
            return
        self._run_import(inspection, choice)

    def _finish_batch_inspection(self, batch: BatchInspection) -> None:
        """批量包: 逐款选择导入方式与存档位置(整批取消就什么都不导入)."""
        selection = batch_import_dialog(
            self,
            self.p,
            title=tr("dialog.import_batch_title"),
            prompt=batch_import_prompt(batch, self.backend.list_games()),
            locations_label=tr("dialog.import_locations"),
            locations_hint=tr("dialog.import_locations_hint"),
            strategy_label=tr("dialog.import_strategy"),
            target_label=tr("dialog.import_target"),
            confirm_text=tr("dialog.import_confirm"),
        )
        if selection is None:
            log_action("import.batch_start", basic=True, result="cancelled")
            self._feedback(FeedbackKind.INFO, tr("action.import_canceled"))
            return
        self._run_batch_import(batch, selection)

    def _run_batch_import(
        self, batch: BatchInspection, selection: BatchImportSelection
    ) -> None:
        """按逐款选择真的导入整批(后台线程), 完成后刷新并尽量选中导入的那一款.

        取消在服务层里表现为异常(整批停下, **已经导完的游戏保留**), 但那不是失败:
        已经落库的游戏必须让用户看到, 因此这里按"完成"处理 —— 提示里说清边界, 并照样
        刷新界面。判断依据是后端抛出的 ``OperationCancelledError``(它是**后台线程自己**
        得到的事实), 而不是主线程上那个取消标志 —— 后者在"导入刚好结束时才按取消"这类
        时序下可能与后台的实际结果不一致。
        """
        self._set_busy(True)
        self._batch_cancelled = False
        self._feedback(
            FeedbackKind.PENDING,
            tr("action.import_batch_pending", games=len(selection.choices)),
        )
        before = {game.game_id for game in self.backend.list_games()}

        def work() -> str:
            try:
                return self.backend.run_import_batch(batch, selection.choices)
            except OperationCancelledError as exc:
                self._batch_cancelled = True
                return str(exc)

        def ok(message: str) -> None:
            self._set_busy(False)
            cancelled, self._batch_cancelled = self._batch_cancelled, False
            self._canceled = False
            self._feedback(
                FeedbackKind.INFO if cancelled else FeedbackKind.SUCCESS, message
            )
            select = None if cancelled else self._batch_imported_game(before)
            self._refresh_after_manage(select=select)

        self._submit(work, ok)

    def _batch_imported_game(self, before: set[str]) -> str | None:
        """批量导入后该选中哪款游戏: 只新建了**一款**时选它, 否则保持原选中.

        后端只回一条可展示的提示(没有回传游戏 id), 所以这里与单包导入一样用导入前后
        游戏库的差集算。一次导入多款时"该看哪一款"没有依据, 因此不猜(返回 ``None``),
        不按差集里的顺序替用户挑一款。
        """
        created = [
            game.game_id
            for game in self.backend.list_games()
            if game.game_id not in before
        ]
        return created[0] if len(created) == 1 else None

    def _run_import(self, inspection: ImportInspection, choice: ImportChoice) -> None:
        """按用户选定的方式真导入(后台线程), 完成后刷新并选中相关游戏."""
        self._set_busy(True)
        self._feedback(
            FeedbackKind.PENDING,
            tr("action.import_running", name=inspection.game_name),
        )
        before = {game.game_id for game in self.backend.list_games()}

        def work() -> str:
            return self.backend.run_import(
                inspection,
                strategy=choice.strategy,
                target_game_id=choice.target_game_id,
                locations=choice.locations,
            )

        def ok(message: str) -> None:
            self._set_busy(False)
            self._feedback(FeedbackKind.SUCCESS, message)
            self._refresh_after_manage(select=self._imported_game(before, choice))

        self._submit(work, ok)

    def _imported_game(self, before: set[str], choice: ImportChoice) -> str | None:
        """导入后该选中哪款游戏: 新建的那一款, 否则就是合并的目标.

        后端只回一条可展示的提示(没有回传游戏 id), 所以"新建的是哪一款"用导入
        前后游戏库的差集算出来: 差集里有就选它, 没有(合并/跳过)就选合并目标,
        两者都没有就返回 ``None``, 由调用方保持原选中。
        """
        created = [
            game.game_id
            for game in self.backend.list_games()
            if game.game_id not in before
        ]
        if created:
            return created[0]
        return choice.target_game_id

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
        界面上的任何一次启用/停用都会走到这里, 所以顺便把自动启停的轮询间隔退回
        最快档: "刚手动改完"是最值得立即再探一次的时刻。
        """
        if self._activation:
            self._reset_activation_ladder()
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
            base_font_px=self._base_font_px,
            verification_mode=self._verification_mode,
            verification_parallel=self._verification_parallel,
            debug=self._debug,
            activation=self._activation,
            remember_window=self._remember_window,
            shortcuts=self._shortcuts,
            on_toggle_theme=self._on_toggle_theme,
            on_apply_language=self._on_language_change,
            on_apply_font_size=self._on_font_size_change,
            on_apply_verification=self._on_verification_change,
            on_apply_debug=self._on_debug_change,
            on_apply_activation=self._on_activation_change,
            on_apply_remember_window=self._on_remember_window_change,
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

    def _remember_window_geometry(self) -> None:
        """把窗口当前的尺寸与位置写回配置(最大化/最小化/全屏时**跳过这一项**).

        跳过等于保留上一次记下的值 —— 用户最大化后用关窗退出时, 下次打开应该还是他
        摆的那个大小(见 :func:`current_window_geometry`)。写不进去(没有配置路径、
        磁盘不可写)只记日志: 退出流程不能被这件事拦住。
        """
        if self._paths is None or not self._remember_window:
            return
        saved = current_window_geometry(self, scale=window_scaling(self))
        if saved is None:
            return
        config = load_or_repair_config(self._paths.config_path).config
        config.window = saved
        try:
            save_config(config, self._paths.config_path)
        except (OSError, ValueError) as exc:
            logger.warning("保存窗口尺寸失败: %s", exc)
            return
        log_action(
            "ui.window_geometry",
            basic=True,
            width=saved.width,
            height=saved.height,
            x=saved.x,
            y=saved.y,
        )

    def _on_close(self) -> None:
        """退出前记住窗口的尺寸位置, 然后走 :meth:`destroy`(它会释放后台资源)."""
        self._remember_window_geometry()
        self.destroy()

    def _release_background(self) -> None:
        """释放后台资源(调度器 + 全局快捷键监听); **幂等**, 可以重复调.

        为什么单独成方法(2026-10-05, PLAN §39.4): 以前只有 :meth:`_on_close` 会释放它们, 而界面
        用例的收尾走的是 ``destroy()`` —— 一个进程里跑完整套用例会攒下几十个活着的
        ``APScheduler`` 线程(每个真后端建起来就 start() 一个, 而界面用例里有 40 多处"建真后端
        的窗口 + ``finally: app.destroy()``"), 而 Tk 解释器早就被销毁了: macOS 上从非主线程碰
        已销毁的 Tk 是已知的段错误来源(那次 SIGTRAP 现场里就挂着约 30 个
        ``apscheduler..._main_loop``)。

        为什么单独成方法而不是把三行直接抄进 :meth:`destroy`: 幂等牌的读写、两个释放入口各自
        的容错都只该有一份; 而且关窗(``_on_close`` → ``destroy``)与用例收尾
        (``tests/gui_support.close_gui_apps``)本来就是两条路, 谁先走都行 —— 方法幂等, 后走的那
        条不会重复释放。

        取资源的方式与 :meth:`destroy` 一致: 从 ``self.__dict__`` 里拿, **拿不到就跳过**。
        构造中途失败也会走到销毁(那时 ``backend`` / ``_hotkeys`` 还没赋值), 而 Tk 控件的
        ``__getattr__`` 会把未知名字转发给 ``self.tk`` —— 连 ``tk`` 都还没有时会无限递归成
        ``RecursionError``, 把真正的失败现场搅乱。
        """
        if self.__dict__.get("_background_released"):
            return
        self._background_released = True
        owners = (
            ("快捷键监听", self.__dict__.get("_hotkeys")),
            ("后台资源", self.__dict__.get("backend")),
        )
        for label, owner in owners:
            release = getattr(owner, "shutdown", None) if owner is not None else None
            if release is None:
                continue
            try:
                release()
            except Exception as exc:  # pragma: no cover - 退出期异常不阻塞关闭
                # GUI 不使用 print: 退出期的问题只写日志, 不干扰界面.
                logger.warning("释放%s失败: %s", label, exc)

    def destroy(self) -> None:
        """销毁前撤掉挂在自己身上的定时任务.

        消息轮询(每 100ms)与详情名称重裁都是 ``after`` 任务; 控件销毁后它们仍在 Tk 的
        队列里, 下一次事件循环会以 ``invalid command name "..._poll_messages"`` 报错 ——
        输出落到 stderr, 在 CI 里会挂到**下一个用例**的 stderr 附件上掩盖真问题(实测报告
        里的 stderr 附件就是这么来的)。

        **光撤不够, 还要先立一块牌子**: 已经到点、已被 Tk 取走的那个任务撤不掉
        (实测: ``destroy()`` 之后仍有一次 ``_poll_messages`` 跑到
        ``_fit_task_name`` → ``can't invoke "winfo" command: application has been destroyed``),
        所以先把 ``_destroyed`` 置上 —— :meth:`_poll_messages` 与 :meth:`_refit_detail_names`
        开头看到它就什么都不做, 也不再排下一个。

        任务 id 从 ``self.__dict__`` 里取而不是 ``getattr``: 构造中途失败时这些字段还没建好,
        而 Tk 控件的 ``__getattr__`` 会把未知名字转发给 ``self.tk``(连 ``tk`` 都还没有时
        会无限递归成 ``RecursionError``)。销毁函数自己不能因为"属性没建好"再抛一个异常,
        把真正的失败现场搅乱。

        后台资源(调度器/快捷键监听)也在这里放掉: 窗口没了它们就再没有别的释放入口, 留着就是
        活着的线程碰已销毁的 Tk(PLAN §39.4 的 macOS SIGTRAP)。放在最后一步拆控件**之前**做 ——
        一个还在跑的调度器随时可能在自己的线程里回回调到界面。
        """
        self._destroyed = True
        self._cancel_poll_job()
        self._cancel_after_job("_refit_job")
        self._release_background()
        super().destroy()

    def _cancel_after_job(self, name: str) -> None:
        """撤掉 ``self.__dict__`` 里记着的一个 ``after`` 任务(没有/已失效都不报错).

        先取 id 再置空: 撤完不管成不成功都不该留着旧 id(留着它下次会去撤一个已经被别的
        任务复用的编号)。
        """
        job = self.__dict__.get(name)
        if job is not None:
            with contextlib.suppress(tk.TclError):
                self.after_cancel(job)
        setattr(self, name, None)

    def _cancel_poll_job(self) -> None:
        """撤掉挂着的消息轮询任务."""
        self._cancel_after_job("_poll_job")

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
        # 主页的长操作入口与主窗口共用同一份忙碌事实(它不走 _submit)。
        self._home_page.set_busy(busy)

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
        """主线程轮询队列并分发完成消息与快捷键请求.

        **开头先把挂着的那个轮询任务撤掉再干活**: 用例与夹具会**直接**调这个方法把后台结果
        搬回主线程(``tests/integration/test_gui_buttons.py`` 的 ``_drain`` 每轮都调, 一
        条用例就调好几次), 而末尾又会排下一个 —— 不先撤, 先前那个的 id 就被覆盖成**孤儿**,
        ``destroy()`` 撤不到它, 它随后在**别的**用例的事件循环里以
        ``invalid command name "..._poll_messages"`` 爆掉, 挂在那条无辜用例的 stderr 附件上
        (2026-10-05 的报告: Linux 分片上 5 条这种报错落在
        ``test_default_view_is_branch_tree_and_hides_old_auto_backups`` 名下)。自己跑完的那种
        正常情形下这个 id 已经失效, 撤它是空操作。

        **已被 Tk 取走的那个任务撤不掉**: :meth:`destroy` 会先立 ``_destroyed`` 牌子, 走到这里
        就什么都不做 —— 否则它会在根已经销毁之后去碰控件, 抛
        ``can't invoke "winfo" command: application has been destroyed``(实测报告里那条
        ``Exception in Tkinter callback``), 那同样是挂在别人名下的噪声。
        """
        if self.__dict__.get("_destroyed", False):
            return
        self._cancel_poll_job()
        # 顺便把后台线程"寄存"的字体删掉: `tkinter.font.Font.__del__` 会调 Tcl, 而 Tcl
        # 只有主线程能安全调用 —— 后台 worker 触发 GC 时终结字体对象会直接段错误(见
        # archive_management.ui.typography._ScaledFont.__del__)。这里本来就在主线程上按
        # 100ms 周期跑, 是天然的排空点, 不需要额外的事件源。
        drain_deferred_fonts()
        while True:
            try:
                kind, payload = self._messages.get_nowait()
            except queue.Empty:
                break
            if kind == "hotkey":
                self._run_hotkey(payload)
                continue
            if kind == "activation":
                self._finish_activation()
                continue
            if kind == "inspected":
                self._finish_inspection()
                continue
            self._finish_message(kind, payload)
        self._refresh_task()
        self._maybe_poll_activation()
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

    def _maybe_poll_activation(self) -> None:
        """到达当前间隔就在后台跑一次自动启停判断.

        开关关闭、上一次还没回来、还没到点都直接跳过 —— 关掉就该完全没有开销
        (一次进程表都不枚举)。间隔由队列是否非空决定(见 activation_delay)。
        """
        if not self._activation or self._activation_busy:
            return
        now = time.monotonic()
        if now < self._activation_due:
            return
        self._activation_busy = True
        self._activation_due = now + activation_delay(
            self._activation_steps, running=self._activation_running
        )
        threading.Thread(target=self._run_activation_poll, daemon=True).start()

    def _run_activation_poll(self) -> None:
        """后台线程: 跑一次自动启停判断, 结果经消息队列交回主线程."""
        try:
            self._activation_run = self.backend.poll_activation(enabled=True)
        except Exception as exc:
            # 探测失败在服务层已经降级成"无法确认", 走到这里说明是数据层的问题:
            # 记下来但不要让后台线程死掉, 也不要弹窗打断用户。
            logger.warning("自动启停轮询失败: %s", exc)
        finally:
            self._messages.put(("activation", ""))

    def _finish_activation(self) -> None:
        """处理自动启停的轮询结果(主线程): 记账、回填分页, 只在真切换时提示.

        列表与详情不用在这里重读: 切换会抬高后端的数据版本号, 下一次
        :meth:`_refresh_task` 自己会重载。
        """
        outcome, self._activation_run = self._activation_run, None
        self._activation_busy = False
        if outcome is None:
            return
        self._track_activation_interval(outcome)
        self._home_page.refresh_activation(outcome, enabled=self._activation)
        self._report_activation(outcome)

    def _track_activation_interval(self, outcome: ActivationOutcome) -> None:
        """队列是否非空决定下一轮的间隔档位(空队列回最快档; 见 activation_delay)."""
        self._activation_running = bool(outcome.queue)
        self._activation_steps = (
            self._activation_steps + 1 if self._activation_running else 0
        )

    def _report_activation(self, outcome: ActivationOutcome) -> None:
        """只在真的改了启用态时给状态栏提示(接管 / 回落 / 收回)."""
        if not outcome.changed:
            return
        if outcome.reason == REASON_FALLBACK and outcome.enabled is not None:
            self._feedback(
                FeedbackKind.INFO,
                tr(
                    "activation.auto_fallback",
                    name="" if outcome.disabled is None else outcome.disabled.name,
                    next=outcome.enabled.name,
                ),
            )
            return
        game = outcome.enabled if outcome.enabled is not None else outcome.disabled
        name = "" if game is None else game.name
        if outcome.reason == REASON_ENABLED:
            self._feedback(
                FeedbackKind.SUCCESS, tr("activation.auto_enabled", name=name)
            )
        elif outcome.reason == REASON_DISABLED:
            self._feedback(FeedbackKind.INFO, tr("activation.auto_disabled", name=name))

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
    from archive_management.config import load_or_repair_config
    from archive_management.infrastructure.database import Database
    from archive_management.logging_config import configure_from_settings
    from archive_management.ui.sql_backend import SqlArchiveService

    paths = ApplicationPaths.default().ensure() if paths is None else paths.ensure()
    loaded = load_or_repair_config(paths.config_path)
    configure_from_settings(paths.log_dir, loaded.config.logging, verbose=verbose)
    if loaded.reset:
        logger.warning("配置文件内容非法, 已还原为默认值: %s", loaded.backup)
    elif loaded.repaired:
        logger.warning(
            "配置文件有 %d 处内容非法, 已剔除并还原为默认值: %s",
            len(loaded.repaired),
            ", ".join(loaded.repaired),
        )
    logger.info("界面启动(verbose=%s)", verbose)
    database = Database(paths.database_path)
    database.migrate()
    backend: ArchiveService = SqlArchiveService(
        database,
        backup_root=paths.backup_root,
        cache_dir=paths.cache_dir,
        artwork_dir=paths.artwork_dir,
        config_path=paths.config_path,
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
