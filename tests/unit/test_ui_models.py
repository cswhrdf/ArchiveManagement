"""UI 展示模型纯逻辑单元测试."""

from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from archive_management.application.home import build_report
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
    BatchGameInspection,
    BatchInspection,
    ImportInspection,
    PackageLocation,
)
from archive_management.domain import (
    DEFAULT_PAGE_SIZE,
    GameFacts,
    HomeFilter,
    HomeLayout,
    HomeView,
    SavePathCandidate,
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
    ImportChoice,
    LocationItem,
    MonitoredDirItem,
    SavePathSuggestion,
    ScanSummary,
    SourceFilter,
    ViewKind,
    batch_export_choice,
    batch_export_filename,
    batch_import_prompt,
    batch_row_choice,
    branch_order,
    can_backup,
    export_batch_prompt,
    exportable_games,
    filter_by_source,
    filter_by_source_label,
    filter_export_options,
    group_by_parent,
    home_board,
    import_prompt,
    import_strategies,
    poster_columns,
    size_label,
    target_game_id,
    target_label_for,
    timeline_order,
    visible_in_branch_view,
)

pytestmark = [
    pytest.mark.ui,
    pytest.mark.normal,
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


# 存档路径建议的样本路径: "安全"那条必须**在两个平台上都是绝对路径**且不落在主目录/盘符
# 根里。POSIX 上 "C:/Saves" 其实是相对路径, 会被判成"非绝对路径"→危险 —— 那条只在
# Windows 上具备"普通存档目录"的语义, 因此按平台各挑一条(CI 的 Linux/macOS 就踩过)。
_SAFE_PATH = "C:/Saves" if os.name == "nt" else "/opt/Saves"


def test_save_path_suggestion_keeps_the_path_and_the_danger_note() -> None:
    """存档路径建议: 路径都来自可信渠道, 因此不再标可信度, 只标危险目标."""
    safe = SavePathSuggestion.from_candidate(
        SavePathCandidate(
            path=_SAFE_PATH,
            path_kind="directory",
            confidence="high",
            reason_code="steam_remotecache",
        )
    )
    assert safe.risk_label == ""
    assert safe.text == _SAFE_PATH

    risky = SavePathSuggestion.from_candidate(
        SavePathCandidate(
            path=str(Path.home()),
            path_kind="directory",
            confidence="low",
            reason_code="steam_remotecache",
        )
    )
    assert risky.dangerous is True
    assert risky.text == f"{Path.home()} · {tr('discovery.save_risk')}"


def test_candidate_item_display_name_falls_back_to_the_detected_name() -> None:
    """有当前语言译名就用译名, 没有就回落探测到的名称."""
    item = CandidateItem(
        candidate_id="1",
        name="Stardew Valley",
        install_dir="D:/Steam/Stardew Valley",
        source="steam",
        confidence="high",
        status="new",
        health="ok",
    )

    assert item.display_name == "Stardew Valley"
    assert replace(item, localized_name="星露谷物语").display_name == "星露谷物语"


def test_candidate_item_save_label_has_three_states() -> None:
    """探测结果行的存档区三态: 平台不支持 / 没推出来 / 推出来 N 条."""
    unsupported = CandidateItem(
        candidate_id="1",
        name="Demo",
        install_dir="D:/Demo",
        source="steam",
        confidence="high",
        status="new",
        health="ok",
        save_supported=False,
    )
    assert unsupported.save_label == tr(
        "discovery.save_unsupported", platform=unsupported.source_label
    )

    supported = replace(unsupported, save_supported=True)
    assert supported.save_label == tr("discovery.save_none")

    found = replace(
        supported,
        save_paths=(
            SavePathSuggestion(
                path="C:/Saves",
                path_kind="directory",
            ),
        ),
    )
    assert found.save_label == tr("discovery.save_found", count=1)


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
        excluded=4,
    )

    assert tr("discovery.scan_done", added=5, total=8) == summary.label
    assert tr("discovery.scan_monitored", count=3, active=2) in summary.detail
    assert tr("discovery.scan_unusable", count=2) in summary.detail
    assert tr("discovery.scan_excluded", count=4) in summary.detail
    assert tr("discovery.scan_linked", count=1) in summary.detail


def test_scan_summary_detail_omits_empty_parts_and_reports_errors() -> None:
    clean = ScanSummary(
        monitored=0, active=0, total=0, added=0, updated=0, linked=0, unusable=0
    )
    assert tr("discovery.scan_excluded", count=1) not in clean.detail
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
        archived=True,
    )

    # 没有关联存档位置时说的是原因, 而不是笼统的"未备份"。
    assert item.backup_label == tr("home.no_save_paths")
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
    assert board.detail == tr("home.detail", backed_up=2, risky=0)
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
    for key in ("backup_done", "backup_none"):
        assert tr(f"home.cat_{key}") != f"home.cat_{key}"


def test_app_page_defaults_to_home_first() -> None:
    """主窗口页面: 成员顺序即默认页面, 第一位是游戏主页(软件打开后的首页)."""
    assert [page.value for page in AppPage] == ["home", "detail"]
    assert AppPage.HOME is next(iter(AppPage))


def test_home_section_order_and_labels() -> None:
    """主页内部分区: 游戏库在前(默认), 游戏发现与游戏启停依次排在后面."""
    assert [section.value for section in HomeSection] == [
        "library",
        "discovery",
        "activation",
    ]
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


# ---------------------------------------------------------------- 导入提示


def _packaged_game(game_id: str, name: str, *, original_name: str = "") -> GameSummary:
    """一条最小游戏摘要(导入对话框只用得上 id 与名称; 原名默认没有)."""
    return GameSummary(
        game_id=game_id,
        name=name,
        has_locations=True,
        location_count=1,
        backup_count=1,
        original_name=original_name,
    )


def _inspection(*, app_id: int | None = 730) -> ImportInspection:
    """一个最小的包体检结果: 两条存档位置(一条本机已存在)."""
    return ImportInspection(
        path=Path("Demo.archive.zip"),
        game_name="Demo",
        steam_app_id=app_id,
        platform="windows",
        origin="steam",
        tags=("动作",),
        locations=(
            PackageLocation(
                index=0,
                path="D:/saves",
                path_kind="directory",
                source="steam",
                is_primary=True,
                exists_here=True,
            ),
            PackageLocation(
                index=1,
                path="E:/gone",
                path_kind="directory",
                source="manual",
                is_primary=False,
                exists_here=False,
            ),
        ),
        nodes=(),
        schedule=None,
        matching_game_id=None,
    )


def test_import_prompt_prefills_only_paths_that_exist_here() -> None:
    """只有本机真有的路径才预填; 不存在的那条留空(留空就不导入)."""
    prompt = import_prompt(_inspection(), [])

    assert "Demo" in prompt.summary
    assert "AppID 730" in prompt.summary
    assert "备份 0 份" in prompt.summary
    assert prompt.match_text == ""
    assert prompt.targets == ()
    assert [row.index for row in prompt.locations] == [0, 1]
    assert prompt.locations[0].default == "D:/saves"
    assert prompt.locations[0].text == tr(
        "dialog.import_location_exists", path="D:/saves"
    )
    assert prompt.locations[1].default == ""
    assert prompt.locations[1].text == "E:/gone"


def test_import_prompt_without_an_app_id_drops_that_part() -> None:
    """手动录入的游戏没有 AppID: 摘要里就不提这一段."""
    prompt = import_prompt(_inspection(app_id=None), [])

    assert "AppID" not in prompt.summary
    assert "Demo" in prompt.summary


def test_import_prompt_preselects_the_matching_game() -> None:
    """疑似同一款排在最前并预选上, 其余的保持库里的顺序."""
    inspection = replace(_inspection(), matching_game_id=42)
    games = [_packaged_game("7", "别的游戏"), _packaged_game("42", "库里的 Demo")]

    prompt = import_prompt(inspection, games)

    assert [option.game_id for option in prompt.targets] == ["42", "7"]
    assert [option.selected for option in prompt.targets] == [True, False]
    assert prompt.match_text == tr("dialog.import_match", name="库里的 Demo")


def test_import_prompt_without_a_match_keeps_the_library_order() -> None:
    """没有匹配时也预选第一款(用户可以直接改成别的)."""
    games = [_packaged_game("7", "别的游戏"), _packaged_game("9", "山海旅人")]

    prompt = import_prompt(_inspection(), games)

    assert [option.game_id for option in prompt.targets] == ["7", "9"]
    assert [option.selected for option in prompt.targets] == [True, False]
    assert prompt.match_text == ""


def test_import_strategies_offer_merge_only_with_a_target() -> None:
    """没有可合并的游戏就不给"合并", 免得选了却没有目标."""
    with_targets = import_strategies(has_targets=True)
    without = import_strategies(has_targets=False)

    assert [key for key, _text in with_targets] == [
        STRATEGY_NEW,
        STRATEGY_MERGE,
        STRATEGY_SKIP,
    ]
    assert [key for key, _text in without] == [STRATEGY_NEW, STRATEGY_SKIP]
    assert all(text and not text.startswith("dialog.") for _key, text in with_targets)


# ---------------------------------------------------------------- 批量导出


def _batch_export_games() -> list[GameSummary]:
    """三款游戏: 已归档的那款不该出现在批量导出的候选里."""
    return [
        _packaged_game("1", "星际拓荒"),
        replace(_packaged_game("2", "山海旅人"), enabled=False),
        replace(_packaged_game("3", "无尽太空"), archived=True),
    ]


def test_export_batch_prompt_offers_every_unarchived_game_and_selects_none() -> None:
    """候选里没有已归档的游戏(停用的照常给), 且默认一个都不勾(要用户明确表态)."""
    games = _batch_export_games()

    prompt = export_batch_prompt(games)

    assert [option.game_id for option in prompt.options] == ["1", "2"]
    assert [option.selected for option in prompt.options] == [False, False]
    assert [option.original_name for option in prompt.options] == ["", ""]
    assert prompt.options[0].detail == games[0].list_detail
    assert prompt.summary == tr("dialog.export_batch_summary", count=2)
    # 规则只有一处: 单独问"这款能不能批量导出"时给的是同一个答案.
    assert [game.game_id for game in exportable_games(games)] == ["1", "2"]


def test_filter_export_options_narrows_ignores_case_and_can_match_nothing() -> None:
    """筛选只看名称: 空输入给全部, 大小写不敏感, 一条都不匹配就是空."""
    options = export_batch_prompt(
        [_packaged_game("1", "Outer Wilds"), _packaged_game("2", "星际拓荒")]
    ).options

    assert filter_export_options(options, "") == options
    assert filter_export_options(options, "   ") == options
    assert [option.name for option in filter_export_options(options, "OUTER")] == [
        "Outer Wilds"
    ]
    assert [option.name for option in filter_export_options(options, "星")] == [
        "星际拓荒"
    ]
    assert filter_export_options(options, "没有这一款") == ()


def test_filter_export_options_also_matches_the_original_name() -> None:
    """筛选也认**录入时的原名**: 库里的名字会被译名写回或用户改名换掉, 用户却可能
    还拿着当初那个名字来搜(这正是这条改动要解决的问题).
    """
    options = export_batch_prompt(
        [
            _packaged_game("1", "星际拓荒", original_name="Outer Wilds"),
            _packaged_game("2", "山海旅人"),
        ]
    ).options

    assert [option.game_id for option in filter_export_options(options, "outer")] == [
        "1"
    ], "按原名(大小写不敏感)命中"
    assert [option.game_id for option in filter_export_options(options, "WILDS")] == [
        "1"
    ]
    assert [option.game_id for option in filter_export_options(options, "拓荒")] == [
        "1"
    ], "按界面上的名称照旧命中"
    assert [option.game_id for option in filter_export_options(options, "山")] == ["2"]
    assert filter_export_options(options, "Outer Wilds 2") == ()


def test_the_batch_export_row_shows_the_original_name_only_when_it_differs() -> None:
    """行尾说明里带原名, 但只在它跟界面名不同的时候带 —— 否则白占宽度.

    带上它是因为"按原名搜得到"这件事得可解释: 搜出来的那行文字里否则看不出跟输入
    有什么关系(而它确实命中了)。
    """
    renamed = _packaged_game("1", "星际拓荒", original_name="Outer Wilds")
    same = _packaged_game("2", "山海旅人", original_name="山海旅人")
    unknown = _packaged_game("3", "无尽太空")

    options = export_batch_prompt([renamed, same, unknown]).options

    assert options[0].original_name == "Outer Wilds"
    assert tr("hero.original_name", name="Outer Wilds") in options[0].detail
    assert renamed.list_detail in options[0].detail, "位置/备份摘要照旧带上"
    assert options[1].detail == same.list_detail, "原名与界面名相同时不重复显示"
    assert options[2].detail == unknown.list_detail, "没有原名时也不显示"


def test_batch_export_choice_keeps_the_candidate_order() -> None:
    """勾选结果按候选顺序(不是点击顺序)给出, 一个都没勾就是空选择."""
    options = export_batch_prompt(
        [_packaged_game("1", "甲"), _packaged_game("2", "乙")]
    ).options

    chosen = batch_export_choice({"2": True, "1": True}, options)

    assert chosen.game_ids == ("1", "2")
    assert batch_export_choice({"1": False, "2": False}, options).game_ids == ()
    assert batch_export_choice({}, options).game_ids == ()


def test_batch_export_filename_carries_the_count_and_the_stamp() -> None:
    """默认文件名写明游戏数并带时间戳: 同一个库导出两次不会互相覆盖."""
    moment = datetime(2026, 9, 26, 12, 34, 56, tzinfo=UTC)

    name = batch_export_filename(3, moment=moment)

    assert name == "batch-3games-20260926T123456.archive.zip"


# ---------------------------------------------------------------- 批量导入


def _batch_inspection(*, app_id: int | None = 730) -> BatchInspection:
    """一个最小的批量包体检结果(单游戏, 用它复用单包的那两条存档位置)."""
    return BatchInspection(
        path=Path("batch.archive.zip"),
        games=(
            BatchGameInspection(
                entry="Demo.archive.zip",
                inspection=_inspection(app_id=app_id),
            ),
        ),
    )


def test_batch_import_prompt_defaults_every_row_to_new_with_a_prefilled_path() -> None:
    """逐行默认值: 策略"新建"、目标预选库里的第一款、只预填本机已存在的位置."""
    prompt = batch_import_prompt(_batch_inspection(), [_packaged_game("7", "别的游戏")])

    row = prompt.rows[0]
    assert row.entry == "Demo.archive.zip"
    assert row.name == "Demo"
    assert row.meta == tr(
        "dialog.import_batch_row_meta",
        platform="Windows",
        app_id=730,
        backups=0,
        files=0,
    )
    assert row.strategy == STRATEGY_NEW
    assert row.target_game_id == "7"
    assert [key for key, _text in row.strategies] == [
        STRATEGY_NEW,
        STRATEGY_MERGE,
        STRATEGY_SKIP,
    ]
    assert [item.default for item in row.locations] == ["D:/saves", ""]
    assert prompt.summary == tr(
        "dialog.import_batch_summary",
        games=1,
        backups=0,
        files=0,
        size=size_label(0),
    )
    assert prompt.hint


def test_batch_import_prompt_without_app_id_or_library_games_offers_no_merge() -> None:
    """包里没有 AppID / 库里一款游戏都没有: 这一行就不该提供"合并"."""
    prompt = batch_import_prompt(_batch_inspection(app_id=None), [])

    row = prompt.rows[0]
    assert "AppID" not in row.meta
    assert row.targets == ()
    assert row.target_game_id is None
    assert [key for key, _text in row.strategies] == [STRATEGY_NEW, STRATEGY_SKIP]


def test_batch_import_prompt_disambiguates_twin_target_names() -> None:
    """目标下拉框的文案在重名时补上游戏 id, 于是文案总能唯一地还原成 id."""
    inspection = replace(_inspection(), matching_game_id=42)
    batch = BatchInspection(
        path=Path("batch.archive.zip"),
        games=(BatchGameInspection(entry="a.archive.zip", inspection=inspection),),
    )
    games = [_packaged_game("42", "同名"), _packaged_game("7", "同名")]
    other = [_packaged_game("7", "同名"), _packaged_game("9", "独一份")]

    row = batch_import_prompt(batch, games).rows[0]
    clean = batch_import_prompt(batch, other).rows[0]

    assert [option.label for option in row.targets] == ["同名 (42)", "同名 (7)"]
    assert [option.selected for option in row.targets] == [True, False]
    assert row.match_text == tr("dialog.import_match", name="同名")
    # 没有重名时文案保持干净(不额外加 id).
    assert [option.label for option in clean.targets] == ["同名", "独一份"]
    assert target_game_id(row.targets, "同名 (7)") == "7"
    assert target_game_id(row.targets, "同名") is None
    assert target_game_id(row.targets, "没有这一项") is None
    assert target_label_for(row.targets, "42") == "同名 (42)"
    assert target_label_for(row.targets, None) == "同名 (42)"


def test_batch_row_choice_keeps_only_what_the_user_expressed() -> None:
    """只有"合并"带目标, 空目标不猜; 留空的存档位置不导入."""
    merged = batch_row_choice(
        strategy=STRATEGY_MERGE, target="7", locations={0: " D:/saves ", 1: "   "}
    )
    fresh = batch_row_choice(strategy=STRATEGY_NEW, target="7", locations={})
    empty_target = batch_row_choice(strategy=STRATEGY_MERGE, target=None, locations={})

    assert merged == ImportChoice(
        strategy=STRATEGY_MERGE,
        target_game_id="7",
        locations={0: "D:/saves"},
    )
    # "新建"即使界面上留着目标也不带它.
    assert fresh.target_game_id is None
    # 选了"合并"却没有目标: 留给后端报"需要先选定要合并到的游戏", 界面不替用户猜.
    assert empty_target.target_game_id is None
