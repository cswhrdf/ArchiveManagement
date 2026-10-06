"""应用层的边界与失败路径: 让"只在拿不准/出错时才会执行"的代码也进统计.

这些行以前是覆盖率报告里的缺口, 而缺口与"没人看过的代码"长得一模一样。这里的判据
一律是**这条边界上的行为是什么**, 不是"跑过就算":

- 策略层(自动启停)的两种"什么都不做": 没人启用且队列空、监控对象退出而启用态早已
  不是它 —— 这两种情况下任何动作都会切错游戏, 所以必须没有动作;
- 用例层的失败路径: 删除中途出错要留下失败审计并把异常抛出去(不能悄悄当成成功),
  未知/同名/别的游戏的数据要给出明确的拒绝理由。

界面与包格式(导出包、导入包)的边界另见各自的用例文件。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.application import backup as backup_mod
from archive_management.application import games as games_cases
from archive_management.application import locations as locations_mod
from archive_management.application.activation import QueueActivation
from archive_management.application.backup import BackupService
from archive_management.application.candidates import suggest_candidates
from archive_management.application.discovery import import_candidate
from archive_management.application.home import set_origin
from archive_management.application.locations import remove_save_location
from archive_management.domain import (
    ActivationState,
    Game,
    GameCandidate,
    SavePathCandidate,
)
from archive_management.domain.activation import REASON_IDLE
from archive_management.exceptions import ArchiveManagementError, DatabaseError
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.processes import GameProbe
from helpers import add_game, make_save_folder, migrated_database

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(应用层边界)"),
    pytest.mark.story("拿不准时什么都不做, 出错时留下可追溯的记录"),
    pytest.mark.layer("unit"),
]


class _FixedPolicy:
    """固定返回一个决定的策略替身(接缝允许注入自定义策略)."""

    def __init__(self, decision: games_cases.ActivationDecision) -> None:
        """记下要交给接缝的那个决定."""
        self._decision = decision

    def decide(self, *args: object, **kwargs: object) -> games_cases.ActivationDecision:
        """忽略全部输入, 直接返回预先给定的决定."""
        return self._decision


class _RecordingSource:
    """记录被问到的 (AppID, 安装目录), 不产出任何候选的候选来源替身."""

    def __init__(self) -> None:
        """一开始什么都没被问过."""
        self.requests: list[tuple[str, Path | None]] = []

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        """记下这次询问并返回空列表."""
        self.requests.append((app_id, install_dir))
        return []


class _Trash:
    """把目标挪进临时"回收站"目录的替身(不触碰系统回收站)."""

    def __init__(self, destination: Path) -> None:
        """指定临时回收站目录."""
        self.destination = destination
        self.moved: list[Path] = []

    def __call__(self, path: str) -> None:
        """把给定路径挪进临时回收站并记一笔."""
        target = self.destination / Path(path).name
        Path(path).rename(target)
        self.moved.append(target)


# ------------------------------------------------- 策略层: 不做动作的两种情形


def test_no_enabled_game_and_an_empty_queue_produce_no_action() -> None:
    """库里没人启用、队列也空着: 没有可接管的对象, 判断必须是"什么都不做"."""
    games = [Game(id=1, name="Demo")]

    decision = QueueActivation().decide(
        games,
        monitorable={1},
        state=ActivationState(),
        queue=(),
        observation=GameProbe(checked=True),
    )

    assert decision.reason == REASON_IDLE
    assert decision.state.monitor_game_id is None
    assert decision.target_game_id is None
    assert decision.deactivate is False


def test_a_released_monitor_leaves_the_enabled_state_alone_when_it_is_not_ours() -> (
    None
):
    """监控对象退出、而启用态早已不是它(用户改过): 清掉监控对象即可, 不发停用动作."""
    games = [Game(id=1, name="Demo")]

    decision = QueueActivation().decide(
        games,
        monitorable={1},
        state=ActivationState(monitor_game_id=1, armed=True),
        queue=(),
        observation=GameProbe(checked=True),
    )

    assert decision.reason == REASON_IDLE
    assert decision.state.monitor_game_id is None
    assert decision.deactivate is False


# ------------------------------------------------------------- 用例层的失败路径


def test_taking_the_enabled_state_back_with_nobody_enabled_is_no_change(
    tmp_path: Path,
) -> None:
    """策略要求收回启用态、而库里本来就没有启用对象: 不算一次变化, 也不报错."""
    database = migrated_database(tmp_path)
    decision = games_cases.ActivationDecision(state=ActivationState(), deactivate=True)

    outcome = games_cases.apply_activation(database, _FixedPolicy(decision))

    assert outcome.disabled is None
    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() is None


def test_suggesting_for_an_unknown_game_never_probes(tmp_path: Path) -> None:
    """给库里没有的游戏探测存档: 报"未知游戏", 一次都不去问候选来源."""
    database = migrated_database(tmp_path)
    source = _RecordingSource()

    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        suggest_candidates(database, 999, source, platform="steam")

    assert source.requests == []


def test_another_games_imported_result_is_not_taken_as_the_install_dir(
    tmp_path: Path,
) -> None:
    """省略安装目录时回查**本游戏**已导入的探测结果; 别的游戏那条不参与."""
    database = migrated_database(tmp_path)
    repository = GameRepository(database)
    other = repository.add(Game(name="Other", steam_app_id=1))
    target = repository.add(Game(name="Demo", steam_app_id=730))
    assert other.id is not None
    assert target.id is not None
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Other",
            install_dir=str(tmp_path / "Games" / "Other"),
            game_id=other.id,
            status="imported",
        )
    )
    source = _RecordingSource()

    suggest_candidates(database, target.id, source, platform="steam")

    assert source.requests == [("730", None)], "别的游戏的安装目录不能被当成它的"


def test_importing_a_candidate_under_an_existing_name_is_refused(
    tmp_path: Path,
) -> None:
    """同名拦截: 库里已有同名游戏时拒绝导入, 否则会多出一款同名游戏."""
    database = migrated_database(tmp_path)
    install = tmp_path / "Hades"
    install.mkdir()
    GameRepository(database).add(Game(name="哈迪斯"))
    stored, _created = CandidateRepository(database).upsert(
        GameCandidate(name="Hades", install_dir=str(install), source="manual")
    )
    assert stored.id is not None

    with pytest.raises(ArchiveManagementError, match="同名游戏"):
        import_candidate(database, stored.id, name="哈迪斯")


def test_recording_an_origin_is_visible_on_the_record(tmp_path: Path) -> None:
    """记录来源平台要真的落到记录上(自动导入时用它判断"这已经是同一款了")."""
    database = migrated_database(tmp_path)
    game_id = add_game(database, "Demo")

    set_origin(database, game_id, "steam")

    stored = GameRepository(database).get(game_id)
    assert stored is not None
    assert stored.origin == "steam"


def test_a_delete_that_fails_keeps_the_node_and_writes_a_failure_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_log: list[str]
) -> None:
    """删除中途失败: 已有节点原样保留, 异常抛出去, 并留下一条失败审计."""
    database = migrated_database(tmp_path)
    save = make_save_folder(tmp_path)
    game_id = add_game(database, "Demo", path=save)
    backups = BackupService(database, backup_root=tmp_path / "backups")
    node = backups.create_backup(game_id)

    def refuse(_path: Path) -> None:
        raise OSError("磁盘忙")

    monkeypatch.setattr(backup_mod, "remove_snapshot", refuse)

    with pytest.raises(OSError, match="磁盘忙"):
        backups.delete_node(game_id, node.id or 0)

    assert [item.id for item in backups.list_nodes(game_id)] == [node.id]
    assert "backup.delete" in " ".join(audit_log)


def test_a_database_failure_after_the_trash_move_is_logged_and_reraised(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_log: list[str]
) -> None:
    """目录已经进回收站、落库却失败: 抛出去并留下失败审计, 不能当成删成功."""
    database = migrated_database(tmp_path)
    save = make_save_folder(tmp_path)
    game_id = add_game(database, "Demo", path=save)
    location = SaveLocationRepository(database).list_for_game(game_id)[0]
    assert location.id is not None
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    repository = SaveLocationRepository(database)

    def refuse(_location_id: int) -> None:
        raise DatabaseError("数据库忙")

    monkeypatch.setattr(repository, "delete", refuse)
    monkeypatch.setattr(locations_mod, "SaveLocationRepository", lambda _db: repository)

    with pytest.raises(DatabaseError, match="数据库忙"):
        remove_save_location(
            database, location.id, confirm_name="Demo", trash=_Trash(trash_dir)
        )

    assert "location.delete" in " ".join(audit_log)
