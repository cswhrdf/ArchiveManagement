"""存档路径校验服务.

提供路径规范化与可用性探测(存在、类型、可读)的纯函数, 供应用用例
与 UI 复用. 服务不依赖图形环境或网络, 可在无显示环境测试。

另外提供三类高风险操作判定, 同样只依赖标准库:

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
        if child.is_symlink():  # 链接本身算一项, 不跟进去数
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


def has_any_content(raw: str) -> bool:
    """这个路径里有没有**至少一项**内容(不跟随符号链接, 读不到就当没有).

    与 :func:`summarize_path` 的判定等价(文件/子目录/符号链接任一存在即为真), 区别是
    **遇到第一项就返回**: 调用方要的经常只是一个布尔(如"要不要建恢复前安全点"), 而完整
    统计会把整棵树走一遍 —— 用户把位置错填成用户主目录时那就是几十万项、几分钟
    (2026-10-03 实测: 木机主目录前 20 秒只数到 18 万项, 队列里还有两百多个目录)。
    """
    path = Path(raw)
    if path.is_symlink():  # 链接本身算一项(与 summarize_path 的 symlinks 计数一致)
        return True
    if path.is_file():
        return True
    if not path.is_dir():
        return False
    try:
        return next(iter(path.iterdir()), None) is not None
    except OSError:  # 读不到(权限/已删): 与 summarize_path 一致地当作“没有内容”
        return False


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
        if part != ".":  # pragma: no branch - Path 会把 `.` 归一掉, 不会出现在 parts 里
            parts.append(part)
    return tuple(parts)


def is_within(child: Path, parent: Path) -> bool:
    r"""判断 ``child`` 是否位于 ``parent`` 之内(含相等).

    盘符风格路径按**大小写不敏感**比较: Windows 上 ``D:\\Steam`` 与 ``d:\\steam``
    是同一处, 而安装目录常由应用清单给出(小写)、候选路径可能来自别处(原样大小写),
    不归一就会让"受保护位置"这条保护静默失效。POSIX 风格路径保持原样。
    """
    child_parts = _lexical_parts(child)
    parent_parts = _lexical_parts(parent)
    if _drive_style(child_parts) or _drive_style(parent_parts):
        child_parts = tuple(part.casefold() for part in child_parts)
        parent_parts = tuple(part.casefold() for part in parent_parts)
    if len(parent_parts) > len(child_parts):
        return False
    return child_parts[: len(parent_parts)] == parent_parts


def _drive_style(parts: tuple[str, ...]) -> bool:
    r"""是否像 Windows 盘符路径(``D:\`` / ``D:``)."""
    return bool(parts) and len(parts[0]) >= 2 and parts[0][1] == ":"


def is_writable_target(raw: str) -> bool:
    """判断目标路径(或其最近的已存在父目录)是否可写."""
    path = Path(raw)
    ancestor = path if path.exists() else path.parent
    while not ancestor.exists() and ancestor != ancestor.parent:
        ancestor = ancestor.parent
    return os.access(ancestor, os.W_OK)


def dangerous_target_reason(
    raw: str, *, protected: tuple[str, ...] = (), protect_subpaths: bool = True
) -> str | None:
    r"""返回拒绝对该路径执行删除/覆盖的原因代码; 安全时返回 None.

    ``protected`` 是绝对不能删除或作为覆盖目标的位置(如应用自己的备份根
    目录): 目标与这些位置重叠或包含它们时都会被拒绝。
    ``protect_subpaths`` 为 False 时, "位于受保护位置**内部**"不再算危险 —— 存档
    目录经常就落在游戏安装目录里(``<安装目录>\saves``), 真正危险的是把整个安装
    目录当目标; 存档候选与存档位置因此按 False 调用。
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
        # 目标就是受保护位置, 或(默认)在它内部, 或包含它: 三种都拒绝.
        if path == guard or (protect_subpaths and is_within(path, guard)):
            return _REASON_PROTECTED
        if is_within(guard, path):
            return _REASON_CONTAINS_PROTECTED
    return None
