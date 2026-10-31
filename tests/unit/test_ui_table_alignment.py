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

    "停手"要能数出来: 到顶之后**不再改几何**(``_head.grid_configure`` 不再被调用),
    否则表头会自己抽起来 —— 常量是给这条用的, 不是给"永远别放弃"用的。
    """
    page = _page()
    _sync(page, home_page._ALIGN_MAX_ATTEMPTS)
    assert len(page._head.pads) == home_page._ALIGN_MAX_ATTEMPTS
    assert page._align_attempts == home_page._ALIGN_MAX_ATTEMPTS

    _sync(page, home_page._ALIGN_MAX_ATTEMPTS)
    assert len(page._head.pads) == home_page._ALIGN_MAX_ATTEMPTS, (
        "同一个行宽下调不通就该停手: 读数没变还继续改几何就是打转"
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
    """量到已经对齐就归零: 下一次几何变化还能用满轮数."""
    page = _page()
    _sync(page)
    assert len(page._head.pads) == 1
    assert page._align_attempts == 1

    page.offset = (0, 0)  # 下一轮量到"已经对齐"
    _sync(page)
    assert page._align_attempts == 0
    assert page.syncs >= 1, "对齐之后不该再反复排同步任务"
