"""文件快照服务: 复制、哈希校验与原子提交.

一次快照把若干存档位置(文件或目录)完整复制到应用自有的备份根目录之下,
并记录逐文件清单与整体内容哈希. 关键约定:

1. **绝不覆盖**: 目标目录已存在时直接失败, 不会把新快照写进旧备份.
2. **临时目录**: 所有写入先落在目标同级的 ``*.partial-*`` 目录中, 复制完成
   并通过哈希复核后, 才用 ``os.replace`` 原子改名为正式目录; 改名遇到
   "拒绝访问/共享冲突"这类瞬时占用(杀毒软件、系统索引器正在扫描刚写入的文件)
   时会短暂重试, 连续失败则按真实错误上报。
3. **失败即清理**: 任一环节异常都会删除临时目录, 不会留下可被误认为完整
   备份的半成品.
4. **符号链接不跟随**: 链接按 ``symlink`` 类型记入清单但不复制内容, 避免把
   快照范围越出用户确认过的存档路径.

模块只依赖标准库, 不导入 Tkinter, 可在无显示环境测试.
"""

from __future__ import annotations

import errno
import hashlib
import json
import logging
import shutil
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from archive_management.domain import FileKind, PathKind
from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
    SnapshotError,
)

logger = logging.getLogger(__name__)

SNAPSHOT_FORMAT_VERSION = 1
MANIFEST_FILENAME = "snapshot.json"
_CHUNK_SIZE = 1024 * 1024
_DIRECTORY_SHA256 = hashlib.sha256(b"directory").hexdigest()
_SYMLINK_SHA256 = hashlib.sha256(b"symlink").hexdigest()

# 提交阶段的重试参数: 杀毒软件/索引器会在文件刚刚写完后短暂持有句柄, 让改名报
# "拒绝访问"; 这类错误稍等即可成功。真正的权限问题会连着失败, 因此重试必须有
# 上限, 且失败时保留原始原因与重试次数供审计排查。
_COMMIT_ATTEMPTS = 5
_COMMIT_DELAY_SECONDS = 0.05
# Windows 错误码: 拒绝访问(5) / 共享冲突(32) / 锁定冲突(33).
_RETRYABLE_WINERRORS = frozenset({5, 32, 33})
_RETRYABLE_ERRNOS = frozenset({errno.EACCES, errno.EBUSY, errno.EPERM})

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
        return cls(
            relative_path=_manifest_path(raw),
            size=_manifest_size(raw),
            sha256=_manifest_text(raw, "sha256"),
            link_target=_manifest_optional_text(raw, "link_target"),
            file_kind=_manifest_file_kind(raw),
        )


# 清单项的 file_kind 取值: 未知取值一律拒绝(而不是静默当成 file)。
_FILE_KINDS: dict[object, FileKind] = {
    "file": "file",
    "symlink": "symlink",
    "directory": "directory",
}


def _manifest_path(raw: dict[str, object]) -> str:
    """取清单项的 relative_path: 必须是非空字符串."""
    value = raw.get("relative_path")
    if not isinstance(value, str) or not value:
        raise SnapshotError(f"快照清单项缺少相对路径: {raw!r}")
    return value


def _manifest_size(raw: dict[str, object]) -> int:
    """取清单项的 size(缺省为 0); 布尔值不算整数."""
    value = raw.get("size", 0)
    if not isinstance(value, int) or isinstance(value, bool):
        raise SnapshotError(f"快照清单项 size 非法: {raw!r}")
    return value


def _manifest_text(raw: dict[str, object], key: str) -> str:
    """取一个必填的字符串字段(允许空串, 但类型必须对)."""
    value = raw.get(key)
    if not isinstance(value, str):
        raise SnapshotError(f"快照清单项 {key} 非法: {raw!r}")
    return value


def _manifest_optional_text(raw: dict[str, object], key: str) -> str | None:
    """取一个可选的字符串字段(缺省为 None)."""
    value = raw.get(key)
    if value is not None and not isinstance(value, str):
        raise SnapshotError(f"快照清单项 {key} 非法: {raw!r}")
    return value


def _manifest_file_kind(raw: dict[str, object]) -> FileKind:
    """取清单项的 file_kind, 未知取值直接拒绝."""
    kind = _FILE_KINDS.get(raw.get("file_kind"))
    if kind is None:
        raise SnapshotError(f"未知的清单文件类型: {raw.get('file_kind')!r}")
    return kind


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
        _commit_snapshot(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return SnapshotResult(
        root=destination,
        entries=tuple(entries),
        content_hash=content_hash,
        total_size=sum(entry.size for entry in entries if entry.file_kind == "file"),
    )


def _commit_snapshot(temporary: Path, destination: Path) -> None:
    """把临时目录改名为正式目录; 对瞬时文件系统错误做有界重试.

    Windows 上杀毒软件与系统索引器会短暂持有刚写入文件的句柄, 使改名报
    "拒绝访问"(5)或"共享冲突"(32)/"锁定冲突"(33); 这类占用稍后即释放, 重试
    通常立刻成功。重试次数与总等待都有上限, 连续失败说明是真实的权限问题,
    此时仍抛 :class:`SnapshotError`, 但保留原始原因并注明重试次数, 便于
    审计日志(`backup.create.failed`)直接看出"是重试过的瞬时故障还是硬性拒绝"。
    """
    delay = _COMMIT_DELAY_SECONDS
    attempts = 0
    while True:
        attempts += 1
        try:
            temporary.replace(destination)
            return
        except OSError as exc:
            if attempts >= _COMMIT_ATTEMPTS or not _is_transient_os_error(exc):
                raise SnapshotError(_commit_failure_message(exc, attempts)) from exc
            logger.debug(
                "快照提交遇到瞬时占用, %.0f ms 后重试(第 %d/%d 次): %s",
                delay * 1000,
                attempts,
                _COMMIT_ATTEMPTS - 1,
                exc,
            )
            time.sleep(delay)
            delay *= 2


def _is_transient_os_error(error: OSError) -> bool:
    """判断错误是否属于"稍后重试可能成功"的瞬时文件系统占用."""
    winerror = getattr(error, "winerror", None)
    if isinstance(winerror, int) and winerror in _RETRYABLE_WINERRORS:
        return True
    return error.errno in _RETRYABLE_ERRNOS


def _commit_failure_message(error: OSError, attempts: int) -> str:
    """渲染提交失败信息(重试过则注明次数)."""
    if attempts <= 1:
        return f"提交快照失败: {error}"
    return f"提交快照失败(已重试 {attempts - 1} 次): {error}"


def _inspect_entry(
    root: Path, entry: SnapshotEntry, *, deep: bool
) -> tuple[Literal["missing", "mismatched"] | None, bool]:
    """检查单条清单项: 返回 (问题, 是否计入"已检查"数量).

    目录与符号链接只要类型对就算检查过 —— 符号链接只记录目标字符串, 不比对内容;
    文件必须真实存在才算检查过, 于是"已检查 N 项"始终是真实看了一眼的数量。
    """
    target = root / entry.relative_path
    if entry.file_kind == "directory":
        return (None if target.is_dir() else "missing"), True
    if entry.file_kind == "symlink":
        return None, True
    if not target.exists():
        return "missing", False
    if deep and sha256_of_file(target) != entry.sha256:
        return "mismatched", True
    return None, True


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
        problem, counted = _inspect_entry(root, entry, deep=deep)
        checked += int(counted)
        if problem == "missing":
            missing.append(entry.relative_path)
        elif problem == "mismatched":
            mismatched.append(entry.relative_path)
    if content_hash_of(entries) != str(raw.get("content_hash", "")):
        mismatched.append(MANIFEST_FILENAME)
    return SnapshotVerification(
        ok=not missing and not mismatched,
        checked=checked,
        missing=tuple(missing),
        mismatched=tuple(mismatched),
    )


@dataclass(frozen=True)
class SnapshotManifest:
    """一份快照清单的解析结果: 清单项、来源位置与整体内容哈希."""

    entries: tuple[SnapshotEntry, ...]
    sources: tuple[SnapshotSource, ...]
    content_hash: str


def read_manifest(root: Path) -> SnapshotManifest:
    """读取快照清单, 供恢复用例定位写回目标.

    与 :func:`verify_snapshot` 的区别是这里还会解析 ``sources``: 恢复必须知道
    ``loc-<index>`` 当初对应哪个存档路径, 才能把内容写回原位置。
    """
    manifest_path = Path(root) / MANIFEST_FILENAME
    if not manifest_path.is_file():
        raise SnapshotError(f"缺少清单文件: {MANIFEST_FILENAME}")
    try:
        raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"无法读取快照清单: {exc}") from exc
    if not isinstance(raw, dict):
        raise SnapshotError("快照清单顶层必须是对象")
    return SnapshotManifest(
        entries=read_manifest_entries(raw),
        sources=_parse_sources(raw.get("sources")),
        content_hash=str(raw.get("content_hash", "")),
    )


def _source_index(item: dict[str, object]) -> int:
    """来源序号: 必须是非负整数(布尔值不算)."""
    index = item.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        raise SnapshotError(f"快照来源序号非法: {item!r}")
    return index


def _source_path(item: dict[str, object]) -> str:
    """来源路径: 必须是非空字符串."""
    path = item.get("path")
    if not isinstance(path, str) or not path:
        raise SnapshotError(f"快照来源路径非法: {item!r}")
    return path


def _source_kind(item: dict[str, object]) -> PathKind:
    """来源类型: 只能是 file 或 directory."""
    raw_kind = item.get("kind")
    if raw_kind not in ("file", "directory"):
        raise SnapshotError(f"快照来源类型非法: {item!r}")
    return "file" if raw_kind == "file" else "directory"


def _source_from_item(item: object, seen: set[int]) -> SnapshotSource:
    """把清单里的一个来源项解析成 :class:`SnapshotSource`(字段非法即抛错)."""
    if not isinstance(item, dict):
        raise SnapshotError("快照来源项必须是对象")
    index = _source_index(item)
    path = _source_path(item)
    kind = _source_kind(item)
    if index in seen:
        raise SnapshotError(f"快照来源序号重复: {index}")
    seen.add(index)
    return SnapshotSource(path=path, kind=kind, index=index)


def _parse_sources(raw: object) -> tuple[SnapshotSource, ...]:
    """解析清单里的来源列表, 字段非法时抛出异常."""
    if not isinstance(raw, list) or not raw:
        raise SnapshotError("快照清单缺少 sources 列表")
    seen: set[int] = set()
    return tuple(_source_from_item(item, seen) for item in raw)


def source_root(index: int) -> str:
    """返回某个来源在快照内的相对根目录名(与生成时保持一致)."""
    return f"loc-{index}"


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
        # 复制占进度前 90%, 剩余留给清单校验与原子提交.
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
        root = source_root(source.index)
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
    if not plans:  # pragma: no branch - create_snapshot 已拒绝空来源列表
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
