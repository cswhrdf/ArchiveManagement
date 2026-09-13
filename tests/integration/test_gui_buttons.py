"""页面按钮功能测试.

在可用图形环境下真实触发主窗口与管理窗口的各按钮, 验证点击不抛
``TclError``、不留下未复位状态, 并产生符合预期的反馈/数据变化。本
模块集中覆盖两类回归: (1) 动态列表重建后 ``UiKit`` 主题重绘不再命中
已销毁控件; (2) 真实 SQLite 后端的阶段占位动作给出明确提示而不是
崩溃。无 tkinter/图形环境自动跳过。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter  # noqa: F401 - 校验 customtkinter 可导入
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

import archive_management.ui.main_window as main_mod
import archive_management.ui.manage_window as mgr_mod
import archive_management.ui.schedule_window as sched_mod
from archive_management.i18n import tr
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.backend import ArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import FeedbackKind

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
_DRAIN_TIMEOUT_SECONDS = 20.0


def _patch_dialogs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ask_text: str = "",
    ask_text_queue: list[str] | None = None,
    branch_name: str = "测试分支",
    confirm: bool = True,
    edit_result: tuple[str, str] | None = ("新名字", "新描述"),
    schedule_result: tuple[str, str] | None = ("", "3"),
    restore_result: bool | None = True,
) -> None:
    """把模态对话框替换为自动应答, 避免 wait_window 阻塞测试线程.

    需要输入路径/文本的测试应通过 ``ask_text`` 显式传入由 ``tmp_path``
    派生的跨平台路径; 未触发文本输入的动作无需关心该默认空值.
    ``ask_text_queue`` 用于一次动作会连续弹出多个输入框的场景(按顺序取值);
    ``edit_result`` / ``schedule_result`` / ``restore_result`` 分别对应单窗口
    编辑对话框、定时任务对话框与恢复选项对话框的返回值(``None`` 表示取消),
    其中 ``restore_result`` 表示是否勾选"恢复前先创建安全点".
    """
    answers = list(ask_text_queue or [])

    def next_text(*_args: Any, **_kwargs: Any) -> str:
        if answers:
            return answers.pop(0)
        return ask_text

    monkeypatch.setattr(main_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(main_mod, "ask_text", next_text)
    monkeypatch.setattr(main_mod, "ask_branch_name", lambda *_a, **_k: branch_name)
    monkeypatch.setattr(main_mod, "info_dialog", lambda *_a, **_k: None)
    monkeypatch.setattr(main_mod, "edit_backup_dialog", lambda *_a, **_k: edit_result)
    monkeypatch.setattr(main_mod, "restore_dialog", lambda *_a, **_k: restore_result)
    monkeypatch.setattr(mgr_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(mgr_mod, "ask_text", next_text)
    monkeypatch.setattr(mgr_mod, "info_dialog", lambda *_a, **_k: None)
    # 定时备份配置入口已移到游戏设置与全局任务窗口, 两者都经 schedule_window 弹框.
    monkeypatch.setattr(sched_mod, "schedule_dialog", lambda *_a, **_k: schedule_result)
    monkeypatch.setattr(sched_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(sched_mod, "info_dialog", lambda *_a, **_k: None)


def _pump(app: ArchiveApp) -> None:
    app.update_idletasks()
    app.update()


def _drain(app: ArchiveApp) -> None:
    """等后台线程结果经消息队列回到主线程(忙碌标志复位).

    恢复与会话级的备份会真的读写文件, 在带覆盖率或高负载机器上会明显变慢,
    因此把等待上限放宽并在超时时给出明确失败信息(而不是让后续断言莫名失败)。
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    while getattr(app, "_busy", False) and time.monotonic() < deadline:
        _pump(app)
        time.sleep(0.005)
    app._poll_messages()  # 手动分发消息队列
    _pump(app)
    assert not getattr(app, "_busy", False), "后台操作在等待上限内未完成"


def _feedback_kind(app: ArchiveApp) -> FeedbackKind:
    """返回最近一次反馈的等级.

    经函数返回可避免 mypy 对 ``app._last_feedback[0]`` 这个索引表达式做
    字面量收窄(收窄后再比较其它等级会被判为 non-overlapping)。
    """
    return app._last_feedback[0]


def _label_texts(widget: Any) -> list[str]:
    """递归收集控件树里所有标签的文案(便于断言界面不再显示某些内容)."""
    import customtkinter as ctk

    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found.extend(_label_texts(child))
    return found


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口; 无显示环境时抛出 TclError 由调用方转 skip."""
    return ArchiveApp(
        backend,
        title="按钮测试",
        # 测试进程不注册系统级快捷键, 避免遗留键盘钩子或误触发备份.
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
    )


# ---------------------------------------------------------------- 主窗口


def test_switch_game_view_and_filter_do_not_crash() -> None:
    """重复切换游戏/视图/筛选不触发旧控件重绘崩溃(回归 UiKit 生命周期)."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import ViewKind

    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
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
    finally:
        app.destroy()


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


def test_default_view_is_branch_tree_and_hides_old_auto_backups() -> None:
    """默认展示分支树; 分支树只保留最新一份自动备份, 时间线展示全部."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import ViewKind

    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("outer-wilds")
        assert app._view == ViewKind.BRANCH
        branch_cards = len(app._cards)
        total_items = len(app._items)

        app._switch_view(ViewKind.TIMELINE)
        _pump(app)

        assert len(app._cards) == total_items
        assert branch_cards < total_items
    finally:
        app.destroy()


def test_delete_and_rename_buttons_update_backups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除备份与重命名/描述按钮直接作用于真实后端数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import FeedbackKind

    _patch_dialogs(monkeypatch, edit_result=("新名字", "新描述"))
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("outer-wilds")
        leaf = next(item for item in app._items if item.backup_id == "b5")
        app._select_backup(leaf)

        app._on_rename_backup()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        renamed = next(item for item in app._items if item.backup_id == "b5")
        assert renamed.title == "新名字"
        assert renamed.sub == "新描述"

        app._select_backup(renamed)
        app._on_delete_backup()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        assert "b5" not in {item.backup_id for item in app._items}
        assert not app._busy
    finally:
        app.destroy()


def test_delete_branch_root_asks_for_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除分支根节点前必须二次确认; 取消时不改动数据."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, confirm=False)
    titles: list[str] = []

    def declining(*_args: Any, **kwargs: Any) -> bool:
        titles.append(str(kwargs.get("title", "")))
        return False

    monkeypatch.setattr(main_mod, "confirm_dialog", declining)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("outer-wilds")
        root = next(item for item in app._items if item.backup_id == "b3")
        app._select_backup(root)
        before = len(app._items)

        app._on_delete_backup()
        _drain(app)

        assert titles, "删除分支根节点应先弹出确认对话框"
        assert len(app._items) == before
    finally:
        app.destroy()


def test_schedule_window_edits_interval_and_keep_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全局定时任务窗口里编辑某个游戏: 周期与保留份数写入后端, 列表随之刷新."""
    from archive_management.i18n import tr
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, schedule_result=("1h", "5"))
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        window = ScheduleWindow(
            app, backend=app.backend, palette=Palette.for_theme(app._theme)
        )
        # 只列出已配置定时备份的游戏(山海旅人/无尽太空都未配置, 不列出).
        assert {item.game_id for item in window._items} == {"outer-wilds"}

        window._select("outer-wilds")
        window._on_edit()
        _pump(app)

        status = app.backend.task_status("outer-wilds")
        assert status.schedule_text == "1h"
        assert status.keep_auto == 5
        refreshed = next(i for i in window._items if i.game_id == "outer-wilds")
        assert refreshed.interval_text == "1h"
        assert refreshed.keep_auto == 5
        assert tr("schedule.updated", name="星际拓荒") in window._status_label.cget(
            "text"
        )
        # 其它游戏的配置不受影响(山海旅人原本就没有定时任务).
        assert app.backend.task_status("shanhai").schedule_text == ""
        assert "shanhai" not in {item.game_id for item in window._items}
    finally:
        app.destroy()


def test_schedule_window_cancel_keeps_configuration(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消对话框时不修改任何定时配置."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, schedule_result=None)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        window = ScheduleWindow(
            app, backend=app.backend, palette=Palette.for_theme(app._theme)
        )
        before = app.backend.task_status("outer-wilds")

        window._select("outer-wilds")
        window._on_edit()
        _pump(app)

        after = app.backend.task_status("outer-wilds")
        assert after.schedule_text == before.schedule_text
        assert after.keep_auto == before.keep_auto
    finally:
        app.destroy()


def test_rename_dialog_cancel_keeps_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消名称/描述编辑时备份保持原样."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, edit_result=None)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("outer-wilds")
        item = next(item for item in app._items if item.backup_id == "b5")
        app._select_backup(item)

        app._on_rename_backup()
        _drain(app)

        after = next(x for x in app._items if x.backup_id == "b5")
        assert after.title == item.title
        assert after.sub == item.sub
    finally:
        app.destroy()


def test_schedule_window_rejects_invalid_keep_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """保留份数非法时弹窗报错且不修改配置."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, schedule_result=("1h", "abc"))
    errors: list[str] = []
    monkeypatch.setattr(
        sched_mod, "info_dialog", lambda *_a, **k: errors.append(k["message"])
    )
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        window = ScheduleWindow(
            app, backend=app.backend, palette=Palette.for_theme(app._theme)
        )
        before = app.backend.task_status("outer-wilds")

        window._select("outer-wilds")
        window._on_edit()
        _pump(app)

        assert errors
        after = app.backend.task_status("outer-wilds")
        assert after.schedule_text == before.schedule_text
        assert after.keep_auto == before.keep_auto
    finally:
        app.destroy()


def test_schedule_window_removes_and_toggles_task(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除清空周期(保留份数不变), 暂停/继续只改启用状态."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, confirm=True)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        window = ScheduleWindow(
            app, backend=app.backend, palette=Palette.for_theme(app._theme)
        )

        window._select("outer-wilds")
        window._on_remove()
        _pump(app)
        assert app.backend.task_status("outer-wilds").schedule_text == ""

        # 删掉任务后它就从列表里消失(列表只展示已配置的任务).
        assert "outer-wilds" not in {item.game_id for item in window._items}
        assert "outer-wilds" not in window._rows
        assert window._toggle_btn.cget("state") == "disabled"

        # 重新配置后可以暂停, 暂停只是不启用定时器而周期仍保留.
        assert app.backend.set_schedule("outer-wilds", "2h", enabled=False)
        window.reload()
        window._select("outer-wilds")
        paused = next(i for i in window._items if i.game_id == "outer-wilds")
        assert paused.interval_text == "2h"
        assert paused.enabled is False
        assert paused.state_label == "已暂停"
    finally:
        app.destroy()


def test_schedule_window_add_picks_game_from_filterable_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新增任务先弹窗选游戏(候选只含可配置的游戏), 选完再走配置对话框."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, schedule_result=("6h", "2"))
    picked: list[list[str]] = []

    def fake_add(
        _parent: Any,
        _palette: Any,
        *,
        candidates: Any,
        blocked: Any = (),
    ) -> str | None:
        picked.append([item.game_name for item in candidates])
        chosen: str = next(item.game_id for item in candidates)
        return chosen

    def fake_info(_parent: Any, _palette: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(sched_mod, "add_schedule_dialog", fake_add)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        window = ScheduleWindow(
            app, backend=app.backend, palette=Palette.for_theme(app._theme)
        )
        # 候选只剩已配置存档位置且未配调度的游戏; 无存档位置的被单独归类.
        assert [item.game_name for item in window._addable()] == ["山海旅人"]
        blocked = [item.game_name for item in window._blocked()]
        assert blocked == ["无尽太空"]
        assert window._add_btn.cget("state") == "normal"

        window._on_add()
        _pump(app)

        assert picked == [["山海旅人"]]
        assert app.backend.task_status("shanhai").schedule_text == "6h"
        assert "shanhai" in {item.game_id for item in window._items}
        assert window._selected == "shanhai"

        # 只剩无存档位置的游戏: 给出原因提示且不再弹选游戏窗口.
        monkeypatch.setattr(sched_mod, "info_dialog", fake_info)
        window.reload()
        assert window._addable() == []
        window._on_add()
        assert "存档位置" in window._status_label.cget("text")
    finally:
        app.destroy()


def test_game_without_locations_cannot_be_scheduled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未配置存档位置的游戏无法创建定时任务, 并提示具体原因."""
    from archive_management.exceptions import ArchiveManagementError
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.schedule_window import edit_schedule

    _patch_dialogs(monkeypatch)
    notes: list[str] = []
    monkeypatch.setattr(
        sched_mod, "info_dialog", lambda *_a, **k: notes.append(k["message"])
    )
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        changed = edit_schedule(
            app,
            app.p,
            app.backend,
            game_id="endless-space",
            game_name="无尽太空",
        )

        assert changed is False
        assert app.backend.task_status("endless-space").schedule_text == ""
        assert notes
        assert "存档位置" in notes[-1]

        # 后端自身也把这类游戏拦下来(界面之外再调一次也会得到同样的原因).
        with pytest.raises(ArchiveManagementError):
            app.backend.set_schedule("endless-space", "30m")
    finally:
        app.destroy()


def test_workspace_nav_opens_schedule_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """工作区的"定时任务"入口打开全局任务窗口(而不是只给一句提示)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(
        main_mod,
        "ScheduleWindow",
        lambda _parent, **kwargs: opened.append(kwargs),
    )
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._on_nav(tr("sidebar.nav_scheduled"), tr("sidebar.nav_scheduled_hint"))

        assert len(opened) == 1
        assert opened[0]["backend"] is app.backend
    finally:
        app.destroy()


def test_game_settings_can_configure_schedule_per_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """游戏设置里的"定时备份"按钮只改当前游戏的配置."""

    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, schedule_result=("30m", "2"))
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("shanhai")
        manager = _open_manager(
            app,
            game_id="shanhai",
            name="山海旅人",
            backup_location="本地备份目录",
        )

        manager._on_schedule()
        _pump(app)

        assert app.backend.task_status("shanhai").schedule_text == "30m"
        assert app.backend.task_status("shanhai").keep_auto == 2
        assert app.backend.task_status("outer-wilds").schedule_text == "1d"
        assert "30m" in manager._schedule_state.cget("text")
    finally:
        app.destroy()


def test_topbar_and_workspace_carry_no_sync_or_theme_button() -> None:
    """顶栏不再展示同步时间与主题按钮; 工作区去掉"全部备份"入口."""

    from archive_management.ui.demo_backend import DemoArchiveService

    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        assert not hasattr(app, "_sync_label")
        assert not hasattr(app, "theme_btn")
        assert not hasattr(app, "_sync_labels")
        # 工作区只保留"定时任务"与"设置".
        texts = _label_texts(app._status_card.master)
        assert tr("sidebar.nav_scheduled") in texts
        assert tr("sidebar.nav_settings") in texts
        assert "全部备份" not in texts
    finally:
        app.destroy()


def test_settings_window_holds_theme_toggle_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置窗口提供主题切换按钮, 且不再展示定时任务配置."""

    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.settings_window import SettingsWindow

    _patch_dialogs(monkeypatch)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        before = app._theme
        window = SettingsWindow(
            app,
            palette=app.p,
            theme=before,
            shortcut=app._shortcut_text,
            on_toggle_theme=app._on_toggle_theme,
        )

        # 定时任务相关的信息不在设置里.
        labels = _label_texts(window._container)
        assert all("定时" not in text for text in labels)
        assert any("外观" in text for text in labels)

        # 点按钮即切换主题, 按钮文案与当前主题标签同步更新.
        window._toggle_btn.invoke()
        _pump(app)
        assert app._theme != before
        assert app.backend.current_theme() == app._theme
        assert window._toggle_text() in {
            tr("theme.to_light"),
            tr("theme.to_dark"),
        }
        assert tr(f"theme.name_{app._theme}") in window._theme_label.cget("text")
    finally:
        app.destroy()


def test_branch_default_name_is_localized(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建分支的默认名称是本地化文案(中文"分支", 英文"Branch")."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, branch_name=tr("dialog.branch_default"))
    assert tr("dialog.branch_default") == "分支"
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("shanhai")
        app._select_backup(app._items[0])

        app._on_branch()
        _drain(app)

        created = app._items[-1]
        assert created.title == "分支"
        assert created.branch_name == "分支"
    finally:
        app.destroy()


def test_task_card_wraps_long_paths() -> None:
    """任务卡里的长文案(备份目标/期间)自动换行而不是被裁掉."""
    from archive_management.ui.demo_backend import DemoArchiveService

    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("outer-wilds")
        _pump(app)

        for label in (app._task_name_label, app._task_next, app._task_target):
            assert int(label.cget("wraplength")) > 0
            assert label.cget("justify") == "left"
        assert app._task_target.cget("text")
        assert len(app._task_name_label.cget("text")) < 40
    finally:
        app.destroy()


def test_task_card_state_follows_schedule_state() -> None:
    """任务卡的状态文案反映定时备份的启用/暂停/未配置, 而不是当前有无操作."""
    from archive_management.ui.demo_backend import DemoArchiveService

    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        # 已配置且启用.
        app._select_game("outer-wilds")
        _pump(app)
        assert app._task_state_label.cget("text") == tr("schedule.state_on")

        # 暂停后立刻显示已暂停.
        app.backend.set_schedule("outer-wilds", "1d", enabled=False)
        app._after_schedule_change()
        _pump(app)
        assert app._task_state_label.cget("text") == tr("schedule.state_paused")

        # 恢复后回到已启用.
        app.backend.set_schedule("outer-wilds", "1d")
        app._after_schedule_change()
        _pump(app)
        assert app._task_state_label.cget("text") == tr("schedule.state_on")

        # 未配置定时备份的游戏显示未配置.
        app._select_game("shanhai")
        _pump(app)
        assert app._task_state_label.cget("text") == tr("schedule.state_off")
        assert app._task_next.cget("text") == "—"
    finally:
        app.destroy()


def test_backup_restore_branch_export_report_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主窗口动作按钮点击后进入成功反馈并复位忙碌状态."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import FeedbackKind

    _patch_dialogs(monkeypatch)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game("outer-wilds")
        backup_count = len(app._items)

        app._on_backup()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        assert not app._busy
        assert len(app._items) == backup_count + 1

        app._select_backup(app._items[0])
        app._on_restore()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS

        app._on_branch()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS

        app._on_export()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS
    finally:
        app.destroy()


def test_add_game_flow_selects_new_game(monkeypatch: pytest.MonkeyPatch) -> None:
    """新增游戏按钮按名称建游戏并选中."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import FeedbackKind

    _patch_dialogs(monkeypatch, ask_text="新按钮游戏")
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
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
    finally:
        app.destroy()


# ---------------------------------------------------------------- 管理窗口


def _open_manager(
    app: ArchiveApp,
    *,
    game_id: str,
    name: str,
    enabled: bool = True,
    backup_location: str,
    on_change: Callable[[], None] | None = None,
) -> Any:
    from archive_management.ui.manage_window import ManageGameWindow
    from archive_management.ui.palette import Palette

    manager: Any = ManageGameWindow(
        app,
        backend=app.backend,
        palette=Palette.for_theme(app._theme),
        game_id=game_id,
        name=name,
        enabled=enabled,
        backup_location=backup_location,
        # 缺省不刷新: 需要验证主窗口反应的用例传入 app._refresh_after_manage.
        on_change=on_change or (lambda: None),
    )
    _pump(app)
    return manager


def test_manage_window_game_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """管理窗口: 重命名/停用/重新启用 不报错且状态正确."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.manage_window import ManageGameWindow

    _patch_dialogs(monkeypatch, ask_text="改名成功")
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        manager = _open_manager(
            app,
            game_id="shanhai",
            name="山海旅人",
            backup_location=str(tmp_path / "backups"),
        )
        assert isinstance(manager, ManageGameWindow)

        manager._on_rename()
        assert manager._name == "改名成功"
        assert manager._backend.get_detail("shanhai").name == "改名成功"

        manager._on_toggle_enabled()
        assert manager._enabled is False
        assert manager._backend.get_detail("shanhai").name == "改名成功"
        manager._on_toggle_enabled()
        assert manager._enabled is True

        manager.close()
    finally:
        app.destroy()


def test_manage_window_location_actions(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """管理窗口: 新增目录/文件、设主位置、编辑、删除位置 均正常."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.manage_window import ManageGameWindow

    dir_target = str(tmp_path / "save-dir")
    file_target = str(tmp_path / "save-file")
    edited_target = str(tmp_path / "save-edited")
    _patch_dialogs(
        monkeypatch,
        ask_text=dir_target,
        confirm=True,
    )
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        manager = _open_manager(
            app,
            game_id="shanhai",
            name="山海旅人",
            backup_location=str(tmp_path / "backups"),
        )
        assert isinstance(manager, ManageGameWindow)
        count = len(manager._items)

        manager._on_add_directory()
        assert len(manager._items) == count + 1
        added = manager._items[-1]
        assert added.path_kind == "directory"
        assert added.path == dir_target

        # 文件用不同路径, 避免触发重复路径拦截
        monkeypatch.setattr(mgr_mod, "ask_text", lambda *_a, **_k: file_target)
        manager._on_add_file()
        assert len(manager._items) == count + 2
        file_item = manager._items[-1]
        assert file_item.path_kind == "file"
        assert file_item.path == file_target

        manager._select(added.location_id)
        manager._on_set_primary()
        primary = [item for item in manager._items if item.is_primary]
        assert [item.location_id for item in primary] == [added.location_id]

        manager._on_verify()
        manager.refresh()

        # 编辑选中的目录路径为不同值
        monkeypatch.setattr(mgr_mod, "ask_text", lambda *_a, **_k: edited_target)
        manager._on_edit_path()
        edited = next(
            item for item in manager._items if item.location_id == added.location_id
        )
        assert edited.path == edited_target

        manager._select(added.location_id)
        manager._on_remove()
        assert len(manager._items) == count + 1

        manager.close()
    finally:
        app.destroy()


def test_manage_window_delete_game_removes_and_closes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """删除游戏按钮经确认后删除并关闭管理窗口."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, confirm=True)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        game_id = app.backend.add_game("待删除").game_id
        manager = _open_manager(
            app,
            game_id=game_id,
            name="待删除",
            backup_location=str(tmp_path / "backups"),
        )
        manager._on_delete_game()
        ids = {game.game_id for game in app.backend.list_games()}
        assert game_id not in ids
    finally:
        app.destroy()


def test_deleting_last_game_clears_hero_panel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归: 删掉唯一游戏后概要区/标题行不得再显示被删游戏."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, confirm=True)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        # 只留一个游戏, 并把它选中(概要区处于"有内容"状态).
        for game in list(app.backend.list_games()):
            if game.game_id != "shanhai":
                app.backend.delete_game(game.game_id)
        app._refresh_after_manage(select="shanhai")
        _pump(app)
        assert app._hero_name_label.cget("text") == "山海旅人"

        manager = _open_manager(
            app,
            game_id="shanhai",
            name="山海旅人",
            backup_location=str(tmp_path / "backups"),
            on_change=app._refresh_after_manage,
        )
        manager._on_delete_game()
        _pump(app)

        assert app.backend.list_games() == []
        assert app._game_id is None
        assert app._game is None
        assert app._items == []
        assert app._cards == {}
        # 概要区与标题行回到空状态, 不再残留被删游戏的名字.
        assert app._hero_name_label.cget("text") == "未选择游戏"
        assert app._hero_location_label.cget("text") == ""
        assert "山海旅人" not in app._title_label.cget("text")
        assert app._selected_name.cget("text") == "未选择节点"
        assert app._hero_origin_label.winfo_manager() == ""
    finally:
        app.destroy()


def test_reload_after_external_delete_clears_panels() -> None:
    """回归: 游戏在别处被删掉后, 轮询触发的重载也要把界面清空."""
    from archive_management.ui.demo_backend import DemoArchiveService

    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        for game in list(app.backend.list_games()):
            app.backend.delete_game(game.game_id)

        app._reload_data()
        _pump(app)

        assert app._game_id is None
        assert app._hero_name_label.cget("text") == "未选择游戏"
        assert app._items == []
    finally:
        app.destroy()


def test_status_card_shows_storage_usage_without_hotkey(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """状态栏显示备份存储的当前占用, 且不再展示快捷键."""
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
        text = app._status_sub.cget("text")

        assert text.startswith("当前占用 ")
        assert "磁盘剩余" not in text
        # 快捷键不再出现在状态卡里(仅保留空间占用).
        assert "Ctrl" not in text
        assert "Alt" not in text
        assert text == tr("status.usage", used=text.removeprefix("当前占用 "))
    finally:
        app.destroy()


def test_task_panel_is_renamed_and_has_no_schedule_editor() -> None:
    """任务卡显示的是任务状态, 定时配置已移到游戏设置/定时任务窗口."""
    from archive_management.ui.demo_backend import DemoArchiveService

    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        assert app._task_hint.cget("text") == tr("task.hint")
        assert not hasattr(app, "_task_edit_btn")
    finally:
        app.destroy()


# ---------------------------------------------------------------- SQLite 后端


def test_sql_backend_backup_creates_node_and_later_phases_report_clearly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实后端: 备份落地为节点, 恢复/导出给出明确的阶段提示而非崩溃."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.models import FeedbackKind
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "btn.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    game = service.add_game("真实游戏")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game.game_id)

        app._on_backup()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        items = service.list_backups(game.game_id)
        assert len(items) == 1
        assert items[0].verified is True

        # "恢复到此节点"= 把备份内容写回原始存档, 再把当前节点移到这里.
        app._select_backup(items[0])
        app._on_restore()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        assert "当前节点" in app._last_feedback[1]
        assert service.list_backups(game.game_id)[0].is_current is True

        # 导出属于阶段 G, 当前必须给出明确提示而不是静默成功.
        app._on_export()
        _drain(app)
        assert _feedback_kind(app) == FeedbackKind.ERROR
        assert "G" in app._last_feedback[1]
        assert not app._busy
    finally:
        app.destroy()


def test_gui_backup_reports_unchanged_save_with_dialog(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """手动备份遇到"存档未变化"时弹窗告知, 且不新增节点."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.models import FeedbackKind
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    notes: list[str] = []
    monkeypatch.setattr(
        main_mod, "info_dialog", lambda *_a, **k: notes.append(k["message"])
    )
    db = Database(tmp_path / "unchanged.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    game = service.add_game("未变化游戏")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game.game_id)
        app._on_backup()
        _drain(app)
        items = service.list_backups(game.game_id)
        assert len(items) == 1

        # 存档没有任何改动: 再次备份应被拒绝并给出弹窗提示.
        app._on_backup()
        _drain(app)

        assert len(service.list_backups(game.game_id)) == 1
        assert notes
        assert "完全一致" in notes[-1]
        assert app._last_feedback[0] == FeedbackKind.INFO

        # 存档变化后备份恢复正常.
        (save_dir / "slot1.dat").write_text("v2", encoding="utf-8")
        app._on_backup()
        _drain(app)
        assert len(service.list_backups(game.game_id)) == 2
    finally:
        app.destroy()


def test_gui_backup_button_writes_snapshot_through_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """点击"立即创建备份"后, 备份目录出现带清单的快照."""
    from archive_management.infrastructure.database import Database
    from archive_management.services.snapshot import MANIFEST_FILENAME
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "btn.db")
    db.migrate()
    backup_root = tmp_path / "backups"
    service = SqlArchiveService(db, backup_root=backup_root)
    game = service.add_game("快照游戏")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "a.sav").write_text("one", encoding="utf-8")
    (save_dir / "nested").mkdir()
    (save_dir / "nested" / "b.sav").write_text("two", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game.game_id)
        app._on_backup()
        _drain(app)

        manifests = list(backup_root.rglob(MANIFEST_FILENAME))
        assert len(manifests) == 1
        snapshot_root = manifests[0].parent
        assert snapshot_root.parent.parent == backup_root
        # 游戏目录用名称命名(而不是数字 id), 并在概要区展示出来.
        assert snapshot_root.parent.name.startswith("快照游戏-")
        assert snapshot_root.parent.name != game.game_id
        assert snapshot_root.parent.name in app._hero_origin_label.cget("text")
        copies = sorted(path.name for path in snapshot_root.rglob("*.sav"))
        assert copies == ["a.sav", "b.sav"]
    finally:
        app.destroy()


def test_gui_restore_writes_files_back_through_service(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实后端: 恢复按钮按对话框选项回写文件, 并按需创建安全点."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.models import FeedbackKind
    from archive_management.ui.sql_backend import SqlArchiveService

    # 取消勾选安全点, 验证选项确实透传到用例层.
    _patch_dialogs(monkeypatch, restore_result=False)
    db = Database(tmp_path / "restore.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    game = service.add_game("恢复游戏")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game.game_id)
        app._on_backup()
        _drain(app)
        items = service.list_backups(game.game_id)
        (save_dir / "slot1.dat").write_text("changed", encoding="utf-8")
        (save_dir / "extra.txt").write_text("extra", encoding="utf-8")

        app._select_backup(items[0])
        app._on_restore()
        _drain(app)

        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        assert (save_dir / "slot1.dat").read_text(encoding="utf-8") == "v1"
        # 恢复是覆盖而不是镜像: 快照之外的文件保持原样, 也不会出现隔离区.
        assert (save_dir / "extra.txt").read_text(encoding="utf-8") == "extra"
        assert not (tmp_path / "backups" / "restore-archive").exists()
        # 取消勾选安全点后不应新增节点.
        assert len(service.list_backups(game.game_id)) == 1
    finally:
        app.destroy()


def test_gui_safety_point_stays_out_of_branch_view(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """安全点只出现在时间线, 分支树里不占位置, 但筛选可以定向到它."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.models import SourceFilter, ViewKind
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch, restore_result=True)
    db = Database(tmp_path / "safety.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    game = service.add_game("安全点游戏")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game.game_id)
        app._on_backup()
        _drain(app)
        items = service.list_backups(game.game_id)
        (save_dir / "slot1.dat").write_text("changed", encoding="utf-8")
        app._select_backup(items[0])
        app._on_restore()
        _drain(app)

        safety_id = next(item.backup_id for item in app._items if item.safety)

        # 时间线可见, 分支树隐藏.
        app._switch_view(ViewKind.TIMELINE)
        _pump(app)
        assert safety_id in app._cards
        app._switch_view(ViewKind.BRANCH)
        _pump(app)
        assert safety_id not in app._cards
        # 显式筛选"恢复前安全点"时, 分支树也能定向到它们.
        app._filter_source.set(SourceFilter.SAFETY.label)
        app._on_filter_change(SourceFilter.SAFETY.label)
        _pump(app)
        assert safety_id in app._cards
    finally:
        app.destroy()


def test_gui_restore_reports_snapshot_damage(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """快照被破坏时, 恢复按钮给出错误反馈而不是弹确认框."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.models import FeedbackKind
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "broken.db")
    db.migrate()
    backup_root = tmp_path / "backups"
    service = SqlArchiveService(db, backup_root=backup_root)
    game = service.add_game("损坏快照")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game.game_id)
        app._on_backup()
        _drain(app)
        items = service.list_backups(game.game_id)
        snapshots = sorted(backup_root.glob("*/*/loc-0/slot1.dat"))
        assert snapshots
        snapshots[0].write_text("tampered", encoding="utf-8")

        app._select_backup(items[0])
        app._on_restore()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.ERROR
        assert not app._busy
    finally:
        app.destroy()


def test_manage_window_delete_original_location_flow(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """管理窗口: 删除原始存档位置需输入游戏名称确认, 成功后移除该位置."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.manage_window import ManageGameWindow

    _patch_dialogs(monkeypatch, ask_text="山海旅人")
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        manager = _open_manager(
            app,
            game_id="shanhai",
            name="山海旅人",
            backup_location=str(tmp_path / "backups"),
        )
        assert isinstance(manager, ManageGameWindow)
        manager._select(manager._items[0].location_id)

        manager._on_delete_origin()

        assert manager._items == []
        summary = next(
            game for game in manager._backend.list_games() if game.game_id == "shanhai"
        )
        assert summary.has_locations is False
        manager.close()
    finally:
        app.destroy()


def test_manage_window_delete_original_rejects_wrong_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """确认名称不匹配时不删除任何内容, 位置列表保持不变."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.manage_window import ManageGameWindow

    _patch_dialogs(monkeypatch, ask_text="随便打个名字")
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        manager = _open_manager(
            app,
            game_id="shanhai",
            name="山海旅人",
            backup_location=str(tmp_path / "backups"),
        )
        assert isinstance(manager, ManageGameWindow)
        manager._select(manager._items[0].location_id)

        manager._on_delete_origin()

        assert len(manager._items) == 1
        manager.close()
    finally:
        app.destroy()


def test_manage_window_delete_original_blocks_backup_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实后端: 位于备份根目录内的位置会被预检拦下, 不做任何删除."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.manage_window import ManageGameWindow
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch, ask_text="Demo")
    db = Database(tmp_path / "blocked.db")
    db.migrate()
    backup_root = tmp_path / "backups"
    service = SqlArchiveService(db, backup_root=backup_root)
    game = service.add_game("Demo")
    inside = backup_root / "save"
    inside.mkdir(parents=True)
    (inside / "slot1.dat").write_text("v1", encoding="utf-8")
    service.add_location(game.game_id, path=str(inside), kind="directory")

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        manager = _open_manager(
            app,
            game_id=game.game_id,
            name="Demo",
            backup_location=str(backup_root),
        )
        assert isinstance(manager, ManageGameWindow)
        manager._select(manager._items[0].location_id)

        manager._on_delete_origin()

        assert len(manager._items) == 1
        assert inside.exists()
        manager.close()
    finally:
        app.destroy()
