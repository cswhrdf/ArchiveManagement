"""界面布局回归测试(边框可见性 + 分区边距一致性).

CustomTkinter 把 ``border_width`` 画在控件自身的 canvas 上, 而 grid/pack 的子
控件也从控件最外一圈开始排布, 因此**贴边的子控件(尤其是列表最后一行文字)会用
填充色盖住这条 1px 线**——表现出来就是右边框/下边框"消失"。本模块遍历真实窗口
的控件树, 断言没有任何有边框的容器被贴边子控件盖住下边或右边。同时校验"游戏
发现"与"游戏库"的内容内缩一致(两个分区的卡片边距不能有明显差异)。无图形环境
自动跳过。

两条测量约定(都是为了避开跨平台的假象, 别改回去):

1. **测量前先等几何稳定**(``_settle_layout``): 窗口刚创建或刚切页面时, 父容器
   尺寸已经变了、子控件还停在旧位置, 直接量会得到"子控件比父容器还宽"的假越界;
2. **用屏幕绝对坐标比较**(``winfo_rootx/rooty``): "子控件 x/y 对父容器宽高"隐含
   假设两者在同一坐标系里, 不同缩放/延迟下这个假设不成立(macOS 上尤其明显)。
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import replace
from math import ceil
from typing import Any

import pytest

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.domain import HomeFilter, HomeLayout
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.home_page import (
    _COLUMNS,
    _POSTER_HEIGHT,
    _POSTER_NAME_LINES,
    _POSTER_TEXT_WIDTH,
    _POSTER_WIDTH,
)
from archive_management.ui.main_window import (
    _HEADER_NAME_LINES,
    _HERO_NAME_LINES,
    WINDOW_MIN_SIZE,
    ArchiveApp,
)
from archive_management.ui.models import (
    DiscoveryPage,
    GameDetail,
    GameSummary,
    HomeBoard,
    HomeSection,
    poster_columns,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("控件边距与边框"),
    pytest.mark.layer("e2e"),
]

_WINDOW_SIZE = "1360x820"


def _pump(app: ctk.CTk) -> None:
    """把待处理事件跑完, 保证布局与绘制都已生效."""
    for _ in range(6):
        app.update_idletasks()
        app.update()


def _settle_layout(root: Any, *, timeout: float = 3.0) -> None:
    """等到控件树的几何不再变化再量尺寸.

    macOS 的窗口管理与 CustomTkinter 的延迟重绘会让"刚创建窗口/刚切换页面"时读到
    上一轮的几何值(父容器尺寸已经变了、子控件还停在旧位置), 这时算出来的"贴边/越界"
    是假的。做法是反复跑事件循环, 直到连续两次拓扑快照完全一致(并至少等一小段
    时间, 避开刚映射时的瞬时一致)。
    """
    deadline = time.monotonic() + timeout
    minimum = time.monotonic() + 0.15
    previous: tuple[int, ...] | None = None
    while time.monotonic() < deadline:
        root.update_idletasks()
        root.update()
        current = tuple(
            value
            for widget in _walk(root)
            if widget.winfo_ismapped()
            for value in (
                widget.winfo_rootx(),
                widget.winfo_rooty(),
                widget.winfo_width(),
                widget.winfo_height(),
            )
        )
        if current == previous and time.monotonic() >= minimum:
            return
        previous = current
        time.sleep(0.02)


def _new_app() -> ArchiveApp:
    """用演示后端构造主窗口(界面元素最全)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = ArchiveApp(
        DemoArchiveService(delay=0),
        title="边框测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
    )
    app.geometry(_WINDOW_SIZE)
    return app


def _wait_mapped(widget: Any, timeout: float = 2.0) -> bool:
    """等到控件真正显示(顶层窗口由 CustomTkinter 延迟显示)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        widget.update_idletasks()
        widget.update()
        if widget.winfo_ismapped():
            return True
        time.sleep(0.01)
    return bool(widget.winfo_ismapped())


def _walk(widget: Any) -> Iterator[Any]:
    """深度优先遍历控件树."""
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


def _bordered_frames(root: Any) -> Iterator[ctk.CTkFrame]:
    """遍历有边框且已显示的框架(滚动容器内部铺满 canvas, 不在检查范围内)."""
    for widget in _walk(root):
        if not isinstance(widget, ctk.CTkFrame):
            continue
        if isinstance(widget, ctk.CTkScrollableFrame):
            continue
        if not widget.winfo_ismapped():
            continue
        try:
            border_width = int(widget.cget("border_width"))
        except (ValueError, TypeError):  # pragma: no cover - 非数值边框
            continue
        if border_width > 0:
            yield widget


def _covering_children(frame: ctk.CTkFrame) -> list[str]:
    """返回盖住框架下边/右边的子控件描述(空列表表示边框可见).

    比较用**屏幕绝对坐标**(``winfo_rootx/rooty``), 不用"子控件 x/y 对父容器宽高":
    后者假设父子上报值在同一坐标系里, 而不同缩放/延迟下两者可能不同源(macOS 上
    尤其明显), 会量出"子控件比父容器还宽"这种假象。绝对坐标把两边放进同一坐标系。
    """
    right_edge = frame.winfo_rootx() + frame.winfo_width()
    bottom_edge = frame.winfo_rooty() + frame.winfo_height()
    covering: list[str] = []
    for child in frame.winfo_children():
        if not child.winfo_ismapped():
            continue
        child_bottom = child.winfo_rooty() + child.winfo_height()
        child_right = child.winfo_rootx() + child.winfo_width()
        edges: list[str] = []
        # "超出 0px" = 子控件边缘正好落在描边上(原来的缺陷形态); 大于 0 则是越界.
        if child_bottom >= bottom_edge:
            edges.append(f"下边超出 {child_bottom - bottom_edge}px")
        if child_right >= right_edge:
            edges.append(f"右边超出 {child_right - right_edge}px")
        if edges:
            covering.append(
                f"{'/'.join(edges)}: {child} "
                f"{child.winfo_width()}x{child.winfo_height()} "
                f"req={child.winfo_reqwidth()}x{child.winfo_reqheight()}"
            )
    return covering


def _offenders(root: Any) -> list[str]:
    """收集所有"边框被贴边子控件盖住"的框架描述(断言前先等布局稳定).

    窗口被环境挤到比容器自己所需宽度还窄时(例如 macOS CI 的虚拟屏比设计宽度窄),
    容器里的控件必然越界——那是"放不下", 不是布局缺陷; 这类容器由
    ``test_home_widgets_fit_the_supported_minimum_window`` 从"最小宽度"上把关,
    这里不再重复报。
    """
    _settle_layout(root)
    found: list[str] = []
    for frame in _bordered_frames(root):
        if frame.winfo_width() < frame.winfo_reqwidth():
            continue  # 环境过窄: 容器自己都放不下, 越界不说明布局有问题
        covering = _covering_children(frame)
        if covering:
            found.append(
                f"{frame} {frame.winfo_width()}x{frame.winfo_height()} "
                f"req={frame.winfo_reqwidth()}: " + "; ".join(covering)
            )
    return found


def _supported_content_width(app: ArchiveApp, current_width: int) -> int:
    """按"支持的最小窗口"折算内容可用宽度(最小窗口宽 - 侧栏与页边距).

    不能直接用当前内容宽度: macOS/小屏上的窗口管理器会把窗口压到比设计尺寸更窄,
    那时"当前宽度"也跟着变小, 拿它当预算就永远看不出"控件堆得太宽"。侧栏与页边距
    是固定的, 所以用 最小窗口宽 - (窗口宽 - 内容宽) 得到设计上保证能放下的宽度。
    """
    chrome = max(0, int(app.winfo_width()) - current_width)
    return WINDOW_MIN_SIZE[0] - chrome


def test_home_widgets_fit_the_supported_minimum_window() -> None:
    """主页各行/工具条的最小宽度必须放得进"支持的最小窗口".

    这条用例与当前窗口大小无关: 只要某个容器的内容最小宽度超过"最小窗口下的可用
    宽度", 它在小屏上就一定会越界(并盖住描边)。macOS CI 的虚拟屏比 1360 窄, 正好
    暴露了筛选工具条堆得太宽——在那里报错太晚, 这里提前拦住。
    """
    try:
        app = _new_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        _settle_layout(app)
        budget = _supported_content_width(app, page._list_box.winfo_width())
        too_wide = [
            f"{frame} req={frame.winfo_reqwidth()}"
            for frame in _bordered_frames(page.frame)
            if frame.winfo_reqwidth() > budget
        ]
        detail = "; ".join(too_wide)
        hint = f"下列容器的最小宽度超过支持的最小窗口下的可用宽度 {budget}px: {detail}"
        assert not too_wide, hint
    finally:
        app.destroy()


def test_filter_controls_are_right_aligned_in_the_toolbar() -> None:
    """回归: 筛选控件(平台/类型/搜索)要在右侧紧挨着排, 不能被拉成居中.

    页签保持左对齐, 多余宽度由"页签与筛选控件之间的空白列"吸收。把多余宽度给筛选
    控件所在的列(早先的写法)会让平台下拉在很宽的单元格里居中, 看着像没对齐。
    """
    try:
        app = _new_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        _settle_layout(app)
        bar = page._origin_box.master
        origin = page._origin_box
        category = page._category_box
        search = page._search_entry.master
        clear = page._clear_btn

        def gap(left: Any, right: Any) -> int:
            """两个控件之间的横向空隙(屏幕绝对坐标)."""
            return int(right.winfo_rootx()) - (
                int(left.winfo_rootx()) + int(left.winfo_width())
            )

        # 平台/类型/搜索紧挨着: 空隙就是各自的 padx(8 / 8 / 12), 不该被空白撑开.
        assert abs(gap(origin, category) - 8) <= 2
        assert abs(gap(category, search) - 8) <= 2
        # 最右侧控件贴住工具条右边(只有 12px 内边距), 证明整行是右对齐收尾. 窗口被
        # 环境挤到比工具条最小宽度还窄时(CI 虚拟屏), 内容整体右溢, 此时右边缘不再有
        # 意义——"最小值放不放得下"由 test_home_widgets_fit_the_supported_minimum_window
        # 在任意窗口尺寸下把关.
        if bar.winfo_width() >= bar.winfo_reqwidth():
            bar_right = int(bar.winfo_rootx()) + int(bar.winfo_width())
            clear_right = int(clear.winfo_rootx()) + int(clear.winfo_width())
            assert abs(bar_right - clear_right - 12) <= 3
        # 页签仍在左侧, 与筛选控件之间留出被吸收的空白.
        tabs_right = max(
            int(tab.winfo_rootx()) + int(tab.winfo_width())
            for tab in page._tabs.values()
        )
        assert tabs_right < int(origin.winfo_rootx())
    finally:
        app.destroy()


def test_library_and_discovery_borders_are_visible() -> None:
    """主页(列表/海报)与游戏发现区的卡片四边框都必须可见."""
    try:
        app = _new_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        page = app._home_page
        _pump(app)
        assert _offenders(app) == []

        page._on_layout_change("海报")
        _pump(app)
        assert _offenders(page._list_box) == []

        page._show_section(HomeSection.DISCOVERY)
        _pump(app)
        assert _offenders(page.frame) == []

        page._discovery._show_page(DiscoveryPage.MONITORED)
        _pump(app)
        assert _offenders(page.frame) == []
    finally:
        app.destroy()


def test_workspace_window_borders_are_visible() -> None:
    """设置与定时任务窗口里的卡片四边框都必须可见."""
    try:
        app = _new_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        for opener in (app._on_open_settings, app._on_open_schedules):
            opener()
            _pump(app)
            window = app._active_window
            assert window is not None
            try:
                # 窗口是延迟显示的顶层窗口: 没映射时控件树全是隐藏状态, 会假通过.
                assert _wait_mapped(window._window)
                assert list(_bordered_frames(window._window))
                assert _offenders(window._window) == []
            finally:
                window.close()
                _pump(app)
    finally:
        app.destroy()


def _insets(page: Any, widget: Any) -> tuple[int, int]:
    """控件相对页面容器的左/右内缩(实时读取基座, 避免窗口还在移动时报错值)."""
    base = page.frame.winfo_rootx()
    right = (
        base + page.frame.winfo_width() - (widget.winfo_rootx() + widget.winfo_width())
    )
    return (widget.winfo_rootx() - base, right)


def _game_name(page: Any, game_id: str) -> str:
    """取主页当前列表里某款游戏的名称(``_board`` 可能为 None, 这里统一窄化)."""
    board = page._board
    assert board is not None
    return str(next(item.name for item in board.games if item.game_id == game_id))


def test_detail_page_and_manage_window_borders_are_visible() -> None:
    """详情页与管理窗口的卡片四边框也必须可见."""
    from archive_management.ui.manage_window import ManageGameWindow

    try:
        app = _new_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        game_id = next(iter(page._rows))
        name = _game_name(page, game_id)
        app._open_game_detail(game_id)
        _pump(app)
        assert _offenders(app._content) == []

        window = ManageGameWindow(
            app,
            backend=app.backend,
            palette=app.p,
            game_id=game_id,
            name=name,
            enabled=True,
            backup_location=app.backend.task_status(game_id).target_label,
            on_change=lambda: None,
        )
        try:
            assert _wait_mapped(window._window)
            # 管理窗口的卡片行本来就不带描边(靠底色区分), 这里只做守卫:
            # 以后如果给它们加上了边框, 同样不能贴边盖住.
            assert _offenders(window._window) == []
        finally:
            window.close()
            _pump(app)
    finally:
        app.destroy()


def test_discovery_uses_same_page_margin_as_library() -> None:
    """回归: "游戏发现"的内容内缩不能比"游戏库"更宽(卡片不能往里挤)."""
    try:
        app = _new_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        _settle_layout(app)
        library_insets = _insets(page, page._list_box)

        page._show_section(HomeSection.DISCOVERY)
        _settle_layout(app)
        assert _insets(page, page._discovery._cand_box) == library_insets

        # 监控目录页未显示时其滚动区没有布局, 必须先切过去再量.
        page._discovery._show_page(DiscoveryPage.MONITORED)
        _settle_layout(app)
        assert _insets(page, page._discovery._dirs_box) == library_insets
    finally:
        app.destroy()


# ------------------------------------------------------------ 长名称不挤坏布局

# 真实世界里出现过的长名称(60+ 字符, 中英混排; 标点用半角, 见仓库的行文约定).
_LONG_NAME = (
    "Kaiju Princess 2: Poochi Q ASMR - A Magic Ticket That Grants Any Desire - "
    "超长的游戏名称示例"
)
# 再长一倍: 详情页的标题在两行(约 900px)里肯定放不下, 用来验证"截断 + 省略号".
_HUGE_NAME = _LONG_NAME * 2


class _LongNameService(DemoArchiveService):
    """演示后端, 但把每款游戏都改成 ``name_text`` 指定的长名称.

    名称撑破布局的几种形态都在同一个窗口里验证: 列表行的请求宽度被撑到容器之外
    (与表头错位、滚动区横向溢出)、海报卡片里的名称被卡片裁掉、详情页标题把右侧
    按钮挤出可视范围。

    主页数据走 ``load_home``(进入页面)与 ``apply_home_filter``(切换筛选/展示),
    侧栏与详情页分别走 ``list_games`` 与 ``get_detail`` —— 全部改名才能保证整窗
    一致。
    """

    name_text: str = _LONG_NAME

    def __init__(self, *, delay: float = 0, name_text: str = _LONG_NAME) -> None:
        """``delay`` 与演示后端一致, ``name_text`` 是要替换成的长名称."""
        super().__init__(delay=delay)
        self.name_text = name_text

    def list_games(self) -> list[GameSummary]:
        return [replace(game, name=self.name_text) for game in super().list_games()]

    def get_detail(self, game_id: str) -> GameDetail:
        return replace(super().get_detail(game_id), name=self.name_text)

    def load_home(self) -> HomeBoard:
        return self._rename(super().load_home())

    def apply_home_filter(self, active: HomeFilter) -> HomeBoard:
        return self._rename(super().apply_home_filter(active))

    def _rename(self, board: HomeBoard) -> HomeBoard:
        """把主页里的每款游戏都换成长名称(``games`` 是元组)."""
        return replace(
            board,
            games=tuple(replace(game, name=self.name_text) for game in board.games),
        )


def _long_name_app(name: str = _LONG_NAME) -> ArchiveApp:
    """构造使用长名称的主窗口(尺寸与其它布局用例一致)."""
    app = ArchiveApp(
        _LongNameService(delay=0, name_text=name),
        title="长名称测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
    )
    app.geometry(_WINDOW_SIZE)
    return app


def _visible_text(widget: Any) -> str:
    """取控件上**实际显示**的文本.

    CTkLabel 把文字画在自己的 canvas 上, 内部的 tkinter Label 没有文本, 因此
    ``cget("text")`` 取到的是空串 —— 这里读它保存文本的属性。
    """
    return str(getattr(widget, "_text", ""))


def _fixed_cells(block: Any) -> list[Any]:
    """取"固定列块"里的单元格(表头与数据行结构相同, 只有它带 len(_COLUMNS) 个格子)."""
    for child in block.winfo_children():
        cells = child.winfo_children()
        if len(cells) == len(_COLUMNS):
            return sorted(cells, key=lambda cell: int(cell.grid_info()["column"]))
    raise AssertionError("没有找到固定列块")


def _place(cell: Any) -> tuple[int, int]:
    """单元格在屏幕上的水平位置与宽度: 跨越不同父控件比较列对齐时用它."""
    return (cell.winfo_rootx(), cell.winfo_width())


def test_long_game_name_does_not_widen_the_list_rows() -> None:
    """名称是唯一可变长的一列: 长名称不能推走固定列, 也不能把行撑宽 or 推挤对齐.

    预算用 ``_supported_content_width`` 折算到"支持的最小窗口", 因此这条断言与
    当前窗口大小无关(小屏上窗口被窗口管理器压小时同样成立)。
    """
    try:
        app = _long_name_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        _settle_layout(app)

        budget = _supported_content_width(app, page._list_box.winfo_width())
        rows = list(page._rows.values())
        assert rows, "演示数据应当有游戏"
        # 固定列块本身必须放得进"最小窗口"的内容区, 否则名称在最小窗口下没有位置.
        block = next(iter(page._row_parts.values())).columns.winfo_width()
        too_wide = f"固定列块 {block}px 放不进最小窗口的内容区({budget}px)"
        assert block <= budget, too_wide

        parts = next(iter(page._row_parts.values()))
        shown = _visible_text(parts.label)
        assert shown.endswith("…"), f"名称应当截断显示: {shown!r}"
        assert len(shown) < len(parts.full_name), "截断后的名称必须比原名短"

        # 各行的固定列必须落在同一个 (屏幕) x 上: 名称长的行不能把后面的列右推.
        places = [[_place(cell) for cell in _fixed_cells(row)] for row in rows]
        misaligned = f"各行列位置不一致: {places}"
        assert all(place == places[0] for place in places), misaligned

        # 表头与数据行的固定列也在同一条竖线上(表头在卡片里, 宽度原本不同).
        header = [_place(cell) for cell in _fixed_cells(page._head)]
        assert header == places[0], f"表头与数据行没有对齐: {header} != {places[0]}"
        # 名称列头与名称文本也落在同一个 x 上.
        assert page._head_name.winfo_rootx() == parts.name_block.winfo_rootx()

        # 固定列块**贴靠右侧**: 最后一列的右边界离行的右边界只差一个内边距.
        row = rows[0]
        row_right = row.winfo_rootx() + row.winfo_width()
        last = _fixed_cells(row)[-1]
        gap = row_right - (last.winfo_rootx() + last.winfo_width())
        assert 0 <= gap <= 20, f"固定列块没有贴右: 右侧还空着 {gap}px"
    finally:
        app.destroy()


def test_long_game_name_wraps_inside_the_poster_card() -> None:
    """海报卡片里的名称最多折两行并截断, 且卡片尺寸不变、内容不越出卡片."""
    try:
        app = _long_name_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        page._on_layout_change(HomeLayout.POSTER.label)
        _settle_layout(app)
        assert page._rows, "演示数据应当有游戏"

        for card in page._rows.values():
            assert (card.winfo_reqwidth(), card.winfo_reqheight()) == (
                _POSTER_WIDTH,
                _POSTER_HEIGHT,
            ), "卡片尺寸由常量固定, 长名称不应该把它撑大"
            label = next(
                child
                for child in card.winfo_children()
                if _visible_text(child).startswith(_LONG_NAME[:5])
            )
            text = _visible_text(label)
            assert text.count("\n") + 1 <= _POSTER_NAME_LINES, f"名称超过两行: {text!r}"
            assert "…" in text, f"两行放不下时应当截断: {text!r}"
            assert label.winfo_reqwidth() <= _POSTER_TEXT_WIDTH
            # 名称必须落在卡片内: 越界就会盖住卡片下边框(或直接看不到).
            bottom = label.winfo_y() + label.winfo_height()
            overflow = f"名称溢出卡片: {bottom} > {_POSTER_HEIGHT}"
            assert bottom <= _POSTER_HEIGHT, overflow
    finally:
        app.destroy()


def test_long_game_name_is_capped_in_the_detail_header() -> None:
    """详情页的名称最多两行(超出补省略号), 不能无限折行把下面的内容推下去."""
    try:
        app = _long_name_app(_HUGE_NAME)
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        page._open(next(iter(page._rows)))
        _settle_layout(app)

        title = app._title_label
        text = _visible_text(title)
        assert text.startswith(_LONG_NAME[:5]), "至少要能看出是哪款游戏"
        assert text.endswith("…"), f"放不下就该补省略号: {text!r}"
        assert text != _HUGE_NAME, "超长名称不应原样显示"
        lines = text.count("\n") + 1
        too_many = f"标题折了 {lines} 行(上限 {_HEADER_NAME_LINES})"
        assert lines <= _HEADER_NAME_LINES, too_many
        # 名称按控件当前宽度裁剪: 请求宽度不会超过它自己分到的那一格.
        width = title.winfo_reqwidth()
        too_wide = f"标题请求 {width}px, 超过可用 {title.winfo_width()}px"
        assert width <= title.winfo_width(), too_wide
        # 右侧按钮不能被名称挤出表头.
        export = app._export_btn
        export_edge = export.winfo_rootx() + export.winfo_width()
        header_edge = title.master.winfo_rootx() + title.master.winfo_width()
        assert export_edge <= header_edge + 1, "名称把右侧按钮挤出了表头"

        hero_name = app._hero_name_label
        hero_text = _visible_text(hero_name)
        hero_lines = hero_text.count("\n") + 1
        hero_limit = f"概要卡名称折了 {hero_lines} 行(上限 {_HERO_NAME_LINES})"
        assert hero_lines <= _HERO_NAME_LINES, hero_limit
        assert hero_text.endswith("…"), f"概要卡名称应当截断: {hero_text!r}"
        assert hero_name.winfo_reqwidth() <= hero_name.winfo_width()
    finally:
        app.destroy()


def test_list_and_detail_names_grow_with_the_window() -> None:
    """名称按可用宽度动态裁剪: 窗口变宽就多显示几个字(列表与详情页都算)."""
    try:
        app = _long_name_app(_HUGE_NAME)
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        _settle_layout(app)
        game_id = next(iter(page._row_parts))
        label = page._row_parts[game_id].label
        narrow = len(_visible_text(label))

        # 先在主页上把窗口拉宽: 名称块分到的宽度变大, 显示的字就应当变多.
        app.geometry("1900x820")
        _settle_layout(app)
        wide = len(_visible_text(label))
        list_hint = f"列表名称没有随窗口变宽: {narrow} -> {wide}"
        assert wide > narrow, list_hint

        # 详情页同理: 在详情页上收窄窗口, 标题显示的字应当变少.
        page._open(game_id)
        _settle_layout(app)
        title_wide = len(_visible_text(app._title_label))
        app.geometry("1200x820")
        _settle_layout(app)
        title_narrow = len(_visible_text(app._title_label))
        detail_hint = f"详情标题没有随窗口变宽: {title_narrow} -> {title_wide}"
        assert title_wide > title_narrow, detail_hint
        # 详情标题仍然受行数上限约束.
        lines = _visible_text(app._title_label).count("\n") + 1
        assert lines <= _HEADER_NAME_LINES, f"标题折了 {lines} 行"
    finally:
        app.destroy()


class _PosterStartService(DemoArchiveService):
    """启动时读到的偏好就是海报模式(相当于 home_state 里存着海报)."""

    def load_home(self) -> HomeBoard:
        return self._poster(super().load_home())

    def apply_home_filter(self, active: HomeFilter) -> HomeBoard:
        return self._poster(super().apply_home_filter(active))

    @staticmethod
    def _poster(board: HomeBoard) -> HomeBoard:
        return replace(board, filter=replace(board.filter, layout=HomeLayout.POSTER))


def _poster_start_app() -> ArchiveApp:
    """构造"启动首屏即海报模式"的主窗口."""
    app = ArchiveApp(
        _PosterStartService(delay=0),
        title="海报首屏测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
    )
    app.geometry(_WINDOW_SIZE)
    return app


def test_poster_layout_is_reflowed_after_the_first_render() -> None:
    """启动首屏就是海报模式时, 卡片要按**真实宽度**排布.

    首屏渲染发生在控件尺寸测量出来之前(此时 ``winfo_width()`` 只有 1), 列数会被
    算得很小 —— 4 款游戏于是排成两行; 首帧完成后必须按真实宽度重排一次。
    """
    try:
        app = _poster_start_app()
    except TclError as exc:  # pragma: no cover - 无显示环境
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        assert _wait_mapped(app)
        page = app._home_page
        _settle_layout(app)
        assert page._rows, "演示数据应当有游戏"

        columns = poster_columns(page._list_box.winfo_width())
        stale = f"列数没有按真实宽度重算: {page._poster_columns} != {columns}"
        assert page._poster_columns == columns, stale
        placed = sorted({int(card.grid_info()["row"]) for card in page._rows.values()})
        expected = list(range(ceil(len(page._rows) / columns)))
        assert placed == expected, f"卡片行列不对: {placed} != {expected}"
    finally:
        app.destroy()
