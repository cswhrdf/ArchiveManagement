"""域层与探测/调度的边界: 环、缺 id、没有回调、目录读不到.

这些函数都是纯函数或只需一个替身对象, 所以边界几乎零成本 —— 值得一条条钉住:

- 队列操作: 把在队里的游戏移到队首时要顺手清掉抑制标记, 并重排位置;
- 删除计划: 缺 id 的节点要在建索引时被忽略(而不是拿 ``None`` 当键);
- 树剪枝: 父链绕不出去的环要断开成根, 绝不能死循环;
- 调度: 移除不存在的任务在"不静默"时要报错, 静默时吞掉; 回调已消失时跑一次什么都不做;
- 探测: 平台取自注入的根目录规则; 目录读不到时返回 ``None``(调用方据此跳过这一处)。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import archive_management.domain.deletion as deletion_mod
import archive_management.services.platform_scan as scan_mod
from archive_management.domain import BackupNode
from archive_management.domain.activation import RunEntry, move_to_front
from archive_management.domain.tree import TreeInput, keep_surviving
from archive_management.exceptions import SchedulingError
from archive_management.services.platform_scan import (
    LocalGameScanner,
    WinRegistry,
    default_roots,
)
from archive_management.services.scheduler import (
    ApschedulerBackend,
    BackupScheduler,
    ManualBackend,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(域层边界)"),
    pytest.mark.story("纯函数与调度的边界输入"),
    pytest.mark.layer("unit"),
]


# --------------------------------------------------------------- 队列与树


def test_moving_a_tracked_game_to_the_front_clears_its_suppression() -> None:
    """手动启用一款在队游戏: 它要回到队首, 抑制标记清掉, 位置按序重排(位置从 1 起)."""
    tracked = RunEntry(game_id=1, position=0, suppressed=True)
    other = RunEntry(game_id=2, position=1)

    moved = move_to_front((tracked, other), 1)

    assert [entry.game_id for entry in moved] == [1, 2]
    assert moved[0].suppressed is False
    assert [entry.position for entry in moved] == [1, 2]


def test_indexing_a_delete_plan_ignores_nodes_without_an_id() -> None:
    """缺 id 的节点在建索引时被忽略: 不拿 ``None`` 当键, 也不进父子表."""
    links = deletion_mod._index([BackupNode(game_id=1, node_kind="manual")])

    assert links.node_kind == {}
    assert links.parent == {}


def test_pruning_a_parent_cycle_breaks_it_into_a_root() -> None:
    """父链绕不出去的环要断开成根 —— 这条守卫防的是死循环, 不是好看."""
    items = [
        TreeInput(node_id="s", parent_id="y"),
        TreeInput(node_id="x", parent_id="y"),
        TreeInput(node_id="y", parent_id="x"),
    ]

    kept = keep_surviving(items, {"s"})

    assert [item.node_id for item in kept] == ["s"]
    assert kept[0].parent_id is None, "环要断成根, 不能一直往上找"


# --------------------------------------------------------------- 调度


def test_removing_a_missing_job_is_reported_unless_silent() -> None:
    """移除不存在的任务: 不静默时给可读错误, 静默时只是不做."""
    backend = ApschedulerBackend()
    try:
        with pytest.raises(SchedulingError, match="移除定时任务失败"):
            backend._remove("no-such-job", silent=False)

        backend._remove("no-such-job", silent=True)  # 不抛
    finally:
        backend.shutdown()


def test_running_a_job_whose_callback_is_gone_does_nothing() -> None:
    """回调已经注销(界面关了、游戏删了)时跑一次: 不算失败, 也不留错误状态."""
    scheduler = BackupScheduler(backend=ManualBackend())

    assert scheduler._run(999) is True

    assert scheduler._errors == {}


# --------------------------------------------------------------- 探测


def test_the_scanner_reports_the_platform_it_was_built_for() -> None:
    """探测用的平台取自注入的根目录规则(界面与用例据此换平台)."""
    roots = default_roots(platform="linux", env={"HOME": "/home/demo"})

    assert LocalGameScanner(roots).platform == "linux"


def test_listing_children_of_an_unreadable_directory_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """监控目录读不到(权限/已删)时返回 ``None``: 调用方据此跳过这一处, 不中断整次探测."""
    folder = tmp_path / "monitored"
    folder.mkdir()

    def refuse(self: Path) -> Iterator[Path]:
        raise OSError("权限不足")

    monkeypatch.setattr(Path, "iterdir", refuse)

    assert LocalGameScanner._list_children(folder) is None


def test_a_missing_registry_module_is_just_a_degradation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``winreg`` 导不进来(非 Windows / 精简过的解释器)时静默给 ``None``, 而不是抛 ImportError.

    这一支原先靠平台标记跳过, 于是它在别的平台上永远记成缺口; 而"导不进来"完全可以用
    替身复现 —— 换成替身之后三平台都能量到。
    """

    def refuse(_name: str, _package: str | None = None) -> object:
        raise ImportError("没有 winreg")

    # 只换掉本模块看到的那份 importlib: 全局那个真模块动不得(别的导入会一起坏)。
    monkeypatch.setattr(scan_mod, "importlib", SimpleNamespace(import_module=refuse))

    assert WinRegistry._module() is None
