"""UI 依赖的后端接口.

阶段 B 通过注入演示实现驱动界面;后续里程碑(阶段 C-E)用真实用例
实现同一接口替换,从而保持 UI 不依赖具体业务实现(PLAN 4).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from archive_management.ui.models import (
    BackupItem,
    GameDetail,
    GameSummary,
    TaskStatus,
)


@runtime_checkable
class ArchiveService(Protocol):
    """UI 消费的存档管理能力集合."""

    def list_games(self) -> list[GameSummary]:
        """返回所有游戏摘要."""
        ...

    def get_detail(self, game_id: str) -> GameDetail:
        """返回单个游戏的概要数据."""
        ...

    def list_backups(self, game_id: str) -> list[BackupItem]:
        """返回某个游戏的备份节点(含时间线/分支视图所需信息)."""
        ...

    def task_status(self) -> TaskStatus:
        """返回定时任务与运行环境状态."""
        ...

    def current_theme(self) -> str:
        """返回当前主题名(dark/light)."""
        ...

    def set_theme(self, name: str) -> str:
        """切换主题并返回最终生效的主题名."""
        ...

    def run_backup_now(self, game_id: str) -> str:
        """立即创建一次备份;成功返回提示,失败抛出异常."""
        ...

    def run_restore(self, game_id: str, backup_id: str) -> str:
        """恢复到指定备份节点."""
        ...

    def run_create_branch(self, game_id: str, backup_id: str, branch_name: str) -> str:
        """从指定节点创建分支."""
        ...

    def run_export(self, game_id: str) -> str:
        """导出选中游戏."""
        ...
