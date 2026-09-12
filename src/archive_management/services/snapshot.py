"""文件快照服务: 复制、哈希校验与原子提交(阶段 D 第 1 条).

一次快照把若干存档位置(文件或目录)完整复制到应用自有的备份根目录之下,
并记录逐文件清单与整体内容哈希. 关键约定:

1. **绝不覆盖**: 目标目录已存在时直接失败, 不会把新快照写进旧备份.
2. **临时目录**: 所有写入先落在目标同级的 ``*.partial-*`` 目录中, 复制完成
   并通过哈希复核后, 才用 ``os.replace`` 原子改名为正式目录.
3. **失败即清理**: 任一环节异常都会删除临时目录, 不会留下可被误认为完整
   备份的半成品.
4. **符号链接不跟随**: 链接按 ``symlink`` 类型记入清单但不复制内容, 避免把
   快照范围越出用户确认过的存档路径.

模块只依赖标准库, 不导入 Tkinter, 可在无显示环境测试.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from archive_management.domain import FileKind, PathKind
from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
    SnapshotError,
)

SNAPSHOT_FORMAT_VERSION = 1
MANIFEST_FILENAME = "snapshot.json"
_CHUNK_SIZE = 1024 * 1024
_DIRECTORY_SHA256 = hashlib.sha256(b"directory").hexdigest()
_SYMLINK_SHA256 = hashlib.sha256(b"symlink").hexdigest()

# 快照进度回调: 接收 0..1 的比例与人类可读说明.
# 用 Callable 别名而不是 Protocol: 这样普通函数/lambda/可调用对象都能直接
# 传参, 不会因为 Protocol 的结构匹配限制而需要额外包装.
ProgressCallback = Callable[[float, str], None]


@dataclass(frozen=True)
class SnapshotSource:
    """一个待快照的存档位置."""

    path: str
    kind: PathKind
    index: int = 0

    def __post_init__(self) -> None:
        """校验来源序号非负."""
        if self.index < 0:
            raise ValueError("来源序号不能为负数")


@dataclass(frozen=True)
class SnapshotEntry:
    """快照清单中的一条记录."""

    relative_path: str
    size: int = 0
    sha256: str = ""
    file_kind: FileKind = "file"
    link_target: str | None = None

    def as_dict(self) -> dict[str, object]:
        """转换为写入 manifest 的 JSON 兼容字典."""
        return {
            "relative_path": self.relative_path,
            "size": self.size,
            "sha256": self.sha256,
            "file_kind": self.file_kind,
            "link_target": self.link_target,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> SnapshotEntry:
        """从 manifest 字典还原清单项, 字段非法时抛出异常."""
        relative_path = raw.get("relative_path")
        raw_size = raw.get("size", 0)
        sha256 = raw.get("sha256")
        raw_kind = raw.get("file_kind")
        raw_target = raw.get("link_target")
        if not isinstance(relative_path, str) or not relative_path:
            raise SnapshotError(f"快照清单项缺少相对路径: {raw!r}")
        if not isinstance(raw_size, int) or isinstance(raw_size, bool):
            raise SnapshotError(f"快照清单项 size 非法: {raw!r}")
        if not isinstance(sha256, str):
            raise SnapshotError(f"快照清单项 sha256 非法: {raw!r}")
        if raw_target is not None and not isinstance(raw_target, str):
            raise SnapshotError(f"快照清单项 link_target 非法: {raw!r}")
        kind: FileKind = "file"
        if raw_kind == "symlink":
            kind = "symlink"
        elif raw_kind == "directory":
            kind = "directory"
        elif raw_kind != "file":
            raise SnapshotError(f"未知的清单文件类型: {raw_kind!r}")
        return cls(
            relative_path=relative_path,
            size=raw_size,
            sha256=sha256,
            file_kind=kind,
            link_target=raw_target,
        )


@dataclass(frozen=True)
class SnapshotResult:
    """一次成功快照的结果."""

    root: Path
    entries: tuple[SnapshotEntry, ...]
    content_hash: str
    total_size: int

    def file_count(self) -> int:
        """返回清单中真实文件的数量(不含目录与符号链接)."""
        return sum(1 for entry in self.entries if entry.file_kind == "file")


@dataclass(frozen=True)
class SnapshotVerification:
    """一次快照完整性校验的结果."""

    ok: bool
    checked: int
    missing: tuple[str, ...] = ()
    mismatched: tuple[str, ...] = ()
    reason: str | None = None


def create_snapshot(
    sources: Sequence[SnapshotSource],
    destination: Path,
    *,
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
) -> SnapshotResult:
    """把 ``sources`` 复制为 ``destination`` 处的完整快照并返回清单.

    ``destination`` 已存在时抛出 :class:`SnapshotError`; 复制过程中任何失败
    都会清理临时目录, 使调用方可以安全地重试或放弃.
    """
    if not sources:
        raise SnapshotError("没有可备份的存档位置")
    destination = Path(destination)
    if destination.exists():
        raise SnapshotError(f"备份目标已存在, 拒绝覆盖: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.parent / f"{destination.name}.partial-{uuid4().hex}"
    try:
        entries = _copy_into(temporary, sources, progress=progress, cancelled=cancelled)
        _report(progress, 0.95, "校验快照清单")
        content_hash = content_hash_of(entries)
        manifest = _manifest(sources, entries, content_hash)
        (temporary / MANIFEST_FILENAME).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        _report(progress, 1.0, "提交快照")
        temporary.replace(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return SnapshotResult(
        root=destination,
        entries=tuple(entries),
        content_hash=content_hash,
        total_size=sum(entry.size for entry in entries if entry.file_kind == "file"),
    )


def verify_snapshot(root: Path, *, deep: bool = True) -> SnapshotVerification:
    """按 manifest 校验快照: 检查缺失项与哈希不一致项.

    ``deep=False`` 时只检查清单项是否存在, 不重新计算文件哈希, 供 UI 列表
    等高频只读场景使用.
    """
    root = Path(root)
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.is_file():
        return SnapshotVerification(
            ok=False, checked=0, reason=f"缺少清单文件: {MANIFEST_FILENAME}"
        )
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    entries = read_manifest_entries(raw)
    missing: list[str] = []
    mismatched: list[str] = []
    checked = 0
    for entry in entries:
        target = root / entry.relative_path
        if entry.file_kind == "directory":
            if not target.is_dir():
                missing.append(entry.relative_path)
            checked += 1
            continue
        # 符号链接只记录目标字符串, 不复制内容, 因此不做存在性检查.
        if entry.file_kind == "symlink":
            checked += 1
            continue
        if not target.exists():
            missing.append(entry.relative_path)
            continue
        checked += 1
        if deep and sha256_of_file(target) != entry.sha256:
            mismatched.append(entry.relative_path)
    if content_hash_of(entries) != str(raw.get("content_hash", "")):
        mismatched.append(MANIFEST_FILENAME)
    return SnapshotVerification(
        ok=not missing and not mismatched,
        checked=checked,
        missing=tuple(missing),
        mismatched=tuple(mismatched),
    )


def read_manifest_entries(raw: object) -> tuple[SnapshotEntry, ...]:
    """从 manifest 原始数据中读取清单项."""
    if not isinstance(raw, dict):
        raise SnapshotError("快照清单顶层必须是对象")
    version = raw.get("version")
    if version != SNAPSHOT_FORMAT_VERSION:
        raise SnapshotError(f"不支持的快照格式版本: {version!r}")
    entries_raw = raw.get("entries")
    if not isinstance(entries_raw, list):
        raise SnapshotError("快照清单缺少 entries 列表")
    entries: list[SnapshotEntry] = []
    for item in entries_raw:
        if not isinstance(item, dict):
            raise SnapshotError("快照清单项必须是对象")
        entries.append(SnapshotEntry.from_dict(item))
    return tuple(entries)


def content_hash_of(entries: Iterable[SnapshotEntry]) -> str:
    """对清单计算稳定的整体内容哈希(与顺序无关)."""
    digest = hashlib.sha256()
    for entry in sorted(entries, key=lambda item: item.relative_path):
        line = "\0".join(
            (
                entry.relative_path,
                str(entry.size),
                entry.file_kind,
                entry.sha256,
                entry.link_target or "",
            )
        )
        digest.update(line.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()


def sha256_of_file(path: Path) -> str:
    """计算文件内容的 SHA-256(分块读取, 避免一次性载入大文件)."""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        while chunk := handle.read(_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def remove_snapshot(root: Path) -> None:
    """删除快照目录; 目录不存在时静默返回."""
    shutil.rmtree(Path(root), ignore_errors=True)


def _manifest(
    sources: Sequence[SnapshotSource],
    entries: Sequence[SnapshotEntry],
    content_hash: str,
) -> dict[str, object]:
    """构造快照自身描述文件的内容."""
    return {
        "version": SNAPSHOT_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "content_hash": content_hash,
        "sources": [
            {"index": source.index, "path": source.path, "kind": source.kind}
            for source in sources
        ],
        "entries": [entry.as_dict() for entry in entries],
    }


def _report(progress: ProgressCallback | None, fraction: float, message: str) -> None:
    """安全地报告进度: 回调异常不应中断备份."""
    if progress is None:
        return
    try:
        progress(fraction, message)
    except ArchiveManagementError:
        raise
    except Exception:
        return


def _copy_into(
    temporary: Path,
    sources: Sequence[SnapshotSource],
    *,
    progress: ProgressCallback | None,
    cancelled: Callable[[], bool] | None,
) -> list[SnapshotEntry]:
    """把全部来源复制到临时目录并返回清单."""
    plans = _plan(sources)
    total = max(len(plans), 1)
    temporary.mkdir(parents=True, exist_ok=True)
    entries: list[SnapshotEntry] = []
    for position, plan in enumerate(plans, start=1):
        if cancelled is not None and cancelled():
            raise OperationCancelledError("备份已取消")
        # 复制阶段占前 90%, 剩余进度留给清单校验与原子提交.
        _report(
            progress,
            0.9 * (position - 1) / total,
            f"复制 {plan.entry.relative_path}",
        )
        entries.append(_materialize(plan, temporary))
    return entries


@dataclass(frozen=True)
class _CopyPlan:
    """一次待执行的复制动作(目标路径 + 预期清单项)."""

    entry: SnapshotEntry
    origin: Path | None  # None 表示目录项或符号链接, 无需复制内容


def _plan(sources: Sequence[SnapshotSource]) -> list[_CopyPlan]:
    """展开全部来源为复制计划(先建目录, 再复制文件)."""
    plans: list[_CopyPlan] = []
    for source in sorted(sources, key=lambda item: item.index):
        origin = Path(source.path)
        root = f"loc-{source.index}"
        if source.kind == "file":
            if not origin.is_file() or origin.is_symlink():
                raise SnapshotError(f"存档文件不可用: {origin}")
            plans.append(
                _CopyPlan(
                    entry=SnapshotEntry(
                        relative_path=f"{root}/{origin.name}", file_kind="file"
                    ),
                    origin=origin,
                )
            )
            continue
        if not origin.is_dir() or origin.is_symlink():
            raise SnapshotError(f"存档目录不可用: {origin}")
        # 记录目录根节点, 使空目录也能形成有效(空)快照.
        plans.append(
            _CopyPlan(
                entry=SnapshotEntry(
                    relative_path=root,
                    sha256=_DIRECTORY_SHA256,
                    file_kind="directory",
                ),
                origin=None,
            )
        )
        for path, relative in _iter_directory(origin):
            plans.append(_plan_entry(f"{root}/{relative}", path))
    if not plans:
        raise SnapshotError("存档位置为空, 没有可备份的内容")
    return plans


def _plan_entry(relative_path: str, path: Path) -> _CopyPlan:
    """按条目类型生成复制计划."""
    if path.is_symlink():
        return _CopyPlan(
            entry=SnapshotEntry(
                relative_path=relative_path,
                sha256=_SYMLINK_SHA256,
                file_kind="symlink",
                link_target=str(path.readlink()),
            ),
            origin=None,
        )
    if path.is_dir():
        return _CopyPlan(
            entry=SnapshotEntry(
                relative_path=relative_path,
                sha256=_DIRECTORY_SHA256,
                file_kind="directory",
            ),
            origin=None,
        )
    return _CopyPlan(
        entry=SnapshotEntry(relative_path=relative_path, file_kind="file"),
        origin=path,
    )


def _iter_directory(root: Path) -> list[tuple[Path, str]]:
    """按字典序展开目录, 不跟随符号链接以避免越界或死循环."""
    found: list[tuple[Path, str]] = []
    stack: list[tuple[Path, str]] = [(root, "")]
    while stack:
        current, prefix = stack.pop()
        for child in sorted(current.iterdir(), key=lambda item: item.name):
            relative = (
                f"{prefix}{child.name}" if not prefix else f"{prefix}/{child.name}"
            )
            found.append((child, relative))
            if child.is_dir() and not child.is_symlink():
                stack.append((child, relative))
    found.sort(key=lambda item: item[1])
    return found


def _materialize(plan: _CopyPlan, temporary: Path) -> SnapshotEntry:
    """把单个计划落到临时目录, 返回校验后的清单项."""
    target = temporary / plan.entry.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if plan.entry.file_kind == "directory":
        target.mkdir(exist_ok=True)
        return plan.entry
    if plan.entry.file_kind == "symlink":
        # 符号链接只记入清单(link_target 保留原始目标), 不复制其内容,
        # 避免快照越出用户确认过的存档路径.
        return plan.entry
    if plan.origin is None:
        raise SnapshotError(f"缺少复制来源: {plan.entry.relative_path}")
    digest = _copy_with_hash(plan.origin, target)
    if sha256_of_file(target) != digest:
        raise SnapshotError(f"哈希校验失败: {plan.entry.relative_path}")
    return replace(plan.entry, sha256=digest, size=target.stat().st_size)


def _copy_with_hash(origin: Path, target: Path) -> str:
    """边复制边计算 SHA-256, 返回源内容的哈希."""
    digest = hashlib.sha256()
    try:
        with origin.open("rb") as source, target.open("wb") as sink:
            while chunk := source.read(_CHUNK_SIZE):
                digest.update(chunk)
                sink.write(chunk)
    except OSError as exc:
        raise SnapshotError(f"复制失败 {origin}: {exc}") from exc
    return digest.hexdigest()
