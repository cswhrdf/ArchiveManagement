"""UI 展示模型纯逻辑单元测试."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from archive_management.ui.models import (
    BackupItem,
    FeedbackKind,
    GameSummary,
    LocationItem,
    ViewKind,
    branch_order,
    can_backup,
    group_by_parent,
    timeline_order,
)

pytestmark = [pytest.mark.ui, pytest.mark.critical]


def _dt(*, day: int, hour: int, minute: int) -> datetime:
    return datetime(2026, 9, day, hour, minute, tzinfo=UTC)


def _item(backup_id: str, created_dt: datetime) -> BackupItem:
    return BackupItem(
        backup_id=backup_id,
        title=backup_id,
        created_dt=created_dt,
        created_label="今天",
        auto=False,
        branch_label="主线",
        size_label="1 MB",
        verified=True,
    )


def test_game_list_detail_with_locations() -> None:
    game = GameSummary(
        game_id="g", name="Demo", has_locations=True, location_count=3, backup_count=5
    )
    assert game.list_detail == "3 个存档位置 · 5 个备份"


def test_game_list_detail_without_locations() -> None:
    game = GameSummary(
        game_id="g", name="Demo", has_locations=False, location_count=0, backup_count=0
    )
    assert game.list_detail == "未配置存档位置"


def test_can_backup_only_with_locations() -> None:
    with_location = GameSummary(
        game_id="a", name="A", has_locations=True, location_count=1, backup_count=1
    )
    no_location = GameSummary(
        game_id="b", name="B", has_locations=False, location_count=0, backup_count=0
    )
    assert can_backup(with_location) is True
    assert can_backup(no_location) is False
    assert can_backup(None) is False


def test_timeline_order_sorts_descending() -> None:
    items = [
        _item("old", _dt(day=4, hour=8, minute=0)),
        _item("new", _dt(day=6, hour=9, minute=40)),
        _item("mid", _dt(day=5, hour=12, minute=0)),
    ]
    ordered = timeline_order(items)
    assert [item.backup_id for item in ordered] == ["new", "mid", "old"]


def test_timeline_order_stable_for_equal_timestamps() -> None:
    stamp = _dt(day=6, hour=9, minute=0)
    items = [
        _item("a", stamp),
        _item("b", stamp),
        _item("c", stamp),
    ]
    ordered = timeline_order(items)
    assert len(ordered) == 3


def test_timeline_order_resets_branch_depth() -> None:
    stamp = _dt(day=6, hour=9, minute=0)
    item = replace(_item("a", stamp), depth=2, parent_id="root")
    assert timeline_order([item])[0].depth == 0


def test_branch_order_follows_parent_links() -> None:
    """分支视图按父子关系排序, 并把层级写入 depth."""
    items = [
        replace(_item("root", _dt(day=4, hour=8, minute=0))),
        replace(_item("child", _dt(day=5, hour=9, minute=0)), parent_id="root"),
        replace(
            _item("grand", _dt(day=6, hour=9, minute=0)),
            parent_id="child",
            is_branch=True,
        ),
    ]
    ordered = branch_order(items)
    assert [item.backup_id for item in ordered] == ["root", "child", "grand"]
    assert [item.depth for item in ordered] == [0, 1, 2]
    assert ordered[-1].is_branch is True


def test_branch_order_keeps_orphans_visible() -> None:
    """父节点缺失时按根节点处理, 不丢节点."""
    items = [
        replace(_item("orphan", _dt(day=6, hour=9, minute=0)), parent_id="gone"),
        _item("root", _dt(day=4, hour=8, minute=0)),
    ]
    ordered = branch_order(items)
    assert {item.backup_id for item in ordered} == {"orphan", "root"}
    assert all(item.depth == 0 for item in ordered)


def test_group_by_parent_matches_branch_order() -> None:
    items = [
        _item("old", _dt(day=4, hour=8, minute=0)),
        replace(_item("new", _dt(day=6, hour=9, minute=40)), parent_id="old"),
    ]
    assert group_by_parent(items) == branch_order(items)


def _auto(backup_id: str, day: int, parent_id: str | None = None) -> BackupItem:
    return replace(
        _item(backup_id, _dt(day=day, hour=9, minute=0)),
        auto=True,
        parent_id=parent_id,
    )


def test_branch_view_shows_only_newest_auto_backup() -> None:
    """自动备份是特殊备份: 分支树里只展示最新的一份."""
    items = [
        _item("root", _dt(day=1, hour=9, minute=0)),
        _auto("auto-1", 2, parent_id="root"),
        _auto("auto-2", 3, parent_id="auto-1"),
        _auto("auto-3", 4, parent_id="auto-2"),
    ]
    ordered = branch_order(items)
    assert [item.backup_id for item in ordered] == ["root", "auto-3"]
    assert [item.depth for item in ordered] == [0, 1]
    # 时间线仍然展示全部自动备份.
    assert len(timeline_order(items)) == 4


def test_branch_view_keeps_all_manual_backups() -> None:
    items = [
        _item("a", _dt(day=1, hour=9, minute=0)),
        _item("b", _dt(day=2, hour=9, minute=0)),
        _item("c", _dt(day=3, hour=9, minute=0)),
    ]
    assert len(branch_order(items)) == 3


def test_timeline_labels_inherit_branch_name() -> None:
    """时间线中的每个备份都显示自己所属的分支."""
    items = [
        replace(_item("root", _dt(day=1, hour=9, minute=0))),
        replace(
            _item("branch", _dt(day=2, hour=9, minute=0)),
            parent_id="root",
            branch_name="黑棘",
            is_branch=True,
        ),
        replace(_item("child", _dt(day=3, hour=9, minute=0)), parent_id="branch"),
    ]
    ordered = {item.backup_id: item for item in timeline_order(items)}
    assert ordered["root"].branch_label == "主线"
    assert "黑棘" in ordered["branch"].branch_label
    assert "黑棘" in ordered["child"].branch_label


def test_ordering_keeps_current_marker() -> None:
    items = [replace(_item("a", _dt(day=1, hour=9, minute=0)), is_current=True)]
    assert branch_order(items)[0].is_current is True
    assert timeline_order(items)[0].is_current is True


def test_view_kind_values() -> None:
    assert ViewKind.TIMELINE.value == "timeline"
    assert ViewKind.BRANCH.value == "branch"


def test_feedback_kind_values() -> None:
    assert FeedbackKind.SUCCESS.value == "success"
    assert FeedbackKind.ERROR.value == "error"


def test_game_list_detail_when_disabled() -> None:
    from archive_management.i18n import tr

    game = GameSummary(
        game_id="g",
        name="Demo",
        has_locations=True,
        location_count=2,
        backup_count=3,
        enabled=False,
    )
    assert game.list_detail == tr("game.disabled_short")


def test_location_item_fields() -> None:
    item = LocationItem(
        location_id="1",
        game_id="2",
        path=r"C:\Games\save",
        path_kind="directory",
        source="manual",
        is_primary=True,
        ok=True,
        note="已校验",
    )
    assert item.location_id == "1"
    assert item.path_kind == "directory"
    assert item.is_primary is True
