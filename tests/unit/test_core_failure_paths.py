"""核心与服务层的边界: 存储层退路、后端降级与"缺文件"的可读错误.

判据仍然是"这条边界上的行为是什么":

- 存储层: 按备份 id 清掉"当前节点"指针、按目标版本迁移到中途、数据库文件打不开时给可读错误;
- 服务层: 键盘/调度后端在自己初始化失败时降级为占位实现(而不是把整个界面拖挂),
  以及"源文件已经不在了"这种最普通的失败要收敛成带原因的异常。

不可达的兜底另见 ``# pragma: no cover`` 那条(附原因)。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import archive_management.services.artwork as artwork_mod
import archive_management.services.hotkeys as hotkeys_mod
import archive_management.services.scheduler as scheduler_mod
import archive_management.services.snapshot as snapshot_mod
from archive_management.domain import BackupNode, Game
from archive_management.exceptions import (
    ArtworkImageError,
    DatabaseError,
    HotkeyError,
    SchedulingError,
    SnapshotError,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
)
from archive_management.services.artwork import normalized_image
from archive_management.services.hotkeys import UnavailableBackend
from archive_management.services.scheduler import ManualBackend
from helpers import migrated_database

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率(核心边界)"),
    pytest.mark.story("出错时给可读原因, 后端坏了要降级"),
    pytest.mark.layer("unit"),
]


# --------------------------------------------------------------- 存储层的退路


def test_the_current_backup_pointer_is_cleared_by_backup_id(tmp_path: Path) -> None:
    """删备份前要按**备份 id** 清掉游戏上的"当前节点"指针(不是按游戏 id)."""
    database = migrated_database(tmp_path)
    games = GameRepository(database)
    game = games.add(Game(name="Demo"))
    assert game.id is not None
    node = BackupRepository(database).add(
        BackupNode(game_id=game.id, node_kind="manual")
    )
    assert node.id is not None
    games.set_current_backup(game.id, node.id)
    assert games.current_backup(game.id) is not None

    games.clear_current_backup(node.id)

    assert games.current_backup(game.id) is None


def test_migrating_to_an_older_target_stops_there(tmp_path: Path) -> None:
    """``target`` 把迁移停在某个历史版本(测试与降级都要用)."""
    database = Database(tmp_path / "app.db")
    latest = database.latest_schema_version()

    moved = database.migrate(target=latest - 1)

    assert moved == latest - 1
    assert database.schema_version() == latest - 1


def test_opening_a_database_at_an_unusable_path_is_reported(tmp_path: Path) -> None:
    """数据库文件的位置被目录占着时给可读错误, 而不是冒裸 ``sqlite3.Error``."""
    blocked = tmp_path / "app.db"
    blocked.mkdir()

    with pytest.raises(DatabaseError, match="无法打开数据库"):
        Database(blocked).connect()


# --------------------------------------------------------------- 后端降级


def test_a_broken_keyboard_backend_falls_back_to_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """键盘模块起不来时降级为"明确不可用", 界面照常开(只是快捷键点了会报原因)."""

    def boom() -> object:
        raise HotkeyError("没有图形环境")

    monkeypatch.setattr(hotkeys_mod, "PynputBackend", boom)

    backend = hotkeys_mod.default_backend()

    assert isinstance(backend, UnavailableBackend)
    assert backend.reason == "没有图形环境"


def test_a_broken_scheduler_backend_falls_back_to_manual(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """调度器起不来时降级为手动后端: 自动备份停了, 但手动备份一条都不少."""

    def boom() -> object:
        raise SchedulingError("apscheduler 不可用")

    monkeypatch.setattr(scheduler_mod, "ApschedulerBackend", boom)

    backend = scheduler_mod.default_backend()

    assert isinstance(backend, ManualBackend)


# --------------------------------------------------------------- 缺文件


def test_a_size_probe_that_fails_is_reported_as_unreadable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """文件在、但取大小时 IO 失败(设备忙/权限): 报"读不到", 而不是冒裸 ``OSError``."""
    source = tmp_path / "cover.png"
    source.write_bytes(b"x")

    def refuse(self: Path) -> os.stat_result:
        raise OSError("设备忙")

    monkeypatch.setattr(Path, "stat", refuse)

    with pytest.raises(ArtworkImageError) as info:
        normalized_image(source, "cover")

    assert info.value.code == artwork_mod.UNREADABLE


def test_copying_a_missing_source_reports_a_copy_failure(tmp_path: Path) -> None:
    """要复制的源文件不见了: 收敛成 ``SnapshotError``(上层文案靠它区分"复制失败")."""
    with pytest.raises(SnapshotError, match="复制失败"):
        snapshot_mod._copy_with_hash(tmp_path / "gone.bin", tmp_path / "target.bin")
