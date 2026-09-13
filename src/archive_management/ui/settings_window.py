"""设置窗口.

工作区的"设置"入口打开这个窗口, 目前包含:

- **外观**: 浅色/深色主题切换按钮(顶栏不再放置切换按钮);
- **快捷键**: 全局保存快捷键及其说明;
- 后续阶段的设置占位说明。

窗口只显示全局设置, 不展示定时任务等某个游戏/某次操作的状态信息——那些内容
分别在"游戏设置"与"定时任务"窗口里维护。本模块采用组合式窗口, 便于无头测试。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable

import customtkinter as ctk

from archive_management.i18n import tr
from archive_management.ui.dialogs import _center
from archive_management.ui.palette import Palette

_ToggleTheme = Callable[[], str]


class SettingsWindow:
    """全局设置窗口(不直接子类化 CTkToplevel)."""

    def __init__(
        self,
        parent: ctk.CTk,
        *,
        palette: Palette,
        theme: str,
        shortcut: str,
        on_toggle_theme: _ToggleTheme,
    ) -> None:
        """构造设置窗口并绑定主题切换回调."""
        self._parent = parent
        self._palette = palette
        self._theme = theme
        self._shortcut = shortcut
        self._on_toggle_theme = on_toggle_theme
        self._build()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        palette = self._palette
        window = ctk.CTkToplevel(self._parent)
        self._window = window
        window.title(tr("settings.title"))
        window.geometry("460x320")
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

        self._shortcut_panel = ctk.CTkFrame(
            self._container,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        self._shortcut_panel.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        self._shortcut_panel.grid_columnconfigure(0, weight=1)
        self._shortcut_title = ctk.CTkLabel(
            self._shortcut_panel,
            text=tr("settings.shortcut"),
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        )
        self._shortcut_title.grid(row=0, column=0, padx=16, pady=(14, 2), sticky="w")
        self._shortcut_hint = ctk.CTkLabel(
            self._shortcut_panel,
            text=tr("settings.shortcut_hint"),
            anchor="w",
            justify="left",
            wraplength=280,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._shortcut_hint.grid(row=1, column=0, padx=16, sticky="w")
        self._shortcut_value = ctk.CTkLabel(
            self._shortcut_panel,
            text=self._shortcut,
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        self._shortcut_value.grid(row=0, column=1, rowspan=2, padx=16, pady=14)

        self._note_label = ctk.CTkLabel(
            self._container,
            text=tr("settings.note"),
            anchor="w",
            justify="left",
            wraplength=400,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        )
        self._note_label.grid(row=3, column=0, sticky="w", pady=(12, 0))

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
        self._close_btn.grid(row=4, column=0, sticky="e", pady=(14, 0))

    def _toggle_text(self) -> str:
        """按钮文案: 点击后会切到的主题."""
        return tr("theme.to_dark") if self._theme == "light" else tr("theme.to_light")

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
        for panel in (self._appearance_panel, self._shortcut_panel):
            panel.configure(fg_color=palette.panel, border_color=palette.border)
        for label, color in (
            (self._title_label, palette.text_primary),
            (self._appearance_title, palette.text_primary),
            (self._shortcut_title, palette.text_primary),
            (self._shortcut_value, palette.text_body),
            (self._appearance_hint, palette.text_muted),
            (self._shortcut_hint, palette.text_muted),
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
        """销毁窗口."""
        self._window.destroy()
