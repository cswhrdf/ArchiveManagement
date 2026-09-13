"""对话框构建逻辑的无头单元测试.

用假控件替换 dialogs 依赖的 customtkinter 控件, 不创建真实窗口,
因此不需要显示环境(GUI 冒烟之外, ubuntu CI 也能稳定执行)。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, TypedDict

import customtkinter as ctk
import pytest

import archive_management.ui.dialogs as dialogs
from archive_management.i18n import tr
from archive_management.ui.palette import DARK

pytestmark = [
    pytest.mark.dialogs,
    pytest.mark.ui,
    pytest.mark.critical,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("对话框"),
    pytest.mark.story("输入与确认对话框"),
    pytest.mark.layer("unit"),
]


class _FakeWidget:
    """可 configure / pack / get 的最小假控件."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        self.master = master
        self.kwargs = dict(kwargs)
        self.text = kwargs.get("text")
        self.command: Callable[[], None] | None = kwargs.get("command")
        self._value: str = kwargs.get("text", "")
        # 测试用 harness 预置过内容时, 对话框自身的 initial 回填不再覆盖它.
        self._preset = False

    def configure(self, **kwargs: Any) -> None:
        self.kwargs.update(kwargs)

    def pack(self, **_kwargs: Any) -> None:
        return None

    def focus_set(self) -> None:
        return None

    def get(self) -> str:
        return self._value

    def bind(self, _sequence: str, _callback: Any) -> None:
        return None

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


class _FakeTextbox(_FakeWidget):
    """假多行文本框: 只保留最后写入的内容."""

    def get(self, _start: str = "1.0", _end: str = "end") -> str:
        return self._value


class _FakeWindow(_FakeWidget):
    """假顶级窗口: 提供对话框所需的几何/几何查询 API."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.window_title = ""
        self.geometry_text: str | None = None
        self.destroyed = False

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

    def update_idletasks(self) -> None:
        return None

    def update(self) -> None:
        return None

    def winfo_rootx(self) -> int:
        return 5

    def winfo_rooty(self) -> int:
        return 8

    def winfo_width(self) -> int:
        return 320

    def winfo_height(self) -> int:
        return 180

    def destroy(self) -> None:
        self.destroyed = True


class _FakeParent(_FakeWidget):
    """假主窗口: 记录 winfo 数值, 在 wait_window 时点击指定按钮."""

    def __init__(
        self,
        buttons: list[_FakeWidget],
        windows: list[_FakeWindow],
        entries: list[_FakeWidget],
    ) -> None:
        super().__init__()
        self.buttons = buttons
        self.windows = windows
        self.entries = entries
        self.click_text: str | None = None
        self.wait_called = False
        self.entry_value = ""
        # 一次对话框含多个输入框时按顺序取值(优先于 entry_value).
        self.entry_values: list[str] = []
        self.textbox_value = ""

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
        if self.click_text is None:
            return
        for button in self.buttons:
            if button.text == self.click_text:
                button.click()
                return


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> _FakeParent:
    """把 dialogs 依赖的 ctk 控件替换为假实现, 返回假主窗口."""
    windows: list[_FakeWindow] = []
    buttons: list[_FakeWidget] = []
    entries: list[_FakeWidget] = []
    parent = _FakeParent(buttons, windows, entries)

    def make_window(master: Any = None, **kwargs: Any) -> _FakeWindow:
        window = _FakeWindow(master=master, **kwargs)
        windows.append(window)
        return window

    def make_button(master: Any = None, **kwargs: Any) -> _FakeWidget:
        button = _FakeWidget(master=master, **kwargs)
        buttons.append(button)
        return button

    def make_entry(master: Any = None, **kwargs: Any) -> _FakeWidget:
        entry = _FakeWidget(master=master, **kwargs)
        if parent.entry_values:
            entry._value = parent.entry_values.pop(0)
            entry._preset = True
        elif parent.entry_value:
            entry._value = parent.entry_value
            entry._preset = True
        entries.append(entry)
        return entry

    def make_textbox(master: Any = None, **kwargs: Any) -> _FakeTextbox:
        box = _FakeTextbox(master=master, **kwargs)
        if parent.textbox_value:
            box._value = parent.textbox_value
            box._preset = True
        return box

    monkeypatch.setattr(ctk, "CTkToplevel", make_window)
    monkeypatch.setattr(
        ctk,
        "CTkLabel",
        lambda master=None, **kwargs: _FakeWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(
        ctk,
        "CTkFrame",
        lambda master=None, **kwargs: _FakeWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(ctk, "CTkButton", make_button)
    monkeypatch.setattr(ctk, "CTkEntry", make_entry)
    monkeypatch.setattr(ctk, "CTkTextbox", make_textbox)
    monkeypatch.setattr(ctk, "CTkFont", lambda **_kwargs: object())
    return parent


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
    desc_prompt: str


def _edit_args() -> _EditArgs:
    return {
        "title": tr("dialog.rename_title"),
        "name_label": tr("dialog.rename_label"),
        "desc_label": tr("dialog.describe_label"),
        "desc_prompt": tr("dialog.describe_prompt", limit=200),
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
