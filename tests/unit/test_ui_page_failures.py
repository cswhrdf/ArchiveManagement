"""页面与工作窗口的处理函数: 后端拒绝、用户取消、窗口已经没了的时候各自怎么收场.

这些处理方法在带 GUI 的用例里几乎都只走"顺利"那一半 —— 点击、等一等、看列表变了。
而出错那一半(后端抛出域异常、用户在确认框上按了取消、延后动作晚于关窗)才决定用户看到
什么: 是**一句能读的原因**, 还是"点了没反应"。

所以这里不建 Tk 根, 只用替身把界面对象拼出来, 然后直接叫那些处理方法。判据是各处的
返回值、"弹了什么"以及**有没有假装成功**(该 reload 的时候没 reload)。
"""

from __future__ import annotations

import tkinter as tk
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.ui import discovery_page, home_page
from archive_management.ui.palette import DARK

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(页面处理函数的失败退路)"),
    pytest.mark.story("后端拒绝与用户取消时的收场"),
    pytest.mark.layer("unit"),
]


class _WidgetStub:
    """最小控件替身: 记下每次改写, 并回答"还在不在/多宽"."""

    def __init__(self, *, exists: bool = True, width: int = 0) -> None:
        self.configured: list[dict[str, object]] = []
        self.exists = exists
        self.width = width

    def configure(self, **kwargs: object) -> None:
        """记下每次改写."""
        self.configured.append(dict(kwargs))

    def winfo_exists(self) -> bool:
        """控件还在不在(已销毁为 False)."""
        return self.exists

    def winfo_width(self) -> int:
        """实得宽度(用例直接摆出来)."""
        return self.width

    def cget(self, option: str) -> object:
        """读一个选项(按钮重绘只读 ``state``)."""
        assert option == "state"
        return "normal"

    def update_idletasks(self) -> None:
        """假控件没有布局要跑."""


class _RefusingBackend:
    """后端替身: 指定的那一步报域异常, 其余步骤按"什么都没有"作答."""

    def __init__(self, failing: str, message: str = "后端拒绝了这一步") -> None:
        self.failing = failing
        self.message = message
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """任何一步都记一笔; 名字对上时抛出域异常."""

        def call(*_args: object, **_kwargs: object) -> Any:
            self.calls.append(name)
            if name == self.failing:
                raise ArchiveManagementError(self.message)
            return SimpleNamespace(
                removed=0, label="摘要", detail="细节", name="演示", game_id="1"
            )

        return call


def _panel(
    monkeypatch: pytest.MonkeyPatch, *, failing: str
) -> tuple[Any, _RefusingBackend, list[str], list[int]]:
    """造一个"没有窗口"的监控/探测面板, 返回它、后端替身、弹过的原因与重载次数."""
    backend = _RefusingBackend(failing)
    panel: Any = discovery_page.DiscoveryPanel.__new__(discovery_page.DiscoveryPanel)
    panel._backend = backend
    panel._palette = DARK
    panel._summary_label = _WidgetStub()
    panel._detail_label = _WidgetStub()
    panel.frame = _WidgetStub()
    errors: list[str] = []
    reloads: list[int] = []
    panel._show_error = lambda exc: errors.append(str(exc))
    panel.reload = lambda: reloads.append(1)
    panel._dir_item = lambda: SimpleNamespace(
        directory_id="d1", path="saves/slot", note="", enabled=True
    )
    panel._candidate_item = lambda: SimpleNamespace(
        candidate_id="c1",
        name="演示",
        status="new",
        importable=True,
        save_supported=True,
        save_label="",
        save_paths=(),
        install_dir="Games/Demo",
    )
    monkeypatch.setattr(
        discovery_page, "confirm_dialog", lambda *_args, **_kwargs: True
    )
    return panel, backend, errors, reloads


def _answers(monkeypatch: pytest.MonkeyPatch, *values: str | None) -> None:
    """让 ``ask_text`` 依次回答给定的几个值(用户按取消 = 返回 ``None``)."""
    remaining = list(values)

    def answer(*_args: object, **_kwargs: object) -> str | None:
        """弹下一个预置的答案."""
        return remaining.pop(0)

    monkeypatch.setattr(discovery_page, "ask_text", answer)


def test_a_failed_scan_shows_the_reason_instead_of_pretending_it_ran(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测失败时把原因摆出来, 并且**不去重载列表** —— 列表还是上一轮的结果, 不该装成新的."""
    panel, _backend, errors, reloads = _panel(monkeypatch, failing="scan_candidates")

    panel._on_scan()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


def test_a_cancelled_clear_writes_no_audit_of_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清空探测结果要确认: 用户按取消时什么都不做(也不留下"已清空"那一笔审计)."""
    panel, backend, errors, reloads = _panel(monkeypatch, failing="clear_scan_results")
    monkeypatch.setattr(
        discovery_page, "confirm_dialog", lambda *_args, **_kwargs: False
    )

    panel._on_clear_scan()

    assert backend.calls == [], "取消之后不该去动后端"
    assert errors == []
    assert reloads == []


def test_a_failed_clear_shows_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """确认之后后端仍可能拒绝(比如库被别的进程锁住): 那时摆出原因, 不重载."""
    panel, _backend, errors, reloads = _panel(monkeypatch, failing="clear_scan_results")

    panel._on_clear_scan()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


def test_a_failed_directory_update_shows_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改监控目录的路径/备注被后端拒绝时摆出原因, 不重载."""
    panel, _backend, errors, reloads = _panel(
        monkeypatch, failing="update_monitored_directory"
    )
    _answers(monkeypatch, "saves/new", "")

    panel._on_edit_dir()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


def test_a_failed_directory_toggle_shows_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用/停用被后端拒绝时摆出原因 —— 开关状态没变, 界面不能装作变了."""
    panel, _backend, errors, reloads = _panel(
        monkeypatch, failing="set_monitored_enabled"
    )

    panel._on_toggle_dir()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


def test_a_failed_directory_removal_shows_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除监控目录被后端拒绝时摆出原因(那一行还在, 不重载)."""
    panel, _backend, errors, reloads = _panel(
        monkeypatch, failing="remove_monitored_directory"
    )

    panel._on_remove_dir()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


def test_a_failed_import_shows_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """导入探测结果被后端拒绝时摆出原因, 不去动列表(也没有"已导入"的提示)."""
    panel, _backend, errors, reloads = _panel(monkeypatch, failing="import_candidate")

    def chosen(*_args: object, **_kwargs: object) -> tuple[str, tuple[str, ...]]:
        """用户在导入对话框里确认了名称与存档路径."""
        return "演示", ("saves/slot",)

    monkeypatch.setattr(discovery_page, "import_game_dialog", chosen)

    panel._on_import()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


def test_a_failed_ignore_shows_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """标记忽略/恢复待处理被后端拒绝时摆出原因, 不重载."""
    panel, _backend, errors, reloads = _panel(
        monkeypatch, failing="set_candidate_ignored"
    )

    panel._on_ignore()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


def test_a_failed_relocate_shows_the_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    """修正安装路径被后端拒绝时摆出原因, 不重载."""
    panel, _backend, errors, reloads = _panel(monkeypatch, failing="relocate_candidate")
    _answers(monkeypatch, "Games/Other")

    panel._on_relocate()

    assert errors == ["后端拒绝了这一步"]
    assert reloads == []


# --------------------------------------------------------- 主页: 五处"后端拒绝"
#
# 主页每个动作(立即备份/加存档位置/改标签/归档/启停)都是"先动后端, 再刷列表".
# 后端拒绝时若继续往下走, 用户会看到"已备份"的提示而磁盘上什么都没有 —— 所以每条都
# 断言两件事: 原因被摆出来了, 而且**没有刷新/没有成功提示**。


class _HomeBackend:
    """主页后端替身: 指定的那一步报域异常, 其余步骤什么都不做."""

    def __init__(self, failing: str, message: str = "后端拒绝了这一步") -> None:
        self.failing = failing
        self.message = message
        self.calls: list[str] = []

    def __getattr__(self, name: str) -> Callable[..., Any]:
        """任何一步都记一笔; 名字对上时抛出域异常."""

        def call(*_args: object, **_kwargs: object) -> Any:
            self.calls.append(name)
            if name == self.failing:
                raise ArchiveManagementError(self.message)
            return None

        return call


def _home(
    monkeypatch: pytest.MonkeyPatch, *, failing: str
) -> tuple[Any, _HomeBackend, list[str], list[int]]:
    """造一个"没有窗口"的主页, 返回它、后端替身、弹过的原因与刷新次数."""
    backend = _HomeBackend(failing)
    page: Any = home_page.HomePage.__new__(home_page.HomePage)
    page._backend = backend
    page._palette = DARK
    page._summary_label = _WidgetStub()
    page.frame = _WidgetStub()
    page._remember_hint = _WidgetStub()
    errors: list[str] = []
    refreshes: list[int] = []
    page._show_error = lambda exc: errors.append(str(exc))
    page._refresh = lambda: refreshes.append(1)
    page._report_archived = lambda _item: page._summary_label.configure(text="已归档")
    page._item = lambda: SimpleNamespace(
        game_id="1",
        name="演示",
        archived=False,
        enabled=True,
        backup_enabled=True,
        tags=(),
        allow=lambda _ability: True,
    )
    return page, backend, errors, refreshes


def test_a_rejected_backup_reports_the_reason_and_does_not_claim_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """立即备份被后端拒绝: 摆出原因, 不刷列表 —— 否则用户以为已经备好了."""
    page, _backend, errors, refreshes = _home(monkeypatch, failing="run_backup_now")

    page._on_backup()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_a_rejected_location_edit_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加存档位置被后端拒绝(路径不可用之类): 摆出原因, 不刷列表."""
    page, _backend, errors, refreshes = _home(monkeypatch, failing="add_location")
    monkeypatch.setattr(home_page, "ask_text", lambda *_args, **_kwargs: "saves/slot")

    page._on_add_location()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_a_rejected_tag_edit_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改标签被后端拒绝: 摆出原因, 不刷列表(标签还是老样子, 界面不能装作存上了)."""
    page, _backend, errors, refreshes = _home(monkeypatch, failing="set_game_tags")
    monkeypatch.setattr(
        home_page, "edit_tags_dialog", lambda *_args, **_kwargs: ("速通",)
    )

    page._on_edit_tags()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_a_rejected_archive_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """归档/取消归档被后端拒绝: 摆出原因, 也不去改选中项."""
    page, _backend, errors, refreshes = _home(monkeypatch, failing="set_game_archived")
    page._selected = "1"

    page._on_archive()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []
    assert page._selected == "1"


def test_a_rejected_enable_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用/停用自动启停被后端拒绝: 摆出原因, 不刷列表."""
    page, _backend, errors, refreshes = _home(monkeypatch, failing="set_game_enabled")
    page._enabled_rival = lambda _item: None

    page._on_toggle_enabled()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


# --------------------------------------------------------- 主页: 控件已经没了的时候


class _RowStub:
    """卡片替身: 可以"随重绘被销毁"(``configure`` 报 TclError)."""

    def __init__(self, *, dead: bool = False) -> None:
        self.dead = dead
        self.configured: list[dict[str, object]] = []

    def configure(self, **kwargs: object) -> None:
        """记下每次改写(已销毁时正如真控件那样报错)."""
        if self.dead:
            raise tk.TclError("bad window path name")
        self.configured.append(dict(kwargs))


def test_restyling_drops_the_registrations_of_rebuilt_buttons() -> None:
    """渲染重建后留下的旧登记要摘掉: 别对已经不存在的控件改色(那会整批中断)."""
    gone = _WidgetStub(exists=False)
    usable = _WidgetStub()
    page: Any = home_page.HomePage.__new__(home_page.HomePage)
    page._palette = DARK
    page._buttons = {gone: "ghost", usable: "ghost"}

    page._restyle_buttons()

    assert list(page._buttons) == [usable]
    assert gone.configured == []


def test_the_row_viewport_falls_back_to_the_container_width() -> None:
    """拿不到内层画布(替身/精简控件)时退回滚动容器自己的宽度, 不能因此报错."""
    page: Any = home_page.HomePage.__new__(home_page.HomePage)
    page._list_box = _WidgetStub(width=640)

    assert page._row_viewport() == 640


def test_an_unreadable_artwork_path_leaves_the_placeholder() -> None:
    """后端读图片路径时抛域异常(图刚被删): 返回 None 走文字占位, 不打断整页渲染."""
    page: Any = home_page.HomePage.__new__(home_page.HomePage)
    page._artwork_images = {}
    page._backend = _HomeBackend(failing="artwork_path")

    item = SimpleNamespace(game_id="1", name="演示")

    assert page._artwork_image(item, "cover", (64, 64)) is None


def test_painting_a_row_that_is_gone_is_ignored() -> None:
    """悬停是高频交互: 卡片在这期间被重绘销毁时, 这一次局部重绘直接作废."""
    page: Any = home_page.HomePage.__new__(home_page.HomePage)
    page._row_colors = lambda _game_id: ("#101820", "#203040")

    page._rows = {}
    page._paint_row("gone")  # 没有这张卡片: 什么都不做
    assert page._rows == {}

    dead = _RowStub(dead=True)
    page._rows = {"1": dead}
    page._paint_row("1")

    assert dead.configured == [], "已销毁的卡片不该被改色"
