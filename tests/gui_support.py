"""GUI 用例的公共支撑: 建主窗口 + 统一收尾.

三个 GUI 用例模块原本各自重复"建窗口(失败就跳过) + ``try/finally: app.destroy()``"
的样板, 共 69 处。现在建窗口统一走 :func:`gui_app`, 销毁交给
``tests/integration/conftest.py`` 里按用例清理的夹具 —— 用例通过、失败或报错时
都会走到, 时机与原来的 ``finally`` 完全一致, 但销毁本身成了报告里的夹具步骤,
也不再需要每个用例重复一遍收尾代码。
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import pytest

# 本次用例创建过、还没销毁的窗口(由夹具在用例结束时清空)。
_LIVE_APPS: list[Any] = []


def gui_app(builder: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """创建主窗口并登记收尾; 没有图形环境时跳过当前用例.

    ``builder`` 是各用例模块自己的工厂(``_new_app`` / ``_long_name_app`` 之类),
    参数原样转发。注意要在 ``monkeypatch`` 换掉模态对话框**之后**调用: 否则构造
    过程中弹出的模态框会卡住测试线程。
    """
    # 延迟导入: 没有 tkinter 的机器上收集期不应报错(那些用例模块自己会整模块跳过)。
    from tkinter import TclError

    try:
        app = builder(*args, **kwargs)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    _LIVE_APPS.append(app)
    return app


def close_gui_apps() -> None:
    """销毁当前用例创建的全部窗口(后建先销).

    用例自己提前销毁过窗口时(例如中途重建主窗口)这里会再销毁一次 —— 窗口已经没了,
    收尾不应因此报错, 所以只咽掉 ``TclError``。
    """
    from tkinter import TclError

    while _LIVE_APPS:
        app = _LIVE_APPS.pop()
        with contextlib.suppress(TclError):
            app.destroy()
