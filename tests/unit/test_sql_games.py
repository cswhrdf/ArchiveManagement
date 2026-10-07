"""
游戏档案 CRUD 与删除导出: 改名/启停/删除级联、删除前导出包与失败回滚、未知游戏守卫。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

from datetime import UTC, datetime, tzinfo
from pathlib import Path

import pytest

import archive_management.ui.sql_backend as sql_mod
from archive_management.domain import (
    GameCandidate,
)
from archive_management.exceptions import (
    ArchiveManagementError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.export_format import (
    ARCHIVE_SUFFIX,
    read_package,
)
from archive_management.ui.models import (
    export_batch_prompt,
    filter_export_options,
)

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _advance,
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


def test_platform_game_needs_an_app_id(tmp_path: Path) -> None:
    """来源是平台但解析不出 AppID 时不给平台数据(免得拿错的 id 去取图)."""
    from archive_management.ui.sql_backend import _platform_game

    candidate = GameCandidate(name="Hades", install_dir=str(tmp_path), source="steam")

    assert _platform_game(candidate) is None
