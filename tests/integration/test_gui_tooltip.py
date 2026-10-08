"""悬停提示的生命周期: 进入延时弹出, 移出/点下/销毁都要收场.

为什么单独一条: 提示是"被省略号裁掉的文字唯一能看全文的路"(I-3), 而它的**弹窗**
只有真的进出事件才会走 —— 光断言"控件绑了 ``<Enter>``"既证明不了会弹出来, 更证明不了
收得回。收不回的代价是留在屏幕上的幽灵窗口, 以及销毁期间往 stderr 吐一串
``invalid command name``(报告里会挂到别的用例上)。

判据取"锚点下面有没有 Toplevel": 弹窗就是建成锚点子窗口的, 所以它出现/消失本身就是
"弹没弹、收没收"的直读事实, 不必去碰实现里的闭包变量。
"""

from __future__ import annotations

import time
import tkinter as tk
from collections.abc import Iterator
from typing import Any

import customtkinter as ctk
import pytest

from archive_management.ui import widgets
from ui_sharing import SharedUiRegistry, demo_app

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("悬停提示"),
    pytest.mark.layer("e2e"),
]

#: 提示是**延后**弹出的(见 ``widgets._TOOLTIP_DELAY_MS``): 等它过期再驱动事件循环, 否则
#: 量到的只是"还没到点"。
_WAIT_SECONDS = widgets._TOOLTIP_DELAY_MS / 1000 + 0.3


def _pump(app: ctk.CTk, seconds: float = 0.0) -> None:
    """跑一会儿事件循环(提示靠 ``after`` 到期, 必须真的给它时间)."""
    if seconds:
        time.sleep(seconds)
    for _ in range(6):
        app.update_idletasks()
        app.update()


@pytest.fixture
def app(ui_shared: SharedUiRegistry) -> Iterator[Any]:
    """主窗口(共享会话: 用例间由池做快照-识别-还原)."""
    with ui_shared.test_scope(demo_app) as application:
        _pump(application)
        yield application


def _tip_windows(widget: Any) -> list[tk.Toplevel]:
    """挂在锚点控件下面的提示窗口."""
    return [
        child for child in widget.winfo_children() if isinstance(child, tk.Toplevel)
    ]


def _anchor(app: ctk.CTk) -> tk.Label:
    """一个挂在窗口角上、已经映射好的锚点控件.

    用**普通 tk.Label** 而不是 CTk 控件: 合成事件在 CTk 控件上到不了绑定(实测: 同样的
    ``event_generate("<Enter>")`` 在 CTkLabel 上没人接), 而这里要量的本来就不是 CTk 的
    事件转发, 而是 :func:`widgets.attach_tooltip` 自己那套接线(延时弹、移出收、销毁收场)。

    ``place`` 而不是 ``pack``: 主窗口的根已被 grid 管着, 再 pack 会 ``TclError``; 没被映射
    的控件也收不到进入事件, 所以必须真的摆上去(只占一个角)。
    """
    label = tk.Label(app, text="被截断的标题…")
    label.place(x=0, y=0)
    widgets.attach_tooltip(label, "完整标题")
    _pump(app)
    return label


def test_hovering_pops_the_tip_and_leaving_takes_it_away(app: Any) -> None:
    """进入延时后弹出、移出立刻收起; 收起窗口**不等于**把文案撤掉(还要能再弹一次)."""
    label = _anchor(app)

    label.event_generate("<Enter>")
    _pump(app, _WAIT_SECONDS)
    assert _tip_windows(label), "进入后该弹出提示窗口"

    label.event_generate("<Leave>")
    _pump(app)
    assert not _tip_windows(label), "移出后提示要收掉"
    assert widgets.tooltip_text(label) == "完整标题", "收窗口不该把文案也撤掉"

    label.event_generate("<Enter>")
    _pump(app, _WAIT_SECONDS)
    assert _tip_windows(label), "再进入要能再弹: 说明收场之后状态是干净的"
    # 锚点是贴在共享窗口上的: 拆掉它(连带计时器), 别留给后面的用例。
    label.destroy()
    _pump(app)


def test_clicking_away_and_destroying_the_anchor_both_clean_up(app: Any) -> None:
    """点下与控件销毁都要收场: 不留幽灵窗口, 销毁期间也不许吐 ``TclError``."""
    label = _anchor(app)

    label.event_generate("<Enter>")
    _pump(app, _WAIT_SECONDS)
    assert _tip_windows(label)

    label.event_generate("<Button-1>")
    _pump(app)
    assert not _tip_windows(label), "点下就该收掉提示"

    label.event_generate("<Enter>")
    _pump(app, _WAIT_SECONDS)
    label.destroy()  # <Destroy> 那条路: 计时器与窗口都要撤, 且不抛
    _pump(app)


def test_attaching_twice_only_replaces_the_text(app: Any) -> None:
    """同一控件再挂一次只换文案: 事件不重复绑(否则一次悬停会弹出好几层)."""
    label = _anchor(app)
    before = label.bind("<Enter>")

    widgets.attach_tooltip(label, "换过的文案")

    assert widgets.tooltip_text(label) == "换过的文案"
    assert label.bind("<Enter>") == before, "事件被重复绑了一次"
    # 锚点是贴在共享窗口上的: 拆掉它(连带计时器), 别留给后面的用例。
    label.destroy()
    _pump(app)
