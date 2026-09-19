"""页面按钮功能测试.

在可用图形环境下真实触发主窗口与管理窗口的各按钮, 验证点击不抛
``TclError``、不留下未复位状态, 并产生符合预期的反馈/数据变化。本
模块集中覆盖两类回归: (1) 动态列表重建后 ``UiKit`` 主题重绘不再命中
已销毁控件; (2) 真实 SQLite 后端尚未实现的动作用给出明确提示而不是
崩溃。无 tkinter/图形环境自动跳过。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gui_support import gui_app

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter as ctk  # 既校验可导入, 也用于构造测试用父容器
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

import archive_management.ui.discovery_page as disc_mod
import archive_management.ui.home_page as home_page_mod
import archive_management.ui.main_window as main_mod
import archive_management.ui.manage_window as mgr_mod
import archive_management.ui.schedule_window as sched_mod
from archive_management.domain import HomeView
from archive_management.exceptions import HotkeyError
from archive_management.i18n import tr
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import (
    ACTION_CREATE_BRANCH,
    ACTION_SAVE_NOW,
    DEFAULT_BRANCH_ACCELERATOR,
    DEFAULT_SAVE_ACCELERATOR,
    GlobalHotkeyService,
    UnavailableBackend,
    format_accelerator,
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
    import_result: tuple[str, tuple[str, ...]] | None = ("导入名", ()),
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
    # 游戏发现已合并进游戏主页的页面, 同样需要文本输入/确认/提示的自动应答.
    monkeypatch.setattr(disc_mod, "ask_text", next_text)
    monkeypatch.setattr(disc_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(disc_mod, "info_dialog", lambda *_a, **_k: None)
    # 导入对话框返回 (名称, 存档路径): 用它可以检查"确认的路径才会写库".
    monkeypatch.setattr(disc_mod, "import_game_dialog", lambda *_a, **_k: import_result)
    # 游戏主页窗口需要文本输入(存档位置/标签)与错误提示的自动应答.
    monkeypatch.setattr(home_page_mod, "ask_text", next_text)
    monkeypatch.setattr(home_page_mod, "info_dialog", lambda *_a, **_k: None)


def _pump(app: ArchiveApp) -> None:
    app.update_idletasks()
    app.update()


def _drain(app: ArchiveApp) -> None:
    """等后台线程结果经消息队列回到主线程(忙碌标志复位且消息已分发).

    恢复与会话级的备份会真的读写文件, 在带覆盖率或高负载机器上会明显变慢,
    因此这里等的是"条件成立"(忙碌标志复位), 而不是固定睡多久; 超时时给出
    指向审计日志的失败信息, 免得后续断言莫名失败。
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        _pump(app)
        app._poll_messages()  # 把后台结果搬回主线程
        _pump(app)
        if not getattr(app, "_busy", False):
            return
        time.sleep(0.005)
    raise AssertionError(
        f"后台操作在 {_DRAIN_TIMEOUT_SECONDS:.0f} 秒内未完成: "
        "若属于备份/恢复, 请查审计日志(backup.* / restore.*)确认是否已失败"
    )


def _service_backups(service: ArchiveService, game_id: str) -> list[Any]:
    """读取真实后端的备份列表, 数量不足时给出可诊断的失败信息.

    后台备份失败(例如 Windows 上临时目录被扫描器占用)时, 直接写
    ``service.list_backups(...)[0]`` 只会抛 IndexError, 看不出"备份根本没成功";
    审计日志里对应的是 ``backup.create.failed``。
    """
    items = service.list_backups(game_id)
    if not items:
        raise AssertionError(
            "预期至少一个备份, 实际为空: 后台备份未成功, 请查审计日志 backup.create.failed"
        )
    return list(items)


def _feedback_kind(app: ArchiveApp) -> FeedbackKind:
    """返回最近一次反馈的等级.

    经函数返回可避免 mypy 对 ``app._last_feedback[0]`` 这个索引表达式做
    字面量收窄(收窄后再比较其它等级会被判为 non-overlapping)。
    """
    return app._last_feedback[0]


def _current_page(app: ArchiveApp) -> Any:
    """返回主窗口当前页面.

    经函数返回可避免 mypy 对 ``app._page`` 做字面量收窄(收窄后再比较另一页
    会被判为 non-overlapping)。
    """
    return app._page


def _home_item(page: Any, game_id: str) -> Any:
    """取出主页列表里的某个游戏项(顺便收窄可选类型)."""
    board = page._board
    assert board is not None
    return next(entry for entry in board.games if entry.game_id == game_id)


def _label_texts(widget: Any) -> list[str]:
    """递归收集控件树里所有标签的文案(便于断言界面不再显示某些内容)."""
    import customtkinter as ctk

    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found.extend(_label_texts(child))
    return found


def _button_texts(widget: Any) -> list[str]:
    """递归收集控件树里所有按钮的文案(便于断言某个动作已从界面移除)."""
    import customtkinter as ctk

    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton):
            found.append(str(child.cget("text")))
        found.extend(_button_texts(child))
    return found


def _new_app(
    backend: ArchiveService,
    *,
    hotkeys: GlobalHotkeyService | None = None,
    paths: ApplicationPaths | None = None,
) -> ArchiveApp:
    """构造主窗口; 无显示环境时抛出 TclError 由调用方转 skip.

    缺省不注册系统级快捷键, 避免遗留键盘钩子或误触发备份; 需要验证"注册成功"
    的用例自行传入带替身后端的服务。
    """
    return ArchiveApp(
        backend,
        title="按钮测试",
        hotkeys=(
            hotkeys
            if hotkeys is not None
            else GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用"))
        ),
        paths=paths,
    )


class _RecordingBackend:
    """记录注册项的后端替身: 用于验证快捷键注册与持久化, 不碰真实系统快捷键."""

    def __init__(self) -> None:
        self.registered: dict[str, str] = {}
        self.suspends = 0
        self.resumes = 0

    def register(self, accelerator: str, callback: Any) -> object:
        """模拟注册; 重复注册当作失败."""
        if accelerator in self.registered:
            raise HotkeyError(f"快捷键已被占用: {accelerator}")
        self.registered[accelerator] = accelerator
        return accelerator

    def unregister(self, handle: object) -> None:
        """移除注册项."""
        self.registered.pop(str(handle), None)

    def suspend(self) -> None:
        """记录暂停次数."""
        self.suspends += 1

    def resume(self) -> None:
        """记录恢复次数."""
        self.resumes += 1

    def stop(self) -> None:
        """空实现."""


# ---------------------------------------------------------------- 主窗口


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
    """回归: 轮询时数据库报错不得让窗口崩掉(记日志 + 状态栏提示即可)."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "poll.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")

    def broken(_game_id: str | None) -> Any:
        raise sqlite3.OperationalError("unsupported file format")

    monkeypatch.setattr(service, "task_status", broken)
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        for _index in range(3):
            app._poll_messages()
            _pump(app)
        assert _feedback_kind(app) == FeedbackKind.ERROR
        assert "unsupported file format" in app._last_feedback[1]
    finally:
        app.destroy()


def test_default_view_is_branch_tree_and_hides_old_auto_backups() -> None:
    """默认展示分支树; 分支树只保留最新一份自动备份, 时间线展示全部."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import ViewKind

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._select_game("outer-wilds")
    assert app._view == ViewKind.BRANCH
    branch_cards = len(app._cards)
    total_items = len(app._items)

    app._switch_view(ViewKind.TIMELINE)
    _pump(app)

    assert len(app._cards) == total_items
    assert branch_cards < total_items


def test_delete_and_rename_buttons_update_backups(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除备份与重命名/描述按钮直接作用于真实后端数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import FeedbackKind

    _patch_dialogs(monkeypatch, edit_result=("新名字", "新描述"))
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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


def test_rename_dialog_cancel_keeps_backup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取消名称/描述编辑时备份保持原样."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, edit_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._select_game("outer-wilds")
    item = next(item for item in app._items if item.backup_id == "b5")
    app._select_backup(item)

    app._on_rename_backup()
    _drain(app)

    after = next(x for x in app._items if x.backup_id == "b5")
    assert after.title == item.title
    assert after.sub == item.sub


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
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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


def test_topbar_opens_schedule_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """顶栏右侧的"定时任务"入口打开全局任务窗口(而不是只给一句提示)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    opened: list[dict[str, Any]] = []
    monkeypatch.setattr(
        main_mod,
        "ScheduleWindow",
        lambda _parent, **kwargs: opened.append(kwargs),
    )
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    assert app._schedule_btn.cget("text") == tr("topbar.nav_scheduled")
    app._on_open_schedules()

    assert len(opened) == 1
    assert opened[0]["backend"] is app.backend


def test_discovery_panel_scans_filters_and_imports_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发现窗口: 扫描、筛选、忽略/恢复与导入都作用到后端数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch, ask_text="空洞骑士")
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        assert [item.directory_id for item in panel._dirs] == ["dir-1", "dir-2"]
        assert len(panel._candidates) == 6
        # 未选中有效候选时"导入"不可用(候选 4 的路径已失效).
        panel._select_candidate("cand-4")
        assert str(panel._import_btn.cget("state")) == "disabled"

        panel._on_scan()
        _pump(app)
        assert tr("discovery.scanning") not in panel._summary_label.cget("text")
        assert panel._dirs[0].last_scan_label != ""

        # 筛选: 只看已忽略的候选, 再把它们恢复成待处理.
        panel._filter_box.set(CandidateFilter.IGNORED.label)
        panel._on_filter_change(CandidateFilter.IGNORED.label)
        assert set(panel._cand_rows) == {"cand-5"}
        panel._select_candidate("cand-5")
        panel._on_ignore()
        _pump(app)
        assert app.backend.list_candidates(status="ignored") == []

        # 导入一条候选: 后端新增游戏, 候选变为已导入.
        before = len(app.backend.list_games())
        panel._filter_box.set(CandidateFilter.NEW.label)
        panel._on_filter_change(CandidateFilter.NEW.label)
        panel._select_candidate("cand-2")
        panel._on_import()
        _pump(app)

        assert len(app.backend.list_games()) == before + 1
        imported = next(
            item for item in panel._candidates if item.candidate_id == "cand-2"
        )
        assert imported.status == "imported"
        assert imported.game_id is not None
    finally:
        app.destroy()


def test_discovery_panel_manages_monitored_directories(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """监控目录的新增校验: 非法路径给出后端原因, 合法路径写入列表."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    # 依次回答: 先给一个空路径(应被拒绝), 再给一个真实目录.
    _patch_dialogs(monkeypatch, ask_text_queue=["   ", str(tmp_path)])
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = DiscoveryPanel(
        ctk.CTkFrame(app),
        backend=app.backend,
        palette=Palette.for_theme(app._theme),
    )
    before = len(panel._dirs)

    panel._on_add_dir()
    _pump(app)
    assert len(panel._dirs) == before
    assert "不能为空" in panel._summary_label.cget("text")

    panel._on_add_dir()
    _pump(app)
    assert len(panel._dirs) == before + 1
    assert str(tmp_path) in {item.path for item in panel._dirs}

    # 停用后不再参与扫描, 但记录仍保留.
    panel._select_dir(panel._dirs[-1].directory_id)
    panel._on_toggle_dir()
    _pump(app)
    assert panel._dir_item() is not None
    assert panel._dir_item().enabled is False  # type: ignore[union-attr]


def test_discovery_panel_defaults_to_candidates_and_switches_pages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """发现窗口分成两页: 默认停在"探测结果", 可切到"监控目录".

    同时锁定回归: 候选操作只剩导入/忽略/修正路径, 不再有"加入监控"按钮。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import DiscoveryPage
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch)
    try:
        app = _new_app(DemoArchiveService(delay=0))
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )

        def current() -> DiscoveryPage:
            """读取当前页面(经函数返回避免 mypy 对属性做字面量收窄)."""
            return panel._page

        candidates = panel._page_frames[DiscoveryPage.CANDIDATES]
        monitored = panel._page_frames[DiscoveryPage.MONITORED]

        # 默认页 = 探测结果, 监控目录页未布局(grid_remove 后 grid_info 为空).
        assert current() is DiscoveryPage.CANDIDATES
        assert candidates.grid_info() != {}
        assert monitored.grid_info() == {}

        # 候选页的按钮恰好是导入/忽略/修正路径三个(没有"加入监控").
        expected = {
            tr("discovery.cand_import"),
            tr("discovery.cand_ignore"),
            tr("discovery.cand_relocate"),
        }
        assert set(_button_texts(candidates)) & expected == expected
        assert len(_button_texts(candidates)) == len(expected)

        panel._show_page(DiscoveryPage.MONITORED)
        assert current() is DiscoveryPage.MONITORED
        assert monitored.grid_info() != {}
        assert candidates.grid_info() == {}
        assert set(_button_texts(monitored)) >= {
            tr("discovery.dir_add"),
            tr("discovery.dir_edit"),
            tr("discovery.dir_remove"),
        }
        # "重新扫描"在页签行上, 两个页面都能用.
        assert tr("discovery.scan") in _button_texts(panel.frame)

        panel._show_page(DiscoveryPage.CANDIDATES)
        assert current() is DiscoveryPage.CANDIDATES
        assert candidates.grid_info() != {}
        assert monitored.grid_info() == {}
    finally:
        app.destroy()


def test_discovery_default_filter_is_pending_with_filter_aware_empty_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """默认筛选为"待处理"; 筛选没有匹配项时提示与该筛选相关而不是"暂无探测结果"."""
    from collections.abc import Sequence

    from archive_management.domain import GameCandidate
    from archive_management.infrastructure.database import Database
    from archive_management.services.platform_scan import LocalGameScanner
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter
    from archive_management.ui.palette import Palette
    from archive_management.ui.sql_backend import SqlArchiveService

    db = Database(tmp_path / "filter.db")
    db.migrate()
    games = tmp_path / "Games"
    (games / "Alpha").mkdir(parents=True)

    # 用替身探测: 无论本机装了多少平台游戏, 这次扫描都只有一条候选.
    def fake_scan(
        self: object, *, monitored: Sequence[str] = ()
    ) -> list[GameCandidate]:
        del self, monitored
        return [
            GameCandidate(
                name="Alpha",
                install_dir=str(games / "Alpha"),
                source="monitored",
                confidence="medium",
                reason_code="monitored_child",
                health="ok",
            )
        ]

    monkeypatch.setattr(LocalGameScanner, "scan", fake_scan)
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    service.add_monitored_directory(str(games))
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app), backend=service, palette=Palette.for_theme(app._theme)
        )
        assert panel._filter is CandidateFilter.NEW
        assert panel._filter_box.get() == CandidateFilter.NEW.label
        assert panel._cand_rows == {}  # 还没扫描过

        panel._on_scan()
        _pump(app)
        assert [item.status for item in panel._candidates] == ["new"]
        assert set(panel._cand_rows) == {panel._candidates[0].candidate_id}

        # 忽略唯一一条待处理项: 待处理筛选下列表变空, 但提示说得清是筛选造成的.
        target = panel._candidates[0]
        panel._select_candidate(target.candidate_id)
        panel._on_ignore()
        _pump(app)
        assert panel._cand_rows == {}
        texts = _label_texts(panel._cand_box)
        assert tr("discovery.empty_filtered", filter=CandidateFilter.NEW.label) in texts
        assert any("已忽略 1" in text for text in texts)
        assert tr("discovery.candidates_empty") not in texts

        # 切到"已忽略"就能看到刚忽略的那条(筛选本身工作正常).
        panel._on_filter_change(CandidateFilter.IGNORED.label)
        _pump(app)
        assert set(panel._cand_rows) == {target.candidate_id}
        assert "已忽略 1" in str(panel._detail_label.cget("text"))
    finally:
        app.destroy()


def test_default_page_is_home_and_detail_round_trips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主页是主窗口内的一页(不是弹窗): 默认显示主页, 与详情页可来回切换."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import AppPage, HomeSection

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page

    # 软件打开后默认停在游戏主页(游戏库分区): 主页可见, 详情页收起.
    assert _current_page(app) is AppPage.HOME
    assert page.frame.grid_info() != {}
    assert app._content.grid_info() == {}
    assert page._section is HomeSection.LIBRARY
    assert page._library.grid_info() != {}
    assert page._discovery.frame.grid_info() == {}
    # 侧边栏已移除: 工作区入口都在顶栏右上角.
    assert not hasattr(app, "_games_container")
    assert not hasattr(app, "_status_card")
    assert app._add_game_btn.cget("text") == tr("topbar.add_game")

    # 主页是整页布局: 分区页签、表头列与数据行齐全, 没有弹窗式的"关闭"按钮.
    section_tabs = _button_texts(page.frame)
    assert HomeSection.LIBRARY.label in section_tabs
    assert HomeSection.DISCOVERY.label in section_tabs
    headers = _label_texts(page.frame)
    for key in (
        "home.col_name",
        "home.col_platform",
        "home.col_locations",
        "home.col_backups",
        "home.col_last_backup",
        "home.col_state",
    ):
        assert tr(key) in headers
    assert set(page._rows) == {"outer-wilds", "shanhai", "endless-space"}
    assert not hasattr(page, "_close_btn")

    # 打开详情: 切到详情页, 主页收起, 顶栏出现"← 游戏主页".
    assert app._back_btn.grid_info() == {}
    app._open_game_detail("outer-wilds")
    _pump(app)
    assert _current_page(app) is AppPage.DETAIL
    assert app._content.grid_info() != {}
    assert page.frame.grid_info() == {}
    assert app._title_label.cget("text") == "星际拓荒"
    assert app._back_btn.grid_info() != {}

    # 顶栏左侧的"← 游戏主页"返回主页(按钮随即隐藏).
    assert app._back_btn.cget("text") == tr("page.back_home")
    app._on_back_home()
    _pump(app)
    assert _current_page(app) is AppPage.HOME
    assert page.frame.grid_info() != {}
    assert app._back_btn.grid_info() == {}


def test_home_page_switches_between_library_and_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """游戏发现作为主页的一个分区: 切过去能看到探测结果与监控目录."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import DiscoveryPage, HomeSection

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    panel = page._discovery
    assert len(panel._candidates) == 6
    assert [item.directory_id for item in panel._dirs] == ["dir-1", "dir-2"]

    page._show_section(HomeSection.DISCOVERY)
    _pump(app)
    assert page._section is HomeSection.DISCOVERY
    assert panel.frame.grid_info() != {}
    assert page._library.grid_info() == {}
    # 发现分区内部仍是"探测结果 / 监控目录"两页, 默认停在探测结果.
    assert panel._page is DiscoveryPage.CANDIDATES
    assert panel._tabs[DiscoveryPage.CANDIDATES].cget("text") == (
        tr("discovery.page_candidates")
    )

    page._show_section(HomeSection.LIBRARY)
    _pump(app)
    assert page._library.grid_info() != {}
    assert panel.frame.grid_info() == {}


def test_home_page_imports_candidate_and_refreshes_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """在发现分区导入候选后, 游戏库列表立即出现这款游戏(两个分区共享数据)."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import HomeSection

    _patch_dialogs(monkeypatch, import_result=("空洞骑士", ()))
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    before = len(app.backend.list_games())

    page._show_section(HomeSection.DISCOVERY)
    _pump(app)
    panel = page._discovery
    panel._select_candidate("cand-2")
    panel._on_import()
    _pump(app)

    assert len(app.backend.list_games()) == before + 1
    # 监控目录来源不支持自动探测: 对话框里没有路径可确认, 就不写存档位置.
    imported = next(
        game for game in app.backend.list_games() if game.name == "空洞骑士"
    )
    assert imported.saved_paths == 0
    assert app.backend.list_locations(imported.game_id) == []
    # 导入后游戏库已经包含新游戏(回到游戏库分区即可看到).
    page._show_section(HomeSection.LIBRARY)
    _pump(app)
    assert "空洞骑士" in {item.name for item in page._board.games}


def test_home_page_supports_poster_mode_and_paging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """海报模式显示封面占位/备份角标/名称/最近活动; 分页按每页条数切页."""
    from archive_management.domain import HomeLayout
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    # 造 35 款游戏: 默认每页 30 条 -> 2 页.
    for index in range(32):
        app.backend.add_game(f"批量游戏{index:02d}")
    page.reload()
    _pump(app)

    # 每页选择在右下角翻页控件的左边.
    assert page._page_size_label.cget("text") == tr("home.page_size")
    assert page._page_size_box.winfo_manager() != ""
    pack_order = page._pager.pack_slaves()
    assert pack_order.index(page._page_size_box) < pack_order.index(page._prev_btn)

    assert len(page._board.games) == 35
    assert page._page_label.cget("text") == tr("home.page_indicator", page=1, pages=2)
    assert len(page._rows) == 30
    assert str(page._prev_btn.cget("state")) == "disabled"
    assert str(page._next_btn.cget("state")) == "normal"

    page._on_next_page()
    _pump(app)
    assert page._page_label.cget("text") == tr("home.page_indicator", page=2, pages=2)
    assert len(page._rows) == 5
    assert str(page._next_btn.cget("state")) == "disabled"

    # 每页 60 条: 一页装得下全部游戏.
    page._on_page_size_change("60")
    _pump(app)
    assert page._filter.page_size == 60
    assert page._page_label.cget("text") == tr("home.page_indicator", page=1, pages=1)
    assert len(page._rows) == 35

    # 海报模式: 竖屏封面(文字占位) + 右下角备份数角标 + 名称 + 最近活动.
    page._on_layout_change(HomeLayout.POSTER.label)
    _pump(app)
    assert page._filter.layout is HomeLayout.POSTER
    assert page._head.grid_info() == {}
    card_texts = _label_texts(page._rows["outer-wilds"])
    assert "星际" in card_texts
    assert "星际拓荒" in card_texts
    assert any(text.startswith("最近活动") for text in card_texts)
    assert tr("home.poster_backups", count=5) in card_texts

    # 封面是竖屏(高度明显大于宽度), 角标贴在封面的右下角.
    card = page._rows["outer-wilds"]
    cover = card.winfo_children()[0]
    assert cover.winfo_reqheight() > 140
    # 海报网格左对齐: 网格从滚动区左上角开始铺(列不分配权重, 卡片不会被
    # 挤到行中间), 因此所有卡片里最靠左/最靠上的那张偏移就是内边距 4.
    cards = list(page._rows.values())
    assert min(item.winfo_x() for item in cards) == 4
    assert min(item.winfo_y() for item in cards) == 4
    badge = next(
        child
        for child in cover.winfo_children()
        if child.cget("text") == tr("home.poster_backups", count=5)
    )
    sticky = str(badge.grid_info()["sticky"])
    assert "s" in sticky
    assert "e" in sticky

    # 展示偏好会持久化: 重新读取主页仍是海报 + 每页 60 条.
    reloaded = app.backend.load_home()
    assert reloaded.filter.layout is HomeLayout.POSTER
    assert reloaded.filter.page_size == 60


def test_home_page_follows_theme_switch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """回归: 切浅/深主题时主页也要换色(主页控件不经过 UiKit 的注册重绘)."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import HomeSection
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    assert page._palette is app.p

    app._on_toggle_theme()
    _pump(app)
    light = Palette.for_theme("light")
    assert app._theme == "light"
    assert page._palette is app.p
    assert page.frame.cget("fg_color") == light.background
    assert page._discovery.frame.cget("fg_color") == light.background
    # 重建后数据仍在: 列表有内容, 分区与筛选条件保持不变.
    assert page._rows
    assert page._section is HomeSection.LIBRARY
    assert page._filter.view is HomeView.ALL

    app._on_toggle_theme()
    _pump(app)
    dark = Palette.for_theme("dark")
    assert page.frame.cget("fg_color") == dark.background
    # 主题切换不影响后端数据.
    assert len(app.backend.list_games()) == 3


def test_archived_game_keeps_only_the_documented_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档后主页只留打开详情/取消归档/删除入口, 管理窗口里只留删除游戏."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.manage_window import ManageGameWindow

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    page._select("shanhai")
    page._on_archive()
    _pump(app)
    page._on_view(HomeView.ARCHIVED)
    _pump(app)
    page._select("shanhai")

    def state(button: Any) -> str:
        """读取按钮的启用状态."""
        return str(button.cget("state"))

    assert state(page._detail_btn) == "normal"
    assert state(page._archive_btn) == "normal"
    # 管理窗口是归档游戏删除自己的唯一入口, 因此仍然可用, 但按钮改名为"删除游戏".
    assert state(page._manage_btn) == "normal"
    assert page._manage_btn.cget("text") == tr("home.action_delete")
    assert state(page._backup_btn) == "disabled"
    assert state(page._location_btn) == "disabled"
    assert state(page._tags_btn) == "disabled"
    assert state(page._enable_btn) == "disabled"

    # 绕过按钮直接调用同样要被拦下并给出原因.
    page._on_backup()
    assert page._summary_label.cget("text") == tr(
        "home.archived_blocked", name="山海旅人"
    )

    window = ManageGameWindow(
        app,
        backend=app.backend,
        palette=app.p,
        game_id="shanhai",
        name="山海",
        enabled=False,
        backup_location="—",
        on_change=lambda: None,
        archived=True,
    )
    assert state(window._delete_btn) == "normal"
    assert state(window._rename_btn) == "disabled"
    assert state(window._toggle_btn) == "disabled"
    assert state(window._schedule_btn) == "disabled"
    assert {state(button) for button in window._location_buttons} == {"disabled"}
    assert state(window._delete_origin_btn) == "disabled"
    window.close()


def test_hotkey_is_ignored_while_the_game_is_disabled(
    monkeypatch: pytest.MonkeyPatch, audit_log: list[str]
) -> None:
    """停用游戏的快捷键不触发备份, 只给出反馈(只有启用的那一款响应快捷键)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app.backend.set_game_enabled("shanhai", False)
    app._select_game("shanhai")
    before = len(app.backend.list_backups("shanhai"))

    # "save_now" 即 services.hotkeys.ACTION_SAVE_NOW, 这里按主窗口收到的消息投递.
    app._messages.put(("hotkey", "save_now"))
    app._poll_messages()

    assert len(app.backend.list_backups("shanhai")) == before
    assert "hotkey.skipped" in " ".join(audit_log)


def test_discovery_import_confirms_paths_and_probes_artwork(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """导入时确认存档路径并触发封面探测: 行内已带推断结果, 状态栏给出提示."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    notices: list[str] = []
    _patch_dialogs(monkeypatch, import_result=("星露谷", ("C:\\Saves", "D:\\Other")))
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = DiscoveryPanel(
        ctk.CTkFrame(app),
        backend=app.backend,
        palette=Palette.for_theme(app._theme),
        on_notice=notices.append,
    )

    # 探测结果行里直接带着推断出来的存档路径与"平台不支持"的说明.
    steam_row = next(
        item for item in panel._candidates if item.candidate_id == "cand-6"
    )
    assert steam_row.save_supported is True
    assert next(path.path for path in steam_row.save_paths).endswith("Saves")
    assert panel._save_lines(steam_row).count("\n") == len(steam_row.save_paths)
    monitored_row = next(
        item for item in panel._candidates if item.candidate_id == "cand-2"
    )
    assert monitored_row.save_supported is False
    assert monitored_row.save_label == tr(
        "discovery.save_unsupported", platform=monitored_row.source_label
    )

    panel._select_candidate("cand-6")
    panel._on_import()
    _pump(app)

    imported = next(game for game in app.backend.list_games() if game.name == "星露谷")
    assert imported.saved_paths == 2
    assert [item.path for item in app.backend.list_locations(imported.game_id)] == [
        "C:\\Saves",
        "D:\\Other",
    ]
    # 页面提示写清登记了几条存档位置.
    assert panel._summary_label.cget("text") == tr(
        "result.game_added_with_paths", name="星露谷", count=2
    )
    # 导入成功后才开始探测封面, 并把这件事写到左下角状态栏(页面不弹窗).
    assert notices == [tr("discovery.artwork_started", name="星露谷")]


def test_artwork_landing_refreshes_the_home_page(tmp_path: Path) -> None:
    """封面/图标是导入后由后台补的: 数据版本一变, 主页要重读才能把图标显示出来."""
    from PIL import Image

    from archive_management.domain import ArtworkKind
    from archive_management.ui.demo_backend import DemoArchiveService

    icon = tmp_path / "icon.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(icon)

    class _LateIconService(DemoArchiveService):
        """图标一开始没有, 后台探测完成后才出现."""

        ready = False

        def artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
            """探测完成后才给出图标路径."""
            if kind == "icon" and self.ready and game_id == "outer-wilds":
                return str(icon)
            return ""

    service = _LateIconService(delay=0)
    app = gui_app(_new_app, service)
    _pump(app)
    page = app._home_page
    assert "icon:outer-wilds" not in page._artwork_images

    # 后台探测完成 → 数据版本变化 → 主窗口重载: 主页必须跟着重读.
    service.ready = True
    app._reload_data()
    _pump(app)

    assert "icon:outer-wilds" in page._artwork_images


def test_settings_window_language_switch_closes_the_window() -> None:
    """设置窗口里换语言: 交给主窗口处理后关掉自己(主窗口会整体重建)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    switched: list[str] = []

    def apply(locale: str) -> str | None:
        switched.append(locale)
        return None

    window._on_apply_language = apply
    window._on_language_selected("English")
    _pump(app)

    assert switched == ["en"]
    assert window._window.winfo_exists() == 0


def test_discovery_rows_show_the_localized_name() -> None:
    """探测结果行直接显示当前语言的译名, 并把探测到的原名放在括号里."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    panel = DiscoveryPanel(
        ctk.CTkFrame(app), backend=app.backend, palette=Palette.for_theme(app._theme)
    )
    row = next(item for item in panel._candidates if item.candidate_id == "cand-6")

    assert row.localized_name == "星露谷物语"
    assert panel._candidate_title(row) == "星露谷物语  (Stardew Valley)"


def test_startup_fills_localized_names_once() -> None:
    """启动时补一次译名探测, 且不是强制刷新(走缓存, 只对缺条目的游戏联网)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    class _RecordingNames(DemoArchiveService):
        """记录译名预取调用(演示后端自身不联网)."""

        def __init__(self) -> None:
            super().__init__(delay=0)
            self.calls: list[bool] = []

        def prefetch_names(self, *, refresh: bool = False) -> None:
            """记下每次调用是否要求强制刷新."""
            self.calls.append(refresh)

    service = _RecordingNames()
    app = gui_app(_new_app, service)
    _pump(app)

    assert service.calls == [False]


def test_switching_language_rebuilds_the_ui_and_reprobes_names(
    tmp_path: Path,
) -> None:
    """切换语言: 界面按新语言整体重建, 译名重新探测, 选择写回配置文件."""
    from archive_management.config import load_config
    from archive_management.i18n import current_locale
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    app = gui_app(_new_app, DemoArchiveService(delay=0), paths=paths)
    _pump(app)
    old_page = app._home_page
    assert app._language == "zh-CN"

    error = app._on_language_change("en")
    _pump(app)

    assert error is None
    assert current_locale() == "en"
    assert app._language == "en"
    # 文案是构建时取的: 换语言必须整体重建, 主页对象会换成新的.
    assert app._home_page is not old_page
    assert tr("settings.language") == "Language"
    assert app._feedback_label.cget("text") == tr(
        "settings.language_switched", language="English"
    )
    # 选择写回配置文件: 重启后仍是英文.
    assert load_config(paths.config_path).language == "en"


def test_icon_slot_survives_switching_and_deleting_games(tmp_path: Path) -> None:
    """回归: 换游戏/删游戏时图标位要能回落色块, 不能留着指向已销毁图片的标签.

    症状: 打开过一次带图标的游戏再返回主页, 之后点任何游戏都抛
    ``image "pyimageN" doesn't exist``(旧 CTkImage 被回收, 标签的 image 选项还指着它),
    删除带图标的游戏后则该位置直接空白。
    """
    from PIL import Image

    from archive_management.domain import ArtworkKind
    from archive_management.ui.demo_backend import DemoArchiveService

    icon = tmp_path / "icon.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(icon)

    class _IconService(DemoArchiveService):
        """只有"星际拓荒"有图标, 其余游戏回落色块."""

        def artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
            """按游戏 id 决定有没有图标."""
            if kind == "icon" and game_id == "outer-wilds":
                return str(icon)
            return ""

    app = gui_app(_new_app, _IconService(delay=0))
    _pump(app)

    app._open_game_detail("outer-wilds")
    _pump(app)
    assert app._hero_icon is not None

    app._on_back_home()
    _pump(app)
    # 换到没有图标的游戏: 不抛异常, 图标位回落成首字色块.
    app._open_game_detail("shanhai")
    _pump(app)
    assert app._hero_icon is None
    assert app._hero_tile.cget("text") == "山"
    # 图标位必须真的被清空(用 image=None 时 Tk 侧的图片选项不会变, 旧图会留在色块位置).
    assert app._hero_tile.cget("image") == ""

    # 删除带图标的游戏: 图标位同样不能残留图片.
    app._open_game_detail("outer-wilds")
    _pump(app)
    assert app._hero_icon is not None
    app.backend.delete_game("outer-wilds")
    app._refresh_after_manage()
    _pump(app)
    assert app._hero_icon is None
    assert app._hero_tile.cget("image") == ""


def test_poster_card_uses_the_cached_cover_when_available(tmp_path: Path) -> None:
    """海报卡片有缓存封面就加载图片, 解码失败时回落文字占位."""
    from PIL import Image

    from archive_management.domain import ArtworkKind, HomeLayout
    from archive_management.ui.demo_backend import DemoArchiveService

    good = tmp_path / "cover.png"
    Image.new("RGB", (10, 10), (0, 0, 0)).save(good)
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")

    class _CoverService(DemoArchiveService):
        """演示后端 + 固定图片路径(省去真实下载)."""

        def __init__(self, cover: str) -> None:
            super().__init__(delay=0)
            self.cover = cover

        def artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
            """封面与图标都用同一个预设文件."""
            return self.cover

    app = gui_app(_new_app, _CoverService(str(good)))
    _pump(app)
    page = app._home_page
    # 列表行头像也会用上图标(没有图标才回落色块圆点).
    assert "icon:outer-wilds" in page._artwork_images
    page._on_layout_change(HomeLayout.POSTER.label)
    _pump(app)
    assert "cover:outer-wilds" in page._artwork_images

    # 换成损坏的文件后不再进图片缓存(界面回落名称占位), 且不影响列表渲染.
    app.backend.cover = str(broken)
    page._artwork_images.clear()
    page._on_layout_change(HomeLayout.LIST.label)
    page._on_layout_change(HomeLayout.POSTER.label)
    _pump(app)
    assert page._artwork_images == {}
    assert set(page._rows) == {"outer-wilds", "shanhai", "endless-space"}


def test_home_enable_button_keeps_a_single_active_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主页启用按钮: 启用一款会自动停用另一款, 再点一次则停用."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    # 先把演示数据的启用态清干净, 否则“点一次”到底是启用还是停用取决于种子数据.
    for game in app.backend.list_games():
        app.backend.set_game_enabled(game.game_id, False)
    page.reload()
    _pump(app)

    page._select("outer-wilds")
    page._on_toggle_enabled()
    _pump(app)
    assert [game.game_id for game in app.backend.list_games() if game.enabled] == [
        "outer-wilds"
    ]
    assert page._summary_label.cget("text") == tr("home.enabled", name="星际拓荒")

    page._select("shanhai")
    page._on_toggle_enabled()
    _pump(app)
    assert [game.game_id for game in app.backend.list_games() if game.enabled] == [
        "shanhai"
    ]
    assert page._summary_label.cget("text") == tr(
        "home.enabled_replaced", name="山海旅人", other="星际拓荒"
    )

    page._on_toggle_enabled()
    _pump(app)
    assert [game.game_id for game in app.backend.list_games() if game.enabled] == []
    assert page._summary_label.cget("text") == tr("home.disabled", name="山海旅人")


def test_home_page_filters_games_and_runs_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主页: 视图/搜索筛选、归档、标签、备份与"打开详情"都作用到后端数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import AppPage

    # 标签输入含重复项与空格: 由用例层清理后落库.
    _patch_dialogs(monkeypatch, ask_text="解谜, 解谜")
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page

    # 默认视图是"全部游戏", 页签带数量, 动作按钮齐全.
    assert set(page._rows) == {"outer-wilds", "shanhai", "endless-space"}
    assert page._tabs[HomeView.ALL].cget("text") == f"{tr('home.view_all')} (3)"
    assert page._detail_btn.cget("text") == tr("home.action_detail")
    assert page._archive_btn.cget("text") == tr("home.action_archive")

    # 待处理视图: 只留下没有存档位置的无尽太空, 且不能直接备份.
    page._on_view(HomeView.PENDING)
    _pump(app)
    assert set(page._rows) == {"endless-space"}
    assert str(page._backup_btn.cget("state")) == "disabled"

    # 搜索: 只匹配名称包含关键字的游戏; 清除后回到全部.
    page._on_view(HomeView.ALL)
    page._search_entry.insert(0, "山海")
    page._submit_search()
    _pump(app)
    assert set(page._rows) == {"shanhai"}
    assert page._filter.search == "山海"
    page._clear_search()
    _pump(app)
    assert set(page._rows) == {"outer-wilds", "shanhai", "endless-space"}
    assert page._filter.view is HomeView.ALL

    # 归档: 从默认视图消失, 只在"已归档"里出现; 页面不跳转, 数据也不删除.
    page._select("endless-space")
    page._on_archive()
    _pump(app)
    assert set(page._rows) == {"outer-wilds", "shanhai"}
    assert page._summary_label.cget("text") == tr("home.archived", name="无尽太空")
    assert _current_page(app) is AppPage.HOME
    # 归档只是"从主页收起来": 游戏记录仍在库里, 详情页照旧可以打开.
    assert "无尽太空" in {item.name for item in app.backend.list_games()}

    page._on_view(HomeView.ARCHIVED)
    _pump(app)
    assert set(page._rows) == {"endless-space"}
    assert page._archive_btn.cget("text") == tr("home.action_unarchive")
    page._select("endless-space")
    page._on_archive()
    _pump(app)
    assert page._rows == {}

    # 标签: 清理后的标签出现在列表行的分类标签上.
    page._on_view(HomeView.ALL)
    page._select("shanhai")
    page._on_edit_tags()
    _pump(app)
    tagged = _home_item(page, "shanhai")
    assert tagged.tags == ("解谜",)
    assert any("解谜" in text for text in _label_texts(page._list_box))

    # 立即备份: 有存档位置的游戏备份数增加, 主页计数随之刷新.
    page._on_backup()
    _pump(app)
    backed_up = _home_item(page, "shanhai")
    assert backed_up.backup_count == tagged.backup_count + 1
    assert page._summary_label.cget("text") == tr("home.backed_up", name="山海旅人")

    # 打开详情: 切到详情页并选中该游戏.
    page._select("outer-wilds")
    page._on_detail()
    _pump(app)
    assert _current_page(app) is AppPage.DETAIL
    assert app._game_id == "outer-wilds"
    assert app._title_label.cget("text") == "星际拓荒"


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


def test_game_settings_can_configure_schedule_per_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """游戏设置里的"定时备份"按钮只改当前游戏的配置."""

    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, schedule_result=("30m", "2"))
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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


def test_topbar_carries_global_entries_and_no_sidebar() -> None:
    """顶栏右侧是全局入口(添加游戏/定时任务/设置); 侧边栏与其内容已移除."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)

    assert not hasattr(app, "_sync_label")
    assert not hasattr(app, "theme_btn")
    assert not hasattr(app, "_sync_labels")
    # 全局入口都在顶栏: 添加游戏 + 定时任务 + 设置.
    assert app._add_game_btn.cget("text") == tr("topbar.add_game")
    assert app._schedule_btn.cget("text") == tr("topbar.nav_scheduled")
    assert app._settings_btn.cget("text") == tr("topbar.nav_settings")
    topbar_texts = _button_texts(app._add_game_btn.master)
    assert tr("topbar.nav_settings") in topbar_texts
    # 侧边栏的"我的游戏"列表、工作区标题与"全部备份"入口都不在了.
    assert not hasattr(app, "_games_container")
    assert not hasattr(app, "_status_card")
    assert not hasattr(app, "_add_game_label")
    assert tr("sidebar.my_games") not in _label_texts(app)
    assert "全部备份" not in _label_texts(app)
    # 备份服务状态挪到右下角的状态条里.
    assert tr("status.service_ok") in app._service_label.cget("text")


def _settings_window(app: ArchiveApp, applied: list[tuple[str, str]]) -> Any:
    """直接构造设置窗口(不走主窗口的窗口复用逻辑), 便于测试录制交互."""
    from archive_management.ui.settings_window import SettingsWindow

    def apply(action: str, accelerator: str) -> str | None:
        applied.append((action, accelerator))
        return None

    return SettingsWindow(
        app,
        palette=app.p,
        theme=app._theme,
        language=app._language,
        shortcuts=app._shortcuts,
        on_toggle_theme=app._on_toggle_theme,
        on_apply_language=app._on_language_change,
        on_apply_shortcut=apply,
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )


def _press(window: Any, keysym: str, keycode: int) -> None:
    """模拟按下并松开一个键(录制只依赖 keysym/keycode)."""
    window._on_key_press(SimpleNamespace(keysym=keysym, keycode=keycode))
    window._on_key_release(SimpleNamespace(keysym=keysym, keycode=keycode))


def _wait_for(
    app: ArchiveApp, predicate: Callable[[], bool], seconds: float = 3.0
) -> bool:
    """轮询等待条件成立; 录制收尾用的是 ``after`` 计时器, 需要真实事件循环."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _pump(app)
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_settings_window_holds_theme_and_hotkey_shortcuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置窗口提供主题切换与两个快捷键, 且不再展示定时任务配置."""

    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    applied: list[tuple[str, str]] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    before = app._theme
    window = _settings_window(app, applied)

    # 定时任务相关的信息不在设置里.
    labels = _label_texts(window._container)
    assert all("定时" not in text for text in labels)
    assert any("外观" in text for text in labels)
    # 两个快捷键按钮显示当前组合(可读写法), 而不是 pynput 的原始语法.
    assert window.shortcut_text(ACTION_SAVE_NOW) == format_accelerator(
        DEFAULT_SAVE_ACCELERATOR
    )
    assert window.shortcut_text(ACTION_CREATE_BRANCH) == format_accelerator(
        DEFAULT_BRANCH_ACCELERATOR
    )

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


def test_settings_window_records_a_pressed_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点击按键区域后按下组合键即录制: 组合键交给回调并立即显示在按钮上."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    applied: list[tuple[str, str]] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, applied)

    window._toggle_capture(ACTION_CREATE_BRANCH)
    assert window.shortcut_text(ACTION_CREATE_BRANCH) == tr("settings.recording")

    _press(window, "Control_L", 17)
    _press(window, "Win_L", 91)
    _press(window, "Z", 90)

    assert _wait_for(app, lambda: bool(applied))
    assert applied == [(ACTION_CREATE_BRANCH, "<win>+<ctrl>+z")]
    assert window.shortcut_text(ACTION_CREATE_BRANCH) == format_accelerator(
        "<win>+<ctrl>+z"
    )
    assert window._shortcut_error.cget("text") == ""


def test_settings_window_rejects_a_single_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """只按修饰键或只按字母都不算合法组合: 给出原因且不修改快捷键."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    applied: list[tuple[str, str]] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, applied)

    window._toggle_capture(ACTION_SAVE_NOW)
    _press(window, "Win_L", 91)

    assert _wait_for(app, lambda: bool(window._shortcut_error.cget("text")))
    assert applied == []
    assert window._shortcut_error.cget("text") == tr("hotkey.err_no_letter")
    assert window.shortcut_text(ACTION_SAVE_NOW) == format_accelerator(
        DEFAULT_SAVE_ACCELERATOR
    )

    # 只按字母缺少修饰键, 同样不生效.
    window._toggle_capture(ACTION_SAVE_NOW)
    _press(window, "s", 83)

    assert _wait_for(app, lambda: bool(window._shortcut_error.cget("text")))
    assert applied == []
    assert window._shortcut_error.cget("text") == tr("hotkey.err_no_modifier")

    # 数字键不在白名单里: 直接提示"不支持的按键".
    window._toggle_capture(ACTION_SAVE_NOW)
    _press(window, "1", 49)

    assert _wait_for(
        app,
        lambda: window._shortcut_error.cget("text") == tr("hotkey.err_unknown_key"),
    )


def test_custom_hotkey_is_persisted_and_reloaded(tmp_path: Path) -> None:
    """自定义快捷键写入配置文件, 下次启动仍然生效."""
    from archive_management.config import load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    hotkeys = GlobalHotkeyService(backend=_RecordingBackend())
    app = gui_app(_new_app, DemoArchiveService(delay=0), hotkeys=hotkeys, paths=paths)
    try:
        _pump(app)
        assert app._apply_shortcut(ACTION_CREATE_BRANCH, "<win>+<ctrl>+z") is None
        assert app._shortcuts[ACTION_CREATE_BRANCH] == "<win>+<ctrl>+z"
        assert load_config(paths.config_path).hotkeys.branch == "<win>+<ctrl>+z"
    finally:
        app.destroy()

    reloaded = _new_app(
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
        paths=paths,
    )
    assert reloaded._shortcuts[ACTION_CREATE_BRANCH] == "<win>+<ctrl>+z"
    assert reloaded._shortcuts[ACTION_SAVE_NOW] == DEFAULT_SAVE_ACCELERATOR


def test_invalid_config_is_reset_to_defaults_on_startup(tmp_path: Path) -> None:
    """启动时发现配置内容非法: 还原为默认值并提示用户, 而不是静默回落."""
    from archive_management.config import load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    paths.config_path.write_text('{"version": 1, "theme": "neon"}', encoding="utf-8")
    app = gui_app(
        _new_app,
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
        paths=paths,
    )
    assert app._shortcuts[ACTION_SAVE_NOW] == DEFAULT_SAVE_ACCELERATOR
    assert app._shortcuts[ACTION_CREATE_BRANCH] == DEFAULT_BRANCH_ACCELERATOR
    assert app._last_feedback[1] == tr("config.reset")
    assert (paths.config_dir / "config.json.invalid").is_file()
    assert load_config(paths.config_path).hotkeys.save == DEFAULT_SAVE_ACCELERATOR


def test_failed_hotkey_registration_keeps_the_previous_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端不可用时注册失败: 给出原因并保留原组合, 不让用户无声地失去快捷键."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    failure = app._apply_shortcut(ACTION_SAVE_NOW, "<win>+<ctrl>+q")

    assert failure is not None
    assert app._shortcuts[ACTION_SAVE_NOW] == DEFAULT_SAVE_ACCELERATOR


def test_task_card_no_longer_shows_the_hotkey() -> None:
    """任务状态卡不再展示快捷键(它属于全局设置, 统一在设置窗口里维护)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._select_game("outer-wilds")
    _pump(app)

    assert not hasattr(app, "_task_shortcut")
    assert not hasattr(app, "_shortcut_text")
    labels = _label_texts(app._task_hint.master)
    assert all(tr("task.shortcut") not in text for text in labels)


def test_branch_hotkey_creates_a_branch_without_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全局快捷键创建分支不弹窗: 直接使用默认分支名."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)

    def forbidden(*_args: Any, **_kwargs: Any) -> str:
        raise AssertionError("全局快捷键不应弹出分支名对话框")

    monkeypatch.setattr(main_mod, "ask_branch_name", forbidden)
    app = gui_app(
        _new_app,
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
    )
    _pump(app)
    app._select_game("shanhai")
    app._select_backup(app._items[0])

    # 快捷键回调只投递消息, 由主线程轮询后执行.
    app._request_hotkey_branch()
    _drain(app)

    created = app._items[-1]
    assert created.branch_name == tr("dialog.branch_default")
    assert app._hotkeys.accelerators()[ACTION_CREATE_BRANCH] == (
        DEFAULT_BRANCH_ACCELERATOR
    )


def test_branch_default_name_is_localized(monkeypatch: pytest.MonkeyPatch) -> None:
    """创建分支的默认名称是本地化文案(中文"分支", 英文"Branch")."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, branch_name=tr("dialog.branch_default"))
    assert tr("dialog.branch_default") == "分支"
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._select_game("shanhai")
    app._select_backup(app._items[0])

    app._on_branch()
    _drain(app)

    created = app._items[-1]
    assert created.title == "分支"
    assert created.branch_name == "分支"


def test_task_card_wraps_long_paths() -> None:
    """任务卡里的长文案(备份目标/期间)自动换行而不是被裁掉."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._select_game("outer-wilds")
    _pump(app)

    for label in (app._task_name_label, app._task_next, app._task_target):
        assert int(label.cget("wraplength")) > 0
        assert label.cget("justify") == "left"
    assert app._task_target.cget("text")
    assert len(app._task_name_label.cget("text")) < 40


def test_task_card_state_follows_schedule_state() -> None:
    """任务卡的状态文案反映定时备份的启用/暂停/未配置, 而不是当前有无操作."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
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


def test_backup_restore_branch_export_report_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主窗口动作按钮点击后进入成功反馈并复位忙碌状态."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import FeedbackKind

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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


def test_deleting_last_game_clears_hero_panel(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """回归: 删掉唯一游戏后概要区/标题行不得再显示被删游戏."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, confirm=True)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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


def test_reload_after_external_delete_clears_panels() -> None:
    """回归: 游戏在别处被删掉后, 轮询触发的重载也要把界面清空."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    for game in list(app.backend.list_games()):
        app.backend.delete_game(game.game_id)

    app._reload_data()
    _pump(app)

    assert app._game_id is None
    assert app._hero_name_label.cget("text") == "未选择游戏"
    assert app._items == []


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


def test_task_panel_is_renamed_and_has_no_schedule_editor() -> None:
    """任务卡显示的是任务状态, 定时配置已移到游戏设置/定时任务窗口."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    assert app._task_hint.cget("text") == tr("task.hint")
    assert not hasattr(app, "_task_edit_btn")


# ---------------------------------------------------------------- SQLite 后端


def test_sql_backend_backup_creates_node_and_unfinished_actions_report_clearly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实后端: 备份落地为节点, 尚未实现的导出给出明确提示而非崩溃."""
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
        items = _service_backups(service, game.game_id)
        assert len(items) == 1
        assert items[0].verified is True

        # "恢复到此节点"= 把备份内容写回原始存档, 再把当前节点移到这里.
        app._select_backup(items[0])
        app._on_restore()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        assert "当前节点" in app._last_feedback[1]
        assert _service_backups(service, game.game_id)[0].is_current is True

        # 导出尚未实现, 当前必须给出明确提示而不是静默成功.
        app._on_export()
        _drain(app)
        assert _feedback_kind(app) == FeedbackKind.ERROR
        assert app._last_feedback[1] == tr("error.not_available")
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
        items = _service_backups(service, game.game_id)
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
        items = _service_backups(service, game.game_id)
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
        items = _service_backups(service, game.game_id)
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
        items = _service_backups(service, game.game_id)
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
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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


def test_manage_window_delete_original_rejects_wrong_name(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """确认名称不匹配时不删除任何内容, 位置列表保持不变."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.manage_window import ManageGameWindow

    _patch_dialogs(monkeypatch, ask_text="随便打个名字")
    app = gui_app(_new_app, DemoArchiveService(delay=0))
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
