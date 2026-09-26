"""批量链路的集成测试: 真实的两款游戏 -> 批量包 -> 整批导入另一台机器.

包由真实的导出服务现场生成(逐游戏走单包导出, 再装成一个批量包), 目标机器是另一个
全新的数据库与备份根, 因此这里锁住的是端到端事实: 批量体检读出的逐游戏内容, 整批
导入后游戏/节点/文件/内容与源机器逐字节一致, "跳过"与"重复导入"的语义, 同一款游戏
在包里出现两次时各占一个备份目录, 以及取消时"前面导完的保留、当前这一款回滚"的
按游戏边界.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

import helpers
from archive_management.application.backup import BackupService
from archive_management.application.export import BatchExportResult, ExportService
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_SKIP,
    BatchImportChoice,
    BatchImportResult,
    ImportService,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("导入导出"),
    pytest.mark.story("批量导出与导入"),
    pytest.mark.layer("integration"),
]

#: 两款游戏的名字与它们在批量包里的内层条目名(导出顺序即导入顺序).
NAMES = ("Demo", "Other")
ENTRIES = ("Demo.archive.zip", "Other.archive.zip")


@dataclass(frozen=True)
class _Source:
    """源机器: 批量包 + 各游戏的主存档目录与"最新一次备份的内容"."""

    batch: Path
    export: BatchExportResult
    saves: tuple[Path, ...]


def _build_source(root: Path) -> _Source:
    """造两款真实游戏(各有位置与两次备份)并批量导出一个包."""
    source_root = root / "source"
    database = helpers.migrated_database(source_root)
    backup_root = source_root / "backups"
    backups = BackupService(database, backup_root=backup_root)
    ids: list[int] = []
    saves: list[Path] = []
    for index, name in enumerate(NAMES):
        save = helpers.make_save_folder(
            source_root, index=index, content=f"state-{index}"
        )
        game_id = helpers.add_game(database, name, path=save)
        backups.create_backup(game_id, title="第一次")
        # 内容变了才肯写第二个节点(备份服务的既有规则).
        helpers.touch_save(source_root, index=index)
        backups.create_backup(game_id, title="第二次")
        ids.append(game_id)
        saves.append(save)
    batch = root / "batch.archive.zip"
    export = ExportService(database, backup_root=backup_root).export_games(ids, batch)
    return _Source(batch=batch, export=export, saves=tuple(saves))


def _target(root: Path) -> tuple[Database, Path, ImportService]:
    """目标机器: 空数据库 + 备份根, 返回数据库、备份根与导入服务."""
    database = helpers.migrated_database(root)
    backup_root = root / "backups"
    return database, backup_root, ImportService(database, backup_root=backup_root)


def _games(database: Database) -> dict[str, int]:
    """目标库里的"游戏名 -> id"(仓储读出的行必然带 id)."""
    found: dict[str, int] = {}
    for game in GameRepository(database).list():
        assert game.id is not None
        found[game.name] = game.id
    return found


def _storage_keys(database: Database) -> list[str]:
    """目标库里各游戏的备份目录名(顺序由仓储决定)."""
    keys: list[str] = []
    for game in GameRepository(database).list():
        assert game.storage_key
        keys.append(game.storage_key)
    return keys


def _node_roots(backup_root: Path, storage_key: str, keys: list[str]) -> list[Path]:
    """某个游戏名下各备份节点在本机备份根里的目录."""
    return [backup_root / storage_key / key for key in keys]


def _mapped(source: _Source) -> dict[str, BatchImportChoice]:
    """两款游戏都新建, 并把包内位置映射回源机器的目录(位置存在, 映射才可用)."""
    return {
        entry: BatchImportChoice(locations={0: str(save)})
        for entry, save in zip(ENTRIES, source.saves, strict=True)
    }


def _merge_into(
    source: _Source, result: BatchImportResult
) -> dict[str, BatchImportChoice]:
    """第二次导入: 合并到第一次建好的游戏上(与单包的"重复导入"用例同一手法)."""
    choices: dict[str, BatchImportChoice] = {}
    for index, entry in enumerate(ENTRIES):
        choices[entry] = BatchImportChoice(
            strategy=STRATEGY_MERGE,
            target_game_id=result.results[index].game_id,
            locations={0: str(source.saves[index])},
        )
    return choices


# -- 整批导入 ---------------------------------------------------------------


def test_a_batch_round_trip_rebuilds_both_games_with_their_content(
    tmp_path: Path,
) -> None:
    """整批导入: 两款游戏、节点、位置与内容都与源机器一致(逐字节)."""
    source = _build_source(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect_batch(source.batch)

    result = service.import_batch(inspection, _mapped(source))

    # 导出侧: 两款游戏各一个内层包, 内容统计与体检一致.
    assert source.export.games == 2
    assert source.export.game_names == NAMES
    assert source.export.backups == 4
    assert inspection.game_count == 2
    assert [item.name for item in inspection.games] == list(NAMES)
    assert [item.entry for item in inspection.games] == list(ENTRIES)
    assert inspection.backup_count == 4
    assert [item.inspection.matching_game_id for item in inspection.games] == [
        None,
        None,
    ]
    # 导入侧: 汇总与逐游戏结果一致(每款两次备份、一个位置).
    assert result.games == 2
    assert result.nodes == 4
    # 注意口径: ImportResult.files 数的是**快照清单条目**(每个来源的目录项 + 文件),
    # 而体检的 file_count 只数内容文件; 两者不能写成同一个数.
    assert result.files == 8
    assert result.total_bytes == inspection.total_bytes == 30
    assert result.skipped_games == 0
    assert result.skipped_nodes == 0
    assert [item.locations for item in result.results] == [1, 1]
    games = _games(database)
    assert sorted(games) == sorted(NAMES)
    for index, name in enumerate(NAMES):
        keys = [node.key for node in inspection.games[index].inspection.nodes]
        assert len(keys) == 2
        roots = _node_roots(backup_root, _storage_keys(database)[index], keys)
        assert all(root.is_dir() for root in roots)
        # 内容原样落盘: 第一次备份是 "state-<index>", 第二次是改过一次的内容.
        assert (roots[0] / "loc-0/slot.dat").read_bytes() == (f"state-{index}".encode())
        latest = (source.saves[index] / "slot.dat").read_bytes()
        assert latest == f"state-{index}+".encode()
        assert (roots[1] / "loc-0/slot.dat").read_bytes() == latest
        stored = SaveLocationRepository(database).list_for_game(games[name])
        assert [item.path for item in stored] == [str(source.saves[index])]
        assert stored[0].is_primary is True
        assert stored[0].source == "manual"
    # 导入的游戏一律新建为停用(启用态是本机状态, 由用户自己选一款).
    assert all(game.enabled is False for game in GameRepository(database).list())


def test_a_skipped_game_is_left_out_of_the_import(tmp_path: Path) -> None:
    """一款选"跳过": 它什么都不写, 另一款照常导入(游戏行与目录都没有它)."""
    source = _build_source(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect_batch(source.batch)
    choices = _mapped(source)
    choices[ENTRIES[1]] = BatchImportChoice(strategy=STRATEGY_SKIP)

    result = service.import_batch(inspection, choices)

    assert result.games == 2
    assert result.skipped_games == 1
    assert result.skipped_nodes == 2
    assert [item.strategy for item in result.results] == ["new", STRATEGY_SKIP]
    assert result.results[1].game_id == 0
    assert result.results[1].nodes == 0
    assert sorted(_games(database)) == [NAMES[0]]
    folders = [item for item in backup_root.glob("*") if item.is_dir()]
    assert len(folders) == 1
    assert len(list(folders[0].iterdir())) == 2


def test_reimporting_the_same_batch_skips_every_existing_node(
    tmp_path: Path,
) -> None:
    """同一个批量包再导一次: 节点目录都已存在, 全部跳过且不产生重复."""
    source = _build_source(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    first = service.import_batch(service.inspect_batch(source.batch), _mapped(source))
    assert first.nodes == 4

    again = service.import_batch(
        service.inspect_batch(source.batch), _merge_into(source, first)
    )

    assert again.nodes == 0
    assert again.files == 0
    assert again.total_bytes == 0
    assert again.skipped_nodes == 4
    assert again.skipped_games == 0
    assert sorted(_games(database)) == sorted(NAMES)
    for game_id in _games(database).values():
        assert len(BackupRepository(database).list_for_game(game_id)) == 2
    # 备份根下仍是两个游戏目录、每目录两个节点目录: 没有多搬一份内容.
    assert len(list(backup_root.glob("*/*"))) == 4


def test_a_game_without_a_location_mapping_is_imported_without_locations(
    tmp_path: Path,
) -> None:
    """只映射了第一款的位置: 第二款照样导入, 但不写任何存档位置(目录不猜)."""
    source = _build_source(tmp_path)
    database, _backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect_batch(source.batch)
    choices = _mapped(source)
    choices[ENTRIES[1]] = BatchImportChoice()

    result = service.import_batch(inspection, choices)

    assert result.games == 2
    assert [item.locations for item in result.results] == [1, 0]
    games = _games(database)
    assert sorted(games) == sorted(NAMES)
    stored = {
        name: SaveLocationRepository(database).list_for_game(game_id)
        for name, game_id in games.items()
    }
    assert [item.path for item in stored[NAMES[0]]] == [str(source.saves[0])]
    assert stored[NAMES[1]] == []


def test_the_same_game_twice_in_one_batch_gets_two_backup_folders(
    tmp_path: Path,
) -> None:
    """同一款游戏在批量包里出现两次: 两行各占一个目录, 两份内容都真的落盘.

    如果没有按条目名派生的目录后缀, 第二款会看到第一款写下的节点目录而整批被当成
    "已存在"跳过 —— 用户会以为导入成功, 实际上少了一款.
    """
    source_root = tmp_path / "source"
    database = helpers.migrated_database(source_root)
    save = helpers.make_save_folder(source_root, content="state-0")
    game_id = helpers.add_game(database, "Demo", path=save)
    BackupService(database, backup_root=source_root / "backups").create_backup(
        game_id, title="第一次"
    )
    batch = tmp_path / "batch.archive.zip"
    ExportService(database, backup_root=source_root / "backups").export_games(
        [game_id, game_id], batch
    )
    target_database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect_batch(batch)

    assert [item.entry for item in inspection.games] == [
        "Demo.archive.zip",
        "Demo-2.archive.zip",
    ]
    choices = {
        item.entry: BatchImportChoice(locations={0: str(save)})
        for item in inspection.games
    }
    result = service.import_batch(inspection, choices)

    assert result.nodes == 2
    assert result.skipped_nodes == 0
    # 同名两款游戏各自一条记录(名字相同, 所以这里数条数而不是用名字做键).
    assert len(GameRepository(target_database).list()) == 2
    assert len(set(_storage_keys(target_database))) == 2
    assert len(list(backup_root.glob("*/*"))) == 2


# -- 取消与失败 -------------------------------------------------------------


def test_cancelling_between_games_keeps_the_finished_one_and_writes_nothing_partial(
    tmp_path: Path,
) -> None:
    """取消: 已经导完的那一款保留, 后面的一个字都不写(按游戏的边界).

    "取消"的判据放在**磁盘事实**上(第一款的两个节点目录都已就位才请求取消), 所以
    这里断言的是真正的按游戏边界: 前面成功、后面不动; 失败/取消的那一款自己回滚.
    """
    source = _build_source(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect_batch(source.batch)
    first_keys = {node.key for node in inspection.games[0].inspection.nodes}

    def cancel_once_the_first_game_is_done() -> bool:
        """第一款的两个节点目录都在磁盘上了就请求取消."""
        for folder in backup_root.glob("*"):
            if not folder.is_dir() or folder.name.startswith("."):
                continue
            if first_keys <= {item.name for item in folder.iterdir()}:
                return True
        return False

    with pytest.raises(ArchiveManagementError):
        service.import_batch(
            inspection, _mapped(source), cancelled=cancel_once_the_first_game_is_done
        )

    assert sorted(_games(database)) == [NAMES[0]]
    first_id = _games(database)[NAMES[0]]
    assert len(BackupRepository(database).list_for_game(first_id)) == 2
    assert len(list(backup_root.glob("*/*"))) == 2
    assert not [item for item in backup_root.glob(".*")]


def test_an_unknown_strategy_aborts_before_writing_anything(tmp_path: Path) -> None:
    """未知策略直接报错, 备份根与数据库都不动(与单包导入同一句报错)."""
    source = _build_source(tmp_path)
    database, backup_root, service = _target(tmp_path / "target")
    inspection = service.inspect_batch(source.batch)
    choices = _mapped(source)
    choices[ENTRIES[0]] = BatchImportChoice(strategy="nope")

    with pytest.raises(ArchiveManagementError):
        service.import_batch(inspection, choices)

    assert GameRepository(database).list() == []
    assert not backup_root.exists()
