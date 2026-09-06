"""应用异常分类.

业务、基础设施与 UI 层抛出的可预期异常统一继承 :class:`ArchiveManagementError`,
便于外层按类型处理并提供可恢复的错误状态(PLAN 第 7 节).
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


class SteamIntegrationError(ArchiveManagementError):
    """Steam 探测或 API 相关的可恢复错误."""
