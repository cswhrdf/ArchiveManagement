"""下拉框弹出列表的端到端用例.

用户 2026-10-02 的两条要求("限高 + 超出就出滚动条, 不要撑出软件界面"、"位于软件下方的
下拉框默认向上")在这里按**量到的几何**验收: 真的把下拉打开, 再读浮层的位置/高度/有没有
滚动条 —— 判据落在"它到底在哪"上, 而不是"代码里写了 flipped 这个分支"。

现场约定(与其它 GUI 用例同): CI 上窗口的实际尺寸不由用例决定, 所以断言一律与**窗口自己**
的矩形比(不写死屏幕坐标), 时间上则用"泵事件到不变量成立"而不是固定秒数。
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from gui_support import gui_app

try:
    import customtkinter as ctk

    import archive_management.ui.dropdown as dropdown_mod
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.dropdown import DROPDOWN_MAX_ROWS, active_dropdown
from archive_management.ui.main_window import ArchiveApp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("下拉框弹出层"),
    pytest.mark.layer("e2e"),
]

# 值多到必须出滚动条: 上限是 10 行, 给 30 个值, 上下都放不下整块。
_MANY_VALUES = [f"选项 {index:02d}" for index in range(30)]


def _build(backend: Any) -> ArchiveApp:
    """建主窗口(热键服务用不可用后端, 免得测试环境真去抢全局快捷键)."""
    return ArchiveApp(
        backend,
        title="下拉测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
    )


def _pump(app: Any, seconds: float = 0.6) -> None:
    """跑一会儿事件循环: 浮层的显示、焦点转移都是延后发生的."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.update_idletasks()
        app.update()
        time.sleep(0.02)


def _open(combo: ctk.CTkComboBox, app: Any) -> dropdown_mod.DropdownPopup:
    """打开下拉并返回浮层(用 CTk 自己的入口, 与点箭头走同一条路)."""
    combo._open_dropdown_menu()
    _pump(app)
    popup = active_dropdown()
    assert popup is not None, "下拉没打开"
    return popup


def test_a_long_dropdown_stops_at_the_cap_and_scrolls() -> None:
    """值很多时: 显示行数锁在上限、右侧给滚动条、整块不越出软件窗口.

    改前 (原生 ``tk.Menu``): 30 个值会排成一列 30 行, 窗口矮的时候直接压到窗口外面。
    """
    app = gui_app(_build, DemoArchiveService(delay=0))
    _pump(app)
    combo = app._home_page._page_size_box
    combo.configure(values=list(_MANY_VALUES))
    popup = _open(combo, app)
    plan = popup.plan
    assert plan is not None
    assert plan.visible_rows == DROPDOWN_MAX_ROWS, "超过上限的部分应该交给滚动条"
    assert plan.scrolls
    assert popup.scrollbar is not None, "限高了就必须有滚动条, 否则看不到后面的值"
    # **真的分到了位置**才算看得见: 2026-10-03 用户报"变成可滚动区域但看不到滚动条" ——
    # 现场是滚动条挂错了父容器(与列表不同容器), 被列表的 expand 吃光后分到 0 高/0 宽。
    bar = popup.scrollbar
    assert isinstance(bar, ctk.CTkScrollbar), "要用 CTk 原生滚动条, 样式才对得上"
    assert int(bar.winfo_height()) > 1, "滚动条分到的高度是 0(所以看不见)"
    assert int(bar.winfo_width()) > 1, "滚动条分到的宽度是 0(所以看不见)"
    assert bar.cget("button_color") != bar.cget("fg_color"), (
        "滑块与底色同色 = 看不出滚动条"
    )
    window = popup.window
    assert window is not None
    # **附属窗口 + 不 topmost**: 用户 2026-10-03 反馈"点击别的软件, 别的窗口被盖住, 但浮窗
    # 还是显示在最前方" —— topmost 是相对整个屏幕的, 浮层会盖在别的程序上面。改成宿主的
    # 附属窗口(transient)之后, 别的程序被激活时浮层就随宿主一起沉下去。
    #
    # 这条断言**只在 macOS 上咬得住**: 那里的无边框窗口 ``-topmost`` 默认读出来就是 1
    # (Windows 上是 0), 2026-10-03 的 macOS CI 正是报了这一条 —— 所以 ``_build`` 里显式
    # 写了一次 ``-topmost False``, 让两个平台的读数是同一个值。
    assert not window.attributes("-topmost"), "浮层不该是 topmost(会盖住别的软件)"
    assert str(window.transient()) == str(app), "浮层应当是宿主窗口的附属窗口"
    app_top = int(app.winfo_rooty())
    app_bottom = app_top + int(app.winfo_height())
    top = int(window.winfo_rooty())
    bottom = top + int(window.winfo_height())
    assert top >= app_top - 1, "浮层跑到窗口上沿外面了"
    assert bottom <= app_bottom + 1, "浮层跑到窗口下沿外面了"
    height = int(window.winfo_height())
    assert height <= plan.height + 4, "浮层比算出来的高度还高(限高没生效)"


def _bottom_window() -> ctk.CTk:
    """建一个自己的小窗口, 下拉框贴在**底部**(用来量"向上展开").

    也走 :func:`gui_support.gui_app`: 它有"Tk 会话抖动"的重试与收尾登记, 自建
    ``ctk.CTk()`` 撞上 ``init.tcl`` 那种抖动时只会直接报错。
    """
    window = ctk.CTk()
    window.geometry("420x260")
    combo = ctk.CTkComboBox(window, values=list(_MANY_VALUES))
    combo.pack(side="bottom", padx=12, pady=(0, 8))
    return window


def _first_combo(window: Any) -> ctk.CTkComboBox:
    """取窗口里唯一的那个下拉框(自建窗口只有一个)."""
    for child in window.winfo_children():
        if isinstance(child, ctk.CTkComboBox):
            return child
    raise AssertionError("窗口里没有下拉框")


def test_a_dropdown_near_the_bottom_opens_upwards() -> None:
    """位于软件下方的下拉框向上展开: 浮层的下沿落在控件上沿以上."""
    app = gui_app(_bottom_window)
    combo = _first_combo(app)
    _pump(app)
    popup = _open(combo, app)
    plan = popup.plan
    assert plan is not None
    assert plan.flipped, "窗口下方放不下, 应该向上展开"
    anchor_top = int(combo.winfo_rooty())
    assert plan.top + plan.height <= anchor_top, "向上展开时不要盖住控件自己"
    assert popup.scrollbar is not None


def test_clicking_the_arrow_opens_the_dropdown() -> None:
    """点箭头真的能打开 —— 走**完整点击路径**.

    出处(用户 2026-10-03): "下拉框点击完全无反应"。根因是打开浮层的那一次点击自己也会
    冒泡到顶层窗口, 而浮层挂了"点窗口别处就收起" —— 当场挂绑定 = 刚开就被这次点击收起。
    直接调 ``_open_dropdown_menu`` 量不到这条闭环(那一步没有点击事件), 所以这里按控件
    自己的坐标点箭头, 让事件走一遍 canvas 的 tag 绑定与顶层窗口的绑定。
    """
    app = gui_app(_build, DemoArchiveService(delay=0))
    _pump(app)
    combo = app._home_page._page_size_box
    combo.configure(values=list(_MANY_VALUES))
    canvas = combo._canvas
    arrow_x = int(canvas.winfo_width()) - 4
    arrow_y = int(canvas.winfo_height()) // 2
    canvas.event_generate("<Button-1>", x=arrow_x, y=arrow_y)
    _pump(app)
    popup = active_dropdown()
    assert popup is not None, "点箭头没打开下拉"
    assert popup.plan is not None
    combo._dropdown_menu.close()
    _pump(app)


def test_picking_a_row_writes_the_value_and_closes() -> None:
    """点一行 = 写进下拉框 + 触发 CTk 自己的回调 + 收起浮层."""
    app = gui_app(_build, DemoArchiveService(delay=0))
    _pump(app)
    combo = app._home_page._page_size_box
    combo.configure(values=list(_MANY_VALUES))
    seen: list[str] = []
    combo.configure(command=seen.append)
    popup = _open(combo, app)
    listbox = popup.listbox
    assert listbox is not None
    target = 2
    box = listbox.bbox(target)
    assert box, "第 3 行没被排出来"
    # 点的是 Listbox 自己给出的行位置(量了再点): 行高由控件决定, 用例不去猜。
    listbox.event_generate("<ButtonRelease-1>", y=int(box[1]) + 1)
    _pump(app)
    assert combo.get() == _MANY_VALUES[target]
    assert seen == [_MANY_VALUES[target]], "选中值没有走到业务回调"
    assert active_dropdown() is None, "选完应该收起浮层"


def test_hiding_the_window_closes_the_dropdown() -> None:
    """宿主窗口最小化/隐藏时收起浮层.

    出处(用户 2026-10-03): "不手动点击软件页面其他处将其关闭的情况下将软件最小化时这个浮窗
    还会显示在原位置"。浮层是**独立**窗口, 不会跟着宿主一起消失, 所以盯宿主的 Unmap。

    用例用 ``withdraw()`` 而不是 ``iconify()``: 两者的效果都是"宿主被 Unmap", 而无窗口
    管理器的环境里 ``iconify`` 未必真的发出事件 —— 判据要能在这里站稳(Windows 上最小化
    就是 Unmap, 见 ``_attach_outside`` 的说明)。
    """
    app = gui_app(_build, DemoArchiveService(delay=0))
    _pump(app)
    combo = app._home_page._page_size_box
    combo.configure(values=list(_MANY_VALUES))
    popup = _open(combo, app)
    window = popup.window
    assert window is not None
    app.withdraw()
    _pump(app)
    assert active_dropdown() is None, "宿主窗口隐藏了, 浮层还开着"
    assert not window.winfo_exists(), "浮层窗口没销毁"
    app.deiconify()
    _pump(app)


def test_moving_the_window_keeps_the_dropdown_glued_to_the_box() -> None:
    """宿主窗口挪动/改尺寸时浮层跟着控件走(别停在旧位置上盖着旁边的程序).

    出处(用户 2026-10-03 的截图): 浮层停在打开时的屏幕坐标上, 窗口挪走之后它反而跑到窗口
    外面、盖在别的程序上。判据是**位置**: 重排一轮之后浮层的左边缘仍与下拉框对齐。

    这里不"收起"是刻意的: 布局期间宿主自己也会发 ``<Configure>``, 一收就会被误踢(实测
    四条用例因此变成"下拉没打开")。
    """
    app = gui_app(_build, DemoArchiveService(delay=0))
    _pump(app)
    combo = app._home_page._page_size_box
    combo.configure(values=list(_MANY_VALUES))
    popup = _open(combo, app)
    window = popup.window
    assert window is not None
    app.geometry("1120x760")
    _pump(app)
    assert active_dropdown() is popup, "窗口尺寸变化不该把浮层踢掉"
    assert int(window.winfo_rootx()) == int(combo.winfo_rootx()), (
        f"浮层没跟着控件走: 浮层 x={window.winfo_rootx()}, 控件 x={combo.winfo_rootx()}"
    )
    combo._dropdown_menu.close()
    _pump(app)


def test_clicking_elsewhere_closes_the_dropdown() -> None:
    """点窗口里别处就收起(与原生菜单一致), 且浮层随控件销毁一起走."""
    app = gui_app(_build, DemoArchiveService(delay=0))
    _pump(app)
    combo = app._home_page._page_size_box
    combo.configure(values=list(_MANY_VALUES))
    popup = _open(combo, app)
    window = popup.window
    assert window is not None
    app.event_generate("<Button-1>", x=4, y=4)
    _pump(app)
    assert active_dropdown() is None, "点别处没收起浮层"
    assert not window.winfo_exists(), "浮层窗口没销毁"
