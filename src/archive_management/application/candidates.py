"""存档路径候选的校验、入库与确认.

平台清单(Steam 的 ``remotecache.vdf``)只能给出"疑似存档"的位置, 因此流程
分成两步: 探测结果先经校验落库为候选, 用户确认后才写进 ``save_locations``。

安全约束: 主目录、盘符根以及游戏安装目录本身(或包含它的目录)会被标记为危险
(``risk_reason`` 非空), 这类候选即使被确认也会被拒绝 —— 探测结果不能自己决定
把哪些目录卷进备份。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from archive_management.domain import (
    Game,
    SaveCandidate,
    SaveCandidateStatus,
    SaveLocation,
    SavePathCandidate,
    SaveSource,
)
from archive_management.exceptions import ArchiveManagementError, SaveCandidateError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
    SaveCandidateRepository,
    SaveLocationRepository,
)
from archive_management.services.audit import log_action, log_failure, redacted_path
from archive_management.services.pathcheck import (
    dangerous_target_reason,
    normalize_path,
)
from archive_management.services.platform_adapters import SaveCandidateSource
from archive_management.services.platform_scan import path_health

SUGGEST_ACTION = "candidates.suggest"
CONFIRM_ACTION = "candidates.confirm"


@dataclass(frozen=True)
class CandidateReport:
    """一次候选探测的结果统计."""

    found: int
    created: int
    updated: int
    dangerous: int
    candidates: tuple[SaveCandidate, ...]


def suggest_candidates(
    database: Database,
    game_id: int,
    source: SaveCandidateSource,
    *,
    platform: str = "steam",
    install_dir: str | Path | None = None,
) -> CandidateReport:
    """探测存档路径候选, 校验后落库(状态 ``suggested``).

    ``source`` 是平台适配器给出的候选来源(Steam 为
    :class:`~archive_management.services.steam_cloud.SteamCloudSource`);
    ``install_dir`` 为游戏安装目录, 用于识别"把整个游戏目录当存档"的危险候选,
    省略时按该游戏已导入的探测结果回查。
    """
    game = _require_game(database, game_id)
    resolved_install = _resolve_install_dir(database, game_id, install_dir)
    app_id = _app_id(game)
    produced = source.candidates(app_id, install_dir=resolved_install)
    repository = SaveCandidateRepository(database)
    locations = SaveLocationRepository(database)
    created = updated = dangerous = 0
    stored: list[SaveCandidate] = []
    for item in produced:
        candidate = _build_candidate(
            game_id,
            item,
            platform=platform,
            platform_game_id=app_id,
            install_dir=resolved_install,
            locations=locations,
        )
        entity, is_new = repository.upsert(candidate)
        created += int(is_new)
        updated += int(not is_new)
        dangerous += int(bool(entity.risk_reason))
        stored.append(entity)
    log_action(
        SUGGEST_ACTION,
        game_id=game_id,
        platform=platform,
        found=len(produced),
        created=created,
        updated=updated,
        dangerous=dangerous,
    )
    return CandidateReport(
        found=len(produced),
        created=created,
        updated=updated,
        dangerous=dangerous,
        candidates=tuple(stored),
    )


def pending_candidates(database: Database, game_id: int) -> list[SaveCandidate]:
    """返回某个游戏尚未处理的候选(界面用它列出待确认项)."""
    return SaveCandidateRepository(database).list_for_game(game_id, status="suggested")


def confirm_candidate(database: Database, candidate_id: int) -> SaveLocation:
    """用户确认一条候选: 只有这一步才会写进存档位置表.

    重复确认是幂等的 —— 路径若已在存档位置里, 只把候选标记为已确认, 不再新增
    一行; 危险候选直接拒绝并记一条失败日志。
    """
    candidates = SaveCandidateRepository(database)
    candidate = _require_candidate(candidates, candidate_id)
    if candidate.risk_reason:
        log_failure(
            CONFIRM_ACTION,
            candidate_id=candidate_id,
            path=redacted_path(candidate.path),
            reason=candidate.risk_reason,
        )
        raise SaveCandidateError(
            f"该路径被判定为危险目标({candidate.risk_reason}), 不能作为存档位置: "
            f"{candidate.path}"
        )
    locations = SaveLocationRepository(database)
    # 比对与落库前先规范化: 判重比的是**字符串相等**(见 duplicate_of), 只有两侧都是
    # 规范化形式才等价于"同一个文件夹"。候选平时由 _build_candidate 规范化后写入,
    # 但这里不能依赖上游 —— 历史数据或将来新增的写入方一旦漏了规范化, 这里会静默
    # 多出一条指向同一目录的位置(而且多出来的那条也是非规范化形式)。
    path = normalize_path(candidate.path)
    location = locations.duplicate_of(candidate.game_id, path)
    if location is None:
        location = locations.add(
            SaveLocation(
                game_id=candidate.game_id,
                path=path,
                path_kind=candidate.path_kind,
                source=_save_source(candidate.platform),
                is_primary=not locations.list_for_game(candidate.game_id),
            )
        )
    candidates.set_status(candidate_id, "confirmed")
    log_action(
        CONFIRM_ACTION,
        candidate_id=candidate_id,
        game_id=candidate.game_id,
        location_id=location.id,
        path=redacted_path(candidate.path),
    )
    return location


def _save_source(platform: str) -> SaveSource:
    """把平台标识映射成存档位置允许的来源取值.

    ``save_locations.source`` 只有 ``steam``/``manual`` 两种(见建表语句的
    CHECK), 目前只有 Steam 适配器会产出候选, 其余平台按其输入性质归为手工。
    """
    return "steam" if platform == "steam" else "manual"


def _build_candidate(
    game_id: int,
    item: SavePathCandidate,
    *,
    platform: str,
    platform_game_id: str,
    install_dir: Path | None,
    locations: SaveLocationRepository,
) -> SaveCandidate:
    """把一条平台候选转换成待落库的候选(含路径校验与危险标记)."""
    path = normalize_path(item.path)
    protected = () if install_dir is None else (str(install_dir),)
    status: SaveCandidateStatus = "suggested"
    if locations.duplicate_of(game_id, path) is not None:
        # 该路径已经是存档位置(用户手动添加过), 直接记为已确认, 不再重复询问.
        status = "confirmed"
    return SaveCandidate(
        game_id=game_id,
        platform=platform,
        platform_game_id=platform_game_id,
        path=path,
        path_kind=item.path_kind,
        reason_code=item.reason_code,
        detail=item.detail,
        relative_path=item.relative_path,
        confidence=item.confidence,
        health=path_health(path),
        risk_reason=dangerous_target_reason(
            path, protected=protected, protect_subpaths=False
        )
        or "",
        status=status,
        found_at=datetime.now(UTC),
    )


def _require_game(database: Database, game_id: int) -> Game:
    """读取游戏记录, 不存在时给出可读错误."""
    game = GameRepository(database).get(game_id)
    if game is None:
        raise ArchiveManagementError(f"未知游戏: {game_id}")
    return game


def _require_candidate(
    repository: SaveCandidateRepository, candidate_id: int
) -> SaveCandidate:
    """读取候选记录, 不存在时给出可读错误."""
    candidate = repository.get(candidate_id)
    if candidate is None:
        raise ArchiveManagementError(f"未知存档候选: {candidate_id}")
    return candidate


def _app_id(game: Game) -> str:
    """返回游戏对应的平台游戏标识(Steam 为 AppID 文本)."""
    if game.steam_app_id is None:
        raise ArchiveManagementError(f"游戏「{game.name}」没有平台标识, 无法探测存档")
    return str(game.steam_app_id)


def _resolve_install_dir(
    database: Database, game_id: int, explicit: str | Path | None
) -> Path | None:
    """确定游戏的安装目录: 优先用调用方给的, 否则回查已导入的探测结果.

    **依赖一条不变量**: 探测结果里 ``status='imported'`` 的行永远不会被清理
    (见 ``application.discovery._prune_candidates`` 的说明) —— 它们带着 ``game_id``,
    是这里唯一的兜底来源。只清"待处理/已忽略"的行不会影响这条路径。
    """
    if explicit is not None:
        return Path(normalize_path(str(explicit)))
    repository = CandidateRepository(database)
    for candidate in repository.list_all():
        if candidate.game_id == game_id:
            return Path(candidate.install_dir)
    return None
