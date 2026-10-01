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
from dataclasses import dataclass
from typing import Literal

import customtkinter as ctk

from archive_management.ui.keyboard import (
    install_keyboard_support,
    remember_palette,
    set_focus_paint,
)
from archive_management.ui.palette import Palette
from archive_management.ui.rendering import apply_border_rendering_fix
from archive_management.ui.textfit import MeasurableFont, fit_path, fit_text
from archive_management.ui.textundo import apply_input_undo_support

apply_border_rendering_fix()
apply_input_undo_support()
# 键盘可用性(Tab 可达 / 焦点环 / 空格回车激活)也在类上打一次补丁: 控件是就地创建的
# 还是走 UiKit 都自动接上, 调用点不需要记得做任何事(见 ui.keyboard)。
install_keyboard_support()
_PaletteKey = str
_Repaint = Callable[..., None]
_Unsubscribe = Callable[[], None]

LabelStyle = Literal["primary", "body", "muted", "hint", "h2"]
ButtonStyle = Literal["accent", "danger", "danger_soft", "ghost", "soft"]
# **动作性质**: "这个按钮是干什么的" —— 业务代码只声明性质, 穿什么颜色由
# :data:`ACTION_STYLES` 一处决定(I-10: 重要功能键的配色也要能断言)。
# ``primary`` = 这一屏最该做的事; ``destructive`` = 破坏性动作; 其余是次要动作。
ActionKind = Literal["primary", "secondary", "destructive", "destructive_soft"]

# 全部动作性质(顺序无关, 只用于"把实测颜色反推回性质"这类遍历)。
ACTION_KINDS: tuple[ActionKind, ...] = (
    "primary",
    "secondary",
    "destructive",
    "destructive_soft",
)

# 性质 -> 样式的**唯一定义处**: 改这里就能同时影响所有同类按钮。
ACTION_STYLES: dict[ActionKind, ButtonStyle] = {
    "primary": "accent",
    "secondary": "ghost",
    "destructive": "danger",
    "destructive_soft": "danger_soft",
}

# 全部按钮样式(顺序无关, 只用于"把实测颜色反推回样式"这类遍历)。
BUTTON_STYLES: tuple[ButtonStyle, ...] = (
    "accent",
    "danger",
    "danger_soft",
    "ghost",
    "soft",
)
# **破坏性动作允许的样式范围**: 只能穿危险色实底或危险色描边(或处于禁用态)。
# "删除类按钮的颜色必须落在这个范围内"这件事只有这一处定义 —— 守卫按它判定,
# 新加一个删除按钮却上了主色/中性色时会直接变红, 不需要有人去维护文案表。
DESTRUCTIVE_STYLES: tuple[ButtonStyle, ...] = ("danger", "danger_soft")


# 样式 -> 性质的反查(把实测颜色/样式反推回性质时用; 与 ACTION_STYLES 互为反正)。
_KIND_FOR_STYLE: dict[ButtonStyle, ActionKind] = {
    mapped: kind for kind, mapped in ACTION_STYLES.items()
}


def style_for(kind: ActionKind) -> ButtonStyle:
    """把动作性质翻译成样式名(业务代码只该写性质)."""
    return ACTION_STYLES[kind]


# **聚焦时的换色规则**(用户实测: "聚焦框在实底按钮上看起来只是按钮缩小了一圈"):
# 主色/危险色实底上任何亮色环都看不出(实测 1.27:1), 所以聚焦时把这类按钮换成对应的
# 软底样式 —— 换完之后那一档亮色(深色主题)/深色(浅色主题)环立刻有 **10:1 以上**,
# 而且**两套主题各自只剩一个抢眼的环色**(见 ``docs/testing.md``)。
# 只换配色、不换功能: 软底样式仍在 ``DESTRUCTIVE_STYLES`` 允许范围内。
FOCUS_STYLE: dict[str, ButtonStyle] = {
    "accent": "soft",
    "danger": "danger_soft",
}


def focus_paint(palette: Palette, style: str) -> dict[str, str]:
    """聚焦时该写给控件的颜色(没有对应软底样式的样式返回空表 = 只换焦点环)."""
    mapped = FOCUS_STYLE.get(style)
    if mapped is None:
        return {}
    colors = button_colors(palette, mapped)
    return {
        "fg_color": colors.fg,
        "hover_color": colors.hover,
        "text_color": colors.text,
    }


@dataclass(frozen=True)
class ButtonColors:
    """一个按钮样式的配色(``border`` 为 None 表示不描边)."""

    fg: str
    hover: str
    text: str
    border: str | None = None


def button_colors(palette: Palette, style: ButtonStyle) -> ButtonColors:
    """按钮样式的**唯一定义处**: 每种样式用哪些 token 只在这里写一次.

    走 ``UiKit`` 登记的按钮与对话框/页脚里就地创建的按钮都从这里取色, 于是
    "破坏性动作用危险色"只需要改这一处(24 号评审: 删除不能与导入/保存共用
    同一个安全色主按钮)。
    """
    if style == "accent":
        return ButtonColors(
            palette.accent, palette.accent_soft_border, palette.accent_text
        )
    if style == "danger":
        # 悬停色用 item_hover: 调色板里没有"更深的危险色", 而拿主色的深绿去
        # 悬停红按钮会闪出一层青, 反而更怪。
        return ButtonColors(palette.danger, palette.item_hover, palette.danger_text)
    if style == "soft":
        return ButtonColors(
            palette.accent_soft, palette.accent_soft_border, palette.accent_soft_text
        )
    if style == "danger_soft":
        # "描边 + 危险色文字": 破坏性动作在同一行里不该比主操作更抢眼(10 号评审)。
        return ButtonColors(
            palette.raised, palette.item_hover, palette.danger, palette.danger
        )
    return ButtonColors(
        palette.raised, palette.item_hover, palette.text_body, palette.border
    )


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
        # 最近一次 apply 的调色板: 按钮一创建就把"当前这套色"记在它和它所在的窗口上,
        # 焦点环才有颜色可用(见 keyboard.ring_color)。
        self._palette: Palette | None = None

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
            # 构造时就取调色板当前值: 原来硬写的是**深色主题**那一档("#314765"), 浅色主题下
            # 新建的滚动区会带着错色的滚动条直到下一次重绘(原先那条静态检查 C9101 拓的就是
            # 这处, 已撤, 见 PLAN §17.3)。
            # `_palette` 为 None 只出现在 `apply()` 之前的极短窗口里, 那时用兜底色。
            scrollbar_button_color=(
                getattr(self._palette, scrollbar_key)
                if self._palette is not None
                else _SCROLLBAR_FALLBACK
            ),
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
        kind: ActionKind | None = None,
        command: Callable[[], None] | None = None,
        width: int = 100,
        height: int = 34,
        corner_radius: int = 7,
    ) -> ctk.CTkButton:
        """创建并登记一个指定样式(或动作性质)的按钮.

        ``kind`` 是推荐写法: 业务代码声明"这个按钮是干什么的", 颜色由一处映射决定;
        ``style`` 保留给"就是想要某个具体样子"的少数场合(两个都给了以 ``kind`` 为准)。
        """
        if kind is not None:
            style = style_for(kind)
        button = ctk.CTkButton(
            parent,
            text=text,
            command=command,
            width=width,
            height=height,
            corner_radius=corner_radius,
            font=ctk.CTkFont(size=13, weight="bold"),
        )
        button._action_kind = kind
        self._buttons[button] = style
        if self._palette is not None:
            remember_palette(button, self._palette)
        self.register(lambda p, w=button, s=style: self._paint_button(w, p, s))
        return button

    # -- 重绘 ---------------------------------------------------------------

    def apply(self, palette: Palette) -> None:
        """用给定调色板重绘全部登记控件.

        已退订或已销毁(引发 ``TclError``)的控件会被安全跳过。
        """
        self._palette = palette
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
        # 配色只有一处(button_colors), 这里不重复写颜色; 禁用态也必须一起保留,
        # 否则切主题会把禁用按钮重新画成亮的。
        paint_button_state(button, palette, style)

    def repaint_button(self, button: ctk.CTkButton, palette: Palette) -> None:
        """按按钮**当前的 state**重画它(样式取登记时的那一个).

        可用性变化后调用: 状态与配色必须一起改, 只改 state 会让禁用按钮留着
        主色/危险色的底(49 号评审)。未登记的按钮直接忽略。
        """
        style = self._buttons.get(button)
        if style is None:
            return
        paint_button_state(button, palette, style)


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

    这里也是按钮记下"当前这套色"的地方: 焦点环的强调色由它决定(见
    :func:`keyboard.ring_color`), 所以不需要每个对话框再传一次调色板。
    """
    remember_palette(button, palette)
    button.configure(
        fg_color=fg_color,
        hover_color=hover_color,
        text_color=text_color,
        text_color_disabled=palette.text_disabled,
        border_width=1 if border_color is not None else 0,
        border_color=palette.border if border_color is None else border_color,
    )


def paint_button_style(
    button: ctk.CTkButton,
    palette: Palette,
    style: ButtonStyle,
) -> None:
    """把**没走 UiKit 登记**的按钮按样式画好.

    对话框里成对的 取消/确认、窗口页脚上的按钮都是就地创建的(随窗口一起销毁,
    不需要登记重绘), 但配色仍然必须来自 :func:`button_colors` —— 否则就会出现
    "取消按钮留着 CustomTkinter 默认蓝"这种漏网(48 号评审: 取消永远是次色)。

    顺便把**样式**与**动作性质**记在按钮上: 对话框的"回车 = 主操作"靠它找到主按钮
    (见 :func:`keyboard.primary_button`), 守卫也靠它把实测颜色反推回性质。
    """
    button._button_style = style
    button._action_kind = _KIND_FOR_STYLE.get(style)
    colors = button_colors(palette, style)
    paint_button_enabled(
        button,
        palette,
        fg_color=colors.fg,
        text_color=colors.text,
        hover_color=colors.hover,
        border_color=colors.border,
    )


def paint_button_disabled(button: ctk.CTkButton, palette: Palette) -> None:
    """把按钮画成禁用态: 更暗的底色 + 更暗的字.

    "几乎一样"的禁用态等于没有禁用态 —— 用户会一直点它。这里三样一起压暗
    (底、字、描边), 与同一行的可用按钮形成明确对比。

    禁用的按钮同时被**摘出 Tab 链**(见 :func:`keyboard.sync_tab_chain`): 键盘用户
    不该把 Tab 浪费在一个按不动的控件上。
    """
    remember_palette(button, palette)
    button.configure(
        fg_color=palette.disabled_bg,
        hover_color=palette.disabled_bg,
        text_color=palette.text_disabled,
        text_color_disabled=palette.text_disabled,
        border_width=1,
        border_color=palette.disabled_border,
    )


def button_is_disabled(button: ctk.CTkButton) -> bool:
    """按钮当前是否禁用(读 Tk 的 state, 而不是各自记的布尔量)."""
    return str(button.cget("state")) == "disabled"


def paint_button_state(
    button: ctk.CTkButton,
    palette: Palette,
    style: ButtonStyle,
) -> None:
    """按按钮**当前的 state**上色: 禁用就压暗, 否则按样式画.

    可用性一变就要调它 —— 只 ``configure(state=...)`` 而不重绘的按钮会留着原来的
    底色: 主色/危险色的按钮被禁用后仍然亮着, 用户会一直点它(49 号评审: 主窗口与
    游戏管理窗口的禁用按钮看起来完全可点)。主题重绘也走这里, 因此"切主题把禁用
    按钮画回亮的"不会再发生。
    """
    if button_is_disabled(button):
        paint_button_disabled(button, palette)
        return
    paint_button_style(button, palette, style)


def card_surface_colors(
    palette: Palette, *, selected: bool, hovered: bool
) -> tuple[str, str]:
    """卡片/列表行的 (底色, 描边色): **选中 > 悬停 > 常规**.

    全应用只允许这一条规则 —— 详情页的备份卡片、主页的列表行与海报卡、发现页的候选/
    目录行、游戏管理窗口的位置行都走它。两件事必须同时成立:

    * **选中的表达是"浅底 + 描边"**, 不是主按钮那种实心强调色: "我选中了谁"与
      "哪里能点"是两件事(见 :meth:`Palette.selection_colors`);
    * **悬停不能盖掉选中** —— 悬停在一张已选中的卡片上时, 它看起来必须还是选中的,
      否则鼠标一划过就"忘了"刚才选了谁。
    """
    if selected:
        return palette.selection_colors(True)
    if hovered:
        return palette.card_hover, palette.card_border
    return palette.selection_colors(False)


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


def track_fit(
    container: ctk.CTkBaseClass,
    label: ctk.CTkLabel,
    lines: tuple[str, ...],
    *,
    path: bool = False,
    inset: int | Callable[[], int] = 0,
    max_lines: int = 1,
) -> None:
    """把 ``lines`` 按**容器**的实际宽度裁好填进 ``label``, 宽度变化时重裁.

    为什么宽度取容器而不是 label 自己: label 若是被内容撑开的(``pack(side="left")``、
    ``grid(sticky="w")``), 它自己的宽度就等于文字宽度 —— 把文字裁短会让它跟着变窄,
    下一轮又裁得更短, 越裁越短。被 ``fill``/``sticky="ew"`` 拉伸的那个容器宽度与文本
    无关, 才算得稳。

    宽度还没量出来(<=1)或没变时都不动, 避免"改文本 -> 新事件 -> 再裁"互相追。
    裁剪用 :func:`fit_text`(尾部省略号), 传 ``path=True`` 换 :func:`fit_path`
    (中间省略, 保留最有用的尾段)。``max_lines > 1`` 时先自己断行到上限再补省略号 ——
    **不能靠 ``wraplength`` 顶这半边**: Tk 只在空格处断, 一整段没有空格的中日韩文字
    实测会被摆在 1038px 的一行里(容器只有 815px), 照样硬裁。
    ``inset`` 可以给个函数 —— 前面的小标题/图标宽度要等控件建好才知道。
    """

    def budget() -> int:
        gap = inset() if callable(inset) else inset
        return int(container.winfo_width()) - gap

    def clip(text: str, width: int) -> str:
        font = label.cget("font")
        if path:
            return fit_path(text, font, width)
        return fit_text(text, font, width, max_lines=max_lines)

    state = {"width": 0}

    def resize(_event: object = None) -> None:
        try:
            width = budget()
            if width <= 1 or width == state["width"]:
                return
            state["width"] = width
            shown = [clip(line, width) for line in lines]
            label.configure(text="\n".join(shown))
            # 真的裁掉了就把完整文本挂成悬停提示: 省略号只是"还有下文"的记号,
            # 看不到下文就等于静默截断(I-3)。宽回来以后又完整了则把提示撤掉 ——
            # 提示与可见文字一样的时候只会碍事。
            sync_tooltip(label, full="\n".join(lines), shown="\n".join(shown))
        except tk.TclError:  # 控件已销毁: 回调作废
            return

    container.bind("<Configure>", resize, add="+")


# 成段说明的 wraplength 下限: 真实宽度要等布局完成才知道, 这个值只负责"还没量出来时"的落位
# (也能挡住"长文本一开始就要个上千像素"把容器顶宽)。公开导出名是为了让守卫能引用它,
# 不要在调用点再写一遍 240。
WRAPLENGTH_MINIMUM = 240


def _measured_width(widget: ctk.CTkBaseClass) -> int:
    """控件的实得宽度; 已经销毁时返回 0.

    销毁过程也会发 ``<Configure>``, 那时 ``winfo_width()`` 会抛 ``TclError`` —— 让它
    漏出去, stderr 里就是一堆 ``bad window path name``(报告里会挂到别的用例上)。
    """
    try:
        return int(widget.winfo_width())
    except tk.TclError:
        return 0


def _write_wraplength(
    label: ctk.CTkLabel,
    budget: int,
    on_change: Callable[[], None] | None,
) -> None:
    """把宽度写进标签, 写成功后再响一次 ``on_change``.

    控件已经销毁时(销毁过程也会发 ``<Configure>``)什么也不做 —— 让 ``TclError`` 漏出去
    会在 stderr 留下一串 ``bad window path name`` (报告里会挂到别的用例上)。
    """
    try:
        label.configure(wraplength=budget)
    except tk.TclError:
        return
    if on_change is not None:
        on_change()


def track_wraplength(
    container: ctk.CTkBaseClass,
    label: ctk.CTkLabel,
    *,
    inset: int = 0,
    minimum: int = WRAPLENGTH_MINIMUM,
    initial: int | None = None,
    on_change: Callable[[], None] | None = None,
) -> None:
    """让成段说明文字跟着**标签自己分到的宽度**换行.

    写死的 ``wraplength`` 在宽窗口里会提前折行: 一句话被劈成"半行 + 半行", 读起来
    像被截断("…候选名一律忽略," 后面直接接下一句), 第 12 号评审就是这个问题。

    上限必须取**标签自己的宽度**而不能只看容器: 同一条里还有别的控件时(监控目录页那
    一行摆着四个按钮, 占掉约 350px), 按容器宽度算出来的 ``wraplength`` 会比标签实际分到
    的宽度还大 —— 文字按那个过宽的预算排成**一行**, 再在标签边界处被 Tk 硬裁: 既不换行
    也没有省略号, 后半句直接看不到(实测: 容器 1312 / 标签 928 / wraplength 1122 → 尾部
    约 8 个字永远读不到)。取小值还顺带保证"标签的请求宽度 ≤ 它分到的宽度", 于是重排不会
    把容器顶宽, 也就不会来回震荡。

    触发源挂在**容器与标签自己**上。两边都要: 同一条里别的控件变宽时(实测: 把设置窗口右列
    的下拉框撑宽 40px)面板宽度一动不动, 说明那一格却从 244 掉到 204 —— 那时候一个事件都不
    来, 只有标签自己会发。

    判定里读的**必须是实得宽度**, 不能读事件带的值, 踩过两次:

    * 标签的 ``bind`` 会同时挂到内层 ``tkinter.Label`` 上, 而那个控件的宽度**就是文字
      宽度**(由 wraplength 反推), 拿事件里的宽度当上限会让说明越排越窄(实测 27 号对话框
      里从 468 一路掉到下限 240);
    * 绑在顶层窗口上时, Tk 会把**所有子控件**的 ``<Configure>`` 也送进这个绑定(子控件的
      bindtags 里带着顶层窗口的路径), 宽度是那个子控件的(实测 24/33/100), 同样把说明压
      到下限。

    真正的判定**延后到 idle 再算**, 而且同一轮只算一次: 事件回调里读到的宽度是**旧值**
    (事件先到、几何后落)。混着新旧两个读数去写 wraplength, 会写下一个此刻并不成立的值;
    对"内容撑高的容器 + 按需滚动条"这种回路来说(差 12px 就能让正文多一行 → 滚动条出现
    → 宽度又变回去), 那就是死循环 —— 实测这样会在 240/252 之间来回写**两千多次**, 界面
    直接抽住。等布局停下来再量, 两个读数来自同一瞬间, 写下去的值自洽: 宽度没变就不再写
    (见 ``apply`` 里那句), 而变窄只会让文字更高、不会反过来把上限顶大, 因此收敛。
    标签那一侧的事件也是这么被压住的: 换行改了高度 → 标签再发 ``<Configure>`` → 再量一次
    得到同一个宽度 → 不写(否则就是来回写)。

    容器或标签的销毁过程也会发 ``<Configure>``, 那时控件已经没了 —— 不兜住 ``TclError``
    就会在 stderr 留下一串 ``invalid command name ...`` (报告里会挂到别的用例上)。

    ``initial`` 是"还没量出可用宽度之前"的落位宽度(不给就取 ``minimum``): 布局从它开始,
    量准之后才被真实宽度取代。它只影响**窗口刚建起来那一瞬间** —— 高度按内容算的窗口
    会先按它排一次; 落位与真值差得多, 窗口就会先长/短一下再弹回去(给个与面板宽度接近的
    值就看不出来)。

    ``on_change`` 只在 wraplength **真的变了**之后响一次(宽度不变不会响): 给"高度按内容
    算"的窗口用 —— 换行变了行数就变了, 高度得跟着重算, 否则底部会空出一块(设置窗口
    就是这么用的)。
    """
    start = minimum if initial is None else max(minimum, initial)
    state = {"width": start, "pending": False}

    def apply(budget: int) -> None:
        """把 wraplength 落到 [minimum, budget] 上(宽度没变就不动)."""
        budget = max(minimum, budget)
        if budget == state["width"]:
            return  # 宽度没变就不动: 否则"改文本 → 新 Configure → 再改"会互相追
        state["width"] = budget
        _write_wraplength(label, budget, on_change)

    def measure() -> None:
        """按**同一瞬间**量到的容器宽度与标签宽度定 wraplength."""
        own = _measured_width(label)
        room = _measured_width(container) - inset
        if own > 1 and room > 1:
            room = min(room, own)
        apply(room if room > 1 else minimum)

    def run() -> None:
        state["pending"] = False
        measure()

    def request(_event: tk.Event | None = None) -> None:
        """收到 <Configure>: 排一次 idle 判定(已经排了就合并, 不再排)."""
        if state["pending"]:
            return
        state["pending"] = True
        try:
            container.after_idle(run)
        except tk.TclError:  # 容器已销毁
            state["pending"] = False
            return

    label.configure(wraplength=start)
    container.bind("<Configure>", request, add="+")
    # 标签自己也绑: 同一格里**别的控件**变宽时容器可以一动不动, 而这一格已经窄了 —— 那时
    # 只有标签会发 ``<Configure>``(实测: 把设置窗口右列的下拉框撑宽 40px, 面板宽度仍是
    # 448, 说明那一格从 244 掉到 204, 容器一个事件都没有)。这么绑之所以安全, 是因为
    # 判定读的是**实得宽度**而不是事件里的值, 而且宽度没变就不写: 标签因换行改了高度而
    # 再发的事件只会白跑一次 idle, 写不出新值(来回写的回路要"每次量都得到不同的宽度"
    # 才成立, 而 ``sticky="ew"`` 之后标签宽度等于格子宽度, 与文字无关)。
    label.bind("<Configure>", request, add="+")


# 兜底: `UiKit.apply()` 之前建出来的滚动区拿不到调色板, 那时先用深色主题那一档
# (窗口还没画出来, 下一次 apply 就换掉了)。写成具名常量: 静态检查只拦"调用里随手写的颜色"。
_SCROLLBAR_FALLBACK = "#314765"

# 悬停多久才弹出提示: 短到"停一下就有", 长到鼠标划过不会到处闪。
# 提示文案记在控件上的属性名: 让"省了尾巴的地方到底挂没挂提示"能量出来(I-3)。
_TOOLTIP_ATTR = "_archive_tooltip"
_TOOLTIP_DELAY_MS = 350  # 提示与鼠标控件的间距(像素)与内衬。
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


def tooltip_text(widget: ctk.CTkBaseClass) -> str:
    """该控件挂着的悬停提示文案(没有提示就返回空串).

    被省略号截掉的文字, 唯一能看到全文的路就是悬停提示 —— 所以"这里到底挂没挂"
    必须是可量的事实, 而不是靠读代码记得(守卫按它断言, 见 I-3)。
    """
    attached = getattr(widget, _TOOLTIP_ATTR, "")
    if callable(attached):
        with suppress(Exception):
            return str(attached())
        return ""
    return str(attached or "")


def detach_tooltip(widget: ctk.CTkBaseClass) -> None:
    """撤掉悬停提示(文案不再被截断时调用)."""
    setattr(widget, _TOOLTIP_ATTR, "")


def sync_tooltip(
    widget: ctk.CTkBaseClass, *, full: str, shown: str | None = None
) -> None:
    """按"有没有被截断"自动挂上/撤掉悬停提示(``shown=None`` 就自己去读控件).

    这是全应用**唯一**一处"裁了就得能给全文"的落点: :func:`track_fit` 与
    :func:`fit_label` 都走它, 所以不存在"某处忘了挂提示"这种漏网(I-3)。
    """
    if shown is None:
        shown = str(widget.cget("text"))
    if shown == full:
        detach_tooltip(widget)
        return
    attach_tooltip(widget, full)


def fit_label(
    label: ctk.CTkLabel,
    text: str,
    font: MeasurableFont,
    width: int,
    *,
    max_lines: int = 1,
) -> None:
    """把 ``text`` 裁进 ``label``, 并在真的裁掉时把完整文本挂成悬停提示.

    一次性裁剪(不跟随宽度变化)的场合用它, 与 :func:`track_fit` 同一条纪律:
    **裁剪与提示绑在一起**, 不留给调用方"记得再挂一次"。中间省略的路径形态走
    :func:`track_fit` 的 ``path=True``(那里才需要按容器宽度反复重裁)。
    """
    shown = fit_text(text, font, width, max_lines=max_lines)
    label.configure(text=shown)
    sync_tooltip(label, full=text, shown=shown)


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
    if getattr(widget, _TOOLTIP_ATTR, None) is not None:
        # 同一控件再挂一次: 只换文案, 不重复绑事件(否则一次悬停会弹出好几层)。
        setattr(widget, _TOOLTIP_ATTR, text)
        return
    setattr(widget, _TOOLTIP_ATTR, text)

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
        # 文案在弹出那一刻才读: 同一控件可以反复挂/撤(= 文本被裁 / 不再被裁),
        # 不必为了换文案再绑一次事件。
        message = tooltip_text(widget)
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


class HoverTip:
    """自己管"什么时候显示、显示在哪"的悬停提示(一个控件里有**多个**可悬停对象时用).

    为什么不能直接用 :func:`attach_tooltip`: 它的触发点是控件自己的 ``<Enter>``/``<Leave>``
    —— 对一个画布不够用。鼠标在画布里从框 A 移到框 B **不会**再来一次 ``<Enter>``, 于是提示
    会一直挂着 A 的文案(比"没有提示"更糟)。所以这里把时机与落点交给调用方, 而窗口的样式
    (底色/描边/留白/间距)仍然与 :func:`attach_tooltip` 共用同一组常量 —— 全应用的提示看起来
    必须是同一种东西。

    ``text`` / ``visible`` 两个只读属性是给守卫用的: "这里到底挂没挂提示"必须是可量的事实。
    """

    def __init__(self, anchor: tk.Misc) -> None:
        """记住锚点控件(提示窗口挂在它下面, 随它一起销毁)."""
        self._anchor = anchor
        self._window: tk.Toplevel | None = None
        self._label: tk.Label | None = None
        self._text = ""

    @property
    def text(self) -> str:
        """现在挂着的文案(没挂窗口时是空串)."""
        return self._text if self._window is not None else ""

    @property
    def visible(self) -> bool:
        """提示窗口现在是不是真的开着."""
        return self._window is not None

    def show(self, text: str, *, root_x: int, root_y: int) -> None:
        """在屏幕坐标 ``(root_x, root_y)`` 旁边弹出/更新提示; ``text`` 为空等同于收起."""
        if not text:
            self.hide()
            return
        if self._window is None and not self._create():
            return
        if self._label is not None:
            # 已经开着就只换文案 —— 关掉再开会让提示在屏幕上闪一下.
            self._label.configure(text=text)
        self._text = text
        self._move(root_x, root_y)

    def hide(self) -> None:
        """收起提示(没开着就什么都不做)."""
        current, self._window = self._window, None
        self._label, self._text = None, ""
        if current is not None:
            with suppress(tk.TclError):
                current.destroy()

    def _create(self) -> bool:
        """建出提示窗口与标签(锚点已经销毁时返回 False, 不抛)."""
        try:
            window = tk.Toplevel(self._anchor)
            window.overrideredirect(True)
            window.attributes("-topmost", True)
            label = tk.Label(
                window,
                text="",
                justify="left",
                background=_TOOLTIP_BG,
                foreground=_TOOLTIP_FG,
                padx=_TOOLTIP_PAD[0],
                pady=_TOOLTIP_PAD[1],
                highlightthickness=1,
                highlightbackground=_TOOLTIP_BORDER,
            )
            label.pack()
        except tk.TclError:
            return False
        self._window, self._label = window, label
        return True

    def _move(self, root_x: int, root_y: int) -> None:
        """把提示挪到给定屏幕坐标下方; 下方放不下就翻到上方, 横向夹进屏幕."""
        window = self._window
        if window is None:  # pragma: no cover - 只在本类的 show 里调用
            return
        try:
            window.update_idletasks()
            width = int(window.winfo_reqwidth())
            height = int(window.winfo_reqheight())
            screen_width = int(window.winfo_screenwidth())
            screen_height = int(window.winfo_screenheight())
            left = min(max(0, root_x), max(0, screen_width - width))
            top = root_y + _TOOLTIP_GAP
            if top + height > screen_height:
                top = max(0, root_y - height - _TOOLTIP_GAP)
            window.geometry(f"+{left}+{top}")
        except tk.TclError:
            self.hide()


# 焦点态要换哪套颜色也回插回去(颜色只在本模块的 ``button_colors`` 一处定义)。
set_focus_paint(focus_paint)
