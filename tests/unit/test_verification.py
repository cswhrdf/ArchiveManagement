"""校验方式与耗时校验执行器的单元测试.

覆盖三件事: 校验方式的取值收敛(空值与陌生取值一律回落到 sha256)、并发上限的推算与夹取,
以及"需要读内容的校验"真的走那个**有并发上限**的协程执行器。
"""

from __future__ import annotations

import asyncio
import threading
import time
from pathlib import Path

import psutil
import pytest

from archive_management.domain import DEFAULT_VERIFICATION_MODE
from archive_management.exceptions import SnapshotError
from archive_management.services import snapshot as snapshot_mod
from archive_management.services import verification as verification_mod
from archive_management.services.snapshot import SnapshotEntry, SnapshotSource
from archive_management.services.verification import (
    DEFAULT_PARALLEL,
    MAX_PARALLEL,
    MIN_PARALLEL,
    VerificationPolicy,
    clamp_parallel,
    hash_files,
    hash_files_async,
    hashes_complete,
    recommended_parallel,
    resolve_check,
    verification_of,
)

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("校验方式"),
    pytest.mark.story("选择校验方式"),
    pytest.mark.layer("unit"),
]


def _write(path: Path, content: bytes) -> Path:
    """写一个文件(顺带建好父目录)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def _snapshot(tmp_path: Path, *, hash_files: bool) -> Path:
    """建一份快照(``hash_files=False`` 即名称模式下那份"还没算哈希"的备份)."""
    save = tmp_path / "save"
    _write(save / "slot1.dat", b"alpha")
    _write(save / "nested" / "slot2.dat", b"beta")
    result = snapshot_mod.create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "backup",
        hash_files=hash_files,
    )
    return result.root


def test_recommended_parallel_stays_inside_the_allowed_range() -> None:
    """按本机算出来也要落在 [MIN, MAX] 里: 上界存在的意义就是别把磁盘与内存吃满."""
    assert MIN_PARALLEL <= recommended_parallel() <= MAX_PARALLEL


def test_recommended_parallel_falls_back_when_the_machine_cannot_be_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """拿不到内存信息时用保守值, 而不是瞎猜一个大数."""

    def _boom() -> object:
        raise OSError("拿不到内存信息")

    monkeypatch.setattr(psutil, "virtual_memory", _boom)

    assert recommended_parallel() == DEFAULT_PARALLEL


@pytest.mark.parametrize(
    ("cpus", "available", "expected"),
    [
        (1, 1, MIN_PARALLEL),  # 单核小内存也要保证至少跑一个任务
        (8, 512 * 1024 * 1024, 2),  # 内存只够两个任务时, 核再多也不超
        (64, 64 * 1024**3, MAX_PARALLEL),  # 机器再宽也只到上限
    ],
)
def test_recommended_parallel_takes_the_smaller_of_cpu_and_memory(
    monkeypatch: pytest.MonkeyPatch, cpus: int, available: int, expected: int
) -> None:
    """CPU 与内存两侧取小: 任一侧紧张就少跑几个任务."""
    memory = type("Memory", (), {"available": available})()
    monkeypatch.setattr(psutil, "cpu_count", lambda logical=True: cpus)
    monkeypatch.setattr(psutil, "virtual_memory", lambda: memory)

    assert recommended_parallel() == expected


def test_clamp_parallel_pulls_values_into_range() -> None:
    """直接构造策略的调用方也要被夹住(配置那边另有 Field 校验)."""
    assert clamp_parallel(0) == MIN_PARALLEL
    assert clamp_parallel(999) == MAX_PARALLEL


def test_hashes_complete_ignores_directories_and_symlinks() -> None:
    """目录与符号链接只记标记, 不看它们带不带哈希."""
    entries = (
        SnapshotEntry(relative_path="loc-0", file_kind="directory", sha256="dir"),
        SnapshotEntry(relative_path="loc-0/link", file_kind="symlink", sha256="link"),
    )

    assert hashes_complete(entries) is True
    assert hashes_complete([*entries, SnapshotEntry(relative_path="a.dat")]) is False


def test_resolve_check_keeps_the_requested_mode_when_the_data_allows_it() -> None:
    """有哈希数据就按请求的 sha256 校; 用户选的名称模式照旧按名称."""
    assert resolve_check("sha256", hashes_complete=True) == "sha256"
    assert resolve_check("name", hashes_complete=True) == "name"


def test_resolve_check_degrades_when_there_are_no_hashes_to_compare() -> None:
    """请求 sha256 而这份备份没有哈希数据时降级为名称(调用方据此提示用户)."""
    assert resolve_check("sha256", hashes_complete=False) == "name"


def test_hash_files_returns_nothing_for_an_empty_input() -> None:
    """没有要算的路径时不建事件循环、也不起线程."""
    assert hash_files([], limit=4) == {}


def test_hash_files_hashes_every_path(tmp_path: Path) -> None:
    """正常路径: 每个文件都拿到与直接算一致的摘要."""
    first = _write(tmp_path / "a.dat", b"alpha")
    second = _write(tmp_path / "b.dat", b"beta")

    digests = hash_files([first, second], limit=2)

    assert digests == {
        first: snapshot_mod.sha256_of_file(first),
        second: snapshot_mod.sha256_of_file(second),
    }
    assert digests[first] != digests[second]


def test_hash_files_never_runs_more_than_the_limit_at_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """并发上限是硬约束: 同时在跑的任务数不会超过它(超过就是同时吃磁盘与内存).

    每个任务先睡一会儿再算 —— 文件太小的话几个线程可能刚好错开, 量不到峰值。
    """
    paths = [_write(tmp_path / f"f{index}.dat", b"x") for index in range(9)]
    lock = threading.Lock()
    live = 0
    peak = 0

    def _slow_hash(path: Path) -> str:
        nonlocal live, peak
        with lock:
            live += 1
            peak = max(peak, live)
        try:
            time.sleep(0.05)
            return snapshot_mod.sha256_of_file(path)
        finally:
            with lock:
                live -= 1

    monkeypatch.setattr(verification_mod, "sha256_of_file", _slow_hash)

    digests = hash_files(paths, limit=3)

    assert len(digests) == 9
    assert peak == 3


def test_hash_files_skips_files_that_disappeared(tmp_path: Path) -> None:
    """算不出来的文件不写进结果(缺的那条会在核对阶段判成不一致, 属于安全的一侧)."""
    assert hash_files([tmp_path / "ghost.dat"], limit=2) == {}


def test_hash_files_stops_early_when_cancelled(tmp_path: Path) -> None:
    """取消后不再开工: 已经算完的留着, 没轮到的就算了."""
    paths = [_write(tmp_path / f"f{index}.dat", b"x") for index in range(4)]

    assert hash_files(paths, limit=1, cancelled=lambda: True) == {}


def test_hash_files_async_is_awaitable_inside_a_running_loop() -> None:
    """执行器本体是协程: 已经在事件循环里时可以直接 await(不必再套一层 asyncio.run)."""

    async def run() -> dict[Path, str]:
        return await hash_files_async([], limit=2)

    assert asyncio.run(run()) == {}


def test_verification_of_checks_hashes_in_sha256_mode(tmp_path: Path) -> None:
    """严格模式: 逐文件比对内容哈希, 内容被改过就报不一致."""
    root = _snapshot(tmp_path, hash_files=True)
    (root / "loc-0" / "slot1.dat").write_bytes(b"tampered")

    result = verification_of(root, policy=VerificationPolicy(mode="sha256"))

    assert result.ok is False
    assert result.mode == "sha256"
    assert result.mismatched == ("loc-0/slot1.dat",)


def test_verification_of_only_looks_at_names_in_name_mode(tmp_path: Path) -> None:
    """名称模式: 不读内容, 因此内容被改过也看不出来(这正是它快的原因)."""
    root = _snapshot(tmp_path, hash_files=True)
    (root / "loc-0" / "slot1.dat").write_bytes(b"tampered")

    result = verification_of(root, policy=VerificationPolicy(mode="name"))

    assert result.ok is True
    assert result.mode == "name"


def test_verification_of_degrades_when_the_snapshot_has_no_hashes(
    tmp_path: Path,
) -> None:
    """名称模式创建的备份没有哈希数据: 请求 sha256 时降级为名称, 并如实报出来."""
    root = _snapshot(tmp_path, hash_files=False)

    result = verification_of(root, policy=VerificationPolicy(mode="sha256"))

    assert result.ok is True
    assert result.mode == "name"


def test_verification_of_reports_a_corrupt_manifest_as_a_failure(
    tmp_path: Path,
) -> None:
    """清单读不出来时给一份"校验未通过"的结果, 而不是把异常甩给界面线程."""
    root = _snapshot(tmp_path, hash_files=True)
    (root / snapshot_mod.MANIFEST_FILENAME).write_text("{", encoding="utf-8")

    result = verification_of(root, policy=VerificationPolicy(mode="sha256"))

    assert result.ok is False
    assert result.reason is not None


def test_defaults_are_the_strict_mode_and_a_conservative_parallelism() -> None:
    """默认策略是 sha256 + 保守并发: 不配置也不能悄悄变松."""
    policy = VerificationPolicy()

    assert policy.mode == DEFAULT_VERIFICATION_MODE == "sha256"
    assert policy.max_parallel == DEFAULT_PARALLEL


def test_reading_entries_from_a_missing_snapshot_raises(tmp_path: Path) -> None:
    """直接读条目时缺清单要抛 SnapshotError(调用方按"快照不完整"处理)."""
    with pytest.raises(SnapshotError):
        snapshot_mod.manifest_entries(tmp_path / "nope")
