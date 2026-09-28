"""窗口几何的记忆: 打开时按记住的尺寸位置摆, 关窗时读回来(最大化时跳过).

两条规则都是**纯算术/纯判定**, 所以这里用替身窗口跑, 不建 Tk:

* :func:`open_window_geometry` —— 记住的那套几何要**夹进当前屏幕**(换了更小的屏幕、
  或那台显示器不在了, 都不能把窗口摆到屏幕外);
* :func:`current_window_geometry` —— 最大化/最小化/全屏时的读数**不属于用户摆的那套**,
  必须交回 ``None``(跳过 = 保留上一次记下的值)。

位置读 ``winfo_x/y``: 与 ``geometry("+x+y")`` 是**同一套坐标**。实测在这台 Windows 上
``winfo_rootx/rooty`` 比它大 8/31(边框 + 标题栏), 拿 root 坐标去复原会让窗口每开一次
就往下右漂一格 —— 这条是集成用例先拿住的。

平台差异也是量出来的: Windows 上最大化时 ``state()`` 是 ``zoomed`` 且**没有**
``-zoomed`` 属性(实测报 ``bad attribute``); X11 上最大化不改 ``state``、只有
``-zoomed`` 为 1; 全屏在三个平台上都只改 ``-fullscreen``。三条都要判。
"""

from __future__ import annotations

import tkinter
from collections.abc import Mapping
from typing import Any, cast

import pytest

from archive_management.config import WindowSettings
from archive_management.ui.main_window import (
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
    current_window_geometry,
    open_window_geometry,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("窗口几何的记忆"),
    pytest.mark.layer("unit"),
]

# 一块 1920x1080 的屏(替身窗口报的屏幕尺寸).
_SCREEN = (1920, 1080)


class _FakeWindow:
    """替身窗口: 只提供这两个函数会用到的那几个能力(不建 Tk).

    默认几何取 **1400x900**(不小于 :data:`WINDOW_MIN_SIZE`): 更小的尺寸在真实窗口上
    根本不会出现(minsize 会拦住), 拿它当夹具会把"夹进最小尺寸"那条规则跟"原样用上"
    混在一起。
    """

    def __init__(
        self,
        *,
        state: str = "normal",
        geometry: tuple[int, int, int, int] = (1400, 900, 160, 120),
        flags: Mapping[str, int] | None = None,
        unknown: tuple[str, ...] = (),
    ) -> None:
        """记下窗口状态、几何与属性; ``unknown`` 是这台"平台"没有的属性."""
        self._state = state
        self._geometry = geometry
        self._flags = dict(flags or {})
        self._unknown = unknown

    def state(self) -> str:
        """``wm state``(normal / zoomed / iconic / withdrawn)."""
        return self._state

    def attributes(self, name: str) -> object:
        """平台没有这个属性就抛 TclError(与真实 Tk 一致), 否则返回属性值."""
        if name in self._unknown:
            raise tkinter.TclError(f'bad attribute "{name}"')
        return self._flags.get(name, 0)

    def winfo_width(self) -> int:
        """客户区宽度."""
        return self._geometry[0]

    def winfo_height(self) -> int:
        """客户区高度."""
        return self._geometry[1]

    def winfo_x(self) -> int:
        """窗口(外框)在屏幕上的 x 坐标: 与 ``geometry("+x+y")`` 同一套坐标."""
        return self._geometry[2]

    def winfo_y(self) -> int:
        """窗口(外框)在屏幕上的 y 坐标(见 :meth:`winfo_x`)."""
        return self._geometry[3]


def _window(**kwargs: Any) -> tkinter.Tk:
    """造一个替身窗口并声明成 Tk(被测函数只按这个契约读它)."""
    return cast("tkinter.Tk", _FakeWindow(**kwargs))


# ------------------------------------------------------------ 打开时的几何


def test_an_unremembered_window_opens_at_the_default_size() -> None:
    """没记过就用设计尺寸(在 1920x1080 的屏上装得下, 因此原样)."""
    assert open_window_geometry(WindowSettings(), screen=_SCREEN) == (
        f"{WINDOW_DEFAULT_SIZE[0]}x{WINDOW_DEFAULT_SIZE[1]}"
    )


def test_the_default_size_is_clamped_on_a_small_screen() -> None:
    """屏幕装不下设计尺寸就按屏幕夹, 但不低于最小尺寸(布局的硬下限)."""
    small = (1366, 768)

    assert open_window_geometry(WindowSettings(), screen=small) == "1270x720"
    # 比最小尺寸还小的屏: 只能不夹(再压就把内容挤出窗口).
    assert open_window_geometry(WindowSettings(), screen=(1000, 600)) == (
        f"{WINDOW_MIN_SIZE[0]}x{WINDOW_MIN_SIZE[1]}"
    )


def test_a_remembered_geometry_is_used_as_is() -> None:
    """记住的整套几何在屏幕里装得下时原样用上(尺寸 + 位置)."""
    saved = WindowSettings(width=1400, height=900, x=160, y=120)

    assert open_window_geometry(saved, screen=_SCREEN) == "1400x900+160+120"


def test_a_remembered_window_is_clamped_back_onto_the_screen() -> None:
    """换了更小的屏幕后: 尺寸按屏幕夹, 位置夹回屏幕内(否则"打开就找不到窗口")."""
    saved = WindowSettings(width=2400, height=1400, x=1700, y=900)

    # 宽高都超了 → 夹到 屏幕 - 96; 位置跟着夹到最后一块可见区域里。
    assert open_window_geometry(saved, screen=(1920, 1080)) == "1824x984+96+96"
    # 负坐标(显示器在主屏左边)同样夹回 0。
    left = WindowSettings(width=1400, height=900, x=-1920, y=-40)
    assert open_window_geometry(left, screen=_SCREEN) == "1400x900+0+0"
    # 位置越出右边/下边时夹到"贴边"。
    corner = WindowSettings(width=1400, height=900, x=1700, y=900)
    assert open_window_geometry(corner, screen=_SCREEN) == "1400x900+520+180"
    # 尺寸小于最小尺寸(手改配置)时往上夹到最小尺寸。
    tiny = WindowSettings(width=200, height=100, x=0, y=0)
    assert open_window_geometry(tiny, screen=_SCREEN) == (
        f"{WINDOW_MIN_SIZE[0]}x{WINDOW_MIN_SIZE[1]}+0+0"
    )


# ------------------------------------------------------------ 关闭时的读数


def test_a_normal_window_is_read_back_as_remembered_geometry() -> None:
    """普通窗口: 尺寸与位置原样读回来."""
    saved = current_window_geometry(_window())

    assert saved is not None
    assert saved.geometry() == (1400, 900, 160, 120)


@pytest.mark.parametrize("state", ["zoomed", "iconic", "withdrawn", "unknown"])
def test_a_maximized_or_hidden_window_is_skipped(state: str) -> None:
    """最大化/最小化等非 normal 状态一律跳过(跳过 = 保留上次记下的值)."""
    assert current_window_geometry(_window(state=state)) is None


def test_a_fullscreen_window_is_skipped() -> None:
    """全屏时 state 仍是 normal: 不判 ``-fullscreen`` 就会把整块屏幕记成用户摆的尺寸."""
    assert current_window_geometry(_window(flags={"-fullscreen": 1})) is None


def test_a_maximized_window_on_x11_is_skipped() -> None:
    """X11 上的最大化不改 state, 只有 ``-zoomed`` 为 1(实测 Windows 没有这个属性)."""
    assert current_window_geometry(_window(flags={"-zoomed": 1})) is None


def test_a_platform_without_the_zoomed_attribute_is_not_maximized() -> None:
    """Windows 上读 ``-zoomed`` 会抛 TclError: 取不到就是没最大化(不能因此跳过)."""
    saved = current_window_geometry(_window(unknown=("-zoomed",)))

    assert saved is not None
    assert saved.geometry() == (1400, 900, 160, 120)


def test_a_destroyed_window_is_skipped() -> None:
    """窗口已经销毁(读 state 抛 TclError)时不记: 宁可保留上一次的值."""
    window = _FakeWindow()

    def boom() -> str:
        raise tkinter.TclError("bad window path name")

    window.state = boom  # type: ignore[method-assign]

    assert current_window_geometry(cast("tkinter.Tk", window)) is None
