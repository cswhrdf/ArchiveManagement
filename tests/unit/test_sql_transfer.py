"""
导出/导入包: 单包导出三种形态、检视不改库、导入策略(新建/合并/跳过)、批量勾选与忙碌拒绝、失败日志与取消。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
    BatchInspection,
    ImportInspection,
)
from archive_management.domain import (
    BackupNode,
    Game,
)
from archive_management.exceptions import (
    ArchiveManagementError,
    OperationCancelledError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
)
from archive_management.services.export_format import (
    ARCHIVE_SUFFIX,
    read_batch_package,
    read_package,
)
from archive_management.services.pathcheck import normalize_path
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.models import (
    ImportChoice,
    size_label,
)
from archive_management.ui.sql_backend import SqlArchiveService

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _service,
    _service_with_save,
    _tree_files,
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


def test_run_export_writes_a_package_with_every_backup(tmp_path: Path) -> None:
    """真实后端的导出: 包里有游戏与备份内容, 提示里的计数与回读的包一致."""
    service = _service(tmp_path)
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot.dat").write_text("state-0", encoding="utf-8")
    game_id = service.add_game("Demo").game_id
    service.add_location(game_id, path=str(save), kind="directory")
    service.run_backup_now(game_id)
    destination = tmp_path / "Demo.archive.zip"

    message = service.run_export(game_id, str(destination))

    contents = read_package(destination, verify_hashes=True)
    total = sum(entry.size for entry in contents.entries)
    assert contents.game["name"] == "Demo"
    assert [item["path"] for item in contents.config_list("locations")] == [
        normalize_path(str(save))
    ]
    assert len(contents.config_list("backups")) == 1
    # 手动备份进 branches/<节点>/, 而且内容真的搬了过去(不只是配置里记了一笔).
    assert contents.member_paths("branches/") != ()
    assert "备份 1 份" in message
    assert f"文件 {len(contents.entries)} 个" in message
    assert size_label(total) in message


def test_run_export_without_backups_writes_a_config_only_package(
    tmp_path: Path,
) -> None:
    """没有备份也能导出: 包里就没有节点内容, 提示里的备份数为 0."""
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    destination = tmp_path / "Demo.archive.zip"

    message = service.run_export(game_id, str(destination))

    contents = read_package(destination, verify_hashes=True)
    assert contents.config_list("backups") == []
    assert "备份 0 份" in message


def test_run_export_of_an_unknown_game_leaves_no_file(tmp_path: Path) -> None:
    """未知游戏: 抛错且用户选的路径上不会留下半成品."""
    service = _service(tmp_path)
    destination = tmp_path / "Demo.archive.zip"

    with pytest.raises(ArchiveManagementError):
        service.run_export("999", str(destination))

    assert not destination.exists()


def _exported_package(
    tmp_path: Path, *, app_id: int | None = None
) -> tuple[Path, Path]:
    """造一台源机器并导出包, 返回包路径与源存档目录.

    源游戏带 ``app_id`` 时导入体检才能匹配到"库里的同一款"; 存档目录留在
    ``tmp_path`` 下, 因此体检时它是"本机已存在"的那一条。
    """
    root = tmp_path / "source"
    database = Database(root / "app.db")
    database.migrate()
    service = SqlArchiveService(
        database,
        backup_root=root / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )
    game = GameRepository(database).add(
        Game(
            name="Demo",
            steam_app_id=app_id,
            platform="windows",
            origin="steam",
            enabled=True,
        )
    )
    assert game.id is not None
    save = root / "save"
    save.mkdir(parents=True)
    (save / "slot.dat").write_text("state-0", encoding="utf-8")
    service.add_location(str(game.id), path=str(save), kind="directory")
    service.run_backup_now(str(game.id))
    package = tmp_path / "Demo.archive.zip"
    service.run_export(str(game.id), str(package))
    return package, save


def _single_inspection(service: SqlArchiveService, path: Path) -> ImportInspection:
    """单游戏包的体检结果(把联合类型收窄: 这些用例测的都是单包那条路径)."""
    inspection = service.inspect_import(str(path))
    assert isinstance(inspection, ImportInspection)
    return inspection


def _target_service(tmp_path: Path) -> tuple[SqlArchiveService, Database, Path]:
    """目标机器: 空数据库 + 独立备份根(导入的内容落在这里).

    返回后端、数据库与**备份根** —— "什么都没写"要断言的是备份根(数据库文件
    本来就在目标目录里, 不能拿整个目录当证据)。
    """
    root = tmp_path / "target"
    backup_root = root / "backups"
    database = Database(root / "app.db")
    database.migrate()
    service = SqlArchiveService(
        database,
        backup_root=backup_root,
        scheduler=BackupScheduler(backend=ManualBackend()),
    )
    return service, database, backup_root


def _one_node(database: Database, game_id: int) -> BackupNode:
    """该游戏的唯一备份节点(数量不对就直接失败, 免得后面断言看不出原因)."""
    nodes = BackupRepository(database).list_for_game(game_id)
    assert len(nodes) == 1, "预期恰好一个备份节点"
    return nodes[0]


def _node_id(node: BackupNode) -> int:
    """仓储读出的节点必然带 id(收窄供后续查询使用)."""
    assert node.id is not None
    return node.id


def test_inspect_import_reads_a_package_without_changing_anything(
    tmp_path: Path,
) -> None:
    """体检: 读得出包内容与"本机已存在"的位置, 但数据库与备份根一点没动."""
    package, save = _exported_package(tmp_path)
    service, _database, backup_root = _target_service(tmp_path)
    contents = read_package(package)

    inspection = _single_inspection(service, package)

    assert inspection.path == package
    assert inspection.game_name == "Demo"
    assert inspection.backup_count == 1
    # 包内文件数 = 节点目录下的成员减去该节点自己的快照清单.
    assert inspection.file_count == len(contents.member_paths("branches/")) - 1
    assert inspection.matching_game_id is None
    assert [item.path for item in inspection.locations] == [normalize_path(str(save))]
    assert [item.exists_here for item in inspection.locations] == [True]
    assert service.list_games() == []
    assert _tree_files(backup_root) == []


def test_inspect_import_matches_the_same_game_in_the_library(
    tmp_path: Path,
) -> None:
    """同平台 + 同 AppID 才算"库里疑似同一款"."""
    package, _save = _exported_package(tmp_path, app_id=730)
    service, database, _backup_root = _target_service(tmp_path)
    existing = GameRepository(database).add(
        Game(
            name="库里的 Demo",
            steam_app_id=730,
            platform="windows",
            origin="steam",
            enabled=True,
        )
    )
    assert existing.id is not None

    inspection = _single_inspection(service, package)

    assert inspection.matching_game_id == existing.id


def test_run_import_creates_the_game_nodes_and_locations(tmp_path: Path) -> None:
    """新建策略: 真的建游戏/写节点/登记映射的位置, 提示里是真实的计数."""
    package, _save = _exported_package(tmp_path)
    service, database, backup_root = _target_service(tmp_path)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    inspection = _single_inspection(service, package)

    message = service.run_import(
        inspection,
        strategy=STRATEGY_NEW,
        target_game_id=None,
        locations={0: str(target_save)},
    )

    games = service.list_games()
    assert [game.name for game in games] == ["Demo"]
    game_id = int(games[0].game_id)
    # 导入一律新建为停用: 启用态是本机的选择, 导入不该替用户启用任何东西.
    assert games[0].enabled is False
    node = _one_node(database, game_id)
    entries = BackupRepository(database).list_files(_node_id(node))
    assert message == tr(
        "result.import_done",
        name="Demo",
        nodes=1,
        files=len(entries),
        size=size_label(sum(entry.size for entry in entries)),
    )
    # 映射到哪个目录就是哪个目录(不是包里那个), 且内容真的落到了备份根下.
    assert [item.path for item in service.list_locations(str(game_id))] == [
        normalize_path(str(target_save))
    ]
    copied = backup_root
    assert node.storage_relpath is not None
    assert (copied / node.storage_relpath / "loc-0" / "slot.dat").is_file()
    assert service.list_backups(str(game_id))[0].verified is True


def test_run_import_appends_to_the_matching_game_and_marks_the_target(
    tmp_path: Path,
) -> None:
    """合并策略: 节点追加到目标游戏上, 而不是新建一款同名游戏."""
    package, _save = _exported_package(tmp_path, app_id=730)
    service, database, _backup_root = _target_service(tmp_path)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    (target_save / "slot.dat").write_text("state-1", encoding="utf-8")
    existing = GameRepository(database).add(
        Game(
            name="库里的 Demo",
            steam_app_id=730,
            platform="windows",
            origin="steam",
            enabled=True,
        )
    )
    assert existing.id is not None
    service.add_location(str(existing.id), path=str(target_save), kind="directory")
    service.run_backup_now(str(existing.id))
    inspection = _single_inspection(service, package)

    message = service.run_import(
        inspection,
        strategy=STRATEGY_MERGE,
        target_game_id=str(existing.id),
        locations={0: str(target_save)},
    )

    assert [game.name for game in service.list_games()] == ["库里的 Demo"]
    assert len(service.list_backups(str(existing.id))) == 2
    assert "已导入" in message
    # 映射到的路径与已有位置是同一个目录: 只记一条, 不重复添加.
    assert len(service.list_locations(str(existing.id))) == 1


def test_run_import_with_the_skip_strategy_writes_nothing(tmp_path: Path) -> None:
    """跳过策略: 既不建游戏也不写节点, 只如实说明跳过了多少份."""
    package, _save = _exported_package(tmp_path)
    service, _database, backup_root = _target_service(tmp_path)
    inspection = _single_inspection(service, package)

    message = service.run_import(
        inspection,
        strategy=STRATEGY_SKIP,
        target_game_id=None,
        locations={},
    )

    assert message == tr("result.import_skipped", name="Demo", skipped=1)
    assert service.list_games() == []
    assert _tree_files(backup_root) == []


def test_run_import_reports_the_nodes_it_skipped_on_a_repeat(
    tmp_path: Path,
) -> None:
    """重复导入同一个包: 已存在的节点被跳过并计数, 不覆盖也不重复写."""
    package, _save = _exported_package(tmp_path)
    service, _database, _backup_root = _target_service(tmp_path)
    inspection = _single_inspection(service, package)
    service.run_import(
        inspection,
        strategy=STRATEGY_NEW,
        target_game_id=None,
        locations={},
    )

    message = service.run_import(
        inspection,
        strategy=STRATEGY_NEW,
        target_game_id=None,
        locations={},
    )

    assert message == tr(
        "result.import_done_skipped",
        name="Demo",
        nodes=0,
        files=0,
        size=size_label(0),
        skipped=1,
    )
    assert [game.name for game in service.list_games()] == ["Demo", "Demo"]


def test_run_import_refuses_an_unknown_target_game(tmp_path: Path) -> None:
    """合并到一个不存在的游戏: 直接报错, 什么都不写."""
    package, _save = _exported_package(tmp_path)
    service, _database, backup_root = _target_service(tmp_path)
    inspection = _single_inspection(service, package)

    with pytest.raises(ArchiveManagementError):
        service.run_import(
            inspection,
            strategy=STRATEGY_MERGE,
            target_game_id="999",
            locations={},
        )

    assert service.list_games() == []
    assert _tree_files(backup_root) == []


def test_inspect_import_rejects_a_file_that_is_not_a_package(
    tmp_path: Path,
) -> None:
    """不是归档包的文件: 报错而不是留下半截数据."""
    service, _database, backup_root = _target_service(tmp_path)
    broken = tmp_path / "not-a-package.zip"
    broken.write_text("definitely not a zip", encoding="utf-8")

    with pytest.raises(ArchiveManagementError):
        service.inspect_import(str(broken))

    assert service.list_games() == []
    assert _tree_files(backup_root) == []


def _source_service(
    tmp_path: Path, *, count: int
) -> tuple[SqlArchiveService, list[str]]:
    """源机器: ``count`` 款游戏各带一个存档位置与一次备份."""
    root = tmp_path / "batch-source"
    database = Database(root / "app.db")
    database.migrate()
    service = SqlArchiveService(
        database,
        backup_root=root / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )
    game_ids: list[str] = []
    for index in range(count):
        game_id = service.add_game(f"Batch{chr(ord('A') + index)}").game_id
        save = root / f"save-{index}"
        save.mkdir(parents=True)
        (save / "slot.dat").write_text(f"state-{index}", encoding="utf-8")
        service.add_location(game_id, path=str(save), kind="directory")
        service.run_backup_now(game_id)
        game_ids.append(game_id)
    return service, game_ids


def _exported_batch(tmp_path: Path, *, count: int = 2) -> Path:
    """造一台源机器并导出一个批量包(批量导入用例的输入)."""
    service, game_ids = _source_service(tmp_path, count=count)
    package = tmp_path / "batch.archive.zip"
    service.run_export_batch(game_ids, str(package))
    return package


def test_run_export_batch_writes_a_package_with_every_selected_game(
    tmp_path: Path,
) -> None:
    """批量导出真的写出一个批量包: 回读的内层游戏与提示里的计数都对得上."""
    service = _service(tmp_path)
    game_ids: list[str] = []
    for index in range(2):
        game_id = service.add_game(f"批量游戏{index}").game_id
        save = tmp_path / f"save-{index}"
        save.mkdir()
        (save / "slot.dat").write_text(f"state-{index}", encoding="utf-8")
        service.add_location(game_id, path=str(save), kind="directory")
        service.run_backup_now(game_id)
        game_ids.append(game_id)
    destination = tmp_path / "batch.archive.zip"

    message = service.run_export_batch(game_ids, str(destination))

    assert destination.is_file()
    with read_batch_package(destination, verify_hashes=True) as contents:
        names = [item.name for item in contents.games]
        files = sum(len(item.package.entries) for item in contents.games)
        size = sum(
            entry.size for item in contents.games for entry in item.package.entries
        )
    assert names == ["批量游戏0", "批量游戏1"]
    assert message == tr(
        "result.export_batch_done",
        games=2,
        file=destination.name,
        backups=2,
        files=files,
        size=size_label(size),
    )
    # 导出的顺序就是传入的顺序(界面按用户在对话框里勾选的顺序传进来).
    assert not list(tmp_path.glob(".batch-*"))


def test_run_export_batch_rejects_an_empty_selection_and_unknown_games(
    tmp_path: Path,
) -> None:
    """空选择与不存在的游戏都直接报错, 目标路径一个文件都不留."""
    service = _service(tmp_path)
    game_id = service.add_game("真实游戏").game_id

    with pytest.raises(ArchiveManagementError):
        service.run_export_batch([], str(tmp_path / "empty.archive.zip"))
    with pytest.raises(ArchiveManagementError):
        service.run_export_batch(["999"], str(tmp_path / "unknown.archive.zip"))

    assert game_id  # 游戏本身没被动过
    assert list(tmp_path.glob("*.archive.zip")) == []
    # 两次都在"进行中操作"之前就被拦下了: 槽位必须已经释放.
    assert service.cancel_active() is False


def test_run_export_batch_cancelled_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取消批量导出: 目标不留文件、临时目录被清掉, 提示说明是取消(不是失败)."""
    service, game_ids = _source_service(tmp_path, count=1)
    monkeypatch.setattr(service, "_cancel_requested", lambda: True)
    destination = tmp_path / "cancelled.archive.zip"

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.run_export_batch(game_ids, str(destination))

    assert str(excinfo.value) == tr("action.export_batch_canceled")
    assert destination.exists() is False
    assert list(tmp_path.glob(".batch-*")) == []
    assert service.cancel_active() is False


def test_inspect_import_dispatches_on_the_package_kind(tmp_path: Path) -> None:
    """体检按包清单里的类型分派: 单包给单包体检, 批量包给批量体检."""
    single, _save = _exported_package(tmp_path)
    batch_path = _exported_batch(tmp_path)
    service, _database, _backup_root = _target_service(tmp_path)

    single_inspection = service.inspect_import(str(single))
    batch_inspection = service.inspect_import(str(batch_path))

    assert isinstance(single_inspection, ImportInspection)
    assert single_inspection.game_name == "Demo"
    assert isinstance(batch_inspection, BatchInspection)
    assert [item.entry for item in batch_inspection.games] == list(
        _batch_entries(batch_path)
    )
    assert batch_inspection.game_count == 2
    assert batch_inspection.backup_count == 2
    # 体检仍然只读: 目标库与备份根都没被写过.
    assert service.list_games() == []


def _batch_entries(package: Path) -> tuple[str, ...]:
    """批量包里的内层条目名(按清单顺序, 就是导出时的顺序)."""
    with read_batch_package(package) as contents:
        return tuple(item.entry for item in contents.games)


def test_run_import_batch_imports_it_for_real_and_honours_every_choice(
    tmp_path: Path,
) -> None:
    """批量导入: 游戏与备份真的出现, 选"跳过"的那一款不在库里, 提示里是真实计数."""
    batch_path = _exported_batch(tmp_path)
    service, database, backup_root = _target_service(tmp_path)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    batch = service.inspect_import(str(batch_path))
    assert isinstance(batch, BatchInspection)
    entries = [item.entry for item in batch.games]

    message = service.run_import_batch(
        batch,
        {
            entries[0]: ImportChoice(
                strategy=STRATEGY_NEW,
                target_game_id=None,
                locations={0: str(target_save)},
            ),
            entries[1]: ImportChoice(
                strategy=STRATEGY_SKIP, target_game_id=None, locations={}
            ),
        },
    )

    games = service.list_games()
    assert [game.name for game in games] == ["BatchA"]
    game_id = int(games[0].game_id)
    node = _one_node(database, game_id)
    entry_files = BackupRepository(database).list_files(_node_id(node))
    assert message == tr(
        "result.import_batch_done",
        games=2,
        nodes=1,
        files=len(entry_files),
        size=size_label(sum(item.size for item in entry_files)),
        skipped=0,
        skipped_games=1,
    )
    # 映射到哪个目录就写哪个目录, 内容也真的落到了备份根下.
    assert [item.path for item in service.list_locations(str(game_id))] == [
        normalize_path(str(target_save))
    ]
    assert node.storage_relpath is not None
    assert (backup_root / node.storage_relpath / "loc-0" / "slot.dat").is_file()


def test_run_import_batch_reports_a_wholly_skipped_batch(tmp_path: Path) -> None:
    """整批都选"跳过": 一句话说清什么都没导入, 而不是"已导入 N 款"."""
    batch_path = _exported_batch(tmp_path)
    service, _database, backup_root = _target_service(tmp_path)
    batch = service.inspect_import(str(batch_path))
    assert isinstance(batch, BatchInspection)

    message = service.run_import_batch(
        batch,
        {
            item.entry: ImportChoice(
                strategy=STRATEGY_SKIP, target_game_id=None, locations={}
            )
            for item in batch.games
        },
    )

    assert message == tr("result.import_batch_all_skipped", games=2)
    assert service.list_games() == []
    assert _tree_files(backup_root) == []


def test_run_export_batch_is_rejected_while_another_operation_runs(
    tmp_path: Path,
) -> None:
    """已有任务在跑时批量导出明确报忙, 目标路径一个文件都不留."""
    from archive_management.ui.sql_backend import _ActiveOperation

    service, game_ids = _source_service(tmp_path, count=1)
    destination = tmp_path / "busy.archive.zip"
    service._active = _ActiveOperation(game_id=int(game_ids[0]))

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.run_export_batch(game_ids, str(destination))

    assert str(excinfo.value) == tr("error.operation_busy")
    assert destination.exists() is False
    assert list(tmp_path.glob(".batch-*")) == []


def test_run_import_batch_is_rejected_while_another_operation_runs(
    tmp_path: Path,
) -> None:
    """已有任务在跑时批量导入明确报忙, 候选包里的游戏一款都不进库."""
    from archive_management.ui.sql_backend import _ActiveOperation

    batch_path = _exported_batch(tmp_path)
    service, _database, _backup_root = _target_service(tmp_path)
    batch = service.inspect_import(str(batch_path))
    assert isinstance(batch, BatchInspection)
    placeholder = service.add_game("占位游戏")
    service._active = _ActiveOperation(game_id=int(placeholder.game_id))

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.run_import_batch(batch, {})

    assert str(excinfo.value) == tr("error.operation_busy")
    assert [game.name for game in service.list_games()] == ["占位游戏"]


def test_a_failed_batch_import_is_logged_and_reraised(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """批量导入失败且不是用户取消时: 记一条失败审计, 异常照常抛上去."""
    batch_path = _exported_batch(tmp_path)
    service, _database, _backup_root = _target_service(tmp_path)
    batch = service.inspect_import(str(batch_path))
    assert isinstance(batch, BatchInspection)
    entry = batch.games[0].entry

    with pytest.raises(ArchiveManagementError):
        service.run_import_batch(
            batch,
            {
                entry: ImportChoice(
                    strategy="不认识的策略", target_game_id=None, locations={}
                )
            },
        )

    assert any("import.batch_failed" in line for line in audit_log)
    assert service.list_games() == []


def test_a_failed_export_is_logged_and_re_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log: list[str],
) -> None:
    """单包导出失败: 原因原样上抛(界面按域异常展示), 但审计里要留下这一笔."""
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ArchiveManagementError("磁盘满了")

    monkeypatch.setattr(service._export, "export_game", boom)

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.run_export(game_id, str(tmp_path / f"out{ARCHIVE_SUFFIX}"))

    assert str(excinfo.value) == "磁盘满了"
    assert any("export.failed" in line for line in audit_log)
    assert service._active is None


def test_a_failed_batch_export_is_logged_and_re_raised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log: list[str],
) -> None:
    """批量导出失败: 不回滚(目标路径不留文件由服务层保证), 只补审计并照样上抛."""
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ArchiveManagementError("磁盘满了")

    monkeypatch.setattr(service._export, "export_games", boom)

    with pytest.raises(ArchiveManagementError):
        service.run_export_batch([game_id], str(tmp_path / f"batch{ARCHIVE_SUFFIX}"))

    assert any("export.batch_failed" in line for line in audit_log)
    assert service._active is None


def test_a_cancelled_import_batch_is_reported_as_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """导入整批时用户真按了取消: 同一个异常换成"已取消"那句(真失败仍照常上抛)."""
    service = _service(tmp_path)
    batch = cast(
        BatchInspection,
        SimpleNamespace(path=tmp_path / f"in{ARCHIVE_SUFFIX}", game_count=1),
    )

    def stop(*_args: object, **_kwargs: object) -> None:
        raise ArchiveManagementError("导入被打断")

    monkeypatch.setattr(service._import, "import_batch", stop)
    monkeypatch.setattr(service, "_cancel_requested", lambda: True)

    with pytest.raises(OperationCancelledError) as excinfo:
        service.run_import_batch(batch, {})

    assert str(excinfo.value) == tr("result.import_batch_canceled")
    assert service._active is None
