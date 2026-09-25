"""自动启停策略: 监控全部已导入游戏, 按"启动顺序队列"接管/回落(见 PLAN 的 G-8).

判断需要三样事实: 这一次枚举出的**运行集合**(一次枚举, 按唯一归属分组)、落库的
**状态**(监控对象 / 见过它运行吗 / 手动暂停)与落库的**队列**(先后被观察到启动的
顺序)。策略本身是纯函数, 出入参都是值对象, 用例注入假进程表就能覆盖全部分支。

**这个功能的失败模式是"切错游戏"**: 启用态决定快捷键与定时备份落到哪一款游戏上,
所以拿不准时一律不动 —— 无法确认是否在运行、名字匹配不上、从没见过它运行、用户
刚手动调整过, 都不产生动作。接管只发生在"库里没有我们维护的监控对象"时, 回落只
发生在"监控对象确认退出"之后: **已经接管之后, 其他游戏启动不会把它抢走**。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass

from archive_management.application.games import (
    ActivationDecision,
    ActivationOutcome,
    apply_activation,
)
from archive_management.domain import ActivationState, Game, RunEntry
from archive_management.domain.activation import (
    REASON_DISABLED,
    REASON_ENABLED,
    REASON_FALLBACK,
    REASON_IDLE,
    REASON_NO_GAMES,
    REASON_OFF,
    REASON_PAUSED,
    REASON_RUNNING,
    REASON_UNCHECKED,
    first_candidate,
    maintain_queue,
    reindex,
)
from archive_management.infrastructure.database import Database
from archive_management.services.processes import GameProbe, ProcessNameProvider


@dataclass
class QueueActivation:
    """默认策略: 按启动顺序队列接管/回落, 队列空了才把启用态收回去."""

    def decide(
        self,
        games: Sequence[Game],
        *,
        monitorable: Set[int],
        state: ActivationState,
        queue: Sequence[RunEntry],
        observation: GameProbe,
        now: str = "",
    ) -> ActivationDecision:
        """按"谁在运行 / 谁被监控 / 手动调整过吗"给出这一次的判断."""
        managed = _managed_games(games, monitorable)
        if not managed:
            return ActivationDecision(
                state=ActivationState(), queue=(), reason=REASON_NO_GAMES
            )
        # 队列里可能残留已归档、已删或已不在监控范围的游戏: 先滤掉再维护。
        entries = maintain_queue(
            reindex(entry for entry in queue if entry.game_id in managed),
            running_ids=observation.running_ids,
            now=now,
        )
        if state.paused:
            return self._paused(entries)
        monitor = state.monitor_game_id if state.monitor_game_id in managed else None
        if not observation.checked:
            return ActivationDecision(
                state=ActivationState(monitor_game_id=monitor, armed=state.armed),
                queue=entries,
                reason=REASON_UNCHECKED,
            )
        running = set(observation.running_ids)
        if monitor is None:
            return self._take_over(games, managed, entries, running)
        return self._with_monitor(
            games, entries, monitor, armed=state.armed, running=running
        )

    def _with_monitor(
        self,
        games: Sequence[Game],
        entries: tuple[RunEntry, ...],
        monitor: int,
        *,
        armed: bool,
        running: set[int],
    ) -> ActivationDecision:
        """已有监控对象: 仍在运行就保持, 确认退出才回落或把启用态收回去."""
        if monitor in running:
            return ActivationDecision(
                state=ActivationState(monitor_game_id=monitor, armed=True),
                queue=entries,
                reason=REASON_RUNNING,
            )
        if not armed:
            # 刚启用、游戏还在启动中(或今天根本没打算玩): 既不停用也不回落。
            return ActivationDecision(
                state=ActivationState(monitor_game_id=monitor),
                queue=entries,
                reason=REASON_IDLE,
            )
        return self._released_or_fall_back(games, entries, monitor)

    @staticmethod
    def _paused(entries: tuple[RunEntry, ...]) -> ActivationDecision:
        """手动暂停期间只维护队列; 所有观察到的游戏都退出后解除暂停."""
        if entries:
            return ActivationDecision(
                state=ActivationState(paused=True), queue=entries, reason=REASON_PAUSED
            )
        return ActivationDecision(
            state=ActivationState(), queue=entries, reason=REASON_IDLE
        )

    @staticmethod
    def _take_over(
        games: Sequence[Game],
        managed: Mapping[int, Game],
        entries: tuple[RunEntry, ...],
        running: set[int],
    ) -> ActivationDecision:
        """没有监控对象时: 先认领"已经启用着"的那一款, 否则接管队首.

        "已经启用着"优先是**手动优先**的推论: 用户手选的启用对象不该被队列里另一款
        顶掉(哪怕它当前没在运行)。它若不在监控范围内(没有存档位置), 就保持现状 ——
        既不认领, 也不接管别的游戏。
        """
        current = _enabled_game(games)
        if current is not None:
            tracked = current.id in managed
            return ActivationDecision(
                state=ActivationState(
                    monitor_game_id=current.id if tracked else None,
                    armed=tracked and current.id in running,
                ),
                queue=entries,
                reason=REASON_RUNNING,
            )
        target = first_candidate(entries)
        if target is None:
            return ActivationDecision(
                state=ActivationState(), queue=entries, reason=REASON_IDLE
            )
        return ActivationDecision(
            state=ActivationState(monitor_game_id=target, armed=True),
            queue=entries,
            target_game_id=target,
            reason=REASON_ENABLED,
        )

    @staticmethod
    def _released_or_fall_back(
        games: Sequence[Game], entries: tuple[RunEntry, ...], monitor: int
    ) -> ActivationDecision:
        """监控对象已确认退出: 回落到队首, 没人可接就把启用态收回去."""
        target = first_candidate(entries)
        if target is not None:
            return ActivationDecision(
                state=ActivationState(monitor_game_id=target, armed=True),
                queue=entries,
                target_game_id=target,
                reason=REASON_FALLBACK,
            )
        current = next((game for game in games if game.id == monitor), None)
        if current is None or not current.enabled:
            # 启用态早已经不是它了(用户改过): 只把监控对象清掉, 不动启用态。
            return ActivationDecision(
                state=ActivationState(), queue=entries, reason=REASON_IDLE
            )
        return ActivationDecision(
            state=ActivationState(),
            queue=entries,
            deactivate=True,
            reason=REASON_DISABLED,
        )


def _managed_games(games: Sequence[Game], monitorable: Set[int]) -> dict[int, Game]:
    """参与监控的游戏(未归档 + 有关联存档位置): 其余一概不进队列也不被接管.

    没有存档位置的游戏是"待处理"状态: 它没有可备份的内容, 接管它既不能定时备份也
    没有意义, 因此排除在监控范围之外(见 PLAN 的 G-8)。
    """
    managed: dict[int, Game] = {}
    for game in games:
        if game.id is None or game.archived or game.id not in monitorable:
            continue
        managed[game.id] = game
    return managed


def _enabled_game(games: Sequence[Game]) -> Game | None:
    """库里处于启用态的那一款(未归档): 手动优先要据此保护用户的选择."""
    return next(
        (
            game
            for game in games
            if game.id is not None and game.enabled and not game.archived
        ),
        None,
    )


def poll_activation(
    database: Database,
    *,
    enabled: bool = True,
    provider: ProcessNameProvider | None = None,
    now: str | None = None,
) -> ActivationOutcome:
    """低频轮询一次自动启停(界面定时器与测试都从这里进).

    ``enabled=False``(设置里的开关关着)时**连状态都不读**: 关掉就该是完全不存在,
    而不是"读了状态、探测一圈、再决定不动手"。
    """
    if not enabled:
        return ActivationOutcome(state=ActivationState(), reason=REASON_OFF)
    return apply_activation(database, QueueActivation(), provider=provider, now=now)


__all__ = ["QueueActivation", "poll_activation"]
