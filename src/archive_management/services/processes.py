"""游戏进程探测.

可选地检测游戏进程, 避免游戏运行时恢复或覆盖存档; 检测失败时仍允许用户明确
强制执行. 这里把进程枚举封装成可注入的提供者: 默认实现
惰性导入 ``psutil``, 导入或枚举失败时返回 ``checked=False``(而不是抛错),
让调用方区分"确认没在运行"和"无法确认"两种状态。
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

# 归一化时只保留小写字母与数字, 便于把
# "Outer Wilds" 与 "OuterWilds.exe" 这类写法匹配起来.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# 可执行文件后缀: Windows 上是 .exe, macOS 上进程名有时带 .app(应用包名),
# 比较前统一去掉, 避免同一款游戏因平台写法不同而漏判。
_EXECUTABLE_SUFFIXES = (".exe", ".app", ".bin", ".run")

# 进程名提供者: 返回当前可见的进程名; 失败时抛出异常.
ProcessNameProvider = Callable[[], Iterable[str]]


@dataclass(frozen=True)
class ProcessProbe:
    """一次游戏进程探测的结果."""

    checked: bool
    running: bool
    matches: tuple[str, ...] = ()


def probe_game_process(
    name: str, *, provider: ProcessNameProvider | None = None
) -> ProcessProbe:
    """判断与游戏名匹配的进程是否在运行(单名称便捷入口)."""
    return probe_processes([name], provider=provider)


def _process_needles(names: Sequence[str]) -> list[str]:
    """归一化候选名称, 丢掉过短的(少于 3 个字符容易误报)."""
    return [
        needle for needle in (_normalize(name) for name in names) if len(needle) >= 3
    ]


def _read_process_names(read: ProcessNameProvider) -> list[str] | None:
    """枚举当前进程名; 提供者异常时返回 None(调用方按"没检查到"处理)."""
    try:
        return [raw for raw in read() if raw]
    except Exception:
        return None


def _matches_any(raw: str, needles: Sequence[str]) -> bool:
    """进程名与任一候选匹配(归一化后互相包含)."""
    normalized = _normalize(raw)
    return any(_matches(needle, normalized) for needle in needles)


def probe_processes(
    names: Sequence[str], *, provider: ProcessNameProvider | None = None
) -> ProcessProbe:
    """判断多个候选名称中是否有进程在运行(只枚举一次进程表).

    匹配规则是"归一化后互相包含": 候选 ``Outer Wilds`` 能匹配到
    ``OuterWilds.exe``; 名称过短或为空时直接跳过, 避免误报。
    """
    needles = _process_needles(names)
    if not needles:
        return ProcessProbe(checked=False, running=False)
    raw_names = _read_process_names(
        provider if provider is not None else psutil_process_names
    )
    if raw_names is None:
        return ProcessProbe(checked=False, running=False)
    matches = sorted({raw for raw in raw_names if _matches_any(raw, needles)})
    return ProcessProbe(checked=True, running=bool(matches), matches=tuple(matches))


def psutil_process_names() -> Iterable[str]:
    """用 psutil 枚举当前进程名(惰性导入)."""
    import psutil

    names: list[str] = []
    for process in psutil.process_iter(["name"]):
        value = (process.info or {}).get("name")
        if isinstance(value, str):
            names.append(value)
    return names


def _normalize(raw: str) -> str:
    """去掉可执行后缀与分隔符, 只留下用于比较的字符."""
    lowered = raw.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return _NON_ALNUM.sub("", lowered)


def _matches(needle: str, candidate: str) -> bool:
    """归一化后的互相包含判定(带最小长度保护)."""
    if len(candidate) < 3:
        return False
    return needle in candidate or candidate in needle
