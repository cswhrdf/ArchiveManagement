"""进程名归一化与匹配(纯函数).

游戏名与进程名写法不同(``Outer Wilds`` / ``OuterWilds.exe``), 因此比较前统一
归一化: 小写、去掉可执行后缀、只保留字母与数字。中文等非 ASCII 名字归一化后
为空, 会被整条丢掉 —— 这是刻意的: 拿不准宁可不判断。

归一化与"互相包含"这两条规则同时被两处使用: 全库监控的归属判定与恢复前的单款
进程检查。它们不依赖任何基础设施, 因此独立成领域模块, 由
:mod:`archive_management.services.processes` 与
:mod:`archive_management.domain.activation` 共用。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

# 归一化时只保留小写字母与数字, 便于把
# "Outer Wilds" 与 "OuterWilds.exe" 这类写法匹配起来.
_NON_ALNUM = re.compile(r"[^0-9a-z]+")

# 可执行文件后缀: Windows 上是 .exe, macOS 上进程名有时带 .app(应用包名),
# 比较前统一去掉, 避免同一款游戏因平台写法不同而漏判。
_EXECUTABLE_SUFFIXES = (".exe", ".app", ".bin", ".run")

# 短于这个长度的名字不参与匹配: 三两个字母的进程名太容易误报。
MIN_NAME_LENGTH = 3


def normalize_name(raw: str) -> str:
    """去掉可执行后缀与分隔符, 只留下用于比较的字符."""
    lowered = raw.lower()
    for suffix in _EXECUTABLE_SUFFIXES:
        if lowered.endswith(suffix):
            lowered = lowered[: -len(suffix)]
            break
    return _NON_ALNUM.sub("", lowered)


def names_match(needle: str, candidate: str) -> bool:
    """归一化后的互相包含判定(带最小长度保护)."""
    if len(candidate) < MIN_NAME_LENGTH:
        return False
    return needle in candidate or candidate in needle


def name_needles(names: Sequence[str]) -> tuple[str, ...]:
    """把候选名称归一化成可用的针(去重、丢掉过短或归一化后为空的)."""
    return tuple(
        dict.fromkeys(
            needle
            for needle in (normalize_name(name) for name in names)
            if len(needle) >= MIN_NAME_LENGTH
        )
    )


__all__ = [
    "MIN_NAME_LENGTH",
    "name_needles",
    "names_match",
    "normalize_name",
]
