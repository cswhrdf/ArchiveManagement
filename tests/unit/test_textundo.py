"""输入框撤销(Ctrl+Z / Ctrl+Y)的单元测试.

CustomTkinter 的输入控件**默认没有任何撤销支持**(见
:mod:`archive_management.ui.textundo`): ``CTkEntry`` 内部的
``tkinter.Entry`` 连 ``-undo`` 选项都不存在, ``CTkTextbox`` 的
原生撤销则没打开。本模块用替身钉住这条链路上的四件事:

1. 补丁打在了内部 Tk 控件上 —— 撤销/重做的按键与记录都接在那里;
2. 一次按键 = 一步撤销, 且 **Ctrl+Z 自身不进入历史**(否则按一次
   撤销键会把刚撤掉的内容又推回栈, 表现成"按不动");
3. **程序写入的内容不可撤销** —— 预填默认值、代码清空输入框都算,
   否则用户按 Ctrl+Z 会把看到的默认值抹掉, 或让界面与内容对不上;
4. 只读下拉框不接管按键(它的内容本来就改不了)。

用假 ``tk`` / ``ctk`` 命名空间替换真库: 生产代码靠 ``isinstance``
判断内部控件类型, 因此替身只要类名对得上就能走到真实绑定路径, 不需要
显示环境(ubuntu 的无头 CI 也能跑)。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk
import pytest

from archive_management.ui import textundo

pytestmark = [
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("输入框撤销"),
    pytest.mark.layer("unit"),
]

# Tk 事件里的修饰键位: Ctrl=0x0004, Command(macOS)=0x0008。
_CONTROL = 0x0004
_COMMAND = 0x0008


class _FakeEntry:
    """假 ``tkinter.Entry``: 只有内容、状态与绑定表.

    ``type`` / ``press`` 是模拟真实按键的入口, 顺序与 Tk 一致: 组合键先投递给
    **最具体**的绑定(``<Control-z>``), 普通字符才落到通用的 ``<KeyPress>``。
    """

    def __init__(self, value: str = "", *, state: str = "normal") -> None:
        self.value = value
        self.state_ = state
        self.cursor: object = None
        self.bound: dict[str, list[Callable[..., Any]]] = {}

    def get(self) -> str:
        """当前内容."""
        return self.value

    def delete(self, first: object, last: object | None = None) -> None:
        """删除整段(生产代码只用 ``delete(0, "end")``)."""
        assert last == "end", f"假 Entry 只支持整段删除: {first!r} {last!r}"
        self.value = ""

    def insert(self, index: object, text: str) -> None:
        """插入文本(生产代码只用 ``insert(0, ...)``)."""
        assert index == 0, f"假 Entry 只支持插到开头: {index!r}"
        self.value = text + self.value

    def icursor(self, index: object) -> None:
        """记录光标位置."""
        self.cursor = index

    def cget(self, key: str) -> str:
        """读控件选项(只用到 state)."""
        assert key == "state"
        return self.state_

    def bind(
        self,
        sequence: str | None = None,
        func: Callable[..., Any] | None = None,
        add: object = None,
    ) -> None:
        """登记绑定; ``add`` 表示追加(生产代码一律追加, 不能顶掉库里已有的)."""
        assert add == "+", "撤销绑定必须用 add='+', 否则会顶掉 CTk 自己的处理器"
        if sequence is None or func is None:
            return
        self.bound.setdefault(sequence, []).append(func)

    def fire(self, sequence: str, **attrs: Any) -> list[Any]:
        """触发某个序列上的绑定, 返回各处理器的返回值(便于断言 ``"break"``)."""
        event = SimpleNamespace(**attrs)
        return [func(event) for func in self.bound.get(sequence, [])]

    def type(self, text: str) -> None:
        """逐字符输入: 按键 -> 内容变化 -> 松键, 与真实输入顺序一致."""
        for char in text:
            self.fire("<KeyPress>", keysym=char, state=0)
            self.value += char
            self.fire("<KeyRelease>", keysym=char, state=0)

    def press(self, key: str, *, modifier: int = _CONTROL) -> None:
        """按下一个组合键(按下 + 松开, 与真实键盘一致).

        Ctrl/Command 组合优先匹配具体序列(如 ``<Control-z>``), 而松开只落回通用的
        ``<KeyRelease>`` —— 与实测的 Tk 行为一致。
        """
        self.fire("<KeyPress>", keysym=key, state=modifier)
        self.fire(f"<Control-{key}>", keysym=key, state=modifier)
        self.fire("<KeyRelease>", keysym=key, state=modifier)


class _FakeText:
    """假 ``tkinter.Text``: 用一个真正会记账的撤销栈模拟 Tk 的原生实现.

    ``edit_undo`` / ``edit_redo`` 在栈空时抛 ``TclError`` —— 与 Tk 一致, 这样
    "空栈按 Ctrl+Z"这条路径也走的是真实分支。
    """

    def __init__(self, value: str = "") -> None:
        self.value = value
        self.options: dict[str, Any] = {}
        self.bound: dict[str, list[Callable[..., Any]]] = {}
        self.stack: list[str] = []
        self.redo_stack: list[str] = []
        self.resets = 0

    def configure(self, cnf: object = None, **kwargs: Any) -> None:
        """记录选项(生产代码用 ``undo=True, autoseparators=True``)."""
        assert cnf is None
        self.options.update(kwargs)

    def cget(self, key: str) -> Any:
        """读选项(用例断言 ``undo`` 已打开)."""
        return self.options[key]

    def get(self, first: object, last: object) -> str:
        """取整段文本."""
        assert (first, last) == ("1.0", "end"), f"假 Text 只支持整段: {first} {last}"
        return self.value

    def edit_undo(self) -> None:
        """撤销一步(栈空时与 Tk 一样抛 TclError)."""
        if not self.stack:
            raise tk.TclError("nothing to undo")
        self.redo_stack.append(self.value)
        self.value = self.stack.pop()

    def edit_redo(self) -> None:
        """重做一步(没有可重做的时与 Tk 一样抛 TclError)."""
        if not self.redo_stack:
            raise tk.TclError("nothing to redo")
        self.stack.append(self.value)
        self.value = self.redo_stack.pop()

    def edit_reset(self) -> None:
        """清空撤销栈."""
        self.resets += 1
        self.stack.clear()
        self.redo_stack.clear()

    def bind(
        self,
        sequence: str | None = None,
        func: Callable[..., Any] | None = None,
        add: object = None,
    ) -> None:
        """登记绑定(与 ``_FakeEntry`` 同一约定)."""
        assert add == "+"
        if sequence is None or func is None:
            return
        self.bound.setdefault(sequence, []).append(func)

    def fire(self, sequence: str, **attrs: Any) -> list[Any]:
        """触发某个序列上的绑定."""
        event = SimpleNamespace(**attrs)
        return [func(event) for func in self.bound.get(sequence, [])]


@pytest.fixture
def fake_tk(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """把 ``textundo`` 眼里的 ``tk`` 换成只有替身的命名空间.

    ``TclError`` 保留真实实现: 被测代码靠它判断"栈空/控件已销毁", 换成假的就测不到
    同一条分支了。
    """
    namespace = SimpleNamespace(Entry=_FakeEntry, Text=_FakeText, TclError=tk.TclError)
    monkeypatch.setattr(textundo, "tk", namespace)
    return namespace


def _wired_entry(value: str = "", *, state: str = "normal") -> _FakeEntry:
    """造一个已经接上撤销的输入框(走生产的接法, 不手工绑回调)."""
    entry = _FakeEntry(value, state=state)
    textundo._attach_entry_undo(SimpleNamespace(_entry=entry))
    return entry


def _wired_text(value: str = "") -> _FakeText:
    """造一个已经接上撤销的多行输入框."""
    box = _FakeText(value)
    textundo._attach_text_undo(SimpleNamespace(_textbox=box))
    return box


def test_undo_key_press_is_recognised_by_modifier_and_keysym() -> None:
    """记录历史时要认出"撤销键本身": 带修饰键的 z/y 算, 普通 z 不算."""
    assert textundo._is_undo_keystroke(SimpleNamespace(keysym="z", state=_CONTROL))
    assert textundo._is_undo_keystroke(SimpleNamespace(keysym="Y", state=_COMMAND))
    assert not textundo._is_undo_keystroke(SimpleNamespace(keysym="z", state=0))
    assert not textundo._is_undo_keystroke(SimpleNamespace(keysym="a", state=_CONTROL))


def test_every_undo_and_redo_key_is_bound_on_the_inner_entry(
    fake_tk: SimpleNamespace,
) -> None:
    """Windows/Linux 用 Ctrl 系, macOS 用 Command 系: 六个组合都要绑上.

    补丁只有落在**内部** ``tkinter.Entry`` 上才有用 —— 绑在 CTk 外壳上收不到
    按键(用户反馈"按 Ctrl+Z 没反应"就是这个原因)。
    """
    entry = _wired_entry()

    for key in textundo._UNDO_KEYS + textundo._REDO_KEYS:
        assert key in entry.bound, f"缺少按键绑定: {key}"


def test_typing_then_ctrl_z_walks_back_one_keypress_at_a_time(
    fake_tk: SimpleNamespace,
) -> None:
    """逐字符退: 敲了 abc 之后 Ctrl+Z 一次只撤掉一个字符(不是整段)."""
    entry = _wired_entry()

    entry.type("abc")
    assert entry.value == "abc"

    entry.press("z")
    assert entry.value == "ab"
    entry.press("z")
    assert entry.value == "a"
    entry.press("z")
    assert entry.value == ""


def test_ctrl_y_redoes_the_undone_keystroke(fake_tk: SimpleNamespace) -> None:
    """Ctrl+Y 把刚撤掉的那一字符重做回来."""
    entry = _wired_entry()
    entry.type("ab")
    entry.press("z")
    assert entry.value == "a"

    entry.press("y")
    assert entry.value == "ab"


def test_undo_and_redo_with_an_empty_history_change_nothing(
    fake_tk: SimpleNamespace,
) -> None:
    """没有历史时按 Ctrl+Z / Ctrl+Y 都不报错、不改内容(启动后第一次按键就是这个分支)."""
    entry = _wired_entry()

    entry.press("z")
    entry.press("y")

    assert entry.value == ""
    assert entry.cursor is None, "空历史不该动光标"


def test_a_ctrl_z_keypress_never_enters_the_history(
    fake_tk: SimpleNamespace,
) -> None:
    """回归: 撤销键本身不能被记成一次编辑.

    实测 Tk 会把 Ctrl+Z 只投给最具体的 ``<Control-z>`` 绑定, 但某些平台/输入法
    会同时投给通用的 ``<KeyPress>``。那时若把"按下前的内容"压进历史, 撤销栈会
    **越长越长**: 第一次 Ctrl+Z 撤掉的内容会被记成新的一步, 第二次按下去又把它
    撤回来 —— 用户看到的就是"按两次只动一次、甚至原地打转"。
    """
    entry = _wired_entry()
    entry.type("abc")

    entry.fire("<KeyPress>", keysym="z", state=_CONTROL)
    entry.fire("<Control-z>", keysym="z", state=_CONTROL)
    entry.fire("<KeyRelease>", keysym="z", state=_CONTROL)
    assert entry.value == "ab", "第一次 Ctrl+Z 撤掉最后一个字符"

    entry.press("z")
    assert entry.value == "a", "第二次 Ctrl+Z 应继续往前撤, 而不是把 ab 撤回来"


def test_prefilled_value_is_not_undoable(fake_tk: SimpleNamespace) -> None:
    """回归: 弹窗预填的默认值不能被 Ctrl+Z 抹掉, 并且它就是撤销的底线.

    打开"编辑标签""定时备份"时输入框里已经有内容, 若它进了历史, 用户按一次
    Ctrl+Z 会把默认值清空 —— 看起来像"数据丢了"。
    """
    entry = _wired_entry()
    # 弹窗打开时程序写入默认值(在真实代码里发生在构造之后、用户操作之前)。
    entry.value = "默认名字"
    entry.fire("<FocusIn>")

    entry.press("z")
    assert entry.value == "默认名字", "预填值必须留在框里"

    entry.type("x")
    entry.press("z")
    assert entry.value == "默认名字", "撤销回到预填值, 而不是回到空串"
    entry.press("z")
    assert entry.value == "默认名字", "按到底也抹不掉预填值"


def test_refocusing_after_typing_keeps_the_history(fake_tk: SimpleNamespace) -> None:
    """回归: 点走再点回来不能丢掉用户自己的编辑历史.

    "获得焦点就清栈"是这条需求最容易写错的实现 —— 它顺手把"预填值不可撤销"做对了,
    代价是用户去点一下别的控件、回来再按 Ctrl+Z 就撤不动了。
    """
    entry = _wired_entry()
    entry.fire("<FocusIn>")
    entry.type("ab")
    entry.fire("<FocusIn>")

    entry.press("z")

    assert entry.value == "a"


def test_programmatic_reset_discards_stale_history(
    fake_tk: SimpleNamespace,
) -> None:
    """回归: 代码替用户清空输入框后, Ctrl+Z 不能把旧内容找回来.

    例如"清除筛选"按钮直接把框清空 —— 撤销栈里还留着清空前的文字, 按 Ctrl+Z
    会让输入框里的内容与列表状态对不上。
    """
    entry = _wired_entry()
    entry.fire("<FocusIn>")
    entry.type("旧筛选词")
    entry.value = ""  # 界面代码直接改写(不是用户输入)

    entry.fire("<FocusIn>")
    entry.press("z")

    assert entry.value == "", "程序改写的旧内容不该被撤销回来"


def test_readonly_combo_box_is_not_wired(fake_tk: SimpleNamespace) -> None:
    """只读下拉框(如界面语言)不接管按键: 它的内容本来就改不了."""
    entry = _FakeEntry(state="readonly")
    textundo._attach_entry_undo(SimpleNamespace(_entry=entry))

    assert entry.bound == {}


def test_undo_restores_the_cursor_to_the_end(fake_tk: SimpleNamespace) -> None:
    """撤销后光标回到末尾: 否则接着输入会插到中间, 用户看到字跑到前面去."""
    entry = _wired_entry()
    entry.type("ab")
    entry.press("z")

    assert entry.cursor == "end"


def test_text_box_enables_native_undo_and_binds_the_keys(
    fake_tk: SimpleNamespace,
) -> None:
    """多行输入框用的是 Tk 原生撤销: 选项要打开, 按键也要绑上."""
    box = _wired_text()

    assert box.options["undo"] is True
    assert box.options["autoseparators"] is True
    for key in textundo._UNDO_KEYS + textundo._REDO_KEYS:
        assert key in box.bound, f"缺少按键绑定: {key}"


def test_text_box_undo_and_redo_delegate_to_the_native_stack(
    fake_tk: SimpleNamespace,
) -> None:
    """多行输入框的 Ctrl+Z / Ctrl+Y 直接落到 ``edit_undo`` / ``edit_redo`` 上."""
    box = _wired_text()
    box.stack.append("旧内容")

    assert box.fire("<Control-z>", keysym="z", state=_CONTROL) == ["break"]
    assert box.value == "旧内容"

    assert box.fire("<Control-y>", keysym="y", state=_CONTROL) == ["break"]
    assert box.value == "", "Ctrl+Y 把刚撤掉的内容重做回来"


def test_text_box_swallows_an_empty_undo_stack(fake_tk: SimpleNamespace) -> None:
    """栈空时 Tk 抛 TclError: 必须咽掉并仍然吞掉这次按键, 不能冒泡成崩溃."""
    box = _wired_text()

    assert box.fire("<Control-z>", keysym="z", state=_CONTROL) == ["break"]
    assert box.fire("<Control-y>", keysym="y", state=_CONTROL) == ["break"]


def test_text_box_prefilled_value_is_not_undoable(fake_tk: SimpleNamespace) -> None:
    """多行输入框同样要保护程序写入的内容, 但用户自己的编辑照旧可撤销."""
    box = _wired_text()
    box.value = "预填的描述"
    box.stack.append("")  # Tk 会把程序写入也记进历史

    box.fire("<FocusIn>")
    assert box.resets == 1, "第一次获得焦点要清掉程序写入的历史"
    box.value = "预填的描述 + 用户补充"
    box.stack.append("预填的描述")
    box.fire("<FocusOut>")
    box.fire("<FocusIn>")
    assert box.resets == 1, "点回来不该再清一次(否则用户丢历史)"

    box.fire("<Control-z>", keysym="z", state=_CONTROL)

    assert box.value == "预填的描述"


def test_the_construction_patch_wires_new_widgets_and_is_idempotent(
    fake_tk: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补丁在**构造时**生效, 且重复打补丁不会叠加包装(幂等).

    这是整套方案的关键: 二十多处输入框都不需要单独改一行, 靠的就是包住
    ``CTkEntry`` / ``CTkComboBox`` / ``CTkTextbox`` 的构造器。
    """
    built: list[str] = []

    class _FakeCtkEntry:
        def __init__(self) -> None:
            built.append("entry")
            self._entry = _FakeEntry()

    class _FakeCtkComboBox:
        def __init__(self) -> None:
            built.append("combo")
            self._entry = _FakeEntry()

    class _FakeCtkTextbox:
        def __init__(self) -> None:
            built.append("textbox")
            self._textbox = _FakeText()

    for name, cls in (
        ("CTkEntry", _FakeCtkEntry),
        ("CTkComboBox", _FakeCtkComboBox),
        ("CTkTextbox", _FakeCtkTextbox),
    ):
        monkeypatch.setattr(ctk, name, cls)
    monkeypatch.setattr(textundo, "_APPLIED", False)

    assert textundo.apply_input_undo_support() is True
    assert textundo.apply_input_undo_support() is False, "重复调用应当是空操作"
    assert built == [], "补丁本身不建控件"

    entry = _FakeCtkEntry()
    combo = _FakeCtkComboBox()
    box = _FakeCtkTextbox()

    assert "<Control-z>" in entry._entry.bound
    assert "<Control-z>" in combo._entry.bound
    assert box._textbox.options["undo"] is True
    assert built == ["entry", "combo", "textbox"]
