"""
窗口生命周期与兜底守卫: 轮询抗故障、窗口复用、工作区导航、激活页、冒烟启动与配置状态报告。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

from gui_support import gui_app

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter as ctk  # 既校验可导入, 也用于构造测试用父容器
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

import archive_management.ui.main_window as main_mod
from archive_management.i18n import tr
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import (
    FeedbackKind,
)
from button_support import (
    _button_texts,
    _close_toplevels,
    _drain,
    _feedback_kind,
    _label_texts,
    _new_app,
    _patch_dialogs,
    _pump,
    _wait_for,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.smoke,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("按钮端到端操作"),
    pytest.mark.layer("e2e"),
]

# 后台操作等待上限: 恢复/备份会真读写文件, 覆盖率与 CI 环境下会明显变慢.


def test_switch_game_view_and_filter_do_not_crash() -> None:
    """重复切换游戏/视图/筛选不触发旧控件重绘崩溃(回归 UiKit 生命周期)."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import ViewKind

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    for _index in range(3):
        app._select_game("outer-wilds")
        app._select_game("shanhai")
        app._switch_view(ViewKind.BRANCH)
        app._switch_view(ViewKind.TIMELINE)
        app._on_filter_change("x")
        app._on_toggle_theme()
        _pump(app)
    assert app._game_id == "shanhai"
    assert app._view == ViewKind.TIMELINE


def test_empty_database_polling_does_not_crash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归: 空库(首次启动)时轮询任务状态 + 重载列表不得报错."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "empty.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        assert app._game_id is None
        for _index in range(5):
            app._poll_messages()
            _pump(app)
        assert app._items == []
    finally:
        app.destroy()


def test_window_build_survives_a_transient_database_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归: 数据库瞬时读失败(CI 上出现过 OperationalError: unsupported file format)
    时窗口仍能构造; 故障过去后重新加载能自行恢复.

    读取失败发生在构造过程中(游戏发现面板会立即读监控目录), 让异常冒出去就等于
    整个窗口起不来 —— 而这类失败通常是瞬时的。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.repository import (
        MonitoredDirectoryRepository,
    )
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "flaky.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    saves = tmp_path / "saves"
    saves.mkdir()
    service.add_monitored_directory(str(saves))

    original = MonitoredDirectoryRepository.list_all
    state: dict[str, Any] = {"broken": True, "calls": 0}

    def flaky(self: MonitoredDirectoryRepository) -> list[Any]:
        state["calls"] += 1
        if state["broken"]:
            raise sqlite3.OperationalError("unsupported file format")
        return original(self)

    monkeypatch.setattr(MonitoredDirectoryRepository, "list_all", flaky)
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        # 故障期间: 面板空着, 但既没抛异常也没弹出模态框。
        assert state["calls"] >= 1
        assert app._home_page._discovery._dirs == []
        # 故障过去后重新加载: 数据回来, 不需要重建窗口。
        state["broken"] = False
        app._home_page._discovery.reload()
        assert [item.path for item in app._home_page._discovery._dirs] == [str(saves)]
    finally:
        app.destroy()


def test_polling_survives_a_transient_database_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归: 轮询时数据库报错不得让窗口崩掉(记日志 + 状态栏提示即可).

    轮询**每 5 拍才问一次后端**(见 ``_refresh_task`` 的节流), 而"构造期已经拍了几下"由时序
    决定 —— 不把计数摆好的话, 这几次轮询可能全落在节流里, 后端一次都没被问到, 而断言靠构造
    期那次失败照样绿(2026-10-10 的覆盖率报告: 失败分支只在 Windows 上被覆盖到)。所以这里
    先把计数推到窗口上, 并断言轮询期间真的问过后端。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "poll.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    calls = {"n": 0}

    def broken(_game_id: str | None) -> Any:
        calls["n"] += 1
        raise sqlite3.OperationalError("unsupported file format")

    monkeypatch.setattr(service, "task_status", broken)
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        asked_before = calls["n"]
        app._task_ticks = 4  # 下一拍正好落在节流窗口上
        for _index in range(3):
            app._poll_messages()
            _pump(app)
        assert calls["n"] > asked_before, "轮询没真的问过后端: 这条用例没走到失败分支"
        assert _feedback_kind(app) == FeedbackKind.ERROR
        assert "unsupported file format" in app._last_feedback[1]
    finally:
        app.destroy()


def test_main_window_keeps_the_last_picture_when_reading_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读取失败时保留上一次的画面并把原因写进反馈区, 而不是把界面清空."""
    from archive_management.exceptions import ArchiveManagementError
    from archive_management.ui.demo_backend import DemoArchiveService

    class _FailingHome(DemoArchiveService):
        """可切换"读取失败"的演示后端."""

        def __init__(self) -> None:
            """建好演示数据, 并先让读取正常工作."""
            super().__init__(delay=0)
            self.fail = False

        def load_home(self) -> Any:
            """按开关决定这次读取是成功还是失败."""
            if self.fail:
                raise ArchiveManagementError("演示故障: 读取数据失败")
            return super().load_home()

    _patch_dialogs(monkeypatch)
    backend = _FailingHome()
    app = gui_app(_new_app, backend)
    _pump(app)
    page = app._home_page
    before = page._board
    assert before is not None
    assert before.games, "演示数据里应当有游戏, 否则这条用例证明不了『保留画面』"

    backend.fail = True
    page.reload()  # 主页自己兜住异常: 不该抛到 Tk 回调之外
    _pump(app)

    assert page._board is before, "读取失败不该把已有画面清掉"
    # 原因要落在主页自己的提示区(而不是只写日志)。
    assert "演示故障" in page._summary_label.cget("text")


def test_detail_actions_without_a_selection_are_blocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有选中的游戏/备份时, 详情页的动作只给提示: 不起后台任务、不弹对话框."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._game = None
    app._backup_id = None

    app._on_backup()
    app._on_cancel()
    app._on_restore()
    app._on_branch()
    app._on_rename_backup()
    app._on_delete_backup()
    app._on_export()
    app._on_game_settings()
    _pump(app)

    assert app._busy is False, "被拦下的动作不该进入忙碌态"
    assert app._active_window is None, "被拦下的动作不该开出子窗口"


def test_activation_reports_cover_every_reason() -> None:
    """自动启停的状态栏提示按原因分支: 接管/停用/回落/无变化各有各的文案."""
    from dataclasses import replace

    from archive_management.domain import (
        REASON_DISABLED,
        REASON_ENABLED,
        REASON_FALLBACK,
        REASON_OFF,
    )
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    base = app.backend.poll_activation(enabled=False)
    ours = app.backend.get_detail("outer-wilds")
    theirs = app.backend.get_detail("shanhai")

    # ① 没有变化(enabled/disabled 都为空): 一个提示都不给, 保持上一次的反馈。
    app._feedback(FeedbackKind.INFO, "占位")
    app._report_activation(replace(base, reason=REASON_OFF))
    assert app._last_feedback[1] == "占位"

    # ② 接管: 成功级提示带上被启用的游戏名。
    app._report_activation(
        replace(base, reason=REASON_ENABLED, enabled=ours, disabled=None)
    )
    assert "星际拓荒" in app._last_feedback[1]

    # ③ 收回启用态: 提示带上被停用的游戏名。
    app._report_activation(
        replace(base, reason=REASON_DISABLED, enabled=None, disabled=ours)
    )
    assert "星际拓荒" in app._last_feedback[1]

    # ④ 回落: 同时说清"谁退了"与"切到了谁"。
    app._report_activation(
        replace(base, reason=REASON_FALLBACK, enabled=ours, disabled=theirs)
    )
    assert "山海旅人" in app._last_feedback[1]
    assert "星际拓荒" in app._last_feedback[1]

    # ⑤ 回落但没记下被接管的那一款: 仍然要能出文案(只在有 enabled 时才走这条)。
    app._feedback(FeedbackKind.INFO, "占位")
    app._report_activation(
        replace(base, reason=REASON_FALLBACK, enabled=None, disabled=theirs)
    )
    assert app._last_feedback[1] == "占位", "回落里没有接管对象时不该乱报"


def test_standalone_home_page_without_callbacks_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主页可以单独构造(不接任何回调): 动作只在自己内部处理, 不去调用空回调."""
    import customtkinter as ctk

    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.home_page import HomePage
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = HomePage(
        ctk.CTkFrame(app), backend=app.backend, palette=Palette.for_theme(app._theme)
    )

    # ① 构造时就会加载一次; 刷分页与刷新启停(两个入参都为 None)都不该崩。
    assert page._board is not None
    page._update_pager()
    page.refresh_activation()
    page.refresh_activation(enabled=True)
    page.refresh_activation(enabled=False)
    # ② 没有 on_open_detail 回调时点"打开详情"。
    page._on_detail()

    page.reload()
    page._select("outer-wilds")
    # ③ 没有 on_change 回调时归档/改标签。
    page._on_archive()
    page._on_edit_tags()
    # ④ 搜索到没有匹配项: 列表重绘要能处理空结果。
    page._search_entry.insert(0, "绝对搜不到的名字")
    page._submit_search()
    _pump(app)

    assert page._board is not None
    assert page._board.games == ()


def test_home_refresh_activation_covers_both_arguments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启停分页的刷新通路: 只给 outcome、只给 enabled、两者都给都要能用."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    outcome = app.backend.poll_activation(enabled=False)

    page.refresh_activation()
    page.refresh_activation(outcome)
    page.refresh_activation(enabled=True)
    page.refresh_activation(outcome, enabled=False)
    _pump(app)

    assert page._activation_enabled is False
    assert page._last_activation is outcome


def test_detail_flows_survive_cancelled_or_blocked_dialogs(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """详情页的恢复/分支/改名/删除在被取消、被拒绝、输入为空时都保留原状."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(
        monkeypatch,
        restore_result=None,  # 恢复选项对话框被取消
        confirm=False,  # 所有确认框都选"否"
        ask_text="",  # 新增游戏时名字为空
        branch_name="",  # 分支名为空
        edit_result=None,  # 备份改名对话框被取消
        export_path=str(tmp_path / "outer-wilds.archive.zip"),
    )
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._select_game("outer-wilds")
    _pump(app)
    backups = list(app.backend.list_backups("outer-wilds"))
    app._select_backup(backups[-1])
    _pump(app)

    # ① 恢复: 选项对话框取消 → 不进忙碌态、备份列表不变。
    app._on_restore()
    assert app._busy is False
    # ② 分支: 名字为空 → 不产生新节点。
    app._on_branch()
    assert len(app.backend.list_backups("outer-wilds")) == len(backups)
    # ③ 改名: 对话框取消 → 标题不变。
    app._on_rename_backup()
    assert app.backend.list_backups("outer-wilds")[-1].title == backups[-1].title
    # ④ 删除**需要确认的节点**(分支根, 会连带删掉整条分支): 这里选"否" → 全都留着。
    needing = [
        item
        for item in backups
        if app.backend.plan_delete("outer-wilds", item.backup_id).needs_confirmation
    ]
    assert needing, "演示数据里应当有一个需要确认才能删的节点(分支根)"
    app._select_backup(needing[0])
    _pump(app)
    app._on_delete_backup()
    _pump(app)
    assert len(app.backend.list_backups("outer-wilds")) == len(backups)
    assert tr("action.delete_canceled") in app._last_feedback[1]
    # ⑤ 删除同一线路上的节点: 不需要确认, 直接删掉(后继节点上移)。
    plain = next(
        item
        for item in backups
        if not app.backend.plan_delete("outer-wilds", item.backup_id).needs_confirmation
    )
    app._select_backup(plain)
    app._select_backup(plain)
    _pump(app)
    app._on_delete_backup()
    _drain(app)
    remaining = app.backend.list_backups("outer-wilds")
    assert len(remaining) == len(backups) - 1
    # ⑥ 新增游戏: 名字为空 → 不新增。
    games_before = len(app.backend.list_games())
    app._on_add_game()
    assert len(app.backend.list_games()) == games_before
    # ⑦ 导出: 保存位置已选好 → 走后台线程(这里只要求能跑完且退回不忙碌)。
    app._on_export()
    _drain(app)
    assert app._busy is False
    # ⑧ 快捷键路径(quick=True): 按文档"不弹窗, 直接用默认分支名"建一条。
    #    删除成功后选中项会被清空, 因此先重新选一个节点(这也是"没有选中就不动作"的那条守卫)。
    app._on_branch(quick=True)
    assert len(app.backend.list_backups("outer-wilds")) == len(remaining), (
        "没有选中节点时不该建分支"
    )
    app._select_backup(app.backend.list_backups("outer-wilds")[-1])
    _pump(app)
    app._on_branch(quick=True)
    _pump(app)
    assert len(app.backend.list_backups("outer-wilds")) == len(remaining) + 1


def test_settings_and_schedule_windows_are_reused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个配置窗口共用同一个位置: 重复点只提前台, 换入口则提示先关掉当前的."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)

    app._on_open_settings()
    _pump(app)
    opened = app._active_window
    assert opened is not None
    # ① 再点同一个入口: 复用同一个窗口, 不新建。
    app._on_open_settings()
    _pump(app)
    assert app._active_window is opened
    # ② 点另一个入口: 提示先关闭当前窗口, 不新建。
    app._on_open_schedules()
    _pump(app)
    assert app._active_window is opened
    assert app._last_feedback[1] != ""

    # ③ 关掉之后两个入口都能再打开。
    opened.close()
    _pump(app)
    app._on_open_schedules()
    _pump(app)
    assert app._active_window is not None
    app._active_window.close()
    _pump(app)


def test_workspace_nav_keeps_a_single_window_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个窗口入口共用一个窗口位置: 不会重复开窗(游戏主页是页面, 不开窗)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    class _FakeWindow:
        """记录聚焦次数、可模拟"仍打开/已关闭"的窗口替身."""

        def __init__(self, **kwargs: Any) -> None:
            """保存构造参数便于断言."""
            self.kwargs = kwargs
            self.focus_calls = 0
            self.alive = True

        def focus(self) -> bool:
            """累加聚焦次数; 返回窗口是否仍然打开."""
            self.focus_calls += 1
            return self.alive

    opened: list[_FakeWindow] = []

    def _factory(_parent: Any, **kwargs: Any) -> _FakeWindow:
        window = _FakeWindow(**kwargs)
        opened.append(window)
        return window

    _patch_dialogs(monkeypatch)
    monkeypatch.setattr(main_mod, "ScheduleWindow", _factory)
    monkeypatch.setattr(main_mod, "SettingsWindow", _factory)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        # "定时任务"与"设置"共用同一个窗口位置: 不会重复开窗.
        app._on_open_schedules()
        assert len(opened) == 1
        # 同一入口再点: 只把已有窗口提到前台.
        app._on_open_schedules()
        assert len(opened) == 1
        assert opened[0].focus_calls == 1
        # 另一个入口: 仍然只有一个窗口, 并提示先关闭当前窗口.
        app._on_open_settings()
        assert len(opened) == 1
        assert opened[0].focus_calls == 2
        assert app._last_feedback[1] == tr("topbar.busy")

        # 用户关闭当前窗口后即可打开另一个入口.
        opened[0].alive = False
        app._on_open_settings()
        assert len(opened) == 2
        assert opened[1].focus_calls == 0
        app._on_open_schedules()
        assert len(opened) == 2
        assert opened[1].focus_calls == 1
    finally:
        app.destroy()


def test_topbar_carries_global_entries_and_no_sidebar() -> None:
    """顶栏右侧是全局入口(添加游戏/导入归档包/定时任务/设置); 侧边栏与其内容已移除."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)

    assert not hasattr(app, "_sync_label")
    assert not hasattr(app, "theme_btn")
    assert not hasattr(app, "_sync_labels")
    # 全局入口都在顶栏: 添加游戏 + 导入归档包 + 定时任务 + 设置.
    assert app._add_game_btn.cget("text") == tr("topbar.add_game")
    assert app._import_btn.cget("text") == tr("topbar.import_package")
    assert app._schedule_btn.cget("text") == tr("topbar.nav_scheduled")
    assert app._settings_btn.cget("text") == tr("topbar.nav_settings")
    topbar_texts = _button_texts(app._add_game_btn.master)
    assert tr("topbar.import_package") in topbar_texts
    assert tr("topbar.nav_settings") in topbar_texts
    # 侧边栏的"我的游戏"列表、工作区标题与"全部备份"入口都不在了.
    assert not hasattr(app, "_games_container")
    assert not hasattr(app, "_status_card")
    assert not hasattr(app, "_add_game_label")
    assert tr("sidebar.my_games") not in _label_texts(app)
    assert "全部备份" not in _label_texts(app)
    # 备份服务状态挪到右下角的状态条里.
    assert tr("status.service_ok") in app._service_label.cget("text")


def test_add_game_flow_selects_new_game(monkeypatch: pytest.MonkeyPatch) -> None:
    """新增游戏按钮按名称建游戏并选中."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import FeedbackKind

    _patch_dialogs(monkeypatch, ask_text="新按钮游戏")
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    before = {game.name for game in app.backend.list_games()}
    app._on_add_game()
    _pump(app)
    names = {game.name for game in app.backend.list_games()}
    assert "新按钮游戏" in names - before
    selected = app._game
    assert selected is not None
    assert selected.name == "新按钮游戏"
    # 新游戏无位置时状态栏为 info 提示, 但绝不应是错误
    assert app._last_feedback[0] != FeedbackKind.ERROR


def _button_named(widget: Any, text: str) -> list[Any]:
    """递归找出文案匹配的按钮(便于检查与点击某一行的动作)."""
    found: list[Any] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton) and str(child.cget("text")) == text:
            found.append(child)
        found.extend(_button_named(child, text))
    return found


def _queue_outcome(*games: tuple[int, str], paused: bool = False) -> Any:
    """构造一个"队列里有这些游戏"的轮询结果(界面用例不必走真实探测)."""
    from archive_management.application.games import ActivationOutcome, QueueItem
    from archive_management.domain import ActivationState, Game

    items = tuple(
        QueueItem(
            game=Game(id=game_id, name=name),
            position=index + 1,
            running=True,
            monitor=index == 0,
            first_seen_at="2026-09-25T10:00:00.000Z",
            last_seen_at="2026-09-25T10:00:05.000Z",
        )
        for index, (game_id, name) in enumerate(games)
    )
    monitor = items[0].game if items else None
    return ActivationOutcome(
        state=ActivationState(
            monitor_game_id=None if monitor is None else monitor.id,
            armed=bool(items),
            paused=paused,
        ),
        monitor=monitor,
        queue=items,
    )


def test_the_activation_page_lists_the_queue_and_picks_the_monitor() -> None:
    """启停分页: 按启动顺序列出队列, 监控中的那款按钮禁用, 其余可点."""
    from archive_management.ui.activation_page import ActivationPanel
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette

    picked: list[str] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = ActivationPanel(
        ctk.CTkFrame(app),
        palette=Palette.for_theme(app._theme),
        on_monitor=picked.append,
    )
    try:
        panel.render(_queue_outcome((3, "First"), (7, "Second")), enabled=True)
        _pump(app)

        texts = _label_texts(panel.frame)
        assert "First" in texts
        assert "Second" in texts
        assert tr("activation.row_monitor") in texts
        assert tr("activation.row_running") in texts
        assert tr("activation.column_order") in texts
        assert tr("activation.summary", monitor="First", count=2) in texts

        buttons = _button_named(panel.frame, tr("activation.action_monitor"))
        assert len(buttons) == 2
        assert str(buttons[0].cget("state")) == "disabled"
        buttons[1].invoke()
        _pump(app)
        assert picked == ["7"]
    finally:
        panel.frame.destroy()
        app.destroy()


def test_the_activation_page_points_at_the_setting_when_off() -> None:
    """开关关闭时不摆空表: 直接告诉用户去哪里打开."""
    from archive_management.ui.activation_page import ActivationPanel
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = ActivationPanel(ctk.CTkFrame(app), palette=Palette.for_theme(app._theme))
    try:
        panel.render(enabled=False)
        _pump(app)

        texts = _label_texts(panel.frame)
        assert tr("activation.state_off") in texts
        assert tr("activation.summary_off") in texts
        assert tr("activation.row_monitor") not in texts
        assert _button_named(panel.frame, tr("activation.action_monitor")) == []
    finally:
        panel.frame.destroy()
        app.destroy()


def test_setting_the_monitor_switches_the_game_and_resets_the_interval() -> None:
    """点「设为监控对象」= 手动启用它, 并把轮询间隔退回最快档(马上再探一次)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    try:
        target = app.backend.list_games()[1].game_id
        app._activation_steps = 3
        app._activation_due = time.monotonic() + 99.0

        app._on_set_monitor(target)
        _pump(app)

        enabled = [game.game_id for game in app.backend.list_games() if game.enabled]
        assert enabled == [target]
        assert app._activation_steps == 0
        assert app._activation_due == 0.0
    finally:
        app.destroy()


def test_the_probe_interval_grows_while_a_game_is_running() -> None:
    """队列非空则轮询逐档放慢; 队列清空回到最快档(由 _finish_activation 记账)."""
    from archive_management.domain.activation import activation_delay
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    try:
        app._activation_run = _queue_outcome((1, "First"))
        app._finish_activation()
        assert app._activation_running is True
        assert app._activation_steps == 1
        assert activation_delay(app._activation_steps, running=True) > activation_delay(
            0, running=False
        )

        app._activation_run = _queue_outcome()
        app._finish_activation()
        assert app._activation_running is False
        assert app._activation_steps == 0
    finally:
        app.destroy()


def test_opening_the_activation_section_probes_immediately() -> None:
    """进入启停分页是人工动作: 间隔清零并立刻再探一次(开关开时)."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import HomeSection

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    try:
        app._activation = True
        app._activation_steps = 2
        app._activation_due = time.monotonic() + 99.0

        app._home_page._show_section(HomeSection.ACTIVATION)

        assert app._home_page._section is HomeSection.ACTIVATION
        assert app._activation_steps == 0
        assert app._activation_due == 0.0
        # 下一次事件循环就会真的探测一次: 到期时间被推到将来(不必等满一个间隔).
        assert _wait_for(app, lambda: app._activation_due > 0.0)
    finally:
        app.destroy()


def test_service_status_shows_storage_usage_without_hotkey(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """右下角的服务状态显示备份存储占用, 且不再展示快捷键."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "usage.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    game = service.add_game("占用测试")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")
    service.run_backup_now(game.game_id)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        text = app._service_label.cget("text")

        assert tr("status.service_ok") in text
        assert "当前占用 " in text
        assert "磁盘剩余" not in text
        # 快捷键不再出现在状态里(仅保留服务状态与空间占用).
        assert "Ctrl" not in text
        assert "Alt" not in text
        # "服务正常 · 当前占用 X" 两段拼接, 占用部分与后端给出的占用文本一致.
        usage = text.split(" · ", 1)[1]
        assert usage.startswith("当前占用 ")
        assert usage == tr("status.usage", used=usage.removeprefix("当前占用 "))
    finally:
        app.destroy()


def test_the_activation_page_covers_conflicts_paused_and_missing_callbacks() -> None:
    """启停分页的边角: 冲突/暂停/被抑制各有文案, 空时间戳有占位符, 没接回调时只返回."""
    from dataclasses import replace

    from archive_management.ui.activation_page import ActivationPanel
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette

    picked: list[str] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    palette = Palette.for_theme(app._theme)
    panel = ActivationPanel(
        ctk.CTkFrame(app),
        palette=palette,
        on_monitor=picked.append,
        on_open_detail=picked.append,
        on_refresh=lambda: picked.append("refresh"),
    )
    bare = ActivationPanel(ctk.CTkFrame(app), palette=palette)
    try:
        base = _queue_outcome((3, "First"), (7, "Second"), paused=True)
        outcome = replace(
            base,
            conflicts=("demo",),
            queue=(
                replace(base.queue[0], first_seen_at="", last_seen_at=""),
                replace(base.queue[1], suppressed=True),
            ),
        )

        # 省略 enabled 时只更新结果、不改开关状态; 再给一次 enabled=True 才走"开启"那套提示.
        panel.render(outcome)
        panel.render(outcome, enabled=True)
        _pump(app)

        texts = _label_texts(panel.frame)
        # 提示行是"固定说明 + 冲突 + 当前状态"拼成的一段, 因此按整段文本查找.
        hints = "\n".join(texts)
        assert tr("activation.conflicts", count=1) in hints
        assert tr("activation.state_paused") in hints
        assert tr("activation.row_suppressed") in texts
        assert "—" in texts, "空时间戳要用占位符, 不能是空白"

        details = _button_named(panel.frame, tr("activation.action_detail"))
        assert len(details) == 2, "接了回调才摆打开详情按钮"
        details[0].invoke()
        _pump(app)
        assert picked[-1] == "3"
        monitors = _button_named(panel.frame, tr("activation.action_monitor"))
        assert str(monitors[0].cget("state")) == "disabled", "监控中那一行不能再点"
        monitors[1].invoke()
        _pump(app)
        assert picked[-1] == "7"

        # 没接任何回调的面板: 三个动作都只返回(不因为回调缺失而崩).
        bare.render(outcome)
        bare._refresh()
        bare._monitor(outcome.queue[1])
        bare._open(outcome.queue[1])
        assert bare._on_refresh is None

        # 同一个调色板重复调用直接返回; 真换了主题才重建整页.
        panel.apply_palette(palette)
        panel.apply_palette(Palette.for_theme("light"))
        _pump(app)
        assert tr("activation.conflicts", count=1) in "\n".join(
            _label_texts(panel.frame)
        )
    finally:
        panel.frame.destroy()
        bare.frame.destroy()
        app.destroy()


def test_main_window_covers_empty_states_and_archived_guards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主窗口的边角守卫: 没有配置路径的写回口、空选中、归档后的动作、忙碌与消息收尾."""
    import archive_management.ui.main_window as main_mod
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    try:
        # ① 没有配置路径: 三个写回口只改内存(不碰文件)也要能跑完.
        assert app._paths is None
        assert app._on_font_size_change(18) is None
        assert app._on_debug_change(True) is None
        app._on_debug_change(False)
        assert app._on_activation_change(True) is None
        assert app._on_verification_change("name") is None
        assert app._verification_mode == "name"
        # 陌生取值收敛成 sha256(手改配置/将来新增的方式都不能让它变松).
        assert app._on_verification_change("crc32") is None
        assert app._verification_mode == "sha256"

        # ② 启停开关关着时"立即再探一次"只把分页切到关闭态.
        app._activation = False
        app._poll_activation_now()

        # ③ 选中已不存在的游戏 / 打开已删除游戏的详情: 回到空状态, 不切页.
        page_before = app._page
        app._open_game_detail("999")
        assert app._game is None
        assert app._page is page_before
        app._select_game("999")
        assert app._game is None

        # ④ 详情页重裁在主页上直接返回; 空库时不重建左侧列表.
        app._refit_job = None
        app._refit_detail_names()
        game_id, app._game_id = app._game_id, None
        app._render_list()
        app._game_id = game_id

        # ⑤ 时间范围筛选的两个方向; 局部重绘的两种"找不到卡片".
        assert app._filter_period.set(tr("filter.all_time")) is None
        assert app._apply_period_filter([]) == []
        app._filter_period.set(tr("filter.last_week"))
        assert app._apply_period_filter([]) == []
        app._paint_card_by_id(None)
        app._paint_card_by_id("没有这张卡片")
        app._set_hover("hover-x")
        app._set_hover("hover-x")
        app._set_hover(None)

        # ⑥ 归档后: 备份/恢复/分支三条路都只提示(界面动作走的是同一套规则).
        #    归档的游戏会从列表里消失, 所以直接给窗口一个"已归档"的实体.
        from dataclasses import replace as _replace

        app._select_game("outer-wilds")
        _pump(app)
        backups = list(app.backend.list_backups("outer-wilds"))
        app._select_backup(backups[-1])
        _pump(app)
        game = app._game
        assert game is not None
        app._game = _replace(game, archived=True)
        app._backup_id = backups[-1].backup_id
        for action in (app._on_backup, app._on_restore, app._on_branch):
            action()
            assert app._last_feedback is not None
            assert tr("home.archived_blocked", name=game.name) in app._last_feedback[1]
        app._game = game

        # ⑦ 恢复预检的原因代码: 空值不翻译, 有值才翻译.
        assert app._restore_problem_text(None) == ""
        assert app._restore_problem_text("protected") != ""

        # ⑧ 忙碌时"添加游戏"直接返回; 没有选中游戏时管理窗口只提示.
        app._set_busy(True)
        app._on_add_game()
        app._set_busy(False)
        game, app._game = app._game, None
        app._open_manage_game()
        assert app._game is None
        app._game = game
        app._open_manage_game()
        _pump(app)
        _close_toplevels(app)
        game_id, app._game_id = app._game_id, None
        app._after_schedule_change()

        # ⑨ 快捷键: 非法组合只回原因; 没有游戏时不触发; 分支那一路走 quick 模式.
        assert app._apply_shortcut("save", "不是加速键") != ""
        app._game = None
        app._run_hotkey(main_mod.ACTION_CREATE_BRANCH)
        app._game = game
        app._game_id = game_id
        app._run_hotkey(main_mod.ACTION_CREATE_BRANCH)
        _pump(app)

        # ⑩ 消息收尾: 没有回调时自己解除忙碌; 取消过的那次用 INFO 而不是错误.
        app._finish_activation()  # 没有轮询结果 → 直接返回
        app._pending_ok = None
        app._finish_message("ok", "")
        app._canceled = True
        app._finish_message("err", "出错了")
        assert app._canceled is False
        app._detail_name = ""
        app._set_detail_names()
    finally:
        app.destroy()


def test_smoke_start_schedules_a_self_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """带 ``--smoke`` 启动: 构造时就排好"到点自己关闭", 并真的走到正常关闭路径.

    没有这个参数时走的是另一条路(启动即补一次译名探测), 两条都要能跑。
    """
    import contextlib

    from archive_management.ui.demo_backend import DemoArchiveService

    closed: list[str] = []
    backend = DemoArchiveService(delay=0)
    monkeypatch.setattr(backend, "shutdown", lambda: closed.append("shutdown"))

    def build(service: Any) -> Any:
        hotkeys = GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用"))
        return ArchiveApp(
            service, title="冒烟自检", hotkeys=hotkeys, smoke_seconds=0.05
        )

    app = gui_app(build, backend)
    deadline = time.monotonic() + 5.0
    while not closed and time.monotonic() < deadline:
        with contextlib.suppress(TclError):
            _pump(app)
        time.sleep(0.02)

    assert closed, "冒烟自检到点必须走 _on_close(它负责释放调度器与快捷键)"


def test_run_gui_reports_every_config_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``run_gui`` 读配置的三个分支: 干净 / 只坏一项(修复) / 整份坏掉(还原).

    窗口在这里用替身: 这条用例要的是"读配置之后的三个分支", 真窗口由冒烟那条用例覆盖。
    """
    import archive_management.ui.main_window as main_window_mod
    from archive_management.ui.main_window import run_gui

    opened: list[Any] = []

    class _StubApp:
        """替身窗口: 不建 Tk 根, 也不进事件循环."""

        def __init__(self, backend: Any, **_kwargs: Any) -> None:
            opened.append(backend)

        def mainloop(self) -> None:
            """替身不进入事件循环."""

    monkeypatch.setattr(main_window_mod, "ArchiveApp", _StubApp)

    def run_once(name: str, config_text: str | None) -> None:
        paths = ApplicationPaths.default(override_root=tmp_path / name).ensure()
        if config_text is not None:
            paths.config_path.write_text(config_text, encoding="utf-8")
        assert run_gui(paths=paths) == 0

    run_once("fresh", None)
    run_once("repaired", '{"version": 1, "theme": "neon"}')
    run_once("reset", "{")
    assert len(opened) == 3, "三次启动都要真的装配过后端"


def test_cancel_with_a_running_operation_reports_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真的有可取消的操作时点取消: 进"正在取消"提示, 并记住这次取消."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        monkeypatch.setattr(app.backend, "cancel_active", lambda: True)

        app._on_cancel()

        assert app._canceled is True
        assert app._last_feedback[0] == FeedbackKind.PENDING
        assert app._last_feedback[1] == tr("action.cancel_pending")
    finally:
        app._canceled = False
        app.destroy()


def test_refresh_after_manage_resets_the_probe_ladder() -> None:
    """手动改完游戏状态后, 自动启停的轮询间隔退回最快档(马上再探一次)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app._activation = True
        app._activation_steps = 4
        app._activation_due = time.monotonic() + 99.0

        app._refresh_after_manage()

        assert app._activation_steps == 0
        assert app._activation_due == 0.0
    finally:
        app._activation = False
        app.destroy()


def test_table_header_alignment_stops_when_it_cannot_improve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """表头对齐的两处止损: 调太多次之后不再折腾; 内边距顶到边上就不再挪.

    真实窗口里布局一旦稳定, 表头与数据行的偏移就是 0(那会走"已经对齐"的提前返回),
    所以这里把两处几何量换成"表头整体偏右 5px"的替身, 专门驱动止损分支。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.home_page import _ALIGN_MAX_ATTEMPTS

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        main = app._home_page
        main._select("outer-wilds")
        _pump(app)
        parts = next(iter(main._row_parts.values()))
        head_name = main._head_name
        head_columns = main._head_columns
        # 偏移: 左边 5px(表头偏右), 右边 5px(表头右边界超出数据行)。
        monkeypatch.setattr(
            parts.name_block, "winfo_rootx", lambda: head_name.winfo_rootx() - 5
        )
        monkeypatch.setattr(parts.columns, "winfo_rootx", head_columns.winfo_rootx)
        monkeypatch.setattr(
            parts.columns, "winfo_width", lambda: head_columns.winfo_width() - 5
        )
        assert head_name.winfo_rootx() - parts.name_block.winfo_rootx() == 5

        # ① 已经调了很多轮: 不再改内边距, 也不再计数。
        main._align_attempts = _ALIGN_MAX_ATTEMPTS
        main._align_table_header()
        assert main._align_attempts == _ALIGN_MAX_ATTEMPTS

        # ② 左边距已经是 0(不能再往左挪), 算出来的新值与当前一致: 就此停手。
        main._align_attempts = 0
        main._head_pads = (0, main._head_pads[1])
        before_pads = main._head_pads
        main._align_table_header()
        assert main._head_pads == before_pads
    finally:
        app.destroy()


def test_activation_page_refresh_reaches_the_callback() -> None:
    """启停分页的"刷新": 把请求交给主窗口(它顺带把轮询间隔退回最快档)."""
    from archive_management.ui.activation_page import ActivationPanel
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette

    seen: list[str] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = ActivationPanel(
        ctk.CTkFrame(app),
        palette=Palette.for_theme(app._theme),
        on_monitor=lambda _game_id: None,
        on_refresh=lambda: seen.append("refresh"),
    )
    try:
        panel._refresh()

        assert seen == ["refresh"]
    finally:
        panel.frame.destroy()
        app.destroy()
