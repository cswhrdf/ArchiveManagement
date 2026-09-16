"""删除原始存档位置的失败路径.

正常路径已由 ``test_locations.py`` 覆盖; 这里专补"删到一半出问题"的几种情形:
回收站后端抛错、记录里缺少 id、剩余位置没有 id 无法提升为主位置、确认名为空白。
共同约定是**要么完整成功, 要么保持原状并留下可追溯的失败记录**。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.application import locations as locations_mod
from archive_management.application.locations import remove_save_location
from archive_management.domain import SaveLocation
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import SaveLocationRepository
from helpers import add_game, add_location, make_save_folder, migrated_database

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("原始目录删除"),
    pytest.mark.story("删除失败保持原状"),
    pytest.mark.layer("integration"),
]


class _FailingTrash:
    """总是失败的回收站替身(例如网络盘不支持)."""

    def __call__(self, path: str) -> None:
        raise RuntimeError(f"回收站拒绝: {path}")


class _TrashDir:
    """把目标挪进临时"回收站"目录的替身(不触碰真实系统回收站)."""

    def __init__(self, destination: Path) -> None:
        self.destination = destination
        self.moved: list[str] = []

    def __call__(self, path: str) -> None:
        destination = self.destination / Path(path).name
        Path(path).rename(destination)
        self.moved.append(str(destination))


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """真实 SQLite 数据库(已迁移)."""
    return migrated_database(tmp_path)


def _game_with_save(database: Database, tmp_path: Path) -> tuple[int, SaveLocation]:
    """登记一个游戏与其主存档位置, 返回游戏 id 与位置记录."""
    game_id = add_game(database, "删除失败游戏", path=make_save_folder(tmp_path))
    location = SaveLocationRepository(database).list_for_game(game_id)[0]
    return game_id, location


def test_trash_failure_keeps_record_and_path(
    database: Database, tmp_path: Path, audit_log: list[str]
) -> None:
    """回收站失败时抛出原错误、保留记录、磁盘内容不动, 并写下失败审计."""
    game_id, location = _game_with_save(database, tmp_path)
    assert location.id is not None
    save = Path(location.path)

    with pytest.raises(RuntimeError, match="回收站拒绝"):
        remove_save_location(
            database,
            location.id,
            confirm_name="删除失败游戏",
            trash=_FailingTrash(),
        )

    assert save.is_dir()
    assert len(SaveLocationRepository(database).list_for_game(game_id)) == 1
    assert "location.delete" in " ".join(audit_log)


def test_blank_confirmation_is_rejected(database: Database, tmp_path: Path) -> None:
    """确认名为空白时按"未确认"处理, 不执行删除."""
    _game_id, location = _game_with_save(database, tmp_path)
    assert location.id is not None

    with pytest.raises(ArchiveManagementError, match="确认名称"):
        remove_save_location(
            database, location.id, confirm_name="   ", trash=_FailingTrash()
        )

    assert Path(location.path).is_dir()


def test_location_without_id_is_reported_as_unknown(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记录缺少 id 时按"未知位置"报错, 而不是拿 None 去查库."""

    def without_id(*_args: object, **_kwargs: object) -> SaveLocation:
        return SaveLocation(game_id=1, path=str(tmp_path / "save"))

    monkeypatch.setattr(locations_mod, "_require_location", without_id)

    with pytest.raises(ArchiveManagementError, match="未知存档位置"):
        remove_save_location(
            database, 1, confirm_name="任意游戏", trash=_FailingTrash()
        )


def test_primary_promotion_is_skipped_without_remaining_id(
    database: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """剩余位置缺少 id 时跳过主位置提升, 而不是拿 ``None`` 去更新数据库."""
    game_id, location = _game_with_save(database, tmp_path)
    second = add_location(database, game_id, tmp_path / "save-b")
    assert location.id is not None
    assert second.id is not None
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    trash = _TrashDir(trash_dir)
    repository = SaveLocationRepository(database)

    def list_without_id(game: int) -> list[SaveLocation]:
        # 主位置刚被删掉时, 列表里剩下一个尚未入库(没有 id)的位置: 无法提升.
        return [SaveLocation(game_id=game, path=str(tmp_path / "save-b"))]

    monkeypatch.setattr(repository, "list_for_game", list_without_id)
    monkeypatch.setattr(locations_mod, "SaveLocationRepository", lambda _db: repository)

    remove_save_location(
        database, location.id, confirm_name="删除失败游戏", trash=trash
    )

    assert len(trash.moved) == 1
    remaining = SaveLocationRepository(database).list_for_game(game_id)
    assert [item.path for item in remaining] == [str(tmp_path / "save-b")]
    assert remaining[0].is_primary is False


def test_directory_is_moved_to_trash_and_record_removed(
    database: Database, tmp_path: Path
) -> None:
    """成功路径的对照: 目录被挪走、记录被删除, 返回值描述影响范围."""
    game_id, location = _game_with_save(database, tmp_path)
    assert location.id is not None
    trash_dir = tmp_path / "trash"
    trash_dir.mkdir()
    trash = _TrashDir(trash_dir)

    result = remove_save_location(
        database, location.id, confirm_name="删除失败游戏", trash=trash
    )

    assert trash.moved == [str(trash_dir / Path(location.path).name)]
    assert not Path(location.path).exists()
    assert result.files == 1
    assert SaveLocationRepository(database).list_for_game(game_id) == []
