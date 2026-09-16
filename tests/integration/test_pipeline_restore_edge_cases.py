"""恢复流程的边界形态: 文件来源、类型变化、符号链接与回滚.

真实数据库 + 真实文件系统 + 真实备份/恢复服务。这里覆盖的都是"备份之后磁盘悄悄
变了"的场景: 存档从文件变成目录、目标位置上冒出同名目录、多出嵌套目录与符号
链接、暂存替换中途失败。约定是**要么完整恢复, 要么回滚到原状并保留可追溯记录**,
绝不能留下半成品或写坏用户数据。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from archive_management.application import restore as restore_mod
from archive_management.application.backup import BackupService
from archive_management.application.restore import RestoreService
from archive_management.domain import BackupNode, Game, PathKind, SaveLocation
from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
    SnapshotError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.snapshot import (
    MANIFEST_FILENAME,
    content_hash_of,
    read_manifest_entries,
)
from helpers import migrated_database

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("恢复用例"),
    pytest.mark.story("恢复边界形态"),
    pytest.mark.layer("integration"),
]


def _idle_provider() -> list[str]:
    """没有任何匹配进程的进程提供者."""
    return []


@dataclass
class _Env:
    """测试环境: 数据库、备份/恢复服务与目标路径."""

    database: Database
    backups: BackupService
    restore: RestoreService
    root: Path
    game_id: int

    def add_location(self, path: Path, *, kind: PathKind = "directory") -> SaveLocation:
        """登记一个存档位置."""
        return SaveLocationRepository(self.database).add(
            SaveLocation(game_id=self.game_id, path=str(path), path_kind=kind)
        )

    def backup(self) -> BackupNode:
        """生成一次备份并返回节点."""
        return self.backups.create_backup(self.game_id, kind="manual")

    def snapshot_root(self, node: BackupNode) -> Path:
        """返回某节点的快照目录."""
        return self.backups.snapshot_root(node)


def _env(tmp_path: Path) -> _Env:
    """构造真实数据库 + 备份/恢复服务(恢复目标由用例登记)."""
    database = migrated_database(tmp_path)
    root = tmp_path / "backups"
    backups = BackupService(database, backup_root=root)
    games = GameRepository(database)
    game = games.add(Game(name="边界游戏"))
    assert game.id is not None
    return _Env(
        database=database,
        backups=backups,
        restore=RestoreService(
            database,
            backup_root=root,
            backups=backups,
            process_provider=_idle_provider,
        ),
        root=root,
        game_id=game.id,
    )


def _node_id(node: BackupNode) -> int:
    """返回备份节点的整型 id."""
    assert node.id is not None
    return node.id


def _patch_manifest(root: Path, mutate: Callable[[dict[str, Any]], None]) -> None:
    """改写快照目录里的清单文件(模拟磁盘上的备份被改动).

    改完条目要重算整体内容哈希, 否则快照会被判为"被篡改"而在预检就被拦下,
    用例就测不到真正想测的恢复分支。
    """
    manifest_path = root / MANIFEST_FILENAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    mutate(raw)
    raw["content_hash"] = content_hash_of(read_manifest_entries(raw))
    manifest_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def _symlink_or_skip(link: Path, target: Path) -> None:
    """建立符号链接; 平台不支持(Windows 无权限)时跳过用例."""
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
        pytest.skip(f"当前环境无法创建符号链接: {exc}")


# --- 文件来源 ---------------------------------------------------------------


def test_file_location_is_restored_as_a_single_file(tmp_path: Path) -> None:
    """存档位置本身是文件时: 备份/恢复都按单文件处理, 内容与哈希一致."""
    env = _env(tmp_path)
    save_file = tmp_path / "profile.sav"
    save_file.write_text("v1", encoding="utf-8")
    env.add_location(save_file, kind="file")
    node = env.backup()

    save_file.write_text("v2-被改坏", encoding="utf-8")
    result = env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert save_file.read_text(encoding="utf-8") == "v1"
    assert result.restored_files == 1
    assert result.skipped_symlinks == ()


def test_cancelled_file_restore_leaves_target_untouched(tmp_path: Path) -> None:
    """取消发生在写入文件之前时: 目标内容保持原样, 也不留下暂存目录."""
    env = _env(tmp_path)
    save_file = tmp_path / "profile.sav"
    save_file.write_text("v1", encoding="utf-8")
    env.add_location(save_file, kind="file")
    node = env.backup()
    save_file.write_text("v2", encoding="utf-8")

    with pytest.raises(OperationCancelledError):
        env.restore.restore(
            env.game_id, _node_id(node), safety_point=False, cancelled=lambda: True
        )

    assert save_file.read_text(encoding="utf-8") == "v2"
    assert not list(tmp_path.glob(".*restore-*"))


# --- 类型变化 ---------------------------------------------------------------


def test_target_kind_mismatch_is_refused_without_touching_disk(
    tmp_path: Path,
) -> None:
    """备份是文件、目标位置上却是目录时: 预检直接拒绝, 不猜用户意图."""
    env = _env(tmp_path)
    save_file = tmp_path / "profile.sav"
    save_file.write_text("v1", encoding="utf-8")
    env.add_location(save_file, kind="file")
    node = env.backup()

    save_file.unlink()
    save_file.mkdir()
    (save_file / "stale.dat").write_text("stale", encoding="utf-8")

    plan = env.restore.plan(env.game_id, _node_id(node))
    assert plan.snapshot_ok is True
    assert [target.problem for target in plan.targets] == ["kind_mismatch"]

    with pytest.raises(ArchiveManagementError, match="无法写入该存档位置"):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert (save_file / "stale.dat").read_text(encoding="utf-8") == "stale"


def test_directory_location_replaced_by_file_is_refused(tmp_path: Path) -> None:
    """备份是目录、目标位置上却是文件时同样拒绝恢复, 保留现场供用户处理."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()

    shutil.rmtree(save_dir)
    save_dir.write_text("我不是目录", encoding="utf-8")

    with pytest.raises(ArchiveManagementError, match="无法写入该存档位置"):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert save_dir.read_text(encoding="utf-8") == "我不是目录"


def test_file_entry_replaces_conflicting_directory_in_target(tmp_path: Path) -> None:
    """快照里的文件条目遇到同名目录时: 清掉旧目录再写文件(不跟随目录内容)."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    (save_dir / "sub").mkdir(parents=True)
    (save_dir / "sub" / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()

    shutil.rmtree(save_dir / "sub" / "slot1.dat", ignore_errors=True)
    (save_dir / "sub" / "slot1.dat").unlink()
    (save_dir / "sub" / "slot1.dat").mkdir()
    (save_dir / "sub" / "slot1.dat" / "junk.dat").write_text("旧内容", encoding="utf-8")

    env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    restored = save_dir / "sub" / "slot1.dat"
    assert restored.is_file()
    assert restored.read_text(encoding="utf-8") == "v1"


def test_directory_entry_replaces_conflicting_file_in_target(tmp_path: Path) -> None:
    """快照里的目录条目遇到同名文件时: 文件被替换为目录, 而不是报错中断."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    (save_dir / "sub").mkdir(parents=True)
    (save_dir / "sub" / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()

    shutil.rmtree(save_dir)
    save_dir.mkdir()
    (save_dir / "sub").write_text("占位文件", encoding="utf-8")

    env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert (save_dir / "sub").is_dir()
    assert (save_dir / "sub" / "slot1.dat").read_text(encoding="utf-8") == "v1"


# --- 符号链接 ---------------------------------------------------------------


def _make_symlink_entry(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """把清单里的第一个文件条目改成指向快照外的符号链接条目."""

    def mutate(raw: dict[str, Any]) -> None:
        entry = next(item for item in raw["entries"] if item["file_kind"] == "file")
        entry["file_kind"] = "symlink"
        entry["link_target"] = str(root / "outside.dat")

    _patch_manifest(root, mutate)


def test_symlink_entry_is_recreated_when_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """符号链接条目按记录的目标重建, 不再复制文件内容."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()
    snapshot_root = env.snapshot_root(node)
    _make_symlink_entry(snapshot_root, monkeypatch)
    created: list[tuple[str, str]] = []

    def fake_symlink(self: Path, target: str, **_kwargs: object) -> None:
        created.append((str(self), str(target)))

    monkeypatch.setattr(Path, "symlink_to", fake_symlink)

    result = env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    # 链接在暂存目录里重建, 目标取自清单记录(不复制内容), 且不算跳过.
    assert len(created) == 1
    link_path, link_target = created[0]
    assert Path(link_path).name == "slot1.dat"
    assert "restore-" in Path(link_path).parent.name
    assert link_target == str(snapshot_root / "outside.dat")
    assert result.skipped_symlinks == ()


def test_symlink_entry_without_permission_is_reported_as_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无权限建立链接(Windows 常见)时记入 skipped, 而不是让整次恢复失败."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()
    _make_symlink_entry(env.snapshot_root(node), monkeypatch)

    def refuse(self: Path, target: str, **_kwargs: object) -> None:
        raise OSError("拒绝创建符号链接")

    monkeypatch.setattr(Path, "symlink_to", refuse)

    result = env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    # 链接条目不会退化成"写入普通文件", 而是如实记为跳过.
    assert result.skipped_symlinks == ("slot1.dat",)
    assert not (save_dir / "slot1.dat").exists()


def test_symlink_entry_without_target_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清单里的链接条目缺少目标时不尝试创建, 直接记为跳过."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()
    snapshot_root = env.snapshot_root(node)

    def mutate(raw: dict[str, Any]) -> None:
        entry = next(item for item in raw["entries"] if item["file_kind"] == "file")
        entry["file_kind"] = "symlink"
        entry["link_target"] = None

    _patch_manifest(snapshot_root, mutate)
    monkeypatch.setattr(
        Path,
        "symlink_to",
        lambda self, target, **_kwargs: pytest.fail("不应尝试创建链接"),
    )

    result = env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert len(result.skipped_symlinks) == 1


# --- 合并现有内容与失败回滚 --------------------------------------------------


def test_existing_nested_content_is_merged_into_restored_directory(
    tmp_path: Path,
) -> None:
    """快照之外的内容(含嵌套目录)在恢复后依然保留: 恢复只补回快照里的部分."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()

    extra = save_dir / "extra" / "nested"
    extra.mkdir(parents=True)
    (extra / "note.txt").write_text("用户自己放的", encoding="utf-8")
    (save_dir / "slot1.dat").write_text("v2", encoding="utf-8")

    env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert (save_dir / "slot1.dat").read_text(encoding="utf-8") == "v1"
    assert (extra / "note.txt").read_text(encoding="utf-8") == "用户自己放的"


def test_failure_while_merging_existing_content_cleans_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """合并现有内容失败时报可理解的错误, 并清掉暂存目录(目标保持原样)."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()
    (save_dir / "extra.dat").write_text("额外内容", encoding="utf-8")

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise OSError("复制被拒绝")

    monkeypatch.setattr(shutil, "copy2", refuse)

    with pytest.raises(SnapshotError, match="复制现有存档失败"):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert (save_dir / "slot1.dat").read_text(encoding="utf-8") == "v1"
    assert (save_dir / "extra.dat").read_text(encoding="utf-8") == "额外内容"
    assert not list(save_dir.parent.glob(".saves.restore-*"))


def test_swap_in_rolls_back_when_replacing_target_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """暂存内容替换失败时: 原内容被改名回去, 不会留下"目标消失"的状态."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()
    (save_dir / "slot1.dat").write_text("v2-恢复前", encoding="utf-8")

    original_move = restore_mod._move
    calls = {"count": 0}

    def flaky_move(source: Path, destination: Path) -> None:
        calls["count"] += 1
        if calls["count"] == 2:  # 第二次: 暂存 -> 目标
            raise OSError("替换失败")
        original_move(source, destination)

    monkeypatch.setattr(restore_mod, "_move", flaky_move)

    with pytest.raises(OSError, match="替换失败"):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert save_dir.is_dir()
    assert (save_dir / "slot1.dat").read_text(encoding="utf-8") == "v2-恢复前"
    assert not list(save_dir.parent.glob(".saves.replaced-*"))


def test_move_falls_back_to_shutil_when_atomic_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同卷改名失败(跨卷/被占用)时回退到 ``shutil.move``, 不能直接放弃恢复."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()
    (save_dir / "slot1.dat").write_text("v2", encoding="utf-8")

    def refuse_replace(self: Path, target: Path | str) -> Path:
        raise OSError("拒绝原子改名")

    monkeypatch.setattr(Path, "replace", refuse_replace)

    env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert (save_dir / "slot1.dat").read_text(encoding="utf-8") == "v1"


def test_entry_outside_source_root_is_rejected_and_staging_cleaned(
    tmp_path: Path,
) -> None:
    """清单条目指到别的来源目录下(被改动过)时拒绝恢复, 并保持目标原样."""
    env = _env(tmp_path)
    save_dir = tmp_path / "saves"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("v1", encoding="utf-8")
    env.add_location(save_dir)
    node = env.backup()
    snapshot_root = env.snapshot_root(node)

    def mutate(raw: dict[str, Any]) -> None:
        entry = next(item for item in raw["entries"] if item["file_kind"] == "file")
        entry["relative_path"] = "loc-9/" + entry["relative_path"].split("/")[-1]

    _patch_manifest(snapshot_root, mutate)
    (save_dir / "slot1.dat").write_text("v2", encoding="utf-8")

    with pytest.raises(SnapshotError):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert (save_dir / "slot1.dat").read_text(encoding="utf-8") == "v2"
    assert not list(save_dir.parent.glob(".saves.restore-*"))


@pytest.fixture(autouse=True)
def _no_editor_backup_files(tmp_path: Path) -> Iterator[None]:
    """确保用例结束后目标目录里没有残留的暂存/回滚目录."""
    yield
    leftovers = [
        path
        for path in tmp_path.rglob(".*")
        if path.is_dir() and ("restore-" in path.name or "replaced-" in path.name)
    ]
    assert leftovers == []
