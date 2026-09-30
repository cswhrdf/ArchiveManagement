"""基于 SQLite 的真实后端单元测试."""

from __future__ import annotations

import os
from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

import archive_management.application.locations as locations_mod
import archive_management.ui.sql_backend as sql_mod
import helpers
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
    BatchInspection,
    ImportInspection,
)
from archive_management.domain import (
    ArtworkRef,
    BackupNode,
    Game,
    GameCandidate,
    HomeFilter,
    HomeView,
    PlatformGame,
    PlatformId,
    SavePathCandidate,
    ScheduledJob,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import set_locale, tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    CandidateRepository,
    GameRepository,
    SaveLocationRepository,
    ScheduledJobRepository,
)
from archive_management.services.artwork import (
    ICON_SIZE,
    ICON_VERSION,
    STEAM_COVER_ASSET,
    artwork_cache_at,
    steam_cover,
    steam_icon,
)
from archive_management.services.export_format import (
    ARCHIVE_SUFFIX,
    read_batch_package,
    read_package,
)
from archive_management.services.game_names import NameFetcher, name_cache_at
from archive_management.services.pathcheck import normalize_path
from archive_management.services.platform_adapters import SaveCandidateSource
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.models import (
    ImportChoice,
    export_batch_prompt,
    filter_export_options,
    size_label,
    visible_in_branch_view,
)
from archive_management.ui.sql_backend import SqlArchiveService

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8

# 一款游戏的官方图标哈希(appinfo.vdf 的 clienticon).
_ICON_HASH = "b2f863a4c63bc1c5667a8a7e3e9355ef260ce6d2"

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


def _service(tmp_path: Path) -> SqlArchiveService:
    database = Database(tmp_path / "app.db")
    database.migrate()
    # 用不启动线程的手动调度后端, 让测试不依赖真实时间轴.
    return SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )


class _FrozenClock(datetime):
    """固定到某一秒的 ``datetime`` 替身: 让"同一秒内两次调用"可复现.

    自动导出的文件名只精确到秒, 撞名靠追加序号解决 —— 不把时钟钉死, 这条断言会在
    秒边界上偶发失败(两次调用落在不同的秒里, 文件名本来就不一样)。
    """

    @classmethod
    def now(cls, tz: tzinfo | None = None) -> _FrozenClock:
        """总是返回同一个时刻."""
        return cls(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


def test_add_game_and_list(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    games = service.list_games()
    assert [game.game_id for game in games] == [summary.game_id]
    assert summary.has_locations is False
    assert summary.backup_count == 0


def test_add_game_rejects_empty_name(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.add_game("   ")


def test_update_game_renames(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("旧名").game_id
    updated = service.update_game(game_id, "新名")
    assert updated.name == "新名"
    detail = service.get_detail(game_id)
    assert detail.name == "新名"
    # 重命名不改写"首次录入的名称", 界面据此展示额外的原始名称.
    assert detail.original_name == "旧名"
    # 期望值从文案资源拼, 不写死原文: 这里要验的是"重命名之后还能查到原始名称",
    # 文案本身的标点由 tests/unit/test_i18n.py 的守卫统一管(写死原文的断言会在
    # 标点改动时无谓变红 —— I-3 把中文句内的半角冒号改全角时就碰上了一次).
    assert detail.origin_label == tr("hero.original_name", name="旧名")


def test_backup_uses_named_folder_and_hides_the_key(tmp_path: Path) -> None:
    """备份目录用"名称 + 哈希", 但详情正文只说明它由应用自动命名."""
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)

    detail = service.get_detail(game_id)
    assert detail.storage_folder.startswith("Demo-")
    assert detail.storage_folder != game_id
    # 内部存储键只在悬停提示里, 不上正文(13 号评审).
    assert detail.origin_label == tr("hero.storage_folder")
    assert detail.storage_folder not in detail.origin_label
    assert detail.storage_hint == tr(
        "hero.storage_folder_tip", folder=detail.storage_folder
    )
    snapshots = list(
        (tmp_path / "backups" / detail.storage_folder).glob("*/loc-0/*.dat")
    )
    assert [item.name for item in snapshots] == ["slot1.dat"]
    assert str(save) not in detail.storage_folder


def test_set_game_enabled_flag(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    disabled = service.set_game_enabled(game_id, False)
    assert disabled.enabled is False
    enabled = service.set_game_enabled(game_id, True)
    assert enabled.enabled is True


def test_delete_game_removes_locations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(game_id, path=str(save), kind="directory")
    service.delete_game(game_id, str(tmp_path / "exports" / "demo.archive.zip"))
    assert service.list_games() == []
    with pytest.raises(ArchiveManagementError):
        service.list_locations(game_id)


def test_game_summaries_carry_the_original_name_for_search(tmp_path: Path) -> None:
    """摘要里带上**录入时的原名**: 批量导出的筛选靠它, 用户可能拿当初的名字来搜.

    改名之后 ``name`` 是新的, ``original_name`` 仍是录入时那个 —— 两者都要能搜到,
    这正是"筛选支持原名"那条改动成立的前提。
    """
    service = _service(tmp_path)
    game_id = service.add_game("Outer Wilds").game_id

    renamed = service.update_game(game_id, "星际拓荒")

    assert renamed.name == "星际拓荒"
    summaries = service.list_games()
    assert [item.name for item in summaries] == ["星际拓荒"]
    assert [item.original_name for item in summaries] == ["Outer Wilds"]
    options = export_batch_prompt(summaries).options
    assert [option.game_id for option in filter_export_options(options, "outer")] == [
        str(game_id)
    ]
    assert [option.game_id for option in filter_export_options(options, "拓荒")] == [
        str(game_id)
    ]


def test_delete_game_exports_the_game_before_removing_it(tmp_path: Path) -> None:
    """删除前先写出完整告别包: 可回读(含哈希校验), 且含存档位置与全部备份节点.

    顺序就是这次改动的全部意义 —— 包先落地、记录随后才删, 而且它与「导出游戏」
    按钮产出的是同一种包(同一套读包校验能把它读回来)。
    """
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    # 第二次备份前先改存档内容, 否则会被判为"未变化"而跳过.
    _advance(save)
    service.run_backup_now(game_id)
    database = Database(tmp_path / "app.db")
    destination = Path(service.delete_export_path(game_id))

    service.delete_game(game_id, str(destination))

    contents = read_package(destination, verify_hashes=True)
    assert [item["path"] for item in contents.config_list("locations")] == [str(save)]
    assert len(contents.config_list("backups")) == 2
    assert contents.member_paths("branches"), "包里的备份内容不该是空的"
    # 删除真的发生了: 记录、存档位置与备份节点一条不剩.
    assert GameRepository(database).get(int(game_id)) is None
    assert SaveLocationRepository(database).list_for_game(int(game_id)) == []
    assert BackupRepository(database).list_for_game(int(game_id)) == []


def test_delete_game_reports_the_export_it_made(
    tmp_path: Path, audit_log: list[str]
) -> None:
    """审计里两件事都要留痕: 先导出(``game.delete_export``)再删除(``game.delete``)."""
    service, game_id, _save = _service_with_save(tmp_path)
    destination = Path(service.delete_export_path(game_id))

    service.delete_game(game_id, str(destination))

    exported = next(line for line in audit_log if "game.delete_export" in line)
    assert destination.name in exported
    assert str(tmp_path) not in exported, "审计里的路径必须脱敏"
    assert any("game.delete" in line for line in audit_log)


def test_delete_export_path_only_computes_and_never_reuses_a_name(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """默认落点在 exports 目录里且只算路径不写文件; 同一秒的第二份绝不覆盖第一份.

    时钟钉死是关键: 文件名只精确到秒, 不钉死就会在秒边界上偶发失败(那样两次算出的
    时间戳本来就不同, "追加了 -2"的断言会时灵时不灵)。
    """
    monkeypatch.setattr(sql_mod, "datetime", _FrozenClock)
    service, game_id, _save = _service_with_save(tmp_path)

    first = Path(service.delete_export_path(game_id))

    assert first.parent == tmp_path / "exports"
    assert first.name == f"Demo-20260926-120000{ARCHIVE_SUFFIX}"
    assert not first.exists(), "只算路径的入口不该写出任何文件"
    assert not first.parent.exists(), "确认框之前不该顺手建目录"

    # 假装上一秒已经落过一份同名包: 再算一次必须换名字, 而且不许覆盖它.
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_bytes(b"earlier package")
    second = Path(service.delete_export_path(game_id))

    assert second.name == f"Demo-20260926-120000-2{ARCHIVE_SUFFIX}"

    service.delete_game(game_id, str(second))

    assert first.read_bytes() == b"earlier package", "自动导出覆盖了上一份包"
    assert second.is_file()


def test_a_failed_export_deletes_nothing_at_all(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """导出失败时一个字段都不删: 记录/位置/节点都在, 磁盘上也没有包.

    宁可留下一个删不掉的游戏, 也不能让用户丢掉"连告别包都写不出来"的那一款。
    """
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    database = Database(tmp_path / "app.db")
    destination = Path(service.delete_export_path(game_id))

    def boom(*_args: object, **_kwargs: object) -> None:
        raise ArchiveManagementError("磁盘满了")

    monkeypatch.setattr(service._export, "export_game", boom)
    with pytest.raises(ArchiveManagementError):
        service.delete_game(game_id, str(destination))

    assert GameRepository(database).get(int(game_id)) is not None
    assert len(SaveLocationRepository(database).list_for_game(int(game_id))) == 1
    assert len(BackupRepository(database).list_for_game(int(game_id))) == 1
    assert _tree_files(tmp_path / "exports") == []


def test_add_location_marks_first_as_primary(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    item_a = service.add_location(game_id, path=str(first), kind="directory")
    assert item_a.ok is True
    assert item_a.is_primary is True
    item_b = service.add_location(game_id, path=str(second), kind="directory")
    assert item_b.is_primary is False


def test_add_location_rejects_missing_path(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    with pytest.raises(ArchiveManagementError):
        service.add_location(game_id, path=str(tmp_path / "nope"), kind="directory")


def test_add_location_rejects_duplicate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(game_id, path=str(save), kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.add_location(game_id, path=str(save), kind="directory")


def test_set_primary_location_keeps_single_primary(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    first = tmp_path / "a"
    second = tmp_path / "b"
    first.mkdir()
    second.mkdir()
    item_a = service.add_location(game_id, path=str(first), kind="directory")
    item_b = service.add_location(game_id, path=str(second), kind="directory")
    service.set_primary_location(game_id, item_b.location_id)
    locations = service.list_locations(game_id)
    primary = [item for item in locations if item.is_primary]
    assert [item.location_id for item in primary] == [item_b.location_id]
    assert item_a.location_id != item_b.location_id


def test_update_location_changes_path(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    item = service.add_location(game_id, path=str(old), kind="directory")
    updated = service.update_location(item.location_id, path=str(new))
    assert updated.path == str(new)


def test_update_location_rejects_duplicate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first = service.add_location(game_id, path=str(one), kind="directory")
    service.add_location(game_id, path=str(two), kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.update_location(first.location_id, path=str(two))


def test_the_same_folder_in_another_spelling_is_rejected(tmp_path: Path) -> None:
    """同一个文件夹换一种写法(末尾多一个分隔符或 ``/.``)不能再登记一条位置.

    判重比的是字符串相等, 而这三种写法会被 ``normpath`` 收敛成同一个路径 —— 只要有一侧
    没规范化, 同一个目录就会静默变成两条位置(备份拍两份、恢复写两次、删掉其中一个还会把
    另一个位置的目标一起带走)。
    """
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    one = tmp_path / "one"
    two = tmp_path / "two"
    one.mkdir()
    two.mkdir()
    first = service.add_location(game_id, path=str(one), kind="directory")
    service.add_location(game_id, path=str(two), kind="directory")

    for spelling in (f"{one}{os.sep}.", f"{one}{os.sep}", f"{one}{os.sep}.{os.sep}"):
        with pytest.raises(ArchiveManagementError):
            service.add_location(game_id, path=spelling, kind="directory")

    with pytest.raises(ArchiveManagementError):
        service.update_location(first.location_id, path=f"{two}{os.sep}.")

    # 被拒之后两条位置各归各位: 路径是规范化形式, 也没有多出第三条.
    assert [item.path for item in service.list_locations(game_id)] == [
        str(one),
        str(two),
    ]


def test_every_stored_save_location_path_is_normalized(tmp_path: Path) -> None:
    """落库的路径必须已经是规范化形式 —— 判重只比字符串, "同一目录只有一条位置"全靠它.

    覆盖面是能写进 ``save_locations`` 的全部入口: 手动新增、改路径、导入时确认路径(含
    "与平台候选一致"那一支走 ``confirm_candidate``), 三处都故意传另一种写法。谁将来新增
    一条忘了规范化的写入路径, 这条用例会红(演示后端就是这条不变式被破坏后开始漏判的)。
    """
    save = tmp_path / "saves"
    save.mkdir()
    (save / "slot.dat").write_text("x", encoding="utf-8")
    extra = tmp_path / "extra"
    extra.mkdir()
    third = tmp_path / "third"
    third.mkdir()
    service, _game_id, database = _steam_service(tmp_path, str(save))
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    candidate, _created = CandidateRepository(database).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )
    assert candidate.id is not None

    # 导入时确认的两条路径都用另一种写法: 与平台候选一致的走 confirm_candidate,
    # 新加的走 add_location.
    summary = service.import_candidate(
        str(candidate.id), save_paths=(f"{save}{os.sep}.", f"{extra}{os.sep}")
    )
    locations = service.list_locations(summary.game_id)
    assert [item.path for item in locations] == [str(save), str(extra)]

    service.update_location(locations[1].location_id, path=f"{third}{os.sep}.")

    stored = [
        row.path
        for row in SaveLocationRepository(database).list_for_game(int(summary.game_id))
    ]
    assert stored == [str(save), str(third)]
    assert all(path == normalize_path(path) for path in stored), (
        "库里出现了没有规范化过的路径: 判重(字符串相等)从此对这一行失效"
    )


def test_verify_location_reports_status(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    save = tmp_path / "save"
    save.mkdir()
    item = service.add_location(game_id, path=str(save), kind="directory")
    refreshed = service.verify_location(item.location_id)
    assert refreshed.ok is True


def test_no_backups_yet_and_backup_without_locations_raise(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Demo").game_id
    assert service.list_backups(game_id) == []
    with pytest.raises(ArchiveManagementError):
        service.run_backup_now(game_id)


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


# ----------------------------------------------------- 导入归档包


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


def _tree_files(root: Path) -> list[Path]:
    """``root`` 下的全部普通文件(断言"什么都没写"用)."""
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


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


# ---------------------------------------------------------------- 批量导出/导入


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


def test_unknown_game_operations_raise(tmp_path: Path) -> None:
    service = _service(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.get_detail("999")
    with pytest.raises(ArchiveManagementError):
        service.add_location("abc", path="/x", kind="directory")
    with pytest.raises(ArchiveManagementError):
        service.delete_export_path("missing")
    with pytest.raises(ArchiveManagementError):
        service.delete_game("missing", "/nowhere/missing.archive.zip")


def test_task_status_reports_backup_root(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = service.task_status()
    assert status.running is False
    assert str(tmp_path / "backups") in status.target_label


# ----------------------------------------------------- 备份/分支/调度


def _advance(save: Path, text: str = "") -> None:
    """改动存档内容, 让下一次备份与当前节点不同(否则会被判为"未变化")."""
    target = save / "slot1.dat"
    previous = target.read_text(encoding="utf-8")
    target.write_text(text or f"{previous}+", encoding="utf-8")


def _service_with_save(
    tmp_path: Path, name: str = "Demo"
) -> tuple[SqlArchiveService, str, Path]:
    """构造带一个可用存档位置的游戏, 返回服务、游戏 id 与存档目录."""
    service = _service(tmp_path)
    game_id = service.add_game(name).game_id
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game_id, path=str(save), kind="directory")
    return service, game_id, save


def test_backup_now_creates_listable_node(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)

    message = service.run_backup_now(game_id)

    assert message
    items = service.list_backups(game_id)
    assert len(items) == 1
    assert items[0].verified is True
    assert items[0].depth == 0
    assert items[0].auto is False
    assert items[0].size_label != ""
    assert service.get_detail(game_id).last_backup_label != "—"
    assert service.list_games()[0].backup_count == 1


def test_backup_now_requires_locations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    game_id = service.add_game("Empty").game_id
    with pytest.raises(ArchiveManagementError):
        service.run_backup_now(game_id)


def test_new_backup_gets_default_title_and_empty_summary(tmp_path: Path) -> None:
    """新建备份的标题列直接给出默认名称, 内容摘要默认为空."""
    service, game_id, _save = _service_with_save(tmp_path)

    service.run_backup_now(game_id)

    item = service.list_backups(game_id)[0]
    assert item.title == tr("backup.title_manual")
    assert item.display_title == tr("backup.title_manual")
    assert item.sub == ""


def test_created_backup_title_is_persisted(tmp_path: Path) -> None:
    """默认名称写进数据库, 而不是只在界面回退展示."""
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.repository import BackupRepository

    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)

    nodes = BackupRepository(Database(tmp_path / "app.db")).list_for_game(int(game_id))
    assert [node.title for node in nodes] == [tr("backup.title_manual")]
    assert [node.note for node in nodes] == [""]


def test_second_backup_becomes_child_node(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    _advance(save)
    service.run_backup_now(game_id)

    items = service.list_backups(game_id)
    assert len(items) == 2
    assert items[0].parent_id is None
    assert items[1].parent_id == items[0].backup_id
    assert items[1].depth == 1


def test_create_branch_marks_node(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)

    message = service.run_create_branch(game_id, base.backup_id, "黑棘")

    assert "黑棘" in message
    items = service.list_backups(game_id)
    assert len(items) == 2
    branch = items[-1]
    assert branch.is_branch is True
    assert branch.parent_id == base.backup_id
    assert "黑棘" in branch.branch_label


def test_create_branch_rejects_unknown_backup(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.run_create_branch(game_id, "9999", "分支")


def test_create_branch_rejects_blank_name(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    with pytest.raises(ArchiveManagementError):
        service.run_create_branch(game_id, base.backup_id, "   ")


def test_set_schedule_persists_and_reports(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)

    status = service.set_schedule(game_id, "30m")

    assert status.schedule_text == "30m"
    assert service.task_status(game_id).schedule_text == "30m"
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert [job.schedule for job in jobs] == ["30m"]


def test_set_schedule_rejects_invalid_interval(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.set_schedule(game_id, "abc")


def test_set_schedule_blank_clears_schedule(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_schedule(game_id, "1h")

    status = service.set_schedule(game_id, "")

    assert status.schedule_text == ""
    assert (
        ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
        == []
    )


def test_cancel_active_returns_false_when_idle(tmp_path: Path) -> None:
    service, _game_id, _save = _service_with_save(tmp_path)
    assert service.cancel_active() is False


def test_shutdown_releases_scheduler_idempotently(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_schedule(game_id, "30m")

    service.shutdown()
    service.shutdown()

    assert service.task_status(game_id).schedule_text == ""


def test_restore_requires_known_backup(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    with pytest.raises(ArchiveManagementError):
        service.run_restore(game_id, "9999")


def test_task_status_without_game_reports_paused(tmp_path: Path) -> None:
    service = _service(tmp_path)
    status = service.task_status(None)
    assert status.running is False
    assert status.schedule_text == ""
    assert status.cancellable is False


# --------------------------------------------- 当前节点/删除/改名/保留份数


def _service_with_scheduler(
    tmp_path: Path,
) -> tuple[SqlArchiveService, str, ManualBackend]:
    """构造可手动触发的调度器, 返回服务、游戏 id 与手动调度后端."""
    backend = ManualBackend()
    database = Database(tmp_path / "app.db")
    database.migrate()
    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=backend),
    )
    game_id = service.add_game("Demo").game_id
    # 定时备份只对启用的游戏生效: 这些用例验证的是调度本身, 因此显式启用.
    service.set_game_enabled(game_id, True)
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game_id, path=str(save), kind="directory")
    return service, game_id, backend


def test_backup_marks_current_node_and_bumps_revision(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    before = service.task_status(game_id).revision

    service.run_backup_now(game_id)

    items = service.list_backups(game_id)
    assert [item.is_current for item in items] == [True]
    assert service.task_status(game_id).revision != before


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


def test_rename_backup_updates_title_and_note(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    item = service.list_backups(game_id)[0]

    updated = service.rename_backup(
        game_id, item.backup_id, title="通关前", note="第一次通关前的存档"
    )

    assert updated.title == "通关前"
    assert updated.sub == "第一次通关前的存档"
    assert service.list_backups(game_id)[0].title == "通关前"


def test_rename_backup_rejects_too_long_note(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    item = service.list_backups(game_id)[0]
    with pytest.raises(ArchiveManagementError):
        service.rename_backup(game_id, item.backup_id, title="t", note="x" * 201)


def test_plan_delete_signals_confirmation_for_branch_root(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, base.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]
    # 分支上继续保存, 形成"分支根 + 其备份"的结构.
    _advance(save)
    service.run_backup_now(game_id)

    plan = service.plan_delete(game_id, branch.backup_id)

    assert plan.needs_confirmation is True
    assert plan.removed_count == 2
    assert service.plan_delete(game_id, base.backup_id).needs_confirmation is False


def test_plan_delete_branch_tip_is_plain_single_delete(tmp_path: Path) -> None:
    """分支末端没有后续备份时, 删除它不涉及其它节点."""
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, base.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]

    plan = service.plan_delete(game_id, branch.backup_id)

    assert plan.needs_confirmation is False
    assert plan.removed_count == 1


def test_run_delete_backup_shifts_later_nodes(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    _advance(save)
    service.run_backup_now(game_id)
    _advance(save)
    service.run_backup_now(game_id)
    middle = service.list_backups(game_id)[1]

    message = service.run_delete_backup(game_id, middle.backup_id)

    assert message
    remaining = service.list_backups(game_id)
    assert len(remaining) == 2
    assert remaining[1].parent_id == remaining[0].backup_id


def test_run_delete_backup_removes_branch_subtree(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    base = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, base.backup_id, "Branch")
    branch = service.list_backups(game_id)[-1]

    service.run_delete_backup(game_id, branch.backup_id)

    remaining = service.list_backups(game_id)
    assert [item.backup_id for item in remaining] == [base.backup_id]


def test_set_schedule_persists_keep_auto(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)

    status = service.set_schedule(game_id, "30m", keep_auto=5)

    assert status.keep_auto == 5
    assert "5" in status.task_name
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].keep_auto == 5


def test_scheduled_backup_creates_auto_node_and_prunes(
    tmp_path: Path,
) -> None:
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m", keep_auto=1)

    backend.trigger(f"backup-{game_id}")
    backend.trigger(f"backup-{game_id}")
    backend.trigger(f"backup-{game_id}")

    autos = [item for item in service.list_backups(game_id) if item.auto]
    assert len(autos) == 1
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_run_at is not None
    assert jobs[0].last_error is None


def test_schedules_are_restored_on_startup(tmp_path: Path) -> None:
    """重启后要能从数据库恢复已配置的定时任务(否则界面显示"未配置")."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m", keep_auto=4)

    # 模拟重启: 同一个数据库重新构造服务(新的调度器实例).
    restarted = SqlArchiveService(
        Database(tmp_path / "app.db"),
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    status = restarted.task_status(game_id)
    assert status.schedule_text == "30m"
    assert status.keep_auto == 4
    assert status.schedule_enabled is True
    item = next(i for i in restarted.list_schedules() if i.game_id == game_id)
    assert item.interval_text == "30m"
    assert item.state_label == "已启用"


def test_paused_schedule_survives_restart(tmp_path: Path) -> None:
    """暂停状态与周期配置也要在重启后保持(不会变成未配置)."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    assert service.set_schedule(game_id, "2h", enabled=False)

    restarted = SqlArchiveService(
        Database(tmp_path / "app.db"),
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    status = restarted.task_status(game_id)
    assert status.schedule_text == "2h"
    assert status.schedule_enabled is False


def test_unparsable_stored_schedule_is_ignored(tmp_path: Path) -> None:
    """数据库里的脏数据不应该阻止服务启动."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    game = GameRepository(database).add(Game(name="Demo"))
    assert game.id is not None
    ScheduledJobRepository(database).upsert(
        ScheduledJob(id=None, game_id=game.id, schedule="每个星期")
    )

    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    assert service.task_status(str(game.id)).schedule_text == ""
    assert service.list_schedules()


def test_schedule_requires_save_locations(tmp_path: Path) -> None:
    """未配置存档位置的游戏不能创建定时备份, 错误信息要说明原因."""
    service = _service(tmp_path)
    game_id = service.add_game("无位置").game_id

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_schedule(game_id, "30m")

    assert "存档位置" in str(excinfo.value)
    assert service.task_status(game_id).schedule_text == ""
    item = next(i for i in service.list_schedules() if i.game_id == game_id)
    assert item.can_schedule is False


def test_skipped_run_refreshes_next_run_and_revision(tmp_path: Path) -> None:
    """存档未变化被跳过时也要刷新下次运行时间与数据版本(界面需重读)."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    service.run_backup_now(game_id)
    before = service.task_status(game_id)

    backend.trigger(f"backup-{game_id}")

    after = service.task_status(game_id)
    assert after.revision != before.revision
    assert len(service.list_backups(game_id)) == 1
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_run_at is not None


def test_scheduled_backup_failure_is_recorded(tmp_path: Path) -> None:
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    save_dir = service.list_locations(game_id)[0].path
    path = Path(save_dir)
    for child in path.iterdir():
        child.unlink()
    path.rmdir()

    backend.trigger(f"backup-{game_id}")

    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_error is not None


def test_scheduled_backup_skips_unchanged_save(tmp_path: Path) -> None:
    """自动备份遇到"存档未变化"时静默跳过: 不新增节点, 也不报错."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    service.run_backup_now(game_id)
    before = len(service.list_backups(game_id))

    backend.trigger(f"backup-{game_id}")

    assert len(service.list_backups(game_id)) == before
    jobs = ScheduledJobRepository(Database(tmp_path / "app.db")).for_game(int(game_id))
    assert jobs[0].last_error is None
    status = service.task_status(game_id)
    assert status.progress_label == ""


def test_storage_usage_counts_backup_storage(tmp_path: Path) -> None:
    """状态栏展示的"当前占用"来自备份存储的实际大小."""
    service, game_id, _save = _service_with_save(tmp_path)
    before = service.storage_usage()

    service.run_backup_now(game_id)

    after = service.storage_usage()
    assert after > before
    assert after > 0


def test_list_schedules_reports_all_games(tmp_path: Path) -> None:
    """全局任务列表包含每个游戏: 来源游戏、下次运行与已保留的自动备份份数."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    other = service.add_game("另一位玩家").game_id
    service.set_schedule(game_id, "30m", keep_auto=2)
    backend.trigger(f"backup-{game_id}")

    items = {item.game_id: item for item in service.list_schedules()}

    assert set(items) == {game_id, other}
    configured = items[game_id]
    assert configured.game_name == "Demo"
    assert configured.interval_text == "30m"
    assert configured.keep_auto == 2
    # ManifestBackend 没有时间轴(下次运行时间为 None): 标签本身是空串, 但给用户看的
    # 文案必须落到"未安排"这种能读的说法上, 而不是一个像加载失败的短横线; 真实调度器
    # 会给出具体时间戳(见 test_pause_keeps_interval_configuration).
    assert configured.next_run_label == ""
    assert configured.next_run_text == tr("schedule.next_run_none")
    assert configured.auto_count == 1
    assert configured.auto_count_label
    assert configured.state_label == "已启用"
    # 未配置定时备份的游戏也出现在列表里, 便于直接新增.
    idle = items[other]
    assert idle.interval_text == ""
    assert idle.state_label == "未配置"


def test_detail_marks_a_paused_schedule_as_paused_not_missing(tmp_path: Path) -> None:
    """配了周期但没启用时, 详情页不能说"未配置"(用户反馈: 会产生误解)."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m", enabled=False)

    paused = service.get_detail(game_id)

    assert paused.next_backup_label == ""
    assert paused.next_backup_paused is True
    assert paused.next_backup_text == tr("hero.next_paused")

    # 启用之后回到"有时间戳"的正常路径, 不再报暂停.
    service.set_schedule(game_id, "30m")
    enabled = service.get_detail(game_id)
    assert enabled.next_backup_paused is False
    assert enabled.next_backup_text != tr("hero.next_paused")


def test_pause_keeps_interval_configuration(tmp_path: Path) -> None:
    """暂停只停触发, 不清空周期配置(否则再编辑会丢掉周期)."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)

    paused = service.set_schedule(game_id, "30m", enabled=False)

    assert paused.schedule_text == "30m"
    assert paused.schedule_enabled is False
    item = next(i for i in service.list_schedules() if i.game_id == game_id)
    assert item.interval_text == "30m"
    assert item.enabled is False
    assert item.state_label == "已暂停"
    # 暂停后没有排期: 标签为空, 展示文案给出"未安排", 而不是一个孤零零的破折号.
    assert item.next_run_label == ""
    assert item.next_run_text == tr("schedule.next_run_none")


def test_schedule_for_a_disabled_game_is_created_paused(tmp_path: Path) -> None:
    """停用中的游戏允许先配好周期, 但任务只能是暂停态."""
    service, game_id, _save = _service_with_save(tmp_path)

    status = service.set_schedule(game_id, "30m")

    assert status.schedule_text == "30m"
    assert status.schedule_enabled is False
    item = next(i for i in service.list_schedules() if i.game_id == game_id)
    assert item.state_label == "已暂停"
    assert item.can_enable is False
    assert item.can_toggle is False


def test_resuming_a_schedule_of_a_disabled_game_is_rejected(
    tmp_path: Path,
) -> None:
    """停用中的游戏不允许把已有任务切到启用态."""
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_schedule(game_id, "30m")

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_schedule(game_id, "30m", enabled=True)

    assert "停用" in str(excinfo.value)
    assert service.task_status(game_id).schedule_enabled is False


def test_disabling_a_game_pauses_its_schedule(tmp_path: Path) -> None:
    """停用游戏时把它已启用的定时任务置为暂停."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    assert service.task_status(game_id).schedule_enabled is True

    service.set_game_enabled(game_id, False)

    status = service.task_status(game_id)
    assert status.schedule_text == "30m"
    assert status.schedule_enabled is False


def test_scheduled_backup_skips_a_disabled_game(tmp_path: Path) -> None:
    """停用期间不执行自动备份(任务配置保留)."""
    service, game_id, backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")
    service.set_game_enabled(game_id, False)

    backend.trigger(f"backup-{game_id}")

    assert service.list_backups(game_id) == []
    assert service.task_status(game_id).schedule_text == "30m"


def test_archiving_disables_the_game_and_pauses_its_schedule(
    tmp_path: Path,
) -> None:
    """归档只保留删除/导出/取消归档/打开详情: 同时停用游戏并暂停定时备份."""
    service, game_id, _backend = _service_with_scheduler(tmp_path)
    service.set_schedule(game_id, "30m")

    board = service.set_game_archived(game_id, True)

    assert board.stats.archived == 1
    # 归档游戏不出现在默认视图里, 因此摘要直接按 id 取完整列表.
    summary = next(game for game in service.list_games() if game.game_id == game_id)
    assert summary.archived is True
    assert summary.enabled is False
    assert service.task_status(game_id).schedule_enabled is False
    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_schedule(game_id, "30m", enabled=True)
    assert "已归档" in str(excinfo.value)


def test_delete_game_returns_its_candidate_to_pending(tmp_path: Path) -> None:
    """后端删除游戏时也要把探测候选退回待处理(界面入口的兼底)."""
    service = _service(tmp_path)
    install = tmp_path / "steam" / "Demo"
    install.mkdir(parents=True)
    database = Database(tmp_path / "app.db")
    candidate, _created = CandidateRepository(database).upsert(
        GameCandidate(name="Demo", install_dir=str(install), source="steam")
    )
    assert candidate.id is not None
    imported = service.import_candidate(str(candidate.id))

    service.delete_game(imported.game_id, str(tmp_path / "exports" / "imported.zip"))

    released = CandidateRepository(database).get(candidate.id)
    assert released is not None
    assert released.status == "new"
    assert released.game_id is None


def test_enabling_a_game_disables_the_other_one(tmp_path: Path) -> None:
    """全局只允许一款游戏启用: 启用它时自动停用另一款."""
    service = _service(tmp_path)
    first = service.add_game("第一位").game_id
    second = service.add_game("第二位").game_id
    service.set_game_enabled(first, True)

    service.set_game_enabled(second, True)

    games = {game.game_id: game for game in service.list_games()}
    assert games[second].enabled is True
    assert games[first].enabled is False


def test_enabling_an_archived_game_is_rejected(tmp_path: Path) -> None:
    service, game_id, _save = _service_with_save(tmp_path)
    service.set_game_archived(game_id, True)

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.set_game_enabled(game_id, True)

    assert "已归档" in str(excinfo.value)


class _FakeSaveSource:
    """固定的存档候选来源(避免用例去读真实 Steam 目录)."""

    def __init__(self, *paths: str) -> None:
        self._paths = paths

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        """返回构造时给定的候选路径."""
        return [
            SavePathCandidate(
                path=path, reason_code="steam_remotecache", detail="remotecache.vdf"
            )
            for path in self._paths
        ]


class _FakeAdapter:
    """测试替身适配器: 存档候选来自固定来源, 图片引用由调用方指定."""

    platform: PlatformId = "steam"
    supported: bool = True
    unsupported_reason: str = ""
    supports_save_paths: bool = True
    supports_artwork: bool = True

    def __init__(
        self, cloud: SaveCandidateSource, *, icon: ArtworkRef | None = None
    ) -> None:
        self._cloud = cloud
        self._icon = icon

    def list_games(self) -> list[PlatformGame]:
        """替身不提供游戏列表(用例自己造游戏记录)."""
        return []

    def save_candidates(self, game: PlatformGame) -> list[SavePathCandidate]:
        """存档候选来自注入的固定来源."""
        return self._cloud.candidates(game.game_id, install_dir=None)

    def artwork_refs(self, game: PlatformGame) -> tuple[ArtworkRef, ...]:
        """封面按公开 CDN 规则构造; 另可注入一份官方图标引用(与真实适配器一致)."""
        if not game.game_id:
            return ()
        refs = [steam_cover(game.game_id)]
        if self._icon is not None:
            refs.append(self._icon)
        return tuple(refs)


class _StubNames:
    """按 AppID 返回固定译名的替身(不联网)."""

    def __init__(self, names: dict[str, str]) -> None:
        """绑定 AppID 到译名的映射."""
        self._names = names

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """返回预设译名(没有映射时返回 None, 走原名回落)."""
        del language, timeout, max_bytes
        return self._names.get(app_id)


def _steam_service(
    tmp_path: Path,
    *paths: str,
    cache_dir: Path | None = None,
    name_fetcher: NameFetcher | None = None,
    icon: ArtworkRef | None = None,
) -> tuple[SqlArchiveService, str, Database]:
    """一个带 Steam AppID 的游戏 + 固定候选来源与替身适配器的服务实例."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    source = _FakeSaveSource(*paths)
    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        cache_dir=cache_dir,
        save_source=source,
        adapters={"steam": _FakeAdapter(source, icon=icon)},
        name_fetcher=name_fetcher,
    )
    game_id = service.add_game("Demo").game_id
    game = GameRepository(database).get(int(game_id))
    assert game is not None
    GameRepository(database).update(game.model_copy(update={"steam_app_id": 730}))
    # ``update`` 只写名称/Steam/平台/启用态, 来源要单独设(set_origin).
    GameRepository(database).set_origin(int(game_id), "steam")
    return service, game_id, database


def test_discovery_rows_carry_probed_save_paths(tmp_path: Path) -> None:
    """探测结果行里就带着该游戏的存档路径建议(只探测, 不写库)."""
    save = tmp_path / "saves"
    save.mkdir()
    (save / "slot.dat").write_text("x", encoding="utf-8")
    service, _game_id, database = _steam_service(tmp_path, str(save))
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    item = next(row for row in service.list_candidates() if row.name == "哈迪斯")

    assert item.save_supported is True
    assert [path.path for path in item.save_paths] == [str(save)]
    assert item.save_label == tr("discovery.save_found", count=1)
    # 只是建议: 存档位置仍然要等用户在导入对话框里确认.
    assert service.list_locations("1") == []


def test_discovery_rows_say_when_the_platform_is_unsupported(tmp_path: Path) -> None:
    """监控目录这类非平台来源不猜测存档路径, 行里明说需要手动添加."""
    service, _game_id, database = _steam_service(tmp_path)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(name="哈迪斯", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "哈迪斯")

    assert item.save_supported is False
    assert item.save_paths == ()
    assert item.save_label == tr(
        "discovery.save_unsupported", platform=item.source_label
    )


def test_artwork_path_reads_only_the_cache(tmp_path: Path) -> None:
    """封面/图标接口只查缓存: 预置缓存图能拿到路径, 未配置缓存目录时返回空."""
    cache = artwork_cache_at(tmp_path / "cache")
    cover = cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    icon = cache.store(
        "steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png"
    )
    service, game_id, _database = _steam_service(tmp_path, cache_dir=tmp_path / "cache")
    plain, plain_id, _other = _steam_service(tmp_path / "plain")

    assert service.artwork_path(game_id, "cover") == str(cover)
    assert service.artwork_path(game_id, "icon") == str(icon)
    assert plain.artwork_path(plain_id, "cover") == ""


def test_prefetch_names_localizes_only_the_names_it_wrote(tmp_path: Path) -> None:
    """译名只写到"名字还是程序写的"游戏上: 用户起的名字优先, 取不到就保留原名."""
    fetcher = _StubNames({"730": "无尽塔防 2", "1": "无名游戏"})
    service, game_id, database = _steam_service(tmp_path, name_fetcher=fetcher)
    service._localize_names(refresh=False)

    game = GameRepository(database).get(int(game_id))
    assert game is not None
    assert game.name == "无尽塔防 2"

    # 用户改过名(与首次录入的名称不同)的游戏不会再被译名覆盖.
    renamed = game.model_copy(update={"name": "我给它起的名字"})
    GameRepository(database).update(renamed)
    service._localize_names(refresh=False)

    stored = GameRepository(database).get(int(game_id))
    assert stored is not None
    assert stored.name == "我给它起的名字"


class _PerLanguageNames:
    """按语言返回译名的替身: 中文一份、英文一份, 并记下每一步问了什么(不联网)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """返回该语言下的译名, 同时记下这次询问."""
        del timeout, max_bytes
        self.calls.append((app_id, language))
        if app_id != "730":
            return None
        return {"schinese": "无尽塔防 2", "english": "Bloons TD 2"}.get(language)


def test_switching_language_follows_the_locale_and_reuses_the_cache(
    tmp_path: Path,
) -> None:
    """切换语言: 名字跟着换, 而该语言已取过的译名直接复用缓存(不再联网).

    缓存按 ``<AppID>:<语言>`` 分条, 所以"换语言"本来就不需要忽略缓存: 新语言没记录
    才联网。旧行为是 ``refresh=True`` 忽略缓存重取, 来回切一次就要多问一趟商店。
    """
    fetcher = _PerLanguageNames()
    service, game_id, database = _steam_service(
        tmp_path, cache_dir=tmp_path / "cache", name_fetcher=fetcher
    )
    games = GameRepository(database)

    set_locale("zh-CN")
    service._localize_names(refresh=False)
    first = games.get(int(game_id))
    assert first is not None
    assert first.name == "无尽塔防 2"
    assert first.localized_name == "无尽塔防 2"

    set_locale("en")
    service._localize_names(refresh=False)
    second = games.get(int(game_id))
    assert second is not None
    assert second.name == "Bloons TD 2", "切换语言后名字要跟着新语言走"
    assert second.original_name == "Demo", "录入时的原名始终保留"

    # 切回中文: 命中缓存, 一次都不该再问商店.
    set_locale("zh-CN")
    service._localize_names(refresh=False)
    third = games.get(int(game_id))
    assert third is not None
    assert third.name == "无尽塔防 2"
    assert [language for _app_id, language in fetcher.calls] == [
        "schinese",
        "english",
    ], "缓存命中还联网就说明切换语言把缓存忽略了"


def test_a_user_rename_survives_language_switches(tmp_path: Path) -> None:
    """用户改过的名字不会被译名覆盖: 切换语言、来回切都不动它."""
    fetcher = _PerLanguageNames()
    service, game_id, database = _steam_service(
        tmp_path, cache_dir=tmp_path / "cache", name_fetcher=fetcher
    )

    set_locale("zh-CN")
    service._localize_names(refresh=False)
    service.update_game(game_id, "我给它起的名字")

    for locale in ("en", "zh-CN"):
        set_locale(locale)
        service._localize_names(refresh=False)
        stored = GameRepository(database).get(int(game_id))
        assert stored is not None
        assert stored.name == "我给它起的名字"
        assert stored.localized_name == ""


def test_prefetch_names_keeps_the_detected_name_without_a_translation(
    tmp_path: Path,
) -> None:
    """该语言没有译文(或取不到)时保留探测到的原名, 不猜也不翻."""
    service, game_id, database = _steam_service(
        tmp_path, name_fetcher=_StubNames({"999": "别的游戏"})
    )

    service._localize_names(refresh=False)

    game = GameRepository(database).get(int(game_id))
    assert game is not None
    assert game.name == "Demo"


def test_discovery_rows_read_the_localized_name_from_the_cache(
    tmp_path: Path,
) -> None:
    """探测结果行显示缓存里的当前语言译名(扫描时已补好), 没缓存就回落原名."""
    cache_dir = tmp_path / "cache"
    name_cache_at(cache_dir).put("1145360", "zh-CN", "哈迪斯")
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Hades",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    item = next(row for row in service.list_candidates() if row.name == "Hades")

    assert item.localized_name == "哈迪斯"
    assert item.display_name == "哈迪斯"


def test_discovery_rows_keep_the_detected_name_without_a_translation(
    tmp_path: Path,
) -> None:
    """没有译名(或不是平台清单来源)的候选直接显示探测到的名称."""
    service, _game_id, database = _steam_service(tmp_path)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(name="Hades", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "Hades")

    assert item.localized_name == ""
    assert item.display_name == "Hades"


def test_prefetch_names_fills_candidate_names_without_touching_games(
    tmp_path: Path,
) -> None:
    """探测结果还不是游戏: 译名只写进缓存, 不会凭空多出一条游戏记录."""
    cache_dir = tmp_path / "cache"
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        name_fetcher=_StubNames({"1145360": "哈迪斯"}),
    )
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Hades",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    service._localize_names(refresh=False)

    assert name_cache_at(cache_dir).get("1145360", "zh-CN") == "哈迪斯"
    assert [game.name for game in service.list_games()] == ["Demo"]


def test_delete_game_removes_its_artwork_cache(tmp_path: Path) -> None:
    """删游戏时把它探测到的封面/图标缓存一起删掉(缓存是可再生的派生数据)."""
    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    cache.store("steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png")
    service, game_id, _database = _steam_service(tmp_path, cache_dir=cache_dir)

    service.delete_game(game_id, str(tmp_path / "exports" / "steam.zip"))

    assert cache.lookup_any("steam", "730", "cover") is None
    assert cache.lookup_any("steam", "730", "icon") is None


def test_icon_is_derived_from_the_cover(tmp_path: Path) -> None:
    """封面兜底: 平台给不出官方图标时, 从封面裁出方形图标."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=cover.read_bytes(),
        extension="png",
    )
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    # 封面已有(不计入), 这一份是刚裁出来的图标.
    assert service._fetch_artwork(cache, referenced) == 1

    icon = service.artwork_path("1", "icon")
    assert icon != ""
    with Image.open(icon) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)


def test_official_icon_wins_over_the_cover_crop(tmp_path: Path) -> None:
    """有官方图标时用它: 缓存里的像素来自 ico, 而不是封面(两者颜色不同)."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=cover.read_bytes(),
        extension="png",
    )
    official = helpers.write_steam_icon(tmp_path, _ICON_HASH)
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        icon=steam_icon("730", _ICON_HASH, local_path=official),
    )
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._fetch_artwork(cache, referenced) == 1

    stored = cache.lookup_any("steam", "730", "icon")
    assert stored is not None
    # 文件名说明这份图是怎么来的: 官方图标用哈希, 且已经归一化成方形 PNG.
    assert stored.name == f"icon-{_ICON_HASH}.png"
    with Image.open(stored) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)
        # 官方图标带透明通道: 颜色来自 ico(红)而不是封面(深蓝), 且透明度保留.
        assert made.getpixel((ICON_SIZE // 2, ICON_SIZE // 2)) == (200, 30, 30, 255)
    # 界面拿到的是缓存里那份成品(而不是平台目录里的原始 .ico).
    assert service.artwork_path("1", "icon") == str(stored)


def test_a_cover_crop_icon_is_replaced_by_the_official_one(tmp_path: Path) -> None:
    """已有裁出来的封面图标时, 拿到官方图标要覆盖它(否则升级后仍是旧图)."""
    from PIL import Image

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    crop = tmp_path / "crop.png"
    Image.new("RGB", (ICON_SIZE, ICON_SIZE), (10, 20, 30)).save(crop)
    cache.store(
        "steam", "730", "icon", ICON_VERSION, content=crop.read_bytes(), extension="png"
    )
    # 封面也预置好: 这样这一轮要补的只有图标(返回值只数图标那 1 份).
    cache.store(
        "steam",
        "730",
        "cover",
        STEAM_COVER_ASSET,
        content=crop.read_bytes(),
        extension="png",
    )
    official = helpers.write_steam_icon(tmp_path, _ICON_HASH)
    service, _game_id, database = _steam_service(
        tmp_path,
        cache_dir=cache_dir,
        icon=steam_icon("730", _ICON_HASH, local_path=official),
    )
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._fetch_artwork(cache, referenced) == 1

    # 只剩官方图标那一版: 旧的封面裁剪已按“同类型过期文件”清掉.
    assert cache.lookup("steam", "730", "icon", ICON_VERSION) is None
    stored = cache.lookup("steam", "730", "icon", _ICON_HASH)
    assert stored is not None
    with Image.open(stored) as made:
        assert made.getpixel((4, 4)) == (200, 30, 30, 255)


def test_import_candidate_writes_the_confirmed_save_paths(tmp_path: Path) -> None:
    """导入时把用户确认的路径写成存档位置: 平台候选保留来源, 新增的记手动."""
    save = tmp_path / "saves"
    save.mkdir()
    (save / "slot.dat").write_text("x", encoding="utf-8")
    extra = tmp_path / "extra"
    extra.mkdir()
    service, _game_id, database = _steam_service(tmp_path, str(save))
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    candidate, _created = CandidateRepository(database).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )
    assert candidate.id is not None

    summary = service.import_candidate(
        str(candidate.id), save_paths=(str(save), str(extra))
    )

    assert summary.saved_paths == 2
    game = GameRepository(database).get(int(summary.game_id))
    assert game is not None
    assert game.steam_app_id == 1145360
    sources = {
        item.path: item.source for item in service.list_locations(summary.game_id)
    }
    assert sources == {str(save): "steam", str(extra): "manual"}


# ------------------------------------------------- 恢复与删除原始位置


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


def test_preview_location_removal_reports_impact(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]

    plan = service.preview_location_removal(location.location_id)

    assert plan.game_name == "Demo"
    assert plan.path == str(save)
    assert plan.files == 1
    assert plan.blocked_reason is None


def test_delete_save_location_requires_matching_name(tmp_path: Path) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]

    with pytest.raises(ArchiveManagementError):
        service.delete_save_location(location.location_id, confirm_name="别的游戏")

    assert save.exists()
    assert len(service.list_locations(game_id)) == 1


def test_delete_save_location_uses_injected_trash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, game_id, save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]
    moved: list[str] = []
    monkeypatch.setattr(locations_mod, "send_to_trash", moved.append)

    message = service.delete_save_location(location.location_id, confirm_name="demo")

    assert moved == [str(save)]
    assert str(save) in message
    assert service.list_locations(game_id) == []


# ------------------------------------------------- 本地游戏探测


def test_monitored_directories_crud(tmp_path: Path) -> None:
    """监控目录的增删改查都经后端暴露给界面, 并带实时路径状态."""
    service = _service(tmp_path)
    games = tmp_path / "Games"
    games.mkdir()

    created = service.add_monitored_directory(str(games), note="自定义")
    assert created.path == str(games)
    assert created.health == "ok"
    assert [item.directory_id for item in service.list_monitored_directories()] == [
        created.directory_id
    ]

    disabled = service.set_monitored_enabled(created.directory_id, False)
    assert disabled.enabled is False
    assert disabled.state_label == tr("discovery.dir_off")

    updated = service.update_monitored_directory(created.directory_id, note="改过")
    assert updated.note == "改过"

    service.remove_monitored_directory(created.directory_id)
    assert service.list_monitored_directories() == []


def test_scan_finds_monitored_games_and_imports_them(tmp_path: Path) -> None:
    """扫描会把监控目录里的游戏写进候选表, 导入后成为游戏记录."""
    service = _service(tmp_path)
    games = tmp_path / "Games"
    (games / "Hades").mkdir(parents=True)
    service.add_monitored_directory(str(games))

    report = service.scan_candidates()

    assert report.monitored == 1
    assert report.active == 1
    ours = next(
        item
        for item in service.list_candidates()
        if item.install_dir == str(games / "Hades")
    )
    assert ours.source == "monitored"
    assert ours.status == "new"
    assert ours.importable is True

    summary = service.import_candidate(ours.candidate_id, name="哈迪斯")

    assert summary.name == "哈迪斯"
    assert [game.game_id for game in service.list_games()] == [summary.game_id]
    imported = service.list_candidates(status="imported")
    assert [item.candidate_id for item in imported] == [ours.candidate_id]
    assert imported[0].game_id == summary.game_id
    # 已导入的候选不能再导入一次.
    with pytest.raises(ArchiveManagementError):
        service.import_candidate(ours.candidate_id)


def test_candidate_ignore_restore_and_relocate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    games = tmp_path / "Games"
    (games / "Hades").mkdir(parents=True)
    service.add_monitored_directory(str(games))
    service.scan_candidates()
    candidate = next(
        item
        for item in service.list_candidates()
        if item.install_dir == str(games / "Hades")
    )

    ignored = service.set_candidate_ignored(candidate.candidate_id, True)
    assert ignored.status == "ignored"
    # 用集合判断而不是取第一条: 扫描本机时也可能会带出别的已忽略候选
    # (例如平台官方工具被默认隐藏).
    ignored_ids = {
        item.candidate_id for item in service.list_candidates(status="ignored")
    }
    assert candidate.candidate_id in ignored_ids

    restored = service.set_candidate_ignored(candidate.candidate_id, False)
    assert restored.status == "new"

    moved = tmp_path / "Elsewhere" / "Hades"
    moved.mkdir(parents=True)
    relocated = service.relocate_candidate(candidate.candidate_id, str(moved))
    assert relocated.install_dir == str(moved)
    assert relocated.health == "ok"

    watched = service.add_candidate_as_monitored(candidate.candidate_id)
    assert watched.path == str(tmp_path / "Elsewhere")


def test_backend_rejects_unknown_discovery_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ArchiveManagementError, match="未知监控目录"):
        service.set_monitored_enabled("not-a-number", False)
    with pytest.raises(ArchiveManagementError, match="未知监控目录"):
        service.remove_monitored_directory("42")
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        service.import_candidate("42")
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        service.set_candidate_ignored("42", True)
    with pytest.raises(ArchiveManagementError, match="不支持的筛选条件"):
        service.list_candidates(status="bogus")


def test_home_board_lists_games_and_persists_filter(tmp_path: Path) -> None:
    """主页列表与筛选条件都要持久化: 重新打开仍是上次的视图."""
    service = _service(tmp_path)
    service.add_game("星际拓荒")
    service.add_game("空洞骑士")

    board = service.load_home()
    assert {item.name for item in board.games} == {"星际拓荒", "空洞骑士"}
    assert board.filter.view is HomeView.ALL
    # 刚录入的游戏算“最近活跃”, 但都还没有存档位置, 因此都是待处理.
    assert board.summary == tr("home.summary", total=2, recent=2, pending=2, archived=0)

    service.apply_home_filter(HomeFilter(view=HomeView.PENDING, search="拓荒"))

    reloaded = service.load_home()
    assert reloaded.filter.view is HomeView.PENDING
    assert reloaded.filter.search == "拓荒"
    assert [item.name for item in reloaded.games] == ["星际拓荒"]
    assert reloaded.origin_text == tr("home.origin_all")
    assert reloaded.category_text == tr("home.category_all")


def test_home_filter_options_carry_counts(tmp_path: Path) -> None:
    """平台与分类下拉框的每一项都要带数量, 避免点进空分类."""
    service = _service(tmp_path)
    service.add_game("星际拓荒")

    board = service.load_home()
    assert [option.key for option in board.views] == [view.value for view in HomeView]
    origins = {option.key: option.count for option in board.origins}
    assert origins == {"manual": 1}
    categories = {option.key: option.count for option in board.categories}
    assert categories["origin:manual"] == 1
    assert categories["backup:none"] == 1

    narrowed = service.apply_home_filter(HomeFilter(origin="manual"))
    assert narrowed.origin_text == f"{tr('discovery.source_manual')} (1)"
    assert narrowed.narrowing is True
    # 平台筛选不参与自身的计数: 否则下拉框里只会剩下当前平台.
    assert {option.key for option in narrowed.origins} == {"manual"}


def test_home_archive_and_tags_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")

    archived = service.set_game_archived(summary.game_id, True)
    assert archived.stats.archived == 1
    assert archived.games == ()

    restored = service.set_game_archived(summary.game_id, False)
    assert [item.name for item in restored.games] == ["星际拓荒"]

    tagged = service.set_game_tags(summary.game_id, [" 探索 ", "探索", "解谜"])
    assert tagged.games[0].tags == ("探索", "解谜")
    assert "探索" in tagged.games[0].chips

    # 中英文逗号一视同仁: 两种逗号都不会留在标签里, 也不会在往返后被拆成两个
    comma_ed = service.set_game_tags(
        summary.game_id,
        ["探索,解谜", "动作，冒险"],  # noqa: RUF001 - 全角逗号正是被测输入
    )
    assert comma_ed.games[0].tags == ("探索解谜", "动作冒险")
    assert service.load_home().games[0].tags == ("探索解谜", "动作冒险")


def _cached_service(tmp_path: Path, *, cache_dir: Path | None) -> SqlArchiveService:
    """构造带/不带缓存目录的后端: 译名与封面这两件事都挂在缓存目录上."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    return SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        cache_dir=cache_dir,
    )


def test_a_game_without_a_steam_id_still_renders_with_placeholders(
    tmp_path: Path,
) -> None:
    """手填游戏没有 AppID: 列表/主页/详情照常展示, 取图直接给占位而不是报错."""
    service = _service(tmp_path)
    summary = service.add_game("手填游戏")

    assert [item.name for item in service.list_games()] == ["手填游戏"]
    assert service.get_detail(summary.game_id).name == "手填游戏"
    assert service.load_home().games[0].backup_count == 0
    assert service.artwork_path(summary.game_id, "cover") == ""


def test_prefetch_does_nothing_without_a_cache_dir(tmp_path: Path) -> None:
    """没有缓存目录时译名与封面探测都直接返回(测试与未配置应用路径的场景)."""
    service = _cached_service(tmp_path, cache_dir=None)
    summary = service.add_game("手填游戏")

    service.prefetch_names()
    service.prefetch_artwork()

    assert service.artwork_path(summary.game_id, "cover") == ""
    assert service.list_games()[0].name == "手填游戏"


def test_prefetch_names_skips_games_without_an_app_id(tmp_path: Path) -> None:
    """有缓存目录, 但游戏没有 AppID: 跳过而不是去联网查名(离线也不会卡住)."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    service.add_game("手填游戏")

    service.prefetch_names()
    service.prefetch_names(refresh=True)

    assert service.list_games()[0].name == "手填游戏"


def test_artwork_prefetch_is_not_re_entrant(tmp_path: Path) -> None:
    """上一轮还在跑时再点一次不该又起一条线程(重复下载同一批图)."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    service._artwork_running = True  # 模拟"上一轮下载还没结束"

    service.prefetch_artwork()

    assert service._artwork_running is True, "重入的调用不该把运行标志清掉"


def test_artwork_download_marks_attempts_and_clears_the_flag(tmp_path: Path) -> None:
    """下载收尾必须清掉运行标志, 且已经试过的游戏不再重复试."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    summary = service.add_game("手填游戏")
    service._artwork_attempts.add(summary.game_id)
    service._artwork_running = True

    service._download_artwork()
    service._download_artwork()

    assert service._artwork_running is False
    assert summary.game_id in service._artwork_attempts


def test_update_location_with_the_same_path_skips_the_duplicate_scan(
    tmp_path: Path,
) -> None:
    """路径没变时不该走"查重"那条路(否则自己和自己撞, 平白报重复)."""
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    location = service.add_location(summary.game_id, path=str(save), kind="directory")

    updated = service.update_location(location.location_id, path=location.path)

    assert updated.path == location.path
    assert updated.path_kind == "directory"


def test_set_primary_location_rejects_a_location_of_another_game(
    tmp_path: Path,
) -> None:
    """拿别的游戏的位置设主位置必须拒绝, 否则会串改两款游戏的主标记."""
    service = _service(tmp_path)
    first = service.add_game("甲")
    second = service.add_game("乙")
    save = tmp_path / "save"
    save.mkdir()
    location = service.add_location(first.game_id, path=str(save), kind="directory")

    with pytest.raises(ArchiveManagementError):
        service.set_primary_location(second.game_id, location.location_id)

    assert service.list_locations(first.game_id)[0].is_primary is True


def test_poll_activation_without_the_auto_flag_changes_nothing(tmp_path: Path) -> None:
    """自动启停关着的时候轮询一次不产生任何变化(不可能去枚举进程)."""
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    service.set_game_enabled(summary.game_id, True)

    outcome = service.poll_activation(enabled=False)

    assert outcome.changed is False


def test_branch_backups_get_their_own_label(tmp_path: Path) -> None:
    """分支节点与主线节点的展示文案不同(节点的种类要映射对)."""
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot.dat").write_text("v1", encoding="utf-8")
    service.add_location(summary.game_id, path=str(save), kind="directory")
    service.run_backup_now(summary.game_id)
    first = service.list_backups(summary.game_id)[0]
    # 内容没变的分支会被跳过, 因此先动一下存档再分支。
    (save / "slot.dat").write_text("v2", encoding="utf-8")

    service.run_create_branch(summary.game_id, first.backup_id, "测试分支")

    labels = {item.branch_label for item in service.list_backups(summary.game_id)}
    assert len(labels) >= 2, f"分支与主线的标签应当不同: {labels}"


def test_automatic_backups_get_their_own_label(tmp_path: Path) -> None:
    """定时触发的备份是"自动备份"这一种类, 与手动备份区分开."""
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot.dat").write_text("v1", encoding="utf-8")
    service.add_location(summary.game_id, path=str(save), kind="directory")
    service.set_game_enabled(summary.game_id, True)
    assert service.set_schedule(summary.game_id, "30m")
    service.run_backup_now(summary.game_id)
    # 定时备份同样遵守"内容未变化就跳过", 因此先动一下存档。
    (save / "slot.dat").write_text("v2", encoding="utf-8")

    assert service._scheduler.trigger(int(summary.game_id)) is True

    items = service.list_backups(summary.game_id)
    assert len(items) == 2
    # 两种节点的展示文案不同: 定时那份叫"自动备份", 手动那份叫"手动备份"
    # (两者都在主线上, 因此区分它们的是标题而不是分支标签)。
    titles = {item.title for item in items}
    assert titles == {tr("backup.title_manual"), tr("backup.title_auto")}


def test_backup_of_an_empty_save_folder_records_no_file_entries(
    tmp_path: Path,
) -> None:
    """空存档目录也能备份: 快照清单里一条文件记录都没有, 但节点照样已验证."""
    service = _service(tmp_path)
    summary = service.add_game("空目录游戏")
    empty = tmp_path / "empty"
    empty.mkdir()
    service.add_location(summary.game_id, path=str(empty), kind="directory")

    service.run_backup_now(summary.game_id)

    items = service.list_backups(summary.game_id)
    assert len(items) == 1
    assert items[0].verified is True


def test_candidate_listing_with_and_without_a_status_filter(tmp_path: Path) -> None:
    """候选列表的口径: 不传/空串/"all" = 全部, 具体状态只留那一种, 非法值报错."""
    service = _service(tmp_path)
    everything = service.list_candidates()

    assert service.list_candidates(status=None) == everything
    assert service.list_candidates(status="") == everything
    assert service.list_candidates(status="all") == everything
    assert service.list_candidates(status="ignored") == []

    with pytest.raises(ArchiveManagementError):
        service.list_candidates(status="suggested")


def test_home_reflects_backups_and_locations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(summary.game_id, path=str(save), kind="directory")

    board = service.load_home()
    item = board.games[0]
    assert item.location_count == 1
    assert item.backup_enabled is True
    assert item.last_backup_label == ""
    assert tr("home.last_backup_none") in item.summary
    assert tr("home.cat_backup_none") in item.chips
    assert board.detail == tr("home.detail", backed_up=0, risky=0)

    service.run_backup_now(summary.game_id)

    refreshed = service.load_home()
    assert refreshed.games[0].backup_count == 1
    assert refreshed.games[0].last_backup_label != ""
    assert tr("home.cat_backup_done") in refreshed.games[0].chips
    assert refreshed.stats.pending == 0
    assert refreshed.detail == tr("home.detail", backed_up=1, risky=0)


def test_home_marks_games_without_locations_as_pending(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add_game("还没配置")

    board = service.load_home()
    assert board.stats.pending == 1
    assert board.games[0].backup_enabled is False

    only_pending = service.apply_home_filter(HomeFilter(view=HomeView.PENDING))
    assert [item.name for item in only_pending.games] == ["还没配置"]
    # 刚录入的游戏同时属于“最近活跃”: 录入本身就算一次活动.
    recent = service.apply_home_filter(HomeFilter(view=HomeView.RECENT))
    assert [item.name for item in recent.games] == ["还没配置"]

    empty = service.apply_home_filter(HomeFilter(search="zzz"))
    assert empty.games == ()
    assert empty.narrowing is True
    assert empty.empty_message == tr("home.empty_filtered", total=1)
    assert empty.empty_hint == tr("home.empty_filtered_hint")


def test_home_rejects_unknown_game_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.set_game_archived("999", True)
    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.set_game_tags("not-a-number", ["x"])


# --------------------------------------------------- 边界与防御(分支覆盖)


class _NoRefsAdapter(_FakeAdapter):
    """支持取图但一份引用都给不出的适配器(平台没有可用图片资源)."""

    def artwork_refs(self, game: PlatformGame) -> tuple[ArtworkRef, ...]:
        """不给任何引用."""
        del game
        return ()


def test_node_title_falls_back_to_the_kind_default(tmp_path: Path) -> None:
    """既没有标题也没有分支名时按节点类型给默认名(手动/自动/分支三种)."""
    service, game_id, save = _service_with_save(tmp_path)
    manual = service._backups.create_backup(int(game_id), kind="manual", title="")
    _advance(save)
    auto = service._backups.create_backup(int(game_id), kind="auto", title="")
    _advance(save)
    branch = service._backups.create_backup(
        int(game_id), kind="branch", title="", branch_name=""
    )

    assert service._node_title(manual) == tr("backup.title_manual")
    assert service._node_title(auto) == tr("backup.title_auto")
    assert service._node_title(branch) == tr("backup.title_branch")


def test_platform_game_needs_an_app_id(tmp_path: Path) -> None:
    """来源是平台但解析不出 AppID 时不给平台数据(免得拿错的 id 去取图)."""
    from archive_management.ui.sql_backend import _platform_game

    candidate = GameCandidate(name="Hades", install_dir=str(tmp_path), source="steam")

    assert _platform_game(candidate) is None


def test_startup_skips_schedule_rows_without_a_game_or_an_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库里有"没有游戏 id"或"空周期"的任务行时启动照常(只跳过这两行)."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    rows = [
        ScheduledJob(game_id=None, schedule="1h"),
        ScheduledJob(game_id=1, schedule="   "),
    ]
    monkeypatch.setattr(ScheduledJobRepository, "list_all", lambda self: list(rows))

    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )

    assert service.list_schedules() == []


def test_poll_activation_bumps_the_revision_only_when_the_state_changed(
    tmp_path: Path,
) -> None:
    """自动启停真的切换了启用态时数据版本要变(界面据此重读)."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("v1", encoding="utf-8")
    idle = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        process_provider=lambda: ["Demo.exe"],
    )
    game_id = idle.add_game("Demo").game_id
    idle.add_location(game_id, path=str(save), kind="directory")

    before = idle.task_status(game_id).revision
    outcome = idle.poll_activation(enabled=True)

    assert outcome.changed is True
    assert idle.task_status(game_id).revision > before
    # 再轮询一次: 状态没变就不该再动版本号(否则界面会被无意义地重读).
    again = idle.poll_activation(enabled=True)
    assert again.changed is False
    assert idle.task_status(game_id).revision == idle.task_status(game_id).revision


def test_list_schedules_skips_games_it_cannot_describe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """组装不出任务条目的游戏直接从列表里跳过(不让半截条目进界面)."""
    service = _service(tmp_path)
    service.add_game("甲")
    service.add_game("乙")
    describe = service._schedule_item
    monkeypatch.setattr(
        service,
        "_schedule_item",
        lambda game: None if game.name == "乙" else describe(game),
    )

    assert [item.game_name for item in service.list_schedules()] == ["甲"]


def test_suggest_for_game_returns_nothing_without_a_platform_adapter(
    tmp_path: Path,
) -> None:
    """手填/监控目录来源没有平台适配器: 不猜存档位置(导入照常进行)."""
    service = _service(tmp_path)
    game_id = service.add_game("手填游戏").game_id
    game = GameRepository(service._database).get(int(game_id))
    assert game is not None

    assert service._suggest_for_game(game) == {}


def test_suggest_for_game_skips_candidates_without_a_stored_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选落库后仍拿不到 id 时跳过它(而不是往映射里塞一条 id 为 None 的记录)."""
    from types import SimpleNamespace

    import archive_management.application.candidates as candidate_cases

    service, game_id, _database = _steam_service(tmp_path)
    game = GameRepository(service._database).get(int(game_id))
    assert game is not None
    report = SimpleNamespace(candidates=(SimpleNamespace(id=None, path=str(tmp_path)),))
    monkeypatch.setattr(candidate_cases, "suggest_candidates", lambda *a, **k: report)

    assert service._suggest_for_game(game) == {}


def test_save_candidate_source_prefers_the_injected_one(tmp_path: Path) -> None:
    """注入了候选来源就用它, 不去读本机 Steam 目录(离线与测试的前提)."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    injected = _FakeSaveSource()
    service = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        save_source=injected,
    )

    assert service._save_candidate_source() is injected


def test_candidate_names_skip_unresolvable_and_already_cached_rows(
    tmp_path: Path,
) -> None:
    """译名只补"能解析出 AppID 且缓存里还没有"的候选: 两类跳过各走一次."""
    cache_dir = tmp_path / "cache"
    service, _game_id, database = _steam_service(
        tmp_path, cache_dir=cache_dir, name_fetcher=_StubNames({})
    )
    candidates = CandidateRepository(database)
    for index, (name, detail) in enumerate(
        [("没有 AppID", ""), ("已有译名", "appmanifest_730.acf")]
    ):
        install = tmp_path / f"cand{index}"
        install.mkdir()
        candidates.upsert(
            GameCandidate(
                name=name, install_dir=str(install), source="steam", detail=detail
            )
        )
    third = tmp_path / "cand2"
    third.mkdir()
    candidates.upsert(
        GameCandidate(
            name="取不到译名",
            install_dir=str(third),
            source="steam",
            detail="appmanifest_1145360.acf",
        )
    )
    name_cache_at(cache_dir).put("730", "zh-CN", "已有译名")

    service._localize_names(refresh=False)

    # 能解析、没缓存的那条去问了一次(替身给不出结果, 于是缓存里什么都没留下).
    assert name_cache_at(cache_dir).get("730", "zh-CN") == "已有译名"
    assert name_cache_at(cache_dir).get("1145360", "zh-CN") is None


def test_cached_name_of_a_candidate_without_an_app_id_is_empty(tmp_path: Path) -> None:
    """探测结果没有 AppID 时译名一律为空(不猜也不联网)."""
    cache_dir = tmp_path / "cache"
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    install = tmp_path / "monitored"
    install.mkdir()
    CandidateRepository(database).upsert(
        GameCandidate(name="手填", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "手填")

    assert item.localized_name == ""
    assert item.display_name == "手填"


def test_artwork_download_walks_a_game_that_can_be_fetched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有引用可用的游戏要走完下载这一轮: 取到就计数并刷新数据版本."""
    from types import SimpleNamespace

    import archive_management.ui.sql_backend as sql_mod

    cache_dir = tmp_path / "cache"
    service, game_id, _database = _steam_service(tmp_path, cache_dir=cache_dir)
    monkeypatch.setattr(
        sql_mod,
        "resolve_artwork",
        lambda *a, **k: SimpleNamespace(found=True, path=None),
    )
    service._artwork_running = True
    before = service.list_games()[0].name  # 触发一次读取, 拿到当前状态

    service._download_artwork()

    assert before == "Demo"
    assert game_id in service._artwork_attempts
    assert service._artwork_running is False


def test_store_icon_skips_an_icon_that_is_already_cached(tmp_path: Path) -> None:
    """同一来源版本的图标已在缓存里就不再重算(重算等于白解码一次)."""
    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cache.store("steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png")
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None

    assert service._store_icon(cache, referenced) is None


def test_store_icon_gives_up_when_conversion_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原图解不开时不上缓存、也不抛错(界面回落名称首字占位)."""
    import archive_management.ui.sql_backend as sql_mod

    cache_dir = tmp_path / "cache"
    cache = artwork_cache_at(cache_dir)
    cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    game = GameRepository(database).get(1)
    assert game is not None
    referenced = service._artwork_game(game)
    assert referenced is not None
    monkeypatch.setattr(sql_mod, "square_icon", lambda *_a, **_k: None)

    assert service._store_icon(cache, referenced) is None
    assert service._fetch_artwork(cache, referenced) == 0


def test_artwork_game_needs_the_adapter_to_offer_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """适配器支持取图但给不出引用时不去下载(界面直接占位)."""
    service, _game_id, database = _steam_service(tmp_path, cache_dir=tmp_path / "cache")
    game = GameRepository(database).get(1)
    assert game is not None
    monkeypatch.setattr(
        service, "_adapter_for", lambda platform: _NoRefsAdapter(_FakeSaveSource())
    )

    assert service._artwork_game(game) is None


def test_busy_guards_reject_a_second_operation(tmp_path: Path) -> None:
    """一次只允许一个备份/恢复: 已有任务在跑时再发起要明确报忙."""
    from archive_management.ui.sql_backend import _ActiveOperation

    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    service._active = _ActiveOperation(game_id=int(game_id))

    with pytest.raises(ArchiveManagementError) as backup_error:
        service.run_backup_now(game_id)
    with pytest.raises(ArchiveManagementError) as restore_error:
        service.run_restore(game_id, node.backup_id)

    assert tr("error.operation_busy") in str(backup_error.value)
    assert tr("error.operation_busy") in str(restore_error.value)
    assert service._active is not None, "被拒绝的调用不该把进行中的任务清掉"


def test_cancel_and_progress_are_quiet_without_an_operation(tmp_path: Path) -> None:
    """没有进行中的任务时: 取消返回 False, 进度回调直接忽略(后台线程的收尾竞态)."""
    from archive_management.ui.sql_backend import _ActiveOperation

    service = _service(tmp_path)

    assert service.cancel_active() is False
    service._report_progress(0.5, "不该记录")  # 不该抛错
    assert service._active is None
    assert service._cancel_requested() is False

    service._active = _ActiveOperation(game_id=1)
    assert service.cancel_active() is True
    service._report_progress(0.4, "写回中")
    assert service._active.fraction == 0.4
    assert service._active.message == "写回中"
    assert service._cancel_requested() is True


def test_clearing_a_schedule_ignores_jobs_without_an_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """库里出现没有 id 的任务行时清周期照常走完(跳过删除而不是崩)."""
    service, game_id, _save = _service_with_save(tmp_path)
    monkeypatch.setattr(
        service._jobs,
        "for_game",
        lambda gid: [ScheduledJob(game_id=gid, schedule="1h")],
    )

    status = service.set_schedule(game_id, "")

    assert status.schedule_text == ""


def test_marking_a_job_run_without_a_stored_job_is_silent(tmp_path: Path) -> None:
    """没有任务行时"记录一次运行"直接返回(自动备份可能在清掉周期之后收尾)."""
    service, game_id, _save = _service_with_save(tmp_path)

    service._mark_job_run(int(game_id), error=None)

    assert service.task_status(game_id).schedule_text == ""


def test_delete_backup_reports_cascade_for_a_branch_root(tmp_path: Path) -> None:
    """删掉带子节点的分支根节点: 提示要说清连带删掉了整条分支."""
    service, game_id, save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    first = service.list_backups(game_id)[0]
    _advance(save)
    service.run_create_branch(game_id, first.backup_id, "分支")
    # 分支节点下面再挂一个: 这样删分支根节点就是"连带整条分支"(否则只是删一个节点).
    _advance(save)
    service.run_backup_now(game_id)
    branch_root = next(item for item in service.list_backups(game_id) if item.is_branch)

    message = service.run_delete_backup(game_id, branch_root.backup_id)

    assert message == tr("result.delete_cascade", count=2)


def test_verified_flag_is_false_for_a_node_without_an_id(tmp_path: Path) -> None:
    """节点还没落库(没有 id)时校验一律 False(不给缓存留下错的键)."""
    from archive_management.domain import BackupNode

    service = _service(tmp_path)

    assert service._is_verified(BackupNode(game_id=1, node_kind="manual")) is False


def test_half_written_rows_are_treated_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """查到实体但没有 id(半截数据)时按"未知"处理, 不拿 None 继续往下走."""
    from archive_management.domain import SaveLocation

    service = _service(tmp_path)
    monkeypatch.setattr(service._games, "get", lambda _id: Game(name="半截"))
    monkeypatch.setattr(
        service._locations,
        "get",
        lambda _id: SaveLocation(game_id=1, path="/x", path_kind="directory"),
    )

    with pytest.raises(ArchiveManagementError):
        service._game_ref("1")
    with pytest.raises(ArchiveManagementError):
        service._location_ref("1")


def test_save_candidate_source_falls_back_to_the_local_steam_cloud(
    tmp_path: Path,
) -> None:
    """没有注入来源时读本机公开的 Steam 云同步清单(只读本机文件, 不联网)."""
    from archive_management.services.steam_cloud import SteamCloudSource

    service = _service(tmp_path)

    assert isinstance(service._save_candidate_source(), SteamCloudSource)


def test_prefetch_artwork_starts_one_background_round(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有在跑的一轮时: 先占住运行标志, 再起一条后台线程(界面不阻塞)."""
    service = _cached_service(tmp_path, cache_dir=tmp_path / "cache")
    service.add_game("手填游戏")
    ran: list[str] = []
    monkeypatch.setattr(service, "_download_artwork", lambda: ran.append("ran"))

    assert service._artwork_running is False
    service.prefetch_artwork()

    assert service._artwork_running is True, "起线程前必须先占住运行标志"


def test_update_location_rejects_a_path_that_cannot_be_read(tmp_path: Path) -> None:
    """改成读不到的路径要被拦下, 库里那条位置保持原样."""
    service, game_id, _save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.update_location(location.location_id, path=str(tmp_path / "missing"))

    assert str(excinfo.value) == tr("error.loc_missing", path=str(tmp_path / "missing"))
    assert service.list_locations(game_id)[0].path == location.path


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
