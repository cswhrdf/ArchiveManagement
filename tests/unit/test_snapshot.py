"""文件快照服务的单元测试.

覆盖"复制 -> 哈希校验 -> 原子提交"的完整链路, 以及三条硬性约束:
不覆盖已有备份、失败不留半成品、可取消。
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Callable
from pathlib import Path

import pytest

from archive_management.exceptions import ArchiveManagementError, SnapshotError
from archive_management.services import snapshot as snapshot_mod
from archive_management.services.snapshot import (
    MANIFEST_FILENAME,
    SnapshotEntry,
    SnapshotSource,
    _is_transient_os_error,
    content_hash_of,
    create_snapshot,
    read_manifest_entries,
    remove_snapshot,
    sha256_of_file,
    verify_snapshot,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("快照服务"),
    pytest.mark.story("生成与校验快照"),
    pytest.mark.layer("integration"),
]


def _saved_dir(root: Path) -> Path:
    """构造一个带子目录的存档目录."""
    save = root / "save"
    (save / "nested").mkdir(parents=True)
    (save / "slot1.dat").write_text("alpha", encoding="utf-8")
    (save / "nested" / "slot2.dat").write_text("beta", encoding="utf-8")
    return save


def test_create_snapshot_copies_files_and_writes_manifest(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    destination = tmp_path / "backup" / "node1"

    result = create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)], destination
    )

    assert result.root == destination
    assert destination.is_dir()
    assert (destination / MANIFEST_FILENAME).is_file()
    assert (destination / "loc-0" / "slot1.dat").read_text(encoding="utf-8") == "alpha"
    assert (destination / "loc-0" / "nested" / "slot2.dat").read_text(
        encoding="utf-8"
    ) == "beta"
    assert result.file_count() == 2
    assert result.total_size == len("alpha") + len("beta")
    assert result.content_hash == content_hash_of(result.entries)


def test_create_snapshot_records_verified_hashes(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    result = create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "node",
    )
    files = [entry for entry in result.entries if entry.file_kind == "file"]
    assert len(files) == 2
    for entry in files:
        copied = result.root / entry.relative_path
        assert entry.sha256 == sha256_of_file(copied)
        assert entry.size == copied.stat().st_size


def test_create_snapshot_refuses_to_overwrite_existing_destination(
    tmp_path: Path,
) -> None:
    save = _saved_dir(tmp_path)
    destination = tmp_path / "node"
    destination.mkdir()
    (destination / "keep.txt").write_text("unchanged", encoding="utf-8")

    with pytest.raises(SnapshotError):
        create_snapshot(
            [SnapshotSource(path=str(save), kind="directory", index=0)], destination
        )

    assert (destination / "keep.txt").read_text(encoding="utf-8") == "unchanged"
    assert not list(tmp_path.glob("node.partial-*"))


def test_create_snapshot_cleans_partial_directory_on_failure(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    calls = {"count": 0}

    def boom(_fraction: float, _message: str) -> None:
        calls["count"] += 1
        if calls["count"] >= 3:
            raise ArchiveManagementError("进度回调故意失败")

    with pytest.raises(ArchiveManagementError):
        create_snapshot(
            [SnapshotSource(path=str(save), kind="directory", index=0)],
            tmp_path / "node",
            progress=boom,
        )

    assert not (tmp_path / "node").exists()
    assert not list(tmp_path.glob("node.partial-*"))


def test_create_snapshot_cancellation_leaves_no_partial(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    seen = {"checks": 0}

    def cancelled() -> bool:
        seen["checks"] += 1
        return seen["checks"] > 1

    with pytest.raises(ArchiveManagementError):
        create_snapshot(
            [SnapshotSource(path=str(save), kind="directory", index=0)],
            tmp_path / "node",
            cancelled=cancelled,
        )

    assert not (tmp_path / "node").exists()
    assert not list(tmp_path.glob("node.partial-*"))


def test_create_snapshot_reports_monotonic_progress(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    fractions: list[float] = []
    create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "node",
        progress=lambda fraction, _message: fractions.append(fraction),
    )
    assert fractions
    assert fractions == sorted(fractions)
    assert fractions[-1] == 1.0


def test_create_snapshot_accepts_single_file_source(tmp_path: Path) -> None:
    payload = tmp_path / "slot.sav"
    payload.write_text("data", encoding="utf-8")
    result = create_snapshot(
        [SnapshotSource(path=str(payload), kind="file", index=0)], tmp_path / "node"
    )
    assert [entry.relative_path for entry in result.entries] == ["loc-0/slot.sav"]
    assert (result.root / "loc-0" / "slot.sav").read_text(encoding="utf-8") == "data"


def test_create_snapshot_handles_empty_directory(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    result = create_snapshot(
        [SnapshotSource(path=str(empty), kind="directory", index=0)],
        tmp_path / "node",
    )
    assert result.file_count() == 0
    assert (result.root / "loc-0").is_dir()
    assert verify_snapshot(result.root).ok is True


def test_create_snapshot_prefixes_multiple_sources_by_index(tmp_path: Path) -> None:
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    (first / "x.dat").write_text("1", encoding="utf-8")
    (second / "x.dat").write_text("2", encoding="utf-8")

    result = create_snapshot(
        [
            SnapshotSource(path=str(first), kind="directory", index=0),
            SnapshotSource(path=str(second), kind="directory", index=1),
        ],
        tmp_path / "node",
    )

    assert (result.root / "loc-0" / "x.dat").read_text(encoding="utf-8") == "1"
    assert (result.root / "loc-1" / "x.dat").read_text(encoding="utf-8") == "2"


def test_create_snapshot_rejects_missing_source(tmp_path: Path) -> None:
    with pytest.raises(SnapshotError):
        create_snapshot(
            [SnapshotSource(path=str(tmp_path / "gone"), kind="directory", index=0)],
            tmp_path / "node",
        )


def test_create_snapshot_records_symlink_without_following(tmp_path: Path) -> None:
    save = tmp_path / "save"
    save.mkdir()
    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    link = save / "link.txt"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建符号链接")

    result = create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "node",
    )

    entries = {entry.relative_path: entry for entry in result.entries}
    entry = entries["loc-0/link.txt"]
    assert entry.file_kind == "symlink"
    assert entry.link_target is not None
    assert not (result.root / "loc-0" / "link.txt").exists()
    assert result.file_count() == 0


def test_verify_snapshot_detects_missing_and_modified_files(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    result = create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "node",
    )
    assert verify_snapshot(result.root).ok is True

    (result.root / "loc-0" / "slot1.dat").unlink()
    broken = verify_snapshot(result.root)
    assert broken.ok is False
    assert "loc-0/slot1.dat" in broken.missing


def test_verify_snapshot_detects_tampering_when_deep(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    result = create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "node",
    )
    (result.root / "loc-0" / "slot1.dat").write_text("tampered", encoding="utf-8")

    shallow = verify_snapshot(result.root, deep=False)
    assert shallow.ok is True
    deep = verify_snapshot(result.root)
    assert deep.ok is False
    assert "loc-0/slot1.dat" in deep.mismatched


def test_verify_snapshot_reports_missing_manifest(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    outcome = verify_snapshot(empty)
    assert outcome.ok is False
    assert outcome.reason is not None


def test_read_manifest_entries_rejects_unknown_version(tmp_path: Path) -> None:
    save = _saved_dir(tmp_path)
    result = create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "node",
    )
    raw = json.loads((result.root / MANIFEST_FILENAME).read_text(encoding="utf-8"))
    raw["version"] = 99
    with pytest.raises(SnapshotError):
        read_manifest_entries(raw)


def test_read_manifest_entries_rejects_bad_entry() -> None:
    raw = {
        "version": 1,
        "entries": [{"relative_path": "", "size": 0, "sha256": "x"}],
    }
    with pytest.raises(SnapshotError):
        read_manifest_entries(raw)


def test_snapshot_entry_roundtrip() -> None:
    entry = SnapshotEntry(
        relative_path="loc-0/a.dat", size=3, sha256="abc", file_kind="file"
    )
    assert SnapshotEntry.from_dict(entry.as_dict()) == entry


def test_remove_snapshot_is_idempotent(tmp_path: Path) -> None:
    target = tmp_path / "gone"
    remove_snapshot(target)
    target.mkdir()
    remove_snapshot(target)
    assert not target.exists()


# --- 提交阶段: 瞬时占用重试 ----------------------------------------------------


class _CommitProbe:
    """记录改名次数并按脚本抛错的替身(前 ``failures`` 次失败)."""

    def __init__(self, error: OSError, *, failures: int) -> None:
        self.error = error
        self.failures = failures
        self.calls = 0

    def handler(self) -> Callable[[Path, Path | str], Path]:
        """返回可直接赋给 ``Path.replace`` 的函数(以方法形式被调用)."""
        original = Path.replace

        def replace(source: Path, target: Path | str) -> Path:
            self.calls += 1
            if self.calls <= self.failures:
                raise self.error
            return original(source, target)

        return replace

    def attach(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """把替身装到 ``Path.replace`` 上."""
        monkeypatch.setattr(Path, "replace", self.handler())


def _blocked_by_scanner() -> OSError:
    """构造"杀毒软件/索引器正在扫描"造成的改名失败.

    Windows 用错误码 5(拒绝访问); 其它平台没有 ``winerror``, 退回 ``EACCES`` ——
    两者都在可重试集合里, 用例因此在三个平台上验证的是同一条重试路径(早先只造
    Windows 错误码, 在 Linux/macOS 上会被归为"硬性失败"而不重试)。
    """
    if os.name == "nt":
        return OSError(errno.EACCES, "拒绝访问。", "temporary", 5)
    return PermissionError(errno.EACCES, "拒绝访问。")


class _FakeWindowsError(OSError):
    """带 Windows 错误码的异常替身.

    POSIX 上 ``OSError`` 没有 ``winerror`` 槽位(直接赋值会 ``AttributeError``), 用子类
    实例(自带 ``__dict__``)挂上错误码, 就能在三个平台上验证同一段分类逻辑。
    """

    def __init__(self, winerror: int) -> None:
        super().__init__(0, "拒绝访问")
        self.winerror = winerror


def _windows_error(winerror: int) -> OSError:
    """构造带 Windows 错误码的异常(供跨平台验证可重试分类)."""
    return _FakeWindowsError(winerror)


@pytest.mark.parametrize(
    ("error", "retryable"),
    [
        (PermissionError(errno.EACCES, "拒绝访问"), True),
        (OSError(errno.EBUSY, "设备忙"), True),
        (OSError(errno.EPERM, "不允许"), True),
        (OSError(errno.ENOSPC, "磁盘已满"), False),
        (FileNotFoundError(errno.ENOENT, "找不到"), False),
    ],
)
def test_transient_error_classification_covers_posix_codes(
    error: OSError, retryable: bool
) -> None:
    """可重试判定要按 errno 认瞬时占用, 硬性失败不能误判."""
    assert _is_transient_os_error(error) is retryable


@pytest.mark.parametrize("winerror", [5, 32, 33])
def test_transient_error_classification_covers_windows_codes(winerror: int) -> None:
    """Windows 上"拒绝访问/共享冲突/锁定冲突"都算瞬时占用."""
    assert _is_transient_os_error(_windows_error(winerror)) is True


def test_transient_error_classification_rejects_unknown_windows_code() -> None:
    """其它 Windows 错误码(例如 2 找不到路径)不算瞬时占用."""
    assert _is_transient_os_error(_windows_error(2)) is False


def _snapshot_dir(tmp_path: Path) -> Path:
    """造一个最小存档目录, 供提交用例使用."""
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("alpha", encoding="utf-8")
    return save


def test_commit_retries_transient_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """改名被瞬时占用拒绝时重试: 前两次失败、第三次成功, 快照照常提交."""
    probe = _CommitProbe(_blocked_by_scanner(), failures=2)
    probe.attach(monkeypatch)
    destination = tmp_path / "backup" / "node1"

    result = create_snapshot(
        [SnapshotSource(path=str(_snapshot_dir(tmp_path)), kind="directory", index=0)],
        destination,
    )

    assert probe.calls == 3
    assert result.root.is_dir()
    assert (destination / "loc-0" / "slot1.dat").read_text(encoding="utf-8") == "alpha"
    assert not list(destination.parent.glob("*.partial-*"))


def test_commit_retries_errno_based_denial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POSIX 上的 EACCES(权限暂时不满足)同样按瞬时错误重试一次即可成功."""
    probe = _CommitProbe(PermissionError(errno.EACCES, "拒绝访问"), failures=1)
    probe.attach(monkeypatch)

    result = create_snapshot(
        [SnapshotSource(path=str(_snapshot_dir(tmp_path)), kind="directory", index=0)],
        tmp_path / "backup" / "node2",
    )

    assert probe.calls == 2
    assert result.root.is_dir()


def test_commit_does_not_retry_hard_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘写满这类硬性失败立刻上报: 重试只会让用户多等, 不会成功."""
    probe = _CommitProbe(OSError(errno.ENOSPC, "磁盘已满"), failures=99)
    probe.attach(monkeypatch)
    destination = tmp_path / "backup" / "node3"

    with pytest.raises(SnapshotError, match="提交快照失败: "):
        create_snapshot(
            [
                SnapshotSource(
                    path=str(_snapshot_dir(tmp_path)), kind="directory", index=0
                )
            ],
            destination,
        )

    assert probe.calls == 1
    assert not destination.exists()
    assert not list(destination.parent.glob("*.partial-*"))


def test_commit_reports_retry_count_when_occupation_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一直占用时在上限处失败, 且错误里保留原始原因与重试次数, 供审计排查."""
    probe = _CommitProbe(_blocked_by_scanner(), failures=99)
    probe.attach(monkeypatch)
    destination = tmp_path / "backup" / "node4"

    with pytest.raises(SnapshotError) as failure:
        create_snapshot(
            [
                SnapshotSource(
                    path=str(_snapshot_dir(tmp_path)), kind="directory", index=0
                )
            ],
            destination,
        )

    message = str(failure.value)
    assert "已重试 4 次" in message
    assert "拒绝访问" in message
    assert probe.calls == 5
    # 失败仍要清理临时目录, 不留半成品.
    assert not destination.exists()
    assert not list(destination.parent.glob("*.partial-*"))


# -- 清单解析的拒绝 ---------------------------------------------------------


def test_read_manifest_entries_rejects_values_that_are_not_manifests() -> None:
    """清单顶层不是对象 / 缺 entries 列表 / 条目不是对象: 一律拒绝."""
    with pytest.raises(SnapshotError, match="顶层必须是对象"):
        read_manifest_entries([1])

    with pytest.raises(SnapshotError, match="缺少 entries"):
        read_manifest_entries({"version": snapshot_mod.SNAPSHOT_FORMAT_VERSION})

    with pytest.raises(SnapshotError, match="清单项必须是对象"):
        read_manifest_entries(
            {"version": snapshot_mod.SNAPSHOT_FORMAT_VERSION, "entries": [1]}
        )


def _node_with_a_manifest(tmp_path: Path) -> Path:
    """造一份真实快照并返回它的根目录(用于改写清单的现场)."""
    destination = tmp_path / "node"
    create_snapshot(
        [SnapshotSource(path=str(_saved_dir(tmp_path)), kind="directory", index=0)],
        destination,
    )
    return destination


def _set_manifest_sources(root: Path, sources: object) -> None:
    """只改清单里的 sources, 其余字段原样保留."""
    path = root / MANIFEST_FILENAME
    payload: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    payload["sources"] = sources
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_read_manifest_rejects_a_source_item_that_is_not_an_object(
    tmp_path: Path,
) -> None:
    """清单里的来源项不是对象时拒绝: 恢复要靠它定位写回目标."""
    root = _node_with_a_manifest(tmp_path)
    _set_manifest_sources(root, [1])

    with pytest.raises(SnapshotError, match="来源项必须是对象"):
        snapshot_mod.read_manifest(root)


def test_create_snapshot_rejects_a_file_source_that_is_not_a_file(
    tmp_path: Path,
) -> None:
    """来源声明是 file 但实际是目录: 拒绝, 而不是把一个目录当成一个文件备一份."""
    save = _saved_dir(tmp_path)

    with pytest.raises(SnapshotError, match="存档文件不可用"):
        create_snapshot(
            [SnapshotSource(path=str(save), kind="file", index=0)], tmp_path / "node"
        )

    assert not (tmp_path / "node").exists()


@pytest.mark.blocker
def test_create_snapshot_records_a_child_that_reports_as_a_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """子项是符号链接时只记 link_target, 既不跟随也不复制它的内容.

    本机(Windows 未开开发者模式)无权创建符号链接, 所以用替身钉住这条分支 ——
    它正是"快照不越出用户确认过的存档路径"的实现所在.
    """
    save = _saved_dir(tmp_path)
    linked = save / "slot1.dat"
    outside = Path("C:/outside/secret.dat")
    real_is_symlink = Path.is_symlink
    real_readlink = Path.readlink

    def looks_like_a_link(self: Path) -> bool:
        return True if self == linked else real_is_symlink(self)

    def read_as_a_link(self: Path) -> Path:
        return outside if self == linked else real_readlink(self)

    monkeypatch.setattr(Path, "is_symlink", looks_like_a_link)
    monkeypatch.setattr(Path, "readlink", read_as_a_link)

    result = create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)],
        tmp_path / "node",
    )

    entries = {entry.relative_path: entry for entry in result.entries}
    entry = entries["loc-0/slot1.dat"]
    assert entry.file_kind == "symlink"
    assert entry.link_target == str(outside)
    assert not (result.root / "loc-0" / "slot1.dat").exists()


def test_materialize_rejects_a_file_plan_without_an_origin(tmp_path: Path) -> None:
    """计划里是文件却没有复制来源: 拒绝, 而不是写一个空文件."""
    plan = snapshot_mod._CopyPlan(
        entry=SnapshotEntry(relative_path="loc-0/slot.dat", file_kind="file"),
        origin=None,
    )

    with pytest.raises(SnapshotError, match="缺少复制来源"):
        snapshot_mod._materialize(plan, tmp_path / "out")


def test_materialize_rechecks_the_hash_of_what_it_copied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """复制后按磁盘上的内容复核哈希: 写出来的字节与算出来的不一致就报错."""
    origin = tmp_path / "src.dat"
    origin.write_bytes(b"alpha")
    plan = snapshot_mod._CopyPlan(
        entry=SnapshotEntry(relative_path="loc-0/src.dat", file_kind="file"),
        origin=origin,
    )
    monkeypatch.setattr(snapshot_mod, "sha256_of_file", lambda _path: "0" * 64)

    with pytest.raises(SnapshotError, match="哈希校验失败"):
        snapshot_mod._materialize(plan, tmp_path / "out")
