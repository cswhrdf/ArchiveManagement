"""游戏设置窗口里"主存档位置"的呈现(2026-10-02 用户反馈).

出处: 主位置原来是在**路径前面加一个字** —— ``f"{tr('loc.primary')}  {path}"``, 也就是
"主  C:\\...\\save"。用户看到的是一个孤零零的"主", 容易当成路径的一部分(读成别的字),
所以改成由行首那枚类型标签表达("主目录 / 主文件"), 路径本身原样显示。

判据分两层: 纯函数那一层直接断标签文案(快、且能穷举四种组合); 界面那一层断"路径标签的
文本就是路径本身" —— 那正是回归会漏掉的地方(改了 chip 却忘了去掉前缀)。
"""

from __future__ import annotations

from typing import Any

import pytest

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.i18n import tr
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.manage_window import (
    ManageGameWindow,
    _chip_text,
    _kind_text,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("游戏设置窗口"),
    pytest.mark.story("主存档位置的呈现"),
    pytest.mark.layer("integration"),
]


def test_the_chip_text_names_the_primary_location() -> None:
    """标签文案: 主位置写"主目录/主文件", 其余位置只写类型."""
    assert _chip_text("directory", primary=True) == tr("loc.primary_dir")
    assert _chip_text("file", primary=True) == tr("loc.primary_file")
    assert _chip_text("directory", primary=False) == _kind_text("directory")
    assert _chip_text("file", primary=False) == _kind_text("file")


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """主窗口: 不注册系统级快捷键(与其它 GUI 用例同一条约定)."""
    return ArchiveApp(
        backend,
        title="主位置标签",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("标签测试禁用")),
    )


def _pump(app: ctk.CTk) -> None:
    """把待处理事件跑完(窗口与行都是事件驱动的)."""
    for _ in range(3):
        app.update_idletasks()
        app.update()


def test_the_path_is_shown_verbatim_and_the_chip_carries_the_marker() -> None:
    """路径前面不再加字: 主位置的路径照原样显示, 标记落在行首的标签上."""
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    manage: Any = None
    try:
        _pump(app)
        games = list(app.backend.list_games())
        assert games, "演示后端应该至少有一款游戏"
        game = games[0]
        locations = list(app.backend.list_locations(game.game_id))
        assert locations, "演示数据里应当有存档位置"
        # 保证有一个主位置(演示数据可能没有): 这一条是判据的前提, 不成立就该显式失败。
        app.backend.set_primary_location(game.game_id, locations[-1].location_id)

        manage = ManageGameWindow(
            app,
            backend=app.backend,
            palette=app.p,
            game_id=game.game_id,
            name=game.name,
            enabled=True,
            backup_location="D:\\Backups",
            on_change=lambda: None,
        )
        manage.refresh()
        _pump(app)

        item = next(
            entry
            for entry in app.backend.list_locations(game.game_id)
            if entry.is_primary
        )
        row, title, _note = manage._rows[item.location_id]
        assert title.cget("text") == item.path, (
            f"路径要原样显示(不在前面加字): {title.cget('text')!r}"
        )
        chips = [
            widget
            for widget in row.grid_slaves()
            if isinstance(widget, ctk.CTkLabel)
            and int(widget.grid_info()["column"]) == 0
        ]
        assert len(chips) == 1, f"行首应当只有一枚类型/主位置标签: {chips}"
        assert chips[0].cget("text") == tr("loc.primary_dir"), (
            f"主位置的标记要在标签上: {chips[0].cget('text')!r}"
        )
    finally:
        if manage is not None:
            manage.close()
        _pump(app)
        app._on_close()
