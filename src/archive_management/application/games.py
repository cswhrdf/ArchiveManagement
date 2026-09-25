"""游戏生命周期: 删除、启用/停用, 以及"当前启用哪一款"的规则.

同一时刻只允许一款游戏处于启用态(快捷键与定时备份只对它生效), 因此所有启用
入口都收敛到 :func:`set_enabled` —— 它会先停用其它游戏再启用目标, 并拒绝启用
已归档的游戏。

按"监控中的游戏是否在运行"自动切换启用态由 :func:`apply_activation` 完成:
策略(:class:`ActivationPolicy`)只判断"该接管谁 / 该回到谁 / 该收回 / 保持不变",
执行仍然走 :func:`set_enabled`, 所以"全局至多一款启用"始终只有一处实现。默认策略
:class:`ManualActivation` 从不改变状态, 真实策略见
:mod:`archive_management.application.activation`。
"""

from __future__ import annotations

from collections.abc import Sequence, Set
from dataclasses import dataclass, replace
from typing import Protocol

from archive_management.domain import (
    ActivationState,
    Game,
    RunEntry,
    build_name_ownership,
    move_to_front,
    suppress_entry,
)
from archive_management.domain.activation import REASON_MANUAL
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database, iso_utc_now
from archive_management.infrastructure.repository import (
    ActivationQueueRepository,
    ActivationStateRepository,
    CandidateRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.audit import log_action
from archive_management.services.processes import (
    UNCHECKED_GAME_PROBE,
    GameProbe,
    ProcessNameProvider,
    probe_games,
)

DELETE_ACTION = "game.delete"
ENABLE_ACTION = "game.set_enabled"
RELEASE_ACTION = "discovery.candidates_released"
# 自动启停的每一次判断都记审计(基础操作): 默认不落盘, 打开调试日志后才写,
# 排查"为什么没跟着切"时这份记录就是全部依据。
ACTIVATION_ACTION = "game.auto_activation"


@dataclass(frozen=True)
class GameDeletion:
    """删除游戏的结果(含退回待处理的探测候选数量)."""

    game: Game
    released_candidates: int


@dataclass(frozen=True)
class EnableResult:
    """启用/停用的结果: 本游戏 + 被自动停用的那一款(没有则为 None)."""

    game: Game
    replaced: Game | None = None


def delete_game(database: Database, game_id: int) -> GameDeletion:
    """删除游戏, 并把它带来的探测候选退回待处理.

    顺序不能颠倒: ``game_candidates.game_id`` 是 ``ON DELETE SET NULL``, 游戏删掉
    之后就再也定位不到这批候选 —— 它们会停在"已导入"却指向任何游戏, 用户既不能
    重新导入也看不到它们。存档位置、备份节点与定时任务由外键级联删除。
    """
    game = _require_game(database, game_id)
    released = CandidateRepository(database).release_imported(game_id)
    GameRepository(database).delete(game_id)
    log_action(DELETE_ACTION, game_id=game_id, name=game.name, released=released)
    if released:
        log_action(RELEASE_ACTION, game_id=game_id, count=released)
    return GameDeletion(game=game, released_candidates=released)


def set_enabled(
    database: Database, game_id: int, enabled: bool, *, manual: bool = False
) -> EnableResult:
    """启用/停用一款游戏; 启用会先停用其它游戏(全局至多一款启用).

    归档的游戏不允许启用(归档只保留"删除/导出/取消归档/打开详情"), 取消归档也
    不会顺手启用: 由用户明确指定"当前在玩的是哪一款"。

    ``manual=True`` 表示这是用户自己在选"现在玩哪一款": 切换之外还要把这次选择记进
    自动启停状态(见 :func:`_record_manual`), 自动判断据此让手动决定优先。
    """
    game = _require_game(database, game_id)
    if enabled and game.archived:
        raise ArchiveManagementError(f"「{game.name}」已归档, 不能启用: 请先取消归档")
    repository = GameRepository(database)
    replaced_ids = repository.set_enabled(game_id, enabled)
    replaced = _first_game(repository, replaced_ids)
    updated = repository.get(game_id)
    if updated is None:  # pragma: no cover - 刚写入的行必然可读
        raise ArchiveManagementError(f"未知游戏: {game_id}")
    log_action(
        ENABLE_ACTION,
        game_id=game_id,
        enabled=enabled,
        manual=manual,
        replaced_id=None if replaced is None else replaced.id,
    )
    if manual:
        _record_manual(database, game_id, enabled)
    return EnableResult(game=updated, replaced=replaced)


def active_game(database: Database) -> Game | None:
    """返回当前启用的游戏; 没有启用任何游戏时返回 None."""
    repository = GameRepository(database)
    game_id = repository.enabled_game_id()
    return None if game_id is None else repository.get(game_id)


def _record_manual(database: Database, game_id: int, enabled: bool) -> None:
    """记下用户的手动启停选择(状态 + 队列).

    启用: 监控对象换成它、armed 复位(还没见它运行)、解除本批次的暂停, 并在队列里
    把它移到队首 —— 手动选的最有资格。它没起来之前, 队列里别的游戏**不会**把它
    顶掉(armed=False 让接管与回落都不触发)。

    停用: 该款进"抑制"(只要它还在运行就绝不被自动启用, 观察到它退出即随行解除);
    停用的若是当前监控对象, 再进入**本批次暂停** —— 否则下一轮就会回落到队列里
    另一款, 用户会觉得停用按钮没生效。本批次暂停只在"所有观察到的游戏都退出"或
    "用户手动启用任意一款"时解除。
    """
    queue_repository = ActivationQueueRepository(database)
    queue = queue_repository.load()
    states = ActivationStateRepository(database)
    if enabled:
        queue_repository.save(move_to_front(queue, game_id))
        states.save(ActivationState(monitor_game_id=game_id))
        return
    state = states.load()
    queue_repository.save(suppress_entry(queue, game_id))
    if state.monitor_game_id == game_id:
        states.save(ActivationState(paused=True))


@dataclass(frozen=True)
class QueueItem:
    """队列里一条用于展示的记录(游戏 + 顺序 + 状态)."""

    game: Game
    position: int
    suppressed: bool = False
    running: bool = False
    monitor: bool = False
    first_seen_at: str = ""
    last_seen_at: str = ""


@dataclass(frozen=True)
class ActivationDecision:
    """策略的一次判断: 观察之后的新状态与队列 + 要执行的动作.

    ``target_game_id`` 与 ``deactivate`` 互斥: 前者要求"把启用态切到它", 后者要求
    "把启用态收回去"(监控对象退出了)。两个都不给就是保持现状。
    """

    state: ActivationState
    queue: tuple[RunEntry, ...] = ()
    target_game_id: int | None = None
    deactivate: bool = False
    reason: str = ""


@dataclass(frozen=True)
class ActivationOutcome:
    """一次自动启停判断的结果(界面据此提示、刷新与展示队列)."""

    state: ActivationState
    monitor: Game | None = None
    queue: tuple[QueueItem, ...] = ()
    enabled: Game | None = None
    disabled: Game | None = None
    conflicts: tuple[str, ...] = ()
    reason: str = ""

    @property
    def changed(self) -> bool:
        """本次是否真的改了启用态."""
        return self.enabled is not None or self.disabled is not None


class ActivationPolicy(Protocol):
    """决定"现在应该由哪一款游戏处于启用态"的策略.

    :func:`apply_activation` 把"全部游戏 + 可监控的游戏集合 + 落库的状态与队列 +
    一次全库探测的结果"交给策略, 策略返回 :class:`ActivationDecision`。**保持现状
    是最常见的结论**: 无法确认是否在运行、名字归不到唯一的一款、用户刚手动调整过,
    都应当返回不带动作的决定。``monitorable`` 是没有存档位置的游戏之外的集合, 那些
    游戏不参与监控(没有可备份的内容)。
    """

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
        """返回本次判断(要启用谁 / 要收回 / 保持现状 + 观察后的新状态与队列)."""


class ManualActivation:
    """默认策略: 从不自动切换(自动启停由设置里的开关决定要不要轮询)."""

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
        """原样交回状态与队列, 不给任何动作."""
        return ActivationDecision(state=state, queue=tuple(queue), reason=REASON_MANUAL)


def apply_activation(
    database: Database,
    policy: ActivationPolicy,
    *,
    provider: ProcessNameProvider | None = None,
    now: str | None = None,
) -> ActivationOutcome:
    """完成一次自动启停判断: 读状态与队列 → 一次枚举 → 问策略 → 落库 → 切换.

    库里没有可参与监控的游戏(没有游戏、全部归档, 或者**都没有关联存档位置**)时一次
    进程表都不枚举 —— 没有判断依据时这套功能不该产生开销。状态与队列都只在真的
    变化时落库; 切换仍然复用 :func:`set_enabled`, 因此"全局至多一款启用"只有一处
    实现。
    """
    stamp = iso_utc_now() if now is None else now
    states = ActivationStateRepository(database)
    state = states.load()
    queue_repository = ActivationQueueRepository(database)
    queue = queue_repository.load()
    games = GameRepository(database).list()
    # 没有存档位置的游戏不参与监控: 名字归属只按监控范围内的游戏建索引, 连探测
    # 都不会为它们枚举进程表。
    monitorable = frozenset(SaveLocationRepository(database).game_ids_with_locations())
    ownership = build_name_ownership([game for game in games if game.id in monitorable])
    observation = (
        probe_games(ownership, provider=provider)
        if ownership.needles
        else replace(UNCHECKED_GAME_PROBE, conflicts=ownership.conflicts)
    )
    decision = policy.decide(
        games,
        monitorable=monitorable,
        state=state,
        queue=queue,
        observation=observation,
        now=stamp,
    )
    if decision.state != state:
        states.save(decision.state)
    if decision.queue != queue:
        queue_repository.save(decision.queue)
    enabled, disabled = _carry_out(database, decision)
    started, stopped = _queue_diff(queue, decision.queue)
    log_action(
        ACTIVATION_ACTION,
        basic=True,
        reason=decision.reason,
        monitor_id=decision.state.monitor_game_id,
        running=len(observation.running_ids),
        monitorable=len(monitorable),
        started=len(started),
        stopped=len(stopped),
        conflicts=len(ownership.conflicts),
        enabled_id=_game_id(enabled),
        disabled_id=_game_id(disabled),
    )
    return ActivationOutcome(
        state=decision.state,
        monitor=_monitor_game(games, decision.state),
        queue=_queue_items(games, decision.queue, observation, decision.state),
        enabled=enabled,
        disabled=disabled,
        conflicts=ownership.conflicts,
        reason=decision.reason,
    )


def _queue_diff(
    before: Sequence[RunEntry], after: Sequence[RunEntry]
) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """返回(新入队的游戏 id, 出队的游戏 id), 供审计记录."""
    old = {entry.game_id for entry in before}
    new = {entry.game_id for entry in after}
    return tuple(sorted(new - old)), tuple(sorted(old - new))


def _monitor_game(games: Sequence[Game], state: ActivationState) -> Game | None:
    """按状态里的监控对象 id 取游戏(没有则返回 None)."""
    return next(
        (game for game in games if game.id == state.monitor_game_id),
        None,
    )


def _queue_items(
    games: Sequence[Game],
    entries: Sequence[RunEntry],
    observation: GameProbe,
    state: ActivationState,
) -> tuple[QueueItem, ...]:
    """把队列行补全成可展示的记录(游戏名、是否在运行、是否监控中)."""
    by_id = {game.id: game for game in games if game.id is not None}
    running = set(observation.running_ids)
    items: list[QueueItem] = []
    for entry in entries:
        game = by_id.get(entry.game_id)
        if game is None:  # pragma: no cover - 队列行随外键级联删除
            continue
        items.append(
            QueueItem(
                game=game,
                position=entry.position,
                suppressed=entry.suppressed,
                running=entry.game_id in running,
                monitor=entry.game_id == state.monitor_game_id,
                first_seen_at=entry.first_seen_at,
                last_seen_at=entry.last_seen_at,
            )
        )
    return tuple(items)


def _carry_out(
    database: Database, decision: ActivationDecision
) -> tuple[Game | None, Game | None]:
    """执行判断里的动作, 返回(被启用的游戏, 被停用的游戏)."""
    if decision.deactivate:
        return None, _deactivate(database)
    if decision.target_game_id is not None:
        return _activate(database, decision.target_game_id), None
    return None, None


def _activate(database: Database, game_id: int) -> Game:
    """把启用态切到 ``game_id``; 它已经是启用态时什么都不做."""
    game = _require_game(database, game_id)
    if game.enabled:
        return game
    return set_enabled(database, game_id, True).game


def _deactivate(database: Database) -> Game | None:
    """把当前启用的那一款停用; 本来就没人启用时返回 ``None``."""
    current = active_game(database)
    if current is None or current.id is None:
        return None
    return set_enabled(database, current.id, False).game


def _game_id(game: Game | None) -> int | None:
    """取审计用的游戏 id(没有游戏时给 ``None``, 空值会被 log_action 跳过)."""
    return None if game is None else game.id


def _require_game(database: Database, game_id: int) -> Game:
    """读取游戏记录, 不存在时给出可读错误."""
    game = GameRepository(database).get(game_id)
    if game is None:
        raise ArchiveManagementError(f"未知游戏: {game_id}")
    return game


def _first_game(repository: GameRepository, game_ids: Sequence[int]) -> Game | None:
    """按 id 依次读取, 返回第一个存在的游戏(用于"被自动停用的那一款")."""
    for game_id in game_ids:
        game = repository.get(game_id)
        if game is not None:
            return game
    return None
