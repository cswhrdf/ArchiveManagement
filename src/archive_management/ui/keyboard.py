"""键盘可用性: 让 Tab 走得到、焦点看得见、空格/回车按得动.

CustomTkinter 的按钮/单选/复选都画在 Canvas 上,本模块的三条行为都是在
Tk 8.6 + CTk 6.0 上**实测**出来的缺口:

* 内层 canvas 的 ``takefocus`` 是空串,而 Tk 对 Canvas 的默认启发式是"不进 Tab
  链" —— 纯键盘用户在对话框里根本走不到按钮、单选与复选上(实测
  ``tk_focusNext`` 只能走到输入框与下拉框);
* 聚焦时没有任何视觉变化(``border_color`` 与聚焦前一模一样), "焦点在哪"看不见;
* Canvas 没有空格/回车的内建绑定, 就算强行 ``focus_set`` 也按不动。

于是这里补三件事: ``takefocus = 1`` + 焦点环 + 空格/回车激活。接线靠
:func:`install_keyboard_support` 在**类上**打一次补丁(与 :mod:`textundo`、
:mod:`rendering` 同一套做法) —— 控件是就地创建的还是走 ``UiKit`` 都能接上,
业务代码一个字也不用改。

12 号实测反馈又修了三处**这种设计本身的坑**:

* 焦点环原来固定用强调色, 而主色实底按钮的底色**就是**强调色 —— 焦点停在"立即备份"
  这类按钮上时环与底色同色, 等于没有提示。现在按"与**被聚焦控件自己的底色**的对比度"
  从调色板里挑(见 :func:`ring_color`), 并要求 ≥ :data:`FOCUS_RING_MINIMUM`;
* 下拉框(``CTkComboBox``/``CTkOptionMenu``)没有键盘操作能力(值只能用鼠标点开列表),
  却因为 ``takefocus`` 进了 Tab 链。让 Tab 停在一个"按什么都没反应"的控件上是误导,
  因此它们一律**不进 Tab 链**(见 :data:`UNOPERABLE_TYPES`);
* 窗口级的 Esc / 回车只属于**抓取式对话框**的约定(取消 / 主操作)。常驻工作窗口
  (设置、定时任务)不吃这两个键 —— 否则在设置窗口按 Esc 会直接关窗、回车会按到
  "切换主题"那颗主色按钮(都为实测反馈)。

"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Iterator
from contextlib import suppress
from typing import Any, cast

import customtkinter as ctk

from archive_management.ui import contrast
from archive_management.ui.palette import Palette

# 焦点环的宽度: 用户实测 2px 在主色实底按钮上像"按钮缩小了一圈", 3px 才明显是一圈框。
FOCUS_RING_WIDTH = 3
# 焦点环与**控件自己的底色**至少要差这么多: 环是"非文字"的信息载体, 按 WCAG 的
# 非文字下限取 3:1(与选中描边、状态点同一条判据)。
FOCUS_RING_MINIMUM = contrast.NON_TEXT_MINIMUM
# 焦点环的候选颜色(按优先级): 两个**专用** token, 不参任何语义色。
# 分两档是量出来的结论: 一套主题里"普通底色"与"主色/危险色实底"要求的明度刚好相反
# (深色主题合并后最优只有 1.87:1, 浅色主题 3.67:1), 所以普通底色用 ``focus_ring``、
# 实底用 ``focus_ring_on_fill`` —— 两档都是专用于焦点的独立颜色, 不再复用
# accent / text_primary(用户实测: 复用出来的深绿色环看起来只是"按钮缩小了一圈")。
RING_COLORS: tuple[str, ...] = ("focus_ring", "focus_ring_on_fill")
# 需要接线的控件类型(输入类走内部 ``_entry``, 其余走 ``_canvas``)。
REACHABLE_TYPES: tuple[type, ...] = (
    ctk.CTkEntry,
    ctk.CTkButton,
    ctk.CTkRadioButton,
    ctk.CTkCheckBox,
    ctk.CTkSwitch,
)
# 不吃键盘的控件: 值只能用鼠标从列表里选, 键盘按什么都没反应。它们**不进 Tab 链**,
# 也不画焦点环 —— 两种做法都是"别让 Tab 停在按不动的东西上"。
UNOPERABLE_TYPES: tuple[type, ...] = (ctk.CTkComboBox, ctk.CTkOptionMenu)
# 空格/回车能"按下去"的类型(输入类不在其中: 空格是打字, 回车由业务自己绑)。
ACTIVATABLE_TYPES: tuple[type, ...] = (
    ctk.CTkButton,
    ctk.CTkRadioButton,
    ctk.CTkCheckBox,
    ctk.CTkSwitch,
)
# 会吃键盘输入的控件: 对话框打开时优先把焦点给它们("打开就能打字")。下拉框不在
# 其中: 键盘拿它没办法, 定焦过去只会让用户以为可以打字。
INPUT_TYPES: tuple[type, ...] = (ctk.CTkEntry,)
# 全部要在类上打补丁的类型: 不吃键盘的那些也要打(它们同样需要在 ``state`` 变化时
# 被挡在 Tab 链外), 只是接的是另一套规则。
PATCHED_TYPES: tuple[type, ...] = (*REACHABLE_TYPES, *UNOPERABLE_TYPES)


def focus_target(widget: tk.Misc) -> tk.Misc | None:
    """返回真正吃焦点的**内部控件**(CTk 把一切都套在 Canvas / Entry 里)."""
    for name in ("_entry", "_canvas"):
        inner = getattr(widget, name, None)
        if isinstance(inner, tk.Misc):
            return inner
    return None


def is_reachable(widget: object) -> bool:
    """该控件是否已经接好线(重复接线没意义, 守卫也用它断言"接上了").

    只读一个属性, 所以入参收 ``object``: 替身控件也能问这句话(它只会答 False)。
    """
    return bool(getattr(widget, "_keyboard_reachable", False))


def walk(widget: tk.Misc) -> Iterator[tk.Misc]:
    """深度优先遍历一个窗口里的全部子控件(含 CTk 控件内部的 Canvas/Entry)."""
    children = getattr(widget, "winfo_children", None)
    if not callable(children):
        return
    for child in children():
        yield child
        yield from walk(child)


def _default_activate(widget: ctk.CTkBaseClass) -> Callable[[], None] | None:
    """取一个控件"被按下"的动作(CTk 的三种叫法各不同)."""
    if not isinstance(widget, ACTIVATABLE_TYPES):
        return None
    for name in ("invoke", "toggle"):
        action = getattr(widget, name, None)
        if callable(action):
            return cast(Callable[[], None], action)
    return None


def _is_disabled(widget: tk.Misc) -> bool:
    with suppress(tk.TclError, AttributeError, ValueError):
        return str(widget.cget("state")) == "disabled"
    return False


# 键盘接线是**增强**: 单元测试里的替身控件连 ``winfo_*`` 都没有, 这时各函数只能安静地
# 当作"没有可接的东西", 不能把自己的存在变成别人的前置条件。
def _toplevel_of(widget: object) -> tk.Misc | None:
    """取控件所在的顶层窗口(替身控件可能没有这个能力)."""
    method = getattr(widget, "winfo_toplevel", None)
    if not callable(method):
        return None
    with suppress(tk.TclError):
        return cast(tk.Misc, method())
    return None


def remember_palette(widget: tk.Misc, palette: object) -> None:
    """把"当前这套颜色"记在控件**和它所在的窗口**上.

    焦点环要用强调色, 而焦点环是运行时才画的 —— 画的时候手边没有调色板。
    记下来的这一份就是焦点环的颜色来源(见 :func:`ring_color`), 顺带让
    "同一个窗口里所有控件用同一套色"这件事有个统一出处。
    """
    widget._palette = palette  # type: ignore[attr-defined]
    toplevel = _toplevel_of(widget)
    if toplevel is not None:
        toplevel._palette = palette  # type: ignore[attr-defined]


def palette_for(widget: object) -> Palette | None:
    """取该控件所在窗口的调色板; 窗口上没记过就按当前外观模式取一套.

    "这个窗口用哪套色"是准确的来源(按钮上色时会一起记下来); ``ctk.get_appearance_mode()``
    是退路 —— 主窗口切主题时会同步设置外观模式, 因此退路与实际主题一致, 不会出现
    "深色界面上画个浅色焦点环"。

    **公开**: 焦点环之外还有别的"运行时才画"的东西要取色(下拉浮层, 见
    :mod:`archive_management.ui.dropdown`), 它们不该各自再写一遍"窗口 → 调色板"。
    """
    with suppress(tk.TclError):
        toplevel = _toplevel_of(widget)
        palette = None if toplevel is None else getattr(toplevel, "_palette", None)
        if not isinstance(palette, Palette):
            palette = Palette.for_theme(str(ctk.get_appearance_mode()).lower())
        return palette
    return None  # pragma: no cover - 只在 Tk 已经崩掉时到这里


def ring_color(widget: object, *, fill: str | None = None) -> str | None:
    """挑一个"在**这个控件聚焦后的底色**上看得见"的焦点环颜色.

    ``fill`` 给出"聚焦时会换成的新底色"(实底按钮会被换成软底); 不传就读控件当前的
    ``fg_color``。只从两个**专用于焦点**的调色板 token (:data:`RING_COLORS`) 里取 ——
    实测"一套主题一个颜色"做不到: 把普通底色与主色/危险色实底合在一起, 最优颜色的
    最差对比度也只有 **1.87:1**(深色主题) / **3.67:1**(浅色主题)。所以分两档, 取
    **对比度更高的那一档**(而不是"第一个达标的": 浅色主题的深环在主色实底上恰好
    3.1:1, 刚过线但看不出, 而另一档是 4.58:1):

    * 普通底色(面板/输入框/卡片/**换成软底之后的实底按钮**) → ``focus_ring``:
      深色主题的亮青 **9.50:1 以上**、浅色主题的深蓝黑 **16.87:1**;
    * 实底底色(换色没生效时的兜底) → ``focus_ring_on_fill``。

    底色读不到(``"transparent"`` 或主题对)时按语义取第一个候选。

    入参收 ``object``: 它只读 ``cget`` 与 ``winfo_toplevel`` 两个能力, 因此**无头**
    测试的替身控件也能问这句话(取色是纯算术, 穷举两套主题、五种按钮样式、五个界面
    底色的那批用例就是这么跑起来的)。
    """
    palette = palette_for(widget)
    if palette is None:
        return None
    candidates = [
        color
        for name in RING_COLORS
        if isinstance(color := getattr(palette, name, None), str)
    ]
    if not candidates:
        return None  # pragma: no cover - 调色板一定有这几个字段
    background = fill if fill is not None else _cget(widget, "fg_color")
    if not contrast.is_hex(background):
        return candidates[0]
    ratios = [
        (contrast.contrast_ratio(color, background), color) for color in candidates
    ]
    return max(ratios)[1]


def _paint_options(widget: ctk.CTkBaseClass) -> dict[str, object]:
    """读出该控件**支持**的、聚焦时会被改写的选项(能读几样读几样).

    实测: ``CTkRadioButton`` 没有可读的 ``border_width``(单选只有描边颜色),
    ``CTkOptionMenu`` 两样都没有 —— 所以"原样"不是一个固定形状的字典。
    """
    options: dict[str, object] = {}
    for option in TOUCHED_OPTIONS:
        try:
            options[option] = widget.cget(option)
        except (tk.TclError, AttributeError, ValueError):
            continue
    return options


def _ring_on(widget: ctk.CTkBaseClass, saved: dict[str, object]) -> None:
    """聚焦: 记住原样并换上焦点态(环 + 实底按钮换成的软底配色).

    两件事都以"先量再改"为准:

    * **实底按钮换成对应的软底样式**(由 ``widgets.focus_paint`` 给出): 主色/危险色
      底上任何亮色环都看不出(实测 1.27:1), 换成软底后那一档亮色环立刻 10:1 以上;
    * **环色按换完之后的新底色取**(所以先算配色再取色), 并且只在第一次记住原样 ——
      重复 ``FocusIn`` 不能再记, 否则会把焦点态自己当原色存下来, 失焦就恢复不回去了。

    **读不齐就不写**: 只存下 ``border_color`` 而没存 ``border_width`` 时, 失焦会去
    pop 一个不存在的键(实测单选按钮报 ``KeyError: 'border_width'``)。
    """
    palette = palette_for(widget)
    if palette is None or _is_disabled(widget):
        return
    paint = _focused_paint(widget)
    fill = str(paint.get("fg_color", _cget(widget, "fg_color")))
    ring = ring_color(widget, fill=fill)
    if ring is None:
        return
    if not saved:
        original = _paint_options(widget)
        if "border_color" not in original:
            return  # 连描边都没有的控件(OptionMenu)画不了环
        saved.update(original)
    options: dict[str, object] = {
        key: value for key, value in paint.items() if key in saved
    }
    if "border_color" in saved:
        options["border_color"] = ring
    if "border_width" in saved:
        options["border_width"] = FOCUS_RING_WIDTH
    with suppress(tk.TclError, AttributeError, ValueError):
        widget.configure(**options)


def _ring_off(widget: ctk.CTkBaseClass, saved: dict[str, object]) -> None:
    """失焦: 把原样放回(配色随样式与状态变, 所以是存下来再还原, 不是猜)."""
    if not saved:
        return
    options = dict(saved)
    saved.clear()
    with suppress(tk.TclError, AttributeError, ValueError):
        widget.configure(**options)


def _set_takefocus(widget: ctk.CTkBaseClass, value: int | str) -> None:
    """给内部控件设 ``takefocus``.

    走 :func:`cast` 是因为 ``takefocus`` 是 Tk 的通用选项,不在 ``Misc.configure``
    的签名里(mypy 的 tkinter 存根只看得到具名选项)。
    """
    inner = focus_target(widget)
    if inner is None:
        return
    with suppress(tk.TclError, AttributeError):
        cast(Any, inner).configure(takefocus=value)


def make_reachable(
    widget: ctk.CTkBaseClass,
    *,
    activate: Callable[[], None] | None = None,
) -> bool:
    """把一个 CTk 控件接进键盘世界,返回本次是否**新**接上.

    * ``takefocus = 1``: 进 Tab 链(按下 Tab 真能走到它);
    * 焦点环: 聚焦时描边改成与**自己底色**分得开的颜色(取自 :func:`ring_color`,
      因此换主题后重画按钮就会跟着换), 失焦时把原来的描边原样放回;
    * ``activate``: 空格/回车触发(默认按控件类型取 ``invoke`` / ``toggle``),
      并且 ``break`` 掉事件, 免得窗口级的回车确认再提交一次。
    """
    if is_reachable(widget):
        return False
    inner = focus_target(widget)
    if inner is None:
        return False
    action = activate if activate is not None else _default_activate(widget)
    saved: dict[str, object] = {}
    _set_takefocus(widget, 1)

    def on_focus_in(_event: object = None) -> None:
        _ring_on(widget, saved)

    def on_focus_out(_event: object = None) -> None:
        _ring_off(widget, saved)

    def on_activate(_event: object = None) -> str:
        if action is not None and not _is_disabled(widget):
            action()
        return "break"

    inner.bind("<FocusIn>", on_focus_in)
    inner.bind("<FocusOut>", on_focus_out)
    if action is not None:
        inner.bind("<space>", on_activate)
        inner.bind("<Return>", on_activate)
    widget._keyboard_reachable = True
    # 把"没聚焦时的原样"挂在控件上, 让别处(主按钮识别)也能看到它 —— 焦点态是临时
    # 改写的, 谁都不该拿焦点态当"这颗按钮是什么按钮"的依据。
    widget._focus_saved = saved
    return True


def leave_tab_chain(widget: ctk.CTkBaseClass) -> None:
    """把控件从 Tab 链里摘掉(禁用态的控件不该被 Tab 走到).

    原本的 ``takefocus`` 会先存下来: 恢复可用时要还回去, 而不是拍一个 1
    (输入类的默认值是空串, 写死 1 会改变 Tk 对它们的处理方式)。
    """
    inner = focus_target(widget)
    if inner is None:
        return
    with suppress(tk.TclError, AttributeError):
        widget._keyboard_takefocus = inner.cget("takefocus")
        cast(Any, inner).configure(takefocus=0)


def keep_out_of_tab_chain(widget: ctk.CTkBaseClass) -> None:
    """把**键盘操作不了**的控件挡在 Tab 链外(不记原值: 它本来就该一直不在).

    下拉框的默认 ``takefocus`` 对内部 ``Entry`` 来说是空串, 而 Tk 对 Entry 的默认
    启发式是"进 Tab 链" —— 所以必须显式写 0, 否则 Tab 还是会停在它上面。
    """
    _set_takefocus(widget, 0)
    widget._keyboard_reachable = False


def enter_tab_chain(widget: ctk.CTkBaseClass) -> None:
    """把控件放回 Tab 链(可用态就是它该在的位置)."""
    remembered = getattr(widget, "_keyboard_takefocus", None)
    if isinstance(widget, ACTIVATABLE_TYPES):
        _set_takefocus(widget, 1)
    elif remembered is not None:
        _set_takefocus(widget, cast(int | str, remembered))


def sync_tab_chain(widget: ctk.CTkBaseClass) -> None:
    """按控件**当前的 state** 把它放回/摘出 Tab 链.

    禁用态不是一次性事件: 按钮/单选/复选/下拉会在运行期反复禁用与恢复(忙碌、
    该项不适用、还没勾选...), 所以每次 ``configure(state=...)`` 之后都要走一遍。
    键盘操作不了的控件无论什么状态都不进 Tab 链。
    """
    if isinstance(widget, UNOPERABLE_TYPES):
        keep_out_of_tab_chain(widget)
    elif _is_disabled(widget):
        leave_tab_chain(widget)
    else:
        enter_tab_chain(widget)


def _button_candidates(window: tk.Misc, wanted: list[str]) -> dict[str, tk.Misc]:
    """收集候选主按钮: 键是"哪一条线索命中"(实测底色优先, 其次是登记的样式)."""
    allowed = (*wanted, "accent", "danger")
    found: dict[str, tk.Misc] = {}
    for child in walk(window):
        if not isinstance(child, ctk.CTkButton):
            continue
        color = resting_fill(child)
        style = str(getattr(child, "_button_style", ""))
        key = color if color in wanted else style
        if key in allowed and key not in found:
            found[key] = child
    return found


def primary_button(window: tk.Misc) -> tk.Misc | None:
    """找窗口里的**主操作**按钮(强调色优先, 其次是危险色).

    判定看的是**实测底色**是否等于调色板里的 ``accent``/``danger``, 而不是
    "这个按钮登记成什么样式": 仓库里有不少就地创建、直接写 ``fg_color=palette.accent``
    的按钮, 只看样式表会漏掉它们(实测四个对话框的主按钮就是这类)。样式登记
    只当没有调色板时的备选。

    对话框的回车确认靠它: "回车 = 这一屏的主操作"与"焦点落在主按钮上"是同一件事,
    所以两处都从这一个函数取。
    """
    palette = _palette_of(window)
    wanted = [palette.accent, palette.danger] if palette is not None else []
    found = _button_candidates(window, wanted)
    for key in (*wanted, "accent", "danger"):
        if key in found:
            return found[key]
    return None


# 聚焦时要临时改写的选项(能读到几样写几样): 环 + 实底按钮换成的软底配色。
TOUCHED_OPTIONS = (
    "border_color",
    "border_width",
    "fg_color",
    "hover_color",
    "text_color",
)

# "聚焦时该换成哪套颜色"由 :mod:`widgets` 在导入时装进来: 颜色只在
# ``widgets.button_colors`` 一处定义, 而本模块不能导入 ``widgets``(会成环)。
_FOCUS_PAINT: Callable[[Palette, str], dict[str, str]] | None = None


def set_focus_paint(provider: Callable[[Palette, str], dict[str, str]]) -> None:
    """登记"某个按钮样式聚焦时该换成哪套颜色"(由 ``widgets`` 导入时调用).

    参数是调色板与样式名, 返回要写给控件的选项(空表 = 只换焦点环)。
    """
    global _FOCUS_PAINT
    _FOCUS_PAINT = provider


def _focus_style(widget: object) -> str | None:
    """取该按钮的样式名: 先看登记的样式, 没有就按**没聚焦时**的底色反推主色/危险色.

    实测有四处对话框的主按钮是就地创建、直接写 ``fg_color=palette.accent`` 的
    (没有登记样式), 所以不能只看 ``_button_style`` —— 否则恰恰是用户报的那几颗
    按钮拿不到换色。

    认色必须认**没聚焦时**的底色(见 :func:`resting_fill`): 焦点态已经把实底换成了
    软底, 拿它去比 ``accent``/``danger`` 永远比不上 —— 那会让"重新聚焦一次"这种
    正常时序把按钮认成"不是主按钮"。
    """
    style = getattr(widget, "_button_style", "")
    if isinstance(style, str) and style:
        return style
    palette = palette_for(widget)
    if palette is None:
        return None
    fill = resting_fill(widget).lower()
    for candidate in ("accent", "danger"):
        value = getattr(palette, candidate, None)
        if isinstance(value, str) and value.lower() == fill:
            return candidate
    return None


def _focused_paint(widget: object) -> dict[str, str]:
    """聚焦时该把控件换成哪套颜色(只有实底按钮会换)."""
    palette = palette_for(widget)
    if palette is None or _FOCUS_PAINT is None:
        return {}
    style = _focus_style(widget)
    return {} if style is None else _FOCUS_PAINT(palette, style)


def _cget(widget: object, option: str) -> str:
    """读一个选项(不支持该选项时返回空串).

    走 ``getattr`` 而不是 ``widget.cget``: 入参收 ``object``, 替身控件可以只有
    ``cget`` 这一个方法(无头测试), 也可以什么都没有 —— 那就当"读不到"。
    """
    reader = getattr(widget, "cget", None)
    if not callable(reader):
        return ""
    with suppress(tk.TclError, AttributeError, ValueError):
        return str(reader(option))
    return ""


def resting_fill(widget: object) -> str:
    """取按钮**没聚焦时**的底色(焦点态不算数).

    聚焦会把实底按钮换成对应的软底配色, 于是"看底色认按钮"的整套机制(主按钮
    识别、配色守卫)在焦点落上去之后就会认错: 危险色按钮被认成软危险、主色按钮
    直接认不出来。实测症状是**回车确认失效**(窗口里找不到主按钮了)与"配色不合规"
    的假红 —— CI 的时序恰好是"先定焦、后测量", 本地反之, 同一份代码就时红时绿。

    所以: 接上键盘线的控件会把原样存在 :attr:`_focus_saved` 里, 有它就认原样。
    """
    saved = getattr(widget, "_focus_saved", None)
    if isinstance(saved, dict) and "fg_color" in saved:
        return str(saved["fg_color"])
    return _cget(widget, "fg_color")


def _palette_of(window: tk.Misc) -> Palette | None:
    """取窗口上记过的调色板(没记过就退回当前外观模式对应的那一套)."""
    palette = getattr(window, "_palette", None)
    if isinstance(palette, Palette):
        return palette
    with suppress(tk.TclError):
        return Palette.for_theme(str(ctk.get_appearance_mode()).lower())
    return None


def bind_window_keys(window: tk.Misc) -> None:
    """给**抓取式对话框**接上键盘习惯: Esc = 取消(关窗), 回车 = 主操作.

    回车绑在**窗口**上, 但按的是 :func:`primary_button` 找到的那颗按钮; 焦点在
    按钮/输入框上时它们自己会先处理(并 ``break``), 所以不会重复提交。

    只给模态对话框调(:func:`~archive_management.ui.dialogs._present` 的 ``modal``
    参数): 常驻工作窗口没有"取消/主操作"这层语义, 替它按主按钮是自作主张 ——
    实测在设置窗口按回车会触发"切换主题"(那颗按钮的底色恰好是强调色)。
    """
    window.bind("<Escape>", lambda _event: window.destroy())

    def on_return(_event: object = None) -> None:
        button = primary_button(window)
        if isinstance(button, ctk.CTkButton) and not _is_disabled(button):
            button.invoke()

    window.bind("<Return>", on_return)


def _focus_on_map(event: tk.Event) -> None:
    """窗口重新显示出来时再定一次焦.

    上一次可能落在"还没映射"的时刻上(实测: 窗口不可见时 ``focus_set`` 不生效),
    所以连这一步也放到 ``after_idle``: 那时映射已经完成。
    """
    widget = event.widget
    action = getattr(widget, "_focus_when_visible", None)
    if callable(action):
        with suppress(tk.TclError):
            widget.after_idle(action)


def focus_first(window: tk.Misc) -> tk.Misc | None:
    """给窗口定个初始焦点: 第一个输入框优先, 没有输入框就落到主操作按钮上.

    打开对话框就能直接打字(或者直接按回车), 不必先点一下。两件事都是实测出来的:

    * **窗口可见之前 ``focus_set`` 不生效** —— 所以除了 ``after_idle`` 之外还要绑一次
      ``<Map>``: CustomTkinter 的标题栏着色会先隐藏再重新映射窗口, 定焦得跟着再来一次;
    * 焦点在**输入框**上时回车等于确认(输入框自己绑了 ``<Return>``), 在按钮上时
      回车按下的是**那个按钮**。

    只挑输入框(:data:`INPUT_TYPES` = ``CTkEntry``): 下拉框键盘动不了, 定焦过去会
    让用户以为可以打字(实测反馈: 设置窗口"默认聚焦到界面字号上", 按回车反而切了主题)。
    """
    target: tk.Misc | None = None
    for child in walk(window):
        if isinstance(child, INPUT_TYPES):
            target = child
            break
    if target is None:
        target = primary_button(window)
    inner = focus_target(target) if target is not None else None
    if inner is None:
        return target

    def apply(_event: object = None) -> None:
        with suppress(tk.TclError):
            inner.focus_set()

    # 记在窗口上: 重映射时要按"最新一次该定焦到哪里"再定一次.
    window._focus_when_visible = apply  # type: ignore[attr-defined]
    if not getattr(window, "_focus_on_map_wired", False):
        window._focus_on_map_wired = True  # type: ignore[attr-defined]
        with suppress(tk.TclError):
            window.bind("<Map>", _focus_on_map, add="+")
    with suppress(tk.TclError):
        window.after_idle(apply)
    return target


# -- 全局补丁 ---------------------------------------------------------------

_APPLIED = False


def _attach(widget: object) -> None:
    """控件建好后接上键盘(创建期接线, 不依赖谁去上色)."""
    if isinstance(widget, ctk.CTkBaseClass):
        with suppress(tk.TclError):
            if isinstance(widget, UNOPERABLE_TYPES):
                keep_out_of_tab_chain(widget)
                return
            make_reachable(widget)


def _wrap_init(original: Callable[..., None]) -> Callable[..., None]:
    r"""包住控件构造器: 先让 CTk 把内部 Canvas/Entry 建好, 再接线."""

    def patched(self: object, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)
        _attach(self)

    return patched


def _wrap_configure(original: Callable[..., object]) -> Callable[..., object]:
    """包住 ``configure``: ``state`` 一变就把控件放回/摘出 Tab 链.

    禁用态会反复出现(忙碌、该项不适用、还没勾选...), 而"禁用控件仍在 Tab 链里"
    只能在这里根治 —— 逐个调用点去记是不可能的。
    """

    def patched(self: object, require_redraw: bool = False, **kwargs: object) -> object:
        result = original(self, require_redraw=require_redraw, **kwargs)
        if "state" in kwargs and isinstance(self, ctk.CTkBaseClass):
            sync_tab_chain(self)
        return result

    return patched


def install_keyboard_support() -> bool:
    """给所有 CTk 控件接上键盘可用性, 返回本次是否真的打了补丁(幂等).

    必须在创建任何控件之前调用(与
    :func:`~archive_management.ui.textundo.apply_input_undo_support` 同一套约定) ——
    补丁只对之后创建的控件生效。``widgets`` 在导入时就调它, 而所有建窗口的模块最终
    都会导入 ``widgets``。
    """
    global _APPLIED
    if _APPLIED:
        return False
    for cls in PATCHED_TYPES:
        # 类上打补丁只能走 setattr + cast: 直接赋值时 mypy 会说"改的是实例的 __init__"
        # /"type 上没有 configure"(B010 想拦的是"setattr 不比属性访问更安全", 这里正是
        # 需要动态改类属性的场合)。
        target = cast(Any, cls)
        setattr(target, "__init__", _wrap_init(target.__init__))  # noqa: B010
        setattr(target, "configure", _wrap_configure(target.configure))  # noqa: B010
    _APPLIED = True
    return True
