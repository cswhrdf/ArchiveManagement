"""GUI 用例的公共支撑: 建主窗口 + 统一收尾.

三个 GUI 用例模块原本各自重复"建窗口(失败就跳过) + ``try/finally: app.destroy()``"
的样板, 共 69 处。现在建窗口统一走 :func:`gui_app`, 销毁交给
``tests/integration/conftest.py`` 里按用例清理的夹具 —— 用例通过、失败或报错时
都会走到, 时机与原来的 ``finally`` 完全一致, 但销毁本身成了报告里的夹具步骤,
也不再需要每个用例重复一遍收尾代码。
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Callable
from typing import Any

import pytest

# 本次用例创建过、还没销毁的窗口(由夹具在用例结束时清空)。
_LIVE_APPS: list[Any] = []

# --- Tk 会话抖动的重试 ------------------------------------------------------
# 同一个进程里反复建/销窗口之后, 解释器偶尔会连 Tcl/Tk 库数据都加载不了, 于是建窗口
# 这一步直接抛错 —— 症状有三种写法(见 tests/tk_guard.KNOWN_TK_SKIP_MARKERS):
# `invalid command name "tcl_findLibrary"`、`Can't find a usable init.tcl`,
# 以及 Tk 侧的 `couldn't read file <...>/tk.tcl`。这是**环境问题**,
# 不是被测行为: 重试一次通常就好(实测同一批用例里只有个别会撞上, 且与用例顺序相关)。
#
# 纪律(由 `tests/unit/test_gui_retry.py` 守住):
# 1. 重试**必须写明原因**(:data:`TK_RETRY_REASON`), 而不是"失败了就再跑一遍";
# 2. 重试必须**在 Allure 报告里看得见** —— 记成参数(可筛选)+ 附件(带原始异常),
#    这样"哪条用例在靠重试过关、为什么"一眼能查, 不会退化成静默重试;
# 3. 重试有上限, 到顶仍然是跳过(而不是把环境问题变成用例失败)。
TK_RETRY_ATTEMPTS = 3
# 两次尝试之间稍等一下: 抖动常出现在前一个窗口刚销毁、Tcl 资源还没释放完时。
TK_RETRY_DELAY_SECONDS = 0.2
# 重试原因(必须写清楚: 这是环境抖动, 不是被测行为; 报告里也是拿这句话解释)。
TK_RETRY_REASON = (
    "Tk 会话抖动: 同进程反复建/销窗口后偶尔加载不了 Tcl/Tk 库数据 "
    '(`invalid command name "tcl_findLibrary"` / `Can\'t find a usable init.tcl` / '
    "`couldn't read file <...>/tk.tcl`), 重试即可恢复, 与用例断言无关"
)


def _record_retry(attempt: int, error: Exception) -> None:
    """把"为什么重试"记进 Allure: 一个可筛选的参数 + 一份带原始异常的附件.

    附件与参数都挂在**当前用例**的结果上: 用例最后还是绿的, 但报告里能看到它重试过、
    重试的是哪一步、原始异常是什么 —— 这正是"重试不能是静默的"的落点。
    """
    import allure

    detail = (
        f"第 {attempt}/{TK_RETRY_ATTEMPTS} 次建窗口失败: {error}\n"
        f"重试原因: {TK_RETRY_REASON}\n"
        f"处理: 等待 {TK_RETRY_DELAY_SECONDS:.1f}s 后重试(最多 {TK_RETRY_ATTEMPTS} 次)"
    )
    allure.attach(
        detail,
        name="Tk 会话抖动: 重试原因",
        attachment_type=allure.attachment_type.TEXT,
    )
    allure.dynamic.parameter("重试", f"第 {attempt} 次: {TK_RETRY_REASON}")


def gui_app(builder: Callable[..., Any], /, *args: Any, **kwargs: Any) -> Any:
    """创建主窗口并登记收尾; 撞上 Tk 会话抖动时**重试**, 仍然不可用才跳过.

    ``builder`` 是各用例模块自己的工厂(``_new_app`` / ``_long_name_app`` 之类),
    参数原样转发。注意要在 ``monkeypatch`` 换掉模态对话框**之后**调用: 否则构造
    过程中弹出的模态框会卡住测试线程。

    抖动说明与纪律见本模块顶部的 :data:`TK_RETRY_REASON`。
    """
    # 延迟导入: 没有 tkinter 的机器上收集期不应报错(那些用例模块自己会整模块跳过)。
    from tkinter import TclError

    from tk_guard import is_known_tk_skip

    attempts = 0
    while True:
        attempts += 1
        try:
            app = builder(*args, **kwargs)
        except TclError as exc:
            if not is_known_tk_skip(str(exc)):
                # 不是"解释器找不到 Tcl 库数据"这类已知环境问题: 那就是用例/被测代码
                # 自己的问题(例如图像的 Tk 名字已经失效), 必须让用例真的红 ——
                # 悄悄跳过会让一条永远是 skip 的用例长期占着位置(见 tests/tk_guard.py)。
                raise
            if attempts >= TK_RETRY_ATTEMPTS:
                pytest.skip(f"tk 环境不可用(重试 {attempts} 次): {exc}")
            _record_retry(attempts, exc)
            time.sleep(TK_RETRY_DELAY_SECONDS)
            continue
        if attempts > 1:
            _record_retry(attempts - 1, RuntimeError("重试后建窗口成功"))
        break
    _LIVE_APPS.append(app)
    return app


def live_apps() -> tuple[Any, ...]:
    """当前用例创建过、还没销毁的窗口(留证截图用: 失败时把现场拍下来).

    返回元组的拷贝: 调用方可能正在遍历它取窗口尺寸, 而截图期间窗口列表不应被改动。
    """
    return tuple(_LIVE_APPS)


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
