"""对话框构建逻辑的无头单元测试.

用假控件替换 dialogs 依赖的 customtkinter 控件, 不创建真实窗口,
因此不需要显示环境(GUI 冒烟之外, ubuntu CI 也能稳定执行)。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypedDict, cast

import customtkinter as ctk
import pytest

import archive_management.ui.dialogs as dialogs
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
)
from archive_management.i18n import tr
from archive_management.ui import models
from archive_management.ui.palette import DARK

pytestmark = [
    pytest.mark.dialogs,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("对话框"),
    pytest.mark.story("输入与确认对话框"),
    pytest.mark.layer("unit"),
]


# 提示文字用一个"没有任何标点"的标记: 这些用例断言的是对齐/宽度/能否提交, 与文案本身
# 无关。原来写的是文案原文("请输入游戏名称:") —— 那会让用例跟着文案标点一起变红(I-3
# 把中文句内的半角逗号/冒号统一成全角时就撞过一次), 而文案的标点由
# tests/unit/test_i18n.py 的三条守卫统一管着。
_PROMPT = "提示语"


class _FakeWidget:
    """可 configure / pack / get 的最小假控件."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        self.master = master
        self.kwargs = dict(kwargs)
        self.text = kwargs.get("text")
        self.command: Callable[[], None] | None = kwargs.get("command")
        self._value: str = kwargs.get("text", "")
        self.destroyed = False
        # 测试用 harness 预置过内容时, 对话框自身的 initial 回填不再覆盖它.
        self._preset = False
        # 是否在 pack 之后(pack_forget 会改回 False).
        self.packed = False
        # 最近一次 pack 的参数(用来断言内边距/填充一类布局细节).
        self.pack_kwargs: dict[str, Any] = {}

    def configure(self, **kwargs: Any) -> None:
        self.kwargs.update(kwargs)

    def cget(self, option: str) -> Any:
        """按真实控件的契约读回选项.

        ``state`` 是"禁用必须看起来禁用"那条判据的输入(``paint_button_state``
        要读它), Tk 侧没写过时默认是 ``normal``。
        """
        if option in self.kwargs:
            return self.kwargs[option]
        return "normal" if option == "state" else None

    def pack(self, **_kwargs: Any) -> None:
        # 记录是否在显示中: 批量导出的筛选会 pack/pack_forget 整行.
        self.packed = True
        self.pack_kwargs = dict(_kwargs)

    def pack_forget(self) -> None:
        self.packed = False

    def focus_set(self) -> None:
        return None

    def get(self) -> str:
        return self._value

    def bind(self, _sequence: str, _callback: Any, **_kwargs: Any) -> None:
        # ``add="+"`` 这类可选参数要接受: auto_scrollbar 会这样挂 Configure.
        return None

    def after_idle(self, _callback: Any = None) -> None:
        """假控件不跑事件循环: 按需滚动条的首帧判定直接跳过."""
        return

    def update_idletasks(self) -> None:
        """假控件没有布局可言: 长对话框"按内容定高"那一步直接跳过."""
        return

    def insert(self, _index: str, text: str) -> None:
        # 真实 Tk 的 insert 不会清空已有内容; 空串插入视为无操作,
        # 这样测试可以通过 harness 预置“用户输入”.
        if self._preset or not text:
            return
        self._value = text

    def delete(self, _first: str, _last: str) -> None:
        self._value = ""

    def select_range(self, _start: str | int, _end: str | int) -> None:
        return None

    def click(self) -> None:
        if self.command is not None:
            self.command()

    def destroy(self) -> None:
        """假控件的销毁接口(标签对话框删行时会用到)."""
        self.destroyed = True


class _FakeTextbox(_FakeWidget):
    """假多行文本框: 只保留最后写入的内容."""

    def get(self, _start: str = "1.0", _end: str = "end") -> str:
        return self._value


class _FakeVar:
    """假 tkinter 变量."""

    def __init__(self, *, value: Any = False) -> None:
        self.value = value

    def get(self) -> Any:
        return self.value

    def set(self, value: Any) -> None:
        self.value = value


class _FakeCheckBox(_FakeWidget):
    """假复选框: 记录变量与文本, 可用 select/deselect 模拟勾选."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.variable = kwargs.get("variable")

    def get(self) -> Any:
        """真实 CTkCheckBox 的取值接口: 返回勾选状态."""
        return False if self.variable is None else self.variable.get()

    def select(self) -> None:
        """模拟勾选."""
        if self.variable is not None:
            self.variable.set(True)

    def deselect(self) -> None:
        """模拟取消勾选."""
        if self.variable is not None:
            self.variable.set(False)

    def invoke(self) -> None:
        """模拟用户点击: 先翻转取值, 再触发命令(与真实 CTkCheckBox 一致).

        命令从 ``kwargs`` 取: 真实控件允许建完再 ``configure(command=...)``
        (全选框就是这样接上的), 而假控件只有 ``configure`` 会更新 ``kwargs``。
        """
        if self.variable is not None:
            self.variable.set(not self.variable.get())
        command = self.kwargs.get("command")
        if command is not None:
            command()


class _FakeRadio(_FakeWidget):
    """假单选按钮: 选中时写入变量, ``invoke()`` 才触发命令(与真实控件一致)."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.variable = kwargs.get("variable")
        self.value = kwargs.get("value")

    def select(self) -> None:
        """模拟"这一项被选中"(真实控件 ``set(True)`` 只写变量, 不调命令)."""
        if self.variable is not None:
            self.variable.set(self.value)

    def invoke(self) -> None:
        """模拟用户点击: 先选中, 再触发命令."""
        self.select()
        if self.command is not None:
            self.command()


class _FakeEntry(_FakeWidget):
    """假单行输入框: 记录 ``<KeyRelease>`` 回调, 可用 type() 模拟用户打字."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.key_callback: Callable[[object], None] | None = None

    def bind(self, _sequence: str, _callback: Any, **_kwargs: Any) -> None:
        """记录按键回调(输入即筛选就是它)."""
        self.key_callback = _callback

    def type(self, text: str) -> None:
        """模拟用户输入: 写值并触发按键回调(与真实控件一致; 空串 = 清空)."""
        self._value = text
        if self.key_callback is not None:
            self.key_callback(None)


class _FakeComboBox(_FakeWidget):
    """假下拉框: 记录候选, 并记住 ``<KeyRelease>`` 回调(输入即筛选就是它)."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.values = list(kwargs.get("values") or ())
        self.key_callback: Callable[[object], None] | None = None

    def configure(self, **kwargs: Any) -> None:
        """``values`` 会被读回(筛选与目标候选都靠它)."""
        if "values" in kwargs:
            self.values = list(kwargs["values"])
        super().configure(**kwargs)

    def set(self, value: str) -> None:
        """真实 CTkComboBox.set: 只改当前值, 不触发回调."""
        self._value = value

    def bind(self, _sequence: str, _callback: Any, **_kwargs: Any) -> None:
        """记录按键回调."""
        self.key_callback = _callback

    def type(self, text: str) -> None:
        """模拟用户输入: 写值并触发按键回调(与真实控件一致)."""
        self.set(text)
        if self.key_callback is not None:
            self.key_callback(None)


class _FakeWindow(_FakeWidget):
    """假顶级窗口: 提供对话框所需的几何/几何查询 API."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.window_title = ""
        self.visible = True
        self.alpha = 1.0
        self.geometry_text: str | None = None
        self.destroyed = False
        # 调用流水(居中实现要靠它证明"窗口可见时已经位于最终位置").
        self.calls: list[str] = []
        # 窗口级绑定(常驻窗口不该吃 Esc/回车, 判据靠它).
        self.binds: list[tuple[str, Any, dict[str, Any]]] = []

    def bind(self, _sequence: str, _callback: Any, **_kwargs: Any) -> None:
        """记录窗口级绑定的序列名与回调."""
        self.binds.append((_sequence, _callback, _kwargs))

    def title(self, value: str) -> None:
        self.window_title = value

    def resizable(self, _width: bool, _height: bool) -> None:
        return None

    def transient(self, master: Any) -> None:
        self.transient_master = master

    def grab_set(self) -> None:
        return None

    def geometry(self, value: str) -> None:
        self.geometry_text = value
        self.calls.append(f"geometry({value})")

    def update_idletasks(self) -> None:
        return None

    def update(self) -> None:
        self.calls.append("update()")

    def withdraw(self) -> None:
        """藏起来: 居中过程里"量偏移"必须在看不见的时候做."""
        self.visible = False
        self.calls.append("withdraw()")

    def deiconify(self) -> None:
        """显示出来."""
        self.visible = True
        self.calls.append("deiconify()")

    def attributes(self, name: str, value: float) -> None:
        """记录不透明度(不支持的平台由用例抛 TclError 模拟)."""
        self.alpha = value
        self.calls.append(f"{name}={value}")

    def winfo_ismapped(self) -> bool:
        return self.visible

    def winfo_reqwidth(self) -> int:
        """请求尺寸: 真窗口在映射时会被窗口管理器改掉, 这里给一个不同的值以便区分."""
        return 200

    def winfo_reqheight(self) -> int:
        return 120

    def winfo_rootx(self) -> int:
        return 5

    def winfo_rooty(self) -> int:
        return 8

    def winfo_width(self) -> int:
        return 320

    def winfo_height(self) -> int:
        return 180

    def winfo_screenheight(self) -> int:
        """假窗口给的屏幕高度: 对话框靠它算正文滚动区的高度。"""
        return 1080

    def destroy(self) -> None:
        self.destroyed = True


class _FakeParent(_FakeWidget):
    """假主窗口: 记录 winfo 数值, 在 wait_window 时点击指定按钮."""

    def __init__(
        self,
        buttons: list[_FakeWidget],
        windows: list[_FakeWindow],
        entries: list[_FakeWidget],
        checkboxes: list[_FakeCheckBox],
        labels: list[_FakeWidget],
        radios: list[_FakeRadio],
        combos: list[_FakeComboBox],
    ) -> None:
        super().__init__()
        self.buttons = buttons
        self.windows = windows
        self.entries = entries
        self.checkboxes = checkboxes
        self.labels = labels
        self.radios = radios
        self.combos = combos
        self.click_text: str | None = None
        self.wait_called = False
        self.entry_value = ""
        # 一次对话框含多个输入框时按顺序取值(优先于 entry_value).
        self.entry_values: list[str] = []
        self.textbox_value = ""
        # wait_window 内先执行的钩子: 用于模拟用户先改选项再确认.
        self.on_wait: Callable[[], None] | None = None

    def winfo_rootx(self) -> int:
        return 100

    def winfo_rooty(self) -> int:
        return 50

    def winfo_width(self) -> int:
        return 800

    def winfo_height(self) -> int:
        return 600

    def wait_window(self, _window: Any) -> None:
        self.wait_called = True
        if self.on_wait is not None:
            self.on_wait()
        if self.click_text is None:
            return
        for button in self.buttons:
            if button.text == self.click_text:
                button.click()
                return

    def preset_entry(self, entry: _FakeWidget) -> None:
        """把用例预置的取值填进新建的输入框(按顺序取, 其次用单值)."""
        if self.entry_values:
            entry._value = self.entry_values.pop(0)
        elif self.entry_value:
            entry._value = self.entry_value
        else:
            return
        entry._preset = True

    def preset_textbox(self, box: _FakeTextbox) -> None:
        """把用例预置的取值填进新建的多行文本框."""
        if not self.textbox_value:
            return
        box._value = self.textbox_value
        box._preset = True


def _parent_of(widget: _FakeWidget) -> Any:
    """假控件的上级控件(``master`` 可为空, 链式访问时显式挡一层)."""
    return widget.master


def _install_fakes(monkeypatch: pytest.MonkeyPatch, parent: _FakeParent) -> None:
    """把 dialogs 依赖的 ctk 控件全部换成记录用的假实现.

    工厂只负责"创建 + 登记", 判断留给 `_FakeParent`(夹具因此保持简单);
    补丁打在 `customtkinter` 模块上, 与生产代码里的用法一致。
    """

    def make_window(master: Any = None, **kwargs: Any) -> _FakeWindow:
        window = _FakeWindow(master=master, **kwargs)
        parent.windows.append(window)
        return window

    def make_button(master: Any = None, **kwargs: Any) -> _FakeWidget:
        button = _FakeWidget(master=master, **kwargs)
        parent.buttons.append(button)
        return button

    def make_entry(master: Any = None, **kwargs: Any) -> _FakeEntry:
        entry = _FakeEntry(master=master, **kwargs)
        parent.preset_entry(entry)
        parent.entries.append(entry)
        return entry

    def make_textbox(master: Any = None, **kwargs: Any) -> _FakeTextbox:
        box = _FakeTextbox(master=master, **kwargs)
        parent.preset_textbox(box)
        return box

    def make_label(master: Any = None, **kwargs: Any) -> _FakeWidget:
        label = _FakeWidget(master=master, **kwargs)
        parent.labels.append(label)
        return label

    def make_checkbox(master: Any = None, **kwargs: Any) -> _FakeCheckBox:
        box = _FakeCheckBox(master=master, **kwargs)
        parent.checkboxes.append(box)
        return box

    def make_radio(master: Any = None, **kwargs: Any) -> _FakeRadio:
        radio = _FakeRadio(master=master, **kwargs)
        parent.radios.append(radio)
        return radio

    def make_combo(master: Any = None, **kwargs: Any) -> _FakeComboBox:
        combo = _FakeComboBox(master=master, **kwargs)
        parent.combos.append(combo)
        return combo

    def make_plain(master: Any = None, **kwargs: Any) -> _FakeWidget:
        return _FakeWidget(master=master, **kwargs)

    monkeypatch.setattr(ctk, "CTkToplevel", make_window)
    monkeypatch.setattr(ctk, "CTkLabel", make_label)
    monkeypatch.setattr(ctk, "CTkFrame", make_plain)
    monkeypatch.setattr(ctk, "CTkScrollableFrame", make_plain)
    monkeypatch.setattr(ctk, "CTkButton", make_button)
    monkeypatch.setattr(ctk, "CTkEntry", make_entry)
    monkeypatch.setattr(ctk, "CTkTextbox", make_textbox)
    monkeypatch.setattr(ctk, "CTkCheckBox", make_checkbox)
    monkeypatch.setattr(ctk, "CTkRadioButton", make_radio)
    monkeypatch.setattr(ctk, "CTkComboBox", make_combo)
    monkeypatch.setattr(ctk, "BooleanVar", lambda **kwargs: _FakeVar(**kwargs))
    monkeypatch.setattr(ctk, "StringVar", lambda **kwargs: _FakeVar(**kwargs))
    monkeypatch.setattr(ctk, "CTkFont", lambda **_kwargs: object())


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _FakeParent:
    """把 dialogs 依赖的 ctk 控件替换为假实现, 返回假主窗口."""
    parent = _FakeParent([], [], [], [], [], [], [])
    _install_fakes(monkeypatch, parent)
    return parent


def test_present_gives_window_keys_only_to_modal_windows(
    harness: _FakeParent,
) -> None:
    """窗口级 Esc/回车/定焦只属于抓取式对话框(12 号实测反馈).

    常驻窗口(设置、定时任务)调到 ``modal=False``: 不许绑 ``<Escape>``/``<Return>``
    —— 实测设置窗口按 Esc 会直接关窗、按回车会按到"切换主题"那颗主色按钮。
    对话框那条路必须保持原样。
    """
    resident = _FakeWindow()
    dialogs._present(harness, resident, modal=False)
    assert [sequence for sequence, *_ in resident.binds] == []
    assert resident.calls[0] == "withdraw()", "不接键盘也不能影响居中"

    modal = _FakeWindow()
    dialogs._present(harness, modal, modal=True)
    sequences = [sequence for sequence, *_ in modal.binds]
    assert "<Escape>" in sequences
    assert "<Return>" in sequences


def test_dialog_button_rows_are_centered(harness: _FakeParent) -> None:
    """对话框底部的按钮行要**居中**(2026-10-02 用户反馈: 贴着左边看着像漏了排版).

    判据落在行的 ``pack`` 参数上: 行里每个按钮都是 ``side="left"``, 所以只要这行自己不写
    ``fill``/``anchor``, Tk 就把它按内容宽度摆在窗口中间; 写了任何一个都会贴到某一边。
    这里钉住两个代表性的对话框(各行最后创建的那个按钮的上级就是这行), 其余对话框由下面
    那条源码扫描覆盖 —— 逐个打开太贵, 而"新加的对话框又贴左"正是最容易复发的回归。
    """
    dialogs.confirm_dialog(harness, DARK, title="删除", message="确定删除?")
    confirm_row = cast("_FakeWidget", harness.buttons[-1].master)
    assert "fill" not in confirm_row.pack_kwargs, "确认框的按钮行贴到边上了"
    assert "anchor" not in confirm_row.pack_kwargs, "确认框的按钮行贴到边上了"

    harness.buttons.clear()
    dialogs.ask_text(harness, DARK, title="重命名", text=_PROMPT)
    text_row = cast("_FakeWidget", harness.buttons[-1].master)
    assert "fill" not in text_row.pack_kwargs, "输入框的按钮行贴到边上了"
    assert "anchor" not in text_row.pack_kwargs, "输入框的按钮行贴到边上了"


def test_every_dialog_button_row_in_the_source_is_centered() -> None:
    """整仓扫描: ``dialogs.py`` 里每一处 ``buttons.pack(...)`` 都不许带 ``fill``/``anchor``.

    逐条点名而不是只报个数: 以后新增对话框时, 报错信息要能直接指到那一行。
    """
    source = Path(dialogs.__file__).read_text(encoding="utf-8")
    offenders = [
        f"dialogs.py:{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if "buttons.pack(" in line and ("fill=" in line or "anchor=" in line)
    ]

    assert offenders == [], "对话框按钮行要居中(去掉 fill/anchor): " + "; ".join(
        offenders
    )


def test_confirm_ok_returns_true(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.confirm")
    result = dialogs.confirm_dialog(harness, DARK, title="删除", message="确定删除?")
    assert result is True
    assert len(harness.windows) == 1
    window = harness.windows[0]
    assert window.window_title == "删除"
    assert window.destroyed is True
    assert harness.wait_called is True


def test_confirm_geometry_is_centered_on_parent(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.confirm")
    dialogs.confirm_dialog(harness, DARK, title="t", message="m")
    expected_x = 100 + (800 - 320) // 2 - 5
    expected_y = 50 + (600 - 180) // 2 - 8
    assert harness.windows[0].geometry_text == f"+{expected_x}+{expected_y}"


def test_centering_never_shows_the_dialog_at_the_screen_origin(
    harness: _FakeParent,
) -> None:
    """居中不能让窗口先在屏幕左上角闪一下(用户实测).

    判据是"可见性 / 不透明度 / 位置"三者的顺序: 量装潢偏移与最终定位都必须在
    **看不见**的状态里完成 —— 窗口第一次可见时就已经在最终位置上。
    """
    harness.click_text = tr("dialog.confirm")
    dialogs.confirm_dialog(harness, DARK, title="t", message="m")
    window = harness.windows[0]
    calls = window.calls

    assert calls[0] == "withdraw()", "先藏起来再量"
    assert calls[-1] == "-alpha=1.0", "最后才恢复不透明"
    # 量装潢偏移用的 (0,0) 必须发生在显示之前 —— 这正是原来的"左上角闪一下"。
    assert calls.index("geometry(+0+0)") < calls.index("deiconify()")
    # 真正的定位两次(按请求尺寸估一次 + 按真实尺寸精确一次)都在看不见的时候。
    positioned = [
        index
        for index, call in enumerate(calls)
        if call.startswith("geometry(+") and call != "geometry(+0+0)"
    ]
    assert len(positioned) == 2, f"应当只在不可见时定位: {calls}"
    assert calls.index("withdraw()") < positioned[0] < calls.index("-alpha=0.0")
    assert calls.index("-alpha=0.0") < calls.index("deiconify()") < positioned[-1]
    assert "update()" not in calls, "不能再用一次全量 update 把窗口先画出来"


def test_centering_survives_a_platform_without_window_transparency(
    harness: _FakeParent, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台不支持 ``-alpha``(无合成器的 Linux)时退化成"估个位置再微调", 不能报错."""
    harness.click_text = tr("dialog.confirm")

    def no_alpha(_self: Any, _name: str, _value: float) -> None:
        raise tk.TclError("wm attributes -alpha not supported")

    monkeypatch.setattr(_FakeWindow, "attributes", no_alpha)

    dialogs.confirm_dialog(harness, DARK, title="t", message="m")

    window = harness.windows[0]
    # 仍然落在居中位置(按真实尺寸算的那一次), 不因为不支持透明度而抛错。
    centered = f"+{100 + (800 - 320) // 2 - 5}+{50 + (600 - 180) // 2 - 8}"
    assert window.geometry_text == centered
    assert window.visible is True


def test_confirm_cancel_returns_false(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.cancel")
    assert dialogs.confirm_dialog(harness, DARK, title="t", message="m") is False


def test_confirm_honours_custom_button_texts(harness: _FakeParent) -> None:
    harness.click_text = "覆盖"
    result = dialogs.confirm_dialog(
        harness,
        DARK,
        title="t",
        message="m",
        confirm_text="覆盖",
        cancel_text="放弃",
    )
    assert result is True
    assert {"覆盖", "放弃"} <= {button.text for button in harness.buttons}


def test_info_dialog_is_non_modal(harness: _FakeParent) -> None:
    dialogs.info_dialog(harness, DARK, title="提示", message="已完成")
    assert harness.wait_called is False
    assert len(harness.windows) == 1
    assert {button.text for button in harness.buttons} == {tr("dialog.ok")}


def test_import_game_dialog_returns_name_and_checked_paths(
    harness: _FakeParent,
) -> None:
    """导入对话框: 预填名称与推断出的存档路径, 确认后原样返回(可改可勾选)."""
    harness.click_text = tr("dialog.import_confirm")
    result = dialogs.import_game_dialog(
        harness,
        DARK,
        title="导入为游戏",
        name_label="确认游戏名称:",
        initial_name="星露谷",
        paths_label="存档路径:",
        paths_hint="提示",
        initial_paths=("C:/one", "C:/two"),
        add_text=tr("dialog.import_add_path"),
        confirm_text=tr("dialog.import_confirm"),
    )

    assert result == ("星露谷", ("C:/one", "C:/two"))
    assert {button.text for button in harness.buttons} >= {
        tr("dialog.import_add_path"),
        tr("dialog.cancel"),
        tr("dialog.import_confirm"),
    }


def test_import_game_dialog_without_a_name_is_cancelled(
    harness: _FakeParent,
) -> None:
    """名称留空时不提交, 与其它输入对话框一致(返回 None)."""
    harness.click_text = tr("dialog.import_confirm")
    result = dialogs.import_game_dialog(
        harness,
        DARK,
        title="导入为游戏",
        name_label="名称:",
        initial_name="   ",
        paths_label="路径:",
        paths_hint="提示",
        confirm_text=tr("dialog.import_confirm"),
    )

    assert result is None


def test_import_game_dialog_browse_fills_each_path_row(harness: _FakeParent) -> None:
    """每条路径都有"浏览…"按钮: 挑到的目录直接填进那一行, 并随确认一起返回."""
    picked = ["C:/picked-one", "C:/picked-two"]
    calls: list[str] = []

    def browse() -> str:
        value = picked[len(calls)]
        calls.append(value)
        return value

    def browse_all_then_confirm() -> None:
        for button in harness.buttons:
            if button.text == tr("dialog.browse"):
                button.click()
        confirm = next(
            button
            for button in harness.buttons
            if button.text == tr("dialog.import_confirm")
        )
        confirm.click()

    harness.on_wait = browse_all_then_confirm
    result = dialogs.import_game_dialog(
        harness,
        DARK,
        title="导入为游戏",
        name_label="确认游戏名称:",
        initial_name="星露谷",
        paths_label="存档路径:",
        paths_hint="提示",
        initial_paths=("C:/old-one", "C:/old-two"),
        add_text=tr("dialog.import_add_path"),
        confirm_text=tr("dialog.import_confirm"),
        browse=browse,
    )

    assert calls == picked
    assert result == ("星露谷", ("C:/picked-one", "C:/picked-two"))


def test_import_game_dialog_without_browse_has_no_browse_button(
    harness: _FakeParent,
) -> None:
    """没传 browse 时不出现"浏览…"按钮(无头环境或未接线时不显示死按钮)."""
    harness.click_text = tr("dialog.import_confirm")
    dialogs.import_game_dialog(
        harness,
        DARK,
        title="导入为游戏",
        name_label="名称:",
        initial_name="星露谷",
        paths_label="路径:",
        paths_hint="提示",
        initial_paths=("C:/one",),
        confirm_text=tr("dialog.import_confirm"),
    )

    assert tr("dialog.browse") not in {button.text for button in harness.buttons}


def test_edit_tags_dialog_collects_rows(harness: _FakeParent) -> None:
    """标签对话框按行收集: 空白被清掉、重复项合并、空行丢弃."""

    def fill_and_add() -> None:
        add = next(b for b in harness.buttons if b.text == tr("dialog.tags_add"))
        harness.entries[0]._value = "  探索  "
        add.click()
        harness.entries[1]._value = "探索"
        add.click()
        harness.entries[2]._value = ""
        next(b for b in harness.buttons if b.text == tr("dialog.tags_save")).click()

    harness.on_wait = fill_and_add
    result = dialogs.edit_tags_dialog(harness, DARK, tags=())

    assert result == ("探索",)


def test_edit_tags_dialog_drops_both_comma_forms(harness: _FakeParent) -> None:
    """中英文逗号一视同仁: 保存时两种逗号都被剔除, 不会被当成两个标签."""

    def fill_and_add() -> None:
        add = next(b for b in harness.buttons if b.text == tr("dialog.tags_add"))
        harness.entries[0]._value = "探索,解谜"
        add.click()
        harness.entries[1]._value = "动作，冒险"  # noqa: RUF001 - 全角逗号正是被测输入
        next(b for b in harness.buttons if b.text == tr("dialog.tags_save")).click()

    harness.on_wait = fill_and_add
    result = dialogs.edit_tags_dialog(harness, DARK, tags=())

    assert result == ("探索解谜", "动作冒险")


def test_restore_dialog_hangs_wrapped_bullet_lines(harness: _FakeParent) -> None:
    """23 号评审: 摘要里的"· "行要带悬挂缩进, 长路径折行后不能顶到最左边."""
    harness.click_text = tr("dialog.restore_confirm")

    dialogs.restore_dialog(
        harness,
        DARK,
        title="恢复到此节点",
        summary=(
            "将把「Demo」恢复到备份「手动」:\n"
            "· 快照 2 个文件 · 1 B\n"
            "· 写回位置:\n"
            "· D:\\Saves\\Demo"
        ),
        safety_label=tr("dialog.restore_safety"),
        safety_hint=tr("dialog.restore_safety_hint"),
        safety_available=True,
    )

    texts = [label.text for label in harness.labels]
    # 符号与正文拆成两个控件, 折行后的正文才与首行文字左对齐.
    assert "·" in texts
    assert "D:\\Saves\\Demo" in texts
    path_label = next(
        label for label in harness.labels if label.text == "D:\\Saves\\Demo"
    )
    assert int(path_label.kwargs["wraplength"]) == 380


def test_restore_dialog_option_does_not_wear_the_primary_green(
    harness: _FakeParent,
) -> None:
    """23 号评审: 勾选态是"选项", 不该与"开始恢复"同一颗实心绿."""
    harness.click_text = tr("dialog.restore_confirm")

    dialogs.restore_dialog(
        harness,
        DARK,
        title="恢复到此节点",
        summary="摘要",
        safety_label=tr("dialog.restore_safety"),
        safety_hint=tr("dialog.restore_safety_hint"),
        safety_available=True,
    )

    box = harness.checkboxes[0]
    assert box.kwargs["fg_color"] == DARK.accent_soft_border
    assert box.kwargs["fg_color"] != DARK.accent


def test_confirm_dialog_gives_the_path_a_row_and_the_button_a_danger_color(
    harness: _FakeParent,
) -> None:
    """24 号评审: 导出路径单独一行带底色, 破坏性确认按钮穿危险色."""
    harness.click_text = "删除记录"

    dialogs.confirm_dialog(
        harness,
        DARK,
        title="删除游戏",
        message="删除「Demo」前, 会先把它的配置与全部备份导出到下面这个文件。",
        detail="D:\\Exports\\Demo-20260926.archive.zip",
        confirm_text="删除记录",
        danger=True,
    )

    path_label = next(
        label
        for label in harness.labels
        if label.text == "D:\\Exports\\Demo-20260926.archive.zip"
    )
    assert path_label.kwargs["fg_color"] == DARK.input_bg
    button = next(b for b in harness.buttons if b.text == "删除记录")
    assert button.kwargs["fg_color"] == DARK.danger


def test_schedule_dialog_shows_the_state_on_its_own_line_with_equal_fields(
    harness: _FakeParent,
) -> None:
    """25 号评审: 当前排期单独一行(不塞在说明句尾), 两个输入框同宽."""
    harness.click_text = tr("dialog.schedule_save")

    dialogs.schedule_dialog(
        harness,
        DARK,
        title="定时备份 · 测试游戏",
        interval_label=tr("dialog.schedule_interval_label"),
        interval_prompt=tr("dialog.schedule_prompt"),
        keep_label=tr("dialog.keep_auto_label"),
        keep_prompt=tr("dialog.keep_auto_prompt", max=60),
        initial_interval="",
        current=tr("dialog.schedule_current", current=tr("task.unscheduled")),
    )

    texts = [label.kwargs.get("text") for label in harness.labels]
    assert tr("dialog.schedule_current", current=tr("task.unscheduled")) in texts
    widths = {entry.kwargs.get("width") for entry in harness.entries}
    assert widths == {dialogs._SCHEDULE_FIELD_WIDTH}


def test_ask_text_hints_enter_and_left_aligns_the_blocks(
    harness: _FakeParent,
) -> None:
    """19 号评审: 回车能提交要写在界面上, 提示/输入框/按钮统一左对齐同宽."""
    harness.click_text = tr("dialog.cancel")

    dialogs.ask_text(harness, DARK, title="新增游戏", text=_PROMPT)

    assert [
        label.text for label in harness.labels if label.text == tr("dialog.enter_hint")
    ] == [tr("dialog.enter_hint")]
    prompt = next(label for label in harness.labels if label.text == _PROMPT)
    assert prompt.kwargs["anchor"] == "w"
    assert int(prompt.kwargs["wraplength"]) == dialogs._TEXT_WIDTH


def test_ask_text_context_says_what_is_being_edited(harness: _FakeParent) -> None:
    """20 号评审: 重命名与"新增"长得一样, 要写清在改谁."""
    harness.click_text = tr("dialog.cancel")

    dialogs.ask_text(
        harness,
        DARK,
        title="重命名",
        text=_PROMPT,
        initial="测试游戏",
        context=tr("dialog.rename_context", name="测试游戏"),
    )

    texts = [label.text for label in harness.labels]
    assert tr("dialog.rename_context", name="测试游戏") in texts


def test_tags_row_delete_is_disabled_while_its_row_is_empty(
    harness: _FakeParent,
) -> None:
    """21 号评审: 空行的"删除"点了等于删一个不存在的标签, 应该压暗."""
    harness.click_text = tr("dialog.tags_save")

    dialogs.edit_tags_dialog(harness, DARK, tags=("探索",))

    removes = [
        button for button in harness.buttons if button.text == tr("dialog.tags_remove")
    ]
    # 一条已有标签 + 一条尾随的空行.
    assert len(removes) == 2
    assert removes[0].kwargs["fg_color"] == DARK.raised
    assert removes[1].kwargs["fg_color"] == DARK.disabled_bg


def test_backup_dialog_does_not_count_its_placeholder(harness: _FakeParent) -> None:
    """22 号评审: 描述框的占位文案不算用户输入, 字数从 0 开始."""
    harness.click_text = tr("dialog.cancel")

    dialogs.edit_backup_dialog(
        harness,
        DARK,
        title=tr("dialog.rename_title"),
        name_label=tr("dialog.rename_label"),
        desc_label=tr("dialog.describe_label"),
        limit=200,
    )

    texts = [label.kwargs.get("text") for label in harness.labels]
    assert tr("dialog.counter", count=0, limit=200) in texts


def test_edit_tags_dialog_starts_from_existing_tags(harness: _FakeParent) -> None:

    def drop_the_first_row() -> None:
        next(b for b in harness.buttons if b.text == tr("dialog.tags_remove")).click()
        next(b for b in harness.buttons if b.text == tr("dialog.tags_save")).click()

    harness.on_wait = drop_the_first_row
    result = dialogs.edit_tags_dialog(harness, DARK, tags=("探索", "解谜"))

    assert result == ("解谜",)


def test_edit_tags_dialog_stops_adding_at_the_limit(harness: _FakeParent) -> None:
    """行数到达上限后"添加标签"不可用, 再点也长不出新行."""

    def add_many() -> None:
        add = next(b for b in harness.buttons if b.text == tr("dialog.tags_add"))
        for _index in range(4):
            add.click()
        next(b for b in harness.buttons if b.text == tr("dialog.tags_save")).click()

    harness.on_wait = add_many
    dialogs.edit_tags_dialog(harness, DARK, tags=("a", "b"), max_tags=3)

    add = next(b for b in harness.buttons if b.text == tr("dialog.tags_add"))
    assert add.kwargs["state"] == "disabled"
    # 已有 2 行 + 自动补的 1 行 = 3 行(上限)。
    assert len(harness.entries) == 3


def test_edit_tags_dialog_cancel_returns_none(harness: _FakeParent) -> None:
    """取消时不返回任何东西, 调用方据此不改动标签."""
    harness.click_text = tr("dialog.cancel")

    assert dialogs.edit_tags_dialog(harness, DARK, tags=("探索",)) is None


def test_ask_branch_name_returns_stripped(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.confirm")
    harness.entry_value = "  新分支A  "
    result = dialogs.ask_branch_name(harness, DARK, title="分支", text="输入名称")
    assert result == "新分支A"


def test_ask_branch_name_empty_returns_none(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.confirm")
    harness.entry_value = "   "
    assert dialogs.ask_branch_name(harness, DARK, title="分支", text="名称") is None


def test_ask_branch_name_cancel_returns_none(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.cancel")
    harness.entry_value = "分支X"
    assert dialogs.ask_branch_name(harness, DARK, title="分支", text="名称") is None


def test_ask_text_returns_input_with_browse(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.confirm")
    harness.entry_value = "  我的游戏  "
    result = dialogs.ask_text(
        harness,
        DARK,
        title=tr("dialog.add_game_title"),
        text="请输入名称",
        browse=lambda: None,
    )
    assert result == "我的游戏"
    assert tr("dialog.browse") in {button.text for button in harness.buttons}


def test_ask_text_empty_returns_none(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.confirm")
    harness.entry_value = "   "
    assert dialogs.ask_text(harness, DARK, title="t", text="请输入名称") is None


def test_ask_text_cancel_returns_none(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.cancel")
    harness.entry_value = "任意"
    assert dialogs.ask_text(harness, DARK, title="t", text="请输入名称") is None


# ------------------------------------------- 单窗口编辑(名称+描述 / 周期+保留份数)


class _EditArgs(TypedDict):
    """edit_backup_dialog 的公共参数(limit 单独传, 便于用例覆盖)."""

    title: str
    name_label: str
    desc_label: str


def _edit_args() -> _EditArgs:
    return {
        "title": tr("dialog.rename_title"),
        "name_label": tr("dialog.rename_label"),
        "desc_label": tr("dialog.describe_label"),
    }


def test_edit_backup_dialog_returns_name_and_description(
    harness: _FakeParent,
) -> None:
    """名称与描述在同一个窗口内提交, 只需一次交互."""
    harness.click_text = tr("dialog.schedule_save")
    harness.entry_value = "  离开量子月亮前  "
    harness.textbox_value = "  第一次通关前的存档  "

    result = dialogs.edit_backup_dialog(harness, DARK, **_edit_args(), limit=200)

    assert result == ("离开量子月亮前", "第一次通关前的存档")
    assert len(harness.windows) == 1
    assert harness.windows[0].destroyed is True


def test_edit_backup_dialog_prefills_existing_values(
    harness: _FakeParent,
) -> None:
    """已有名称与描述会回填到同一个窗口."""
    harness.click_text = tr("dialog.schedule_save")
    result = dialogs.edit_backup_dialog(
        harness,
        DARK,
        **_edit_args(),
        initial_name="初始存档",
        initial_desc="第一次启动时的存档",
    )
    assert result == ("初始存档", "第一次启动时的存档")


def test_edit_backup_dialog_cancel_returns_none(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.cancel")
    harness.entry_value = "新名字"
    assert dialogs.edit_backup_dialog(harness, DARK, **_edit_args()) is None


def test_edit_backup_dialog_rejects_over_limit_description(
    harness: _FakeParent,
) -> None:
    """描述超限时不提交且窗口保持打开(不做静默截断)."""
    harness.click_text = tr("dialog.schedule_save")
    harness.textbox_value = "x" * 201

    assert dialogs.edit_backup_dialog(harness, DARK, **_edit_args(), limit=200) is None
    assert harness.windows[0].destroyed is False


class _ScheduleArgs(TypedDict):
    """schedule_dialog 的公共参数."""

    title: str
    interval_label: str
    interval_prompt: str
    keep_label: str
    keep_prompt: str


def _schedule_args() -> _ScheduleArgs:
    return {
        "title": tr("dialog.schedule_title"),
        "interval_label": tr("dialog.schedule_interval_label"),
        "interval_prompt": tr("dialog.schedule_prompt", current="未配置"),
        "keep_label": tr("dialog.keep_auto_label"),
        "keep_prompt": tr("dialog.keep_auto_prompt", max=20),
    }


def test_schedule_dialog_returns_interval_and_keep(harness: _FakeParent) -> None:
    """周期与保留份数在同一个窗口内提交."""
    harness.click_text = tr("dialog.schedule_save")
    harness.entry_values = ["  2h  ", " 5 "]

    result = dialogs.schedule_dialog(harness, DARK, **_schedule_args())

    assert result == ("2h", "5")
    assert harness.windows[0].destroyed is True


def test_schedule_dialog_allows_empty_interval(harness: _FakeParent) -> None:
    """留空周期表示取消定时备份, 由调用方解析."""
    harness.click_text = tr("dialog.schedule_save")
    harness.entry_values = ["", "3"]

    assert dialogs.schedule_dialog(harness, DARK, **_schedule_args()) == ("", "3")


def test_schedule_dialog_cancel_returns_none(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.cancel")
    assert dialogs.schedule_dialog(harness, DARK, **_schedule_args()) is None


class _RestoreArgs(TypedDict):
    """restore_dialog 的公共参数."""

    title: str
    summary: str
    safety_label: str
    safety_hint: str
    safety_available: bool


def _restore_args(*, safety_available: bool = True) -> _RestoreArgs:
    """构造恢复对话框参数(按用例覆盖候选开关)."""
    return {
        "title": tr("dialog.restore_title"),
        "summary": tr(
            "dialog.restore_summary",
            name="星际拓荒",
            title="离开量子月亮前",
            files=3,
            size="1 MB",
            targets="· D:/save",
        ),
        "safety_label": tr("dialog.restore_safety"),
        "safety_hint": tr("dialog.restore_safety_hint"),
        "safety_available": safety_available,
    }


def test_restore_dialog_defaults_to_safety_point(harness: _FakeParent) -> None:
    """默认勾选安全点: 恢复因此可逆(安全点只在时间线视图显示)."""
    harness.click_text = tr("dialog.restore_confirm")

    result = dialogs.restore_dialog(harness, DARK, **_restore_args())

    assert result is True
    assert len(harness.checkboxes) == 1
    assert harness.windows[0].destroyed is True


def test_restore_dialog_returns_false_when_safety_point_cleared(
    harness: _FakeParent,
) -> None:
    """取消勾选安全点后返回 False(仍然继续恢复)."""
    harness.click_text = tr("dialog.restore_confirm")
    harness.on_wait = lambda: harness.checkboxes[0].deselect()

    assert dialogs.restore_dialog(harness, DARK, **_restore_args()) is False


def test_restore_dialog_forces_unavailable_option_off(harness: _FakeParent) -> None:
    """存档没有可备份内容时, 安全点选项被禁用且不会生效."""
    harness.click_text = tr("dialog.restore_confirm")
    harness.on_wait = lambda: harness.checkboxes[0].select()

    result = dialogs.restore_dialog(
        harness, DARK, **_restore_args(safety_available=False)
    )

    assert result is False
    assert [box.kwargs["state"] for box in harness.checkboxes] == ["disabled"]


def test_restore_dialog_cancel_returns_none(harness: _FakeParent) -> None:
    harness.click_text = tr("dialog.cancel")
    assert dialogs.restore_dialog(harness, DARK, **_restore_args()) is None


def test_restore_dialog_shows_danger_note_in_same_window(
    harness: _FakeParent,
) -> None:
    """风险提示与选项在同一个窗口内展示, 不额外弹窗."""
    harness.click_text = tr("dialog.restore_confirm")
    note = tr("dialog.restore_process", matches="OuterWilds.exe")

    dialogs.restore_dialog(harness, DARK, **_restore_args(), danger_note=note)

    assert len(harness.windows) == 1
    texts = [label.kwargs.get("text") for label in harness.labels]
    assert note in texts


# ---------------------------------------------------------------- 导入归档包


def _import_args(
    *,
    locations: tuple[models.ImportLocationRow, ...] = (),
    targets: tuple[models.ImportTargetOption, ...] = (),
    match: str = "库里疑似同一款游戏: 星际拓荒(已默认选中)",
) -> dict[str, Any]:
    """导入对话框的参数(策略选项按"有没有目标游戏"给, 与界面同一套规则)."""
    return {
        "title": tr("dialog.import_title"),
        "prompt": models.ImportPrompt(
            summary="包摘要",
            match_text=match,
            locations=locations,
            targets=targets,
        ),
        "locations_label": tr("dialog.import_locations"),
        "locations_hint": tr("dialog.import_locations_hint"),
        "strategy_label": tr("dialog.import_strategy"),
        "strategies": models.import_strategies(has_targets=bool(targets)),
        "target_label": tr("dialog.import_target"),
        "target_hint": tr("dialog.import_target_hint"),
        "target_locked_hint": tr("dialog.import_target_locked"),
        "confirm_text": tr("dialog.import_confirm"),
    }


def _rows() -> tuple[models.ImportLocationRow, ...]:
    """两条包内存档位置: 一条本机已存在(预填), 一条不存在(留空)."""
    return (
        models.ImportLocationRow(
            index=0, text="D:/saves(本机已存在)", default="D:/saves"
        ),
        models.ImportLocationRow(index=1, text="E:/gone", default=""),
    )


def _targets() -> tuple[models.ImportTargetOption, ...]:
    return (
        models.ImportTargetOption(game_id="7", label="星际拓荒", selected=True),
        models.ImportTargetOption(game_id="9", label="山海旅人"),
    )


def _strategy_radios(harness: _FakeParent) -> list[_FakeRadio]:
    """导入方式那组单选按钮(目标游戏那组的 value 是游戏 id, 不在此列)."""
    keys = {STRATEGY_NEW, STRATEGY_MERGE, STRATEGY_SKIP}
    return [radio for radio in harness.radios if radio.value in keys]


def _target_radios(harness: _FakeParent) -> list[_FakeRadio]:
    """目标游戏那组单选按钮."""
    return [radio for radio in harness.radios if radio.value in {"7", "9"}]


def test_import_package_dialog_maps_only_filled_rows(harness: _FakeParent) -> None:
    """确认后返回策略 + 目标 + "填了路径的那几条"映射(留空的那条不导入)."""
    harness.click_text = tr("dialog.import_confirm")
    harness.entry_values = ["", "E:/local-saves"]

    result = dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), targets=_targets())
    )

    assert result == models.ImportChoice(
        strategy=STRATEGY_NEW,
        target_game_id=None,
        locations={1: "E:/local-saves"},
    )
    assert len(harness.windows) == 1
    assert harness.windows[0].window_title == tr("dialog.import_title")


def test_import_package_dialog_defaults_to_the_packaged_path(
    harness: _FakeParent,
) -> None:
    """包内路径本机已存在时才预填, 否则留空(用户填了才算映射)."""
    harness.click_text = tr("dialog.import_confirm")

    result = dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows())
    )

    assert [entry.get() for entry in harness.entries] == ["D:/saves", ""]
    assert result is not None
    assert result.locations == {0: "D:/saves"}


def test_import_package_dialog_merges_into_the_selected_game(
    harness: _FakeParent,
) -> None:
    """选"合并"时才返回目标游戏, 并顺手解锁目标游戏的单选区."""
    harness.click_text = tr("dialog.import_confirm")
    harness.on_wait = lambda: next(
        radio for radio in _strategy_radios(harness) if radio.value == STRATEGY_MERGE
    ).invoke()

    result = dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), targets=_targets())
    )

    assert result is not None
    assert result.strategy == STRATEGY_MERGE
    assert result.target_game_id == "7", "默认应选中匹配到的那一款"
    assert [radio.kwargs.get("state") for radio in _target_radios(harness)] == [
        "normal",
        "normal",
    ]
    assert [radio.kwargs.get("state") for radio in _strategy_radios(harness)] == [
        None,
        None,
        None,
    ], "只置灰目标区, 导入方式那一组保持可用"


def test_import_package_dialog_disables_the_target_picker_before_merging(
    harness: _FakeParent,
) -> None:
    """默认的"新建游戏"用不到目标游戏: 目标区一开始就是置灰的."""
    harness.click_text = tr("dialog.import_confirm")

    dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), targets=_targets())
    )

    assert [radio.kwargs.get("state") for radio in _target_radios(harness)] == [
        "disabled",
        "disabled",
    ]


def test_import_package_dialog_defaults_to_the_first_target(
    harness: _FakeParent,
) -> None:
    """调用方没标预选时默认选第一款, 而不是留空(留空会让合并没有目标)."""
    harness.click_text = tr("dialog.import_confirm")
    harness.on_wait = lambda: next(
        radio for radio in _strategy_radios(harness) if radio.value == STRATEGY_MERGE
    ).invoke()
    targets = (
        models.ImportTargetOption(game_id="7", label="星际拓荒"),
        models.ImportTargetOption(game_id="9", label="山海旅人"),
    )

    result = dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), targets=targets)
    )

    assert result is not None
    assert result.target_game_id == "7"


def test_import_package_dialog_without_candidates_offers_no_merge(
    harness: _FakeParent,
) -> None:
    """库里没有可合并的游戏时不给"合并"选项, 也不返回目标游戏."""
    harness.click_text = tr("dialog.import_confirm")

    result = dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), match="")
    )

    assert [radio.value for radio in harness.radios] == [STRATEGY_NEW, STRATEGY_SKIP]
    assert result is not None
    assert result.strategy == STRATEGY_NEW
    assert result.target_game_id is None


def test_import_package_dialog_skip_returns_the_skip_strategy(
    harness: _FakeParent,
) -> None:
    """选"跳过"时返回跳过策略且不带任何映射(用户仍然看得到包内容)."""
    harness.click_text = tr("dialog.import_confirm")
    harness.on_wait = lambda: next(
        radio for radio in _strategy_radios(harness) if radio.value == STRATEGY_SKIP
    ).invoke()

    result = dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows())
    )

    assert result == models.ImportChoice(
        strategy=STRATEGY_SKIP, target_game_id=None, locations={0: "D:/saves"}
    )


def test_import_package_dialog_cancel_returns_none(harness: _FakeParent) -> None:
    """取消: 返回 None, 并且不会把当前选择写出去."""
    harness.click_text = tr("dialog.cancel")

    result = dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), targets=_targets())
    )

    assert result is None
    assert harness.windows[0].destroyed is True


def test_import_package_dialog_shows_the_summary_and_the_match(
    harness: _FakeParent,
) -> None:
    """包摘要与"疑似同一款"的提示都在同一个窗口里给出."""
    harness.click_text = tr("dialog.cancel")
    args = _import_args(locations=_rows(), targets=_targets())

    dialogs.import_package_dialog(harness, DARK, **args)

    texts = [label.kwargs.get("text") for label in harness.labels]
    assert args["prompt"].summary in texts
    assert args["prompt"].match_text in texts
    # 每行存档位置都要把包内路径摆出来(用户得知道这一条对应哪个目录).
    assert "D:/saves(本机已存在)" in texts
    assert "E:/gone" in texts


def test_dialog_body_height_is_clamped_to_the_screen() -> None:
    """28 号评审: 长对话框的正文高度按屏幕封顶, 并夹在上下限之间.

    口径是**整个窗口**不超过屏幕可用高度的 80%(optimizations.csv 35 号), 所以正文区的
    预算要先扣掉"正文之外"那段固定高度(`_DIALOG_CHROME_HEIGHT`, 实测约 50px)。只夹
    正文区的旧口径在 768 高的屏上会把窗口顶到 664(正文 614 + 正文之外 50), 底部的
    「导入」照样差 16px 出屏幕 —— 本用例当初只钉了正文区那一半, 漏的正是这 16px。
    """

    class _Screen:
        """只要能报出屏幕高度的最小替身."""

        def __init__(self, height: int) -> None:
            self._height = height

        def winfo_screenheight(self) -> int:
            return self._height

    # 屏幕够高时用上限: 不跟着屏幕一起涨.
    assert dialogs._dialog_body_height(_Screen(1080)) == dialogs._DIALOG_BODY_MAX
    # 屏幕矮时按 80% 扣掉正文之外那段.
    budget = dialogs._dialog_body_height(_Screen(800))
    assert budget == int(800 * 0.8) - dialogs._DIALOG_CHROME_HEIGHT, (
        "矮屏取 80% 再扣正文之外"
    )
    # 真正要守住的是这条: 窗口总高(正文 + 正文之外)落在 80% 以内.
    assert budget + dialogs._DIALOG_CHROME_HEIGHT <= int(800 * 0.8), (
        "总高必须落在屏幕 80% 内"
    )
    # 屏幕再矮也不压到下限以下(此时靠正文区自己滚保证内容可达).
    assert dialogs._dialog_body_height(_Screen(400)) == dialogs._DIALOG_BODY_MIN


class _ComfortBody:
    """舒适线用例的假正文滚动区: 只记高度, 供 cget/configure 读写."""

    def __init__(self, height: int) -> None:
        """记下初始高度."""
        self.height = height

    def cget(self, _option: str) -> int:
        """读高度(coverage 之外还会被问别的选项, 这里只关心 height)."""
        return self.height

    def configure(self, **kwargs: Any) -> None:
        """写高度."""
        self.height = int(kwargs["height"])

    def winfo_children(self) -> list[Any]:
        """滚动区内部没有子控件(判据只看高度)."""
        return []


class _ComfortWindow:
    """舒适线用例的假对话框: 屏幕 700(舒适线 560), 请求高度与实测高度分开给.

    ``requested`` 给一个固定的请求高度; ``chrome`` 则按“正文之外 + 正文区”算(真窗口就是
    这样随正文区伸缩的, 夹取之后请求高度会跟着变)。``measured`` 是映射那一刻量到的实测高度,
    可以与请求高度不一致 —— 无窗口管理器的 X11 上它常常晚一拍。
    """

    def __init__(
        self,
        body: _ComfortBody,
        *,
        measured: int,
        requested: int | None = None,
        chrome: int | None = None,
    ) -> None:
        """记下正文区与两个高度(请求高度二选一: 固定值, 或者按正文区算)."""
        assert (requested is None) != (chrome is None), "请求高度只能给一种"
        self._body = body
        self._requested = requested
        self._chrome = chrome
        self._measured = measured

    def update_idletasks(self) -> None:
        """假窗口没有待处理事件."""

    def winfo_screenheight(self) -> int:
        """屏幕高度(舒适线 = 它的 80%)."""
        return 700

    def winfo_reqheight(self) -> int:
        """请求高度: 固定值, 或者"正文之外 + 正文区"."""
        if self._requested is not None:
            return self._requested
        assert self._chrome is not None
        return self._chrome + self._body.height

    def winfo_height(self) -> int:
        """映射之后的实测高度."""
        return self._measured

    def winfo_children(self) -> list[Any]:
        """正文区是唯一的子控件."""
        return [self._body]


def test_the_comfort_line_is_clamped_again_after_the_window_is_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """映射之后要按**实测**高度再收一次(请求高度压线 + 实测多几个像素的形态).

    ``winfo_reqheight()`` 是建窗那一刻的请求高度, 而窗口管理器与 CustomTkinter 会在映射时
    改尺寸 —— 只按它收一次的话最终窗口仍会越线几个像素(CI 实测: 700 高的屏上 Linux
    561 > 560, 而 Windows 恰好 560, 所以这条只在 Linux 红)。
    """
    monkeypatch.setattr(ctk, "CTkScrollableFrame", _ComfortBody)
    body = _ComfortBody(508)
    window: Any = _ComfortWindow(body, requested=560, measured=561)

    dialogs._clamp_to_comfort_line(window)
    assert body.height == 508, "请求高度没越线时不该动正文区"

    dialogs._clamp_to_comfort_line(window, current=561)
    assert body.height == 507, "映射后多出来的 1px 要从正文区里收掉"

    # 收到下限就停手: 矮屏上宁可让正文区滚, 也不能把内容切掉.
    dialogs._clamp_to_comfort_line(window, current=900)
    assert body.height == dialogs._DIALOG_BODY_MIN


def test_the_comfort_line_also_reads_the_requested_height_after_mapping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """映射之后要把**请求高度**也一起看: 实测高度会晚一拍, 比真正的布局小 1px.

    CI 现场(2026-09-30, 只有 Linux 红): 700 高的屏上最终窗口 561 > 560, 而夹取那一刻量到的
    实测高度是 560 —— 只认实测值的话这次夹取什么都不做, 那 1px 就一直留着。请求高度是本地
    立刻算出来的(不等窗口管理器的 ConfigureNotify), 所以两个取大者。
    """
    monkeypatch.setattr(ctk, "CTkScrollableFrame", _ComfortBody)
    body = _ComfortBody(509)
    window: Any = _ComfortWindow(body, measured=560, chrome=52)  # 请求高度 = 52 + 509

    dialogs._clamp_to_comfort_line(window, current=560)

    assert body.height == 508, "请求高度越线时也要收"
    assert window.winfo_reqheight() == 560, "收一轮就该落到线上(收敛)"


def test_the_comfort_line_keeps_shrinking_until_the_request_fits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """布局不保证 1:1 跟着正文高度走: 收一轮不够时继续收, 收到不越线为止.

    假窗口把"正文之外"的那部分也算成 52 —— 真窗口里它可能是别的控件在撑, 于是收掉 1px
    正文只换来 0.x 个像素的整窗高度; 这类情况下只收一轮就停会让窗口一直差着那一两像素。
    """
    monkeypatch.setattr(ctk, "CTkScrollableFrame", _ComfortBody)
    body = _ComfortBody(509)
    window: Any = _ComfortWindow(body, measured=561, chrome=52)

    dialogs._clamp_to_comfort_line(window, current=561)

    assert window.winfo_reqheight() <= 560, "最终要落在舒适线内"
    assert body.height == 508, "只该收掉真正多出来的那 1px"


def test_the_comfort_line_stops_after_the_pass_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """布局一直不合规时**不能无限收**: 收够 :data:`_CLAMP_PASSES` 轮就停手.

    这里让请求高度**固定不动**(现实中是"撑住整窗高度的是别的控件, 收正文区没用"), 于是每轮都
    能收掉 1px、永远差那 1px —— 上限就是为这种情况留的保险。
    """
    monkeypatch.setattr(ctk, "CTkScrollableFrame", _ComfortBody)
    body = _ComfortBody(509)
    window: Any = _ComfortWindow(body, requested=561, measured=561)

    dialogs._clamp_to_comfort_line(window, current=561)

    assert body.height == 509 - dialogs._CLAMP_PASSES, "收够上限就该停手"


def test_import_package_dialog_keeps_the_buttons_outside_the_scrolling_body(
    harness: _FakeParent,
) -> None:
    """28 号评审: 正文可以滚, 但「取消 / 导入」永远留在滚动区外面."""
    harness.click_text = tr("dialog.cancel")

    dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), targets=_targets())
    )

    window = harness.windows[0]
    for text in (tr("dialog.cancel"), tr("dialog.import_confirm")):
        button = next(
            button for button in harness.buttons if button.kwargs.get("text") == text
        )
        # 按钮在 window → buttons 框里, 而不是在正文滚动区里。
        assert _parent_of(_parent_of(button)) is window


def test_import_package_dialog_explains_why_the_target_list_is_locked(
    harness: _FakeParent,
) -> None:
    """28 号评审: 没选"合并"时目标区要写明原因并看得出是禁用的, 选中后说明换回来."""
    dialogs.import_package_dialog(
        harness, DARK, **_import_args(locations=_rows(), targets=_targets())
    )

    targets = _target_radios(harness)
    assert targets, "应当有可合并的目标游戏"
    assert {radio.kwargs.get("state") for radio in targets} == {"disabled"}
    assert {radio.kwargs.get("text_color_disabled") for radio in targets} == {
        DARK.text_disabled
    }
    assert {radio.kwargs.get("fg_color") for radio in targets} == {
        DARK.accent_soft_border
    }, "选中态用淡强调色, 不与「导入」按钮的实心绿撞色"

    hint = next(
        label
        for label in harness.labels
        if label.kwargs.get("text") == tr("dialog.import_target_locked")
    )
    assert hint is not None

    merge = next(
        radio for radio in _strategy_radios(harness) if radio.value == STRATEGY_MERGE
    )
    merge.invoke()

    assert hint.kwargs.get("text") == tr("dialog.import_target_hint")
    assert {radio.kwargs.get("state") for radio in targets} == {"normal"}


# ---------------------------------------------------------------- 批量导出


def _export_batch_prompt() -> models.BatchExportPrompt:
    """两个候选: 一个是本机路径, 一个没有存档位置."""
    return models.BatchExportPrompt(
        summary="共 2 款游戏可以批量导出",
        filter_hint="输入名称可缩小列表",
        options=(
            models.BatchExportOption(
                game_id="1", name="星际拓荒", detail="1 个存档位置 · 1 个备份"
            ),
            models.BatchExportOption(
                game_id="2", name="山海旅人", detail="没有存档位置"
            ),
        ),
    )


def _export_batch_args() -> dict[str, Any]:
    return {
        "title": tr("dialog.export_batch_title"),
        "prompt": _export_batch_prompt(),
        "filter_label": tr("dialog.export_batch_filter"),
        "list_label": tr("dialog.export_batch_list"),
        "no_match_text": tr("dialog.export_batch_no_match"),
        "select_all_label": tr("dialog.export_batch_select_all"),
        "select_all_scope": tr("dialog.export_batch_select_all_scope"),
        "confirm_text": tr("dialog.export_batch_confirm"),
    }


def _tick_boxes(harness: _FakeParent) -> list[Any]:
    """逐行勾选框(全选框除外, 顺序与候选一致).

    用文案认出全选框而不是靠创建顺序: 逐行勾选框的文案是游戏名, 全选框是固定文案。
    """
    master = next(
        box
        for box in harness.checkboxes
        if box.kwargs.get("text") == tr("dialog.export_batch_select_all")
    )
    return [box for box in harness.checkboxes if box is not master]


def _select_all_box(harness: _FakeParent) -> Any:
    """批量导出对话框里的全选框."""
    ticks = _tick_boxes(harness)
    return next(box for box in harness.checkboxes if box not in ticks)


def _export_filter(harness: _FakeParent) -> Any:
    """批量导出的筛选框: 必须是一个**普通输入框**(不是下拉选框).

    用 ``harness.entries`` 取它顺便钉住了控件类型 —— 换回 ``CTkComboBox`` 时这里找不到
    输入框, 用例会直接红。
    """
    assert len(harness.entries) == 1, "批量导出对话框里应当只有一个输入框(筛选框)"
    return harness.entries[0]


def _export_rows(harness: _FakeParent) -> list[Any]:
    """复选框所在的行(筛选只是 pack/pack_forget 这些行)."""
    return [box.master for box in _tick_boxes(harness)]


def test_export_batch_dialog_returns_the_ticked_games(harness: _FakeParent) -> None:
    """确认后返回勾选的游戏(按候选顺序), 默认一个都不勾."""
    harness.click_text = tr("dialog.export_batch_confirm")

    def check_defaults_and_tick_the_second() -> None:
        assert [box.get() for box in _tick_boxes(harness)] == [False, False]
        _tick_boxes(harness)[1].select()

    harness.on_wait = check_defaults_and_tick_the_second

    result = dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    assert result == models.BatchExportChoice(game_ids=("2",))
    assert harness.windows[0].window_title == tr("dialog.export_batch_title")
    texts = [label.kwargs.get("text") for label in harness.labels]
    assert "共 2 款游戏可以批量导出" in texts
    assert _export_batch_prompt().filter_hint in texts


def test_export_batch_dialog_with_nothing_ticked_returns_an_empty_choice(
    harness: _FakeParent,
) -> None:
    """一个都没勾也照常返回(界面据此提示"没有勾选任何游戏"), 而不是当成取消.

    按钮本身在没勾选时是禁用态(见下一个用例), 这里断言的是**兜底**: 万一它被强行
    触发, 也不能把"什么都没选"当成用户取消。
    """
    harness.click_text = tr("dialog.export_batch_confirm")

    result = dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    assert result == models.BatchExportChoice(game_ids=())


def test_export_batch_dialog_disables_confirm_until_something_is_ticked(
    harness: _FakeParent,
) -> None:
    """27 号评审: 一份都没勾时确认按钮就该是禁用态, 勾上第一个之后才亮起来."""
    dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    confirm = next(
        button
        for button in harness.buttons
        if button.kwargs.get("text") == tr("dialog.export_batch_confirm")
    )
    assert confirm.kwargs.get("state") == "disabled"
    assert confirm.kwargs.get("fg_color") == DARK.disabled_bg

    boxes = _tick_boxes(harness)
    boxes[0].select()
    boxes[0].kwargs["command"]()
    assert confirm.kwargs.get("state") == "normal"
    assert confirm.kwargs.get("fg_color") == DARK.accent

    boxes[0].deselect()
    boxes[0].kwargs["command"]()
    assert confirm.kwargs.get("state") == "disabled"


def test_export_batch_dialog_rows_put_the_metadata_on_its_own_line(
    harness: _FakeParent,
) -> None:
    """27 号评审: 勾选框只写游戏名, 原名与状态摘要是另一行、且各用各的颜色."""
    prompt = models.BatchExportPrompt(
        summary="共 1 款游戏可以批量导出",
        filter_hint="输入名称可缩小列表",
        options=(
            models.BatchExportOption(
                game_id="1",
                name="暴食的怪兽公主",
                detail=tr("game.disabled_short"),
                original_name="Kaiju Princess 2",
                original_label=tr("hero.original_name", name="Kaiju Princess 2"),
            ),
        ),
    )
    args = _export_batch_args()
    args["prompt"] = prompt
    dialogs.export_batch_dialog(harness, DARK, **args)

    row_box = _tick_boxes(harness)[0]
    assert row_box.kwargs.get("text") == "暴食的怪兽公主", "勾选框只写游戏名"
    meta = {
        label.kwargs.get("text"): label.kwargs.get("text_color")
        for label in harness.labels
    }
    assert meta.get("· " + tr("hero.original_name", name="Kaiju Princess 2")) == (
        DARK.text_hint
    )
    assert meta.get(tr("game.disabled_short")) == DARK.text_muted
    # 全选的作用域单独成行, 不再挂在"全选"后面同字号同色。
    assert meta.get(tr("dialog.export_batch_select_all_scope")) == DARK.text_hint

    # 勾选行是一张铺满宽度的卡片(右侧不再是空白 + 常驻滚动条)。
    row = row_box.master
    assert row.kwargs.get("fg_color") == DARK.card


# ---------------------------------------------------------------- 批量导入


def _batch_import_prompt() -> models.BatchImportPrompt:
    """两行: 第一行有两条存档位置与可合并的目标, 第二行什么都没有."""
    return models.BatchImportPrompt(
        summary="包内 2 款游戏",
        hint="逐款选择导入方式",
        rows=(
            models.BatchImportRow(
                entry="Demo.archive.zip",
                name="Demo",
                meta="Windows · AppID 730",
                match_text="库里疑似同一款游戏: 星际拓荒(已默认选中)",
                locations=_rows(),
                strategies=models.import_strategies(has_targets=True),
                targets=_targets(),
                strategy=STRATEGY_NEW,
                target_game_id="7",
            ),
            models.BatchImportRow(
                entry="Other.archive.zip",
                name="Other",
                meta="Windows",
                match_text="",
                locations=(),
                strategies=models.import_strategies(has_targets=False),
                targets=(),
                strategy=STRATEGY_NEW,
                target_game_id=None,
            ),
        ),
    )


def _batch_import_args() -> dict[str, Any]:
    return {
        "title": tr("dialog.import_batch_title"),
        "prompt": _batch_import_prompt(),
        "locations_label": tr("dialog.import_locations"),
        "locations_hint": tr("dialog.import_locations_hint"),
        "strategy_label": tr("dialog.import_strategy"),
        "target_label": tr("dialog.import_target"),
        "confirm_text": tr("dialog.import_confirm"),
    }


def _batch_strategy_radios(harness: _FakeParent) -> list[list[_FakeRadio]]:
    """按游戏分行的一组组策略单选按钮(同一行共用一个变量, 且是连着建的)."""
    groups: list[list[_FakeRadio]] = []
    for radio in _strategy_radios(harness):
        if groups and groups[-1][0].variable is radio.variable:
            groups[-1].append(radio)
        else:
            groups.append([radio])
    return groups


def test_batch_import_dialog_collects_defaults_per_game(harness: _FakeParent) -> None:
    """确认后逐款返回选择: 默认"新建", 只带预填过的存档位置, 没目标的那款不带目标."""
    harness.click_text = tr("dialog.import_confirm")

    result = dialogs.batch_import_dialog(harness, DARK, **_batch_import_args())

    assert result is not None
    assert result.choices == {
        "Demo.archive.zip": models.ImportChoice(
            strategy=STRATEGY_NEW, target_game_id=None, locations={0: "D:/saves"}
        ),
        "Other.archive.zip": models.ImportChoice(
            strategy=STRATEGY_NEW, target_game_id=None, locations={}
        ),
    }
    # 每款游戏一行卡片: 名称/标识/存档位置都摆出来了(用户得知道这一行是哪一款).
    texts = [label.kwargs.get("text") for label in harness.labels]
    assert "Demo" in texts
    assert "Windows · AppID 730" in texts
    assert "Other.archive.zip" not in texts
    assert len(harness.entries) == 2


def test_batch_import_dialog_keeps_cards_compact_and_uniform(
    harness: _FakeParent,
) -> None:
    """29 号评审: 卡片紧凑(名称与元信息同一行)、控件同高、边距左右对称、无绿字小标题."""
    dialogs.batch_import_dialog(harness, DARK, **_batch_import_args())

    texts = [label.kwargs.get("text") for label in harness.labels]
    # 四个小标题同一套样式(次要色), 其中"疑似同一款"原来是最亮的提示色, 看着像链接。
    headings = [
        label
        for label in harness.labels
        if label.kwargs.get("text")
        in {
            tr("dialog.import_strategy"),
            tr("dialog.import_target"),
            tr("dialog.import_locations"),
            "库里疑似同一款游戏: 星际拓荒(已默认选中)",
        }
    ]
    assert len(headings) >= 3
    assert {label.kwargs.get("text_color") for label in headings} == {
        DARK.text_muted
    }, "小标题不该有的亮有的暗"
    # 元信息不再单独占一行(它是右侧那一列), 名称与它同一行。
    assert "Windows · AppID 730" in texts
    # 输入框与下拉框统一高度: 同一张卡片里不再出现 28px 与 30px 两种控件高度。
    heights = {
        widget.kwargs.get("height")
        for widget in [*harness.entries, *harness.combos]
        if "height" in widget.kwargs
    }
    assert heights == {dialogs._CARD_CONTROL_HEIGHT}
    # 卡片右边界不再被滚动条压住: 卡片在两份对称的内边距之间, 卡片内的控件也是。
    demo = next(label for label in harness.labels if label.kwargs.get("text") == "Demo")
    card = _parent_of(_parent_of(demo))
    assert card.kwargs.get("fg_color") == DARK.card
    assert card.kwargs.get("corner_radius") == 8
    assert card.pack_kwargs.get("padx") == dialogs._CARD_INSET
    assert harness.entries[0].pack_kwargs.get("padx") == dialogs._CARD_PAD


def test_batch_import_dialog_merges_into_the_chosen_target(
    harness: _FakeParent,
) -> None:
    """选"合并"并把目标改成另一款: 返回的映射里就是那一款的 id."""
    harness.click_text = tr("dialog.import_confirm")
    harness.entry_values = ["", "E:/local-saves"]

    def merge_into_the_second_target() -> None:
        merge = next(
            radio
            for radio in _batch_strategy_radios(harness)[0]
            if radio.value == STRATEGY_MERGE
        )
        merge.invoke()
        harness.combos[0].set("山海旅人")

    harness.on_wait = merge_into_the_second_target

    result = dialogs.batch_import_dialog(harness, DARK, **_batch_import_args())

    assert result is not None
    assert result.choices["Demo.archive.zip"] == models.ImportChoice(
        strategy=STRATEGY_MERGE, target_game_id="9", locations={1: "E:/local-saves"}
    )
    assert result.choices["Other.archive.zip"].strategy == STRATEGY_NEW


def test_batch_import_dialog_merging_without_a_target_keeps_it_empty(
    harness: _FakeParent,
) -> None:
    """选了"合并"却把目标清空: 界面不猜目标(留给后端报"需要先选定要合并到的游戏")."""
    harness.click_text = tr("dialog.import_confirm")

    def merge_and_clear() -> None:
        merge = next(
            radio
            for radio in _batch_strategy_radios(harness)[0]
            if radio.value == STRATEGY_MERGE
        )
        merge.invoke()
        harness.combos[0].set("")

    harness.on_wait = merge_and_clear

    result = dialogs.batch_import_dialog(harness, DARK, **_batch_import_args())

    assert result is not None
    choice = result.choices["Demo.archive.zip"]
    assert choice.strategy == STRATEGY_MERGE
    assert choice.target_game_id is None


def test_batch_import_dialog_starts_in_merge_mode_when_the_row_says_so(
    harness: _FakeParent,
) -> None:
    """模型层给出"合并"作默认值时, 对话框要**开局就是合并态**.

    "库里已有同一款就默认合并"这条要求落在两处: 默认值由 ``models._batch_row`` 给
    (那里有它自己的用例), 对话框把它读进单选变量并**开局就启用**目标下拉框 —— 否则
    用户还得先点一下单选框才会亮起来。两处都断言, 只改一处不会绿。
    """
    args = _batch_import_args()
    args["prompt"] = models.BatchImportPrompt(
        summary="包内 1 款游戏",
        hint="逐款选择导入方式",
        rows=(
            models.BatchImportRow(
                entry="Demo.archive.zip",
                name="Demo",
                meta="Windows · AppID 730",
                match_text="库里疑似同一款游戏: 星际拓荒(已默认选中)",
                locations=_rows(),
                strategies=models.import_strategies(has_targets=True),
                targets=_targets(),
                strategy=STRATEGY_MERGE,
                target_game_id="7",
            ),
        ),
    )

    def check_the_initial_state() -> None:
        merge = next(
            radio
            for radio in _batch_strategy_radios(harness)[0]
            if radio.value == STRATEGY_MERGE
        )
        assert merge.variable is not None
        assert merge.variable.get() == STRATEGY_MERGE, "开局就选中合并"
        assert harness.combos[0].kwargs.get("state") == "normal", "开局就能选目标"

    harness.click_text = tr("dialog.import_confirm")
    harness.on_wait = check_the_initial_state

    result = dialogs.batch_import_dialog(harness, DARK, **args)

    assert result is not None
    assert result.choices["Demo.archive.zip"] == models.ImportChoice(
        strategy=STRATEGY_MERGE, target_game_id="7", locations={0: "D:/saves"}
    )


def test_batch_import_dialog_disables_targets_until_merging(
    harness: _FakeParent,
) -> None:
    """目标下拉框只在"合并"时可用; 没有候选游戏的那一款整块不出现."""
    harness.click_text = tr("dialog.cancel")

    def check_the_states() -> None:
        assert [combo.kwargs.get("state") for combo in harness.combos] == [
            "disabled",
            "disabled",
        ]
        assert harness.combos[1].packed is False
        merge = next(
            radio
            for radio in _batch_strategy_radios(harness)[0]
            if radio.value == STRATEGY_MERGE
        )
        merge.invoke()
        assert harness.combos[0].kwargs.get("state") == "normal"
        assert harness.combos[0].values == ["星际拓荒", "山海旅人"]

    harness.on_wait = check_the_states

    dialogs.batch_import_dialog(harness, DARK, **_batch_import_args())


def test_batch_import_dialog_cancel_returns_none(harness: _FakeParent) -> None:
    """取消: 返回 None, 整批都不导入."""
    harness.click_text = tr("dialog.cancel")

    result = dialogs.batch_import_dialog(harness, DARK, **_batch_import_args())

    assert result is None
    assert harness.windows[0].destroyed is True


def test_export_batch_dialog_cancel_returns_none(harness: _FakeParent) -> None:
    """取消: 返回 None 并关掉窗口."""
    harness.click_text = tr("dialog.cancel")

    result = dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    assert result is None
    assert harness.windows[0].destroyed is True


def test_export_batch_dialog_filter_hides_rows_but_keeps_the_tick(
    harness: _FakeParent,
) -> None:
    """筛选只影响显示: 被筛掉的游戏仍然带着用户的勾选被导出."""
    harness.click_text = tr("dialog.export_batch_confirm")

    def tick_then_filter() -> None:
        _tick_boxes(harness)[1].select()
        _export_filter(harness).type("星际")
        assert [row.packed for row in _export_rows(harness)] == [True, False]
        _export_filter(harness).type("没有这一款")
        assert [row.packed for row in _export_rows(harness)] == [False, False]

    harness.on_wait = tick_then_filter

    result = dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    assert result == models.BatchExportChoice(game_ids=("2",))


def test_export_batch_select_all_ticks_every_visible_row(
    harness: _FakeParent,
) -> None:
    """全选框: 一下把当前筛出的行都勾上, 再点一下全部取消(状态跟着亮/灭)."""
    harness.click_text = tr("dialog.export_batch_confirm")

    def tick_all_then_untick_all() -> None:
        master = _select_all_box(harness)
        assert master.get() is False, "默认没勾选时全选框也不该是勾上的"
        master.invoke()
        assert [box.get() for box in _tick_boxes(harness)] == [True, True]
        assert master.get() is True, "全部勾上后全选框应当跟着亮起来"
        master.invoke()
        assert [box.get() for box in _tick_boxes(harness)] == [False, False]
        assert master.get() is False

    harness.on_wait = tick_all_then_untick_all

    result = dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    assert result == models.BatchExportChoice(game_ids=()), "取消全选后一个都不导"


def test_export_batch_select_all_only_touches_the_filtered_rows(
    harness: _FakeParent,
) -> None:
    """筛选后点全选: 只动当前显示的行, 被筛掉的保持原来的勾选(与筛选框同一条语义)."""
    harness.click_text = tr("dialog.export_batch_confirm")

    def tick_second_then_select_all_visible() -> None:
        _tick_boxes(harness)[1].select()
        _export_filter(harness).type("星际")
        assert [row.packed for row in _export_rows(harness)] == [True, False]
        master = _select_all_box(harness)
        master.invoke()
        assert [box.get() for box in _tick_boxes(harness)] == [True, True]
        master.invoke()
        assert [box.get() for box in _tick_boxes(harness)] == [False, True]

    harness.on_wait = tick_second_then_select_all_visible

    result = dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    assert result == models.BatchExportChoice(game_ids=("2",)), "被筛掉的那一款保留勾选"


def test_export_batch_select_all_is_disabled_when_nothing_matches(
    harness: _FakeParent,
) -> None:
    """一条都不匹配时全选框置灰(没有可见行可全选), 清空筛选后恢复可用."""
    harness.click_text = tr("dialog.export_batch_confirm")

    def filter_to_nothing_then_back() -> None:
        master = _select_all_box(harness)
        assert master.kwargs["state"] == "normal"
        _export_filter(harness).type("没有这一款")
        assert master.kwargs["state"] == "disabled"
        _export_filter(harness).type("")
        assert master.kwargs["state"] == "normal"

    harness.on_wait = filter_to_nothing_then_back

    result = dialogs.export_batch_dialog(harness, DARK, **_export_batch_args())

    assert result == models.BatchExportChoice(game_ids=())


def test_import_game_dialog_cancelled_browse_keeps_the_row(
    harness: _FakeParent,
) -> None:
    """浏览被取消(返回 None)时那一行保持原样, 不会被清空."""

    def browse_then_confirm() -> None:
        for button in harness.buttons:
            if button.text == tr("dialog.browse"):
                button.click()
        next(
            button
            for button in harness.buttons
            if button.text == tr("dialog.import_confirm")
        ).click()

    harness.on_wait = browse_then_confirm
    result = dialogs.import_game_dialog(
        harness,
        DARK,
        title="导入为游戏",
        name_label="确认游戏名称:",
        initial_name="星露谷",
        paths_label="存档路径:",
        paths_hint="提示",
        initial_paths=("C:/kept",),
        add_text=tr("dialog.import_add_path"),
        confirm_text=tr("dialog.import_confirm"),
        browse=lambda: None,
    )

    assert result == ("星露谷", ("C:/kept",))


def test_edit_tags_dialog_at_the_limit_adds_no_empty_row(
    harness: _FakeParent,
) -> None:
    """现有标签已经顶满上限时不再补空行(否则会超出行数上限)."""
    harness.click_text = tr("dialog.tags_save")

    result = dialogs.edit_tags_dialog(harness, DARK, tags=("a", "b", "c"), max_tags=3)

    assert len(harness.entries) == 3
    assert result == ("a", "b", "c")


def test_edit_tags_dialog_removing_the_last_row_restores_an_empty_one(
    harness: _FakeParent,
) -> None:
    """删掉唯一一行后自动补一行空的, 对话框里总有一个输入框可用."""

    def drop_the_only_row() -> None:
        next(b for b in harness.buttons if b.text == tr("dialog.tags_remove")).click()
        next(b for b in harness.buttons if b.text == tr("dialog.tags_save")).click()

    harness.on_wait = drop_the_only_row
    result = dialogs.edit_tags_dialog(harness, DARK, tags=("探索",), max_tags=1)

    assert len(harness.entries) == 2, "删空后应当自动补一行空的"
    assert result == ()


def test_ask_text_browse_fills_the_entry_and_ignores_a_cancelled_pick(
    harness: _FakeParent,
) -> None:
    """浏览按钮的两种结果: 挑到路径就写进输入框, 取消则保持原样."""
    picks: list[str | None] = ["C:/picked", None]

    def browse() -> str | None:
        return picks.pop(0)

    def click_browse_twice() -> None:
        button = next(b for b in harness.buttons if b.text == tr("dialog.browse"))
        button.click()
        button.click()
        next(b for b in harness.buttons if b.text == tr("dialog.confirm")).click()

    harness.on_wait = click_browse_twice
    result = dialogs.ask_text(harness, DARK, title="选目录", text="路径", browse=browse)

    assert picks == [], "两次点击都要真的请求过路径"
    assert result == "C:/picked"
