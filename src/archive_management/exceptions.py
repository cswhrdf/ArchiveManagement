"""应用异常分类.

业务、基础设施与 UI 层抛出的可预期异常统一继承 :class:`ArchiveManagementError`,
便于外层按类型处理并提供可恢复的错误状态.
"""

from __future__ import annotations


class ArchiveManagementError(Exception):
    """所有可预期业务/基础设施错误的基类."""


class ConfigurationError(ArchiveManagementError):
    """配置缺失、非法或无法加载."""


class DatabaseError(ArchiveManagementError):
    """数据库连接、迁移或查询失败."""


class StorageError(ArchiveManagementError):
    """备份存储相关的文件系统错误(空间不足、权限不足、路径消失等)."""


class SnapshotError(StorageError):
    """生成或校验文件快照失败(含临时目录残留、哈希不一致)."""


class OperationCancelledError(ArchiveManagementError):
    """用户主动取消了正在进行的操作."""


class ContentUnchangedError(ArchiveManagementError):
    """存档内容与参照的备份节点完全一致, 因此不需要新建备份.

    手动/分支备份由界面提示用户"存档未变化"; 自动备份与恢复前的安全点
    直接跳过(写入 DEBUG 级日志), 不打扰用户。
    """

    def __init__(
        self, message: str, *, backup_id: int | None = None, title: str = ""
    ) -> None:
        """记录与之相同的那份备份, 供界面拼出可读提示."""
        super().__init__(message)
        self.backup_id = backup_id
        self.title = title

    @property
    def target_label(self) -> str:
        """返回与之相同的备份节点展示名(如 "手动备份" 或 "#4")."""
        return self.title or ("—" if self.backup_id is None else f"#{self.backup_id}")


class HotkeyError(ArchiveManagementError):
    """全局快捷键注册或监听失败(权限不足、被占用、无图形环境)."""


class SchedulingError(ArchiveManagementError):
    """定时备份任务的创建、暂停或释放失败."""


class SteamIntegrationError(ArchiveManagementError):
    """Steam 探测或 API 相关的可恢复错误."""


class PlatformIntegrationError(ArchiveManagementError):
    """平台数据格式不兼容或字段非法(适配器与领域模型之间的契约错误)."""


class SaveCandidateError(ArchiveManagementError):
    """存档路径候选被拒绝(危险目标或处理进度不允许确认)."""


class ArtworkError(ArchiveManagementError):
    """封面/图标的下载、内容校验或缓存写入失败."""


class PackageError(StorageError):
    """导出包的读写失败(格式非法、内容与清单不一致、解包越界或超限)."""
