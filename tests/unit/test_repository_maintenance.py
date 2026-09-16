"""仓储的维护类写入路径(监控目录、候选、文件清单).

这些表只在"发现/维护"流程里被写入, 与游戏/备份主链路不同的地方是: 它们没有
稳定的界面入口, 因此很容易在重构时悄悄失去覆盖。这里跑真实 SQLite, 断言的是
"写进去能读回来 + 缺字段时立刻报错", 而不是某条 SQL 的形状。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.domain import (
    BackupFileEntry,
    GameCandidate,
    MonitoredDirectory,
)
from archive_management.exceptions import DatabaseError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    CandidateRepository,
    MonitoredDirectoryRepository,
)
from helpers import add_backup_node, add_game, migrated_database, utc_moment

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("数据仓储"),
    pytest.mark.story("监控目录与候选维护"),
    pytest.mark.layer("integration"),
]


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """真实 SQLite 数据库(已迁移)."""
    return migrated_database(tmp_path)


def _candidate(
    name: str = "候选游戏", install_dir: str = "/games/cand"
) -> GameCandidate:
    """构造一个待入库的候选游戏."""
    return GameCandidate(name=name, install_dir=install_dir, source="manual")


# --- 监控目录 -----------------------------------------------------------------


def test_monitored_directory_roundtrip(database: Database, tmp_path: Path) -> None:
    """写入后能按 id/路径读回, 创建时间落到数据库而不是仅存在于内存对象."""
    repository = MonitoredDirectoryRepository(database)
    folder = tmp_path / "library"

    saved = repository.add(
        MonitoredDirectory(path=str(folder), note="游戏库", created_at=utc_moment(1))
    )

    assert saved.id is not None
    assert repository.get(saved.id) is not None
    assert repository.get(999999) is None
    listed = repository.list_all()
    assert [item.path for item in listed] == [str(folder)]
    assert listed[0].note == "游戏库"
    assert listed[0].created_at == utc_moment(1)


def test_monitored_directory_update_requires_id(
    database: Database, tmp_path: Path
) -> None:
    """缺少 id 时无法定位要更新的记录, 必须立即报错."""
    repository = MonitoredDirectoryRepository(database)

    with pytest.raises(ValueError, match="更新监控目录需要 id"):
        repository.update(MonitoredDirectory(path=str(tmp_path)))


def test_monitored_directory_duplicate_detection_ignores_self(
    database: Database, tmp_path: Path
) -> None:
    """重复判定忽略大小写与结尾分隔符, 且编辑自身时不算重复."""
    repository = MonitoredDirectoryRepository(database)
    folder = repository.add(MonitoredDirectory(path=str(tmp_path / "Library")))

    assert repository.duplicate_of(str(tmp_path / "library").upper()) is not None
    assert repository.duplicate_of(str(tmp_path / "Library")) is not None
    assert folder.id is not None
    assert (
        repository.duplicate_of(str(tmp_path / "Library"), exclude_id=folder.id) is None
    )


def test_monitored_directory_scan_and_enabled_paths(
    database: Database, tmp_path: Path
) -> None:
    """扫描时间与结果要落库, 只有启用的目录才参与扫描."""
    repository = MonitoredDirectoryRepository(database)
    enabled = repository.add(MonitoredDirectory(path=str(tmp_path / "a")))
    repository.add(MonitoredDirectory(path=str(tmp_path / "b"), enabled=False))
    assert enabled.id is not None

    repository.mark_scan(enabled.id, status="ok", when=utc_moment(2))

    refreshed = repository.get(enabled.id)
    assert refreshed is not None
    assert refreshed.last_scan_status == "ok"
    assert refreshed.last_scan_at == utc_moment(2)
    assert repository.enabled_paths() == [str(tmp_path / "a")]

    repository.delete(enabled.id)
    assert repository.enabled_paths() == []


# --- 候选游戏 -----------------------------------------------------------------


def test_candidate_update_paths_roundtrip(database: Database, tmp_path: Path) -> None:
    """候选的进度/关联游戏/安装路径/健康状态都能更新并读回."""
    repository = CandidateRepository(database)
    game_id = add_game(database, "已入库游戏")

    saved, created = repository.upsert(_candidate(install_dir=str(tmp_path / "game")))
    assert created is True
    assert saved.id is not None

    imported = repository.set_status(saved.id, "imported", game_id=game_id)
    assert imported.status == "imported"
    assert imported.game_id == game_id

    relocated = repository.set_install_dir(saved.id, str(tmp_path / "fixed"))
    assert relocated.install_dir == str(tmp_path / "fixed")

    repository.set_health(saved.id, "missing")
    updated = repository.get(saved.id)
    assert updated is not None
    assert updated.health == "missing"

    assert repository.count_by_status()["imported"] == 1
    assert repository.list_all(status="new") == []

    repository.delete(saved.id)
    assert repository.get(saved.id) is None
    assert repository.count_by_status()["imported"] == 0


def test_candidate_update_of_unknown_id_raises(database: Database) -> None:
    """对不存在的候选做更新时抛 :class:`DatabaseError`(而不是静默通过)."""
    repository = CandidateRepository(database)

    with pytest.raises(DatabaseError, match="未知候选"):
        repository.set_status(4242, "ignored", game_id=None)

    with pytest.raises(DatabaseError, match="未知候选"):
        repository.set_install_dir(4242, "/nowhere")


# --- 备份文件清单 -------------------------------------------------------------


def test_backup_file_batch_insert_and_read(database: Database, tmp_path: Path) -> None:
    """批量写入文件清单: 空清单返回 0, 写入后按路径排序读回."""
    game_id = add_game(database, "清单游戏")
    node = add_backup_node(database, game_id)
    assert node.id is not None
    repository = BackupRepository(database)
    entries = [
        BackupFileEntry(
            backup_id=node.id, relative_path="loc-0/b.dat", size=2, sha256="b" * 64
        ),
        BackupFileEntry(
            backup_id=node.id, relative_path="loc-0/a.dat", size=1, sha256="a" * 64
        ),
        BackupFileEntry(
            backup_id=node.id,
            relative_path="loc-0/empty",
            sha256="c" * 64,
            file_kind="directory",
        ),
    ]

    assert repository.add_files(node.id, []) == 0
    assert repository.add_files(node.id, entries) == 3

    stored = repository.list_files(node.id)
    assert [item.relative_path for item in stored] == [
        "loc-0/a.dat",
        "loc-0/b.dat",
        "loc-0/empty",
    ]
    assert stored[2].file_kind == "directory"
    assert repository.list_files(node.id + 999) == []
