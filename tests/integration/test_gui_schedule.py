"""
定时任务窗口与任务卡: 间隔/保留数编辑、增删启停、归档拒绝、对话框过滤。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
"""

from __future__ import annotations

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
import archive_management.ui.schedule_window as sched_mod
from archive_management.i18n import tr
from button_support import (
    _label_texts,
    _new_app,
    _open_manager,
    _patch_dialogs,
    _pump,
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
    # 没有排期时给出口径, 而不是一个像加载失败的短横线(评审时定的).
    assert app._task_next.cget("text") == tr("task.next_none")
    assert app._task_next.cget("text") != "—"


def test_task_panel_is_renamed_and_has_no_schedule_editor() -> None:
    """任务卡显示的是任务状态, 定时配置已移到游戏设置/定时任务窗口."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    assert app._task_hint.cget("text") == tr("task.hint")
    assert not hasattr(app, "_task_edit_btn")


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
