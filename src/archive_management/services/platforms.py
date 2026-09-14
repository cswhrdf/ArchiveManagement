"""运行平台识别(Windows / macOS / Linux).

不同平台上"本地游戏装在哪儿"的事实来源并不相同: Windows 把安装位置写进注册表
(因此才有注册表探测), macOS 与 Linux 既没有注册表, 也没有 GOG/Ubisoft 的
清单文件, 只有各平台自己或客户端自己的目录结构。为了让"哪些功能在当前平台
可用"成为显式事实, 这里集中提供平台识别, 由本地探测与全局快捷键模块共用。

识别本身是纯函数: :func:`detect_platform` 接受 ``sys.platform`` 字符串, 测试
可以传入任意取值, 不需要真的运行在 macOS 上。
"""

from __future__ import annotations

import sys
from typing import Literal

# 平台族: 其余类 Unix 系统(FreeBSD 等)按 Linux 处理, 路径规则一致.
PlatformFamily = Literal["windows", "macos", "linux"]

# 平台族的展示名, 用于日志与 ``doctor`` 诊断输出.
PLATFORM_LABELS: dict[str, str] = {
    "windows": "Windows",
    "macos": "macOS",
    "linux": "Linux",
}


def detect_platform(value: str | None = None) -> PlatformFamily:
    """把 ``sys.platform`` 取值归一化为平台族."""
    raw = (sys.platform if value is None else value).lower()
    if raw.startswith("win"):
        return "windows"
    if raw.startswith("darwin"):
        return "macos"
    return "linux"


def current_platform() -> PlatformFamily:
    """返回当前进程所在平台族."""
    return detect_platform()


def platform_label(platform: PlatformFamily | None = None) -> str:
    """返回平台族的展示名."""
    return PLATFORM_LABELS.get(platform or current_platform(), "未知平台")


def has_windows_registry(platform: PlatformFamily | None = None) -> bool:
    """判断该平台是否存在注册表(只有 Windows 有).

    注册表相关的探测(GOG、Ubisoft 的安装位置、Steam 的安装路径)只在
    Windows 上有意义, 其它平台必须走各自的目录规则或监控目录。
    """
    return (platform or current_platform()) == "windows"
