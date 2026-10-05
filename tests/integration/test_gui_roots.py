"""窗口会话的守卫: 一个进程里"根"不能被留下, 也不能抢走别人的图片.

背景(2026-10-04, Linux 分片 0 的报告): ``test_icon_slot_survives_switching_and_deleting_games``
报

    _tkinter.TclError: image "pyimage1" does not exist

用 CI 留下的崩溃现场 dump 查出来: 出事时 ``tkinter._default_root`` 是**上一个用例里没被
拆掉的窗口**(一个 ``ArchiveApp``; 它的 ``_poll_job`` 已置空, 说明 ``destroy()`` 跑过,
但根窗口还在 —— 销毁链断在了中间)。两件事叠在一起才成了事故:

1. **窗口没拆干净** —— ``tkinter.Tk.destroy()`` 的子控件循环只要有一个子控件抛错就整条
   中断, 根窗口与 ``tkinter._default_root`` 一起留下(``close_gui_apps`` 只咽掉异常,
   于是完全静默);
2. **图片不带 master** —— ``CTkImage`` 贴图时用 ``ImageTk.PhotoImage(图片)``, 没有
   master 就落到默认根那个解释器里; 而标签属于新窗口的解释器, 贴上去自然是"图片不存在"。

本模块钉住这两条的修法: ``gui_support.close_gui_apps`` 收尾要**验证并补救, 并把这条用例
判红**(断裂是缺陷; 只记一笔会让"本机绿、CI 偶发红、且永远只看到受害者"一直演下去),
``rendering.host_image`` 让图片只属于要用它的那个窗口。
"""

from __future__ import annotations

import threading
import time
import tkinter
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gui_support import (
    close_gui_apps,
    forget_app,
    gui_app,
    root_is_alive,
    stray_roots,
    teardown_rescues,
)

try:
    from tkinter import TclError
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.domain import ArtworkKind
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.smoke,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("窗口会话与收尾"),
    pytest.mark.layer("e2e"),
]


def _new_app(backend: Any) -> ArchiveApp:
    """建一个主窗口: 不注册系统级快捷键(测试环境里没有可用的热键后端)."""
    return ArchiveApp(
        backend,
        title="窗口会话测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("窗口会话测试禁用")),
    )


def _default_root() -> object:
    """当前默认根(私有全局, 类型存根里没有它)."""
    return getattr(tkinter, "_default_root", None)


def _default_root_is_usable() -> bool:
    """默认根这个位置现在安全吗(空着, 或者指的是一个**能用**的根).

    判据刻意写成状态而不是"它就是 None": 同一进程里跑整批用例时, 这里可能已经有过
    别的根(例如前面某条用例重试留下的), 拿"必须是 None"当断言会让守卫的成败取决于
    运行顺序。那条不变式本身只关心一件事 —— 这个位置上不能挂着**已经不能用**的根,
    因为那正是后面每一条用例贴图报 `image "pyimageN" does not exist` 的原因。
    """
    root = _default_root()
    return root is None or root_is_alive(root)


def test_a_stray_root_cannot_steal_our_images(tmp_path: Path) -> None:
    """回归: 会话里还留着别的 Tk 根时, 界面里的图片仍要落在**自己**的解释器里.

    连建两个窗口(第一个故意不关, 它就是"上一个没拆干净的根"), 然后在第二个窗口里
    显示图标 —— 修之前这里抛 ``image "pyimage1" does not exist``, 正是 CI 上的症状。
    """
    icon = tmp_path / "icon.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(icon)

    class _IconService(DemoArchiveService):
        """星际拓荒有图标, 其余回落色块."""

        def artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
            """只有图标位给图."""
            if kind == "icon" and game_id == "outer-wilds":
                return str(icon)
            return ""

    stray = gui_app(_new_app, DemoArchiveService(delay=0))
    assert _default_root() is not None, "这条用例要的就是'会话里已经有一个根'"

    app = gui_app(_new_app, _IconService(delay=0))
    app.update_idletasks()
    app._open_game_detail("outer-wilds")
    app.update_idletasks()

    assert _default_root() is not app, (
        "新窗口不该顶掉已占位的默认根(否则这条用例什么都没验到)"
    )
    assert app._hero_tile.cget("image") is not None, (
        "图标没贴上: 图片跑到别的解释器里了"
    )
    assert root_is_alive(stray), "留着的那个根应当还在(它模拟的是没拆干净的窗口)"


def test_teardown_rescues_a_window_whose_destroy_breaks() -> None:
    """回归: 销毁链断在半路时, 收尾要把它拆干净、把默认根清掉, 并把用例**判红**.

    这是 CI 那次事故的**直接复现**: 让某个子控件的 ``destroy`` 抛 ``TclError`` ——
    ``Tk.destroy()`` 的循环就停在那里, 根窗口留在会话里。补救之后会话是干净的,
    但这条用例必须红: 断裂本身是缺陷, 只记一笔会让"本机绿、CI 偶尔红"一直演下去。
    """
    before = len(teardown_rescues())
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    app.update_idletasks()
    child = next(iter(app.winfo_children()))

    def boom() -> None:
        raise TclError(f'bad window path name "{child._w}"')

    child.destroy = boom
    with pytest.raises(pytest.fail.Exception, match="收尾时窗口没被真正销毁"):
        close_gui_apps()

    assert len(teardown_rescues()) == before + 1, "断裂必须在报告里看得见"
    assert root_is_alive(app) is False, "补救之后根窗口要真的没了"
    assert _default_root() is not app, (
        "刚拆掉的那个根不能还占着默认根这个位置(否则后面用例的图片会跑错解释器)"
    )
    assert _default_root_is_usable()


def test_a_healthy_teardown_leaves_no_root_and_no_rescue() -> None:
    """正常收尾: 不留根, 也不该产生"补救"记录(否则上面那条守卫就成了摆设)."""
    before = len(teardown_rescues())
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    app.update_idletasks()

    close_gui_apps()

    assert len(teardown_rescues()) == before, "正常销毁不该走补救路径"
    assert root_is_alive(app) is False
    assert _default_root() is not app, "正常销毁后它不该还是默认根"
    assert _default_root_is_usable(), "默认根这个位置上不能留着已经不能用的根"


def test_a_failed_window_build_does_not_leave_its_root_behind() -> None:
    """回归: 建窗口那一次失败(抖动)留下的根要收掉, 重试建起来的窗口重新拿回默认根.

    出处(2026-10-04, 只有**整批连跑**才暴露): ``Tk.__init__`` 一上来就把
    ``_default_root`` 指向自己, 而构造函数可能在后面某一步抛错(Tk 会话抖动) —— 那个
    对象再没有谁会去销毁它(``gui_app`` 拿到的是异常, 不是对象), 于是一直占着"默认根"
    这个位置。后果与 2026-10-04 那次事故一模一样: 之后每条用例建起来的窗口都**不是**
    默认根, 不带 master 的图片会落到那个已经不能用的解释器里。
    """
    built: list[Any] = []

    def flaky() -> Any:
        """第一次: 窗口已经建好, 但"抖动"发生在返回之前。"""
        app = _new_app(DemoArchiveService(delay=0))
        built.append(app)
        if len(built) == 1:
            raise TclError('invalid command name "tcl_findLibrary"')
        return app

    before = len(teardown_rescues())
    app = gui_app(flaky)

    assert len(built) == 2, "第一次必须被当成抖动重试"
    assert root_is_alive(built[0]) is False, "半成品根要收掉(它还占着解释器)"
    assert _default_root() is app, "重试建起来的窗口要重新拿到默认根这个位置"
    assert len(teardown_rescues()) == before, "它不是我们登记的窗口没拆干净, 不进补救账"


def test_a_child_that_is_already_gone_does_not_break_the_teardown() -> None:
    """回归: `children` 里留着一个"窗口已经没了"的控件时, 收尾不能断, 更不该判红.

    这是 2026-10-04 CI 上 Linux 那 4 条"收尾断裂"的**机制**: `Tk.destroy()` 是
    "先取快照, 再逐个 ``child.destroy()``"的一次性循环, 快照里的控件可能已经被前一次
    销毁**连带**带走 —— CustomTkinter 6.0.0 的 ``CTkScrollableFrame.destroy()`` 就会顺手
    拆掉自己的容器 ``_parent_frame``, 而那个容器是它在 ``children`` 里的**兄弟**。循环走到
    已经没了的那个时抛 ``TclError("can't delete Tcl command")``, 整条循环中断, 根窗口留在
    会话里(而它其实只是"没来得及拆", 补救一下就干净了)。

    这里直接把那个局面摆出来: 绕过 Python 的 ``destroy()``、只对 Tcl 下手把某个子控件的
    窗口拆掉, 于是它**留在 children 里但窗口已经没了**。收尾要先识别出这种条目(摘掉、跳过),
    再走窗口自己的 ``destroy()`` —— 既不该抛异常, 也不该把用例判红。
    """
    before = len(teardown_rescues())
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    app.update_idletasks()
    frame = next(iter(app.winfo_children()))
    path = frame._w
    frame.tk.call("destroy", path)  # 只拆窗口: Python 的账(children)不动

    close_gui_apps()

    assert len(teardown_rescues()) == before, "根没留下, 就不该进补救账(那是判红项)"
    assert root_is_alive(app) is False, "收尾必须把根拆掉"
    assert _default_root_is_usable()


def test_a_widget_whose_command_ledger_is_stale_is_repaired() -> None:
    """回归: 控件的 `_tclCommands` 里留着"已经删掉的命令"时, 收尾要修账并继续拆干净.

    这是 Linux 那 4 条"收尾断裂"报的另一个可能来源(报的就是 ``TclError: can't delete Tcl
    command``): 控件的账本(`_tclCommands`)与解释器里真实存在的命令**不一致**(命令先被删掉了,
    账上还留着名字), 于是它自己的 ``destroy()`` 会在 ``deletecommand`` 那一步抛错, 而
    ``Tk.destroy()`` 是"取快照 + 一次性循环", 一抛就把整条循环打断、根窗口留在会话里。

    这里直接把那个局面摆出来: 绑一条命令(会记账)之后**绕过 Python 把命令删掉**。收尾要么
    修好账继续拆, 要么把根留下 —— 后者会走补救账(判红), 所以这条用例本身就在盯这件事。
    """
    before = len(teardown_rescues())
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    app.update_idletasks()
    frame = next(iter(app.winfo_children()))
    frame.bind("<Button-1>", lambda _event: None)  # 注册命令 + 记账
    stale = str(frame._tclCommands[-1])
    frame.tk.deletecommand(stale)  # 命令没了, 账本还留着名字

    close_gui_apps()

    assert len(teardown_rescues()) == before, "修好账之后根不该被留下(那是判红项)"
    assert root_is_alive(app) is False, "收尾必须把根拆掉"
    assert stale not in (frame._tclCommands or []), "账上那条已经不存在的命令要划掉"


def test_a_foreign_root_left_in_the_session_gets_collected() -> None:
    """收尾的最后一道兜底: 别人的根占着默认根位置时, 收尾也要把它收掉.

    来源不止一种(重试留下的半成品、用例或第三方库在没有窗口时建控件/图片让 tkinter
    自己造出来的隐藏根), 所以收尾不只管自己登记的窗口 —— 收完之后, 默认根这个位置上
    不能留着**不能用的**根, 那正是"后面每条用例都贴不上图"的唯一原因。这条不做判红:
    收尾时无法把这个根归因给当前用例, 贸然判红会让整批连锁变红、把真正那条盖掉。

    窗口走 `gui_app` 建、再 `forget_app` 摘掉登记: 一是"别人建的根"本来就该绕开这套登记,
    二是**不能自己 `tkinter.Tk()`** —— 那会绕开重试, 撞上已知的 Tk 抖动
    (`Can't find a usable init.tcl`) 时当场红(实测本机 10 次里红 1 次)。
    """
    foreign = gui_app(_new_app, DemoArchiveService(delay=0))
    forget_app(foreign)

    close_gui_apps()  # 不抛异常即为通过(它是兜底, 不是判红项)

    # 这两条是**确定性**的, 也是真正重要的: 会伤到后面用例的是"默认根指着一个不能用的
    # 解释器", 而不是"那个解释器还在不在"。
    assert _default_root() is not foreign, "默认根这个位置不能还指着它"
    assert _default_root_is_usable()
    # 能不能真的把它拆掉是环境侧的事(收尾会试两次并把原因记进报告), 所以失败时把那份
    # 现场当消息抛出来, 而不是只给一句 assert。
    assert root_is_alive(foreign) is False, stray_roots()[-1]


def _poll_jobs(app: Any) -> list[str]:
    """Tcl 任务表里还挂着的**消息轮询**任务(回调名里带 ``_poll_messages``)."""
    pending = app.tk.call("after", "info")
    return [
        str(app.tk.call("after", "info", job))
        for job in pending
        if "_poll_messages" in str(app.tk.call("after", "info", job))
    ]


def test_a_manual_poll_does_not_orphan_the_next_poll() -> None:
    """手动调 ``_poll_messages`` 之后, 挂着的轮询任务必须仍然只有一个.

    出处(2026-10-05 报告): Linux 分片上 ``Default view is branch tree and hides old auto
    backups`` 的 stderr 附件里是 5 条 ``invalid command name "..._poll_messages"``, 而那条
    用例本身的断言全过了 —— 噪声来自**前面的**用例: 用例辅助(``_drain``)每轮都直接调
    ``app._poll_messages()`` 把后台结果搬回主线程, 而该方法末尾又排一个新任务并覆盖
    ``_poll_job``, 于是先前那个的 id 被丢掉、``destroy()`` 撤不到它; 它随后在别处的事件
    循环里爆掉, 挂在那条无辜用例名下。

    判据用 Tcl 自己的任务表(``after info``), 不看有没有异常: "还挂着一个"比"等它炸"可靠
    —— 爆的时机取决于事件循环什么时候跑, 而"多了一个"当场就能量到。
    """
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    app.update_idletasks()
    app.update()
    assert len(_poll_jobs(app)) == 1, (
        f"正常跑着的时候恰好一个轮询任务: {_poll_jobs(app)}"
    )
    for _ in range(3):
        app._poll_messages()  # 用例就是这么把后台结果搬回主线程的
        app.update()
    assert len(_poll_jobs(app)) == 1, (
        f"手动轮询之后挂着的轮询任务应当还是一个(多出来的是撤不掉的孤儿): "
        f"{_poll_jobs(app)}"
    )
    # 为什么不在这一层再量一次"stderr 里还有没有 invalid command name": 那种报错要等
    # **销毁之后**还有事件循环经过才会响, 而单条用例里量不到那一步(实测: 把产品侧修复撤掉,
    # 这条守卫仍然绿) —— 结构判据("任务表里多了一个")才是可靠的, 噪声本身只能靠整文件跑。


def test_the_pump_does_nothing_after_the_window_is_destroyed() -> None:
    """窗口销毁之后再轮到那两个延后任务时, 它们必须什么都不做.

    出处(2026-10-05 整文件日志里的那条 ``Exception in Tkinter callback``): 轮询任务**已经到点、
    被 Tk 取走**之后 ``destroy()`` 撤不掉它, 它随后跑到 ``_fit_task_name`` →
    ``can't invoke "winfo" command: application has been destroyed`` —— 同一类噪声, 挂在别人名下。

    判据是确定性的: 直接调那两个方法(销毁之后), 既不能抛, 也不能再排任务 —— 只靠"撤"是撤不掉
    已经派发出去的那一个的, 所以销毁时要先立牌子(``_destroyed``), 它们看到就返回。
    """
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    app.update_idletasks()
    app.update()
    close_gui_apps()

    app._poll_messages()
    app._refit_detail_names()

    assert app.__dict__.get("_destroyed") is True, "销毁时要立牌子"
    assert app.__dict__.get("_poll_job") is None, "销毁之后不该再排轮询任务"
    assert app.__dict__.get("_refit_job") is None, "销毁之后不该再排重裁任务"


def _scheduler_threads() -> list[str]:
    """活着的后台调度线程(``apscheduler`` 的调度线程名字里带 ``APScheduler``)."""
    return [t.name for t in threading.enumerate() if "APScheduler" in t.name]


def test_a_real_backend_does_not_outlive_its_window(tmp_path: Path) -> None:
    """回归: 真后端(带调度器)不能活过它的窗口 —— 那是 macOS 分片被整片带走的根子.

    出处(2026-10-05, PLAN §39.4): macOS 的崩溃现场里同时挂着约 30 个
    ``apscheduler..._main_loop`` 线程 —— ``BackgroundScheduler()`` 构造时就 ``start()``,
    但释放入口只有一个: 只有 ``_on_close()``(用户点关闭)会放掉它, 而界面用例的收尾走的是
    ``destroy()``。用例里有 40 多处是“建一个真后端 + 建窗口 + ``finally: app.destroy()``”,
    于是一个进程跑完整批用例就攒下几十个活线程, 而 Tk 解释器早被销毁 —— 从非主线程碰已销毁的
    Tk 是已知的段错误来源(它随后把整个分片带走, 那一批的结论与覆盖率一起丢, 只剩一份 dump)。

    **判据是相对的**(2026-10-05 首次上 CI 实测): 第一版写成"进程里一个都不许剩", 三个平台各红
    一次 —— 残留数分别是 **2 / 3 / 8**, 没有一次是 0。原因是同一个进程里还有别的用例建过真后端
    (它们只建后端、不建窗口, 也就没有释放入口), 那些存量不该由这条用例来背。所以先量一次基线,
    再要求"这条用例没有**多**攺下"—— 与 :func:`gui_support.close_gui_apps` 里那条守卫同一个
    口径。产品侧真退化了(关窗不再释放)仍然会红: 那会让计数超出基线。

    其余两条判据不变: 真后端建起来**必须多出**一个调度线程(否则这条量到的是空气);
    ``apscheduler`` 的线程名里带 ``APScheduler``, 不会与 pytest/uv/线程池的线程混淆。
    末尾再验一次幂等 —— 关窗与收尾本来就是两条路, 谁先走都行。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    db = Database(tmp_path / "guard.db")
    db.migrate()
    # 基线取在**建后端之前**: 调度线程是 ``ApschedulerBackend`` 构造时 ``start()`` 出来的
    # (窗口只是用它, 不新建), 收尾之后要回到这个数。顺带把"别人的存量"排在外面。
    baseline = len(_scheduler_threads())
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    # 故意不经 gui_app / close_gui_apps: 这条量的是**产品**的关窗路径。收尾那套是第二道防线,
    # 它会替产品把线程放掉 —— 走收尾的话, 产品侧退化了这条用例也看不出来。
    app = _new_app(service)
    try:
        app.update_idletasks()
        app.update()
        assert len(_scheduler_threads()) > baseline, (
            f"真后端建起来就该多出调度线程(否则这条守卫量到的是空气): 基线 {baseline}"
        )
    finally:
        app.destroy()

    deadline = time.monotonic() + 2.0
    while len(_scheduler_threads()) > baseline and time.monotonic() < deadline:
        time.sleep(0.02)  # 产品侧的 shutdown 是 wait=False, 收尾是异步的
    assert len(_scheduler_threads()) <= baseline, (
        f"关窗之后不该多出调度线程: 之前 {baseline} 个, 现在 {_scheduler_threads()}"
    )
    app._release_background()  # 幂等: 已经释放过再调一次不该抛
