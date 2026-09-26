"""输入框的撤销 / 重做(Ctrl+Z / Ctrl+Y).

CustomTkinter 的输入控件本身**没有任何撤销支持**(实测, 不是推断):

- ``CTkEntry`` 内部的 ``tkinter.Entry`` 连撤销选项都没有 —— ``entry configure -undo``
  报 ``unknown option "-undo"``(Tk 从来没有给单行输入框实现撤销), 只能自己记历史;
- ``CTkTextbox`` 内部的 ``tkinter.Text`` 有原生撤销(``undo=True`` + ``edit_undo``), 但
  CTk 默认没打开, 也没有绑定按键, 所以按 Ctrl+Z 同样没反应。

这里在 CTk 的三个输入控件(``CTkEntry`` / ``CTkComboBox`` / ``CTkTextbox``)的构造器上
包一层: 造出来的控件自动带上 Ctrl+Z 撤销(以及 Ctrl+Y / Ctrl+Shift+Z 重做; macOS 上同时
支持 Command 组合)。选择"包构造器"而不是"每个创建点手动接一次"的理由: 仓库里输入框有
二十多处, 靠纪律必然会漏, 而漏掉的表现只是"这个框按 Ctrl+Z 没反应" —— 评审和用例都很难
发现(与 :mod:`archive_management.ui.rendering` 里关掉绘制取整是同一类库补丁)。**新增输入
框请照旧用这三个 CTk 控件**(直接用原生 ``tkinter.Entry`` / ``Text`` 就不在补丁范围内,
仓库当前没有这种用法)。

两个细节值得留意:

- **程序写入的内容不可撤销**: 打开"编辑标签""定时备份"这类弹窗时输入框里已经有内容, 若把
  它记进历史, 用户按一次 Ctrl+Z 就会把默认值抹掉; 代码后续替用户清空/重置输入框时同理。
  因此每次获得焦点时都比对一下内容, 发现改动不是用户做的就把历史作废。
- 单行输入框的撤销粒度是**一次按键**(不是整段), 与 ``tkinter.Text`` 的行为一致; 多行
  输入框沿用 Tk 原生的分组(连续输入算一段)。
"""

from __future__ import annotations

import contextlib
import tkinter as tk
from collections.abc import Callable
from typing import Protocol

import customtkinter as ctk

_APPLIED = False

# macOS 上 Command 是 Mod1(Tk 的 state 位 0x0008), Ctrl 是 0x0004。
_CONTROL_MASK = 0x0004
_COMMAND_MASK = 0x0008
_MODIFIER_MASK = _CONTROL_MASK | _COMMAND_MASK

# 撤销/重做的按键: Windows/Linux 用 Ctrl 系, macOS 同时接受 Command 系。
_UNDO_KEYS = ("<Control-z>", "<Command-z>")
_REDO_KEYS = (
    "<Control-y>",
    "<Control-Shift-Z>",
    "<Command-y>",
    "<Command-Shift-Z>",
)
# 撤销栈上限: 单行输入框的历史没必要无限长。
_MAX_HISTORY = 200
# 光标 / 整段范围在 Tk 里的定位方式(Entry 用 ``"end"``, Text 用 ``"1.0"``).
_END = "end"
_TEXT_START = "1.0"


class _Keystroke(Protocol):
    """按键事件里用到的那两个字段(``tk.Event`` 与测试替身都满足).

    ``state`` 在 typeshed 里是 ``int | str``(Tk 允许按字符串给修饰键掩码),
    所以这里照抄, 取值前统一 ``int(...)``。
    """

    keysym: str
    state: int | str


def _is_undo_keystroke(event: _Keystroke) -> bool:
    """判断这次按键是不是"撤销/重做"本身(记录历史时要跳过它自己)."""
    if event.keysym.lower() not in {"z", "y"}:
        return False
    return bool(int(event.state) & _MODIFIER_MASK)


def _try(action: Callable[[], None]) -> None:
    """执行一次原生编辑操作: 栈空或控件已销毁时 Tk 抛 TclError, 按"什么都没发生"处理."""
    with contextlib.suppress(tk.TclError):
        action()


def _invoke(action: Callable[[], None]) -> str:
    """执行一次原生撤销/重做并吞掉本次按键(不再传给其它绑定)."""
    _try(action)
    return "break"


class _EntryUndo:
    """单行输入框的撤销栈(``tkinter.Entry`` 没有原生撤销, 只能自己记).

    每次按键**之前**把当前内容收进"待定", 松开时若内容真的变了才压栈 —— 这样方向键、
    单纯移动光标、以及被拦下的按键都不会污染历史。撤销/重做把整段字串换回去, 光标放到
    末尾(单行输入框里的撤销点到为止就够用)。

    另一个坑是**程序改写**: 代码可能在用户没操作的时候直接 ``insert``/``delete``(清空
    筛选、重置表单)。那种改动不该能"撤销回去", 否则 Ctrl+Z 会让输入框和界面状态对不上,
    因此每次获得焦点时都比对一下内容 —— 与上次记录不一致就说明改动的不是用户, 历史作废。
    """

    def __init__(self, entry: tk.Entry) -> None:
        self._entry = entry
        self._done: list[str] = []
        self._undone: list[str] = []
        self._pending: str | None = None
        # 上次"确认过"的内容: 用来识别程序改写(构造时的内容也算一次确认)。
        self._seen = entry.get()

    def watch(self) -> None:
        """开始记录: 每次按键前后各看一眼内容, 获得焦点时清理作废的历史."""
        self._entry.bind("<KeyPress>", self._snapshot, add="+")
        self._entry.bind("<KeyRelease>", self._commit, add="+")
        self._entry.bind("<FocusIn>", self.forget_stale_history, add="+")

    def _snapshot(self, event: tk.Event) -> None:
        """按键之前记下当前内容(撤销/重做键本身不算一次编辑)."""
        if _is_undo_keystroke(event):
            self._pending = None
            return
        self._pending = self._entry.get()

    def _commit(self, _event: tk.Event) -> None:
        """按键之后确认内容变了才真正压栈."""
        pending = self._pending
        self._pending = None
        current = self._entry.get()
        self._seen = current
        if pending is None or pending == current:
            return
        self._done.append(pending)
        del self._done[:-_MAX_HISTORY]
        self._undone.clear()

    def forget_stale_history(self, _event: tk.Event | None = None) -> None:
        """内容与上次记录不一致(程序改写或预填默认值)时丢掉历史."""
        current = self._entry.get()
        if current == self._seen:
            return
        self._seen = current
        self._done.clear()
        self._undone.clear()

    def undo(self, _event: tk.Event | None = None) -> str:
        """撤销一步: 内容为空栈时什么都不做."""
        if not self._done:
            return "break"
        self._undone.append(self._entry.get())
        self._replace(self._done.pop())
        return "break"

    def redo(self, _event: tk.Event | None = None) -> str:
        """重做一步(被撤销过的那一步)."""
        if not self._undone:
            return "break"
        self._done.append(self._entry.get())
        self._replace(self._undone.pop())
        return "break"

    def _replace(self, value: str) -> None:
        """把整段内容换成 ``value``, 光标落到末尾."""
        self._entry.delete(0, _END)
        self._entry.insert(0, value)
        self._entry.icursor(_END)
        self._seen = value


def _attach_entry_undo(widget: object) -> None:
    """给 ``CTkEntry`` / ``CTkComboBox`` 内部的 Entry 接上撤销."""
    entry = getattr(widget, "_entry", None)
    if not isinstance(entry, tk.Entry):  # pragma: no cover - CTk 一定给 Entry
        return
    state = str(entry.cget("state"))
    if state == "readonly":
        # 只读下拉框(比如界面语言)改不了内容, 不需要历史; 它的 Entry 也不接受编辑。
        return
    undo = _EntryUndo(entry)
    undo.watch()
    for key in _UNDO_KEYS:
        entry.bind(key, undo.undo, add="+")
    for key in _REDO_KEYS:
        entry.bind(key, undo.redo, add="+")


class _TextUndo:
    """多行输入框用 ``tkinter.Text`` 的原生撤销.

    ``Text`` 的原生撤销会把**程序写入**的内容(弹窗打开时预填的值)也记进历史, 所以要
    先 :meth:`tkinter.Text.edit_reset`。清栈规则与 :class:`_EntryUndo` 一致: 失去焦点时
    记下内容, 下次获得焦点时若内容已变, 说明改动来自程序, 历史作废 —— 这样既不会让
    Ctrl+Z 抹掉预填值, 也不会让用户每次点回输入框就丢掉自己的编辑历史。
    """

    def __init__(self, box: tk.Text) -> None:
        self._box = box
        self._seen = box.get(_TEXT_START, _END)

    def watch(self) -> None:
        """打开原生撤销并绑上按键."""
        self._box.configure(undo=True, autoseparators=True)
        for key in _UNDO_KEYS:
            self._box.bind(key, self.undo, add="+")
        for key in _REDO_KEYS:
            self._box.bind(key, self.redo, add="+")
        self._box.bind("<FocusIn>", self.forget_stale_history, add="+")
        self._box.bind("<FocusOut>", self.remember_current, add="+")

    def undo(self, _event: tk.Event | None = None) -> str:
        """撤销一步(栈空时什么都没发生)."""
        return _invoke(self._box.edit_undo)

    def redo(self, _event: tk.Event | None = None) -> str:
        """重做一步(没有可重做的则什么都没发生)."""
        return _invoke(self._box.edit_redo)

    def remember_current(self, _event: tk.Event | None = None) -> None:
        """失去焦点时记下内容, 作为下次判断"是否被程序改写"的基准."""
        self._seen = self._box.get(_TEXT_START, _END)

    def forget_stale_history(self, _event: tk.Event | None = None) -> None:
        """内容与上次记录不一致(程序改写或预填默认值)时丢掉历史."""
        current = self._box.get(_TEXT_START, _END)
        if current == self._seen:
            return
        self._seen = current
        _try(self._box.edit_reset)


def _attach_text_undo(widget: object) -> None:
    """给 ``CTkTextbox`` 内部的 Text 打开原生撤销."""
    box = getattr(widget, "_textbox", None)
    if not isinstance(box, tk.Text):  # pragma: no cover - CTk 一定给 Text
        return
    undo = _TextUndo(box)
    try:
        undo.watch()
    except tk.TclError:  # pragma: no cover - 老 Tk 没有 undo 选项
        return


def _wrap_init(
    original: Callable[..., None], attach: Callable[[object], None]
) -> Callable[..., None]:
    """包住控件构造器: 先让 CTk 把内部 Tk 控件建好, 再给它们接上撤销."""

    def patched(self: object, *args: object, **kwargs: object) -> None:
        original(self, *args, **kwargs)
        attach(self)

    return patched


def apply_input_undo_support() -> bool:
    """给 CTk 的输入控件接上撤销, 返回本次是否真的打了补丁(幂等).

    必须在创建任何输入控件之前调用: 补丁替换的是三个类的 ``__init__``, 只对之后创建的
    控件生效(与 :func:`~archive_management.ui.rendering.apply_border_rendering_fix`
    同一套约定)。
    """
    global _APPLIED
    if _APPLIED:
        return False
    ctk.CTkEntry.__init__ = _wrap_init(ctk.CTkEntry.__init__, _attach_entry_undo)
    ctk.CTkComboBox.__init__ = _wrap_init(ctk.CTkComboBox.__init__, _attach_entry_undo)
    ctk.CTkTextbox.__init__ = _wrap_init(ctk.CTkTextbox.__init__, _attach_text_undo)
    _APPLIED = True
    return True
