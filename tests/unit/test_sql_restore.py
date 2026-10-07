"""
恢复动作: 预览目标与多余文件、写回并保持当前指针、默认安全点、取消报告。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
)
from archive_management.i18n import tr
from archive_management.ui.models import (
    visible_in_branch_view,
)

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _advance,
    _service_with_save,
)

pytestmark = [
    pytest.mark.backend,
    pytest.mark.database,
    pytest.mark.critical,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("真实 SQLite 后端"),
    pytest.mark.story("备份恢复与删除数据流"),
    # 真实 SQLite + 文件系统 + 调度后端, 按层定义归入 integration.
    pytest.mark.layer("integration"),
]


def test_restore_requires_known_backup(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.run_restore(game_id, "9999")


def test_restore_moves_current_node_and_keeps_branch_label(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    first = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, first.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]

    service.run_restore(game_id, first.backup_id)

    items = {item.backup_id: item for item in service.list_backups(game_id)}
    assert items[first.backup_id].is_current is True
    assert items[branch.backup_id].is_current is False
    # 新备份从当前节点(而非末尾)继续.
    _advance(save, "restored")
    service.run_backup_now(game_id)
    created = [item for item in service.list_backups(game_id) if not item.is_branch]
    assert created[-1].parent_id == first.backup_id


def test_branch_node_carries_branch_name_for_inheritance(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)

    service.run_create_branch(game_id, base.backup_id, "黑棘")

    branch = service.list_backups(game_id)[-1]
    assert branch.branch_name == "黑棘"
    assert branch.title == "黑棘"


def test_preview_restore_reports_targets_and_extra_files(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "notes.txt").write_text("extra", encoding="utf-8")

    plan = service.preview_restore(game_id, node.backup_id)

    assert plan.snapshot_ok is True
    assert plan.file_count == 1
    assert [target.path for target in plan.targets] == [str(save)]
    assert plan.blocked_reason is None
    assert plan.safety_point_available is True


def test_preview_restore_rejects_unknown_backup(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.preview_restore(game_id, "9999")


def test_run_restore_writes_files_and_keeps_current_pointer(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "slot1.dat").write_text("changed", encoding="utf-8")

    message = service.run_restore(game_id, node.backup_id, safety_point=False)

    assert (save / "slot1.dat").read_text(encoding="utf-8") == "progress"
    assert "当前节点" in message
    items = {item.backup_id: item for item in service.list_backups(game_id)}
    assert items[node.backup_id].is_current is True


def test_run_restore_keeps_extra_files(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "notes.txt").write_text("extra", encoding="utf-8")

    message = service.run_restore(game_id, node.backup_id, safety_point=False)

    # 恢复是覆盖而不是镜像: 快照之外的文件保持原样.
    assert (save / "notes.txt").read_text(encoding="utf-8") == "extra"
    assert "1 个文件" in message


def test_run_restore_creates_safety_point_by_default(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    (save / "slot1.dat").write_text("changed", encoding="utf-8")

    service.run_restore(game_id, node.backup_id)

    items = service.list_backups(game_id)
    safety = [item for item in items if item.safety]
    assert [item.display_title for item in safety] == ["恢复前安全点"]
    # 安全点带 safety 标记: 只出现在时间线, 不占分支树的位置.
    assert safety[0].backup_id not in visible_in_branch_view(items)


def test_a_cancelled_restore_reports_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """恢复中途被取消: 提示要说清"原始存档未被修改"; 槽位照样放掉, 当前节点也不动."""
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    backup_id = service.list_backups(game_id)[0].backup_id

    def stop(*_args: object, **_kwargs: object) -> None:
        raise OperationCancelledError("用户按了取消")

    monkeypatch.setattr(service._restore, "restore", stop)

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.run_restore(game_id, backup_id)

    assert str(excinfo.value) == tr("result.restore_canceled")
    assert service._active is None
    assert service.list_backups(game_id)[0].backup_id == backup_id
