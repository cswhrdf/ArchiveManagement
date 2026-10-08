"""
备份/恢复/分支动作: 快照写入、危险分支根确认、取消与损坏报告、游戏运行拦截。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gui_support import gui_app

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

import archive_management.ui.main_window as main_mod
from archive_management.i18n import tr
from archive_management.services.hotkeys import (
    ACTION_CREATE_BRANCH,
    DEFAULT_BRANCH_ACCELERATOR,
    GlobalHotkeyService,
)
from archive_management.ui.models import (
    FeedbackKind,
)
from button_support import (
    _drain,
    _feedback_kind,
    _new_app,
    _open_manager,
    _patch_dialogs,
    _pump,
    _RecordingBackend,
    _service_backups,
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


def test_default_view_is_branch_tree_and_hides_old_auto_backups() -> None:
    """默认展示分支树; 分支树只保留最新一份自动备份, 时间线展示全部.

    分支视图自改成**图画布**后(不再建卡片), 所以"这一屏显示了几个节点"要量
    ``app._tree_view.node_ids``; 时间线仍然用卡片。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import ViewKind

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._open_game_detail("outer-wilds")
    # 时间范围默认是"最近 30 天", 而演示数据的日期是**固定的**(2026-09-04..06): 要断言
    # "时间线里每一份都有卡片"就得先把范围设成"全部", 否则这条断言随"今天"漂移 ——
    # 2026-10-05 一到, 09-04 那份(22:31)掉出 30 天窗口, 这里当场 `4 != 5`(实测);
    # 报告里它一直是绿的, 只是因为跑的那天(10-04 17:52)还在窗口边上。
    app._filter_period.set(tr("filter.all_time"))
    app._render_list()
    _pump(app)
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
    assert app._title_label.cget("text") == "山海旅人"

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
    assert app._title_label.cget("text") == "未选择游戏"
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
    assert app._title_label.cget("text") == "未选择游戏"
    assert app._items == []


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
        # 目录名收在悬停提示里(评审时定的).
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
