"""按需滚动条的真实控件回归(评审时量到的"常驻灰条").

单元测试用替身钉住了 :func:`archive_management.ui.widgets.sync_scrollbar` 的判定逻辑;
这里换成**真实**的 CustomTkinter 滚动容器, 验证"收起"在真实窗口里真的成立 ——
两条只有真控件才会暴露的路径:

- 首次判定发生在窗口还没画出来(或所在分区/子页还藏着)的时候, 此时"是否已映射"读到的
  是 0, 若拿它当初值就会认为"已经收起", 之后内容装得下时什么都不做, 那条滚动条永远
  立在界面上(评审时发现页那条常驻灰条);
- CustomTkinter 的滚动条每次收到画布回叫(``set()``)都会 ``_draw()``, 而 ``_draw()``
  结尾那句 ``update_idletasks()`` 会把 Tk 里排队的映射动作执行掉, 刚从布局里摘掉的
  滚动条当场又被映射回来(实测: 该位置灰色像素 3706, 修后 0)。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import GlobalHotkeyService, UnavailableBackend
from archive_management.ui import widgets
from archive_management.ui.backend import ArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import DiscoveryPage, HomeSection

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("按需滚动条"),
    pytest.mark.layer("integration"),
]

# 内容装得下的行数 / 撑爆视口的行数(视口约 200px, 每行约 28px)。
_FITTING_ROWS = 2
_OVERFLOWING_ROWS = 40


def _pump(root: Any, seconds: float = 0.3) -> None:
    """跑一会儿事件循环: 判定与几何都靠 idle 回调推进."""
    import time

    end = time.monotonic() + seconds
    while time.monotonic() < end:
        root.update_idletasks()
        root.update()
        time.sleep(0.02)


def _scrollable(root: Any, rows: int) -> ctk.CTkScrollableFrame:
    """建一个只装了 ``rows`` 行文字的滚动容器, 并接上按需滚动条."""
    box = ctk.CTkScrollableFrame(root, width=240, height=200, fg_color="#0c1524")
    box.pack(padx=10, pady=10)
    _fill(box, rows)
    widgets.auto_scrollbar(box)
    _pump(root)
    return box


def _fill(box: ctk.CTkScrollableFrame, rows: int) -> None:
    """填入若干行文字."""
    for index in range(rows):
        ctk.CTkLabel(box, text=f"第 {index} 行").grid(row=index, column=0, sticky="w")


def _is_shown(box: ctk.CTkScrollableFrame) -> bool:
    """滚动条此刻是否真的在画面上."""
    return bool(box._scrollbar.winfo_ismapped())


def test_a_scrollbar_appears_only_when_the_content_overflows() -> None:
    """装得下就收起, 溢出才出现 —— 真实控件上的两个方向都要成立."""
    root = gui_app(ctk.CTk)
    box = _scrollable(root, _FITTING_ROWS)
    assert not _is_shown(box), "两行文字装得下, 不该有滚动条"
    assert box._scrollbar.winfo_manager() == "", "应当真的不在布局里"

    _fill(box, _OVERFLOWING_ROWS)
    _pump(root)
    assert _is_shown(box), "内容溢出后必须出现滚动条"


def test_a_hidden_scrollbar_survives_the_canvas_reporting_its_position() -> None:
    """回归: 发现页监控目录那条滚动条收起后不能再被"画布回叫"带回来.

    CustomTkinter 的 ``set()`` → ``_draw()`` 结尾会强制刷新事件队列, 那次刷新正是把
    已经摘掉的滚动条放回画面的时刻 —— 评审时那条拖不动的常驻灰条就是这么
    来的(实测: 该位置灰色像素 3706, 修后 0)。这条路径只在真实窗口里出现(裸控件上触发
    回叫不会重现), 因此这条用例走真正的主窗口。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app, 0.5)
    page = app._home_page
    page._show_section(HomeSection.DISCOVERY)
    page._discovery._show_page(DiscoveryPage.MONITORED)  # 监控目录: 一行内容装得下
    _pump(app, 0.6)

    scrollbar = page._discovery._dirs_box._scrollbar
    assert not scrollbar.winfo_ismapped(), "内容装得下时那条灰条必须消失"
    assert scrollbar.winfo_manager() == "", "应当真的不在布局里"

    page._discovery._dirs_box._parent_canvas.yview_moveto(1.0)
    _pump(app, 0.4)

    assert not scrollbar.winfo_ismapped(), "回叫之后滚动条不能回来"
    assert scrollbar.winfo_manager() == "", "应当仍然不在布局里"


def test_the_discovery_panel_has_no_stray_scrollbar_with_an_empty_library(
    tmp_path: Path,
) -> None:
    """回归: 空库时发现页探测结果那条滚动条不该出现(评审时量到的).

    发现页是在"游戏库"分区里 `grid_remove` 建好的, 首次判定发生窗口还没画出来时 ——
    旧实现拿"未映射"当初值, 得出"已经收起"的错误结论后什么都不做, 那一条滚动条就一直
    挂在界面上。
    """
    backend, paths = _empty_backend(tmp_path)
    app = gui_app(_new_app, backend, paths=paths)
    _pump(app, 0.5)
    page = app._home_page
    page._show_section(HomeSection.DISCOVERY)
    page._discovery._show_page(DiscoveryPage.CANDIDATES)
    _pump(app, 0.6)

    scrollbar = page._discovery._cand_box._scrollbar
    assert not scrollbar.winfo_ismapped(), "空库没有候选, 不该有滚动条"
    assert scrollbar.winfo_manager() == "", "应当真的不在布局里"


def _empty_backend(tmp_path: Path) -> tuple[ArchiveService, ApplicationPaths]:
    """建一个真实的空库后端(库里没有游戏), 与评审时用的同一条路径."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    database = Database(paths.database_path)
    database.migrate()
    backend = SqlArchiveService(
        database, backup_root=paths.backup_root, cache_dir=paths.cache_dir
    )
    return backend, paths


def _new_app(
    backend: ArchiveService, *, paths: ApplicationPaths | None = None
) -> ArchiveApp:
    """构造主窗口(不注册系统级快捷键, 避免遗留键盘钩子)."""
    return ArchiveApp(
        backend,
        title="滚动条测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用")),
        paths=paths,
    )


def test_a_scrollbar_hidden_while_its_section_is_still_hidden_is_really_removed() -> (
    None
):
    """回归: 所在分区还藏着时做出的"收起"判定必须真的执行(发现页就是这么建的).

    主页默认停在"游戏库"分区, 发现页的两个滚动区是在 ``grid_remove`` 状态下建好的:
    那时"是否已映射"读到 0, 若拿它当初值就会认为"已经收起", 于是永远不做收起动作 ——
    分区显示出来后那条滚动条就一直挂着(评审时量到的)。
    """
    root = gui_app(ctk.CTk)
    section = ctk.CTkFrame(root)
    section.pack(fill="both", expand=True)
    section.grid_remove()  # 与主页一样: 分区先藏着, 容器在隐藏状态建成

    box = ctk.CTkScrollableFrame(section, width=240, height=200, fg_color="#0c1524")
    box.pack(padx=10, pady=10)
    _fill(box, _FITTING_ROWS)
    widgets.auto_scrollbar(box)
    _pump(root)

    section.grid()  # 显示分区
    _pump(root, 0.5)

    assert not _is_shown(box), "分区显示后滚动条仍必须收起来"
    assert box._scrollbar.winfo_manager() == "", "应当真的不在布局里"
