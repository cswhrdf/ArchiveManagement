"""本地游戏探测与监控目录用例的单元测试.

覆盖监控目录的增删改查与校验(存在性、类型、高风险路径、重复检测)、扫描结果的
落库与去重(用户的"已忽略"决定不被下一次扫描覆盖)、候选的导入/忽略/恢复/修正
路径, 以及探测失败时的降级行为。全部使用临时数据库与临时目录。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from archive_management.application.discovery import (
    add_candidate_as_monitored,
    add_monitored_directory,
    ignore_candidate,
    import_candidate,
    relocate_candidate,
    remove_monitored_directory,
    restore_candidate,
    scan_library,
    set_monitored_enabled,
    update_monitored_directory,
)
from archive_management.domain import Game, GameCandidate
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
    MonitoredDirectoryRepository,
)
from archive_management.services.platform_scan import (
    LocalGameScanner,
    NullRegistry,
    ScanRoots,
)
from helpers import migrated_database

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("本地游戏探测"),
    pytest.mark.story("管理监控目录与探测结果"),
    pytest.mark.layer("unit"),
]


def _database(tmp_path: Path) -> Database:
    """创建并迁移一个临时数据库(共享实现见 tests/helpers.py)."""
    return migrated_database(tmp_path)


def _stub_scanner(
    candidates: Sequence[GameCandidate], *, fail: bool = False
) -> LocalGameScanner:
    """返回一个固定结果的探测替身(注册表与根目录都不使用)."""

    class _Stub(LocalGameScanner):
        def __init__(self) -> None:
            empty = Path()
            super().__init__(
                ScanRoots(
                    platform="windows",
                    program_data=empty,
                    program_files=empty,
                    program_files_x86=empty,
                    local_app_data=empty,
                    user_profile=empty,
                    registry=NullRegistry(),
                )
            )

        def scan(self, *, monitored: Sequence[str] = ()) -> list[GameCandidate]:
            del monitored
            if fail:
                raise RuntimeError("探测服务不可用")
            return list(candidates)

    return _Stub()


def _candidate(name: str, path: Path, **overrides: object) -> GameCandidate:
    """构造一条可用的候选(默认来源为 Steam 清单)."""
    base: dict[str, object] = {
        "name": name,
        "install_dir": str(path),
        "source": "steam",
        "confidence": "high",
        "reason_code": "steam_manifest",
        "health": "ok",
    }
    base.update(overrides)
    return GameCandidate.model_validate(base)


# ---------------------------------------------------------------- 监控目录


def test_add_monitored_directory_persists_and_rejects_duplicates(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    games = tmp_path / "Games"
    games.mkdir()

    created = add_monitored_directory(database, str(games), note="自定义")

    assert created.id is not None
    assert created.path == str(games)
    assert created.note == "自定义"
    assert created.enabled is True
    stored = MonitoredDirectoryRepository(database).list_all()
    assert [item.path for item in stored] == [str(games)]

    with pytest.raises(ArchiveManagementError, match="已在监控列表"):
        add_monitored_directory(database, str(games))


def test_add_monitored_directory_validates_path(tmp_path: Path) -> None:
    database = _database(tmp_path)
    saved = tmp_path / "save.dat"
    saved.write_text("x", encoding="utf-8")

    with pytest.raises(ArchiveManagementError, match="必须是文件夹"):
        add_monitored_directory(database, str(saved))
    with pytest.raises(ArchiveManagementError, match="不存在或无法访问"):
        add_monitored_directory(database, str(tmp_path / "missing"))
    with pytest.raises(ArchiveManagementError, match="高风险位置"):
        add_monitored_directory(database, str(Path.cwd().anchor))
    assert MonitoredDirectoryRepository(database).list_all() == []


def test_update_toggle_and_remove_monitored_directory(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = tmp_path / "Games"
    second = tmp_path / "More Games"
    first.mkdir()
    second.mkdir()
    created = add_monitored_directory(database, str(first))
    assert created.id is not None

    updated = update_monitored_directory(
        database, created.id, path=str(second), note="换目录"
    )
    assert updated.path == str(second)
    assert updated.note == "换目录"

    disabled = set_monitored_enabled(database, created.id, False)
    assert disabled.enabled is False

    with pytest.raises(ArchiveManagementError, match="不存在或无法访问"):
        update_monitored_directory(database, created.id, path=str(tmp_path / "nope"))

    remove_monitored_directory(database, created.id)
    assert MonitoredDirectoryRepository(database).list_all() == []
    with pytest.raises(ArchiveManagementError, match="未知监控目录"):
        remove_monitored_directory(database, created.id)


# ------------------------------------------------------------------ 扫描


def test_scan_persists_candidates_and_marks_directory_status(tmp_path: Path) -> None:
    database = _database(tmp_path)
    games = tmp_path / "Games"
    games.mkdir()
    add_monitored_directory(database, str(games))
    installed = tmp_path / "Steam" / "Hades"
    installed.mkdir(parents=True)

    report = scan_library(
        database, scanner=_stub_scanner([_candidate("Hades", installed)])
    )

    assert report.total == 1
    assert report.added == 1
    assert report.updated == 0
    assert report.active == 1
    assert report.ok
    stored = CandidateRepository(database).list_all()
    assert [item.name for item in stored] == ["Hades"]
    assert stored[0].health == "ok"
    directory = MonitoredDirectoryRepository(database).list_all()[0]
    assert directory.last_scan_at is not None
    assert directory.last_scan_status == "ok"


def test_scan_keeps_user_decision_and_refreshes_path_health(tmp_path: Path) -> None:
    database = _database(tmp_path)
    games = tmp_path / "Stale"
    games.mkdir()
    stale = tmp_path / "Gone"
    stale.mkdir()
    first = scan_library(database, scanner=_stub_scanner([_candidate("Gone", stale)]))
    assert first.added == 1
    candidate = CandidateRepository(database).list_all()[0]
    assert candidate.id is not None
    ignore_candidate(database, candidate.id)

    # 游戏被卸载后再次扫描: 已忽略的候选不会被重新变成待处理, 但路径状态会更新.
    stale.rmdir()
    second = scan_library(database, scanner=_stub_scanner([]))

    assert second.total == 0
    refreshed = CandidateRepository(database).list_all()[0]
    assert refreshed.status == "ignored"
    assert refreshed.health == "missing"


def test_scan_links_candidates_that_match_existing_games(tmp_path: Path) -> None:
    database = _database(tmp_path)
    installed = tmp_path / "Hades"
    installed.mkdir()
    game = GameRepository(database).add(Game(name="Hades"))
    assert game.id is not None

    report = scan_library(
        database, scanner=_stub_scanner([_candidate("Hades", installed)])
    )

    assert report.linked == 1
    stored = CandidateRepository(database).list_all()[0]
    assert stored.status == "imported"
    assert stored.game_id == game.id


def test_scan_counts_unusable_paths_and_survives_failure(tmp_path: Path) -> None:
    database = _database(tmp_path)
    missing = tmp_path / "Missing"
    report = scan_library(
        database,
        scanner=_stub_scanner([_candidate("Missing", missing, health="missing")]),
    )
    assert report.unusable == 1
    assert report.added == 1

    failed = scan_library(database, scanner=_stub_scanner([], fail=True))

    assert not failed.ok
    assert len(failed.errors) == 1
    assert failed.total == 0


def test_scan_updates_existing_candidate_instead_of_duplicating(tmp_path: Path) -> None:
    database = _database(tmp_path)
    installed = tmp_path / "Hades"
    installed.mkdir()
    scan_library(database, scanner=_stub_scanner([_candidate("Hades", installed)]))
    report = scan_library(
        database,
        scanner=_stub_scanner([_candidate("Hades 2", installed, source="epic")]),
    )

    assert report.added == 0
    assert report.updated == 1
    stored = CandidateRepository(database).list_all()
    assert len(stored) == 1
    assert stored[0].name == "Hades 2"
    assert stored[0].source == "epic"


# ---------------------------------------------------------------- 候选处理


def test_import_candidate_creates_game_and_marks_imported(tmp_path: Path) -> None:
    database = _database(tmp_path)
    installed = tmp_path / "Hades"
    installed.mkdir()
    scan_library(database, scanner=_stub_scanner([_candidate("Hades", installed)]))
    candidate = CandidateRepository(database).list_all()[0]
    assert candidate.id is not None

    game = import_candidate(database, candidate.id, name="哈迪斯")

    assert game.id is not None
    assert game.name == "哈迪斯"
    assert game.original_name == "Hades"
    updated = CandidateRepository(database).get(candidate.id)
    assert updated is not None
    assert updated.status == "imported"
    assert updated.game_id == game.id

    with pytest.raises(ArchiveManagementError, match="已导入为游戏"):
        import_candidate(database, candidate.id)


def test_import_candidate_requires_name(tmp_path: Path) -> None:
    database = _database(tmp_path)
    installed = tmp_path / "Hades"
    installed.mkdir()
    scan_library(database, scanner=_stub_scanner([_candidate("Hades", installed)]))
    candidate = CandidateRepository(database).list_all()[0]
    assert candidate.id is not None

    with pytest.raises(ArchiveManagementError, match="名称不能为空"):
        import_candidate(database, candidate.id, name="   ")


def test_ignore_and_restore_candidate(tmp_path: Path) -> None:
    database = _database(tmp_path)
    installed = tmp_path / "Hades"
    installed.mkdir()
    scan_library(database, scanner=_stub_scanner([_candidate("Hades", installed)]))
    candidate = CandidateRepository(database).list_all()[0]
    assert candidate.id is not None

    ignored = ignore_candidate(database, candidate.id)
    assert ignored.status == "ignored"
    assert CandidateRepository(database).count_by_status()["ignored"] == 1

    restored = restore_candidate(database, candidate.id)
    assert restored.status == "new"


def test_restore_candidate_relinks_when_game_still_exists(tmp_path: Path) -> None:
    database = _database(tmp_path)
    installed = tmp_path / "Hades"
    installed.mkdir()
    game = GameRepository(database).add(Game(name="Hades"))
    assert game.id is not None
    scan_library(database, scanner=_stub_scanner([_candidate("Hades", installed)]))
    candidate = CandidateRepository(database).list_all()[0]
    assert candidate.id is not None
    ignore_candidate(database, candidate.id)

    restored = restore_candidate(database, candidate.id)

    assert restored.status == "imported"
    assert restored.game_id == game.id


def test_relocate_candidate_validates_path_and_duplicates(tmp_path: Path) -> None:
    database = _database(tmp_path)
    first = tmp_path / "First"
    second = tmp_path / "Second"
    first.mkdir()
    second.mkdir()
    scan_library(
        database,
        scanner=_stub_scanner(
            [_candidate("First", first), _candidate("Second", second)]
        ),
    )
    items = CandidateRepository(database).list_all()
    target = next(item for item in items if item.name == "Second")
    assert target.id is not None
    moved = tmp_path / "Moved"
    moved.mkdir()

    updated = relocate_candidate(database, target.id, str(moved))

    assert updated.install_dir == str(moved)
    assert updated.health == "ok"
    with pytest.raises(ArchiveManagementError, match="已存在于探测结果"):
        relocate_candidate(database, target.id, str(first))
    with pytest.raises(ArchiveManagementError, match="不存在或无法访问"):
        relocate_candidate(database, target.id, str(tmp_path / "nope"))


def test_add_candidate_as_monitored_uses_parent_directory(tmp_path: Path) -> None:
    database = _database(tmp_path)
    games = tmp_path / "Games"
    installed = games / "Hades"
    installed.mkdir(parents=True)
    scan_library(database, scanner=_stub_scanner([_candidate("Hades", installed)]))
    candidate = CandidateRepository(database).list_all()[0]
    assert candidate.id is not None

    created = add_candidate_as_monitored(database, candidate.id)

    assert created.path == str(games)
    assert created.note == "Hades"
    with pytest.raises(ArchiveManagementError, match="已在监控列表"):
        add_candidate_as_monitored(database, candidate.id)


def test_unknown_ids_are_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        import_candidate(database, 999)
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        ignore_candidate(database, 999)
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        restore_candidate(database, 999)
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        relocate_candidate(database, 999, str(tmp_path))
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        add_candidate_as_monitored(database, 999)
