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


def test_is_within_ignores_case_for_drive_paths() -> None:
    """盘符路径不分大小写: 安装目录与候选路径大小写不同也要认得出来.

    Steam 清单里的安装目录常是小写(``d:\\steam\\...``), 候选路径可能保持原样;
    不归一比较就会让"受保护位置"这条保护静默失效。
    """
    assert is_within(Path("D:/Steam/saves"), Path("d:/steam")) is True
    assert is_within(Path("d:/steam"), Path("D:/Steam/saves")) is False
    # POSIX 风格路径保持大小写敏感.
    assert is_within(Path("/opt/Saves"), Path("/opt/saves")) is False


def test_protected_subdirectories_can_be_unprotected(tmp_path: Path) -> None:
    """``protect_subpaths=False``: 只拦受保护位置本身或包含它的目标."""
    install = tmp_path / "Games" / "Demo"
    saves = install / "saves"
    saves.mkdir(parents=True)

    assert dangerous_target_reason(str(saves), protected=(str(install),)) == "protected"
    assert (
        dangerous_target_reason(
            str(saves), protected=(str(install),), protect_subpaths=False
        )
        is None
    )
    assert (
        dangerous_target_reason(
            str(install), protected=(str(install),), protect_subpaths=False
        )
        == "protected"
    )
    assert (
        dangerous_target_reason(
            str(tmp_path), protected=(str(install),), protect_subpaths=False
        )
        == "contains_protected"
    )


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


def test_is_within_ignores_leading_parent_references(tmp_path: Path) -> None:
    """前导 ``..`` 没有可回退的层级: 忽略它而不是报错(词法比较, 不碰文件系统)."""
    assert is_within(Path("..") / "a" / "b", Path("a")) is True
    assert is_within(Path(".."), Path("..")) is True


def test_summarize_path_survives_a_file_that_vanishes_between_calls(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """竞态: ``is_file()`` 说在、紧接着 ``stat()`` 失败 —— 仍要报"1 个文件"而不是崩.

    真实场景是备份前的预览: 统计与用户点确认之间, 存档文件正好被游戏改写/删除。
    用"判定之后就删掉它"来复现, 比数调用次数更稳、也更贴近真实时序。
    """
    target = tmp_path / "save.dat"
    target.write_text("abcd", encoding="utf-8")
    real_is_file = Path.is_file

    def vanish_after_is_file(self: Path) -> bool:
        result = real_is_file(self)
        if self == target and result:
            target.unlink()
        return result

    monkeypatch.setattr(Path, "is_file", vanish_after_is_file)

    summary = summarize_path(str(target))

    assert summary.files == 1
    assert summary.total_size == 0


def test_summarize_path_survives_an_entry_that_vanishes_mid_walk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """遍历目录时某个条目读不出大小: 计数照旧, 只是不计入字节数."""
    root = tmp_path / "loc"
    root.mkdir()
    (root / "keep.dat").write_text("ab", encoding="utf-8")
    (root / "gone.dat").write_text("xyz", encoding="utf-8")
    real_stat = Path.stat
    calls = {"count": 0}

    def flaky_stat(self: Path, **kwargs: bool) -> object:
        if self.name == "gone.dat":
            calls["count"] += 1
            if calls["count"] > 1:
                raise FileNotFoundError(2, "条目在遍历过程中消失了")
        return real_stat(self, **kwargs)

    monkeypatch.setattr(Path, "stat", flaky_stat)

    summary = summarize_path(str(root))

    assert summary.files == 2
    assert summary.total_size == 2


@pytest.mark.blocker  # 危险目标判定是"写回用户目录"之前的最后一道闸
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
