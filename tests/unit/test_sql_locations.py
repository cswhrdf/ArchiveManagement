"""
存档位置: 首位置自动主位/去重/同一目录异写拒绝/路径规范化/校验状态/删除预览与回收站。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import archive_management.application.locations as locations_mod
from archive_management.domain import (
    GameCandidate,
)
from archive_management.exceptions import (
    ArchiveManagementError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.repository import (
    CandidateRepository,
    SaveLocationRepository,
)
from archive_management.services.pathcheck import normalize_path

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _service,
    _service_with_save,
    _steam_service,
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


def test_update_location_rejects_a_path_that_cannot_be_read(tmp_path: Path) -> None:
    """改成读不到的路径要被拦下, 库里那条位置保持原样."""
    service, game_id, _save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]

    with pytest.raises(ArchiveManagementError) as excinfo:
        service.update_location(location.location_id, path=str(tmp_path / "missing"))

    assert str(excinfo.value) == tr("error.loc_missing", path=str(tmp_path / "missing"))
    assert service.list_locations(game_id)[0].path == location.path


def test_remove_location_drops_the_record(tmp_path: Path) -> None:
    """删掉一条存档位置记录: 列表与摘要都不再算它."""
    service, game_id, _save = _service_with_save(tmp_path)
    location = service.list_locations(game_id)[0]

    service.remove_location(location.location_id)

    assert service.list_locations(game_id) == []
    assert service.list_games()[0].has_locations is False
