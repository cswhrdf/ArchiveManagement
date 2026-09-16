"""演示后端的维护动作: 监控目录、候选与删除计划.

演示后端是 GUI 的默认数据源, 因此这里的"修路径/忽略/导入/删除"分支必须和真实
后端保持同样的用户可见行为: 空白路径拒绝、重复目录拒绝、删除后当前节点要落到
仍然存在的备份上。全部为内存数据, 不触碰磁盘。
"""

from __future__ import annotations

import pytest

from archive_management.exceptions import ArchiveManagementError
from archive_management.ui.demo_backend import DemoArchiveService

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("演示数据后端"),
    pytest.mark.story("维护动作"),
    pytest.mark.layer("unit"),
]


@pytest.fixture
def service() -> DemoArchiveService:
    """不模拟耗时的演示后端."""
    return DemoArchiveService(delay=0)


def test_update_monitored_directory_changes_path_and_note(
    service: DemoArchiveService,
) -> None:
    """改路径与改备注都能生效, 且不会顶掉另一个字段."""
    original = service.list_monitored_directories()[0]

    updated = service.update_monitored_directory(
        original.directory_id, path="D:/Games/Library", note="新备注"
    )

    assert updated.path == "D:/Games/Library"
    assert updated.note == "新备注"
    same = service.list_monitored_directories()[0]
    assert (same.path, same.note) == ("D:/Games/Library", "新备注")


def test_update_monitored_directory_rejects_blank_path(
    service: DemoArchiveService,
) -> None:
    """空白路径会让监控扫描失效, 必须直接拒绝."""
    original = service.list_monitored_directories()[0]

    with pytest.raises(ArchiveManagementError):
        service.update_monitored_directory(original.directory_id, path="   ")


def test_update_monitored_directory_rejects_duplicates(
    service: DemoArchiveService,
) -> None:
    """把路径改成另一个已监控目录(忽略大小写与结尾分隔符)时拒绝."""
    directories = service.list_monitored_directories()
    first, second = directories[0], directories[1]

    with pytest.raises(ArchiveManagementError):
        service.update_monitored_directory(
            first.directory_id, path=second.path.rstrip("\\/").upper() + "/"
        )


def test_update_monitored_directory_requires_known_id(
    service: DemoArchiveService,
) -> None:
    """未知 id 抛出可展示错误, 而不是静默新建一条."""
    with pytest.raises(ArchiveManagementError):
        service.update_monitored_directory("dir-unknown", note="x")


def test_relocate_candidate_requires_a_path(service: DemoArchiveService) -> None:
    """修正候选路径时空白值无效; 成功时顺带把健康状态改回正常."""
    candidate = service.list_candidates()[0]

    with pytest.raises(ArchiveManagementError):
        service.relocate_candidate(candidate.candidate_id, "   ")

    moved = service.relocate_candidate(candidate.candidate_id, "D:/Games/Fixed")
    assert moved.install_dir == "D:/Games/Fixed"
    assert moved.health == "ok"


@pytest.mark.parametrize(
    "install_dir",
    ["D:/Games/Game/bin", "D:\\Steam\\SteamApps\\Game", "game-only"],
)
def test_add_candidate_as_monitored_uses_parent_directory(
    service: DemoArchiveService, install_dir: str
) -> None:
    """把候选的上一层目录加入监控; 路径只有一段时退化为原路径."""
    candidate = service.list_candidates()[0]
    service.relocate_candidate(candidate.candidate_id, install_dir)

    added = service.add_candidate_as_monitored(candidate.candidate_id)

    assert added.path in {
        install_dir.rsplit("\\", 1)[0],
        install_dir.rsplit("/", 1)[0],
    } | {install_dir}
    assert added.note == candidate.name


def test_candidate_ignore_and_import_roundtrip(service: DemoArchiveService) -> None:
    """忽略后可恢复为待处理; 已导入的候选不能重复导入."""
    candidate = service.list_candidates()[0]

    ignored = service.set_candidate_ignored(candidate.candidate_id, True)
    assert ignored.status == "ignored"
    restored = service.set_candidate_ignored(candidate.candidate_id, False)
    assert restored.status == "new"

    service.import_candidate(candidate.candidate_id, name="导入后的名字")
    assert service.list_candidates(status="imported")


def test_scan_candidates_reports_directory_state(
    service: DemoArchiveService,
) -> None:
    """扫描摘要要区分"已启用/未启用"的监控目录与不可用候选."""
    directory = service.list_monitored_directories()[0]
    service.set_monitored_enabled(directory.directory_id, False)

    summary = service.scan_candidates()

    assert summary.monitored == len(service.list_monitored_directories())
    assert summary.active <= summary.monitored
    assert summary.total >= summary.linked
    assert summary.unusable >= 0


def test_delete_backups_keeps_current_pointer_on_remaining_node(
    service: DemoArchiveService,
) -> None:
    """逐个删除备份: 每次删除都要给出结果文案, 且当前节点始终指向仍存在的备份.

    删除分支根节点会连带子分支(CASCADE), 删除中间节点则上移子节点(SHIFT),
    因此这里只断言"数量严格减少 + 当前节点不悬空", 不假设具体树形。
    """
    game_id = "outer-wilds"
    remaining = [item.backup_id for item in service.list_backups(game_id)]
    assert len(remaining) > 1

    while remaining:
        before = {item.backup_id for item in service.list_backups(game_id)}
        removed = remaining.pop(0)
        if removed not in before:
            continue

        message = service.run_delete_backup(game_id, removed)

        assert message
        left = service.list_backups(game_id)
        assert {item.backup_id for item in left} < before
        assert removed not in {item.backup_id for item in left}
        if left:
            assert any(item.is_current for item in left)


def test_delete_unknown_backup_is_reported(service: DemoArchiveService) -> None:
    """删除不存在的备份时给出可展示错误, 而不是让树结构错乱."""
    with pytest.raises(ArchiveManagementError):
        service.run_delete_backup("outer-wilds", "backup-unknown")
