"""游戏状态与可用动作的规则.

规则集中在这里而不是散落在各个界面: 归档的游戏只保留"删除游戏、导出游戏、
取消归档、打开详情"四件事(管理窗口本身仍然可以打开 —— 它是删除游戏的唯一
入口), 其余动作由界面与后端共同拒绝。

"启用/停用"与"归档"是两个独立开关:

* **归档** 表达"这款游戏我已经收起来了", 与备份数据无关, 只影响可用动作;
* **启用** 表达"当前正在玩的就是这一款", 全局至多一个 —— 快捷键与定时备份
  只对启用且未归档的游戏生效(:func:`is_active`)。
"""

from __future__ import annotations

from typing import Literal

from archive_management.domain.entities import Game

# 界面与后端可以发起的动作. 这里的取值是规则表与调用点之间的契约: 增删动作
# 必须同步修改 ARCHIVED_ACTIONS 与守卫用例.
GameAction = Literal[
    "detail",
    "delete",
    "export",
    "archive",
    "manage",
    "backup",
    "restore",
    "branch",
    "backup_edit",
    "backup_delete",
    "locations",
    "tags",
    "rename",
    "schedule",
    "enable",
]

# 归档后仍然允许的动作, 其余一律不可用.
# ``manage``(打开管理窗口)留在里面, 因为归档游戏只有从这里才能执行"删除游戏"。
ARCHIVED_ACTIONS: frozenset[str] = frozenset(
    {"detail", "delete", "export", "archive", "manage"}
)


def action_allowed(action: GameAction, *, archived: bool) -> bool:
    """判断某个动作在给定归档状态下是否可用."""
    return action in ARCHIVED_ACTIONS if archived else True


def is_active(game: Game) -> bool:
    """该游戏是否会响应快捷键与定时备份.

    启用与归档是两个开关, 这里再判一次归档: 归档时会把游戏停用并暂停定时备份
    (见 :class:`~archive_management.ui.sql_backend.SqlArchiveService`), 但库里的
    数据可能来自旧版本或手工修改, 执行前仍然要按最终状态判断。
    """
    return game.enabled and not game.archived
