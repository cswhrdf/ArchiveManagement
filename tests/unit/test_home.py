"""统一游戏主页分类与筛选规则的单元测试.

主页的核心是"把游戏事实换算成视图、分类与排序": 这里覆盖分类标签、标签清理、
待处理判定、活跃/长期未更新窗口、归档可见性、平台与分类筛选、搜索、排序以及
各筛选项的计数口径。全部是纯函数, 不依赖数据库与界面。
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from archive_management.domain.home import (
    HOME_STATE_VERSION,
    MAX_TAG_LENGTH,
    MAX_TAGS,
    RECENT_DAYS,
    STALE_DAYS,
    TAG_SEPARATORS,
    CategoryKind,
    GameCategory,
    GameFacts,
    HomeFilter,
    HomeView,
    category_counts,
    filter_games,
    home_sort_key,
    home_stats,
    normalize_tags,
    origin_counts,
    parse_category,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("统一游戏主页"),
    pytest.mark.story("分类与筛选规则"),
    pytest.mark.layer("unit"),
]

_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _facts(
    game_id: str,
    *,
    name: str = "",
    origin: str = "manual",
    locations: int = 1,
    backups: int = 1,
    last_backup: datetime | None = None,
    activity: datetime | None = None,
    risk: bool = False,
    archived: bool = False,
    tags: tuple[str, ...] = (),
) -> GameFacts:
    """构造一个事实对象, 未指定的时间统一取"刚刚备份过"(避免误判为长期未更新)."""
    return GameFacts(
        game_id=game_id,
        name=name or game_id,
        origin=origin,
        location_count=locations,
        backup_count=backups,
        last_backup_at=(
            _NOW - timedelta(days=1) if backups and last_backup is None else last_backup
        ),
        last_activity_at=activity if activity is not None else _NOW,
        risk=risk,
        archived=archived,
        tags=tags,
    )


def test_every_view_has_a_translated_label() -> None:
    for view in HomeView:
        label = view.label
        assert label
        assert label != f"home.view_{view.value}"


def test_view_membership_order_is_stable() -> None:
    assert [view.value for view in HomeView] == ["all", "recent", "pending", "archived"]


def test_category_value_round_trip() -> None:
    category = GameCategory(kind=CategoryKind.BACKUP, key="done")
    assert category.value == "backup:done"
    assert parse_category(category.value) == category


def test_category_parse_rejects_invalid_values() -> None:
    assert parse_category("") is None
    assert parse_category("backup:") is None
    assert parse_category("nope:done") is None
    assert parse_category("done") is None


def test_category_labels_are_translated_except_tags() -> None:
    for kind, key in (
        (CategoryKind.ORIGIN, "steam"),
        (CategoryKind.BACKUP, "done"),
        (CategoryKind.ACTIVITY, "stale"),
        (CategoryKind.RISK, "yes"),
    ):
        label = GameCategory(kind=kind, key=key).label
        assert label != f"home.cat_{kind.value}_{key}"
    assert GameCategory(kind=CategoryKind.TAG, key="动作").label == "动作"


def test_normalize_tags_cleans_and_limits() -> None:
    tags = normalize_tags(["  动作 ", "动作", "有,逗号", "", "x" * 40, "6", "7", "8"])
    assert tags[0] == "动作"
    assert "有逗号" in tags
    assert len(tags) == MAX_TAGS
    assert len(tags[2]) == MAX_TAG_LENGTH
    assert len(set(tags)) == len(tags)


def test_normalize_tags_strips_both_comma_forms() -> None:
    """中英文逗号一视同仁: 全角逗号同样被剔除, 不会留在标签里."""
    tags = normalize_tags(["动，作", "策,略", "   ，  "])  # noqa: RUF001 - 全角逗号正是被测输入

    assert tags == ("动作", "策略")
    assert all(not set(tag) & set(TAG_SEPARATORS) for tag in tags)


def test_pending_only_covers_missing_or_broken_locations() -> None:
    """待处理 = 还没配置存档位置或路径已失效; 从未备份但位置可用的不算待处理."""
    assert _facts("a", locations=0).pending is True
    assert _facts("b", risk=True).pending is True
    # 配置了可用存档位置但从未备份: 已经可以正常备份, 归到"未备份"分类而不是待处理.
    never_backed_up = _facts("c", backups=0, last_backup=None)
    assert never_backed_up.pending is False
    assert GameCategory(
        kind=CategoryKind.BACKUP, key="none"
    ) in never_backed_up.categories(_NOW)
    assert _facts("d").pending is False


def test_recent_and_stale_windows() -> None:
    fresh = _facts("a", activity=_NOW - timedelta(days=RECENT_DAYS - 1))
    old = _facts(
        "b",
        backups=0,
        last_backup=None,
        activity=_NOW - timedelta(days=RECENT_DAYS + 1),
    )
    assert fresh.is_recent(_NOW) is True
    assert old.is_recent(_NOW) is False
    assert fresh.is_stale(_NOW) is False
    stale = _facts(
        "c", last_backup=_NOW - timedelta(days=STALE_DAYS + 1), activity=_NOW
    )
    assert stale.is_stale(_NOW) is True
    never = _facts("d", backups=0, last_backup=None)
    assert never.is_stale(_NOW) is True


def test_game_facts_without_any_time_is_not_recent() -> None:
    bare = GameFacts(game_id="a", name="A")
    assert bare.activity_at is None
    assert bare.is_recent(_NOW) is False


def test_archived_games_only_appear_in_archived_view() -> None:
    archived = _facts("a", archived=True)
    assert archived.in_view(HomeView.ARCHIVED, now=_NOW) is True
    assert archived.in_view(HomeView.ALL, now=_NOW) is False
    assert archived.in_view(HomeView.RECENT, now=_NOW) is False
    assert archived.in_view(HomeView.PENDING, now=_NOW) is False


def test_filter_matches_platform_category_and_search() -> None:
    game = _facts("a", name="Outer Wilds", origin="steam", tags=("探索",))
    assert HomeFilter().matches(game, now=_NOW) is True
    assert HomeFilter(origin="steam").matches(game, now=_NOW) is True
    assert HomeFilter(origin="gog").matches(game, now=_NOW) is False
    assert HomeFilter(category="origin:steam").matches(game, now=_NOW) is True
    assert HomeFilter(category="tag:探索").matches(game, now=_NOW) is True
    assert HomeFilter(category="backup:none").matches(game, now=_NOW) is False
    assert HomeFilter(search="outer").matches(game, now=_NOW) is True
    assert HomeFilter(search="hollow").matches(game, now=_NOW) is False


def test_normalized_filter_falls_back_for_bad_values() -> None:
    dirty = HomeFilter(
        origin="  steam  ",
        category="nope:value",
        search="  wilds  ",
        version=99,
    )
    clean = dirty.normalized()
    assert clean.origin == "steam"
    assert clean.category == ""
    assert clean.category_item is None
    assert clean.search == "wilds"
    assert clean.version == HOME_STATE_VERSION


def test_filter_games_orders_pending_first() -> None:
    games = [
        _facts("done", backups=3, activity=_NOW - timedelta(days=3)),
        _facts("needs", locations=0),
        _facts("never", backups=0, last_backup=None),
    ]
    ordered = [game.game_id for game in filter_games(games, HomeFilter(), now=_NOW)]
    # 只有"还没有存档位置"的算待处理, 它排在最前; 其余按最近活动倒序.
    assert ordered == ["needs", "never", "done"]


def test_archived_games_only_show_in_the_archived_view() -> None:
    games = [_facts("a", backups=3), _facts("z", backups=3, archived=True)]
    assert [game.game_id for game in filter_games(games, HomeFilter(), now=_NOW)] == [
        "a"
    ]
    archived = filter_games(games, HomeFilter(view=HomeView.ARCHIVED), now=_NOW)
    assert [game.game_id for game in archived] == ["z"]


def test_home_sort_key_is_stable_for_same_activity() -> None:
    first = _facts("a", name="Alpha", activity=_NOW)
    second = _facts("b", name="beta", activity=_NOW)
    assert home_sort_key(first) < home_sort_key(second)


def test_home_stats_skips_archived_games() -> None:
    games = [
        _facts("a", backups=2),
        _facts("b", locations=0),
        _facts("c", risk=True, backups=0, last_backup=None),
        _facts("d", archived=True),
    ]
    stats = home_stats(games, now=_NOW)
    assert stats.total == 3
    assert stats.archived == 1
    assert stats.pending == 2
    assert stats.backed_up == 2
    assert stats.risky == 1
    assert stats.count_for(HomeView.ALL) == 3
    assert stats.count_for(HomeView.ARCHIVED) == 1


def test_category_counts_ignores_the_category_filter_itself() -> None:
    games = [
        _facts("a", origin="steam", tags=("探索",)),
        _facts("b", origin="gog"),
    ]
    active = HomeFilter(category="origin:steam")
    counts = dict(category_counts(games, active, now=_NOW))
    # 分类筛选被忽略: 两个游戏的平台都参与统计, 否则用户无法切换到其它分类.
    assert counts[GameCategory(kind=CategoryKind.ORIGIN, key="gog")] == 1
    assert counts[GameCategory(kind=CategoryKind.TAG, key="探索")] == 1


def test_category_counts_respects_view_and_search() -> None:
    games = [_facts("a", name="Alpha"), _facts("b", name="Beta", archived=True)]
    counts = dict(category_counts(games, HomeFilter(view=HomeView.ALL), now=_NOW))
    # 归档的游戏不进统计: 只剩一款未归档游戏的分类。
    assert counts[GameCategory(kind=CategoryKind.BACKUP, key="done")] == 1
    assert counts[GameCategory(kind=CategoryKind.RISK, key="no")] == 1


def test_origin_counts_ignores_the_origin_filter_itself() -> None:
    games = [_facts("a", origin="steam"), _facts("b", origin="gog")]
    origins = dict(origin_counts(games, HomeFilter(origin="steam"), now=_NOW))
    assert origins == {"steam": 1, "gog": 1}


def test_origin_counts_honours_search() -> None:
    games = [_facts("a", name="Alpha", origin="steam"), _facts("b", origin="gog")]
    origins = dict(origin_counts(games, HomeFilter(search="alpha"), now=_NOW))
    assert origins == {"steam": 1}


def test_filter_games_returns_empty_for_unmatched_search() -> None:
    games = [_facts("a", name="Alpha")]
    assert filter_games(games, HomeFilter(search="zzz"), now=_NOW) == []


def test_facts_replace_keeps_rules_working() -> None:
    game = _facts("a", backups=2)
    archived = replace(game, archived=True)
    assert filter_games([archived], HomeFilter(view=HomeView.ALL), now=_NOW) == []
