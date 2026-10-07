"""
由 test_sql_backend.py 拆分出的共享测试基建(被多个主题文件引用), 拆分原则与映射见 docs/test-refactor-plan.md。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.domain import (
    ArtworkRef,
    PlatformGame,
    PlatformId,
    SavePathCandidate,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    GameRepository,
)
from archive_management.services.artwork import (
    steam_cover,
)
from archive_management.services.game_names import NameFetcher
from archive_management.services.platform_adapters import SaveCandidateSource
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.sql_backend import SqlArchiveService

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).

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


def _service(tmp_path: Path, *, config_path: Path | None = None) -> SqlArchiveService:
    database = Database(tmp_path / "app.db")
    database.migrate()
    # 用不启动线程的手动调度后端, 让测试不依赖真实时间轴.
    return SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        config_path=config_path,
    )


def _tree_files(root: Path) -> list[Path]:
    """``root`` 下的全部普通文件(断言"什么都没写"用)."""
    if not root.exists():
        return []
    return [path for path in root.rglob("*") if path.is_file()]


def _advance(save: Path, text: str = "") -> None:
    """改动存档内容, 让下一次备份与当前节点不同(否则会被判为"未变化")."""
    target = save / "slot1.dat"
    previous = target.read_text(encoding="utf-8")
    target.write_text(text or f"{previous}+", encoding="utf-8")


def _service_with_save(
    tmp_path: Path, name: str = "Demo", *, config_path: Path | None = None
) -> tuple[SqlArchiveService, str, Path]:
    """构造带一个可用存档位置的游戏, 返回服务、游戏 id 与存档目录."""
    service = _service(tmp_path, config_path=config_path)
    game_id = service.add_game(name).game_id
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game_id, path=str(save), kind="directory")
    return service, game_id, save


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
