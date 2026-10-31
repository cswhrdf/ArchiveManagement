"""快照清单的健壮性与真实文件协作.

真实文件系统 + 真实快照服务, 覆盖"清单被改坏、条目类型变化、深/浅校验差异"
这类只在集成时才成立的组合: 内存里手搓的清单说明不了磁盘上的实际内容。清单
条目来自磁盘上的备份文件, 属于不可信输入, 因此每种非法取值都要被拒绝。

同时验证快照之外的两条真实协作路径: 目录条目缺失与符号链接条目(只记目标,
不复制内容), 以及进度回调抛错时的行为。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from archive_management.exceptions import SnapshotError
from archive_management.services.snapshot import (
    MANIFEST_FILENAME,
    SnapshotSource,
    create_snapshot,
    read_manifest,
    verify_snapshot,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("快照服务"),
    pytest.mark.story("清单健壮性"),
    pytest.mark.layer("integration"),
]

_DEFAULT_FILES = {"slot1.dat": "v1", "sub/slot2.dat": "v2"}


def _make_snapshot(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    """在临时目录里造一份真实快照, 返回快照根目录."""
    source = tmp_path / "save"
    source.mkdir()
    for name, text in (files or _DEFAULT_FILES).items():
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    destination = tmp_path / "backups" / "node-1"
    create_snapshot([SnapshotSource(path=str(source), kind="directory")], destination)
    return destination


def _patch_manifest(root: Path, mutate: Any) -> None:
    """读取磁盘上的清单, 交给 ``mutate`` 改写后再写回."""
    path = root / MANIFEST_FILENAME
    raw = json.loads(path.read_text(encoding="utf-8"))
    mutate(raw)
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def _entry(raw: dict[str, Any], **changes: Any) -> dict[str, Any]:
    """取清单中的第一条文件条目并按需改写字段."""
    entry: dict[str, Any] = next(
        item for item in raw["entries"] if item["file_kind"] == "file"
    )
    entry.update(changes)
    return entry


def test_deleted_directory_is_reported_as_missing(tmp_path: Path) -> None:
    """目录条目在磁盘上消失后, 校验要把该目录报成缺失项."""
    root = _make_snapshot(tmp_path)
    assert verify_snapshot(root).ok is True

    for child in sorted((root / "loc-0" / "sub").iterdir()):
        child.unlink()
    (root / "loc-0" / "sub").rmdir()

    verification = verify_snapshot(root)
    assert verification.ok is False
    assert "loc-0/sub" in verification.missing
    # 目录条目也算"检查过", 不会让 checked 少计.
    assert verification.checked >= 2


def test_deep_flag_controls_hash_verification(tmp_path: Path) -> None:
    """内容被改但清单未变时: 浅校验只查存在性, 深校验必须发现哈希不一致."""
    root = _make_snapshot(tmp_path)
    (root / "loc-0" / "slot1.dat").write_text("被篡改的内容", encoding="utf-8")

    shallow = verify_snapshot(root, deep=False)
    assert shallow.ok is True
    assert shallow.mismatched == ()

    deep = verify_snapshot(root)
    assert deep.ok is False
    assert "loc-0/slot1.dat" in deep.mismatched


@pytest.mark.parametrize("broken_content", ["not json at all", "{broken"])
def test_unreadable_manifest_is_reported(tmp_path: Path, broken_content: str) -> None:
    """清单文件损坏时给出可展示的错误, 而不是抛 JSON 解析异常."""
    root = _make_snapshot(tmp_path)
    (root / MANIFEST_FILENAME).write_text(broken_content, encoding="utf-8")

    with pytest.raises(SnapshotError, match="无法读取快照清单"):
        read_manifest(root)


def test_missing_manifest_is_reported(tmp_path: Path) -> None:
    """缺少清单文件时明确报错, 而不是把空目录当成有效快照."""
    root = _make_snapshot(tmp_path)
    (root / MANIFEST_FILENAME).unlink()

    with pytest.raises(SnapshotError, match="缺少清单文件"):
        read_manifest(root)


def test_non_object_manifest_is_reported(tmp_path: Path) -> None:
    """清单顶层不是对象时拒绝读取."""
    root = _make_snapshot(tmp_path)
    (root / MANIFEST_FILENAME).write_text("[1, 2, 3]", encoding="utf-8")

    with pytest.raises(SnapshotError, match="顶层必须是对象"):
        read_manifest(root)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("size", "12", "size 非法"),
        ("sha256", 5, "sha256 非法"),
        ("link_target", ["not-a-string"], "link_target 非法"),
        ("file_kind", "socket", "未知的清单文件类型"),
    ],
)
def test_invalid_manifest_entry_fields_are_rejected(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    """清单条目字段类型/取值非法时拒绝读取(磁盘清单是不可信输入)."""
    root = _make_snapshot(tmp_path)
    _patch_manifest(root, lambda raw: _entry(raw, **{field: value}))

    with pytest.raises(SnapshotError, match=message):
        read_manifest(root)


@pytest.mark.parametrize(
    "sources",
    [
        None,
        [],
        "loc-0",
        [{"index": 0, "path": "", "kind": "directory"}],
        [{"index": -1, "path": "x", "kind": "directory"}],
        [{"index": 0, "path": "x", "kind": "socket"}],
        [{"index": "0", "path": "x", "kind": "directory"}],
        [
            {"index": 0, "path": "x", "kind": "directory"},
            {"index": 0, "path": "y", "kind": "directory"},
        ],
    ],
)
def test_invalid_manifest_sources_are_rejected(tmp_path: Path, sources: object) -> None:
    """来源列表缺失/类型错误/序号重复时拒绝读取, 恢复才不会写错位置."""
    root = _make_snapshot(tmp_path)
    _patch_manifest(root, lambda raw: raw.__setitem__("sources", sources))

    with pytest.raises(SnapshotError):
        read_manifest(root)


def test_empty_source_sequence_is_refused(tmp_path: Path) -> None:
    """没有任何来源时拒绝创建快照, 而不是产出空快照."""
    with pytest.raises(SnapshotError, match="没有可备份的存档位置"):
        create_snapshot([], tmp_path / "backups" / "node-empty")


def test_negative_source_index_is_refused() -> None:
    """来源序号为负时立即失败(会直接影响快照内的目录名)."""
    with pytest.raises(ValueError, match="来源序号不能为负数"):
        SnapshotSource(path="x", kind="file", index=-1)


def test_known_progress_callback_error_propagates(tmp_path: Path) -> None:
    """回调抛出已知的应用异常时必须向外抛出(取消/磁盘错误要让调用方看到)."""
    source = tmp_path / "save"
    source.mkdir()
    (source / "slot1.dat").write_text("v1", encoding="utf-8")

    def failing(_fraction: float, _message: str) -> None:
        raise SnapshotError("进度回调里的业务失败")

    with pytest.raises(SnapshotError, match="进度回调里的业务失败"):
        create_snapshot(
            [SnapshotSource(path=str(source), kind="directory")],
            tmp_path / "backups" / "node-fail",
            progress=failing,
        )
    # 失败后不留下半成品目录.
    assert not (tmp_path / "backups" / "node-fail").exists()
    assert not list((tmp_path / "backups").glob("*.partial-*"))


def test_unexpected_progress_callback_error_is_swallowed(tmp_path: Path) -> None:
    """回调抛出的意外异常只影响进度显示, 不能中断备份."""
    source = tmp_path / "save"
    source.mkdir()
    (source / "slot1.dat").write_text("v1", encoding="utf-8")

    def failing(_fraction: float, _message: str) -> None:
        raise ValueError("界面回调里的意外错误")

    result = create_snapshot(
        [SnapshotSource(path=str(source), kind="directory")],
        tmp_path / "backups" / "node-ok",
        progress=failing,
    )
    assert result.file_count() == 1


def _symlink_or_skip(link: Path, target: Path) -> None:
    """建立符号链接; 平台不支持(Windows 无权限)时跳过用例."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
        pytest.skip(f"当前环境无法创建符号链接: {exc}")


def _plain_path(raw: str) -> Path:
    r"""去掉 Windows 的扩展长度前缀(``\\?\``), 便于跨平台比较路径."""
    prefix = "\\\\?\\"
    return Path(raw[len(prefix) :] if raw.startswith(prefix) else raw)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\\\\?\\C:\\saves\\slot.dat", "C:\\saves\\slot.dat"),
        ("/home/u/saves/slot.dat", "/home/u/saves/slot.dat"),
    ],
)
def test_plain_path_strips_only_the_windows_prefix(raw: str, expected: str) -> None:
    """只剥掉扩展长度前缀, 普通路径原样返回(Windows 的 readlink 会带前缀)."""
    assert _plain_path(raw) == Path(expected)


def test_symlink_entry_is_recorded_and_verified_without_following(
    tmp_path: Path,
) -> None:
    """符号链接只记目标字符串: 清单里是 symlink 条目, 校验不跟随它."""
    source = tmp_path / "save"
    source.mkdir()
    outside = tmp_path / "outside.dat"
    outside.write_text("secret", encoding="utf-8")
    _symlink_or_skip(source / "linked.dat", outside)

    destination = tmp_path / "backups" / "node-link"
    create_snapshot([SnapshotSource(path=str(source), kind="directory")], destination)

    manifest = read_manifest(destination)
    links = [entry for entry in manifest.entries if entry.file_kind == "symlink"]
    assert len(links) == 1
    # Windows 的 readlink 会带上扩展长度前缀(``\\?\``)且大小写可能与原路径不同,
    # 因此先剥前缀再按平台规则归一化大小写后比较.
    recorded = _plain_path(str(links[0].link_target))
    assert os.path.normcase(str(recorded)) == os.path.normcase(str(outside))
    # 链接指向的文件没有被复制进快照.
    assert not (destination / "loc-0" / "linked.dat").exists()

    verification = verify_snapshot(destination)
    assert verification.ok is True
    assert verification.missing == ()
