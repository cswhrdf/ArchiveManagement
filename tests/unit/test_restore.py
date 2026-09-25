"""恢复用例的单元测试.

覆盖预检(快照完整性、目标路径、游戏进程)、安全点、暂存与原子替换、清单
越界防护与操作日志。全部使用临时目录, 不依赖显示环境。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from archive_management.application.backup import BackupService
from archive_management.application.restore import (
    PROBLEM_KIND_MISMATCH,
    PROBLEM_UNWRITABLE,
    WARN_MORE_LOCATIONS,
    WARN_RECORDED_PATH,
    RestoreService,
    _safe_path,
    _target_problem,
)
from archive_management.domain import BackupNode, Game, NodeKind, SaveLocation
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
from archive_management.services.processes import ProcessNameProvider
from archive_management.services.snapshot import MANIFEST_FILENAME, SnapshotEntry

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("恢复用例"),
    pytest.mark.story("写回原始存档"),
    # 真实数据库 + 文件系统 + 备份/恢复服务协作, 按层定义归入 integration.
    pytest.mark.layer("integration"),
]


@dataclass(frozen=True)
class _Env:
    """测试环境: 数据库、用例、游戏与路径."""

    database: Database
    backups: BackupService
    restore: RestoreService
    games: GameRepository
    locations: SaveLocationRepository
    game_id: int
    save: Path
    root: Path

    def backup(self, *, kind: NodeKind = "manual") -> BackupNode:
        """生成一次备份并返回节点."""
        return self.backups.create_backup(self.game_id, kind=kind)


def _idle_provider() -> list[str]:
    """默认的进程提供者: 没有任何匹配进程."""
    return ["explorer.exe", "svn.exe"]


def _setup(
    tmp_path: Path,
    *,
    files: dict[str, str] | None = None,
    process_provider: ProcessNameProvider | None = None,
) -> _Env:
    """构造带存档目录与仓库的环境(备份由用例按需创建)."""
    save = tmp_path / "save"
    save.mkdir()
    for name, text in (files or {"slot1.dat": "v1"}).items():
        target = save / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    database = Database(tmp_path / "app.db")
    database.migrate()
    root = tmp_path / "backups"
    backups = BackupService(database, backup_root=root)
    games = GameRepository(database)
    locations = SaveLocationRepository(database)
    restore = RestoreService(
        database,
        backup_root=root,
        backups=backups,
        process_provider=process_provider or _idle_provider,
    )
    game = games.add(Game(name="Demo"))
    assert game.id is not None
    locations.add(SaveLocation(game_id=game.id, path=str(save), path_kind="directory"))
    return _Env(
        database=database,
        backups=backups,
        restore=restore,
        games=games,
        locations=locations,
        game_id=game.id,
        save=save,
        root=root,
    )


def _node_id(node: BackupNode) -> int:
    """返回备份节点的整型 id."""
    assert node.id is not None
    return node.id


def _slot(save: Path, name: str = "slot1.dat") -> str:
    """读取存档文件内容."""
    return (save / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ 预检


def test_plan_reports_snapshot_size_and_targets(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    (env.save / "notes.txt").write_text("extra", encoding="utf-8")

    plan = env.restore.plan(env.game_id, _node_id(node))

    assert plan.snapshot_ok is True
    assert plan.snapshot_reason is None
    assert plan.file_count == 1
    assert plan.targets[0].path == str(env.save)
    assert plan.targets[0].blocked is False
    assert plan.process.checked is True
    assert plan.process.running is False
    assert plan.safety_point_available is True
    assert plan.blocked_reason is None
    assert plan.blocked_targets == ()


def test_plan_marks_snapshot_invalid_when_content_changed(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    snapshot = env.backups.snapshot_root(node)
    (snapshot / "loc-0" / "slot1.dat").write_text("tampered", encoding="utf-8")

    plan = env.restore.plan(env.game_id, _node_id(node))

    assert plan.snapshot_ok is False
    assert plan.snapshot_reason is not None
    with pytest.raises(SnapshotError):
        env.restore.restore(env.game_id, _node_id(node))


@pytest.mark.blocker  # 允许写回备份根就等于让备份被自己的恢复流程改掉
def test_plan_blocks_target_inside_backup_root(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    # 把备份根目录里的子目录当成存档位置: 快照会记录它, 恢复必须拦住写回.
    inside = env.root / "nested"
    inside.mkdir(parents=True)
    (inside / "data.txt").write_text("x", encoding="utf-8")
    env.locations.add(
        SaveLocation(game_id=env.game_id, path=str(inside), path_kind="directory")
    )
    node = env.backup()

    plan = env.restore.plan(env.game_id, _node_id(node))

    assert plan.blocked_reason == "protected"
    assert [target.problem for target in plan.targets] == [None, "protected"]
    with pytest.raises(ArchiveManagementError):
        env.restore.restore(env.game_id, _node_id(node))


def test_plan_warns_when_more_locations_than_snapshot(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    extra = tmp_path / "save2"
    extra.mkdir()
    env.locations.add(
        SaveLocation(game_id=env.game_id, path=str(extra), path_kind="directory")
    )

    plan = env.restore.plan(env.game_id, _node_id(node))

    assert WARN_MORE_LOCATIONS in plan.warnings
    assert WARN_RECORDED_PATH not in plan.warnings


def test_plan_uses_recorded_path_when_location_removed(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    location = env.locations.list_for_game(env.game_id)[0]
    env.locations.delete(location.id or 0)

    plan = env.restore.plan(env.game_id, _node_id(node))

    assert plan.targets[0].path == str(env.save)
    assert WARN_RECORDED_PATH in plan.warnings


def test_target_problem_codes_cover_kind_and_permission(tmp_path: Path) -> None:
    backup_root = tmp_path / "backups"
    as_file = tmp_path / "save.dat"
    as_file.write_text("x", encoding="utf-8")

    assert (
        _target_problem(str(as_file), "directory", True, backup_root)
        == PROBLEM_KIND_MISMATCH
    )
    assert (
        _target_problem(str(tmp_path / "missing"), "directory", False, backup_root)
        == PROBLEM_UNWRITABLE
    )
    assert (
        _target_problem(str(backup_root), "directory", True, backup_root) == "protected"
    )


@pytest.mark.blocker  # 清单是外部输入: 越界路径必须写成不可达而不是试着写
def test_safe_path_rejects_traversal_and_absolute_entries(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()

    assert _safe_path(root, "a/b.txt") == root / "a" / "b.txt"
    for invalid in ("../outside.txt", "/etc/passwd", "", "a/../../b.txt"):
        with pytest.raises(SnapshotError):
            _safe_path(root, invalid)


# ------------------------------------------------------------------ 恢复


def test_restore_keeps_extra_files(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    (env.save / "slot1.dat").write_text("changed", encoding="utf-8")
    (env.save / "notes.txt").write_text("extra", encoding="utf-8")

    result = env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert _slot(env.save) == "v1"
    assert (env.save / "notes.txt").read_text(encoding="utf-8") == "extra"
    assert result.restored_files == 1
    assert result.safety_point_id is None
    # 替换过程不留中间目录(旧内容改名让位后即时丢弃).
    assert not [
        item.name for item in env.save.parent.iterdir() if item.name.startswith(".")
    ]


def test_restore_moves_deleted_files_back_and_removes_stale_ones(
    tmp_path: Path,
) -> None:
    env = _setup(tmp_path, files={"saves/slot1.dat": "v1"})
    node = env.backup()
    stale = env.save / "saves" / "stale.dat"
    stale.write_text("stale", encoding="utf-8")
    (env.save / "saves" / "slot1.dat").write_text("changed", encoding="utf-8")

    env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert _slot(env.save, "saves/slot1.dat") == "v1"
    # 快照之外的文件保留: 恢复是"覆盖"而不是"镜像".
    assert stale.read_text(encoding="utf-8") == "stale"


def test_restore_recreates_deleted_files_and_directories(tmp_path: Path) -> None:
    env = _setup(tmp_path, files={"saves/slot1.dat": "v1"})
    node = env.backup()
    (env.save / "saves" / "slot1.dat").unlink()
    (env.save / "saves").rmdir()

    env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert _slot(env.save, "saves/slot1.dat") == "v1"


def test_restore_skips_safety_point_when_content_unchanged(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """存档与当前节点一致时无需安全点: 恢复照常进行, 但不新增节点."""
    env = _setup(tmp_path)
    node = env.backup()

    result = env.restore.restore(env.game_id, _node_id(node))

    assert result.safety_point_id is None
    assert len(env.backups.list_nodes(env.game_id)) == 1
    assert any("restore.safety_skipped" in line for line in audit_log)
    assert _slot(env.save) == "v1"


def test_restore_creates_safety_point_with_given_title(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    (env.save / "slot1.dat").write_text("changed", encoding="utf-8")

    result = env.restore.restore(
        env.game_id,
        _node_id(node),
        safety_point=True,
        safety_title="安全点",
        safety_note="恢复前自动创建",
    )

    assert result.safety_point_id is not None
    point = env.backups.get(result.safety_point_id)
    assert point is not None
    assert point.title == "安全点"
    # 安全点节点带 is_safety 标记: 只出现在时间线, 不占分支树的位置.
    assert point.is_safety is True
    snapshot = env.backups.snapshot_root(point)
    assert (snapshot / "loc-0" / "slot1.dat").read_text(encoding="utf-8") == "changed"


@pytest.mark.blocker  # 游戏运行中覆盖存档是真正的数据损坏场景
def test_restore_requires_force_when_game_process_running(tmp_path: Path) -> None:
    # 游戏名是 Demo, 候选进程名来自游戏名与存档目录名.
    env = _setup(tmp_path, process_provider=lambda: ["DemoGame.exe", "explorer.exe"])
    node = env.backup()
    (env.save / "slot1.dat").write_text("changed", encoding="utf-8")

    plan = env.restore.plan(env.game_id, _node_id(node))
    assert plan.process.running is True
    assert plan.process.matches == ("DemoGame.exe",)
    with pytest.raises(ArchiveManagementError):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    result = env.restore.restore(
        env.game_id, _node_id(node), safety_point=False, force=True
    )
    assert result.restored_files == 1
    assert _slot(env.save) == "v1"


def test_restore_records_operation_with_summary(
    tmp_path: Path, audit_log: list[str]
) -> None:
    env = _setup(tmp_path)
    node = env.backup()

    env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert any(
        message.startswith("restore.start ")
        and f"backup_id={_node_id(node)}" in message
        for message in audit_log
    )
    assert any(
        message.startswith("restore.succeeded ") and "files=1" in message
        for message in audit_log
    )


def test_restore_rejects_unknown_backup(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    with pytest.raises(ArchiveManagementError):
        env.restore.restore(env.game_id, 9999)


def test_restore_cancel_before_writing_keeps_original_files(
    tmp_path: Path, audit_log: list[str]
) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    (env.save / "slot1.dat").write_text("changed", encoding="utf-8")

    with pytest.raises(OperationCancelledError):
        env.restore.restore(
            env.game_id,
            _node_id(node),
            safety_point=False,
            cancelled=lambda: True,
        )

    assert _slot(env.save) == "changed"
    assert any(message.startswith("restore.cancel ") for message in audit_log)


def test_restore_reports_progress_fraction(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    seen: list[float] = []

    def progress(fraction: float, _message: str) -> None:
        seen.append(fraction)

    env.restore.restore(
        env.game_id, _node_id(node), safety_point=False, progress=progress
    )

    assert seen
    assert seen[-1] == 1.0
    assert all(0.0 <= value <= 1.0 for value in seen)


@pytest.mark.blocker  # 篡改清单是攻击面: 拒绝恢复比"尽力而为"重要
def test_restore_rejects_manipulated_manifest(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()
    manifest_path = env.backups.snapshot_root(node) / MANIFEST_FILENAME
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["entries"].append(
        {
            "relative_path": "loc-0/../../escaped.txt",
            "size": 1,
            "sha256": "0" * 64,
            "file_kind": "file",
            "link_target": None,
        }
    )
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(SnapshotError):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert not (tmp_path / "escaped.txt").exists()
    assert _slot(env.save) == "v1"


def test_restore_wraps_callback_errors_without_failing(tmp_path: Path) -> None:
    env = _setup(tmp_path)
    node = env.backup()

    def broken(_fraction: float, _message: str) -> None:
        raise RuntimeError("进度回调不应影响恢复")

    env.restore.restore(
        env.game_id, _node_id(node), safety_point=False, progress=broken
    )

    assert _slot(env.save) == "v1"


# --------------------------------------------------- 边界与清理(分支覆盖)


def test_plan_marks_a_missing_manifest_as_unusable(tmp_path: Path) -> None:
    """清单读不出来时预检照常返回, 只是快照被判不可用且没有任何写回目标."""
    env = _setup(tmp_path)
    node = env.backup()
    (env.backups.snapshot_root(node) / MANIFEST_FILENAME).unlink()

    plan = env.restore.plan(env.game_id, _node_id(node))

    assert plan.snapshot_ok is False
    assert plan.snapshot_reason
    assert plan.targets == ()
    with pytest.raises(SnapshotError):
        env.restore.restore(env.game_id, _node_id(node), safety_point=False)


def test_restore_recreates_a_target_directory_that_disappeared(tmp_path: Path) -> None:
    """存档目录被整个删掉时恢复负责建回来(写回走"目标还不存在"那条路)."""
    import shutil

    env = _setup(tmp_path)
    node = env.backup()
    shutil.rmtree(env.save)
    assert not env.save.exists()

    result = env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert result.restored_files == 1
    assert _slot(env.save) == "v1"


def test_helper_relative_of_keeps_foreign_entries_untouched() -> None:
    """不属于该来源目录的清单条目原样返回(前缀不符时不做任何切割)."""
    from archive_management.application.restore import _relative_of

    assert _relative_of("loc-0/slot1.dat", 0) == "slot1.dat"
    assert _relative_of("loc-1/slot1.dat", 0) == "loc-1/slot1.dat"


def test_helper_target_for_reports_an_unknown_source_index(tmp_path: Path) -> None:
    """按来源序号找不到目标时返回 None(不抛错, 也不误用别的目标)."""
    from archive_management.application.restore import _target_for

    env = _setup(tmp_path)
    node = env.backup()
    plan = env.restore.plan(env.game_id, _node_id(node))

    assert _target_for(plan, plan.targets[0].index) is plan.targets[0]
    assert _target_for(plan, 99) is None


def test_helper_target_problem_accepts_a_path_that_does_not_exist_yet(
    tmp_path: Path,
) -> None:
    """目标还不存在不算问题: 恢复会自己创建它(预检只拦危险/不可写/类型不符)."""
    target = tmp_path / "not-yet"

    assert _target_problem(str(target), "directory", True, tmp_path / "backups") is None
    assert (
        _target_problem(str(target), "directory", False, tmp_path / "backups")
        == PROBLEM_UNWRITABLE
    )


def _manifest_entries(env: _Env, node: BackupNode) -> tuple[SnapshotEntry, ...]:
    """读取真实快照清单里的条目(用于直接驱动暂存阶段的私有函数)."""
    from archive_management.services.snapshot import read_manifest

    return read_manifest(env.backups.snapshot_root(node)).entries


def test_helper_stage_file_requires_exactly_one_file(tmp_path: Path) -> None:
    """文件来源的清单里不是恰好一个文件时直接取消(不写半个文件)."""
    from archive_management.application.restore import _stage_file

    with pytest.raises(SnapshotError, match="清单异常"):
        _stage_file(
            entries=(),
            snapshot_root=tmp_path,
            staging=tmp_path / "stage",
            cancelled=None,
        )


def test_helper_stage_file_and_directory_stop_when_already_cancelled(
    tmp_path: Path,
) -> None:
    """开工前就被取消时立刻报取消: 文件来源与目录来源两条路各拦一次."""
    from archive_management.application.restore import _stage_directory, _stage_file

    env = _setup(tmp_path)
    node = env.backup()
    entries = _manifest_entries(env, node)

    def cancelled() -> bool:
        return True

    with pytest.raises(OperationCancelledError):
        _stage_file(
            entries=entries,
            snapshot_root=tmp_path,
            staging=tmp_path / "stage-file",
            cancelled=cancelled,
        )
    with pytest.raises(OperationCancelledError):
        _stage_directory(
            entries=entries,
            source_index=0,
            snapshot_root=tmp_path,
            staging=tmp_path / "stage-dir",
            progress=None,
            cancelled=cancelled,
        )
    assert not (tmp_path / "stage-file").exists()
    assert not (tmp_path / "stage-dir").exists()


def test_helper_write_file_rejects_content_that_does_not_match_the_hash(
    tmp_path: Path,
) -> None:
    """复制完还要复核哈希: 对不上就按快照损坏处理, 不把错的数据写回存档."""
    from archive_management.application.restore import _write_file

    origin = tmp_path / "slot1.dat"
    origin.write_text("v1", encoding="utf-8")

    with pytest.raises(SnapshotError, match="哈希不一致"):
        _write_file(origin, tmp_path / "copy.dat", "0" * 64)


def test_helper_discard_removes_files_directories_and_is_quiet(
    tmp_path: Path,
) -> None:
    """暂存清理三种情况: 空路径静默、文件删除、目录整棵删掉."""
    from archive_management.application.restore import _discard

    staging = tmp_path / "stage"
    (staging / "inner").mkdir(parents=True)
    (staging / "inner" / "slot1.dat").write_text("v1", encoding="utf-8")
    solo = tmp_path / "solo.dat"
    solo.write_text("v1", encoding="utf-8")

    _discard(tmp_path / "never-existed")
    _discard(solo)
    _discard(staging)

    assert not solo.exists()
    assert not staging.exists()


def test_helper_safe_path_keeps_lexical_guard_as_a_second_line(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """词法检查之外还有一道 is_within: 归一化后仍越界就拒绝(纵深防御)."""
    import archive_management.application.restore as restore_mod

    monkeypatch.setattr(restore_mod, "is_within", lambda candidate, root: False)

    with pytest.raises(SnapshotError, match="越界"):
        _safe_path(tmp_path, "loc-0/slot1.dat")


def test_helper_copy_tree_recreates_existing_symlinks(tmp_path: Path) -> None:
    """已有内容里的符号链接按链接重建, 不跟随复制(否则会多留一份真实数据)."""
    from archive_management.application.restore import _copy_tree

    existing = tmp_path / "save"
    existing.mkdir()
    (existing / "slot1.dat").write_text("v1", encoding="utf-8")
    link = existing / "linked.dat"
    try:
        link.symlink_to(existing / "slot1.dat")
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接")
    staging = tmp_path / "stage"
    staging.mkdir()

    _copy_tree(existing, staging)

    assert (staging / "linked.dat").is_symlink()
    assert (staging / "slot1.dat").read_text(encoding="utf-8") == "v1"


def test_restore_replaces_a_directory_with_a_file_of_the_same_name(
    tmp_path: Path,
) -> None:
    """类型变了(快照里是文件, 存档目录里成了同名目录)时先清掉旧目录再写."""
    env = _setup(tmp_path, files={"slot1.dat": "v1"})
    node = env.backup()
    (env.save / "slot1.dat").unlink()
    (env.save / "slot1.dat").mkdir()

    result = env.restore.restore(env.game_id, _node_id(node), safety_point=False)

    assert result.restored_files == 1
    assert _slot(env.save) == "v1"
