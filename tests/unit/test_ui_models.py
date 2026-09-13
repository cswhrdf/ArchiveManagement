"""UI 展示模型纯逻辑单元测试."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from archive_management.ui.models import (
    BackupItem,
    FeedbackKind,
    GameDetail,
    GameSummary,
    LocationItem,
    SourceFilter,
    ViewKind,
    branch_order,
    can_backup,
    filter_by_source,
    filter_by_source_label,
    group_by_parent,
    timeline_order,
    visible_in_branch_view,
)

pytestmark = [
    pytest.mark.ui,
    pytest.mark.critical,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("展示模型"),
    pytest.mark.story("备份列表展示"),
    pytest.mark.layer("unit"),
]


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


def _detail(*, name: str, original_name: str = "", folder: str = "") -> GameDetail:
    return GameDetail(
        name=name,
        subtitle="",
        main_location="",
        location_verified=False,
        location_note="",
        last_backup_label="—",
        last_backup_sub="",
        total_backups_label="0 个节点",
        total_backups_sub="",
        next_backup_label="—",
        original_name=original_name,
        storage_folder=folder,
    )


def test_detail_origin_label_lists_original_name_and_folder() -> None:
    """改名后额外展示原始名称, 并始终展示磁盘上的备份目录."""
    detail = _detail(name="新名字", original_name="旧名字", folder="旧名字-1a2b3c4d")

    assert detail.origin_label == "原始名称: 旧名字 · 备份目录: 旧名字-1a2b3c4d"


def test_detail_origin_label_skips_unchanged_name() -> None:
    detail = _detail(name="Demo", original_name="Demo", folder="Demo-1a2b3c4d")

    assert detail.origin_label == "备份目录: Demo-1a2b3c4d"


def test_detail_origin_label_empty_before_first_backup() -> None:
    assert _detail(name="Demo", original_name="Demo").origin_label == ""


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


def _safety(backup_id: str, day: int, parent_id: str | None = None) -> BackupItem:
    return replace(
        _item(backup_id, _dt(day=day, hour=9, minute=0)),
        safety=True,
        parent_id=parent_id,
    )


def test_safety_point_stays_out_of_branch_view_by_default() -> None:
    """恢复前安全点只出现在时间线, 分支树里不展示."""
    items = [
        _item("root", _dt(day=1, hour=9, minute=0)),
        _safety("safety", 2, parent_id="root"),
    ]

    assert [item.backup_id for item in branch_order(items)] == ["root"]
    assert visible_in_branch_view(items) == {"root"}
    # 时间线展示全部节点.
    assert len(timeline_order(items)) == 2
    # 只有显式筛选安全点时才进入分支树.
    assert [item.backup_id for item in branch_order(items, include_safety=True)] == [
        "root",
        "safety",
    ]


def test_safety_point_kind_label_differs_from_manual() -> None:
    stamp = _dt(day=2, hour=9, minute=0)
    assert _safety("safety", 2).kind_label != _item("manual", stamp).kind_label


def test_filter_by_source_covers_all_kinds() -> None:
    items = [
        _item("manual", _dt(day=1, hour=9, minute=0)),
        _auto("auto-1", 2),
        _safety("safety", 3),
    ]

    assert len(filter_by_source(items, SourceFilter.ALL)) == 3
    # 手动来源包含安全点(它也走手动保存链路).
    assert {
        item.backup_id for item in filter_by_source(items, SourceFilter.MANUAL)
    } == {
        "manual",
        "safety",
    }
    assert [item.backup_id for item in filter_by_source(items, SourceFilter.AUTO)] == [
        "auto-1"
    ]
    assert [
        item.backup_id for item in filter_by_source(items, SourceFilter.SAFETY)
    ] == ["safety"]


def test_filter_by_source_label_matches_dropdown_text() -> None:
    items = [
        _item("manual", _dt(day=1, hour=9, minute=0)),
        _safety("safety", 3),
    ]

    assert len(filter_by_source_label(items, SourceFilter.ALL.label)) == 2
    assert [
        item.backup_id
        for item in filter_by_source_label(items, SourceFilter.SAFETY.label)
    ] == ["safety"]
    # 未知文案(语言切换/旧配置)按不过滤处理, 避免列表变空.
    assert len(filter_by_source_label(items, "未知筛选")) == 2


def test_every_source_filter_label_is_translated() -> None:
    """回归: 下拉框展示的就是标签文案, 缺 key 会在界面上露出 filter.xxx."""
    for source in SourceFilter:
        label = source.label
        assert label != f"filter.{source.value}"
        assert not label.startswith("filter.")


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
