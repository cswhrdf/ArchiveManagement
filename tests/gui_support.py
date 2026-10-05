"""GUI 用例的公共支撑: 建主窗口 + 统一收尾.

三个 GUI 用例模块原本各自重复"建窗口(失败就跳过) + ``try/finally: app.destroy()``"
的样板, 共 69 处。现在建窗口统一走 :func:`gui_app`, 销毁交给
``tests/conftest.py`` 里按用例清理的夹具 —— 用例通过、失败或报错时
都会走到, 时机与原来的 ``finally`` 完全一致, 但销毁本身成了报告里的夹具步骤,
也不再需要每个用例重复一遍收尾代码。

收尾这一步现在还要**保证根窗口真的被拆掉**(见 :func:`close_gui_apps`): 销毁链
断在半路会把 ``tkinter._default_root`` 留在会话里, 之后每一条建窗口的用例都会撞上
``image "pyimageN" does not exist``(2026-10-04 Linux 分片 0 的事故)。
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

# 本次用例创建过、还没销毁的窗口(由夹具在用例结束时清空)。
_LIVE_APPS: list[Any] = []

#: "销毁链断过、靠补救才拆干净"的记录(见 :func:`close_gui_apps`); 守卫读它。
_TEARDOWN_RESCUES: list[str] = []

#: "默认根位置上挂着别人的根, 已收掉"的记录(见 :func:`_discard_stray_root`); 守卫读它。
#: 不进 `_TEARDOWN_RESCUES`: 那一个是"我们登记的窗口没拆干净", 会判红; 这个不能判红
#: (无法归因给当前用例), 但必须看得见。
_STRAY_ROOTS: list[str] = []

#: 把"占着默认根位置的别人的根"收掉时最多试几次。第一次偶尔打不中: Tk 抖动, 负载重时更
#: 容易(实测偶发), 见 :func:`_discard_stray_root`。
_STRAY_DESTROY_ATTEMPTS = 2

# --- Tk 会话抖动的重试 ------------------------------------------------------
# 同一个进程里反复建/销窗口之后, 解释器偶尔会连 Tcl/Tk 库数据都加载不了, 于是建窗口
# 这一步直接抛错 —— 症状有几种写法(见 tests/tk_guard.KNOWN_TK_SKIP_MARKERS):
# `invalid command name "tcl_findLibrary"`、`Can't find a usable init.tcl`,
# 以及 Tcl/Tk 侧的 `couldn't read file <...>/tk.tcl|auto.tcl`。这是**环境问题**,
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
    "`couldn't read file <...>/tk.tcl|auto.tcl`), 重试即可恢复, 与用例断言无关"
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
    # "这条用例开始时"的调度线程数: 在创建窗口**之前**采样, 否则新窗口自己 start 的那个调度器会
    # 被算进基线, 收尾时它就永远不超线了(守卫白设)。用途见 close_gui_apps。
    scheduler_baseline = len(_scheduler_threads())
    while True:
        attempts += 1
        opened_before = _default_root_object()
        try:
            app = builder(*args, **kwargs)
        except TclError as exc:
            if not is_known_tk_skip(str(exc)):
                # 不是"解释器找不到 Tcl 库数据"这类已知环境问题: 那就是用例/被测代码
                # 自己的问题(例如图像的 Tk 名字已经失效), 必须让用例真的红 ——
                # 悄悄跳过会让一条永远是 skip 的用例长期占着位置(见 tests/tk_guard.py)。
                raise
            # 这一次失败可能已经留下了"半成品根"(见 _discard_stray_root): 不收拾的话,
            # 下面重试成功建起来的那个窗口**不会**是默认根。
            _discard_half_built_root(opened_before)
            if attempts >= TK_RETRY_ATTEMPTS:
                pytest.skip(f"tk 环境不可用(重试 {attempts} 次): {exc}")
            _record_retry(attempts, exc)
            time.sleep(TK_RETRY_DELAY_SECONDS)
            continue
        if attempts > 1:
            _record_retry(attempts - 1, RuntimeError("重试后建窗口成功"))
        break
    _LIVE_APPS.append(app)
    # 记在窗口上而不是模块级变量 —— 一个进程里可能同时存在多个窗口。
    app._scheduler_baseline = scheduler_baseline
    return app


def forget_app(app: Any) -> None:
    """把窗口从收尾登记里摘掉(守卫用它摆出"会话里还留着**别人**的根").

    收尾只负责它登记过的窗口; 用例要测兜底那一段时, 得先把这个窗口摘出去 —— 否则
    `close_gui_apps` 会顺手把它拆了, 测到的就是"正常收尾"而不是"别人的根"。

    注意摘掉之后就**没人**再负责它了: 只能靠收尾的最后那一步兜底收掉, 这不正是要测的吗。
    """
    with contextlib.suppress(ValueError):
        _LIVE_APPS.remove(app)


def live_apps() -> tuple[Any, ...]:
    """当前用例创建过、还没销毁的窗口(留证截图用: 失败时把现场拍下来).

    返回元组的拷贝: 调用方可能正在遍历它取窗口尺寸, 而截图期间窗口列表不应被改动。
    """
    return tuple(_LIVE_APPS)


def close_gui_apps() -> None:
    """销毁当前用例创建的全部窗口(后建先销), 并**保证根窗口不留在会话里**.

    用例自己提前销毁过窗口时(例如中途重建主窗口)这里会再销毁一次 —— 窗口已经没了,
    收尾不应因此报错, 所以只咽掉 ``TclError``。

    但"咽掉"不等于"拆干净了": ``tkinter.Tk.destroy()`` 是

        for c in list(self.children.values()): c.destroy()

    —— **任意一个子控件**那一步抛 ``TclError``, 整条链就断在半路, 根窗口与
    ``tkinter._default_root`` 一起留下来。之后每一条建窗口的用例都会撞上

        TclError: image "pyimage1" does not exist

    因为 CTkImage 内部的 ``ImageTk.PhotoImage`` **不带 master**, 会落到
    ``_default_root``(那个没被拆掉的窗口)的解释器里, 而标签属于新窗口的解释器。
    出处: 2026-10-04 Linux 分片 0 报在 ``test_icon_slot_survives_switching_and_deleting_games``
    上; 用 CI 留下的崩溃现场 dump 查出 ``_default_root`` 是上一批用例里一个没拆掉的
    ``ArchiveApp``(它的 ``_poll_job`` 已置空 = ``destroy()`` 跑过, 但根窗口还在)。

    所以这里改成"**验证 + 补救 + 判红**": 销毁之后确认根窗口真的没了, 没拆干净就用
    逐个拆子控件的方式补救(单个子控件失败不再拖垮整条链), 记进报告, 然后**把这条用例
    判红** —— 断裂是缺陷(只是它不总在本机复现), 记一笔就算过会让"本机绿、CI 偶尔红
    且永远只能看到受害者"继续下去。记录方式沿用 `tests/tk_guard.py` 那套"环境问题要看
    得见"(附件 + 参数 + stderr), 但结论相反: 那条是 skip, 这条是失败 —— 跳过的前提是
    "这条用例量不到东西", 而这里断言都真跑过了, 只是收尾留了个活根。

    拆的顺序也是被实测教训过的: **先 `destroy_widget_tree` 把控件树逐层拆掉, 再调窗口
    自己的 `destroy()`**。前者不可靠 —— 快照里的兄弟/子控件可能已被连带拆掉(见
    :func:`destroy_widget_tree`, 4 条用例就是栽在这里); 后者必须跑, 因为 ``after`` 任务
    的撤销与外观/缩放/字体的解绑都在它那里。两遍下来再判"根还在不在", 剩下的才是真缺陷。
    """
    from tkinter import TclError

    broken: list[str] = []
    baselines: list[int] = []
    while _LIVE_APPS:
        app = _LIVE_APPS.pop()
        # 记下这条用例**开始时**的调度线程数(建窗口那一刻记在窗口上, 见 gui_app): 收尾的判据是
        # "这条用例有没有攒下活的调度器", 而不是"进程里一个都不能有" —— 同一个进程里还会跑非 GUI
        # 的集成用例, 它们建的真后端一样会起 scheduler(实测有一条会漏, 见 PLAN §15.9), 绝对判据
        # 会把别人的存量算到这条用例头上。
        baselines.append(int(getattr(app, "_scheduler_baseline", 0)))
        # **先**放后台资源再拆控件: 一个活着的调度器随时可能在自己的线程里回调到界面, 而 Tk
        # 正在被拆 —— macOS 上从非主线程碰已销毁的 Tk 是已知的段错误源(PLAN §39.4)。方法幂等,
        # 已经走过 `_on_close()` 的窗口再调一次是空操作。
        _release_background(app)
        close_child_windows(app)
        teardown = destroy_widget_tree(app)
        error: BaseException | None = None
        try:
            app.destroy()
        except TclError as exc:
            error = exc
        # 判据**只看根还在不在**, 不看 destroy() 有没有抛异常: 用例自己提前销毁过窗口时,
        # 这里的第二次 destroy() 必然抛 "bad window path name"(窗口已经没了), 而那正是上面
        # 写明的、要容忍的用法(实测 test_gui_buttons / test_gui_layout / test_gui_theme_repaint
        # 里几十处都这么写); 同理 "can't delete Tcl command" 也可能只是"控件已被连带拆掉"或
        # "账上留着已经删掉的命令"(后者会被 destroy_widget_tree 修掉)。
        # 反过来, 两遍之后根还在才是真缺陷 —— 只看异常会把前一种全判成红。
        if not root_is_alive(app):
            if error is not None or teardown.stuck or teardown.repaired:
                # 根是干净的, 所以不判红; 但"收尾并不完全干净"这件事得看得见。
                _record_teardown_note(app, error, teardown)
            continue
        rescue_stuck = force_destroy(app)
        broken.append(
            _record_teardown_rescue(app, error, [*teardown.stuck, *rescue_stuck])
        )
    # 最后一步: `tkinter._default_root` 这个全局位置也得是干净的 —— 哪怕那里挂着的是
    # **别人的**根。留着它, 后面每一条用例贴图都会报 `image "pyimageN" does not exist`
    # (2026-10-04 的事故), 所以这里是那件事的最后一道兜底。
    stray = _default_root_object()
    if stray is not None:
        _discard_stray_root(stray, phase="收尾时")
    # 后台资源也得不留: 一个进程里跑完整套界面用例会攒下几十个活着的 `APScheduler` 线程, 而
    # Tk 解释器早被销毁 —— 那正是那次 macOS SIGTRAP 现场里挂着的东西(约 30 个
    # `apscheduler..._main_loop`, 见 PLAN §39.4)。判据是**相对**的(见上面 baselines 的说明):
    # 收尾之后不得超过"这条用例开始时"那个数, 超了就是这条用例攒下的。
    # 这条用例压根没建自己的窗口(baselines 为空)时**不判**: 没有可归因的对象, 而进程里的存量
    # 可能是别人留下的 —— 界面文件里有一批用例只建真后端不建窗口(`SqlArchiveService(...)` 直接
    # 用), 它们的存量不该由下一条无辜用例来报(实测: 不做这个区分, 整文件会多出 27 条收尾红)。
    allowed = min(baselines) if baselines else 0
    if baselines and _wait_for_scheduler_threads_at_most(allowed) > allowed:
        broken.append(
            "收尾后攒下了活的后台调度线程(没释放, 或那个后端没有释放入口): "
            + ", ".join(_scheduler_threads())
            + f"; 这条用例开始时是 {allowed} 个"
        )
    if broken:
        # pytrace=False: 这里没有"出错的那一行"可指, 要紧的是上面那段现场描述(哪条用例、
        # 哪个子控件、原始 TclError)。pytest 会把它记成 teardown 阶段失败。
        pytest.fail("\n\n".join(broken), pytrace=False)


def _release_background(app: Any) -> None:
    """让窗口释放后台资源(调度器 + 快捷键监听); 不是我们的窗口就跳过.

    只调 ``_release_background``(幂等, 见 ``main_window.ArchiveApp._release_background``),
    **不**在这里自己 shutdown 后端 —— 释放的时机与顺序只有产品知道(例如"同一个 backend 中途
    重建窗口"的写法里, 提前释放会让第二个窗口的调度整片消失)。
    """
    release = getattr(app, "_release_background", None)
    if release is None:
        return
    with contextlib.suppress(Exception):
        release()


def _scheduler_threads() -> list[str]:
    """还活着的后台调度线程(``apscheduler`` 的调度线程名字里带 ``APScheduler``).

    为什么这么认: 一个活着的 scheduler 恰好一个这样的线程, 而它崩在 macOS 上的栈就是
    ``apscheduler/schedulers/blocking.py::_main_loop``(PLAN §39.4 的现场)。名字里带
    ``APScheduler`` 的只有它, 不会误伤 pytest / uv / 线程池那些线程。
    """
    return [
        f"{thread.name}(daemon={thread.daemon})"
        for thread in threading.enumerate()
        if "APScheduler" in thread.name
    ]


def _wait_for_scheduler_threads_at_most(allowed: int, timeout: float = 2.0) -> int:
    """等到活着的调度线程不超过 ``allowed`` 个, 返回最后数到的个数.

    按"等不变量成立"判, 而不是释放完立刻断言: 产品侧的 ``shutdown`` 用的是 ``wait=False``
    (退出不该被阻塞), 收尾是异步的 —— 实测本机几十毫秒内就干净, 2 秒上限只是给慢机器余量。
    """
    deadline = time.monotonic() + timeout
    count = len(_scheduler_threads())
    while count > allowed and time.monotonic() < deadline:
        time.sleep(0.02)
        count = len(_scheduler_threads())
    return count


def close_child_windows(app: Any) -> None:
    """先关掉用例自己打开的附属窗口, 再拆主窗口.

    Toplevel 在 Tcl 侧挂在根窗口下、在 Python 侧挂在锚点控件下(两套"父亲"不一致),
    先按 Python 的账把它们拆干净, 主窗口的销毁链就少一处"半路炸掉"的机会。

    整段都咽掉异常: 用例自己提前销毁过窗口的话(实测 `test_gui_branch_graph` 的夹具
    就是这么做的), 这里问控件树时解释器已经没了 —— 收尾不该因此报错。
    """
    with contextlib.suppress(Exception):
        import customtkinter as ctk

        children = getattr(app, "winfo_children", None)
        if not callable(children):
            return  # 替身窗口(单元测试里的 _FakeApp)没有控件树
        for child in list(children()):
            if isinstance(child, ctk.CTkToplevel):
                with contextlib.suppress(Exception):
                    child.destroy()


def root_is_alive(app: Any) -> bool:
    """窗口的根还在不在(没有解释器、或解释器已经拆掉时都算"不在了")."""
    try:
        tk_obj = getattr(app, "tk", None)
        if tk_obj is None:
            return False
        return bool(tk_obj.call("winfo", "exists", "."))
    except Exception:  # 解释器被拆掉时问不了(半成品窗口连 tk 都没有)
        return False


def _default_root_object() -> Any:
    """`tkinter._default_root` 当前指着谁(私有全局, 类型存根里没有 -> 用 getattr)."""
    import tkinter

    return getattr(tkinter, "_default_root", None)


def _discard_half_built_root(opened_before: Any) -> None:
    """把"建窗口那一次失败留下的半成品根"收掉(没留下东西就什么都不做).

    ``Tk.__init__`` 一上来就把 ``tkinter._default_root`` 指向自己, 而构造函数可能在
    后面某一步抛错(Tk 会话抖动)。那个半成品再没有对象能去销毁它 —— 它就一直占着
    "默认根"这个位置: 之后建起来的窗口**不是**默认根, 任何不带 master 的图片会落到
    那个已经不能用的解释器里(症状与 2026-10-04 的事故一模一样)。
    """
    current = _default_root_object()
    if current is None or current is opened_before:
        return
    _discard_stray_root(current, phase="建窗口失败那一次")


def _discard_stray_root(root: Any, *, phase: str) -> None:
    """把"占着默认根位置的别人的根"收掉, 并记进报告(**不判红**).

    两种来源: ① 上面那种重试留下的半成品; ② 用例或第三方库在没有窗口时建控件/图片,
    tkinter 自己造出来的隐藏根。

    为什么只记不判红: 收尾时无法把"这个根是谁留下的"归因给当前用例(它可能是任何一条
    更早的用例, 也可能是上一步重试), 贸然判红会让整批用例连锁变红、把真正的那条盖掉。
    但可见性不能少(附件 + 参数 + stderr), 而"会话现在安全吗"由
    `tests/integration/test_gui_roots.py` 的守卫直接盯住默认根的状态。

    **无论拆没拆掉, 这个位置都要腾干净**: 会伤到后面用例的是"默认根指着一个不能用的
    解释器", 而不是"那个解释器还在不在"。拆不掉时把原因(异常原文)一并写进现场 ——
    实测它会偶发(Tk 抖动, 负载重时更容易), 而静默的"没收掉"比报警更难查。
    """
    import allure

    alive = root_is_alive(root)
    stuck: list[str] = []
    for _attempt in range(_STRAY_DESTROY_ATTEMPTS):
        try:
            stuck = force_destroy(root)
        except Exception as exc:  # 半成品可能连控件树都问不了(实测会 RecursionError)
            stuck = [f"<拿不到控件树>: {exc!r}"]
        if not root_is_alive(root):
            break
        time.sleep(TK_RETRY_DELAY_SECONDS / 2)
    survived = root_is_alive(root)
    if _default_root_object() is root:
        _clear_default_root(root)
    detail = (
        f"默认根的位置上还挂着**别人的**根({phase}): {root!r}\n"
        f"它还活着吗(收之前 / 收之后): {alive} / {survived}\n"
        f"拆不掉的原因: {stuck or '<没有>'}\n"
        f"腾干净之后的默认根: {_default_root_object()!r}"
    )
    _STRAY_ROOTS.append(detail)
    allure.attach(
        detail,
        name="会话里残留的根: 已收掉",
        attachment_type=allure.attachment_type.TEXT,
    )
    allure.dynamic.parameter(
        "残留的根",
        [
            f"{phase}: 收之前{'还活着' if alive else '已经没用了'}",
            f"{phase}: 收之后{'还在' if survived else '没了'}",
        ],
    )
    print(
        f"\n[gui_support] 会话里残留的根(已收掉, 不判红): {detail}\n", file=sys.stderr
    )


def stray_roots() -> tuple[str, ...]:
    """本次会话里"默认根位置被别人的根占着"的记录(守卫看它, 报告里也找得到)."""
    return tuple(_STRAY_ROOTS)


def _clear_default_root(root: Any) -> None:
    """把 `tkinter._default_root` 这个位置清空(只清指着 `root` 的那一个)."""
    import tkinter

    if getattr(tkinter, "_default_root", None) is root:
        tkinter._default_root = None  # type: ignore[attr-defined]


@dataclass(frozen=True)
class TreeTeardown:
    """控件树拆除的结果(见 :func:`destroy_widget_tree`)."""

    #: 没拆掉的控件(名字与异常原文) —— 这一条会让根窗口留下来, 是缺陷。
    stuck: tuple[str, ...] = ()
    #: "账上留着已经删掉的 Tcl 命令"、清账之后拆成功的控件 —— 修好了, 但要看得见。
    repaired: tuple[str, ...] = ()


def _widget_exists(widget: Any) -> bool:
    """控件的窗口路径还在不在(被连带拆掉的控件返回 False)."""
    try:
        path = getattr(widget, "_w", None)
        tk_obj = getattr(widget, "tk", None)
        if path is None or tk_obj is None:
            return False
        return bool(tk_obj.call("winfo", "exists", path))
    except Exception:  # 解释器没了 / 半成品控件: 都算"不在了"
        return False


def _forget_deleted_tcl_commands(widget: Any) -> list[str]:
    """把控件 `_tclCommands` 里"其实已经不存在"的命令名划掉, 返回划掉的名单.

    为什么会有"账实不符": ``_tclCommands`` 是控件"我注册过哪些 Tcl 命令"的账本, 而命令可能
    先被删掉(``after`` 的回调触发后会自己 ``deletecommand``、控件被连带拆掉、别处先拆了
    同一条), 账本上却还留着名字。于是这个控件**自己的** ``destroy()`` 会在
    ``Misc.destroy`` 的 ``deletecommand`` 那一步抛::

        TclError: can't delete Tcl command

    而 ``Tk.destroy()`` 是"取快照 + 一次性循环", 这一抛就把循环打断, 根窗口留在会话里
    (2026-10-04 CI: Linux 上 4 条用例的收尾报"断裂", 报的正是这个字符串)。这里按
    "账实不符就修账"处理: 逐条拿 ``info commands`` 核对, 不存在的从账上划掉。
    """
    commands = getattr(widget, "_tclCommands", None)
    if not commands:
        return []
    tk_obj = getattr(widget, "tk", None)
    if tk_obj is None:
        return []
    kept: list[str] = []
    removed: list[str] = []
    for name in list(commands):
        try:
            alive = bool(tk_obj.call("info", "commands", name))
        except Exception:  # 解释器都没了: 全部当"已经不存在"
            alive = False
        (kept if alive else removed).append(str(name))
    if removed:
        with contextlib.suppress(AttributeError):
            widget._tclCommands = kept or None
    return removed


def destroy_widget_tree(root: Any) -> TreeTeardown:
    """逐层拆掉 ``root`` 下的全部控件(**不含 root 自己**), 返回没拆掉的那些.

    为什么不把这件事交给 ``Tk.destroy()``: 它是"**先取快照**, 再逐个 ``child.destroy()``"
    的一次性循环, 而快照里的控件可能已经被前一次销毁**连带**带走 —— 于是循环走到它时窗口
    已经没了, ``TclError("can't delete Tcl command")`` 把整条循环打断, 根窗口留在会话里
    (2026-10-04 CI: Linux 上 4 条用例的收尾因此报"断裂")。出处是 CustomTkinter 6.0.0::

        class CTkScrollableFrame(tkinter.Frame):
            def destroy(self):
                tkinter.Frame.destroy(self)
                self._parent_frame.destroy()   # <- 顺手拆掉自己的容器

    而那个容器正是它在 ``children`` 里的**兄弟**(滚动区自己的 master 是容器里的 canvas,
    容器挂在页面上), 所以谁先被拆, 谁就把对方连带带走。

    这里的三个做法各堵一种情况:

    - **自底向上**: 父控件的循环不会再因为某个子控件出问题而中断;
    - **动手前先看窗口还在不在**: 被连带带走的控件直接跳过(既不报错也不记一笔), 并把它从
      ``children`` 里摘掉 —— 后面 ``Tk.destroy()`` 的快照就不会再撞上它;
    - **每个控件各自兜异常, 抩不动就先修账再试一次**: 单个控件的 destroy 抛出时先查它
      `_tclCommands` 里是不是留着已经删掉的命令(见 :func:`_forget_deleted_tcl_commands`),
      清掉账再拆一次; 仍然不行才记进 ``stuck``(那才会把根留下来)。
    """
    stuck: list[str] = []
    repaired: list[str] = []
    children = getattr(root, "children", None)
    if not isinstance(children, dict):
        return TreeTeardown()  # 替身对象没有控件树
    items = list(children.items())
    for _name, child in items:
        nested = destroy_widget_tree(child)
        stuck.extend(nested.stuck)
        repaired.extend(nested.repaired)
    for name, child in items:
        if not _widget_exists(child):
            # 已经被连带拆掉了(见上): 不是缺陷, 也不该记一笔; 但**必须摘掉这个死条目**,
            # 否则 Tk.destroy 的快照还会撞上它。
            children.pop(name, None)
            continue
        try:
            child.destroy()
            continue
        except Exception as exc:  # 单个控件出错只影响它自己
            reason = repr(exc)
            removed = _forget_deleted_tcl_commands(child)
        if not removed:
            stuck.append(f"{name} ({type(child).__name__}): {reason}")
            continue
        try:
            child.destroy()
        except Exception as exc2:
            stuck.append(f"{name} ({type(child).__name__}): 清账后仍失败 {exc2!r}")
            continue
        repaired.append(
            f"{name} ({type(child).__name__}): {reason} -> 划掉账上已删的命令 "
            f"{removed} 后拆成功"
        )
    return TreeTeardown(stuck=tuple(stuck), repaired=tuple(repaired))


def force_destroy(app: Any) -> list[str]:
    """把没被真正拆掉的窗口硬拆掉, 返回"把销毁链断掉"的子控件描述.

    逐个子控件拆: 某一个抛异常只影响它自己, 剩下的照拆 —— ``Tk.destroy()`` 是一次
    性循环, 一个失败就全停, 这正是根被留下来的原因。最后单独拆根窗口并清掉
    ``tkinter._default_root``(即使根还活着也要清: 留着它, 后面用例的图片就会被建到
    **这个**解释器里, 症状与事故完全一样)。

    返回值里也可能出现一条 ``<根窗口 .>`` —— 根窗口自己那一下没拆掉的原因(实测偶发:
    Tk 抖动, 负载重时更容易). 静默的"没收掉"比报警更难查, 所以它跟子控件走同一个账。
    """
    tk_obj = getattr(app, "tk", None)
    if tk_obj is None:
        return []
    stuck: list[str] = []
    for name, child in list(getattr(app, "children", {}).items()):
        try:
            child.destroy()
        except Exception as exc:  # 什么异常都要拆得下去
            stuck.append(f"{name} ({type(child).__name__}): {exc!r}")
    try:
        tk_obj.call("destroy", ".")
    except Exception as exc:  # 连根窗口都拆不掉(抖动下偶发): 记下来, 别静默
        stuck.append(f"<根窗口 .>: {exc!r}")
    _clear_default_root(app)
    return stuck


def teardown_rescues() -> tuple[str, ...]:
    """本次会话里"靠补救才拆干净"的记录(守卫看它, 报告里也找得到)."""
    return tuple(_TEARDOWN_RESCUES)


def _record_teardown_note(
    app: Any, error: BaseException | None, teardown: TreeTeardown
) -> str:
    """把"收尾报了一声、但根已经拆干净"记进报告(附件 + 参数 + stderr), **不判红**.

    为什么不判红: 会伤到后面用例的是"根留在会话里"(那一种由 :func:`close_gui_apps` 判红),
    而这个分支里根已经没了 —— 报的那一声来自 Tk 自己的时序/记账问题(控件已被连带拆掉、
    账上留着已删的命令, 见 :func:`destroy_widget_tree`)。但它必须看得见: 一旦它变成常态,
    就说明收尾的方式又该改了; 而且**这条记录里带着控件名与异常原文** —— 下次再出现就能直接
    点名是哪个控件、哪一条命令。
    注: stderr 里那些 ``invalid command name "...check"/"...sync_now"/"...apply"`` 是 Tk 与
    CustomTkinter **自己**的定时任务在窗口销毁过程中触发的(它们在销毁那一下才被排上, 撤不
    到), 不能当成本用例的缺陷; 我们自己的会自报家门(见 ``main_window._poll_messages``)。
    """
    import allure

    current = os.environ.get("PYTEST_CURRENT_TEST", "<不在用例里>")
    detail = (
        f"收尾报了一声, 但根已经拆干净(不判红)。\n"
        f"用例: {current}\n"
        f"窗口: {app!r}\n"
        f"destroy() 抛的异常: {error!r}\n"
        f"没拆掉的控件: {list(teardown.stuck) or '<没有>'}\n"
        f"靠清账救回来的控件: {list(teardown.repaired) or '<没有>'}\n"
        f"根还在吗: {root_is_alive(app)}"
    )
    allure.attach(
        detail,
        name="窗口收尾: 报了一声但已拆干净",
        attachment_type=allure.attachment_type.TEXT,
    )
    allure.dynamic.parameter("窗口收尾", "destroy() 报错/有控件没拆, 但根已拆干净")
    print(f"\n[gui_support] 窗口收尾(不判红): {detail}\n", file=sys.stderr)
    return detail


def _record_teardown_rescue(
    app: Any, error: BaseException | None, stuck: list[str]
) -> str:
    """把"销毁链断过、已补救"记进 Allure 与 stderr, 并返回这段描述(给 pytest.fail 用).

    记录与判红是两件事: 记录让"哪条用例的窗口没拆干净、卡在哪个子控件"一眼能查;
    判红由调用方(``close_gui_apps``)做 —— 断裂本身是**缺陷**, 不是可以忽略的环境噪声。
    """
    import allure

    current = os.environ.get("PYTEST_CURRENT_TEST", "<不在用例里>")
    detail = (
        f"收尾时窗口没被真正销毁(销毁链断在半路), 已逐个拆子控件补救。\n"
        f"用例: {current}\n"
        f"窗口: {app!r}\n"
        f"destroy() 抛的异常: {error!r}\n"
        f"卡住的子控件: {stuck or '<没有: 只是根窗口没被拆掉>'}\n"
        f"补救后根还在吗: {root_is_alive(app)}"
    )
    _TEARDOWN_RESCUES.append(detail)
    allure.attach(
        detail,
        name="窗口收尾异常: 已补救",
        attachment_type=allure.attachment_type.TEXT,
    )
    allure.dynamic.parameter("窗口收尾", "靠补救才拆干净")
    print(
        f"\n[gui_support] 窗口收尾异常(已补救, 用例判红): {detail}\n", file=sys.stderr
    )
    return detail
