"""本地游戏探测与监控目录的用例.

把 :mod:`archive_management.services.platform_scan` 的探测结果落到数据库, 并
实现用户围绕"探测结果"需要的四类操作:

- **监控目录维护**: 新增/编辑/启用停用/删除, 新增与编辑都要通过路径校验
  (存在、是文件夹、可读、不是盘符根目录或用户主目录)与重复检测;
- **周期扫描**: :func:`scan_library` 扫描平台清单与已启用的监控目录, 把结果
  写入候选表; 用户的"已导入/已忽略"决定不会被下一次扫描覆盖, 与游戏库同名的
  候选会被自动标记为"已纳入库";
- **候选处理**: 导入为游戏、忽略、恢复、修正路径、加入监控目录;
- **可追溯**: 高风险动作(扫描、导入、监控目录增删)写入审计日志。

探测失败(平台清单损坏、目录无权限、注册表不可读)只记录结果摘要与日志,
不会抛出到界面, 用户始终可以手动添加游戏与存档位置。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from archive_management.domain import (
    Game,
    GameCandidate,
    MonitoredDirectory,
)
from archive_management.domain.discovery import normalize_game_name
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
    IgnoredCandidateRepository,
    MonitoredDirectoryRepository,
)
from archive_management.services.audit import log_action, log_failure, redacted_path
from archive_management.services.exclusions import Exclusions, load_exclusions
from archive_management.services.pathcheck import normalize_path
from archive_management.services.platform_scan import (
    LocalGameScanner,
    ScanRoots,
    app_id_from_manifest,
    default_roots,
    path_health,
)


@dataclass(frozen=True)
class ScanReport:
    """一次本机探测的结果摘要(界面据此拼提示文案)."""

    # 参与扫描的监控目录数量(含已停用的, 用于说明"配置了几个目录").
    monitored: int
    # 已启用的监控目录数量(真正参与扫描).
    active: int
    total: int
    added: int
    updated: int
    # 自动识别为"已纳入库"的候选数量.
    linked: int
    # 路径缺失/不可读/高风险, 暂不可直接导入的候选数量.
    unusable: int
    # 命中平台工具排除清单、被默认隐藏(已忽略)的候选数量.
    excluded: int = 0
    # 这次扫描清理掉(磁盘上已经没有, 且不是已导入)的旧候选数量.
    pruned: int = 0
    errors: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """探测过程是否没有出现意外错误."""
        return not self.errors


def _directory_key(path: str) -> str:
    """把路径规范化为用于去重比较的键(忽略大小写与尾部斜杠)."""
    return str(path).rstrip("\\/").casefold()


def _require_directory(raw: str, *, context: str) -> str:
    """校验一个目录并返回规范化路径; 不满足条件时抛出可理解的异常."""
    normalized = normalize_path(raw)
    health = path_health(normalized)
    if health == "unsafe":
        raise ArchiveManagementError(
            f"{context}不能是盘符根目录或用户主目录等高风险位置: {normalized}"
        )
    if health == "missing":
        raise ArchiveManagementError(f"{context}不存在或无法访问: {normalized}")
    if health == "not_directory":
        raise ArchiveManagementError(f"{context}必须是文件夹: {normalized}")
    if health == "unreadable":
        raise ArchiveManagementError(f"{context}没有读取权限: {normalized}")
    return normalized


def _require_monitored(
    repository: MonitoredDirectoryRepository, directory_id: int
) -> MonitoredDirectory:
    """按 id 取监控目录, 不存在时抛出异常."""
    directory = repository.get(directory_id)
    if directory is None:
        raise ArchiveManagementError(f"未知监控目录: {directory_id}")
    return directory


def _require_candidate(
    repository: CandidateRepository, candidate_id: int
) -> GameCandidate:
    """按 id 取候选, 不存在时抛出异常."""
    candidate = repository.get(candidate_id)
    if candidate is None:
        raise ArchiveManagementError(f"未知探测结果: {candidate_id}")
    return candidate


# ---------------------------------------------------------------- 监控目录管理


def add_monitored_directory(
    database: Database, path: str, *, note: str = ""
) -> MonitoredDirectory:
    """新增一个监控目录; 路径不可用或重复时抛出异常."""
    normalized = _require_directory(path, context="监控目录")
    repository = MonitoredDirectoryRepository(database)
    if repository.duplicate_of(normalized) is not None:
        log_action(
            "monitor.add",
            result="rejected",
            reason="duplicate",
            path=redacted_path(normalized),
        )
        raise ArchiveManagementError(f"该目录已在监控列表中: {normalized}")
    created = repository.add(MonitoredDirectory(path=normalized, note=note.strip()))
    log_action(
        "monitor.add",
        directory_id=created.id,
        path=redacted_path(normalized),
    )
    return created


def update_monitored_directory(
    database: Database,
    directory_id: int,
    *,
    path: str | None = None,
    note: str | None = None,
) -> MonitoredDirectory:
    """修改监控目录的路径或备注; 新路径重复时抛出异常."""
    repository = MonitoredDirectoryRepository(database)
    directory = _require_monitored(repository, directory_id)
    new_path = directory.path
    if path is not None and normalize_path(path) != directory.path:
        new_path = _require_directory(path, context="监控目录")
        if repository.duplicate_of(new_path, exclude_id=directory_id) is not None:
            log_action(
                "monitor.update",
                directory_id=directory_id,
                result="rejected",
                reason="duplicate",
                path=redacted_path(new_path),
            )
            raise ArchiveManagementError(f"该目录已在监控列表中: {new_path}")
    updated = repository.update(
        MonitoredDirectory(
            id=directory.id,
            path=new_path,
            enabled=directory.enabled,
            note=directory.note if note is None else note.strip(),
            created_at=directory.created_at,
            last_scan_at=directory.last_scan_at,
            last_scan_status=directory.last_scan_status,
        )
    )
    log_action(
        "monitor.update",
        directory_id=directory_id,
        path=redacted_path(updated.path),
    )
    return updated


def set_monitored_enabled(
    database: Database, directory_id: int, enabled: bool
) -> MonitoredDirectory:
    """启用或停用一个监控目录(停用后不再参与扫描)."""
    repository = MonitoredDirectoryRepository(database)
    directory = _require_monitored(repository, directory_id)
    updated = repository.update(
        MonitoredDirectory(
            id=directory.id,
            path=directory.path,
            enabled=enabled,
            note=directory.note,
            created_at=directory.created_at,
            last_scan_at=directory.last_scan_at,
            last_scan_status=directory.last_scan_status,
        )
    )
    log_action(
        "monitor.toggle",
        directory_id=directory_id,
        enabled=enabled,
        path=redacted_path(directory.path),
    )
    return updated


def remove_monitored_directory(database: Database, directory_id: int) -> None:
    """删除一个监控目录记录(磁盘上的目录与已发现的候选保持不变)."""
    repository = MonitoredDirectoryRepository(database)
    directory = _require_monitored(repository, directory_id)
    repository.delete(directory_id)
    log_action(
        "monitor.remove",
        directory_id=directory_id,
        path=redacted_path(directory.path),
    )


# -------------------------------------------------------------------- 扫描


def scan_library(
    database: Database,
    *,
    scanner: LocalGameScanner | None = None,
    roots: ScanRoots | None = None,
    exclusions: Exclusions | None = None,
) -> ScanReport:
    """扫描平台安装目录与监控目录, 并刷新候选表.

    ``scanner``/``roots`` 用于注入替身(测试或演示); 缺省按当前环境构造真实
    探测器。游戏库中已存在同名游戏的候选会被标记为"已纳入库"; 上次扫描发现、
    这次没再出现的候选会重新判定路径健康状态(游戏被卸载后界面仍能给出明确
    提示, 而不是永远停在"可用")。``exclusions`` 是平台官方工具的排除清单,
    命中且仍待处理的候选会被默认标记为"已忽略", 详见
    :mod:`archive_management.services.exclusions`。
    """
    repository = MonitoredDirectoryRepository(database)
    directories = repository.list_all()
    active = [item.path for item in directories if item.enabled]
    engine = (
        scanner if scanner is not None else LocalGameScanner(roots or default_roots())
    )
    active_exclusions = exclusions if exclusions is not None else load_exclusions()
    _mark_scanned(repository, directories, when=datetime.now(UTC))

    errors: list[str] = []
    found = _scan_found(engine, active, errors)
    candidates = CandidateRepository(database)
    ignored = IgnoredCandidateRepository(database)
    seen: set[str] = set()
    counts = _store_candidates(
        candidates,
        found,
        known=_known_games(database),
        seen=seen,
        exclusions=active_exclusions,
        ignored=ignored,
    )
    _refresh_candidate_health(candidates, seen=seen)
    # 先把自己标为已忽略的名字都记下来(包括之前那几次扫描标的), 再清掉磁盘上已经没有的
    # 旧候选 —— 忽略决定按名字留在表里, 记录本身不再占着列表。
    pruned = _prune_candidates(candidates, ignored, seen=seen)

    log_action(
        "discovery.scan",
        basic=True,
        monitored=len(directories),
        active=len(active),
        total=len(found),
        added=counts.added,
        updated=counts.updated,
        linked=counts.linked,
        unusable=counts.unusable,
        excluded=counts.excluded,
        pruned=pruned,
    )
    return ScanReport(
        monitored=len(directories),
        active=len(active),
        total=len(found),
        added=counts.added,
        updated=counts.updated,
        linked=counts.linked,
        unusable=counts.unusable,
        excluded=counts.excluded,
        pruned=pruned,
        errors=tuple(errors),
    )


@dataclass
class _ScanCounts:
    """一次扫描的统计口径: 新增 / 更新 / 已在库 / 路径不可用 / 自动隐藏."""

    added: int = 0
    updated: int = 0
    linked: int = 0
    unusable: int = 0
    excluded: int = 0


def _mark_scanned(
    repository: MonitoredDirectoryRepository,
    directories: list[MonitoredDirectory],
    *,
    when: datetime,
) -> None:
    """把每个监控目录的最近扫描时间与路径健康状态写回(路径失效也要留痕)."""
    for directory in directories:
        if directory.id is not None:  # pragma: no branch - 目录行来自仓储, id 必然存在
            repository.mark_scan(
                directory.id, status=path_health(directory.path), when=when
            )


def _scan_found(
    engine: LocalGameScanner, active: list[str], errors: list[str]
) -> list[GameCandidate]:
    """执行一次扫描; 失败时记审计日志并把原因收进 ``errors``(不向上抛)."""
    try:
        return engine.scan(monitored=active)
    except Exception as exc:
        log_failure("discovery.scan", error=str(exc))
        errors.append(str(exc))
        return []


def _known_games(database: Database) -> dict[str, Game]:
    """已入库游戏按名称比较键索引, 用来判断候选是否已经在库里."""
    return {
        normalize_game_name(game.name): game
        for game in GameRepository(database).list()
        if game.id
    }


def _link_existing(
    candidates: CandidateRepository, stored: GameCandidate, *, known: dict[str, Game]
) -> bool:
    """候选项与库里同名游戏对上时标记为"已纳入库", 返回是否真的标了."""
    if stored.status != "new" or stored.id is None:
        return False
    match = known.get(normalize_game_name(stored.name))
    if match is None or match.id is None:
        return False
    candidates.set_status(stored.id, "imported", game_id=match.id)
    return True


def _store_candidates(
    candidates: CandidateRepository,
    found: list[GameCandidate],
    *,
    known: dict[str, Game],
    seen: set[str],
    exclusions: Exclusions,
    ignored: IgnoredCandidateRepository,
) -> _ScanCounts:
    """把扫描结果写入候选表, 顺带统计各类数量(同时记录这次见过的路径).

    "按名字记住的忽略"在这里生效: 一条**新**候选只要名字在忽略表里, 建出来就直接是
    已忽略 —— 用户忽略过的游戏重新装上/重新扫到不会又冒回待处理。
    """
    counts = _ScanCounts()
    ignored_keys = ignored.keys()
    for candidate in found:
        seen.add(_directory_key(candidate.install_dir))
        if candidate.health != "ok":
            counts.unusable += 1
        stored, created = candidates.upsert(candidate)
        if created:
            counts.added += 1
        else:
            counts.updated += 1
        if _hide_excluded(candidates, stored, exclusions=exclusions):
            counts.excluded += 1
            continue
        if _hide_ignored(candidates, stored, keys=ignored_keys):
            counts.excluded += 1
            continue
        if _link_existing(candidates, stored, known=known):
            counts.linked += 1
    return counts


def _prune_candidates(
    candidates: CandidateRepository,
    ignored: IgnoredCandidateRepository,
    *,
    seen: set[str],
    only_unseen: bool = True,
) -> int:
    """清掉不再需要的候选记录, 返回删了几条.

    规则(用户 2026-10-03 的要求: 重新扫描时清空旧记录, 忽略只按名字记):

    * **已忽略的**: 名字先记进 ``ignored_candidates``, 然后连同记录一起删 —— 磁盘上
      已经没有、用户也不想要的条目不该继续占着列表; 以后同名游戏再出现时会自动回到
      已忽略(见 :func:`_hide_ignored`);
    * **已导入的: 一条都不删**。它们带着 ``game_id``(删除游戏时的退回依据), 并且是
      :func:`archive_management.application.candidates._resolve_install_dir`
      在游戏没显式给安装目录时的**唯一来源** —— 删了会让"探测存档位置"退化成失败。
      数量受游戏库规模约束, 不会堆积。
    * 其余(待处理)的: ``only_unseen`` 为真时只删这次没扫到的(重新扫描的收尾); 手工
      "清空扫描结果"时传 ``False``, 一次清干净。
    """
    doomed: list[int] = []
    for item in candidates.list_all():
        if item.id is None or item.status == "imported":
            continue
        if only_unseen and _directory_key(item.install_dir) in seen:
            continue
        if item.status == "ignored":
            ignored.remember(item.name)
        doomed.append(item.id)
    removed = candidates.delete_many(doomed)
    if removed:
        log_action("discovery.prune", basic=True, removed=removed, unseen=only_unseen)
    return removed


def clear_scan_results(database: Database) -> int:
    """清空探测结果(保留已导入的候选与按名字记住的忽略), 返回删了几条.

    界面上是一个带确认的危险动作(用户 2026-10-03): 想从头再扫一遍时按它, 不必等到下次
    扫描的收尾。已导入的候选不在此列 —— 它们是游戏的安装目录来源(见
    :func:`_prune_candidates` 的说明)。
    """
    candidates = CandidateRepository(database)
    ignored = IgnoredCandidateRepository(database)
    removed = _prune_candidates(candidates, ignored, seen=set(), only_unseen=False)
    log_action("discovery.clear", basic=True, removed=removed)
    return removed


def _hide_ignored(
    candidates: CandidateRepository, stored: GameCandidate, *, keys: set[str]
) -> bool:
    """名字在忽略表里的新候选直接标为已忽略, 返回是否真的标了."""
    if stored.status != "new" or stored.id is None:
        return False
    if normalize_game_name(stored.name) not in keys:
        return False
    candidates.set_status(stored.id, "ignored", game_id=stored.game_id)
    return True


def _hide_excluded(
    candidates: CandidateRepository, stored: GameCandidate, *, exclusions: Exclusions
) -> bool:
    """命中排除清单且仍待处理的候选改判为"已忽略", 返回是否真的隐藏了它.

    已导入/已忽略的记录一律不动: 排除清单是"默认"行为, 不能覆盖用户的决定。
    """
    if stored.status != "new" or stored.id is None:
        return False
    matched = exclusions.match(
        platform=stored.source,
        app_id=candidate_app_id(stored),
        name=stored.name,
    )
    if matched is None:
        return False
    candidates.set_status(stored.id, "ignored", game_id=stored.game_id)
    log_action(
        "discovery.exclude",
        candidate_id=stored.id,
        name=stored.name,
        reason=matched.reason,
    )
    return True


def _refresh_candidate_health(
    candidates: CandidateRepository, *, seen: set[str]
) -> None:
    """为这次没有出现的候选重新判定路径健康状态.

    健康状态是客观事实(游戏被卸载/目录被删), 与用户的"已忽略"决定无关, 因此
    对所有候选都刷新, 但不动 status 与 game_id。
    """
    for item in candidates.list_all():
        if item.id is None or _directory_key(item.install_dir) in seen:
            continue
        health = path_health(item.install_dir)
        if health != item.health:
            candidates.set_health(item.id, health)


# ---------------------------------------------------------------- 候选处理


def import_candidate(
    database: Database, candidate_id: int, *, name: str | None = None
) -> Game:
    """把一条候选导入为游戏记录, 并把它标记为"已导入".

    导入只创建游戏记录: 探测到的路径是**安装目录**, 不是存档目录, 因此不会
    自动注册成存档位置(那会造成整份游戏被当成存档备份)。用户随后可在游戏
    设置里添加真正的存档路径。
    """
    candidates = CandidateRepository(database)
    candidate = _require_candidate(candidates, candidate_id)
    games = GameRepository(database)
    if candidate.status == "imported" and candidate.game_id is not None:
        existing = games.get(candidate.game_id)
        # 删除游戏时外键会把候选退回待处理, 因此这条游戏行必然还在 ——
        # 这个 if 只会走 True 一个方向(False 那支不可达), 所以标 no branch 而不是 no cover.
        if existing is not None:  # pragma: no branch - 已导入的候选必然指向存在的游戏
            raise ArchiveManagementError(f"该探测结果已导入为游戏「{existing.name}」")
    final_name = (name if name is not None else candidate.name).strip()
    if not final_name:
        raise ArchiveManagementError("游戏名称不能为空")
    # 同名拦截: 候选记录会被重新扫描的收尾清掉(用户 2026-10-03 的"清空重扫"),
    # 已导入这条状态不再是可靠依据 —— 按名字再挡一道, 否则改过名的那条候选会被重复导入成
    # 两款同名游戏。代价是"两份同名游戏"也挡了(需要的话只能先改名再导)。
    known = _known_games(database).get(normalize_game_name(final_name))
    if known is not None:
        raise ArchiveManagementError(f"同名游戏「{known.name}」已在库中")
    game = games.add(
        Game(name=final_name, original_name=candidate.name, origin=candidate.source)
    )
    if game.id is None:  # pragma: no cover - add() 总是返回 id
        raise ArchiveManagementError("导入游戏失败: 未返回游戏 id")
    app_id = candidate_app_id(candidate)
    if app_id is not None:
        # AppID 跟着游戏走: 之后才能用它读云端存档清单与缓存图片.
        game = games.update(game.model_copy(update={"steam_app_id": app_id}))
    candidates.set_status(candidate_id, "imported", game_id=game.id)
    log_action(
        "discovery.import",
        candidate_id=candidate_id,
        game_id=game.id,
        name=final_name,
        source=candidate.source,
        path=redacted_path(candidate.install_dir),
    )
    return game


def candidate_app_id(candidate: GameCandidate) -> int | None:
    """从探测结果里推断平台游戏标识(Steam 的 AppID 在清单文件名里).

    只有 Steam 清单来源的候选谈得上 AppID; 其它平台与监控目录返回 ``None``,
    游戏保持"没有平台标识"的状态。
    """
    if candidate.source != "steam":
        return None
    app_id = app_id_from_manifest(candidate.detail)
    return int(app_id) if app_id else None


def ignore_candidate(database: Database, candidate_id: int) -> GameCandidate:
    """把一条候选标记为"已忽略"(后续扫描不会重新变成待处理).

    已关联的游戏保持不变, 这样用户之后执行"恢复"时还能识别出它已在库中。同时把**名字**
    记进 ``ignored_candidates``: 候选记录本身会被重新扫描的收尾清掉, 而"这款游戏忽略过"
    这个决定要留下来(它重新出现时直接回到已忽略)。
    """
    candidates = CandidateRepository(database)
    candidate = _require_candidate(candidates, candidate_id)
    IgnoredCandidateRepository(database).remember(candidate.name)
    updated = candidates.set_status(candidate_id, "ignored", game_id=candidate.game_id)
    log_action("discovery.ignore", candidate_id=candidate_id)
    return updated


def restore_candidate(database: Database, candidate_id: int) -> GameCandidate:
    """把忽略过的候选恢复为"待处理"(若游戏仍在库中则直接标记为已纳入库)."""
    candidates = CandidateRepository(database)
    candidate = _require_candidate(candidates, candidate_id)
    IgnoredCandidateRepository(database).forget(candidate.name)
    game = None
    if candidate.game_id is not None:
        game = GameRepository(database).get(candidate.game_id)
    if game is not None and game.id is not None:
        updated = candidates.set_status(candidate_id, "imported", game_id=game.id)
    else:
        updated = candidates.set_status(candidate_id, "new", game_id=None)
    log_action("discovery.restore", candidate_id=candidate_id)
    return updated


def relocate_candidate(
    database: Database, candidate_id: int, path: str
) -> GameCandidate:
    """修正候选的安装路径(用户手动纠正探测结果)."""
    candidates = CandidateRepository(database)
    _require_candidate(candidates, candidate_id)
    normalized = _require_directory(path, context="安装路径")
    duplicate = candidates.find_by_dir(normalized)
    if duplicate is not None and duplicate.id != candidate_id:
        raise ArchiveManagementError(f"该路径已存在于探测结果中: {normalized}")
    updated = candidates.set_install_dir(candidate_id, normalized)
    candidates.set_health(candidate_id, path_health(normalized))
    log_action(
        "discovery.relocate",
        candidate_id=candidate_id,
        path=redacted_path(normalized),
    )
    return updated


def add_candidate_as_monitored(
    database: Database, candidate_id: int
) -> MonitoredDirectory:
    """把候选所在的**上一层目录**加入监控列表.

    候选本身通常是某一个游戏的目录, 把它自己加进监控列表没有意义; 真正有用的
    是它所在的"游戏总目录"(如 ``D:/Games``), 这样下次扫描时该目录下新装的
    游戏会被自动发现。
    """
    candidate = _require_candidate(CandidateRepository(database), candidate_id)
    parent = str(Path(candidate.install_dir).parent)
    return add_monitored_directory(database, parent, note=candidate.name)
