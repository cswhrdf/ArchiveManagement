"""存档路径候选的校验、落库与确认(阶段 G-3).

锁住三条约定: 未确认的候选绝不进 ``save_locations``; 主目录/盘符根/游戏安装
目录这类危险候选会被标记并且拒绝确认; 重复探测与重复确认都不产生重复数据。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.application.candidates import (
    confirm_candidate,
    suggest_candidates,
)
from archive_management.domain import (
    Game,
    GameCandidate,
    SaveCandidate,
    SavePathCandidate,
)
from archive_management.exceptions import ArchiveManagementError, SaveCandidateError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
    SaveCandidateRepository,
    SaveLocationRepository,
)
from helpers import migrated_database

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("存档路径候选"),
    pytest.mark.story("候选校验与确认落库"),
    pytest.mark.layer("unit"),
]


class _FakeSource:
    """固定的候选来源, 同时记录平台适配器收到的参数."""

    def __init__(self, *paths: str) -> None:
        self._paths = paths
        self.requests: list[tuple[str, Path | None]] = []

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        """返回构造时给定的候选路径."""
        self.requests.append((app_id, install_dir))
        return [
            SavePathCandidate(
                path=path, reason_code="steam_remotecache", detail="remotecache.vdf"
            )
            for path in self._paths
        ]


def _game(database: Database, *, app_id: int | None = 730) -> int:
    """建一个游戏并返回 id; ``app_id`` 为 None 时模拟手工录入的游戏."""
    game = GameRepository(database).add(
        Game(name="Demo", steam_app_id=app_id, origin="steam")
    )
    assert game.id is not None
    return game.id


def _identifier(candidate: SaveCandidate) -> int:
    """取出候选的 id(经过落库的候选必然有 id)."""
    assert candidate.id is not None
    return candidate.id


def pending_candidates(database: Database, game_id: int) -> list[SaveCandidate]:
    """本文件用的便利函数: 直接查仓库里待确认的候选(应用层同名函数已删除)."""
    return SaveCandidateRepository(database).list_for_game(game_id, status="suggested")


def test_migration_creates_the_candidate_table(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    assert database.schema_version() == database.latest_schema_version()
    with database.connect() as connection:
        rows = connection.execute("PRAGMA table_info(save_path_candidates)")
        columns = {str(row[1]) for row in rows}
    assert {"game_id", "path", "risk_reason", "status", "decided_at"} <= columns


@pytest.mark.blocker
def test_dangerous_candidates_are_marked_and_never_stored(tmp_path: Path) -> None:
    """主目录、盘符根与游戏安装目录只标记不落库."""
    database = migrated_database(tmp_path)
    install_dir = tmp_path / "steam" / "Demo"
    install_dir.mkdir(parents=True)
    safe = tmp_path / "saves"
    game_id = _game(database)
    source = _FakeSource(
        str(Path.home()), str(Path(install_dir.anchor)), str(install_dir), str(safe)
    )

    report = suggest_candidates(database, game_id, source, install_dir=install_dir)

    dangerous = {
        candidate.path: candidate.risk_reason
        for candidate in report.candidates
        if candidate.risk_reason
    }
    assert report.found == 4
    assert report.dangerous == 3
    assert set(dangerous.values()) == {"user_home", "drive_root", "protected"}
    assert SaveLocationRepository(database).list_for_game(game_id) == []


@pytest.mark.blocker
def test_unconfirmed_candidates_never_reach_save_locations(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    game_id = _game(database)

    report = suggest_candidates(database, game_id, _FakeSource(str(tmp_path / "saves")))

    assert report.created == 1
    assert [item.status for item in pending_candidates(database, game_id)] == [
        "suggested"
    ]
    assert SaveLocationRepository(database).list_for_game(game_id) == []


def test_confirming_a_candidate_registers_the_save_location(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    game_id = _game(database)
    suggest_candidates(database, game_id, _FakeSource(str(save_dir)))
    candidate = pending_candidates(database, game_id)[0]

    location = confirm_candidate(database, _identifier(candidate))

    assert location.path == str(save_dir)
    assert location.path_kind == "directory"
    assert location.source == "steam"
    assert location.is_primary is True
    assert SaveLocationRepository(database).list_for_game(game_id) == [location]
    stored = SaveCandidateRepository(database).get(_identifier(candidate))
    assert stored is not None
    assert stored.status == "confirmed"
    assert stored.decided_at is not None
    assert pending_candidates(database, game_id) == []


def test_confirming_twice_keeps_a_single_save_location(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    game_id = _game(database)
    suggest_candidates(database, game_id, _FakeSource(str(tmp_path / "saves")))
    candidate_id = _identifier(pending_candidates(database, game_id)[0])

    first = confirm_candidate(database, candidate_id)
    second = confirm_candidate(database, candidate_id)

    assert first.id == second.id
    assert len(SaveLocationRepository(database).list_for_game(game_id)) == 1


@pytest.mark.blocker
def test_confirming_a_dangerous_candidate_is_rejected(
    tmp_path: Path, audit_log: list[str]
) -> None:
    database = migrated_database(tmp_path)
    install_dir = tmp_path / "steam" / "Demo"
    install_dir.mkdir(parents=True)
    game_id = _game(database)
    suggest_candidates(
        database, game_id, _FakeSource(str(install_dir)), install_dir=install_dir
    )
    candidate_id = _identifier(pending_candidates(database, game_id)[0])

    with pytest.raises(SaveCandidateError):
        confirm_candidate(database, candidate_id)

    assert SaveLocationRepository(database).list_for_game(game_id) == []
    assert "candidates.confirm" in " ".join(audit_log)


def test_save_folders_inside_the_install_dir_are_allowed(tmp_path: Path) -> None:
    """存档目录就在游戏安装目录里是常见做法, 不能算危险(真正危险的是整个安装目录)."""
    database = migrated_database(tmp_path)
    install_dir = tmp_path / "steam" / "Demo"
    saves = install_dir / "saves"
    saves.mkdir(parents=True)
    game_id = _game(database)

    inside = suggest_candidates(
        database, game_id, _FakeSource(str(saves)), install_dir=install_dir
    )

    assert inside.dangerous == 0
    assert inside.candidates[0].risk_reason == ""
    assert inside.candidates[0].status == "suggested"

    # 而"整个安装目录"仍然被拦下(那种情况恢复/删除会动到游戏本体).
    whole = suggest_candidates(
        database, game_id, _FakeSource(str(install_dir)), install_dir=install_dir
    )

    assert whole.dangerous == 1
    assert whole.candidates[0].risk_reason == "protected"


def test_repeated_scan_updates_a_single_candidate(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    game_id = _game(database)
    source = _FakeSource(str(tmp_path / "saves"))

    first = suggest_candidates(database, game_id, source)
    second = suggest_candidates(database, game_id, source)

    assert (first.created, first.updated) == (1, 0)
    assert (second.created, second.updated) == (0, 1)
    assert len(SaveCandidateRepository(database).list_for_game(game_id)) == 1


def test_unknown_candidate_is_rejected(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    with pytest.raises(ArchiveManagementError):
        confirm_candidate(database, 999)


def test_games_without_a_platform_identifier_are_rejected(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    game_id = _game(database, app_id=None)
    with pytest.raises(ArchiveManagementError):
        suggest_candidates(database, game_id, _FakeSource(str(tmp_path / "saves")))


def test_install_dir_comes_from_the_imported_candidate(tmp_path: Path) -> None:
    """没有显式给安装目录时, 回查该游戏已导入的探测结果."""
    database = migrated_database(tmp_path)
    install_dir = tmp_path / "steam" / "Demo"
    install_dir.mkdir(parents=True)
    game_id = _game(database)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Demo",
            install_dir=str(install_dir),
            source="steam",
            status="imported",
            game_id=game_id,
        )
    )
    source = _FakeSource(str(tmp_path / "saves"))

    suggest_candidates(database, game_id, source)

    assert source.requests == [("730", install_dir)]


def test_candidate_health_reflects_the_current_path_state(tmp_path: Path) -> None:
    database = migrated_database(tmp_path)
    missing = tmp_path / "gone"
    game_id = _game(database)
    suggest_candidates(database, game_id, _FakeSource(str(missing)))

    assert pending_candidates(database, game_id)[0].health == "missing"

    missing.mkdir()
    report = suggest_candidates(database, game_id, _FakeSource(str(missing)))

    assert report.candidates[0].health == "ok"
