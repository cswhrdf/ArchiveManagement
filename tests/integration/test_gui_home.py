"""
主页库: 视图切换、海报/列表外观、分页与滚动、筛选与标签、封面缓存。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
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
from archive_management.domain import HomeView
from archive_management.i18n import tr
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.widgets import scaled_px
from button_support import (
    _button_texts,
    _close_toplevels,
    _home_item,
    _label_texts,
    _location_ids,
    _new_app,
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


def _current_page(app: ArchiveApp) -> Any:
    """返回主窗口当前页面.

    经函数返回可避免 mypy 对 ``app._page`` 做字面量收窄(收窄后再比较另一页
    会被判为 non-overlapping)。
    """
    return app._page


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
    """断言行内只剩完整标签: 放不下时**整块让位**给末尾那个孤零零的省略号(评审时发现的).

    口径 2026-10-02 调整(用户): 状态一行、标签一行, 超出部分用省略号 —— 省略号是独立
    的一项(前面带空格), 而不是把最后一个标签裁短成 "测…"。
    """
    body = line.rsplit(" …", 1)[0]
    parts = [part for part in body.split(" · ") if part]
    halves = [part for part in parts if part not in chips]
    assert not halves, f"状态列把标签拦腰截断: {halves} (整行 {line!r})"


def _card_labels(card: Any) -> list[Any]:
    """海报卡片里的文本标签(**递归**).

    ``CTkFrame`` 不接受 ``cget("text")``(会抛 ValueError), 所以必须先按类型筛,
    不能对卡片的每个子控件直接读 text。名称现在装在**固定高度的名称块**里(见
    ``home_page.poster_name_block_height``), 所以得往下走一层 —— 只查直接子控件会
    漏掉名称, 那些 ``next(...)`` 会变成 StopIteration(2026-10-04 三个平台都这么红)。
    """
    found: list[Any] = []
    for child in card.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(child)
        else:
            found.extend(_card_labels(child))
    return found


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

    回归(评审时发现): 一条数据都没有却摆着七个列头, 底下再挂一行
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
    """内容装得下时右侧不立滚动条, 真的溢出才出现(评审时发现的).

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
    """分页按钮的禁用态必须与可用态明显不同: 底色/文字一起压暗(评审时定的)."""
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
    控件(可点、可选中)在两种页面上行为不一致(评审时发现的)。规则只有一条, 落在
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
    """海报卡片: 角标不压封面、名称与元信息分开、占位字不再超大(评审时定的)."""
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
    """状态列放不下时整块让位给省略号, 不许出现"测…"这种半句话(评审时发现的).

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


def test_artwork_landing_on_the_detail_page_leaves_the_home_page_alone() -> None:
    """详情页可见时的重载不重绘主页: 那次重绘是给"看得见的主页"用的.

    与上一条互补 —— 主页不可见时重绘它只是白做一次整页重建(先销毁旧行再重建),
    用户一个像素也看不到; 但详情页自己的刷新照旧(下面的断言只看主页那半边)。
    """
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.models import AppPage

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._open_game_detail("outer-wilds")
    _pump(app)
    assert _current_page(app) is AppPage.DETAIL, "前提: 详情页真的打开了"

    repaints: list[bool] = []
    app._home_page.refresh_artwork = lambda: repaints.append(True)
    app._reload_data()
    _pump(app)

    assert repaints == [], "详情页可见时轮询触发的重载不该重绘主页"
    assert _current_page(app) is AppPage.DETAIL, "重载不该把用户从详情页挤回主页"


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
