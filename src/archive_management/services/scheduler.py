"""定期备份调度服务(阶段 D 第 3 条).

调度器只负责"什么时候触发", 不关心备份怎么落盘: 触发后调用注入的回调,
由 :class:`~archive_management.application.backup.BackupService` 完成实际工作.

设计要点:

- **重复任务保护**: 同一游戏的备份不会并发执行. 底层调度器设置
  ``max_instances=1``/``coalesce``, 本服务再叠加一把按游戏的非阻塞锁,
  定时任务、全局快捷键与手动按钮同时触发时只有一次真正执行, 其余被记为
  "跳过"而不是排队堆积.
- **失败不中断调度**: 单次执行抛出异常只记录 ``last_error``, 后续周期继续.
- **退出即释放**: :meth:`BackupScheduler.shutdown` 幂等, 应用退出时调用,
  确保后台线程被回收.
- **后端可替换**: 默认使用 ``apscheduler``, 导入失败时退回不启动线程的
  :class:`ManualBackend`, 使无调度器环境仍能手动触发并保持 UI 可用.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from archive_management.exceptions import SchedulingError

logger = logging.getLogger(__name__)

MIN_INTERVAL_MINUTES = 1
DEFAULT_INTERVAL_MINUTES = 30

_UNIT_MINUTES = {"m": 1, "h": 60, "d": 1440}


def parse_interval(text: str) -> int:
    """把 ``"30m"`` / ``"2h"`` / ``"1d"`` / ``"45"`` 解析为分钟数.

    不带单位的纯数字按分钟处理; 小于 :data:`MIN_INTERVAL_MINUTES` 的周期被
    视为非法, 避免误配置导致备份风暴.
    """
    clean = text.strip().lower()
    if not clean:
        raise SchedulingError("备份周期不能为空")
    unit = clean[-1]
    factor = _UNIT_MINUTES.get(unit, 1)
    number = clean[:-1] if unit in _UNIT_MINUTES else clean
    try:
        value = int(number)
    except ValueError as exc:
        raise SchedulingError(f"无法解析备份周期: {text}") from exc
    minutes = value * factor
    if minutes < MIN_INTERVAL_MINUTES:
        raise SchedulingError(f"备份周期不能小于 {MIN_INTERVAL_MINUTES} 分钟")
    return minutes


def format_interval(minutes: int) -> str:
    """把分钟数还原为精简周期文本(60 -> ``"1h"``, 1440 -> ``"1d"``)."""
    if minutes > 0 and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes > 0 and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


@dataclass(frozen=True)
class ScheduledEntry:
    """一个已登记的定期备份任务."""

    game_id: int
    interval_minutes: int
    enabled: bool = True
    next_run_at: datetime | None = None


class BackendScheduler(Protocol):
    """底层调度后端: 只管登记/暂停/释放任务."""

    def add(
        self,
        job_id: str,
        *,
        interval_minutes: int,
        callback: Callable[[], None],
    ) -> None:
        """登记(或替换)一个周期任务."""
        ...

    def remove(self, job_id: str) -> None:
        """移除任务; 不存在时静默返回."""
        ...

    def pause(self, job_id: str) -> None:
        """暂停任务但不移除配置."""
        ...

    def resume(self, job_id: str) -> None:
        """恢复被暂停的任务."""
        ...

    def next_run_at(self, job_id: str) -> datetime | None:
        """返回任务下次触发时间(不可知时返回 None)."""
        ...

    def shutdown(self) -> None:
        """释放全部资源, 幂等."""
        ...


class ManualBackend:
    """不启动线程的调度后端: 只记录任务, 由调用方显式触发.

    用于测试与无调度器依赖的环境(此时 UI 仍可通过"立即备份"工作).
    """

    def __init__(self) -> None:
        """初始化空的任务表."""
        self._jobs: dict[str, tuple[int, Callable[[], None], bool]] = {}

    def add(
        self,
        job_id: str,
        *,
        interval_minutes: int,
        callback: Callable[[], None],
    ) -> None:
        """登记任务, 覆盖同 id 的旧任务."""
        self._jobs[job_id] = (interval_minutes, callback, True)

    def remove(self, job_id: str) -> None:
        """移除任务."""
        self._jobs.pop(job_id, None)

    def pause(self, job_id: str) -> None:
        """暂停任务."""
        entry = self._jobs.get(job_id)
        if entry is not None:
            self._jobs[job_id] = (entry[0], entry[1], False)

    def resume(self, job_id: str) -> None:
        """恢复任务."""
        entry = self._jobs.get(job_id)
        if entry is not None:
            self._jobs[job_id] = (entry[0], entry[1], True)

    def next_run_at(self, job_id: str) -> datetime | None:
        """手动后端没有时间轴, 永远返回 None."""
        del job_id
        return None

    def shutdown(self) -> None:
        """清空任务表."""
        self._jobs.clear()

    def jobs(self) -> dict[str, tuple[int, bool]]:
        """返回 ``{job_id: (周期分钟, 是否启用)}``, 供测试断言."""
        return {job_id: (entry[0], entry[2]) for job_id, entry in self._jobs.items()}

    def trigger(self, job_id: str) -> None:
        """立即执行一次任务(仅为测试与"立即运行"提供)."""
        entry = self._jobs.get(job_id)
        if entry is None:
            raise SchedulingError(f"任务不存在: {job_id}")
        entry[1]()


class ApschedulerBackend:
    """基于 ``apscheduler`` 的后台调度后端."""

    def __init__(self) -> None:
        """启动后台调度线程; apscheduler 不可用时抛出异常."""
        try:
            from apscheduler.schedulers.background import BackgroundScheduler
        except ImportError as exc:  # pragma: no cover - 依赖缺失时的降级路径
            raise SchedulingError(f"无法导入 apscheduler: {exc}") from exc
        self._scheduler = BackgroundScheduler()
        self._scheduler.start()

    def add(
        self,
        job_id: str,
        *,
        interval_minutes: int,
        callback: Callable[[], None],
    ) -> None:
        """按固定间隔登记任务, 同 id 覆盖, 不并发执行."""
        self._scheduler.add_job(
            callback,
            "interval",
            minutes=interval_minutes,
            id=job_id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
        )

    def remove(self, job_id: str) -> None:
        """移除任务."""
        self._remove(job_id, silent=True)

    def pause(self, job_id: str) -> None:
        """暂停任务."""
        self._pause_resume("pause_job", job_id)

    def resume(self, job_id: str) -> None:
        """恢复任务."""
        self._pause_resume("resume_job", job_id)

    def next_run_at(self, job_id: str) -> datetime | None:
        """返回下次触发时间."""
        job = self._scheduler.get_job(job_id)
        if job is None:
            return None
        return getattr(job, "next_run_time", None)

    def shutdown(self) -> None:
        """停止后台调度线程."""
        if not self._scheduler.running:
            return
        try:
            self._scheduler.shutdown(wait=False)
        except Exception as exc:  # pragma: no cover - 关闭期异常只记录
            logger.warning("释放调度器失败: %s", exc)

    def _remove(self, job_id: str, *, silent: bool) -> None:
        """移除任务, 按需吞掉 JobLookupError."""
        try:
            self._scheduler.remove_job(job_id)
        except Exception as exc:
            if not silent:
                raise SchedulingError(f"移除定时任务失败: {job_id}: {exc}") from exc

    def _pause_resume(self, method: str, job_id: str) -> None:
        """调用 apscheduler 的 pause_job/resume_job."""
        try:
            getattr(self._scheduler, method)(job_id)
        except Exception as exc:
            raise SchedulingError(f"定时任务操作失败: {method} {job_id}") from exc


def default_backend() -> BackendScheduler:
    """返回默认调度后端; apscheduler 不可用时降级为手动后端."""
    try:
        return ApschedulerBackend()
    except SchedulingError as exc:
        logger.warning("定时备份已降级为手动模式: %s", exc)
        return ManualBackend()


def job_id_for(game_id: int) -> str:
    """返回某个游戏的调度任务 id."""
    return f"backup-{game_id}"


class BackupScheduler:
    """定期备份任务注册表 + 重复任务保护."""

    def __init__(self, *, backend: BackendScheduler | None = None) -> None:
        """绑定调度后端(默认 :func:`default_backend`)."""
        self._backend: BackendScheduler = backend or default_backend()
        self._entries: dict[int, ScheduledEntry] = {}
        self._callbacks: dict[int, Callable[[], None]] = {}
        self._running: set[int] = set()
        self._skipped: dict[int, int] = {}
        self._errors: dict[int, str] = {}
        self._gate = threading.Lock()
        self._closed = False

    # -- 注册与查询 ---------------------------------------------------------

    def schedule(
        self,
        game_id: int,
        interval_minutes: int,
        callback: Callable[[], None],
        *,
        enabled: bool = True,
    ) -> ScheduledEntry:
        """登记(或替换)某游戏的定期备份任务."""
        if self._closed:
            raise SchedulingError("调度器已释放, 无法登记新任务")
        if interval_minutes < MIN_INTERVAL_MINUTES:
            raise SchedulingError(f"备份周期不能小于 {MIN_INTERVAL_MINUTES} 分钟")
        job_id = job_id_for(game_id)

        def runner(game: int = game_id) -> None:
            self._run(game)

        self._backend.add(
            job_id,
            interval_minutes=interval_minutes,
            callback=runner,
        )
        if not enabled:
            self._backend.pause(job_id)
        self._callbacks[game_id] = callback
        entry = ScheduledEntry(
            game_id=game_id,
            interval_minutes=interval_minutes,
            enabled=enabled,
            next_run_at=self._backend.next_run_at(job_id),
        )
        self._entries[game_id] = entry
        return entry

    def unschedule(self, game_id: int) -> bool:
        """取消某游戏的定期备份任务; 原本没有任务时返回 False."""
        existed = game_id in self._entries
        self._backend.remove(job_id_for(game_id))
        self._entries.pop(game_id, None)
        self._callbacks.pop(game_id, None)
        with self._gate:
            self._skipped.pop(game_id, None)
            self._errors.pop(game_id, None)
        return existed

    def set_enabled(self, game_id: int, enabled: bool) -> ScheduledEntry:
        """启用/暂停某游戏的定期备份任务."""
        entry = self._entries.get(game_id)
        if entry is None:
            raise SchedulingError(f"游戏 {game_id} 没有定时备份任务")
        job_id = job_id_for(game_id)
        if enabled:
            self._backend.resume(job_id)
        else:
            self._backend.pause(job_id)
        updated = ScheduledEntry(
            game_id=game_id,
            interval_minutes=entry.interval_minutes,
            enabled=enabled,
            next_run_at=self._backend.next_run_at(job_id),
        )
        self._entries[game_id] = updated
        return updated

    def entries(self) -> tuple[ScheduledEntry, ...]:
        """返回全部已登记任务(按游戏 id 排序)."""
        return tuple(self._entries[game_id] for game_id in sorted(self._entries))

    def get(self, game_id: int) -> ScheduledEntry | None:
        """返回某游戏的任务配置."""
        return self._entries.get(game_id)

    def refresh(self, game_id: int) -> ScheduledEntry | None:
        """重新读取后端的下次触发时间, 返回最新条目(无任务时返回 None).

        ``next_run_at`` 在登记时从后端读一次, 每次执行后后端会重新排下一次
        触发时间; 不刷新的话界面会一直显示上一次(已经过去的)时间。
        """
        entry = self._entries.get(game_id)
        if entry is None:
            return None
        updated = ScheduledEntry(
            game_id=entry.game_id,
            interval_minutes=entry.interval_minutes,
            enabled=entry.enabled,
            next_run_at=self._backend.next_run_at(job_id_for(game_id)),
        )
        self._entries[game_id] = updated
        return updated

    def running_games(self) -> tuple[int, ...]:
        """返回当前正在执行备份的游戏 id."""
        with self._gate:
            return tuple(sorted(self._running))

    def skipped_count(self, game_id: int) -> int:
        """返回因重复触发而被跳过的次数."""
        with self._gate:
            return self._skipped.get(game_id, 0)

    def last_error(self, game_id: int) -> str | None:
        """返回某游戏最近一次定时备份的错误摘要."""
        with self._gate:
            return self._errors.get(game_id)

    # -- 执行与释放 ---------------------------------------------------------

    def trigger(self, game_id: int) -> bool:
        """立即触发一次备份; 已有同名任务在跑时返回 False."""
        callback = self._callbacks.get(game_id)
        if callback is None:
            raise SchedulingError(f"游戏 {game_id} 没有定时备份任务")
        return self._run(game_id)

    def shutdown(self) -> None:
        """释放调度器与全部任务; 幂等, 供应用退出时调用."""
        if self._closed:
            return
        self._closed = True
        for game_id in list(self._entries):
            self._backend.remove(job_id_for(game_id))
        self._entries.clear()
        self._callbacks.clear()
        self._backend.shutdown()
        with self._gate:
            self._running.clear()

    def _run(self, game_id: int) -> bool:
        """执行一次回调, 通过非阻塞锁实现重复任务保护."""
        with self._gate:
            if game_id in self._running:
                self._skipped[game_id] = self._skipped.get(game_id, 0) + 1
                logger.info("定时备份跳过: 游戏 %s 已有备份在进行", game_id)
                return False
            self._running.add(game_id)
        callback = self._callbacks.get(game_id)
        try:
            if callback is not None:
                callback()
        except Exception as exc:
            with self._gate:
                self._errors[game_id] = str(exc)
            logger.warning("定时备份失败(游戏 %s): %s", game_id, exc)
        else:
            with self._gate:
                self._errors.pop(game_id, None)
        finally:
            with self._gate:
                self._running.discard(game_id)
        return True
