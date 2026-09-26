"""输入框撤销(Ctrl+Z)的真实控件验证.

单元测试用替身钉住了记录与清理的逻辑(``tests/unit/test_textundo.py``);
这里换成**真实**的 CustomTkinter 控件与真实按键事件, 验证端到端成立:

- 单行输入框按一次 Ctrl+Z 退回一个字符, Ctrl+Y 再重做回来;
- 多行输入框(描述)沿用 Tk 原生撤销, 且弹窗预填的内容不会被抹掉;
- 只读下拉框不接管按键(靠真实的 ``cget("state")`` 返回 ``readonly`` 判定);
- 主窗口里现成的输入控件**全部**接上了撤销 —— 用户反馈的就是"这些框按
  Ctrl+Z 没反应", 因此这条守卫直接遍历真实控件树, 而不是逐个手写清单。
"""

from __future__ import annotations

from typing import Any

import pytest

from gui_support import gui_app

try:
    import customtkinter as ctk  # 既校验可导入, 也用于构造测试用控件
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.services.hotkeys import GlobalHotkeyService, UnavailableBackend
from archive_management.ui.backend import ArchiveService
from archive_management.ui.main_window import ArchiveApp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("输入框撤销"),
    pytest.mark.layer("integration"),
]


def _pump(root: Any) -> None:
    """跑一遍事件循环: 合成的按键要先映射窗口才会落到控件上."""
    root.update_idletasks()
    root.update()


def _type(root: Any, target: Any, text: str) -> None:
    """按真实按键输入文本.

    给内部 Tk 控件焦点后合成 ``<KeyPress>``/``<KeyRelease>`` —— Tk 自己在按下时插入
    字符, 因此走的是与用户输入完全相同的路径(不是直接 ``insert``)。
    """
    target.focus_force()
    _pump(root)
    for char in text:
        target.event_generate("<KeyPress>", keysym=char)
        target.event_generate("<KeyRelease>", keysym=char)
    _pump(root)


def _press(root: Any, target: Any, sequence: str, keysym: str, state: int) -> None:
    """按下一个组合键(如 Ctrl+Z)."""
    target.event_generate(sequence, keysym=keysym, state=state)
    _pump(root)


def _input_widgets(widget: Any) -> list[Any]:
    """递归收集控件树里所有文本输入控件(单行/下拉/多行)."""
    found: list[Any] = []
    for child in widget.winfo_children():
        if isinstance(child, (ctk.CTkEntry, ctk.CTkComboBox, ctk.CTkTextbox)):
            found.append(child)
        found.extend(_input_widgets(child))
    return found


def _has_binding(target: Any, *, modifier: str, detail: str) -> bool:
    """控件上是否绑定了"某修饰键 + 某个键".

    Tk 会把 ``<Control-z>`` 规范化成 ``<Control-Key-z>`` 再存进绑定表, 所以既不能拿
    原字串比对(恒不一), 也不能断言 ``"<Control-z>" not in ...``(恒真 —— 只读用例
    曾因此变成永远通过)。这里按 Tk 的写法拆开判断: 修饰键在段里、末段是目标键。
    """
    wanted = detail.lower()
    for sequence in target.bind():
        parts = str(sequence).strip("<>").split("-")
        if modifier in parts and "Shift" not in parts and parts[-1].lower() == wanted:
            return True
    return False


def _binds_undo(target: Any) -> bool:
    """控件上是否绑了 Ctrl+Z."""
    return _has_binding(target, modifier="Control", detail="z")


def _binds_redo(target: Any) -> bool:
    """控件上是否绑了 Ctrl+Y."""
    return _has_binding(target, modifier="Control", detail="y")


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口(不注册系统级快捷键, 避免遗留键盘钩子)."""
    return ArchiveApp(
        backend,
        title="撤销测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
    )


def test_a_real_entry_undoes_one_keystroke_at_a_time() -> None:
    """单行输入框: Ctrl+Z 逐字符退回, Ctrl+Y 重做回来."""
    root = gui_app(ctk.CTk)
    entry = ctk.CTkEntry(root, width=200)
    entry.pack(padx=10, pady=10)
    _pump(root)

    _type(root, entry._entry, "abc")
    assert entry.get() == "abc", "先确认合成的按键真的输进去了"

    _press(root, entry._entry, "<Control-z>", "z", 0x0004)
    assert entry.get() == "ab"
    _press(root, entry._entry, "<Control-z>", "z", 0x0004)
    assert entry.get() == "a"

    _press(root, entry._entry, "<Control-y>", "y", 0x0004)
    assert entry.get() == "ab", "Ctrl+Y 应把刚撤掉的那一字符重做回来"


def test_a_real_combo_box_entry_undoes_its_typing() -> None:
    """下拉框的可输入部分同样支持撤销(存档位置、排序等都靠它)."""
    root = gui_app(ctk.CTk)
    combo = ctk.CTkComboBox(root, values=["甲", "乙"])
    combo.pack(padx=10, pady=10)
    _pump(root)
    combo.set("")

    _type(root, combo._entry, "xy")
    assert combo.get() == "xy"

    _press(root, combo._entry, "<Control-z>", "z", 0x0004)

    assert combo.get() == "x"


def test_a_real_text_box_undoes_typing_but_keeps_the_prefilled_text() -> None:
    """多行输入框: 预填的描述不可撤销, 用户后写的内容可以撤回去."""
    root = gui_app(ctk.CTk)
    box = ctk.CTkTextbox(root, width=200, height=60)
    box.pack(padx=10, pady=10)
    box.insert("1.0", "预填的描述")  # 弹窗打开时程序写入
    _pump(root)

    _type(root, box._textbox, "hello")
    assert box.get("1.0", "end-1c") == "预填的描述hello"

    _press(root, box._textbox, "<Control-z>", "z", 0x0004)
    assert box.get("1.0", "end-1c") == "预填的描述", "撤销不能抹掉预填内容"

    _press(root, box._textbox, "<Control-z>", "z", 0x0004)
    assert box.get("1.0", "end-1c") == "预填的描述", "再按也不该把预填内容撤没"


def test_a_real_readonly_combo_box_does_not_claim_the_keys() -> None:
    """只读下拉框(界面语言)不接管按键: 它的内部 Entry 必须是 readonly."""
    root = gui_app(ctk.CTk)
    combo = ctk.CTkComboBox(root, values=["中文"], state="readonly")
    combo.pack(padx=10, pady=10)
    _pump(root)

    assert str(combo._entry.cget("state")) == "readonly", "跳过判据依赖这个取值"
    assert not _binds_undo(combo._entry), "只读下拉框不该接管 Ctrl+Z"


def test_every_input_widget_in_the_main_window_supports_undo() -> None:
    """守卫: 主窗口里现成的输入控件必须全都能撤销.

    策略是遍历真实控件树, 而不是维护一张"哪些框要接撤销"的清单 —— 清单一定会
    漏(漏掉的表现只是"这个框按 Ctrl+Z 没反应", 评审和用例都看不出来)。新增页面
    或输入框时这条守卫自动覆盖, 前提是它们经 ``ctk.CTkEntry`` 等构造
    (补丁见 ``archive_management.ui.textundo``)。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)

    inputs = _input_widgets(app)
    assert len(inputs) >= 3, f"控件树里至少应有搜索框/每页条数等输入控件: {inputs}"

    for widget in inputs:
        kind = widget.__class__.__name__
        if isinstance(widget, ctk.CTkTextbox):
            assert widget._textbox.cget("undo") == 1, f"{kind} 没打开原生撤销"
            hint = f"{kind} 缺少 Ctrl+Z 绑定: {widget._textbox.bind()}"
            assert _binds_undo(widget._textbox), hint
        else:
            target = widget._entry
            if str(target.cget("state")) == "readonly":
                continue  # 只读下拉框改不了内容, 按设计不接管按键
            hint = f"{kind} 缺少 Ctrl+Z 绑定: {target.bind()}"
            assert _binds_undo(target), hint
            hint = f"{kind} 缺少 Ctrl+Y 绑定: {target.bind()}"
            assert _binds_redo(target), hint
