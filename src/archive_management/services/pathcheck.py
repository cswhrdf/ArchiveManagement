"""存档路径校验服务.

提供路径规范化与可用性探测(存在、类型、可读)的纯函数, 供应用用例
与 UI 复用. 服务不依赖图形环境或网络, 可在无显示环境测试
(PLAN 3.1, 阶段 C 第 3 条).

阶段 E 另外用到三类判定, 同样只依赖标准库:

- :func:`summarize_path`: 汇总目录/文件的条目与体积, 用于删除前展示影响范围;
- :func:`is_within`: 路径包含关系, 防止恢复或删除越出用户确认过的范围;
- :func:`dangerous_target_reason`: 识别盘符根目录、用户主目录、受保护目录等
  高风险目标, 避免把整个磁盘或应用自己的备份区当成存档删除。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from archive_management.domain import PathKind

# 路径被拒绝的原因代码(由 UI 映射为文案).
_REASON_DRIVE_ROOT = "drive_root"
_REASON_USER_HOME = "user_home"
_REASON_PROTECTED = "protected"
_REASON_NOT_ABSOLUTE = "not_absolute"
_REASON_CONTAINS_PROTECTED = "contains_protected"


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


@dataclass(frozen=True)
class PathSummary:
    """一个路径的内容汇总(条数与体积)."""

    files: int = 0
    directories: int = 0
    symlinks: int = 0
    total_size: int = 0

    @property
    def entries(self) -> int:
        """返回文件与符号链接的数量之和."""
        return self.files + self.symlinks


@dataclass
class _Tally:
    """目录遍历时累计的文件/目录/符号链接数与总字节数."""

    files: int = 0
    directories: int = 0
    symlinks: int = 0
    total_size: int = 0

    def visit(self, child: Path, pending: list[Path]) -> None:
        """记录一个子项; 目录会加入待遍历队列."""
        if child.is_symlink():
            self.symlinks += 1
            return
        if child.is_dir():
            self.directories += 1
            pending.append(child)
            return
        self.files += 1
        try:
            self.total_size += child.stat().st_size
        except OSError:
            return

    def summary(self) -> PathSummary:
        """返回不可变的汇总结果."""
        return PathSummary(
            files=self.files,
            directories=self.directories,
            symlinks=self.symlinks,
            total_size=self.total_size,
        )


def summarize_path(raw: str) -> PathSummary:
    """汇总路径内容: 文件/目录/符号链接数量与总字节数.

    路径不存在或不是目录时返回空汇总; 不跟随符号链接, 避免把统计范围
    越出用户确认过的存档路径, 也避免链接环导致无限递归。
    """
    path = Path(raw)
    if path.is_symlink():
        return PathSummary(symlinks=1)
    if path.is_file():
        try:
            return PathSummary(files=1, total_size=path.stat().st_size)
        except OSError:
            return PathSummary(files=1)
    if not path.is_dir():
        return PathSummary()
    tally = _Tally()
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError:
            continue
        for child in children:
            tally.visit(child, stack)
    return tally.summary()


def _lexical_parts(path: Path) -> tuple[str, ...]:
    """把路径拆成词法片段(自行处理 ``..`` 与 ``.``, 不访问文件系统)."""
    parts: list[str] = []
    for part in Path(path).parts:
        if part in ("", "/", os.sep, os.altsep):
            continue
        if part == "..":
            if parts:
                parts.pop()
            continue
        if part != ".":
            parts.append(part)
    return tuple(parts)


def is_within(child: Path, parent: Path) -> bool:
    """判断 ``child`` 是否位于 ``parent`` 之内(含相等)."""
    child_parts = _lexical_parts(child)
    parent_parts = _lexical_parts(parent)
    if len(parent_parts) > len(child_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


def is_writable_target(raw: str) -> bool:
    """判断目标路径(或其最近的已存在父目录)是否可写."""
    path = Path(raw)
    ancestor = path if path.exists() else path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    return os.access(ancestor, os.W_OK)


def dangerous_target_reason(raw: str, *, protected: tuple[str, ...] = ()) -> str | None:
    """返回拒绝对该路径执行删除/覆盖的原因代码; 安全时返回 None.

    ``protected`` 是绝对不能删除或作为覆盖目标的位置(如应用自己的备份根
    目录): 目标与这些位置重叠或包含它们时都会被拒绝。
    """
    path = Path(raw)
    if not path.is_absolute():
        return _REASON_NOT_ABSOLUTE
    if path.parent == path:
        return _REASON_DRIVE_ROOT
    if path == Path.home():
        return _REASON_USER_HOME
    for item in protected:
        guard = Path(item)
        if not str(guard).strip():
            continue
        # 目标就是受保护位置、位于其内部, 或包含受保护位置: 三种都拒绝.
        if path == guard or is_within(path, guard):
            return _REASON_PROTECTED
        if is_within(guard, path):
            return _REASON_CONTAINS_PROTECTED
    return None
