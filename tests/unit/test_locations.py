"""删除原始存档位置的单元测试.

覆盖影响范围预检、回收站后端(用替身, 不触碰真实回收站)、游戏名确认、
危险路径拒绝、主位置转移与失败时的记录保留。全部使用临时目录。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from archive_management.application.locations import (
    REMOVAL_MISSING,
    plan_location_removal,
    remove_save_location,
)
from archive_management.domain import Game, SaveLocation
from archive_management.exceptions import ArchiveManagementError, StorageError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.pathcheck import is_within

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("原始目录删除"),
    pytest.mark.story("删除原始存档位置"),
    pytest.mark.layer("unit"),
]


@dataclass(frozen=True)
class _Env:
    """测试环境: 数据库、仓库、游戏与存档位置."""

    database: Database
    games: GameRepository
    locations: SaveLocationRepository
    game_id: int
    location_id: int
    save: Path
    root: Path


class _Trash:
    """记录调用的回收站替身."""

    def __init__(self, *, fail: bool = False) -> None:
        """按需构造一个总是失败的替身."""
        self.paths: list[str] = []
        self._fail = fail

    def __call__(self, path: str) -> None:
        """记录路径; 标记失败时抛出 StorageError."""
        if self._fail:
            raise StorageError("回收站不可用")
        self.paths.append(path)
        target = Path(path)
        if target.is_dir():
            import shutil

            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()


def _env(
    tmp_path: Path, *, extra_location: Path | None = None, location: Path | None = None
) -> _Env:
    """构造含一个(或两个)存档位置的环境.

    ``location`` 指向危险路径(如盘符根目录)时不会真的往里写文件, 只登记
    位置记录, 让预检负责拒绝它。
    """
    tmp_path.mkdir(parents=True, exist_ok=True)
    save = location if location is not None else tmp_path / "save"
    if is_within(save, tmp_path):
        save.mkdir(parents=True, exist_ok=True)
        (save / "slot1.dat").write_text("v1", encoding="utf-8")
        (save / "nested").mkdir(exist_ok=True)
        (save / "nested" / "deep.dat").write_text("v2", encoding="utf-8")
    root = tmp_path / "backups"
    root.mkdir(parents=True, exist_ok=True)
    database = Database(tmp_path / "app.db")
    database.migrate()
    games = GameRepository(database)
    locations = SaveLocationRepository(database)
    game = games.add(Game(name="Demo"))
    assert game.id is not None
    created = locations.add(
        SaveLocation(
            game_id=game.id, path=str(save), path_kind="directory", is_primary=True
        )
    )
    assert created.id is not None
    if extra_location is not None:
        extra_location.mkdir(parents=True, exist_ok=True)
        locations.add(
            SaveLocation(
                game_id=game.id, path=str(extra_location), path_kind="directory"
            )
        )
    return _Env(
        database=database,
        games=games,
        locations=locations,
        game_id=game.id,
        location_id=created.id,
        save=save,
        root=root,
    )


def test_plan_reports_impact_of_removal(tmp_path: Path) -> None:
    env = _env(tmp_path)

    plan = plan_location_removal(env.database, env.location_id, protect=(env.root,))

    assert plan.blocked is False
    assert plan.blocked_reason is None
    assert plan.game_name == "Demo"
    assert plan.path == str(env.save)
    assert plan.files == 2
    assert plan.directories == 1
    assert plan.total_size > 0
    assert plan.exists is True


def test_remove_moves_path_to_trash_and_deletes_record(
    tmp_path: Path, audit_log: list[str]
) -> None:
    env = _env(tmp_path)
    trash = _Trash()

    result = remove_save_location(
        env.database,
        env.location_id,
        confirm_name="demo",
        trash=trash,
        protect=(env.root,),
    )

    assert trash.paths == [str(env.save)]
    assert not env.save.exists()
    assert env.locations.list_for_game(env.game_id) == []
    assert result.files == 2
    assert any(
        message.startswith("location.delete ") and "result=succeeded" in message
        for message in audit_log
    )


def test_remove_requires_matching_game_name(
    tmp_path: Path, audit_log: list[str]
) -> None:
    env = _env(tmp_path)
    trash = _Trash()

    with pytest.raises(ArchiveManagementError):
        remove_save_location(
            env.database,
            env.location_id,
            confirm_name="其它游戏",
            trash=trash,
            protect=(env.root,),
        )

    assert trash.paths == []
    assert env.save.exists()
    assert len(env.locations.list_for_game(env.game_id)) == 1
    assert any("reason=confirm_mismatch" in message for message in audit_log)


def test_remove_rejects_backup_root_and_its_parent(tmp_path: Path) -> None:
    env = _env(tmp_path, location=tmp_path / "backups" / "inside")
    plan = plan_location_removal(env.database, env.location_id, protect=(env.root,))
    assert plan.blocked_reason == "protected"

    parent_env = _env(tmp_path / "second", location=tmp_path)
    parent_plan = plan_location_removal(
        parent_env.database, parent_env.location_id, protect=(parent_env.root,)
    )
    assert parent_plan.blocked_reason == "contains_protected"

    with pytest.raises(ArchiveManagementError):
        remove_save_location(
            parent_env.database,
            parent_env.location_id,
            confirm_name="Demo",
            trash=_Trash(),
            protect=(parent_env.root,),
        )


def test_remove_rejects_drive_root(tmp_path: Path) -> None:
    anchor = Path(tmp_path.anchor)
    env = _env(tmp_path / "third", location=anchor)

    plan = plan_location_removal(env.database, env.location_id, protect=(env.root,))

    assert plan.blocked_reason == "drive_root"


def test_plan_reports_missing_path(tmp_path: Path) -> None:
    env = _env(tmp_path)
    import shutil

    shutil.rmtree(env.save)

    plan = plan_location_removal(env.database, env.location_id, protect=(env.root,))

    assert plan.blocked_reason == REMOVAL_MISSING
    assert plan.exists is False


def test_remove_promotes_remaining_location_to_primary(tmp_path: Path) -> None:
    extra = tmp_path / "save2"
    env = _env(tmp_path, extra_location=extra)

    remove_save_location(
        env.database,
        env.location_id,
        confirm_name="Demo",
        trash=_Trash(),
        protect=(env.root,),
    )

    remaining = env.locations.list_for_game(env.game_id)
    assert [item.path for item in remaining] == [str(extra)]
    assert remaining[0].is_primary is True


def test_trash_failure_keeps_record_and_marks_operation_failed(
    tmp_path: Path, audit_log: list[str]
) -> None:
    env = _env(tmp_path)

    with pytest.raises(StorageError):
        remove_save_location(
            env.database,
            env.location_id,
            confirm_name="Demo",
            trash=_Trash(fail=True),
            protect=(env.root,),
        )

    assert env.save.exists()
    assert len(env.locations.list_for_game(env.game_id)) == 1
    assert any(message.startswith("location.delete.failed ") for message in audit_log)


def test_remove_rejects_unknown_location(tmp_path: Path) -> None:
    env = _env(tmp_path)
    with pytest.raises(ArchiveManagementError):
        plan_location_removal(env.database, 4242)
