"""存档路径校验服务的单元测试."""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.services.pathcheck import normalize_path, probe_path

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
