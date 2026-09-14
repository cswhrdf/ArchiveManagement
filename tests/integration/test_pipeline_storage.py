"""配置、数据库与日志文件的跨层协作.

这个模块验证"真实基础设施一起工作"而不是单个函数: 应用目录解析出的路径真的
能落盘、配置写盘后能读回、SQLite 迁移幂等且数据在重开连接后仍在、审计日志经
真实文件处理器写进日志文件并且路径被脱敏。

不访问用户真实目录、真实网络或真实系统快捷键: 一切都收敛在临时根目录下。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from archive_management.config import AppConfig, load_config, save_config
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    HomeRepository,
    SaveLocationRepository,
)
from archive_management.logging_config import configure_logging
from archive_management.services.audit import log_action, redacted_path
from helpers import add_backup_node, add_game, add_location, make_save_folder

pytestmark = [
    pytest.mark.integration,
    pytest.mark.normal,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("跨层协作"),
    pytest.mark.story("配置数据库与日志协同"),
    pytest.mark.layer("integration"),
]

_LOG_ROOT = "archive_management"


@pytest.fixture
def isolated_logging() -> Iterator[None]:
    """让用例挂上的日志处理器在结束时不残留(否则后续用例会写到已删除目录)."""
    yield
    logger = logging.getLogger(_LOG_ROOT)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()


def test_paths_config_and_database_share_one_root(tmp_path: Path) -> None:
    """应用目录、配置文件与数据库都落在同一个根目录下, 且能反复重开."""
    paths = ApplicationPaths.default(override_root=tmp_path).ensure()

    assert paths.config_dir.is_dir()
    assert paths.backup_root.is_dir()
    assert paths.database_path.parent == paths.data_dir

    save_config(AppConfig(theme="dark"), paths.config_path)
    assert load_config(paths.config_path).theme == "dark"
    assert paths.config_path.is_file()

    database = Database(paths.database_path)
    assert database.migrate() == database.latest_schema_version()
    # 重复迁移必须幂等(应用每次启动都会调用它).
    assert database.migrate() == database.latest_schema_version()

    save = make_save_folder(tmp_path, content="state-0")
    game_id = add_game(database, "跨层游戏", path=save)
    node = add_backup_node(database, game_id, title="首个节点")
    assert node.id is not None

    # 新连接(模拟"下次启动")能读到同一份数据.
    reopened = Database(paths.database_path)
    assert reopened.schema_version() == database.latest_schema_version()
    games = GameRepository(reopened).list()
    assert [game.name for game in games] == ["跨层游戏"]
    assert len(SaveLocationRepository(reopened).list_for_game(game_id)) == 1
    assert BackupRepository(reopened).count(game_id) == 1


def test_audit_log_is_written_to_file_with_redacted_path(
    tmp_path: Path, isolated_logging: None
) -> None:
    """审计日志经真实文件处理器落盘, 且用户完整路径不出现在日志里."""
    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    configure_logging(paths.log_dir, console=False)

    save = make_save_folder(tmp_path, content="state-0")
    log_action("backup.create", game_id=1, path=redacted_path(str(save)))
    log_action("ui.select_game", basic=True, game_id=1)

    log_file = paths.log_dir / "archive-management.log"
    assert log_file.is_file()
    content = log_file.read_text(encoding="utf-8")

    assert "backup.create" in content
    assert "game_id=1" in content
    # DEBUG 级基础操作也落盘(控制台默认不打印, 文件恒 DEBUG).
    assert "ui.select_game" in content
    # 完整路径被脱敏: 只保留最后两级片段.
    assert str(tmp_path) not in content
    assert "save0" in content


def test_repository_aggregates_reflect_written_rows(tmp_path: Path) -> None:
    """仓储层的聚合查询与写入结果一致(主页与详情页都依赖它)."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    save = make_save_folder(tmp_path, content="state-0")
    game_id = add_game(database, "聚合游戏")
    add_location(database, game_id, save, primary=True)
    for index in range(3):
        add_backup_node(database, game_id, title=f"节点 {index}")

    row = next(
        item for item in HomeRepository(database).list_rows() if item.game.id == game_id
    )
    assert len(row.locations) == 1
    assert row.backup_count == 3
    assert row.last_backup_at is not None
