"""应用发布信息与运行时资源定位.

集中管理应用标识与版本来源: 源码运行时与打包产物(PyInstaller)都通过
包元数据读取版本号, 避免在多个入口重复维护版本常量.
"""

from __future__ import annotations

from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _distribution_version

APP_ID = "archive-management"
APP_NAME = "ArchiveManagement"
APP_DISPLAY_NAME = "存档管理工具"
ORG_NAME = "cswhrdf"


@lru_cache(maxsize=1)
def package_version() -> str:
    """返回已安装发行版的版本号;未安装(如直接在源码树运行)时回退为开发版本."""
    try:
        return _distribution_version(APP_ID)
    except PackageNotFoundError:
        return "0.0.0.dev0"
