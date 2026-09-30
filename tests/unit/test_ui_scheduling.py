"""界面里"延后合并"的定时任务: 只能合并请求, **不能**把任务无限推后; 名称重裁也不能漏.

CI 上真实挂过三次: 名称重裁是延后 60ms 的任务, 旧实现每次请求都先 ``after_cancel`` 再重新
计时 —— 事件流只要足够密集(无窗口管理器的 Xvfb、CustomTkinter 的延迟重绘、滚动区尺寸连续
变化), 任务就被无限推后, 永远轮不到执行。另外滚动区的 ``<Configure>`` 只在**它自己**的尺寸变
化时来: 内宽变了而外宽没变(滚动条出现/消失、表头内边距被重算)、行刚重建标签刚量到真实宽度
时都可能等不到下一轮 —— 所以名称标签自己也要盯宽度。

这类回归不会让任何用例变红(旧实现在 Windows 上一直是绿的), 所以这里盯住**契约**本身:
排队两次不等于排两个任务, 更不等于把前一个撤掉; 宽度没变就不该重写文本(否则"改文本 →
新的 Configure → 再裁"会互相追)。用替身控件驱动真实的方法, 不建窗口。
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import customtkinter as ctk
import pytest

from archive_management.ui.home_page import HomePage, _RowParts
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import AppPage
from archive_management.ui.textfit import fit_text

_REPO_ROOT = Path(__file__).resolve().parents[2]
_HOME_PAGE = _REPO_ROOT / "src" / "archive_management" / "ui" / "home_page.py"

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("延后合并的定时任务"),
    pytest.mark.layer("unit"),
]


class _Scheduler:
    """替身控件: 记录排了哪些任务、撤了哪些任务, 并能按"时间到"手动触发."""

    def __init__(self) -> None:
        """初始化空的排队记录."""
        self.jobs: list[tuple[int, Any]] = []
        self.cancelled: list[str] = []
        # ``CTkFrame.bind`` 把绑定转发到内层画布, 销毁事件里的 widget 是它(见
        # ``HomePage._on_frame_destroyed``): 替身也备一个, 好把那条接线也测到.
        self.canvas = object()

    def after(self, delay_ms: int, callback: Any) -> str:
        """排一个任务, 返回形如 ``after#N`` 的 id(与 Tk 一致)."""
        job = f"after#{len(self.jobs) + 1}"
        self.jobs.append((delay_ms, callback))
        return job

    def after_cancel(self, job: str) -> None:
        """撤掉一个任务."""
        self.cancelled.append(job)

    def fire(self, index: int = 0) -> None:
        """让第 ``index`` 个排队任务"到点执行"."""
        self.jobs[index][1]()


class _HomePageScheduler:
    """借用真实的 ``HomePage._schedule_list_sync``, 但不跑 ``__init__``(不建控件)."""

    def __init__(self) -> None:
        """装上替身控件与真实方法依赖的状态字段."""
        self.frame = _Scheduler()
        self._sync_job: str | None = None
        self.syncs = 0

    def _sync_list_layout(self) -> None:
        """记录同步真的跑了一次."""
        self.syncs += 1
        self._sync_job = None

    def schedule(self) -> None:
        """调用真实实现(不绑到 HomePage 实例上, 免得构造整棵控件树)."""
        HomePage._schedule_list_sync(self)  # type: ignore[arg-type]


class _DetailScheduler:
    """借用真实的 ``MainWindow._on_content_resize``, 但不建窗口."""

    def __init__(self) -> None:
        """装上替身内容区与真实方法依赖的状态字段."""
        self._content = _Scheduler()
        self._refit_job: str | None = None
        self._page = AppPage.DETAIL
        self._detail_name = object()
        self.refits = 0
        # 真实实现把 ``self._refit_detail_names`` 交给 after; 这里用同一个入口记录执行.
        self._refit_detail_names = self.run_refit

    def resize(self) -> None:
        """模拟内容区尺寸变化事件."""
        ArchiveApp._on_content_resize(self, None)  # type: ignore[arg-type]

    def run_refit(self) -> None:
        """记录详情重裁真的跑了一次."""
        self.refits += 1
        self._refit_job = None


@pytest.fixture
def list_sync() -> _HomePageScheduler:
    """列表同步(表头对齐 + 名称重裁)的调度器."""
    return _HomePageScheduler()


@pytest.fixture
def detail_refit() -> _DetailScheduler:
    """详情页名称重裁的调度器."""
    return _DetailScheduler()


class _NameLabel:
    """替身名称标签: 记录被写进去的文本."""

    def __init__(self) -> None:
        """从空文本开始."""
        self.writes: list[str] = []
        self.bound: list[str] = []

    def configure(self, *, text: str) -> None:
        """记录一次文本重设."""
        self.writes.append(text)

    def bind(self, sequence: str, *_args: Any, **_kwargs: Any) -> str:
        """裁剪会被省掉的尾巴时, 标签上会挂悬停提示(记下绑过哪些事件)."""
        self.bound.append(sequence)
        return ""


class _Font:
    """替身字体: 每个字符固定 10px(便于算出期望的裁剪结果)."""

    def measure(self, text: str) -> int:
        """字符数乘 10."""
        return len(text) * 10


def _row_parts(label: Any, full_name: str) -> _RowParts:
    """只装名称标签与完整名称的行部件(其余字段留空)."""
    return _RowParts(cast(Any, None), cast(Any, None), cast(Any, label), full_name)


def test_row_name_fit_skips_unknown_and_unchanged_widths() -> None:
    """宽度未知或没变就不重设文本.

    "没变也重设"会一直把文本改回去, 于是每一次改动又触发一个 Configure —— 自己追自己。
    """
    page = object.__new__(HomePage)
    page._name_font = cast(Any, _Font())
    label = _NameLabel()
    full = "abcdefghij"
    parts = _row_parts(label, full)

    HomePage._fit_row_name(page, parts, 1)  # 宽度还没量出来: 不动
    HomePage._fit_row_name(page, parts, 50)  # 第一次按 50px 裁
    HomePage._fit_row_name(page, parts, 50)  # 同一宽度: 不重写
    HomePage._fit_row_name(page, parts, 80)  # 变宽: 重新裁
    HomePage._fit_row_name(page, parts, 200)  # 宽到放得下整串: 不再裁

    assert label.writes == [
        fit_text(full, cast(Any, _Font()), 50),
        fit_text(full, cast(Any, _Font()), 80),
        full,
    ]
    assert parts.fitted_width == 200
    # 被裁掉的时候要挂悬停提示(完整内容还能看到): 裁了两次但只绑一遍(提示文案就地换,
    # 不重复绑事件); 不再被裁时也不新增绑定。
    assert label.bound.count("<Enter>") == 1, label.bound
    assert set(label.bound) == {"<Enter>", "<Leave>", "<Button-1>", "<Destroy>"}


def test_row_name_label_watches_its_own_width() -> None:
    """行内名称标签要自己绑 ``<Configure>``: 容器事件不一定来(内宽变了而外宽没变)."""
    source = _HOME_PAGE.read_text(encoding="utf-8")

    assert "def _on_name_resize" in source, "标签自己的尺寸变化要有人处理"
    bound = re.search(r'name\.bind\(\s*"<Configure>"', source) is not None
    assert bound, "名称标签没有绑自己的 <Configure>: 宽度变了可能一直不重裁"


def test_list_sync_coalesces_without_cancelling(list_sync: _HomePageScheduler) -> None:
    """连续请求只排一个任务, 且**一次都不撤销**: 撤销就是"无限推后"的病因."""
    list_sync.schedule()
    first = list_sync.frame.jobs[0]
    for _ in range(50):
        list_sync.schedule()

    assert list_sync.frame.jobs == [first], "重复请求不该再排任务(会重复重裁)"
    assert list_sync.frame.cancelled == [], (
        "不能撤销已排的任务: 密集的尺寸事件会把它无限推后(CI 上 Linux/macOS 挂过)"
    )
    assert first[0] > 0, "延后合并的延迟应当为正数"


def test_list_sync_runs_after_the_queued_job(list_sync: _HomePageScheduler) -> None:
    """事件再密集, 已经排好的那一次也一定会执行, 执行后才能再排下一次."""
    list_sync.schedule()
    for _ in range(10):
        list_sync.schedule()

    list_sync.frame.fire()

    assert list_sync.syncs == 1, "重裁必须真的跑起来"
    list_sync.schedule()
    assert len(list_sync.frame.jobs) == 2, "执行完成后可以再排一次"


def test_detail_refit_coalesces_without_cancelling(
    detail_refit: _DetailScheduler,
) -> None:
    """详情页的名称重裁走同一条规则(窗口连续缩放时同样不能被推后)."""
    detail_refit.resize()
    first = detail_refit._content.jobs[0]
    for _ in range(50):
        detail_refit.resize()

    assert detail_refit._content.jobs == [first]
    assert detail_refit._content.cancelled == []
    detail_refit._content.fire()
    assert detail_refit.refits == 1


def test_detail_refit_is_ignored_on_other_pages() -> None:
    """不在详情页时尺寸变化不该排队(白算一次重裁)."""
    scheduler = _DetailScheduler()
    scheduler._page = AppPage.HOME

    scheduler.resize()

    assert scheduler._content.jobs == []


def test_home_page_clears_pending_sync_when_host_is_destroyed() -> None:
    """宿主帧销毁时要撤掉挂起的任务: after 回调打到已销毁控件上会往 stderr 丢噪声.

    这里走**真实接线**(销毁事件 → :meth:`HomePage._on_frame_destroyed`, 判据是内层画布):
    收尾只测 ``cancel_list_sync()`` 的话, "接线接没接上"没人管 —— 实测那样写时清理根本
    没被调用(销毁事件里的 widget 是 CTkFrame 的内层画布, 不是 ``self.frame``)。
    """
    page = object.__new__(HomePage)
    page.frame = _Scheduler()
    page._frame_host = page.frame.canvas
    page._sync_job = None
    HomePage._schedule_list_sync(page)
    assert page._sync_job is not None

    HomePage._on_frame_destroyed(
        page, cast(Any, SimpleNamespace(widget=page.frame.canvas))
    )

    assert page._sync_job is None
    assert page.frame.cancelled == ["after#1"]


def test_a_child_destroy_does_not_cancel_the_pending_sync() -> None:
    """子控件的 Destroy 会冒泡上来: 那不叫"宿主没了", 不该撤掉挂起的任务."""
    page = object.__new__(HomePage)
    page.frame = _Scheduler()
    page._frame_host = page.frame.canvas
    page._sync_job = None
    HomePage._schedule_list_sync(page)

    HomePage._on_frame_destroyed(page, cast(Any, SimpleNamespace(widget=object())))

    assert page._sync_job is not None, "子控件销毁不该撤掉挂起的同步任务"
    assert page.frame.cancelled == []


def test_destroy_cancels_the_timers_the_window_owns() -> None:
    """主窗口销毁前撤掉消息轮询与详情重裁: 否则销毁后它们还会以 Tcl 报错刷 stderr."""
    window = object.__new__(ArchiveApp)
    window._poll_job = "after#7"
    window._refit_job = "after#8"
    cancelled: list[str] = []
    window.after_cancel = cancelled.append
    destroyed: list[bool] = []

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(ctk.CTk, "destroy", lambda _self: destroyed.append(True))
        ArchiveApp.destroy(window)

    assert sorted(cancelled) == ["after#7", "after#8"]
    assert window._poll_job is None
    assert window._refit_job is None
    assert destroyed == [True], "撤完定时任务仍然要真的销毁窗口"


def test_destroy_survives_a_half_built_window() -> None:
    """构造中途失败也会走到销毁, 那时任务字段还没建好 —— 销毁不能再抛异常搅乱现场."""
    window = object.__new__(ArchiveApp)
    destroyed: list[bool] = []

    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(ctk.CTk, "destroy", lambda _self: destroyed.append(True))
        ArchiveApp.destroy(window)

    assert destroyed == [True]
    assert window._poll_job is None
    assert window._refit_job is None
