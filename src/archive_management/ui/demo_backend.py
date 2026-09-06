"""演示后端.

阶段 B 用内存演示数据驱动界面;每个写操作是可阻塞的(模拟真实耗时),
由 UI 在后台线程执行。某些节点刻意失败以演示“失败反馈”路径。
"""

from __future__ import annotations

import time
from datetime import datetime

from archive_management.exceptions import ArchiveManagementError
from archive_management.ui.models import (
    BackupItem,
    GameDetail,
    GameSummary,
    TaskStatus,
)

# 刻意用于演示恢复失败的中断节点
_FAIL_RESTORE_ID = "b2"


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute)


_GAMES: list[GameSummary] = [
    GameSummary(
        game_id="outer-wilds",
        name="星际拓荒",
        has_locations=True,
        location_count=3,
        backup_count=5,
        tone="orange",
    ),
    GameSummary(
        game_id="shanhai",
        name="山海旅人",
        has_locations=True,
        location_count=1,
        backup_count=2,
        tone="blue",
    ),
    GameSummary(
        game_id="endless-space",
        name="无尽太空",
        has_locations=False,
        location_count=0,
        backup_count=0,
        tone="green",
    ),
]

_BACKUPS: dict[str, list[BackupItem]] = {
    "outer-wilds": [
        BackupItem(
            backup_id="b1",
            title="离开量子月亮前",
            created_dt=_dt(2026, 9, 6, 9, 40),
            created_label="今天 09:40",
            auto=False,
            branch_label="分支:主线",
            size_label="128 MB",
            verified=True,
            sub="手动保存的探索节点",
        ),
        BackupItem(
            backup_id=_FAIL_RESTORE_ID,
            title="尝试黑棘入口",
            created_dt=_dt(2026, 9, 5, 21, 15),
            created_label="昨天 21:15",
            auto=False,
            branch_label="主线",
            size_label="125 MB",
            verified=True,
            sub="演示:此节点恢复会失败",
        ),
        BackupItem(
            backup_id="b3",
            title="每日自动备份",
            created_dt=_dt(2026, 9, 5, 18, 0),
            created_label="昨天 18:00",
            auto=True,
            branch_label="主线",
            size_label="124 MB",
            verified=True,
        ),
        BackupItem(
            backup_id="b4",
            title="每日自动备份",
            created_dt=_dt(2026, 9, 4, 22, 31),
            created_label="2026/09/04 22:31",
            auto=True,
            branch_label="分支「黑棘」",
            size_label="122 MB",
            verified=True,
        ),
    ],
    "shanhai": [
        BackupItem(
            backup_id="s1",
            title="初始存档",
            created_dt=_dt(2026, 9, 6, 8, 0),
            created_label="今天 08:00",
            auto=False,
            branch_label="主线",
            size_label="8 MB",
            verified=True,
        ),
        BackupItem(
            backup_id="s2",
            title="每日自动备份",
            created_dt=_dt(2026, 9, 5, 12, 0),
            created_label="昨天 12:00",
            auto=True,
            branch_label="主线",
            size_label="8 MB",
            verified=True,
        ),
    ],
}

_DETAILS: dict[str, GameDetail] = {
    "outer-wilds": GameDetail(
        name="星际拓荒",
        subtitle="保存你的探索进度,随时回到任意一个分岔点。",
        main_location=r"D:\Games\OuterWilds\save",
        location_verified=True,
        location_note="主存档位置",
        last_backup_label="今天 09:40",
        last_backup_sub="手动触发 · 128 MB",
        total_backups_label="5 个节点",
        total_backups_sub="3 条分支 · 7 天活跃",
        next_backup_label="今天 12:00",
    ),
    "shanhai": GameDetail(
        name="山海旅人",
        subtitle="留住每一段旅途的转折。",
        main_location=r"D:\Games\ShanHai\save",
        location_verified=True,
        location_note="主存档位置",
        last_backup_label="今天 08:00",
        last_backup_sub="手动触发 · 8 MB",
        total_backups_label="2 个节点",
        total_backups_sub="1 条分支",
        next_backup_label="今天 12:00",
    ),
    "endless-space": GameDetail(
        name="无尽太空",
        subtitle="尚未配置存档位置,无法自动备份。",
        main_location="",
        location_verified=False,
        location_note="未配置存档位置",
        last_backup_label="—",
        last_backup_sub="",
        total_backups_label="0 个节点",
        total_backups_sub="",
        next_backup_label="—",
    ),
}


class DemoArchiveService:
    """基于内存演示数据的 :class:`ArchiveService` 实现."""

    def __init__(self, *, delay: float = 0.5) -> None:
        """用内存数据构造演示后端; ``delay`` 控制模拟耗时."""
        self._delay = delay
        self._theme = "dark"
        self._extra_backups: dict[str, int] = {}

    # -- 读接口 -------------------------------------------------------------

    def list_games(self) -> list[GameSummary]:
        """返回全部演示游戏."""
        return list(_GAMES)

    def get_detail(self, game_id: str) -> GameDetail:
        """返回单个游戏的概要数据."""
        try:
            return _DETAILS[game_id]
        except KeyError as exc:
            raise ArchiveManagementError(f"未知游戏: {game_id}") from exc

    def list_backups(self, game_id: str) -> list[BackupItem]:
        """返回某游戏的备份节点, 含本会话新增节点."""
        items = list(_BACKUPS.get(game_id, []))
        extra = self._extra_backups.get(game_id, 0)
        if extra:
            created = _dt(2026, 9, 6, 10, 0 + extra)
            items.append(
                BackupItem(
                    backup_id=f"new-{extra}",
                    title=f"手动备份 #{extra}",
                    created_dt=created,
                    created_label="刚刚",
                    auto=False,
                    branch_label="主线",
                    size_label="1 MB",
                    verified=True,
                    sub="本会话创建",
                )
            )
        return items

    def task_status(self) -> TaskStatus:
        """返回定时任务状态."""
        return TaskStatus(
            running=True,
            task_name="每日自动备份",
            progress=0.7,
            next_run_label="今天 12:00",
            target_label="本地备份目录",
            shortcut_label="Ctrl Alt S",
            theme_name=self._theme,
            backend_ok=True,
        )

    # -- 主题 ---------------------------------------------------------------
    def current_theme(self) -> str:
        """返回当前主题名."""
        return self._theme

    def set_theme(self, name: str) -> str:
        """切换主题 (dark/light), 返回生效主题."""
        self._theme = "dark" if name == "dark" else "light"
        return self._theme

    # -- 写操作(阻塞,供后台线程执行) --------------------------------------

    def run_backup_now(self, game_id: str) -> str:
        """立即备份; 无存档位置时抛错."""
        self._simulate()
        self._require_locations(game_id)
        self._extra_backups[game_id] = self._extra_backups.get(game_id, 0) + 1
        return f"备份完成:{self._name_of(game_id)} 已保存当前状态"

    def run_restore(self, game_id: str, backup_id: str) -> str:
        """恢复到指定节点; 特定节点刻意失败以演示错误路径."""
        self._simulate()
        if backup_id == _FAIL_RESTORE_ID:
            raise ArchiveManagementError("恢复中断:检测到游戏进程正在运行,已取消操作")
        return f"已恢复「{backup_id}」到 {self._name_of(game_id)}"

    def run_create_branch(self, game_id: str, backup_id: str, branch_name: str) -> str:
        """从指定节点创建分支."""
        self._simulate()
        return f"已从 {backup_id} 创建分支「{branch_name}」"

    def run_export(self, game_id: str) -> str:
        """导出选中游戏."""
        self._simulate()
        return f"已导出 {self._name_of(game_id)}"

    # -- 内部 ---------------------------------------------------------------

    def _require_locations(self, game_id: str) -> None:
        if not self._game(game_id).has_locations:
            raise ArchiveManagementError("该游戏未配置存档位置,无法备份")

    def _game(self, game_id: str) -> GameSummary:
        for game in _GAMES:
            if game.game_id == game_id:
                return game
        raise ArchiveManagementError(f"未知游戏: {game_id}")

    def _name_of(self, game_id: str) -> str:
        return self._game(game_id).name

    def _simulate(self) -> None:
        if self._delay > 0:
            time.sleep(self._delay)
