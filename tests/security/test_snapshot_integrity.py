"""损坏快照、篡改内容与恢复前置校验.

备份目录是磁盘上的普通文件, 可能被外部程序修改、被同步工具截断, 或者根本
写不完整。这里验证"坏快照不会变成一次错误的恢复":

1. 文件内容被篡改 → 深校验发现哈希不一致;
2. 文件被删除 → 校验发现缺失项;
3. 清单 JSON 损坏 → 读取清单抛 SnapshotError, 预检标记快照不可用;
4. 任一情况下 ``restore`` 都必须在写入任何文件之前拒绝执行。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archive_management.application.backup import BackupService
from archive_management.application.restore import RestorePlan, RestoreService
from archive_management.exceptions import ArchiveManagementError, SnapshotError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import BackupRepository
from archive_management.services.snapshot import (
    MANIFEST_FILENAME,
    read_manifest,
    verify_snapshot,
)
from helpers import add_game, make_save_folder, migrated_database
from reporting import SecurityRecorder

pytestmark = [
    pytest.mark.security,
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("快照完整性防护"),
    pytest.mark.story("损坏备份不得被恢复"),
    pytest.mark.layer("security"),
    pytest.mark.timeout(120),
]

_CATEGORY = "snapshot_integrity"


def _scene(tmp_path: Path) -> tuple[Database, int, int, BackupService, RestoreService]:
    """准备"一个游戏 + 一份完整备份"的场景, 返回数据库与相关服务."""
    database = migrated_database(tmp_path)
    save = make_save_folder(tmp_path, content="state-0")
    game_id = add_game(database, "损坏场景", path=save)
    backups = BackupService(database, backup_root=tmp_path / "backups")
    node = backups.create_backup(game_id)
    assert node.id is not None
    restore = RestoreService(
        database, backup_root=tmp_path / "backups", backups=backups
    )
    return database, game_id, node.id, backups, restore


def _plan(restore: RestoreService, game_id: int, backup_id: int) -> RestorePlan:
    """执行恢复预检(只读, 不会写任何文件)."""
    return restore.plan(game_id, backup_id)


def _snapshot_file(backups: BackupService, database: Database, backup_id: int) -> Path:
    """返回这份备份里第一个数据文件(清单之外的 *.dat)的路径."""
    node = BackupRepository(database).get(backup_id)
    assert node is not None
    root = backups.snapshot_root(node)
    candidates = [path for path in root.rglob("*.dat") if path.is_file()]
    assert candidates
    return sorted(candidates)[0]


def test_tampered_content_is_detected_and_blocks_restore(
    tmp_path: Path, security_recorder: SecurityRecorder
) -> None:
    """篡改快照文件内容: 深校验报告哈希不一致, 恢复被拒绝."""
    database, game_id, backup_id, backups, restore = _scene(tmp_path)
    target = _snapshot_file(backups, database, backup_id)
    target.write_text("被篡改的内容", encoding="utf-8")

    node = BackupRepository(database).get(backup_id)
    assert node is not None
    verification = verify_snapshot(backups.snapshot_root(node))
    plan = _plan(restore, game_id, backup_id)
    blocked = False
    try:
        restore.restore(game_id, backup_id, safety_point=False)
    except (SnapshotError, ArchiveManagementError):
        blocked = True

    assert verification.ok is False
    assert verification.mismatched
    assert plan.snapshot_ok is False
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario="快照内容被篡改后恢复",
        input_summary=str(target),
        expected="深校验发现哈希不一致, 预检不可用且恢复被拒绝",
        actual=f"mismatched={len(verification.mismatched)} 拒绝={'是' if blocked else '否'}",
        blocked=blocked and not plan.snapshot_ok,
    )


def test_missing_snapshot_file_is_detected(
    tmp_path: Path, security_recorder: SecurityRecorder
) -> None:
    """快照文件被删除: 校验报告缺失项, 恢复被拒绝."""
    database, game_id, backup_id, backups, restore = _scene(tmp_path)
    target = _snapshot_file(backups, database, backup_id)
    target.unlink()

    node = BackupRepository(database).get(backup_id)
    assert node is not None
    verification = verify_snapshot(backups.snapshot_root(node))
    plan = _plan(restore, game_id, backup_id)

    assert verification.ok is False
    assert verification.missing
    assert plan.snapshot_ok is False
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario="快照文件缺失后恢复",
        input_summary=str(target),
        expected="校验报告缺失项, 预检标记快照不可用",
        actual=f"missing={len(verification.missing)} snapshot_ok={plan.snapshot_ok}",
        blocked=not plan.snapshot_ok,
    )


def test_corrupt_manifest_is_reported_as_snapshot_error(
    tmp_path: Path, security_recorder: SecurityRecorder
) -> None:
    """清单 JSON 损坏: 读取清单抛 SnapshotError, 预检给出可展示的原因."""
    database, game_id, backup_id, backups, restore = _scene(tmp_path)
    node = BackupRepository(database).get(backup_id)
    assert node is not None
    manifest_path = backups.snapshot_root(node) / MANIFEST_FILENAME
    manifest_path.write_text("{ broken", encoding="utf-8")

    raised = False
    try:
        read_manifest(backups.snapshot_root(node))
    except SnapshotError:
        raised = True
    plan = _plan(restore, game_id, backup_id)

    assert plan.snapshot_ok is False
    assert plan.snapshot_reason
    security_recorder.expect_blocked(
        category=_CATEGORY,
        scenario="清单 JSON 损坏",
        input_summary=str(manifest_path),
        expected="read_manifest 抛 SnapshotError, 预检 snapshot_ok=False",
        actual=f"抛出={'是' if raised else '否'}, reason={plan.snapshot_reason!r}",
        blocked=raised and not plan.snapshot_ok,
    )


def test_manifest_with_unknown_kind_is_rejected(tmp_path: Path) -> None:
    """清单里的条目类型白名单之外的取值必须被拒绝."""
    database, _game_id, backup_id, backups, _restore = _scene(tmp_path)
    node = BackupRepository(database).get(backup_id)
    assert node is not None
    manifest_path = backups.snapshot_root(node) / MANIFEST_FILENAME
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    raw["entries"][0]["file_kind"] = "executable"
    manifest_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(SnapshotError):
        read_manifest(backups.snapshot_root(node))
