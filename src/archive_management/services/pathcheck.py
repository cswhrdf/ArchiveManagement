"""存档路径校验服务.

提供路径规范化与可用性探测(存在、类型、可读)的纯函数, 供应用用例
与 UI 复用. 服务不依赖图形环境或网络, 可在无显示环境测试
(PLAN 3.1, 阶段 C 第 3 条).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from archive_management.domain import PathKind


@dataclass(frozen=True)
class PathProbe:
    """一次路径探测的结构化结果."""

    normalized: str
    exists: bool
    is_dir: bool
    is_file: bool
    readable: bool
    kind: PathKind

    @property
    def matches_kind(self) -> bool:
        """探测到的类型是否与期望类型一致."""
        return self.is_dir if self.kind == "directory" else self.is_file

    @property
    def ok(self) -> bool:
        """路径可被安全使用(存在、可读且类型匹配)."""
        return self.exists and self.readable and self.matches_kind

    @property
    def reason_code(self) -> str | None:
        """未通过时的原因代码; 通过时为 None."""
        if not self.exists:
            return "missing"
        if not self.readable:
            return "unreadable"
        if not self.matches_kind:
            return "wrong_kind"
        return None


def normalize_path(raw: str) -> str:
    """把用户输入规范化为绝对路径(展开用户目录, 折叠冗余片段)."""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    return os.path.normpath(str(path))


def probe_path(raw: str, kind: PathKind) -> PathProbe:
    """探测指定路径并返回结构化结果; 不抛出路径相关异常."""
    normalized = normalize_path(raw)
    path = Path(normalized)
    exists = path.exists()
    is_dir = path.is_dir()
    is_file = path.is_file()
    readable = os.access(path, os.R_OK) if exists else False
    return PathProbe(
        normalized=normalized,
        exists=exists,
        is_dir=is_dir,
        is_file=is_file,
        readable=readable,
        kind=kind,
    )
