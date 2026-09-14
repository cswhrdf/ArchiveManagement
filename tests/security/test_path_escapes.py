"""路径越界、危险目标与符号链接防护.

覆盖三类"不可信路径"输入:

1. 快照清单里的相对路径 —— 备份文件本身也是磁盘数据, 必须当作不可信输入;
2. 写回目标的危险位置判定 —— 盘符根目录、用户主目录、应用备份根目录;
3. 存档目录里的符号链接 —— 不能跟随链接把链接之外的内容复制进快照。

约定: 只对危险位置做**判定**, 不执行写入或删除; 恶意清单都在临时目录里伪造。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from archive_management.application.backup import BackupService
from archive_management.application.restore import RestoreService, _safe_path
from archive_management.domain import SaveLocation
from archive_management.exceptions import ArchiveManagementError, SnapshotError
from archive_management.infrastructure.database import Database
from archive_management.services.pathcheck import dangerous_target_reason
from archive_management.services.snapshot import (
    MANIFEST_FILENAME,
    SnapshotSource,
    content_hash_of,
    create_snapshot,
    read_manifest_entries,
)
from helpers import add_game, make_save_folder, migrated_database
from reporting import SecurityRecorder

pytestmark = [
    pytest.mark.security,
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("路径与越界防护"),
    pytest.mark.story("拒绝越界与危险路径"),
    pytest.mark.layer("security"),
    pytest.mark.timeout(120),
]

_CATEGORY = "path_escape"
# 恶意清单相对路径: 目录穿越、绝对路径、Windows 盘符与 UNC 路径、空值.
_ESCAPE_PATHS = [
    "../outside.dat",
    "a/../../outside.dat",
    "..",
    "/etc/passwd",
    "C:/Windows/System32/drivers/etc/hosts",
    "C:\\Windows\\win.ini",
    "\\\\server\\share\\secret.dat",
    "",
    "   ",
    "loc-0/../../../outside.dat",
]


def _backups(database: Database, root: Path) -> BackupService:
    """构造备份用例(与真实后端同一套依赖)."""
    return BackupService(database, backup_root=root / "backups")


def _restore(database: Database, root: Path, backups: BackupService) -> RestoreService:
    """构造恢复用例, 复用同一个备份服务(与 SqlArchiveService 的组装一致)."""
    return RestoreService(database, backup_root=root / "backups", backups=backups)


def _append_manifest_entry(manifest_path: Path, entry: dict[str, object]) -> None:
    """往清单里追加一个恶意条目, 并重算整体哈希让清单校验能通过.

    重算哈希是为了让用例真正走到"路径拼接"这一层;
    如果连清单校验都过不了, 就测不到越界防护本身。
    """
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["entries"] = [*raw["entries"], entry]
    raw["content_hash"] = content_hash_of(read_manifest_entries(raw))
    manifest_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


@pytest.mark.parametrize("relative", _ESCAPE_PATHS)
def test_manifest_path_escapes_are_refused(
    tmp_path: Path, relative: str, security_recorder: SecurityRecorder
) -> None:
    """清单里的越界相对路径一律拒绝, 不会拼到目标根目录之外."""
    root = tmp_path / "snapshot"
    root.mkdir()
    accepted = True
    try:
        _safe_path(root, relative)
    except SnapshotError:
        accepted = False
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario="快照清单相对路径越界",
        input_summary=repr(relative),
        expected="抛出 SnapshotError, 拒绝拼接越界路径",
        actual="未拒绝, 路径被接受" if accepted else "已拒绝(SnapshotError)",
        blocked=not accepted,
    )


def test_manifest_path_inside_root_is_accepted(tmp_path: Path) -> None:
    """守卫用例: 合法相对路径必须仍然可用, 避免"一律拒绝"式的假安全."""
    root = tmp_path / "snapshot"
    target = _safe_path(root, "loc-0/sub/slot.dat")
    assert target == root / "loc-0" / "sub" / "slot.dat"
    assert _safe_path(root, "loc-0\\sub\\slot.dat") == target


def test_traversal_entry_in_manifest_blocks_restore(
    tmp_path: Path, security_recorder: SecurityRecorder
) -> None:
    """篡改清单加入越界条目后, 恢复被拒绝且快照根之外没有任何写入."""
    database = migrated_database(tmp_path)
    save = make_save_folder(tmp_path, content="state-0")
    game_id = add_game(database, "越界游戏", path=save)
    backups = _backups(database, tmp_path)
    node = backups.create_backup(game_id)
    assert node.id is not None
    snapshot_root = backups.snapshot_root(node)

    outside = tmp_path / "escaped.dat"
    manifest_path = snapshot_root / MANIFEST_FILENAME
    # 相对路径带来源前缀(能通过 loc-N 前缀过滤)但仍然向上穿越: 这是真正危险的形态.
    _append_manifest_entry(
        manifest_path,
        {
            "relative_path": "loc-0/../../escaped.dat",
            "size": 0,
            "sha256": "0" * 64,
            "file_kind": "symlink",
            "link_target": str(outside),
        },
    )

    blocked = False
    try:
        _restore(database, tmp_path, backups).restore(
            game_id, node.id, safety_point=False
        )
    except SnapshotError:
        blocked = True
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario="清单含父目录穿越条目的恢复",
        input_summary="relative_path='loc-0/../../escaped.dat'",
        expected="恢复中止(SnapshotError), 不写出快照根之外的任何文件",
        actual="已拒绝" if blocked else "未拒绝, 恢复继续执行",
        blocked=blocked and not outside.exists(),
    )
    assert not outside.exists()


def test_entry_outside_source_prefix_cannot_write_anywhere(
    tmp_path: Path, security_recorder: SecurityRecorder
) -> None:
    """清单里与 loc-N 无关的条目会被直接忽略, 不可能被用来写文件."""
    database = migrated_database(tmp_path)
    save = make_save_folder(tmp_path, content="state-0")
    game_id = add_game(database, "无关条目", path=save)
    backups = _backups(database, tmp_path)
    node = backups.create_backup(game_id)
    assert node.id is not None
    outside = tmp_path / "unrelated.dat"
    _append_manifest_entry(
        backups.snapshot_root(node) / MANIFEST_FILENAME,
        {
            "relative_path": "../unrelated.dat",
            "size": 0,
            "sha256": "0" * 64,
            "file_kind": "symlink",
            "link_target": str(outside),
        },
    )

    result = _restore(database, tmp_path, backups).restore(
        game_id, node.id, safety_point=False
    )

    assert result.restored_files >= 1
    assert not outside.exists()
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario="清单含来源前缀之外的条目",
        input_summary="relative_path='../unrelated.dat'",
        expected="该条目被忽略(不参与恢复), 快照根之外无写入",
        actual=f"恢复完成 {result.restored_files} 个文件, 外部文件={outside.exists()}",
        blocked=not outside.exists(),
    )


@pytest.mark.parametrize(
    ("label", "target", "protected"),
    [
        ("相对路径", "saves/local", ()),
        ("用户主目录", str(Path.home()), ()),
        ("盘符或根目录", str(Path(Path.home().anchor)), ()),
        ("受保护目录本身", "%BACKUPS%", ("%BACKUPS%",)),
        ("受保护目录的子目录", "%BACKUPS%/game-1", ("%BACKUPS%",)),
        ("包含受保护目录的祖先", "%TEMP%", ("%BACKUPS%",)),
    ],
)
def test_dangerous_targets_are_refused(
    tmp_path: Path,
    label: str,
    target: str,
    protected: tuple[str, ...],
    security_recorder: SecurityRecorder,
) -> None:
    """危险写回/删除目标: 相对路径、用户主目录、受保护目录及其祖孙关系."""
    backups = str(tmp_path / "backups")
    resolved_target = target.replace("%BACKUPS%", backups).replace(
        "%TEMP%", str(tmp_path)
    )
    resolved_protected = tuple(item.replace("%BACKUPS%", backups) for item in protected)
    reason = dangerous_target_reason(resolved_target, protected=resolved_protected)
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario=f"危险目标判定: {label}",
        input_summary=resolved_target,
        expected="返回拒绝原因(非 None)",
        actual=f"reason={reason}",
        blocked=reason is not None,
    )


def test_safe_target_inside_temp_dir_is_allowed(tmp_path: Path) -> None:
    """守卫用例: 普通临时目录必须可写, 否则整个恢复功能会被误杀."""
    assert dangerous_target_reason(str(tmp_path / "save"), protected=()) is None
    assert (
        dangerous_target_reason(str(tmp_path), protected=(str(tmp_path / "b"),))
        is not None
    )


def test_dangerous_location_blocks_restore(
    tmp_path: Path, security_recorder: SecurityRecorder
) -> None:
    """存档位置记录被改成用户主目录后, 恢复在动文件之前就被拦下."""
    database = migrated_database(tmp_path)
    save = make_save_folder(tmp_path, content="state-0")
    game_id = add_game(database, "被篡改的游戏", path=save)
    backups = _backups(database, tmp_path)
    node = backups.create_backup(game_id)
    assert node.id is not None

    # 模拟"位置记录被改成危险目录"(用户误操作或外部程序篡改数据库).
    with database.session() as connection:
        connection.execute(
            "UPDATE save_locations SET path = ? WHERE game_id = ?",
            (str(Path.home()), game_id),
        )

    restore = _restore(database, tmp_path, backups)
    plan = restore.plan(game_id, node.id)
    blocked = False
    try:
        restore.restore(game_id, node.id, safety_point=False)
    except ArchiveManagementError:
        blocked = True
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario="写回目标为用户主目录的恢复",
        input_summary=str(Path.home()),
        expected="预检标记 blocked 且恢复抛出异常",
        actual=f"blocked_targets={len(plan.blocked_targets)} 抛出={'是' if blocked else '否'}",
        blocked=blocked and bool(plan.blocked_targets),
    )


def test_symlinked_file_is_not_copied_into_snapshot(tmp_path: Path) -> None:
    """存档目录内的符号链接只记录目标字符串, 不跟随链接复制内容."""
    secret = tmp_path / "outside-secret.dat"
    secret.write_text("外部敏感内容", encoding="utf-8")
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot.dat").write_text("state-0", encoding="utf-8")
    try:
        (save / "linked.dat").symlink_to(secret)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不允许创建符号链接(需要管理员权限或开发者模式)")

    destination = tmp_path / "snap"
    create_snapshot(
        [SnapshotSource(path=str(save), kind="directory", index=0)], destination
    )

    copied = [
        path
        for path in destination.rglob("*")
        if path.is_file() and "外部敏感内容" in path.read_text(errors="ignore")
    ]
    assert copied == []
    manifest = (destination / MANIFEST_FILENAME).read_text(encoding="utf-8")
    assert str(secret) in manifest  # 只保留链接目标字符串


def test_domain_model_rejects_blank_location_path() -> None:
    """领域模型本身也拒绝空路径, 避免"空位置"进入文件操作流程."""
    with pytest.raises(ValidationError) as excinfo:
        SaveLocation(game_id=1, path="")
    assert "path" in str(excinfo.value)
