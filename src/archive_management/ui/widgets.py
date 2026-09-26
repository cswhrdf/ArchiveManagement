"""CustomTkinter 通用构件.

``UiKit`` 集中创建控件并在主题切换时按调色板统一重绘(repaint),使
业务代码只面向展示模型与回调,不直接散落控件配色逻辑。

导入本模块即关闭 CustomTkinter 绘制层的"偶数取整"(详见
:mod:`archive_management.ui.rendering`),否则奇数尺寸的控件会丢掉
1px 右边框/下边框;同时给输入控件接上 Ctrl+Z 撤销(详见
:mod:`archive_management.ui.textundo`)。两件事都必须在创建控件前生效,
而所有创建窗口的模块最终都经 ``main_window`` 导入这里。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from contextlib import suppress
from typing import Literal

import customtkinter as ctk

from archive_management.ui.palette import Palette
from archive_management.ui.rendering import apply_border_rendering_fix
from archive_management.ui.textundo import apply_input_undo_support

apply_border_rendering_fix()
apply_input_undo_support()
_PaletteKey = str
_Repaint = Callable[..., None]
_Unsubscribe = Callable[[], None]

LabelStyle = Literal["primary", "body", "muted", "hint", "h2"]
ButtonStyle = Literal["accent", "danger", "ghost", "soft"]


class UiKit:
    """创建并登记控件,主题切换时重绘所有登记项.

    动态重建的控件(如列表卡片)应在销毁前调用 ``register`` 返回的退订
    函数, 避免重绘已销毁控件导致 ``TclError``。
    """

    def __init__(self) -> None:
        """初始化空的重绘登记表."""
        self._repaints: list[tuple[int, _Repaint]] = []
        self._active: set[int] = set()
        self._next_token = 0
        self._buttons: dict[ctk.CTkButton, ButtonStyle] = {}

    # -- 登记 ---------------------------------------------------------------

    def register(self, repaint: _Repaint) -> _Unsubscribe:
        """登记一个主题重绘回调, 返回退订函数(控件销毁前应调用)."""
        self._next_token += 1
        token = self._next_token
        self._repaints.append((token, repaint))
        self._active.add(token)

        def unsubscribe() -> None:
            self._active.discard(token)

        return unsubscribe

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

    def scroll_frame(
        self,
        parent: ctk.CTkBaseClass,
        *,
        bg_key: str,
        corner_radius: int = 0,
        scrollbar_key: str = "border",
    ) -> ctk.CTkScrollableFrame:
        """创建并登记一个随主题重绘的滚动容器.

        CustomTkinter 只在构造时把内层 canvas 的背景取为父容器当时的颜色,
        主题切换后不会自动更新, 因此这里显式用调色板重设 ``fg_color``,
        让列表区域的背景与卡片一起跟随主题(修复浅色/深色错位).
        """
        frame = ctk.CTkScrollableFrame(
            parent,
            fg_color="transparent",
            corner_radius=corner_radius,
            scrollbar_button_color="#314765",
        )
        self.register(lambda p: self._recolor_scroll(frame, p, bg_key, scrollbar_key))
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
            "hint": "text_hint",
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
        """用给定调色板重绘全部登记控件.

        已退订或已销毁(引发 ``TclError``)的控件会被安全跳过。
        """
        for token, repaint in list(self._repaints):
            if token not in self._active:
                continue
            try:
                repaint(palette)
            except tk.TclError:
                # 控件已被销毁或根窗口关闭: 忽略并退订, 防止持续报错.
                self._active.discard(token)

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

    @staticmethod
    def _recolor_scroll(
        frame: ctk.CTkScrollableFrame,
        palette: Palette,
        bg_key: str,
        scrollbar_key: str,
    ) -> None:
        # configure(fg_color=...) 会同时更新内层 Frame 与 canvas 的背景.
        frame.configure(
            fg_color=getattr(palette, bg_key),
            scrollbar_button_color=getattr(palette, scrollbar_key),
        )

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


def paint_button_enabled(
    button: ctk.CTkButton,
    palette: Palette,
    *,
    fg_color: str,
    text_color: str,
    hover_color: str,
    border_color: str | None = None,
) -> None:
    """把按钮画回可用态.

    同时写 ``text_color_disabled``: CustomTkinter 在禁用时**只**用它渲染文字,
    只改 ``text_color`` 是看不见效果的(这是一个很容易踩的坑)。
    """
    button.configure(
        fg_color=fg_color,
        hover_color=hover_color,
        text_color=text_color,
        text_color_disabled=palette.text_disabled,
        border_width=1 if border_color is not None else 0,
        border_color=palette.border if border_color is None else border_color,
    )


def paint_button_disabled(button: ctk.CTkButton, palette: Palette) -> None:
    """把按钮画成禁用态: 更暗的底色 + 更暗的字.

    "几乎一样"的禁用态等于没有禁用态 —— 用户会一直点它。这里三样一起压暗
    (底、字、描边), 与同一行的可用按钮形成明确对比。
    """
    button.configure(
        fg_color=palette.disabled_bg,
        hover_color=palette.disabled_bg,
        text_color=palette.text_disabled,
        text_color_disabled=palette.text_disabled,
        border_width=1,
        border_color=palette.disabled_border,
    )


def scrollbar_needed(
    content_height: int, viewport_height: int, *, slack: int = 2
) -> bool:
    """内容是否真的超出视口(纯函数, 便于单独测试).

    ``slack`` 是取整容差: 内容比视口高 1~2 像素是布局取整的常态, 不该因此立起
    一条拖不动的滚动条。
    """
    return content_height > viewport_height + slack


# 出现滚动条的门槛(像素): 溢出得比"取整误差"更明显才立起一条滑块。
#
# 两个方向用的门槛不一样, 但都是以"溢出"为口径:
#
# * 已显示: 只要装得下(容差 2px, 见 ``scrollbar_needed`` 的默认值)就收起。这里
#   **不能**再要求"比视口低 8px" —— 窗口按内容定高时内容高度恰好等于视口, 那个要求
#   永远达不到, 滚动条从此收不起来(设置窗口实测, 16 号评审)。
# * 已收起: 要溢出 8px 以上才出现, 免得一两个像素的抖动把滚动条来回抽。
#
# 这样也不会在两个状态之间来回翻: 滚动条出现会让画布变窄、内容只会更高, 收起会让
# 画布变宽、内容只会更矮 —— "该显示"在两种状态下都成立(反之亦然)。
_SCROLLBAR_HYSTERESIS = 8
# 重入标记: ``sync_scrollbar`` 自己会改几何, 而几何回调又会通知它。没有这道闸门
# 就会递归到 ``maximum recursion depth exceeded``(编辑标签页连点"添加标签"实测崩溃)。
_SYNCING_FLAG = "_am_scrollbar_syncing"
# 上一次决策(该不该显示).
#
# **不能拿 Tk 的映射状态当依据**: ``grid()`` 之后控件要等一轮几何才真正 mapped,
# 这中间读到"未显示"就会再 ``grid()`` 一次 —— 每次又触发新的 Configure, 于是自激
# 循环(实测: 打开编辑标签弹窗时滚动条反复抽动, 判定被调用上百次)。
_SHOWN_FLAG = "_am_scrollbar_shown"
# 自定义滚动条重画补丁是否已打过(见 :func:`apply_scrollbar_visibility_fix`).
_APPLIED_SCROLLBAR_FIX = False


def _no_flush() -> None:
    """代替画布上的强制刷新(形参与原方法一致, 什么都不做)."""


def apply_scrollbar_visibility_fix() -> bool:
    """让"收起滚动条"真的生效: 重画时不再强制刷新事件队列(幂等).

    背景(2026-09-27 实测, 不是推断): ``CTkScrollbar._draw()`` 结尾有一句
    ``self._canvas.update_idletasks()``, 而画布每次滚动/尺寸变化都会回叫
    ``set()`` → ``_draw()`` —— 那次强制刷新会把 Tk 里排队的映射动作执行掉, 我们刚
    ``grid_forget()`` 掉的滚动条当场又被映射回来。于是"内容装得下就收起"在真实窗口里
    从来收不起来: 评审截图里发现页两个列表、主页列表、定时任务窗口、编辑标签弹窗右侧
    都立着那条常驻灰条;修前实测该区域灰色像素 3706, 修后 0。

    去掉这句只影响"是否在本调用里立刻重画" —— 画布本来就会在下一个 idle 重画,
    悬停配色等观感差别肉眼不可见。
    """
    global _APPLIED_SCROLLBAR_FIX
    if _APPLIED_SCROLLBAR_FIX:
        return False
    original_draw = ctk.CTkScrollbar._draw

    def draw(self: ctk.CTkScrollbar, no_color_updates: bool = False) -> None:
        canvas = self._canvas
        canvas.update_idletasks = _no_flush
        try:
            original_draw(self, no_color_updates)
        finally:
            canvas.__dict__.pop("update_idletasks", None)

    ctk.CTkScrollbar._draw = draw
    _APPLIED_SCROLLBAR_FIX = True
    return True


# 导入即生效: 补丁必须打在**任何滚动条被创建之前**(与绘制取整补丁同一套约定).
apply_scrollbar_visibility_fix()


def _hide_scrollbar(scrollbar: ctk.CTkScrollbar) -> None:
    """把滚动条从布局里摘掉(内容装得下时).

    用 ``grid_forget`` 而不是 ``grid_remove``: 后者的处理是 Tk 自己的方法,
    ``CTkBaseClass`` 不知道我们摘过控件, 它记下的最后一次几何调用会在缩放/外观变化时
    被重放(见 ``CTkBaseClass._set_scaling``) —— 已经收起的滚动条会被重新摆回去;而
    ``grid_forget`` 会把那条记录清成 ``None``。
    """
    scrollbar.grid_forget()


def _pin_scrollbar_height(scrollbar: ctk.CTkScrollbar) -> None:
    """把滚动条的高度请求压到 1px.

    CTk 的滚动条默认请求 200px 高, 而它和画布在同一行(``grid(row=1, column=1)``):
    一旦显示, 这一行就被撑高到 200, 视口跟着变高, 内容又装得下了 → 收起 → 视口变矮
    → 又溢出 → 再显示 …… 实测就在边界上无限抽动(编辑标签弹窗里最明显)。

    压到 1px 后行高只由画布决定; 滚动条自己靠 ``sticky="nsew"`` 被拉伸到行高,
    外观不变。
    """
    scrollbar._desired_height = 1
    scrollbar._set_dimensions(height=1)


def _show_scrollbar(frame: ctk.CTkScrollableFrame, scrollbar: ctk.CTkScrollbar) -> None:
    """把滚动条放回布局.

    ``grid()`` 不带参数在 Tk 里只是查询, 不会把 ``grid_remove`` 掉的控件映射回来;
    这里按 ``CTkScrollableFrame`` 自己的摆放参数(第 1 行第 1 列)放回去。
    """
    parent = getattr(frame, "_parent_frame", None)
    if parent is None:  # pragma: no cover - CTk 一定提供
        scrollbar.grid()
        return
    spacing = int(parent.cget("corner_radius")) + int(parent.cget("border_width"))
    border = int(getattr(frame, "_border_width", 0))
    scrollbar.grid(row=1, column=1, sticky="nsew", padx=(0, border + 1), pady=spacing)
    _pin_scrollbar_height(scrollbar)


def sync_scrollbar(frame: ctk.CTkScrollableFrame) -> bool:
    """按内容高度决定滚动条是否出现, 返回它当前是否可见.

    CustomTkinter 的滚动条一直占位: 只有三行内容时右侧仍立着一条滑块, 视觉上
    在说"下面还有内容"。这里按"内容高度 vs 视口高度"把它收起来, 溢出时才显示。

    三条保命规则(都来自实测): 重入时直接返回; 边界上带滞回; 只在这里改几何,
    调用方(``auto_scrollbar``)把几何事件合并到一次 idle。
    """
    canvas = getattr(frame, "_parent_canvas", None)
    scrollbar = getattr(frame, "_scrollbar", None)
    if canvas is None or scrollbar is None:  # pragma: no cover - CTk 一定提供这两者
        return False
    if getattr(frame, _SYNCING_FLAG, False):
        # 正在同步时又被通知: 这是本次调整自己引出的 Configure, 忽略以免递归.
        return False
    try:
        setattr(frame, _SYNCING_FLAG, True)
        box = canvas.bbox("all")
        viewport = int(canvas.winfo_height())
        shown = getattr(frame, _SHOWN_FLAG, None)
        if shown is None:
            # 首次判定的起点是"**已显示**": ``CTkScrollableFrame`` 建容器时一定把滚动条
            # 摆进布局(第 1 行第 1 列), 而"是否已映射"在窗口还没画出来、或所在分区/子页
            # 是隐藏的时候读到的总是 0 —— 拿它当初值就会得出"已经收起了"的错误结论,
            # 之后内容装得下时我们会因为"决策没变"而**什么都不做**, 那条滚动条就永远
            # 立在界面上(评审截图里主页列表、定时任务窗口、编辑标签弹窗、发现页两个
            # 列表右侧的常驻灰条都是这一条)。
            shown = True
            setattr(frame, _SHOWN_FLAG, shown)
        content = 0 if box is None else int(box[3]) - int(box[1])
        # 已显示时按"装得下就收起"判(默认容差), 已收起时要确实溢出才显示(滞回).
        needed = scrollbar_needed(
            content,
            viewport,
            slack=2 if shown else _SCROLLBAR_HYSTERESIS,
        )
        if needed == shown:
            # 决策没变就什么都不做: 这正是防自激的关键(改几何会再引出一轮通知).
            return needed
        setattr(frame, _SHOWN_FLAG, needed)
        if needed:
            _show_scrollbar(frame, scrollbar)
        else:
            _hide_scrollbar(scrollbar)
    except tk.TclError:  # 控件已销毁: 交给调用方
        return False
    finally:
        setattr(frame, _SYNCING_FLAG, False)
    return needed


def auto_scrollbar(frame: ctk.CTkScrollableFrame) -> None:
    """让滚动条随内容溢出与否出现/消失(连续的几何变化合并成一次判定).

    `CTkScrollableFrame` 自己会绑定 `<Configure>` 更新 scrollregion, 这里再挂一个
    (``add="+"`` 不覆盖它的回调)。Tk 的 `<Configure>` 不会从子控件冒泡到父控件,
    因此不需要再过滤 ``event.widget``。
    """
    canvas = getattr(frame, "_parent_canvas", None)
    scrollbar = getattr(frame, "_scrollbar", None)
    if scrollbar is not None:
        # 一开始就压住它的高度请求: 否则首次显示时那一行会先被撑高一次.
        _pin_scrollbar_height(scrollbar)
    pending: dict[str, str | None] = {"job": None}

    def sync_now() -> None:
        pending["job"] = None
        sync_scrollbar(frame)

    def request(_event: object = None) -> None:
        """把这一轮几何变化合并到一次 idle 判定.

        直接在 ``<Configure>`` 里同步判定会在回调里改几何、又通知自己 —— 编辑标签页
        连点"添加标签"实测递归到崩溃(``maximum recursion depth exceeded``)。
        """
        job = pending["job"]
        if job is not None:
            with suppress(tk.TclError):
                frame.after_cancel(job)
        try:
            pending["job"] = frame.after_idle(sync_now)
        except tk.TclError:  # 控件已销毁: 不再排队
            return

    frame.bind("<Configure>", request, add="+")
    if canvas is not None:
        canvas.bind("<Configure>", request, add="+")
    # 首帧: 尺寸还没量出来时先按"不溢出"收起, 真实的 Configure 会立刻纠正.
    request()


def track_wraplength(
    container: ctk.CTkBaseClass,
    label: ctk.CTkLabel,
    *,
    inset: int = 0,
    minimum: int = 240,
) -> None:
    """让成段说明文字跟着容器的实际宽度换行.

    写死的 ``wraplength`` 在宽窗口里会提前折行: 一句话被劈成"半行 + 半行", 读起来
    像被截断("…候选名一律忽略," 后面直接接下一句), 第 12 号评审就是这个问题。

    容器的销毁过程也会发 ``<Configure>``, 那时标签已经没了 —— 不兜住 ``TclError``
    就会在 stderr 留下一串 ``invalid command name ...`` (报告里会挂到别的用例上)。
    """
    label.configure(wraplength=minimum)

    def resize(event: tk.Event) -> None:
        try:
            label.configure(wraplength=max(minimum, int(event.width) - inset))
        except tk.TclError:  # 控件已销毁: 回调作废
            return

    container.bind("<Configure>", resize, add="+")


# 悬停多久才弹出提示: 短到"停一下就有", 长到鼠标划过不会到处闪。
_TOOLTIP_DELAY_MS = 350
# 提示与鼠标控件的间距(像素)与内衬。
_TOOLTIP_GAP = 6
_TOOLTIP_PAD = (10, 5)
# 提示是浮在界面之上的临时层, 固定用深色底 + 浅色字(两种主题下都读得清, 也是
# 桌面软件里 tooltip 的通行做法), 因此不随调色板重绘。
_TOOLTIP_BG = "#1f2937"
_TOOLTIP_FG = "#f4f7fb"
_TOOLTIP_BORDER = "#3b4a60"


def _tooltip_position(anchor: ctk.CTkBaseClass, window: tk.Toplevel) -> str:
    """算出提示窗口的位置: 优先在控件下方, 下方放不下就改到上方."""
    window.update_idletasks()
    left = max(0, int(anchor.winfo_rootx()))
    height = int(window.winfo_reqheight())
    below = int(anchor.winfo_rooty()) + int(anchor.winfo_height()) + _TOOLTIP_GAP
    if below + height > int(anchor.winfo_screenheight()):
        below = int(anchor.winfo_rooty()) - height - _TOOLTIP_GAP
    return f"+{left}+{max(0, below)}"


def _open_tooltip(anchor: ctk.CTkBaseClass, message: str) -> tk.Toplevel | None:
    """建出提示窗口并放到锚点旁边; 锚点已销毁时返回 None."""
    window = tk.Toplevel(anchor)
    window.overrideredirect(True)
    window.attributes("-topmost", True)
    tk.Label(
        window,
        text=message,
        justify="left",
        background=_TOOLTIP_BG,
        foreground=_TOOLTIP_FG,
        padx=_TOOLTIP_PAD[0],
        pady=_TOOLTIP_PAD[1],
        highlightthickness=1,
        highlightbackground=_TOOLTIP_BORDER,
    ).pack()
    try:
        window.geometry(_tooltip_position(anchor, window))
    except tk.TclError:  # pragma: no cover - 锚点在弹出途中被销毁
        window.destroy()
        return None
    return window


def attach_tooltip(widget: ctk.CTkBaseClass, text: str | Callable[[], str]) -> None:
    """给控件挂一个悬停提示.

    技术标识(备份目录名这类"slug + 短哈希"、脚本临时路径)对用户既改不了也用不上,
    摆在正文里只会挤掉真正要读的字; 收进悬停提示既让正文干净, 又不把信息藏起来。

    ``text`` 可以是字符串, 也可以是"显示时求值"的函数 —— 详情页的标签是复用的,
    同一条提示会随当前选中的游戏变化。

    进出与销毁都要能安全收场: 鼠标移出、点下、控件销毁时都要撤掉计时器与提示窗口,
    销毁期间的 ``TclError`` 一律咽掉(否则会在 stderr 留下一串 ``invalid command
    name ...``, 报告里会挂到别的用例上)。
    """
    window: tk.Toplevel | None = None
    job: str | None = None

    def cancel() -> None:
        """撤掉已排队的弹出任务(没有排队就什么都不做)."""
        nonlocal job
        pending, job = job, None
        if pending is None:
            return
        with suppress(tk.TclError):
            widget.after_cancel(pending)

    def hide() -> None:
        """收起提示窗口."""
        nonlocal window
        cancel()
        current, window = window, None
        if current is not None:
            with suppress(tk.TclError):
                current.destroy()

    def show() -> None:
        """弹出提示(文本为空时不出一个空框)."""
        nonlocal window, job
        job = None
        message = text() if callable(text) else text
        if message:
            window = _open_tooltip(widget, message)

    def schedule(_event: tk.Event) -> None:
        """鼠标进入: 稍后才弹, 避免"鼠标路过"也弹一层提示."""
        nonlocal job
        hide()
        job = widget.after(_TOOLTIP_DELAY_MS, show)

    widget.bind("<Enter>", schedule, add="+")
    widget.bind("<Leave>", lambda _event: hide(), add="+")
    widget.bind("<Button-1>", lambda _event: hide(), add="+")
    widget.bind("<Destroy>", lambda _event: hide(), add="+")
