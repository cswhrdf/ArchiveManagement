"""统一游戏主页的用例.

主页回答的是"游戏库里有哪些游戏、各自处于什么状态": 事实来自一次聚合查询(游戏 +
存档位置 + 备份计数 + 探测关联), 再补上一次路径判定得到"存档路径是否失效"。分类、
视图、筛选与排序规则全部放在 :mod:`archive_management.domain.home`, 本模块只负责
取数、落库与审计日志, 因此规则可以脱离数据库单独测试。

主页的筛选条件(视图/平台/分类/搜索词)按"用户偏好"处理: 单独存进 ``home_state``
单行表, 下次打开主页仍是上次看到的视图; 自定义标签与归档标记存在 ``games`` 上,
属于业务数据。归档不是删除: 记录、备份与存档位置都保留, 只是默认列表不再显示。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from archive_management.domain import (
    Game,
    GameCategory,
    GameFacts,
    HomeFilter,
    HomeStats,
    PathKind,
    category_counts,
    filter_games,
    home_stats,
    normalize_tags,
    origin_counts,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import GameRepository, HomeRepository
from archive_management.services.audit import log_action
from archive_management.services.pathcheck import probe_path


@dataclass(frozen=True)
class HomeReport:
    """主页一次读取/筛选的完整结果(展示层据此渲染列表与筛选项)."""

    filter: HomeFilter
    games: tuple[GameFacts, ...]
    stats: HomeStats
    categories: tuple[tuple[GameCategory, int], ...]
    origins: tuple[tuple[str, int], ...]


def _now(moment: datetime | None) -> datetime:
    """统一取"现在": 未指定时使用当前 UTC 时间."""
    return datetime.now(UTC) if moment is None else moment


def load_facts(database: Database) -> list[GameFacts]:
    """把主页聚合查询的结果补齐为可筛选的事实(含存档路径风险判定)."""
    facts: list[GameFacts] = []
    for row in HomeRepository(database).list_rows():
        game = row.game
        if game.id is None:  # pragma: no cover - 查询总是带 id
            continue
        risk = any(
            not probe_path(path, cast(PathKind, kind)).ok
            for path, kind in row.locations
        )
        facts.append(
            GameFacts(
                game_id=str(game.id),
                name=game.name,
                origin=game.origin,
                created_at=game.created_at,
                location_count=len(row.locations),
                backup_count=row.backup_count,
                last_backup_at=row.last_backup_at,
                last_activity_at=game.last_activity_at,
                risk=risk,
                archived=game.archived,
                enabled=game.enabled,
                tags=game.tags,
            )
        )
    return facts


def build_report(
    facts: Sequence[GameFacts],
    active: HomeFilter,
    *,
    now: datetime | None = None,
) -> HomeReport:
    """按筛选条件组装主页结果(纯函数, 便于测试与复用)."""
    moment = _now(now)
    clean = active.normalized()
    return HomeReport(
        filter=clean,
        games=tuple(filter_games(facts, clean, now=moment)),
        stats=home_stats(facts, now=moment),
        categories=tuple(category_counts(facts, clean, now=moment)),
        origins=tuple(origin_counts(facts, clean, now=moment)),
    )


def load_home(database: Database, *, now: datetime | None = None) -> HomeReport:
    """读取持久化的**展示偏好**并返回主页数据.

    启动时只继承"海报/列表"与"每页条数"(见 :class:`HomeFilter` 里展示偏好与筛选条件
    的划分): 视图页签(全部/最近/待处理/已归档)、平台、分类、搜索词都属于"我上次在看
    什么", 跨启动带回来不符合预期 —— 用户 2026-10-02 反馈: "我在首页的待处理分页关的
    软件下次启动还在这个页面打开了, 这点不符合预期, 我认为只需要记住用户上次选择的是
    海报页面还是列表页面即可"。

    落库那一侧(:func:`save_filter`)仍然写整行 —— 表结构不变, 也就没有迁移; 这里只
    决定**启动时读哪些字段**, 以后要改口径就是这一处。
    """
    saved = HomeRepository(database).load_state()
    facts = load_facts(database)
    if saved is None:
        return build_report(facts, HomeFilter(), now=now)
    display = HomeFilter(layout=saved.layout, page_size=saved.page_size)
    return build_report(facts, display, now=now)


def filter_home(
    database: Database,
    active: HomeFilter,
    *,
    facts: Sequence[GameFacts] | None = None,
    now: datetime | None = None,
) -> HomeReport:
    """按给定筛选条件重算主页数据(``facts`` 可由调用方复用, 避免重复探测)."""
    source = load_facts(database) if facts is None else facts
    return build_report(source, active, now=now)


def save_filter(database: Database, active: HomeFilter) -> HomeFilter:
    """保存主页筛选条件并返回实际落库的取值."""
    saved = HomeRepository(database).save_state(active)
    log_action(
        "home.filter",
        basic=True,
        view=saved.view.value,
        origin=saved.origin,
        category=saved.category,
        search=saved.search,
    )
    return saved


def _require_game(repository: GameRepository, game_id: int) -> Game:
    """按 id 取游戏, 不存在时抛出异常."""
    game = repository.get(game_id)
    if game is None:
        raise ArchiveManagementError(f"未知游戏: {game_id}")
    return game


def set_archived(database: Database, game_id: int, archived: bool) -> Game:
    """归档或取消归档一个游戏(不删除任何数据)."""
    repository = GameRepository(database)
    _require_game(repository, game_id)
    repository.set_archived(game_id, archived)
    log_action("home.archive", game_id=game_id, archived=archived)
    return _require_game(repository, game_id)


def set_tags(database: Database, game_id: int, tags: Sequence[str]) -> tuple[str, ...]:
    """覆盖写入游戏的自定义标签, 返回清理后的标签元组."""
    repository = GameRepository(database)
    _require_game(repository, game_id)
    cleaned = normalize_tags(tags)
    repository.set_tags(game_id, cleaned)
    log_action("home.tags", game_id=game_id, count=len(cleaned))
    return cleaned


def set_origin(database: Database, game_id: int, origin: str) -> None:
    """记录游戏的来源平台(供自动导入探测结果时使用)."""
    GameRepository(database).set_origin(game_id, origin)
