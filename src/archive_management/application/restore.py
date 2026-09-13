"""恢复用例: 把备份快照写回原始存档位置(阶段 E).

一次恢复只有三步, 每一步都能被用户理解与追溯:

1. **预检**(:meth:`RestoreService.plan`): 深度校验快照清单(缺失项、哈希不
   一致直接拒绝), 检查写回目标的路径边界与写权限, 统计要写回的文件数, 并
   探测游戏进程是否在运行;
2. **安全点**: 恢复前先创建一份"恢复前安全点"备份(``is_safety=True``,
   只在时间线视图展示), 使恢复操作可逆——这正是替代隔离区的做法: 需要回到
   恢复前的状态时, 从时间线恢复安全点即可;
3. **暂存与原子替换**: 新内容先写到目标同级的 ``.restore-*`` 暂存位置(与目标
   同盘, 避免跨盘改名失败), 逐文件核对哈希后用改名原子替换; 替换失败会把
   原内容改名回去, 不会留下半成品。快照之外的文件在覆盖模式下原样保留。

清单里的相对路径按不可信输入处理(拒绝绝对路径、盘符与 ``..`` 越界条目);
符号链接只重建链接本身, 环境不允许时记为跳过项而不是中断恢复。每次恢复都
通过 :mod:`archive_management.services.audit` 写入日志文件(操作日志不落库)。
"""

from __future__ import annotations

import hashlib
import re
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from archive_management.application.backup import BackupService
from archive_management.domain import BackupNode, PathKind, SaveLocation
from archive_management.exceptions import (
    ArchiveManagementError,
    ContentUnchangedError,
    OperationCancelledError,
    SnapshotError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.audit import log_action, log_failure, redacted_path
from archive_management.services.pathcheck import (
    dangerous_target_reason,
    is_within,
    is_writable_target,
    summarize_path,
)
from archive_management.services.processes import (
    ProcessNameProvider,
    ProcessProbe,
    probe_processes,
)
from archive_management.services.snapshot import (
    ProgressCallback,
    SnapshotEntry,
    SnapshotManifest,
    SnapshotSource,
    read_manifest,
    source_root,
)

_CHUNK_SIZE = 1024 * 1024
# 清单相对路径中出现这些前缀即视为绝对路径/盘符, 直接拒绝.
_ABSOLUTE_HINT = re.compile(r"^[A-Za-z]:")
# 预检结果的提示代码: UI 用 i18n 映射为文案, 也便于测试断言.
PROBLEM_UNWRITABLE = "unwritable"
PROBLEM_KIND_MISMATCH = "kind_mismatch"
WARN_RECORDED_PATH = "recorded_path"
WARN_MORE_LOCATIONS = "more_locations"


@dataclass(frozen=True)
class RestoreTarget:
    """一个待写回的原始存档位置及其预检结论."""

    index: int
    path: str
    kind: PathKind
    exists: bool
    writable: bool
    problem: str | None = None

    @property
    def blocked(self) -> bool:
        """该目标是否被预检拦下(不允许写回)."""
        return self.problem is not None


@dataclass(frozen=True)
class RestorePlan:
    """恢复前的预检结果(只读, 不修改任何数据)."""

    backup_id: int
    title: str
    snapshot_ok: bool
    snapshot_reason: str | None
    file_count: int
    total_size: int
    targets: tuple[RestoreTarget, ...]
    process: ProcessProbe
    safety_point_available: bool
    warnings: tuple[str, ...] = ()

    @property
    def blocked_targets(self) -> tuple[RestoreTarget, ...]:
        """返回被预检拦下的目标."""
        return tuple(target for target in self.targets if target.blocked)

    @property
    def blocked_reason(self) -> str | None:
        """返回第一个拦下恢复的原因代码; 全部可用时为 None."""
        for target in self.targets:
            if target.problem is not None:
                return target.problem
        return None


@dataclass(frozen=True)
class RestoreResult:
    """一次成功恢复的结果摘要."""

    backup_id: int
    targets: tuple[str, ...]
    restored_files: int
    restored_directories: int
    skipped_symlinks: tuple[str, ...] = ()
    safety_point_id: int | None = None
    total_size: int = 0


@dataclass(frozen=True)
class _Outcome:
    """单个来源的写回结果."""

    files: int = 0
    directories: int = 0
    size: int = 0
    skipped: tuple[str, ...] = ()


class RestoreService:
    """把选定备份写回原始存档位置, 并保证操作可逆、可追溯."""

    def __init__(
        self,
        database: Database,
        *,
        backup_root: Path,
        backups: BackupService | None = None,
        process_provider: ProcessNameProvider | None = None,
    ) -> None:
        """绑定数据库、备份根目录与可替换的进程探测提供者."""
        self._root = Path(backup_root)
        self._games = GameRepository(database)
        self._locations = SaveLocationRepository(database)
        self._nodes = BackupRepository(database)
        self._backups = (
            backups
            if backups is not None
            else BackupService(database, backup_root=backup_root)
        )
        self._process_provider = process_provider

    # -- 预检 ---------------------------------------------------------------

    def plan(self, game_id: int, backup_id: int) -> RestorePlan:
        """检查快照完整性与写回目标, 返回可展示的预检结果."""
        node = self._require_node(game_id, backup_id)
        entries: tuple[SnapshotEntry, ...] = ()
        manifest: SnapshotManifest | None = None
        reason: str | None = None
        try:
            manifest = read_manifest(self._backups.snapshot_root(node))
        except (SnapshotError, OSError) as exc:
            reason = str(exc)
        snapshot_ok = False
        if manifest is not None:
            verification = self._backups.verify(node)
            entries = manifest.entries
            snapshot_ok = verification.ok
            if not verification.ok:
                reason = (
                    f"快照清单校验未通过: 缺失 {len(verification.missing)} 项, "
                    f"哈希不一致 {len(verification.mismatched)} 项"
                )
        targets: list[RestoreTarget] = []
        warnings: list[str] = []
        if manifest is not None:
            targets, warnings = self._inspect_targets(game_id, manifest)
        locations = self._locations.list_for_game(game_id)
        plan = RestorePlan(
            backup_id=backup_id,
            title=self._title(node),
            snapshot_ok=snapshot_ok,
            snapshot_reason=reason,
            file_count=sum(1 for entry in entries if entry.file_kind == "file"),
            total_size=sum(
                entry.size for entry in entries if entry.file_kind == "file"
            ),
            targets=tuple(targets),
            process=probe_processes(
                self._process_hints(game_id, locations),
                provider=self._process_provider,
            ),
            safety_point_available=self._has_save_content(locations),
            warnings=tuple(warnings),
        )
        log_action(
            "restore.plan",
            basic=True,
            game_id=game_id,
            backup_id=backup_id,
            snapshot_ok=snapshot_ok,
            files=plan.file_count,
            bytes=plan.total_size,
            blocked=plan.blocked_reason,
            process_running=plan.process.running,
        )
        return plan

    # -- 执行 ---------------------------------------------------------------

    def restore(
        self,
        game_id: int,
        backup_id: int,
        *,
        safety_point: bool = True,
        force: bool = False,
        safety_title: str = "",
        safety_note: str = "",
        progress: ProgressCallback | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> RestoreResult:
        """把备份内容写回原始存档位置.

        ``force=False`` 时检测到游戏进程在运行会直接拒绝; ``safety_point=True``
        时先创建一份安全点备份, 使恢复前的状态可以重新取回。
        """
        node = self._require_node(game_id, backup_id)
        plan = self.plan(game_id, backup_id)
        self._ensure_restorable(plan, force=force, game_id=game_id, backup_id=backup_id)
        snapshot_root = self._backups.snapshot_root(node)
        manifest = read_manifest(snapshot_root)
        log_action(
            "restore.start",
            game_id=game_id,
            backup_id=backup_id,
            title=plan.title,
            files=plan.file_count,
            targets=len(plan.targets),
            safety_point=safety_point,
            forced=force,
            paths=", ".join(redacted_path(target.path) for target in plan.targets),
        )
        restored_files = 0
        restored_dirs = 0
        restored_size = 0
        skipped: list[str] = []
        safety_id: int | None = None
        try:
            if safety_point and plan.safety_point_available:
                safety_id = self._try_safety_point(
                    game_id,
                    title=safety_title,
                    note=safety_note,
                    progress=progress,
                    cancelled=cancelled,
                )
            total = max(len(manifest.sources), 1)
            for position, source in enumerate(manifest.sources, start=1):
                if cancelled is not None and cancelled():
                    raise OperationCancelledError("恢复已取消")
                target = _target_for(plan, source.index)
                destination = target.path if target is not None else source.path
                kind = target.kind if target is not None else source.kind
                _report(
                    progress,
                    0.3 + 0.7 * (position - 1) / total,
                    f"恢复 {destination}",
                )
                outcome = self._restore_source(
                    source=source,
                    entries=_entries_for(manifest.entries, source.index),
                    target_path=destination,
                    kind=kind,
                    snapshot_root=snapshot_root,
                    progress=progress,
                    cancelled=cancelled,
                )
                restored_files += outcome.files
                restored_dirs += outcome.directories
                restored_size += outcome.size
                skipped.extend(outcome.skipped)
            _report(progress, 1.0, "完成")
        except OperationCancelledError as exc:
            log_action(
                "restore.cancel",
                game_id=game_id,
                backup_id=backup_id,
                reason=str(exc),
            )
            raise
        except Exception as exc:
            log_failure("restore", game_id=game_id, backup_id=backup_id, error=str(exc))
            raise
        log_action(
            "restore.succeeded",
            game_id=game_id,
            backup_id=backup_id,
            files=restored_files,
            directories=restored_dirs,
            bytes=restored_size,
            safety_point_id=safety_id,
            skipped_symlinks=len(skipped) or None,
        )
        return RestoreResult(
            backup_id=backup_id,
            targets=tuple(source.path for source in manifest.sources),
            restored_files=restored_files,
            restored_directories=restored_dirs,
            skipped_symlinks=tuple(skipped),
            safety_point_id=safety_id,
            total_size=restored_size,
        )

    # -- 内部 ---------------------------------------------------------------

    def _ensure_restorable(
        self, plan: RestorePlan, *, force: bool, game_id: int, backup_id: int
    ) -> None:
        """在真正动文件之前拦下所有已知风险."""
        if not plan.snapshot_ok:
            log_action(
                "restore.rejected",
                game_id=game_id,
                backup_id=backup_id,
                reason="snapshot",
            )
            raise SnapshotError(plan.snapshot_reason or "备份快照不完整, 已取消恢复")
        blocked = plan.blocked_targets
        if blocked:
            log_action(
                "restore.rejected",
                game_id=game_id,
                backup_id=backup_id,
                reason=blocked[0].problem,
                path=redacted_path(blocked[0].path),
            )
            raise ArchiveManagementError(f"无法写入该存档位置: {blocked[0].path}")
        if plan.process.running and not force:
            log_action(
                "restore.rejected",
                game_id=game_id,
                backup_id=backup_id,
                reason="process_running",
                matches=", ".join(plan.process.matches[:3]),
            )
            raise ArchiveManagementError("检测到游戏进程正在运行, 请退出游戏后再恢复")

    def _inspect_targets(
        self, game_id: int, manifest: SnapshotManifest
    ) -> tuple[list[RestoreTarget], list[str]]:
        """按快照来源逐个检查写回目标, 返回目标列表与提示代码."""
        locations = self._locations.list_for_game(game_id)
        by_path = {location.path: location for location in locations}
        by_index = dict(enumerate(locations))
        targets: list[RestoreTarget] = []
        warnings: list[str] = []
        for source in manifest.sources:
            # 优先按路径匹配当前配置(用户可能重新排序过位置),
            # 其次按备份时的顺序, 最后回退到清单里记录的原始路径.
            location = by_path.get(source.path) or by_index.get(source.index)
            path = source.path if location is None else location.path
            kind = source.kind if location is None else location.path_kind
            if location is None:
                warnings.append(WARN_RECORDED_PATH)
            writable = is_writable_target(path)
            targets.append(
                RestoreTarget(
                    index=source.index,
                    path=path,
                    kind=kind,
                    exists=Path(path).exists(),
                    writable=writable,
                    problem=_target_problem(path, kind, writable, self._root),
                )
            )
        if len(locations) > len(manifest.sources):
            warnings.append(WARN_MORE_LOCATIONS)
        return targets, warnings

    def _restore_source(
        self,
        *,
        source: SnapshotSource,
        entries: tuple[SnapshotEntry, ...],
        target_path: str,
        kind: PathKind,
        snapshot_root: Path,
        progress: ProgressCallback | None,
        cancelled: Callable[[], bool] | None,
    ) -> _Outcome:
        """写回单个来源: 暂存 -> 原子替换 -> 清理原内容."""
        target = Path(target_path)
        staging = _staging_path(target)
        try:
            if kind == "file":
                outcome = _stage_file(
                    entries=entries,
                    snapshot_root=snapshot_root,
                    staging=staging,
                    cancelled=cancelled,
                )
            else:
                staging.mkdir(parents=True, exist_ok=False)
                if target.is_dir():
                    # 先并入现有内容: 快照之外的文件在恢复后依然保留.
                    _copy_tree(target, staging)
                outcome = _stage_directory(
                    entries=entries,
                    source_index=source.index,
                    snapshot_root=snapshot_root,
                    staging=staging,
                    progress=progress,
                    cancelled=cancelled,
                )
            _swap_in(staging, target)
        except BaseException:
            _discard(staging)
            raise
        return outcome

    def _try_safety_point(
        self,
        game_id: int,
        *,
        title: str,
        note: str,
        progress: ProgressCallback | None,
        cancelled: Callable[[], bool] | None,
    ) -> int | None:
        """创建恢复前安全点; 存档与当前节点完全一致时无需安全点, 直接跳过."""
        try:
            created = self._create_safety_point(
                game_id,
                title=title,
                note=note,
                progress=progress,
                cancelled=cancelled,
            )
        except ContentUnchangedError:
            log_action(
                "restore.safety_skipped",
                basic=True,
                game_id=game_id,
                reason="unchanged",
            )
            return None
        return created.id

    def _create_safety_point(
        self,
        game_id: int,
        *,
        title: str,
        note: str,
        progress: ProgressCallback | None,
        cancelled: Callable[[], bool] | None,
    ) -> BackupNode:
        """恢复前创建一份安全点备份(把它的进度压到整体进度的前 30%)."""
        return self._backups.create_backup(
            game_id,
            kind="manual",
            title=title,
            note=note,
            safety=True,
            progress=_scaled(progress, 0.0, 0.3),
            cancelled=cancelled,
        )

    def _has_save_content(self, locations: Sequence[SaveLocation]) -> bool:
        """判断当前存档位置是否真的有内容(决定是否值得建安全点)."""
        for location in locations:
            summary = summarize_path(location.path)
            if summary.entries or summary.directories:
                return True
        return False

    def _process_hints(
        self, game_id: int, locations: Sequence[SaveLocation]
    ) -> list[str]:
        r"""构造进程名候选: 游戏名 + 各存档位置所在文件夹名.

        存档目录的名字往往就是游戏名(如 ``D:\Games\OuterWilds\save``),
        比中文游戏名更容易匹配到进程名。
        """
        hints = [self._game_name(game_id)]
        for location in locations:
            parent = Path(location.path).parent.name
            if parent and parent not in hints:
                hints.append(parent)
        return [hint for hint in hints if hint]

    def _require_node(self, game_id: int, backup_id: int) -> BackupNode:
        """要求备份节点存在且属于该游戏."""
        node = self._nodes.get(backup_id)
        if node is None or node.game_id != game_id:
            raise ArchiveManagementError(f"未知的备份节点: {backup_id}")
        return node

    def _game_name(self, game_id: int) -> str:
        """返回游戏名称(缺失时返回空串, 不影响恢复本身)."""
        game = self._games.get(game_id)
        return "" if game is None else game.name

    def _title(self, node: BackupNode) -> str:
        """返回备份的展示名称."""
        name = (node.title or "").strip() or (node.branch_name or "").strip()
        return name or f"#{node.id}"


def _target_for(plan: RestorePlan, index: int) -> RestoreTarget | None:
    """按来源序号取回预检过的目标."""
    for target in plan.targets:
        if target.index == index:
            return target
    return None


def _entries_for(
    entries: Iterable[SnapshotEntry], index: int
) -> tuple[SnapshotEntry, ...]:
    """取出某个来源的清单项(只保留位于该来源目录之下的条目)."""
    prefix = source_root(index)
    return tuple(
        entry for entry in entries if entry.relative_path.startswith(f"{prefix}/")
    )


def _relative_of(relative_path: str, index: int) -> str:
    """去掉来源前缀, 返回相对于来源根目录的路径."""
    prefix = f"{source_root(index)}/"
    if relative_path.startswith(prefix):
        return relative_path[len(prefix) :]
    return relative_path


def _target_problem(
    path: str, kind: PathKind, writable: bool, backup_root: Path
) -> str | None:
    """判断写回目标是否被拒绝, 返回原因代码或 None."""
    reason = dangerous_target_reason(path, protected=(str(backup_root),))
    if reason is not None:
        return reason
    if not writable:
        return PROBLEM_UNWRITABLE
    target = Path(path)
    if target.exists():
        if kind == "file" and not target.is_file():
            return PROBLEM_KIND_MISMATCH
        if kind == "directory" and not target.is_dir():
            return PROBLEM_KIND_MISMATCH
    return None


def _staging_path(target: Path) -> Path:
    """返回与目标同盘的暂存路径(保证改名替换是同一卷内的操作)."""
    if target.parent == target:
        raise SnapshotError(f"拒绝在磁盘根目录下创建暂存目录: {target}")
    return target.parent / f".{target.name}.restore-{uuid4().hex[:8]}"


def _stage_file(
    *,
    entries: tuple[SnapshotEntry, ...],
    snapshot_root: Path,
    staging: Path,
    cancelled: Callable[[], bool] | None,
) -> _Outcome:
    """把"文件来源"的单个文件写入暂存路径."""
    files = [entry for entry in entries if entry.file_kind == "file"]
    if len(files) != 1:
        raise SnapshotError("快照中的文件来源清单异常, 已取消恢复")
    if cancelled is not None and cancelled():
        raise OperationCancelledError("恢复已取消")
    entry = files[0]
    origin = _safe_path(snapshot_root, entry.relative_path)
    size = _write_file(origin, staging, entry.sha256)
    return _Outcome(files=1, size=size)


def _stage_directory(
    *,
    entries: tuple[SnapshotEntry, ...],
    source_index: int,
    snapshot_root: Path,
    staging: Path,
    progress: ProgressCallback | None,
    cancelled: Callable[[], bool] | None,
) -> _Outcome:
    """把目录来源的清单内容写入暂存目录(先建目录, 再写链接与文件)."""
    ordered = sorted(
        entries, key=lambda entry: (entry.file_kind == "file", entry.relative_path)
    )
    total = max(len(ordered), 1)
    files = 0
    directories = 0
    size = 0
    skipped: list[str] = []
    for position, entry in enumerate(ordered, start=1):
        if cancelled is not None and cancelled():
            raise OperationCancelledError("恢复已取消")
        relative = _relative_of(entry.relative_path, source_index)
        destination = _safe_path(staging, relative)
        if entry.file_kind == "directory":
            _make_directory(destination)
            directories += 1
            continue
        if entry.file_kind == "symlink":
            _discard(destination)
            if not _recreate_symlink(destination, entry.link_target or ""):
                skipped.append(relative)
            continue
        if destination.is_dir():
            # 当前类型与快照不一致(目录变文件): 先清掉旧目录再写文件.
            _discard(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        origin = _safe_path(snapshot_root, entry.relative_path)
        size += _write_file(origin, destination, entry.sha256)
        files += 1
        _report(progress, 0.3 + 0.7 * position / total, f"恢复 {relative}")
    return _Outcome(
        files=files, directories=directories, size=size, skipped=tuple(skipped)
    )


def _make_directory(path: Path) -> None:
    """确保目录存在; 同名文件会被替换为目录(应对类型变化)."""
    if path.exists() and not path.is_dir():
        _discard(path)
    path.mkdir(parents=True, exist_ok=True)


def _safe_path(root: Path, relative: str) -> Path:
    """把清单里的相对路径安全地拼到根目录下, 拒绝越界条目.

    清单来自磁盘上的备份文件, 必须当作不可信输入: 绝对路径(含 Windows 盘符)
    和 ``..`` 条目一律拒绝, 避免恢复时写到用户确认过的存档位置之外。
    """
    cleaned = relative.replace("\\", "/").strip()
    if not cleaned or cleaned.startswith("/") or _ABSOLUTE_HINT.match(cleaned):
        raise SnapshotError(f"快照清单中的相对路径非法: {relative!r}")
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        raise SnapshotError(f"快照清单中的相对路径非法: {relative!r}")
    candidate = root.joinpath(*parts)
    if not is_within(candidate, root):
        raise SnapshotError(f"快照清单中的相对路径越界: {relative!r}")
    return candidate


def _write_file(origin: Path, destination: Path, expected_sha256: str) -> int:
    """复制单个文件并复核内容哈希, 返回写入的字节数."""
    digest = hashlib.sha256()
    try:
        with origin.open("rb") as source, destination.open("wb") as sink:
            while chunk := source.read(_CHUNK_SIZE):
                digest.update(chunk)
                sink.write(chunk)
    except OSError as exc:
        raise SnapshotError(f"恢复文件失败 {origin}: {exc}") from exc
    if digest.hexdigest() != expected_sha256:
        raise SnapshotError(f"恢复内容哈希不一致: {origin.name}")
    return destination.stat().st_size


def _copy_tree(source: Path, destination: Path) -> None:
    """把已有内容复制进暂存目录(不跟随符号链接)."""
    for child in sorted(source.iterdir(), key=lambda item: item.name):
        target = destination / child.name
        if child.is_symlink():
            _discard(target)
            _recreate_symlink(target, str(child.readlink()))
            continue
        if child.is_dir():
            _make_directory(target)
            _copy_tree(child, target)
            continue
        try:
            shutil.copy2(child, target)
        except OSError as exc:
            raise SnapshotError(f"复制现有存档失败 {child}: {exc}") from exc


def _swap_in(staging: Path, target: Path) -> None:
    """用暂存内容替换目标, 失败时回滚.

    原内容先改名让位到目标同级的隐藏位置, 替换成功后删除: 同盘内两次
    ``replace`` 都不涉及数据复制, 失败时把原内容改名回去, 不会留下半成品。
    """
    if not (target.exists() or target.is_symlink()):
        _move(staging, target)
        return
    replaced = target.parent / f".{target.name}.replaced-{uuid4().hex[:8]}"
    _move(target, replaced)
    try:
        _move(staging, target)
    except OSError:
        _move(replaced, target)
        raise
    _discard(replaced)


def _move(source: Path, destination: Path) -> None:
    """同卷内优先原子改名, 跨卷时回退到复制后删除."""
    try:
        source.replace(destination)
        return
    except OSError:
        pass
    try:
        shutil.move(str(source), str(destination))
    except OSError as exc:
        raise SnapshotError(f"移动失败 {source} -> {destination}: {exc}") from exc


def _discard(path: Path) -> None:
    """删除暂存路径(文件或目录), 不存在时静默返回."""
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)


def _recreate_symlink(path: Path, link_target: str) -> bool:
    """尽力重建符号链接; Windows 无权限时返回 False 而不是中断恢复."""
    if not link_target:
        return False
    try:
        path.symlink_to(link_target)
    except (OSError, NotImplementedError, ValueError):
        return False
    return True


def _scaled(
    progress: ProgressCallback | None, start: float, end: float
) -> ProgressCallback | None:
    """把回调的 0..1 映射到整体进度的某个区间."""
    if progress is None:
        return None

    def report(fraction: float, message: str) -> None:
        bounded = max(0.0, min(1.0, fraction))
        progress(start + (end - start) * bounded, message)

    return report


def _report(progress: ProgressCallback | None, fraction: float, message: str) -> None:
    """安全地报告进度: 回调异常不应中断恢复."""
    if progress is None:
        return
    try:
        progress(fraction, message)
    except ArchiveManagementError:
        raise
    except Exception:
        return
