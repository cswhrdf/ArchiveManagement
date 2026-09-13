"""界面布局回归测试(边框可见性 + 分区边距一致性).

CustomTkinter 把 ``border_width`` 画在控件自身的 canvas 上, 而 grid/pack 的子
控件也从控件最外一圈开始排布, 因此**贴边的子控件(尤其是列表最后一行文字)会用
填充色盖住这条 1px 线**——表现出来就是右边框/下边框"消失"。本模块遍历真实窗口
的控件树, 断言没有任何有边框的容器被贴边子控件盖住下边或右边。同时校验"游戏
发现"与"游戏库"的内容内缩一致(两个分区的卡片边距不能有明显差异)。无图形环境
自动跳过。
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
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import DiscoveryPage, HomeSection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
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


def _settle(app: ctk.CTk, seconds: float = 0.3) -> None:
    """带真实等待地跑事件循环: 窗口位置/尺寸要等窗口管理器定下来才可信."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.update_idletasks()
        app.update()
        time.sleep(0.01)


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
    """返回盖住框架下边/右边的子控件描述(空列表表示边框可见)."""
    width, height = frame.winfo_width(), frame.winfo_height()
    covering: list[str] = []
    for child in frame.winfo_children():
        if not child.winfo_ismapped():
            continue
        bottom = child.winfo_y() + child.winfo_height()
        right = child.winfo_x() + child.winfo_width()
        edges = []
        if bottom >= height:
            edges.append("下")
        if right >= width:
            edges.append("右")
        if edges:
            covering.append(
                f"{'/'.join(edges)}边被 {child} "
                f"{child.winfo_width()}x{child.winfo_height()}"
                f"@{child.winfo_x()},{child.winfo_y()} 盖住"
            )
    return covering


def _offenders(root: Any) -> list[str]:
    """收集所有"边框被贴边子控件盖住"的框架描述."""
    found: list[str] = []
    for frame in _bordered_frames(root):
        covering = _covering_children(frame)
        if covering:
            found.append(
                f"{frame} {frame.winfo_width()}x{frame.winfo_height()}: "
                + "; ".join(covering)
            )
    return found


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
        _settle(app)
        library_insets = _insets(page, page._list_box)

        page._show_section(HomeSection.DISCOVERY)
        _settle(app)
        assert _insets(page, page._discovery._cand_box) == library_insets

        # 监控目录页未显示时其滚动区没有布局, 必须先切过去再量.
        page._discovery._show_page(DiscoveryPage.MONITORED)
        _settle(app)
        assert _insets(page, page._discovery._dirs_box) == library_insets
    finally:
        app.destroy()
