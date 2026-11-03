"""在系统文件管理器里打开一个目录(跨平台的"打开位置"按钮).

做成独立服务并经参数注入设置窗口: 一处实现三个平台, 界面与用例都能换替身。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from archive_management.services.platforms import current_platform

logger = logging.getLogger(__name__)


def reveal_in_file_manager(path: str) -> None:
    """用系统文件管理器打开 ``path``(文件则打开它所在的目录).

    目录还不存在(比如"下次启动"才会建的备份根)时退一步打开最近的已存在父
    目录 —— 用户点这个按钮的意图是"到那一带看看", 而不是"必须精确到这个
    尚不存在的目录"。打不开只记日志: 这是个便利动作, 不值得打断用户。
    """
    target = Path(path)
    while not target.exists() and target != target.parent:
        target = target.parent
    try:
        family = current_platform()
        if family == "windows":
            # os.startfile 只在 Windows 上存在, 放在分支里避免其他平台导入失败。
            import os

            os.startfile(str(target))  # noqa: S606 - 打开的是本机目录路径, 不是命令
        elif family == "macos":
            subprocess.run(  # noqa: S603 - 固定命令 + 路径参数, 无 shell
                ["open", str(target)],  # noqa: S607 - PATH 解析系统工具是平台惯例
                check=False,
            )
        else:
            subprocess.run(  # noqa: S603 - 固定命令 + 路径参数, 无 shell
                ["xdg-open", str(target)],  # noqa: S607 - PATH 解析系统工具是平台惯例
                check=False,
            )
    except OSError as exc:
        logger.warning("打开位置失败(%s): %s", target, exc)
