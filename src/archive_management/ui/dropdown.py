"""下拉框的弹出列表: 限高 + 滚动条 + 按位置决定往下还是往上展开.

出处(用户 2026-10-02): "下拉框应该要设定一下最大高度, 内容超出这个高度的要变成滚动条
不要一直撑开, 不然内容太多显示超出软件界面会显得奇怪; 还有根据下拉框所在的位置要决定好
下拉选择部分要往哪边展示, 位于软件下方的下拉框应该默认就向上展示"。

为什么不能就地改 CustomTkinter 自带的那张下拉: 它是 ``tkinter.Menu``
(``core_widget_classes.DropdownMenu``), 而 Tk 的菜单**没有"最大高度"这个选项** ——
内容多就一路撑长, 直到超过**屏幕**才由 Tk 自己加滚动箭头; 软件窗口比屏幕矮的时候,
列表直接压在窗口外面, 看起来像飘出去一截。所以这里把弹出层换成自建的
``tk.Toplevel`` + ``Listbox``: 行数自己定上限、超出加滚动条、位置按"软件窗口里还剩多少
地方"决定朝上还是朝下。

接线方式与 :mod:`rendering` / :mod:`textundo` / :mod:`keyboard` 一致: 在
``DropdownMenu`` 类上包一层(``open`` / ``close`` / ``is_open``), 于是 ``CTkComboBox``
与 ``CTkOptionMenu`` **都**自动走这套(两者都是
``self._dropdown_menu.open(winfo_rootx(), winfo_rooty() + height)``), 业务代码一个字
都不用改, 也不必逐个控件传新参数。

浮层里的控件一律用**原生 tk**(与 ``widgets.attach_tooltip`` 的提示窗口同一条理由):
它是一次性的临时层, 不在 CTk 窗口链上 —— ``CTkScrollbar`` 这类控件要靠 CTk 窗口才能
算缩放, 挂在临时层上不稳; 颜色从控件所在窗口的调色板里取, 看着仍是同一套。

单位: ``tk`` 控件的 ``geometry()`` / ``winfo_*`` 都是**物理**像素, 而本模块的
``DROPDOWN_*`` 常量是**设计**值(逻辑像素) —— 一律过 ``menu._apply_widget_scaling``
换算; 字体走 ``menu._apply_font_scaling``(**渲染**字号, 不是量的那个未缩放字号, 见
``widgets.measured_font`` 的说明)。
"""

from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from functools import partial
from math import floor
from typing import Any, cast

import customtkinter as ctk
from customtkinter.windows.widgets.core_widget_classes import DropdownMenu

from archive_management.ui.keyboard import palette_for
from archive_management.ui.palette import Palette

# 弹出层最多显示几行(设计值): 再多就一直撑长, 顶出软件界面。
DROPDOWN_MAX_ROWS = 10
# 弹出层与下拉框之间的缝(设计值): 贴着看起来像控件的一部分。
DROPDOWN_GAP = 2
# 与**软件窗口**边缘至少留出的距离(设计值): 列表不贴边, 也不越过窗口。
DROPDOWN_WINDOW_MARGIN = 4
# 与**屏幕**边缘至少留出的距离(设计值): 窗口本身比屏幕还高时兜底。
DROPDOWN_SCREEN_MARGIN = 8
# 列表文字左右的内衬(设计值): 内容比控件宽时按它放宽浮层。
DROPDOWN_TEXT_PAD = 8
# 滚动条的宽度(设计值): 与 CustomTkinter 的滚动条同宽, 观感才对得上
# (原生那一档是 16px, 用户 2026-10-03: "样式差距太大")。
DROPDOWN_SCROLLBAR_WIDTH = 16
# 浮层描边的宽度(物理像素): 原生 tk 控件的 highlightthickness 不参与缩放。
DROPDOWN_BORDER = 1

_APPLIED = False


@dataclass(frozen=True)
class DropdownPlan:
    """弹出层该放在哪、显示几行(全是**物理**像素/屏幕坐标)."""

    visible_rows: int
    """真的显示几行(被上限或剩余空间压过时就小于总数)."""

    scrolls: bool
    """内容是不是放不下所有行(= 右侧要出滚动条)."""

    flipped: bool
    """是不是**往上**展开(只有下方装不下时才这样)."""

    top: int
    """浮层上沿的屏幕 y 坐标."""

    height: int
    """浮层高度(``visible_rows`` 乘行高)."""


def plan_dropdown(
    *,
    rows: int,
    row_height: int,
    anchor_top: int,
    anchor_bottom: int,
    window_top: int,
    window_bottom: int,
    screen_top: int,
    screen_bottom: int,
    max_rows: int = DROPDOWN_MAX_ROWS,
    gap: int = DROPDOWN_GAP,
    window_margin: int = DROPDOWN_WINDOW_MARGIN,
    screen_margin: int = DROPDOWN_SCREEN_MARGIN,
) -> DropdownPlan:
    """算浮层的位置与行数(纯函数, 无头可测).

    判"这地方放得下吗"用的是**软件窗口**(而不是屏幕): 用户看到的问题是"列表跑到窗口
    外面去了", 屏幕边界只在窗口本身就装不下时兜底。顺序:

    1. 下方放得下整块 → 往下展开(默认, 与原生下拉一致);
    2. 下方放不下、上方放得下 → **往上**展开(用户 2026-10-02 的要求: 位于软件下方的
       下拉框默认向上);
    3. 两边都放不下 → 取空间大的一边, 并把行数压到放得下的行数(剩下的走滚动条);
    4. 最后按屏幕夹一次 —— 窗口比屏幕还高时, 至少保证浮层在屏幕上看得见。

    行高由调用方量出来(``Listbox`` 的行高就是字体的 ``linespace``): "压到几行"与
    "浮层多高"必须用同一个行高, 否则两处判断会互相打架。
    """
    ideal = min(rows, max_rows) * row_height
    below = (window_bottom - window_margin) - (anchor_bottom + gap)
    above = (anchor_top - gap) - (window_top + window_margin)
    fits_below = below >= ideal
    fits_above = above >= ideal
    flipped = not fits_below and (fits_above or above > below)
    room = above if flipped else below
    visible = rows if row_height <= 0 else min(rows, floor(room / row_height))
    visible = max(1, min(visible, max_rows))
    height = visible * row_height
    top = anchor_top - gap - height if flipped else anchor_bottom + gap
    ceiling = screen_bottom - screen_margin - height
    top = max(screen_top + screen_margin, min(top, ceiling))
    return DropdownPlan(
        visible_rows=visible,
        scrolls=visible < rows,
        flipped=flipped,
        top=top,
        height=height,
    )


def scrollbar_colors(palette: Palette) -> tuple[str, str]:
    """浮层滚动条的(底色, 滑块色).

    滑块必须与底色**一眼看得出区别**: 用户 2026-10-03 反馈"内容超出上限变成可滚动区域, 但
    看不到滚动条, 容易误判"(以为后面没内容了)。所以滑块不用 CTk 滚动条那一档暗色
    (``border``), 而用 ``text_muted`` —— 两套主题下与 ``panel`` 的对比度都在 3:1 以上,
    与非文字信息的下限一致(见 :func:`archive_management.ui.contrast.NON_TEXT_MINIMUM`)。

    控件本身用 CTk 原生的 ``CTkScrollbar``(形状/圆角/悬停与原生的完全一致): 这两个色分别
    喂给它的 ``fg_color`` 与 ``button_color``。

    取成纯函数是为了能**不建窗口**把"滑块与底色要分得开"钉住(见
    ``tests/unit/test_ui_dropdown.py``)。
    """
    return palette.panel, palette.text_muted


def plan_dropdown_width(
    *,
    anchor_left: int,
    anchor_width: int,
    widest: int,
    window_right: int,
    screen_right: int,
    text_pad: int = DROPDOWN_TEXT_PAD,
    scrollbar_width: int = 0,
    margin: int = DROPDOWN_WINDOW_MARGIN,
) -> tuple[int, int]:
    """算浮层的左边与宽度(物理像素): 至少与下拉框同宽, 内容更宽就放宽, 但不越出右边界.

    放宽是必须的: 列表里的文字被裁掉就再也看不到全文(原生菜单也是按内容撑宽的)。
    窗口太窄时宁可裁字也不让浮层飘出窗口。
    """
    limit = min(window_right, screen_right) - margin
    room = max(1, limit - anchor_left)
    width = max(anchor_width, widest + 2 * text_pad + scrollbar_width)
    return anchor_left, max(1, min(width, room))


class DropdownPopup:
    """一次弹出(同一时刻只会有一个)."""

    def __init__(
        self, menu: DropdownMenu, anchor: tk.Misc, palette: Palette, values: list[str]
    ) -> None:
        """记住来源与配色, 量出行高(还没建控件/定位)."""
        self.menu = menu
        self.anchor = anchor
        self.palette = palette
        self.values = values
        self.window: tk.Toplevel | None = None
        self.listbox: tk.Listbox | None = None
        self.scrollbar: ctk.CTkScrollbar | None = None
        self.plan: DropdownPlan | None = None
        self.font = tkfont.Font(font=menu._apply_font_scaling(menu._font))
        self.row_height = max(1, int(self.font.metrics("linespace")))
        self._binds: list[tuple[tk.Misc, str, str]] = []
        self._hover = -1

    # -- 开与收 ----------------------------------------------------------
    def show(self, x: int, y: int) -> bool:
        """按锚点位置摆好浮层并显示; 建控件/显示失败时返回 False(不抛)."""
        if not self._build():
            return False
        try:
            return self._place(int(x), int(y))
        except tk.TclError:  # pragma: no cover - 途中被销毁
            self.close()
            return False

    def close(self) -> None:
        """收起浮层(没开着就什么都不做), 并把挂在外面的绑定解开."""
        self._unbind_outside()
        window, self.window = self.window, None
        self.listbox, self.scrollbar = None, None
        if window is not None:
            with suppress(tk.TclError):  # 随锚点一起被销毁了
                window.destroy()

    def is_open(self) -> bool:
        """浮层现在是不是真的开着."""
        return self.window is not None

    def _build(self) -> bool:
        """建出浮层窗口与列表(锚点已销毁时返回 False).

        **附属窗口 + 不用 topmost**: 用户 2026-10-03 实测"点击别的软件, 别的窗口被盖住,
        但浮窗还是显示在最前方" —— topmost 是相对**整个屏幕**的, 它会让浮层盖在别的程序
        上面。改成 :meth:`tk.Toplevel.transient` 之后浮层是宿主窗口的**附属窗口**(Windows
        上的 owner): 始终在自己的宿主之上, 但别的程序被激活时就随着宿主一起沉下去 ——
        同时最小化宿主时它会跟着隐藏, 不用额外收拾。

        这也回答了"能不能和原生组件结合": 原生下拉是 ``tk.Menu``, 而 Tk 菜单没有"最大
        高度"这个选项(见本模块开头), 所以选了"让浮层成为宿主窗口的附属窗口"这条路。
        """
        host = self.anchor.winfo_toplevel()
        try:
            window = tk.Toplevel(self.anchor)
            window.overrideredirect(True)
            window.transient(host)
            # 显式关掉 topmost(**写在 ``transient`` 之后**: 它会让 Tk 在 macOS 上把这个
            # 窗口重新置于父窗口之上)。
            #
            # 实测 2026-10-03 / 10-04 两轮 CI: **macOS 上这句写不掉** —— 无边框
            # (override-redirect) 窗口的 ``-topmost`` 无论先写后写、写几次, 读出来都是 1
            # (Windows 上同样的代码读出来是 0)。那是 Tk 给这类窗口的平台默认值, 不要
            # 再试"换个顺序"或"重写一遍": 两个顺序都已经在 CI 上验过了。
            # 真正在起作用的机制是上面那句 ``transient`` —— 浮层是宿主的附属窗口, 宿主被
            # 隐藏/最小化时它跟着消失, 也不会随别的前台程序一起跑到最上层。所以用例里
            # "不盖住别的软件"那条只在 Windows 上钉读数(见 test_gui_dropdown 的说明),
            # macOS 上钉的是附属关系。
            window.attributes("-topmost", False)
            frame = tk.Frame(
                window,
                background=self.palette.panel,
                highlightthickness=DROPDOWN_BORDER,
                highlightbackground=self.palette.border,
            )
            frame.pack(fill="both", expand=True)
            listbox = tk.Listbox(
                frame,
                height=1,
                activestyle="none",
                exportselection=False,
                selectmode="browse",
                justify="left",
                highlightthickness=0,
                borderwidth=0,
                relief="flat",
                cursor="hand2",
                font=self.font,
                background=self.palette.panel,
                foreground=self.palette.text_body,
                selectbackground=self.palette.accent_soft,
                selectforeground=self.palette.accent_soft_text,
            )
            listbox.pack(side="left", fill="both", expand=True)
            for value in self.values:
                listbox.insert("end", value)
        except tk.TclError:  # pragma: no cover - 锚点在弹出途中被销毁
            return False
        self.window, self.listbox = window, listbox
        self._bind_interactions()
        return True

    # -- 位置与内容 ------------------------------------------------------
    def _place(self, x: int, y: int) -> bool:
        """算位置、限高、决定上下, 然后显示.

        ``x`` / ``y`` 是 CTk 传进来的"下拉框左下角"屏幕坐标(它自己会再加一段平台偏移,
        这里用原值 —— 偏移统一由 :data:`DROPDOWN_GAP` 负责)。

        行高**量两次**: 先按字体的 ``linespace`` 估一遍把浮层摆出来, 显示之后再读
        ``Listbox.bbox(0)`` 拿它自己排出来的行高重算一次。Tk 的 Listbox 行高不完全等于
        字体的 linespace(它还会算上自己的行距), 估错会让"点第几行"与"算出来多高"一起
        偏掉 —— 量准了再摆, 两处就用同一个数。
        """
        listbox, window = self.listbox, self.window
        if listbox is None or window is None:  # pragma: no cover - 只在本类的 show 里调
            return False
        plan, left, width = self._measure(x, y)
        self._fill(plan)
        self._write(left, width, plan)
        window.update_idletasks()
        window.deiconify()
        window.update_idletasks()
        self._refine(x, y)
        window.lift()
        listbox.focus_set()
        self._bind_outside()
        return True

    def _measure(self, x: int, y: int) -> tuple[DropdownPlan, int, int]:
        """按**当前**行高算一遍: 返回 (计划, 左边, 宽度)."""
        toplevel = self.anchor.winfo_toplevel()
        scale = self.menu._apply_widget_scaling
        anchor_top = min(int(self.anchor.winfo_rooty()), int(y))
        anchor_bottom = max(anchor_top + int(self.anchor.winfo_height()), int(y))
        window_top = int(toplevel.winfo_rooty())
        window_bottom = window_top + int(toplevel.winfo_height())
        plan = plan_dropdown(
            rows=len(self.values),
            row_height=self.row_height,
            anchor_top=anchor_top,
            anchor_bottom=anchor_bottom,
            window_top=window_top,
            window_bottom=window_bottom,
            screen_top=0,
            screen_bottom=int(self.anchor.winfo_screenheight()),
            gap=scale(DROPDOWN_GAP),
            window_margin=scale(DROPDOWN_WINDOW_MARGIN),
            screen_margin=scale(DROPDOWN_SCREEN_MARGIN),
        )
        scrollbar_width = scale(DROPDOWN_SCROLLBAR_WIDTH) if plan.scrolls else 0
        left, width = plan_dropdown_width(
            anchor_left=int(x),
            anchor_width=int(self.anchor.winfo_width()),
            widest=max(self.font.measure(value) for value in self.values),
            window_right=int(toplevel.winfo_rootx()) + int(toplevel.winfo_width()),
            screen_right=int(self.anchor.winfo_screenwidth()),
            text_pad=scale(DROPDOWN_TEXT_PAD),
            scrollbar_width=scrollbar_width,
            margin=scale(DROPDOWN_WINDOW_MARGIN),
        )
        return plan, left, width

    def _refine(self, x: int, y: int) -> None:
        """显示之后按 Listbox 自己排出来的行高重算一次(对不上才重算).

        行高取**第 0 行与第 1 行上沿之差**, 不是 ``bbox(0)`` 自己的高度: Tk 的 Listbox 在
        行之间还留了它自己的行距, 拿单个 bbox 的高度会把浮层算矮一点(实测点第 3 行落到了
        第 2 行上, 而点中哪一行正是按这个行高算的)。只有一行时量不出差值, 保持估出来的值。
        """
        listbox, window = self.listbox, self.window
        if (
            listbox is None or window is None
        ):  # pragma: no cover - 只在本类的 _place 里调
            return
        first, second = listbox.bbox(0), listbox.bbox(1)
        if not first or not second:
            return
        pitch = int(second[1]) - int(first[1])
        if pitch <= 0 or pitch == self.row_height:
            return
        self.row_height = pitch
        plan, left, width = self._measure(x, y)
        self._fill(plan)
        self._write(left, width, plan)

    def _fill(self, plan: DropdownPlan) -> None:
        """按计划限行、必要时挂滚动条、标出当前值."""
        listbox = self.listbox
        if listbox is None:  # pragma: no cover - 只在本类的 _place 里调
            return
        listbox.configure(height=plan.visible_rows)
        if plan.scrolls and self.scrollbar is None:
            self._add_scrollbar()
        self._mark_current()
        self.plan = plan

    def _write(self, left: int, width: int, plan: DropdownPlan) -> None:
        """把算出来的位置与高度写进浮层窗口(高度算上描边)."""
        window = self.window
        if window is None:  # pragma: no cover - 只在本类的 _place 里调
            return
        window.geometry(
            f"{width}x{plan.height + 2 * DROPDOWN_BORDER}+{left}+{plan.top}"
        )

    def _add_scrollbar(self) -> None:
        """内容超过上限时右侧挂一条滚动条.

        用 **CustomTkinter 原生**的 ``CTkScrollbar``(用户 2026-10-03: 自建的那条与原生样式
        差距太大) —— 它在普通 ``tk`` 临时窗口里也能拿到正确的窗口根与 DPI 缩放
        (``ScalingTracker.get_window_root_of_widget`` 就是往上找到我这个 Toplevel)。

        两处摆位必须对:

        * **父容器与列表相同**(列表所在的那个 ``tk.Frame``): 挂到浮层窗口上的话, 它与
          "``fill="both", expand=True`` 的容器"是兄弟, 容器把地方吃完后就轮不到它 ——
          实测它分到 **0 高**, 于是"内容超出上限却没有滚动条", 用户会以为后面没内容;
        * **先给它位置**(``before=listbox``): 列表先前已 ``pack`` 好了, 再挂就抢不到宽度。

        颜色走 :func:`scrollbar_colors`(靠对比度选): CTk 滚动条那一档暗色滑块在浮层上
        看不出区别, 等于没有滚动条。
        """
        listbox = self.listbox
        if listbox is None:  # pragma: no cover - 只在本类的 _place 里调
            return
        trough, slider = scrollbar_colors(self.palette)
        scrollbar = ctk.CTkScrollbar(
            listbox.master,
            command=listbox.yview,
            width=DROPDOWN_SCROLLBAR_WIDTH,
            fg_color=trough,
            button_color=slider,
            button_hover_color=self.palette.accent,
        )
        listbox.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y", before=listbox)
        self.scrollbar = scrollbar

    def _mark_current(self) -> None:
        """把当前值标成选中项并滚到它: 打开就看得见"现在选的是哪个"."""
        listbox = self.listbox
        if listbox is None:  # pragma: no cover - 只在本类的 _place 里调
            return
        reader = getattr(self.anchor, "get", None)
        if not callable(reader):  # pragma: no cover - 无头测试里的替身控件
            return
        try:
            current = str(reader())
        except (tk.TclError, ValueError):  # pragma: no cover - 控件已销毁
            return
        if current in self.values:
            index = self.values.index(current)
            listbox.selection_set(index)
            listbox.activate(index)
            listbox.see(index)

    # -- 交互 ------------------------------------------------------------
    def _bind_interactions(self) -> None:
        """点击选择、悬停高亮、键盘选中/收起.

        **不接 ``<FocusOut>``**: 浮层是 ``overrideredirect`` 的临时窗口, 它一出来就把焦点
        抢到自己身上, 而测试环境(无窗口管理器)里焦点会被立刻还给主窗口 —— 一开就收, 实测
        那条端到端用例因此时红时绿。收起改由**真实的动作**负责: 点窗口里别处、按 Esc、再点
        一次箭头、换一个下拉、锚点被销毁(见 :meth:`_bind_outside` 与 :func:`register`)。
        """
        listbox, window = self.listbox, self.window
        if (
            listbox is None or window is None
        ):  # pragma: no cover - 只在本类的 _build 里调
            return
        listbox.bind("<ButtonRelease-1>", self._on_click)
        listbox.bind("<Motion>", self._on_motion)
        listbox.bind("<Leave>", lambda _event: self._paint_hover(-1))
        listbox.bind("<Return>", self._on_return)
        listbox.bind("<KP_Enter>", self._on_return)
        listbox.bind("<Escape>", lambda _event: self.close())
        window.bind("<Escape>", lambda _event: self.close())

    def _on_click(self, event: tk.Event) -> None:
        """点一行 = 选中它并收起浮层."""
        self._commit(self._index_at(event))

    def _on_return(self, _event: tk.Event) -> str:
        """回车 = 选中当前高亮的那一行."""
        index = -1
        if self.listbox is not None:
            box = cast(Any, self.listbox)
            selected = box.curselection()
            index = int(selected[0]) if selected else int(box.index("active"))
        self._commit(index)
        return "break"

    def _index_at(self, event: tk.Event) -> int:
        """事件落在第几行(落在列表外面时为 -1)."""
        if self.listbox is None:  # pragma: no cover - 只在本类的回调里调
            return -1
        index = int(cast(Any, self.listbox).nearest(int(event.y)))
        return index if 0 <= index < len(self.values) else -1

    def _commit(self, index: int) -> None:
        """把第 ``index`` 行交给 CTk 自己的回调(它会写进输入框并触发业务回调)."""
        if not 0 <= index < len(self.values):
            return
        value = self.values[index]
        self.close()
        callback = getattr(self.menu, "_button_callback", None)
        if callable(callback):
            callback(value)

    def _on_motion(self, event: tk.Event) -> None:
        """悬停高亮跟着鼠标走(原生菜单也是这个反馈)."""
        self._paint_hover(self._index_at(event))

    def _paint_hover(self, index: int) -> None:
        """把上一次高亮的行恢复底色, 再给新的一行刷上悬停色."""
        listbox = self.listbox
        if listbox is None or index == self._hover:
            return
        if 0 <= self._hover < len(self.values):
            listbox.itemconfig(self._hover, background=self.palette.panel)
        if 0 <= index < len(self.values):
            listbox.itemconfig(index, background=self.palette.item_hover)
        self._hover = index

    # -- 外面该收起的两种时机 --------------------------------------------
    def _bind_outside(self) -> None:
        """挂几处"该收起了 / 该挪位了": 窗口里点到别处、锚点被销毁、宿主挪动或隐藏.

        绑在**顶层窗口**上而不是 ``bind_all``: 控件的 bindtags 里带顶层窗口, 所以窗口里
        任何一处点击都会冒泡上来(与原生菜单"点别处就消失"一致); ``bind_all`` 会把绑定
        挂到整个应用上, 收起时也没法只解自己那一条。

        **延后一拍再挂**(``after_idle``): 打开浮层的那一次点击自己也会冒泡到顶层窗口 ——
        当场挂上就等于"刚打开就被这次点击收起", 用户看到的是**点了完全没反应**
        (2026-10-03 实测; 只调 ``_open_dropdown_menu`` 的用例量不到, 因为那一步没有点击
        事件)。``after_idle`` 跑在这次点击的所有 bindtags 处理完之后, 所以抓不到它。

        幂等: 重定位会再走一遍 ``_place``, 里面的调用不能把同一批绑定挂两遍。
        """
        if self._binds:
            return
        toplevel = self.anchor.winfo_toplevel()
        window = self.window
        if window is None:  # pragma: no cover - 只在本类的 _place 里调
            return
        window.after_idle(partial(self._attach_outside, toplevel))

    def _attach_outside(self, toplevel: tk.Misc) -> None:
        """真正挂上那几处绑定(延后执行, 见 :meth:`_bind_outside`)."""
        if self.window is None:
            return  # 这一拍还没到就收起了: 不用挂
        self._bind(toplevel, "<Button-1>", self._on_outside_click)
        self._bind(self.anchor, "<Destroy>", self._on_anchor_destroy)
        # 宿主窗口被最小化/隐藏(任务栏最小化、``withdraw``)时也要收起: 浮层是**独立**窗口,
        # 不会跟着宿主一起消失, 于是它会飘在屏幕原处(用户 2026-10-03 实测)。最小化在 Tk 里
        # 就是 Unmap(Windows 上是 WM_SIZE 的 SIZE_MINIMIZED), 盯 <Unmap> 就够。
        self._bind(toplevel, "<Unmap>", self._on_host_gone)
        # ``<Iconify>`` 是 X11 那边才有的虚拟事件: Windows 上 Tk 直接报
        # ``bad event type or keysym "Iconify"``(实测), 所以这里咽掉 —— 有它的平台多一层
        # 保险, 没有的平台靠上面那条 Unmap。
        with suppress(tk.TclError):
            self._bind(toplevel, "<Iconify>", self._on_host_gone)
        # 宿主被移动/改尺寸 → 浮层就不再贴在控件下面了(用户截图里它正是停在旧位置上,
        # 还盖着旁边的程序), 直接收起 —— 原生下拉也是这个行为。
        self._bind(toplevel, "<Configure>", self._follow_anchor)

    def _follow_anchor(self, _event: tk.Event) -> None:
        """宿主窗口挪动/改尺寸 → 浮层跟着控件重算一次位置.

        **不收起**(试过: 布局期间宿主也会发 ``<Configure>``, 一收就被误踢; 用户报的是"浮层
        停在旧位置, 还盖着旁边的程序" —— 跟着走既解决了它, 又不会把正常的打开踢掉)。
        锚点已经没了就什么都不做。
        """
        if self.window is None:
            return
        with suppress(tk.TclError):
            self._place(
                int(self.anchor.winfo_rootx()),
                int(self.anchor.winfo_rooty()) + int(self.anchor.winfo_height()),
            )

    def _bind(
        self, widget: tk.Misc, sequence: str, handler: Callable[[tk.Event], None]
    ) -> None:
        """挂一个绑定并记下 id(收起时逐个解开, 不靠"窗口销毁会一起清掉")."""
        bind_id = widget.bind(sequence, handler, add="+")
        self._binds.append((widget, sequence, bind_id))

    def _unbind_outside(self) -> None:
        """解开上面那几处绑定(重复调用安全)."""
        pending, self._binds = self._binds, []
        for widget, sequence, bind_id in pending:
            with suppress(tk.TclError):  # 控件/窗口已销毁
                widget.unbind(sequence, bind_id)

    def _on_host_gone(self, _event: tk.Event) -> None:
        """宿主窗口被最小化/隐藏 → 收起浮层(它不会跟着宿主一起动).

        **不在这里当场销毁**: 这条绑定是宿主的 ``<Unmap>``(最小化/``withdraw`` 也在其中),
        那一刻窗口管理器正在处理这一轮的映射变化, 从回调里再销毁一个 ``overrideredirect``
        的附属窗口在 macOS 上直接段错误 —— 2026-10-03 的 macOS 分片就是这么没的(信号栈落在
        ``update_idletasks``, 进程被内核杀掉: coredumpy 没机会写 dump, pytest-cov 也没来得及
        落盘覆盖率)。推到下一拍(idle)再收: 判据(宿主已经 Unmap)没变, 但已经离开了 WM 的
        这次调用。
        """
        window = self.window
        if window is None:  # pragma: no cover - 已经收起了
            return
        window.after_idle(self.close)

    def _on_outside_click(self, _event: tk.Event) -> None:
        """窗口里点到别处 → 收起(不 ``break``: 那一下点击照常生效)."""
        self.close()

    def _on_anchor_destroy(self, _event: tk.Event) -> None:
        """锚点没了 → 收起(浮层是它的子窗口, 留着会变成一个孤儿浮层)."""
        unregister(self)


_ACTIVE: DropdownPopup | None = None


def active_dropdown() -> DropdownPopup | None:
    """当前开着的那张浮层(没有就是 None).

    守卫靠它量"浮层到底在哪、多高、有没有滚动条" —— 这些必须是可量的事实, 不能靠读代码
    记得。
    """
    return _ACTIVE if _ACTIVE is not None and _ACTIVE.is_open() else None


def register(popup: DropdownPopup) -> None:
    """登记"现在开着的是这张"并收起上一张(同一时刻只留一张)."""
    global _ACTIVE
    previous, _ACTIVE = _ACTIVE, popup
    if previous is not None and previous is not popup:
        previous.close()


def unregister(popup: DropdownPopup) -> None:
    """取消登记并收起(锚点销毁的路径用; 不是当前这张也照样收)."""
    global _ACTIVE
    if _ACTIVE is popup:
        _ACTIVE = None
    popup.close()


def _close_active() -> None:
    """收起当前那张浮层(没有就什么都不做)."""
    active = _ACTIVE
    if active is not None:
        unregister(active)


def is_open_for(menu: DropdownMenu) -> bool:
    """这张菜单的浮层现在开着吗(CTk 用它判断"再点一次是收起")."""
    active = active_dropdown()
    return active is not None and active.menu is menu


def open_for(menu: DropdownMenu, x: int, y: int) -> None:
    """替 ``DropdownMenu.open`` 打开自建的浮层(锚点不是控件时什么都不做)."""
    _close_active()
    anchor = getattr(menu, "master", None)
    if not isinstance(anchor, tk.Misc):
        return
    values = [str(value) for value in getattr(menu, "_values", [])]
    if not values:
        return
    palette = palette_for(anchor)
    if palette is None:  # pragma: no cover - 只有窗口已经崩掉时才会这样
        return
    popup = DropdownPopup(menu, anchor, palette, values)
    if not popup.show(x, y):
        popup.close()
        return
    register(popup)


def apply_combo_dropdown_fix() -> bool:
    """让所有下拉框都走本模块的浮层, 返回本次调用是否真的打了补丁(幂等).

    接 ``DropdownMenu`` 的三个入口, 与 :mod:`rendering` 给 ``CTkComboBox`` 接边框是同
    一套做法(进程级补丁, 导入 :mod:`archive_management.ui.widgets` 时打上):

    * ``open`` —— 换成自建浮层(限高 + 滚动条 + 按位置决定上下);
    * ``close`` —— CTk 在"再点一次"时直接调它(见 ``CTkComboBox._clicked``);
    * ``is_open`` —— 那一下的判据; 不接的话"再点一次"会被当成第一次, 于是点了又点。
    """
    global _APPLIED
    if _APPLIED:
        return False

    def open_wrapper(self: DropdownMenu, x: int, y: int) -> None:
        """原来的 ``open`` 只是把 ``tk.Menu`` 贴出去, 这里整个换掉."""
        open_for(self, x, y)

    def close_wrapper(self: DropdownMenu) -> None:
        """收起浮层(顺带清掉"当前这张"的登记)."""
        del self
        _close_active()

    def is_open_wrapper(self: DropdownMenu) -> bool:
        """浮层开着才算开着(原生菜单的 ``winfo_viewable`` 已经不适用)."""
        return is_open_for(self)

    DropdownMenu.open = open_wrapper
    DropdownMenu.close = close_wrapper
    DropdownMenu.is_open = is_open_wrapper
    _APPLIED = True
    return True
