"""游戏生命周期用例(删除释放候选、启用互斥、自动启停接缝).

这些约束都是"数据一致性"级别的: 删除游戏后探测候选不能卡在已导入, 启用第二款
游戏必须把第一款停用, 自动启停策略在没实现之前不能改变任何状态。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.application import discovery as discovery_cases
from archive_management.application import games as games_cases
from archive_management.application.home import set_archived
from archive_management.domain import Game, GameCandidate
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
)
from helpers import migrated_database

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("游戏生命周期"),
    pytest.mark.story("删除与启用规则"),
    pytest.mark.layer("unit"),
]


def _database(tmp_path: Path) -> Database:
    return migrated_database(tmp_path)


def _imported_game(database: Database, tmp_path: Path) -> tuple[Game, int]:
    """造一条探测候选并导入为游戏, 返回游戏与候选 id."""
    install = tmp_path / "steam" / "Demo"
    install.mkdir(parents=True)
    candidate, _created = CandidateRepository(database).upsert(
        GameCandidate(name="Demo", install_dir=str(install), source="steam")
    )
    assert candidate.id is not None
    game = discovery_cases.import_candidate(database, candidate.id)
    return game, candidate.id


def test_deleting_a_game_releases_its_discovery_candidate(tmp_path: Path) -> None:
    """删除游戏要把候选退回待处理, 否则它会停在"已导入"却指向任何游戏."""
    database = _database(tmp_path)
    game, candidate_id = _imported_game(database, tmp_path)
    assert game.id is not None

    result = games_cases.delete_game(database, game.id)

    assert result.released_candidates == 1
    assert GameRepository(database).get(game.id) is None
    released = CandidateRepository(database).get(candidate_id)
    assert released is not None
    assert released.status == "new"
    assert released.game_id is None
    assert CandidateRepository(database).count_by_status()["new"] == 1


def test_deleting_a_manually_added_game_releases_nothing(tmp_path: Path) -> None:
    database = _database(tmp_path)
    game = GameRepository(database).add(Game(name="手工录入", enabled=True))
    assert game.id is not None

    result = games_cases.delete_game(database, game.id)

    assert result.released_candidates == 0
    assert GameRepository(database).get(game.id) is None


def test_enabling_a_game_disables_the_other_one(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = GameRepository(database)
    first = repository.add(Game(name="第一位", enabled=True))
    second = repository.add(Game(name="第二位"))
    assert first.id is not None
    assert second.id is not None

    result = games_cases.set_enabled(database, second.id, True)

    assert result.game.enabled is True
    assert result.replaced is not None
    assert result.replaced.id == first.id
    assert repository.enabled_game_id() == second.id
    active = games_cases.active_game(database)
    assert active is not None
    assert active.id == second.id


def test_disabling_a_game_leaves_no_active_game(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = GameRepository(database)
    game = repository.add(Game(name="Demo", enabled=True))
    assert game.id is not None

    result = games_cases.set_enabled(database, game.id, False)

    assert result.game.enabled is False
    assert result.replaced is None
    assert repository.enabled_game_id() is None
    assert games_cases.active_game(database) is None


def test_enabling_an_archived_game_is_rejected(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = GameRepository(database)
    game = repository.add(Game(name="Demo"))
    assert game.id is not None
    set_archived(database, game.id, True)

    with pytest.raises(ArchiveManagementError):
        games_cases.set_enabled(database, game.id, True)

    assert repository.enabled_game_id() is None


def test_manual_activation_never_changes_anything(tmp_path: Path) -> None:
    """自动启停尚未实现: 默认策略必须保持用户手动设置的状态."""
    database = _database(tmp_path)
    repository = GameRepository(database)
    game = repository.add(Game(name="Demo", enabled=True))
    assert game.id is not None
    policy = games_cases.ManualActivation()

    assert games_cases.apply_activation(database, policy, running=["game.exe"]) is None
    assert repository.enabled_game_id() == game.id


def test_activation_switches_to_the_policy_target(tmp_path: Path) -> None:
    """策略给出目标时复用 set_enabled: 切换同时把原来的那款停用."""
    database = _database(tmp_path)
    repository = GameRepository(database)
    current = repository.add(Game(name="旧目标", enabled=True))
    target = repository.add(Game(name="新目标"))
    assert current.id is not None
    assert target.id is not None

    class _FakePolicy:
        """固定返回目标游戏 id 的假策略."""

        def target_game(self, games: object, *, running: object) -> int | None:
            return target.id

    result = games_cases.apply_activation(database, _FakePolicy(), running=[])

    assert result is not None
    assert result.game.id == target.id
    assert result.replaced is not None
    assert result.replaced.id == current.id
    assert repository.enabled_game_id() == target.id


def test_activation_rejects_an_unknown_target(tmp_path: Path) -> None:
    database = _database(tmp_path)
    repository = GameRepository(database)
    repository.add(Game(name="Demo", enabled=True))

    class _BadPolicy:
        """返回不存在游戏的策略."""

        def target_game(self, games: object, *, running: object) -> int | None:
            return 999

    with pytest.raises(ArchiveManagementError):
        games_cases.apply_activation(database, _BadPolicy())
