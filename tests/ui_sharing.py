"""方向② B2: 共享 UI 会话 —— 一份窗口服务一批用例, 用例之间靠"快照-识别-还原"隔离.

为什么值得做(2026-10-07 实测): 296 条 UI 用例占全套件约 27 分钟里的 25 分钟, 而
"建窗 + 首帧 + 销毁"一轮合计 1.6~1.9s —— 纯生命周期开销约 8 分钟, 还不含反复建/销
带来的 Tk 会话抖动(``gui_support`` 那套重试正是为它建的)。把"同一构造配置"的用例并
到**一份**窗口上: 用例开始前先对齐基线, 结束后识别漂移并还原 —— 复用的同时不把上一
条用例的污染漏给下一条。

三条纪律(全部有守卫, 见 ``tests/unit/test_ui_sharing.py``):

1. **还原走被测应用自己的动作**(``_show_page`` / ``_switch_view`` / ``_select_game``
   / ``_on_toggle_theme`` / ``_set_busy``): 不直接改写内部字段 —— 那会绕过重绘,
   还原出来的"干净"只是账面上的;
2. **还原后必须重新识别并验证**: 与基线不一致就**重建整窗**(销毁再建), 并把重建原因
   记进 Allure。验证不硬、硬扛着, 等于把漂移静默漏给下一条用例 —— 那比不复用更糟;
3. **后端被用例改过就重建**: 共享的是窗口, 不是数据。演示后端是内存态, 删游戏/导包/
   改调度/切主题都会改它; 只读接口拼出的状态签名(:func:`backend_signature`)对不上
   时, "还原数据"比"重建窗口"更贵也更险, 直接重建。

哪些用例**不**入池: 每条用例要不同构造参数的模块(尺寸/布局/生命周期/根窗口守卫)与会
重度改后端数据的大户(主页/导入导出)维持"每用例一窗"的原状 —— 它们复用不到, 硬塞只
会换来每次重建(比不复用还慢)。

池按**工厂函数身份**区分(:func:`SharedUiRegistry._pool_for`): 同一个工厂(如
:func:`demo_app`)全局一份窗口, 不同工厂(长文案替身那类)各自一份。共享一份窗口的模
块因此共用同一个后端实例 —— 这正是"全局共享一份 UI"的落点, 前提是它们断言的东西不
依赖各自的窗口标题或热键禁用原因(共享工厂里这些是统一值)。
"""

from __future__ import annotations

import contextlib
import os
import queue
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

import pytest

#: 状态快照里"这个应用根本没有该属性"的哨兵: 与"属性存在但值是 None"是两回事。
_UNSET = object()

#: 还原后把消息队列/延后任务跑完的轮数上限: 每轮 ``update_idletasks + update``,
#: 与各 GUI 模块自己的 ``_pump`` 同一件事。到顶还不干净就交给"验证不过 → 重建"。
_DRAIN_ROUNDS = 24


class RootGoneError(Exception):
    """共享窗口的根已经不在了(被用例销毁 / Tk 崩了): 只能重建, 不能假装还能还原."""


@dataclass(frozen=True)
class UiState:
    """一份可识别的界面状态(见 :func:`capture`).

    只收"用例会动、下一条用例会疼"的字段; 页面/视图/主题存**原对象**(枚举直接比
    较, 不猜字符串)。``backend`` 是后端只读签名 —— 它不参与"还原动作", 只参与
    "还原后还干不干净"的判定。
    """

    page: Any
    view: Any
    game_id: str | None
    home_selected: Any
    theme: Any
    busy: bool
    canceled: bool
    task_running: bool
    workspace_open: bool
    toplevels: tuple[str, ...]
    geometry: str | None
    backend: tuple[Any, ...] | None

    def drift(self, other: UiState) -> list[str]:
        """与另一份状态的差异(字段名 + 前后值); 空列表就是"没有漂移"."""
        fields = (
            "page",
            "view",
            "game_id",
            "home_selected",
            "theme",
            "busy",
            "canceled",
            "task_running",
            "workspace_open",
            "toplevels",
            "geometry",
        )
        return [
            f"{name}: {getattr(self, name)!r} → {getattr(other, name)!r}"
            for name in fields
            if getattr(self, name) != getattr(other, name)
        ] + ([] if self.backend == other.backend else ["backend: 数据被用例改过"])


def _attr(app: Any, name: str, default: Any = None) -> Any:
    """读应用的属性; 没有这个属性时给 ``default``(替身窗口/裸根没有全部字段)."""
    value = getattr(app, name, _UNSET)
    return default if value is _UNSET else value


def backend_signature(service: Any) -> tuple[Any, ...] | None:
    """后端数据的只读签名: 游戏清单 / 每款游戏的备份 / 调度 / 主题 / 用量.

    用 ``repr`` 拼接而不挑字段: 漏一个字段就会把"改了它"当成"没改", 而这里漏判的
    代价是把脏数据漏给下一条用例。接口不全(裸 CTk 根、单元测试替身)时返回 ``None``
    —— ``None`` 只和 ``None`` 相等, 刚好表达"这个池不以后端数据判漂移"。
    """
    list_games = getattr(service, "list_games", None)
    if not callable(list_games):
        return None
    try:
        games = tuple(repr(game) for game in list_games())
        backups = tuple(
            (
                getattr(game, "game_id", "?"),
                tuple(repr(item) for item in service.list_backups(game.game_id)),
            )
            for game in list_games()
        )
        schedules = tuple(sorted(repr(item) for item in service.list_schedules()))
        return (
            games,
            backups,
            schedules,
            service.current_theme(),
            service.storage_usage(),
        )
    except Exception:  # 接口不齐/读失败: 不猜, 交回"不判后端"那一档
        return None


def pump(app: Any, *, rounds: int = 2) -> None:
    """跑几轮事件循环(各 GUI 模块 ``_pump`` 的公共版, 共享池内部统一用它)."""
    idle = getattr(app, "update_idletasks", None)
    update = getattr(app, "update", None)
    for _ in range(rounds):
        with contextlib.suppress(Exception):
            if callable(idle):
                idle()
        with contextlib.suppress(Exception):
            if callable(update):
                update()


def drain(app: Any) -> None:
    """把上一条用例留下的"进行中收尾"跑完(有界): 消息队列清空且不再忙碌为止.

    队列里的消息要靠应用自己的 ``_poll_messages`` 在事件循环里消化 —— 直接
    ``get_nowait`` 丢掉等于把收尾步骤拦腰斩断, 所以这里只 pump、不掏队列。
    到 ``_DRAIN_ROUNDS`` 还不干净就停: 剩下的交给"验证不过 → 重建"兜底。
    """
    messages = getattr(app, "_messages", None)
    for _ in range(_DRAIN_ROUNDS):
        empty = not isinstance(messages, queue.Queue) or messages.empty()
        if empty and not _attr(app, "_busy", False):
            return
        pump(app, rounds=1)


def _safe_geometry(app: Any) -> str | None:
    """窗口几何(``WxH+X+Y``); 根都问不了的时候返回 ``None``(不猜)."""
    with contextlib.suppress(Exception):
        return str(app.winfo_geometry())
    return None


def capture(app: Any) -> UiState:
    """识别窗口当前状态; 根已经不在时抛 :class:`RootGoneError`(由池兜底重建)."""
    import gui_support

    if not gui_support.root_is_alive(app):
        raise RootGoneError("共享窗口的根不在了")
    toplevels: tuple[str, ...] = ()
    children = getattr(app, "winfo_children", None)
    if callable(children):
        with contextlib.suppress(Exception):
            import customtkinter as ctk

            toplevels = tuple(
                str(child) for child in children() if isinstance(child, ctk.CTkToplevel)
            )
    return UiState(
        page=_attr(app, "_page", None),
        view=_attr(app, "_view", None),
        game_id=_attr(app, "_game_id", None),
        home_selected=_attr(getattr(app, "_home_page", None), "_selected", ""),
        theme=_attr(app, "_theme", None),
        busy=bool(_attr(app, "_busy", False)),
        canceled=bool(_attr(app, "_canceled", False)),
        task_running=bool(_attr(app, "_task_running", False)),
        workspace_open=getattr(app, "_active_window", None) is not None,
        toplevels=toplevels,
        geometry=_safe_geometry(app),
        backend=backend_signature(getattr(app, "backend", None)),
    )


def _workspace_windows(app: Any) -> list[Any]:
    """还开着的附属窗口: 登记位上的工作区窗口 + 根下挂着的 CTkToplevel."""
    found: list[Any] = []
    active = getattr(app, "_active_window", None)
    if active is not None:
        found.append(active)
    children = getattr(app, "winfo_children", None)
    if callable(children):
        with contextlib.suppress(Exception):
            import customtkinter as ctk

            found.extend(
                child
                for child in list(children())
                if isinstance(child, ctk.CTkToplevel) and child not in found
            )
    return found


def _close_window(window: Any) -> None:
    """关一个附属窗口: 有 ``close()`` 就走它(会自己释放后台), 否则直接销毁."""
    close = getattr(window, "close", None)
    with contextlib.suppress(Exception):
        if callable(close):
            close()
            return
    with contextlib.suppress(Exception):
        window.destroy()


def restore(app: Any, target: UiState) -> list[str]:
    """把窗口还原到 ``target``, 返回做了哪些事(空列表 = 本来就在基线上).

    顺序有讲究: 附属窗口先关(它们的回调可能改其余全部状态) → 忙碌标志复位(经
    ``_set_busy`` 走重绘) → 主题(切换自带全量重绘, 放在页面切换前少刷一遍) →
    页面/视图/选中 → 队列排干 → 几何。每一步都走应用**自己的动作**; 各步拆在
    下面的私有函数里, 这里的顺序本身就是文档。
    """
    actions: list[str] = []
    _close_workspace_windows(app, actions)
    _reset_busy_flags(app, actions)
    _restore_theme(app, target, actions)
    _restore_navigation(app, target, actions)
    drain(app)
    _restore_geometry(app, target, actions)
    pump(app)
    return actions


def _close_workspace_windows(app: Any, actions: list[str]) -> None:
    """附属窗口先关: 它们的回调可能改其余全部状态, 必须排在最前."""
    for window in _workspace_windows(app):
        _close_window(window)
        actions.append(f"关掉附属窗口 {type(window).__name__}")


def _reset_busy_flags(app: Any, actions: list[str]) -> None:
    """忙碌态经 ``_set_busy`` 复位(走重绘); 其余任务标志是纯标志, 直接归零."""
    if _attr(app, "_busy", False):
        set_busy = getattr(app, "_set_busy", None)
        if callable(set_busy):
            set_busy(False)
            actions.append("复位忙碌态")
    with contextlib.suppress(Exception):
        app._canceled = False
        app._task_running = False


def _restore_theme(app: Any, target: UiState, actions: list[str]) -> None:
    """主题: 切换自带全量重绘, 放在页面切换前可以少刷一遍."""
    toggle = getattr(app, "_on_toggle_theme", None)
    for _ in range(2):  # 两套主题, 一步切回; 第三步还不对就是接口变了, 交给验证
        if _attr(app, "_theme", None) == target.theme or not callable(toggle):
            return
        with contextlib.suppress(Exception):
            toggle()
            actions.append(f"切回基线主题 {target.theme!r}")


def _restore_navigation(app: Any, target: UiState, actions: list[str]) -> None:
    """页面 → 视图 → 选中: 后一步依赖前一步把界面摆回来."""
    show_page = getattr(app, "_show_page", None)
    if callable(show_page) and _attr(app, "_page", None) != target.page:
        with contextlib.suppress(Exception):
            show_page(target.page)
            actions.append(f"切回页面 {target.page!r}")
    switch_view = getattr(app, "_switch_view", None)
    if callable(switch_view) and _attr(app, "_view", None) != target.view:
        with contextlib.suppress(Exception):
            switch_view(target.view)
            actions.append(f"切回视图 {target.view!r}")
    _restore_selection(app, target, actions)


def _restore_selection(app: Any, target: UiState, actions: list[str]) -> None:
    """选中: app 级(``_game_id`)与主页高亮(``_selected`)是**两本账** —— 新窗启动时
    高亮指着第一行而 app 级选中是空的, 用例里也可能只动其中一本。所以分开快照、
    各自还原, 不能拿一边去推另一边(否则会把"有高亮"的基线还原成"全空")。
    """
    if _attr(app, "_game_id", None) != target.game_id and target.game_id is not None:
        select_game = getattr(app, "_select_game", None)
        if callable(select_game):
            with contextlib.suppress(Exception):
                select_game(target.game_id)
                actions.append(f"还原选中游戏 {target.game_id!r}")
    home = getattr(app, "_home_page", None)
    home_select = getattr(home, "_select", None)
    if not callable(home_select):
        return
    if _attr(home, "_selected", None) != target.home_selected:
        with contextlib.suppress(Exception):
            home_select(target.home_selected)
            actions.append(f"还原主页高亮 {target.home_selected!r}")


def _restore_geometry(app: Any, target: UiState, actions: list[str]) -> None:
    """几何放最后: 前面的重绘可能触发布局, 布局之后再定的才是最终位置."""
    geometry = _safe_geometry(app)
    if target.geometry is None or geometry in (None, target.geometry):
        return
    with contextlib.suppress(Exception):
        app.geometry(target.geometry)
        actions.append(f"还原窗口几何 {target.geometry}")


def _record_rebuild(reason: str, teardown: Sequence[str]) -> None:
    """把"共享窗口被重建"记进 Allure(附件 + 参数) —— 静默重建等于把漂移藏起来."""
    import allure

    current = os.environ.get("PYTEST_CURRENT_TEST", "<不在用例里>")
    detail = (
        f"共享 UI 会话: 窗口已重建\n原因: {reason}\n用例: {current}\n"
        f"旧窗收尾: {list(teardown) or '<干净>'}"
    )
    allure.attach(
        detail, name="共享 UI: 窗口已重建", attachment_type=allure.attachment_type.TEXT
    )
    allure.dynamic.parameter("共享UI重建", reason)


class _Pool:
    """一个工厂对应的一份共享窗口(建/重建/基线管理; 生命周期纪律见模块文档)."""

    def __init__(self, factory: Callable[[], Any]) -> None:
        self.factory = factory
        self.app: Any = None
        self.baseline: UiState | None = None
        #: 重建原因账本(会话级守卫与报告读它; 空列表 = 一直没重建过)。
        self.rebuilds: list[str] = []

    def acquire(self) -> Any:
        """确保池里有活窗口: 没建过或根没了就(重新)建."""
        import gui_support

        if self.app is not None and gui_support.root_is_alive(self.app):
            return self.app
        reason = (
            "共享窗口的根已经不在(用例销毁了它, 或 Tk 崩了)"
            if self.app is not None
            else "首次创建共享窗口"
        )
        self._rebuild_or_create(reason)
        return self.app

    def _rebuild_or_create(self, reason: str) -> None:
        import gui_support

        teardown: list[str] = []
        if self.app is not None:
            # 先撤销"共享根"登记再销毁: 它马上要变成一扇要拆干净的旧窗, 收尾兜底得能管它。
            gui_support.unprotect_shared_root(self.app)
            teardown = gui_support.close_apps([self.app])
            # 收尾现场里可能带着"销毁链断在半路"的描述: 那是缺陷, 不能被重建吞掉 ——
            # 原样并进重建账本, Allure 附件里也看得见。
            self.rebuilds.append(f"{reason}; 旧窗收尾: {teardown or '干净'}")
            _record_rebuild(reason, teardown)
        self.app = gui_support.build_with_retry(self.factory)
        gui_support.protect_shared_root(self.app)
        pump(self.app)
        self.baseline = capture(self.app)

    def rebaseline(self) -> None:
        """把"当前状态"定为新基线(共态用例组摆好状态后调用, 见 group_scope)."""
        self.baseline = capture(self.app)


def _snapshot_attrs(app: Any, names: Sequence[str]) -> dict[str, Any]:
    """记下用例可能改写的实例属性(如 ``wait_window``); 没有的属性记 ``_UNSET``."""
    return {name: getattr(app, name, _UNSET) for name in names}


def _restore_attrs(app: Any, saved: dict[str, Any]) -> None:
    """按快照还原实例属性; 用例新加的属性(快照里是 ``_UNSET``)直接撤掉."""
    for name, value in saved.items():
        if value is _UNSET:
            with contextlib.suppress(Exception):
                delattr(app, name)
        else:
            with contextlib.suppress(Exception):
                setattr(app, name, value)


class SharedUiRegistry:
    """会话级共享窗口注册表(``ui_shared`` 夹具); 按工厂函数身份分池.

    用法(模块自己的 ``app`` 夹具)::

        @pytest.fixture
        def app(ui_shared: SharedUiRegistry) -> Iterator[Any]:
            with ui_shared.test_scope(
                demo_app, patched_attrs=("wait_window",)
            ) as application:
                _pump(application)
                application.wait_window = _hook   # 量模态框的钩子, 退出时自动还原
                yield application

    会话末尾 :meth:`close` 把池里所有窗口交给 :func:`gui_support.close_apps` ——
    与逐用例收尾同一条"保证拆干净"路径, 拆不干净同样判红。
    """

    def __init__(self) -> None:
        self._pools: dict[str, _Pool] = {}
        self.closed = False

    def _pool_for(self, factory: Callable[[], Any]) -> _Pool:
        key = getattr(factory, "__qualname__", None) or repr(factory)
        pool = self._pools.get(key)
        if pool is None:
            pool = _Pool(factory)
            self._pools[key] = pool
        return pool

    @contextlib.contextmanager
    def test_scope(
        self, factory: Callable[[], Any], *, patched_attrs: Sequence[str] = ()
    ) -> Iterator[Any]:
        """一条用例的共享窗口边界: 进入前对齐基线, 退出后识别漂移并还原/重建."""
        pool = self._pool_for(factory)
        app = pool.acquire()
        self._align(pool)
        saved = _snapshot_attrs(app, patched_attrs)
        try:
            yield app
        finally:
            _restore_attrs(app, saved)
            self._settle(pool)

    @contextlib.contextmanager
    def group_scope(self, factory: Callable[[], Any]) -> Iterator[_Pool]:
        """一组共态用例的统一前后置: 组前对齐基线 → 组夹具摆状态 → ``rebaseline``.

        组夹具在 ``yield`` 前把状态摆好并调用 ``pool.rebaseline()``, 组内每条用例的
        ``test_scope`` 就都以组状态为基线做快照/还原; 组结束时把基线与状态都还原回
        入组前 —— 池子回到"通用基线", 后面的普通用例不受组状态影响。
        """
        pool = self._pool_for(factory)
        pool.acquire()
        self._align(pool)
        original = pool.baseline
        assert original is not None  # acquire 一定建过基线
        yield pool
        with contextlib.suppress(Exception):
            restore(pool.app, original)
            pump(pool.app)
        pool.baseline = original

    def _align(self, pool: _Pool) -> None:
        """进入用例前保证站在基线上(上一条用例异常路径没走 settle 时的兜底)."""
        assert pool.baseline is not None
        try:
            current = capture(pool.app)
        except RootGoneError:
            pool._rebuild_or_create("进入用例时发现共享窗口已经没了")
            return
        if not current.drift(pool.baseline):
            return
        restore(pool.app, pool.baseline)
        pump(pool.app)
        try:
            after = capture(pool.app)
        except RootGoneError:
            pool._rebuild_or_create("进入用例时对齐基线把窗口弄没了")
            return
        remaining = after.drift(pool.baseline)
        if remaining:
            pool._rebuild_or_create("进入用例时状态还原不到位: " + "; ".join(remaining))

    def _settle(self, pool: _Pool) -> None:
        """用例结束: 识别状态 —— 后端被改过/还原不到位就重建, 否则原地还原到基线."""
        assert pool.baseline is not None
        try:
            after = capture(pool.app)
        except RootGoneError:
            # 根没了: 不在这里重建(把重建成本记到下一条真正要用窗口的用例头上),
            # 但必须看得见 —— rebuilds 账本留给会话末守卫与报告。
            pool.rebuilds.append("用例销毁了共享窗口(根没了; 下次取用时重建)")
            _record_rebuild("用例销毁了共享窗口(根没了; 下次取用时重建)", [])
            return
        baseline = pool.baseline
        if after.backend != baseline.backend:
            pool._rebuild_or_create(
                "用例改了共享后端的数据(游戏/备份/调度/主题), 不还原数据、直接重建"
            )
            return
        if not after.drift(baseline):
            return
        restore(pool.app, baseline)
        pump(pool.app)
        settled = capture(pool.app)
        remaining = settled.drift(baseline)
        if remaining:
            pool._rebuild_or_create("用例后状态还原不到位: " + "; ".join(remaining))

    def pools(self) -> tuple[_Pool, ...]:
        """现有各池(守卫/报告读: 重建账本、基线是否还在)."""
        return tuple(self._pools.values())

    def close(self) -> None:
        """会话收尾: 池里的窗口走 ``gui_support.close_apps`` 的同一条拆窗纪律."""
        import gui_support

        if self.closed:
            return
        self.closed = True
        # 会话收尾要真销毁这些窗: 先撤销"共享根"登记, 让 close_apps 的兜底能看见它们。
        for pool in self._pools.values():
            if pool.app is not None:
                gui_support.unprotect_shared_root(pool.app)
        # 线程判据换成"会话末的实存量": 池窗建在会话早期, 它建窗时的基线(常常是 0)没法
        # 代表整批用例跑完后的进程 —— 期间"只建真后端不建窗"的用例攒下的调度线程是存量
        # (gui_support.scheduler_thread_count 的说明), 用旧基线会把存量算到收尾头上。
        current = gui_support.scheduler_thread_count()
        for pool in self._pools.values():
            if pool.app is not None:
                pool.app._scheduler_baseline = current
        broken = gui_support.close_apps(
            [pool.app for pool in self._pools.values() if pool.app is not None]
        )
        if broken:
            pytest.fail("\n\n".join(broken), pytrace=False)


def demo_app() -> Any:
    """标准演示窗口(共享池的统一工厂): 演示后端 + 不注册系统快捷键.

    入池的模块共用这一份: 窗口标题与热键禁用原因在这里是**统一值** —— 断言依赖
    各自标题/原因的用例不要入池。Tk 相关导入放函数内: 单元测试在无头环境收集
    本模块时不应被迫装 ctk。
    """
    from archive_management.services.hotkeys import (
        GlobalHotkeyService,
        UnavailableBackend,
    )
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.main_window import ArchiveApp

    return ArchiveApp(
        DemoArchiveService(delay=0),
        title="共享 UI 会话",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("共享 UI 会话禁用")),
    )
