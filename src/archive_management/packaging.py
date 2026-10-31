"""应用发布信息与运行时资源定位.

集中管理应用标识与版本来源: 源码运行时与打包产物(PyInstaller)都通过
这里的版本常量读取, 避免在多个入口重复维护版本号.
"""

from __future__ import annotations

APP_ID = "archive-management"
APP_NAME = "ArchiveManagement"
APP_DISPLAY_NAME = "存档管理工具"
ORG_NAME = "cswhrdf"
__version__ = "0.0.1"


def package_version() -> str:
    """返回应用版本号."""
    return __version__
