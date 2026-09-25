"""自动启停的状态模型与队列规则(见 PLAN 的阶段 G-8).

"现在玩哪一款"的最终决定权始终在用户手里(全局至多一款启用), 自动启停只做两件事:
把观察到的运行按顺序登记下来, 并在监控对象退出后按这个顺序接管下一款。因此这里
除了状态, 还定义队列与名称归属这两组纯规则 —— 它们不碰数据库、不碰进程表, 用例
可以直接把它们当函数测。

- ``monitor_game_id``: 当前监控对象(通常就是启用中的那一款)。手动启用一款还没
  运行的游戏时也会先记在这里 —— 没有这一列, 下一次轮询就会按队列把它改写掉。
- ``armed``: 是否观察到过**当前监控对象**运行。没有这个前置条件就没有"退出"可谈:
  用户刚在软件里启用、游戏还在启动中, 或者今天根本没打算玩, 看起来都是"没在运行"。
- ``paused``: 用户手动停用过当前监控对象 —— 这一批不再自动接管, 直到所有观察到的
  游戏都退出(或用户手动启用任意一款)。
- ``ActivationState`` 落单行表 ``activation_state``, 队列落 ``activation_runs``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace

from archive_management.domain.entities import Game
from archive_management.domain.process_names import name_needles

# 状态格式版本(与 home_state 同一套做法): 版本不符时旧记录按"没有状态"处理。
ACTIVATION_STATE_VERSION = 2

# 判断原因: 只描述"这一次为什么这么判断", 界面文案由调用方按它取 i18n。
# ``REASON_ENABLED`` / ``REASON_FALLBACK`` / ``REASON_DISABLED`` 是仅有的三个
# "真的改了启用态"的原因(接管 / 回落 / 收回)。
REASON_MANUAL = "manual"
# 库里没有可参与监控的游戏: 一款都没有, 或者全部都没有关联存档位置。
REASON_NO_GAMES = "no_games"
REASON_UNCHECKED = "unchecked"
REASON_RUNNING = "running"
REASON_ENABLED = "enabled"
REASON_FALLBACK = "fallback"
REASON_IDLE = "idle"
REASON_DISABLED = "disabled"
REASON_PAUSED = "paused"
REASON_OFF = "off"
REASON_UNAVAILABLE = "unavailable"

# 轮询间隔阶梯: 队列为空时用第一档(尽快发现"游戏启动了"), 有游戏在运行时逐档
# 放慢到最后一档。代价是监控对象退出的发现最多晚 ``ACTIVATION_DELAY_LADDER[-1]``
# 秒(回落与"全部退出后停用"都跟着晚), 但启动那一刻的响应最快。
ACTIVATION_DELAY_LADDER = (5.0, 8.0, 12.0, 16.0, 20.0)


def activation_delay(steps: int, *, running: bool) -> float:
    """返回下一次轮询的间隔秒数(纯函数, 便于用例锁住阶梯)."""
    if not running:
        return ACTIVATION_DELAY_LADDER[0]
    index = min(max(steps, 0), len(ACTIVATION_DELAY_LADDER) - 1)
    return ACTIVATION_DELAY_LADDER[index]


@dataclass(frozen=True)
class ActivationState:
    """自动启停的持久化状态(单行表 ``activation_state``)."""

    monitor_game_id: int | None = None
    armed: bool = False
    paused: bool = False
    version: int = ACTIVATION_STATE_VERSION


@dataclass(frozen=True)
class RunEntry:
    """队列里的一条"观察到的运行".

    ``position`` 从 1 开始, 顺序就是"先后被观察到启动"的顺序(回落依据);
    ``suppressed`` 记录"用户手动停用过它" —— 只要它还在运行就不会被自动接管,
    该行随它退出一起消失(抑制随之解除)。
    """

    game_id: int
    position: int
    suppressed: bool = False
    first_seen_at: str = ""
    last_seen_at: str = ""


def reindex(entries: Iterable[RunEntry]) -> tuple[RunEntry, ...]:
    """按当前顺序重排位置号(每次改动队列后都过一遍, 位置永远连续)."""
    return tuple(
        replace(entry, position=index + 1) for index, entry in enumerate(entries)
    )


def maintain_queue(
    queue: Sequence[RunEntry], *, running_ids: Sequence[int], now: str = ""
) -> tuple[RunEntry, ...]:
    """按本次观察到的运行集合出队/入队, 返回新的队列(纯函数).

    ``running_ids`` 的顺序就是"同一轮里新观察到"的入队顺序 —— 调用方按游戏 id
    升序传进来, 保证确定且可测; 已经在队列里的项保持原有顺序, 只刷新
    ``last_seen_at``。没在运行的项直接删掉(它的 ``suppressed`` 也随行消失)。
    """
    running = set(running_ids)
    kept = [
        replace(entry, last_seen_at=now) for entry in queue if entry.game_id in running
    ]
    known = {entry.game_id for entry in kept}
    appended = [
        RunEntry(game_id=game_id, position=0, first_seen_at=now, last_seen_at=now)
        for game_id in running_ids
        if game_id not in known
    ]
    return reindex((*kept, *appended))


def first_candidate(queue: Sequence[RunEntry]) -> int | None:
    """队列里第一条"仍在运行且未被抑制"的游戏(队列本身就是运行中的快照)."""
    for entry in queue:
        if not entry.suppressed:
            return entry.game_id
    return None


def move_to_front(queue: Sequence[RunEntry], game_id: int) -> tuple[RunEntry, ...]:
    """把某款移到队首并清掉它的抑制标记(手动启用: 手动选的最有资格)."""
    target = next((entry for entry in queue if entry.game_id == game_id), None)
    if target is None:
        return reindex(queue)
    rest = [entry for entry in queue if entry.game_id != game_id]
    return reindex((replace(target, suppressed=False), *rest))


def suppress_entry(queue: Sequence[RunEntry], game_id: int) -> tuple[RunEntry, ...]:
    """把某款标成"手动停用过"(不在队列里则原样返回)."""
    if not any(entry.game_id == game_id for entry in queue):
        return reindex(queue)
    return reindex(
        replace(entry, suppressed=True) if entry.game_id == game_id else entry
        for entry in queue
    )


def process_names(game: Game) -> tuple[str, ...]:
    """返回用于匹配进程名的候选名称.

    界面上的名字可能是译名(中文), 与进程名对不上; 录入时识别到的原始名称
    (``Stardew Valley``) 正好补上这一层。两者都交给
    :func:`~archive_management.domain.process_names.name_needles` 归一化后再匹配,
    太短或全是非 ASCII 的名字会被它丢掉。
    """
    names = (game.name.strip(), game.original_name.strip())
    return tuple(dict.fromkeys(name for name in names if name))


@dataclass(frozen=True)
class NameOwnership:
    """候选名 → 唯一拥有者的索引(全库匹配的第一道保险).

    ``probe_processes`` 的"归一化后互相包含"在**全库**名称集下会明显变凶(一款叫
    ``Wilds`` 的游戏会被 ``OuterWilds.exe`` 命中), 所以只有"恰好一款游戏拥有"的
    针才进 ``needles``; 被多款共享的针进 ``conflicts`` 并被整体丢弃(DEL 日志与界面
    上都看得到)。
    """

    needles: Mapping[str, int] = field(default_factory=dict)
    conflicts: tuple[str, ...] = ()


def build_name_ownership(games: Sequence[Game]) -> NameOwnership:
    """把全部未归档游戏的候选名分组, 产出"针 → 唯一拥有者"(纯函数)."""
    owners: dict[str, set[int]] = {}
    for game in games:
        if game.archived or game.id is None:
            continue
        for needle in name_needles(process_names(game)):
            owners.setdefault(needle, set()).add(game.id)
    needles = {
        needle: next(iter(ids)) for needle, ids in owners.items() if len(ids) == 1
    }
    conflicts = tuple(sorted(needle for needle, ids in owners.items() if len(ids) > 1))
    return NameOwnership(needles=needles, conflicts=conflicts)


__all__ = [
    "ACTIVATION_DELAY_LADDER",
    "ACTIVATION_STATE_VERSION",
    "REASON_DISABLED",
    "REASON_ENABLED",
    "REASON_FALLBACK",
    "REASON_IDLE",
    "REASON_MANUAL",
    "REASON_NO_GAMES",
    "REASON_OFF",
    "REASON_PAUSED",
    "REASON_RUNNING",
    "REASON_UNAVAILABLE",
    "REASON_UNCHECKED",
    "ActivationState",
    "NameOwnership",
    "RunEntry",
    "activation_delay",
    "build_name_ownership",
    "first_candidate",
    "maintain_queue",
    "move_to_front",
    "process_names",
    "reindex",
    "suppress_entry",
]
