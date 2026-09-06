"""CustomTkinter 通用构件.

``UiKit`` 集中创建控件并在主题切换时按调色板统一重绘(repaint),使
业务代码只面向展示模型与回调,不直接散落控件配色逻辑。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Literal

import customtkinter as ctk

from archive_management.ui.palette import Palette

_PaletteKey = str
_Repaint = Callable[..., None]

LabelStyle = Literal["primary", "body", "muted", "h2"]
ButtonStyle = Literal["accent", "danger", "ghost", "soft"]


class UiKit:
    """创建并登记控件,主题切换时重绘所有登记项."""

    def __init__(self) -> None:
        """初始化空的重绘登记表."""
        self._repaints: list[_Repaint] = []
        self._buttons: dict[ctk.CTkButton, ButtonStyle] = {}

    # -- 登记 ---------------------------------------------------------------

    def register(self, repaint: _Repaint) -> None:
        """登记一个主题重绘回调."""
        self._repaints.append(repaint)

    # -- 基础控件 -----------------------------------------------------------

    def frame(
        self,
        parent: ctk.CTkBaseClass,
        *,
        bg_key: str,
        border_key: str | None = None,
        corner_radius: int = 10,
    ) -> ctk.CTkFrame:
        """创建并登记一个按调色板着色的框架."""
        frame = ctk.CTkFrame(
            parent,
            corner_radius=corner_radius,
            border_width=1 if border_key else 0,
        )
        self.register(lambda p: self._recolor_frame(frame, p, bg_key, border_key))
        return frame

    def label(
        self,
        parent: ctk.CTkBaseClass,
        text: str,
        style: LabelStyle = "body",
        size: int = 13,
        weight: str = "normal",
        *,
        anchor: str = "w",
    ) -> ctk.CTkLabel:
        """创建并登记一个文本标签."""
        label = ctk.CTkLabel(
            parent,
            text=text,
            anchor=anchor,
            font=ctk.CTkFont(size=size, weight=weight),
        )
        color_key = {
            "primary": "text_primary",
            "body": "text_body",
            "muted": "text_muted",
            "h2": "text_primary",
        }[style]
        self.register(
            lambda p, w=label, k=color_key: w.configure(text_color=getattr(p, k))
        )
        return label

    def button(
        self,
        parent: ctk.CTkBaseClass,
        text: str,
        style: ButtonStyle = "ghost",
        *,
        command: Callable[[], None] | None = None,
        width: int = 100,
        height: int = 34,
        corner_radius: int = 7,
    ) -> ctk.CTkButton:
        """创建并登记一个指定样式的按钮."""
        button = ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=height,
            corner_radius=corner_radius,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        self._buttons[button] = style
        self.register(lambda p, w=button, s=style: self._paint_button(w, p, s))
        return button

    # -- 重绘 ---------------------------------------------------------------

    def apply(self, palette: Palette) -> None:
        """用给定调色板重绘全部登记控件."""
        for repaint in self._repaints:
            repaint(palette)

    # -- 内部 ---------------------------------------------------------------

    def _recolor_frame(
        self,
        frame: ctk.CTkFrame,
        palette: Palette,
        bg_key: str,
        border_key: str | None,
    ) -> None:
        frame.configure(fg_color=getattr(palette, bg_key))
        if border_key is not None:
            frame.configure(border_color=getattr(palette, border_key))

    def _paint_button(
        self,
        button: ctk.CTkButton,
        palette: Palette,
        style: ButtonStyle,
    ) -> None:
        if style == "accent":
            button.configure(
                fg_color=palette.accent,
                hover_color=palette.accent_soft_border,
                text_color=palette.accent_text,
            )
        elif style == "danger":
            button.configure(
                fg_color=palette.danger,
                hover_color=palette.accent_soft_border,
                text_color=palette.danger_text,
            )
        elif style == "soft":
            button.configure(
                fg_color=palette.accent_soft,
                hover_color=palette.accent_soft_border,
                text_color=palette.accent_soft_text,
            )
        else:  # ghost
            button.configure(
                fg_color=palette.raised,
                hover_color=palette.item_hover,
                text_color=palette.text_body,
                border_width=1,
                border_color=palette.border,
            )
