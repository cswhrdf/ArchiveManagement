"""共享 UI 会话的端到端守卫: 复用与隔离都要在**真窗口**上站得住.

单元层(``tests/unit/test_ui_sharing.py``)用替身钉住了逻辑分支; 这一层量的是
"真 Tk + 真 ``ArchiveApp`` + 真演示后端"上的三件事:

1. **复用**: 两条用例先后进入 ``test_scope``, 拿到的是同一个窗口对象(没重建);
2. **隔离**: 第一条把页面/视图/选中/忙碌全部拨乱后退出, 第二条进入时量到的
   状态与池的基线零差异 —— 漂移被还原掉了, 而不是被硬扛着;
3. **数据漂移走重建**: 切主题会改共享后端(``set_theme`` 落在后端上), 属于
   "共享的是窗口、不是数据"那一条 —— 旧窗收掉、工厂重建, 两条用例拿到的
   **不是**同一个对象, 且重建留了账。

模块内用例按文件顺序执行(pytest 默认), "同对象"断言依赖这一点; 单跑后一条时
它退化成只量"站在基线上", 仍然有效。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui_sharing import SharedUiRegistry, capture, demo_app

try:
    import customtkinter as ctk  # noqa: F401
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import AppPage, ViewKind

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("共享 UI 会话"),
    pytest.mark.layer("e2e"),
]

#: 上一条用例拿到的窗口(同对象断言用; 只在本模块文件顺序内才有意义)。
_LAST_APP: dict[str, Any] = {}


def _pump(app: Any, rounds: int = 6) -> None:
    """把事件跑完(布局/选中/重绘都要事件循环才生效)."""
    for _ in range(rounds):
        app.update_idletasks()
        app.update()


def _rebuild_factory() -> ArchiveApp:
    """独立小池的工厂: 切主题会改后端 → 用来量"数据漂移走重建"那一条."""
    return ArchiveApp(
        DemoArchiveService(delay=0),
        title="共享会话重建守卫",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("重建守卫禁用")),
    )


def test_scope_a_dirtying_the_window_still_settles_back(
    ui_shared: SharedUiRegistry,
) -> None:
    """第一条: 把状态面拨乱(不动后端数据), 退出边界让池还原."""
    with ui_shared.test_scope(demo_app) as app:
        _pump(app)
        games = app.backend.list_games()
        first = app._game_id or games[0].game_id
        other = next(g.game_id for g in games if g.game_id != first)
        app._open_game_detail(other)  # 页面 + 选中一起拨离基线
        _pump(app)
        app._switch_view(ViewKind.TIMELINE)
        app._set_busy(True)
        _pump(app)
        _LAST_APP["app"] = app


def test_scope_b_reuses_the_window_and_starts_at_baseline(
    ui_shared: SharedUiRegistry,
) -> None:
    """第二条: 同一个窗口对象, 且进入时的状态与池基线零差异."""
    with ui_shared.test_scope(demo_app) as app:
        _pump(app)
        if _LAST_APP.get("app") is not None:
            assert app is _LAST_APP["app"], "无数据漂移的路径不该重建窗口"
        pool = ui_shared._pool_for(demo_app)
        assert pool.baseline is not None
        drift = pool.baseline.drift(capture(app))
        assert not drift, "上一条用例的漂移漏给了这一条: " + "; ".join(drift)


def test_group_scope_holds_its_state_and_restores_afterwards(
    ui_shared: SharedUiRegistry,
) -> None:
    """共态组: 组状态在组内当基线、成员用例拨乱后还原到组状态, 组退出回通用基线."""
    with ui_shared.group_scope(demo_app) as pool:
        app = pool.app
        first = app.backend.list_games()[0].game_id
        app._open_game_detail(first)
        _pump(app)
        pool.rebaseline()
        with ui_shared.test_scope(demo_app) as member:
            assert member._page is AppPage.DETAIL, "组内应从组状态出发"
            member._show_page(AppPage.HOME)  # 成员拨乱: 离开组状态
            _pump(member)
        assert app._page is AppPage.DETAIL, "成员用例退出后应回到组基线"
    with ui_shared.test_scope(demo_app) as after:
        assert after._page is AppPage.HOME, "组退出后要回到通用基线(主页)"


def test_backend_drift_rebuilds_the_shared_window(
    ui_shared: SharedUiRegistry,
) -> None:
    """数据漂移: 切主题落在共享后端上 → 旧窗收掉重建, 且重建留了账."""
    with ui_shared.test_scope(_rebuild_factory) as before:
        _pump(before)
        before._on_toggle_theme()
        _pump(before)
    with ui_shared.test_scope(_rebuild_factory) as after:
        assert after is not before, "后端数据被改过必须重建, 不能继续共用"
    pool = ui_shared._pool_for(_rebuild_factory)
    assert pool.rebuilds, "重建必须留账(报告里要看得见)"
