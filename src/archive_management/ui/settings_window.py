"""设置窗口.

工作区的"设置"入口打开这个窗口, 目前包含:

- **外观**: 浅色/深色主题切换按钮(顶栏不再放置切换按钮);
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
from archive_management.ui.dialogs import _center
from archive_management.ui.palette import Palette

logger = logging.getLogger(__name__)

_ToggleTheme = Callable[[], str]
# 应用一个新组合键; 返回 None 表示成功, 否则返回可直接展示的失败说明.
_ApplyShortcut = Callable[[str, str], "str | None"]
# 切换界面语言; 返回 None 表示成功, 否则返回可直接展示的失败说明.
_ApplyLanguage = Callable[[str], "str | None"]
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


class SettingsWindow:
    """全局设置窗口(不直接子类化 CTkToplevel)."""

    def __init__(
        self,
        parent: ctk.CTk,
        *,
        palette: Palette,
        theme: str,
        language: str,
        shortcuts: Mapping[str, str],
        on_toggle_theme: _ToggleTheme,
        on_apply_language: _ApplyLanguage,
        on_apply_shortcut: _ApplyShortcut,
        on_capture_start: _CaptureHook,
        on_capture_end: _CaptureHook,
    ) -> None:
        """构造设置窗口并绑定主题、语言与快捷键回调."""
        self._parent = parent
        self._palette = palette
        self._theme = theme
        self._language = language
        self._shortcuts: dict[str, str] = dict(shortcuts)
        self._on_toggle_theme = on_toggle_theme
        self._on_apply_language = on_apply_language
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
        window.geometry("480x500")
        window.resizable(False, False)
        window.transient(self._parent)
        window.configure(fg_color=palette.background)
        _center(self._parent, window)

        self._container = ctk.CTkFrame(window, fg_color=palette.background)
        self._container.pack(fill="both", expand=True, padx=18, pady=16)
        self._container.grid_columnconfigure(0, weight=1)

        self._title_label = ctk.CTkLabel(
            self._container,
            text=tr("settings.title"),
            anchor="w",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=palette.text_primary,
        )
        self._title_label.grid(row=0, column=0, sticky="w", pady=(0, 12))

        self._appearance_panel = ctk.CTkFrame(
            self._container,
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
        self._appearance_title.grid(row=0, column=0, padx=16, pady=(14, 2), sticky="w")
        self._appearance_hint = ctk.CTkLabel(
            self._appearance_panel,
            text=tr("settings.appearance_hint"),
            anchor="w",
            justify="left",
            wraplength=280,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._appearance_hint.grid(row=1, column=0, padx=16, sticky="w")
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
        self._toggle_btn.grid(row=0, column=1, rowspan=2, padx=16, pady=14)
        self._theme_label = ctk.CTkLabel(
            self._appearance_panel,
            text=tr("settings.current_theme", theme=tr(f"theme.name_{self._theme}")),
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._theme_label.grid(
            row=2, column=0, columnspan=2, padx=16, pady=(0, 14), sticky="w"
        )

        self._language_panel = ctk.CTkFrame(
            self._container,
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
        self._language_title.grid(row=0, column=0, padx=16, pady=(14, 2), sticky="w")
        self._language_hint = ctk.CTkLabel(
            self._language_panel,
            text=tr("settings.language_hint"),
            anchor="w",
            justify="left",
            wraplength=280,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._language_hint.grid(row=1, column=0, padx=16, sticky="w")
        self._language_box = ctk.CTkComboBox(
            self._language_panel,
            values=list(self._locales),
            width=140,
            command=self._on_language_selected,
            state="readonly",
            fg_color=palette.input_bg,
            button_color=palette.raised,
            button_hover_color=palette.item_hover,
            border_color=palette.border,
            text_color=palette.text_body,
            dropdown_fg_color=palette.panel,
            dropdown_text_color=palette.text_body,
            font=ctk.CTkFont(size=12),
            dropdown_font=ctk.CTkFont(size=12),
        )
        self._language_box.set(self._locale_label(self._language))
        self._language_box.grid(row=0, column=1, rowspan=2, padx=16, pady=14)
        self._language_label = ctk.CTkLabel(
            self._language_panel,
            text=tr(
                "settings.current_language",
                language=self._locale_label(self._language),
            ),
            anchor="w",
            justify="left",
            wraplength=400,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._language_label.grid(
            row=2, column=0, columnspan=2, padx=16, pady=(0, 14), sticky="w"
        )

        self._shortcut_panel = ctk.CTkFrame(
            self._container,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        self._shortcut_panel.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        self._shortcut_panel.grid_columnconfigure(0, weight=1)
        self._shortcut_title = ctk.CTkLabel(
            self._shortcut_panel,
            text=tr("settings.shortcut"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        self._shortcut_title.grid(
            row=0, column=0, columnspan=2, padx=16, pady=(14, 6), sticky="w"
        )

        # 每个动作一行: 左侧是动作名, 右侧是可点击的"按键区域".
        self._shortcut_buttons: dict[str, ctk.CTkButton] = {}
        for index, (action, label_key) in enumerate(_SHORTCUT_ROWS):
            row = 1 + index
            ctk.CTkLabel(
                self._shortcut_panel,
                text=tr(label_key),
                anchor="w",
                font=ctk.CTkFont(size=12),
                text_color=palette.text_body,
            ).grid(row=row, column=0, padx=16, pady=(0, 8), sticky="w")
            button = ctk.CTkButton(
                self._shortcut_panel,
                text=format_accelerator(self._shortcuts.get(action, "")),
                command=lambda name=action: self._toggle_capture(name),
                width=148,
                height=30,
                corner_radius=8,
                font=ctk.CTkFont(size=12, weight="bold"),
            )
            button.grid(row=row, column=1, padx=16, pady=(0, 8), sticky="e")
            self._shortcut_buttons[action] = button

        self._shortcut_hint = ctk.CTkLabel(
            self._shortcut_panel,
            text=tr("settings.shortcut_hint"),
            anchor="w",
            justify="left",
            wraplength=400,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._shortcut_hint.grid(
            row=1 + len(_SHORTCUT_ROWS), column=0, columnspan=2, padx=16, sticky="w"
        )
        # 面板最后一行的底部留白不能省: 贴边的文字会盖住面板自己的下边框。
        self._shortcut_error = ctk.CTkLabel(
            self._shortcut_panel,
            text="",
            anchor="w",
            justify="left",
            wraplength=400,
            font=ctk.CTkFont(size=11),
            text_color=palette.danger,
        )
        self._shortcut_error.grid(
            row=2 + len(_SHORTCUT_ROWS),
            column=0,
            columnspan=2,
            padx=16,
            pady=(4, 14),
            sticky="w",
        )
        self._paint_shortcuts()

        self._note_label = ctk.CTkLabel(
            self._container,
            text=tr("settings.note"),
            anchor="w",
            justify="left",
            wraplength=400,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._note_label.grid(row=4, column=0, sticky="w", pady=(12, 0))

        self._close_btn = ctk.CTkButton(
            self._container,
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
        self._close_btn.grid(row=5, column=0, sticky="e", pady=(14, 0))

    def _toggle_text(self) -> str:
        """按钮文案: 点击后会切到的主题."""
        return tr("theme.to_dark") if self._theme == "light" else tr("theme.to_light")

    @staticmethod
    def _locale_label(locale: str) -> str:
        """语言在列表里显示的名字(每种语言用自己那套写法)."""
        return tr(f"locale.{locale}")

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
        for panel in (
            self._appearance_panel,
            self._language_panel,
            self._shortcut_panel,
        ):
            panel.configure(fg_color=palette.panel, border_color=palette.border)
        for label, color in (
            (self._title_label, palette.text_primary),
            (self._appearance_title, palette.text_primary),
            (self._language_title, palette.text_primary),
            (self._shortcut_title, palette.text_primary),
            (self._appearance_hint, palette.text_muted),
            (self._language_hint, palette.text_muted),
            (self._language_label, palette.text_muted),
            (self._shortcut_hint, palette.text_muted),
            (self._shortcut_error, palette.danger),
            (self._note_label, palette.text_muted),
            (self._theme_label, palette.text_muted),
        ):
            label.configure(text_color=color)
        self._toggle_btn.configure(
            text=self._toggle_text(),
            fg_color=palette.accent,
            hover_color=palette.accent,
            text_color=palette.accent_text,
        )
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
        """累计按下的按键(白名单之外的键只提示, 不参与组合)."""
        if self._capturing is None:
            return "break"
        self._cancel_pending_finish()
        token = tk_token(str(event.keysym))
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
        """刷新快捷键按钮与错误提示的文案与配色."""
        palette = self._palette
        for action, button in self._shortcut_buttons.items():
            recording = self._capturing == action
            button.configure(
                text=self.shortcut_text(action),
                fg_color=palette.accent if recording else palette.raised,
                hover_color=palette.accent if recording else palette.item_hover,
                text_color=palette.accent_text if recording else palette.text_body,
                border_color=palette.accent if recording else palette.border,
            )
        self._shortcut_error.configure(text=self._error)

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
