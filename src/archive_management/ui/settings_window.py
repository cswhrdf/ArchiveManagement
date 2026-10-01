"""设置窗口.

工作区的"设置"入口打开这个窗口, 目前包含:

- **外观**: 浅色/深色主题切换按钮(顶栏不再放置切换按钮);
- **界面语言**: 切换后界面整体重建, 并按新语言重探游戏译名;
- **日志**: “启用调试日志”开关(默认关闭)。关闭时日志里只保留 INFO 及以上的
  操作, 开启后 DEBUG 级基础操作也会写进日志 —— 排查问题时才需要;
- **游戏启停**: “按进程自动启停”开关(默认关闭)。开启后低频探测当前启用的那一款
  游戏, 观察到它运行就保持启用、观察到它退出就停用; 手动调整始终优先;
- **快捷键**: 全局保存与创建分支的组合键, 点击按键区域即可现场录制新组合;
- 后续计划中的设置项说明。

录制流程: 点击某个快捷键按钮后进入"监听输入"状态(同时通知主窗口暂停全局
快捷键, 否则按下的组合会真的触发动作), 用户按下组合键并松开后收敛成组合键
文本; 不满足"至少两个按键且含 Win/Ctrl/Alt/Shift 之一"时给出明确原因。小键盘
数字与主键盘数字是不同的按键, 因此依靠 ``keysym``/``keycode`` 区分。

窗口只显示全局设置, 不展示定时任务等某个游戏/某次操作的状态信息——那些内容
分别在"游戏设置"与"定时任务"窗口里维护。本模块采用组合式窗口, 便于无头测试。
"""

from __future__ import annotations

import logging
import tkinter as tk
from collections.abc import Callable, Mapping

import customtkinter as ctk

from archive_management.i18n import available_locales, tr
from archive_management.services.hotkeys import (
    ACTION_CREATE_BRANCH,
    ACTION_SAVE_NOW,
    combo_from_pressed,
    format_accelerator,
    tk_token,
)
from archive_management.ui.dialogs import _present
from archive_management.ui.palette import Palette
from archive_management.ui.typography import FONT_CHOICES
from archive_management.ui.widgets import auto_scrollbar, track_wraplength

logger = logging.getLogger(__name__)

_ToggleTheme = Callable[[], str]

# 窗口固定宽度 + 边距; 高度按内容算: 说明文字会随语言换行, 写死高度会把底部的
# 说明与"关闭"按钮裁到窗口外面(用户看不到也点不到)。
_WINDOW_WIDTH = 480
_WINDOW_PAD_X = 16
_WINDOW_PAD_Y = 16
_WINDOW_MIN_HEIGHT = 500
# 滚动区与固定页脚之间的间距(与 _footer 的 pady 一致).
_FOOTER_GAP = 12
# 窗口高度最多占到"屏幕高 - 这个留白": 给系统标题栏与任务栏留位, 免得窗口
# 在 1366x768 上比屏幕还高(底部的关闭按钮被推到屏幕外)。
_SCREEN_MARGIN = 120
# 成段说明的左右内裐(单侧): 面板里的说明都按 padx=16 排版, 算可用宽度时要减掉两侧。
_HINT_PAD = 16
# 说明 wraplength 的下限: 这里**不能用**默认的 widgets.WRAPLENGTH_MINIMUM(240) ——
# 窗口宽度写死不可缩放, 而右列控件会变宽(语言下拉框在 macOS 上更宽), 那时左列说明只剩
# 228; 掉 240 当地板会把 228 抬回 240, 右边照样被裁(2026-10-01 的 macOS CI 就是这么
# 红的: “界面语言”说明被切掉 12px)。
_HINT_MIN_WRAPLENGTH = 120
# 说明的"落位"宽度(还没量出可用宽度之前就按它排): 左列说明右侧被下拉框占掉约 172px,
# 跨两列的说明几乎占满面板, 页脚那一格右侧还有一个"关闭"按钮。这三个值**只影响窗口刚
# 建起来那一瞬间** —— 窗口高度按内容算, 落位与实际差得多就会先长/短一下再弹回去。
_HINT_INITIAL_WRAPLENGTH = 240
_WIDE_INITIAL_WRAPLENGTH = 400
_NOTE_INITIAL_WRAPLENGTH = 320
# 应用一个新组合键; 返回 None 表示成功, 否则返回可直接展示的失败说明.
_ApplyShortcut = Callable[[str, str], "str | None"]
# 切换界面语言; 返回 None 表示成功, 否则返回可直接展示的失败说明.
_ApplyLanguage = Callable[[str], "str | None"]
_ApplyFontSize = Callable[[int], "str | None"]
# 切换布尔开关(调试日志/自动启停); 返回 None 表示成功, 否则返回可直接展示的失败说明.
_ApplyToggle = Callable[[bool], "str | None"]
# 录制开始/结束的钩子(主窗口据此暂停与恢复全局快捷键).
_CaptureHook = Callable[[], None]

# 快捷键面板里展示的动作与文案键(顺序即展示顺序).
_SHORTCUT_ROWS: tuple[tuple[str, str], ...] = (
    (ACTION_SAVE_NOW, "settings.shortcut_save"),
    (ACTION_CREATE_BRANCH, "settings.shortcut_branch"),
)

# 所有键松开后等待多久才算录制结束(毫秒).
# 用户可能一根一根地按(而不是一直按着), 所以不能一松开就收尾;
# 留出一个很短的空档, 既容得下逐键输入, 也不会让人觉得卡。
_CAPTURE_SETTLE_MS = 600


def _paint_combo(box: ctk.CTkComboBox, palette: Palette) -> None:
    """按调色板重绘一个下拉框(含展开后那层菜单的配色).

    下拉框不归 UiKit 登记(它不属于本窗口的重绘表), 所以主题切换时得自己重绘一遍 ——
    漏在 :meth:`SettingsWindow.restyle` 外面时, 同一个窗口里会留下上一套主题的底色
    (用户实测: 界面字号/界面语言两个下拉框还是旧的样式, 重新打开窗口才变)。
    """
    box.configure(
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        button_color=palette.raised,
        button_hover_color=palette.item_hover,
        text_color=palette.text_body,
        dropdown_fg_color=palette.panel,
        dropdown_hover_color=palette.item_hover,
        dropdown_text_color=palette.text_body,
    )


class SettingsWindow:
    """全局设置窗口(不直接子类化 CTkToplevel)."""

    def __init__(
        self,
        parent: ctk.CTk,
        *,
        palette: Palette,
        theme: str,
        language: str,
        base_font_px: int,
        on_apply_font_size: _ApplyFontSize,
        debug: bool,
        activation: bool,
        shortcuts: Mapping[str, str],
        on_toggle_theme: _ToggleTheme,
        on_apply_language: _ApplyLanguage,
        on_apply_debug: _ApplyToggle,
        on_apply_activation: _ApplyToggle,
        on_apply_shortcut: _ApplyShortcut,
        on_capture_start: _CaptureHook,
        on_capture_end: _CaptureHook,
    ) -> None:
        """构造设置窗口并绑定主题、语言、两个开关与快捷键回调."""
        self._parent = parent
        self._palette = palette
        self._theme = theme
        self._language = language
        self._base_font_px = base_font_px
        self._on_apply_font_size = on_apply_font_size
        self._debug = debug
        self._activation = activation
        self._shortcuts: dict[str, str] = dict(shortcuts)
        self._on_toggle_theme = on_toggle_theme
        self._on_apply_language = on_apply_language
        self._on_apply_debug = on_apply_debug
        self._on_apply_activation = on_apply_activation
        self._on_apply_shortcut = on_apply_shortcut
        self._on_capture_start = on_capture_start
        self._on_capture_end = on_capture_end
        # 下拉框里显示的是语言自己的名字(中文写中文、英文写 English), 因此需要文案到
        # locale 的映射; 值来自资源目录, 新增语言不用改这里。
        self._locales: dict[str, str] = {
            self._locale_label(locale): locale for locale in available_locales()
        }
        self._capturing: str | None = None
        self._held: set[str] = set()
        self._captured: list[str] = []
        self._pending_finish: str | None = None
        self._error = ""
        self._build()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        palette = self._palette
        window = ctk.CTkToplevel(self._parent)
        self._window = window
        window.title(tr("settings.title"))
        window.resizable(False, False)
        window.transient(self._parent)
        window.configure(fg_color=palette.background)

        self._container = ctk.CTkFrame(window, fg_color=palette.background)
        self._container.pack(
            fill="both",
            expand=True,
            padx=_WINDOW_PAD_X,
            pady=_WINDOW_PAD_Y,
        )
        self._container.grid_columnconfigure(0, weight=1)
        self._container.grid_rowconfigure(0, weight=1)

        # 面板与说明都放进滚动区: 窗口高度被夹到屏幕可用高度以内, 内容装不下时自己
        # 滚动 —— 底部那条说明与"关闭"按钮留在固定页脚里, 永远看得见(16 号评审)。
        self._body = ctk.CTkScrollableFrame(
            self._container, fg_color=palette.background, corner_radius=0
        )
        self._body.grid(row=0, column=0, sticky="nsew")
        self._body.grid_columnconfigure(0, weight=1)
        # 内容装得下就不立滚动条: 窗口高度按内容算, 恰好放下时右侧那条拖不动的滑块
        # 只是噪声(与主页列表/定时窗口同一条规则)。
        auto_scrollbar(self._body)

        self._title_label = ctk.CTkLabel(
            self._body,
            text=tr("settings.title"),
            anchor="w",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=palette.text_primary,
        )
        self._title_label.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self._appearance_panel = ctk.CTkFrame(
            self._body,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        self._appearance_panel.grid(row=1, column=0, sticky="ew")
        self._appearance_panel.grid_columnconfigure(0, weight=1)
        self._appearance_title = ctk.CTkLabel(
            self._appearance_panel,
            text=tr("settings.appearance"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        self._appearance_title.grid(row=0, column=0, padx=16, pady=(16, 2), sticky="w")
        self._appearance_hint = ctk.CTkLabel(
            self._appearance_panel,
            text=tr("settings.appearance_hint"),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._appearance_hint.grid(row=1, column=0, padx=16, sticky="ew")
        self._track_hint(
            self._appearance_panel,
            self._appearance_hint,
            initial=_HINT_INITIAL_WRAPLENGTH,
        )
        self._toggle_btn = ctk.CTkButton(
            self._appearance_panel,
            text=self._toggle_text(),
            command=self._toggle_theme,
            width=112,
            height=30,
            corner_radius=8,
            fg_color=palette.accent,
            hover_color=palette.accent,
            text_color=palette.accent_text,
            font=ctk.CTkFont(size=12),
        )
        self._toggle_btn.grid(row=0, column=1, rowspan=2, padx=16, pady=16)
        self._theme_label = ctk.CTkLabel(
            self._appearance_panel,
            text=tr("settings.current_theme", theme=tr(f"theme.name_{self._theme}")),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._theme_label.grid(
            row=2, column=0, columnspan=2, padx=16, pady=(0, 6), sticky="w"
        )
        # 界面字号: 1rem = 这个值(默认 16px), 界面里所有字号等比缩放。
        self._font_label = ctk.CTkLabel(
            self._appearance_panel,
            text=tr("settings.font_size"),
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
        )
        self._font_label.grid(row=3, column=0, padx=16, pady=(0, 16), sticky="w")
        self._font_box = ctk.CTkComboBox(
            self._appearance_panel,
            values=[self._font_label_of(size) for size in FONT_CHOICES],
            width=140,
            height=30,
            corner_radius=8,
            fg_color=palette.input_bg,
            border_color=palette.input_border,
            button_color=palette.raised,
            button_hover_color=palette.item_hover,
            text_color=palette.text_body,
            dropdown_fg_color=palette.panel,
            dropdown_hover_color=palette.item_hover,
            dropdown_text_color=palette.text_body,
            font=ctk.CTkFont(size=12),
            dropdown_font=ctk.CTkFont(size=12),
            command=self._on_font_selected,
        )
        self._font_box.set(self._font_label_of(self._base_font_px))
        self._font_box.grid(row=3, column=1, padx=16, pady=(0, 16))

        self._language_panel = ctk.CTkFrame(
            self._body,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        self._language_panel.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._language_panel.grid_columnconfigure(0, weight=1)
        self._language_title = ctk.CTkLabel(
            self._language_panel,
            text=tr("settings.language"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        self._language_title.grid(row=0, column=0, padx=16, pady=(16, 2), sticky="w")
        self._language_hint = ctk.CTkLabel(
            self._language_panel,
            text=tr("settings.language_hint"),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._language_hint.grid(row=1, column=0, padx=16, sticky="ew")
        self._track_hint(
            self._language_panel,
            self._language_hint,
            initial=_HINT_INITIAL_WRAPLENGTH,
        )
        self._language_box = ctk.CTkComboBox(
            self._language_panel,
            values=list(self._locales),
            width=140,
            command=self._on_language_selected,
            state="readonly",
            fg_color=palette.input_bg,
            button_color=palette.raised,
            button_hover_color=palette.item_hover,
            border_color=palette.input_border,
            text_color=palette.text_body,
            dropdown_fg_color=palette.panel,
            dropdown_hover_color=palette.item_hover,
            dropdown_text_color=palette.text_body,
            font=ctk.CTkFont(size=12),
            dropdown_font=ctk.CTkFont(size=12),
        )
        self._language_box.set(self._locale_label(self._language))
        self._language_box.grid(row=0, column=1, rowspan=2, padx=16, pady=16)
        self._language_label = ctk.CTkLabel(
            self._language_panel,
            text=tr(
                "settings.current_language",
                language=self._locale_label(self._language),
            ),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._language_label.grid(
            row=2, column=0, columnspan=2, padx=16, pady=(0, 16), sticky="ew"
        )
        self._track_hint(
            self._language_panel,
            self._language_label,
            initial=_WIDE_INITIAL_WRAPLENGTH,
        )

        self._logging_panel = ctk.CTkFrame(
            self._body,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        self._logging_panel.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._logging_panel.grid_columnconfigure(0, weight=1)
        self._logging_title = ctk.CTkLabel(
            self._logging_panel,
            text=tr("settings.logging"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        self._logging_title.grid(row=0, column=0, padx=16, pady=(16, 2), sticky="w")
        self._logging_hint = ctk.CTkLabel(
            self._logging_panel,
            text=tr("settings.logging_hint"),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._logging_hint.grid(row=1, column=0, padx=16, sticky="ew")
        self._track_hint(
            self._logging_panel,
            self._logging_hint,
            initial=_HINT_INITIAL_WRAPLENGTH,
        )
        self._debug_switch = ctk.CTkSwitch(
            self._logging_panel,
            text=tr("settings.debug_logging"),
            command=self._on_debug_toggled,
            width=120,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
            progress_color=palette.accent,
            button_color=palette.text_muted,
            button_hover_color=palette.item_hover,
            fg_color=palette.input_bg,
        )
        self._set_debug_switch(self._debug)
        self._debug_switch.grid(row=0, column=1, rowspan=2, padx=16, pady=16)
        self._debug_label = ctk.CTkLabel(
            self._logging_panel,
            text=self._debug_state_text(),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._debug_label.grid(
            row=2, column=0, columnspan=2, padx=16, pady=(0, 16), sticky="ew"
        )
        self._track_hint(
            self._logging_panel,
            self._debug_label,
            initial=_WIDE_INITIAL_WRAPLENGTH,
        )

        self._activation_panel = ctk.CTkFrame(
            self._body,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        self._activation_panel.grid(row=4, column=0, sticky="ew", pady=(10, 0))
        self._activation_panel.grid_columnconfigure(0, weight=1)
        self._activation_title = ctk.CTkLabel(
            self._activation_panel,
            text=tr("settings.activation"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        self._activation_title.grid(row=0, column=0, padx=16, pady=(16, 2), sticky="w")
        self._activation_hint = ctk.CTkLabel(
            self._activation_panel,
            text=tr("settings.activation_hint"),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._activation_hint.grid(row=1, column=0, padx=16, sticky="ew")
        self._track_hint(
            self._activation_panel,
            self._activation_hint,
            initial=_HINT_INITIAL_WRAPLENGTH,
        )
        self._activation_switch = ctk.CTkSwitch(
            self._activation_panel,
            text=tr("settings.activation_auto"),
            command=self._on_activation_toggled,
            width=120,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
            progress_color=palette.accent,
            button_color=palette.text_muted,
            button_hover_color=palette.item_hover,
            fg_color=palette.input_bg,
        )
        self._set_activation_switch(self._activation)
        self._activation_switch.grid(row=0, column=1, rowspan=2, padx=16, pady=16)
        self._activation_label = ctk.CTkLabel(
            self._activation_panel,
            text=self._activation_state_text(),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._activation_label.grid(
            row=2, column=0, columnspan=2, padx=16, pady=(0, 16), sticky="ew"
        )
        self._track_hint(
            self._activation_panel,
            self._activation_label,
            initial=_WIDE_INITIAL_WRAPLENGTH,
        )

        self._shortcut_panel = ctk.CTkFrame(
            self._body,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        self._shortcut_panel.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        self._shortcut_panel.grid_columnconfigure(0, weight=1)
        self._shortcut_title = ctk.CTkLabel(
            self._shortcut_panel,
            text=tr("settings.shortcut"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        self._shortcut_title.grid(
            row=0, column=0, columnspan=2, padx=16, pady=(16, 6), sticky="w"
        )

        # 每个动作一行: 左侧是动作名, 右侧是可点击的"按键区域".
        self._shortcut_buttons: dict[str, ctk.CTkButton] = {}
        # 左侧动作名要留住引用: 切主题时要跟着重绘(否则它会留着旧主题的正文色).
        self._shortcut_labels: list[ctk.CTkLabel] = []
        for index, (action, label_key) in enumerate(_SHORTCUT_ROWS):
            row = 1 + index
            label = ctk.CTkLabel(
                self._shortcut_panel,
                text=tr(label_key),
                anchor="w",
                font=ctk.CTkFont(size=12),
                text_color=palette.text_body,
            )
            label.grid(row=row, column=0, padx=16, pady=(0, 8), sticky="w")
            self._shortcut_labels.append(label)
            button = ctk.CTkButton(
                self._shortcut_panel,
                text=format_accelerator(self._shortcuts.get(action, "")),
                command=lambda name=action: self._toggle_capture(name),
                width=148,
                height=30,
                corner_radius=8,
                # 按键区域要看起来能点: 输入框那样的底 + 1px 描边(16 号评审:
                # 光秃秃的粗体文字看不出可以点进去录制)。
                fg_color=palette.input_bg,
                hover_color=palette.item_hover,
                text_color=palette.text_body,
                border_width=1,
                # 描边色与真输入框共用 ``input_border``: 两者形状相同、又在同一个窗口里
                # 并排, 用两个深浅就不像一套控件了(这条评审要的就是"像输入框")。
                border_color=palette.input_border,
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            button.grid(row=row, column=1, padx=16, pady=(0, 8), sticky="e")
            self._shortcut_buttons[action] = button

        # 录制中的明确状态: 只有按钮文字变色的话, 用户看不出"正在监听"(17 号评审)。
        self._capture_status = ctk.CTkLabel(
            self._shortcut_panel,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12, weight="bold"),
            text_color=palette.accent,
        )
        self._capture_status.grid(
            row=1 + len(_SHORTCUT_ROWS),
            column=0,
            columnspan=2,
            padx=16,
            pady=(0, 6),
            sticky="ew",
        )
        self._track_hint(
            self._shortcut_panel,
            self._capture_status,
            initial=_WIDE_INITIAL_WRAPLENGTH,
        )

        # 这条说明是"为什么我按的键不被接受"的唯一出处, 因此用正文色(而不是最弱的
        # 灰字) —— 它还要配得上"录制中"时被反复阅读(17 号评审)。
        self._shortcut_hint = ctk.CTkLabel(
            self._shortcut_panel,
            text=tr("settings.shortcut_hint"),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
        )
        self._shortcut_hint.grid(
            row=2 + len(_SHORTCUT_ROWS), column=0, columnspan=2, padx=16, sticky="ew"
        )
        self._track_hint(
            self._shortcut_panel,
            self._shortcut_hint,
            initial=_WIDE_INITIAL_WRAPLENGTH,
        )
        # 面板最后一行的底部留白不能省: 贴边的文字会盖住面板自己的下边框。
        self._shortcut_error = ctk.CTkLabel(
            self._shortcut_panel,
            text="",
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=11),
            text_color=palette.danger,
        )
        self._shortcut_error.grid(
            row=3 + len(_SHORTCUT_ROWS),
            column=0,
            columnspan=2,
            padx=16,
            pady=(4, 16),
            sticky="ew",
        )
        self._track_hint(
            self._shortcut_panel,
            self._shortcut_error,
            initial=_WIDE_INITIAL_WRAPLENGTH,
        )
        self._paint_shortcuts()

        # 说明与关闭按钮在一个**固定页脚**里: 内容再长也只滚动上面的面板区,
        # 这两样永远留在窗口里可读可点(16 号评审)。
        self._footer = ctk.CTkFrame(self._container, fg_color=palette.background)
        self._footer.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        self._footer.grid_columnconfigure(0, weight=1)
        self._note_label = ctk.CTkLabel(
            self._footer,
            text=tr("settings.note"),
            anchor="w",
            justify="left",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._note_label.grid(row=0, column=0, sticky="ew")
        # 页脚这一格没有 padx, 因此左右内衬是 0。
        self._track_hint(
            self._footer,
            self._note_label,
            initial=_NOTE_INITIAL_WRAPLENGTH,
            pad=0,
        )

        self._close_btn = ctk.CTkButton(
            self._footer,
            text=tr("settings.close"),
            command=self.close,
            width=96,
            height=30,
            corner_radius=8,
            fg_color=palette.raised,
            hover_color=palette.item_hover,
            text_color=palette.text_body,
            border_width=1,
            border_color=palette.border,
            font=ctk.CTkFont(size=12),
        )
        self._close_btn.grid(row=0, column=1, sticky="e")

        # 先定宽再量 —— 换行后的高度才是准的。之后说明一换行就再收一次(见 _refit_height)。
        window.geometry(f"{_WINDOW_WIDTH}x{_WINDOW_MIN_HEIGHT}")
        window.update_idletasks()
        self._refit_height()
        # 常驻窗口不接窗口级的 Esc/回车/定焦: 那三件事是对话框的约定(见 _present)。
        _present(self._parent, window, modal=False)

    def _track_hint(
        self,
        parent: ctk.CTkBaseClass,
        label: ctk.CTkLabel,
        *,
        initial: int,
        pad: int = _HINT_PAD,
    ) -> None:
        """让一条成段说明跟着**它自己分到的宽度**换行, 并在换行变化时重算窗口高度.

        写死的 ``wraplength`` 两头都不对: 窄列里(右列控件变宽, 说明这一格只剩 228)会被 Tk
        硬裁 —— 没有省略号, 后半句直接看不到(macOS CI 上的“界面语言”说明); 宽面板里又会
        提前折行, 读起来像被截断(第 12 号评审)。规则见 :func:`widgets.track_wraplength`。

        ``initial`` 是还没量出可用宽度之前的落位宽度: 量准之后就不再生效。
        """
        track_wraplength(
            parent,
            label,
            inset=2 * pad,
            minimum=_HINT_MIN_WRAPLENGTH,
            initial=initial,
            on_change=self._refit_height,
        )

    def _refit_height(self) -> None:
        """按当前内容重算窗口高度(说明换行变了行数就变了).

        宽度写死, 高度只能按**实际内容**算: 说明文字会随语言与可用宽度换行, 写死高度会把
        底部的说明与“关闭”按钮裁到窗口外面(16 号评审)。装不下时夹到屏幕可用高度以内,
        滚动区自己滚, 那两样留在固定页脚里(1366x768 上底部按钮被屏幕下沿切掉过)。
        """
        try:
            self._window.update_idletasks()
            content_height = (
                int(self._body.winfo_reqheight())
                + int(self._footer.winfo_reqheight())
                + _FOOTER_GAP
                + _WINDOW_PAD_Y * 2
            )
            available = int(self._parent.winfo_screenheight()) - _SCREEN_MARGIN
            self._window.geometry(
                f"{_WINDOW_WIDTH}x{max(_WINDOW_MIN_HEIGHT, min(content_height, available))}"
            )
        except tk.TclError:  # 窗口已销毁: 延后的量宽可能晚于关闭
            return

    def _toggle_text(self) -> str:
        """按钮文案: 点击后会切到的主题."""
        return tr("theme.to_dark") if self._theme == "light" else tr("theme.to_light")

    @staticmethod
    def _locale_label(locale: str) -> str:
        """语言在列表里显示的名字(每种语言用自己那套写法)."""
        return tr(f"locale.{locale}")

    def _font_label_of(self, size: int) -> str:
        """字号下拉里的文案(带上 px 口径, 与「1rem = 基准字号」的说法一致)."""
        return tr("settings.font_choice", size=size)

    def _on_font_selected(self, value: str) -> None:
        """下拉里选了一个基准字号: 交给主窗口应用(它会重建界面)."""
        size = next(
            (
                candidate
                for candidate in FONT_CHOICES
                if self._font_label_of(candidate) == value
            ),
            None,
        )
        if size is None or size == self._base_font_px:
            return
        error = self._on_apply_font_size(size)
        if error is not None:
            self._font_box.set(self._font_label_of(self._base_font_px))

    def _on_language_selected(self, value: str) -> None:
        """切换语言: 交给主窗口处理, 成功后本窗口关闭(主窗口会整体重建).

        文案是构建时取的, 换语言必须重建界面 —— 重建后本窗口也会用新语言重开,
        因此这里直接关掉, 让用户看到一份彻底一致的新界面。
        """
        locale = self._locales.get(value)
        if locale is None or locale == self._language:
            return
        error = self._on_apply_language(locale)
        if error is None:
            self._window.destroy()
            return
        self._language_label.configure(
            text=tr("settings.language_failed", reason=error)
        )

    def _debug_state_text(self) -> str:
        """调试开关当前状态的说明文字."""
        state = tr("settings.debug_on" if self._debug else "settings.debug_off")
        return tr("settings.debug_state", state=state)

    def _set_debug_switch(self, enabled: bool) -> None:
        """把开关拨到指定状态.

        ``select``/``deselect`` 只改控件的值, **不会**触发 ``command``(那只有
        真实点击才会走), 所以下面用开关失败回滚时不会递归回调。
        """
        if enabled:
            self._debug_switch.select()
        else:
            self._debug_switch.deselect()

    def _on_debug_toggled(self) -> None:
        """切换调试日志开关: 交给主窗口应用; 失败时说明原因并把开关拨回去."""
        wanted = bool(self._debug_switch.get())
        if wanted == self._debug:
            return
        error = self._on_apply_debug(wanted)
        if error is not None:
            self._debug_label.configure(text=tr("settings.debug_failed", reason=error))
            # 开关拨回实际生效的状态: 否则看起来像已经改成功了.
            self._set_debug_switch(self._debug)
            return
        self._debug = wanted
        self._debug_label.configure(text=self._debug_state_text())

    def _activation_state_text(self) -> str:
        """自动启停开关当前状态的说明文字."""
        state = tr(
            "settings.activation_on" if self._activation else "settings.activation_off"
        )
        return tr("settings.activation_state", state=state)

    def _set_activation_switch(self, enabled: bool) -> None:
        """把开关拨到指定状态(不触发 ``command``, 所以失败回滚不会递归)."""
        if enabled:
            self._activation_switch.select()
        else:
            self._activation_switch.deselect()

    def _on_activation_toggled(self) -> None:
        """切换自动启停: 交给主窗口应用; 失败时说明原因并把开关拨回去."""
        wanted = bool(self._activation_switch.get())
        if wanted == self._activation:
            return
        error = self._on_apply_activation(wanted)
        if error is not None:
            self._activation_label.configure(
                text=tr("settings.activation_failed", reason=error)
            )
            self._set_activation_switch(self._activation)
            return
        self._activation = wanted
        self._activation_label.configure(text=self._activation_state_text())

    # -- 交互 ---------------------------------------------------------------

    def _toggle_theme(self) -> None:
        """调用主窗口切换主题, 并按新配色重绘本窗口."""
        self._theme = self._on_toggle_theme()
        self.restyle(Palette.for_theme(self._theme))

    def restyle(self, palette: Palette) -> None:
        """按新调色板重绘窗口(主题切换后保持一致)."""
        self._palette = palette
        self._window.configure(fg_color=palette.background)
        self._container.configure(fg_color=palette.background)
        self._body.configure(fg_color=palette.background)
        self._footer.configure(fg_color=palette.background)
        for panel in (
            self._appearance_panel,
            self._language_panel,
            self._logging_panel,
            self._activation_panel,
            self._shortcut_panel,
        ):
            panel.configure(fg_color=palette.panel, border_color=palette.border)
        for label, color in (
            (self._title_label, palette.text_primary),
            (self._appearance_title, palette.text_primary),
            (self._language_title, palette.text_primary),
            (self._logging_title, palette.text_primary),
            (self._activation_title, palette.text_primary),
            (self._shortcut_title, palette.text_primary),
            (self._appearance_hint, palette.text_hint),
            (self._language_hint, palette.text_hint),
            (self._language_label, palette.text_muted),
            (self._font_label, palette.text_body),
            (self._logging_hint, palette.text_hint),
            (self._debug_label, palette.text_muted),
            (self._activation_hint, palette.text_hint),
            (self._activation_label, palette.text_muted),
            (self._shortcut_hint, palette.text_body),
            (self._capture_status, palette.accent),
            (self._shortcut_error, palette.danger),
            (self._note_label, palette.text_hint),
            (self._theme_label, palette.text_muted),
        ):
            label.configure(text_color=color)
        self._toggle_btn.configure(
            text=self._toggle_text(),
            fg_color=palette.accent,
            hover_color=palette.accent,
            text_color=palette.accent_text,
        )
        for switch in (self._debug_switch, self._activation_switch):
            switch.configure(
                text_color=palette.text_body,
                progress_color=palette.accent,
                button_color=palette.text_muted,
                button_hover_color=palette.item_hover,
                fg_color=palette.input_bg,
            )
        # 两个下拉框也要一起重绘: 它们漏在这里时, 切主题后同一个窗口里会留下旧底色.
        for box in (self._font_box, self._language_box):
            _paint_combo(box, palette)
        # 快捷键行的动作名也是文字控件, 漏在重绘表外时它会留着**旧主题**的正文色
        # (浅色主题的深灰文字落在深色面板上, 就是一条几乎看不见的淡灰字)。
        for label in self._shortcut_labels:
            label.configure(text_color=palette.text_body)
        self._theme_label.configure(
            text=tr("settings.current_theme", theme=tr(f"theme.name_{self._theme}"))
        )
        self._close_btn.configure(
            fg_color=palette.raised,
            hover_color=palette.item_hover,
            text_color=palette.text_body,
            border_color=palette.border,
        )
        self._paint_shortcuts()

    # -- 快捷键录制 ---------------------------------------------------------
    def shortcut_text(self, action: str) -> str:
        """返回某个动作当前展示的组合键文本(录制中显示提示文案)."""
        if self._capturing == action:
            return tr("settings.recording")
        return format_accelerator(self._shortcuts.get(action, ""))

    def _toggle_capture(self, action: str) -> None:
        """点击按键区域: 开始录制, 再次点击则取消录制."""
        if self._capturing == action:
            self._finish_capture(canceled=True)
            return
        if self._capturing is not None:
            # 切换动作时先把上一个录制收尾, 避免两行同时处于录制态。
            self._finish_capture(canceled=True)
        self._start_capture(action)

    def _start_capture(self, action: str) -> None:
        """进入录制状态并通知主窗口暂停全局快捷键."""
        self._capturing = action
        self._held = set()
        self._captured = []
        self._error = ""
        self._on_capture_start()
        self._window.bind("<KeyPress>", self._on_key_press)
        self._window.bind("<KeyRelease>", self._on_key_release)
        self._window.focus_force()
        self._paint_shortcuts()

    def _on_key_press(self, event: tk.Event) -> str:
        """累计按下的按键(白名单之外的键只提示, 不参与组合); Esc 直接退出录制.

        Esc 在这里处理而不是留给窗口级绑定: 设置窗口是常驻窗口, 不该"按 Esc 就关掉"
        (实测反馈), 而录制中按 Esc 的意图是"这一行我不改了"。
        """
        if self._capturing is None:
            return "break"
        self._cancel_pending_finish()
        keysym = str(event.keysym)
        if keysym == "Escape":
            self._finish_capture(canceled=True)
            return "break"
        token = tk_token(keysym)
        if token is None:
            self._error = tr("hotkey.err_unknown_key")
        else:
            self._held.add(token)
            if token not in self._captured:
                self._captured.append(token)
        self._paint_shortcuts()
        return "break"

    def _on_key_release(self, event: tk.Event) -> str:
        """松开一个键; 全部松开后延迟收尾(允许用户逐键输入)."""
        if self._capturing is None:
            return "break"
        token = tk_token(str(event.keysym))
        if token is not None:
            self._held.discard(token)
        if not self._held:
            self._schedule_finish()
        return "break"

    def _schedule_finish(self) -> None:
        """安排延迟收尾; 期间再按键会被取消并继续累计."""
        self._cancel_pending_finish()
        self._pending_finish = self._window.after(
            _CAPTURE_SETTLE_MS, self._finish_when_idle
        )

    def _cancel_pending_finish(self) -> None:
        """取消尚未触发的延迟收尾."""
        pending, self._pending_finish = self._pending_finish, None
        if pending is None:
            return
        try:
            self._window.after_cancel(pending)
        except tk.TclError:  # pragma: no cover - 窗口已销毁
            logger.debug("取消录制收尾计时器失败")

    def _finish_when_idle(self) -> None:
        """延迟收尾回调: 仍然没有任何键按住时才真正结束录制."""
        self._pending_finish = None
        if self._capturing is not None and not self._held:
            self._finish_capture()

    def _finish_capture(self, *, canceled: bool = False) -> None:
        """结束录制: 取消时保留原值, 否则校验并应用新组合键."""
        action, tokens = self._capturing, list(self._captured)
        if action is None:
            return
        self._cancel_pending_finish()
        self._capturing = None
        self._held = set()
        self._captured = []
        if canceled:
            self._error = ""
        else:
            combo, reason = combo_from_pressed(tokens)
            if combo is None:
                self._error = tr(f"hotkey.err_{reason}")
            else:
                failure = self._on_apply_shortcut(action, combo.to_accelerator())
                if failure is None:
                    self._shortcuts[action] = combo.to_accelerator()
                    self._error = ""
                else:
                    self._error = failure
        self._unbind_keys()
        self._on_capture_end()
        self._paint_shortcuts()

    def _unbind_keys(self) -> None:
        """解绑键盘事件, 避免录制结束后继续吞掉按键."""
        for sequence in ("<KeyPress>", "<KeyRelease>"):
            try:
                self._window.unbind(sequence)
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                continue

    def _paint_shortcuts(self) -> None:
        """刷新快捷键按钮、录制状态与错误提示的文案与配色.

        录制中除了底色变强调色, 还要加粗描边 + 一行"正在监听…可取消": 只把按钮
        文字变绿的话, 用户分不出"录好了"还是"卡住了"(17 号评审)。
        """
        palette = self._palette
        for action, button in self._shortcut_buttons.items():
            recording = self._capturing == action
            button.configure(
                text=self.shortcut_text(action),
                fg_color=palette.accent if recording else palette.input_bg,
                hover_color=palette.accent if recording else palette.item_hover,
                text_color=palette.accent_text if recording else palette.text_body,
                border_width=2 if recording else 1,
                border_color=palette.accent if recording else palette.input_border,
            )
        self._capture_status.configure(text=self._recording_status())
        self._shortcut_error.configure(text=self._error)

    def _recording_status(self) -> str:
        """录制中的说明文字(平时为空, 不占高度)."""
        if self._capturing is None:
            return ""
        key = dict(_SHORTCUT_ROWS).get(self._capturing, "")
        return tr("settings.recording_hint", action=tr(key) if key else self._capturing)

    def focus(self) -> bool:
        """把窗口提到前台; 窗口已关闭时返回 False(主窗口据此允许重新打开)."""
        try:
            if not self._window.winfo_exists():
                return False
            self._window.deiconify()
            self._window.lift()
            self._window.focus_set()
        except tk.TclError:  # pragma: no cover - 窗口在检查与操作之间被销毁
            return False
        return True

    def close(self) -> None:
        """销毁窗口; 录制中关闭时先恢复被暂停的全局快捷键."""
        if self._capturing is not None:
            self._finish_capture(canceled=True)
        self._window.destroy()
