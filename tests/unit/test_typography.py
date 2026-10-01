"""界面字号单位(px / rem)与缩放换算的单元测试."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

from archive_management.config import DEFAULT_BASE_FONT_PX
from archive_management.ui import typography

pytestmark = [
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("设置窗口"),
    pytest.mark.story("可调基准字号"),
    pytest.mark.layer("unit"),
]


@pytest.fixture(autouse=True)
def _restore_base_font() -> Iterator[None]:
    """每个用例后把基准字号拨回默认值, 避免影响其它用例(它是进程内全局)."""
    yield
    typography.set_base_font_px(DEFAULT_BASE_FONT_PX)


def test_default_scale_is_identity() -> None:
    """默认 16px 时倍数为 1.0: 既有字号一个像素都不会变."""
    scale = typography.set_base_font_px(DEFAULT_BASE_FONT_PX)

    assert scale.ratio == 1.0
    assert typography.scaled(11) == 11
    assert typography.scaled(34) == 34
    assert typography.rem(1) == DEFAULT_BASE_FONT_PX
    assert typography.rem(0.5) == DEFAULT_BASE_FONT_PX // 2


def test_scaling_follows_the_base_size() -> None:
    """基准字号 20px 时一切等比放大(12 → 15), rem 直接等于基准字号."""
    typography.set_base_font_px(20)

    assert typography.scaled(12) == 15
    assert typography.scaled(16) == 20
    assert typography.rem(1) == 20
    assert typography.rem(1.5) == 30


def test_scale_never_produces_a_zero_size() -> None:
    """再小也不给 0: 0 号字在 Tk 里等于用默认字体, 会让"缩小"看起来没生效."""
    typography.set_base_font_px(DEFAULT_BASE_FONT_PX)
    tiny = typography.FontScale(base_px=1)

    assert tiny.px(0) == 1
    assert tiny.px(8) == 1
    assert tiny.rem(0) == 1
    # Tk 里负字号表示"以像素为单位", 缩放后也要保持负数
    assert typography.scaled(-12) == -12
    assert tiny.px(-16) == -1


@pytest.mark.parametrize(
    ("given", "expected"),
    [(5, 12), (99, 28), (16, 16), (18, 18)],
)
def test_base_font_is_clamped_to_the_allowed_range(given: int, expected: int) -> None:
    """越界的基准字号夹到配置允许的区间(最后一道保护)."""
    assert typography.set_base_font_px(given).base_px == expected
    assert typography.current_scale().base_px == expected


def _fake_font() -> tuple[typography._ScaledFont, list[str]]:
    """造一个不碰 Tk 的字体对象, 并把"真正调 Tcl 的那一步"换成计数器.

    ``tkinter.font.Font`` 没有 ``__slots__``, 所以可以跳过 ``__init__`` 直接构造 —— 要验证的
    正是"这一步发生在哪个线程上", 而"这一步做了什么"无关紧要。
    """
    calls: list[str] = []
    font = object.__new__(typography._ScaledFont)
    font._delete_tcl_font = lambda: calls.append(  # type: ignore[method-assign]
        "delete"
    )
    return font, calls


def test_a_font_is_released_immediately_on_the_main_thread() -> None:
    """主线程上终结字体: 立刻释放(这条路径与改造前完全一致)."""
    font, calls = _fake_font()
    typography.drain_deferred_fonts()

    font.__del__()

    assert calls == ["delete"]
    assert typography.deferred_font_count() == 0


def test_a_font_is_only_deferred_on_a_worker_thread() -> None:
    """后台线程上终结字体: **一次 Tcl 都不碰**, 只寄存等主线程来删.

    这正是 2026-10-01 CI 段错误的那条路径(``tkinter/font.py`` 的 ``__del__`` 在 worker
    线程的 GC 里调 Tcl, 主线程同时正在 ``canvas.coords``)。修法不是"让它在那儿删得对",
    而是"根本别在那儿删"。
    """
    font, calls = _fake_font()
    typography.drain_deferred_fonts()

    worker = threading.Thread(target=font.__del__, name="font-reaper")
    worker.start()
    worker.join()

    assert calls == []
    assert typography.deferred_font_count() == 1

    assert typography.drain_deferred_fonts() == 1
    assert calls == ["delete"]
    assert typography.deferred_font_count() == 0


def test_the_message_pump_drains_deferred_fonts() -> None:
    """主窗口的消息泵负责排空: 它本来就在主线程上按 100ms 周期跑, 不需要新的事件源."""
    from archive_management.ui import main_window

    source = Path(main_window.__file__).read_text(encoding="utf-8")
    body = source[source.index("def _poll_messages") :][:600]

    assert "drain_deferred_fonts()" in body


def test_each_platform_prefers_a_family_that_really_exists_there() -> None:
    """候选表的第一位必须是"那个平台真的装了的"字体 —— 顺序错了画面就变.

    - Windows 上必须还是 **Roboto**: customtkinter 自带并私有注册它, 现有界面的字形与
      入库的基线都是照它采的;
    - Linux 上必须是**中日韩字体**: uv 管的那份 Tk 一个 TTF 都用不了(只走 X11 核心位图字体,
      见 PLAN §22), 发行版的 Tk 则靠 fontconfig 找字体 —— 而 CI 装的是 fonts-noto-cjk。
    """
    assert typography.preferred_candidates("win32")[0] == "Roboto"
    assert typography.preferred_candidates("linux")[0].startswith("Noto Sans CJK")
    assert "PingFang SC" in typography.preferred_candidates("darwin")


def test_the_family_is_chosen_by_really_resolving_it() -> None:
    """挑族名靠"真的解析得到", 而不是"在 ``families()`` 列表里".

    实测 2026-10-01~02: Windows 上 ``font.families()`` **看不到** Roboto(customtkinter 用
    ``FR_PRIVATE | FR_NOT_ENUM`` 注册, 它不进枚举), 但 ``actual("family")`` 就是 Roboto。
    只查列表会得出"Roboto 不存在", 顺手把 Windows 的画面也改掉。
    """
    assert typography.pick_family(["A", "B", "C"], lambda name: name == "B") == "B"
    assert typography.pick_family(["A"], lambda name: False) is None
    assert typography.pick_family([], lambda name: True) is None


def test_a_requested_family_wins_over_the_platform_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调用方写了 family 就用它的(那是"这一处有意用别的字体"), 只有没写才补平台默认."""
    monkeypatch.setattr(typography, "current_family", lambda: "平台默认")

    assert typography._family_for_request("指定字体") == "指定字体"
    assert typography._family_for_request(None) == "平台默认"


def test_creating_a_font_goes_through_the_platform_family() -> None:
    """接线守卫: ``_ScaledFont.__init__`` 必须真的把解析结果用上(否则整段解析白做)."""
    source = Path(typography.__file__).read_text(encoding="utf-8")

    assert "family=_family_for_request(family)" in source
