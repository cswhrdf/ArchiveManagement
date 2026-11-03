"""在系统文件管理器里打开一个目录(跨平台的"打开位置"按钮).

做成独立服务并经参数注入设置窗口: 一处实现三个平台, 界面与用例都能换替身。
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

from archive_management.services.platforms import current_platform

logger = logging.getLogger(__name__)


def _nearest_existing(path: Path) -> Path:
    """退到最近的已存在祖先(最多退到磁盘根, 根不存在时也停在根)."""
    while not path.exists() and path != path.parent:
        path = path.parent
    return path


def _resolve_target(path: Path) -> tuple[Path, Path | None]:
    """返回(要打开的目录, 要高亮的文件或 None).

    文件行落到父目录并带上高亮; 不存在的目标退最近已存在父目录 ——
    选中信息只在目标真实存在时才有意义。
    """
    if path.exists() and path.is_file():
        return path.parent, path
    return _nearest_existing(path), None


def _reveal_windows(target: Path, highlight: Path | None) -> None:
    # os.startfile 只在 Windows 上存在, 放在单独函数里避免其他平台导入失败。
    import os

    if highlight is not None:
        subprocess.run(  # noqa: S603 - 固定命令 + 路径参数, 无 shell
            ["explorer", f"/select,{highlight}"],  # noqa: S607 - 平台惯例
            check=False,
        )
    else:
        os.startfile(str(target))  # noqa: S606 - 打开的是本机目录路径, 不是命令


def _reveal_macos(target: Path, highlight: Path | None) -> None:
    args = (
        ["open", "-R", str(highlight)]
        if highlight is not None
        else ["open", str(target)]
    )
    subprocess.run(  # noqa: S603 - 固定命令 + 路径参数, 无 shell
        args,
        check=False,
    )


def _reveal_linux(target: Path, highlight: Path | None) -> None:
    # xdg-open 没有统一的"选中"惯例, 能到所在目录即可。
    if highlight is not None:
        target = highlight.parent
    subprocess.run(  # noqa: S603 - 固定命令 + 路径参数, 无 shell
        ["xdg-open", str(target)],  # noqa: S607 - PATH 解析系统工具是平台惯例
        check=False,
    )


def reveal_in_file_manager(path: str) -> None:
    """打开 ``path``: 目录就打开它, 文件就打开所在目录并尽量把目标选中.

    "打开位置"的意图是"到那一带看看": 对文件行(数据库/配置/日志)直接打开
    文件本体会变成"用什么程序打开它"(.json 弹编辑器、.db 没有关联程序),
    所以文件一律落到所在目录 —— Windows 与 macOS 的文件管理器还支持顺手
    高亮目标(``explorer /select,``、``open -R``), Linux 没有统一惯例就只开目录。

    目标还不存在(比如"下次启动"才会建的备份根)时退一步打开最近的已存在父
    目录 —— 用户点这个按钮的意图是"到那一带看看", 而不是"必须精确到这个
    尚不存在的目录"。打不开只记日志: 这是个便利动作, 不值得打断用户。
    """
    target, highlight = _resolve_target(Path(path))
    try:
        family = current_platform()
        if family == "windows":
            _reveal_windows(target, highlight)
        elif family == "macos":
            _reveal_macos(target, highlight)
        else:
            _reveal_linux(target, highlight)
    except OSError as exc:
        logger.warning("打开位置失败(%s): %s", path, exc)
