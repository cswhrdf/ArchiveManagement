"""备份分支树的纯计算模型.

分支关系由 ``parent_id`` 表达: "向下保存" 以当前节点为父节点
新增子节点, "从当前节点分支" 同样落在父节点之下并带有分支名. 本模块只做
结构计算(层级、子节点顺序、深度优先遍历序), 不涉及数据库、文件系统或
Tkinter, 因此可在无显示环境直接单测.

容错策略: 父节点缺失(被删除或数据不完整)的节点按根节点处理, 出现环时
以访问标记截断, 保证界面永远拿得到一棵可渲染的森林.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from archive_management.domain.entities import BackupNode


@dataclass(frozen=True)
class TreeInput:
    """构建分支树所需的最小节点信息."""

    node_id: str
    parent_id: str | None = None
    created_at: datetime | None = None
    # 该节点自身开启的分支名(为空表示没有在此处开分支).
    branch_name: str | None = None


@dataclass(frozen=True)
class TreeNode:
    """一个已确定层级与子节点顺序的树节点."""

    node_id: str
    parent_id: str | None
    depth: int
    children: tuple[str, ...]
    is_root: bool

    def __post_init__(self) -> None:
        """校验层级非负."""
        if self.depth < 0:
            raise ValueError("depth 不能为负数")


def tree_inputs(nodes: Sequence[BackupNode]) -> list[TreeInput]:
    """把领域备份节点转换为树输入(缺 id 的节点被忽略)."""
    inputs: list[TreeInput] = []
    for node in nodes:
        if node.id is None:
            continue
        inputs.append(
            TreeInput(
                node_id=str(node.id),
                parent_id=None if node.parent_id is None else str(node.parent_id),
                created_at=node.created_at,
                branch_name=node.branch_name,
            )
        )
    return inputs


def _sort_key(item: TreeInput) -> tuple[float, str]:
    """子节点顺序: 创建时间升序(越早越靠上), 时间相同按 id 稳定排序."""
    stamp = item.created_at.timestamp() if item.created_at is not None else 0.0
    return (stamp, item.node_id)


def build_tree(items: Sequence[TreeInput]) -> list[TreeNode]:
    """构建分支树, 返回深度优先序遍历的节点列表.

    子节点按创建时间升序排列, 使 "向下保存" 形成的时间线自上而下延伸;
    父节点不在集合中的节点被视为根, 使其仍可被展示.
    """
    known = {item.node_id for item in items}
    children: dict[str, list[TreeInput]] = {}
    roots: list[TreeInput] = []
    for item in items:
        parent_id = item.parent_id
        if parent_id is None or parent_id not in known or parent_id == item.node_id:
            roots.append(item)
        else:
            children.setdefault(parent_id, []).append(item)

    for group in children.values():
        group.sort(key=_sort_key)
    roots.sort(key=_sort_key)

    ordered: list[TreeNode] = []
    visited: set[str] = set()

    def walk(item: TreeInput, depth: int) -> None:
        if item.node_id in visited:
            return
        visited.add(item.node_id)
        child_inputs = children.get(item.node_id, [])
        ordered.append(
            TreeNode(
                node_id=item.node_id,
                parent_id=item.parent_id,
                depth=depth,
                children=tuple(child.node_id for child in child_inputs),
                is_root=depth == 0,
            )
        )
        for child in child_inputs:
            walk(child, depth + 1)

    for root in roots:
        walk(root, 0)
    # 环中的节点不会被任何根到达, 兜底为根节点后继续遍历.
    for item in sorted(items, key=_sort_key):
        if item.node_id not in visited:
            walk(item, 0)
    return ordered


def tree_depths(items: Sequence[TreeInput]) -> dict[str, int]:
    """返回 ``{node_id: depth}`` 映射, 供列表缩进使用."""
    return {node.node_id: node.depth for node in build_tree(items)}


def branch_lineage(items: Sequence[TreeInput]) -> dict[str, str | None]:
    """返回 ``{node_id: 所属分支名}``.

    节点所属的分支取"自身或最近带有分支名的祖先": 分支节点开启一条新线,
    其后续备份(即使自身没有分支名)都归属于该分支, 因此时间线视图可以
    显示每个备份当前处在哪条分支下.
    """
    by_id = {item.node_id: item for item in items}
    cache: dict[str, str | None] = {}

    def resolve(node_id: str) -> str | None:
        if node_id in cache:
            return cache[node_id]
        item = by_id.get(node_id)
        cache[node_id] = None  # 先占位, 避免环导致无限递归
        if item is None:
            return None
        if item.branch_name:
            cache[node_id] = item.branch_name
        else:
            parent = item.parent_id
            cache[node_id] = None if parent is None else resolve(parent)
        return cache[node_id]

    for item in items:
        resolve(item.node_id)
    return cache


def keep_surviving(items: Sequence[TreeInput], surviving: set[str]) -> list[TreeInput]:
    """只保留 ``surviving`` 中的节点, 并把它们的父节点重挂到最近的在集合内的祖先.

    用于分支视图过滤(例如只显示最新的自动备份): 被过滤掉的中间节点不会让
    后续节点变成"孤儿根", 层级因此仍然连续.
    """
    by_id = {item.node_id: item for item in items}
    remapped: list[TreeInput] = []
    for item in items:
        if item.node_id not in surviving:
            continue
        parent_id = item.parent_id
        seen: set[str] = {item.node_id}
        while parent_id is not None and parent_id not in surviving:
            if parent_id in seen:
                parent_id = None
                break
            seen.add(parent_id)
            parent = by_id.get(parent_id)
            parent_id = None if parent is None else parent.parent_id
        remapped.append(
            TreeInput(
                node_id=item.node_id,
                parent_id=parent_id,
                created_at=item.created_at,
                branch_name=item.branch_name,
            )
        )
    return remapped
