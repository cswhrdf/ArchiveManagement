"""存档管理工具(ArchiveManagement).

一个面向桌面用户的游戏存档管理工具, 详见项目根目录 ``PLAN.md``.
"""

from archive_management.packaging import APP_DISPLAY_NAME, APP_NAME, package_version

__all__ = [
    "APP_DISPLAY_NAME",
    "APP_NAME",
    "__version__",
    "package_version",
]

__version__ = package_version()
