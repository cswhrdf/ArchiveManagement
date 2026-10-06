"""页面与工作窗口的处理函数: 后端拒绝、用户取消、窗口已经没了的时候各自怎么收场.

这些处理方法在带 GUI 的用例里几乎都只走"顺利"那一半 —— 点击、等一等、看列表变了。
而出错那一半(后端抛出域异常、用户在确认框上按了取消、延后动作晚于关窗)才决定用户看到
什么: 是**一句能读的原因**, 还是"点了没反应"。

所以这里不建 Tk 根, 只用替身把界面对象拼出来, 然后直接叫那些处理方法。判据是各处的
返回值、"弹了什么"以及**有没有假装成功**(该 reload 的时候没 reload)。
"""

from __future__ import annotations

import sqlite3
import tkinter as tk
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import customtkinter as ctk
import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.services.hotkeys import ACTION_SAVE_NOW
from archive_management.ui import discovery_page, home_page, main_window, manage_window
from archive_management.ui.models import FeedbackKind, ViewKind
from archive_management.ui.palette import DARK, DEFAULT_THEME

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


# --------------------------------------------------------- 主窗口: 写不进去 / 后端拒绝 / 后台线程


class _ConfigStub:
    """配置替身: 各段都是普通对象(这些用例只关心"保存有没有抛")."""

    def __init__(self) -> None:
        self.language = ""
        self.ui = SimpleNamespace()
        self.logging = SimpleNamespace()
        self.activation = SimpleNamespace()
        self.window: object = None
        self.hotkeys: object = None


def _app(monkeypatch: pytest.MonkeyPatch, *, failing: str = "__never__") -> Any:
    """造一个"没有窗口"的主窗口: 后端、配置、提示、重绘都换成可观察的替身."""
    monkeypatch.setattr(
        main_window,
        "load_or_repair_config",
        lambda _path: SimpleNamespace(config=_ConfigStub()),
    )
    app: Any = main_window.ArchiveApp.__new__(main_window.ArchiveApp)
    app._paths = SimpleNamespace(config_path="config.json")
    app._shortcuts = defaultdict(lambda: "ctrl+alt+x")
    app._remember_window = True
    app.p = DARK
    app.backend = _HomeBackend(failing)
    app._view = ViewKind.TIMELINE
    app._busy = False
    app._hover_id = None
    app._game = SimpleNamespace(game_id="1", name="演示", archived=False)
    app._selected_item = lambda: SimpleNamespace(backup_id="b1")
    app._card_painters = {}
    notices: list[str] = []
    app._notice = notices.append
    feedbacks: list[tuple[object, str]] = []
    app._feedback = lambda kind, message: feedbacks.append((kind, message))
    app._report_read_failure = lambda exc: feedbacks.append(("read-failure", str(exc)))
    posted: list[tuple[str, str]] = []
    app._messages = SimpleNamespace(put=lambda message: posted.append(message))
    app._posted = posted
    app._notices = notices
    app._feedbacks = feedbacks
    app._reset_activation_ladder = lambda: None
    return app


def test_an_unwritable_config_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """配置写不进去(只读/磁盘满)时只记一条日志.

    设置窗口已经把改动落到界面上, 因此这里不能把异常抛回去 —— 那会让"改了个开关"变成
    一次报错。顺带钉住"没有配置路径时"那一支(只保存在内存里)。
    """
    app = _app(monkeypatch)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("只读")

    monkeypatch.setattr(main_window, "save_config", refuse)

    app._save_language("en")
    app._save_font_size(15)
    app._save_remember_window(True)  # 打开时不动已记下的窗口几何
    app._save_remember_window(False)  # 关掉时还要顺手清掉已记下的窗口几何
    app._save_debug(True)
    app._save_activation(True)
    app._save_shortcuts()

    app._paths = None  # 没有配置路径: 只保存在内存里
    app._save_remember_window(False)


def test_an_unsupported_language_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """切到不支持的语言时把原因回给设置窗口, 不改当前语言、不重建界面."""
    app = _app(monkeypatch)

    def refuse(_locale: str) -> None:
        raise ValueError("不认识的语言")

    monkeypatch.setattr(main_window, "set_locale", refuse)

    assert app._on_language_change("xx") == "不认识的语言"


def test_a_rejected_monitor_change_is_announced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """把某款游戏设为监控对象被后端拒绝时, 用一句可读的提示说明白."""
    app = _app(monkeypatch, failing="set_game_enabled")

    app._on_set_monitor("1")

    assert app._notices, "被拒绝时要有提示"
    assert "后端拒绝了这一步" in app._notices[0]


def test_the_hotkey_callbacks_only_post_a_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """快捷键回调跑在监听线程里: 只投递消息, 界面动作留给主线程(否则会跨线程碰 Tk)."""
    app = _app(monkeypatch)

    app._request_hotkey_backup()

    assert app._posted == [("hotkey", ACTION_SAVE_NOW)]


def test_a_missing_or_broken_icon_falls_back_to_the_letter(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """图标读不到(刚被删)或解不开(文件损坏)时返回 None, 交给界面走首字回落."""
    app = _app(monkeypatch, failing="artwork_path")
    assert app._icon_image("1") is None

    broken = tmp_path / "icon.png"
    broken.write_text("这不是图片", encoding="utf-8")
    app.backend = SimpleNamespace(artwork_path=lambda *_args: str(broken))
    assert app._icon_image("1") is None


def test_a_failed_reload_keeps_the_last_picture(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """读数据失败(库被外部占用之类)只记一笔并保留上次画面, 不让 Tk 回调里冒异常."""
    app = _app(monkeypatch)

    def refuse(_task: object) -> None:
        raise sqlite3.OperationalError("database is locked")

    app._reload_data_now = refuse

    app._reload_data()

    assert app._feedbacks == [("read-failure", "database is locked")]


def test_repainting_all_cards_hands_the_palette_to_the_kit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全量重绘就是把当前配色交给套件(它自己记住谁要重画)."""
    app = _app(monkeypatch)
    painted: list[object] = []
    app.kit = SimpleNamespace(apply=painted.append)

    app._paint_cards()

    assert painted == [DARK]


def test_painting_one_card_that_is_gone_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """悬停是高频交互: 卡片刚好随列表刷新被销毁时, 这一次局部重绘直接作废."""
    app = _app(monkeypatch)

    def refuse(_palette: object) -> None:
        raise tk.TclError("bad window path name")

    app._card_painters = {"b1": refuse}

    app._paint_card_by_id("b1")
    app._paint_card_by_id("never-registered")


def test_hovering_paints_only_the_two_affected_cards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """悬停变化只重绘离开与进入的那两张(全量重绘在卡片多时肉眼可见地卡)."""
    app = _app(monkeypatch)
    painted: list[str | None] = []
    app._paint_card_by_id = painted.append

    app._on_graph_hover("b2")

    assert painted == [None, "b2"]


def test_a_rejected_restore_preview_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复前的预检被后端拒绝时给出错误提示, 不再往下走(不弹恢复对话框)."""
    app = _app(monkeypatch, failing="preview_restore")

    app._on_restore()

    assert app._feedbacks == [(FeedbackKind.ERROR, "后端拒绝了这一步")]


def test_a_rejected_delete_plan_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除前的计划被后端拒绝时给出错误提示, 不进入确认流程."""
    app = _app(monkeypatch, failing="plan_delete")

    app._on_delete_backup()

    assert app._feedbacks == [(FeedbackKind.ERROR, "后端拒绝了这一步")]


def test_a_rejected_add_game_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """新增游戏被后端拒绝时: 审计里留一笔失败, 界面上给出原因."""
    app = _app(monkeypatch, failing="add_game")
    monkeypatch.setattr(main_window, "ask_text", lambda *_args, **_kwargs: "演示")

    app._on_add_game()

    assert app._feedbacks == [(FeedbackKind.ERROR, "后端拒绝了这一步")]


def test_a_failed_window_geometry_save_only_warns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """窗口尺寸写不进去时留一条日志并收工(关窗路径上不该因为配置只读而报错)."""
    app = _app(monkeypatch)
    monkeypatch.setattr(
        main_window,
        "current_window_geometry",
        lambda *_args, **_kwargs: SimpleNamespace(width=800, height=600),
    )
    monkeypatch.setattr(main_window, "window_scaling", lambda _window: 1.0)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("只读")

    monkeypatch.setattr(main_window, "save_config", refuse)

    app._remember_window_geometry()


def test_a_failed_activation_poll_only_warns(monkeypatch: pytest.MonkeyPatch) -> None:
    """后台轮询失败只记日志: 不能让线程死掉, 也不能弹窗打断用户; 消息照旧投回主线程."""
    app = _app(monkeypatch, failing="poll_activation")

    app._run_activation_poll()

    assert app._posted == [("activation", "")]


def test_the_default_theme_comes_from_one_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认主题只有一处定义(设置窗口显示的就是它)."""
    assert main_window.default_theme() == DEFAULT_THEME


# --------------------------------------------------------- 游戏管理窗口: 后端拒绝与用户取消
#
# 这个窗口的每个动作都是"点一下 → 后端 → 重画", 而且动作之间还有一道归档闸门
# (``_blocked``)。闸门、用户取消、后端拒绝三者都要**原地停住**: 既不重画列表, 也不留下
# 一句"已改成"的提示 —— 否则用户会以为改动生效了。


def _manage(
    monkeypatch: pytest.MonkeyPatch, *, failing: str = "__never__"
) -> tuple[Any, _HomeBackend, list[str], list[int]]:
    """造一个"没有窗口"的管理窗口, 返回它、后端替身、弹过的原因与刷新次数."""
    window: Any = manage_window.ManageGameWindow.__new__(manage_window.ManageGameWindow)
    kinds = manage_window._ARTWORK_KINDS
    window._game_id = "1"
    window._name = "演示"
    window._enabled = True
    window._palette = DARK
    window._window = _WidgetStub()
    window._backend = _HomeBackend(failing)
    window._backend.user_artwork_path = lambda *_args: ""
    window._blocked = lambda _ability: False
    window._selected_item = lambda: SimpleNamespace(
        location_id="l1", path="saves/slot", path_kind="directory", ok=True
    )
    window._items = ()
    window._rows = {}
    window._hover = None
    window._artwork_images = {}
    window._artwork_preview = {kind: _WidgetStub() for kind in kinds}
    window._artwork_state = {kind: _WidgetStub() for kind in kinds}
    window._artwork_buttons = {kind: (_WidgetStub(), _WidgetStub()) for kind in kinds}
    window._toggle_btn = _WidgetStub()
    window._closed = []
    errors: list[str] = []
    refreshes: list[int] = []
    window._show_error = lambda exc: errors.append(str(exc))
    window.refresh = lambda: refreshes.append(1)
    window._on_change = lambda: None
    window._paint_buttons = lambda: None
    window._fit_window_height = lambda: None
    window.close = lambda: window._closed.append(1)
    return window, window._backend, errors, refreshes


def test_artwork_render_survives_a_backend_that_cannot_answer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端读不出有效图片路径(图刚被删)时按"没有图"画占位, 而不是让整窗渲染失败."""
    monkeypatch.setattr(ctk, "CTkFont", lambda **_kwargs: object())
    window, _backend, _errors, _refreshes = _manage(monkeypatch, failing="artwork_path")

    window._render_artwork()

    icon_preview = window._artwork_preview["icon"]
    assert icon_preview.configured[-1]["text"] == "演", "图标位放名称首字"
    assert icon_preview.configured[-1]["image"] is None


def test_an_unreadable_preview_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """预览图的三种"读不到"(没有路径/文件没了/解不开)都给 None, 让界面走文字占位."""
    window, _backend, _errors, _refreshes = _manage(monkeypatch)
    assert window._load_artwork("", "cover") is None

    gone = tmp_path / "gone.png"
    assert window._load_artwork(str(gone), "cover") is None

    broken = tmp_path / "broken.png"
    broken.write_text("这不是图片", encoding="utf-8")
    assert window._load_artwork(str(broken), "cover") is None


def test_a_loaded_preview_is_cached_per_file_version(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """缓存键带上 mtime 与大小: 换了图自然是新键, 同一份文件不重复解码."""
    window, _backend, _errors, _refreshes = _manage(monkeypatch)
    picture = tmp_path / "cover.png"
    picture.write_text("这不是图片", encoding="utf-8")
    info = picture.stat()
    key = f"{picture}:{info.st_mtime_ns}:{info.st_size}:cover"
    cached = object()
    window._artwork_images[key] = cached

    assert window._load_artwork(str(picture), "cover") is cached


def test_choosing_artwork_stops_at_the_gate_the_cancel_and_the_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """选图这条路有三处该原地停住: 归档后不让改、用户取消、后端拒绝."""
    window, _backend, errors, refreshes = _manage(
        monkeypatch, failing="set_game_artwork"
    )
    window._blocked = lambda _ability: True

    window._choose_artwork("cover")

    monkeypatch.setattr(manage_window, "pick_file", lambda **_kwargs: "")
    window._blocked = lambda _ability: False
    window._choose_artwork("cover")  # 用户取消: 连后端都不碰

    monkeypatch.setattr(
        manage_window, "pick_file", lambda **_kwargs: "pictures/cover.png"
    )
    window._choose_artwork("cover")

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_resetting_artwork_does_nothing_when_there_is_nothing_to_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """本来就用的平台图(没有自定义那份)时恢复默认什么都不做, 不必重画."""
    window, _backend, _errors, _refreshes = _manage(monkeypatch)
    window._backend.clear_game_artwork = lambda *_args: False
    rendered: list[int] = []
    window._render_artwork = lambda: rendered.append(1)

    window._reset_artwork("cover")

    assert rendered == []


def test_painting_a_location_row_that_is_gone_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """管理窗口的行被重画销毁(或已经不在这一页)时这一次局部重绘直接作废."""
    window, _backend, _errors, _refreshes = _manage(monkeypatch)

    window._paint_row("gone")

    assert window._rows == {}


def test_hovering_paints_only_the_two_affected_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """悬停变化只重绘离开与进入的那两行(空白处不算一行)."""
    window, _backend, _errors, _refreshes = _manage(monkeypatch)
    painted: list[str] = []
    window._paint_row = painted.append

    window._set_hover("l1")
    window._set_hover("l1")  # 同一行: 直接返回
    window._set_hover(None)

    assert painted == ["l1", "l1"]


def test_a_cancelled_schedule_edit_does_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """排期对话框被取消(或保存失败)时什么都不做: 不重画、不提示成功."""
    window, _backend, _errors, refreshes = _manage(monkeypatch)
    monkeypatch.setattr(manage_window, "edit_schedule", lambda *_args, **_kwargs: False)

    window._on_schedule()

    assert refreshes == []


def test_a_rejected_rename_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改名被后端拒绝(重名/参数不对)时摆出原因, 也不把新名字记下来."""
    window, _backend, errors, refreshes = _manage(monkeypatch, failing="update_game")
    monkeypatch.setattr(manage_window, "ask_text", lambda *_args, **_kwargs: "新名")

    window._on_rename()

    assert errors == ["后端拒绝了这一步"]
    assert window._name == "演示"
    assert refreshes == []


def test_a_rejected_enable_in_the_manage_window_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """启用/停用被拒绝时摆出原因, 也不改本窗口记着的状态."""
    window, _backend, errors, _refreshes = _manage(
        monkeypatch, failing="set_game_enabled"
    )

    window._on_toggle_enabled()

    assert errors == ["后端拒绝了这一步"]
    assert window._enabled is True


def test_a_rejected_delete_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删除的两步都可能被拒: 算导出路径时(不该弹确认框), 以及真删的时候(窗口不关)."""
    window, _backend, errors, _refreshes = _manage(
        monkeypatch, failing="delete_export_path"
    )

    window._on_delete_game()

    assert errors == ["后端拒绝了这一步"]
    assert window._closed == []

    other, _backend2, errors2, _refreshes2 = _manage(monkeypatch, failing="delete_game")
    monkeypatch.setattr(manage_window, "confirm_dialog", lambda *_args, **_kwargs: True)

    other._on_delete_game()

    assert errors2 == ["后端拒绝了这一步"]
    assert other._closed == [], "删失败时窗口要留着"


def test_a_rejected_add_location_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """加存档位置被拒绝时摆出原因, 且不重画列表."""
    window, _backend, errors, refreshes = _manage(monkeypatch, failing="add_location")
    monkeypatch.setattr(
        manage_window, "ask_text", lambda *_args, **_kwargs: "saves/new"
    )

    window._add_location("directory")

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_a_rejected_primary_change_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改主存档位置被拒绝时摆出原因, 且不重画列表."""
    window, _backend, errors, refreshes = _manage(
        monkeypatch, failing="set_primary_location"
    )

    window._on_set_primary()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_a_rejected_verify_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """校验存档位置被拒绝时摆出原因."""
    window, _backend, errors, refreshes = _manage(
        monkeypatch, failing="verify_location"
    )

    window._on_verify()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_a_rejected_location_edit_in_the_manage_window_reports_the_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """改存档位置路径被拒绝时摆出原因, 且不重画列表."""
    window, _backend, errors, refreshes = _manage(
        monkeypatch, failing="update_location"
    )
    monkeypatch.setattr(manage_window, "ask_text", lambda *_args, **_kwargs: "saves/新")

    window._on_edit_path()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_removing_a_location_needs_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删存档位置要确认: 取消时连后端都不碰; 确认后被拒绝则摆出原因."""
    window, backend, errors, refreshes = _manage(monkeypatch, failing="remove_location")
    monkeypatch.setattr(
        manage_window, "confirm_dialog", lambda *_args, **_kwargs: False
    )

    window._on_remove()

    assert backend.calls == [], "取消之后不该去动后端"
    assert errors == []

    monkeypatch.setattr(manage_window, "confirm_dialog", lambda *_args, **_kwargs: True)
    window._on_remove()

    assert errors == ["后端拒绝了这一步"]
    assert refreshes == []


def test_deleting_the_origin_needs_a_gate_a_selection_and_a_typed_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """删原始目录: 归档闸门、没有选中项、预检被拒、没输入确认名 —— 四处都该原地停住."""
    window, _backend, errors, refreshes = _manage(
        monkeypatch, failing="preview_location_removal"
    )

    window._blocked = lambda _ability: True
    window._on_delete_origin()
    assert errors == []

    window._blocked = lambda _ability: False
    window._selected_item = lambda: None
    window._on_delete_origin()
    assert errors == []

    window._selected_item = lambda: SimpleNamespace(
        location_id="l1", path="saves/slot", path_kind="directory", ok=True
    )
    window._on_delete_origin()
    assert errors == ["后端拒绝了这一步"]

    window._backend.preview_location_removal = lambda *_args: SimpleNamespace(
        blocked=None, path="saves/slot", files=1, total_size=10, game_name="演示"
    )
    monkeypatch.setattr(manage_window, "ask_text", lambda *_args, **_kwargs: None)
    window._on_delete_origin()

    assert len(errors) == 1, "没有输入确认名时不该再报一次错"
    assert refreshes == []
