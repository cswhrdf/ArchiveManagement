"""列表/表格里的文字不得被"无省略号地"硬裁 (I-1 第三个子项).

判据: 一个文本控件的**任意一行**像素宽 > 它自己的宽度时, Tk 会把文字裁掉而且**不补
省略号** —— 用户看到的就是半句话。所以每个列表/表格里都要求: 每行都放得下, 放进不
下的那些**必须带省略号**(`ui/textfit` 的 fit_text/fit_path 就是干这个的)。

为什么夹具要用**长内容**: 演示数据的名字都很短, 拿它量什么都证明不了 —— 必须把各处
可能变长的字段(游戏名/译名、安装路径、标签、备份标题与描述)都换成长的, 量出来的
"0 被硬裁"才有意义。同时要求"长到必须裁"的地方**真的出现了省略号**(夹具没白长,
截断也是打了标记的)。

两档宽度: 1366(设计尺寸) 与 1200(主窗口最小尺寸, 布局的硬下限)。
"""

from __future__ import annotations

from collections import deque
from dataclasses import replace
from typing import Any

import pytest

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.domain.home import HomeFilter, HomeLayout
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import HomeBoard, HomeSection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("列表与表格的文字不被硬裁"),
    pytest.mark.layer("e2e"),
]

# 两档宽度(与 test_gui_sizes.py 同一套基准).
WIDTHS = ((1366, 820), (1200, 720))
# 长内容: 真实世界里出现过的长名称(中英混排) + 很深的安装路径.
_LONG_NAME = (
    "Kaiju Princess 2: Poochi Q ASMR - A Magic Ticket That Grants Any Desire - "
    "超长的游戏名称示例"
) * 3
_LONG_PATH = "D:\\SteamLibrary\\steamapps\\common\\" + "VeryLongFolderName\\" * 6
_LONG_TITLE = "恢复之前自动创建的安全点(超长的备份标题示例)" * 3
_LONG_NOTE = "描述会显示在备份卡片上, 这里故意写得很长很长很长很长很长很长很长" * 3
# 状态列的内容 = 平台 + 备份状态 + 风险 (+ 停用/归档) + 自定义标签: 四个长标签足以
# 把它顶到"换行 + 省略号"那一条路上(见 models.chips_lines)。
_LONG_TAGS = (
    "超长的标签名示例一",
    "超长的标签名示例二",
    "超长的标签名示例三",
    "超长的标签名示例四",
)


class _LongTextService(DemoArchiveService):
    """演示后端, 但把每处可能变长的文案都换成长的(名称/路径/标签/标题/描述)."""

    def __init__(self) -> None:
        super().__init__(delay=0)

    def list_games(self) -> list[Any]:
        return [replace(game, name=_LONG_NAME) for game in super().list_games()]

    def get_detail(self, game_id: str) -> Any:
        detail = super().get_detail(game_id)
        return replace(detail, name=_LONG_NAME, main_location=_LONG_PATH)

    def list_locations(self, game_id: str) -> list[Any]:
        return [
            replace(location, path=_LONG_PATH)
            for location in super().list_locations(game_id)
        ]

    def list_backups(self, game_id: str) -> list[Any]:
        return [
            replace(backup, title=_LONG_TITLE, sub=_LONG_NOTE)
            for backup in super().list_backups(game_id)
        ]

    def list_candidates(self, *, status: str | None = None) -> list[Any]:
        return [
            replace(candidate, name=_LONG_NAME, install_dir=_LONG_PATH)
            for candidate in super().list_candidates(status=status)
        ]

    def list_schedules(self) -> list[Any]:
        return [
            replace(item, game_name=_LONG_NAME) for item in super().list_schedules()
        ]

    def load_home(self) -> HomeBoard:
        return self._rename(super().load_home())

    def apply_home_filter(self, active: HomeFilter) -> HomeBoard:
        return self._rename(super().apply_home_filter(active))

    def _rename(self, board: HomeBoard) -> HomeBoard:
        return replace(
            board,
            games=tuple(
                replace(game, name=_LONG_NAME, tags=_LONG_TAGS) for game in board.games
            ),
        )


def _pump(app: Any) -> None:
    """把待处理事件跑完(裁剪与换行都是事件驱动的)."""
    for _ in range(8):
        app.update_idletasks()
        app.update()


def _new_app(backend: ArchiveService) -> ArchiveApp:
    return ArchiveApp(
        backend,
        title="长文本裁切测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
    )


def _labels(root: Any) -> list[Any]:
    """子树里所有 CTkLabel."""
    found: list[Any] = []
    queue = deque([root])
    while queue:
        widget = queue.popleft()
        if isinstance(widget, ctk.CTkLabel):
            found.append(widget)
        queue.extend(widget.winfo_children())
    return found


def _widest_line(widget: Any, text: str) -> int:
    """文本里最宽的一行(多行文本按行分别量)."""
    font = widget.cget("font")
    return int(max(font.measure(line) for line in text.splitlines()))


def _problems(name: str, container: Any, *, expect_truncation: bool) -> list[str]:
    """一块列表/表格区域的问题清单(空列表 = 这块没问题).

    两块判据: ① 每个控件的每行都放得下(否则 Tk 会裁掉且不补省略号);
    ② ``expect_truncation`` 为真时, 这块区域里**必须**出现省略号 —— 那是夹具的前提,
    长文本没长到必须裁的话, 这条用例在这块区域上什么都证明不了。

    一次收集完所有区域再一次断言: 只报第一处的话, 改一处就得再跑一轮。
    """
    container.update_idletasks()
    texts: list[str] = []
    problems: list[str] = []
    for widget in _labels(container):
        text = str(widget.cget("text"))
        if not text.strip():
            continue
        width = int(widget.winfo_width())
        if width <= 1:
            continue  # 还没布局, 量不出结论
        widest = _widest_line(widget, text)
        if widest > width:
            problems.append(
                f"{name}: 文字比控件还宽 —— Tk 会裁掉且不补省略号 "
                f"(宽 {width} < 文字 {widest}) | {text[:80]!r}"
            )
        texts.append(text)
    if expect_truncation and not any("…" in text for text in texts):
        problems.append(
            f"{name}: 夹具的长文本应该长到必须裁(带省略号) —— 否则这条用例证明不了"
            f"什么. 实测这些文本: {[text[:40] for text in texts][:6]}"
        )
    return problems


def _assert_areas(app: ArchiveApp, areas: list[tuple[str, Any, bool]]) -> None:
    """逐块区域收集问题, 有问题就把它们全列出来."""
    problems: list[str] = []
    for name, container, expect_truncation in areas:
        problems.extend(_problems(name, container, expect_truncation=expect_truncation))
    hint = "列表/表格里的文字被硬裁了\n" + "\n".join(problems)
    assert not problems, hint


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> ArchiveApp:
    application: ArchiveApp = gui_app(_new_app, _LongTextService())
    _pump(application)
    return application


def _areas(app: ArchiveApp) -> list[tuple[str, Any, bool]]:
    """要检查的列表/表格区域: (名字, 容器, 夹具的长文本该不该长到必须裁)."""
    page = app._home_page
    found: list[tuple[str, Any, bool]] = [
        ("游戏库表头", page._head, False),
        ("游戏库列表", page._list_box, True),
    ]

    page._show_section(HomeSection.DISCOVERY)
    _pump(app)
    found.append(("发现候选列表", page._discovery._cand_box, True))
    found.append(("监控目录列表", page._discovery._dirs_box, False))

    page._show_section(HomeSection.ACTIVATION)
    _pump(app)
    found.append(("启停队列", page._activation._rows_box, False))

    page._show_section(HomeSection.LIBRARY)
    games = list(app.backend.list_games())
    app._open_game_detail(games[0].game_id)
    _pump(app)
    found.append(("详情备份列表", app._list_scroll, True))

    schedules = app._open_schedule_window()
    _pump(app)
    found.append(("定时任务列表", schedules._list_scroll, True))

    manage = ManageGameWindow(
        app,
        backend=app.backend,
        palette=app.p,
        game_id=games[0].game_id,
        name=games[0].name,
        enabled=True,
        backup_location="D:\\Backups",
        on_change=lambda: None,
    )
    manage.refresh()
    _pump(app)
    found.append(("管理窗口位置列表", manage._list_scroll, True))
    return found


def test_list_and_table_text_is_never_clipped_without_an_ellipsis(
    app: ArchiveApp,
) -> None:
    """两档宽度下, 每个列表/表格里的文字要么放得下, 要么带省略号."""
    try:
        for width, height in WIDTHS:
            app.geometry(f"{width}x{height}")
            _pump(app)
            areas = [
                (f"{width}-{name}", container, expect)
                for name, container, expect in _areas(app)
            ]
            _assert_areas(app, areas)
    finally:
        for child in app.winfo_children():
            if isinstance(child, ctk.CTkToplevel):
                child.destroy()
        _pump(app)


def test_poster_cards_keep_their_names_inside_the_card(app: ArchiveApp) -> None:
    """海报卡片里的名称同样不许被硬裁(卡片宽度固定, 名称按卡片宽度裁)."""
    page = app._home_page
    page._on_layout_change(HomeLayout.POSTER.label)
    _pump(app)
    _assert_areas(app, [("海报卡片", page._list_box, True)])
