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
from archive_management.ui.models import (
    DiscoveryPage,
    HomeBoard,
    HomeSection,
    ViewKind,
)

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
# 备份标题与描述**必须带一段长拉丁串**: 缺中文字体的机器会把汉字量成近乎零宽, 纯汉字的
# "超长标题"在那台机器上根本放得下, 于是"这块区域里的文字必须长到被裁"这个**前提**恒不
# 成立 —— 判据退化成空断言(CI 实测 Linux 上 1366 档的"详情备份列表"与"右侧选中备份面板"
# 就是这么失守的)。拉丁字形任何字体都量得出宽度, 前提因此在每个平台都成立。
_LONG_LATIN = "-VeryLongBackupTitleSample0123456789" * 4
_LONG_TITLE = "恢复之前自动创建的安全点(超长的备份标题示例)" * 3 + _LONG_LATIN
_LONG_NOTE = (
    "这是一条故意写得很长很长的描述, 用来撑出省略号, 再长一点, 再长一点吧" * 3
    + _LONG_LATIN
)
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


def _flush_delayed(app: ArchiveApp) -> None:
    """把"延后跑"的重排任务直接跑一次(不靠时间).

    详情页的名称重裁与主页的"表头对齐 + 名称重裁"都是 ``after(60ms)`` 的任务: 用例里
    真正流逝的时间很短, CI 上它们可能**还没跑**, 于是量到的是按旧宽度裁出来的文本 ——
    "夹具的长文本必须带省略号"那一条就会假红(本地机器快一点就恰好跑到了, 所以只在 CI
    上红了两个平台)。

    直接调一次: 这与 ``test_gui_buttons`` 里冲刷 ``_refit_detail_names`` 的做法一致。
    """
    app._refit_job = None
    app._refit_detail_names()
    page = getattr(app, "_home_page", None)
    if page is not None:
        page.cancel_list_sync()
        page._sync_list_layout()


def _widest_line(widget: Any, text: str) -> int:
    """文本里最宽的一行(多行文本按行分别量)."""
    font = widget.cget("font")
    return int(max(font.measure(line) for line in text.splitlines()))


def _needed_width(widget: Any, text: str) -> int:
    """这个标签**需要**多宽才不被裁.

    会自己换行的标签(``wraplength`` > 0)不能拿"整段文字的宽度"去比: Tk 已经按
    ``wraplength`` 断过行了, 该比的是断行之后最宽的那一行 —— 也就是控件自己的
    ``winfo_reqwidth()``。实测右侧栏那条说明正是这种: 整段文字 413px、控件 240px,
    但断行后最宽的一行只有 240px —— **一个字都没被裁**, 拿整段去比就是假红。
    """
    if int(widget.cget("wraplength") or 0) > 0:
        return int(widget.winfo_reqwidth())
    return _widest_line(widget, text)


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
        needed = _needed_width(widget, text)
        if needed > width:
            problems.append(
                f"{name}: 文字需要的宽度超过控件 —— Tk 会裁掉且不补省略号 "
                f"(控件 {width} < 需要 {needed}) | {text[:80]!r}"
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
    # 分支视图自 I-9 起是**图画布**(框里的文字由 tree_view 自己用 fit_text 裁), 这里要
    # 量的是"按卡片铺出来的列表" —— 切到时间线才是那一块。图画布自己的裁剪判据在
    # tests/integration/test_gui_branch_graph.py 里(画布 item 的实测宽度 ≤ 框宽)。
    app._switch_view(ViewKind.TIMELINE)
    _pump(app)
    found.append(("详情备份列表", app._list_scroll, True))
    # 右侧「选中备份」面板: 超长备份名**只有这一处**能读全 —— 从前它不在量测清单里,
    # 于是被 Tk 硬裁且不补省略号也没人发现(它既不是列表也不是表格)。
    # 选中"当前节点": 它在图上与列表里必然都在, 所以之后就算重排一次也不会被清掉
    # (按"列表第一项"选的话, 它可能被时段筛选排除, 量到的就是空状态 —— 实测踩过)。
    current = next(item for item in app._items if item.is_current)
    app._select_backup(current)
    found.append(("右侧选中备份面板", app._rail_scroll, True))

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
            # 重裁是延后的任务: 先把它真跑一次, 否则量的是按旧宽度裁的文本(见 _flush_delayed).
            _flush_delayed(app)
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


def test_hint_text_is_never_clipped_without_an_ellipsis(app: ArchiveApp) -> None:
    """成段说明同样不许被硬裁 —— 它们不在列表/表格里, 从前没人量.

    监控目录页那条说明因此长期被裁掉尾部: 容器 1312 时标签只分到 928(同一行还有四个
    按钮), 而按容器算出来的 ``wraplength`` 是 1122 → 文字按 1122 排成**一行**, 再在标签
    边界处被 Tk 硬裁(不换行、无省略号), 结尾的"也可以加进监控目录。"永远看不到。
    """
    page = app._home_page
    page._show_section(HomeSection.DISCOVERY)
    _pump(app)
    for width, height in WIDTHS:
        app.geometry(f"{width}x{height}")
        _pump(app)
        hints = (
            ("发现页说明", DiscoveryPage.CANDIDATES, page._discovery._hint_label),
            ("监控目录说明", DiscoveryPage.MONITORED, page._discovery._dirs_hint),
        )
        for name, holder, label in hints:
            # 两块说明在不同的子页上: 隐藏的那一页量不出宽度, 必须切过去再量。
            page._discovery._show_page(holder)
            _pump(app)
            _assert_areas(app, [(f"{width}-{name}", label, False)])

    page._show_section(HomeSection.ACTIVATION)
    _pump(app)
    _assert_areas(app, [("启停页空状态说明", page._activation._empty, False)])


def test_poster_cards_keep_their_names_inside_the_card(app: ArchiveApp) -> None:
    """海报卡片里的名称同样不许被硬裁(卡片宽度固定, 名称按卡片宽度裁)."""
    page = app._home_page
    page._on_layout_change(HomeLayout.POSTER.label)
    _pump(app)
    _assert_areas(app, [("海报卡片", page._list_box, True)])
