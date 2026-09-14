"""跨模块共享的测试辅助设施.

这里只放"多个测试模块都要用到"的构造器: 临时 SQLite 数据库、真实领域服务实例、
可复用的测试数据与测试运行元信息。设计约束:

- 一律使用真实实现(领域模型、仓储、服务、临时目录), 不引入 mock 替身 ——
  需要"可控依赖"时由调用方继续注入(例如不启动线程的调度后端);
- 辅助函数只把数据准备到"可以开始断言"的程度, 不替调用方做业务判断,
  也不为了测试在生产代码里加分支;
- 模块内常见的薄封装(如 ``_service``/``_database``)保留原签名, 只把实现
  委托到这里, 避免几十处调用点跟着改。

测试模块通过 ``import helpers`` 使用本模块(pytest 配置把 ``tests`` 目录加入了
``pythonpath``), 因此这里不依赖任何测试包结构。
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from archive_management.application.backup import BackupService
from archive_management.domain import (
    BackupNode,
    Game,
    NodeKind,
    PathKind,
    SaveLocation,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.platforms import current_platform
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.sql_backend import SqlArchiveService

# 测试数据使用的时间基准: 固定的 UTC 时刻, 保证用例与报告可复现.
BASE_MOMENT = datetime(2026, 9, 14, 9, 0, tzinfo=UTC)


def utc_moment(
    day: int, hour: int = 9, minute: int = 0, *, month: int = 9, year: int = 2026
) -> datetime:
    """返回一个固定的 UTC 时刻(替代各模块重复的 ``_dt`` 构造)."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def migrated_database(root: Path, *, name: str = "app.db") -> Database:
    """在 ``root`` 下创建并迁移一个 SQLite 数据库."""
    database = Database(root / name)
    database.migrate()
    return database


def add_game(
    database: Database,
    name: str = "Demo",
    *,
    path: Path | None = None,
    location_kind: PathKind = "directory",
) -> int:
    """插入一个游戏并返回 id; 传入 ``path`` 时同时登记一个主存档位置."""
    repository = GameRepository(database)
    game = repository.add(Game(name=name, original_name=name))
    assert game.id is not None
    if path is not None:
        path.mkdir(parents=True, exist_ok=True)
        add_location(database, game.id, path, kind=location_kind, primary=True)
    return game.id


def add_location(
    database: Database,
    game_id: int,
    path: Path | str,
    *,
    kind: PathKind = "directory",
    primary: bool = False,
    checked: bool = True,
) -> SaveLocation:
    """给游戏登记一个存档位置."""
    return SaveLocationRepository(database).add(
        SaveLocation(
            game_id=game_id,
            path=str(path),
            path_kind=kind,
            is_primary=primary,
            last_checked_at=datetime.now(UTC) if checked else None,
            last_check_status="ok" if checked else None,
        )
    )


def add_backup_node(
    database: Database,
    game_id: int,
    *,
    when: datetime | None = None,
    kind: NodeKind = "manual",
    parent_id: int | None = None,
    title: str = "",
    note: str = "",
    is_safety: bool = False,
) -> BackupNode:
    """写入一个备份节点(不含文件清单), 用于计数、时间线与分页类断言."""
    return BackupRepository(database).add(
        BackupNode(
            game_id=game_id,
            parent_id=parent_id,
            node_kind=kind,
            created_at=when or datetime.now(UTC),
            title=title,
            note=note,
            is_safety=is_safety,
        )
    )


def make_save_folder(
    root: Path, *, index: int = 0, content: str = "state-0", name: str = "slot.dat"
) -> Path:
    """在 ``root`` 下造一个存档目录, 内含一个内容确定的文件."""
    folder = root / f"save{index}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(content, encoding="utf-8")
    return folder


def touch_save(root: Path, *, index: int = 0, text: str = "") -> None:
    """改动存档内容, 让下一次备份与当前节点不同.

    "内容没有变化就不产生新备份"是备份服务层的规则, 因此需要连续备份的用例
    必须先推进一次存档状态; ``text`` 为空时在原文后追加一个字符。
    """
    target = root / f"save{index}" / "slot.dat"
    previous = target.read_text(encoding="utf-8")
    target.write_text(text or f"{previous}+", encoding="utf-8")


def backup_service(
    root: Path,
    *,
    saves: int = 1,
    name: str = "Demo",
    backup_dir: str = "backups",
) -> tuple[BackupService, int]:
    """构造"一个游戏 + ``saves`` 个存档位置"的备份服务, 返回服务与游戏 id."""
    database = migrated_database(root)
    game_id = add_game(database, name)
    for index in range(saves):
        folder = make_save_folder(root, index=index, content=f"state-{index}")
        add_location(database, game_id, folder, primary=index == 0)
    service = BackupService(database, backup_root=root / backup_dir)
    return service, game_id


def manual_scheduler() -> BackupScheduler:
    """返回不启动线程的调度器, 让用例可以手动触发定时任务."""
    return BackupScheduler(backend=ManualBackend())


def sql_archive_service(
    root: Path,
    *,
    scheduler: BackupScheduler | None = None,
    backup_dir: str = "backups",
) -> SqlArchiveService:
    """构造真实 SQLite 后端; 默认注入手动调度器, 不依赖真实时间轴."""
    return SqlArchiveService(
        migrated_database(root),
        backup_root=root / backup_dir,
        scheduler=scheduler if scheduler is not None else manual_scheduler(),
    )


def write_json_report(path: Path, payload: dict[str, Any]) -> Path:
    """把机器可读的测试结果写入 ``path``(父目录自动创建)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def environment_info() -> dict[str, str]:
    """收集测试运行环境元信息(平台、Python、提交号).

    结果同时写进性能/安全结果文件, 保证每条基准都能追溯到具体运行; CI 里
    这些字段还会由 ``scripts/create_allure_summary.py`` 写进报告环境信息。
    """
    return {
        "os": platform.platform(),
        "os_family": current_platform(),
        "python": platform.python_version(),
        "python_implementation": sys.implementation.name,
        "commit": git_commit(),
    }


def git_commit() -> str:
    """返回当前提交 SHA; 不是 Git 仓库(或 HEAD 未展开)时返回空串.

    直接读 ``.git/HEAD``, 不调用 git 命令: 测试不应该依赖外部可执行文件,
    也不应该在某些平台触发子进程开销。
    """
    root = Path(__file__).resolve().parent.parent
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if head.startswith("ref: "):
        try:
            return (
                (root / ".git" / head.removeprefix("ref: "))
                .read_text(encoding="utf-8")
                .strip()
            )
        except OSError:  # pragma: no cover - 打包/浅克隆环境
            return ""
    return head


def _stamp(moment: datetime) -> str:
    """把时间写成与生产代码一致的清单时间戳(毫秒精度 UTC)."""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def seed_home_games(
    database: Database,
    count: int,
    *,
    locations: int = 2,
    backups: int = 3,
    base: datetime = BASE_MOMENT,
) -> None:
    """批量写入主页规模测试所需的游戏、存档位置与备份节点.

    这是**批量造数据**的辅助(单事务 ``executemany``), 不是被测代码路径:
    大规模数据准备如果用仓储逐条写入, 光是准备数据就会比被测的查询还慢,
    基准会失去意义。查询侧仍然走真实的仓储与用例函数。
    """
    origins: tuple[str, ...] = ("steam", "manual", "epic", "gog", "ubisoft")
    games: list[tuple[Any, ...]] = []
    places: list[tuple[Any, ...]] = []
    nodes: list[tuple[Any, ...]] = []
    for index in range(count):
        game_id = index + 1
        created = base + timedelta(minutes=index)
        stamps = _stamp(created)
        games.append(
            (
                game_id,
                f"游戏 {index:05d}",
                None,
                "windows",
                1 if index % 11 else 0,
                stamps,
                f"游戏 {index:05d}",
                f"game-{index:05d}",
                origins[index % len(origins)],
                "收藏" if index % 5 == 0 else "",
                1 if index % 17 == 0 else 0,
                stamps,
            )
        )
        places.extend(
            (
                game_id,
                f"/saves/{index:05d}/loc{slot}",
                "directory",
                "manual",
                1 if slot == 0 else 0,
                stamps,
                "ok",
            )
            for slot in range(locations)
        )
        nodes.extend(
            (
                game_id,
                None if depth == 0 else (index * backups) + depth,
                "manual" if depth == 0 else "auto",
                None,
                _stamp(created + timedelta(seconds=depth)),
                f"备份 {depth}",
                "",
                f"hash-{index:05d}-{depth}",
                f"game-{index:05d}/snap-{depth}",
                0,
            )
            for depth in range(backups)
        )
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO games (id, name, steam_app_id, platform, enabled,"
            " created_at, original_name, storage_key, origin, tags, archived,"
            " last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            games,
        )
        connection.executemany(
            "INSERT INTO save_locations (game_id, path, path_kind, source,"
            " is_primary, last_checked_at, last_check_status)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            places,
        )
        connection.executemany(
            "INSERT INTO backup_nodes (game_id, parent_id, node_kind, branch_name,"
            " created_at, title, note, content_hash, storage_relpath, is_safety)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            nodes,
        )


def seed_backup_chain(
    database: Database, game_id: int, count: int, *, base: datetime = BASE_MOMENT
) -> list[int]:
    """给一个游戏写入 ``count`` 个首尾相连的备份节点, 返回节点 id(升序).

    用于时间线/分支树在大规模节点下的排序与缩进基准。
    """
    nodes: list[tuple[Any, ...]] = []
    for index in range(count):
        created = base + timedelta(minutes=index)
        nodes.append(
            (
                game_id,
                None if index == 0 else index,
                "manual" if index % 13 == 0 else "auto",
                None,
                created.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
                f"节点 {index:05d}",
                "",
                f"hash-{index:05d}",
                f"chain/{index:05d}",
                0,
            )
        )
    with database.connect() as connection:
        connection.executemany(
            "INSERT INTO backup_nodes (game_id, parent_id, node_kind, branch_name,"
            " created_at, title, note, content_hash, storage_relpath, is_safety)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            nodes,
        )
        rows = connection.execute(
            "SELECT id FROM backup_nodes WHERE game_id = ? ORDER BY id", (game_id,)
        ).fetchall()
    return [int(row[0]) for row in rows]


def write_text_files(
    root: Path, count: int, *, size: int = 32 * 1024
) -> Sequence[Path]:
    """在 ``root`` 下写 ``count`` 个固定大小文件, 返回路径列表(快照基准用)."""
    root.mkdir(parents=True, exist_ok=True)
    payload = b"x" * size
    paths: list[Path] = []
    for index in range(count):
        target = root / f"part-{index:04d}.dat"
        target.write_bytes(payload)
        paths.append(target)
    return paths
