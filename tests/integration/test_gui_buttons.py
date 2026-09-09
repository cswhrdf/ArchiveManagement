"""页面按钮功能测试.

在可用图形环境下真实触发主窗口与管理窗口的各按钮, 验证点击不抛
``TclError``、不留下未复位状态, 并产生符合预期的反馈/数据变化。本
模块集中覆盖两类回归: (1) 动态列表重建后 ``UiKit`` 主题重绘不再命中
已销毁控件; (2) 真实 SQLite 后端的阶段占位动作给出明确提示而不是
崩溃。无 tkinter/图形环境自动跳过。
"""

from __future__ import annotations

import time
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
from archive_management.ui.backend import ArchiveService
from archive_management.ui.main_window import ArchiveApp

pytestmark = [
    pytest.mark.integration,
    pytest.mark.smoke,
    pytest.mark.ui,
    pytest.mark.normal,
]


def _patch_dialogs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ask_text: str = "",
    branch_name: str = "测试分支",
    confirm: bool = True,
) -> None:
    """把模态对话框替换为自动应答, 避免 wait_window 阻塞测试线程.

    需要输入路径/文本的测试应通过 ``ask_text`` 显式传入由 ``tmp_path``
    派生的跨平台路径; 未触发文本输入的动作无需关心该默认空值.
    """
    monkeypatch.setattr(main_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(main_mod, "ask_text", lambda *_a, **_k: ask_text)
    monkeypatch.setattr(main_mod, "ask_branch_name", lambda *_a, **_k: branch_name)
    monkeypatch.setattr(main_mod, "info_dialog", lambda *_a, **_k: None)
    monkeypatch.setattr(mgr_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(mgr_mod, "ask_text", lambda *_a, **_k: ask_text)
    monkeypatch.setattr(mgr_mod, "info_dialog", lambda *_a, **_k: None)


def _pump(app: ArchiveApp) -> None:
    app.update_idletasks()
    app.update()


def _drain(app: ArchiveApp) -> None:
    """等后台线程结果经消息队列回到主线程(忙碌标志复位)."""
    deadline = time.monotonic() + 5
    while getattr(app, "_busy", False) and time.monotonic() < deadline:
        _pump(app)
        time.sleep(0.005)
    app._poll_messages()  # 手动分发消息队列
    _pump(app)


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口; 无显示环境时抛出 TclError 由调用方转 skip."""
    return ArchiveApp(backend, title="按钮测试")


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
        on_change=lambda: None,
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


# ---------------------------------------------------------------- SQLite 后端


def test_sql_backend_placeholder_actions_show_clear_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """真实后端上未实现的备份/导出动作给出"阶段未实现"提示而非崩溃."""
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
        assert app._last_feedback[0] == FeedbackKind.ERROR
        assert "D" in app._last_feedback[1] or "E" in app._last_feedback[1]

        app._on_export()
        _drain(app)
        assert app._last_feedback[0] == FeedbackKind.ERROR
        assert not app._busy
    finally:
        app.destroy()
