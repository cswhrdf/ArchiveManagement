"""游戏启停分页(主窗口内的一页, 由游戏主页承载).

展示自动启停看到的东西: 按**启动顺序**登记的游戏队列(谁先被观察到运行就排在前面)、
当前监控中的那一款, 以及每行的处理动作。这里只做展示与交互编排 —— 进程探测、接管与
回落、状态落库都在后端完成; 队列本身是"最近一次轮询的结果"(内存值), 因此页面提供
"刷新"让用户主动再探一次(它同时把轮询间隔退回最快档)。

开关关闭时只显示一句"去哪里打开", 不显示空表: 关掉就该是完全不存在。
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

import customtkinter as ctk

from archive_management.application.games import ActivationOutcome, QueueItem
from archive_management.i18n import tr
from archive_management.ui.palette import Palette

_MonitorCallback = Callable[[str], None]
_DetailCallback = Callable[[str], None]
_RefreshCallback = Callable[[], None]

# 卡片内文字相对卡片边缘的内缩: 与游戏库/游戏发现的卡片保持一致。
_PANEL_PAD = 10

# 列顺序与宽度(序号 / 游戏 / 状态 / 首次观察到 / 最近一次探测), 最后一列是动作。
_COLUMNS = ("order", "name", "state", "first_seen", "last_seen")
_COLUMN_WIDTHS = (48, 260, 130, 150, 150)


def _stamp(value: str) -> str:
    """把 ISO 时间戳裁成"日期 + 分钟"(空值返回占位符)."""
    if not value:
        return "—"
    return value[:16].replace("T", " ")


class ActivationPanel:
    """游戏启停分页的内容."""

    def __init__(
        self,
        parent: ctk.CTkFrame,
        *,
        palette: Palette,
        on_monitor: _MonitorCallback | None = None,
        on_open_detail: _DetailCallback | None = None,
        on_refresh: _RefreshCallback | None = None,
    ) -> None:
        """在 ``parent`` 内构造分页(构造时还没有轮询结果, 先显示等待状态)."""
        self._palette = palette
        self._on_monitor = on_monitor
        self._on_open_detail = on_open_detail
        self._on_refresh = on_refresh
        self._outcome: ActivationOutcome | None = None
        self._enabled = False
        self.frame = ctk.CTkFrame(parent, fg_color=palette.background, corner_radius=0)
        self._build()
        self.paint()

    # -- 布局 ---------------------------------------------------------------

    def _build(self) -> None:
        palette = self._palette
        container = self.frame
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(container, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew")
        header.grid_columnconfigure(0, weight=1)
        self._summary_label = ctk.CTkLabel(
            header,
            text="",
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_body,
        )
        self._summary_label.grid(row=0, column=0, sticky="w")
        self._refresh_btn = self._button(
            header, tr("activation.refresh"), self._refresh, width=88
        )
        self._refresh_btn.grid(row=0, column=1, sticky="e")

        self._hint_label = ctk.CTkLabel(
            container,
            text="",
            anchor="w",
            justify="left",
            wraplength=900,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        )
        self._hint_label.grid(row=1, column=0, sticky="w", pady=(6, 0))

        table = ctk.CTkFrame(
            container,
            fg_color=palette.panel,
            corner_radius=10,
            border_width=1,
            border_color=palette.border,
        )
        table.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        table.grid_columnconfigure(0, weight=1)
        table.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(table, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=_PANEL_PAD, pady=(10, 4))
        self._configure_columns(head)
        for index, key in enumerate(_COLUMNS):
            ctk.CTkLabel(
                head,
                text=tr(f"activation.column_{key}"),
                anchor="w",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=palette.text_muted,
            ).grid(row=0, column=index, sticky="w", padx=(0, 8))

        self._rows_box = ctk.CTkScrollableFrame(
            table, fg_color=palette.well, corner_radius=8
        )
        self._rows_box.grid(
            row=1, column=0, sticky="nsew", padx=_PANEL_PAD, pady=(0, 10)
        )
        self._rows_box.grid_columnconfigure(0, weight=1)
        self._row_widgets: list[ctk.CTkFrame] = []

    @staticmethod
    def _configure_columns(frame: ctk.CTkFrame) -> None:
        """给表头与每一行同一套列宽(两个 frame 各自 grid, 只能靠相同的配置对齐)."""
        for index, width in enumerate(_COLUMN_WIDTHS):
            frame.grid_columnconfigure(
                index, minsize=width, weight=1 if index == 1 else 0
            )
        frame.grid_columnconfigure(len(_COLUMNS), minsize=240)

    def _button(
        self,
        parent: ctk.CTkFrame,
        text: str,
        command: Callable[[], None],
        *,
        style: str = "ghost",
        width: int = 96,
    ) -> ctk.CTkButton:
        """按当前调色板创建一个按钮."""
        palette = self._palette
        colors = {
            "accent": (palette.accent, palette.accent_soft_border, palette.accent_text),
            "ghost": (palette.raised, palette.item_hover, palette.text_body),
        }[style]
        return ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=30,
            corner_radius=8,
            fg_color=colors[0],
            hover_color=colors[1],
            text_color=colors[2],
            border_width=1 if style == "ghost" else 0,
            border_color=palette.border,
            font=ctk.CTkFont(size=12, weight="bold"),
        )

    # -- 渲染 ---------------------------------------------------------------

    def render(
        self,
        outcome: ActivationOutcome | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        """按最近一次轮询结果重绘(``enabled`` 跟着设置里的开关走)."""
        if outcome is not None:
            self._outcome = outcome
        if enabled is not None:
            self._enabled = enabled
        self.paint()

    def paint(self) -> None:
        """按当前状态刷新摘要、提示与队列列表."""
        if not self._enabled:
            self._paint_off()
            return
        outcome = self._outcome
        items = () if outcome is None else outcome.queue
        self._summary_label.configure(
            text=tr(
                "activation.summary",
                monitor=self._monitor_label(outcome),
                count=len(items),
            ),
            text_color=self._palette.text_body,
        )
        self._paint_hint(outcome, count=len(items))
        self._render_rows(items)

    def _paint_off(self) -> None:
        """开关关闭: 不摆空表, 只说清去哪里打开."""
        self._summary_label.configure(
            text=tr("activation.summary_off"), text_color=self._palette.text_muted
        )
        self._hint_label.configure(
            text=tr("activation.state_off"), text_color=self._palette.text_hint
        )
        self._render_rows(())

    def _paint_hint(self, outcome: ActivationOutcome | None, *, count: int) -> None:
        """提示行: 固定说明 + 冲突数 + "暂停中 / 等待游戏启动"这类当下状态."""
        conflicts = 0 if outcome is None else len(outcome.conflicts)
        hints = [tr("activation.hint")]
        if conflicts:
            hints.append(tr("activation.conflicts", count=conflicts))
        if outcome is not None and outcome.state.paused:
            hints.append(tr("activation.state_paused"))
        elif not count:
            hints.append(tr("activation.state_waiting"))
        self._hint_label.configure(
            text="  ".join(hints),
            text_color=self._palette.danger if conflicts else self._palette.text_hint,
        )

    @staticmethod
    def _monitor_label(outcome: ActivationOutcome | None) -> str:
        """摘要里的监控对象名(没有就给"暂无")."""
        if outcome is None or outcome.monitor is None:
            return tr("activation.monitor_none")
        return outcome.monitor.name

    def _render_rows(self, items: tuple[QueueItem, ...]) -> None:
        """重画队列列表(先清空: 位置与状态每次轮询都可能变)."""
        for row in self._row_widgets:
            row.destroy()
        self._row_widgets.clear()
        if not items:
            empty = ctk.CTkLabel(
                self._rows_box,
                text=tr("activation.empty"),
                anchor="w",
                font=ctk.CTkFont(size=12),
                text_color=self._palette.text_hint,
            )
            empty.grid(row=0, column=0, sticky="w", padx=6, pady=10)
            self._row_widgets.append(empty)
            return
        for index, item in enumerate(items):
            self._render_row(index, item)

    def _render_row(self, index: int, item: QueueItem) -> None:
        """画一行: 序号 / 名称 / 状态 / 两个时间 + 监控与详情动作."""
        palette = self._palette
        row = ctk.CTkFrame(self._rows_box, fg_color="transparent")
        row.grid(row=index, column=0, sticky="ew", pady=1)
        self._configure_columns(row)
        values = (
            str(item.position),
            item.game.name,
            self._state_text(item),
            _stamp(item.first_seen_at),
            _stamp(item.last_seen_at),
        )
        for column, value in enumerate(values):
            ctk.CTkLabel(
                row,
                text=value,
                anchor="w",
                font=ctk.CTkFont(size=12, weight="bold" if column == 1 else "normal"),
                text_color=palette.text_body if column < 3 else palette.text_muted,
            ).grid(row=0, column=column, sticky="w", padx=(0, 8), pady=6)
        actions = ctk.CTkFrame(row, fg_color="transparent")
        actions.grid(row=0, column=len(_COLUMNS), sticky="e")
        monitor_btn = self._button(
            actions,
            tr("activation.action_monitor"),
            partial(self._monitor, item),
            style="ghost" if item.monitor else "accent",
            width=132,
        )
        monitor_btn.grid(row=0, column=0, padx=(0, 6))
        if item.monitor:
            monitor_btn.configure(state="disabled")
        if self._on_open_detail is not None:
            self._button(
                actions,
                tr("activation.action_detail"),
                partial(self._open, item),
                width=96,
            ).grid(row=0, column=1)
        self._row_widgets.append(row)

    @staticmethod
    def _state_text(item: QueueItem) -> str:
        """行状态文案: 监控中 / 已暂停自动接管 / 运行中."""
        if item.monitor:
            return tr("activation.row_monitor")
        if item.suppressed:
            return tr("activation.row_suppressed")
        return tr("activation.row_running")

    # -- 动作与主题 ---------------------------------------------------------

    def _monitor(self, item: QueueItem) -> None:
        """把这一款设为监控对象(后端会走手动启用那条路)."""
        if self._on_monitor is not None and item.game.id is not None:
            self._on_monitor(str(item.game.id))

    def _open(self, item: QueueItem) -> None:
        """打开这一款的详情页."""
        if self._on_open_detail is not None and item.game.id is not None:
            self._on_open_detail(str(item.game.id))

    def _refresh(self) -> None:
        """手动刷新: 交给主窗口立即再探一次(它顺带把轮询间隔退回最快档)."""
        if self._on_refresh is not None:
            self._on_refresh()

    def apply_palette(self, palette: Palette) -> None:
        """主题切换后按新调色板重建(自绘控件不会跟着 UiKit 重绘)."""
        if palette is self._palette:
            return
        self._palette = palette
        self.frame.configure(fg_color=palette.background)
        for child in self.frame.winfo_children():
            child.destroy()
        self._build()
        self.paint()


__all__ = ["ActivationPanel"]
