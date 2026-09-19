"""游戏生命周期: 删除、启用/停用, 以及"当前启用哪一款"的规则.

同一时刻只允许一款游戏处于启用态(快捷键与定时备份只对它生效), 因此所有启用
入口都收敛到 :func:`set_enabled` —— 它会先停用其它游戏再启用目标, 并拒绝启用
已归档的游戏。

按"被监控的游戏进程是否启动"自动切换启用态这件事只留了接缝
(:class:`ActivationPolicy` + :func:`apply_activation`), 具体策略尚未实现, 需求与
约束写在 PLAN.md 的 G-6.5 子步骤里。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from archive_management.domain import Game
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
)
from archive_management.services.audit import log_action

DELETE_ACTION = "game.delete"
ENABLE_ACTION = "game.set_enabled"
RELEASE_ACTION = "discovery.candidates_released"


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


def set_enabled(database: Database, game_id: int, enabled: bool) -> EnableResult:
    """启用/停用一款游戏; 启用会先停用其它游戏(全局至多一款启用).

    归档的游戏不允许启用(归档只保留"删除/导出/取消归档/打开详情"), 取消归档也
    不会顺手启用: 由用户明确指定"当前在玩的是哪一款"。
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
        replaced_id=None if replaced is None else replaced.id,
    )
    return EnableResult(game=updated, replaced=replaced)


def active_game(database: Database) -> Game | None:
    """返回当前启用的游戏; 没有启用任何游戏时返回 None."""
    repository = GameRepository(database)
    game_id = repository.enabled_game_id()
    return None if game_id is None else repository.get(game_id)


class ActivationPolicy(Protocol):
    """决定"现在应该由哪一款游戏处于启用态"的策略.

    这是按游戏进程自动启停(PLAN 的 G-6.5)的接入点: 未来由"被监控的游戏是否
    启动"算出目标游戏 id。**返回 None 表示"不做判断"** —— 用户手动调整过之后
    策略必须能表达"保持现状", 否则下一次轮询就会把手动选择刷回去。
    """

    def target_game(
        self, games: Sequence[Game], *, running: Sequence[str]
    ) -> int | None:
        """返回要启用的游戏 id; 不改变现状时返回 None."""


class ManualActivation:
    """默认策略: 从不自动切换(自动启停尚未实现)."""

    def target_game(
        self, games: Sequence[Game], *, running: Sequence[str]
    ) -> int | None:
        """始终返回 None, 保持用户手动设置的启用状态."""
        return None


def apply_activation(
    database: Database,
    policy: ActivationPolicy,
    *,
    running: Sequence[str] = (),
) -> EnableResult | None:
    """按策略切换启用游戏; 策略认为不需要改变时返回 None.

    目标与当前启用的游戏相同就直接返回(避免每次轮询都写库); 真正切换时复用
    :func:`set_enabled`, 于是"全局至多一款启用"这条不变量只有一处实现。
    """
    repository = GameRepository(database)
    games = repository.list()
    target = policy.target_game(games, running=running)
    if target is None or target == repository.enabled_game_id():
        return None
    if not any(game.id == target for game in games):
        raise ArchiveManagementError(f"未知游戏: {target}")
    return set_enabled(database, target, True)


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
