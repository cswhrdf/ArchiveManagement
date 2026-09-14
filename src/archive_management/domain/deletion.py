"""备份删除计划.

删除一个备份节点需要按分支树决定两种处理方式:

- **上移**(``SHIFT``): 被删节点位于同一条线路上, 其唯一子节点直接挂到被删
  节点的父节点上, 线路自然闭合, 不丢任何备份;
- **级联**(``CASCADE``): 被删节点是分支的根(自身是分支节点, 或拥有多个子
  节点), 删除它会让后续分支失去起点, 因此必须由用户确认后连同其整个子
  树一起删除;
- **单点**(``SINGLE``): 没有子节点, 直接删除.

本模块只做纯计算, 不触碰数据库或文件系统, 便于在无环境依赖下单测.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from archive_management.domain.entities import BackupNode

# 自动备份默认保留份数(用户可按游戏自定义).
DEFAULT_KEEP_AUTO = 3


class DeletionMode(StrEnum):
    """删除模式."""

    SINGLE = "single"
    SHIFT = "shift"
    CASCADE = "cascade"


@dataclass(frozen=True)
class DeletionPlan:
    """一次删除操作的计划(展示 + 执行)."""

    mode: DeletionMode
    node_id: int
    removed_ids: tuple[int, ...]
    # SHIFT 模式下需要重新挂到 ``new_parent_id`` 的子节点.
    shifted_child_id: int | None = None
    new_parent_id: int | None = None

    @property
    def removed_count(self) -> int:
        """返回将被删除的节点总数."""
        return len(self.removed_ids)

    @property
    def needs_confirmation(self) -> bool:
        """级联删除必须先经用户确认."""
        return self.mode is DeletionMode.CASCADE


@dataclass(frozen=True)
class _Links:
    """节点间的父子关系索引."""

    node_kind: dict[int, str] = field(default_factory=dict)
    parent: dict[int, int | None] = field(default_factory=dict)
    children: dict[int, list[int]] = field(default_factory=dict)


def _index(nodes: Sequence[BackupNode]) -> _Links:
    """建立 id -> 父/子/类型 的索引(缺 id 的节点被忽略)."""
    links = _Links()
    for node in nodes:
        if node.id is None:
            continue
        links.node_kind[node.id] = node.node_kind
        links.parent[node.id] = node.parent_id
        links.children.setdefault(node.id, [])
    for node_id, parent_id in links.parent.items():
        if parent_id is not None and parent_id in links.node_kind:
            links.children.setdefault(parent_id, []).append(node_id)
    return links


def descendants(links: _Links, node_id: int) -> list[int]:
    """返回某节点的全部后代(不含自身), 深度优先序."""
    found: list[int] = []
    stack = list(links.children.get(node_id, []))
    while stack:
        current = stack.pop()
        found.append(current)
        stack.extend(links.children.get(current, []))
    return found


def plan_deletion(nodes: Sequence[BackupNode], node_id: int) -> DeletionPlan:
    """按分支树计算删除计划; 节点不存在时抛出 :class:`KeyError`."""
    links = _index(nodes)
    if node_id not in links.node_kind:
        raise KeyError(node_id)
    children = links.children.get(node_id, [])
    parent_id = links.parent.get(node_id)
    if not children:
        return DeletionPlan(
            mode=DeletionMode.SINGLE, node_id=node_id, removed_ids=(node_id,)
        )
    if len(children) == 1 and links.node_kind.get(node_id) != "branch":
        # 同一条线路: 唯一的子节点上移, 线路闭合.
        return DeletionPlan(
            mode=DeletionMode.SHIFT,
            node_id=node_id,
            removed_ids=(node_id,),
            shifted_child_id=children[0],
            new_parent_id=parent_id,
        )
    # 分支根节点: 连同其下所有备份一起删除(需用户确认).
    return DeletionPlan(
        mode=DeletionMode.CASCADE,
        node_id=node_id,
        removed_ids=(node_id, *descendants(links, node_id)),
    )


def descendant_ids(nodes: Iterable[BackupNode], node_id: int) -> list[int]:
    """返回某节点全部后代的 id 列表."""
    return descendants(_index(list(nodes)), node_id)


def auto_prune_ids(
    nodes: Sequence[BackupNode],
    keep: int,
    *,
    protected: Iterable[int] = (),
) -> list[int]:
    """返回自动备份中超出保留份数、应当删除的节点 id(最旧的在前).

    保留规则: 最新的 ``keep`` 份(含 ``protected`` 中的节点)一定保留;
    ``protected``(例如刚创建的节点)永不被剪除, 且计入保留份数.
    """
    protected_ids = set(protected)
    autos = [node for node in nodes if node.id is not None and node.node_kind == "auto"]
    autos.sort(key=lambda node: (node.created_at, node.id or 0))
    retained: set[int] = set(protected_ids)
    if keep > 0:
        retained.update(node.id for node in autos[-keep:] if node.id is not None)
    return [
        node.id for node in autos if node.id is not None and node.id not in retained
    ]
