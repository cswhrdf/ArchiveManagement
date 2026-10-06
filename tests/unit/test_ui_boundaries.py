"""界面层的纯边界: 字体探测在"没有 Tk 根"时的退路, 与演示后端的失败语义.

这些用例刻意**不建 Tk 根**: 界面上真正难测的从来不是"有窗口时画得对不对", 而是
"没有窗口/取不到时报什么" —— 而这类退路在带 GUI 的用例里反而被"环境恰好可用"
掩盖掉了(整套跑下来它们从来没被执行过)。判据是各处的返回值/异常, 不是"跑过就算"。

后来补的几个窗口/面板也是同一个理由: 面板重绘时的"这个按钮已经没了"、设置窗口
按内容定高时的"窗口刚被关掉"、保存排期时后端的拒绝 —— 它们都只需要一个替身对象。
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.ui import (
    activation_page,
    schedule_window,
    settings_window,
    typography,
)
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.palette import DARK

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(界面纯边界)"),
    pytest.mark.story("没有 Tk 根与未知对象时的退路"),
    pytest.mark.layer("unit"),
]


# --------------------------------------------------------- 字体探测的退路


def test_installed_families_is_empty_when_tk_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """取不到字体族(没有 Tk 根 / Tcl 异常)时给空表, 而不是把异常抛给调用方."""
    tkfont = pytest.importorskip("tkinter.font")

    def refuse() -> list[str]:
        raise RuntimeError("Too early to use font")

    monkeypatch.setattr(tkfont, "families", refuse)

    assert typography.installed_families() == ()


def test_a_family_that_cannot_be_probed_does_not_resolve_to_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """探测字体名(``Font(...)``)失败时按"解析不到自己"处理: 候选会被跳过, 不抛错."""
    tkfont = pytest.importorskip("tkinter.font")

    def refuse(**kwargs: object) -> object:
        raise RuntimeError("Too early to use font")

    monkeypatch.setattr(tkfont, "Font", refuse)

    assert typography.resolves_to_itself("Noto Sans CJK SC") is False


def test_the_chosen_family_is_not_cached_without_a_tk_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """还没有 Tk 根时``current_family``给 ``None`` 且**不缓存** —— 那不是环境的结论.

    缓存了就坏了: 之后真的建起窗口也永远认不出字体(界面会一直用回退字号)。
    """
    monkeypatch.setattr(typography, "_FAMILY", None)
    monkeypatch.setattr(typography, "_FAMILY_RESOLVED", False)
    monkeypatch.setattr(typography, "pick_family", lambda *args: None)
    monkeypatch.setattr(typography, "installed_families", lambda: ())

    assert typography.current_family() is None

    assert typography._FAMILY_RESOLVED is False, "没有 Tk 根不是环境的结论, 不该记下来"


# --------------------------------------------------------- 演示后端的失败语义


def test_demo_artwork_for_an_unknown_game_is_rejected(tmp_path: Path) -> None:
    """给不存在的游戏设置自定义图片要报"未知游戏"(与真实后端同一套失败语义)."""
    service = DemoArchiveService(delay=0)

    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.set_game_artwork("no-such-game", "cover", str(tmp_path / "cover.png"))


def test_a_backend_that_cannot_answer_counts_as_no_locations() -> None:
    """后端报错(未知游戏等)时按"没有存档位置"处理: 定时备份不可用, 但界面不该炸."""

    class _Broken:
        """一被问存档位置就报错的后端替身."""

        def list_locations(self, game_id: str) -> list[object]:
            """模拟未知游戏."""
            raise ArchiveManagementError(f"未知游戏: {game_id}")

    backend = cast("ArchiveService", _Broken())

    assert schedule_window._has_locations(backend, "nope") is False


# --------------------------------------------------------- 面板/窗口的销毁与退化


class _WidgetStub:
    """最小控件替身: 回答"还在不在/什么状态", 并记下每次改写与销毁."""

    def __init__(self, *, state: str = "normal", exists: bool = True) -> None:
        self.state = state
        self.exists = exists
        self.configured: list[dict[str, object]] = []
        self.destroyed = 0

    def winfo_exists(self) -> bool:
        """控件还在不在(已销毁为 False)."""
        return self.exists

    def winfo_reqheight(self) -> int:
        """内容高度(用例只关心"量得到")."""
        return 10

    def winfo_screenheight(self) -> int:
        """屏幕高度."""
        return 1000

    def cget(self, option: str) -> object:
        """读一个选项(面板重绘只读 ``state``)."""
        assert option == "state"
        return self.state

    def configure(self, **kwargs: object) -> None:
        """记下每次改写."""
        self.configured.append(dict(kwargs))

    def destroy(self) -> None:
        """记一次销毁."""
        self.destroyed += 1


def test_restyling_the_activation_buttons_skips_the_ones_that_are_gone() -> None:
    """面板重绘时, 已经销毁的按钮要跳过 —— 否则整批重绘会从中间断掉."""
    gone = _WidgetStub(exists=False)
    disabled = _WidgetStub(state="disabled")
    usable = _WidgetStub()
    panel: Any = activation_page.ActivationPanel.__new__(
        activation_page.ActivationPanel
    )
    panel._palette = DARK
    panel._styles = {gone: "accent", disabled: "accent", usable: "accent"}
    panel._is_inside = lambda _button, _parent: True

    panel._restyle_buttons(cast(Any, object()))

    assert gone.configured == [], "已经不在了的按钮不该被碰"
    assert disabled.configured, "禁用态要按禁用外观重绘"
    assert usable.configured, "可用态要回到强调色"


def test_clearing_the_queue_rows_destroys_every_row() -> None:
    """清空队列时逐行销毁并清掉登记(否则下次重画会叠在旧行上)."""
    rows = [_WidgetStub(), _WidgetStub()]
    panel: Any = activation_page.ActivationPanel.__new__(
        activation_page.ActivationPanel
    )
    panel._row_widgets = list(rows)

    panel._clear_rows()

    assert [row.destroyed for row in rows] == [1, 1]
    assert panel._row_widgets == []


class _DeadWindowStub:
    """已经销毁的窗口替身: 几何操作报 TclError."""

    def update_idletasks(self) -> None:
        """什么都不做."""

    def geometry(self, _value: str) -> None:
        """已销毁的窗口就是这个反应."""
        raise tk.TclError("bad window path name")


def test_refitting_the_settings_height_gives_up_when_the_window_is_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """按内容定高的那次延后量宽可能晚于关窗: 那时静默作罢, 不把 TclError 抛出去."""
    monkeypatch.setattr(settings_window, "window_scaling", lambda _window: 1.0)
    window: Any = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
    window._window = _DeadWindowStub()
    window._body = _WidgetStub()
    window._footer = _WidgetStub()
    window._parent = _WidgetStub()

    window._refit_height()


def test_the_remember_switch_flips_back_when_applying_fails() -> None:
    """ "记住窗口大小与位置"应用失败时: 说明原因并把开关拨回去(失败不能只留在日志里)."""

    def _refuse(_wanted: bool) -> str:
        """模拟"主窗口存不下去"."""
        return "配置只读"

    window: Any = settings_window.SettingsWindow.__new__(settings_window.SettingsWindow)
    window._remember_window = False
    window._remember_switch = _SwitchStub(wanted=True)
    window._remember_hint = _WidgetStub()
    window._on_apply_remember_window = _refuse

    window._on_remember_toggled()

    assert window._remember_switch.deselected == 1
    assert window._remember_window is False
    assert "配置只读" in str(window._remember_hint.configured[-1]["text"])

    window._remember_switch = _SwitchStub(wanted=True)
    window._on_apply_remember_window = lambda _wanted: None
    window._on_remember_toggled()

    assert window._remember_window is True, "成功之后才把新状态记下来"
    assert window._remember_hint.configured[-1]["text"] == tr(
        "settings.remember_window_hint"
    )

    # 开关已经是记下来的那个状态(重复收到同一次切换) → 不去打扰主窗口。
    window._remember_hint.configured.clear()
    window._on_apply_remember_window = _refuse
    window._on_remember_toggled()

    assert window._remember_hint.configured == []


class _SwitchStub:
    """开关替身: 记下被拨动几次与当前状态."""

    def __init__(self, *, wanted: bool) -> None:
        self.wanted = wanted
        self.selected = 0
        self.deselected = 0

    def get(self) -> bool:
        """当前状态."""
        return self.wanted

    def select(self) -> None:
        """拨到开."""
        self.selected += 1
        self.wanted = True

    def deselect(self) -> None:
        """拨到关."""
        self.deselected += 1
        self.wanted = False


def test_a_rejected_schedule_shows_the_reason_and_keeps_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端拒绝这次排期时把原因摆给用户并返回 False(不要静默失败)."""
    shown: list[str] = []
    monkeypatch.setattr(
        schedule_window,
        "_error_dialog",
        lambda _parent, _palette, message: shown.append(message),
    )
    monkeypatch.setattr(
        schedule_window, "schedule_dialog", lambda *_args, **_kwargs: ("30m", "5")
    )

    class _Refusing:
        """只回答前置条件与排期的后端替身."""

        def list_locations(self, _game_id: str) -> list[object]:
            """有一个存档位置 —— 前置条件满足."""
            return [object()]

        def task_status(self, _game_id: str) -> object:
            """当前排期: 还没配过."""
            return SimpleNamespace(schedule_text="", keep_auto=3)

        def set_schedule(self, *_args: object, **_kwargs: object) -> None:
            """拒绝这次修改(例如周期写法不认识)."""
            raise ArchiveManagementError("周期不认识")

    ok = schedule_window.edit_schedule(
        cast(Any, object()),
        DARK,
        cast("ArchiveService", _Refusing()),
        game_id="1",
        game_name="演示",
    )

    assert ok is False
    assert shown == ["周期不认识"]


def test_rescanning_the_demo_data_keeps_the_imported_candidates() -> None:
    """演示后端重铺候选时: 已经在的不重复铺, 已导入的丢了就补回来.

    已导入那一款的 ``game_id`` 是运行期状态(导入到哪一款), 演示夹具里没有 —— 所以它
    只能按原样补回去, 不能被当成"这次没扫到"而清掉。

    这里直接连铺两次: 真实路径里 ``scan_candidates`` 会先剪掉没扫到的候选, 于是"已经在
    的那些不重复铺"那一支根本走不到 —— 而那正是重铺该有的幂等性。
    """
    service = DemoArchiveService()
    service._seed_candidates()
    imported = next(
        candidate_id
        for candidate_id, item in service._candidates.items()
        if item.status == "imported"
    )
    del service._candidates[imported]

    service._seed_candidates()

    assert imported in service._candidates, "已导入的那一款要按原样补回来"
    assert service.scan_candidates().linked >= 1, "它仍然算在「已关联」里"
