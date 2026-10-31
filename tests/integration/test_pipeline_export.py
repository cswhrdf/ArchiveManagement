"""导出链路的集成测试: 真实数据库与备份目录 -> 导出包 -> 回读校验.

锁住三件事: 包里的配置足以在另一台机器上重建这款游戏(存档位置标记为"原机器
路径"), 每个备份节点的内容**原样**进包(含节点自己的快照清单), 以及取消不会在
用户选择的路径上留下半成品.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import helpers
from archive_management.application.backup import BackupService
from archive_management.application.export import ExportService
from archive_management.domain import BackupNode, ScheduledJob
from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    ScheduledJobRepository,
)
from archive_management.services import export_format as fmt

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("导入导出"),
    pytest.mark.story("导出游戏归档包"),
    pytest.mark.layer("integration"),
]


def _fixture(root: Path) -> tuple[Database, int, Path]:
    """一个游戏 + 一个存档位置 + 两次备份 + 一个安全点 + 一条定时任务."""
    database = helpers.migrated_database(root)
    save = helpers.make_save_folder(root, content="state-0")
    game_id = helpers.add_game(database, "Demo", path=save)
    backups = BackupService(database, backup_root=root / "backups")
    backups.create_backup(game_id, title="第一次")
    helpers.touch_save(root)
    backups.create_backup(game_id, title="第二次")
    helpers.touch_save(root)
    backups.create_backup(game_id, title="恢复前安全点", safety=True)
    ScheduledJobRepository(database).upsert(
        ScheduledJob(game_id=game_id, schedule="1d", enabled=True, keep_auto=5)
    )
    return database, game_id, save


def _nodes(database: Database, game_id: int) -> list[BackupNode]:
    """该游戏的备份节点(按创建顺序)."""
    return list(BackupRepository(database).list_for_game(game_id))


def test_export_packages_the_game_backups_and_schedule(tmp_path: Path) -> None:
    """导出: 配置、存档位置、备份结构与定时任务都能在包里看见, 内容可回读校验."""
    database, game_id, save = _fixture(tmp_path)
    nodes = _nodes(database, game_id)
    service = ExportService(database, backup_root=tmp_path / "backups")
    destination = tmp_path / "Demo.archive.zip"

    result = service.export_game(game_id, destination)

    contents = fmt.read_package(destination, verify_hashes=True)
    assert result.destination == destination
    assert result.game_name == "Demo"
    assert result.backups == len(nodes) == 3
    assert result.files == len(contents.entries) > 0
    assert result.total_bytes == sum(entry.size for entry in contents.entries)
    config = contents.config
    assert contents.game["name"] == "Demo"
    assert config["locations"] == [
        {
            "index": 0,
            "path": str(save),
            "path_kind": "directory",
            "source": "manual",
            "is_primary": True,
            "original_machine": True,
        }
    ]
    assert config["schedule"] == {"interval": "1d", "enabled": True, "keep_auto": 5}
    backups = contents.config_list("backups")
    assert [item["is_safety"] for item in backups] == [False, False, True]
    assert sum(1 for item in backups if item["current"]) == 1


def test_export_carries_the_verification_mode_of_each_node(tmp_path: Path) -> None:
    """包里的每条备份都记着它当初用的校验方式(导入时才不会把"名称模式"当成严格)."""
    database, game_id, _save = _fixture(tmp_path)
    service = ExportService(database, backup_root=tmp_path / "backups")
    destination = tmp_path / "Demo.archive.zip"

    service.export_game(game_id, destination)

    contents = fmt.read_package(destination, verify_hashes=True)
    assert {item["verify_mode"] for item in contents.config_list("backups")} == {
        "sha256"
    }


def test_export_keeps_content_under_the_single_timeline_tree(tmp_path: Path) -> None:
    """v2 布局: 内容全部在 ``timeline/`` 下, 每个节点用 ``content`` 指向自己的内容目录."""
    database, game_id, _save = _fixture(tmp_path)
    service = ExportService(database, backup_root=tmp_path / "backups")

    service.export_game(game_id, tmp_path / "Demo.archive.zip")

    contents = fmt.read_package(tmp_path / "Demo.archive.zip", verify_hashes=True)
    backups = contents.config_list("backups")
    # 每个节点都带着内容引用, 引用的目录确实在包里; fixture 的三次备份内容各不相同,
    # 因此每个节点各自独占一份内容(content 就是它自己的目录名).
    for item in backups:
        assert contents.member_paths(f"timeline/{item['content']}") != ()
        assert item["content"] == item["id"]
    assert not [
        entry.path for entry in contents.entries if entry.path.startswith("branches/")
    ], "v2 包里不再有 branches/ 这棵树"
    assert any(
        path.endswith("/snapshot.json")
        for path in contents.member_paths(f"timeline/{backups[-1]['content']}")
    )

    staging = tmp_path / "staging"
    staging.mkdir()
    fmt.extract_package(contents, staging)
    latest = sorted(staging.glob(f"timeline/{backups[-1]['content']}/loc-0/*"))
    assert [item.name for item in latest] == ["slot.dat"]
    assert latest[0].read_text(encoding="utf-8") == "state-0++"


def test_identical_content_is_packed_once_and_shared_by_config(tmp_path: Path) -> None:
    """内容相同的节点在包里只装一份: 第三份备份与前一份共用同一个内容目录."""
    database = helpers.migrated_database(tmp_path)
    save = helpers.make_save_folder(tmp_path, content="state-0")
    game_id = helpers.add_game(database, "Demo", path=save)
    backups = BackupService(database, backup_root=tmp_path / "backups")
    backups.create_backup(game_id, title="第一次")
    helpers.touch_save(tmp_path)
    backups.create_backup(game_id, title="第二次")
    # 存档改回与第一次**完全相同**的字节: 与"当前节点"不同(不会被跳过),
    # 但与前一份备份内容一致 —— 这正是去重要消除的重复.
    (save / "slot.dat").write_text("state-0", encoding="utf-8")
    backups.create_backup(game_id, title="回到最初")
    service = ExportService(database, backup_root=tmp_path / "backups")

    result = service.export_game(game_id, tmp_path / "Demo.archive.zip")

    contents = fmt.read_package(tmp_path / "Demo.archive.zip", verify_hashes=True)
    backups_config = contents.config_list("backups")
    assert result.backups == len(backups_config) == 3
    first, _second, third = backups_config
    # 第三份引用第一份的内容目录; 包里只有两份内容, 3 份备份少装了一份重复内容.
    assert third["content"] == first["id"]
    assert third["content"] != third["id"]
    content_dirs = {entry.path.split("/", 2)[1] for entry in contents.entries}
    assert len(content_dirs) == 2


def test_export_records_the_parent_and_current_node(tmp_path: Path) -> None:
    """分支关系用包内节点名表达, "当前节点"标记只落在一个节点上."""
    database, game_id, _save = _fixture(tmp_path)
    nodes = _nodes(database, game_id)
    first, second = nodes[0], nodes[1]
    assert first.id is not None
    assert second.id is not None
    service = ExportService(database, backup_root=tmp_path / "backups")

    service.export_game(game_id, tmp_path / "Demo.archive.zip")

    contents = fmt.read_package(tmp_path / "Demo.archive.zip")
    backups = contents.config_list("backups")
    by_title = {item["title"]: item for item in backups}
    assert by_title["第一次"]["parent_id"] is None
    assert by_title["第二次"]["parent_id"] == by_title["第一次"]["id"]
    assert [item["title"] for item in backups if item["current"]] == ["恢复前安全点"]


def test_export_of_a_game_without_backups_still_makes_a_package(tmp_path: Path) -> None:
    """还没备份过的游戏也能导出(只有配置, 没有任何快照内容)."""
    database = helpers.migrated_database(tmp_path)
    save = helpers.make_save_folder(tmp_path)
    game_id = helpers.add_game(database, "Demo", path=save)
    service = ExportService(database, backup_root=tmp_path / "backups")

    result = service.export_game(game_id, tmp_path / "Demo.archive.zip")

    contents = fmt.read_package(tmp_path / "Demo.archive.zip", verify_hashes=True)
    assert result.backups == 0
    assert result.files == 0
    assert contents.entries == ()
    assert contents.config["backups"] == []
    assert contents.config["schedule"] is None


def test_export_refuses_an_unknown_game(tmp_path: Path) -> None:
    """未知游戏 id 直接报错, 不写文件."""
    database = helpers.migrated_database(tmp_path)
    service = ExportService(database, backup_root=tmp_path / "backups")
    destination = tmp_path / "Demo.archive.zip"

    with pytest.raises(ArchiveManagementError):
        service.export_game(999, destination)

    assert not destination.exists()


def test_export_can_be_cancelled_without_leaving_a_partial_file(
    tmp_path: Path,
) -> None:
    """取消: 目标路径上什么都没有(临时文件也被清掉)."""
    database, game_id, _save = _fixture(tmp_path)
    service = ExportService(database, backup_root=tmp_path / "backups")
    destination = tmp_path / "Demo.archive.zip"

    with pytest.raises(OperationCancelledError):
        service.export_game(game_id, destination, cancelled=lambda: True)

    assert not destination.exists()
    assert not list(tmp_path.glob("Demo.archive.zip.partial-*"))


def test_export_does_not_touch_the_backup_root(tmp_path: Path) -> None:
    """导出只读备份目录: 包写完以后备份根里的内容一字未改."""
    database, game_id, _save = _fixture(tmp_path)
    backup_root = tmp_path / "backups"
    before = sorted(
        (path.relative_to(backup_root), path.stat().st_size)
        for path in backup_root.rglob("*")
        if path.is_file()
    )
    service = ExportService(database, backup_root=backup_root)

    service.export_game(game_id, tmp_path / "Demo.archive.zip")

    after = sorted(
        (path.relative_to(backup_root), path.stat().st_size)
        for path in backup_root.rglob("*")
        if path.is_file()
    )
    assert after == before


def test_exporting_no_games_is_rejected(tmp_path: Path) -> None:
    """空列表不算一次批量导出: 报错, 而不是写出一个什么都没有的包."""
    database = helpers.migrated_database(tmp_path)
    service = ExportService(database, backup_root=tmp_path / "backups")
    destination = tmp_path / "batch.archive.zip"

    with pytest.raises(ArchiveManagementError, match="至少 1 款"):
        service.export_games([], destination)

    assert not destination.exists()


def test_exporting_an_unknown_game_is_rejected(tmp_path: Path) -> None:
    """导出库里没有的游戏: 报"未知游戏", 且不在目标路径上留半成品."""
    database = helpers.migrated_database(tmp_path)
    service = ExportService(database, backup_root=tmp_path / "backups")
    destination = tmp_path / "gone.archive.zip"

    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.export_game(999, destination)

    assert not destination.exists()


def test_a_node_without_a_storage_path_is_refused_before_writing(
    tmp_path: Path,
) -> None:
    """节点行缺少存储路径(历史数据或外部改过库): 明确拒绝, 且不留半成品.

    实际拦下它的是备份服务的 ``snapshot_root``(同一批节点先过那里), 这里只钉住
    "拒绝 + 目标路径上没有残缺包"这条契约, 不限定是哪一道守卫开口.
    """
    database, game_id, _save = _fixture(tmp_path)
    BackupRepository(database).add(BackupNode(game_id=game_id, node_kind="manual"))
    service = ExportService(database, backup_root=tmp_path / "backups")
    destination = tmp_path / "Demo.archive.zip"

    with pytest.raises(ArchiveManagementError, match="缺少存储路径"):
        service.export_game(game_id, destination)

    assert not destination.exists()


def test_two_games_with_the_same_name_get_distinct_inner_entries(
    tmp_path: Path,
) -> None:
    """同名两款游戏不能共用内层条目名(否则装包时后者会顶掉前者), 撞车时加序号."""
    database = helpers.migrated_database(tmp_path)
    backup_root = tmp_path / "backups"
    backups = BackupService(database, backup_root=backup_root)
    ids: list[int] = []
    for index in range(2):
        save = helpers.make_save_folder(tmp_path, index=index, content=f"state-{index}")
        game_id = helpers.add_game(database, "Demo", path=save)
        backups.create_backup(game_id, title=f"第 {index} 次")
        ids.append(game_id)
    destination = tmp_path / "batch.archive.zip"

    result = ExportService(database, backup_root=backup_root).export_games(
        ids, destination
    )

    assert result.games == 2
    assert result.game_names == ("Demo", "Demo")
    with fmt.read_batch_package(destination) as batch:
        assert [game.entry for game in batch.games] == [
            "Demo.archive.zip",
            "Demo-2.archive.zip",
        ]
        assert [game.name for game in batch.games] == ["Demo", "Demo"]
