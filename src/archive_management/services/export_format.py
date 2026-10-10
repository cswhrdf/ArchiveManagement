"""导出包格式: 打包、校验与安全解包.

导出包是一个 zip, 只放"用户确认过的逻辑信息 + 备份内容", 不含凭据、缓存图与
本机草稿数据。包内结构固定::

    manifest.json            # 格式版本、导出时间、工具版本、游戏标识、逐条校验
    config.json              # 游戏配置、存档位置、分支关系、时间线元数据、定时任务
    branches/<节点>/...      # 分支树上的每个节点(内含该节点自己的快照清单)
    timeline/<节点>/...      # 时间线上的安全点

本模块只做"格式"这一层: 写包时逐条算 sha256 并回读校验, 改名前先落在同级临时文件,
因此**半成品不会出现在用户选择的路径上**; 读包时先按 zip 目录里的声明做上限检查
(条目数、单文件、总展开量、压缩比), 再对每个条目做路径校验, 拒绝绝对路径、``..``
片段、盘符与反斜杠逃逸, 最后用 :func:`is_within` 兜底。符号链接条目一律拒绝:
包是从别处来的输入, 不跟随链接才能保证"只在目标目录里写普通文件"。

同一条纪律也适用于**批量包**(把若干款游戏一次导出去): 外层是一个 zip, 里面只放
各款游戏**已经写好的单包**加上外层 ``manifest.json``, 因此批量包的体积约等于各内层
包之和, 不会把游戏内容重复一遍。两类包用清单里的 ``kind`` 区分: 单包是
``"package"``, 批量包是 ``"batch"``; 外层清单的 ``games`` 数组逐游戏记录条目名、
游戏标识、节点/文件/字节统计与内层包的 sha256(读包时可按它复核)。读批量包时外层
只做"结构 + 上限 + 条目名"这一层检查, 每个内层包再交给 :func:`read_package` 完成
单包的全部校验 —— 单包校验只有一份, 不会在批量路径上走样。解出来的内层包落在临时
目录里, 它的生命周期由 :class:`BatchContents` 这个上下文管理器负责(退出时删除),
因此调用方必须在 ``with`` 块内用完这些内层包。

模块只依赖标准库, 不导入 Tkinter, 可在无显示环境测试.
"""

from __future__ import annotations

import hashlib
import json
import logging
import posixpath
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import TracebackType
from uuid import uuid4

from archive_management.exceptions import (
    OperationCancelledError,
    PackageError,
)
from archive_management.packaging import package_version
from archive_management.services.pathcheck import is_within

logger = logging.getLogger(__name__)

#: 包格式标识(与版本号分开: 别的工具写的 zip 一眼能看出不是导出包).
EXPORT_FORMAT = "archive-management.export"
#: 包格式版本; 读包时只认这一个值, 未知版本直接拒绝.
EXPORT_FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"
CONFIG_NAME = "config.json"
BRANCHES_DIR = "branches"
TIMELINE_DIR = "timeline"

#: 包类型: 单游戏包与批量包共用同一套读包纪律, 靠清单里的这个字段互相区分.
PACKAGE_KIND = "package"
BATCH_KIND = "batch"
#: 包类型的可读名称(报错时说清"需要什么、实际是什么").
_KIND_LABELS = {PACKAGE_KIND: "单游戏导出包", BATCH_KIND: "批量导出包"}
#: 单游戏导出包的文件名后缀; 批量包里的内层条目沿用同一形态.
ARCHIVE_SUFFIX = ".archive.zip"

# 解包上限: 包是外部输入, 必须防"解压炸弹"与异常结构。上限按"用户存档规模"取值:
# 单文件 4 GiB、总展开 24 GiB、2 万个条目已经远超正常游戏存档。
MAX_ENTRIES = 20_000
MAX_ENTRY_BYTES = 4 * 1024**3
MAX_TOTAL_BYTES = 24 * 1024**3
# 批量包的额外上限: 内层包合计受 MAX_TOTAL_BYTES 约束, 游戏数另设上限 ——
# 一次导入几百款游戏在界面上已经没有可用性, 也远超"用户自己的一批游戏".
MAX_BATCH_GAMES = 500
_BATCH_TEMP_PREFIX = "archive-batch-"
#: 单条目压缩比上限(压缩后 1 KiB 以上才判): 高度重复的数据能压得很小, 但几千倍
#: 的比率只出现在刻意构造的包里。
MAX_COMPRESSION_RATIO = 2000.0
_RATIO_FLOOR_BYTES = 1024

_CHUNK_SIZE = 1024 * 1024
_PARTIAL_SUFFIX = ".partial-"

# 进度回调: 与备份快照同一套约定(比例 + 人类可读说明).
ProgressCallback = Callable[[float, str], None]
# 取消探针: 返回 True 表示用户已请求取消.
CancelProbe = Callable[[], bool]

# 包内路径允许的形态: 相对路径、正斜杠分隔、每段都是普通名字.
_FORBIDDEN_SEGMENTS = frozenset({"", ".", ".."})
_ABSOLUTE_HINTS = re.compile(r"^(/|\\|[A-Za-z]:)")


@dataclass(frozen=True)
class PackageFile:
    """一个待写进包的文件: 包内路径 + 磁盘上的来源."""

    path: str
    source: Path

    def __post_init__(self) -> None:
        """包内路径必须是安全的相对路径(写入前就拦下, 免得写出坏包)."""
        validate_member_path(self.path)


@dataclass(frozen=True)
class PackageEntry:
    """包内一条文件的声明: 路径、大小与 sha256."""

    path: str
    size: int
    sha256: str

    def as_dict(self) -> dict[str, object]:
        """转换为写进 manifest 的 JSON 兼容字典."""
        return {"path": self.path, "size": self.size, "sha256": self.sha256}


@dataclass(frozen=True)
class PackageSummary:
    """写包结果(供界面展示"导出了多少")."""

    path: Path
    entries: tuple[PackageEntry, ...]

    @property
    def file_count(self) -> int:
        """包内文件条数."""
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        """包内文件展开后的总字节数."""
        return sum(entry.size for entry in self.entries)


@dataclass(frozen=True)
class PackageContents:
    """读包结果: 已解析的清单与配置, 以及逐条声明."""

    path: Path
    manifest: Mapping[str, object]
    config: Mapping[str, object]
    entries: tuple[PackageEntry, ...]

    @property
    def game(self) -> Mapping[str, object]:
        """Manifest 里的游戏标识(缺失时返回空字典)."""
        raw = self.manifest.get("game")
        return raw if isinstance(raw, Mapping) else {}

    def member_paths(self, prefix: str) -> tuple[str, ...]:
        """返回包内以 ``prefix`` 开头的文件路径(按包内顺序)."""
        wanted = prefix.rstrip("/") + "/"
        return tuple(
            entry.path for entry in self.entries if entry.path.startswith(wanted)
        )

    def config_list(self, key: str) -> list[Mapping[str, object]]:
        """取 config.json 里一个对象数组(缺失返回空, 类型不符则拒绝).

        配置里的数组(存档位置、备份节点)是导入的输入, 因此这里就要求它们是
        对象数组 —— 结构不符时直接报错, 而不是留给调用方逐项去猜.
        """
        raw = self.config.get(key)
        if raw is None:
            return []
        if not isinstance(raw, list) or not all(
            isinstance(item, Mapping) for item in raw
        ):
            raise PackageError(f"config.json 里的 {key} 不是对象数组")
        return [item for item in raw if isinstance(item, Mapping)]


def validate_member_path(name: str) -> str:
    """校验并返回包内路径; 不安全的形态直接抛 :class:`PackageError`.

    规则: 必须是相对路径、正斜杠分隔、每段都是普通名字(没有空段、``.``、``..``),
    不以盘符或分隔符开头, 且不含反斜杠(Windows 风格分隔符是常见的逃逸写法).
    """
    raw = str(name)
    if not raw or raw.endswith("/"):
        raise PackageError(_unavailable("包内路径为空或以分隔符结尾", raw))
    if "\\" in raw or _ABSOLUTE_HINTS.match(raw):
        raise PackageError(_unavailable("包内路径不是相对路径", raw))
    for segment in raw.split("/"):
        if segment in _FORBIDDEN_SEGMENTS:
            raise PackageError(_unavailable("包内路径含有不安全的片段", raw))
    return raw


def member_target(root: Path, name: str) -> Path:
    """把包内路径解析为 ``root`` 下的目标路径, 并做边界兜底.

    除了逐段校验, 再用 :func:`is_within` 复核一次解析结果 —— 双保险, 免得将来
    有人改坏了上面的规则就顺手放行了越界写入。
    """
    relative = validate_member_path(name)
    candidate = root / relative
    if not is_within(candidate, root):  # pragma: no branch - 上一步已排除越界形态与盘符
        raise PackageError(  # pragma: no cover - 兜底, validate_member_path 已先拒绝
            _unavailable("包内路径越出了目标目录", name)
        )
    return candidate


def _unavailable(problem: str, name: str) -> str:
    """统一的拒绝文案: 说明问题与那条路径."""
    return f"{problem}: {name}"


def entry_key(name: str) -> str:
    """返回条目的归一键(大小写与分隔符不敏感), 用于检测包内重复路径."""
    return posixpath.normpath(name.replace("\\", "/")).casefold()


# ---------------------------------------------------------------- 写包


def write_package(
    destination: Path,
    *,
    game: Mapping[str, object],
    config: Mapping[str, object],
    members: Sequence[PackageFile],
    tool_version: str | None = None,
    progress: ProgressCallback | None = None,
    cancelled: CancelProbe | None = None,
) -> PackageSummary:
    """把游戏配置与备份内容写成一个导出包, 返回写包结果.

    流程: 校验包内路径 -> 写入同级临时文件(逐条算 sha256) -> **回读校验**每个条目的
    大小与哈希 -> 原子改名到 ``destination``。任一步失败都会删掉临时文件, 因此目标
    路径上只会出现完整包或被用户原有文件覆盖后的完整包, 不会出现半成品。
    """
    entries = _plan_entries(members)
    _reject_duplicates(entries)
    temporary = _partial_path(destination)
    manifest = _build_manifest(game=game, entries=entries, tool_version=tool_version)
    try:
        _write_zip(
            temporary,
            config=config,
            members=members,
            manifest=manifest,
            progress=progress,
            cancelled=cancelled,
        )
        _verify_written(temporary, entries=entries, cancelled=cancelled)
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    logger.debug("导出包已写入: %s (%d 个文件)", destination, len(entries))
    return PackageSummary(path=destination, entries=entries)


def _plan_entries(members: Sequence[PackageFile]) -> tuple[PackageEntry, ...]:
    """按包内路径排序并统计来源文件的大小与哈希(顺序确定, 便于比对)."""
    planned: list[PackageEntry] = []
    for member in sorted(members, key=lambda item: item.path):
        if member.source.is_symlink():
            # 跟随链接会把链接指向的内容(可能在存档范围之外)一起打进包里.
            raise PackageError(f"导出内容不能是符号链接: {member.path}")
        try:
            stat_result = member.source.stat()
        except OSError as exc:
            raise PackageError(f"导出内容读不到: {member.source} ({exc})") from exc
        if not stat.S_ISREG(stat_result.st_mode):
            raise PackageError(f"导出内容不是普通文件: {member.source}")
        if stat_result.st_size > MAX_ENTRY_BYTES:
            raise PackageError(f"单个文件超过导出上限: {member.path}")
        planned.append(
            PackageEntry(
                path=member.path,
                size=int(stat_result.st_size),
                sha256=sha256_of_file(member.source),
            )
        )
    total = sum(entry.size for entry in planned)
    if total > MAX_TOTAL_BYTES:
        raise PackageError("导出内容总大小超过上限")
    if len(planned) > MAX_ENTRIES:
        raise PackageError("导出内容条数超过上限")
    return tuple(planned)


def _reject_duplicates(entries: Sequence[PackageEntry]) -> None:
    """拒绝同一个包内路径出现两次(否则解包会互相覆盖)."""
    seen: set[str] = set()
    for entry in entries:
        key = entry_key(entry.path)
        if key in seen:
            raise PackageError(f"包内路径重复: {entry.path}")
        seen.add(key)


def _partial_path(destination: Path) -> Path:
    """返回目标路径同级的临时文件名(必须同盘, 改名才是原子的)."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination.with_name(
        f"{destination.name}{_PARTIAL_SUFFIX}{uuid4().hex[:8]}"
    )


def _build_manifest(
    *,
    game: Mapping[str, object],
    entries: Sequence[PackageEntry],
    tool_version: str | None,
) -> dict[str, object]:
    """组装 manifest: 格式版本、时间、工具版本、游戏标识与逐条校验."""
    return {
        "format": EXPORT_FORMAT,
        "kind": PACKAGE_KIND,
        "version": EXPORT_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool_version": package_version() if tool_version is None else tool_version,
        "game": dict(game),
        "files": {
            "count": len(entries),
            "bytes": sum(entry.size for entry in entries),
        },
        "entries": [entry.as_dict() for entry in entries],
    }


def _write_zip(
    temporary: Path,
    *,
    config: Mapping[str, object],
    members: Sequence[PackageFile],
    manifest: Mapping[str, object],
    progress: ProgressCallback | None,
    cancelled: CancelProbe | None,
) -> None:
    """把 config.json、各成员与 manifest.json 依次写进 zip."""
    ordered = sorted(members, key=lambda item: item.path)
    total = max(1, len(ordered))
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(CONFIG_NAME, _json_text(config))
        for index, member in enumerate(ordered):
            _report(cancelled, progress, index / total, f"正在写入 {member.path}")
            with (
                archive.open(member.path, "w") as target,
                member.source.open("rb") as source,
            ):
                for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
                    target.write(chunk)
        _report(cancelled, progress, 1.0, "正在写入清单")
        archive.writestr(MANIFEST_NAME, _json_text(manifest))


def _report(
    cancelled: CancelProbe | None,
    progress: ProgressCallback | None,
    ratio: float,
    message: str,
) -> None:
    """检查取消探针并上报进度(取消时抛 :class:`OperationCancelledError`)."""
    if cancelled is not None and cancelled():
        raise OperationCancelledError("导出已取消")
    if progress is not None:
        progress(ratio, message)


def _verify_written(
    temporary: Path, *, entries: Sequence[PackageEntry], cancelled: CancelProbe | None
) -> None:
    """回读刚写好的包, 逐条比对大小与 sha256.

    只信任磁盘上真正写出来的字节: 磁盘写满、压缩器异常这类问题都在这里暴露,
    而不是等用户拿到一个打不开或缺内容的包。
    """
    expected = {entry.path: entry for entry in entries}
    with zipfile.ZipFile(temporary) as archive:
        found = _written_members(archive)
        if found != set(expected):
            raise PackageError("导出包的条目与清单不一致")
        for name in sorted(found):
            _verify_member(archive, expected[name], cancelled)


def _written_members(archive: zipfile.ZipFile) -> set[str]:
    """返回包内除清单与配置之外的文件名集合(少一个就拒绝)."""
    names = [name for name in archive.namelist() if not name.endswith("/")]
    for name in (CONFIG_NAME, MANIFEST_NAME):
        if name not in names:
            raise PackageError(f"导出包缺少 {name}")
    return {name for name in names if name not in (CONFIG_NAME, MANIFEST_NAME)}


def _verify_member(
    archive: zipfile.ZipFile, expected: PackageEntry, cancelled: CancelProbe | None
) -> None:
    """比对单个条目的大小与哈希(只信任磁盘上真正写出来的字节)."""
    if cancelled is not None and cancelled():
        raise OperationCancelledError("导出已取消")
    info = archive.getinfo(expected.path)
    if info.file_size != expected.size:
        raise PackageError(f"导出包条目大小不一致: {expected.path}")
    if _hash_member(archive, expected.path) != expected.sha256:
        raise PackageError(f"导出包条目哈希不一致: {expected.path}")


def _hash_member(archive: zipfile.ZipFile, name: str) -> str:
    """计算 zip 内某个成员内容的 sha256."""
    digest = hashlib.sha256()
    with archive.open(name) as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _json_text(payload: Mapping[str, object]) -> str:
    """把清单/配置序列化为可读的 JSON 文本(导出包是给人看的文件, 不压成一行)."""
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def sha256_of_file(path: Path) -> str:
    """分块计算文件的 sha256."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK_SIZE), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------- 读包


def read_package(path: Path, *, verify_hashes: bool = False) -> PackageContents:
    """读取导出包并做安全检查, 返回已解析的清单与配置.

    检查顺序: 文件存在 -> 是合法 zip -> 清单/配置可解析且版本已知 -> 条目数与体积
    在限额内 -> 每个条目的路径安全且不重复 -> (可选)逐条哈希与清单一致。任何一项
    不通过都抛 :class:`PackageError`, 并且此时**还没有写出任何文件**。
    """
    if not path.is_file():
        raise PackageError(f"导出包不存在: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            return _read_archive(path, archive, verify_hashes=verify_hashes)
    except zipfile.BadZipFile as exc:
        raise PackageError(f"不是有效的导出包(压缩包损坏): {path.name}") from exc


def read_package_kind(path: Path) -> str:
    """只读清单里的包类型(``kind``), 供调用方决定走哪条读包路径.

    只做"是合法 zip + 清单是可解析的 JSON 对象"这两步, **不解压任何内容**: 界面用它
    分派"单包 / 批量包"的体检流程, 真正的校验仍由 :func:`read_package` 与
    :func:`read_batch_package` 完成(所以这里放宽一点不会让坏包混进来)。旧包清单里
    没有 ``kind``, 一律按单游戏包处理 —— 与 :func:`_check_manifest` 的默认值一致.
    """
    if not path.is_file():
        raise PackageError(f"导出包不存在: {path}")
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = _read_json(archive, MANIFEST_NAME)
    except zipfile.BadZipFile as exc:
        raise PackageError(f"不是有效的导出包(压缩包损坏): {path.name}") from exc
    return str(manifest.get("kind", PACKAGE_KIND))


def _read_archive(
    path: Path, archive: zipfile.ZipFile, *, verify_hashes: bool
) -> PackageContents:
    """在已打开的 zip 上完成解析与全部校验."""
    infos = archive.infolist()
    _check_limits(infos)
    manifest = _read_json(archive, MANIFEST_NAME)
    # 先判包类型再找 config.json: 批量包本来就没有 config.json, 反过来问会报"缺配置",
    # 用户按那句提示找不到真正的原因(拿错了哪一类包).
    _check_manifest(manifest)
    config = _read_json(archive, CONFIG_NAME)
    entries = _declared_entries(manifest)
    _check_declared_files(infos, entries)
    if verify_hashes:
        _verify_hashes(archive, entries)
    return PackageContents(path=path, manifest=manifest, config=config, entries=entries)


def _check_limits(infos: Sequence[zipfile.ZipInfo]) -> None:
    """在解压之前按 zip 目录里的声明做上限检查."""
    files = [info for info in infos if not info.is_dir()]
    if len(files) > MAX_ENTRIES:
        raise PackageError("导出包条目数超过上限")
    total = 0
    for info in files:
        if info.file_size > MAX_ENTRY_BYTES:
            raise PackageError(f"导出包单个条目超过上限: {info.filename}")
        _check_ratio(info)
        total += info.file_size
    if total > MAX_TOTAL_BYTES:
        raise PackageError("导出包展开后总大小超过上限")


def _check_ratio(info: zipfile.ZipInfo) -> None:
    """拒绝压缩比异常高的条目(解压炸弹的特征)."""
    if info.compress_size == 0 or info.file_size < _RATIO_FLOOR_BYTES:
        return
    if info.file_size / info.compress_size > MAX_COMPRESSION_RATIO:
        raise PackageError(f"导出包条目压缩比异常: {info.filename}")


def _read_json(archive: zipfile.ZipFile, name: str) -> Mapping[str, object]:
    """从包里读一个 JSON 对象(缺失或不是对象都拒绝)."""
    try:
        raw = archive.read(name)
    except KeyError as exc:
        raise PackageError(f"导出包缺少 {name}") from exc
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise PackageError(f"导出包里的 {name} 不是合法 JSON") from exc
    if not isinstance(payload, Mapping):
        raise PackageError(f"导出包里的 {name} 不是 JSON 对象")
    return payload


def _check_manifest(
    manifest: Mapping[str, object], *, kind: str = PACKAGE_KIND
) -> None:
    """校验格式标识、版本号与包类型(未知版本一律拒绝, 不猜结构).

    ``kind`` 的默认值是单游戏包, 因此**旧包(清单里没有 kind)照常能读**;
    两类包互喂时给出"需要什么、实际是什么"的可读报错, 而不是等更深一层才失败。
    """
    if manifest.get("format") != EXPORT_FORMAT:
        raise PackageError("这不是本工具的导出包")
    version = manifest.get("version")
    if version != EXPORT_FORMAT_VERSION:
        raise PackageError(f"不支持的导出包格式版本: {version!r}")
    actual = manifest.get("kind", PACKAGE_KIND)
    if actual != kind:
        need, found = _kind_label(kind), _kind_label(actual)
        raise PackageError(f"包类型不符: 需要{need}, 实际是{found}")


def _kind_label(kind: object) -> str:
    """包类型的可读名称(未知取值原样显示, 便于报错定位)."""
    return _KIND_LABELS.get(str(kind), str(kind))


def _declared_entries(manifest: Mapping[str, object]) -> tuple[PackageEntry, ...]:
    """解析 manifest 里的条目表, 字段缺失或类型不符直接拒绝."""
    raw = manifest.get("entries")
    if not isinstance(raw, list):
        raise PackageError("导出包清单缺少条目表")
    entries: list[PackageEntry] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise PackageError("导出包清单的条目不是对象")
        name = validate_member_path(str(item.get("path", "")))
        size = item.get("size")
        sha = item.get("sha256")
        if not isinstance(size, int) or size < 0:
            raise PackageError(f"导出包清单条目的 size 非法: {name}")
        if not isinstance(sha, str) or len(sha) != 64:
            raise PackageError(f"导出包清单条目的 sha256 非法: {name}")
        entries.append(PackageEntry(path=name, size=size, sha256=sha))
    return tuple(entries)


def _check_declared_files(
    infos: Sequence[zipfile.ZipInfo], entries: Sequence[PackageEntry]
) -> None:
    """比对 zip 里的实际文件与清单声明: 一边少一边多都拒绝."""
    actual = _index_actual(infos)
    declared = {entry_key(entry.path) for entry in entries}
    missing = [entry.path for entry in entries if entry_key(entry.path) not in actual]
    if missing:
        raise PackageError(f"导出包缺少清单声明的条目: {missing[0]}")
    extra = [key for key in actual if key not in declared]
    if extra:
        raise PackageError(f"导出包含有清单未声明的条目: {extra[0]}")


def _index_actual(infos: Sequence[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
    """按归一键索引包内文件: 逐条校验路径, 拒绝符号链接与重复路径."""
    actual: dict[str, zipfile.ZipInfo] = {}
    for info in infos:
        if info.is_dir():
            continue
        name = validate_member_path(info.filename)
        if _is_symlink(info):
            raise PackageError(f"导出包含有符号链接条目: {name}")
        key = entry_key(name)
        if key in actual:
            raise PackageError(f"导出包里同一路径出现多次: {name}")
        actual[key] = info
    for name in (CONFIG_NAME, MANIFEST_NAME):
        actual.pop(entry_key(name), None)
    return actual


def _is_symlink(info: zipfile.ZipInfo) -> bool:
    """判断 zip 条目是否是符号链接(POSIX 模式下以文件类型位表示)."""
    mode = info.external_attr >> 16
    return bool(mode) and stat.S_ISLNK(mode)


def _verify_hashes(archive: zipfile.ZipFile, entries: Sequence[PackageEntry]) -> None:
    """逐条比对包内内容与清单哈希(导入前必做)."""
    for entry in entries:
        if _hash_member(archive, entry.path) != entry.sha256:
            raise PackageError(f"导出包内容与清单不一致: {entry.path}")


# ---------------------------------------------------------------- 解包


def extract_package(
    contents: PackageContents,
    destination: Path,
    *,
    members: Iterable[str] | None = None,
    verify_hashes: bool = True,
    progress: ProgressCallback | None = None,
    cancelled: CancelProbe | None = None,
) -> tuple[str, ...]:
    """把选定的条目解到 ``destination`` 之下, 返回实际写出的包内路径.

    每个条目在写之前都会重新算一次哈希(默认), 并且**拒绝覆盖已存在的文件** ——
    解包目标是调用方新建的暂存目录, 出现同名文件说明包结构或调用方式有问题,
    宁可立刻失败也不要互相覆盖。目录按需创建, 上限在读取阶段已经查过, 这里再按
    实际读到的字节数复核一次, 免得 zip 头里的声明与实际内容不符。
    """
    selected = _selected_entries(contents, members)
    written: list[str] = []
    total = 0
    with zipfile.ZipFile(contents.path) as archive:
        for index, entry in enumerate(selected):
            _report(
                cancelled,
                progress,
                index / max(1, len(selected)),
                f"正在解出 {entry.path}",
            )
            total = _extract_entry(
                archive, entry, destination, verify_hashes=verify_hashes, total=total
            )
            written.append(entry.path)
    return tuple(written)


def _selected_entries(
    contents: PackageContents, members: Iterable[str] | None
) -> list[PackageEntry]:
    """按调用方给定的包内路径挑选条目(不给就全部解出)."""
    if members is None:
        return list(contents.entries)
    wanted = {entry_key(name) for name in members}
    return [entry for entry in contents.entries if entry_key(entry.path) in wanted]


def _extract_entry(
    archive: zipfile.ZipFile,
    entry: PackageEntry,
    destination: Path,
    *,
    verify_hashes: bool,
    total: int,
) -> int:
    """解出一个条目并返回累计写入字节数(超过总上限直接拒绝)."""
    target = member_target(destination, entry.path)
    target.parent.mkdir(parents=True, exist_ok=True)
    written = _extract_member(archive, entry, target, verify_hashes=verify_hashes)
    if total + written > MAX_TOTAL_BYTES:
        raise PackageError("导出包展开后总大小超过上限")
    return total + written


def _extract_member(
    archive: zipfile.ZipFile,
    entry: PackageEntry,
    target: Path,
    *,
    verify_hashes: bool,
) -> int:
    """写出一个条目并返回实际字节数(边写边算哈希; 失败不留半成品)."""
    if target.exists():
        raise PackageError(f"解包目标已存在同名文件: {entry.path}")
    digest = hashlib.sha256()
    written = 0
    try:
        with archive.open(entry.path) as source, target.open("wb") as handle:
            for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
                written += len(chunk)
                if written > MAX_ENTRY_BYTES:
                    raise PackageError(f"导出包条目超过上限: {entry.path}")
                digest.update(chunk)
                handle.write(chunk)
        if written != entry.size:
            raise PackageError(f"导出包条目大小与清单不一致: {entry.path}")
        if verify_hashes and digest.hexdigest() != entry.sha256:
            raise PackageError(f"导出包条目哈希与清单不一致: {entry.path}")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return written


# ---------------------------------------------------------------- 批量包


@dataclass(frozen=True)
class BatchGame:
    """一个待写进批量包的游戏: 内层条目名 + 已经写好的单游戏导出包."""

    entry: str
    source: Path

    def __post_init__(self) -> None:
        """条目名在写入前就校验(不安全的形态绝不写出去)."""
        validate_batch_entry(self.entry)


@dataclass(frozen=True)
class BatchGameRow:
    """批量包清单里的一行: 内层条目 + 它的游戏标识与内容统计."""

    entry: PackageEntry
    name: str
    steam_app_id: int | None
    platform: str
    nodes: int
    files: int
    bytes: int

    def as_dict(self) -> dict[str, object]:
        """转换为写进批量包清单的 JSON 兼容字典."""
        return {
            "entry": self.entry.path,
            "name": self.name,
            "steam_app_id": self.steam_app_id,
            "platform": self.platform,
            "nodes": self.nodes,
            "files": self.files,
            "bytes": self.bytes,
            # 内层包**自身**的大小与哈希: 读包时先按 size 复核, 要求校验哈希时再比 sha256.
            "size": self.entry.size,
            "sha256": self.entry.sha256,
        }


@dataclass(frozen=True)
class BatchSummary:
    """写批量包的结果(供界面展示"这一批导出了多少")."""

    path: Path
    rows: tuple[BatchGameRow, ...]
    entries: tuple[PackageEntry, ...]

    @property
    def game_count(self) -> int:
        """包里的游戏数."""
        return len(self.rows)

    @property
    def file_count(self) -> int:
        """外层条目数(等于游戏数: 批量包不重复游戏内容)."""
        return len(self.entries)

    @property
    def total_bytes(self) -> int:
        """外层条目(各内层单包)的总字节数."""
        return sum(entry.size for entry in self.entries)


@dataclass(frozen=True)
class BatchGameContents:
    """批量包里的一个游戏: 外层清单那一行 + 已解析好的内层单包."""

    row: BatchGameRow
    package: PackageContents

    @property
    def entry(self) -> str:
        """内层包在批量包里的条目名."""
        return self.row.entry.path

    @property
    def name(self) -> str:
        """游戏名称(取自外层清单)."""
        return self.row.name


@dataclass
class BatchContents:
    """批量包的读取结果: 外层清单 + 有序的内层单包.

    内层单包已解到 ``workdir`` 下的临时文件里, 调用方需要它们在自己用完之前一直
    存在, 所以本对象是**上下文管理器**: ``with read_batch_package(path) as batch:``
    在退出时删掉临时目录; 也可以显式调用 :meth:`close`。不进入 ``with`` 就必须自己
    负责收尾, 否则临时文件会留到进程结束(只是临时文件, 不会写到别处).
    """

    path: Path
    manifest: Mapping[str, object]
    games: tuple[BatchGameContents, ...]
    workdir: Path

    @property
    def game_count(self) -> int:
        """包里的游戏数."""
        return len(self.games)

    def close(self) -> None:
        """删除解出来的临时目录(可重复调用)."""
        shutil.rmtree(self.workdir, ignore_errors=True)

    def __enter__(self) -> BatchContents:
        """进入上下文: 返回自身, 临时目录在退出时才删."""
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """退出上下文: 删掉临时目录(异常照常向外传播)."""
        self.close()


@dataclass(frozen=True)
class _BatchPlan:
    """一个内层包的写入计划: 条目声明 + 磁盘上的来源."""

    entry: PackageEntry
    source: Path


def archive_entry_name(slug: str) -> str:
    """由游戏名片段拼出批量包里的内层条目名(``<slug>.archive.zip``)."""
    return f"{slug}{ARCHIVE_SUFFIX}"


def validate_batch_entry(name: str) -> str:
    """校验内层包在批量包里的条目名: 单段相对名 + 以 ``.zip`` 结尾.

    内层包是**一个文件**, 所以条目名必须是单个路径片段(不含 ``/``); 这条规则
    同时排除了"用一层子目录把内容藏起来"的形态。其余路径检查复用
    :func:`validate_member_path`(绝对路径、``..``、反斜杠一律拒绝).
    """
    raw = validate_member_path(name)
    if "/" in raw or not raw.lower().endswith(".zip"):
        raise PackageError(f"批量包里的条目名必须是单段 .zip 文件名: {raw}")
    return raw


def write_batch_package(
    destination: Path,
    *,
    games: Sequence[BatchGame],
    tool_version: str | None = None,
    progress: ProgressCallback | None = None,
    cancelled: CancelProbe | None = None,
) -> BatchSummary:
    """把若干已经写好的单游戏导出包装成一个批量包, 返回包摘要.

    ``games`` 的每一项是"内层条目名 + 磁盘上已经存在的单包"(由
    :func:`write_package` 产出): 这里只把它们**原样**搬进外层 zip, 因此批量包
    不重复任何游戏内容, 它的体积约等于各内层包之和。

    流程与 :func:`write_package` 完全一致: 校验条目名与来源 -> 写同级临时文件
    (逐条算 sha256) -> **回读校验**每个内层条目的大小与哈希 -> 原子改名。任一步
    失败都删掉临时文件, 用户选择的路径上不会出现半成品。
    """
    plans = _plan_batch_entries(games)
    rows = tuple(_read_batch_game(plan) for plan in plans)
    manifest = _build_batch_manifest(rows=rows, tool_version=tool_version)
    temporary = _partial_path(destination)
    try:
        _write_batch_zip(
            temporary,
            plans=plans,
            manifest=manifest,
            progress=progress,
            cancelled=cancelled,
        )
        _verify_written_batch(temporary, plans=plans, cancelled=cancelled)
        temporary.replace(destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    logger.debug("批量导出包已写入: %s (%d 款游戏)", destination, len(plans))
    return BatchSummary(
        path=destination,
        rows=rows,
        entries=tuple(plan.entry for plan in plans),
    )


def _plan_batch_entries(games: Sequence[BatchGame]) -> tuple[_BatchPlan, ...]:
    """校验每个内层包并记录它的大小与哈希(条目名不许重复)."""
    if not games:
        raise PackageError("批量包里至少要有 1 款游戏")
    if len(games) > MAX_BATCH_GAMES:
        raise PackageError(f"批量包里的游戏数超过上限: {MAX_BATCH_GAMES}")
    plans = tuple(_plan_batch_entry(game) for game in games)
    _reject_duplicates([plan.entry for plan in plans])
    if sum(plan.entry.size for plan in plans) > MAX_TOTAL_BYTES:
        raise PackageError("批量包的总大小超过上限")
    return plans


def _plan_batch_entry(game: BatchGame) -> _BatchPlan:
    """校验一个内层包来源并算出它的条目声明(大小 + sha256)."""
    entry = validate_batch_entry(game.entry)
    if game.source.is_symlink():
        # 跟随链接会把链接指向的内容(可能在游戏目录之外)一起装进包里.
        raise PackageError(f"批量包的条目不能是符号链接: {entry}")
    try:
        stat_result = game.source.stat()
    except OSError as exc:
        raise PackageError(f"批量包的条目读不到: {game.source} ({exc})") from exc
    if not stat.S_ISREG(stat_result.st_mode):
        raise PackageError(f"批量包的条目不是普通文件: {game.source}")
    size = int(stat_result.st_size)
    if size > MAX_ENTRY_BYTES:
        raise PackageError(f"批量包的条目超过上限: {entry}")
    return _BatchPlan(
        entry=PackageEntry(path=entry, size=size, sha256=sha256_of_file(game.source)),
        source=game.source,
    )


def _read_batch_game(plan: _BatchPlan) -> BatchGameRow:
    """读内层单包的清单, 把它的游戏标识与内容统计记进批量包清单.

    内层包在这里就交给 :func:`read_package` 走一遍单包的全部校验: 坏包在**写包时**
    就被拒绝, 不会先写出去再等导入时才发现。
    """
    contents = read_package(plan.source)
    game = contents.game
    name = _game_text(game.get("name"))
    if not name:
        raise PackageError(f"内层导出包里没有游戏名称: {plan.entry.path}")
    files, total = _content_totals(contents)
    return BatchGameRow(
        entry=plan.entry,
        name=name,
        steam_app_id=_game_app_id(game),
        platform=_game_platform(game),
        nodes=len(contents.config_list("backups")),
        files=files,
        bytes=total,
    )


def _content_totals(contents: PackageContents) -> tuple[int, int]:
    """内层包的内容文件数与总字节数(与它自己的清单一致)."""
    return len(contents.entries), sum(entry.size for entry in contents.entries)


def _game_app_id(game: Mapping[str, object]) -> int | None:
    """清单里的 steam_app_id(布尔值不算整数)."""
    value = game.get("steam_app_id")
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _game_platform(game: Mapping[str, object]) -> str:
    """清单里的平台标识(缺失按 windows 处理, 与单包导入同一口径)."""
    return _game_text(game.get("platform"), "windows")


def _build_batch_manifest(
    *, rows: Sequence[BatchGameRow], tool_version: str | None
) -> dict[str, object]:
    """组装批量包清单: 格式/类型/版本/时间 + 逐游戏条目与统计."""
    return {
        "format": EXPORT_FORMAT,
        "kind": BATCH_KIND,
        "version": EXPORT_FORMAT_VERSION,
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tool_version": package_version() if tool_version is None else tool_version,
        "games": [row.as_dict() for row in rows],
        "files": {
            "count": len(rows),
            "bytes": sum(row.entry.size for row in rows),
        },
    }


def _write_batch_zip(
    temporary: Path,
    *,
    plans: Sequence[_BatchPlan],
    manifest: Mapping[str, object],
    progress: ProgressCallback | None,
    cancelled: CancelProbe | None,
) -> None:
    """把各内层包与清单依次写进外层 zip(内容原样搬过去, 不重复游戏内容)."""
    total = max(1, len(plans))
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, plan in enumerate(plans):
            _report(cancelled, progress, index / total, f"正在写入 {plan.entry.path}")
            with (
                archive.open(plan.entry.path, "w") as target,
                plan.source.open("rb") as source,
            ):
                for chunk in iter(lambda: source.read(_CHUNK_SIZE), b""):
                    target.write(chunk)
        _report(cancelled, progress, 1.0, "正在写入清单")
        archive.writestr(MANIFEST_NAME, _json_text(manifest))


def _verify_written_batch(
    temporary: Path, *, plans: Sequence[_BatchPlan], cancelled: CancelProbe | None
) -> None:
    """回读刚写好的批量包, 逐条比对内层条目的大小与 sha256."""
    expected = {plan.entry.path: plan.entry for plan in plans}
    with zipfile.ZipFile(temporary) as archive:
        found = _written_batch_members(archive)
        if found != set(expected):
            raise PackageError("批量包的条目与清单不一致")
        for name in sorted(found):
            _verify_member(archive, expected[name], cancelled)


def _written_batch_members(archive: zipfile.ZipFile) -> set[str]:
    """返回外层包内除清单之外的文件名集合(少一个就拒绝)."""
    names = [name for name in archive.namelist() if not name.endswith("/")]
    if MANIFEST_NAME not in names:
        raise PackageError(f"批量包缺少 {MANIFEST_NAME}")
    return {name for name in names if name != MANIFEST_NAME}


def read_batch_package(path: Path, *, verify_hashes: bool = False) -> BatchContents:
    """读取批量包, 把每个内层单包解到临时目录, 返回上下文管理器.

    外层只做"结构 + 上限 + 条目名"这一层检查, 每个内层包再交给
    :func:`read_package` 完成单包的全部校验(路径、大小、符号链接、哈希), 因此
    批量格式没有第二套单包校验。

    **临时目录的生命周期由调用方决定**: 内层包在 ``with`` 块内一直可用, 退出时
    临时目录被删掉; 也可以显式 :meth:`BatchContents.close`。``verify_hashes``
    的含义与单包一致: 为真时逐条比对内层包的 sha256(导入前必做), 为假时只按
    清单声明的大小复核。
    """
    if not path.is_file():
        raise PackageError(f"导出包不存在: {path}")
    workdir = Path(tempfile.mkdtemp(prefix=_BATCH_TEMP_PREFIX))
    try:
        contents = _open_batch(path, workdir, verify_hashes=verify_hashes)
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    return contents


def _open_batch(path: Path, workdir: Path, *, verify_hashes: bool) -> BatchContents:
    """在临时目录里展开外层包(损坏的 zip 在这里变成可读的错误)."""
    try:
        with zipfile.ZipFile(path) as archive:
            return _read_batch_archive(
                path, archive, workdir, verify_hashes=verify_hashes
            )
    except zipfile.BadZipFile as exc:
        raise PackageError(f"不是有效的导出包(压缩包损坏): {path.name}") from exc


def _read_batch_archive(
    path: Path, archive: zipfile.ZipFile, workdir: Path, *, verify_hashes: bool
) -> BatchContents:
    """在已打开的外层 zip 上完成解析: 上限 -> 清单 -> 条目 -> 逐个内层包."""
    _check_limits(archive.infolist())
    manifest = _read_json(archive, MANIFEST_NAME)
    _check_manifest(manifest, kind=BATCH_KIND)
    rows = _declared_games(manifest)
    _check_batch_declared(archive, rows)
    games = tuple(
        _materialize_game(archive, row, workdir, verify_hashes=verify_hashes)
        for row in rows
    )
    return BatchContents(path=path, manifest=manifest, games=games, workdir=workdir)


def _declared_games(manifest: Mapping[str, object]) -> tuple[BatchGameRow, ...]:
    """解析批量包清单里的 games 数组(空数组同样拒绝)."""
    raw = manifest.get("games")
    if not isinstance(raw, list) or not raw:
        raise PackageError("批量包清单里没有游戏")
    if len(raw) > MAX_BATCH_GAMES:
        raise PackageError(f"批量包里的游戏数超过上限: {MAX_BATCH_GAMES}")
    return tuple(_game_row(item) for item in raw)


def _game_row(item: object) -> BatchGameRow:
    """解析清单里的一行游戏(字段缺失或类型不符直接拒绝)."""
    if not isinstance(item, Mapping):
        raise PackageError("批量包清单里的游戏不是对象")
    entry = _row_entry(item)
    name = _game_text(item.get("name"))
    if not name:
        raise PackageError(f"批量包清单条目的 name 非法: {entry.path}")
    return BatchGameRow(
        entry=entry,
        name=name,
        steam_app_id=_game_app_id(item),
        platform=_game_platform(item),
        nodes=_row_count(item.get("nodes")),
        files=_row_count(item.get("files")),
        bytes=_row_count(item.get("bytes")),
    )


def _row_entry(item: Mapping[str, object]) -> PackageEntry:
    """解析清单里一行的条目名、大小与哈希(类型不符直接拒绝)."""
    entry = validate_batch_entry(str(item.get("entry", "")))
    size = item.get("size")
    sha = item.get("sha256")
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise PackageError(f"批量包清单条目的 size 非法: {entry}")
    if not isinstance(sha, str) or len(sha) != 64:
        raise PackageError(f"批量包清单条目的 sha256 非法: {entry}")
    return PackageEntry(path=entry, size=size, sha256=sha)


def _game_text(value: object, default: str = "") -> str:
    """取清单里的非空字符串字段(不是非空字符串就按默认值处理)."""
    return value if isinstance(value, str) and value else default


def _row_count(value: object) -> int:
    """取清单里的计数字段(布尔值与缺省都按 0 处理)."""
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _check_batch_declared(
    archive: zipfile.ZipFile, rows: Sequence[BatchGameRow]
) -> None:
    """比对清单与实际条目: 缺、多、重复、大小不符都拒绝.

    哈希不在这里查 —— 每个声明的条目接下来都会被写成临时文件, 那一步走的是
    :func:`_extract_member`, 它自己就会边写边算哈希(``verify_hashes`` 为真时), 所以
    整包只读一遍而不是两遍.
    """
    actual = _index_actual(archive.infolist())
    declared = _unique_batch_entries(rows)
    _check_batch_members(declared, actual)
    for row in rows:
        if actual[entry_key(row.entry.path)].file_size != row.entry.size:
            raise PackageError(f"批量包条目大小与清单不一致: {row.entry.path}")


def _unique_batch_entries(rows: Sequence[BatchGameRow]) -> dict[str, BatchGameRow]:
    """按归一键索引清单里的内层条目(重复即拒绝)."""
    declared: dict[str, BatchGameRow] = {}
    for row in rows:
        key = entry_key(row.entry.path)
        if key in declared:
            raise PackageError(f"批量包清单里的条目重复: {row.entry.path}")
        declared[key] = row
    return declared


def _check_batch_members(
    declared: Mapping[str, BatchGameRow], actual: Mapping[str, zipfile.ZipInfo]
) -> None:
    """清单与实际条目必须一一对应."""
    missing = [row.entry.path for key, row in declared.items() if key not in actual]
    if missing:
        raise PackageError(f"批量包缺少清单声明的条目: {missing[0]}")
    extra = [key for key in actual if key not in declared]
    if extra:
        raise PackageError(f"批量包含有清单未声明的条目: {extra[0]}")


def _materialize_game(
    archive: zipfile.ZipFile, row: BatchGameRow, workdir: Path, *, verify_hashes: bool
) -> BatchGameContents:
    """把一个内层包解到临时目录, 再交给 read_package 做单包的全部校验."""
    target = workdir / row.entry.path
    _extract_member(archive, row.entry, target, verify_hashes=verify_hashes)
    package = _read_inner(target, row)
    _check_game_identity(row, package)
    return BatchGameContents(row=row, package=package)


def _read_inner(target: Path, row: BatchGameRow) -> PackageContents:
    """读内层单包并把单包自己的报错挂上条目名(坏包也能报出是哪一款)."""
    try:
        return read_package(target)
    except PackageError as exc:
        raise PackageError(
            f"批量包里的 {row.entry.path} 不是合法的单包: {exc}"
        ) from exc


def _check_game_identity(row: BatchGameRow, package: PackageContents) -> None:
    """外层清单那一行必须与内层包自己的游戏标识一致(否则是拼错的包)."""
    game = package.game
    if _game_text(game.get("name")) != row.name:
        raise PackageError(f"批量包条目与内层包的游戏名称不一致: {row.entry.path}")
    if _game_app_id(game) != row.steam_app_id:
        raise PackageError(f"批量包条目与内层包的游戏标识不一致: {row.entry.path}")
    if _game_platform(game) != row.platform:
        raise PackageError(f"批量包条目与内层包的游戏平台不一致: {row.entry.path}")
