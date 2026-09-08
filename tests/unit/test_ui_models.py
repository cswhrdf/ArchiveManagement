"""UI 展示模型纯逻辑单元测试."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from archive_management.ui.models import (
    BackupItem,
    FeedbackKind,
    GameSummary,
    LocationItem,
    ViewKind,
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


def test_group_by_parent_orders_descending() -> None:
    items = [
        _item("old", _dt(day=4, hour=8, minute=0)),
        _item("new", _dt(day=6, hour=9, minute=40)),
    ]
    ordered = group_by_parent(items)
    assert [item.backup_id for item in ordered] == ["new", "old"]


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
