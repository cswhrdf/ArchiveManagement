"""模态对话框与确认.

统一高风险操作确认与信息展示方式,全部在主线程调用(后台回调经消息
队列回到主线程后再触发),避免工作线程直接操作 Tk 控件。所有文案经
i18n 配置加载,弹窗一律居中显示在主窗口上。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import customtkinter as ctk

from archive_management.application.imports import STRATEGY_MERGE
from archive_management.domain import (
    MAX_TAG_LENGTH,
    MAX_TAGS,
    normalize_tags,
)
from archive_management.i18n import tr
from archive_management.ui.keyboard import bind_window_keys, focus_first, walk
from archive_management.ui.models import (
    BatchExportChoice,
    BatchExportOption,
    BatchExportPrompt,
    BatchImportPrompt,
    BatchImportRow,
    BatchImportSelection,
    ImportChoice,
    ImportLocationRow,
    ImportPrompt,
    ImportTargetOption,
    batch_export_choice,
    batch_row_choice,
    filter_export_options,
    target_game_id,
    target_label_for,
)
from archive_management.ui.palette import Palette
from archive_management.ui.widgets import (
    auto_scrollbar,
    paint_button_disabled,
    paint_button_enabled,
    paint_button_state,
    paint_button_style,
    track_wraplength,
    window_scaling,
)

# 文本类对话框的正文宽度: 提示、输入框与按钮行都按这个宽度左对齐, 三块不再各宽各的
# (19 号评审: 三块内容都居中但宽度各不相同, 看着像三个独立组件堆在一起)。
_TEXT_WIDTH = 360
# 对话框里成段说明的左右内边距(单侧): 算可用宽度时要按它的**两倍**减(见 _dialog_hint)。
_DIALOG_HINT_PAD = 24
# 定时对话框里两个输入框的宽度: 两者同宽, 左右边界才对得上(25 号评审)。
_SCHEDULE_FIELD_WIDTH = 210

# 长对话框的正文区: 内容超高时由它自己滚, 底部的按钮永远留在屏幕里。
#
# 533x832 的导入弹窗在 768/900 高的屏幕上会把底部的「导入」切出屏幕, 而它自己
# 又不能滚(28 号评审)。高度取屏幕的 80%(有上下限), 宽度是 460 的内容 + 左右各 24
# 边距, 再留一点滚动条的余地。
_DIALOG_BODY_MAX = 760
_DIALOG_BODY_MIN = 360
_DIALOG_BODY_WIDTH = 518
# 模态对话框的舒适线: 评审约定的"弹窗不超过屏幕可用高度的 80%"。
_DIALOG_COMFORT = 0.8
# 正文区之外那部分(标题/提示行/按钮行 + 上下内边距)的高度, **估算**约 50px。
# 80% 是**整个窗口**的预算, 不是正文区的预算: 只夹正文区的话 768 高的屏幕上窗口
# 会变成 664(正文 614 + 正文之外 50), 底部的「导入」还是差 16px 出屏幕。
#
# 估算值有个已知的偏差: 这一部分的高度由**字体**和窗口管理器决定, 实测同一份代码
# Linux 上比 Windows 高 7 像素 —— 于是正文区顶到上限的对话框会整窗超出舒适线
# (CI 实测 621 > 614)。所以建完窗口之后还要按**实测**收一次(见
# :func:`_clamp_to_comfort_line`), 这个常量只负责"还没法量的时候"的初值。
_DIALOG_CHROME_HEIGHT = 52
# 舒适线夹取的收敛轮数: 每轮按"现在量到的高度"收一次, 收完再量(布局不保证 1:1 跟着正文区走)。
# 上限只是保险 —— 正常情况下第一轮或第二轮就落到线内了。
_CLAMP_PASSES = 4
# 勾选行下面那行提示的左缩进: 24(正文距边) + 22(方框宽与它到文字的间距), 量出来是为了
# 与复选框的**文字**左对齐 -- 这条对齐没法落在间距刻度上, 所以留个具名常量。
_CHECK_HINT_PAD = (24 + 22, 24)

# 摘要里的项目符号: "· " 开头的行会被渲染成悬挂缩进的一行(见 _bullet_block).
_BULLET = "· "


def _bullet_block(
    parent: ctk.CTkBaseClass,
    text: str,
    palette: Palette,
    *,
    wraplength: int,
    padx: int = 24,
    top_pad: int = 0,
) -> None:
    """把带"· "项目符号的多行文本渲染成悬挂缩进.

    单个带 ``wraplength`` 的标签做不到这件事: 长路径折行后会顶到最左边, 看起来像
    "比上一行更靠左"的两级列表却只缩进了一级(23 号评审)。这里把符号与正文拆成两个
    控件, 折行后的文字就与首行文字左对齐。
    """
    for index, line in enumerate(text.splitlines()):
        stripped = line.strip()
        bullet = stripped.startswith(_BULLET)
        body = stripped[len(_BULLET) :] if bullet else stripped
        row = ctk.CTkFrame(parent, fg_color="transparent")
        row.pack(fill="x", padx=padx, pady=(top_pad, 1) if index == 0 else 1)
        if bullet:
            ctk.CTkLabel(
                row,
                text=_BULLET.strip(),
                width=10,
                anchor="nw",
                font=ctk.CTkFont(size=13),
                text_color=palette.text_muted,
            ).pack(side="left", anchor="n")
        ctk.CTkLabel(
            row,
            text=body,
            anchor="w",
            justify="left",
            wraplength=wraplength,
            font=ctk.CTkFont(size=13),
            text_color=palette.text_body,
        ).pack(side="left", fill="x", expand=True, anchor="w")


def _add_state(count: int, max_tags: int) -> str:
    """返回"添加标签"按钮的可用状态(到达上限就禁用)."""
    return "normal" if count < max_tags else "disabled"


def _drop_tag_row(entries: list[ctk.CTkEntry], entry: ctk.CTkEntry) -> bool:
    """从登记表里移除一行, 返回"删空后是否需要补一个空行"."""
    if entry in entries:  # pragma: no branch - 按钮随所在行一起销毁
        entries.remove(entry)
    return not entries


def _paint_row_remove(button: ctk.CTkButton, palette: Palette, *, filled: bool) -> None:
    """行内"删除"按钮的可用态: 空行压暗.

    空行的"删除"点了等于删一个不存在的标签(21 号评审); 放到模块级是为了不让
    :func:`edit_tags_dialog` 的分支数超限。
    """
    if filled:
        paint_button_enabled(
            button,
            palette,
            fg_color=palette.raised,
            text_color=palette.text_body,
            hover_color=palette.item_hover,
            border_color=palette.border,
        )
        return
    paint_button_disabled(button, palette)


def _set_alpha(window: ctk.CTkToplevel, value: float) -> bool:
    """设置窗口不透明度, 返回平台是否支持(不支持时调用方走退路)."""
    try:
        window.attributes("-alpha", value)
    except tk.TclError:  # 无合成器的 Linux 等环境
        return False
    return True


def _centered_position(
    parent: ctk.CTk,
    window: ctk.CTkToplevel,
    size: tuple[int, int],
    offset: tuple[int, int],
) -> str:
    """算出让**客户区**居中于主窗口的位置字符串.

    ``wm geometry`` 的 ``+x+y`` 是**窗口框**(含标题栏与边框)的位置, 而
    ``winfo_rootx/rooty`` 给的是客户区 —— 所以要减去装潢偏移, 否则弹窗会整体偏右下。
    """
    width, height = size
    offset_x, offset_y = offset
    x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2 - offset_x
    y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2 - offset_y
    return f"+{max(x, 0)}+{max(y, 0)}"


def _dialog_comfort_limit(window: ctk.CTkToplevel) -> int:
    """该窗口的舒适线上限(屏幕可用高度的 80%)."""
    return int(int(window.winfo_screenheight()) * _DIALOG_COMFORT)


def _clamp_to_comfort_line(
    window: ctk.CTkToplevel, *, current: int | None = None
) -> None:
    """把建好的窗口收进舒适线: 超出多少就把正文区收掉多少(内容由它自己滚).

    ``_DIALOG_CHROME_HEIGHT`` 是估算值, 而"正文区之外"的高度由字体与窗口管理器决定
    —— 实测同一份代码 Linux 上比 Windows 高 7 像素, 于是正文区顶到上限的对话框会
    整窗超出舒适线(CI 实测: 768 高的屏幕上 621 > 614)。这里用**实测**的整窗高补齐
    那几像素, 而不是把估算值再调大一点(调大就会在 Windows 上白留一块空白)。

    ``current`` 是"现在量到的整窗高度", 两个时刻必须分开喂:

    * 建窗时(还没映射)只能看请求高度 ``winfo_reqheight()``;
    * 映射之后要把**实测高度与请求高度一起看**(取大者): 只看实测值会漏掉一种现场 ——
      无窗口管理器的 X11 上 ``winfo_height()`` 会晚一拍(它等 ConfigureNotify), 于是夹取那一刻
      量到 560、布局定下来却是 561(2026-09-30 的 Linux CI: 700 高的屏上 561 > 560, 而 Windows
      恰好 560, 所以只有 Linux 红)。请求高度是本地立刻算出来的, 没有这个滞后。

    收一轮不够就继续收(布局不保证 1:1 跟着正文高度走): 最多 :data:`_CLAMP_PASSES` 轮, 每轮
    都重新量, 收到下限为止。只收正文区(不缩内容): 矮屏上宁可让正文区滚也不能把内容切掉。
    没有滚动正文区的对话框(短提示框)本来就不会超线, 直接返回。

    正文区是**递归找**的: 实测批量导入把它套在一层卡片容器里(不是窗口的直接子控件,
    只找一层就会漏掉它, 那时这条夹取看起来"跑了但什么都没做")。
    """
    limit = _dialog_comfort_limit(window)
    body = next(
        (child for child in walk(window) if isinstance(child, ctk.CTkScrollableFrame)),
        None,
    )
    if body is None:
        return
    for _ in range(_CLAMP_PASSES):
        window.update_idletasks()
        height = int(window.winfo_reqheight())
        if current is not None:
            # 映射之后那一轮: 实测高度也要算进去(上面解释的那个滞后), 但**只有这一轮** ——
            # 后面几轮是"收完再量", 量的该是新布局的请求高度。
            height = max(height, int(window.winfo_height()), int(current))
            current = None
        overflow = height - limit
        if overflow <= 0:
            return
        current_height = int(body.cget("height"))
        if current_height <= _DIALOG_BODY_MIN:
            return
        # ``overflow`` 是**物理**像素(整窗实测/请求高度与屏幕替身同源), 而正文区的
        # ``height`` 与写进 geometry 的一样是**逻辑**像素: 直接相减会在高 DPI 下多收
        # 1/scale 的量(125% 的屏上收 94 物理会写成 94 逻辑 = 117 物理), 于是矮屏上
        # 的弹窗被多压一截、正文字体大一点就真被裁。这里先把要多收的量换到逻辑像素。
        body.configure(
            height=max(
                _DIALOG_BODY_MIN,
                current_height - round(overflow / window_scaling(window)),
            )
        )


def _present(parent: ctk.CTk, window: ctk.CTkToplevel, *, modal: bool = True) -> None:
    """显示对话框前的收尾: 键盘接线 + 居中 + 收进舒适线, 且不让它先在屏幕左上角闪一下.

    键盘那几件(Esc = 取消、回车 = 主操作、初始焦点、按钮进 Tab 链)都在这里接完,
    所以"新加一个对话框忘了接键盘"不会发生 —— 所有对话框都会调这个函数。

    ``modal``: Esc / 回车 / 打开就定焦都是**抓取式对话框**的约定, 常驻工作窗口
    (设置、定时任务)要传 ``modal=False`` —— 实测: 设置窗口按 Esc 会直接把窗口关掉
    (用户的意图是"退出快捷键录制"), 按回车会触发"切换主题"那颗主色按钮, 而且
    一打开就把焦点定在"界面字号"下拉框上(键盘动不了的下拉框)。窗口本身仍然由
    :func:`~archive_management.ui.keyboard.remember_palette` 的同一条路径取色。

    原实现是"``geometry("+0+0")`` → ``update()``(窗口真的出现在左上角) → 再挪到目标
    位置", 所以每次弹窗都会先闪一下左上角再跳过去(用户实测很多次)。两步测量现在都放在
    **看不见**的状态里做:

    1. ``withdraw()`` 之后 ``winfo_rootx/rooty`` 照样给出装潢偏移(实测隐藏窗口也正确),
       不必先把它显示到 (0,0);
    2. 用**全透明**属性映射一次, 让窗口管理器给出真实尺寸 —— CTk 的弹窗在映射时会调整
       尺寸, ``winfo_req*`` 与最终尺寸并不相等(实测 88 → 120), 因此不能只按请求尺寸估;
    3. 位置算准后再恢复不透明度: 窗口第一次可见时就已经在最终位置上。

    平台不支持透明度时(无合成器的 Linux)退化成"先按请求尺寸放个大概位置, 映射后再校正
    一次" —— 仍有一次小位移, 但不会再从屏幕左上角跳过来。
    """
    # 接线放在最前面: 下面几步只是摆位置, 而窗口一旦可见用户就可能已经在按 Esc。
    if modal:
        bind_window_keys(window)
        # 舒适线只对**模态弹窗**约定(见 docs/testing.md): 常驻工作窗口有自己的最小
        # 尺寸, 屏幕再矮也不该把它们压矮(见 test_gui_sizes 的"工作窗口只守硬线")。
        _clamp_to_comfort_line(window)
    window.withdraw()
    window.update_idletasks()
    window.geometry("+0+0")
    window.update_idletasks()
    offset = (window.winfo_rootx(), window.winfo_rooty())
    # 先把位置按请求尺寸估好: 不支持透明度时这就是它第一次出现的位置(近似居中).
    window.geometry(
        _centered_position(
            parent,
            window,
            (window.winfo_reqwidth(), window.winfo_reqheight()),
            offset,
        )
    )
    transparent = _set_alpha(window, 0.0)
    window.deiconify()
    window.update_idletasks()
    if modal:
        # 映射之后尺寸才定下来(窗口管理器与 CTk 都会在这中间改尺寸): 再收一次 —— 夹取同时看
        # **实测高度与请求高度**(取大者, 见 _clamp_to_comfort_line: 无窗口管理器的 X11 上
        # 实测值会晚一拍), 否则"请求高度刚好在舒适线内、实际多出几个像素"的对话框仍会越线。
        # 这一步在窗口透明时做, 所以用户看不到任何跳动。
        _clamp_to_comfort_line(window, current=int(window.winfo_height()))
        window.update_idletasks()
    # 映射之后才知道窗口管理器给的真实尺寸: 透明时用户看不到这一步, 不透明时也只是微调.
    window.geometry(
        _centered_position(
            parent, window, (window.winfo_width(), window.winfo_height()), offset
        )
    )
    if transparent:
        _set_alpha(window, 1.0)
    # 定焦要等窗口真的映射出来(tk 会把"最后一次聚焦"记在这个窗口上).
    if modal:
        focus_first(window)


def confirm_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    message: str,
    confirm_text: str | None = None,
    cancel_text: str | None = None,
    detail: str = "",
    danger: bool = False,
) -> bool:
    """显示居中确认对话框,返回用户选择.

    ``detail`` 是正文里不该被当成句子一部分的技术值(导出路径、目录路径): 它单独
    占一行、带输入框底色, 一眼能看出到哪里结束(24 号评审)。``danger`` 为 True 时
    确认按钮穿危险色 —— 破坏性动作不该与"导入/保存"同一颗绿按钮(24 号评审)。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    no_text = tr("dialog.cancel") if cancel_text is None else cancel_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=message,
        wraplength=420,
        justify="left",
        anchor="w",
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    label.pack(padx=24, pady=(20, 6), fill="x")
    if detail:
        ctk.CTkLabel(
            window,
            text=detail,
            anchor="w",
            justify="left",
            wraplength=420,
            fg_color=palette.input_bg,
            corner_radius=6,
            text_color=palette.text_body,
            font=ctk.CTkFont(size=12),
        ).pack(padx=24, pady=(0, 6), fill="x", ipady=6)

    result: list[bool] = []

    def choose(value: bool) -> None:
        result.append(value)
        window.destroy()

    # 按钮行**居中**: 贴着左边排看起来像“漏了排版”, 而且各对话框不一致
    # (2026-10-02 用户反馈)。不写 ``fill``/``anchor`` 时, 这行框就按内容宽度居中
    # —— 行里的按钮都是 ``side="left"``, 所以整组就落在中间。
    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(10, 20))
    cancel = ctk.CTkButton(
        buttons,
        text=no_text,
        width=96,
        height=32,
        command=lambda: choose(False),
    )
    # 取消永远是次色, 破坏性动作的危险色来自 widgets.button_colors 这一处。
    paint_button_style(cancel, palette, "ghost")
    cancel.pack(side="left", padx=(0, 10))
    ok = ctk.CTkButton(
        buttons,
        text=ok_text,
        width=96,
        height=32,
        command=lambda: choose(True),
    )
    paint_button_style(ok, palette, "danger" if danger else "accent")
    ok.pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return bool(result and result[0])


def info_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    message: str,
) -> None:
    """显示居中信息对话框(非模态)."""
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=message,
        wraplength=400,
        justify="left",
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    label.pack(padx=24, pady=(20, 10), fill="x")

    ok = ctk.CTkButton(
        window,
        text=tr("dialog.ok"),
        width=96,
        height=32,
        command=window.destroy,
    )
    paint_button_style(ok, palette, "accent")
    ok.pack(pady=(0, 20))
    _present(parent, window)


def ask_branch_name(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    text: str,
    initial: str = "",
    confirm_text: str | None = None,
) -> str | None:
    """询问分支名称;取消或内容为空返回 None.

    ``initial`` 用作预填名称(创建分支时默认给一个可直接确认的名字).
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=text,
        justify="left",
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    label.pack(padx=24, pady=(20, 8))

    entry = ctk.CTkEntry(
        window,
        width=320,
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        text_color=palette.text_body,
    )
    entry.insert(0, initial)
    entry.pack(padx=24, pady=(0, 10))
    entry.focus_set()
    entry.select_range(0, "end")

    result: list[str] = []

    def submit() -> None:
        name = entry.get().strip()
        if name:
            result.append(name)
            window.destroy()

    entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 20))
    cancel = ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    )
    cancel.pack(side="left", padx=(0, 10))
    ok = ctk.CTkButton(
        buttons,
        text=ok_text,
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    )
    ok.pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def import_game_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    name_label: str,
    initial_name: str,
    paths_label: str,
    paths_hint: str,
    initial_paths: Sequence[str] = (),
    add_text: str = "",
    confirm_text: str | None = None,
    browse: Callable[[], str | None] | None = None,
) -> tuple[str, tuple[str, ...]] | None:
    """询问游戏名与要写进库的存档路径; 取消或名称为空时返回 ``None``.

    路径区预填探测到的候选: 每条都能改, 取消勾选就不写库(用户可以只留其中几条),
    也能自己再加一条。``browse`` 非 None 时每条路径后面多一个"浏览…"按钮(与游戏
    详情里添加/修改存档位置同一个目录选择框), 点它就能可视化挑目录。返回
    ``(名称, 勾选且非空的路径)``。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    name_text = ctk.CTkLabel(
        window,
        text=name_label,
        justify="left",
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    )
    name_text.pack(padx=24, pady=(20, 4), anchor="w")
    name_entry = ctk.CTkEntry(
        window,
        width=440,
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        text_color=palette.text_body,
    )
    name_entry.insert(0, initial_name)
    name_entry.pack(padx=24, pady=(0, 10))
    name_entry.focus_set()
    name_entry.select_range(0, "end")

    paths_text = ctk.CTkLabel(
        window,
        text=paths_label,
        justify="left",
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    )
    paths_text.pack(padx=24, pady=(4, 2), anchor="w")
    hint = ctk.CTkLabel(
        window,
        text=paths_hint,
        justify="left",
        wraplength=440,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    )
    hint.pack(padx=24, anchor="w")

    rows = ctk.CTkScrollableFrame(
        window, width=440, height=150, fg_color=palette.well, corner_radius=8
    )
    rows.pack(padx=24, pady=(6, 4), fill="x")
    entries: list[tuple[ctk.CTkCheckBox, ctk.CTkEntry]] = []

    def fill_from_browse(field: ctk.CTkEntry) -> None:
        """把系统目录选择框里挑到的路径填进这一行(用户取消时保持原样)."""
        if browse is None:  # pragma: no cover - 按钮只在 browse 非空时创建
            return
        picked = browse()
        if picked:
            field.delete(0, "end")
            field.insert(0, picked)

    def add_row(value: str) -> None:
        row = ctk.CTkFrame(rows, fg_color="transparent")
        row.pack(fill="x", pady=2)
        box = ctk.CTkCheckBox(
            row,
            text="",
            variable=ctk.BooleanVar(value=True),
            width=20,
            checkbox_width=18,
            checkbox_height=18,
            fg_color=palette.accent,
            hover_color=palette.accent_soft_border,
            border_color=palette.border,
        )
        box.pack(side="left", padx=(4, 6))
        entry = ctk.CTkEntry(
            row,
            fg_color=palette.input_bg,
            border_color=palette.input_border,
            text_color=palette.text_body,
        )
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        if browse is not None:
            browse_btn = ctk.CTkButton(
                row,
                text=tr("dialog.browse"),
                width=64,
                height=28,
                fg_color=palette.raised,
                hover_color=palette.item_hover,
                text_color=palette.text_body,
                command=lambda field=entry: fill_from_browse(field),
            )
            browse_btn.pack(side="left", padx=(0, 4))
        entries.append((box, entry))

    for path in initial_paths:
        add_row(path)
    if not initial_paths:
        add_row("")

    result: list[tuple[str, tuple[str, ...]]] = []

    def submit() -> None:
        name = name_entry.get().strip()
        if not name:
            return
        chosen = tuple(
            entry.get().strip()
            for box, entry in entries
            if box.get() and entry.get().strip()
        )
        result.append((name, chosen))
        window.destroy()

    name_entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(6, 20))
    add = ctk.CTkButton(
        buttons,
        text=add_text,
        width=116,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=lambda: add_row(""),
    )
    add.pack(side="left", padx=(0, 10))
    cancel = ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    )
    cancel.pack(side="left", padx=(0, 10))
    ok = ctk.CTkButton(
        buttons,
        text=ok_text,
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    )
    ok.pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def edit_tags_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    tags: Sequence[str] = (),
    max_tags: int = MAX_TAGS,
    max_length: int = MAX_TAG_LENGTH,
) -> tuple[str, ...] | None:
    """编辑一款游戏的自定义标签: 一行一个, 与导入时调整存档路径同一个样式.

    每行一个输入框加一个"删除", 底部是"添加标签 / 取消 / 保存"; 行数达到上限后
    "添加标签"不可用(与 :func:`~archive_management.domain.home.normalize_tags` 的
    上限一致)。回车等同于保存; 空行与重复项直接丢掉, 中英文逗号都会被剔除。返回
    清理后的标签, 取消时返回 ``None``。
    """
    window = ctk.CTkToplevel(parent)
    window.title(tr("dialog.home_tags_title"))
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    label = ctk.CTkLabel(
        window,
        text=tr("dialog.tags_label", max=max_tags, length=max_length),
        justify="left",
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    )
    label.pack(padx=24, pady=(20, 2), anchor="w")
    hint = ctk.CTkLabel(
        window,
        text=tr("dialog.tags_hint"),
        anchor="w",
        justify="left",
        wraplength=440,
        font=ctk.CTkFont(size=12),
        text_color=palette.text_hint,
    )
    hint.pack(padx=24, anchor="w")

    # 按钮先建好(回调里要按行数切"添加标签"的可用状态), 打包留到最后 —— Tk 的布局
    # 按 pack 的顺序, 与创建顺序无关。
    buttons = ctk.CTkFrame(window, fg_color="transparent")
    add = ctk.CTkButton(
        buttons,
        text=tr("dialog.tags_add"),
        width=116,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=lambda: add_row(""),
    )
    add.pack(side="left", padx=(0, 10))
    cancel = ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    )
    cancel.pack(side="left", padx=(0, 10))
    ok = ctk.CTkButton(
        buttons,
        text=tr("dialog.tags_save"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=lambda: submit(),
    )
    ok.pack(side="left")

    rows = ctk.CTkScrollableFrame(
        window, width=440, height=150, fg_color=palette.well, corner_radius=8
    )
    # 三行内容也立着一条拖不动的滚动条只是噪声(21 号评审).
    auto_scrollbar(rows)
    entries: list[ctk.CTkEntry] = []
    result: list[tuple[str, ...]] = []

    def refresh_add_state() -> None:
        """行数到上限后不允许再加(上限与 normalize_tags 一致)."""
        add.configure(state=_add_state(len(entries), max_tags))
        # 置灰必须连配色一起改: 只改 state 的话按钮底色仍是亮的(49 号评审).
        paint_button_state(add, palette, "ghost")

    def remove_row(row: ctk.CTkFrame, entry: ctk.CTkEntry) -> None:
        """删掉一行; 删空了补一个空行, 窗户里总有一个输入框可用."""
        row.destroy()
        if _drop_tag_row(entries, entry):
            add_row("")
        refresh_add_state()

    def refresh_row_state(entry: ctk.CTkEntry, remove: ctk.CTkButton) -> None:
        """按行内是否填了内容切换"删除"的可用态."""
        _paint_row_remove(remove, palette, filled=bool(entry.get().strip()))

    def add_row(value: str) -> None:
        """追加一行(输入框 + "删除"); 到达上限就什么也不做.

        "添加标签"按钮那时已经是禁用状态, 这里再加一道兜底: 行的数量永远不会
        超过 ``normalize_tags`` 能保留下来的上限。
        """
        if len(entries) >= max_tags:
            return
        row = ctk.CTkFrame(rows, fg_color="transparent")
        row.pack(fill="x", pady=2)
        entry = ctk.CTkEntry(
            row,
            fg_color=palette.input_bg,
            border_color=palette.input_border,
            text_color=palette.text_body,
        )
        entry.insert(0, value)
        entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        entry.bind("<Return>", lambda _event: submit())
        remove = ctk.CTkButton(
            row,
            text=tr("dialog.tags_remove"),
            width=64,
            height=28,
            fg_color=palette.raised,
            hover_color=palette.item_hover,
            text_color=palette.text_body,
            command=lambda: remove_row(row, entry),
        )
        remove.pack(side="left")
        entry.bind("<KeyRelease>", lambda _event: refresh_row_state(entry, remove))
        refresh_row_state(entry, remove)
        entries.append(entry)
        refresh_add_state()

    def submit() -> None:
        """收集非空行, 清理(去空白/去重/截断)后交给调用方."""
        result.append(normalize_tags([entry.get() for entry in entries]))
        window.destroy()

    for tag in tags[:max_tags]:
        add_row(tag)
    if len(entries) < max_tags:
        add_row("")

    rows.pack(padx=24, pady=(6, 4), fill="x")
    buttons.pack(padx=24, pady=(6, 20))

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def ask_text(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    text: str,
    initial: str = "",
    browse: Callable[[], str | None] | None = None,
    confirm_text: str | None = None,
    allow_empty: bool = False,
    context: str = "",
) -> str | None:
    """询问一段文本(如游戏名或路径); 取消或内容为空返回 None.

    ``browse`` 非 None 时显示"浏览"按钮, 点击后把返回的路径填入输入框.
    ``allow_empty`` 为 True 时允许提交空内容(返回 ``""``), 供"清空配置"
    这类需要区分"取消"与"留空"的场景使用.
    ``context`` 非空时在提示下方补一行上下文(如重命名时的"当前名称")。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=text,
        anchor="w",
        justify="left",
        wraplength=_TEXT_WIDTH,
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    ).pack(fill="x", padx=24, pady=(20, 6))
    if context:
        ctk.CTkLabel(
            window,
            text=context,
            anchor="w",
            justify="left",
            wraplength=_TEXT_WIDTH,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(fill="x", padx=24, pady=(0, 8))

    entry = ctk.CTkEntry(
        window,
        width=_TEXT_WIDTH,
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        text_color=palette.text_body,
    )
    entry.insert(0, initial)
    entry.pack(fill="x", padx=24, pady=(0, 8))
    entry.focus_set()
    # "回车也能提交"要写在界面上: 否则用户只能靠猜(19 号评审)。
    ctk.CTkLabel(
        window,
        text=tr("dialog.enter_hint"),
        anchor="w",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    ).pack(fill="x", padx=24, pady=(0, 12))

    result: list[str] = []

    def submit() -> None:
        value = entry.get().strip()
        if value or allow_empty:
            result.append(value)
            window.destroy()

    entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 16))
    cancel = ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    )
    cancel.pack(side="left", padx=(0, 10))

    def fill_from_browse() -> None:
        if browse is not None:  # pragma: no branch - 按钮只在 browse 非空时创建
            picked = browse()
            if picked:
                entry.delete(0, "end")
                entry.insert(0, picked)

    if browse is not None:
        browse_btn = ctk.CTkButton(
            buttons,
            text=tr("dialog.browse"),
            width=96,
            height=32,
            fg_color=palette.raised,
            hover_color=palette.item_hover,
            text_color=palette.text_body,
            command=fill_from_browse,
        )
        browse_btn.pack(side="left", padx=(0, 10))

    ok = ctk.CTkButton(
        buttons,
        text=ok_text,
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    )
    ok.pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def edit_backup_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    name_label: str,
    desc_label: str,
    initial_name: str = "",
    initial_desc: str = "",
    limit: int = 200,
) -> tuple[str, str] | None:
    """在同一个窗口里编辑备份名称与描述; 取消返回 None.

    ``limit`` 为描述的字数上限: 超限时不会提交并把计数器标红, 避免静默
    截断用户输入(服务层还会再校验一次)。

    描述的去处**不写提示**: 它只在时间线卡片与右侧选中面板里显示, 而分支视图
    是一张图(框里没有描述) —— "描述会显示在备份卡片上" 这句在默认视图里就是错的。
    """
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    section_font = ctk.CTkFont(size=12)
    ctk.CTkLabel(
        window,
        text=title,
        anchor="w",
        font=ctk.CTkFont(size=15, weight="bold"),
        text_color=palette.text_primary,
    ).pack(padx=24, pady=(20, 10), anchor="w")

    ctk.CTkLabel(
        window,
        text=name_label,
        anchor="w",
        font=section_font,
        text_color=palette.text_body,
    ).pack(padx=24, anchor="w")
    name_entry = ctk.CTkEntry(
        window,
        width=420,
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        text_color=palette.text_body,
    )
    name_entry.insert(0, initial_name)
    name_entry.pack(padx=24, pady=(6, 16))
    name_entry.focus_set()

    ctk.CTkLabel(
        window,
        text=desc_label,
        anchor="w",
        font=section_font,
        text_color=palette.text_body,
    ).pack(padx=24, anchor="w")
    # 空着的描述框零信息量: 给一句占位说明, 聚焦时自动清掉(22 号评审).
    desc_placeholder = tr("dialog.backup_desc_placeholder")
    desc_box = ctk.CTkTextbox(
        window,
        width=420,
        height=110,
        wrap="word",
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        border_width=1,
        text_color=palette.text_body,
        font=ctk.CTkFont(size=12),
    )
    desc_box.insert("1.0", initial_desc or desc_placeholder)
    if not initial_desc:
        desc_box.configure(text_color=palette.text_muted)
    desc_box.pack(padx=24, pady=(6, 4))
    counter = ctk.CTkLabel(
        window,
        text="",
        anchor="e",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    )
    counter.pack(padx=24, pady=(0, 20), anchor="e")

    result: list[tuple[str, str]] = []

    def current_desc() -> str:
        """当前描述; 展示中的占位文案不算用户输入."""
        text = str(desc_box.get("1.0", "end")).strip()
        return "" if text == desc_placeholder else text

    def clear_placeholder() -> None:
        """聚焦时清掉占位文案: 否则用户一打字就得先删掉这段灰字."""
        if str(desc_box.get("1.0", "end")).strip() != desc_placeholder:
            return
        desc_box.delete("1.0", "end")
        desc_box.configure(text_color=palette.text_body)

    def refresh_counter(_event: object = None) -> None:
        length = len(current_desc())
        counter.configure(
            text=tr("dialog.counter", count=length, limit=limit),
            text_color=palette.danger if length > limit else palette.text_muted,
        )

    def submit() -> None:
        if len(current_desc()) > limit:
            refresh_counter()
            return
        result.append((name_entry.get().strip(), current_desc()))
        window.destroy()

    name_entry.bind("<Return>", lambda _event: submit())
    desc_box.bind("<KeyRelease>", refresh_counter)
    desc_box.bind("<FocusIn>", lambda _event: clear_placeholder())
    refresh_counter()

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 20))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.schedule_save"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def schedule_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    interval_label: str,
    interval_prompt: str,
    keep_label: str,
    keep_prompt: str,
    initial_interval: str = "",
    initial_keep: str = "3",
    current: str = "",
) -> tuple[str, str] | None:
    """在同一个窗口里设置定期备份周期与自动备份保留份数; 取消返回 None.

    周期与保留份数由调用方解析校验: 本函数只收集原始文本, 便于沿用既有
    的错误反馈路径。``current`` 是"当前值"这种**状态**, 单独占一行而不是塞在
    说明句尾 —— 塞在句尾时用户扫一眼找不到现在是什么状态(25 号评审)。
    """
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    section_font = ctk.CTkFont(size=12)
    hint_font = ctk.CTkFont(size=11)
    ctk.CTkLabel(
        window,
        text=title,
        anchor="w",
        font=ctk.CTkFont(size=15, weight="bold"),
        text_color=palette.text_primary,
    ).pack(fill="x", padx=24, pady=(20, 6), anchor="w")
    # 标题与字段之间画一条线: 不然标题看起来像第一个字段的标签(25 号评审).
    ctk.CTkFrame(window, height=1, fg_color=palette.border).pack(
        padx=24, pady=(0, 10), fill="x"
    )
    if current:
        ctk.CTkLabel(
            window,
            text=current,
            anchor="w",
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        ).pack(fill="x", padx=24, pady=(0, 10), anchor="w")

    def section(text: str) -> None:
        ctk.CTkLabel(
            window,
            text=text,
            anchor="w",
            font=section_font,
            text_color=palette.text_body,
        ).pack(fill="x", padx=24, anchor="w")

    def hint(text: str, *, bottom: int = 6) -> None:
        """字段的说明: 紧跟在它自己的输入框下面, 形成一组(25 号评审的"节奏碎")."""
        ctk.CTkLabel(
            window,
            text=text,
            anchor="w",
            justify="left",
            wraplength=420,
            font=hint_font,
            text_color=palette.text_muted,
        ).pack(fill="x", padx=24, pady=(0, bottom), anchor="w")

    section(interval_label)
    interval_entry = ctk.CTkEntry(
        window,
        width=_SCHEDULE_FIELD_WIDTH,
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        text_color=palette.text_body,
    )
    interval_entry.insert(0, initial_interval)
    interval_entry.pack(padx=24, pady=(4, 2), anchor="w")
    interval_entry.focus_set()
    hint(interval_prompt)

    section(keep_label)
    keep_entry = ctk.CTkEntry(
        window,
        width=_SCHEDULE_FIELD_WIDTH,
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        text_color=palette.text_body,
    )
    keep_entry.insert(0, initial_keep)
    keep_entry.pack(padx=24, pady=(4, 2), anchor="w")
    hint(keep_prompt, bottom=14)

    result: list[tuple[str, str]] = []

    def submit() -> None:
        result.append((interval_entry.get().strip(), keep_entry.get().strip()))
        window.destroy()

    interval_entry.bind("<Return>", lambda _event: submit())
    keep_entry.bind("<Return>", lambda _event: submit())

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 20))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.schedule_save"),
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def restore_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    summary: str,
    safety_label: str,
    safety_hint: str,
    safety_available: bool,
    safety_default: bool = True,
    danger_note: str = "",
) -> bool | None:
    """在同一个窗口里确认恢复并选择是否先创建安全点; 取消返回 None.

    返回 ``True`` 表示恢复前先创建安全点(默认), ``False`` 表示直接恢复。
    当前没有可备份内容时选项被禁用且强制为 ``False``; 风险提示(例如检测到
    游戏进程在运行)与选项在同一窗口展示, 不再额外弹窗。
    """
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    _bullet_block(window, summary, palette, wraplength=380, top_pad=18)
    ctk.CTkFrame(window, height=6, fg_color="transparent").pack(fill="x")

    if danger_note:
        ctk.CTkLabel(
            window,
            text=danger_note,
            anchor="w",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=12),
            text_color=palette.danger,
        ).pack(padx=24, pady=(0, 10), fill="x")
        # 警告与下方的选项之间画一条分割线: 只隔十几像素时, 它会被当成复选框的说明
        # (23 号评审)。
        ctk.CTkFrame(window, height=1, fg_color=palette.border).pack(
            padx=24, pady=(0, 12), fill="x"
        )

    safety_var = ctk.BooleanVar(value=safety_default and safety_available)

    def option(
        label: str,
        hint_text: str,
        variable: ctk.BooleanVar,
        *,
        available: bool,
    ) -> None:
        box = ctk.CTkCheckBox(
            window,
            text=label,
            variable=variable,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
            # 禁用态的字色也要来自调色板: 不给的话会落到 CustomTkinter 主题里的灰
            # (与这套界面无关的另一个灰阶, 49 号评审)。
            text_color_disabled=palette.text_disabled,
            # 勾选态不用主按钮那种实心绿: 一个是"选项状态", 一个是"执行"(23 号评审).
            fg_color=palette.accent_soft_border,
            hover_color=palette.accent_soft,
            checkmark_color=palette.text_primary,
        )
        if not available:
            box.configure(state="disabled")
        box.pack(padx=24, pady=(0, 2), anchor="w")
        ctk.CTkLabel(
            window,
            text=hint_text,
            anchor="w",
            justify="left",
            wraplength=400,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(padx=_CHECK_HINT_PAD, pady=(0, 10), anchor="w")

    option(safety_label, safety_hint, safety_var, available=safety_available)

    result: list[bool] = []

    def submit() -> None:
        result.append(bool(safety_var.get()) and safety_available)
        window.destroy()

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 20))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.restore_confirm"),
        width=112,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


def import_package_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    prompt: ImportPrompt,
    locations_label: str,
    locations_hint: str,
    strategy_label: str,
    strategies: Sequence[tuple[str, str]],
    target_label: str,
    target_hint: str,
    target_locked_hint: str,
    confirm_text: str | None = None,
) -> ImportChoice | None:
    """展示包内容与冲突项, 让用户选导入方式并映射存档位置; 取消返回 ``None``.

    ``strategies`` 是 ``(策略键, 文案)`` 列表(调用方按"有没有可合并的游戏"决定
    是否提供"合并"); ``prompt`` 里的每个存档位置一行输入框, **留空表示这一条不
    导入**, 只有本就存在于本机的包内路径才会被预填。目标游戏只在"合并"时返回。

    正文放在一个按屏幕高度封顶的滚动区里(矮屏上底部的按钮不会被切掉); 单选按钮的
    选中态用淡强调色而不是主按钮那种实心绿, 免得同一屏里绿色既表示"选中"又表示
    "执行"(28 号评审)。

    与其它对话框一致: 只在主线程调用, 窗口居中于主窗口, 用户点"取消"或直接关窗
    都返回 ``None``(调用方据此什么都不做)。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)
    body = _dialog_body(window, palette)

    ctk.CTkLabel(
        body,
        text=prompt.summary,
        anchor="w",
        justify="left",
        wraplength=460,
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    ).pack(padx=24, pady=(20, 6), anchor="w")

    if prompt.match_text:
        ctk.CTkLabel(
            body,
            text=prompt.match_text,
            anchor="w",
            justify="left",
            wraplength=460,
            font=ctk.CTkFont(size=12),
            text_color=palette.text_hint,
        ).pack(padx=24, pady=(0, 6), anchor="w")

    _dialog_section(body, palette, locations_label)
    _dialog_hint(body, palette, locations_hint)
    # 高度按"两条位置 + 行间分割线"给够: 行距按 28 号评审拉开后, 120 高会把第二条输入框
    # 裁掉一半, 滚动条也就一直立着。
    rows = ctk.CTkScrollableFrame(
        body, width=460, height=150, fg_color=palette.well, corner_radius=8
    )
    # 说明到第一条输入框本来就只隔几像素, 更要多给一点间距, 否则它看起来属于第一行。
    rows.pack(padx=24, pady=(8, 10), fill="x")
    auto_scrollbar(rows)
    entries = _import_location_rows(rows, palette, prompt.locations)
    _dialog_section(body, palette, strategy_label)
    strategy_var = ctk.StringVar(value=strategies[0][0])
    for key, text in strategies:
        ctk.CTkRadioButton(
            body,
            text=text,
            value=key,
            variable=strategy_var,
            command=lambda: _paint_targets(),
            font=ctk.CTkFont(size=12),
            text_color=palette.text_body,
            text_color_disabled=palette.text_disabled,
            # 选中态不用主按钮那种实心绿: 一个是"选中项", 一个是"执行"(28 号评审).
            fg_color=palette.accent_soft_border,
            hover_color=palette.accent_soft,
            border_color=palette.border,
        ).pack(padx=24, pady=(2, 0), anchor="w")

    target_var = ctk.StringVar(value=_default_target(prompt.targets))
    # 只登记"能接受 state 参数"的控件: 滚动容器的 configure 不吃 state(会报未知选项).
    target_parts: list[ctk.CTkBaseClass] = []
    target_hint_label: ctk.CTkBaseClass | None = None
    if prompt.targets:
        target_parts.append(_dialog_section(body, palette, target_label))
        target_hint_label = _dialog_hint(body, palette, target_hint)
        target_parts.append(target_hint_label)
        target_rows = ctk.CTkScrollableFrame(
            body, width=460, height=110, fg_color=palette.well, corner_radius=8
        )
        target_rows.pack(padx=24, pady=(6, 10), fill="x")
        auto_scrollbar(target_rows)
        for option in prompt.targets:
            radio = ctk.CTkRadioButton(
                target_rows,
                text=option.label,
                value=option.game_id,
                variable=target_var,
                font=ctk.CTkFont(size=12),
                text_color=palette.text_body,
                text_color_disabled=palette.text_disabled,
                fg_color=palette.accent_soft_border,
                hover_color=palette.accent_soft,
                border_color=palette.border,
            )
            radio.pack(padx=4, pady=2, anchor="w")
            target_parts.append(radio)

    def _paint_targets() -> None:
        """只有"合并"才需要目标游戏: 其余方式把目标区置灰并写明原因.

        只置灰不提原因时, 用户会以为自己的选择被忽略了(28 号评审); 这里把说明文字
        换成"先选合并才能挑目标", 位置不变、高度也不变。
        """
        merging = strategy_var.get() == STRATEGY_MERGE
        if target_hint_label is not None:
            target_hint_label.configure(
                text=target_hint if merging else target_locked_hint
            )
        state = "normal" if merging else "disabled"
        for part in target_parts:
            part.configure(state=state)

    _paint_targets()

    result: list[ImportChoice] = []

    def submit() -> None:
        strategy = strategy_var.get()
        chosen = {row.index: entry.get().strip() for row, entry in entries}
        result.append(
            ImportChoice(
                strategy=strategy,
                target_game_id=_chosen_target(strategy, target_var.get()),
                locations={index: path for index, path in chosen.items() if path},
            )
        )
        window.destroy()

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 20))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=ok_text,
        width=96,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _fit_dialog_body(body, _dialog_body_height(window))
    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


@dataclass(frozen=True)
class _BatchTicks:
    """批量导出对话框的勾选状态: 全选框 + 逐行变量(两者共用同一份变量表)."""

    master: ctk.CTkCheckBox
    variables: dict[str, ctk.BooleanVar]

    def selected(self) -> dict[str, bool]:
        """逐行取值(提交时用)."""
        return {game_id: bool(var.get()) for game_id, var in self.variables.items()}


def _pack_batch_row(row: ctk.CTkFrame) -> None:
    """把一行勾选卡片放回列表(行会随筛选隐藏/重现, 边距只能走同一个入口)."""
    row.pack(fill="x", padx=6, pady=(2, 4))


def _batch_export_row(
    rows: ctk.CTkScrollableFrame,
    palette: Palette,
    option: BatchExportOption,
    variable: ctk.BooleanVar,
    *,
    on_tick: Callable[[], None],
) -> ctk.CTkFrame:
    """批量导出列表里的一行: 一行一张卡片, 游戏名与元信息分两行.

    名字与元信息分行是关键: 原来名字、"原始名称: X"、"已停用"全部串在一个复选框标签
    里, 三行长短不一, 两种灰色也分不出哪个是哪个(27 号评审)。现在勾选框只写游戏名,
    元信息左对齐在固定位置, 原名(提示色)与状态摘要(次要色)各用各的颜色。
    """
    row = ctk.CTkFrame(rows, fg_color=palette.card, corner_radius=6)
    _pack_batch_row(row)
    ctk.CTkCheckBox(
        row,
        text=option.name,
        variable=variable,
        font=ctk.CTkFont(size=12),
        text_color=palette.text_body,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        border_color=palette.border,
        command=on_tick,
    ).pack(padx=10, pady=(6, 0), anchor="w")
    meta = ctk.CTkFrame(row, fg_color="transparent")
    meta.pack(fill="x", padx=10, pady=(0, 6), anchor="w")
    if option.original_label:
        ctk.CTkLabel(
            meta,
            text=f"{_BULLET}{option.original_label}",
            anchor="w",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_hint,
        ).pack(side="left", padx=(0, 8))
    ctk.CTkLabel(
        meta,
        text=option.detail,
        anchor="w",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    ).pack(side="left")
    return row


def _build_batch_game_list(
    window: ctk.CTk,
    palette: Palette,
    *,
    prompt: BatchExportPrompt,
    list_label: str,
    select_all_label: str,
    select_all_scope: str,
    no_match_text: str,
    filter_box: ctk.CTkEntry,
    on_change: Callable[[Mapping[str, bool]], None],
) -> tuple[_BatchTicks, Callable[[object], None]]:
    """建"全选框 + 可滚动勾选列表", 返回勾选状态与"按筛选刷新显示"的回调.

    筛选只影响显示: 勾选状态存在按游戏 id 索引的变量里, 被筛掉的行不会丢掉用户做过的
    选择。全选框作用于**当前筛选后的行**(筛掉的行保持用户原来的勾选), 自己的状态则始终
    反映"可见行是否已全部勾上", 没有可见行时置灰。

    ``on_change`` 在勾选变化时收到"逐行勾选状态", 调用方据此维护"可以提交了吗"
    (27 号评审: 一个都没勾时确认按钮仍是亮的, 点了才被告知没勾选)。
    """
    _dialog_section(window, palette, list_label)
    master_var = ctk.BooleanVar(value=all(option.selected for option in prompt.options))
    master = ctk.CTkCheckBox(
        window,
        text=select_all_label,
        variable=master_var,
        font=ctk.CTkFont(size=12),
        text_color=palette.text_body,
        # 没有可见行时全选框是禁用的, 禁用字色必须来自调色板(49 号评审)。
        text_color_disabled=palette.text_disabled,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        border_color=palette.border,
    )
    master.pack(padx=24, pady=(6, 0), anchor="w")
    # 限定条件单独一行: 跟在"全选"后面同字号同颜色, 很容易被读成标签的一部分(27 号评审)。
    ctk.CTkLabel(
        window,
        text=select_all_scope,
        anchor="w",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_hint,
    ).pack(padx=24, pady=(0, 2), anchor="w")
    rows = ctk.CTkScrollableFrame(
        window, width=460, height=220, fg_color=palette.well, corner_radius=8
    )
    rows.pack(padx=24, pady=(6, 10), fill="x")
    # 四行内容也立着一条拖不动的滚动条(27 号评审)。
    auto_scrollbar(rows)
    no_match = ctk.CTkLabel(
        rows,
        text=no_match_text,
        anchor="w",
        font=ctk.CTkFont(size=12),
        text_color=palette.text_muted,
    )
    variables: dict[str, ctk.BooleanVar] = {}
    # 勾选状态的取值器提前建好: 它持有同一个字典的引用, 后面逐行填充即可。
    ticks = _BatchTicks(master=master, variables=variables)
    lines: list[tuple[BatchExportOption, ctk.CTkFrame]] = []

    def visible_ids() -> set[str]:
        """当前筛选后仍然显示的游戏 id(筛选只影响显示)."""
        return {
            option.game_id
            for option in filter_export_options(prompt.options, str(filter_box.get()))
        }

    def refresh_master() -> None:
        """刷新全选框: 没有可见行时置灰, 否则反映"可见行是否已全部勾上"."""
        visible = visible_ids()
        master.configure(state="normal" if visible else "disabled")
        master_var.set(bool(visible) and all(variables[gid].get() for gid in visible))
        on_change(ticks.selected())

    def toggle_all() -> None:
        """点全选: 只改**当前筛选后的**行, 筛掉的行保持用户原来的勾选."""
        wanted = bool(master_var.get())
        for game_id in visible_ids():
            variables[game_id].set(wanted)
        refresh_master()

    master.configure(command=toggle_all)

    for option in prompt.options:
        variables[option.game_id] = ctk.BooleanVar(value=option.selected)
        lines.append(
            (
                option,
                _batch_export_row(
                    rows,
                    palette,
                    option,
                    variables[option.game_id],
                    on_tick=refresh_master,
                ),
            )
        )

    def apply_filter(_event: object = None) -> None:
        """按输入内容显示/隐藏行; 一条都不匹配时给出提示(勾选状态不受影响)."""
        visible = visible_ids()
        for option, row in lines:
            if option.game_id in visible:
                _pack_batch_row(row)
            else:
                row.pack_forget()
        if visible:
            no_match.pack_forget()
        else:
            no_match.pack(padx=6, pady=8, anchor="w")
        refresh_master()

    refresh_master()
    return ticks, apply_filter


def export_batch_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    prompt: BatchExportPrompt,
    filter_label: str,
    list_label: str,
    no_match_text: str,
    select_all_label: str,
    select_all_scope: str,
    confirm_text: str | None = None,
) -> BatchExportChoice | None:
    """让用户勾选要批量导出的游戏; 取消返回 ``None``.

    列表上方是一个**可输入的筛选框**(与定时任务窗口的选择框同一套做法: 输入即过滤),
    但过滤只影响显示 —— 勾选状态存在按游戏 id 索引的变量里, 随后被筛掉的行不会丢掉
    用户已经做过的选择(勾选是用户明确表达过的意思, 不该被一次输入悄悄丢掉)。

    筛选框下面还有一个**全选框**: 它作用于**当前筛选后的行**(筛掉的行保持用户原来的
    勾选 —— 与上面同一条语义), 自己的状态则始终反映"可见行是否已全部勾上", 没有
    可见行时置灰; 它旁边单独一行写明这个范围。

    一份都没勾选时确认按钮是**禁用态**: 点了才被告知"一份都没勾选"太晚了
    (27 号评审)。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=prompt.summary,
        anchor="w",
        justify="left",
        wraplength=460,
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    ).pack(padx=24, pady=(20, 6), anchor="w")

    _dialog_section(window, palette, filter_label)
    _dialog_hint(window, palette, prompt.filter_hint)
    # 普通输入框(不是下拉选框): 这里用户要做的就是打字筛选, 下拉列表只会挡住下面的
    # 勾选列表; 输入即筛选的接线与下面 apply_filter 一致。
    filter_box = ctk.CTkEntry(
        window,
        width=460,
        placeholder_text=filter_label,
        fg_color=palette.input_bg,
        border_color=palette.input_border,
        text_color=palette.text_body,
        font=ctk.CTkFont(size=13),
    )
    filter_box.pack(padx=24, pady=(6, 10))
    # 按钮先建后打包: 确认按钮要能被列表回过来的勾选状态改写(打包顺序仍由下面的
    # buttons.pack 决定, 它排在列表之后)。
    buttons = ctk.CTkFrame(window, fg_color="transparent")
    ok_button = ctk.CTkButton(
        buttons,
        text=ok_text,
        width=104,
        height=32,
        command=lambda: _submit(),
    )
    cancel_button = ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        command=window.destroy,
    )
    # 两个按钮都要显式上色: 不写 fg_color 就会留着 CustomTkinter 的那个蓝
    # (与调色板无关, 换主题也不会变) —— 48 号评审。
    paint_button_style(ok_button, palette, "accent")
    paint_button_style(cancel_button, palette, "ghost")
    cancel_button.pack(side="left", padx=(0, 10))

    def paint_ok(ticked: Mapping[str, bool]) -> None:
        """一份都没勾选时把确认按钮禁用.

        禁用要两件事一起做: ``state`` 让点击真的不生效, ``paint_button_disabled``
        把底色与文字一起压暗 —— 只改颜色等于按钮还亮着, 只改 state 则看起来仍可点
        (27 号评审: 点了才被告知"没有勾选任何游戏"太晚)。
        """
        if any(ticked.values()):
            ok_button.configure(state="normal")
            paint_button_style(ok_button, palette, "accent")
        else:
            ok_button.configure(state="disabled")
            paint_button_disabled(ok_button, palette)

    ticks, apply_filter = _build_batch_game_list(
        window,
        palette,
        prompt=prompt,
        list_label=list_label,
        select_all_label=select_all_label,
        select_all_scope=select_all_scope,
        no_match_text=no_match_text,
        filter_box=filter_box,
        on_change=paint_ok,
    )
    filter_box.bind("<KeyRelease>", apply_filter)

    result: list[BatchExportChoice] = []

    def _submit() -> None:
        result.append(batch_export_choice(ticks.selected(), prompt.options))
        window.destroy()

    ok_button.pack(side="left")
    buttons.pack(padx=24, pady=(0, 20))

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


@dataclass(frozen=True)
class _BatchRowWidgets:
    """批量导入对话框里一行的控件(提交时逐行取值)."""

    row: BatchImportRow
    strategy: ctk.StringVar
    target: ctk.CTkComboBox
    entries: tuple[tuple[ImportLocationRow, ctk.CTkEntry], ...]
    target_parts: tuple[ctk.CTkBaseClass, ...]


def _paint_batch_row(parts: _BatchRowWidgets) -> None:
    """只有"合并"才启用目标选择(与单包对话框同一套语义)."""
    state = "normal" if parts.strategy.get() == STRATEGY_MERGE else "disabled"
    for part in parts.target_parts:
        part.configure(state=state)


def batch_import_dialog(
    parent: ctk.CTk,
    palette: Palette,
    *,
    title: str,
    prompt: BatchImportPrompt,
    locations_label: str,
    locations_hint: str,
    strategy_label: str,
    target_label: str,
    confirm_text: str | None = None,
) -> BatchImportSelection | None:
    """逐款确认批量导入的方式; 取消返回 ``None``.

    一款游戏一张小卡片: 名称与标识、"疑似同一款"的提示、存档位置(留空 = 不导入)、
    策略单选按钮与目标游戏下拉框。语义与单包 :func:`import_package_dialog` 完全一致;
    这里的目标选择用下拉框而不是单选按钮, 因为一批里有好几款游戏, 每款铺一组单选按钮
    排不下 —— 下拉框的取值是**去重后的文案**(重名时带游戏 id, 见
    :func:`~archive_management.ui.models.unique_targets`)。
    """
    ok_text = tr("dialog.confirm") if confirm_text is None else confirm_text
    window = ctk.CTkToplevel(parent)
    window.title(title)
    window.resizable(False, False)
    window.transient(parent)
    window.grab_set()
    window.configure(fg_color=palette.background)

    ctk.CTkLabel(
        window,
        text=prompt.summary,
        anchor="w",
        justify="left",
        wraplength=460,
        font=ctk.CTkFont(size=13),
        text_color=palette.text_body,
    ).pack(padx=24, pady=(20, 0), anchor="w")
    _dialog_hint(window, palette, prompt.hint)

    # 高度能放下**两张**卡片: 批量场景里一屏只看得到一款牌时, 三款包就要滚三次(29 号评审)。
    cards = ctk.CTkScrollableFrame(
        window, width=460, height=420, fg_color=palette.well, corner_radius=8
    )
    cards.pack(padx=24, pady=(8, 10), fill="x")
    auto_scrollbar(cards)

    def build_row(row: BatchImportRow) -> _BatchRowWidgets:
        """一款游戏的一张卡片(控件在这里建, 显示顺序由 pack 决定)."""
        card = ctk.CTkFrame(cards, fg_color=palette.card, corner_radius=8)
        # 左右边距对称, 并且不贴滚动条: 原来左 16 右 8, 卡片的右边界被滚动条压住一点缝
        # (29 号评审)。
        card.pack(fill="x", padx=_CARD_INSET, pady=(2, 4))
        # 名称与元信息同一行: 分开两行时卡片的第二行只有一条灰字, 白占一行高度 —— 批量
        # 场景里一屏只能放下一款牌(29 号评审)。
        head = ctk.CTkFrame(card, fg_color="transparent")
        head.pack(fill="x", padx=_CARD_PAD, pady=(8, 0))
        ctk.CTkLabel(
            head,
            text=row.name,
            anchor="w",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=palette.text_primary,
        ).pack(side="left")
        ctk.CTkLabel(
            head,
            text=row.meta,
            anchor="e",
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(side="right")
        if row.match_text:
            _card_heading(card, palette, row.match_text)
        entries = _batch_location_entries(
            card, palette, row, locations_label, locations_hint
        )
        strategy = ctk.StringVar(value=row.strategy)
        target = ctk.CTkComboBox(
            card,
            values=[option.label for option in row.targets],
            height=_CARD_CONTROL_HEIGHT,
            fg_color=palette.input_bg,
            button_color=palette.raised,
            button_hover_color=palette.raised,
            border_color=palette.input_border,
            text_color=palette.text_body,
            dropdown_fg_color=palette.panel,
            dropdown_text_color=palette.text_body,
            font=ctk.CTkFont(size=12),
            dropdown_font=ctk.CTkFont(size=12),
        )
        heading = _card_heading(card, palette, target_label, pack=False)
        parts = _BatchRowWidgets(
            row=row,
            strategy=strategy,
            target=target,
            entries=entries,
            target_parts=(heading, target),
        )

        def paint() -> None:
            _paint_batch_row(parts)

        # 小标题一律在自己控件上方(与“包内存档位置”“合并到”同一套排法): 原来“导入方式”
        # 的标签在单选按钮左边, 而“合并到”的标签在上方, 同一张卡片里两种排法(29 号评审)。
        _card_heading(card, palette, strategy_label)
        radios = ctk.CTkFrame(card, fg_color="transparent")
        radios.pack(fill="x", padx=_CARD_PAD, pady=(2, 0), anchor="w")
        for key, text in row.strategies:
            ctk.CTkRadioButton(
                radios,
                text=text,
                value=key,
                variable=strategy,
                command=paint,
                font=ctk.CTkFont(size=12),
                text_color=palette.text_body,
                text_color_disabled=palette.text_disabled,
                fg_color=palette.accent_soft_border,
                hover_color=palette.accent_soft,
                border_color=palette.border,
            ).pack(side="left", padx=(0, 8))
        if row.targets:
            heading.pack(padx=_CARD_PAD, pady=(6, 0), anchor="w")
            target.pack(padx=_CARD_PAD, pady=(2, 8), fill="x")
            target.set(target_label_for(row.targets, row.target_game_id))
        paint()
        return parts

    built = [build_row(row) for row in prompt.rows]

    result: list[BatchImportSelection] = []

    def submit() -> None:
        result.append(
            BatchImportSelection(
                choices={
                    parts.row.entry: batch_row_choice(
                        strategy=str(parts.strategy.get()),
                        target=target_game_id(
                            parts.row.targets, str(parts.target.get())
                        ),
                        locations={
                            item.index: str(entry.get())
                            for item, entry in parts.entries
                        },
                    )
                    for parts in built
                }
            )
        )
        window.destroy()

    buttons = ctk.CTkFrame(window, fg_color="transparent")
    buttons.pack(padx=24, pady=(0, 20))
    ctk.CTkButton(
        buttons,
        text=tr("dialog.cancel"),
        width=96,
        height=32,
        fg_color=palette.raised,
        hover_color=palette.item_hover,
        text_color=palette.text_body,
        command=window.destroy,
    ).pack(side="left", padx=(0, 10))
    ctk.CTkButton(
        buttons,
        text=ok_text,
        width=104,
        height=32,
        fg_color=palette.accent,
        hover_color=palette.accent_soft_border,
        text_color=palette.accent_text,
        command=submit,
    ).pack(side="left")

    _present(parent, window)
    parent.wait_window(window)
    return result[0] if result else None


# 批量导入卡片的几何: 左右内边距对称, 输入框与下拉框同高。
#
# 原来左内边距 16 而右边只剩 8(卡片右边界被滚动条压住一条缝), 同一张卡片里输入框
# 28px、下拉框 30px, 四个小标题还只靠颜色区分(绿的那个看起来像超链接)(29 号评审)。
_CARD_PAD = 12
_CARD_INSET = 6
_CARD_CONTROL_HEIGHT = 30
_CARD_TEXT_WIDTH = 400


def _card_heading(
    card: ctk.CTkBaseClass,
    palette: Palette,
    text: str,
    *,
    first: bool = False,
    pack: bool = True,
) -> ctk.CTkBaseClass:
    """批量导入卡片里的小标题: 四个小标题共用同一套样式(次要色 11px, 左对齐).

    需要"先建、晚一点再摆"的调用方(目标游戏那块要排在"导入方式"之后)传
    ``pack=False``, 自己决定摆放位置。
    """
    label = ctk.CTkLabel(
        card,
        text=text,
        anchor="w",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_muted,
    )
    if pack:
        label.pack(padx=_CARD_PAD, pady=(0 if first else 6, 0), anchor="w")
    return label


def _batch_location_entries(
    card: ctk.CTkFrame,
    palette: Palette,
    row: BatchImportRow,
    label: str,
    hint: str,
) -> tuple[tuple[ImportLocationRow, ctk.CTkEntry], ...]:
    """一行游戏卡片里的存档位置区(没有位置时整块不显示)."""
    if not row.locations:
        return ()
    _card_heading(card, palette, label)
    ctk.CTkLabel(
        card,
        text=hint,
        anchor="w",
        justify="left",
        wraplength=_CARD_TEXT_WIDTH,
        font=ctk.CTkFont(size=11),
        text_color=palette.text_hint,
    ).pack(padx=_CARD_PAD, pady=(0, 2), anchor="w")
    entries: list[tuple[ImportLocationRow, ctk.CTkEntry]] = []
    for item in row.locations:
        ctk.CTkLabel(
            card,
            text=item.text,
            anchor="w",
            justify="left",
            wraplength=_CARD_TEXT_WIDTH,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(padx=_CARD_PAD, pady=(2, 0), anchor="w")
        entry = ctk.CTkEntry(
            card,
            height=_CARD_CONTROL_HEIGHT,
            fg_color=palette.input_bg,
            border_color=palette.input_border,
            text_color=palette.text_body,
        )
        entry.insert(0, item.default)
        entry.pack(padx=_CARD_PAD, pady=(2, 0), fill="x")
        entries.append((item, entry))
    return tuple(entries)


def _import_location_rows(
    rows: ctk.CTkScrollableFrame,
    palette: Palette,
    items: Sequence[ImportLocationRow],
) -> list[tuple[ImportLocationRow, ctk.CTkEntry]]:
    """一层存档位置输入框(每条一行: 说明 + 输入框, 行与行之间画一条分割线).

    原来的间距是反的: 说明到第一条输入框只隔 8px, 而两条输入框之间空着 30px —— 读起来
    像两组不同的内容被误分到了一起(28 号评审)。现在每组自己成块, 块之间用分割线断开。
    """
    entries: list[tuple[ImportLocationRow, ctk.CTkEntry]] = []
    for index, item in enumerate(items):
        if index:
            ctk.CTkFrame(rows, height=1, fg_color=palette.border).pack(
                fill="x", pady=(8, 0)
            )
        frame = ctk.CTkFrame(rows, fg_color="transparent")
        frame.pack(fill="x", pady=(6, 2))
        ctk.CTkLabel(
            frame,
            text=item.text,
            anchor="w",
            justify="left",
            wraplength=420,
            font=ctk.CTkFont(size=11),
            text_color=palette.text_muted,
        ).pack(fill="x")
        entry = ctk.CTkEntry(
            frame,
            fg_color=palette.input_bg,
            border_color=palette.input_border,
            text_color=palette.text_body,
        )
        entry.insert(0, item.default)
        entry.pack(fill="x", pady=(2, 0))
        entries.append((item, entry))
    return entries


def _dialog_section(
    window: ctk.CTkBaseClass, palette: Palette, text: str
) -> ctk.CTkBaseClass:
    """对话框里的小节标题(返回控件, 便于按策略置灰整块)."""
    label = ctk.CTkLabel(
        window,
        text=text,
        anchor="w",
        font=ctk.CTkFont(size=12),
        text_color=palette.text_muted,
    )
    label.pack(padx=24, pady=(4, 0), anchor="w")
    return label


def _dialog_hint(
    window: ctk.CTkBaseClass, palette: Palette, text: str
) -> ctk.CTkBaseClass:
    """对话框里的补充说明(成段文字, 用更好读的 text_hint).

    宽度**不写死**: 说明按对话框实际分给它的宽度换行(``fill="x"`` 之后这个宽度就是真实
    可用宽度, 见 :func:`widgets.track_wraplength`)。写死 460 时, 一句话只要比它宽几个
    像素, 末尾的句号就会被挤到第二行独自站着(27 号评审的筛选说明实测如此)。
    """
    label = ctk.CTkLabel(
        window,
        text=text,
        anchor="w",
        justify="left",
        font=ctk.CTkFont(size=11),
        text_color=palette.text_hint,
    )
    label.pack(fill="x", padx=_DIALOG_HINT_PAD, pady=(0, 2), anchor="w")
    track_wraplength(window, label, inset=_DIALOG_HINT_PAD * 2)
    return label


def _dialog_body(
    window: ctk.CTkToplevel, palette: Palette, *, height: int | None = None
) -> ctk.CTkScrollableFrame:
    """建对话框的正文滚动区(内容都在里面, 按钮留在外面).

    高度默认按屏幕算(见 :func:`_dialog_body_height`), 超过就由正文区自己滚 —— 矮屏上
    底部的按钮因此不会被切掉。
    """
    body = ctk.CTkScrollableFrame(
        window,
        fg_color=palette.background,
        width=_DIALOG_BODY_WIDTH,
        height=_dialog_body_height(window) if height is None else height,
        corner_radius=0,
    )
    body.pack(fill="both", expand=True)
    auto_scrollbar(body)
    return body


def _dialog_body_height(window: ctk.CTkToplevel) -> int:
    """正文区的目标高度: 让对话框总高至多占屏幕的 80%, 并夹在上下限之间."""
    budget = _dialog_comfort_limit(window) - _DIALOG_CHROME_HEIGHT
    return max(_DIALOG_BODY_MIN, min(_DIALOG_BODY_MAX, budget))


def _fit_dialog_body(body: ctk.CTkScrollableFrame, cap: int) -> None:
    """把正文区收到"内容高度"(不超过封顶值, 留一点余量).

    封顶值只是上限: 内容比它矮时不该在按钮上方留一大块空白。量不出内容高度时
    (没显示环境/内容还没布局)保持原值, 宁可多留白也不能把内容切成看不见。
    """
    body.update_idletasks()
    canvas = getattr(body, "_parent_canvas", None)
    box = None if canvas is None else canvas.bbox("all")
    if box is None:
        return
    content = int(box[3]) - int(box[1])
    if content > 0:
        body.configure(height=max(_DIALOG_BODY_MIN, min(cap, content + 4)))


def _default_target(targets: Sequence[ImportTargetOption]) -> str:
    """目标游戏的默认选中项: "疑似同一款"(没有就是列表里的第一款)."""
    for option in targets:
        if option.selected:
            return option.game_id
    return targets[0].game_id if targets else ""


def _chosen_target(strategy: str, selected: str) -> str | None:
    """只有"合并"方式才返回目标游戏; 其余方式一律不带.

    目标区在"合并"时一定有预选项(没有可选游戏时界面根本不提供合并), 所以这里
    不做"没选中"的兜底判断: 界面不替用户猜要合并到哪一款。
    """
    return selected if strategy == STRATEGY_MERGE else None
