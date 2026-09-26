"""演示后端.

用内存演示数据驱动界面;每个写操作是可阻塞的(模拟真实耗时),
由 UI 在后台线程执行。某些节点刻意失败以演示“失败反馈”路径。
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from archive_management.application import home as home_cases
from archive_management.application.backup import MAX_NOTE_LENGTH
from archive_management.application.games import ActivationOutcome
from archive_management.application.imports import (
    STRATEGIES,
    STRATEGY_NEW,
    STRATEGY_SKIP,
    BatchInspection,
    ImportInspection,
)
from archive_management.application.locations import LocationRemovalPlan
from archive_management.application.restore import RestorePlan, RestoreTarget
from archive_management.domain import (
    DEFAULT_KEEP_AUTO,
    ActivationState,
    ArtworkKind,
    BackupNode,
    DeletionMode,
    DeletionPlan,
    GameFacts,
    HomeFilter,
    normalize_tags,
    plan_deletion,
)
from archive_management.domain.activation import REASON_UNAVAILABLE
from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import current_locale, tr
from archive_management.services.audit import log_action
from archive_management.services.export_format import ARCHIVE_SUFFIX
from archive_management.services.naming import game_folder, game_slug
from archive_management.services.pathcheck import normalize_path
from archive_management.services.processes import ProcessProbe
from archive_management.ui.models import (
    BackupItem,
    CandidateItem,
    GameDetail,
    GameSummary,
    HomeBoard,
    ImportChoice,
    LocationItem,
    MonitoredDirItem,
    SavePathSuggestion,
    ScanSummary,
    ScheduleItem,
    TaskStatus,
    home_board,
)

# 刻意用于演示恢复失败的中断节点
_FAIL_RESTORE_ID = "b2"


def _chosen_strategy(choice: ImportChoice | None) -> str:
    """这一款游戏的选择策略: 没有给选择的那一款按默认值(新建)处理.

    与用例层 :meth:`ImportService.import_batch` 的默认值一致(缺项 = 新建, 且不导入
    任何存档位置), 否则演示后端报出来的"跳过了几款"会和真实后端对不上。
    """
    return STRATEGY_NEW if choice is None else choice.strategy


def _dt(year: int, month: int, day: int, hour: int, minute: int) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _stamp(moment: datetime) -> str:
    """把时间格式化为本地时区的展示文本(与真实后端一致)."""
    return moment.astimezone().strftime("%Y/%m/%d %H:%M")


# 演示用的译名(真实后端按 AppID 向平台问一次; 这里写死两种语言, 便于演示与用例).
_DEMO_TRANSLATIONS: dict[str, dict[str, str]] = {
    "cand-1": {"zh-CN": "星际拓荒", "en": "Outer Wilds"},
    "cand-6": {"zh-CN": "星露谷物语", "en": "Stardew Valley"},
}


def _demo_localized(candidate_id: str) -> str:
    """探测结果在当前界面语言下的译名(没有译名的候选返回空串, 界面回落原名)."""
    return _DEMO_TRANSLATIONS.get(candidate_id, {}).get(current_locale(), "")


def _now_label() -> str:
    """返回当前时间的展示文本(演示扫描时间用)."""
    return datetime.now().astimezone().strftime("%Y/%m/%d %H:%M")


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
            branch_label="主线",
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
            parent_id="b1",
        ),
        BackupItem(
            backup_id="b3",
            title="",
            created_dt=_dt(2026, 9, 5, 18, 0),
            created_label="昨天 18:00",
            auto=True,
            branch_label="主线",
            size_label="124 MB",
            verified=True,
            parent_id="b2",
        ),
        BackupItem(
            backup_id="b4",
            title="",
            created_dt=_dt(2026, 9, 4, 22, 31),
            created_label="2026/09/04 22:31",
            auto=True,
            branch_label="主线",
            size_label="122 MB",
            verified=True,
            parent_id="b3",
        ),
        BackupItem(
            backup_id="b5",
            title="黑棘",
            created_dt=_dt(2026, 9, 6, 11, 5),
            created_label="今天 11:05",
            auto=False,
            branch_label="分支:黑棘",
            size_label="126 MB",
            verified=True,
            sub="从黑棘入口开始的另一条线",
            parent_id="b3",
            is_branch=True,
            branch_name="黑棘",
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
            parent_id="s1",
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
        original_name="星际拓荒",
        storage_folder=game_folder("星际拓荒", [r"D:\Games\OuterWilds\save"]),
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
        original_name="山海旅人",
        storage_folder=game_folder("山海旅人", [r"D:\Games\ShanHai\save"]),
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

# 演示数据: 监控目录与探测到的候选游戏.
_MONITORED_DIRS: tuple[MonitoredDirItem, ...] = (
    MonitoredDirItem(
        directory_id="dir-1",
        path="D:\\Games",
        enabled=True,
        note="自定义游戏目录",
        health="ok",
        last_scan_label="2026/09/13 09:20",
    ),
    MonitoredDirItem(
        directory_id="dir-2",
        path="E:\\Portable Games",
        enabled=False,
        note="移动硬盘(已停用)",
        health="missing",
        last_scan_label="2026/09/11 21:05",
    ),
)

_CANDIDATES: tuple[CandidateItem, ...] = (
    CandidateItem(
        candidate_id="cand-1",
        name="星际拓荒",
        install_dir="D:\\Steam\\steamapps\\common\\OuterWilds",
        source="steam",
        confidence="high",
        status="imported",
        health="ok",
        detail="appmanifest_753640.acf",
        game_id="outer-wilds",
        save_paths=(
            SavePathSuggestion(
                path="C:\\Users\\demo\\Documents\\Outer Wilds",
                path_kind="directory",
            ),
        ),
    ),
    CandidateItem(
        candidate_id="cand-2",
        name="空洞骑士",
        install_dir="D:\\Games\\HollowKnight",
        source="monitored",
        confidence="medium",
        status="new",
        health="ok",
        detail="D:\\Games",
        save_supported=False,
    ),
    CandidateItem(
        candidate_id="cand-3",
        name="泰拉瑞亚",
        install_dir="D:\\Epic\\Terraria",
        source="epic",
        confidence="high",
        status="new",
        health="ok",
        detail="terraria.item",
        save_supported=False,
    ),
    CandidateItem(
        candidate_id="cand-4",
        name="巫师三",
        install_dir="D:\\GOG\\Witcher3",
        source="gog",
        confidence="high",
        status="new",
        health="missing",
        detail="1207664623",
        save_supported=False,
    ),
    CandidateItem(
        candidate_id="cand-5",
        name="旧版工具",
        install_dir="D:\\Games\\LegacyTool",
        source="monitored",
        confidence="medium",
        status="ignored",
        health="ok",
        detail="D:\\Games",
        save_supported=False,
    ),
    CandidateItem(
        candidate_id="cand-6",
        name="Stardew Valley",
        install_dir="D:\\Steam\\steamapps\\common\\StardewValley",
        source="steam",
        confidence="high",
        status="new",
        health="ok",
        detail="appmanifest_413150.acf",
        save_paths=(
            SavePathSuggestion(
                path="C:\\Users\\demo\\AppData\\Roaming\\StardewValley\\Saves",
                path_kind="directory",
            ),
            SavePathSuggestion(
                path="C:\\Users\\demo",
                path_kind="directory",
                dangerous=True,
            ),
        ),
    ),
)


def _delete_result_text(plan: DeletionPlan) -> str:
    """删除操作的结果文案: 连带子分支 / 顶替父节点 / 单节点三种."""
    if plan.mode is DeletionMode.CASCADE:
        return tr("result.delete_cascade", count=plan.removed_count)
    if plan.mode is DeletionMode.SHIFT:
        return tr("result.delete_shift")
    return tr("result.delete_single")


def _same_path(left: str, right: str) -> bool:
    r"""两条存档位置是否指向同一个路径(大小写不敏感, **两侧都规范化**后再比).

    演示数据里的位置路径是原样保存的展示字符串(如 ``D:\\Games\\OuterWilds\\save``),
    而用户输入会过一遍 ``normalize_path`` —— 在 POSIX 上这类"相对路径"会被拼上
    工作目录, 因此只规范化输入侧就永远比不出重复(实测 Linux 分片里漏判过一条用例:
    ``test_demo_update_location_rejects_a_path_used_by_another_location`` 报
    ``DID NOT RAISE``, 而 Windows 上恰好因为 normpath 对这类路径是幂等的而看不出来)。
    监控目录的判重本来就是这么做的(见 :meth:`add_monitored_directory`)。
    """
    return normalize_path(left).lower() == normalize_path(right).lower()


class DemoArchiveService:
    """基于内存演示数据的 :class:`ArchiveService` 实现."""

    def __init__(self, *, delay: float = 0.5) -> None:
        """用内存数据构造演示后端; ``delay`` 控制模拟耗时."""
        self._delay = delay
        self._theme = "dark"
        # 每个游戏的定时备份配置: game_id -> (周期文本, 是否启用, 保留份数).
        self._schedules: dict[str, tuple[str, bool, int]] = {
            "outer-wilds": ("1d", True, 3),
        }
        self._keep_auto = 3
        self._revision = 0
        self._items: dict[str, list[BackupItem]] = {
            game_id: list(items) for game_id, items in _BACKUPS.items()
        }
        self._current: dict[str, str | None] = {}
        self._meta: dict[str, GameSummary] = {game.game_id: game for game in _GAMES}
        self._details = dict(_DETAILS)
        self._locations: dict[str, list[LocationItem]] = {}
        self._next_game_id = 1
        self._next_location_id = 1
        # 演示监控目录与探测结果(固定数据, 不触碰真实磁盘).
        self._monitored_dirs: dict[str, MonitoredDirItem] = {
            item.directory_id: item for item in _MONITORED_DIRS
        }
        self._candidates: dict[str, CandidateItem] = {
            item.candidate_id: item for item in _CANDIDATES
        }
        self._next_dir_id = len(_MONITORED_DIRS) + 1
        # 演示主页的筛选条件、归档标记与自定义标签.
        self._home_filter = HomeFilter()
        self._archived: dict[str, bool] = dict.fromkeys(self._meta, False)
        self._tags: dict[str, tuple[str, ...]] = {key: () for key in self._meta}
        for game in _GAMES:
            existing = self._items.get(game.game_id, [])
            self._current[game.game_id] = existing[-1].backup_id if existing else None
            self._items.setdefault(game.game_id, [])
            primary = self._details[game.game_id].main_location
            if primary:
                self._locations[game.game_id] = [
                    LocationItem(
                        # 位置 id 必须带上游戏 id: 演示数据每个游戏各有一个主位置,
                        # 用固定 id 会让按 id 查找/删除命中别的游戏.
                        location_id=f"{game.game_id}-primary",
                        game_id=game.game_id,
                        path=primary,
                        path_kind="directory",
                        source="manual",
                        is_primary=True,
                        ok=True,
                        note=tr("loc.verified"),
                    )
                ]
            else:
                self._locations[game.game_id] = []

    # -- 读接口 -------------------------------------------------------------

    def list_games(self) -> list[GameSummary]:
        """返回全部演示游戏(实时计算摘要)."""
        return [self._live_summary(game_id) for game_id in self._meta]

    def get_detail(self, game_id: str) -> GameDetail:
        """返回单个游戏的概要数据."""
        self._require_game(game_id)
        return self._details[game_id]

    def list_backups(self, game_id: str) -> list[BackupItem]:
        """返回某游戏的备份节点, 并标记当前节点."""
        self._require_game(game_id)
        current = self._effective_current(game_id)
        return [
            replace(item, is_current=item.backup_id == current)
            for item in self._items.get(game_id, [])
        ]

    def task_status(self, game_id: str | None = None) -> TaskStatus:
        """返回定时任务状态(演示数据按游戏记录周期)."""
        interval, enabled, keep = self._schedule_of(game_id)
        return TaskStatus(
            running=False,
            task_name=(
                tr("task.every", interval=interval, keep=keep)
                if interval
                else tr("task.unscheduled")
            ),
            progress=0.0,
            next_run_label="今天 12:00" if interval else "—",
            target_label="本地备份目录",
            theme_name=self._theme,
            backend_ok=True,
            schedule_text=interval,
            schedule_enabled=enabled,
            keep_auto=keep,
            revision=self._revision,
        )

    def set_schedule(
        self,
        game_id: str,
        interval_text: str,
        *,
        enabled: bool = True,
        keep_auto: int = DEFAULT_KEEP_AUTO,
    ) -> TaskStatus:
        """记录演示用的定时备份周期与自动备份保留份数(按游戏)."""
        self._require_game(game_id)
        summary = self._meta[game_id]
        clean = interval_text.strip()
        if clean and self._archived.get(game_id, False):
            # 与真实后端一致: 归档游戏只保留删除/导出/取消归档/打开详情.
            raise ArchiveManagementError(
                tr("error.archived_game_schedule", name=summary.name)
            )
        if clean and not self._locations[game_id]:
            # 与真实后端一致: 没有存档位置就不能定时备份.
            raise ArchiveManagementError(tr("error.no_locations_schedule", name=""))
        if not clean:
            # 空周期 = 删除任务(暂停状态也不再保留).
            self._schedules.pop(game_id, None)
        else:
            existing = bool(self._schedule_of(game_id)[0])
            if enabled and not summary.enabled:
                # 停用中的游戏允许先配好周期, 但任务只能暂停; 已有任务时说明用户在
                # "继续", 与真实后端一样明确拒绝.
                if existing:
                    raise ArchiveManagementError(
                        tr("error.disabled_game_schedule", name=summary.name)
                    )
                enabled = False
            self._schedules[game_id] = (clean, enabled, max(1, keep_auto))
        return self.task_status(game_id)

    def _pause_schedule(self, game_id: str) -> None:
        """把演示游戏的定时任务置为暂停(保留周期配置)."""
        interval, _enabled, keep = self._schedule_of(game_id)
        if interval:
            self._schedules[game_id] = (interval, False, keep)

    def list_schedules(self) -> list[ScheduleItem]:
        """返回全部游戏的定时备份配置."""
        items: list[ScheduleItem] = []
        for summary in self.list_games():
            interval, enabled, keep = self._schedule_of(summary.game_id)
            autos = [item for item in self._items.get(summary.game_id, []) if item.auto]
            items.append(
                ScheduleItem(
                    game_id=summary.game_id,
                    game_name=summary.name,
                    interval_text=interval,
                    enabled=enabled and bool(interval),
                    keep_auto=keep,
                    next_run_label="今天 12:00" if interval and enabled else "—",
                    auto_count=len(autos),
                    tone=summary.tone,
                    has_locations=summary.has_locations,
                    game_enabled=summary.enabled,
                    archived=self._archived.get(summary.game_id, False),
                )
            )
        return items

    def storage_usage(self) -> int:
        """返回演示用的存储占用(按节点数量估算)."""
        return sum(len(items) for items in self._items.values()) * 128 * 1024 * 1024

    def _schedule_of(self, game_id: str | None) -> tuple[str, bool, int]:
        """返回某游戏的调度配置; 未指定游戏或未配置时给出空周期."""
        if game_id is None:
            return ("", True, self._keep_auto)
        return self._schedules.get(game_id, ("", True, self._keep_auto))

    def plan_delete(self, game_id: str, backup_id: str) -> DeletionPlan:
        """按分支树计算删除计划."""
        nodes, mapping = self._nodes_of(game_id)
        try:
            return plan_deletion(nodes, mapping[backup_id])
        except KeyError as exc:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            ) from exc

    def run_delete_backup(self, game_id: str, backup_id: str) -> str:
        """按分支树删除备份(同线路节点上移, 分支根节点连带子分支)."""
        self._simulate()
        self._require_game(game_id)
        plan = self.plan_delete(game_id, backup_id)
        _nodes, mapping = self._nodes_of(game_id)
        reverse = {value: key for key, value in mapping.items()}
        removed = {
            reverse[node_id] for node_id in plan.removed_ids if node_id in reverse
        }
        if plan.mode is DeletionMode.SHIFT:
            self._reparent_shifted_child(game_id, plan, reverse)
        self._items[game_id] = [
            item for item in self._items[game_id] if item.backup_id not in removed
        ]
        if self._current.get(game_id) in removed:
            self._restore_current_after_delete(game_id, plan, reverse)
        self._revision += 1
        return _delete_result_text(plan)

    def _reparent_shifted_child(
        self, game_id: str, plan: DeletionPlan, reverse: dict[int, str]
    ) -> None:
        """SHIFT 模式: 把被顶替的子分支改挂到新的父节点上."""
        if plan.shifted_child_id is None:
            return
        child_id = reverse.get(plan.shifted_child_id)
        parent_id = (
            None if plan.new_parent_id is None else reverse.get(plan.new_parent_id)
        )
        self._items[game_id] = [
            replace(item, parent_id=parent_id) if item.backup_id == child_id else item
            for item in self._items[game_id]
        ]

    def _restore_current_after_delete(
        self, game_id: str, plan: DeletionPlan, reverse: dict[int, str]
    ) -> None:
        """当前节点被删除后重算"接下来的备份挂在哪里".

        优先回退到父节点, 否则落到剩余的最新备份, 使列表里始终能看到锚点。
        """
        fallback = reverse.get(plan.new_parent_id) if plan.new_parent_id else None
        remaining = {item.backup_id for item in self._items[game_id]}
        self._current[game_id] = (
            fallback if fallback in remaining else self._effective_current(game_id)
        )

    def rename_backup(
        self, game_id: str, backup_id: str, *, title: str, note: str
    ) -> BackupItem:
        """修改演示备份的名称与描述."""
        self._require_game(game_id)
        clean_note = note.strip()
        if len(clean_note) > MAX_NOTE_LENGTH:
            raise ArchiveManagementError(
                tr("error.note_too_long", limit=MAX_NOTE_LENGTH)
            )
        items = self._items.get(game_id, [])
        updated: BackupItem | None = None
        result: list[BackupItem] = []
        for item in items:
            if item.backup_id == backup_id:
                updated = replace(
                    item, title=title.strip() or item.title, sub=clean_note
                )
                result.append(updated)
            else:
                result.append(item)
        if updated is None:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            )
        self._items[game_id] = result
        self._revision += 1
        return updated

    def cancel_active(self) -> bool:
        """演示后端没有可取消的后台操作."""
        return False

    def shutdown(self) -> None:
        """演示后端无需释放资源."""

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
        self._require_game(game_id)
        self._require_locations(game_id)
        items = self._items[game_id]
        created = _dt(2026, 9, 6, 10, len(items))
        items.append(
            BackupItem(
                backup_id=f"new-{len(items) + 1}",
                title=tr("backup.title_manual"),
                created_dt=created,
                created_label="刚刚",
                auto=False,
                branch_label=tr("backup.mainline"),
                size_label="1 MB",
                verified=True,
                sub="",
                parent_id=self._current.get(game_id),
            )
        )
        self._current[game_id] = items[-1].backup_id
        self._remember_storage_folder(game_id)
        self._revision += 1
        return tr("result.backup_done", name=self._name_of(game_id))

    def preview_restore(self, game_id: str, backup_id: str) -> RestorePlan:
        """返回演示用的恢复预检(不触碰磁盘)."""
        self._require_game(game_id)
        _nodes, mapping = self._nodes_of(game_id)
        try:
            node_id = mapping[backup_id]
        except KeyError as exc:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            ) from exc
        item = next(
            (
                entry
                for entry in self._items.get(game_id, [])
                if entry.backup_id == backup_id
            ),
            None,
        )
        if item is None:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            )
        locations = self._locations.get(game_id, [])
        targets = tuple(
            RestoreTarget(
                index=index,
                path=location.path,
                kind=location.path_kind,
                exists=True,
                writable=True,
            )
            for index, location in enumerate(locations)
        )
        return RestorePlan(
            backup_id=node_id,
            title=item.display_title,
            snapshot_ok=True,
            snapshot_reason=None,
            file_count=24,
            total_size=128 * 1024 * 1024,
            targets=targets,
            # 演示: 中断节点同时用于展示"游戏正在运行"的风险提示.
            process=ProcessProbe(checked=True, running=backup_id == _FAIL_RESTORE_ID),
            safety_point_available=bool(locations),
        )

    def run_restore(
        self,
        game_id: str,
        backup_id: str,
        *,
        safety_point: bool = True,
        force: bool = False,
    ) -> str:
        """模拟写回存档并把当前节点切到指定备份; 特定节点刻意失败.

        与真实后端一致: ``safety_point=True`` 时先在时间线里补一个安全点节点。
        """
        self._simulate()
        self._require_game(game_id)
        if backup_id == _FAIL_RESTORE_ID:
            raise ArchiveManagementError(tr("error.restore_busy"))
        del force
        items = {item.backup_id: item for item in self._items.get(game_id, [])}
        node = items.get(backup_id)
        if node is None:
            raise ArchiveManagementError(
                tr("error.unknown_backup", backup_id=backup_id)
            )
        if safety_point and self._locations.get(game_id):
            self._items[game_id].append(
                BackupItem(
                    backup_id=f"safety-{len(self._items[game_id]) + 1}",
                    title=tr("backup.title_safety_point"),
                    created_dt=_dt(2026, 9, 6, 12, len(self._items[game_id])),
                    created_label=tr("backup.safety_label_just_now"),
                    auto=False,
                    safety=True,
                    branch_label=tr("backup.mainline"),
                    size_label=node.size_label,
                    verified=True,
                    sub="",
                    parent_id=self._current.get(game_id),
                )
            )
        self._current[game_id] = backup_id
        self._revision += 1
        return tr(
            "result.restore_written",
            name=self._name_of(game_id),
            title=node.display_title,
            files=24,
        )

    def run_create_branch(self, game_id: str, backup_id: str, branch_name: str) -> str:
        """从指定节点创建分支."""
        self._simulate()
        self._require_game(game_id)
        items = self._items[game_id]
        created = _dt(2026, 9, 6, 11, len(items))
        items.append(
            BackupItem(
                backup_id=f"branch-{len(items) + 1}",
                title=branch_name,
                created_dt=created,
                created_label="刚刚",
                auto=False,
                branch_label=tr("backup.branch_label", branch=branch_name),
                size_label="1 MB",
                verified=True,
                sub="",
                parent_id=backup_id,
                is_branch=True,
                branch_name=branch_name,
            )
        )
        self._current[game_id] = items[-1].backup_id
        self._remember_storage_folder(game_id)
        self._revision += 1
        return tr("result.branch_done", backup_id=backup_id, branch_name=branch_name)

    def run_export(self, game_id: str, destination: str) -> str:
        """模拟导出(演示后端不碰文件系统, 因此不会真的写出 ``destination``).

        演示后端的内存数据没有真实快照内容可打包, 所以这里只走一遍"可阻塞的写
        操作"流程, 并明确告诉用户没有文件产生 —— 不能假装写出了包。
        """
        self._simulate()
        self._require_game(game_id)
        return tr("result.export_simulated", name=self._name_of(game_id))

    def run_export_batch(self, game_ids: Sequence[str], destination: str) -> str:
        """模拟批量导出(演示后端不碰文件系统, 因此不会真的写出 ``destination``).

        与单游戏的 :meth:`run_export` 同一套"诚实"口径: 只走一遍"可阻塞的写操作"流程,
        并明确说明没有文件产生。空选择与未知游戏仍然报错(与真实后端一致), 不假装
        成功 —— 演示后端也不该让用户以为这一批归档好了。
        """
        self._simulate()
        if not game_ids:
            raise ArchiveManagementError("批量导出需要至少 1 款游戏")
        for game_id in game_ids:
            self._require_game(game_id)
        return tr("result.export_batch_simulated", count=len(game_ids))

    def inspect_import(self, path: str) -> ImportInspection:
        """模拟体检一个导出包: 演示后端不读磁盘, 所以内容是按文件名编的.

        编出来的"包"里**没有任何存档位置与备份节点**, 游戏名也写成演示包的名字 ——
        用户在对话框里一眼就能看出这份体检不代表磁盘上的那个文件, 不会被哄着以为
        这里真的有一份可导入的备份。
        """
        self._simulate()
        return ImportInspection(
            path=Path(path),
            game_name=tr("demo.import_name", file=Path(path).stem),
            steam_app_id=None,
            platform="windows",
            origin="manual",
            tags=(),
            locations=(),
            nodes=(),
            schedule=None,
            matching_game_id=None,
        )

    def run_import(
        self,
        inspection: ImportInspection,
        *,
        strategy: str,
        target_game_id: str | None,
        locations: Mapping[int, str],
    ) -> str:
        """模拟导入(演示后端不会真的建游戏、写备份或登记存档位置).

        与 :meth:`run_export` 一样只走一遍"可阻塞的写操作"流程并明确说明什么都没
        写。目标游戏仍然要校验(合并到一个不存在的游戏与真实后端一样报错), "跳过"
        也如实回报跳过了多少份 —— 只有真的导入了才会用"已模拟导入"那句话。
        """
        self._simulate()
        if strategy not in STRATEGIES:
            raise ArchiveManagementError(f"未知的导入策略: {strategy}")
        if target_game_id is not None:
            self._require_game(target_game_id)
        if strategy == STRATEGY_SKIP:
            return tr(
                "result.import_skipped",
                name=inspection.game_name,
                skipped=inspection.backup_count,
            )
        return tr("result.import_simulated", name=inspection.game_name)

    def run_import_batch(
        self, batch: BatchInspection, choices: Mapping[str, ImportChoice]
    ) -> str:
        """模拟批量导入(演示后端不会真的建游戏、写备份或登记存档位置).

        与 :meth:`run_import` 同一套口径: 只走一遍"可阻塞的写操作"流程并说清什么都
        没写; 未知策略与不存在的目标游戏仍然报错; 整批都选了"跳过"时如实回报跳过了
        多少款, 而不是拿"已模拟导入"糊过去。
        """
        self._simulate()
        if not batch.games:
            raise ArchiveManagementError("批量包是空的")
        self._check_batch_choices(choices)
        skipped = sum(
            1
            for item in batch.games
            if _chosen_strategy(choices.get(item.entry)) == STRATEGY_SKIP
        )
        if skipped == batch.game_count:
            return tr("result.import_batch_all_skipped", games=skipped)
        return tr("result.import_batch_simulated", count=batch.game_count - skipped)

    def _check_batch_choices(self, choices: Mapping[str, ImportChoice]) -> None:
        """逐条校验用户的选择: 未知策略与不存在的目标游戏都要报错(不是静默跳过)."""
        for choice in choices.values():
            if choice.strategy not in STRATEGIES:
                raise ArchiveManagementError(f"未知的导入策略: {choice.strategy}")
            if choice.target_game_id is not None:
                self._require_game(choice.target_game_id)

    # -- 游戏与存档位置管理 -------------------------------------------------

    def add_game(self, name: str) -> GameSummary:
        """新增一个游戏并返回其摘要."""
        clean = self._clean_name(name)
        game_id = f"game-{self._next_game_id}"
        self._next_game_id += 1
        summary = GameSummary(
            game_id=game_id,
            name=clean,
            has_locations=False,
            location_count=0,
            backup_count=0,
            tone="blue",
        )
        self._meta[game_id] = summary
        self._details[game_id] = GameDetail(
            name=clean,
            subtitle=tr("detail.subtitle"),
            main_location="",
            location_verified=False,
            location_note=tr("game.no_locations_short"),
            last_backup_label="—",
            last_backup_sub="",
            total_backups_label=tr("detail.backups_none"),
            total_backups_sub="",
            next_backup_label="—",
            original_name=clean,
        )
        self._locations[game_id] = []
        self._items[game_id] = []
        self._current[game_id] = None
        return summary

    def update_game(self, game_id: str, name: str) -> GameSummary:
        """重命名游戏并返回其摘要."""
        clean = self._clean_name(name)
        self._require_game(game_id)
        self._meta[game_id] = replace(self._meta[game_id], name=clean)
        # 只改展示名称: 原始名称与备份目录名保持不变.
        self._details[game_id] = replace(self._details[game_id], name=clean)
        return self._live_summary(game_id)

    def _remember_storage_folder(self, game_id: str) -> None:
        """首次备份时记录"名称 + 存档路径"推导出的目录名(之后保持不变)."""
        detail = self._details[game_id]
        if detail.storage_folder:
            return
        paths = [item.path for item in self._locations.get(game_id, [])]
        self._details[game_id] = replace(
            detail,
            storage_folder=game_folder(detail.original_name or detail.name, paths),
        )

    def delete_export_path(self, game_id: str) -> str:
        """算出"告别包"的默认落点(演示后端只给出一条像样的路径).

        命名规则与真实后端一致(游戏名 + 时间戳 + 归档包后缀), 但演示后端全程不碰
        文件系统: 这里**只计算**路径, 不建目录也不写文件。
        """
        self._require_game(game_id)
        stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
        stem = f"{game_slug(self._name_of(game_id))}-{stamp}"
        return str(Path("exports") / f"{stem}{ARCHIVE_SUFFIX}")

    def delete_game(self, game_id: str, destination: str) -> None:
        """删除游戏记录及其存档位置, 并把它的探测候选退回待处理.

        顺序与真实后端一致(**先导出, 后删除**), 但演示后端不碰文件系统:
        ``destination`` 上不会出现任何文件, 所以这里只如实记一条"模拟导出"的审计,
        不假装写出了告别包(与 :meth:`run_export` 同一套诚实口径)。删内存数据这一步
        与原来相同。
        """
        self._require_game(game_id)
        log_action(
            "game.delete_export",
            game_id=game_id,
            result="simulated",
            note=tr("result.delete_export_simulated", name=self._name_of(game_id)),
        )
        self._meta.pop(game_id)
        self._details.pop(game_id)
        self._locations.pop(game_id, None)
        self._items.pop(game_id, None)
        self._current.pop(game_id, None)
        self._archived.pop(game_id, None)
        self._tags.pop(game_id, None)
        self._schedules.pop(game_id, None)
        # 与真实后端一致: 挂着该游戏的"已导入"候选要退回待处理, 否则它们会停在
        # "已导入"却指向一个不存在的游戏, 既看不到也导不进来.
        for candidate_id, item in list(self._candidates.items()):
            if item.game_id == game_id and item.status == "imported":
                self._candidates[candidate_id] = replace(
                    item, status="new", game_id=None
                )

    def set_game_enabled(self, game_id: str, enabled: bool) -> GameSummary:
        """启用或停用一个游戏(启用时会自动停用其它启用中的游戏)."""
        self._require_game(game_id)
        if enabled and self._archived.get(game_id, False):
            raise ArchiveManagementError(tr("error.archived_game_enable"))
        if enabled:
            for other_id, other in list(self._meta.items()):
                if other_id != game_id and other.enabled:
                    self._meta[other_id] = replace(other, enabled=False)
                    self._pause_schedule(other_id)
        self._meta[game_id] = replace(self._meta[game_id], enabled=enabled)
        if not enabled:
            self._pause_schedule(game_id)
        # 启用态会显示在详情页与管理窗口, 因此要刷新数据版本让界面重读.
        self._revision += 1
        return self._live_summary(game_id)

    def poll_activation(self, *, enabled: bool = True) -> ActivationOutcome:
        """演示后端不做自动启停: 内存数据没有可落库的跟踪状态."""
        return ActivationOutcome(state=ActivationState(), reason=REASON_UNAVAILABLE)

    def list_locations(self, game_id: str) -> list[LocationItem]:
        """返回某游戏的全部存档位置."""
        self._require_game(game_id)
        return list(self._locations[game_id])

    def add_location(self, game_id: str, *, path: str, kind: str) -> LocationItem:
        """新增一个存档位置; 重复路径抛错."""
        self._require_game(game_id)
        normalized = normalize_path(path)
        existing = self._locations[game_id]
        if any(_same_path(item.path, normalized) for item in existing):
            raise ArchiveManagementError(
                tr("error.duplicate_location", path=normalized)
            )
        item = LocationItem(
            location_id=f"demo-loc-{self._next_location_id}",
            game_id=game_id,
            path=normalized,
            path_kind=kind,  # type: ignore[arg-type]
            source="manual",
            is_primary=not existing,
            ok=True,
            note=tr("loc.verified"),
        )
        self._next_location_id += 1
        self._locations[game_id] = [*existing, item]
        return item

    def update_location(
        self,
        location_id: str,
        *,
        path: str | None = None,
        kind: str | None = None,
    ) -> LocationItem:
        """修改存档位置的路径/类型."""
        location = self._find_location(location_id)
        new_path = location.path if path is None else normalize_path(path)
        new_kind = location.path_kind if kind is None else kind
        if new_path != location.path:
            for item in self._locations[location.game_id]:
                if item.location_id != location_id and _same_path(item.path, new_path):
                    raise ArchiveManagementError(
                        tr("error.duplicate_location", path=new_path)
                    )
        updated = replace(
            location,
            path=new_path,
            path_kind=new_kind,  # type: ignore[arg-type]
        )
        self._replace_location(updated)
        return updated

    def remove_location(self, location_id: str) -> None:
        """删除一个存档位置."""
        location = self._find_location(location_id)
        remaining = [
            item
            for item in self._locations[location.game_id]
            if item.location_id != location_id
        ]
        if remaining and location.is_primary:
            remaining[0] = replace(remaining[0], is_primary=True)
        self._locations[location.game_id] = remaining

    def set_primary_location(self, game_id: str, location_id: str) -> LocationItem:
        """把某位置设为主位置."""
        self._require_game(game_id)
        target = self._find_location(location_id)
        if target.game_id != game_id:
            raise ArchiveManagementError(
                tr("error.unknown_location", location_id=location_id)
            )
        updated_list: list[LocationItem] = []
        updated_target: LocationItem | None = None
        for item in self._locations[game_id]:
            if item.location_id == location_id:
                promoted = replace(item, is_primary=True)
                updated_target = promoted
                updated_list.append(promoted)
            else:
                updated_list.append(replace(item, is_primary=False))
        self._locations[game_id] = updated_list
        if updated_target is None:
            raise ArchiveManagementError(
                tr("error.unknown_location", location_id=location_id)
            )
        return updated_target

    def verify_location(self, location_id: str) -> LocationItem:
        """重新校验位置(演示后端恒为可用)."""
        location = self._find_location(location_id)
        updated = replace(location, ok=True, note=tr("loc.verified"))
        self._replace_location(updated)
        return updated

    # -- 内部 ---------------------------------------------------------------

    def preview_location_removal(self, location_id: str) -> LocationRemovalPlan:
        """返回演示用的删除预检(影响范围是演示数据)."""
        item = self._find_location(location_id)
        return LocationRemovalPlan(
            game_name=self._name_of(item.game_id),
            path=item.path,
            path_kind=item.path_kind,
            files=18,
            directories=3,
            symlinks=0,
            total_size=64 * 1024 * 1024,
            exists=True,
            blocked_reason=None,
        )

    def delete_save_location(self, location_id: str, *, confirm_name: str) -> str:
        """模拟把原始存档目录移入回收站, 并移除该位置."""
        self._simulate()
        item = self._find_location(location_id)
        game_name = self._name_of(item.game_id)
        if confirm_name.strip().casefold() != game_name.strip().casefold():
            raise ArchiveManagementError(tr("error.location_delete_confirm"))
        remaining = [
            entry
            for entry in self._locations.get(item.game_id, [])
            if entry.location_id != location_id
        ]
        if item.is_primary and remaining:
            remaining[0] = replace(remaining[0], is_primary=True)
        self._locations[item.game_id] = remaining
        self._revision += 1
        return tr("result.location_deleted", path=item.path, count=18)

    # -- 本地游戏探测与监控目录 -------------------------------------------

    def list_monitored_directories(self) -> list[MonitoredDirItem]:
        """返回演示监控目录(按添加顺序)."""
        return list(self._monitored_dirs.values())

    def add_monitored_directory(self, path: str, *, note: str = "") -> MonitoredDirItem:
        """新增演示监控目录; 路径为空或重复时抛错."""
        clean = path.strip()
        if not clean:
            raise ArchiveManagementError(tr("error.monitor_path_empty"))
        if any(
            item.path.rstrip("\\/").casefold() == clean.rstrip("\\/").casefold()
            for item in self._monitored_dirs.values()
        ):
            raise ArchiveManagementError(tr("error.monitor_duplicate", path=clean))
        directory_id = f"dir-{self._next_dir_id}"
        self._next_dir_id += 1
        item = MonitoredDirItem(
            directory_id=directory_id,
            path=clean,
            enabled=True,
            note=note.strip(),
            health="ok",
        )
        self._monitored_dirs[directory_id] = item
        self._revision += 1
        return item

    def update_monitored_directory(
        self, directory_id: str, *, path: str | None = None, note: str | None = None
    ) -> MonitoredDirItem:
        """修改演示监控目录的路径或备注."""
        item = self._require_directory(directory_id)
        if path is not None and path.strip() != item.path:
            others = [
                entry
                for key, entry in self._monitored_dirs.items()
                if key != directory_id
            ]
            clean = path.strip()
            if not clean:
                raise ArchiveManagementError(tr("error.monitor_path_empty"))
            if any(
                entry.path.rstrip("\\/").casefold() == clean.rstrip("\\/").casefold()
                for entry in others
            ):
                raise ArchiveManagementError(tr("error.monitor_duplicate", path=clean))
            item = replace(item, path=clean)
        if note is not None:
            item = replace(item, note=note.strip())
        self._monitored_dirs[directory_id] = item
        return item

    def set_monitored_enabled(
        self, directory_id: str, enabled: bool
    ) -> MonitoredDirItem:
        """启用/停用一个演示监控目录."""
        item = replace(self._require_directory(directory_id), enabled=enabled)
        self._monitored_dirs[directory_id] = item
        return item

    def remove_monitored_directory(self, directory_id: str) -> None:
        """删除一个演示监控目录."""
        self._require_directory(directory_id)
        self._monitored_dirs.pop(directory_id)
        self._revision += 1

    def scan_candidates(self) -> ScanSummary:
        """模拟一次探测: 刷新扫描时间并返回结果摘要."""
        active = [item for item in self._monitored_dirs.values() if item.enabled]
        now = _now_label()
        for directory_id, item in self._monitored_dirs.items():
            health = "ok" if item.enabled else "missing"
            self._monitored_dirs[directory_id] = replace(
                item,
                last_scan_label=now,
                health=health if item.enabled else item.health,
            )
        candidates = list(self._candidates.values())
        return ScanSummary(
            monitored=len(self._monitored_dirs),
            active=len(active),
            total=len(candidates),
            added=0,
            updated=len(candidates),
            linked=sum(1 for item in candidates if item.status == "imported"),
            unusable=sum(1 for item in candidates if item.health != "ok"),
        )

    def list_candidates(self, *, status: str | None = None) -> list[CandidateItem]:
        """返回演示探测结果(可按处理进度筛选), 并带上当前语言的译名."""
        items = [
            replace(item, localized_name=_demo_localized(item.candidate_id))
            for item in self._candidates.values()
        ]
        if status not in (None, "", "all"):
            items = [item for item in items if item.status == status]
        return items

    def import_candidate(
        self,
        candidate_id: str,
        *,
        name: str | None = None,
        save_paths: Sequence[str] = (),
    ) -> GameSummary:
        """把演示候选导入为游戏, 并写入用户确认的存档路径."""
        item = self._require_candidate(candidate_id)
        if item.status == "imported" and item.game_id is not None:
            raise ArchiveManagementError(tr("error.candidate_imported"))
        summary = self.add_game(name if name is not None else item.name)
        self._candidates[candidate_id] = replace(
            item, status="imported", game_id=summary.game_id
        )
        wanted = [path for path in save_paths if path.strip()]
        for path in wanted:
            self.add_location(summary.game_id, path=path, kind="directory")
        self._revision += 1
        imported = replace(summary, saved_paths=len(wanted))
        self._meta[summary.game_id] = imported
        return imported

    def set_candidate_ignored(self, candidate_id: str, ignored: bool) -> CandidateItem:
        """把演示候选标记为"已忽略"或恢复为"待处理"."""
        item = self._require_candidate(candidate_id)
        updated = replace(item, status="ignored" if ignored else "new")
        self._candidates[candidate_id] = updated
        return updated

    def relocate_candidate(self, candidate_id: str, path: str) -> CandidateItem:
        """修正演示候选的安装路径."""
        item = self._require_candidate(candidate_id)
        clean = path.strip()
        if not clean:
            raise ArchiveManagementError(tr("error.candidate_path_empty"))
        updated = replace(item, install_dir=clean, health="ok")
        self._candidates[candidate_id] = updated
        return updated

    def add_candidate_as_monitored(self, candidate_id: str) -> MonitoredDirItem:
        """把演示候选所在的上一层目录加入监控列表."""
        item = self._require_candidate(candidate_id)
        parent = item.install_dir.rsplit("\\", 1)[0]
        if parent == item.install_dir:
            parent = item.install_dir.rsplit("/", 1)[0] or item.install_dir
        return self.add_monitored_directory(parent, note=item.name)

    def _require_directory(self, directory_id: str) -> MonitoredDirItem:
        item = self._monitored_dirs.get(directory_id)
        if item is None:
            raise ArchiveManagementError(
                tr("error.unknown_monitored", directory_id=directory_id)
            )
        return item

    def _require_candidate(self, candidate_id: str) -> CandidateItem:
        item = self._candidates.get(candidate_id)
        if item is None:
            raise ArchiveManagementError(
                tr("error.unknown_candidate", candidate_id=candidate_id)
            )
        return item

    # -- 图片 ---------------------------------------------------------------

    def artwork_path(self, game_id: str, kind: ArtworkKind) -> str:
        """演示后端不提供图片(界面回落名称/色块占位)."""
        return ""

    def prefetch_artwork(self) -> None:
        """演示后端不下载图片."""

    def prefetch_names(self, *, refresh: bool = False) -> None:
        """演示后端不联网探测译名(真实后端会按当前语言问一次商店)."""

    # -- 统一游戏主页 ------------------------------------------------------

    def load_home(self) -> HomeBoard:
        """返回演示主页数据(沿用内存中保存的筛选条件)."""
        return self._board()

    def apply_home_filter(self, active: HomeFilter) -> HomeBoard:
        """按给定筛选条件重算演示主页并记住该条件."""
        self._home_filter = active.normalized()
        return self._board()

    def set_game_archived(self, game_id: str, archived: bool) -> HomeBoard:
        """归档或取消归档一个演示游戏(归档会同时停用并暂停定时备份)."""
        self._require_game(game_id)
        if archived:
            self._meta[game_id] = replace(self._meta[game_id], enabled=False)
            self._pause_schedule(game_id)
        self._archived[game_id] = archived
        self._revision += 1
        return self._board()

    def set_game_tags(self, game_id: str, tags: Sequence[str]) -> HomeBoard:
        """覆盖写入演示游戏的自定义标签."""
        self._require_game(game_id)
        self._tags[game_id] = normalize_tags(tags)
        return self._board()

    def _home_facts(self) -> list[GameFacts]:
        """把演示数据换算为主页事实(备份时间/存档位置/风险)."""
        facts: list[GameFacts] = []
        for game_id, summary in self._meta.items():
            items = self._items.get(game_id, [])
            moments = [item.created_dt for item in items]
            locations = self._locations.get(game_id, [])
            facts.append(
                GameFacts(
                    game_id=game_id,
                    name=summary.name,
                    origin=self._origin_of(game_id),
                    location_count=len(locations),
                    backup_count=len(items),
                    last_backup_at=max(moments) if moments else None,
                    last_activity_at=max(moments) if moments else None,
                    risk=any(not item.ok for item in locations),
                    archived=self._archived.get(game_id, False),
                    enabled=summary.enabled,
                    tags=self._tags.get(game_id, ()),
                )
            )
        return facts

    def _origin_of(self, game_id: str) -> str:
        """演示来源: 有探测记录时沿用探测来源, 否则视为手动添加."""
        for item in self._candidates.values():
            if item.game_id == game_id:
                return item.source
        return "manual"

    def _board(self) -> HomeBoard:
        """按当前筛选条件重算演示主页(复用真实后端的映射逻辑)."""
        report = home_cases.build_report(self._home_facts(), self._home_filter)
        return home_board(report, stamp=_stamp)

    # -- 其它 ---------------------------------------------------------------

    def _require_locations(self, game_id: str) -> None:
        if not self._locations[game_id]:
            raise ArchiveManagementError(tr("error.no_locations_backup"))

    def _effective_current(self, game_id: str) -> str | None:
        """返回有效的当前节点: 指针缺失或已删除时回退到最新备份."""
        items = self._items.get(game_id, [])
        pointer = self._current.get(game_id)
        if pointer is not None and any(item.backup_id == pointer for item in items):
            return pointer
        return items[-1].backup_id if items else None

    def _nodes_of(self, game_id: str) -> tuple[list[BackupNode], dict[str, int]]:
        """把演示备份项转换为领域节点, 返回节点表与"展示 id -> 领域 id"映射.

        演示数据用字符串 id(如 ``b1``), 而删除计划需要在整型 id 上做纯计算,
        因此这里做一次稳定映射, 复用与真实后端相同的分支树规则.
        """
        self._require_game(game_id)
        items = self._items.get(game_id, [])
        mapping = {item.backup_id: index for index, item in enumerate(items, start=1)}
        nodes = [
            BackupNode(
                id=mapping[item.backup_id],
                game_id=0,
                parent_id=(
                    None if item.parent_id is None else mapping.get(item.parent_id)
                ),
                node_kind=(
                    "branch" if item.is_branch else ("auto" if item.auto else "manual")
                ),
                branch_name=item.branch_name or None,
            )
            for item in items
        ]
        return nodes, mapping

    def _require_game(self, game_id: str) -> None:
        if game_id not in self._meta:
            raise ArchiveManagementError(tr("error.unknown_game", game_id=game_id))

    def _clean_name(self, name: str) -> str:
        """去除首尾空白; 为空时抛错."""
        clean = name.strip()
        if not clean:
            raise ArchiveManagementError(tr("error.game_name_empty"))
        return clean

    def _live_summary(self, game_id: str) -> GameSummary:
        meta = self._meta[game_id]
        locations = self._locations[game_id]
        return GameSummary(
            game_id=game_id,
            name=meta.name,
            has_locations=bool(locations),
            location_count=len(locations),
            backup_count=len(self.list_backups(game_id)),
            tone=meta.tone,
            enabled=meta.enabled,
            archived=self._archived.get(game_id, False),
            saved_paths=meta.saved_paths,
            original_name=meta.original_name,
        )

    def _find_location(self, location_id: str) -> LocationItem:
        for items in self._locations.values():
            for item in items:
                if item.location_id == location_id:
                    return item
        raise ArchiveManagementError(
            tr("error.unknown_location", location_id=location_id)
        )

    def _replace_location(self, location: LocationItem) -> None:
        items = [
            location if item.location_id == location.location_id else item
            for item in self._locations[location.game_id]
        ]
        self._locations[location.game_id] = items

    def _name_of(self, game_id: str) -> str:
        return self._meta[game_id].name

    def _simulate(self) -> None:
        if self._delay > 0:
            time.sleep(self._delay)
