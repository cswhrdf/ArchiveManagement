"""按进程自动启停的单元测试(PLAN 阶段 G-8).

自动启停的失败模式是"切错游戏": 启用态决定快捷键与定时备份落到哪一款游戏上,
因此用例的重点在"什么情况下必须什么都不做" —— 无法确认是否在运行、名字归不到
唯一的一款、从没见过它运行、用户刚手动调整过, 都不允许产生动作。所有用例都注入
假进程表, 一次也不枚举真实进程。
"""

from __future__ import annotations

from collections.abc import Sequence, Set
from pathlib import Path

import pytest

from archive_management.application import activation as activation_cases
from archive_management.application import games as games_cases
from archive_management.application.home import set_archived
from archive_management.domain import ActivationState, Game, SaveLocation
from archive_management.domain.activation import (
    ACTIVATION_DELAY_LADDER,
    REASON_DISABLED,
    REASON_ENABLED,
    REASON_FALLBACK,
    REASON_IDLE,
    REASON_NO_GAMES,
    REASON_OFF,
    REASON_PAUSED,
    REASON_RUNNING,
    REASON_UNCHECKED,
    RunEntry,
    activation_delay,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    ActivationQueueRepository,
    ActivationStateRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.processes import GameProbe
from helpers import migrated_database

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("游戏启停"),
    pytest.mark.story("按进程自动启停"),
    pytest.mark.layer("unit"),
]


class _Processes:
    """可数的假进程表: 记录被枚举过几次, 便于断言"关掉就一次都不枚举"."""

    def __init__(self, *names: str, broken: bool = False) -> None:
        """按给定的"正在运行"的进程名构造(``broken`` 模拟枚举失败)."""
        self.names = list(names)
        self.broken = broken
        self.calls = 0

    def __call__(self) -> list[str]:
        """返回当前进程名; ``broken`` 时抛出异常."""
        self.calls += 1
        if self.broken:
            raise OSError("无法枚举进程")
        return list(self.names)


def _database(tmp_path: Path) -> Database:
    return migrated_database(tmp_path)


def _game(
    database: Database,
    name: str,
    *,
    enabled: bool = False,
    original: str = "",
    save_paths: bool = True,
) -> int:
    """插入一个游戏并返回 id; ``enabled`` 为真时走真实的手动启用入口.

    ``save_paths`` 默认为真(补一条存档位置): 没有关联存档位置的游戏不参与自动
    启停监控, 因此除专门验证这一点的用例外都要给上。
    """
    repository = GameRepository(database)
    created = repository.add(Game(name=name, original_name=original or name))
    assert created.id is not None
    if save_paths:
        SaveLocationRepository(database).add(
            SaveLocation(game_id=created.id, path=f"C:/Saves/{created.id}")
        )
    if enabled:
        games_cases.set_enabled(database, created.id, True, manual=True)
    return created.id


def _state(database: Database) -> ActivationState:
    return ActivationStateRepository(database).load()


def _queue(database: Database) -> tuple[int, ...]:
    """返回队列里的游戏 id(按启动顺序)."""
    return tuple(entry.game_id for entry in ActivationQueueRepository(database).load())


def test_an_empty_library_never_looks_at_the_process_table(tmp_path: Path) -> None:
    """库里没有任何游戏时: 一次进程表都不枚举, 队列保持为空."""
    database = _database(tmp_path)
    provider = _Processes("Idle.exe")

    outcome = activation_cases.poll_activation(database, provider=provider)

    assert provider.calls == 0
    assert outcome.reason == REASON_NO_GAMES
    assert outcome.changed is False
    assert outcome.monitor is None
    assert outcome.queue == ()
    assert _queue(database) == ()


def test_manual_enable_records_the_monitored_game(tmp_path: Path) -> None:
    """手动启用会把这一款记成监控对象, 但还不算"见过它运行"."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)

    state = _state(database)

    assert state.monitor_game_id == game_id
    assert state.armed is False
    assert state.paused is False


def test_a_game_that_never_ran_is_left_enabled(tmp_path: Path) -> None:
    """刚启用、游戏还没起来(或今天没打算玩)时, 不能当成"已经退出"."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    provider = _Processes("explorer.exe")

    outcome = activation_cases.poll_activation(database, provider=provider)

    assert provider.calls == 1
    assert outcome.reason == REASON_IDLE
    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() == game_id


def test_seeing_the_game_run_arms_the_tracking(tmp_path: Path) -> None:
    """观察到它运行: 记下 armed, 启用态保持不变."""
    database = _database(tmp_path)
    game_id = _game(database, "Outer Wilds", enabled=True)

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("OuterWilds.exe")
    )

    assert outcome.reason == REASON_RUNNING
    assert outcome.changed is False
    assert _state(database).armed is True
    assert GameRepository(database).enabled_game_id() == game_id


def test_leaving_the_game_releases_the_activation(tmp_path: Path) -> None:
    """见过它运行之后不再运行: 停用并复位 armed."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))

    outcome = activation_cases.poll_activation(database, provider=_Processes())

    assert outcome.reason == REASON_DISABLED
    assert outcome.disabled is not None
    assert outcome.disabled.id == game_id
    assert GameRepository(database).enabled_game_id() is None
    assert _state(database).armed is False


def test_a_tracked_game_that_starts_again_is_enabled_automatically(
    tmp_path: Path,
) -> None:
    """自动停用之后不必再手动启用一遍: 它重新启动就该被接管."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))
    activation_cases.poll_activation(database, provider=_Processes())
    assert GameRepository(database).enabled_game_id() is None

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe")
    )

    assert outcome.reason == REASON_ENABLED
    assert outcome.enabled is not None
    assert outcome.enabled.id == game_id
    assert GameRepository(database).enabled_game_id() == game_id


def test_an_unconfirmed_probe_keeps_everything_as_is(tmp_path: Path) -> None:
    """无法确认是否在运行时不能下任何结论: 不切换, armed 也不变."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))

    outcome = activation_cases.poll_activation(
        database, provider=_Processes(broken=True)
    )

    assert outcome.reason == REASON_UNCHECKED
    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() == game_id
    assert _state(database).armed is True


def test_names_that_cannot_match_a_process_are_not_probed(tmp_path: Path) -> None:
    """全是中文或过短的名字匹配不上任何进程: 连进程表都不枚举."""
    database = _database(tmp_path)
    _game(database, "星露谷物语", enabled=True)
    provider = _Processes("StardewValley.exe")

    outcome = activation_cases.poll_activation(database, provider=provider)

    assert provider.calls == 0
    assert outcome.reason == REASON_UNCHECKED
    assert outcome.changed is False


def test_the_original_name_is_used_for_matching(tmp_path: Path) -> None:
    """界面显示译名, 进程名是英文: 靠录入时识别到的原始名称匹配."""
    database = _database(tmp_path)
    _game(database, "星露谷物语", enabled=True, original="Stardew Valley")
    provider = _Processes("StardewValley.exe")

    outcome = activation_cases.poll_activation(database, provider=provider)

    assert provider.calls == 1
    assert outcome.reason == REASON_RUNNING
    assert _state(database).armed is True


def test_a_manual_stop_is_not_undone_while_the_game_keeps_running(
    tmp_path: Path,
) -> None:
    """用户手动停用之后, 只要游戏还在运行就不能把它启用回来."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))
    games_cases.set_enabled(database, game_id, False, manual=True)

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe")
    )

    assert outcome.reason == REASON_PAUSED
    assert outcome.changed is False
    assert _state(database).paused is True
    assert GameRepository(database).enabled_game_id() is None


def test_the_pause_is_released_once_every_game_exited(tmp_path: Path) -> None:
    """手动停用带来的暂停只在所有观察到的游戏都退出后解除, 再启动就能被接管."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    games_cases.set_enabled(database, game_id, False, manual=True)

    released = activation_cases.poll_activation(database, provider=_Processes())
    assert released.reason == REASON_IDLE
    assert _state(database).paused is False

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe")
    )

    assert outcome.reason == REASON_ENABLED
    assert GameRepository(database).enabled_game_id() == game_id


def test_switching_to_another_game_restarts_the_tracking(tmp_path: Path) -> None:
    """换成另一款之后只跟新的那一款: 旧的那款在运行也不再管."""
    database = _database(tmp_path)
    _game(database, "First", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("First.exe"))
    second_id = _game(database, "Second", enabled=True)

    state = _state(database)
    assert state.monitor_game_id == second_id
    assert state.armed is False

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("First.exe")
    )

    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() == second_id


def test_an_archived_game_leaves_the_queue(tmp_path: Path) -> None:
    """归档之后不再监控它: 状态清零, 队列里也不再有它, 也不再枚举进程表."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))
    set_archived(database, game_id, True)
    provider = _Processes("Demo.exe")

    outcome = activation_cases.poll_activation(database, provider=provider)

    assert provider.calls == 0
    assert outcome.reason == REASON_NO_GAMES
    assert _state(database) == ActivationState()
    assert _queue(database) == ()


def test_the_feature_switch_off_never_probes(tmp_path: Path) -> None:
    """设置里的开关关着时: 连状态都不读, 一次进程表都不枚举."""
    database = _database(tmp_path)
    _game(database, "Demo", enabled=True)
    provider = _Processes("Demo.exe")

    outcome = activation_cases.poll_activation(
        database, enabled=False, provider=provider
    )

    assert provider.calls == 0
    assert outcome.reason == REASON_OFF
    assert outcome.changed is False


def test_every_poll_is_audited_for_diagnosis(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """每次判断都记一条基础操作审计: 排查"为什么没跟着切"时这就是全部依据."""
    database = _database(tmp_path)
    _game(database, "Demo", enabled=True)

    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))

    assert any("game.auto_activation" in line for line in audit_log)


def test_an_unknown_target_is_rejected(tmp_path: Path) -> None:
    """策略给出不存在的游戏要显式报错, 而不是静默什么都不做."""
    database = _database(tmp_path)
    _game(database, "Demo", enabled=True)

    class _BadPolicy:
        """返回不存在游戏的假策略."""

        def decide(
            self,
            games: Sequence[Game],
            *,
            monitorable: Set[int],
            state: ActivationState,
            queue: Sequence[RunEntry],
            observation: GameProbe,
            now: str = "",
        ) -> games_cases.ActivationDecision:
            """固定返回一个不存在的游戏 id."""
            return games_cases.ActivationDecision(state=state, target_game_id=9999)

    with pytest.raises(ArchiveManagementError):
        games_cases.apply_activation(database, _BadPolicy())


def test_the_stored_state_survives_a_reopen(tmp_path: Path) -> None:
    """状态要真的落库: 换了连接(相当于重启软件)之后仍然是同一份."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))

    reopened = Database(tmp_path / "app.db")

    assert _state(reopened).monitor_game_id == game_id
    assert _state(reopened).armed is True


def test_a_state_from_another_version_is_ignored(tmp_path: Path) -> None:
    """版本不符时按"没有状态"处理: 读不到状态也不该报错."""
    database = _database(tmp_path)
    repository = ActivationStateRepository(database)
    repository.save(ActivationState(armed=True))
    with database.session() as connection:
        connection.execute("UPDATE activation_state SET version = 99 WHERE id = 1")

    assert repository.load() == ActivationState()


def test_a_game_already_running_is_taken_over_on_the_first_poll(tmp_path: Path) -> None:
    """软件启动(或打开开关)时已有游戏在运行: 立刻接管."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo")

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe")
    )

    assert outcome.reason == REASON_ENABLED
    assert outcome.enabled is not None
    assert outcome.enabled.id == game_id
    assert _state(database).armed is True
    assert _queue(database) == (game_id,)


def test_a_second_launch_does_not_steal_the_activation(tmp_path: Path) -> None:
    """接管之后其他游戏启动不再抢: 只入队, 不切换."""
    database = _database(tmp_path)
    first_id = _game(database, "First")
    second_id = _game(database, "Second")
    activation_cases.poll_activation(database, provider=_Processes("First.exe"))
    assert _queue(database) == (first_id,)

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe")
    )

    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() == first_id
    assert _queue(database) == (first_id, second_id)


def test_an_enabled_game_that_is_running_is_claimed(tmp_path: Path) -> None:
    """状态丢失(例如升级)后: 已在运行的**启用中**那一款被认领, 不切到队首那一款."""
    database = _database(tmp_path)
    first_id = _game(database, "First")
    second_id = _game(database, "Second")
    activation_cases.poll_activation(database, provider=_Processes("First.exe"))
    games_cases.set_enabled(database, second_id, True, manual=True)
    with database.session() as connection:
        connection.execute("DELETE FROM activation_state")

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe")
    )

    assert outcome.reason == REASON_RUNNING
    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() == second_id
    assert _state(database).monitor_game_id == second_id
    assert first_id != second_id


def test_the_first_still_running_entry_takes_over(tmp_path: Path) -> None:
    """监控对象退出后回落到**队首**仍在运行的那款, 不是最近启动的那款."""
    database = _database(tmp_path)
    first_id = _game(database, "First")
    second_id = _game(database, "Second")
    third_id = _game(database, "Third")
    activation_cases.poll_activation(database, provider=_Processes("First.exe"))
    activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe", "Third.exe")
    )

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Second.exe", "Third.exe")
    )

    assert outcome.reason == REASON_FALLBACK
    assert outcome.enabled is not None
    assert outcome.enabled.id == second_id
    assert GameRepository(database).enabled_game_id() == second_id
    assert _queue(database) == (second_id, third_id)
    assert first_id not in _queue(database)
    assert third_id != second_id  # 不是最近启动的那款


def test_games_seen_in_the_same_round_are_queued_by_id(tmp_path: Path) -> None:
    """同一轮里同时观察到多款启动: 按游戏 id 升序入队(确定、可测)."""
    database = _database(tmp_path)
    first_id = _game(database, "First")
    second_id = _game(database, "Second")

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Second.exe", "First.exe")
    )

    assert _queue(database) == (first_id, second_id)
    assert outcome.reason == REASON_ENABLED
    assert GameRepository(database).enabled_game_id() == first_id


def test_everything_exited_releases_the_activation(tmp_path: Path) -> None:
    """所有游戏都退出: 停用当前启用态, 队列与状态一起清空."""
    database = _database(tmp_path)
    _game(database, "First")
    _game(database, "Second")
    activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe")
    )

    outcome = activation_cases.poll_activation(database, provider=_Processes())

    assert outcome.disabled is not None
    assert GameRepository(database).enabled_game_id() is None
    assert _state(database) == ActivationState()
    assert _queue(database) == ()


def test_a_manually_enabled_game_is_not_stolen_by_a_running_one(
    tmp_path: Path,
) -> None:
    """手动启用一款还没运行的游戏: 队列里正在运行的别的游戏不会把它顶掉."""
    database = _database(tmp_path)
    running_id = _game(database, "Running")
    picked_id = _game(database, "Picked")
    activation_cases.poll_activation(database, provider=_Processes("Running.exe"))
    games_cases.set_enabled(database, picked_id, True, manual=True)

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Running.exe")
    )

    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() == picked_id
    assert _state(database).monitor_game_id == picked_id
    assert running_id != picked_id


def test_a_manual_enable_resumes_the_takeover(tmp_path: Path) -> None:
    """手动启用任意一款都会解除"本批次暂停", 自动接管随即恢复."""
    database = _database(tmp_path)
    first_id = _game(database, "First", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("First.exe"))
    games_cases.set_enabled(database, first_id, False, manual=True)
    assert _state(database).paused is True

    second_id = _game(database, "Second", enabled=True)

    assert _state(database).paused is False
    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Second.exe")
    )
    assert outcome.changed is False
    assert _state(database).armed is True
    assert second_id != first_id


def test_a_suppressed_game_is_skipped_when_falling_back(tmp_path: Path) -> None:
    """被手动停用过的那一款还在运行时, 回落要跳过它."""
    database = _database(tmp_path)
    first_id = _game(database, "First")
    second_id = _game(database, "Second")
    activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe")
    )
    # 手动停用第二款(它不是监控对象): 只抑制它自己, 不进"本批次暂停"
    games_cases.set_enabled(database, second_id, False, manual=True)
    assert _state(database).paused is False

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe")
    )
    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() == first_id

    released = activation_cases.poll_activation(
        database, provider=_Processes("Second.exe")
    )
    assert released.disabled is not None
    assert GameRepository(database).enabled_game_id() is None


def test_a_suppressed_game_is_released_once_it_exits(tmp_path: Path) -> None:
    """抑制随那一行一起消失: 它退出之后再启动它, 就能被自动接管."""
    database = _database(tmp_path)
    _game(database, "First")
    second_id = _game(database, "Second")
    activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe")
    )
    games_cases.set_enabled(database, second_id, False, manual=True)

    activation_cases.poll_activation(database, provider=_Processes())

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Second.exe")
    )

    assert outcome.reason == REASON_ENABLED
    assert GameRepository(database).enabled_game_id() == second_id


def test_identical_names_are_reported_as_conflicts(tmp_path: Path) -> None:
    """两款游戏归一化后同名: 那个针整体丢弃(记进 conflicts), 别的游戏照常判断."""
    database = _database(tmp_path)
    _game(database, "Demo")
    _game(database, "demo")
    other_id = _game(database, "Other")

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe", "Other.exe")
    )

    assert outcome.conflicts == ("demo",)
    assert outcome.reason == REASON_ENABLED
    assert GameRepository(database).enabled_game_id() == other_id
    assert _queue(database) == (other_id,)


def test_a_process_name_matching_two_games_is_dropped(tmp_path: Path) -> None:
    """一个进程名同时指向两款游戏时整条丢弃: 宁可不判断, 也不能切错游戏."""
    database = _database(tmp_path)
    game_id = _game(database, "Wilds", enabled=True)
    _game(database, "Outer Wilds")

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("OuterWilds.exe")
    )

    assert outcome.reason == REASON_IDLE
    assert outcome.changed is False
    assert _queue(database) == ()
    assert GameRepository(database).enabled_game_id() == game_id


def test_the_queue_keeps_the_first_seen_time(tmp_path: Path) -> None:
    """首次观察时间不随轮询变化, 最近一次探测时间每轮更新."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo")
    activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe"), now="2026-01-01T00:00:00Z"
    )

    activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe"), now="2026-01-02T00:00:00Z"
    )

    entry = ActivationQueueRepository(database).load()[0]
    assert entry.first_seen_at == "2026-01-01T00:00:00Z"
    assert entry.last_seen_at == "2026-01-02T00:00:00Z"
    assert entry.game_id == game_id
    assert entry.position == 1


def test_the_queue_order_survives_a_reopen(tmp_path: Path) -> None:
    """队列顺序必须跨重启保留: 它是回落的唯一依据."""
    database = _database(tmp_path)
    first_id = _game(database, "First")
    second_id = _game(database, "Second")
    activation_cases.poll_activation(database, provider=_Processes("First.exe"))
    activation_cases.poll_activation(
        database, provider=_Processes("First.exe", "Second.exe")
    )

    reopened = Database(tmp_path / "app.db")

    assert _queue(reopened) == (first_id, second_id)
    assert _state(reopened).monitor_game_id == first_id


def test_a_game_without_save_locations_is_not_monitored(tmp_path: Path) -> None:
    """没有关联存档位置的游戏不加入监控: 不接管, 也不为它枚举进程表."""
    database = _database(tmp_path)
    _game(database, "Pending", save_paths=False)
    provider = _Processes("Pending.exe")

    outcome = activation_cases.poll_activation(database, provider=provider)

    assert provider.calls == 0
    assert outcome.reason == REASON_NO_GAMES
    assert outcome.changed is False
    assert _queue(database) == ()


def test_an_enabled_game_without_locations_blocks_the_takeover(
    tmp_path: Path,
) -> None:
    """用户启用了没有存档位置的那一款: 自动启停保持现状, 不改成别的游戏."""
    database = _database(tmp_path)
    _game(database, "Pending", enabled=True, save_paths=False)
    running_id = _game(database, "Running")

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Pending.exe", "Running.exe")
    )

    assert outcome.changed is False
    assert GameRepository(database).enabled_game_id() != running_id
    assert _state(database) == ActivationState()
    # 可监控的那款仍然会被登记进队列(它只是不该被接管)。
    assert _queue(database) == (running_id,)


def test_losing_the_save_locations_removes_it_from_monitoring(
    tmp_path: Path,
) -> None:
    """本来在监控的游戏失去存档位置后: 离开队列, 也不再被跟踪."""
    database = _database(tmp_path)
    game_id = _game(database, "Demo", enabled=True)
    activation_cases.poll_activation(database, provider=_Processes("Demo.exe"))
    assert _queue(database) == (game_id,)
    with database.session() as connection:
        connection.execute("DELETE FROM save_locations WHERE game_id = ?", (game_id,))

    outcome = activation_cases.poll_activation(
        database, provider=_Processes("Demo.exe")
    )

    assert outcome.reason == REASON_NO_GAMES
    assert _queue(database) == ()
    assert _state(database) == ActivationState()


def test_the_probe_interval_slows_down_while_a_game_runs() -> None:
    """阶梯: 队列为空用最快档, 有游戏在运行时逐档放慢到上限."""
    fast = ACTIVATION_DELAY_LADDER[0]

    assert activation_delay(0, running=False) == fast
    assert activation_delay(9, running=False) == fast
    assert activation_delay(0, running=True) == fast
    assert activation_delay(4, running=True) == ACTIVATION_DELAY_LADDER[-1]
    assert activation_delay(99, running=True) == ACTIVATION_DELAY_LADDER[-1]
