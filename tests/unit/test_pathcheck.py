"""存档路径校验服务的单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.services.pathcheck import (
    PathSummary,
    dangerous_target_reason,
    is_within,
    is_writable_target,
    normalize_path,
    probe_path,
    summarize_path,
)

pytestmark = [
    pytest.mark.paths,
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("存档路径校验"),
    pytest.mark.story("校验存档路径"),
    pytest.mark.layer("unit"),
]


def test_normalize_path_returns_absolute(tmp_path: Path) -> None:
    target = str(tmp_path / "saves")
    assert normalize_path(target) == str(tmp_path / "saves")


def test_normalize_path_expands_home_dotdot_and_dots(tmp_path: Path) -> None:
    import os

    parent = tmp_path / "a" / "b"
    raw = f"{parent}{os.sep}..{os.sep}b{os.sep}.{os.sep}c"
    assert normalize_path(raw) == str(tmp_path / "a" / "b" / "c")


def test_probe_ok_for_existing_directory(tmp_path: Path) -> None:
    directory = tmp_path / "save-dir"
    directory.mkdir()
    probe = probe_path(str(directory), "directory")
    assert probe.ok is True
    assert probe.reason_code is None
    assert probe.normalized == str(directory)


def test_probe_ok_for_existing_file(tmp_path: Path) -> None:
    target = tmp_path / "save.dat"
    target.write_text("x", encoding="utf-8")
    probe = probe_path(str(target), "file")
    assert probe.ok is True
    assert probe.reason_code is None


def test_probe_flags_missing_path(tmp_path: Path) -> None:
    probe = probe_path(str(tmp_path / "nope"), "directory")
    assert probe.ok is False
    assert probe.reason_code == "missing"
    assert probe.exists is False


def test_probe_flags_wrong_kind(tmp_path: Path) -> None:
    target = tmp_path / "save.dat"
    target.write_text("x", encoding="utf-8")
    probe = probe_path(str(target), "directory")
    assert probe.ok is False
    assert probe.reason_code == "wrong_kind"


# ------------------------------------------------- 汇总与边界判定


def test_summarize_path_counts_entries_and_size(tmp_path: Path) -> None:
    root = tmp_path / "save"
    (root / "nested").mkdir(parents=True)
    (root / "slot1.dat").write_text("abc", encoding="utf-8")
    (root / "nested" / "slot2.dat").write_text("de", encoding="utf-8")

    summary = summarize_path(str(root))

    assert summary.files == 2
    assert summary.directories == 1
    assert summary.entries == 2
    assert summary.total_size == 5


def test_summarize_path_handles_file_and_missing(tmp_path: Path) -> None:
    target = tmp_path / "save.dat"
    target.write_text("abcd", encoding="utf-8")

    assert summarize_path(str(target)).files == 1
    assert summarize_path(str(target)).total_size == 4
    assert summarize_path(str(tmp_path / "nope")) == PathSummary()


def test_is_within_compares_lexically(tmp_path: Path) -> None:
    assert is_within(tmp_path / "a" / "b", tmp_path / "a") is True
    assert is_within(tmp_path / "a", tmp_path / "a") is True
    assert is_within(tmp_path / "a" / ".." / "b", tmp_path / "a") is False
    assert is_within(tmp_path / "a", tmp_path / "a" / "b") is False


def test_dangerous_target_reason_covers_protected_and_roots(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    backup_root.mkdir()

    assert dangerous_target_reason(str(backup_root), protected=(str(backup_root),)) == (
        "protected"
    )
    assert (
        dangerous_target_reason(
            str(backup_root / "nested"), protected=(str(backup_root),)
        )
        == "protected"
    )
    assert (
        dangerous_target_reason(str(tmp_path), protected=(str(backup_root),))
        == "contains_protected"
    )
    assert dangerous_target_reason(str(Path(tmp_path.anchor))) == "drive_root"
    assert dangerous_target_reason("relative/path") == "not_absolute"
    assert dangerous_target_reason(str(tmp_path / "save")) is None


def test_is_writable_target_uses_existing_ancestor(tmp_path: Path) -> None:
    assert is_writable_target(str(tmp_path / "missing" / "deep")) is True
    assert is_writable_target(str(tmp_path)) is True
