"""共享 UI 会话(``tests/ui_sharing.py``)的守卫用例.

B2 的价值在"复用", 风险在"漏隔离"。这里用**替身窗口**(不建 Tk, 无头环境也能跑)
把三条纪律逐条钉住:

1. 还原必须走应用自己的动作(``_show_page``/``_switch_view``/…), 且把窗口真的
   送回基线 —— 漏一类状态, 后一条用例就吃到上一条的污染;
2. 还原后必须验证, 验证不过必须**重建**整窗(而不是硬扛着继续用);
3. 后端数据被用例改过必须重建 —— 共享的是窗口, 不是数据。

另钉三个生命周期事实: 池内跨用例复用**同一个**窗口对象、``patched_attrs`` 能把
用例改写的实例属性(如 ``wait_window``)还原、共态组(``group_scope``)摆的状态只
在组内当基线、组退出即还原。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

import gui_support
import ui_sharing
from ui_sharing import RootGoneError, UiState, backend_signature, capture, restore

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("共享 UI 会话"),
    pytest.mark.layer("unit"),
]


class _FakeTk:
    """让 ``gui_support.root_is_alive`` 认为根还活着(``winfo exists .`` 返回 1)."""

    def call(self, *_args: str) -> str:
        return "1"


class _FakeService:
    """演示后端的最小替身: 只读接口齐全, 数据可以按需改脏."""

    def __init__(self) -> None:
        self.games = [SimpleNamespace(game_id="g1", name="演示一")]
        self.backups: dict[str, list[str]] = {"g1": ["b1"]}
        self.theme = "dark"

    def list_games(self) -> list[Any]:
        return list(self.games)

    def list_backups(self, game_id: str) -> list[str]:
        return list(self.backups.get(game_id, []))

    def list_schedules(self) -> list[Any]:
        return []

    def current_theme(self) -> str:
        return self.theme

    def storage_usage(self) -> int:
        return 42


class _FakeHome:
    """主页替身: 只提供共享状态面用到的 ``_selected`` 与 ``_select``."""

    def __init__(self) -> None:
        self._selected = "g1"

    def _select(self, game_id: str) -> None:
        self._selected = game_id


class _FakeWindow:
    """附属窗口替身(登记位上的工作区窗口): ``close`` 被调用即视为收场.

    真窗口的 ``close()`` 会回调主窗口把登记位清空(``_active_window = None``), 替身
    必须带上这个行为 —— 否则还原后的验证会把"登记位还指着已关的窗口"判成漂移。
    """

    def __init__(self, app: Any = None) -> None:
        self.app = app
        self.closed = False

    def close(self) -> None:
        self.closed = True
        if self.app is not None:
            self.app._active_window = None


class FakeApp:
    """ArchiveApp 的最小替身: 走到哪个还原入口都记进 ``calls``."""

    def __init__(self, service: _FakeService | None = None) -> None:
        self.tk: Any = _FakeTk()  # 用例里可能被置成 None(模拟根没了)
        self.backend = service if service is not None else _FakeService()
        self._page: Any = "HOME"
        self._view: Any = "BRANCH"
        self._game_id: str | None = "g1"
        self._theme = "dark"
        self._busy = False
        self._canceled = False
        self._task_running = False
        self._active_window: Any = None
        self._home_page = _FakeHome()
        self.geometry_value = "980x680+0+0"
        self.wait_window = "原始 wait_window"
        self.calls: list[tuple[Any, ...]] = []

    # --- 状态面 ---------------------------------------------------------------
    def winfo_children(self) -> list[Any]:
        return []

    def winfo_geometry(self) -> str:
        return self.geometry_value

    # --- 还原会走的"应用自己的动作"(与 ArchiveApp 同名同义) --------------------
    def geometry(self, value: str) -> None:
        self.calls.append(("geometry", value))
        self.geometry_value = value

    def _show_page(self, page: Any) -> None:
        self.calls.append(("show_page", page))
        self._page = page

    def _switch_view(self, view: Any) -> None:
        self.calls.append(("switch_view", view))
        self._view = view

    def _select_game(self, game_id: str) -> None:
        self.calls.append(("select_game", game_id))
        self._game_id = game_id

    def _on_toggle_theme(self) -> str:
        self.calls.append(("toggle_theme",))
        self._theme = "light" if self._theme == "dark" else "dark"
        return self._theme

    def _set_busy(self, busy: bool) -> None:
        self.calls.append(("set_busy", busy))
        self._busy = busy

    def update_idletasks(self) -> None:  # pump 的落点
        self.calls.append(("update_idletasks",))

    def update(self) -> None:
        self.calls.append(("update",))

    # --- 测试里"弄脏"窗口用的直通口 --------------------------------------------
    def dirty_everything(self) -> None:
        """把状态面的每个字段都拨离基线(还原守卫用)."""
        self._page = "DETAIL"
        self._view = "TIMELINE"
        self._game_id = "g2"
        self._home_page._selected = ""
        self._theme = "light"
        self._busy = True
        self._canceled = True
        self._task_running = True
        self._active_window = _FakeWindow(self)
        self.geometry_value = "640x480+10+10"


def test_capture_reads_the_documented_surface() -> None:
    """识别: 状态面各字段来自应用与后端, 没有的字段落到安全的默认值."""
    app = FakeApp()
    app.dirty_everything()
    state = capture(app)
    assert state.page == "DETAIL"
    assert state.view == "TIMELINE"
    assert state.game_id == "g2"
    assert state.theme == "light"
    assert state.busy is True
    assert state.canceled is True
    assert state.task_running is True
    assert state.workspace_open is True
    assert state.toplevels == ()
    assert state.geometry == "640x480+10+10"
    assert state.backend is not None


def test_capture_raises_when_the_root_is_gone() -> None:
    """根没了必须响亮地抛 RootGone: 池靠它触发重建, 静默的假状态更危险."""
    app = FakeApp()
    app.tk = None
    with pytest.raises(RootGoneError):
        capture(app)


def test_drift_lists_every_changed_field() -> None:
    """差异清单要能指出"哪个字段从什么变成了什么", 否则红灯没法读."""
    clean = capture(FakeApp())
    dirty = capture(FakeApp())
    dirty = UiState(
        page="DETAIL",
        view=dirty.view,
        game_id=dirty.game_id,
        home_selected="",
        theme=dirty.theme,
        busy=True,
        canceled=dirty.canceled,
        task_running=dirty.task_running,
        workspace_open=dirty.workspace_open,
        toplevels=dirty.toplevels,
        geometry=dirty.geometry,
        backend=dirty.backend,
    )
    joined = " | ".join(clean.drift(dirty))
    assert "page" in joined
    assert "busy" in joined
    assert not clean.drift(clean), "同一份状态不该有差异"


def test_restore_walks_the_apps_own_actions_and_lands_on_baseline() -> None:
    """还原: 全部漂移都经应用自己的动作送回, 结束时与基线零差异."""
    app = FakeApp()
    baseline = capture(app)
    app.dirty_everything()
    window = app._active_window

    actions = restore(app, baseline)

    assert window.closed, "附属窗口必须先关(它的回调可能改其余全部状态)"
    walked = {call[0] for call in app.calls}
    assert {
        "set_busy",
        "toggle_theme",
        "show_page",
        "switch_view",
        "select_game",
    } <= walked
    assert ("set_busy", False) in app.calls
    assert not baseline.drift(capture(app)), "还原后仍有漂移: " + "; ".join(
        baseline.drift(capture(app))
    )
    assert actions, "做了还原动作却没记账"


def test_restore_on_a_clean_app_does_nothing() -> None:
    """本来就在基线上: 还原必须是空操作(复用的日常路径不能有无谓动作)."""
    app = FakeApp()
    baseline = capture(app)
    assert restore(app, baseline) == []
    assert app.calls == [("update_idletasks",), ("update",)] * 2, "只剩收尾 pump"


def test_backend_signature_detects_data_changes() -> None:
    """后端签名: 改游戏/备份/主题都算数据漂移; 接口不全的服务返回 None."""
    service = _FakeService()
    before = backend_signature(service)
    service.backups["g1"].append("b2")
    assert backend_signature(service) != before, "备份变了却没被发现"
    service.backups["g1"].pop()
    service.theme = "light"
    assert backend_signature(service) != before, "主题变了却没被发现"
    assert backend_signature(object()) is None, "不是归档后端就不参与判定"


@pytest.fixture
def pool_factory(monkeypatch: pytest.MonkeyPatch) -> tuple[list[FakeApp], Any]:
    """装好替身的池环境: 记录建窗次数, 关窗走空实现(替身没有真的 Tk)."""
    made: list[FakeApp] = []
    closed: list[list[Any]] = []
    rebuilds: list[str] = []

    def factory() -> FakeApp:
        app = FakeApp()
        made.append(app)
        return app

    monkeypatch.setattr(gui_support, "build_with_retry", lambda builder: builder())

    def _fake_close_apps(apps: list[Any]) -> list[str]:
        closed.append(list(apps))
        return []

    monkeypatch.setattr(gui_support, "close_apps", _fake_close_apps)
    monkeypatch.setattr(
        ui_sharing, "_record_rebuild", lambda reason, teardown: rebuilds.append(reason)
    )
    registry = ui_sharing.SharedUiRegistry()
    return made, SimpleNamespace(
        registry=registry, factory=factory, closed=closed, rebuilds=rebuilds
    )


def test_the_pool_reuses_one_window_across_scopes(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """复用: 两个用例边界拿到的是**同一个**窗口, 工厂只被叫一次."""
    made, env = pool_factory
    with env.registry.test_scope(env.factory) as first:
        pass
    with env.registry.test_scope(env.factory) as second:
        pass
    assert first is second, "两次边界应拿到同一个窗口对象"
    assert len(made) == 1, "同一工厂应全局一份窗口"
    assert not env.rebuilds, "干净路径不该重建"


def test_settle_restores_state_drift_between_scopes(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """隔离: 用例把状态面拨乱后退出, 下一条用例进入时已回到基线."""
    made, env = pool_factory
    with env.registry.test_scope(env.factory) as app:
        app.dirty_everything()
    assert made[0]._page == "HOME", "退出边界后没回到基线(页面)"
    assert made[0]._busy is False, "退出边界后没回到基线(忙碌)"


def test_settle_rebuilds_when_the_backend_data_was_changed(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """数据漂移: 用例改了共享后端 → 不还数据、直接重建(工厂被叫第二次)."""
    made, env = pool_factory
    with env.registry.test_scope(env.factory) as app:
        app.backend.backups["g1"].append("b2")
    assert len(made) == 2, "后端被改过必须重建窗口"
    assert made[0] in env.closed[0], "旧窗要走统一收尾路径"
    assert env.rebuilds, "重建必须留下账(不能静默)"


def test_settle_rebuilds_when_restore_cannot_land_on_baseline(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """验证不硬: 还原动作"走了但没落地"(接口坏了)时必须重建, 不能硬扛."""
    made, env = pool_factory
    with env.registry.test_scope(env.factory) as app:

        def broken_switch(view: Any) -> None:  # 记账但不落地
            app.calls.append(("switch_view", view))

        app._switch_view = broken_switch
        app._view = "TIMELINE"
    assert len(made) == 2, "还原不到位必须重建"


def test_the_pool_rebuilds_after_the_root_was_destroyed(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """根没了: 当条用例的收尾只记账, 下一次取用时重建出活窗口."""
    made, env = pool_factory
    with env.registry.test_scope(env.factory) as app:
        app.tk = None  # 用例把窗口销毁了
    assert len(made) == 1, "根没了当条收尾不该重建(留给下次取用)"
    assert env.rebuilds, "根没了必须先看得见(记账)"
    with env.registry.test_scope(env.factory) as fresh:
        assert fresh is made[1], "下一次取用要拿到重建后的窗口"


def test_patched_attrs_are_restored_and_added_ones_removed(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """实例属性还原: 用例换掉的 ``wait_window`` 回得去, 新加的属性被撤掉."""
    made, env = pool_factory
    with env.registry.test_scope(
        env.factory, patched_attrs=("wait_window", "extra_hook")
    ) as app:
        app.wait_window = "用例的量点钩子"
        app.extra_hook = "用例新加的"
    assert made[0].wait_window == "原始 wait_window"
    assert not hasattr(made[0], "extra_hook"), "用例新加的属性要撤掉"


def test_group_scope_rebaselines_and_restores_the_original(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """共态组: 组内以组状态为基线, 组退出后基线与状态都回到通用基线."""
    made, env = pool_factory
    with env.registry.group_scope(env.factory) as pool:
        pool.app._page = "DETAIL"  # 组夹具摆状态
        pool.rebaseline()
        with env.registry.test_scope(env.factory) as inner:
            assert inner._page == "DETAIL", "组内应从组状态出发"
            inner._page = "HOME"  # 组内用例拨乱
        assert pool.app._page == "DETAIL", "组内用例退出后应回到组基线"
    assert made[0]._page == "HOME", "组退出后要回到通用基线"


def test_registry_close_closes_every_pool_window(
    pool_factory: tuple[list[FakeApp], Any],
) -> None:
    """会话收尾: 每个池的窗口都交给统一的拆窗路径, 且 close 幂等."""
    made, env = pool_factory
    with env.registry.test_scope(env.factory):
        pass

    def other_factory() -> FakeApp:
        return FakeApp()

    monkeypatch_close_args: list[list[Any]] = env.closed
    with env.registry.test_scope(other_factory):
        pass
    env.registry.close()
    env.registry.close()  # 幂等: 二次调用不许再拆
    assert len(monkeypatch_close_args) == 1, (
        f"收尾应只发生一次: {monkeypatch_close_args}"
    )
    assert made[0] in monkeypatch_close_args[0], "demo 池的窗口要走统一收尾"
    assert len(monkeypatch_close_args[0]) == 2, "两个池的窗口都要收"
