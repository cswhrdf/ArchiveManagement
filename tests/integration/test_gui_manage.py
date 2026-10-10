"""
管理窗口: 改名/封面/位置动作的守卫与反馈、删除原始目录、归档全禁用。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
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

import archive_management.ui.manage_window as mgr_mod
from archive_management.domain import HomeView
from archive_management.i18n import tr
from archive_management.ui.main_window import ArchiveApp
from button_support import (
    _location_ids,
    _new_app,
    _open_manager,
    _patch_dialogs,
    _pump,
    _real_service_with_one_backup,
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
    assert {
        state(button) for pair in window._artwork_buttons.values() for button in pair
    } <= {"disabled"}
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
    """重命名弹窗要带上"当前名称": 它与"新增游戏"长得一样(评审时发现的)."""
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


def _picked_image(tmp_path: Path) -> Path:
    """写一张真 PNG(扮演"用户挑的那张图")."""
    from PIL import Image

    path = tmp_path / "picked.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (240, 360), "green").save(path, format="PNG")
    return path


def _build_manage_window(app: ArchiveApp, on_change: Any) -> Any:
    """构造一个带指定回调的管理窗口(要断言"通知外部刷新"的用例用)."""
    from archive_management.ui.manage_window import ManageGameWindow

    return ManageGameWindow(
        app,
        backend=app.backend,
        palette=app.p,
        game_id="outer-wilds",
        name="星际拓荒",
        enabled=True,
        backup_location="—",
        on_change=on_change,
    )


def test_manage_window_sets_and_resets_a_custom_cover(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """管理窗口里能挑一张图当封面, 也能恢复默认(用户 2026-10-03 的要求).

    演示后端不写用户的磁盘, 但**语义与真实后端一致**(用户指定的图优先、可恢复默认),
    所以这条走的是与生产同一条界面路径: 点按钮 → 选文件 → 写后端 → 重画预览与状态 →
    通知外部刷新(主页海报要跟着变)。真实后端的落盘/归一化/拒收由
    ``tests/unit/test_sql_backend.py`` 与 ``tests/unit/test_artwork.py`` 守。
    """
    from archive_management.ui import manage_window as manage_mod
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    picked = _picked_image(tmp_path)
    monkeypatch.setattr(manage_mod, "pick_file", lambda **_kwargs: str(picked))
    refreshed: list[str] = []
    window = _build_manage_window(app, lambda: refreshed.append("changed"))
    try:
        assert window._artwork_state["cover"].cget("text") == tr(
            "manage.artwork_builtin"
        )
        assert str(window._artwork_buttons["cover"][1].cget("state")) == "disabled"

        window._artwork_buttons["cover"][0].invoke()
        _pump(app)

        assert app.backend.user_artwork_path("outer-wilds", "cover") == str(picked)
        assert app.backend.artwork_path("outer-wilds", "cover") == str(picked)
        assert window._artwork_state["cover"].cget("text") == tr(
            "manage.artwork_custom"
        )
        assert str(window._artwork_buttons["cover"][1].cget("state")) == "normal"
        assert window._artwork_preview["cover"].cget("image") is not None, (
            "预览要真的换成这张图"
        )
        assert refreshed == ["changed"], "改完图要通知外部刷新"

        window._artwork_buttons["cover"][1].invoke()
        _pump(app)

        assert app.backend.user_artwork_path("outer-wilds", "cover") == ""
        assert window._artwork_state["cover"].cget("text") == tr(
            "manage.artwork_builtin"
        )
        assert str(window._artwork_buttons["cover"][1].cget("state")) == "disabled"
        # 图标没被动过(两类互不影响).
        assert window._artwork_state["icon"].cget("text") == tr(
            "manage.artwork_builtin"
        )
    finally:
        window.close()


def test_a_picked_file_that_cannot_be_used_shows_the_reason(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """挑到不能用的图: 按原因代码弹一句"为什么不能用", 状态与后端都不变.

    静默失败最坏 —— 用户会以为界面没反应。真实后端在这里抛
    ``ArtworkImageError(code)``, 界面负责把代码翻成当前语言的一句话。
    """
    from archive_management.exceptions import ArtworkImageError
    from archive_management.ui import manage_window as manage_mod
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    shown: list[tuple[str, str]] = []
    monkeypatch.setattr(
        mgr_mod,
        "info_dialog",
        lambda _parent, _palette, *, title, message: shown.append((title, message)),
    )
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    junk = tmp_path / "not-an-image.txt"
    junk.write_text("这不是图片", encoding="utf-8")
    monkeypatch.setattr(manage_mod, "pick_file", lambda **_kwargs: str(junk))

    def refuse(_game_id: str, _kind: Any, source: str) -> str:
        raise ArtworkImageError("not_an_image", source)

    monkeypatch.setattr(app.backend, "set_game_artwork", refuse)
    window = _build_manage_window(app, lambda: None)
    try:
        window._artwork_buttons["cover"][0].invoke()
        _pump(app)

        assert shown == [
            (tr("manage.artwork_problem"), tr("artwork.error.not_an_image"))
        ]
        assert app.backend.user_artwork_path("outer-wilds", "cover") == ""
        assert window._artwork_state["cover"].cget("text") == tr(
            "manage.artwork_builtin"
        )
    finally:
        window.close()


def test_manage_window_location_buttons_stay_inside_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """位置操作按钮必须整颗落在窗口里.

    六个按钮挤一行时总宽已经等于容器可用宽度, 再加间距必然溢出 —— 最右边的
    "删除"会被窗口边缘裁掉半颗(评审时发现的)。
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
        # 告别包的落点按评审时的要求单独一行给出(带底色), 不再塞进正文句子里。
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
