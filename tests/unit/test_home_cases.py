"""统一游戏主页用例的单元测试.

覆盖主页事实的取数(存档位置数量、备份计数、最近备份时间、路径风险、探测关联)、
筛选条件的持久化与版本守卫、归档与自定义标签, 以及导入探测结果后游戏会带上来源
平台。全部使用临时数据库与临时目录。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from archive_management.application.discovery import (
    add_monitored_directory,
    import_candidate,
    scan_library,
)
from archive_management.application.home import (
    filter_home,
    load_facts,
    load_home,
    save_filter,
    set_archived,
    set_tags,
)
from archive_management.domain import (
    DEFAULT_PAGE_SIZE,
    HOME_STATE_VERSION,
    BackupNode,
    Game,
    GameCandidate,
    HomeFilter,
    HomeLayout,
    HomeView,
    SaveLocation,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    CandidateRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.platform_scan import (
    LocalGameScanner,
    NullRegistry,
    ScanRoots,
)
from helpers import migrated_database

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("统一游戏主页"),
    pytest.mark.story("主页取数与筛选持久化"),
    pytest.mark.layer("unit"),
]


def _database(tmp_path: Path) -> Database:
    """创建并迁移一个临时数据库(共享实现见 tests/helpers.py)."""
    return migrated_database(tmp_path)


def _game(database: Database, name: str, *, path: Path | None = None) -> int:
    """插入一个游戏并返回它的 id, 可选地附带一个可用的存档位置."""
    repository = GameRepository(database)
    game = repository.add(Game(name=name, original_name=name))
    assert game.id is not None
    game_id = game.id
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        SaveLocationRepository(database).add(
            SaveLocation(
                game_id=game_id,
                path=str(path),
                path_kind="directory",
                is_primary=True,
            )
        )
    return game_id


def _backup(database: Database, game_id: int, *, when: datetime) -> None:
    """写入一个备份节点(用于主页的备份计数与最近备份时间)."""
    BackupRepository(database).add(
        BackupNode(game_id=game_id, node_kind="manual", created_at=when)
    )


def _candidates(database: Database) -> list[GameCandidate]:
    """读取全部候选(测试辅助: 避免直接依赖仓库细节)."""
    return CandidateRepository(database).list_all()


def test_load_facts_collects_counts_times_and_locations(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game(database, "星际拓荒", path=tmp_path / "save")
    _backup(database, game_id, when=datetime(2026, 9, 1, 8, 0, tzinfo=UTC))

    facts = load_facts(database)
    assert len(facts) == 1
    item = facts[0]
    assert item.game_id == str(game_id)
    assert item.location_count == 1
    assert item.backup_count == 1
    assert item.risk is False
    assert item.origin == "manual"
    assert item.last_backup_at is not None
    assert item.pending is False


def test_load_facts_flags_missing_save_location(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _game(database, "丢失的存档", path=tmp_path / "gone")
    (tmp_path / "gone").rmdir()

    facts = load_facts(database)
    assert facts[0].risk is True
    assert facts[0].pending is True


def test_game_without_locations_needs_attention(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _game(database, "还没配置")

    facts = load_facts(database)
    assert facts[0].location_count == 0
    assert facts[0].pending is True


def test_filter_state_round_trip(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _game(database, "星际拓荒")

    saved = save_filter(
        database, HomeFilter(view=HomeView.PENDING, origin="steam", search=" 拓荒 ")
    )
    assert saved.search == "拓荒"
    report = load_home(database)
    assert report.filter.view is HomeView.PENDING
    assert report.filter.origin == "steam"
    assert report.filter.search == "拓荒"


def test_display_preferences_round_trip(tmp_path: Path) -> None:
    """展示方式与每页条数也是持久化的: 重启后仍是上次看到的样子."""
    database = _database(tmp_path)
    _game(database, "星际拓荒")

    saved = save_filter(
        database,
        HomeFilter(layout=HomeLayout.POSTER, page_size=60),
    )
    assert saved.layout is HomeLayout.POSTER
    assert saved.page_size == 60

    report = load_home(database)
    assert report.filter.layout is HomeLayout.POSTER
    assert report.filter.page_size == 60


def test_illegal_display_preferences_fall_back(tmp_path: Path) -> None:
    """非法取值在落库前被规范化, 不会写进数据库让界面读到无法解释的值."""
    database = _database(tmp_path)
    _game(database, "星际拓荒")

    saved = save_filter(database, HomeFilter(page_size=7))
    assert saved.page_size == DEFAULT_PAGE_SIZE
    assert load_home(database).filter.page_size == DEFAULT_PAGE_SIZE


def test_load_home_drops_state_from_another_version(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _game(database, "星际拓荒")
    save_filter(database, HomeFilter(view=HomeView.ARCHIVED))

    raw = Database(database.path)
    with raw.session() as connection:
        connection.execute("UPDATE home_state SET version = ? WHERE id = 1", (99,))

    report = load_home(raw)
    assert report.filter.view is HomeView.ALL
    assert report.filter.version == HOME_STATE_VERSION


def test_filter_home_reuses_facts_and_applies_search(tmp_path: Path) -> None:
    database = _database(tmp_path)
    _game(database, "星际拓荒")
    _game(database, "空洞骑士")
    facts = load_facts(database)

    report = filter_home(database, HomeFilter(search="空洞"), facts=facts)
    assert [fact.name for fact in report.games] == ["空洞骑士"]
    assert report.stats.total == 2


def test_set_archived_hides_game_from_default_view(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game(database, "老游戏", path=tmp_path / "save")

    updated = set_archived(database, game_id, True)
    assert updated.archived is True
    assert load_home(database).games == ()
    archived = load_home(database)
    assert archived.stats.archived == 1

    report = filter_home(database, HomeFilter(view=HomeView.ARCHIVED))
    assert [fact.name for fact in report.games] == ["老游戏"]

    assert set_archived(database, game_id, False).archived is False
    assert len(load_home(database).games) == 1


def test_set_tags_cleans_input(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game(database, "星际拓荒")

    tags = set_tags(database, game_id, [" 探索 ", "探索", "解谜"])
    assert tags == ("探索", "解谜")
    assert load_facts(database)[0].tags == ("探索", "解谜")


def test_unknown_game_is_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with pytest.raises(ArchiveManagementError):
        set_archived(database, 999, True)
    with pytest.raises(ArchiveManagementError):
        set_tags(database, 999, ["x"])


def test_imported_candidate_carries_platform_origin(tmp_path: Path) -> None:
    database = _database(tmp_path)
    library = tmp_path / "steam"
    (library / "HollowKnight").mkdir(parents=True)
    add_monitored_directory(database, str(library))
    scanner = LocalGameScanner(
        ScanRoots(
            platform="windows",
            program_data=tmp_path,
            program_files=tmp_path,
            program_files_x86=tmp_path,
            local_app_data=tmp_path,
            user_profile=tmp_path,
            registry=NullRegistry(),
        )
    )
    scan_library(database, scanner=scanner)

    facts = load_facts(database)
    assert facts == []

    candidate = next(
        item for item in _candidates(database) if item.name == "HollowKnight"
    )
    assert candidate.id is not None
    game = import_candidate(database, candidate.id)
    facts = load_facts(database)
    assert facts[0].game_id == str(game.id)
    assert facts[0].origin == "monitored"
    assert facts[0].monitored is True


def test_recent_window_uses_backup_time(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game_id = _game(database, "刚备份过", path=tmp_path / "save")
    now = datetime.now(UTC)
    _backup(database, game_id, when=now)

    facts = load_facts(database)
    assert facts[0].is_recent(now + timedelta(days=1)) is True
    assert facts[0].is_stale(now + timedelta(days=60)) is True
