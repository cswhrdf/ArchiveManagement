"""表头对齐的"轮数记账": 行宽一变就必须重新给满, 否则表头会永久停在旧内边距.

出处(2026-10-06 CI Windows 现场 dump, 失败用例
``tests/integration/test_gui_layout.py::test_a_long_state_column_does_not_push_the_fixed_columns``):
主页列表的表头比数据行整体差 22px —— 现场读数是

* ``_head_pads == (22, 38)``: 这是**按"滚动条还在"的行宽**算出来的内边距;
* ``_align_attempts == 4``: 对齐的轮数已经用光;
* ``_sync_job is None``: 没有挂起的同步任务;
* 行控件宽 942, 而表头按 920 摆 —— 差的 22px = 滚动条自己 16px + 数据行右侧内缩 6px。

也就是说: 滚动条收起(它只占滚动区**内部**宽度, 外层帧尺寸一点没变)之后, 行宽变了、旧内边距
作废, 但那一轮"该重算"的同步被"轮数已满"挡掉, 此后没有任何事件会再来纠正它(用例等了 3s)。
所以轮数不能按"控件一辈子"记, 得按**数据行的实测宽度**记: 宽度一变就是新的一轮几何。

三条用例把机制钉在替身上(不建 Tk 根, 不跑事件循环), 判据全是可数的数字:
同一个行宽下最多 ``_ALIGN_MAX_ATTEMPTS`` 轮, 行宽一变重新给满, 量到对齐则归零。

2026-11-07 补第四条: **这一轮算不出新内边距也要再排一轮复核**。满载(双片 + 覆盖率追踪)
时量到的往往是还没落定的几何 —— 按旧读数算出来的内边距与当前一致, 而真正的布局要等下一轮
才到; 以前这种情况直接收工, 表头就永久停在旧内边距上(六列整体差一个固定量, 用例等 3s 也
没人来纠正)。所以停手只按**轮数**, 不按读数。

2026-11-07 第二轮(**同一天的全量分片里又咬了一次**, 现场 dump 见
``test_the_header_follows_the_scrollbar_in_the_smallest_window``): ``_align_attempts=0``、
``_head_pads=(22, 66)``、名称列对上了而固定列整体差 **33px**, 等了 5s 没人来纠正。这回卡住的
不是"改不动", 而是**有一轮量到了还没重新摆的旧几何(0/0)** 就直接归零收工; 另外"量不到
(``_header_offset()`` 返回 None)"也是静默返回。所以第五、六条把两个出口也堵上: 读到 0/0 要
**连续两轮**才算对齐, 量不到也要排下一轮复核(两者都计入同一份轮数预算, 打转的闸没松)。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from archive_management.domain import HomeLayout
from archive_management.ui import home_page
from archive_management.ui.home_page import HomePage

pytestmark = [
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("控件边距与边框"),
    pytest.mark.layer("unit"),
]


class _HeadStub:
    """表头控件的替身: 只回答"被摆到哪"(对齐只通过 ``grid_configure(padx=...)`` 动几何)."""

    def __init__(self) -> None:
        self.pads: list[tuple[int, int]] = []

    def grid_configure(self, **kwargs: object) -> None:
        """记下这一次的左右内边距."""
        self.pads.append(kwargs["padx"])  # type: ignore[arg-type]


def _page() -> Any:
    """摆一个"只差几何"的主页对象: 量偏移、量行宽、排同步三件事都换成可摆的读数.

    偏移与行宽由用例直接给(真机上它们来自两个控件的屏幕坐标与滚动区的内层画布), 因此
    "表头偏了多少/行有多宽"完全可控 —— 这正是要验的那条链:
    ``_sync_list_layout`` → 按行宽记账 → ``_align_table_header``。
    """
    page: Any = HomePage.__new__(HomePage)
    page._head = _HeadStub()
    page._head_pads = (home_page._HEAD_LEFT_PAD, home_page._TABLE_SIDE_PAD)
    page._align_attempts = 0
    page._align_confirmed = False
    page._aligned_row_width = 0
    page._sync_job = None
    page._poster_name_blocks = []
    page._row_parts = {}
    page._rows = {"a": object()}
    page._filter = SimpleNamespace(layout=HomeLayout.LIST)
    # 现场读数与行宽: 用例自己改.
    page.offset = (0, 22)
    page.row_width = 942
    page.syncs = 0
    # 三个量法/动作换成替身(真机上分别是屏幕坐标、滚动区里行能用的宽度、延后任务).
    page._header_offset = lambda: page.offset
    page._row_viewport = lambda: page.row_width
    page._schedule_list_sync = lambda: setattr(page, "syncs", page.syncs + 1)
    return page


def _sync(page: Any, times: int = 1) -> None:
    """跑 ``times`` 轮同步(与真机同一条路: 滚动区事件排下来的那一轮)."""
    for _ in range(times):
        page._sync_list_layout()


def test_a_stuck_reading_stops_at_the_attempt_cap() -> None:
    """同一个行宽下读数一直不收敛: 调满 ``_ALIGN_MAX_ATTEMPTS`` 轮就停手(防自激).

    "停手"要能数出来: 到顶之后**既不再改几何、也不再排下一轮复核**
    (``_head.grid_configure`` 与 ``_schedule_list_sync`` 都不再被调用), 否则表头会自己抽起来
    —— 常量是给这条用的, 不是给"永远别放弃"用的。
    """
    page = _page()
    _sync(page, home_page._ALIGN_MAX_ATTEMPTS)
    assert len(page._head.pads) == home_page._ALIGN_MAX_ATTEMPTS
    assert page._align_attempts == home_page._ALIGN_MAX_ATTEMPTS
    assert page.syncs == home_page._ALIGN_MAX_ATTEMPTS, "每一轮都排了下一轮复核"

    _sync(page, home_page._ALIGN_MAX_ATTEMPTS)
    assert len(page._head.pads) == home_page._ALIGN_MAX_ATTEMPTS, (
        "同一个行宽下调不通就该停手: 读数没变还继续改几何就是打转"
    )
    assert page.syncs == home_page._ALIGN_MAX_ATTEMPTS, (
        "到顶之后不许再排复核 —— 那等于把 1.4s 的预算变成一个永不结束的循环"
    )


def test_a_stale_reading_does_not_end_the_alignment() -> None:
    """满载时前几轮读到的可能是还没落定的几何: 改不动也要继续复核, 直到真的对上.

    现场(2026-11-07): 六列整体差 22px, 而 ``_align_attempts`` 已经到顶、``_sync_job`` 是
    None —— 前几轮按旧读数算出"内边距不用变"就直接收工了, 真正的布局到了却再没人来量。
    这里把那一幕摆出来: 两轮都读不到进展(内边距已经没收的余量), 第三轮布局落定后才归零。
    """
    page = _page()
    page._head_pads = (0, 0)
    page.offset = (5, 0)  # 左边已经没有余量可收: 算出来的新值与当前一致

    _sync(page)
    _sync(page)

    assert page._head.pads == [], "算不出新内边距时不该改几何"
    assert page._align_attempts == 2, "每一轮都要计入轮数(否则会无限重排)"
    assert page.syncs == 2, "改不动也要再排一轮复核 —— 直接收工就是那个 22px 的偶发"

    page.offset = (0, 0)  # 布局落定
    _sync(page)
    assert page._align_attempts == 3, "读到 0 只是'也许': 还要复核一轮"

    _sync(page)
    assert page._align_attempts == 0, (
        "连续两轮都是 0 才归零: 下一次几何变化还能用满预算"
    )


def test_a_changed_row_width_gives_the_alignment_a_fresh_budget() -> None:
    """行宽变了 ⇒ 轮数重新给满(CI Windows 永久差 22px 就是缺这一条).

    行宽变 22px 是真的会发生的事: 滚动条收起后它腾出的宽度 = 滚动条 16px + 行右侧内缩 6px,
    而滚动条只占滚动区内部宽度 —— 外层帧的 ``<Configure>`` 一次都不来, 通知可能从标签自己的
    ``<Configure>`` 或页面自己补的那次同步来。按行宽记账就不挑通知是谁发的。
    """
    page = _page()
    _sync(page, home_page._ALIGN_MAX_ATTEMPTS + 3)
    spent = len(page._head.pads)
    assert spent == home_page._ALIGN_MAX_ATTEMPTS

    page.row_width += 22  # 滚动条收起: 行宽多了"滚动条 16 + 行右侧内缩 6"
    _sync(page)
    assert len(page._head.pads) == spent + 1, (
        "行宽变了就必须重新给满轮数 —— 否则表头永久停在旧内边距上(现场: 六列整体差 22px)"
    )
    assert page._align_attempts < home_page._ALIGN_MAX_ATTEMPTS


def test_a_settled_alignment_clears_the_budget() -> None:
    """量到已经对齐就归零 —— 但要**连续两轮**都读到 0/0 才算数(单次读数可能是旧几何)."""
    page = _page()
    _sync(page)
    assert len(page._head.pads) == 1
    assert page._align_attempts == 1

    page.offset = (0, 0)  # 第一轮读到"已经对齐": 只算'也许', 再复核一轮
    _sync(page)
    assert page._align_attempts == 2
    assert page.syncs == 2, "读到 0 也要复核一轮: 满载时那一轮可能是还没落定的旧几何"

    _sync(page)  # 第二轮同样读到 0/0: 这才算确认
    assert page._align_attempts == 0, "确认之后归零: 下一次几何变化还能用满预算"
    assert page.syncs == 2, "确认之后不该再反复排同步任务"


def test_a_stale_zero_reading_does_not_end_the_alignment() -> None:
    """读到 0/0 也可能是旧几何 —— 2026-11-07 全量分片现场就是这么卡住的(固定列差 33px).

    现场 dump: ``_align_attempts=0``、``_head_pads=(22, 66)``、名称列对上了、只有固定列那一块
    整体差 33px, 此后 5s 再没人来纠正。成因是某一轮量到了"还没重新摆"的旧几何(0/0), 按它
    归零就等于收工, 而真正的布局 60ms 后才到(行宽没变、标签宽度也没变, 没有任何事件再来叫
    我们)。这里把那一幕摆出来: 先把读数摆成 0/0, 下一轮才是真实偏差。
    """
    page = _page()
    page.offset = (0, 0)
    _sync(page)

    assert page._align_confirmed is True
    assert page.syncs == 1, (
        "读到 0 也要再排一轮复核 —— 直接归零收工就是那个 33px 的偶发"
    )

    page.offset = (0, -33)  # 布局落定, 固定列那一块才露出偏差
    _sync(page)
    assert len(page._head.pads) == 1, "复核那一轮必须真的去改几何"
    assert page._align_confirmed is False, "刚改过几何, 下一个 0/0 只能算'也许'"


def test_an_unmeasurable_round_still_rearms() -> None:
    """量不到(行正在重建/尚未 map)也不许静默收工 —— 但同样守着那份轮数预算."""
    page = _page()
    page.offset = None
    _sync(page, 3)

    assert page._head.pads == [], "量不到就没几何可改"
    assert page.syncs == 3, "量不到就排下一轮: 静默返回会让表头永久停在旧内边距上"
    assert page._align_attempts == 3, "复核同样计入轮数, 否则会无限重排"

    _sync(page, home_page._ALIGN_MAX_ATTEMPTS)  # 一直量不到: 到顶后停手
    assert page.syncs == home_page._ALIGN_MAX_ATTEMPTS, "到顶之后不许再排复核"

    page.offset = (0, 0)
    _sync(page, 2)
    assert page._align_attempts == 0, "行回来之后照样能确认并归零"


def test_a_zero_reading_at_the_cap_stops_instead_of_rearming() -> None:
    """轮数到顶时即便读到 0/0 也只记下'也许'、不再排复核(防自激的最后一道闸)."""
    page = _page()
    # 行宽已经记过账(否则 ``_rearm_alignment`` 会把它当成"新几何"重新给满轮数).
    page._aligned_row_width = page.row_width
    page._align_attempts = home_page._ALIGN_MAX_ATTEMPTS
    page.offset = (0, 0)

    _sync(page)

    assert page._align_confirmed is True, "这一次读数照样要记下来"
    assert page.syncs == 0, "到顶之后不许再排复核 —— 那等于把 1.4s 的预算变成死循环"
