"""存档管理工具(ArchiveManagement).

一个面向桌面用户的游戏存档管理工具: 管理游戏与存档位置、定期备份、分支树与
时间线视图、按需恢复, 以及本地游戏库(发现)管理。功能与用法见项目根目录的
``README.md``, 架构与模块说明见 ``docs/`` 目录。
"""

from archive_management.packaging import APP_DISPLAY_NAME, APP_NAME, package_version

__all__ = [
    "APP_DISPLAY_NAME",
    "APP_NAME",
    "__version__",
    "package_version",
]

__version__ = package_version()
