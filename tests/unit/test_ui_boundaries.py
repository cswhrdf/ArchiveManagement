"""界面层的纯边界: 字体探测在"没有 Tk 根"时的退路, 与演示后端的失败语义.

这些用例刻意**不建 Tk 根**: 界面上真正难测的从来不是"有窗口时画得对不对", 而是
"没有窗口/取不到时报什么" —— 而这类退路在带 GUI 的用例里反而被"环境恰好可用"
掩盖掉了(整套跑下来它们从来没被执行过)。判据是各处的返回值/异常, 不是"跑过就算"。
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.ui import schedule_window, typography
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService

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
