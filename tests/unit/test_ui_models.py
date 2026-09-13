"""UI 展示模型纯逻辑单元测试."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import pytest

from archive_management.application.home import build_report
from archive_management.domain import (
    DEFAULT_PAGE_SIZE,
    GameFacts,
    HomeFilter,
    HomeLayout,
    HomeView,
)
from archive_management.i18n import tr
from archive_management.ui.models import (
    AppPage,
    BackupItem,
    CandidateFilter,
    CandidateItem,
    DiscoveryPage,
    FeedbackKind,
    GameDetail,
    GameSummary,
    HomeBoard,
    HomeGameItem,
    HomeSection,
    LocationItem,
    MonitoredDirItem,
    ScanSummary,
    SourceFilter,
    ViewKind,
    branch_order,
    can_backup,
    filter_by_source,
    filter_by_source_label,
    group_by_parent,
    home_board,
    poster_columns,
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


def test_every_discovery_filter_and_page_label_is_translated() -> None:
    """回归: 发现窗口的筛选与页签文案缺 key 会直接露出 discovery.xxx."""
    for item in CandidateFilter:
        assert not item.label.startswith("discovery.")
    for page in DiscoveryPage:
        assert not page.label.startswith("discovery.")


def test_discovery_page_order_defaults_to_candidates() -> None:
    """页签顺序就是枚举成员顺序: 第一个(探测结果)是默认页面."""
    assert [page.value for page in DiscoveryPage] == ["candidates", "monitored"]
    assert DiscoveryPage.CANDIDATES.label == tr("discovery.page_candidates")
    assert DiscoveryPage.MONITORED.label == tr("discovery.page_monitored")


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


# --------------------------------------------------------- 本地游戏探测(E-1)


def _candidate() -> CandidateItem:
    """构造一条展示用候选(默认: Steam 探测、待处理、路径可用)."""
    return CandidateItem(
        candidate_id="1",
        name="Hades",
        install_dir=r"D:\Steam\Hades",
        source="steam",
        confidence="high",
        status="new",
        health="ok",
    )


def test_candidate_labels_come_from_i18n() -> None:
    item = _candidate()

    assert item.source_label == tr("discovery.source_steam")
    assert item.confidence_label == tr("discovery.confidence_high")
    assert item.status_label == tr("discovery.status_new")
    assert item.health_label == tr("discovery.health_ok")
    assert item.importable is True
    assert item.summary.startswith(tr("discovery.source_steam"))


def test_candidate_is_not_importable_when_path_unusable_or_handled() -> None:
    assert replace(_candidate(), health="missing").importable is False
    assert replace(_candidate(), status="ignored").importable is False
    assert replace(_candidate(), status="imported", game_id="7").importable is False


def test_candidate_filter_labels_and_values() -> None:
    assert [item.value for item in CandidateFilter] == [
        "all",
        "new",
        "imported",
        "ignored",
    ]
    assert CandidateFilter.NEW.label == tr("discovery.filter_new")


def test_monitored_dir_item_summary_reports_state_and_scan_time() -> None:
    item = MonitoredDirItem(
        directory_id="1",
        path=r"D:\Games",
        enabled=True,
        note="自定义",
        health="ok",
        last_scan_label="2026/09/13 09:20",
    )

    assert item.state_label == tr("discovery.dir_on")
    assert item.health_label == tr("discovery.health_ok")
    assert r"D:\Games" not in item.summary  # 路径单独展示, 摘要里不重复
    assert "自定义" in item.summary
    assert item.summary.endswith(
        tr("discovery.dir_last_scan", stamp="2026/09/13 09:20")
    )
    assert MonitoredDirItem(
        directory_id="2",
        path=r"E:\Gone",
        enabled=False,
        note="",
        health="missing",
    ).state_label == tr("discovery.dir_off")


def test_scan_summary_label_and_detail() -> None:
    summary = ScanSummary(
        monitored=3,
        active=2,
        total=8,
        added=5,
        updated=3,
        linked=1,
        unusable=2,
    )

    assert tr("discovery.scan_done", added=5, total=8) == summary.label
    assert tr("discovery.scan_monitored", count=3, active=2) in summary.detail
    assert tr("discovery.scan_unusable", count=2) in summary.detail
    assert tr("discovery.scan_linked", count=1) in summary.detail


def test_scan_summary_detail_omits_empty_parts_and_reports_errors() -> None:
    clean = ScanSummary(
        monitored=0, active=0, total=0, added=0, updated=0, linked=0, unusable=0
    )
    assert tr("discovery.scan_linked", count=1) not in clean.detail
    assert tr("discovery.scan_errors", count=1) not in clean.detail

    broken = ScanSummary(
        monitored=1,
        active=1,
        total=0,
        added=0,
        updated=0,
        linked=0,
        unusable=0,
        errors=("注册表不可用",),
    )
    assert tr("discovery.scan_errors", count=1) in broken.detail


# --------------------------------------------------------- 统一游戏主页(E-2)

_HOME_NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _home_facts(
    game_id: str,
    *,
    name: str = "",
    origin: str = "manual",
    locations: int = 1,
    backups: int = 1,
    risk: bool = False,
    monitored: bool = False,
    archived: bool = False,
    tags: tuple[str, ...] = (),
) -> GameFacts:
    """构造主页事实: 默认是"刚备份过、路径正常"的游戏."""
    return GameFacts(
        game_id=game_id,
        name=name or game_id,
        origin=origin,
        location_count=locations,
        backup_count=backups,
        last_backup_at=_HOME_NOW if backups else None,
        last_activity_at=_HOME_NOW,
        risk=risk,
        monitored=monitored,
        archived=archived,
        tags=tags,
    )


def _home_board(facts: list[GameFacts], active: HomeFilter | None = None) -> HomeBoard:
    """按给定事实生成主页展示模型(与两个后端共用同一条映射路径)."""
    report = build_report(facts, active or HomeFilter(), now=_HOME_NOW)
    return home_board(report, stamp=lambda moment: moment.strftime("%m-%d %H:%M"))


def test_home_game_item_labels_and_summary() -> None:
    item = HomeGameItem(
        game_id="1",
        name="星际拓荒",
        origin="steam",
        location_count=2,
        backup_count=3,
        last_backup_label="09-12 08:00",
        activity_label="09-13 09:00",
        risk=False,
        monitored=True,
        archived=False,
        tags=("探索",),
    )

    assert item.platform_label == tr("discovery.source_steam")
    assert item.backup_label == tr("home.cat_backup_done")
    assert item.risk_label == tr("home.risk_ok")
    assert item.state_label == ""
    assert item.meta == tr("home.meta", locations=2, backups=3)
    assert tr("home.last_backup", stamp="09-12 08:00") in item.summary
    assert tr("home.activity", stamp="09-13 09:00") in item.summary
    assert item.backup_enabled is True
    chips = item.chips
    assert tr("discovery.source_steam") in chips
    assert tr("home.cat_monitor_on") in chips
    assert "探索" in chips
    assert tr("home.chip_risk") not in chips


def test_home_game_item_marks_risk_archive_and_missing_backup() -> None:
    item = HomeGameItem(
        game_id="2",
        name="空游戏",
        origin="manual",
        location_count=0,
        backup_count=0,
        last_backup_label="",
        activity_label="",
        risk=True,
        monitored=False,
        archived=True,
    )

    assert item.backup_label == tr("home.cat_backup_none")
    assert item.risk_label == tr("home.risk_bad")
    assert item.state_label == tr("home.chip_archived")
    assert tr("home.last_backup_none") in item.summary
    assert tr("home.activity", stamp="") not in item.summary
    assert item.backup_enabled is False
    assert tr("home.chip_risk") in item.chips
    assert tr("home.chip_archived") in item.chips


def test_home_board_summary_detail_and_options() -> None:
    board = _home_board(
        [
            _home_facts("1", name="A"),
            _home_facts("2", name="B", locations=0),
        ]
    )

    assert board.summary == tr("home.summary", total=2, recent=2, pending=1, archived=0)
    assert board.detail == tr("home.detail", backed_up=2, monitored=0, risky=0)
    assert [option.key for option in board.views] == [view.value for view in HomeView]
    assert board.view_text == f"{tr('home.view_all')} (2)"
    assert board.origin_text == tr("home.origin_all")
    assert board.category_text == tr("home.category_all")
    assert board.narrowing is False


def test_home_board_option_texts_carry_counts() -> None:
    board = _home_board(
        [_home_facts("1", origin="steam")],
        HomeFilter(origin="steam", category="backup:done"),
    )

    assert board.origin_text == f"{tr('discovery.source_steam')} (1)"
    assert board.category_text == f"{tr('home.cat_backup_done')} (1)"
    assert board.narrowing is True


def test_home_board_uses_fallback_text_for_unavailable_selection() -> None:
    """选中项在当前结果里没有出现时(例如组合筛选后为空)仍要显示可读文案."""
    board = _home_board([_home_facts("1", origin="steam")], HomeFilter(origin="gog"))

    assert board.games == ()
    assert board.origin_text == tr("discovery.source_gog")


def test_home_board_empty_states_distinguish_library_view_and_filter() -> None:
    empty_library = _home_board([])
    assert empty_library.empty_message == tr("home.empty_library")
    assert empty_library.empty_hint == tr("home.empty_library_hint")

    facts = [_home_facts("1", name="A")]
    filtered = _home_board(facts, HomeFilter(search="zzz"))
    assert filtered.empty_message == tr("home.empty_filtered", total=1)
    assert filtered.empty_hint == tr("home.empty_filtered_hint")

    only_archived = _home_board([_home_facts("1", archived=True)])
    assert only_archived.empty_message == tr("home.empty_view", view=HomeView.ALL.label)
    assert only_archived.empty_hint == tr("home.empty_hint")


def test_home_filter_display_preferences_are_validated() -> None:
    """展示偏好(列表/海报与每页条数)也要做取值校验."""
    assert HomeFilter().layout is HomeLayout.LIST
    assert HomeFilter().page_size == DEFAULT_PAGE_SIZE

    assert HomeLayout.LIST.label == tr("home.layout_list")
    assert HomeLayout.POSTER.label == tr("home.layout_poster")

    # 非法的每页条数回落到默认值, 合法取值原样保留.
    assert HomeFilter(page_size=7).normalized().page_size == DEFAULT_PAGE_SIZE
    assert HomeFilter(page_size=60).normalized().page_size == 60


def test_home_labels_are_all_translated() -> None:
    """回归: 缺 key 会让界面直接露出 home.xxx / dialog.xxx 这样的原始键名."""
    keys = (
        "home.title",
        "home.hint",
        "home.search",
        "home.search_placeholder",
        "home.clear",
        "home.origin_all",
        "home.category_all",
        "home.list_hint",
        "home.require_game",
        "home.risk_ok",
        "home.risk_bad",
        "home.chip_risk",
        "home.chip_archived",
        "home.col_name",
        "home.col_platform",
        "home.col_locations",
        "home.col_backups",
        "home.col_last_backup",
        "home.col_activity",
        "home.col_state",
        "home.action_detail",
        "home.action_backup",
        "home.action_location",
        "home.action_manage",
        "home.action_tags",
        "home.action_archive",
        "home.action_unarchive",
        "page.back_home",
        "page.library",
        "page.discovery",
        "topbar.add_game",
        "topbar.nav_scheduled",
        "topbar.nav_settings",
        "topbar.busy",
        "home.layout_list",
        "home.layout_poster",
        "home.page_size",
        "home.poster_backups",
        "home.page_indicator",
        "home.prev_page",
        "home.next_page",
        "home.activity_none",
        "dialog.home_tags_title",
        "dialog.home_location_title",
        "error.home_location_required",
    )
    for key in keys:
        assert tr(key) != key
    for view in HomeView:
        assert not view.label.startswith("home.")
    for key in ("backup_done", "backup_none", "monitor_on", "monitor_off"):
        assert tr(f"home.cat_{key}") != f"home.cat_{key}"


def test_app_page_defaults_to_home_first() -> None:
    """主窗口页面: 成员顺序即默认页面, 第一位是游戏主页(软件打开后的首页)."""
    assert [page.value for page in AppPage] == ["home", "detail"]
    assert AppPage.HOME is next(iter(AppPage))


def test_home_section_order_and_labels() -> None:
    """主页内部分区: 游戏库在前(默认), 游戏发现作为第二个分区."""
    assert [section.value for section in HomeSection] == ["library", "discovery"]
    assert HomeSection.LIBRARY is next(iter(HomeSection))
    for section in HomeSection:
        assert section.label == tr(f"page.{section.value}")
        assert not section.label.startswith("page.")


def test_poster_columns_scales_with_width() -> None:
    """海报模式每行张数随可用宽度变化(收窄时至少两列)."""
    assert poster_columns(400) == 2
    assert poster_columns(1000) == 4
    assert poster_columns(1600) == 7
    assert poster_columns(1400) > poster_columns(1000)
