"""文件快照服务的单元测试.

覆盖"复制 -> 哈希校验 -> 原子提交"的完整链路, 以及三条硬性约束:
不覆盖已有备份、失败不留半成品、可取消。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archive_management.exceptions import ArchiveManagementError, SnapshotError
from archive_management.services.snapshot import (
    MANIFEST_FILENAME,
    SnapshotEntry,
    SnapshotSource,
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
