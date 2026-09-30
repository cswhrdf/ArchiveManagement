"""键盘可用性的守卫(I-6: 纯键盘能走完整个流程).

七条判据, 每一条都对应一个实测出来的缺口(不是"应该会好用"):

A. **每个可交互控件都在 Tab 链里** —— CustomTkinter 的按钮/单选/复选画在 Canvas
   上, 内层 ``takefocus`` 是空串而 Tk 对 Canvas 的默认启发式是"不进 Tab 链":
   纯键盘用户根本到不了按钮上(实测 ``tk_focusNext`` 只能走到输入框)。量法是读
   ``takefocus``: 按钮类必须显式 ``1``, 输入类不许显式 ``0``。
   **不用 ``tk_focusNext`` 直接走一遍链**: 它由 Tcl 侧的 ``tk.tcl`` 定义, 而同进程
   反复建/销窗口时本机会出现"加载不了 Tcl/Tk 库数据"的抖动(见
   ``tests/gui_support.py`` 的 :data:`TK_RETRY_REASON`), 那时它直接报
   ``invalid command name "tk_focusNext"`` —— 守卫不能建在这种量具上。
   "真的按 Tab 走得过去"这件事在探针里验过一次(修前只有 2 个输入框, 修后 40+ 个
   控件全在链上), 结论记在 ``docs/testing.md``。
B. **禁用态不在 Tab 链里** —— 把 Tab 花在一个按不动的控件上是白费。
C. **焦点看得见** —— 聚焦时描边要换成**与聚焦后底色**分得开的颜色(不是恒定的
   强调色: 主色实底按钮的底色就是强调色); 主色/危险色实底按钮还要**换成抢眼的
   那一档环色**(用户实测: 深色环在主色实底上"看起来只是按钮缩小了一圈"),
   做法是聚焦时换上对应的软底配色; 失焦要**原样还原**(描边与配色都不是猜一个颜色)。
D. **Esc = 取消, 回车 = 主操作** —— 对话框里两个键都要真的接上, 并且"回车按下的
   按钮"必须是这一屏的主操作(强调色, 其次危险色)。
E. **Tab 顺序 = 视觉顺序** —— Tk 的 ``tk_focusNext`` 按窗口的**堆叠顺序**(≈ 创建
   顺序)走, 与 ``grid`` 的 ``row``/``column`` 无关。首页操作行因此出现过"Tab 先从
   归档走到启动"的反序(12 号实测反馈)。量法是比"创建顺序"与"按 (row, column)
   排序"两个序列。
F. **键盘操作不了的控件不进 Tab 链** —— 下拉框的值只能用鼠标点开列表选, 让 Tab
   停在它上面等于告诉用户"这里能按"(12 号实测反馈)。
G. **窗口级 Esc/回车只属于对话框** —— 常驻窗口(设置、定时任务)吃下这两个键就会
   出现"按 Esc 把设置窗口关了""按回车切了主题"(12 号实测反馈)。

最后一条是**兜底断言**: 每个界面都必须真的量到控件, 否则夹具坏了会让上面几条静默空转。

做法: 打开每个对话框(模态框在替身 ``wait_window`` 那一刻量), 量完再收尾销毁;
按键行为单独驱动一次, 用 ``event_generate`` 真的按下 Esc/回车/空格。
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.i18n import tr
from archive_management.services.hotkeys import (
    ACTION_SAVE_NOW,
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui import contrast, dialogs, keyboard
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import (
    BatchImportPrompt,
    BatchImportRow,
    ImportLocationRow,
    ImportPrompt,
    ImportTargetOption,
    export_batch_prompt,
    exportable_games,
    import_strategies,
)
from archive_management.ui.palette import DARK, Palette

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("纯键盘可用"),
    pytest.mark.layer("e2e"),
]

# 生成事件前要跑的完整事件循环次数: 实测只 ``update_idletasks`` 时窗口还没映射,
# 发给子控件的按键会被丢掉(docs/testing.md 里同一条坑)。
_SETTLE_ROUNDS = 3

# 量不出来的控件(界面在忙 / 本环境推不上焦点): 不计问题, 但要留下痕迹 ——
# "静悄悄的绿"是这个仓库最讨厌的事(焦点环那一条就是因为合成事件假绿过一次)。
_UNMEASURED: list[str] = []


def _pump(app: ctk.CTk) -> None:
    """把待处理事件跑完, 保证布局与绑定都已生效."""
    for _ in range(6):
        app.update_idletasks()
        app.update()


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口: 不注册系统级快捷键, 避免遗留键盘钩子."""
    return ArchiveApp(
        backend,
        title="键盘测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("键盘测试禁用")),
    )


@pytest.fixture
def app() -> Any:
    """主窗口."""
    application = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(application)
    return application


def _state(widget: Any) -> str:
    try:
        return str(widget.cget("state"))
    except Exception:
        return ""


def _label(widget: Any) -> str:
    """控件的可读名字: 类型 + 文案(失败信息里要能一眼找到是谁)."""
    try:
        text = str(widget.cget("text"))
    except Exception:
        text = ""
    return f"{type(widget).__name__}({text!r})"


def _settle(window: Any) -> None:
    """把窗口真正映射出来(生成事件之前必须做这件事)."""
    for _ in range(_SETTLE_ROUNDS):
        window.update_idletasks()
        window.update()


def _focus(window: Any, widget: Any) -> None:
    """把焦点真的放到控件上(生成子控件事件之前必须做这件事).

    实测三件事: ① 窗口不可见时子控件收不到按键; ② ``focus_set`` 在窗口可见时生效;
    ③ **对窗口本身** ``focus_force()`` 反而会把子控件的焦点顶掉(焦点回到窗口上), 所以
    这里只显示窗口 + 给子控件 ``focus_set``。
    """
    inner = keyboard.focus_target(widget)
    with suppress(Exception):
        window.deiconify()
    _settle(window)
    with suppress(Exception):
        (inner if inner is not None else widget).focus_set()
    _settle(window)


def _takefocus(widget: Any) -> str:
    """读内部控件的 ``takefocus``(空串 = 交给 Tk 的默认规则)."""
    inner = keyboard.focus_target(widget)
    if inner is None:
        return ""
    try:
        return str(inner.cget("takefocus"))
    except Exception:  # pragma: no cover - 控件类型不支持该选项
        return ""


def _in_tab_chain(widget: Any) -> bool:
    """这个控件会不会被 Tab 走到(按钮显式进链; 输入类靠 Tk 默认).

    Tk 的默认规则: 输入类(Entry 等)在链上, Canvas 不在 —— 而 CustomTkinter 的
    按钮就是 Canvas, 所以它们必须由 :mod:`archive_management.ui.keyboard` 显式
    写 ``takefocus=1``; 反之禁用态的按钮必须显式 ``0``(把 Tab 花在按不动的控件上
    是白费)。
    """
    value = _takefocus(widget)
    if isinstance(widget, keyboard.ACTIVATABLE_TYPES):
        return value in ("1", "True", "true")
    return value not in ("0", "False", "false")


def _controls(window: Any) -> list[Any]:
    """界面里全部"该量"的控件: 接键盘线的那几类 + 键盘操作不了的那几类.

    后者也要量 —— 判据是**反的**(它们不许进 Tab 链), 但同样得有人盯着。
    """
    wanted = (*keyboard.REACHABLE_TYPES, *keyboard.UNOPERABLE_TYPES)
    return [widget for widget in keyboard.walk(window) if isinstance(widget, wanted)]


def _position(widget: Any) -> tuple[int, int]:
    """控件在网格里的位置(不是 ``grid`` 摆放的返回一个很大的值, 排到最后)."""
    try:
        info = widget.grid_info()
        return (int(info["row"]), int(info["column"]))
    except Exception:
        return (1_000, 1_000)


def _order_problems(label: str, window: Any) -> list[str]:
    """E: Tab 顺序必须等于**视觉顺序**.

    Tk 的 ``tk_focusNext`` 走的是窗口的堆叠顺序 —— 对同一个父控件下的兄弟来说就是
    **创建顺序**(与 ``grid`` 的 ``row``/``column`` 无关)。所以"先建归档再建启动,
    但把启动摆在归档左边"的界面, Tab 会反着走(12 号实测反馈)。

    量法: 拿每个父控件里"真的会被 Tab 走到"的**同类可聚焦控件**(``REACHABLE_TYPES``
    且 ``takefocus`` 允许), 比创建顺序与按 (row, column) 排序后的顺序。标签、
    下拉框内部的 Canvas/Entry 都不参与(它们本来就不吃焦点)。
    不依赖 ``tk_focusNext``(见文件头 A 的理由)。
    """
    found: list[str] = []
    containers = [window, *keyboard.walk(window)]
    for parent in containers:
        children = [
            widget
            for widget in parent.winfo_children()
            if isinstance(widget, keyboard.REACHABLE_TYPES)
            and _in_tab_chain(widget)
            and _state(widget) != "disabled"
        ]
        if len(children) < 2:
            continue
        expected = sorted(children, key=_position)
        if children != expected:
            found.append(
                f"{label}: {type(parent).__name__} 里的 Tab 顺序与摆放顺序不一致: "
                f"实际 {[_label(w) for w in children]} vs "
                f"期望 {[_label(w) for w in expected]}"
            )
    return found


def _control_problems(label: str, widget: Any) -> list[str]:
    """控件级别的几条: 在 Tab 链里、禁用态摘出去、可激活的要能空格按下."""
    found: list[str] = []
    inner = keyboard.focus_target(widget)
    if inner is None:
        return [f"{label}: {_label(widget)} 没有承载焦点的内部控件"]
    reachable = _in_tab_chain(widget)
    disabled = _state(widget) == "disabled"
    if isinstance(widget, keyboard.UNOPERABLE_TYPES):
        # F: 值只能用鼠标选的控件不许进 Tab 链(也不许画焦点环).
        if reachable:
            found.append(
                f"{label}: {_label(widget)} 键盘操作不了却进了 Tab 链"
                f"(takefocus={_takefocus(widget)!r})"
            )
        if keyboard.is_reachable(widget):
            found.append(f"{label}: {_label(widget)} 键盘操作不了却接了焦点环")
        return found
    if disabled and reachable:
        found.append(f"{label}: 禁用的 {_label(widget)} 还在 Tab 链里")
    if not disabled and not reachable:
        found.append(f"{label}: {_label(widget)} 不在 Tab 链里(Tab 走不到)")
    if isinstance(widget, keyboard.ACTIVATABLE_TYPES):
        if not keyboard.is_reachable(widget):
            found.append(f"{label}: {_label(widget)} 没接过键盘线")
        elif not inner.bind("<space>"):
            found.append(f"{label}: {_label(widget)} 空格按不动")
    return found


def _open(app: Any, call: Callable[[], Any]) -> Any:
    """打开一个对话框并把它**留下**来量(替身 wait_window 记下窗口就返回).

    对话框函数在替身里立刻返回, 所以窗口仍在 ``app`` 的孩子里 —— 这与
    ``test_gui_styles`` 的做法一致, 量完自己销毁即可。

    **量之前先 deiconify**: CustomTkinter 的标题栏着色会先隐藏窗口、再靠一个 5ms
    定时器把它显示回来; 测试里的 ``update()`` 不会等那个定时器, 于是窗口一直停在
    "隐藏"状态 —— 而隐藏的窗口收不到按键, 也定不了焦(实测 ``focus_set`` 不生效)。
    """
    before = set(app.winfo_children())
    app.wait_window = lambda *_args, **_kwargs: None
    call()
    _pump(app)
    fresh = [child for child in app.winfo_children() if child not in before]
    window = fresh[-1] if fresh else None
    if window is not None:
        with suppress(Exception):
            window.deiconify()
        _settle(window)
    return window


def _focus_sink(window: Any) -> Any:
    """造一个"接走焦点"的控件: 量完一个控件就把焦点挪给它, 于是每个控件都被真的失焦.

    三条实测约束:

    * 它必须**真的被摆进布局**(映射出来). 没摆进布局的控件 ``focus_force`` 之后,
      上一个控件收不到 ``FocusOut``, 于是"失焦还原原描边"会被量成假红;
    * 摆放位置要在**可见区域里**: 固定尺寸的窗口(设置/游戏管理)里摆在很靠下的
      行会超出客户区, 那样的控件一样吃不到焦点;
    * 摆放管理器要跟随窗口已有的那种: 对话框的容器是 ``pack``、工作窗口是 ``grid``,
      用错了 Tk 直接报 "cannot use geometry manager grid ... managed by pack"。

    1 像素宽、放在 (0, 0) 上会遮住别的控件一小块 —— 量完立刻销毁, 而且它没有文字,
    留下的唯一影响是那一个像素。
    """
    sink = ctk.CTkEntry(window, width=1, height=1)
    if any(
        child.winfo_manager() == "grid"
        for child in window.winfo_children()
        if child is not sink
    ):
        sink.grid(row=0, column=0, sticky="nw")
    else:
        sink.pack(side="bottom")
    _settle(window)
    return sink


def _defocus(window: Any, sink: Any) -> None:
    """把焦点从当前控件上拿走(量"失焦还原原描边"的前提).

    先试"把焦点挪给 sink"; 抓取式窗口(游戏管理)里这一步**实测不生效** —— 那 12 颗
    按钮聚焦后一直不失焦。兜底是给**窗口** ``focus_force``(相当于用户切到别的窗口),
    它一定会把子控件的焦点顶掉。
    """
    inner = keyboard.focus_target(sink)
    if inner is not None:
        inner.focus_force()
        _settle(window)
    with suppress(Exception):
        window.focus_force()
    _settle(window)
    if inner is not None:
        inner.focus_force()
        _settle(window)


def _tree_shape(root: Any) -> tuple[str, ...]:
    """整棵控件树的"形状": 用来判断界面还有没有在重建."""
    return tuple(str(widget) for widget in keyboard.walk(root))


def _stabilize(root: Any) -> None:
    """等界面把挂起的重绘跑完再量(最多 8 轮).

    主页有一个"延后重排"(``_schedule_list_sync``)与延后的名称重裁: 它们跑起来会**整批
    换掉**控件。量到一半时换掉的话, 手里那个控件已经不在界面上了 —— 给它 ``focus_force``
    Tk 会把"最后焦点"记下来(所以焦点看着是放上去了), 但**不会**产生 ``FocusIn``, 于是
    焦点环看起来"没画"(实测本地就能复现, CI 上则是常事)。

    这与仓库里"先确认触发条件再量"的做法一致: 先让树长稳, 再取控件、再量。
    """
    last: tuple[str, ...] | None = None
    for _ in range(8):
        shape = _tree_shape(root)
        if shape == last:
            return
        last = shape
        _settle(root)


def _focus_hard(root: Any, inner: Any) -> bool:
    """把焦点真放到 ``inner`` 上, 返回是不是真放上去了(最多试三次).

    ``focus_force`` 一般一次就生效, 但 CI 上实测有量不到的情况(窗口刚建出来、
    控件正在重建、另一个窗口抢走了激活态)。量不到就不能当"环没画"报红 —— 那是在
    冤枉实现, 也正好是这一轮 CI 报的那个假红。
    """
    for _ in range(3):
        inner.focus_force()
        _settle(root)
        focused = root.focus_get()
        if focused is inner or str(focused) == str(inner):
            return True
    return False


def _enabled_now(root: Any, widget: Any) -> bool:
    """等控件回到可用态(界面在忙时会临时把按钮置灰, 那时它本来就不画焦点环)."""
    for _ in range(6):
        if _state(widget) != "disabled":
            return True
        _settle(root)
    return False


def _ring_measurable(widget: Any) -> bool:
    """量焦点环的两个前提: 这个控件该有环, 而且现在真的在界面上、不是灰的.

    置灰的控件实现里直接跳过(不该有环), 所以那时量到的"没画环"是环境而不是缺陷;
    隐藏页面/滚动区外的控件本来就走不到, 也无所谓环。
    """
    if isinstance(widget, keyboard.UNOPERABLE_TYPES):
        return False
    return _enabled_now(widget, widget) and bool(widget.winfo_viewable())


def _focus_and_read(
    widget: Any, inner: Any, sink: Any
) -> tuple[str, str, str, str] | None:
    """量一次"聚焦前 → 聚焦后"的颜色, 最多试三次; 画不上去就返回 ``None``.

    **为什么要重试**: 量的时候界面还在自己跑(忙碌收尾 / 延后重排), 实测偶尔某一次
    聚焦就是没生效 —— 那时报"没画环"是在冤枉实现(CI 上那条就是这么假红的)。
    判据本身不放松: **三次都画不上去**才算量不出来, 真没实现环的话每次都画不上去。

    读完就返回: 中间不再多跑事件循环(实测每多跑一轮就多一次"界面又动了"的机会)。
    """
    for _ in range(3):
        _defocus(widget, sink)
        before_border = str(widget.cget("border_color"))
        before_fill = str(widget.cget("fg_color"))
        if not _focus_hard(widget, inner) or not bool(widget.winfo_ismapped()):
            continue
        focused_border = str(widget.cget("border_color"))
        if focused_border != before_border:
            return (
                before_border,
                before_fill,
                focused_border,
                str(widget.cget("fg_color")),
            )
    return None


def _ring_canary(sink: Any) -> bool:
    """这个窗口现在能不能把焦点交付出去(拿焦点黑洞当煤鸟, 它自己也接焦点环).

    煤鸟能画环 = 同一窗口里"聚焦→画环"这条路是通的 → 目标控件画不出来就是缺陷。
    煤鸟也画不出来 = 这一轮是环境(窗口没被激活 / 界面正在重建), 不能记在实现头上。

    有了它, :func:`_focus_and_read` 的重试就不会把"真没实现环"也当成量不出来 ——
    那正是本轮修假红时最不该放松的东西。
    """
    inner = keyboard.focus_target(sink)
    if inner is None:  # pragma: no cover - 已在上一条报过
        return False
    for _ in range(3):
        before = str(sink.cget("border_color"))
        inner.focus_force()
        _settle(sink)
        if str(sink.cget("border_color")) != before:
            return True
    return False


def _unmeasured_problems(label: str, widget: Any, sink: Any) -> list[str]:
    """量不到焦点环时怎么办: 对照控件画得上就是真缺陷, 对照也画不出就是环境.

    环境那一路记进 ``_UNMEASURED`` 并附在失败信息里 —— 不计问题, 但不静悄悄。
    """
    if _ring_canary(sink):
        return [
            f"{label}: {_label(widget)} 聚焦后根本没画焦点环"
            f"(同一窗口的对照控件画得上, 所以不是环境问题)"
        ]
    _UNMEASURED.append(
        f"{label}: {_label(widget)} 三次都没量到焦点环"
        f"(本环境推不上焦点, 对照控件也一样)"
    )
    return []


def _restore_problems(
    label: str, widget: Any, before: tuple[str, str], restored: tuple[str, str]
) -> list[str]:
    """失焦还原的判据: 描边与底色都要回到聚焦前的样子."""
    if restored == before:
        return []
    return [
        f"{label}: {_label(widget)} 失焦后没还原"
        f"(描边 {before[0]} → {restored[0]}, 底色 {before[1]} → {restored[1]})"
    ]


def _ring_problems(label: str, widget: Any, sink: Any) -> list[str]:
    """C: 焦点环要与**聚焦后的底色**分得开, 且失焦要把颜色与描边原样还原.

    必须用**真焦点**(``focus_force`` + 把焦点挪给另一个控件): 实测
    ``event_generate("<FocusIn>")`` **根本不会触发绑定**(合成事件对焦点事件无效),
    拿它当量具会让这一条永远"看着还行"—— 一开始就是这么假绿的。

    ``sink`` 是一个专门用来"接走焦点"的控件: 把焦点挪给它就会产生真正的
    ``FocusOut``, 于是"原样有没有还回来"也是量出来的。

    **先失焦再取基准**: 对话框打开时会定焦到第一个输入框, 那一刻它已经戴着焦点环;
    不先把它放下就直接取基准, 会得到"聚焦前 = 环色"这种颠倒的基准(实测: 四条
    "失焦后描边没还原"其实都是这个原因)。

    **实底按钮会在聚焦时换色**(用户要求的"显眼"), 所以: 期望的环色按**换完之后**的
    底色算(聚焦后控件上读到的 ``fg_color`` 就是新底色), 并且额外断言底色与文字色
    也还原了 —— 只盯描边会放过"按钮聚焦一次之后颜色就变了"这种事故。

    **先确认触发条件再量**: 界面在忙时会把按钮临时置灰, 置灰的控件本来就不画环
    (实现里也直接跳过), 那时量到的"没画环"是环境而不是缺陷; 界面自己在跑的时候也会
    偶尔有一次聚焦不生效, 所以量一次不算数(:func:`_focus_and_read` 试三次)。
    量不出来时再用同窗口的对照控件当煤鸟分辨"环境"还是"真缺陷"
    (:func:`_unmeasured_problems`) —— 环境那一路记进 ``_UNMEASURED``, 免得静悄悄地
    变成绿, 而真缺陷照样报红。

    只量用户真能聚焦到的控件: 隐藏页面/滚动区外的按钮本身就走不到, 也就无所谓环。
    """
    if not _ring_measurable(widget):
        return []
    inner = keyboard.focus_target(widget)
    sink_inner = keyboard.focus_target(sink)
    if inner is None or sink_inner is None:  # pragma: no cover - 已在上一条报过
        return []
    measured = _focus_and_read(widget, inner, sink)
    if measured is None:
        return _unmeasured_problems(label, widget, sink)
    before_border, before_fill, focused_border, focused_fill = measured
    ring = keyboard.ring_color(widget, fill=focused_fill)  # 与实现同一条取色路径
    _defocus(widget, sink)
    restored = (str(widget.cget("border_color")), str(widget.cget("fg_color")))
    problems = _restore_problems(label, widget, (before_border, before_fill), restored)
    if problems:
        return problems
    if ring is None:
        return [f"{label}: {_label(widget)} 算不出焦点环颜色"]
    if not (contrast.is_hex(focused_fill) and contrast.is_hex(ring)):
        return []
    if focused_border != ring:
        return [
            f"{label}: {_label(widget)} 聚焦后描边是 {focused_border}, 应该是 {ring}"
        ]
    ratio = contrast.contrast_ratio(ring, focused_fill)
    if ratio < keyboard.FOCUS_RING_MINIMUM:
        return [
            f"{label}: {_label(widget)} 的焦点环({ring})在底色({focused_fill})上只有 "
            f"{ratio:.2f}:1 (< {keyboard.FOCUS_RING_MINIMUM})"
        ]
    palette = keyboard._palette_for(widget)
    if (
        palette is not None
        and before_fill in (palette.accent, palette.danger)
        and ring != palette.focus_ring
    ):
        # 实底按钮(用户报的"有额外颜色的按钮"): 光"≥3:1"不够 —— 深色环虽然达标却
        # 看起来像"按钮缩小了一圈", 所以这里钉住"必须换成抢眼的那一档环色"。
        return [
            f"{label}: {_label(widget)} 是实底按钮, 焦点环应该用 "
            f"{palette.focus_ring}(抢眼的那一档), 实际是 {ring}"
        ]
    return []


def _widget_problems(label: str, widget: Any, sink: Any) -> list[str]:
    """一个控件的全部判据: Tab 链/禁用态/可激活 + 焦点环与失焦还原."""
    return [*_control_problems(label, widget), *_ring_problems(label, widget, sink)]


def _problems(label: str, window: Any, *, dialog: bool) -> list[str]:
    """量一个界面的键盘可及性, 返回问题清单.

    ``dialog=True`` 指"抓取式窗口": 它吃窗口级的 Esc/回车, 也要求打开就定焦。
    游戏管理窗口虽然是常驻工作窗口, 但它 ``grab_set`` 了, 因此按对话框判据量。
    """
    _pump(window)
    _stabilize(window)
    controls = _controls(window)
    if not controls:
        return [f"{label}: 一个可交互控件都没量到(夹具失效, 上面几条会静默空转)"]
    found: list[str] = []
    if dialog:
        found.extend(_dialog_problems(label, window))
    else:
        # G: 常驻窗口不许吃窗口级的 Esc/回车(否则按 Esc 会关窗、按回车会按到主按钮).
        found.extend(
            f"{label}: 常驻窗口绑了 {key}, 它会吃掉这个键"
            for key in ("<Escape>", "<Return>")
            if window.bind(key)
        )
        # 也不许"打开就抢焦点": 实测那个默认焦点落在"界面字号"下拉框上(键盘动不了).
        if getattr(window, "_focus_when_visible", None) is not None:
            found.append(f"{label}: 常驻窗口被自动定焦了(打开就抢焦点)")
    found.extend(_order_problems(label, window))
    found += _all_widget_problems(label, controls, window)
    return found


def _all_widget_problems(label: str, controls: list[Any], window: Any) -> list[str]:
    """逐控件的判据(焦点黑洞在这里建好, 量完销毁)."""
    sink = _focus_sink(window)
    try:
        return [
            problem
            for widget in controls
            for problem in _widget_problems(label, widget, sink)
        ]
    finally:
        sink.destroy()
        _settle(window)


def _dialog_problems(label: str, window: Any) -> list[str]:
    """对话框级别的三条: Esc、回车(要有主操作)、初始焦点.

    初始焦点量的是"窗口可见时定焦真会生效": 量的时候窗口已经被 ``_open`` 显示出来了,
    所以这里直接调一次 ``focus_first`` 再看 Tk 记下的最后焦点。
    """
    found: list[str] = []
    if not window.bind("<Escape>"):
        found.append(f"{label}: 没绑 <Escape>, Esc 关不掉")
    if not window.bind("<Return>"):
        found.append(f"{label}: 没绑 <Return>, 回车不会确认")
    if keyboard.primary_button(window) is None:
        found.append(f"{label}: 找不到主操作按钮, 回车按不动")
    target = keyboard.focus_first(window)
    # 定焦是排进 after_idle 的(那一刻窗口可能还没映射): 跑完事件循环再读 Tk 记下的焦点.
    _settle(window)
    last = window.focus_lastfor()
    if target is None:
        found.append(f"{label}: 没有可定焦的目标(既没有输入框也没有主按钮)")
    elif last is None or not str(last).startswith(f"{window}."):
        found.append(f"{label}: 窗口可见时定焦也没生效(最后焦点={last})")
    return found


def _settings_window(app: Any) -> Any:
    """直接构造设置窗口(它是**常驻窗口**, 判据与对话框不同, 因此单列一屏来量)."""
    from archive_management.ui.settings_window import SettingsWindow

    return SettingsWindow(
        app,
        palette=app.p,
        theme=app._theme,
        language=app._language,
        base_font_px=app._base_font_px,
        debug=app._debug,
        activation=app._activation,
        shortcuts=app._shortcuts,
        on_toggle_theme=app._on_toggle_theme,
        on_apply_language=app._on_language_change,
        on_apply_font_size=app._on_font_size_change,
        on_apply_debug=app._on_debug_change,
        on_apply_activation=app._on_activation_change,
        on_apply_shortcut=lambda _action, _accelerator: None,
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )


def _measure(app: Any, palette: Palette) -> list[str]:
    """驱动全部界面, 收集键盘可及性问题(一次跑完, 失败信息里按界面逐条列出)."""
    games = list(app.backend.list_games())
    seed = games[0]
    locations = (ImportLocationRow(index=0, text="D:\\Saves", default=""),)
    targets = tuple(
        ImportTargetOption(
            game_id=f"g{index}", label=f"游戏{index}", selected=index == 0
        )
        for index in range(3)
    )
    rows = tuple(
        BatchImportRow(
            entry=f"游戏{index}.archive.zip",
            name=f"游戏{index}",
            meta="Windows · 1234 · 3 备份",
            match_text=tr("dialog.import_match", name=f"游戏{index}"),
            locations=locations,
            strategies=import_strategies(has_targets=True),
            targets=targets,
            strategy="new",
            target_game_id=targets[0].game_id,
        )
        for index in range(3)
    )

    problems: list[str] = []
    problems.extend(_problems("主窗口", app, dialog=False))

    manage = ManageGameWindow(
        app,
        backend=app.backend,
        palette=palette,
        game_id=seed.game_id,
        name=seed.name,
        enabled=True,
        backup_location="D:\\Backups",
        archived=False,
        on_change=lambda: None,
    )
    _pump(app)
    problems.extend(_problems("游戏管理窗口", manage._window, dialog=True))
    manage.close()
    _pump(app)

    settings = _settings_window(app)
    _pump(app)
    problems.extend(_problems("设置窗口", settings._window, dialog=False))
    settings.close()
    _pump(app)

    cases: list[tuple[str, Callable[[], Any]]] = [
        (
            "确认框(危险)",
            lambda: dialogs.confirm_dialog(
                app,
                palette,
                title="删除",
                message="确定删除?",
                detail="D:\\x.zip",
                confirm_text="删除",
                danger=True,
            ),
        ),
        (
            "新增游戏(输入框)",
            lambda: dialogs.ask_text(
                app,
                palette,
                title=tr("dialog.add_game_title"),
                text=tr("dialog.add_game_prompt"),
                browse=lambda: None,
            ),
        ),
        (
            "编辑标签",
            lambda: dialogs.edit_tags_dialog(app, palette, tags=("标签一", "标签二")),
        ),
        (
            "导入归档包",
            lambda: dialogs.import_package_dialog(
                app,
                palette,
                title="导入",
                prompt=ImportPrompt(
                    summary="摘要",
                    match_text="疑似同一款",
                    locations=locations,
                    targets=targets,
                ),
                locations_label="存档位置",
                locations_hint="勾选",
                strategy_label="导入方式",
                strategies=import_strategies(has_targets=True),
                target_label="合并到",
                target_hint="提示",
                target_locked_hint="仅合并生效",
                confirm_text="导入",
            ),
        ),
        (
            "批量导出",
            lambda: dialogs.export_batch_dialog(
                app,
                palette,
                title="批量导出",
                prompt=export_batch_prompt(exportable_games(tuple(games))),
                filter_label="筛选",
                list_label="勾选要导出的游戏",
                no_match_text="没有匹配",
                select_all_label="全选",
                select_all_scope="当前筛选",
                confirm_text="导出选中",
            ),
        ),
        (
            "批量导入",
            lambda: dialogs.batch_import_dialog(
                app,
                palette,
                title="批量导入",
                prompt=BatchImportPrompt(
                    summary="3 款游戏", hint="逐游戏选择", rows=rows
                ),
                locations_label="存档位置",
                locations_hint="勾选",
                strategy_label="导入方式",
                target_label="合并到",
                confirm_text="导入",
            ),
        ),
    ]
    for label, call in cases:
        window = _open(app, call)
        if window is None:
            problems.append(f"{label}: 对话框没开出来(夹具失效)")
            continue
        problems.extend(_problems(label, window, dialog=True))
        window.destroy()
        _pump(app)
    return problems


def test_every_screen_is_reachable_by_keyboard(app: Any) -> None:
    """纯键盘走一遍全部界面: Tab 到得了、焦点看得见、Esc/回车接得上."""
    palette = DARK
    _UNMEASURED.clear()
    problems = _measure(app, palette)
    hint = "键盘可及性问题:\n" + "\n".join(problems)
    if _UNMEASURED:  # pragma: no cover - 只在环境推不上焦点/界面忙时才会走到
        hint += "\n\n没量到的控件(不算问题, 但要知道):\n" + "\n".join(_UNMEASURED)
    assert not problems, hint


def _focus_inside(app: Any, window: Any) -> bool:
    """当前焦点是不是落在 ``window`` 这个对话框里(焦点窗口本身也算)."""
    target = app.focus_get()
    return target is not None and str(target).startswith(str(window))


def _key_target(app: Any, window: Any) -> Any:
    """按键该送给哪个控件: 这个对话框的**焦点控件**.

    Tk 会把"送给子控件的合成键盘事件"转到**焦点窗口**上。焦点不在这个对话框里时, 连"送给
    对话框本身的 `<Return>`"也会被转给那个真正的焦点窗口 —— 对话框的接线一个都不响
    (2026-09-30 实测: 焦点被主窗口拿着时对话框返回 False, 正是 Linux CI 上那条
    `(False, False)`: `按 Esc/回车都没反应`)。

    所以分两步走:

    * 焦点已经在这个对话框里 —— 就量**当下**的那一个(用例要走的正是"用户打开对话框后直接
      按键"那条路, 不是先 ``focus_set`` 一个再看结果);
    * **焦点压根没落进对话框** —— 先按用户面对的事实把焦点交给它(无窗口管理器的 Xvfb 上
      没人会把输入焦点交给新窗口, 而用户眼里这个对话框就在最前面), 再重新量一次。

    两次都没落进去时把事件送给窗口本身: 它这时已经是焦点窗口, 而 `<Escape>` / `<Return>`
    正是接在窗口自己身上的。
    """
    if _focus_inside(app, window):
        return app.focus_get()
    with suppress(Exception):
        # 先 `focus_force`(无窗口管理器的 Xvfb 上只有它能真的把输入焦点交给这个窗口), 再跑一次
        # `update()`(本机上 CTk 那次重新定焦的 `after_idle` 要靠它才会跑, 实测光 force 不回
        # 焦点); 两步都要 —— 只留 update 在 CI 上不管用, 只留 force 在本机不管用。
        window.focus_force()
    app.update()
    if _focus_inside(app, window):
        return app.focus_get()
    return window


def _press_in_dialog(
    app: Any, key: str, call: Callable[[], Any], *, steal_focus: bool = False
) -> tuple[Any, Any]:
    """在替身 ``wait_window`` 里按下一个键, 返回(对话框返回值, 那个对话框窗口).

    按键前先把窗口真的映射出来(未映射的窗口收不到合成的键盘事件, 见 :func:`_settle`),
    并按 :func:`_key_target` 送到焦点控件上。

    ``steal_focus``: 先把焦点抢回主窗口 —— 那就是无窗口管理器的 Xvfb 上新对话框的处境
    (没人会把输入焦点交给它), 见那条反例用例。
    """
    opened: list[Any] = []

    def hook(window: Any, *_args: Any, **_kwargs: Any) -> None:
        # 顺序要紧: 先把窗口映射出来(未映射时不仅收不到按键, 焦点也还没定下来), 再量焦点.
        _settle(window)
        if steal_focus:
            # 把"焦点在别的窗口上"这个条件搬进用例(无窗口管理器的 Xvfb 上新对话框就是这样:
            # 没有任何东西会把输入焦点交给它)。只跑 idle 任务: 完整 `update()` 会把 CTk 排好的
            # 那次重新定焦回调一起跑掉, 这个状态就不存在了。
            app.focus_force()
            app.update_idletasks()
            assert not _focus_inside(app, window), (
                "前提: 把焦点交给主窗口后它不该还在对话框里(否则这条反例是空转)"
            )
        target = _key_target(app, window)
        opened.append(window)
        target.event_generate(key, when="now")

    app.wait_window = hook
    result = call()
    return result, (opened[0] if opened else None)


def test_escape_cancels_and_return_confirms(app: Any) -> None:
    """Esc = 取消、回车 = 确认: 两个键都要真的把对话框关掉并给出对应结果.

    Esc 那半边额外断言窗口**真的被销毁** —— 只看返回值的话,"键没接上"与"接上了并取消"
    都是 False(这条判据最容易退化成空断言, CI 上那次 Linux 红就是只有返回值能看)。
    """
    cancel, cancel_window = _press_in_dialog(
        app,
        "<Escape>",
        lambda: dialogs.confirm_dialog(app, DARK, title="t", message="m"),
    )
    confirm, _confirm_window = _press_in_dialog(
        app,
        "<Return>",
        lambda: dialogs.confirm_dialog(app, DARK, title="t", message="m"),
    )
    _pump(app)
    assert (cancel, confirm) == (False, True)
    assert cancel_window is not None, "对话框没开出来(夹具失效)"
    assert not cancel_window.winfo_exists(), "Esc 必须真的把对话框关掉"


def test_escape_and_return_reach_the_dialog_even_when_the_focus_is_elsewhere(
    app: Any,
) -> None:
    """反例: 焦点被别的窗口拿着时, Esc/回车**仍**要真的走到这个对话框的接线.

    2026-09-30 的 Linux CI 红的就是这一条: Xvfb 上没有窗口管理器, 没有任何东西会把输入焦点
    交给新开的对话框 —— `event_generate` 送出去的按键被 Tk 转给**真正的**焦点窗口, 对话框的
    Esc/回车一个都不响(用例看到的是 `(False, False)`)。本机(带窗口管理器)不会自然出现这个
    状态, 所以这里先把焦点抢到主窗口上, 把那个条件搬进用例: `_key_target` 会按"对话框此刻
    就在最前面"这个事实把焦点交回来, 然后把按键送到对话框上。
    """
    confirm, _confirm_window = _press_in_dialog(
        app,
        "<Return>",
        lambda: dialogs.confirm_dialog(app, DARK, title="t", message="m"),
        steal_focus=True,
    )
    cancel, cancel_window = _press_in_dialog(
        app,
        "<Escape>",
        lambda: dialogs.confirm_dialog(app, DARK, title="t", message="m"),
        steal_focus=True,
    )
    _pump(app)
    assert confirm is True, "焦点不在对话框里时回车丢了(合成事件被转给了别的焦点窗口)"
    assert cancel is False, "Esc 不该被当成确认"
    assert cancel_window is not None, "对话框没开出来(夹具失效)"
    assert not cancel_window.winfo_exists(), "焦点不在对话框里时 Esc 没把它关掉"


def test_return_inside_an_input_submits_the_dialog(app: Any) -> None:
    """焦点在输入框上时回车 = 确认(输入框自己的绑定优先, 且不重复提交)."""

    def hook(window: Any, *_args: Any, **_kwargs: Any) -> None:
        _settle(window)
        entry = next(
            widget
            for widget in keyboard.walk(window)
            if isinstance(widget, ctk.CTkEntry)
        )
        entry.insert(0, "新名字")
        _focus(window, entry)
        inner = keyboard.focus_target(entry)
        assert inner is not None
        inner.event_generate("<Return>", when="now")

    app.wait_window = hook
    result = dialogs.ask_text(app, DARK, title="t", text="请输入名字:")
    _pump(app)
    assert result == "新名字"


def test_focus_ring_stands_out_on_the_primary_button(app: Any) -> None:
    """焦点看得见: 主色实底按钮上的环**不能**也是主色(否则等于没有提示).

    这是 12 号实测反馈的核心: 焦点停在"立即备份"这类按钮上时, 描边原来是强调色、底色
    也是强调色, 分不出哪个控件被选中。现在按"与控件自己底色的对比度"取色。

    量具是**真焦点**("``event_generate("<FocusIn>")`` 不触发绑定"是实测结论, 见
    :func:`_ring_problems`), 并且额外断言"描边真的变了" —— 否则一个从来没画环的
    实现也能因为"原描边恰好不是 accent"而假绿。
    """
    window = _open(
        app,
        lambda: dialogs.confirm_dialog(app, DARK, title="t", message="m"),
    )
    assert window is not None
    button = keyboard.primary_button(window)
    assert isinstance(button, ctk.CTkButton)
    # 夹具必须造出"没聚焦时底色 = 主色": 对话框一打开就把焦点定在主按钮上了,
    # 那时它已经换成软底配色, 所以这里认的是**没聚焦时**的底色(见 resting_fill).
    assert keyboard.resting_fill(button) == DARK.accent
    inner = keyboard.focus_target(button)
    sink_widget = _focus_sink(window)
    sink = keyboard.focus_target(sink_widget)
    assert inner is not None
    assert sink is not None
    _defocus(window, sink_widget)  # 基准必须是"没聚焦"时的描边
    before = (str(button.cget("border_color")), int(button.cget("border_width")))
    assert str(button.cget("fg_color")) == DARK.accent, "基准状态没回到实底"

    inner.focus_force()
    _settle(window)
    ring = (str(button.cget("border_color")), int(button.cget("border_width")))
    ring_fill = str(button.cget("fg_color"))
    sink.focus_force()
    _settle(window)
    restored = (str(button.cget("border_color")), int(button.cget("border_width")))
    restored_fill = str(button.cget("fg_color"))
    window.destroy()
    _pump(app)

    assert ring[0] != before[0], "聚焦后描边根本没变(焦点环没画出来)"
    assert ring[0] != DARK.accent, "主色实底按钮的焦点环不能与底色同色"
    assert ring[0] == DARK.focus_ring, "实底按钮换色后应该用抢眼的那一档环色"
    assert ring_fill == DARK.accent_soft, "实底按钮聚焦时要换成对应的软底配色"
    assert contrast.contrast_ratio(ring[0], ring_fill) >= keyboard.FOCUS_RING_MINIMUM
    assert ring[1] == keyboard.FOCUS_RING_WIDTH
    assert restored == before, "失焦必须原样还原原描边"
    assert restored_fill == DARK.accent, "失焦必须把底色也还原"


def test_focus_ring_uses_the_dedicated_colour_on_plain_buttons(app: Any) -> None:
    """底子不是实底时用另一档专用环色(两档都是独立颜色, 不再复用语义色)."""
    window = _open(
        app,
        lambda: dialogs.confirm_dialog(app, DARK, title="t", message="m"),
    )
    assert window is not None
    cancel = next(
        widget
        for widget in keyboard.walk(window)
        if isinstance(widget, ctk.CTkButton)
        and getattr(widget, "_button_style", "") == "ghost"
    )
    inner = keyboard.focus_target(cancel)
    sink = keyboard.focus_target(_focus_sink(window))
    assert inner is not None
    assert sink is not None
    before = str(cancel.cget("border_color"))
    inner.focus_force()
    _settle(window)
    ring = str(cancel.cget("border_color"))
    fill = str(cancel.cget("fg_color"))
    sink.focus_force()
    _settle(window)
    restored = str(cancel.cget("border_color"))
    window.destroy()
    _pump(app)

    assert keyboard.ring_color(cancel) == DARK.focus_ring
    assert ring == DARK.focus_ring, f"底子是 {fill}, 环本该是 focus_ring"
    assert ring != before, "聚焦后描边根本没变(焦点环没画出来)"
    assert restored == before, "失焦必须把原描边还回来"


def test_escape_leaves_the_hotkey_recording(app: Any) -> None:
    """录制中按 Esc = 退出录制(**不是**关掉设置窗口), 且不写坏原有快捷键."""
    window = _settings_window(app)
    _settle(window._window)
    before = dict(window._shortcuts)
    assert window._window.bind("<Escape>") == "", "常驻窗口不该绑窗口级 Esc"

    window._toggle_capture(ACTION_SAVE_NOW)
    assert window._capturing is not None, "夹具没进录制态"
    window._on_key_press(SimpleNamespace(keysym="Escape"))
    changed = window._capturing
    exists = bool(window._window.winfo_exists())
    window.close()
    _pump(app)

    assert changed is None, "Esc 没有退出录制"
    assert exists, "Esc 把设置窗口关掉了"
    assert window._shortcuts == before
    assert window._error == ""


def test_only_dialogs_own_the_window_keys(app: Any) -> None:
    """G: 窗口级 Esc/回车只属于抓取式对话框, 常驻窗口一个都不许绑."""
    settings = _settings_window(app)
    _settle(settings._window)
    bound = {key: settings._window.bind(key) for key in ("<Escape>", "<Return>")}
    settings.close()
    _pump(app)
    assert bound == {"<Escape>": "", "<Return>": ""}

    dialog = _open(
        app,
        lambda: dialogs.confirm_dialog(app, DARK, title="t", message="m"),
    )
    assert dialog is not None
    both = (dialog.bind("<Escape>"), dialog.bind("<Return>"))
    dialog.destroy()
    _pump(app)
    assert all(both), "对话框必须仍然吃 Esc 与回车"


def test_space_presses_a_focused_button(app: Any) -> None:
    """空格按下焦点所在的按钮(Canvas 没有内建绑定, 这一条是补出来的)."""
    state: dict[str, int] = {"closed": 0}

    def hook(window: Any, *_args: Any, **_kwargs: Any) -> None:
        _settle(window)
        cancel = next(
            widget
            for widget in keyboard.walk(window)
            if isinstance(widget, ctk.CTkButton)
            and getattr(widget, "_button_style", "") == "ghost"
        )
        _focus(window, cancel)
        inner = keyboard.focus_target(cancel)
        assert inner is not None
        inner.event_generate("<space>", when="now")
        state["closed"] = 0 if window.winfo_exists() else 1

    app.wait_window = hook
    result = dialogs.confirm_dialog(app, DARK, title="t", message="m")
    _pump(app)
    assert (result, state["closed"]) == (False, 1)
