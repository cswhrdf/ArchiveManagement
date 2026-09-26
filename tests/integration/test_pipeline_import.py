"""导入链路的集成测试: 导出包 -> 体检 -> 按策略导入本机.

包由真实的导出服务现场生成, 目标机器是另一个全新的数据库与备份根, 因此这里
锁住的是端到端事实: 体检读出的身份/位置/节点统计/定时任务, 三种策略(新建/
合并/跳过)各自写了什么、不写什么, 以及导入中途失败时节点目录与数据库行都会
被回收, 备份根下不留半截备份.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

import helpers
from archive_management.application.backup import BackupService
from archive_management.application.export import ExportService
from archive_management.application.imports import ImportService, PackageNode
from archive_management.application.locations import add_save_location
from archive_management.domain import BackupFileEntry, BackupNode, Game, ScheduledJob
from archive_management.exceptions import (
    ArchiveManagementError,
    DatabaseError,
    PackageError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
    ScheduledJobRepository,
)
from archive_management.services import export_format as fmt
from archive_management.services.naming import game_folder
from archive_management.services.pathcheck import normalize_path
from archive_management.services.snapshot import read_manifest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("导入导出"),
    pytest.mark.story("导入游戏归档包"),
    pytest.mark.layer("integration"),
]

# 源机器上的游戏标识(导入体检要用它做"疑似同一款"的匹配).
APP_ID = 730
NAME = "Demo"
# 三次快照的内容: 每推进一次存档内容, 备份服务才肯再写一个节点.
FIRST_BYTES = b"state-0"
SECOND_BYTES = b"state-0+"
THIRD_BYTES = b"state-0++"
OTHER_BYTES = b"extra-0"


@dataclass(frozen=True)
class _Source:
    """源机器: 造好的导出包与两个存档位置(供断言回读)."""

    package: Path
    save: Path
    other: Path
    node_ids: tuple[int, ...]


def _node_ids(nodes: Sequence[BackupNode]) -> tuple[int, ...]:
    """节点 id(源机器上的"旧 id", 用来证明父指针指向的是本机新行)."""
    ids: list[int] = []
    for node in nodes:
        assert node.id is not None
        ids.append(node.id)
    return tuple(ids)


def _build_package(root: Path) -> _Source:
    """造一台源机器并导出包: 两个存档位置 + 三次备份 + 一条定时任务."""
    source_root = root / "source"
    database = helpers.migrated_database(source_root)
    save = helpers.make_save_folder(source_root, content="state-0")
    other = helpers.make_save_folder(source_root, index=1, content="extra-0")
    game = GameRepository(database).add(
        Game(
            name=NAME,
            steam_app_id=APP_ID,
            platform="windows",
            origin="steam",
            tags=("动作", "存档"),
            enabled=True,
        )
    )
    assert game.id is not None
    add_save_location(database, game.id, path=str(save), kind="directory")
    add_save_location(
        database, game.id, path=str(other), kind="directory", source="steam"
    )
    backups = BackupService(database, backup_root=source_root / "backups")
    backups.create_backup(game.id, title="第一次")
    helpers.touch_save(source_root)
    backups.create_backup(game.id, title="第二次")
    helpers.touch_save(source_root)
    backups.create_backup(game.id, title="恢复前安全点", safety=True)
    ScheduledJobRepository(database).upsert(
        ScheduledJob(game_id=game.id, schedule="1d", enabled=True, keep_auto=5)
    )
    package = root / "Demo.archive.zip"
    ExportService(database, backup_root=source_root / "backups").export_game(
        game.id, package
    )
    nodes = BackupRepository(database).list_for_game(game.id)
    return _Source(package=package, save=save, other=other, node_ids=_node_ids(nodes))


def _target(root: Path) -> tuple[Database, Path, ImportService]:
    """目标机器: 空数据库 + 备份根, 返回数据库、备份根与导入服务."""
    database = helpers.migrated_database(root)
    backup_root = root / "backups"
    return database, backup_root, ImportService(database, backup_root=backup_root)


def _game(database: Database, game_id: int) -> Game:
    """读取目标库里的游戏记录(不存在就直接断言失败)."""
    game = GameRepository(database).get(game_id)
    assert game is not None
    return game


def _node_id(node: BackupNode) -> int:
    """备份节点 id(仓储读出的行必然带 id)."""
    assert node.id is not None
    return node.id


def _decoy_nodes(database: Database) -> tuple[int, ...]:
    """在目标库里先放一款别的游戏与三个节点.

    这样本次导入写出的节点 id 与包里的旧 id 错开, "父指针指向本机新行"才
    真的被断言到, 而不是两边恰好同号.
    """
    game_id = helpers.add_game(database, "别的游戏")
    ids: list[int] = []
    for index in range(3):
        node = helpers.add_backup_node(database, game_id, title=f"decoy-{index}")
        assert node.id is not None
        ids.append(node.id)
    return tuple(ids)


def _snapshot_roots(
    backup_root: Path, storage_key: str, nodes: Sequence[PackageNode]
) -> dict[str, Path]:
    """每个包内节点在本机备份根下的目录(按节点 key 索引)."""
    return {node.key: backup_root / storage_key / node.key for node in nodes}


def _files_under(root: Path) -> list[Path]:
    """``root`` 下的全部普通文件(判断"有没有留下半截备份")."""
    return [path for path in root.rglob("*") if path.is_file()]


def _mapping(source: _Source) -> dict[int, str]:
    """包内两个存档位置都映射到源机器的目录(位置存在, 体检才有意义)."""
    return {0: str(source.save), 1: str(source.other)}


# -- 体检 -------------------------------------------------------------------


def test_inspect_reads_the_package_and_finds_no_same_platform_game(
    tmp_path: Path,
) -> None:
    """体检读出身份/位置/节点统计与定时任务, 平台不同就不算匹配."""
    source = _build_package(tmp_path)
    database = helpers.migrated_database(tmp_path / "target")
    GameRepository(database).add(Game(name=NAME, steam_app_id=APP_ID, platform="linux"))
    service = ImportService(database, backup_root=tmp_path / "target" / "backups")

    inspection = service.inspect(source.package)

    assert inspection.path == source.package
    assert inspection.game_name == NAME
    assert inspection.steam_app_id == APP_ID
    assert inspection.platform == "windows"
    assert inspection.origin == "steam"
    assert inspection.tags == ("动作", "存档")
    assert inspection.matching_game_id is None
    assert [item.path for item in inspection.locations] == [
        str(source.save),
        str(source.other),
    ]
    assert [item.path_kind for item in inspection.locations] == [
        "directory",
        "directory",
    ]
    assert [item.source for item in inspection.locations] == ["manual", "steam"]
    assert [item.is_primary for item in inspection.locations] == [True, False]
    assert all(item.exists_here for item in inspection.locations)
    assert [node.title for node in inspection.nodes] == [
        "第一次",
        "第二次",
        "恢复前安全点",
    ]
    assert [node.is_safety for node in inspection.nodes] == [False, False, True]
    assert [node.parent_key for node in inspection.nodes] == [
        None,
        inspection.nodes[0].key,
        inspection.nodes[1].key,
    ]
    assert [node.current for node in inspection.nodes] == [False, False, True]
    assert inspection.backup_count == 3
    # 每个节点两个来源; 三次快照的内容是 state-0/state-0+/state-0++ 加 extra-0.
    assert inspection.file_count == 6
    assert inspection.total_bytes == (7 + 7) + (8 + 7) + (9 + 7)
    assert inspection.schedule == {"interval": "1d", "enabled": True, "keep_auto": 5}


def test_inspect_matches_a_game_on_the_same_platform_and_app_id(
    tmp_path: Path,
) -> None:
    """库里已有同平台 + 同 AppID 的游戏时, 体检给出这一款的 id."""
    source = _build_package(tmp_path)
    database = helpers.migrated_database(tmp_path / "target")
    existing = GameRepository(database).add(
        Game(name="别的名字", steam_app_id=APP_ID, platform="windows")
    )
    assert existing.id is not None
    service = ImportService(database, backup_root=tmp_path / "target" / "backups")

    inspection = service.inspect(source.package)

    assert inspection.matching_game_id == existing.id


# -- 新建策略 ---------------------------------------------------------------


def test_the_new_strategy_creates_a_disabled_game_and_normalizes_locations(
    tmp_path: Path,
) -> None:
    """新建策略: 游戏默认停用, 位置经规范化后落库, 未映射的位置不导入."""
    source = _build_package(tmp_path)
    database, _backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)
    messy = f"{source.save}/sub/.."

    result = service.import_package(inspection, strategy="new", locations={0: messy})

    assert result.strategy == "new"
    assert result.locations == 1
    game = _game(database, result.game_id)
    assert game.name == NAME
    assert game.original_name == NAME
    assert game.enabled is False
    assert game.steam_app_id == APP_ID
    assert game.platform == "windows"
    assert game.origin == "steam"
    assert game.tags == ("动作", "存档")
    # 备份目录名按"调用方映射进来的路径"推导(落库那条位置则是规范化后的), 值可精确算出.
    assert game.storage_key == game_folder(NAME, [messy])
    locations = SaveLocationRepository(database).list_for_game(result.game_id)
    assert normalize_path(messy) == str(source.save)
    assert [item.path for item in locations] == [normalize_path(messy)]
    assert locations[0].path_kind == "directory"
    assert locations[0].source == "manual"
    assert locations[0].is_primary is True


def test_the_new_strategy_imports_nodes_files_and_the_schedule(
    tmp_path: Path,
) -> None:
    """新建策略: 节点/父指针/当前节点/文件清单按包重建, 定时任务补上."""
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    decoys = _decoy_nodes(database)
    inspection = service.inspect(source.package)

    result = service.import_package(
        inspection, strategy="new", locations=_mapping(source)
    )

    assert result.nodes == 3
    assert result.skipped_nodes == 0
    assert result.locations == 2
    assert result.total_bytes == (7 + 7) + (8 + 7) + (9 + 7)
    # 每个节点四条清单项(两个来源的目录项 + 两个文件), 与包内清单逐条对应.
    assert result.files == 12
    game = _game(database, result.game_id)
    roots = _snapshot_roots(backup_root, game.storage_key, inspection.nodes)
    backups = BackupRepository(database)
    rows = {
        node.storage_relpath: node for node in backups.list_for_game(result.game_id)
    }
    expected_relpaths = {f"{game.storage_key}/{item.key}" for item in inspection.nodes}
    assert set(rows) == expected_relpaths
    ids = {
        item.key: _node_id(rows[f"{game.storage_key}/{item.key}"])
        for item in inspection.nodes
    }
    # 本机 id 从别处已占用的行之后开始: 与包里的旧 id 不同, 父指针必须重映射.
    assert set(ids.values()).isdisjoint(source.node_ids)
    assert set(ids.values()).isdisjoint(decoys)
    for item in inspection.nodes:
        row = rows[f"{game.storage_key}/{item.key}"]
        expected = None if item.parent_key is None else ids[item.parent_key]
        assert row.parent_id == expected
        assert roots[item.key].is_dir()
        stored = backups.list_files(ids[item.key])
        manifest = read_manifest(roots[item.key])
        assert [entry.relative_path for entry in stored] == [
            entry.relative_path for entry in manifest.entries
        ]
        assert [entry.sha256 for entry in stored] == [
            entry.sha256 for entry in manifest.entries
        ]
    current = ids[inspection.nodes[-1].key]
    assert GameRepository(database).current_backup(result.game_id) == current
    jobs = ScheduledJobRepository(database).for_game(result.game_id)
    assert len(jobs) == 1
    assert jobs[0].schedule == "1d"
    assert jobs[0].enabled is True
    assert jobs[0].keep_auto == 5


def test_the_imported_content_matches_the_source_save_folder_byte_for_byte(
    tmp_path: Path,
) -> None:
    """导入的内容原样落盘: 每个节点带自己的清单, 文件与源文件夹逐字节相同."""
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)

    result = service.import_package(
        inspection, strategy="new", locations=_mapping(source)
    )

    game = _game(database, result.game_id)
    roots = _snapshot_roots(backup_root, game.storage_key, inspection.nodes)
    ordered = [roots[item.key] for item in inspection.nodes]
    first, second, safety = ordered
    for root in ordered:
        assert (root / "loc-1/slot.dat").read_bytes() == OTHER_BYTES
        assert (root / "snapshot.json").is_file()
    assert (first / "loc-0/slot.dat").read_bytes() == FIRST_BYTES
    assert (second / "loc-0/slot.dat").read_bytes() == SECOND_BYTES
    assert (safety / "loc-0/slot.dat").read_bytes() == THIRD_BYTES
    live = (source.save / "slot.dat").read_bytes()
    assert live == THIRD_BYTES
    assert (safety / "loc-0/slot.dat").read_bytes() == live


# -- 合并策略 ---------------------------------------------------------------


def test_the_merge_strategy_appends_to_the_game_and_keeps_its_settings(
    tmp_path: Path,
) -> None:
    """合并策略: 节点挂到已有游戏上, 已有位置与定时任务原样不动."""
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    games = GameRepository(database)
    existing = games.add(Game(name=NAME, steam_app_id=APP_ID, platform="windows"))
    assert existing.id is not None
    keep = tmp_path / "target" / "keep"
    keep.mkdir()
    add_save_location(database, existing.id, path=str(keep), kind="directory")
    added = tmp_path / "target" / "added"
    added.mkdir()
    previous = helpers.add_backup_node(database, existing.id, title="既有节点")
    assert previous.id is not None
    games.set_current_backup(existing.id, previous.id)
    jobs = ScheduledJobRepository(database)
    jobs.upsert(
        ScheduledJob(game_id=existing.id, schedule="2h", enabled=True, keep_auto=2)
    )

    inspection = service.inspect(source.package)
    result = service.import_package(
        inspection,
        strategy="merge",
        target_game_id=existing.id,
        locations={0: str(keep), 1: str(added)},
    )

    assert result.game_id == existing.id
    # 映射到既有位置的那一条被跳过, 只有新目录真的写进去.
    assert result.locations == 1
    assert len(games.list()) == 1
    stored = SaveLocationRepository(database).list_for_game(existing.id)
    assert [item.path for item in stored] == [str(keep), str(added)]
    imported = BackupRepository(database).list_for_game(existing.id)
    assert len(imported) == 4
    key = _game(database, existing.id).storage_key
    rows = {node.storage_relpath: node for node in imported}
    ids = {item.key: _node_id(rows[f"{key}/{item.key}"]) for item in inspection.nodes}
    assert rows[f"{key}/{inspection.nodes[0].key}"].parent_id == previous.id
    assert (
        rows[f"{key}/{inspection.nodes[1].key}"].parent_id
        == ids[inspection.nodes[0].key]
    )
    assert (
        rows[f"{key}/{inspection.nodes[2].key}"].parent_id
        == ids[inspection.nodes[1].key]
    )
    assert games.current_backup(existing.id) == ids[inspection.nodes[2].key]
    assert {path.name for path in (backup_root / key).iterdir()} == {
        item.key for item in inspection.nodes
    }
    remaining = jobs.for_game(existing.id)
    assert len(remaining) == 1
    assert remaining[0].schedule == "2h"
    assert remaining[0].keep_auto == 2


# -- 跳过 / 重复导入 ---------------------------------------------------------


def test_the_skip_strategy_writes_nothing(tmp_path: Path) -> None:
    """跳过策略: 游戏数不变, 备份根与存档位置都没有任何写入."""
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    helpers.add_game(database, "Other")
    inspection = service.inspect(source.package)

    result = service.import_package(
        inspection, strategy="skip", locations={0: str(source.save)}
    )

    assert result.strategy == "skip"
    assert result.game_id == 0
    assert result.nodes == 0
    assert result.files == 0
    assert result.locations == 0
    assert result.skipped_nodes == 3
    assert [game.name for game in GameRepository(database).list()] == ["Other"]
    assert not backup_root.exists()


def test_reimporting_the_same_package_skips_every_existing_node(
    tmp_path: Path,
) -> None:
    """同一个包再导一次: 目录已存在的节点被跳过并计数, 不再产生重复行."""
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)

    first = service.import_package(
        inspection, strategy="new", locations=_mapping(source)
    )
    again = service.import_package(
        inspection,
        strategy="merge",
        target_game_id=first.game_id,
        locations=_mapping(source),
    )

    assert first.nodes == 3
    assert again.nodes == 0
    assert again.files == 0
    assert again.total_bytes == 0
    assert again.skipped_nodes == 3
    assert again.locations == 0
    assert len(BackupRepository(database).list_for_game(first.game_id)) == 3
    assert GameRepository(database).count_locations(first.game_id) == 2
    assert len(GameRepository(database).list()) == 1
    game = _game(database, first.game_id)
    for item in inspection.nodes:
        assert (backup_root / game.storage_key / item.key).is_dir()


def test_locations_without_a_mapping_are_not_imported(tmp_path: Path) -> None:
    """只有映射里给了路径的存档位置会被导入(没确认的目录不猜)."""
    source = _build_package(tmp_path)
    database, _backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)

    result = service.import_package(
        inspection, strategy="new", locations={1: str(source.other)}
    )

    assert result.locations == 1
    stored = SaveLocationRepository(database).list_for_game(result.game_id)
    assert [item.path for item in stored] == [str(source.other)]
    assert stored[0].source == "steam"
    assert stored[0].is_primary is True


def test_two_mappings_to_the_same_folder_store_only_one_location(
    tmp_path: Path,
) -> None:
    """包内两个位置指向同一个目录时, 第二条会被判重拦下."""
    source = _build_package(tmp_path)
    database, _backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)

    result = service.import_package(
        inspection,
        strategy="new",
        locations={0: str(source.save), 1: f"{source.save}/."},
    )

    assert result.locations == 1
    stored = SaveLocationRepository(database).list_for_game(result.game_id)
    assert [item.path for item in stored] == [str(source.save)]


# -- 回滚 -------------------------------------------------------------------


def test_a_cancelled_import_rolls_back_every_node(tmp_path: Path) -> None:
    """取消: 已就位的节点目录与行都被回收, 备份根下不留半截备份."""
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)
    seen = 0

    def cancel_after_the_first_node() -> bool:
        """放行第一个节点, 之后立刻请求取消."""
        nonlocal seen
        seen += 1
        return seen > 1

    with pytest.raises(ArchiveManagementError):
        service.import_package(
            inspection,
            strategy="new",
            locations={0: str(source.save)},
            cancelled=cancel_after_the_first_node,
        )

    game = GameRepository(database).list()[0]
    assert game.id is not None
    assert game.name == NAME
    assert BackupRepository(database).list_for_game(game.id) == []
    assert _files_under(backup_root) == []
    for item in inspection.nodes:
        assert not (backup_root / game.storage_key / item.key).exists()
    assert list(backup_root.glob(".import-*")) == []
    # 游戏与位置是按设计保留的: 节点层面的回收只管备份内容.
    assert GameRepository(database).count_locations(game.id) == 1


def test_a_failed_node_insert_leaves_no_directory_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """写入节点行失败时, 已改名到位的目录也必须被回收.

    留下的目录会被下一次导入当成"已存在"而跳过, 于是这份备份永远进不了库.
    """
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)
    original = BackupRepository.add_with_files
    calls = 0

    def fail_on_the_second_node(
        self: BackupRepository,
        node: BackupNode,
        entries: Sequence[BackupFileEntry],
    ) -> BackupNode:
        """第一次照常写入, 第二次直接失败(模拟落库异常)."""
        nonlocal calls
        calls += 1
        if calls == 2:
            raise DatabaseError("模拟写入备份节点失败")
        return original(self, node, entries)

    monkeypatch.setattr(BackupRepository, "add_with_files", fail_on_the_second_node)

    with pytest.raises(DatabaseError):
        service.import_package(inspection, strategy="new", locations=_mapping(source))

    game = GameRepository(database).list()[0]
    assert game.id is not None
    assert BackupRepository(database).list_for_game(game.id) == []
    assert _files_under(backup_root) == []
    for item in inspection.nodes:
        assert not (backup_root / game.storage_key / item.key).exists()


# -- 配置解析的拒绝与退化 ---------------------------------------------------


def _crafted_package(
    root: Path,
    *,
    config: dict[str, object],
    game: dict[str, object] | None = None,
    members: list[fmt.PackageFile] | None = None,
) -> Path:
    """手写一个结构合法、配置特殊的单包(钉住体检与导入侧的配置解析分支)."""
    source = root / "crafted.dat"
    source.write_bytes(b"alpha\n")
    files = [fmt.PackageFile("branches/n1/loc-0/crafted.dat", source)]
    return fmt.write_package(
        root / "crafted.archive.zip",
        game={"name": NAME, "steam_app_id": APP_ID, "platform": "windows"}
        if game is None
        else game,
        config=config,
        members=files if members is None else members,
        tool_version="9.9.9",
    ).path


def test_inspect_rejects_a_package_without_a_game_name(tmp_path: Path) -> None:
    """config.json 里的游戏名称为空: 体检直接拒绝, 不返回一份没法命名的文档."""
    package = _crafted_package(tmp_path, config={"game": {"name": ""}})
    _database, _root, service = _target(tmp_path / "target")

    with pytest.raises(PackageError, match="游戏名称为空"):
        service.inspect(package)


def test_inspect_rejects_a_game_field_that_is_not_an_object(tmp_path: Path) -> None:
    """config.json 里的 game 不是对象时拒绝(结构不符不猜)."""
    package = _crafted_package(tmp_path, config={"game": []})
    _database, _root, service = _target(tmp_path / "target")

    with pytest.raises(PackageError, match="game"):
        service.inspect(package)


def test_inspect_rejects_a_backup_node_without_an_id(tmp_path: Path) -> None:
    """备份节点缺少 id 时拒绝: 没有它就无法在包里定位节点内容."""
    package = _crafted_package(
        tmp_path, config={"game": {"name": NAME}, "backups": [{"title": "无名"}]}
    )
    _database, _root, service = _target(tmp_path / "target")

    with pytest.raises(PackageError, match="缺少 id"):
        service.inspect(package)


def test_inspect_rejects_a_save_location_without_an_index_or_path(
    tmp_path: Path,
) -> None:
    """存档位置缺少序号或路径时拒绝(两者缺一都无法映射到本机)."""
    package = _crafted_package(
        tmp_path, config={"game": {"name": NAME}, "locations": [{"path": ""}]}
    )
    _database, _root, service = _target(tmp_path / "target")

    with pytest.raises(PackageError, match="存档位置缺少序号或路径"):
        service.inspect(package)


def test_inspect_reads_branch_and_auto_nodes(tmp_path: Path) -> None:
    """节点类型 branch/auto 原样读出(不是一律当成手动备份)."""
    package = _crafted_package(
        tmp_path,
        config={
            "game": {"name": NAME},
            "backups": [
                {"id": "n1", "kind": "branch", "branch_name": "实验"},
                {"id": "n2", "kind": "auto"},
            ],
        },
    )
    _database, _root, service = _target(tmp_path / "target")

    inspection = service.inspect(package)

    assert [node.kind for node in inspection.nodes] == ["branch", "auto"]
    assert inspection.nodes[0].branch_name == "实验"


def test_inspect_tolerates_a_node_without_a_creation_time(tmp_path: Path) -> None:
    """节点没有 created_at 时按"由数据库补当前时间"处理, 不拒绝整包."""
    package = _crafted_package(
        tmp_path, config={"game": {"name": NAME}, "backups": [{"id": "n1"}]}
    )
    _database, _root, service = _target(tmp_path / "target")

    inspection = service.inspect(package)

    assert inspection.nodes[0].created_at is None


def test_inspect_treats_a_non_list_tag_field_as_no_tags(tmp_path: Path) -> None:
    """标签不是数组时按"没有标签"处理(包来自别处, 不该因此整包失败)."""
    package = _crafted_package(
        tmp_path, config={"game": {"name": NAME, "tags": "不是数组"}}
    )
    _database, _root, service = _target(tmp_path / "target")

    inspection = service.inspect(package)

    assert inspection.tags == ()


# -- 导入侧的拒绝 -----------------------------------------------------------


def test_merge_import_needs_a_target_game(tmp_path: Path) -> None:
    """合并策略没给目标游戏: 拒绝(否则不知道该并到哪一款), 一款游戏也不新建."""
    source = _build_package(tmp_path)
    database, _backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)

    with pytest.raises(ArchiveManagementError, match="合并导入需要先选定"):
        service.import_package(inspection, strategy="merge", target_game_id=None)

    assert GameRepository(database).list() == []


def test_merge_import_rejects_an_unknown_target_game(tmp_path: Path) -> None:
    """合并到不存在的游戏: 拒绝, 备份根下一个文件也不留."""
    source = _build_package(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)

    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.import_package(inspection, strategy="merge", target_game_id=999999)

    assert GameRepository(database).list() == []
    assert _files_under(backup_root) == []


def test_import_rejects_a_location_that_is_not_usable(tmp_path: Path) -> None:
    """映射到本机的存档位置不可用时拒绝(不把内容挂到一个不存在的路径上)."""
    source = _build_package(tmp_path)
    _database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(source.package)
    gone = tmp_path / "target" / "gone"

    with pytest.raises(ArchiveManagementError, match="存档位置不可用"):
        service.import_package(inspection, locations={0: str(gone)})

    assert _files_under(backup_root) == []


def test_import_rejects_a_declared_node_without_content(tmp_path: Path) -> None:
    """配置声明了一个节点但包里没有它的内容: 拒绝, 而不是写一个空备份."""
    package = _crafted_package(
        tmp_path,
        config={"game": {"name": NAME}, "backups": [{"id": "n1"}]},
        members=[],
    )
    _database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(package)

    with pytest.raises(PackageError, match="缺少节点内容"):
        service.import_package(inspection)

    assert _files_under(backup_root) == []


def test_import_ignores_a_schedule_without_an_interval(tmp_path: Path) -> None:
    """包里的定时任务没有周期时不登记: 空周期排不出下一次运行时间."""
    package = _crafted_package(
        tmp_path,
        config={
            "game": {"name": NAME},
            "schedule": {"interval": "", "enabled": True},
        },
        members=[],
    )
    database, _backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect(package)

    result = service.import_package(inspection)

    assert ScheduledJobRepository(database).for_game(result.game_id) == []


def _edit_package_config(path: Path, edit: Callable[[dict[str, Any]], None]) -> None:
    """就地改写包内 config.json(它不在清单的哈希校验范围内)."""
    with zipfile.ZipFile(path) as archive:
        items = [
            (info.filename, archive.read(info.filename)) for info in archive.infolist()
        ]
    rewritten: list[tuple[str, bytes]] = []
    for name, blob in items:
        if name == fmt.CONFIG_NAME:
            payload: dict[str, Any] = json.loads(blob)
            edit(payload)
            blob = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        rewritten.append((name, blob))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as target:
        for name, blob in rewritten:
            target.writestr(name, blob)


def test_import_accepts_a_node_without_a_creation_time(tmp_path: Path) -> None:
    """包里的节点没有 created_at 时由数据库补当前时间(不是拒绝整包)."""
    source = _build_package(tmp_path)
    database, _backup_root, service = _target(tmp_path / "target")

    def drop_timestamps(config: dict[str, Any]) -> None:
        for item in config["backups"]:
            item.pop("created_at", None)

    _edit_package_config(source.package, drop_timestamps)
    with zipfile.ZipFile(source.package) as archive:
        config: dict[str, Any] = json.loads(archive.read(fmt.CONFIG_NAME))
    assert all("created_at" not in item for item in config["backups"])

    inspection = service.inspect(source.package)
    result = service.import_package(inspection, locations=_mapping(source))

    assert result.nodes == len(inspection.nodes)
    stored = BackupRepository(database).list_for_game(result.game_id)
    assert {node.title for node in stored} == {"第一次", "第二次", "恢复前安全点"}
