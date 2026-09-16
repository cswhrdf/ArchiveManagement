"""路径汇总与词法比较的边界情形.

``summarize_path`` 会被"主页/发现页"用于估算删除影响, 因此它对符号链接、不可读
目录、符号链接子项的处理必须是"宁可少算也不越界": 不跟随链接、读不了的子项跳过
而不是让整次统计失败。``is_within`` 是纯词法比较, 不访问文件系统, 所以 ``..``/``.``
必须自己处理干净。
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from archive_management.services.pathcheck import (
    PathProbe,
    PathSummary,
    dangerous_target_reason,
    is_within,
    summarize_path,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("路径校验"),
    pytest.mark.story("路径统计与比较"),
    pytest.mark.layer("unit"),
]


def _symlink_or_skip(link: Path, target: Path) -> None:
    """建立符号链接; 平台不支持(Windows 无权限)时跳过用例."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
        pytest.skip(f"当前环境无法创建符号链接: {exc}")


def test_summarize_symlink_counts_as_link_only(tmp_path: Path) -> None:
    """直接统计符号链接时不跟随: 只记 1 条链接, 不把目标内容算进来."""
    target = tmp_path / "real"
    target.mkdir()
    (target / "big.dat").write_bytes(b"x" * 32)
    link = tmp_path / "link"
    _symlink_or_skip(link, target)

    assert summarize_path(str(link)) == PathSummary(symlinks=1)


def test_summarize_skips_symlink_children(tmp_path: Path) -> None:
    """目录内的链接子项只计入链接数, 不会递归进目标目录(避免越界与死循环)."""
    root = tmp_path / "root"
    root.mkdir()
    (root / "slot.dat").write_text("v1", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.dat").write_text("secret", encoding="utf-8")
    _symlink_or_skip(root / "link", outside)

    summary = summarize_path(str(root))

    assert summary.files == 1
    assert summary.symlinks == 1
    assert summary.directories == 0
    assert summary.entries == 2


def test_summarize_file_without_readable_stat_is_still_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件在"判定存在"之后消失时仍然要计数, 只是不累加字节数.

    这里模拟"检查与取值之间文件被删掉"的真实竞态: 第一次 ``stat()`` 让
    ``is_file()`` 通过, 第二次取值时抛文件不存在(仍属 ``OSError``)。
    """
    target = tmp_path / "slot.dat"
    target.write_text("v1", encoding="utf-8")
    original = Path.stat
    calls = {"count": 0}

    def flaky_stat(self: Path, **kwargs: object) -> os.stat_result:
        if self == target and not kwargs:
            calls["count"] += 1
            if calls["count"] > 1:
                raise FileNotFoundError("统计失败")
        return original(self, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "stat", flaky_stat)

    assert summarize_path(str(target)) == PathSummary(files=1)


def test_summarize_unreadable_directory_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录读不了时跳过它的子项, 而不是让整次统计抛错."""
    blocked = tmp_path / "blocked"
    blocked.mkdir()
    (blocked / "slot.dat").write_text("v1", encoding="utf-8")
    readable = tmp_path / "readable"
    readable.mkdir()
    (readable / "slot.dat").write_text("v1", encoding="utf-8")
    original = Path.iterdir

    def failing_iterdir(self: Path) -> Iterator[Path]:
        if self == blocked:
            raise PermissionError("拒绝访问")
        return original(self)

    monkeypatch.setattr(Path, "iterdir", failing_iterdir)

    summary = summarize_path(str(tmp_path))

    assert summary.files == 1
    assert summary.directories == 2


def test_probe_reason_code_covers_unreadable() -> None:
    """不可读路径要给出专门的 reason_code, 便于界面区分"缺失"与"权限不足"."""
    probe = PathProbe(
        normalized="x",
        exists=True,
        is_dir=True,
        is_file=False,
        readable=False,
        kind="directory",
    )

    assert probe.reason_code == "unreadable"
    assert probe.ok is False
    assert probe.matches_kind is True


@pytest.mark.parametrize(
    ("child", "parent", "expected"),
    [
        ("/data/saves/../saves/slot", "/data/saves", True),
        ("/data/./saves", "/data/saves", True),
        ("/data/other", "/data/saves", False),
        ("/data", "/data/saves", False),
        ("/data/saves", "/data/saves", True),
    ],
)
def test_is_within_is_purely_lexical(child: str, parent: str, expected: bool) -> None:
    """词法比较要自己处理 ``..`` 与 ``.``, 并且不因为路径不存在而改变结论."""
    assert is_within(Path(child), Path(parent)) is expected


def test_dangerous_target_reason_ignores_blank_protected_entries(
    tmp_path: Path,
) -> None:
    """受保护列表里的纯空白条目跳过, 不能把正常路径判成受保护."""
    safe = tmp_path / "saves"

    assert dangerous_target_reason(str(safe), protected=("   ",)) is None
