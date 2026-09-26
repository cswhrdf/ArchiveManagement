"""备份用例: 向下保存与创建分支.

用例把"存档位置 -> 文件快照 -> 备份节点"串成一次可追踪的操作:

1. 读取游戏的全部存档位置(顺序在备份与恢复之间保持稳定);
2. 生成快照(临时目录 + 哈希校验 + 原子提交, 见
   :mod:`archive_management.services.snapshot`);
3. 在**同一事务**中写入备份节点与文件清单, 使"有节点必有清单";
4. 每次备份/分支/安全点都通过 :mod:`archive_management.services.audit` 记录到
   日志文件, 便于事后追溯用户操作(日志不落库);
5. 落库失败时删除已提交的快照目录, 保证磁盘状态与数据库一致.

备份目录不再用数字 id: 每个游戏在备份根下有一个由**游戏名称 + 名称与存档
路径推导的短哈希**组成的目录(``<slug>-<token>``, 见
:mod:`archive_management.services.naming`)。该目录名在第一次备份时写入
``games.storage_key`` 后不再变化, 因此改名或增删存档位置都不会搬动已有备份;
录入时的名称另存为 ``games.original_name``, 供界面展示"原始名称"。

分支关系由 ``parent_id`` 表达: "向下保存" 追加在当前末端节点之后,
"从当前节点分支" 以选中节点为父节点并标记分支名.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from archive_management.domain import (
    DEFAULT_KEEP_AUTO,
    BackupFileEntry,
    BackupNode,
    DeletionMode,
    DeletionPlan,
    Game,
    NodeKind,
    SaveLocation,
    TreeInput,
    TreeNode,
    auto_prune_ids,
    build_tree,
    plan_deletion,
    tree_inputs,
)
from archive_management.exceptions import (
    ArchiveManagementError,
    ContentUnchangedError,
    OperationCancelledError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.audit import log_action, log_failure
from archive_management.services.naming import backup_relpath, game_folder
from archive_management.services.snapshot import (
    ProgressCallback,
    SnapshotResult,
    SnapshotSource,
    SnapshotVerification,
    create_snapshot,
    remove_snapshot,
    verify_snapshot,
)

# 自动备份默认保留份数从领域层导入(常量定义见 domain/deletion.py).
# 备份名称与描述的长度上限.
MAX_TITLE_LENGTH = 60
MAX_NOTE_LENGTH = 200


class Snapshotter(Protocol):
    """快照生成函数签名, 便于测试注入替身."""

    def __call__(
        self,
        sources: Sequence[SnapshotSource],
        destination: Path,
        *,
        progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> SnapshotResult:
        """生成一次快照."""
        ...


@dataclass(frozen=True)
class BackupFacts:
    """备份节点的内容摘要, 供列表与详情展示."""

    file_count: int
    total_size: int
    content_hash: str | None = None


class BackupService:
    """基于 SQLite 与本地快照的备份用例实现."""

    def __init__(
        self,
        database: Database,
        *,
        backup_root: Path,
        snapshotter: Snapshotter = create_snapshot,
    ) -> None:
        """绑定数据库、备份根目录与可替换的快照实现."""
        self._root = Path(backup_root)
        self._snapshotter = snapshotter
        self._games = GameRepository(database)
        self._locations = SaveLocationRepository(database)
        self._backups = BackupRepository(database)

    # -- 查询 ---------------------------------------------------------------

    def list_nodes(self, game_id: int) -> list[BackupNode]:
        """返回某游戏的全部备份节点(创建时间升序)."""
        return self._backups.list_for_game(game_id)

    def get(self, backup_id: int) -> BackupNode | None:
        """按 id 返回备份节点."""
        return self._backups.get(backup_id)

    def tree(self, game_id: int) -> list[TreeNode]:
        """返回某游戏的分支树(深度优先序, 含层级)."""
        return build_tree(self.tree_inputs(game_id))

    def tree_inputs(self, game_id: int) -> list[TreeInput]:
        """返回构建分支树所需的输入(供 UI 与树计算复用)."""
        return tree_inputs(self._backups.list_for_game(game_id))

    def latest(self, game_id: int) -> BackupNode | None:
        """返回某游戏最新的备份节点."""
        return self._backups.latest_for_game(game_id)

    def current_node(self, game_id: int) -> BackupNode | None:
        """返回某游戏的"当前节点"("恢复到此节点"后新备份的起点).

        指针缺失或指向已被删除的节点时, 回退到最新的备份节点, 保证
        "向下保存"始终有一个可用的父节点.
        """
        current_id = self._games.current_backup(game_id)
        if current_id is not None:
            node = self._backups.get(current_id)
            if node is not None:  # pragma: no branch - 外键保证指针不会悬空
                return node
        return self._backups.latest_for_game(game_id)

    def set_current(self, game_id: int, backup_id: int) -> BackupNode:
        """把当前节点切换到指定备份("恢复到此节点")."""
        node = self._backups.get(backup_id)
        if node is None or node.game_id != game_id:
            raise ArchiveManagementError(f"未知的备份节点: {backup_id}")
        self._games.set_current_backup(game_id, backup_id)
        return node

    def facts(self, node: BackupNode) -> BackupFacts:
        """返回备份节点的文件数与总大小."""
        if node.id is None:
            return BackupFacts(file_count=0, total_size=0, content_hash=None)
        count, total = self._backups.describe(node.id)
        return BackupFacts(
            file_count=count, total_size=total, content_hash=node.content_hash
        )

    def snapshot_root(self, node: BackupNode) -> Path:
        """返回备份节点在磁盘上的快照目录."""
        if not node.storage_relpath:
            raise ArchiveManagementError("备份节点缺少存储路径, 无法定位快照")
        return self._root / node.storage_relpath

    def verify(self, node: BackupNode) -> SnapshotVerification:
        """校验备份节点的快照完整性."""
        return verify_snapshot(self.snapshot_root(node))

    # -- 写入 ---------------------------------------------------------------

    def create_backup(
        self,
        game_id: int,
        *,
        kind: NodeKind = "manual",
        note: str = "",
        parent_id: int | None = None,
        branch_name: str | None = None,
        title: str = "",
        keep_auto: int = DEFAULT_KEEP_AUTO,
        safety: bool = False,
        progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> BackupNode:
        """生成一次快照并写入备份节点.

        ``parent_id`` 为 ``None`` 时挂到**当前节点**之后("向下保存"); 当前节点
        由 "恢复到此节点" 决定, 因此恢复之后的新备份/新分支都从该节点重新
        开始. 传入节点 id 时以该节点为父节点(创建分支)。

        自动备份(``kind="auto"``)是特殊备份: 成功后会按 ``keep_auto`` 只保留
        最近的若干份, 更早的自动备份连同其快照一起清理. ``safety=True`` 表示
        这是恢复前自动创建的安全点: 只在时间线视图展示, 不进入分支树。
        """
        sources = self._sources_for_backup(game_id)
        parent = self._resolve_parent(game_id, parent_id)
        folder = self.storage_folder(game_id)
        relpath = backup_relpath(folder, self._stamp(), uuid4().hex[:8])
        destination = self._root / relpath
        action = "branch.create" if kind == "branch" else "backup.create"
        log_action(
            action,
            game_id=game_id,
            folder=folder,
            kind=kind,
            safety=safety,
            auto=kind == "auto",
            sources=len(sources),
            title=title or None,
            branch=branch_name,
            parent_id=None if parent is None else parent.id,
        )
        result = self._run_snapshot(
            sources,
            destination,
            action=action,
            game_id=game_id,
            progress=progress,
            cancelled=cancelled,
        )
        if parent is not None and parent.content_hash:
            self._reject_unchanged(
                action=action,
                game_id=game_id,
                parent=parent,
                destination=destination,
                content_hash=result.content_hash,
            )
        node = BackupNode(
            game_id=game_id,
            parent_id=None if parent is None else parent.id,
            node_kind=kind,
            branch_name=branch_name,
            title=title,
            note=note,
            content_hash=result.content_hash,
            storage_relpath=relpath,
            created_at=datetime.now(UTC),
            is_safety=safety,
        )
        node = self._insert_node(
            node,
            destination=destination,
            result=result,
            action=action,
            game_id=game_id,
        )
        pruned = self._activate_node(
            node, game_id=game_id, keep_auto=keep_auto, kind=kind
        )
        log_action(
            action,
            game_id=game_id,
            backup_id=node.id,
            result="succeeded",
            entries=len(result.entries),
            files=result.file_count(),
            bytes=result.total_size,
            pruned_auto=len(pruned) or None,
        )
        return node

    def _sources_for_backup(self, game_id: int) -> list[SnapshotSource]:
        """校验游戏与存档位置, 并按位置顺序列出快照来源."""
        if self._games.get(game_id) is None:
            raise ArchiveManagementError(f"未知游戏: {game_id}")
        locations = self._locations.list_for_game(game_id)
        if not locations:
            raise ArchiveManagementError("该游戏未配置存档位置, 无法备份")
        return [
            SnapshotSource(path=location.path, kind=location.path_kind, index=index)
            for index, location in enumerate(locations)
        ]

    def _run_snapshot(
        self,
        sources: list[SnapshotSource],
        destination: Path,
        *,
        action: str,
        game_id: int,
        progress: ProgressCallback | None,
        cancelled: Callable[[], bool] | None,
    ) -> SnapshotResult:
        """执行一次快照; 取消与失败先记审计日志再原样抛出."""
        try:
            return self._snapshotter(
                sources, destination, progress=progress, cancelled=cancelled
            )
        except OperationCancelledError as exc:
            log_action(action, game_id=game_id, result="cancelled", reason=str(exc))
            raise
        except Exception as exc:
            log_failure(action, game_id=game_id, error=str(exc))
            raise

    def _insert_node(
        self,
        node: BackupNode,
        *,
        destination: Path,
        result: SnapshotResult,
        action: str,
        game_id: int,
    ) -> BackupNode:
        """写入备份节点并返回落库后的节点; 写库失败时回收已落盘的快照目录."""
        try:
            return self._backups.add_with_files(node, _file_entries(result))
        except Exception as exc:
            # 数据库写入失败时回收快照目录, 避免留下"孤儿"备份.
            remove_snapshot(destination)
            log_failure(action, game_id=game_id, error=str(exc))
            raise

    def _activate_node(
        self, node: BackupNode, *, game_id: int, keep_auto: int, kind: NodeKind
    ) -> list[int]:
        """把新节点设为"当前节点", 自动备份再按保留份数清理更早的节点."""
        if node.id is None:  # pragma: no cover - 查询总是带 id
            return []
        # 新备份成为"当前节点": 后续的向下保存都会接在它后面.
        self._games.set_current_backup(game_id, node.id)
        if kind != "auto":
            return []
        return self._prune_auto(game_id, keep_auto, protect=node.id)

    def create_branch(
        self,
        game_id: int,
        from_backup_id: int,
        branch_name: str,
        *,
        progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> BackupNode:
        """从指定备份节点创建一条新分支(记录当前存档状态)."""
        clean = branch_name.strip()
        if not clean:
            raise ArchiveManagementError("分支名称不能为空")
        return self.create_backup(
            game_id,
            kind="branch",
            title=clean,
            parent_id=from_backup_id,
            branch_name=clean,
            progress=progress,
            cancelled=cancelled,
        )

    def update_meta(
        self,
        game_id: int,
        backup_id: int,
        *,
        title: str,
        note: str,
    ) -> BackupNode:
        """修改备份的名称与描述(描述长度上限见 :data:`MAX_NOTE_LENGTH`)."""
        node = self._require_node(game_id, backup_id)
        clean_title = title.strip()
        clean_note = note.strip()
        if len(clean_note) > MAX_NOTE_LENGTH:
            raise ArchiveManagementError(f"备份描述不能超过 {MAX_NOTE_LENGTH} 个字符")
        if len(clean_title) > MAX_TITLE_LENGTH:
            raise ArchiveManagementError(f"备份名称不能超过 {MAX_TITLE_LENGTH} 个字符")
        if node.id is None:  # pragma: no cover - 刚写入的行必然可读
            raise ArchiveManagementError(f"未知的备份节点: {backup_id}")
        updated = self._backups.update_meta(node.id, title=clean_title, note=clean_note)
        if updated is None:
            raise ArchiveManagementError(f"未知的备份节点: {backup_id}")
        log_action(
            "backup.update_meta",
            game_id=game_id,
            backup_id=backup_id,
            named=bool(clean_title),
            note_length=len(clean_note),
        )
        return updated

    def plan_delete(self, game_id: int, backup_id: int) -> DeletionPlan:
        """返回某个备份的删除计划(不修改任何数据)."""
        self._require_node(game_id, backup_id)
        try:
            return plan_deletion(self.list_nodes(game_id), backup_id)
        except KeyError as exc:
            raise ArchiveManagementError(f"未知的备份节点: {backup_id}") from exc

    def delete_node(
        self,
        game_id: int,
        backup_id: int,
        *,
        cascade: bool = False,
    ) -> DeletionPlan:
        """按分支树删除备份节点.

        同一线路上的节点会让其唯一子节点上移; 分支根节点会连同其下所有
        备份一起删除, 未显式确认(``cascade=True``)时拒绝执行.
        """
        plan = self.plan_delete(game_id, backup_id)
        if plan.needs_confirmation and not cascade:
            raise ArchiveManagementError("删除分支根节点会一并删除其分支下的全部备份")
        nodes = {node.id: node for node in self.list_nodes(game_id)}
        log_action(
            "backup.delete",
            game_id=game_id,
            backup_id=backup_id,
            mode=plan.mode.value,
            removed=plan.removed_count,
            cascade=cascade,
        )
        try:
            if plan.mode is DeletionMode.SHIFT and plan.shifted_child_id is not None:
                self._backups.reparent(plan.shifted_child_id, plan.new_parent_id)
            for removed_id in plan.removed_ids:
                node = nodes.get(removed_id)
                if node is not None and node.storage_relpath:
                    remove_snapshot(self._root / node.storage_relpath)
            self._backups.delete_many(list(plan.removed_ids))
            self._fix_current_after_delete(game_id, plan)
        except Exception as exc:
            log_failure("backup.delete", game_id=game_id, error=str(exc))
            raise
        log_action(
            "backup.delete",
            game_id=game_id,
            backup_id=backup_id,
            result="succeeded",
            removed=plan.removed_count,
        )
        return plan

    def delete(self, node: BackupNode) -> None:
        """删除备份节点及其快照目录."""
        if node.id is None:
            return
        root = self.snapshot_root(node) if node.storage_relpath else None
        self._backups.delete(node.id)
        if root is not None:
            remove_snapshot(root)
        log_action("backup.discard", basic=True, backup_id=node.id)

    # -- 内部 ---------------------------------------------------------------

    def _require_node(self, game_id: int, backup_id: int) -> BackupNode:
        """要求备份节点存在且属于该游戏."""
        node = self._backups.get(backup_id)
        if node is None or node.game_id != game_id:
            raise ArchiveManagementError(f"未知的备份节点: {backup_id}")
        return node

    def _prune_auto(self, game_id: int, keep: int, *, protect: int) -> list[int]:
        """只保留最近 ``keep`` 份自动备份, 返回被清理的节点 id."""
        nodes = self.list_nodes(game_id)
        targets = auto_prune_ids(nodes, keep, protected={protect})
        if not targets:
            return []
        by_id = {node.id: node for node in nodes if node.id is not None}
        children = _children_by_parent(nodes)
        for backup_id in targets:
            target = by_id.get(backup_id)
            if target is not None:  # pragma: no branch - 目标来自同一份节点列表
                self._prune_node(backup_id, target, children=children)
        log_action(
            "backup.prune_auto",
            game_id=game_id,
            keep=keep,
            pruned=len(targets),
        )
        return targets

    def _prune_node(
        self,
        backup_id: int,
        target: BackupNode,
        *,
        children: dict[int, list[int]],
    ) -> None:
        """删除一个被剪除的节点: 子节点上移保持线路连续, 再删库与快照目录."""
        for child_id in children.get(backup_id, []):
            self._backups.reparent(child_id, target.parent_id)
        self._backups.delete(backup_id)
        if target.storage_relpath:
            remove_snapshot(self._root / target.storage_relpath)

    def _fix_current_after_delete(self, game_id: int, plan: DeletionPlan) -> None:
        """删除后修正"当前节点"指针, 避免指向已删除的节点."""
        current = self._games.current_backup(game_id)
        if current is None or current not in plan.removed_ids:
            return
        fallback = plan.new_parent_id
        if fallback is None and plan.shifted_child_id is not None:
            fallback = plan.shifted_child_id
        self._games.set_current_backup(game_id, fallback)

    def storage_folder(self, game_id: int) -> str:
        """返回(必要时确定并持久化)该游戏在备份根下的目录名.

        目录名 = 游戏名称规范化后的片段 + 由"名称 + 全部存档路径"推导的短哈希
        (``<slug>-<token>``)。首次调用时写入 ``games.storage_key``, 之后固定
        不变: 改名或增删存档位置都不会搬动已有备份。
        """
        game = self._games.get(game_id)
        if game is None:  # pragma: no cover - 调用前刚从库里取出这款游戏
            raise ArchiveManagementError(f"未知游戏: {game_id}")
        key = game.storage_key.strip()
        if key:
            return key
        return self._freeze_storage_key(game)

    def _freeze_storage_key(self, game: Game) -> str:
        """按当前名称与存档位置推导目录名并写入 ``games.storage_key``."""
        if game.id is None:  # pragma: no cover - 查询总是带 id
            raise ArchiveManagementError("游戏缺少 id, 无法确定备份目录")
        locations: Sequence[SaveLocation] = self._locations.list_for_game(game.id)
        paths = [location.path for location in locations]
        key = game_folder(game.original_name or game.name, paths)
        frozen = self._games.ensure_storage_key(game.id, key)
        log_action(
            "game.storage_folder",
            basic=True,
            game_id=game.id,
            folder=frozen,
            name=game.name,
        )
        return frozen

    @staticmethod
    def _stamp() -> str:
        """返回备份目录使用的时间前缀(UTC, 便于人工排查)."""
        return datetime.now(UTC).strftime("%Y%m%dT%H%M%S")

    def _reject_unchanged(
        self,
        *,
        action: str,
        game_id: int,
        parent: BackupNode,
        destination: Path,
        content_hash: str,
    ) -> None:
        """存档内容与参照节点一致时丢弃刚生成的快照并抛错.

        "没有变化就不算一次新备份": 手动/分支备份由界面提示用户, 自动备份
        与安全点由调用方静默跳过; 两种情形都会在这里留下 DEBUG 级日志。
        """
        if parent.content_hash != content_hash:
            return
        remove_snapshot(destination)
        # 基础操作级别(DEBUG): 自动备份的重复跳过要留痕但不打扰用户.
        log_action(
            action,
            basic=True,
            game_id=game_id,
            result="skipped_unchanged",
            backup_id=parent.id,
            hash=content_hash[:12],
        )
        raise ContentUnchangedError(
            f"存档内容与备份 #{parent.id} 完全一致, 本次备份已跳过",
            backup_id=parent.id,
            title=(parent.title or parent.branch_name or ""),
        )

    def _resolve_parent(self, game_id: int, parent_id: int | None) -> BackupNode | None:
        """解析父节点: 缺省为当前节点, 显式指定时必须属于同一游戏."""
        if parent_id is None:
            return self.current_node(game_id)
        parent = self._backups.get(parent_id)
        if parent is None or parent.game_id != game_id:
            raise ArchiveManagementError(f"未知的父备份节点: {parent_id}")
        return parent


def _children_by_parent(nodes: list[BackupNode]) -> dict[int, list[int]]:
    """父节点 id -> 子节点 id 列表(只统计已落库、且确实有父节点的节点)."""
    children: dict[int, list[int]] = {}
    for node in nodes:
        if node.id is not None and node.parent_id is not None:
            children.setdefault(node.parent_id, []).append(node.id)
    return children


def _file_entries(result: SnapshotResult) -> list[BackupFileEntry]:
    """把快照清单转换为数据库清单项(``backup_id`` 由仓库回填)."""
    return [
        BackupFileEntry(
            backup_id=0,
            relative_path=entry.relative_path,
            size=entry.size,
            sha256=entry.sha256,
            file_kind=entry.file_kind,
        )
        for entry in result.entries
    ]
