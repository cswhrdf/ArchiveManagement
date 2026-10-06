"""页面与工作窗口的处理函数: 后端拒绝、用户取消、窗口已经没了的时候各自怎么收场.

这些处理方法在带 GUI 的用例里几乎都只走"顺利"那一半 —— 点击、等一等、看列表变了。
而出错那一半(后端抛出域异常、用户在确认框上按了取消、延后动作晚于关窗)才决定用户看到
什么: 是**一句能读的原因**, 还是"点了没反应"。

所以这里不建 Tk 根, 只用替身把界面对象拼出来, 然后直接叫那些处理方法。判据是各处的
返回值、"弹了什么"以及**有没有假装成功**(该 reload 的时候没 reload)。
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.ui import discovery_page
from archive_management.ui.palette import DARK

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(页面处理函数的失败退路)"),
    pytest.mark.story("后端拒绝与用户取消时的收场"),
    pytest.mark.layer("unit"),
]


class _WidgetStub:
    """最小控件替身: 记下每次改写(标签文案就是判据)."""

    def __init__(self) -> None:
        self.configured: list[dict[str, object]] = []

    def configure(self, **kwargs: object) -> None:
        """记下每次改写."""
        self.configured.append(dict(kwargs))

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
