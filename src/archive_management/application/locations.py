"""删除原始存档位置的用例.

"删除原始存档位置"默认进入系统回收站, 删除前要求再次确认并显示
目标路径与文件数量. 这里把流程拆成两个可独立调用的步骤:

- :func:`plan_location_removal`: 只读预检。统计影响范围(文件/目录/链接数量与
  体积), 并判断路径是否落在禁止删除的范围(盘符根目录、用户主目录、应用
  备份根目录及其父目录)内;
- :func:`remove_save_location`: 核对用户输入的确认名称, 把原始目录移入系统
  回收站, 然后删除该位置的记录(主位置被删除时把主标记交给下一个位置).

删除动作通过 :mod:`archive_management.services.audit` 写入日志文件(操作日志
不落库), 且**永远不会永久删除**: 回收站后端失败时按"未删除"处理并原样上报
错误(所有高风险操作都可追溯)。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from archive_management.domain import PathKind, SaveLocation
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    GameRepository,
    SaveLocationRepository,
)
from archive_management.services.audit import log_action, log_failure, redacted_path
from archive_management.services.pathcheck import (
    PathSummary,
    dangerous_target_reason,
    summarize_path,
)
from archive_management.services.trash import TrashBackend, send_to_trash

# 预检被拒绝的原因代码(UI 用 i18n 映射为文案).
REMOVAL_MISSING = "missing"


@dataclass(frozen=True)
class LocationRemovalPlan:
    """删除原始存档位置前的预检结果(只读)."""

    game_name: str
    path: str
    path_kind: PathKind
    files: int
    directories: int
    symlinks: int
    total_size: int
    exists: bool
    blocked_reason: str | None = None

    @property
    def blocked(self) -> bool:
        """是否被预检拦下(不允许删除)."""
        return self.blocked_reason is not None


@dataclass(frozen=True)
class LocationRemovalResult:
    """一次成功删除的结果摘要."""

    path: str
    files: int
    total_size: int


def plan_location_removal(
    database: Database, location_id: int, *, protect: Sequence[Path] = ()
) -> LocationRemovalPlan:
    """统计待删除位置的影响范围并检查路径安全性.

    先做安全判定: 被拒绝的路径(盘符根目录、用户主目录、应用备份目录等)
    不会再做目录遍历, 以免为了报错而扫遍整个磁盘。
    """
    location = _require_location(database, location_id)
    game_name = _game_name(database, location.game_id)
    exists = Path(location.path).exists()
    reason = dangerous_target_reason(
        location.path, protected=tuple(str(item) for item in protect)
    )
    if reason is None and not exists:
        reason = REMOVAL_MISSING
    summary = PathSummary() if reason is not None else summarize_path(location.path)
    return LocationRemovalPlan(
        game_name=game_name,
        path=location.path,
        path_kind=location.path_kind,
        files=summary.files,
        directories=summary.directories,
        symlinks=summary.symlinks,
        total_size=summary.total_size,
        exists=exists,
        blocked_reason=reason,
    )


def remove_save_location(
    database: Database,
    location_id: int,
    *,
    confirm_name: str,
    trash: TrashBackend | None = None,
    protect: Sequence[Path] = (),
) -> LocationRemovalResult:
    """把某个存档位置的原始目录移入回收站, 并删除其记录.

    ``confirm_name`` 必须与游戏名称一致(忽略大小写与首尾空白), 用于防止误删
    其它游戏的存档; 路径被保护、不存在或回收站不可用时都会抛出可理解的异常。
    ``trash`` 缺省使用系统回收站(惰性解析, 便于测试注入替身)。
    """
    trash_backend = send_to_trash if trash is None else trash
    location = _require_location(database, location_id)
    location_id_value = location.id
    if location_id_value is None:
        raise ArchiveManagementError(f"未知存档位置: {location_id}")
    game_id = location.game_id
    plan = plan_location_removal(database, location_id, protect=protect)
    if not _confirm_matches(plan.game_name, confirm_name):
        log_action(
            "location.delete",
            game_id=game_id,
            location_id=location_id_value,
            result="rejected",
            reason="confirm_mismatch",
        )
        raise ArchiveManagementError("确认名称与游戏名称不一致, 已取消删除")
    if plan.blocked_reason is not None:
        log_action(
            "location.delete",
            game_id=game_id,
            location_id=location_id_value,
            result="rejected",
            reason=plan.blocked_reason,
            path=redacted_path(plan.path),
        )
        raise ArchiveManagementError(f"该路径不允许删除: {plan.path}")
    log_action(
        "location.delete",
        game_id=game_id,
        location_id=location_id_value,
        kind=plan.path_kind,
        files=plan.files,
        bytes=plan.total_size,
        path=redacted_path(plan.path),
    )
    try:
        trash_backend(plan.path)
    except Exception as exc:
        log_failure(
            "location.delete",
            game_id=game_id,
            location_id=location_id_value,
            error=str(exc),
        )
        raise
    locations = SaveLocationRepository(database)
    try:
        locations.delete(location_id_value)
        _reassign_primary(locations, game_id)
    except Exception as exc:
        log_failure(
            "location.delete",
            game_id=game_id,
            location_id=location_id_value,
            error=str(exc),
        )
        raise
    log_action(
        "location.delete",
        game_id=game_id,
        location_id=location_id_value,
        result="succeeded",
        files=plan.files,
        path=redacted_path(plan.path),
    )
    return LocationRemovalResult(
        path=plan.path, files=plan.files, total_size=plan.total_size
    )


def _reassign_primary(locations: SaveLocationRepository, game_id: int) -> None:
    """被删除的是主位置时, 把主标记交给该游戏剩余的第一个位置."""
    remaining = locations.list_for_game(game_id)
    if not remaining or any(item.is_primary for item in remaining):
        return
    if remaining[0].id is None:
        return
    locations.make_primary(game_id, remaining[0].id)


def _confirm_matches(game_name: str, typed: str) -> bool:
    """判断用户输入的确认名称是否与游戏名称一致."""
    clean = typed.strip()
    if not clean:
        return False
    return clean.casefold() == game_name.strip().casefold()


def _game_name(database: Database, game_id: int) -> str:
    """返回游戏名称(记录缺失时返回空串)."""
    game = GameRepository(database).get(game_id)
    return "" if game is None else game.name


def _require_location(database: Database, location_id: int) -> SaveLocation:
    """要求存档位置存在, 返回实体."""
    location = SaveLocationRepository(database).get(location_id)
    if location is None or location.id is None:
        raise ArchiveManagementError(f"未知存档位置: {location_id}")
    return location
