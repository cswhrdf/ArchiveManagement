"""存档位置候选探测接口 (阶段 C 预留).

阶段 C 只定义"候选路径 + 用户确认"流程所需的数据模型与接口, 不做
具体的平台扫描(注册表、快捷方式等将在阶段 E-1 实现). 探测结果必须
经用户确认后才可写入存档位置, 不能静默信任猜测路径(PLAN 阶段 C 第 5 条).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from archive_management.domain import Game, SaveSource


@dataclass(frozen=True)
class LocationCandidate:
    """一个待用户确认的候选存档路径."""

    path: str
    source: SaveSource = "manual"
    confidence: float = 0.5
    reason_code: str = "auto"


@runtime_checkable
class CandidateProbe(Protocol):
    """为游戏产生候选存档位置的探测服务."""

    def probe(self, game: Game) -> list[LocationCandidate]:
        """返回基于本地信息推断的候选路径."""
        ...


class NoopCandidateProbe:
    """阶段 C 占位实现: 不执行任何探测."""

    def probe(self, game: Game) -> list[LocationCandidate]:
        """返回空候选列表(具体规则在阶段 E-1 落地)."""
        del game
        return []
