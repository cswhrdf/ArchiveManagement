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
from typing import Any

import pytest

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.main_window import WINDOW_MIN_SIZE, ArchiveApp
from archive_management.ui.models import DiscoveryPage, HomeSection

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
