"""UI 依赖的后端接口.

阶段 B 通过注入演示实现驱动界面;后续里程碑(阶段 C-E)用真实用例
实现同一接口替换,从而保持 UI 不依赖具体业务实现(PLAN 4).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from archive_management.application.locations import LocationRemovalPlan
from archive_management.application.restore import RestorePlan
from archive_management.domain import DEFAULT_KEEP_AUTO, DeletionPlan, PathKind
from archive_management.ui.models import (
    BackupItem,
    GameDetail,
    GameSummary,
    LocationItem,
    ScheduleItem,
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

    def task_status(self, game_id: str | None = None) -> TaskStatus:
        """返回定时任务与运行环境状态(可按游戏查看周期配置)."""
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

    def run_restore(
        self,
        game_id: str,
        backup_id: str,
        *,
        safety_point: bool = True,
        force: bool = False,
    ) -> str:
        """把备份内容写回原始存档位置, 并把当前节点切到该备份.

        ``safety_point`` 为 True 时恢复前先创建一份安全点备份(只在时间线视图
        展示), 使恢复可逆; ``force`` 用于在检测到游戏进程运行时由用户明确
        强制执行。
        """
        ...

    def preview_restore(self, game_id: str, backup_id: str) -> RestorePlan:
        """恢复预检: 快照完整性、写回目标、多余文件与游戏进程状态."""
        ...

    def run_create_branch(self, game_id: str, backup_id: str, branch_name: str) -> str:
        """从指定节点创建分支."""
        ...

    def run_export(self, game_id: str) -> str:
        """导出选中游戏."""
        ...

    # -- 游戏与存档位置管理(阶段 C) ---------------------------------------

    def add_game(self, name: str) -> GameSummary:
        """新增一个游戏, 返回其摘要."""
        ...

    def update_game(self, game_id: str, name: str) -> GameSummary:
        """重命名游戏并返回其摘要."""
        ...

    def delete_game(self, game_id: str) -> None:
        """删除游戏记录及其存档位置."""
        ...

    def set_game_enabled(self, game_id: str, enabled: bool) -> GameSummary:
        """启用/停用一个游戏, 返回其摘要."""
        ...

    def list_locations(self, game_id: str) -> list[LocationItem]:
        """返回某游戏的全部存档位置及校验状态."""
        ...

    def add_location(self, game_id: str, *, path: str, kind: PathKind) -> LocationItem:
        """新增并校验一个存档位置; 路径不可用或重复时抛出异常."""
        ...

    def update_location(
        self,
        location_id: str,
        *,
        path: str | None = None,
        kind: PathKind | None = None,
    ) -> LocationItem:
        """修改存档位置的路径/类型并重新校验."""
        ...

    def remove_location(self, location_id: str) -> None:
        """删除一个存档位置记录."""
        ...

    def set_primary_location(self, game_id: str, location_id: str) -> LocationItem:
        """把某位置设为主位置(其余位置清除主标记)."""
        ...

    def verify_location(self, location_id: str) -> LocationItem:
        """重新校验一个存档位置并返回最新状态."""
        ...

    def preview_location_removal(self, location_id: str) -> LocationRemovalPlan:
        """删除原始存档目录前的预检: 影响范围与路径安全判定."""
        ...

    def delete_save_location(self, location_id: str, *, confirm_name: str) -> str:
        """把原始存档目录移入系统回收站并删除该位置记录(阶段 E 第 5 条)."""
        ...

    # -- 备份、调度与生命周期(阶段 D) ------------------------------------

    def set_schedule(
        self,
        game_id: str,
        interval_text: str,
        *,
        enabled: bool = True,
        keep_auto: int = DEFAULT_KEEP_AUTO,
    ) -> TaskStatus:
        """设置或取消某游戏的定期备份, 返回最新任务状态."""
        ...

    def list_schedules(self) -> list[ScheduleItem]:
        """返回全部游戏的定时备份配置(供全局任务窗口展示/编辑)."""
        ...

    def storage_usage(self) -> int:
        """返回备份存储当前占用的字节数(供状态栏显示)."""
        ...

    def plan_delete(self, game_id: str, backup_id: str) -> DeletionPlan:
        """返回删除某备份的计划(是否需要二次确认由 mode 决定)."""
        ...

    def run_delete_backup(self, game_id: str, backup_id: str) -> str:
        """按分支树删除备份: 同线路节点让后续上移, 分支根节点连带子分支."""
        ...

    def rename_backup(
        self, game_id: str, backup_id: str, *, title: str, note: str
    ) -> BackupItem:
        """修改备份的名称与描述, 返回更新后的列表项."""
        ...

    def cancel_active(self) -> bool:
        """请求取消当前正在进行的后台操作; 无进行中操作时返回 False."""
        ...

    def shutdown(self) -> None:
        """释放后台资源(调度器、监听器); 幂等, 供应用退出时调用."""
        ...
