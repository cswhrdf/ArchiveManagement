"""领域层: 游戏、存档位置、备份节点等领域模型."""

from archive_management.domain.deletion import (
    DEFAULT_KEEP_AUTO,
    DeletionMode,
    DeletionPlan,
    auto_prune_ids,
    descendant_ids,
    plan_deletion,
)
from archive_management.domain.entities import (
    BackupFileEntry,
    BackupNode,
    FileKind,
    Game,
    NodeKind,
    PathKind,
    SaveLocation,
    SaveSource,
    ScheduledJob,
)
from archive_management.domain.steam_data import (
    STEAM_DATA_FORMAT_VERSION,
    SteamDataFile,
    SteamGameEntry,
)
from archive_management.domain.tree import (
    TreeInput,
    TreeNode,
    branch_lineage,
    build_tree,
    keep_surviving,
    tree_depths,
    tree_inputs,
)

__all__ = [
    "DEFAULT_KEEP_AUTO",
    "STEAM_DATA_FORMAT_VERSION",
    "BackupFileEntry",
    "BackupNode",
    "DeletionMode",
    "DeletionPlan",
    "FileKind",
    "Game",
    "NodeKind",
    "PathKind",
    "SaveLocation",
    "SaveSource",
    "ScheduledJob",
    "SteamDataFile",
    "SteamGameEntry",
    "TreeInput",
    "TreeNode",
    "auto_prune_ids",
    "branch_lineage",
    "build_tree",
    "descendant_ids",
    "keep_surviving",
    "plan_deletion",
    "tree_depths",
    "tree_inputs",
]
