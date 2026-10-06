"""校验方式(严格程度)与"耗时校验"的协程执行器.

校验方式见 ``domain.entities.VERIFICATION_MODES``, 它决定备份与还原时把内容核对到
什么程度:

- ``sha256``: 逐文件比对内容哈希 —— 严格, 但哈希本身就是这条链路上最贵的一步(要把
  磁盘上的数据完整读一遍, Windows 上刚写过的文件还要过一遍杀毒软件的实时扫描);
- ``name``:   只核对清单里的名称与类型, 不读快照内容 —— 快, 但查不出"内容被改过"。

于是名称模式下的备份**先不算哈希**(备份很快就结束), 由后台的协程执行器随后补齐; 还原
预检这类"必须把哈希算出来"的场合也走同一个执行器。执行器对并发做**显式上限**: 每个任务
都要完整读一个文件, 同时跑几十个既争磁盘又堆内存(用户要求: 同时触发过多的校验会内存
溢出)。上限按本机 CPU 与内存算一次(见 :func:`recommended_parallel`), 首次启动写进
配置文件, 之后从配置里读(见 ``config.VerificationSettings``)。

模块只依赖标准库与 psutil, 不导入 Tkinter, 可在无显示环境测试。
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from archive_management.domain import (
    DEFAULT_VERIFICATION_MODE,
    VerificationMode,
)
from archive_management.exceptions import SnapshotError
from archive_management.services.snapshot import (
    SnapshotEntry,
    SnapshotVerification,
    manifest_entries,
    sha256_of_file,
    verify_snapshot,
)

# 并发上限的允许区间, 以及"配置里没有/探测不出来"时的保守取值.
MIN_PARALLEL = 1
MAX_PARALLEL = 8
DEFAULT_PARALLEL = 2
# 每个并发任务按这么多可用内存估算: 校验要把文件完整读一遍, 每个任务至少占一份读缓冲
# (1 MiB)与解释器开销, 可用内存越少就该少跑几个任务。
_PARALLEL_MEMORY_STEP = 256 * 1024 * 1024
# 只让一半逻辑核心参与: 校验是"读磁盘 + 算哈希"的混合负载, 占满所有核心会让界面发卡。
_PARALLEL_CPU_SHARE = 2


def recommended_parallel() -> int:
    """按本机 CPU 与可用内存算一个"不打扰用户"的校验并发数.

    取"CPU 允许的一半核心"与"可用内存允许的任务数"中较小的一边, 再夹进
    [:data:`MIN_PARALLEL`, :data:`MAX_PARALLEL`]。上限的存在是为了保证一次校验不会把
    磁盘与内存吃满; 探测失败时回落到 :data:`DEFAULT_PARALLEL` —— 宁可慢一点, 也不猜。
    """
    import psutil

    try:
        cpus = psutil.cpu_count(logical=True) or 0
        available = int(psutil.virtual_memory().available)
    except Exception:
        # 个别平台/容器里读不到内存信息: 此时宁可用保守值, 也不猜一个大的
        # (用例里把 virtual_memory 换成抛异常的替身来覆盖这一路)。
        return DEFAULT_PARALLEL
    by_cpu = cpus // _PARALLEL_CPU_SHARE
    by_memory = available // _PARALLEL_MEMORY_STEP
    return max(MIN_PARALLEL, min(by_cpu, by_memory, MAX_PARALLEL))


def clamp_parallel(value: int) -> int:
    """把并发上限夹进允许区间(配置已校验, 这里兜住直接构造策略的调用方)."""
    return max(MIN_PARALLEL, min(value, MAX_PARALLEL))


@dataclass(frozen=True)
class VerificationPolicy:
    """一次备份/还原使用的校验方式与并发上限(生产里来自配置的 ``verification`` 段)."""

    mode: VerificationMode = DEFAULT_VERIFICATION_MODE
    max_parallel: int = DEFAULT_PARALLEL


def hashes_complete(entries: Iterable[SnapshotEntry]) -> bool:
    """清单里的文件条目是否都带着 sha256(目录与符号链接只记标记, 不参与)."""
    return all(entry.sha256 for entry in entries if entry.file_kind == "file")


def resolve_check(
    requested: VerificationMode, *, hashes_complete: bool
) -> VerificationMode:
    """决定这次**真正执行**的校验方式.

    用户选的名称模式按原样执行(它本来就不需要哈希数据); 选了 sha256(以及将来新增的
    校验方式)时, 只有这份备份真的有哈希数据才能按 sha256 校 —— 老备份、或者名称模式下
    还没补齐的备份没有哈希, 只能降级为只校验名称。降级不会静默: 调用方拿
    :attr:`SnapshotVerification.mode` 与请求值一比就知道, 界面据此提示用户。

    回落方向一律是**更严**的那一侧: 将来新增的校验方式在老备份上没有数据时, 同样回落到
    sha256, 而不是什么都不查。
    """
    if requested == "name":
        return "name"
    return "sha256" if hashes_complete else "name"


async def hash_files_async(
    paths: Sequence[Path],
    *,
    limit: int,
    cancelled: Callable[[], bool] | None = None,
) -> dict[Path, str]:
    """并发算出这些文件的 sha256, 同时进行的任务数不超过 ``limit``.

    用**固定数量的工作协程**从队列里取活, 而不是"给每个文件建一个任务、再用信号量排队":
    后者会一次建出与文件数同样多的待命任务(大存档是几万个), 光任务对象本身就是内存开销。
    这里的队列里只放路径, 在跑的任务数与内存占用都恒定。
    """
    pending = deque(paths)
    results: dict[Path, str] = {}
    if not pending:
        return results
    width = min(clamp_parallel(limit), len(pending))

    async def worker() -> None:
        # 下面三步之间没有 await: "取一条路径"不会与别的协程交错。
        while pending:
            if cancelled is not None and cancelled():
                return
            path = pending.popleft()
            try:
                results[path] = await asyncio.to_thread(sha256_of_file, path)
            except OSError:
                # 文件在两次扫描之间被删掉/读不了: 不中断整轮校验, 缺的那条哈希会在
                # 核对阶段被判成"不一致"(见 _digest_lookup), 属于安全的那一侧。
                continue

    await asyncio.gather(*(worker() for _ in range(width)))
    return results


def hash_files(
    paths: Sequence[Path],
    *,
    limit: int,
    cancelled: Callable[[], bool] | None = None,
) -> dict[Path, str]:
    """同步门面: 在同一线程里跑完协程执行器(调用方自己负责放进后台线程).

    ``asyncio.run`` 需要一个没有事件循环的线程, 因此这个函数只从普通线程里调 —— 本项目的
    后台工作就是 ``threading.Thread``(界面线程不会被它挡住)。
    """
    return asyncio.run(hash_files_async(paths, limit=limit, cancelled=cancelled))


def _digest_lookup(digests: dict[Path, str]) -> Callable[[Path], str]:
    """给清单核对用的取哈希函数: 没算出来的返回空串(判成不一致, 不静默放过)."""
    return lambda path: digests.get(path, "")


def verification_of(
    root: Path,
    *,
    policy: VerificationPolicy,
    cancelled: Callable[[], bool] | None = None,
) -> SnapshotVerification:
    """按 ``policy`` 校验一份快照; 需要读内容时走并发有上限的协程执行器.

    结果里的 ``mode`` 是**真正执行**的方式: 它与 ``policy.mode`` 不一致就表示这次降级了
    (这份备份还没有哈希数据), 调用方据此提示用户。清单缺失或损坏时返回一份
    ``ok=False`` 的结果(不抛异常), 与 :func:`verify_snapshot` 同一套交代。
    """
    root = Path(root)
    try:
        entries = manifest_entries(root)
    except SnapshotError as exc:
        # 清单缺失或损坏: 给一份"校验未通过"的结果, 与 verify_snapshot 遇到缺清单时的
        # 做法一致 —— 调用方(还原预检/界面)按"快照不完整"处理, 不必自己接异常。
        return SnapshotVerification(ok=False, checked=0, reason=str(exc))
    mode = resolve_check(policy.mode, hashes_complete=hashes_complete(entries))
    if mode == "name":
        result = verify_snapshot(root, deep=False)
    else:
        targets = [
            root / entry.relative_path for entry in entries if entry.file_kind == "file"
        ]
        digests = hash_files(targets, limit=policy.max_parallel, cancelled=cancelled)
        result = verify_snapshot(root, deep=True, hasher=_digest_lookup(digests))
    return replace(result, mode=mode)


__all__ = [
    "DEFAULT_PARALLEL",
    "MAX_PARALLEL",
    "MIN_PARALLEL",
    "VerificationPolicy",
    "clamp_parallel",
    "hash_files",
    "hash_files_async",
    "hashes_complete",
    "recommended_parallel",
    "resolve_check",
    "verification_of",
]
