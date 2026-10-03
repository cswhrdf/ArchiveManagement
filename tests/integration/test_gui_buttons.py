"""页面按钮功能测试.

在可用图形环境下真实触发主窗口与管理窗口的各按钮, 验证点击不抛
``TclError``、不留下未复位状态, 并产生符合预期的反馈/数据变化。本
模块集中覆盖两类回归: (1) 动态列表重建后 ``UiKit`` 主题重绘不再命中
已销毁控件; (2) 真实 SQLite 后端的关键动作(备份/恢复/导出)端到端跑通,
包括被取消、被拦下时"什么都不写"。无 tkinter/图形环境自动跳过。
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Callable, Sequence
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

import archive_management.ui.dialogs as dialogs_mod
import archive_management.ui.discovery_page as disc_mod
import archive_management.ui.home_page as home_page_mod
import archive_management.ui.main_window as main_mod
import archive_management.ui.manage_window as mgr_mod
import archive_management.ui.schedule_window as sched_mod
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
)
from archive_management.domain import HomeView
from archive_management.exceptions import HotkeyError
from archive_management.i18n import current_locale, set_locale, tr
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
from archive_management.services.pathcheck import normalize_path
from archive_management.ui.backend import ArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import (
    BatchExportChoice,
    BatchImportSelection,
    FeedbackKind,
    ImportChoice,
)
from archive_management.ui.widgets import scaled_px, scrollbar_needed

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
    tags_result: tuple[str, ...] | None = ("解谜",),
    export_path: str | None = None,
    import_package_path: str | None = None,
    import_choice: ImportChoice | None = None,
    export_batch_choice: BatchExportChoice | None = None,
    batch_import_choice: BatchImportSelection | None = None,
) -> None:
    """把模态对话框替换为自动应答, 避免 wait_window 阻塞测试线程.

    需要输入路径/文本的测试应通过 ``ask_text`` 显式传入由 ``tmp_path``
    派生的跨平台路径; 未触发文本输入的动作无需关心该默认空值.
    ``ask_text_queue`` 用于一次动作会连续弹出多个输入框的场景(按顺序取值);
    ``edit_result`` / ``schedule_result`` / ``restore_result`` 分别对应单窗口
    编辑对话框、定时任务对话框与恢复选项对话框的返回值(``None`` 表示取消),
    其中 ``restore_result`` 表示是否勾选"恢复前先创建安全点"; ``export_path``
    是导出时保存对话框的返回值(``None`` 表示用户取消), 需要真的写出包的用例
    自己传 ``tmp_path`` 下的路径。``import_package_path`` 与 ``import_choice``
    同理对应"选要导入的包"与导入冲突对话框(``None`` 就是用户取消);
    ``export_batch_choice`` 与 ``batch_import_choice`` 是批量导出与批量导入
    对话框的返回值(``None`` 同样表示取消)。
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
    # 游戏主页窗口需要文本输入(存档位置)、标签编辑对话框与错误提示的自动应答.
    monkeypatch.setattr(home_page_mod, "ask_text", next_text)
    monkeypatch.setattr(
        home_page_mod, "edit_tags_dialog", lambda *_a, **_k: tags_result
    )
    monkeypatch.setattr(home_page_mod, "info_dialog", lambda *_a, **_k: None)
    # 导出先弹系统保存对话框(主线程阻塞), 同样要自动应答.
    monkeypatch.setattr(main_mod, "pick_save_file", lambda *_a, **_k: export_path)
    # 导入先弹系统文件选择框, 体检后再弹冲突对话框(两者同样要自动应答).
    monkeypatch.setattr(main_mod, "pick_file", lambda *_a, **_k: import_package_path)
    monkeypatch.setattr(
        main_mod, "import_package_dialog", lambda *_a, **_k: import_choice
    )
    # 批量导出/导入的对话框同样要替身(它们也是模态框).
    monkeypatch.setattr(
        main_mod, "export_batch_dialog", lambda *_a, **_k: export_batch_choice
    )
    monkeypatch.setattr(
        main_mod, "batch_import_dialog", lambda *_a, **_k: batch_import_choice
    )


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


def _labels_of(widget: Any) -> list[Any]:
    """递归收集控件树里所有 CTkLabel(裁剪类断言用它定位标签)."""
    import customtkinter as ctk

    found: list[Any] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(child)
        found.extend(_labels_of(child))
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
    """默认展示分支树; 分支树只保留最新一份自动备份, 时间线展示全部.

    分支视图自 I-9 起是**图画布**(不再建卡片), 所以"这一屏显示了几个节点"要量
    ``app._tree_view.node_ids``; 时间线仍然用卡片。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import ViewKind

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._open_game_detail("outer-wilds")
    assert app._view == ViewKind.BRANCH
    branch_nodes = len(app._tree_view.node_ids)
    total_items = len(app._items)
    assert app._cards == {}, "分支视图不建卡片"

    app._switch_view(ViewKind.TIMELINE)
    _pump(app)

    assert len(app._cards) == total_items
    assert branch_nodes < total_items

    app._switch_view(ViewKind.BRANCH)
    _pump(app)
    assert len(app._tree_view.node_ids) == branch_nodes


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
        # 已导入的候选(cand-1)不进这一页: 它是游戏库里的一款游戏, 在库里有完整动作.
        assert len(panel._candidates) == 5
        assert "cand-1" not in {item.candidate_id for item in panel._candidates}
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
            item
            for item in app.backend.list_candidates()
            if item.candidate_id == "cand-2"
        )
        assert imported.status == "imported"
        assert imported.game_id is not None
        # 导入之后它就从这一页收走了(转而在游戏库里查看).
        assert "cand-2" not in {item.candidate_id for item in panel._candidates}
        assert "cand-2" not in panel._cand_rows
    finally:
        app.destroy()


def test_discovery_panel_hides_already_imported_candidates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """已导入的候选不进发现页: 它们是游戏库里的游戏, 在库里有完整动作.

    演示数据里 cand-1("星际拓荒")就是"已导入"的那一条 —— 夹具先自证它存在, 再钉住
    三件事: 列表里没有它、筛选项里没有"已导入"、底栏的分母也不算它。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        imported = {
            item.candidate_id for item in app.backend.list_candidates(status="imported")
        }
        assert imported == {"cand-1"}, "夹具失效: 演示数据里应当正好有一条已导入候选"

        # ① 列表里没有它(切到"全部"也一样), 但后端那条记录仍然留着.
        listed = {item.candidate_id for item in panel._candidates}
        assert listed & imported == set()
        assert len(listed) == len(app.backend.list_candidates()) - len(imported)
        panel._on_filter_change(CandidateFilter.ALL.label)
        assert set(panel._cand_rows) & imported == set()

        # ② 筛选枚举里也没有"已导入"这一项(它的动作只在游戏库里).
        assert [item.value for item in CandidateFilter] == ["all", "new", "ignored"]

        # ③ 底栏分母按"这一页真的会列出来的条数"算, 三种状态并列在**同一行**里.
        pending = sum(1 for item in panel._candidates if item.status == "new")
        ignored = sum(1 for item in panel._candidates if item.status == "ignored")
        assert panel._summary_label.cget("text") == tr(
            "discovery.counts_candidates",
            candidates=len(listed),
            pending=pending,
            ignored=ignored,
        )
        # 第二行留给扫描结果: 不再重复同一批数(03 号评审的两行计数重复).
        assert panel._detail_label.cget("text") == ""
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


def test_discovery_panel_dir_actions_with_nothing_to_act_on(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """监控目录的编辑/启停/删除在"输入为空、未确认、选中已不存在"时都不动数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    # ask_text 返回空串(= 用户直接确定/取消), confirm_dialog 一律返回"否"。
    _patch_dialogs(monkeypatch, ask_text="", confirm=False)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app.backend.add_monitored_directory(str(tmp_path))
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        before = [
            item.directory_id for item in app.backend.list_monitored_directories()
        ]
        assert before, "演示数据里应该至少有一个监控目录"

        # ① 新增目录: 路径为空 → 什么都不发生。
        panel._on_add_dir()
        assert [
            item.directory_id for item in app.backend.list_monitored_directories()
        ] == before

        # ② 编辑目录: 没选中任何目录。
        panel._selected_dir = None
        panel._on_edit_dir()
        panel._on_toggle_dir()
        panel._on_remove_dir()
        # ③ 编辑目录: 选中了真实目录, 但新路径为空/取消。
        panel._select_dir(before[0])
        panel._on_edit_dir()
        # ④ 删除目录: 用户在确认框里选了"否"。
        panel._on_remove_dir()
        assert [
            item.directory_id for item in app.backend.list_monitored_directories()
        ] == before

        # ⑤ 选中的 id 已经不存在(窗口没刷新, 记录在别处被删了)。
        panel._selected_dir = "已经不存在的目录"
        panel._on_edit_dir()
        panel._on_toggle_dir()
        panel._on_remove_dir()
        _pump(app)

        assert [
            item.directory_id for item in app.backend.list_monitored_directories()
        ] == before
        assert panel._dir_item() is None
    finally:
        app.destroy()


def test_discovery_panel_candidate_actions_need_a_valid_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候选的导入/忽略/修正路径: 未选中、选中已不存在、对话框取消都不动数据."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    # import_game_dialog 返回 None(= 关闭对话框), ask_text 为空(= 不填新路径)。
    _patch_dialogs(monkeypatch, ask_text="", import_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app.backend.scan_candidates()
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        panel.reload()
        before = app.backend.list_candidates()
        assert before, "演示数据里应该至少有候选游戏"
        # 这一页只列未导入的候选(cand-1 已导入, 只在游戏库里), 所以挑一条真在列表里的.
        listed = next(item for item in panel._candidates if item.status == "new")

        # ① 没选候选。
        panel._selected_candidate = None
        panel._on_import()
        panel._on_ignore()
        panel._on_relocate()
        # ② 选中的候选 id 已经不存在。
        panel._selected_candidate = "已经消失的候选"
        panel._on_import()
        panel._on_ignore()
        panel._on_relocate()
        # ③ 真实候选, 但导入对话框被取消、修正路径没填新路径。
        panel._select_candidate(listed.candidate_id)
        panel._on_import()
        panel._on_relocate()
        _pump(app)

        assert app.backend.list_candidates() == before
        # 取消导入、又不填新路径: 候选一个字段都没变(选中仍是那个真实候选)。
        assert panel._candidate_item() is not None
    finally:
        app.destroy()


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

        # 切到"已忽略"就能看到刚忽略的那条(筛选本身工作正常), 底栏同一行里数得到它.
        panel._on_filter_change(CandidateFilter.IGNORED.label)
        _pump(app)
        assert set(panel._cand_rows) == {target.candidate_id}
        assert "已忽略 1" in str(panel._summary_label.cget("text"))
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


def test_discovery_rows_clip_long_paths_and_ignore_stale_refits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """候选行的长安装路径按"中间省略"裁进卡片; 行重建后旧标签的回调不再写已销毁控件.

    回归 08/09 号评审: 长路径既不换行也不省略, 会直接顶到卡片右边缘。
    两条断言分工(2026-09-27 重写, 原版在两个平台上都"不会执行"):

    * 比对**同一个裁剪函数**在同一宽度下的输出 —— 比文本不比像素, 与字体库无关,
      钉的是"布局停在上一次窄宽度"这类回归;
    * "放不下就必须裁"由夹具里的**拉丁字符**保证: 缺中日韩字体的环境(裸 Linux
      runner)量汉字近乎零宽, 但拉丁字符在任何字体下都能量出来, 所以这条重想
      在哪个平台都真的会执行。
    """
    from archive_management.domain import GameCandidate
    from archive_management.infrastructure.database import Database
    from archive_management.services.platform_scan import LocalGameScanner
    from archive_management.ui.discovery_page import _CARD_TEXT_WIDTH, DiscoveryPanel
    from archive_management.ui.palette import Palette
    from archive_management.ui.sql_backend import SqlArchiveService

    db = Database(tmp_path / "clip.db")
    db.migrate()
    games = tmp_path / "Games"
    # 路径刻意长到"任何字体都放不下": 拉丁字符每个都能被量出宽度(中日韩字体缺失也
    # 一样), 所以下面"必须裁"那一条在每个平台都真的会被执行到。
    deep = (
        games
        / "Alpha"
        / "one-very-long-directory-name-level"
        / "another-long-directory-name-level"
        / "third-long-directory-name-level"
        / "一个很深的目录层级"
        / "再来一层目录"
        / "存档目录"
    )
    deep.mkdir(parents=True)

    def fake_scan(
        self: object, *, monitored: Sequence[str] = ()
    ) -> list[GameCandidate]:
        del self, monitored
        return [
            GameCandidate(
                name="路径很长的候选游戏",
                install_dir=str(deep),
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
        # 面板必须被**框定宽度**: 没被摆放的容器里每个控件都按内容自适应宽度, 标签宽度
        # 就等于文本宽度 —— 那样 `width` 与被测文本同源, "放不下就该裁"永远不会成立
        # (2026-09-27 实测: 标签宽 1407 == 整条路径的度量值, 于是那两条断言从没跑过).
        host = ctk.CTkFrame(
            app, fg_color="transparent", width=_CARD_TEXT_WIDTH + 40, height=420
        )
        # CTk 的 place 不接受 width/height(尺寸给构造函数), 且固定尺寸不许被内容撑大.
        host.grid_propagate(False)
        host.place(x=0, y=0)
        host.grid_columnconfigure(0, weight=1)
        host.grid_rowconfigure(0, weight=1)
        panel = DiscoveryPanel(
            host, backend=service, palette=Palette.for_theme(app._theme)
        )
        panel.frame.grid(row=0, column=0, sticky="nsew")
        panel._on_scan()
        _pump(app)
        item = panel._candidates[0]
        row = panel._cand_rows[item.candidate_id]
        _pump(app)

        # 这条候选行里只有安装路径是长文本(没有存档路径那样只剩结论一行), 所以
        # "带省略号的标签"就是它 —— 裁剪本身由 ui.widgets.track_fit 按容器宽度做.
        clipped = [label for label in _labels_of(row) if "…" in str(label.cget("text"))]
        assert len(clipped) == 1, "这条候选行里应该只有一个被裁的标签(安装路径)"
        label = clipped[0]

        shown = str(label.cget("text"))
        width = int(label.winfo_width())
        assert width > 1, "夹具没给标签宽度约束, 下面的断言会退化成循环论证"
        font = panel._path_font
        # 期望值用同一个裁剪函数算(比文本, 不比像素): 钉的是"布局停在上一次窄宽度".
        assert shown == panel._clip_path(item.install_dir, width), (
            "标签里显示的就是裁剪后的文本"
        )
        assert font.measure(shown) <= width, f"路径溢出卡片: {shown!r}"
        # 非循环的"必须被裁": 只量路径里的**拉丁字符** —— 缺中日韩字体的环境照样能量出
        # 它们的宽度, 所以这条前提在每个平台都成立(2026-09-27 假红就是因为夹具里全是汉字).
        ascii_only = "".join(ch for ch in item.install_dir if ord(ch) < 0x2E80)
        assert font.measure(ascii_only) > width, f"夹具路径不够长: {ascii_only!r}"
        assert shown != item.install_dir, "长安装路径必须被裁"
        assert "…" in shown, f"路径要中间省略: {shown!r}"

        # 没有存档路径时只剩结论那一行(调用方据此决定不登记重裁).
        assert panel._save_lines(item) == item.save_label

        # 忽略这条候选后整个列表重建: 旧行(连同它上面的 <Configure> 绑定)被销毁,
        # 不可能再往已销毁的控件上写文本 —— 旧实现是靠"登记表里取不到目标就返回"
        # 兜住同一个问题的, 现在结构上就不会发生。
        panel._select_candidate(item.candidate_id)
        panel._on_ignore()
        _pump(app)
        assert item.candidate_id not in panel._cand_rows
        assert not row.winfo_exists(), "旧行必须随重建销毁"
    finally:
        app.destroy()


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
    # 已导入的候选只出现在游戏库里, 发现分区不列它们.
    assert len(panel._candidates) == 5
    assert "cand-1" not in {item.candidate_id for item in panel._candidates}
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
    # 名称占两行高度: 一行名也带一个换行(见 home_page._build_poster), 所以比的是 strip 后的文案.
    assert any(text.strip() == "星际拓荒" for text in card_texts)
    assert any(text.startswith("最近活动") for text in card_texts)
    assert tr("home.poster_backups", count=5) in card_texts

    # 封面是竖屏(高度明显大于宽度); 备份数角标排在卡片里、封面**下方**, 不压在封面上.
    card = page._rows["outer-wilds"]
    cover = card.winfo_children()[0]
    assert cover.winfo_reqheight() > 140
    # 海报网格左对齐: 网格从滚动区左上角开始铺(列不分配权重, 卡片不会被
    # 挤到行中间), 因此所有卡片里最靠左/最靠上的那张偏移就是内边距 4.
    cards = list(page._rows.values())
    # 内边距 4 是**设计**值: CTk 控件自己的 grid padx/pady 会乘窗口缩放(125% 下读回
    # 5), 所以期望值要过 scaled_px, 不能写死 4.
    pad = scaled_px(cards[0], 4)
    assert min(item.winfo_x() for item in cards) == pad
    assert min(item.winfo_y() for item in cards) == pad
    badge = next(
        label
        for label in _card_labels(card)
        if str(label.cget("text")) == tr("home.poster_backups", count=5)
    )
    assert badge not in cover.winfo_children(), "备份数角标不该压在封面上"
    cover_bottom = cover.winfo_y() + cover.winfo_height()
    assert badge.winfo_y() >= cover_bottom, "备份数角标要排在封面下面"

    # 展示偏好会持久化: 重新读取主页仍是海报 + 每页 60 条.
    reloaded = app.backend.load_home()
    assert reloaded.filter.layout is HomeLayout.POSTER
    assert reloaded.filter.page_size == 60


def _assert_no_half_chip(line: str, chips: Sequence[str]) -> None:
    """断言行内只剩完整标签: 放不下时**整块让位**给末尾那个孤零零的省略号(第 6 号评审).

    口径 2026-10-02 调整(用户): 状态一行、标签一行, 超出部分用省略号 —— 省略号是独立
    的一项(前面带空格), 而不是把最后一个标签裁短成 "测…"。
    """
    body = line.rsplit(" …", 1)[0]
    parts = [part for part in body.split(" · ") if part]
    halves = [part for part in parts if part not in chips]
    assert not halves, f"状态列把标签拦腰截断: {halves} (整行 {line!r})"


def _card_labels(card: Any) -> list[Any]:
    """海报卡片里的文本标签.

    ``CTkFrame`` 不接受 ``cget("text")``(会抛 ValueError), 所以必须先按类型筛,
    不能对卡片的每个子控件直接读 text。
    """
    return [child for child in card.winfo_children() if isinstance(child, ctk.CTkLabel)]


def _binds_enter(widget: Any) -> bool:
    """这个控件是否接上了 ``<Enter>``.

    CustomTkinter 重写了 ``bind``: 查询式 ``widget.bind("<Enter>")`` 一律返回 ``None``,
    真正的绑定落在它内部的 canvas 上(实测), 所以这里读 ``_canvas``。
    """
    return bool(widget._canvas.bind("<Enter>"))


def test_home_page_hides_the_table_and_footer_when_the_library_is_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """空库时收起表头/表格骨架与底部统计翻页, 加进第一款游戏后再长回来.

    回归第 1/2 号评审: 一条数据都没有却摆着七个列头, 底下再挂一行
    "共 0 款游戏 · 第 1/1 页", 页面看起来像渲染了一半。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    db = Database(tmp_path / "empty.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    try:
        app = _new_app(service)
    except TclError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        page = app._home_page
        assert page._head.grid_info() == {}, "空库不该显示表头"
        assert page._footer.grid_info() == {}, "空库不该显示底部统计与翻页"
        texts = _label_texts(page._list_box)
        assert tr("home.empty_library") in texts
        assert tr("home.empty_library_hint") in texts

        app.backend.add_game("第一款游戏")
        page.reload()
        _pump(app)
        assert page._head.grid_info() != {}, "有游戏后表头要回来"
        assert page._footer.grid_info() != {}, "有游戏后底部统计与翻页要回来"
        board = page._board
        assert board is not None
        assert page._summary_label.cget("text") == board.summary
    finally:
        app.destroy()


def test_list_scrollbar_appears_only_when_the_games_overflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内容装得下时右侧不立滚动条, 真的溢出才出现(第 1/2/7 号评审).

    常驻的滚动条在说"下面还有内容", 而它其实拖不动 —— 少几行数据时这是纯噪声。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    _pump(app)
    scrollbar = page._list_box._scrollbar
    assert not scrollbar.winfo_ismapped(), "三行游戏装得下, 不该出现滚动条"

    for index in range(40):
        app.backend.add_game(f"溢出游戏{index:02d}")
    page.reload()
    _pump(app)
    page._sync_scrollbar()
    _pump(app)
    assert scrollbar.winfo_ismapped(), "内容溢出后必须出现滚动条"


def test_pager_buttons_look_disabled_when_there_is_nowhere_to_go(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """分页按钮的禁用态必须与可用态明显不同: 底色/文字一起压暗(第 1 号评审)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    palette = page._palette
    assert str(page._prev_btn.cget("state")) == "disabled"
    disabled_bg = str(page._prev_btn.cget("fg_color"))
    assert disabled_bg == palette.disabled_bg
    assert str(page._prev_btn.cget("text_color")) == palette.text_disabled
    assert disabled_bg != palette.raised, "禁用底色不能与可用按钮同色"

    for index in range(32):
        app.backend.add_game(f"批量游戏{index:02d}")
    page.reload()
    _pump(app)
    assert str(page._next_btn.cget("state")) == "normal"
    assert str(page._next_btn.cget("fg_color")) == palette.raised, (
        "可用按钮要回到常规底色"
    )


def test_list_and_poster_selection_share_the_soft_accent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """列表与海报的"选中"是同一套表达(淡底 + 描边), 并与主按钮的实心绿区分开."""
    from archive_management.domain import HomeLayout
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    palette = page._palette
    page._select("shanhai")
    _pump(app)
    # CI 上鼠标可能正好压在某一行的位置上(实测真发生过), 那会给它加上悬停底色 ——
    # 这条量的是"选中表达", 所以先显式把悬停清掉再读(不 pump: 不给别的事件插队的机会).
    page._set_hover(None)
    selected = page._rows["shanhai"]
    other = page._rows["outer-wilds"]
    assert str(selected.cget("fg_color")) == palette.accent_soft
    assert str(selected.cget("border_color")) == palette.accent_soft_border
    assert str(other.cget("fg_color")) == palette.card
    assert palette.accent_soft != palette.accent, "选中不能与主按钮同色"

    page._on_layout_change(HomeLayout.POSTER.label)
    _pump(app)
    page._set_hover(None)
    card = page._rows["shanhai"]
    assert str(card.cget("fg_color")) == palette.accent_soft
    assert str(card.cget("border_color")) == palette.accent_soft_border
    assert str(page._rows["outer-wilds"].cget("fg_color")) == palette.card


def test_cards_highlight_on_hover_without_losing_the_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """悬停与选中是两件事: 未选中悬停只提底色, 已选中的卡片悬停仍是"选中"的样子.

    悬停反馈原来只长在**详情页的备份卡片**上, 主页的列表行与海报卡没有 —— 同一类
    控件(可点、可选中)在两种页面上行为不一致(第 5 号评审)。规则只有一条, 落在
    ``widgets.card_surface_colors``: 选中 > 悬停 > 常规。

    **设完悬停立刻读**(不中间再 ``_pump``): 主页有一个"延后重排"
    (``_schedule_list_sync``), 它跑起来会重建行并把悬停复位 —— CI 的时间线恰好让它
    落在 ``_pump`` 里, 于是刚设的悬停被清掉、量到的是"没悬停"(本地反而量不到这个
    时序)。重绘本身是同步的(``configure`` 当场生效), 所以读之前不需要 pump。
    """
    from archive_management.domain import HomeLayout
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        page = app._home_page
        palette = page._palette
        page._select("shanhai")
        _pump(app)
        assert _binds_enter(page._rows["outer-wilds"]), "列表行要绑悬停"

        # ① 未选中的那一行悬停: 底色提到 card_hover, 描边不变.
        page._set_hover("outer-wilds")
        assert str(page._rows["outer-wilds"].cget("fg_color")) == palette.card_hover
        assert (
            str(page._rows["outer-wilds"].cget("border_color")) == palette.card_border
        )

        # ② 已选中的那一行也悬停: 必须还是"选中"的样子(悬停不得盖掉选中).
        page._set_hover("shanhai")
        assert str(page._rows["shanhai"].cget("fg_color")) == palette.accent_soft
        assert (
            str(page._rows["shanhai"].cget("border_color"))
            == palette.accent_soft_border
        )

        # ③ 鼠标移开: 两张都回到各自的常态.
        page._set_hover(None)
        assert str(page._rows["outer-wilds"].cget("fg_color")) == palette.card
        assert str(page._rows["shanhai"].cget("fg_color")) == palette.accent_soft

        # ④ 海报卡接的是同一套, 而且卡片与它的子控件都会触发(否则移到文字上会闪掉).
        page._on_layout_change(HomeLayout.POSTER.label)
        _pump(app)
        card = page._rows["outer-wilds"]
        assert _binds_enter(card), "海报卡要绑悬停"
        page._set_hover("outer-wilds")
        assert str(card.cget("fg_color")) == palette.card_hover
        assert all(_binds_enter(child) for child in card.winfo_children()), (
            "子控件也要触发同一张卡片的悬停"
        )
    finally:
        app.destroy()


def test_poster_card_keeps_its_meta_below_the_title(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """海报卡片: 角标不压封面、名称与元信息分开、占位字不再超大(第 5 号评审)."""
    from archive_management.domain import HomeLayout
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.home_page import (
        _POSTER_PLACEHOLDER_SIZE,
    )

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    page._on_layout_change(HomeLayout.POSTER.label)
    _pump(app)
    page.frame.update_idletasks()
    card = page._rows["outer-wilds"]
    cover = card.winfo_children()[0]
    badge = next(
        label
        for label in _card_labels(card)
        if str(label.cget("text")) == tr("home.poster_backups", count=5)
    )
    assert badge not in cover.winfo_children(), "角标不该压在封面上"
    cover_bottom = cover.winfo_y() + cover.winfo_height()
    assert badge.winfo_y() >= cover_bottom, "角标要排在封面下面"
    name = next(
        label
        for label in _card_labels(card)
        if str(label.cget("text")).strip() == "星际拓荒"
    )
    name_bottom = name.winfo_y() + name.winfo_height()
    assert name_bottom <= badge.winfo_y(), "名称与元信息之间要分开, 不能挤在一起"
    placeholder = next(
        child for child in cover.winfo_children() if str(child.cget("text")) == "星际"
    )
    size = int(placeholder.cget("font").cget("size"))
    assert size == _POSTER_PLACEHOLDER_SIZE
    assert size < 34, "占位字不该比卡片标题大一倍"
    # 卡片内所有直接子控件都不许越出卡片: 越界就会盖住下边框.
    # 卡片高度是按内容算的(见 home_page._fit_poster_height), 所以比的是**它自己**的高度。
    bottom = max(
        child.winfo_y() + child.winfo_height() for child in card.winfo_children()
    )
    assert bottom <= card.winfo_height()


def test_state_column_never_shows_half_a_chip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """状态列放不下时整块让位给省略号, 不许出现"测…"这种半句话(第 6 号评审).

    口径 2026-10-02 调整(用户): **状态一行、自定义标签一行**, 标签再多也只有两行。
    "放不下"由夹具里的**拉丁字符**保证: 裸 Linux runner 上缺中日韩字体时汉字近乎
    零宽(2026-09-27 实测就在那里假红), 而 16 字的拉丁标签在任何字体下都远超 200px,
    所以下面几条强制断言在每个平台都真的会执行。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.home_page import _COLUMNS, _STATE_MAX_LINES
    from archive_management.ui.models import status_lines

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    # 六个满长(16 字)标签 + 平台/备份状态: 一定塞不进 200px 的状态列.
    app.backend.set_game_tags(
        "outer-wilds",
        [
            "very-long-tag-01",
            "very-long-tag-02",
            "very-long-tag-03",
            "very-long-tag-04",
            "very-long-tag-05",
            "very-long-tag-06",
        ],
    )
    page.reload()
    _pump(app)

    item = _home_item(page, "outer-wilds")
    label = page._row_parts["outer-wilds"].columns.winfo_children()[-1]
    text = str(label.cget("text"))
    lines = text.split("\n")
    column = _COLUMNS[-1][1]
    font = page._value_font
    # 页面算折行用的预算已经换成**物理**像素(见 home_page._fill_row_columns): 列宽在
    # 设计里是逻辑像素, 而字体度量是物理像素 —— 不换算的话 125% 的屏上两边差 25%,
    # 文字比列宽多出来那截会把整块固定列推歪(2026-10-02 的实测)。
    room = scaled_px(label, column)
    assert text == status_lines(item.state_chips, item.tags, font, room), (
        "状态列显示的应当是排布函数在当前字体/列宽下的结果"
    )
    assert 1 <= len(lines) <= _STATE_MAX_LINES
    state_line, tags_line = lines[0], lines[-1]
    assert state_line == " · ".join(item.state_chips), (
        f"状态那几个要在同一行: {state_line!r}"
    )
    full = " · ".join(item.tags)
    assert font.measure(full) > room, f"夹具要长到 {room}px 放不下: {full!r}"
    assert len(lines) == 2, f"状态一行 + 标签一行: {text!r}"
    assert tags_line.endswith(" …"), f"标签放不下时要补省略号: {text!r}"
    for line in lines:
        _assert_no_half_chip(line, item.chips)


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


def test_home_actions_without_a_selection_only_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """主页各动作在"没有选中游戏"时只给提示: 不改数据, 也不开出子窗口."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, ask_text="")
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    page._selected = None

    page._on_detail()
    page._on_backup()
    page._on_add_location()
    page._on_manage()
    page._on_edit_tags()
    page._on_archive()
    page._on_toggle_enabled()
    _pump(app)

    assert page._selected is None
    assert app._active_window is None, "没有选中游戏时不该开出管理/定时任务窗口"
    assert len(app.backend.list_games()) == 3


def test_home_pagination_stays_put_with_a_single_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有一页时上一页/下一页都是空操作(不该把页码翻到界外)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page

    page._on_prev_page()
    assert page._page_index == 0
    page._on_next_page()
    assert page._page_index == 0

    page._on_page_size_change("60")
    assert page._page_index == 0


def test_home_filter_switches_clear_the_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """换平台/类型筛选会清掉当前选中; 重复选同一个值则是空操作(选中保留)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    page._select("outer-wilds")

    # ① 重新选当前那一项: 不改动, 选中也保留。
    same = [
        label for label, key in page._origin_keys.items() if key == page._filter.origin
    ]
    assert same, "演示数据里应当能找到与当前取值对应的下拉文案"
    page._on_origin_change(same[0])
    assert page._selected == "outer-wilds"

    # ② 换一个不同的平台: 清掉选中并真的换过去。
    others = [
        label for label, key in page._origin_keys.items() if key != page._filter.origin
    ]
    assert others, "演示数据应当有多个平台取值"
    page._on_origin_change(others[0])
    assert page._filter.origin == page._origin_keys[others[0]]
    # 换筛选后不会保留原来那款: 要么落成空选中, 要么自动选中新筛选里的第一款。
    assert page._selected != "outer-wilds"
    assert page._selected is None or page._selected in {
        item.game_id for item in page._board.games
    }

    # ③ 类型筛选同理。
    page._select("outer-wilds")
    other_categories = [
        label
        for label, key in page._category_keys.items()
        if key != page._filter.category
    ]
    assert other_categories, "演示数据应当有多个类型取值"
    page._on_category_change(other_categories[0])

    assert page._filter.category == page._category_keys[other_categories[0]]
    assert page._selected != "outer-wilds"


def test_home_actions_are_blocked_for_an_archived_game(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档后主页各动作即使被直接调用也只给提示: 备份/加位置/改标签/启停都不生效."""
    from archive_management.ui.demo_backend import DemoArchiveService

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
    before_ids = _location_ids(app, "shanhai")
    before_tags = next(
        item for item in app.backend.load_home().games if item.game_id == "shanhai"
    ).tags

    page._on_backup()
    page._on_add_location()
    page._on_edit_tags()
    page._on_toggle_enabled()
    _pump(app)

    assert tr("home.archived_blocked", name="山海旅人") in page._summary_label.cget(
        "text"
    )
    assert _location_ids(app, "shanhai") == before_ids
    assert (
        next(
            item for item in app.backend.load_home().games if item.game_id == "shanhai"
        ).tags
        == before_tags
    )


def test_home_edit_tags_with_a_cancelled_dialog_changes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """标签对话框被取消(返回 None): 不改动该游戏的标签, 也不报错."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, tags_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page
    page._select("outer-wilds")
    before = next(
        item for item in app.backend.load_home().games if item.game_id == "outer-wilds"
    ).tags

    page._on_edit_tags()
    _pump(app)

    after = next(
        item for item in app.backend.load_home().games if item.game_id == "outer-wilds"
    ).tags
    assert after == before


def test_home_view_switches_cover_every_view(monkeypatch: pytest.MonkeyPatch) -> None:
    """逐个切换全部视图: 每个视图都要能渲染(也覆盖视图按钮的重绘循环)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    page = app._home_page

    for view in HomeView:
        page._on_view(view)
        _pump(app)
        assert page._filter.view is view

    page._on_view(HomeView.ALL)
    _pump(app)
    assert page._board is not None


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


def test_settings_changes_without_a_config_path_stay_in_memory() -> None:
    """没有配置路径时(测试/未初始化场景): 各设置写回口都只存内存, 不写文件也不报错."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    assert app._paths is None

    app._save_language("en")
    app._save_debug(True)
    app._save_activation(True)
    app._save_font_size(20)
    app._save_shortcuts()

    # 没有任何配置路径: 写回口只改内存态, 不写文件也不报错。
    assert app._base_font_px >= 11


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


class _KeyEvent:
    """最简键盘事件替身(录制流程只用到 ``keysym``)."""

    def __init__(self, keysym: str) -> None:
        self.keysym = keysym


def test_settings_shortcut_recording_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """快捷键录制的状态机: 开始/取消/切换动作/无效键/合法组合/未录制时收尾."""
    from archive_management.services.hotkeys import combo_from_pressed, tk_token
    from archive_management.ui.demo_backend import DemoArchiveService

    # 自检: 下面的 keysym 与 token 映射变了的活, 这条用例要立刻失败而不是静默走过。
    keysyms = ("Super_L", "s")
    tokens = [tk_token(name) for name in keysyms]
    assert None not in tokens, (
        f"keysym 映射变了: {list(zip(keysyms, tokens, strict=True))}"
    )
    assert combo_from_pressed([token for token in tokens if token]) is not None, (
        "Win + 字母 应当是一个合法组合"
    )

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    applied: list[tuple[str, str]] = []
    window = _settings_window(app, applied)

    # ① 没在录制时按/放键: 直接放行。
    assert window._on_key_press(_KeyEvent("s")) == "break"
    assert window._on_key_release(_KeyEvent("s")) == "break"
    # ② 没在录制时收尾: 什么都不做。
    window._finish_capture()

    # ③ 点一下开始录制, 再点一下取消(保留原值, 不调用应用回调)。
    window._toggle_capture(ACTION_SAVE_NOW)
    assert window._capturing == ACTION_SAVE_NOW
    assert window.shortcut_text(ACTION_SAVE_NOW) == tr("settings.recording")
    window._toggle_capture(ACTION_SAVE_NOW)
    assert window._capturing is None
    assert applied == []

    # ④ 录制中切到另一行动作: 上一行先收尾(取消), 新的一行进入录制。
    window._toggle_capture(ACTION_SAVE_NOW)
    window._toggle_capture(ACTION_CREATE_BRANCH)
    assert window._capturing == ACTION_CREATE_BRANCH
    assert applied == []

    # ⑤ 按一个不在白名单里的键: 只提示, 不参与组合。
    window._on_key_press(_KeyEvent("F13"))
    assert window._error != ""
    # ⑥ 同一个键按两次: 第二次不重复累计(松开全部键后才安排收尾)。
    for name in ("Super_L", "s", "s"):
        window._on_key_press(_KeyEvent(name))
    window._on_key_release(_KeyEvent("s"))
    assert window._held, "还有键按着, 不该安排收尾"
    window._on_key_release(_KeyEvent("Super_L"))
    assert window._pending_finish is not None, "全部松开后应当安排延迟收尾"
    window._finish_when_idle()

    # ⑦ 合法组合被应用: 记录新组合、清掉错误提示。
    assert len(applied) == 1
    action, accelerator = applied[0]
    assert action == ACTION_CREATE_BRANCH
    assert accelerator == window._shortcuts[ACTION_CREATE_BRANCH]
    assert window._error == ""

    # ⑧ 只按字母(没有辅助键): 组合非法, 保留原值并给出原因。
    before = dict(window._shortcuts)
    window._toggle_capture(ACTION_SAVE_NOW)
    window._on_key_press(_KeyEvent("s"))
    window._on_key_release(_KeyEvent("s"))
    window._finish_when_idle()
    assert window._error != ""
    assert window._shortcuts == before
    assert len(applied) == 1, "非法组合不该调用应用回调"

    # ⑨ 录制中关闭窗口: 先收尾再销毁, 不留悬挂的计时器。
    window._toggle_capture(ACTION_SAVE_NOW)
    window.close()
    assert window._capturing is None


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


def test_schedule_window_actions_need_a_selection_and_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定时任务窗口: 没选中/取消新增/阻塞游戏/取消编辑/未确认删除都不改配置."""
    import archive_management.ui.schedule_window as sched_window_mod
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, confirm=False, schedule_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = ScheduleWindow(
        app, backend=app.backend, palette=Palette.for_theme(app._theme)
    )
    before = {item.game_id: item.interval_text for item in window._items}
    assert before, "演示数据里应当已经有一个定时任务"

    # ① 一个都没选中: 取选中项直接返回 None, 三个动作都只提示。
    assert window._selected_item() is None
    window._on_edit()
    window._on_toggle()
    window._on_remove()
    _pump(app)
    assert {item.game_id: item.interval_text for item in window._items} == before

    # ② 新增: 游戏选择框被取消 → 什么都不发生。
    monkeypatch.setattr(sched_window_mod, "add_schedule_dialog", lambda *_a, **_k: None)
    window._on_add()
    _pump(app)
    assert {item.game_id for item in window._items} == set(before)

    # ③ 新增: 选了一款还没配置的游戏 → 真的加进来。
    #    (选择框只会给出"有存档位置"的游戏, 因此这里传的必须是候选里的 id。)
    monkeypatch.setattr(
        sched_window_mod, "add_schedule_dialog", lambda *_a, **_k: "shanhai"
    )
    _patch_dialogs(monkeypatch, schedule_result=("1h", "3"))
    window._on_add()
    _pump(app)
    assert "shanhai" in {item.game_id for item in window._items}
    assert app.backend.task_status("shanhai").schedule_text != ""

    # ④ 编辑: 对话框取消 → 周期不变(同时把确认框设为"否认", 供下一步用)。
    _patch_dialogs(monkeypatch, schedule_result=None, confirm=False)
    window._select("outer-wilds")
    window._on_edit()
    _pump(app)
    assert app.backend.task_status("outer-wilds").schedule_text == before["outer-wilds"]

    # ⑤ 删除: 未确认 → 任务还在。
    window._on_remove()
    _pump(app)
    assert app.backend.task_status("outer-wilds").schedule_text == before["outer-wilds"]

    # ⑥ 启停: 暂停只改启用态, 周期照旧保留。
    window._on_toggle()
    _pump(app)
    assert app.backend.task_status("outer-wilds").schedule_text == before["outer-wilds"]

    # ⑦ 删除: 确认后周期被清空, 但备份不受影响。
    _patch_dialogs(monkeypatch, confirm=True)
    backups_before = len(app.backend.list_backups("outer-wilds"))
    window._select("outer-wilds")
    window._on_remove()
    _pump(app)
    assert app.backend.task_status("outer-wilds").schedule_text == ""
    assert len(app.backend.list_backups("outer-wilds")) == backups_before


def test_add_schedule_dialog_filters_typing_and_needs_a_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新增定时任务的选游戏弹窗: 输入即筛选, 没命中候选时不会返回 id."""
    import customtkinter as ctk

    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import (
        ScheduleWindow,
        add_schedule_dialog,
    )

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    palette = Palette.for_theme(app._theme)
    window = ScheduleWindow(app, backend=app.backend, palette=palette)
    candidates = window._addable()
    blocked = window._blocked()
    assert candidates, "演示数据里应当至少有一款可以配置定时备份的游戏"
    assert blocked, "演示数据里应当有一款未配置存档位置的游戏(用于显示被拦下的原因)"
    names = [item.game_name for item in candidates]

    combos: list[Any] = []
    toplevels: list[Any] = []
    real_combo = ctk.CTkComboBox
    real_toplevel = ctk.CTkToplevel

    def make_combo(*args: Any, **kwargs: Any) -> Any:
        combo = real_combo(*args, **kwargs)
        combos.append(combo)
        return combo

    def make_toplevel(*args: Any, **kwargs: Any) -> Any:
        top = real_toplevel(*args, **kwargs)
        toplevels.append(top)
        return top

    # 补丁打在 customtkinter 模块上: 弹窗内部就是通过它取这两个控件的。
    monkeypatch.setattr(ctk, "CTkComboBox", make_combo)
    monkeypatch.setattr(ctk, "CTkToplevel", make_toplevel)
    # 弹窗不再阻塞主线程: "等窗口关闭"这一步由测试自己接管。
    monkeypatch.setattr(app, "wait_window", lambda *_a, **_k: None)

    def descendants(widget: Any) -> list[Any]:
        found: list[Any] = []
        for child in widget.winfo_children():
            found.append(child)
            found.extend(descendants(child))
        return found

    seen_values: list[list[str]] = []

    def open_window(typed: str) -> tuple[str | None, Any]:
        """打开弹窗并在返回后逐个控件驱动它.

        ``wait_window`` 被替换成空操作: 否则主线程会一直卡在等待里, 由测试
        自己接管"关闭窗口"这一步(点的是弹窗自己的按钮, 与用户操作一致)。
        """
        combos.clear()
        toplevels.clear()
        result = add_schedule_dialog(
            app, palette, candidates=candidates, blocked=blocked
        )
        top = toplevels[-1]
        combo = combos[-1]
        combo.set(typed)
        combo._entry.event_generate("<KeyRelease>")
        seen_values.append(list(combo.cget("values")))
        return result, top

    def buttons_of(top: Any) -> list[Any]:
        # 动作区里"取消"在前, "确认"在后。
        found = [w for w in descendants(top) if isinstance(w, ctk.CTkButton)]
        assert len(found) == 2
        return found

    # ① 输入不存在的名字: 下拉回退为全部候选, 点确认不会关窗(等于没选中)。
    result, top = open_window("这个游戏不存在")
    assert result is None
    assert seen_values[-1] == names
    buttons_of(top)[1].invoke()
    assert top.winfo_exists(), "没有命中候选时点确认应当保持窗口打开"
    buttons_of(top)[0].invoke()
    assert not top.winfo_exists()

    # ② 输入名字的一部分: 下拉只剩命中的那一条(输入即筛选), 确认即关窗。
    _, top = open_window(names[-1][1:])
    assert seen_values[-1] == [names[-1]]
    buttons_of(top)[1].invoke()
    assert not top.winfo_exists()

    # ③ 完全命中候选名: 同样确认即关窗。
    _, top = open_window(names[0])
    assert seen_values[-1] == [names[0]]
    buttons_of(top)[1].invoke()
    assert not top.winfo_exists()

    # ④ 没有候选时直接返回, 连窗口都不创建。
    assert add_schedule_dialog(app, palette, candidates=(), blocked=()) is None


def test_add_schedule_dialog_separates_title_hint_and_blocked_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新增定时任务的弹窗: 标题有分割线, 被拦下的游戏名逐行列出, 不再重复窗口的指引."""
    import customtkinter as ctk

    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import (
        ScheduleWindow,
        add_schedule_dialog,
    )

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    palette = Palette.for_theme(app._theme)
    window = ScheduleWindow(app, backend=app.backend, palette=palette)
    candidates = window._addable()
    blocked = window._blocked()
    assert blocked, "演示数据里应当有一款未配置存档位置的游戏"

    toplevels: list[Any] = []
    real_toplevel = ctk.CTkToplevel

    def make_toplevel(*args: Any, **kwargs: Any) -> Any:
        top = real_toplevel(*args, **kwargs)
        toplevels.append(top)
        return top

    monkeypatch.setattr(ctk, "CTkToplevel", make_toplevel)
    monkeypatch.setattr(app, "wait_window", lambda *_a, **_k: None)

    assert (
        add_schedule_dialog(app, palette, candidates=candidates, blocked=blocked)
        is None
    )
    top = toplevels[-1]
    texts = _label_texts(top)

    # ① 标题下面有一条 1px 分割线(否则粗体标题看起来就是第一个字段的标签)。
    dividers = [
        child
        for child in top.winfo_children()
        if isinstance(child, ctk.CTkFrame)
        and child.cget("fg_color") == palette.border
        and int(child.cget("height")) == 1
    ]
    assert dividers, "标题与正文之间应当有一条分割线"

    # ② 被拦下的游戏名逐行列出(带项目符号), 不再是拼在一句话里的逗号列表。
    expected = "\n".join(f"· {item.game_name}" for item in blocked)
    assert expected in texts, "被拦下的游戏名应当逐行列出"
    assert ", ".join(item.game_name for item in blocked) not in texts, (
        "游戏名不该再拼回一句逗号列表"
    )

    # ③ "推荐去游戏设置"的指引只留在窗口副标题里, 弹窗不再重复一遍。
    assert tr("schedule.subtitle") not in texts
    assert not any("推荐" in text for text in texts)

    # ④ "可以打字筛选"紧贴在选择框下方(而不是藏在说明句中间)。
    children = list(top.winfo_children())
    picker_index = next(
        index
        for index, child in enumerate(children)
        if isinstance(child, ctk.CTkComboBox)
    )
    assert isinstance(children[picker_index + 1], ctk.CTkLabel)
    assert children[picker_index + 1].cget("text") == tr("dialog.schedule_add_filter")


@pytest.mark.blocker
def test_tags_dialog_does_not_twitch_or_crash_while_adding_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真窗口回归: 连点两次"添加标签"时滚动条不能抽动, 更不能把回调递归到崩溃.

    这条用例盯的是当年那个真实故障(用户实测: 打开"编辑标签"后连点两次"添加标签"
    界面抽搐, 控制台报 ``Exception in Tkinter callback`` / ``maximum recursion depth
    exceeded``)。两个机制都要拦:

    ① **滚动条的高度请求必须是 1px** —— CTk 的滚动条默认请求 200px, 与画布在同一行:
       一旦显示就把那一行撑到 200, 视口跟着变高 → 内容又装得下 → 收起 → 视口变矮 →
       又溢出 → 再显示…… 实测就在边界上无限抽动(它同时会引出无穷的 Configure);
    ② **重入与判定次数有上限** —— 改几何会引出新的 Configure, 没有重入闸门时会在同一个
       调用栈里递归到崩溃。这里用 1.5 秒的有限事件循环统计判定次数, 抽搐时实测上百次。

    断言顺序是刻意的: 先查高度请求(确定性, 且在任何点击之前), 再跑事件循环 ——
    高度请求被改回去时用例立刻红, 不会先掉进那个可能卡住的事件循环里。
    """
    import customtkinter as ctk

    from archive_management.ui import widgets
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    # Tk 回调里的异常默认只打到 stderr(测试里看不见): 这里收下来当断言用。
    callback_errors: list[str] = []
    app.report_callback_exception = lambda exc, value, _tb: callback_errors.append(
        f"{exc.__name__}: {value}"
    )

    toplevels: list[Any] = []
    real_toplevel = ctk.CTkToplevel

    def make_toplevel(*args: Any, **kwargs: Any) -> Any:
        top = real_toplevel(*args, **kwargs)
        toplevels.append(top)
        return top

    monkeypatch.setattr(ctk, "CTkToplevel", make_toplevel)
    # 弹窗不再阻塞主线程: "点按钮"这一步由用例自己接管。
    monkeypatch.setattr(app, "wait_window", lambda *_a, **_k: None)

    calls: list[Any] = []
    real_sync = widgets.sync_scrollbar

    def counting_sync(frame: Any) -> bool:
        calls.append(frame)
        return real_sync(frame)

    monkeypatch.setattr(widgets, "sync_scrollbar", counting_sync)

    dialogs_mod.edit_tags_dialog(app, app.p, tags=("探索",))
    window = toplevels[-1]
    rows = _scrollable_with_height(window, 150)
    assert getattr(rows._scrollbar, "_desired_height", None) == 1, (
        "滚动条的高度请求必须压到 1px: 否则它一显示就会把视口撑高, 判定在边界上无限抽动"
    )

    add = _button_by_text(window, tr("dialog.tags_add"))
    calls.clear()
    add.invoke()
    add.invoke()
    for _ in range(60):
        app.update_idletasks()
        app.update()
        time.sleep(0.01)

    assert callback_errors == [], f"回调里不该出现异常: {callback_errors}"
    assert len(calls) < 30, f"滚动条判定被反复触发({len(calls)} 次): 界面在抽搐"
    window.destroy()


def _scrollable_with_height(widget: Any, height: int) -> Any:
    """递归找出指定高度的滚动容器(弹窗里同一个高度只有一个)."""
    for child in widget.winfo_children():
        if (
            hasattr(child, "_scrollbar")
            and hasattr(child, "_parent_canvas")
            and int(child.cget("height")) == height
        ):
            return child
        found = _scrollable_with_height(child, height)
        if found is not None:
            return found
    return None


def _button_by_text(widget: Any, text: str) -> Any:
    """递归找出文案匹配的按钮(CTk 自绘, 只能按 cget("text") 认)."""
    import customtkinter as ctk

    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton) and str(child.cget("text")) == text:
            return child
        found = _button_by_text(child, text)
        if found is not None:
            return found
    return None


def test_edit_schedule_rejects_invalid_keep_auto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """编辑定时任务: 保留份数非法/游戏没有存档位置时不写库, 留空则沿用原值."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import MAX_KEEP_AUTO
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import edit_schedule

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    palette = Palette.for_theme(app._theme)
    before = app.backend.task_status("outer-wilds")

    for raw in ("abc", "0", str(MAX_KEEP_AUTO + 1)):
        _patch_dialogs(monkeypatch, schedule_result=("2h", raw))
        assert (
            edit_schedule(
                app,
                palette,
                app.backend,
                game_id="outer-wilds",
                game_name="星际拓荒",
            )
            is False
        )
        unchanged = app.backend.task_status("outer-wilds")
        assert unchanged.schedule_text == before.schedule_text
        assert unchanged.keep_auto == before.keep_auto

    # 留空表示"沿用原值": 只改周期, 保留份数不动。
    _patch_dialogs(monkeypatch, schedule_result=("2h", ""))
    assert (
        edit_schedule(
            app, palette, app.backend, game_id="outer-wilds", game_name="星际拓荒"
        )
        is True
    )
    updated = app.backend.task_status("outer-wilds")
    assert updated.schedule_text == "2h"
    assert updated.keep_auto == before.keep_auto

    # 停用中的游戏允许先配周期, 只是任务保持暂停态(返回 True 并提示原因)。
    _patch_dialogs(monkeypatch, schedule_result=("3h", "2"))
    assert (
        edit_schedule(
            app,
            palette,
            app.backend,
            game_id="outer-wilds",
            game_name="星际拓荒",
            game_enabled=False,
        )
        is True
    )
    assert app.backend.task_status("outer-wilds").schedule_text == "3h"

    # 没有存档位置的游戏在对话框弹出前就被拦下。
    _patch_dialogs(monkeypatch, schedule_result=("3h", "2"))
    assert (
        edit_schedule(
            app,
            palette,
            app.backend,
            game_id="endless-space",
            game_name="无尽太空",
        )
        is False
    )


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


def _manage_game_window(
    app: ArchiveApp, *, game_id: str, name: str, enabled: bool, archived: bool = False
) -> Any:
    """构造游戏管理窗口(不经过主页入口), 便于逐个动作地验证守卫分支."""
    from archive_management.ui.manage_window import ManageGameWindow

    return ManageGameWindow(
        app,
        backend=app.backend,
        palette=app.p,
        game_id=game_id,
        name=name,
        enabled=enabled,
        backup_location="—",
        on_change=lambda: None,
        archived=archived,
    )


def _location_ids(app: ArchiveApp, game_id: str) -> list[str]:
    """读取某个游戏当前的存档位置 id 列表."""
    return [item.location_id for item in app.backend.list_locations(game_id)]


def test_archived_manage_window_blocks_every_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档后绕过按钮直接调用也要被拦下: 名称/位置/主标记/启用态一个都不许改.

    按钮在 ``_apply_archived_rules`` 里已被置灰, 这条用例走的是方法级守卫
    (``_blocked``), 也就是"快捷键或旧代码路径直接调进来"的那种场景。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    before_name = app.backend.get_detail("outer-wilds").name
    before_ids = _location_ids(app, "outer-wilds")
    before_primary = [
        item.is_primary for item in app.backend.list_locations("outer-wilds")
    ]
    window = _manage_game_window(
        app, game_id="outer-wilds", name=before_name, enabled=False, archived=True
    )

    window._on_schedule()
    window._on_rename()
    window._on_toggle_enabled()
    window._add_location("directory")
    window._on_set_primary()
    window._on_verify()
    window._on_edit_path()
    window._on_remove()
    _pump(app)

    assert app.backend.get_detail("outer-wilds").name == before_name
    assert _location_ids(app, "outer-wilds") == before_ids
    assert [
        item.is_primary for item in app.backend.list_locations("outer-wilds")
    ] == before_primary
    home = app.backend.load_home()
    entry = next(item for item in home.games if item.game_id == "outer-wilds")
    # 演示数据里这款游戏本来是启用的: 被拦下的"启停"没有把它翻成停用。
    assert entry.enabled is True
    window.close()


def test_manage_window_refuses_a_blank_rename_and_an_unconfirmed_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改名输入为空、删除游戏未确认: 都只给提示, 不动数据."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    before = app.backend.get_detail("outer-wilds").name
    window = _manage_game_window(app, game_id="outer-wilds", name=before, enabled=True)

    _patch_dialogs(monkeypatch, ask_text="")
    window._on_rename()
    _pump(app)
    assert app.backend.get_detail("outer-wilds").name == before

    _patch_dialogs(monkeypatch, confirm=False)
    window._on_delete_game()
    _pump(app)
    assert [item.game_id for item in app.backend.list_games()].count("outer-wilds") == 1
    window.close()


def test_manage_window_location_actions_need_a_real_selection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没选中位置、或选中的 id 已经不存在: 这些动作都只提示, 不做任何改动."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    before = _location_ids(app, "outer-wilds")
    window = _manage_game_window(
        app, game_id="outer-wilds", name="星际拓荒", enabled=True
    )

    # ① 一个都没选: `_selected_item` 直接返回 None。
    window._on_set_primary()
    window._on_verify()
    window._on_edit_path()
    window._on_remove()
    # ② 选中的 id 已经不在列表里(窗口没刷新, 位置在别处被删掉了)。
    window._selected = "已经不存在的位置"
    window._on_set_primary()
    window._on_verify()
    window._on_edit_path()
    window._on_remove()
    _pump(app)

    assert _location_ids(app, "outer-wilds") == before
    assert [item.is_primary for item in app.backend.list_locations("outer-wilds")] == [
        True,
        *[False] * (len(before) - 1),
    ]
    window.close()


def test_manage_window_rename_passes_the_current_name_as_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重命名弹窗要带上"当前名称": 它与"新增游戏"长得一样(20 号评审)."""
    from archive_management.ui import manage_window as manage_mod
    from archive_management.ui.demo_backend import DemoArchiveService

    seen: dict[str, object] = {}

    def record(*_args: object, **kwargs: object) -> str | None:
        seen.update(kwargs)
        return None

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    monkeypatch.setattr(manage_mod, "ask_text", record)
    window = _manage_game_window(
        app, game_id="outer-wilds", name="星际拓荒", enabled=True
    )

    window._on_rename()

    assert seen["context"] == tr("dialog.rename_context", name="星际拓荒")
    window.close()


def test_manage_window_location_buttons_stay_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """位置操作按钮必须整颗落在窗口里.

    六个按钮挤一行时总宽已经等于容器可用宽度, 再加间距必然溢出 —— 最右边的
    "删除"会被窗口边缘裁掉半颗(15 号评审)。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _manage_game_window(
        app, game_id="outer-wilds", name="星际拓荒", enabled=True
    )
    assert _wait_for(app, lambda: window._location_buttons[0].winfo_width() > 1), (
        "窗口未布局"
    )

    frame = window._window
    right = frame.winfo_rootx() + frame.winfo_width()
    for index, button in enumerate(window._location_buttons):
        edge = button.winfo_rootx() + button.winfo_width()
        assert edge <= right, f"第 {index} 个位置按钮被窗口裁掉: {edge} > {right}"
    window.close()


def test_manage_window_location_edits_need_a_real_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加位置/改路径传空值、路径没变、以及删除原始目录未确认: 都不该落库."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    before = _location_ids(app, "outer-wilds")
    window = _manage_game_window(
        app, game_id="outer-wilds", name="星际拓荒", enabled=True
    )
    selected = next(
        item for item in app.backend.list_locations("outer-wilds") if item.is_primary
    )
    window._select(selected.location_id)

    # ① 新增位置时输入为空(用户直接按了确定/取消)。
    _patch_dialogs(monkeypatch, ask_text="")
    window._on_add_directory()
    window._on_add_file()
    assert _location_ids(app, "outer-wilds") == before

    # ② 编辑路径时给了同一个路径: 没有变化就不该写库。
    _patch_dialogs(monkeypatch, ask_text=selected.path)
    window._on_edit_path()
    assert _location_ids(app, "outer-wilds") == before

    # ③ 删除原始存档位置未确认。
    _patch_dialogs(monkeypatch, confirm=False)
    window._on_delete_origin()
    _pump(app)
    assert _location_ids(app, "outer-wilds") == before
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
    # 路径比对走 normalize_path: 后端存的是**规范化后的绝对路径**, 而 "C:\Saves" 这类
    # Windows 风格写法的规范化结果与平台有关(POSIX 上会落到当前工作目录之下)。
    assert [item.path for item in app.backend.list_locations(imported.game_id)] == [
        normalize_path("C:\\Saves"),
        normalize_path("D:\\Other"),
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


def test_settings_window_toggles_debug_logging() -> None:
    """设置窗口里拨调试开关: 交给主窗口应用, 并就地更新状态说明."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    applied: list[bool] = []

    def apply(enabled: bool) -> str | None:
        applied.append(enabled)
        return None

    window._on_apply_debug = apply
    window._debug_switch.select()
    window._on_debug_toggled()
    _pump(app)

    assert applied == [True]
    assert window._debug is True
    assert window._debug_label.cget("text") == tr(
        "settings.debug_state", state=tr("settings.debug_on")
    )


def test_settings_window_reverts_the_switch_when_applying_fails() -> None:
    """应用失败时说明原因, 并把开关拨回实际生效的状态(不能看着像拨成了)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    assert window._debug is False

    window._on_apply_debug = lambda enabled: "写配置失败"
    window._debug_switch.select()
    window._on_debug_toggled()
    _pump(app)

    assert window._debug is False
    assert bool(window._debug_switch.get()) is False
    assert "写配置失败" in window._debug_label.cget("text")


def test_settings_window_toggles_auto_activation() -> None:
    """设置窗口里拨自动启停开关: 交给主窗口应用, 并就地更新状态说明."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    assert window._activation is False

    window._activation_switch.select()
    window._on_activation_toggled()
    _pump(app)

    assert app._activation is True
    assert window._activation is True
    assert window._activation_label.cget("text") == tr(
        "settings.activation_state", state=tr("settings.activation_on")
    )


def test_settings_window_reverts_the_activation_switch_when_applying_fails() -> None:
    """应用失败时把自动启停开关拨回实际生效的状态, 并说明原因."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])

    window._on_apply_activation = lambda enabled: "写配置失败"
    window._activation_switch.select()
    window._on_activation_toggled()
    _pump(app)

    assert window._activation is False
    assert bool(window._activation_switch.get()) is False
    assert "写配置失败" in window._activation_label.cget("text")


def test_auto_activation_polls_only_when_switched_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """自动启停只在开关打开时轮询: 关着时一次进程表都不枚举, 打开后后台轮询启动.

    用真实的 SQL 后端(注入可数的假进程表), 因此覆盖"界面定时器 → 后台线程 →
    服务层判断"这整条接线, 而不是只测策略本身。
    """
    from archive_management.domain import Game, SaveLocation
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.repository import (
        GameRepository,
        SaveLocationRepository,
    )
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    calls: list[str] = []

    def provider() -> list[str]:
        calls.append("probe")
        return []

    db = Database(tmp_path / "activation.db")
    db.migrate()
    game = GameRepository(db).add(Game(name="Demo", enabled=True))
    assert game.id is not None
    # 没有存档位置的游戏不参与监控(见 PLAN 的 G-8), 这里必须给上一条路径。
    SaveLocationRepository(db).add(SaveLocation(game_id=game.id, path="C:/Saves/Demo"))
    service = SqlArchiveService(
        db, backup_root=tmp_path / "backups", process_provider=provider
    )
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        assert app._activation is False
        # 关着的时候多跑几轮定时器: 一次都不该探测. 开关打开后立刻轮询一次
        # (不用等满一个间隔), 否则"刚打开却没反应"看起来就像坏了.
        for _index in range(6):
            app._poll_messages()
            _pump(app)
            time.sleep(0.02)
        assert calls == []

        app._on_activation_change(True)
        assert _wait_for(app, lambda: bool(calls), seconds=3.0)
        assert _wait_for(app, lambda: not app._activation_busy, seconds=3.0)
    finally:
        app.destroy()


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

    # 切换语言同样不忽略缓存(译名按语言分条, 新语言缺条目的才联网).
    assert app._on_language_change("en") is None
    _pump(app)

    assert service.calls == [False, False]


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
    app.backend.delete_game(
        "outer-wilds", app.backend.delete_export_path("outer-wilds")
    )
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
        base_font_px=app._base_font_px,
        debug=app._debug,
        activation=app._activation,
        remember_window=app._remember_window,
        shortcuts=app._shortcuts,
        on_toggle_theme=app._on_toggle_theme,
        on_apply_language=app._on_language_change,
        on_apply_font_size=app._on_font_size_change,
        on_apply_debug=app._on_debug_change,
        on_apply_activation=app._on_activation_change,
        on_apply_remember_window=lambda _enabled: None,
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

    # 界面字号/界面语言两个下拉框也要跟着换配色: 它们不在 UiKit 的重绘表里, 漏在
    # restyle 之外时同一个窗口里会留着上一套主题的底色(用户实测: 要重开窗口才恢复)。
    from archive_management.ui.palette import Palette

    palette = Palette.for_theme(app._theme)
    for box in (window._font_box, window._language_box):
        assert box.cget("fg_color") == palette.input_bg
        # 边界走**输入控件**那一份颜色: 面板/卡片那份 ``border`` 只有 1.28:1,
        # 用它是"边界看不见"的老毛病(见 palette 里 input_border 的取值说明)。
        assert box.cget("border_color") == palette.input_border
        assert box.cget("button_color") == palette.raised
        assert box.cget("text_color") == palette.text_body
        # 展开后的那层菜单同样要换: 只改外框时点开还是旧配色。
        menu = box._dropdown_menu
        assert menu.cget("fg_color") == palette.panel
        assert menu.cget("text_color") == palette.text_body


def test_settings_window_fits_its_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置窗口的高度按内容算: 底部说明与"关闭"按钮必须在窗口里, 说明也不能被裁.

    说明文字会随语言换行(实测中文 630px、英文 644px), 写死高度时"关闭"按钮会被推到
    窗口外面 —— 用户看不到也点不到; 界面语言的说明还会被右侧下拉框挤掉一截。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    # 窗口要先真的被映射/布局, 下面的尺寸才有意义(winfo_width 在未布局时是 1).
    assert _wait_for(app, lambda: window._note_label.winfo_width() > 1), "窗口未布局"

    frame = window._window
    frame.update_idletasks()
    bottom = frame.winfo_rooty() + frame.winfo_height()
    for name, widget in (
        ("说明", window._note_label),
        ("关闭按钮", window._close_btn),
    ):
        edge = widget.winfo_rooty() + widget.winfo_height()
        hint = f"{name}被推出窗口: {edge} > {bottom}"
        assert edge <= bottom, hint
    for name, label in (
        ("外观说明", window._appearance_hint),
        ("界面语言说明", window._language_hint),
        ("快捷键说明", window._shortcut_hint),
    ):
        cut = label.winfo_reqwidth() - label.winfo_width()
        hint = f"{name}被裁掉 {cut}px(换行宽度超过了可用宽度)"
        assert cut <= 1, hint


def test_settings_window_hints_follow_a_narrow_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """说明的换行宽度要跟着**它自己那一格**走: 一格只有 228px 时不能还按 240 排.

    CI 的 macOS runner 上说明那一格比 Windows 窄 16px(控件度量不同), 那时写死的
    ``wraplength=240`` 会让 Tk 按 240 排成一行, 再在标签边界处硬裁掉右边 12px: 既不换行
    也没有省略号, 后半句直接看不到(2026-10-01 的 :func:`test_settings_window_fits_its_content`
    报的就是"界面语言说明被裁掉 12px")。本机桌面宽, 原样永远量不出那一格, 所以这里把右列
    两个下拉框各撑宽 16px 来复现同一个窄列 —— 量的是"说明有没有跟着这一格换行", 不是控件
    宽度本身。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    assert _wait_for(app, lambda: window._note_label.winfo_width() > 1), "窗口未布局"

    # 右列变宽 = 左边那一格被挤窄(macOS 上由控件度量造成, 这里手动复现)。
    window._language_box.configure(width=156)
    window._font_box.configure(width=156)

    narrow = (
        ("外观说明", window._appearance_hint),
        ("界面语言说明", window._language_hint),
        ("日志说明", window._logging_hint),
        ("自动启停说明", window._activation_hint),
    )
    wide = (
        ("界面语言当前值", window._language_label),
        ("调试状态", window._debug_label),
        ("启停状态", window._activation_label),
        ("快捷键说明", window._shortcut_hint),
        ("页脚说明", window._note_label),
    )
    # 界面语言的说明文案在 240 宽度下正好要 240px —— 报错里那 12px 就是从这里来的,
    # 所以它这一格一窄过 240 就是"按 240 排、右边被裁"的现场。
    language = window._language_hint
    # 240 是**设计**值(逻辑像素), 而 ``winfo_width`` 是物理像素: 125% 的屏上窄列
    # 实测 285(=228 逻辑), 拿逻辑值直接比就是假红。
    narrow_limit = scaled_px(language, 240)
    assert _wait_for(app, lambda: 1 < language.winfo_width() < narrow_limit), (
        f"界面语言说明这一格没被挤窄: {language.winfo_width()}"
    )
    # 窄列已经成立, 现在等说明按这一格重排完(延后到 idle 才量宽)。
    cut = language.winfo_reqwidth() - language.winfo_width()
    assert _wait_for(
        app, lambda: language.winfo_reqwidth() <= language.winfo_width()
    ), (
        f"说明的换行宽度没跟着被挤窄的那一格收窄: 这一格 {language.winfo_width()}px, "
        f"说明要 {language.winfo_reqwidth()}px(裁掉 {cut}px)"
    )
    for name, label in narrow + wide:
        cut = label.winfo_reqwidth() - label.winfo_width()
        assert cut <= 1, f"{name}被裁掉 {cut}px(换行宽度没跟着这一格走)"


def test_settings_window_scrollbar_only_when_the_content_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """屏幕够高(窗口按内容定高)时不该立着一条拖不动的滚动条(16 号评审).

    窗口没映射时 ``winfo_ismapped()`` 恒为 0(假绿), 因此先等到窗口真的在屏幕上,
    再断言滚动条与“内容是否溢出”一致。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    # 把屏幕报得很高: 窗口高度会等于内容高度, 滚动区刚好装下.
    monkeypatch.setattr(app, "winfo_screenheight", lambda: 4000)
    window = _settings_window(app, [])
    assert _wait_for(app, lambda: window._window.winfo_ismapped()), "窗口未映射"

    canvas = window._body._parent_canvas
    region = canvas.bbox("all")
    content = 0 if region is None else int(region[3]) - int(region[1])
    assert scrollbar_needed(content, int(canvas.winfo_height())) is False
    assert _wait_for(app, lambda: not window._body._scrollbar.winfo_ismapped()), (
        "内容装得下却仍立着滚动条"
    )


def test_settings_window_scrollbar_appears_when_the_content_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """屏幕不够高时窗口被夹住, 内容必须能滚动(不能把底部说明与"关闭"推出去)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    monkeypatch.setattr(app, "winfo_screenheight", lambda: 520)
    window = _settings_window(app, [])
    assert _wait_for(app, lambda: window._window.winfo_ismapped()), "窗口未映射"

    canvas = window._body._parent_canvas
    region = canvas.bbox("all")
    content = 0 if region is None else int(region[3]) - int(region[1])
    assert scrollbar_needed(content, int(canvas.winfo_height())) is True
    assert _wait_for(app, lambda: window._body._scrollbar.winfo_ismapped()), (
        "内容溢出却没有滚动条"
    )
    frame = window._window
    frame.update_idletasks()
    bottom = frame.winfo_rooty() + frame.winfo_height()
    edge = window._close_btn.winfo_rooty() + window._close_btn.winfo_height()
    assert edge <= bottom, f"关闭按钮被推出窗口: {edge} > {bottom}"


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


def test_invalid_config_entries_are_repaired_on_startup(tmp_path: Path) -> None:
    """启动时只写坏一项: 剔除那一项并提示用户, 而不是把整份配置都冲掉."""
    from archive_management.config import load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    paths.config_path.write_text(
        '{"version": 1, "theme": "neon", "language": "en"}', encoding="utf-8"
    )
    before = current_locale()
    app = gui_app(
        _new_app,
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
        paths=paths,
    )
    assert app._shortcuts[ACTION_SAVE_NOW] == DEFAULT_SAVE_ACCELERATOR
    assert app._shortcuts[ACTION_CREATE_BRANCH] == DEFAULT_BRANCH_ACCELERATOR
    # 提示是按"读配置那一刻"的语言渲染的(之后才切到配置里的语言)
    set_locale(before)
    assert app._last_feedback[1] == tr("config.repaired", count=1, fields="theme")
    assert (paths.config_dir / "config.json.invalid").is_file()
    repaired = load_config(paths.config_path)
    assert repaired.theme == "system"
    assert repaired.language == "en"


def test_unreadable_config_is_reset_to_defaults_on_startup(tmp_path: Path) -> None:
    """整份文件都读不出来时: 还原为默认值并提示用户, 而不是静默回落."""
    from archive_management.config import AppConfig, load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    paths.config_path.write_text("{", encoding="utf-8")
    before = current_locale()
    app = gui_app(
        _new_app,
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
        paths=paths,
    )
    set_locale(before)
    assert app._last_feedback[1] == tr("config.reset")
    assert (paths.config_dir / "config.json.invalid").is_file()
    assert load_config(paths.config_path) == AppConfig()


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
    # 没有排期时给出口径, 而不是一个像加载失败的短横线(13/18 号评审).
    assert app._task_next.cget("text") == tr("task.next_none")
    assert app._task_next.cget("text") != "—"


def test_backup_restore_branch_export_report_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主窗口动作按钮点击后进入成功反馈并复位忙碌状态."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import FeedbackKind

    _patch_dialogs(monkeypatch, export_path=str(tmp_path / "Demo.archive.zip"))
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


def test_deleting_a_game_shows_the_package_path_and_really_exports_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实后端: 确认框里给出告别包的具体落点, 确认后先写出包再删记录.

    这条用例盯的是"先导出"这件事在界面路径上真的发生了: 包里的内容可回读, 而游戏
    与它的备份记录在删除后一条不剩。
    """
    from archive_management.services.export_format import read_package

    service, game_id, _save = _real_service_with_one_backup(monkeypatch, tmp_path)
    messages: list[str] = []
    details: list[str] = []

    def record_confirm(*_args: Any, **kwargs: Any) -> bool:
        """记下确认框文案并直接同意."""
        messages.append(str(kwargs.get("message", "")))
        # 告别包的落点按 24 号评审单独一行给出(带底色), 不再塞进正文句子里。
        details.append(str(kwargs.get("detail", "")))
        return True

    monkeypatch.setattr(mgr_mod, "confirm_dialog", record_confirm)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        manager = _manage_game_window(
            app, game_id=game_id, name="真实游戏", enabled=True
        )
        _pump(app)

        manager._on_delete_game()
        _pump(app)

        # ① 确认框里必须写明包会落在哪里(用户要能先看到再决定).
        assert messages, "删除前必须先弹确认框"
        assert str(tmp_path / "exports") in details[-1]
        # ② 确认后真的写出了一份可以回读的完整包.
        packages = list((tmp_path / "exports").glob("*.archive.zip"))
        assert len(packages) == 1, f"告别包应当恰好一份: {packages}"
        assert packages[0].name in details[-1]
        contents = read_package(packages[0], verify_hashes=True)
        assert contents.game["name"] == "真实游戏"
        assert len(contents.config_list("backups")) == 1
        # ③ 记录确实删掉了(而磁盘上的备份包还在).
        assert service.list_games() == []
    finally:
        app.destroy()


# -- 游戏启停分页(PLAN 阶段 G-8) ----------------------------------------------


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
            app.backend.delete_game(
                game.game_id, app.backend.delete_export_path(game.game_id)
            )
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
        app.backend.delete_game(
            game.game_id, app.backend.delete_export_path(game.game_id)
        )

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


def _real_service_with_one_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    export_path: str | None = None,
) -> tuple[ArchiveService, str, Path]:
    """建一个真实后端(一款游戏 + 一个存档位置 + 一次备份)并打好对话框替身.

    返回后端、游戏 id 与存档目录。导出用例需要"真的写出文件", 所以这里不能用演示
    后端: 它不碰文件系统, 无法验证包是否落在用户选的路径上。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch, export_path=export_path)
    db = Database(tmp_path / "export.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    game = service.add_game("真实游戏")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")
    service.run_backup_now(game.game_id)
    return service, game.game_id, save_dir


def test_sql_backend_backup_creates_node_and_unfinished_actions_report_clearly(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实后端: 备份落地为节点, 导出写出真实归档包并给出成功反馈."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.models import FeedbackKind
    from archive_management.ui.sql_backend import SqlArchiveService

    destination = tmp_path / "真实游戏.archive.zip"
    _patch_dialogs(monkeypatch, export_path=str(destination))
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

        # 导出: 用户在保存对话框里选好文件后, 真的写出一个归档包.
        app._on_export()
        _drain(app)
        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        assert destination.is_file()
        assert not app._busy
    finally:
        app.destroy()


def test_gui_export_writes_the_package_the_user_picked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """点「导出游戏」后落地的必须是用户选的那个文件, 反馈里带真实计数."""
    from archive_management.services.export_format import read_package
    from archive_management.ui.models import FeedbackKind, size_label

    service, game_id, save_dir = _real_service_with_one_backup(monkeypatch, tmp_path)
    destination = tmp_path / "saves" / "真实游戏.archive.zip"
    seen: dict[str, Any] = {}

    def save_file(**kwargs: Any) -> str:
        """记录对话框入参并返回用户"选定"的路径."""
        seen.update(kwargs)
        destination.parent.mkdir(exist_ok=True)
        return str(destination)

    monkeypatch.setattr(main_mod, "pick_save_file", save_file)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game_id)

        app._on_export()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        assert destination.is_file(), "导出包必须写在用户选的路径上"
        assert not (save_dir / destination.name).exists(), "不该写到存档目录里"
        contents = read_package(destination, verify_hashes=True)
        assert contents.game["name"] == "真实游戏"
        assert len(contents.config_list("backups")) == 1
        # 保存对话框预填的名字按游戏名派生(用户仍可改目录, 这里就改到了 saves/).
        assert str(seen["initialfile"]).endswith(".archive.zip")
        assert app._last_feedback[1] == tr(
            "result.export_done",
            name="真实游戏",
            file=destination.name,
            backups=1,
            files=len(contents.entries),
            size=size_label(sum(entry.size for entry in contents.entries)),
        )
    finally:
        app.destroy()


def test_gui_export_cancelled_by_the_picker_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """保存对话框取消: 不写任何文件、不进忙碌态, 反馈说明是用户取消的."""
    from archive_management.ui.models import FeedbackKind

    service, game_id, _save = _real_service_with_one_backup(
        monkeypatch, tmp_path, export_path=None
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game_id)

        app._on_export()
        _pump(app)

        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_canceled")
        assert app._busy is False
        assert list(tmp_path.glob("*.zip")) == []
        assert list(tmp_path.glob("*.partial-*")) == []
    finally:
        app.destroy()


def _exported_package(tmp_path: Path, *, app_id: int | None = 730) -> Path:
    """造一台源机器并导出包(一款游戏 + 一个存档位置 + 一次备份).

    导入侧一律用真实后端: 演示后端既不写文件也不写库, 证明不了"游戏与备份真的
    出现了"。源游戏带 AppID 时导入体检才能匹配到库里的同一款(合并用例要用)。
    """
    from archive_management.domain import Game
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.repository import GameRepository
    from archive_management.ui.sql_backend import SqlArchiveService

    root = tmp_path / "source"
    database = Database(root / "app.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=root / "backups")
    game = GameRepository(database).add(
        Game(
            name="源游戏",
            steam_app_id=app_id,
            platform="windows",
            origin="steam",
            enabled=True,
        )
    )
    assert game.id is not None
    save = root / "save"
    save.mkdir(parents=True)
    (save / "slot1.dat").write_text("state-0", encoding="utf-8")
    service.add_location(str(game.id), path=str(save), kind="directory")
    service.run_backup_now(str(game.id))
    package = tmp_path / "源游戏.archive.zip"
    service.run_export(str(game.id), str(package))
    return package


def _empty_real_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **dialogs: Any
) -> tuple[Any, Any]:
    """一个真实的空后端(库里没有游戏)并打好对话框替身; 返回后端与数据库."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch, **dialogs)
    database = Database(tmp_path / "import.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=tmp_path / "import-backups")
    return service, database


def test_gui_import_package_creates_the_game_and_selects_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """点「导入归档包…」: 选包 → 体检 → 冲突对话框确认 → 游戏与备份真的出现."""
    package = _exported_package(tmp_path)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> ImportChoice:
        """记录界面拼给对话框的文案与选项, 并按用户的选择返回."""
        seen.update(kwargs)
        return ImportChoice(
            strategy=STRATEGY_NEW,
            target_game_id=None,
            locations={0: str(target_save)},
        )

    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package)
    )
    monkeypatch.setattr(main_mod, "import_package_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        games = service.list_games()
        assert [game.name for game in games] == ["源游戏"]
        assert app._game_id == games[0].game_id, "导入完要选中新建的游戏"
        assert len(_service_backups(service, games[0].game_id)) == 1
        # 用户映射到哪个目录就写哪个目录(不是包里那台机器的路径).
        assert [item.path for item in service.list_locations(games[0].game_id)] == [
            normalize_path(str(target_save))
        ]
        # 提示里的计数来自真实后端, 不是界面自己编的.
        assert "已导入「源游戏」" in app._last_feedback[1]
        assert "备份 1 份" in app._last_feedback[1]
        assert _home_item(app._home_page, games[0].game_id).backup_count == 1
        # 对话框拿到的是这个包的摘要; 库里没有可合并的游戏时不给"合并"选项.
        assert "源游戏" in seen["prompt"].summary
        assert [key for key, _text in seen["strategies"]] == [
            STRATEGY_NEW,
            STRATEGY_SKIP,
        ]
        assert seen["prompt"].targets == ()
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_import_package_merges_into_the_matched_game(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """库里已有同平台同 AppID 的游戏: 预选它合并, 导入后选中的也是它."""
    from archive_management.domain import Game
    from archive_management.infrastructure.repository import GameRepository

    package = _exported_package(tmp_path, app_id=730)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    (target_save / "slot1.dat").write_text("state-1", encoding="utf-8")
    service, database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package)
    )
    existing = GameRepository(database).add(
        Game(
            name="库里的源游戏",
            steam_app_id=730,
            platform="windows",
            origin="steam",
            enabled=True,
        )
    )
    assert existing.id is not None
    game_id = str(existing.id)
    service.add_location(game_id, path=str(target_save), kind="directory")
    service.run_backup_now(game_id)
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> ImportChoice:
        seen.update(kwargs)
        return ImportChoice(
            strategy=STRATEGY_MERGE,
            target_game_id=game_id,
            locations={0: str(target_save)},
        )

    monkeypatch.setattr(main_mod, "import_package_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game_id)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        assert [game.name for game in service.list_games()] == ["库里的源游戏"]
        assert len(service.list_backups(game_id)) == 2, "合并是追加一份, 不是另建游戏"
        assert app._game_id == game_id, "导入完要停在合并的目标游戏上"
        assert "已导入" in app._last_feedback[1]
        assert seen["prompt"].match_text == tr(
            "dialog.import_match", name="库里的源游戏"
        )
        assert [option.game_id for option in seen["prompt"].targets] == [game_id]
        assert [option.selected for option in seen["prompt"].targets] == [True]
        assert [key for key, _text in seen["strategies"]] == [
            STRATEGY_NEW,
            STRATEGY_MERGE,
            STRATEGY_SKIP,
        ]
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_import_picker_cancelled_imports_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """没选文件: 连体检都不做, 不进忙碌态也不弹对话框."""
    opened: list[Any] = []
    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=None
    )
    monkeypatch.setattr(
        main_mod, "import_package_dialog", lambda *args, **kwargs: opened.append(args)
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _pump(app)

        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert app._busy is False
        assert opened == []
        assert service.list_games() == []

        # 忙碌时再点一次: 连文件选择框都不该弹(异步流程不能被插队).
        picked: list[Any] = []
        monkeypatch.setattr(
            main_mod, "pick_file", lambda **kwargs: picked.append(kwargs)
        )
        app._busy = True
        app._on_import_package()
        app._busy = False

        assert picked == []
    finally:
        app.destroy()


def test_gui_import_of_a_broken_package_reports_an_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """选了一个不是归档包的文件: 报错、不进忙碌态, 也不弹冲突对话框."""
    broken = tmp_path / "broken.zip"
    broken.write_text("definitely not a zip", encoding="utf-8")
    opened: list[Any] = []
    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(broken)
    )
    monkeypatch.setattr(
        main_mod, "import_package_dialog", lambda *args, **kwargs: opened.append(args)
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.ERROR
        assert app._busy is False, "读包失败也要把忙碌状态收回去"
        assert opened == []
        assert service.list_games() == []
    finally:
        app.destroy()


def test_gui_import_dialog_cancelled_imports_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """体检完用户又说取消: 什么都不写, 且不留下忙碌状态."""
    package = _exported_package(tmp_path)
    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package), import_choice=None
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert app._busy is False
        assert service.list_games() == []
        assert list((tmp_path / "import-backups").glob("**/*")) == []
    finally:
        app.destroy()


# ------------------------------------------------- 批量导出/导入 (真实后端)


def _real_service_with_games(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    names: Sequence[str],
) -> tuple[Any, list[str]]:
    """真实后端 + 若干款"各带一个存档位置与一次备份"的游戏(批量导出用例用).

    批量导出必须落在真实后端上才能断言"包真的写出来了"; 每个游戏的内容不同, 否则
    第二次备份会被"存档未变化"跳过。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    database = Database(tmp_path / "batch.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=tmp_path / "batch-backups")
    game_ids: list[str] = []
    for index, name in enumerate(names):
        game_id = service.add_game(name).game_id
        save_dir = tmp_path / f"save-{index}"
        save_dir.mkdir()
        (save_dir / "slot1.dat").write_text(f"state-{index}", encoding="utf-8")
        service.add_location(game_id, path=str(save_dir), kind="directory")
        service.run_backup_now(game_id)
        game_ids.append(game_id)
    return service, game_ids


def _exported_batch(tmp_path: Path, *, count: int = 2) -> Path:
    """造一台源机器并导出一个批量包(两款游戏各一次备份)."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    root = tmp_path / "batch-source"
    database = Database(root / "app.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=root / "backups")
    game_ids: list[str] = []
    for index in range(count):
        game_id = service.add_game(f"源游戏{index}").game_id
        save = root / f"save-{index}"
        save.mkdir(parents=True)
        (save / "slot1.dat").write_text(f"state-{index}", encoding="utf-8")
        service.add_location(game_id, path=str(save), kind="directory")
        service.run_backup_now(game_id)
        game_ids.append(game_id)
    package = tmp_path / "batch.archive.zip"
    service.run_export_batch(game_ids, str(package))
    return package


def _batch_entries(package: Path) -> tuple[str, ...]:
    """批量包里的内层条目名(按清单顺序; 跳过/合并的选择要用它当键)."""
    from archive_management.services.export_format import read_batch_package

    with read_batch_package(package) as contents:
        return tuple(item.entry for item in contents.games)


def test_gui_export_batch_writes_the_package_the_user_ticked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主页「批量导出…」: 多选对话框 → 保存位置 → 真的写出批量包, 反馈带真实计数."""
    from archive_management.services.export_format import read_batch_package
    from archive_management.ui.models import size_label

    service, game_ids = _real_service_with_games(
        monkeypatch, tmp_path, ["甲游戏", "乙游戏"]
    )
    destination = tmp_path / "saves" / "batch.archive.zip"
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> BatchExportChoice:
        """记录界面拼给对话框的候选, 并按"用户只勾了第二款"返回."""
        seen.update(kwargs)
        return BatchExportChoice(game_ids=(game_ids[1],))

    def save_file(**kwargs: Any) -> str:
        """记录保存框的入参并返回用户"选定"的路径."""
        seen.update(kwargs)
        destination.parent.mkdir(exist_ok=True)
        return str(destination)

    monkeypatch.setattr(main_mod, "export_batch_dialog", choose)
    monkeypatch.setattr(main_mod, "pick_save_file", save_file)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._home_page._export_batch_btn.invoke()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        assert destination.is_file(), "批量包必须写在用户选的路径上"
        with read_batch_package(destination, verify_hashes=True) as contents:
            names = [item.name for item in contents.games]
            files = sum(len(item.package.entries) for item in contents.games)
            size = sum(
                entry.size for item in contents.games for entry in item.package.entries
            )
        assert names == ["乙游戏"], "只该导出用户勾选的那一款"
        assert app._last_feedback[1] == tr(
            "result.export_batch_done",
            games=1,
            file=destination.name,
            backups=1,
            files=files,
            size=size_label(size),
        )
        # 对话框拿到的是全部候选, 默认文件名带批次与后缀.
        assert [option.name for option in seen["prompt"].options] == [
            "甲游戏",
            "乙游戏",
        ]
        assert str(seen["initialfile"]).startswith("batch-1games-")
        assert str(seen["initialfile"]).endswith(".archive.zip")
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_export_batch_cancel_paths_write_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """对话框取消 / 一个都没勾 / 保存框取消: 三种都不写文件、不进忙碌态."""
    service, game_ids = _real_service_with_games(monkeypatch, tmp_path, ["甲游戏"])

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        def run(dialog_result: Any) -> None:
            """换一个对话框返回值再点一次「批量导出…」."""
            monkeypatch.setattr(
                main_mod, "export_batch_dialog", lambda *_a, **_k: dialog_result
            )
            app._home_page._export_batch_btn.invoke()
            _drain(app)
            assert app._busy is False

        run(None)
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_canceled")

        run(BatchExportChoice(game_ids=()))
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_none")

        # 保存框取消: 对话框已经确认过(勾了一款), 但文件选择框被取消.
        run(BatchExportChoice(game_ids=(game_ids[0],)))
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_canceled")

        assert list(tmp_path.glob("*.archive.zip")) == []
        assert list(tmp_path.glob("**/*.partial-*")) == []
    finally:
        app.destroy()


def test_gui_export_batch_hides_archived_games_and_reports_an_empty_library(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """归档的游戏不提供; 一款可导的都没有时连对话框都不弹."""
    service, game_ids = _real_service_with_games(
        monkeypatch, tmp_path, ["甲游戏", "乙游戏"]
    )
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> BatchExportChoice:
        seen.update(kwargs)
        return BatchExportChoice(game_ids=())

    monkeypatch.setattr(main_mod, "export_batch_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        service.set_game_archived(game_ids[1], True)

        app._on_export_batch()

        assert [option.name for option in seen["prompt"].options] == ["甲游戏"]

        # 只剩下归档的那一款时: 直接说明原因, 不弹一个空对话框.
        opened: list[Any] = []
        monkeypatch.setattr(
            main_mod, "export_batch_dialog", lambda *args, **_k: opened.append(args)
        )
        service.set_game_archived(game_ids[0], True)

        app._on_export_batch()

        assert opened == []
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_empty")
    finally:
        app.destroy()


def test_gui_import_batch_imports_the_whole_batch_per_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """批量导入: 游戏与备份真的出现, 逐款的策略与位置映射都被照办."""
    from archive_management.application.imports import (
        STRATEGY_NEW,
        STRATEGY_SKIP,
    )

    package = _exported_batch(tmp_path)
    entries = _batch_entries(package)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> BatchImportSelection:
        """记录界面拼给对话框的逐行选项, 并按"第一行新建+映射, 第二行跳过"返回."""
        seen.update(kwargs)
        return BatchImportSelection(
            choices={
                entries[0]: ImportChoice(
                    strategy=STRATEGY_NEW,
                    target_game_id=None,
                    locations={0: str(target_save)},
                ),
                entries[1]: ImportChoice(
                    strategy=STRATEGY_SKIP, target_game_id=None, locations={}
                ),
            }
        )

    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package)
    )
    monkeypatch.setattr(main_mod, "batch_import_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        games = service.list_games()
        assert [game.name for game in games] == ["源游戏0"], "跳过那一款不该进库"
        assert len(_service_backups(service, games[0].game_id)) == 1
        assert [item.path for item in service.list_locations(games[0].game_id)] == [
            normalize_path(str(target_save))
        ]
        # 只新建了一款, 因此导入完就停在它上面.
        assert app._game_id == games[0].game_id
        assert "已导入 2 款游戏" in app._last_feedback[1]
        assert "整款跳过 1 款" in app._last_feedback[1]
        assert _home_item(app._home_page, games[0].game_id).backup_count == 1
        # 对话框拿到的逐行数据: 两款游戏各一行, 存档位置已经预填到本机路径上.
        rows = seen["prompt"].rows
        assert [row.entry for row in rows] == list(entries)
        assert rows[0].locations[0].default == normalize_path(
            str(tmp_path / "batch-source" / "save-0")
        )
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_import_batch_cancel_paths_import_nothing_extra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """取消的三条路径: 没选包 / 对话框取消 / 导入中途取消(已导完的保留)."""
    from archive_management.application.imports import STRATEGY_NEW

    package = _exported_batch(tmp_path)
    entries = _batch_entries(package)
    target_save = tmp_path / "target-save"
    target_save.mkdir()

    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=None
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        # 1) 没选包: 连体检都不做.
        app._on_import_package()
        _pump(app)
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert service.list_games() == []
        assert app._busy is False

        # 2) 对话框取消: 什么都不导入.
        monkeypatch.setattr(main_mod, "pick_file", lambda **_k: str(package))
        monkeypatch.setattr(main_mod, "batch_import_dialog", lambda *_a, **_k: None)
        app._on_import_package()
        _drain(app)
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert service.list_games() == []
        assert app._busy is False

        # 3) 取消探针一响: 整批停下, 界面给出"已取消"的提示且不留在忙碌态.
        #
        #    这里**不抢时序**: 早先这条路径靠 time.sleep 造出来的窗口"卡在导入进行中",
        #    满负载(全量 + 覆盖率)下工作线程会先把整批导完, 于是断言"其余的不再导入"
        #    红过(实测 `assert ['源游戏0', '源游戏1'] == ['源游戏0']`)。
        #    "取消落在第一款之后、已导完的保留"那条**按游戏边界**的语义由
        #    tests/integration/test_pipeline_batch.py 的
        #    test_cancelling_between_games_keeps_the_finished_one_and_writes_nothing_partial
        #    确定性地钉住(探针看数据库, 不靠时间); 这一段守的是界面侧的接线:
        #    探针一响 → 后端回"已取消" → 窗口回到非忙碌态。
        def probe() -> bool:
            """取消探针的替身: 第一条就问一次取消, 导入立刻停下."""
            return True

        monkeypatch.setattr(service, "_cancel_requested", probe)
        monkeypatch.setattr(
            main_mod,
            "batch_import_dialog",
            lambda *_a, **_k: BatchImportSelection(
                choices={
                    entry: ImportChoice(
                        strategy=STRATEGY_NEW,
                        target_game_id=None,
                        locations={0: str(target_save)},
                    )
                    for entry in entries
                }
            ),
        )

        app._on_import_package()
        _drain(app)

        assert service.list_games() == [], "取消之后一款都不该落库"
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("result.import_batch_canceled")
        assert app._busy is False
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
        # 游戏目录用名称命名(而不是数字 id); 界面上只说"由应用自动命名", 真实
        # 目录名收在悬停提示里(13 号评审).
        assert snapshot_root.parent.name.startswith("快照游戏-")
        assert snapshot_root.parent.name != game.game_id
        assert app._hero_origin_label.cget("text") == tr("hero.storage_folder")
        assert snapshot_root.parent.name in app._origin_tip
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

        # 时间线用卡片(安全点画得出来), 分支图的框里默认没有它。
        app._switch_view(ViewKind.TIMELINE)
        _pump(app)
        assert safety_id in app._cards
        app._switch_view(ViewKind.BRANCH)
        _pump(app)
        assert safety_id not in app._tree_view.node_ids
        # 显式筛选"恢复前安全点"时, 分支图也能定向到它们(筛选是用户的明确要求).
        app._filter_source.set(SourceFilter.SAFETY.label)
        app._on_filter_change(SourceFilter.SAFETY.label)
        _pump(app)
        assert safety_id in app._tree_view.node_ids
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


def test_settings_window_skips_unchanged_values_and_rolls_failures_back() -> None:
    """设置窗口: 值没变/选择无效就不动作, 写回失败要把控件拨回实际状态并说明原因."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.settings_window import SettingsWindow

    applied: list[tuple[str, object]] = []

    def last() -> tuple[str, object]:
        """最近一次写回(经函数返回, 避开 mypy 对元组索引做字面量收窄)."""
        assert applied
        return applied[-1]

    def fail_language(locale: str) -> str | None:
        applied.append(("language", locale))
        return "语言失败"

    def fail_font(size: int) -> str | None:
        applied.append(("font", size))
        return "字号失败"

    def fail_debug(wanted: bool) -> str | None:
        applied.append(("debug", wanted))
        return "调试失败"

    def fail_activation(wanted: bool) -> str | None:
        applied.append(("activation", wanted))
        return "启停失败"

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = SettingsWindow(
        app,
        palette=app.p,
        theme=app._theme,
        language=app._language,
        base_font_px=16,
        debug=True,
        activation=True,
        remember_window=True,
        shortcuts=app._shortcuts,
        on_toggle_theme=lambda: app._theme,
        on_apply_language=fail_language,
        on_apply_font_size=fail_font,
        on_apply_debug=fail_debug,
        on_apply_activation=fail_activation,
        on_apply_remember_window=lambda _enabled: None,
        on_apply_shortcut=lambda action, accelerator: None,
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )
    try:
        # ① 语言: 不认识的值与"当前语言"都不写回; 换一种语言则回调报错 → 显示原因.
        window._on_language_selected("不认识")
        window._on_language_selected(window._locale_label(app._language))
        other_locale = next(
            label
            for label, locale in window._locales.items()
            if locale != app._language
        )
        window._on_language_selected(other_locale)
        assert last() == ("language", window._locales[other_locale])
        assert tr("settings.language_failed", reason="语言失败") in _label_texts(
            window._container
        )

        # ② 字号: 不认识/与当前一致都不写回; 选新字号但回调报错 → 下拉拨回当前值.
        current_label = window._font_label_of(16)
        other_label = next(
            label for label in window._font_box.cget("values") if label != current_label
        )
        window._on_font_selected("不认识")
        window._on_font_selected(current_label)
        window._on_font_selected(other_label)
        kind, size = last()
        assert kind == "font"
        assert size != 16, "选中的确实是另一个字号"
        assert window._font_box.get() == current_label

        # ③ 调试开关: 开关初值与实际一致时不动; 关掉后回调报错 → 开关拨回"开".
        window._on_debug_toggled()
        window._debug_switch.deselect()
        window._on_debug_toggled()
        assert last() == ("debug", False)
        assert bool(window._debug_switch.get()) is True
        assert tr("settings.debug_failed", reason="调试失败") in _label_texts(
            window._container
        )

        # ④ 自动启停同一套.
        window._on_activation_toggled()
        window._activation_switch.deselect()
        window._on_activation_toggled()
        assert last() == ("activation", False)
        assert bool(window._activation_switch.get()) is True
        assert tr("settings.activation_failed", reason="启停失败") in _label_texts(
            window._container
        )

        # ⑤ 录制收尾的定时器可能比录制活得久: 没在录制时什么都不做.
        window._finish_when_idle()
        assert window._capturing is None
    finally:
        window.close()
        app.destroy()


def _close_toplevels(app: Any) -> None:
    """关掉用例自己打开的附属窗口(主窗口留给夹具收尾).

    Tk/CustomTkinter 在同一个进程里跨多个根窗口时会留全局状态: 用例开过的附属
    窗口不关, 后续用例建窗口时就可能撞上 `image "pyimageN" doesn't exist` ——
    以前这类报错会被当成"环境不可用"跳过, 现在会真的红(见 tests/tk_guard.py), 所以
    用例自己开的窗口要自己关。
    """
    for child in list(app.winfo_children()):
        if isinstance(child, ctk.CTkToplevel):
            child.destroy()


def test_home_page_covers_section_and_action_guards(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """主页的边角守卫: 没接回调的分区切换/同调色板不重建/无选中项的动作/表头对齐的止损."""
    from archive_management.exceptions import ArchiveManagementError
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.home_page import _ALIGN_MAX_ATTEMPTS, HomePage
    from archive_management.ui.models import HomeSection
    from archive_management.ui.palette import Palette

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    palette = Palette.for_theme(app._theme)
    # 不接任何回调的独立主页: 分区切换、发现分区改动、同调色板重设都不该崩.
    page = HomePage(ctk.CTkFrame(app), backend=app.backend, palette=palette)
    try:
        page._show_section(HomeSection.ACTIVATION)
        page._show_section(HomeSection.LIBRARY)
        page._after_discovery_change()
        page.apply_palette(palette)
        assert page._on_change is None
        assert page._on_activation_refresh is None

        # 主页数据读不出来时只记日志: 不重绘、也不凭空造一份 board.
        def broken_load() -> None:
            raise ArchiveManagementError("读不到主页数据")

        page._board = None
        monkeypatch.setattr(app.backend, "load_home", broken_load)
        page.refresh_artwork()
        assert page._board is None

        main = app._home_page
        # 没有选中任何游戏时: 六个动作都只提示, 不写库不开窗口.
        main._select("")
        main._on_backup()
        main._on_add_location()
        main._on_manage()
        main._on_detail()
        assert tr("home.require_game") in _label_texts(main.frame)

        # 补上"选中了游戏"的那一半: 加位置的输入框取消 / 真的选了一个目录 / 打开设置窗口.
        main._select("outer-wilds")
        _pump(app)
        _patch_dialogs(monkeypatch, ask_text="")
        before = len(app.backend.list_locations("outer-wilds"))
        main._on_add_location()
        assert len(app.backend.list_locations("outer-wilds")) == before, (
            "取消时不该加位置"
        )
        new_dir = tmp_path / "new-save"
        new_dir.mkdir()
        _patch_dialogs(monkeypatch, ask_text=str(new_dir))
        main._on_add_location()
        _pump(app)
        assert len(app.backend.list_locations("outer-wilds")) == before + 1
        main._on_manage()
        _pump(app)
        _close_toplevels(app)

        # 排版下拉给了不认识的值 / 上一页 / 选中当前分类: 三处都只在守卫里返回.
        main._on_layout_change("不认识的排版")
        main._page_index = 1
        main._on_prev_page()
        assert main._page_index == 0
        main._on_category_change(main._category_box.get())

        # 延后任务只排一次、撤销要真的撤销; 控件销毁事件只认自己那一层.
        main._schedule_list_sync()
        main._schedule_list_sync()
        main.cancel_list_sync()
        main._on_frame_destroyed(SimpleNamespace(widget=main.frame))

        # 表头对齐的两处止损: 调太多次 / 已经调到边上.
        main._align_attempts = _ALIGN_MAX_ATTEMPTS
        main._align_table_header()
        main._align_attempts = 0
        main._align_table_header()
        main._align_table_header()

        # 不存在的行: 重裁与启用对手查找都要安静返回.
        main._on_name_resize("不存在的游戏", SimpleNamespace(width=120))
        board = main._board
        main._board = None
        assert main._enabled_rival(main._item() or object()) is None
        main._board = board
    finally:
        page.frame.destroy()
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


# --------------------------------- 补分支: 界面层剩下的边角与守卫(逐条钉住)


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


def test_settings_writes_reach_the_config_file(tmp_path: Path) -> None:
    """有配置路径时三个写回口真的落盘(字号 / 调试日志 / 自动启停)."""
    from archive_management.config import load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    app = gui_app(_new_app, DemoArchiveService(delay=0), paths=paths)
    try:
        _pump(app)
        assert app._paths is not None

        assert app._on_font_size_change(18) is None
        assert load_config(paths.config_path).ui.base_font_px == 18

        assert app._on_debug_change(True) is None
        assert load_config(paths.config_path).logging.debug is True
        app._on_debug_change(False)
        assert load_config(paths.config_path).logging.debug is False

        app._on_activation_change(True)
        _drain(app)
        assert load_config(paths.config_path).activation.auto is True
        app._on_activation_change(False)
        _drain(app)
        assert load_config(paths.config_path).activation.auto is False
    finally:
        app.destroy()


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


def test_export_batch_is_ignored_while_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """忙碌时批量导出直接返回: 连"勾选游戏"的多选对话框都不弹."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(
        main_mod, "export_batch_dialog", lambda *_a, **_k: asked.append("dialog")
    )
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app._set_busy(True)

        app._on_export_batch()

        assert asked == [], "忙碌时不该弹出批量导出对话框"
        app._set_busy(False)
        assert app._busy is False
    finally:
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


def test_save_hotkey_starts_a_backup(monkeypatch: pytest.MonkeyPatch) -> None:
    """快捷键"立即备份"那一路: 选中一款可备份的游戏时真的发起一次备份."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app._select_game("outer-wilds")
        _pump(app)
        before = len(app.backend.list_backups("outer-wilds"))

        from archive_management.services.hotkeys import ACTION_SAVE_NOW

        app._run_hotkey(ACTION_SAVE_NOW)
        _drain(app)

        assert len(app.backend.list_backups("outer-wilds")) == before + 1
    finally:
        app.destroy()


def test_confirming_a_branch_root_deletes_the_subtree(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """确认删除分支根节点后连带子树一起删掉(取消的那一半已有用例)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch, confirm=True)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app._select_game("outer-wilds")
        _pump(app)
        root = next(item for item in app._items if item.backup_id == "b3")
        app._select_backup(root)
        _pump(app)
        plan = app.backend.plan_delete("outer-wilds", "b3")
        assert plan.needs_confirmation is True
        before = len(app._items)

        app._on_delete_backup()
        _drain(app)

        assert app._last_feedback[0] == FeedbackKind.SUCCESS
        assert len(app._items) == before - len(plan.removed_ids)
    finally:
        app.destroy()


def test_restore_danger_note_flags_a_running_game() -> None:
    """预检发现游戏正在运行时, 危险提示行里要写明(用户据此决定是否强制恢复)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        plan = app.backend.preview_restore("outer-wilds", "b2")
        assert plan.process.running is True

        note = app._restore_danger_note(plan)

        expected = tr(
            "dialog.restore_process", matches=", ".join(plan.process.matches[:3])
        )
        assert expected in note
    finally:
        app.destroy()


def test_restore_with_a_blocked_target_reports_instead_of_asking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """预检报告"目标被拦下"时直接进错误提示: 不弹恢复对话框、也不起后台任务."""
    from dataclasses import replace

    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(
        main_mod, "restore_dialog", lambda *_a, **_k: asked.append("dialog")
    )
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app._select_game("outer-wilds")
        _pump(app)
        item = app._items[0]
        app._select_backup(item)
        _pump(app)
        plan = app.backend.preview_restore("outer-wilds", item.backup_id)
        blocked = replace(plan.targets[0], problem="protected")
        monkeypatch.setattr(
            app.backend,
            "preview_restore",
            lambda *_a, **_k: replace(plan, targets=(blocked,)),
        )

        app._on_restore()

        assert asked == [], "目标被拦下时不该弹恢复对话框"
        assert app._last_feedback[0] == FeedbackKind.ERROR
        expected = tr(
            "dialog.restore_blocked",
            path=blocked.path,
            reason=app._restore_problem_text(blocked.problem),
        )
        assert expected in app._last_feedback[1]
        assert app._busy is False
    finally:
        app.destroy()


def test_home_page_guards_need_forced_state() -> None:
    """主页三处守卫: 没接批量导出回调 / 选中的游戏没有可用存档位置 / 海报下没有卡片."""
    from dataclasses import replace

    from archive_management.domain import HomeLayout
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.home_page import HomePage
    from archive_management.ui.palette import Palette

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    palette = Palette.for_theme(app._theme)
    page = HomePage(ctk.CTkFrame(app), backend=app.backend, palette=palette)
    try:
        # ① 独立构造的主页没有批量导出回调: 点按钮什么都不做。
        page._request_export_batch()
        assert page._on_export_batch is None

        main = app._home_page
        main._select("outer-wilds")
        _pump(app)
        board = main._board
        assert board is not None

        # ② 选中的游戏没有可用存档位置: 只提示原因, 不起后台任务。
        main._board = replace(
            board,
            games=tuple(
                replace(game, location_count=0)
                if game.game_id == "outer-wilds"
                else game
                for game in board.games
            ),
        )
        before = len(app.backend.list_backups("outer-wilds"))
        main._on_backup()
        assert app._busy is False
        assert tr("error.home_location_required") in _label_texts(main.frame)
        assert len(app.backend.list_backups("outer-wilds")) == before

        # ③ 海报模式下还没有卡片时收到尺寸事件: 直接返回(不去重排空网格)。
        main._board = board
        main._filter = replace(main._filter, layout=HomeLayout.POSTER)
        main._rows.clear()
        main._on_list_box_resize(SimpleNamespace(width=900))
        assert main._rows == {}
    finally:
        page.frame.destroy()
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


def test_schedule_window_notifies_and_refuses_archived_items(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """定时任务窗口: 每次成功改动都通知主窗口; 归档游戏的任务不可编辑(只提示原因)."""
    from dataclasses import replace

    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, schedule_result=("1h", "3"))
    noted: list[str] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        window = ScheduleWindow(
            app,
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
            on_change=lambda: noted.append("changed"),
        )
        item = next(row for row in window._items if row.game_id == "outer-wilds")

        # ① 编辑成功。
        assert window._apply(item) is True
        assert noted == ["changed"]

        # ② 暂停/继续。
        window._select("outer-wilds")
        window._on_toggle()
        assert noted == ["changed", "changed"]

        # ③ 删除(确认后)。
        _patch_dialogs(monkeypatch, confirm=True)
        window._select("outer-wilds")
        window._on_remove()
        assert noted == ["changed", "changed", "changed"]

        # ④ 归档游戏的任务: 不可编辑, 只把原因写进提示行。
        archived = replace(item, archived=True)
        assert window._apply(archived) is False
        expected = tr("error.archived_game_schedule", name=archived.game_name)
        assert window._status_label.cget("text") == expected
    finally:
        window.close()
        app.destroy()


def test_schedule_window_blocks_toggle_and_keeps_add_cancelled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """停用中的游戏不能启用任务; 新增时配置对话框被取消就不加任务、也不改选中项."""
    import archive_management.ui.schedule_window as sched_window_mod
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    _patch_dialogs(monkeypatch, schedule_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        # 停用"outer-wilds": 任务保留周期但被强制暂停, 于是 can_toggle 为假。
        app.backend.set_game_enabled("outer-wilds", False)
        window = ScheduleWindow(
            app, backend=app.backend, palette=Palette.for_theme(app._theme)
        )
        window._select("outer-wilds")
        selected = window._selected_item()
        assert selected is not None
        assert selected.can_toggle is False
        before = app.backend.task_status("outer-wilds").schedule_enabled

        # ① 启停: 只弹提示(已替身), 配置一点没变。
        window._on_toggle()
        assert app.backend.task_status("outer-wilds").schedule_enabled == before

        # ② 新增: 选好了游戏但配置对话框被取消 → 不写库, 也不选中它。
        monkeypatch.setattr(
            sched_window_mod, "add_schedule_dialog", lambda *_a, **_k: "shanhai"
        )
        window._selected = None
        window._on_add()
        assert window._selected is None
        assert app.backend.task_status("shanhai").schedule_text == ""
    finally:
        window.close()
        app.destroy()


def test_add_schedule_dialog_handles_an_empty_blocked_list_and_typing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """新增定时任务的弹窗: 没有被拦下的游戏就不显示原因行; 输入即筛选真的过滤候选."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import (
        ScheduleWindow,
        add_schedule_dialog,
    )

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        # 多补一款可调度的游戏: 只有一个候选时"筛选"根本看不出效果。
        extra = app.backend.add_game("第二款待配置")
        save = tmp_path / "extra-save"
        save.mkdir()
        app.backend.add_location(extra.game_id, path=str(save), kind="directory")
        palette = Palette.for_theme(app._theme)
        window = ScheduleWindow(app, backend=app.backend, palette=palette)
        candidates = window._addable()
        assert len(candidates) >= 2, "候选太少就测不出筛选"
        names = [item.game_name for item in candidates]

        combos: list[Any] = []
        toplevels: list[Any] = []
        real_combo = ctk.CTkComboBox
        real_toplevel = ctk.CTkToplevel

        def make_combo(*args: Any, **kwargs: Any) -> Any:
            combo: Any = real_combo(*args, **kwargs)
            original = combo.bind

            def recording_bind(
                sequence: Any = None, command: Any = None, add: Any = True
            ) -> Any:
                if sequence == "<KeyRelease>":
                    combo.key_callback = command
                return original(sequence, command, add)

            combo.bind = recording_bind
            combos.append(combo)
            return combo

        def make_toplevel(*args: Any, **kwargs: Any) -> Any:
            top: Any = real_toplevel(*args, **kwargs)
            toplevels.append(top)
            return top

        monkeypatch.setattr(ctk, "CTkComboBox", make_combo)
        monkeypatch.setattr(ctk, "CTkToplevel", make_toplevel)
        # 弹窗不再阻塞主线程: "关闭窗口"这一步由用例自己接管。
        monkeypatch.setattr(app, "wait_window", lambda *_a, **_k: None)

        # ① 没有被拦下的游戏: 不显示原因行(窗口仍然正常打开)。
        assert (
            add_schedule_dialog(app, palette, candidates=candidates, blocked=()) is None
        )
        assert tr("dialog.schedule_add_blocked_title") not in _label_texts(
            toplevels[-1]
        )
        toplevels[-1].destroy()

        # ② 输入即筛选: 清空时回到全部候选, 输入片段时只剩命中的那一条。
        assert (
            add_schedule_dialog(app, palette, candidates=candidates, blocked=()) is None
        )
        combo = combos[-1]
        combo.set("")
        combo.key_callback(None)
        assert list(combo.cget("values")) == names
        combo.set(names[-1][1:])
        combo.key_callback(None)
        assert list(combo.cget("values")) == [names[-1]]
        toplevels[-1].destroy()
    finally:
        app.destroy()


def test_schedule_window_focus_reports_whether_it_is_open() -> None:
    """焦点请求: 窗口还在时提前台返回 True, 关掉之后再请求返回 False."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.palette import Palette
    from archive_management.ui.schedule_window import ScheduleWindow

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = ScheduleWindow(
        app, backend=app.backend, palette=Palette.for_theme(app._theme)
    )

    assert window.focus() is True

    window.close()
    assert window.focus() is False
    assert app._active_window is None
    app.destroy()


def test_discovery_panel_edits_and_removes_a_monitored_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """监控目录: 只答路径不答备注就整条不动; 两个都答完才写回; 删除要确认."""
    import archive_management.ui.discovery_page as disc_page_mod
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.palette import Palette

    _patch_dialogs(monkeypatch, confirm=True)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        target = panel._dirs[0].directory_id
        panel._select_dir(target)
        before = app.backend.list_monitored_directories()
        moved = str(tmp_path / "moved")

        # ① 备注那一步被取消: 路径虽然填了也不写回。
        answers = iter([moved, None])
        monkeypatch.setattr(disc_page_mod, "ask_text", lambda *_a, **_k: next(answers))
        panel._on_edit_dir()
        assert app.backend.list_monitored_directories() == before

        # ② 两个输入框都答完: 路径与备注一起写回。
        answers = iter([moved, "新备注"])
        panel._on_edit_dir()
        updated = next(
            item
            for item in app.backend.list_monitored_directories()
            if item.directory_id == target
        )
        assert (updated.path, updated.note) == (moved, "新备注")

        # ③ 删除: 确认后记录消失。
        panel._on_remove_dir()
        assert target not in {
            item.directory_id for item in app.backend.list_monitored_directories()
        }
    finally:
        app.destroy()


def test_discovery_panel_candidate_answers_and_counts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """候选: 取消导入不动数据 / 填了新路径就修正 / 不认识的筛选与状态都安静跳过."""
    from dataclasses import replace

    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.discovery_page import DiscoveryPanel
    from archive_management.ui.models import CandidateFilter, SavePathSuggestion
    from archive_management.ui.palette import Palette

    relocated = str(tmp_path / "relocated")
    _patch_dialogs(monkeypatch, ask_text=relocated, import_result=None)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        panel = DiscoveryPanel(
            ctk.CTkFrame(app),
            backend=app.backend,
            palette=Palette.for_theme(app._theme),
        )
        # ① 下拉框给了不认识的值: 筛选保持原样, 列表照常重绘。
        panel._on_filter_change("不认识的筛选")
        assert panel._filter is CandidateFilter.NEW

        candidate = next(item for item in panel._candidates if item.importable)
        panel._select_candidate(candidate.candidate_id)
        before = app.backend.list_candidates()

        # ② 导入对话框被取消: 候选一个字段都不变。
        panel._on_import()
        assert app.backend.list_candidates() == before

        # ③ 修正路径: 填了新路径就写回。
        panel._on_relocate()
        moved = next(
            item
            for item in app.backend.list_candidates()
            if item.candidate_id == candidate.candidate_id
        )
        assert moved.install_dir == relocated

        # ④ 后端给出三种之外的状态时, 计数只统计认识的那些。
        weird = replace(candidate, status="unknown")
        known = panel._candidates
        panel._candidates = [weird, *known]
        counts = panel._status_counts()
        panel._candidates = known
        assert sum(counts.values()) == len(known), "不认识的状态不该被算进任何一档"

        # ⑤ 有存档路径且都不危险: 路径区用成功色。
        suggestion = SavePathSuggestion(path=relocated, path_kind="directory")
        with_paths = replace(candidate, save_paths=(suggestion,))
        assert panel._save_color(with_paths) == panel._palette.success
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


def test_settings_window_keeps_a_successful_font_and_reports_a_failed_shortcut() -> (
    None
):
    """字号改成功就停在新值上(不回拨); 快捷键注册失败要把原因写进提示行."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.settings_window import SettingsWindow

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = SettingsWindow(
        app,
        palette=app.p,
        theme=app._theme,
        language=app._language,
        base_font_px=16,
        debug=False,
        activation=False,
        remember_window=True,
        shortcuts=app._shortcuts,
        on_toggle_theme=lambda: app._theme,
        on_apply_language=lambda _locale: None,
        on_apply_font_size=lambda _size: None,
        on_apply_debug=lambda _enabled: None,
        on_apply_activation=lambda _enabled: None,
        on_apply_remember_window=lambda _enabled: None,
        on_apply_shortcut=lambda _action, _accelerator: "快捷键被占用",
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )
    try:
        # ① 字号: 回调返回 None 表示成功, 下拉停在新值上。
        current = window._font_label_of(16)
        other = next(
            label for label in window._font_box.cget("values") if label != current
        )
        # 真实流程是"下拉框先选中新值, 再触发命令", 这里照做。
        window._font_box.set(other)
        window._on_font_selected(other)
        assert window._font_box.get() == other, "成功时不该把下拉拨回旧值"

        # ② 快捷键: 录到一个合法组合但注册失败 → 原因进提示行, 按钮仍是旧值。
        window._toggle_capture(ACTION_SAVE_NOW)
        _press(window, "Win_L", 91)
        _press(window, "Control_L", 17)
        _press(window, "s", 83)

        assert _wait_for(
            app, lambda: window._shortcut_error.cget("text") == "快捷键被占用"
        )
        assert window.shortcut_text(ACTION_SAVE_NOW) == format_accelerator(
            DEFAULT_SAVE_ACCELERATOR
        )
    finally:
        window.close()
        app.destroy()
