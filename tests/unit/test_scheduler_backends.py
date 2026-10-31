"""真实 apscheduler 后端与手动后端的边界行为.

``BackupScheduler`` 的用例全部跑 ``ManualBackend``(不启动线程), 因此真实的
``ApschedulerBackend`` 一直缺覆盖: 它的下一次触发时间、暂停/恢复、重复移除与
释放都在真实调度器线程上执行, 出错必须转成 :class:`SchedulingError`。这里
统一用命令式关闭, 避免留下后台线程。
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime

import pytest

from archive_management.exceptions import SchedulingError
from archive_management.services.scheduler import (
    ApschedulerBackend,
    BackupScheduler,
    ManualBackend,
    default_backend,
)

pytestmark = [
    pytest.mark.domain,
    pytest.mark.normal,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("定时调度"),
    pytest.mark.story("调度后端"),
    pytest.mark.layer("unit"),
]


@pytest.fixture
def backend() -> Iterator[ApschedulerBackend]:
    """提供真实 apscheduler 后端, 用例结束后关闭后台线程."""
    instance = ApschedulerBackend()
    try:
        yield instance
    finally:
        instance.shutdown()


def test_manual_backend_has_no_timeline() -> None:
    """手动后端没有时间轴: 下次运行时间恒为 ``None``."""
    manual = ManualBackend()
    manual.add("job", interval_minutes=30, callback=lambda: None)

    assert manual.next_run_at("job") is None
    assert manual.next_run_at("missing") is None


def test_manual_backend_ignores_unknown_job_operations() -> None:
    """pause/resume/remove 遇到未登记的 id 时静默通过(重启后重建任务时会遇到)."""
    manual = ManualBackend()

    manual.pause("missing")
    manual.resume("missing")
    manual.remove("missing")

    assert manual.jobs() == {}


def test_manual_backend_trigger_requires_a_job() -> None:
    """未登记就触发属于编程错误: 直接抛错而不是静默返回."""
    manual = ManualBackend()

    with pytest.raises(SchedulingError, match="任务不存在"):
        manual.trigger("missing")


def test_manual_backend_shutdown_clears_jobs() -> None:
    """释放后任务表为空, 不会在退出流程里留下待执行回调."""
    manual = ManualBackend()
    manual.add("job", interval_minutes=30, callback=lambda: None)

    manual.shutdown()

    assert manual.jobs() == {}


def test_manual_backend_can_be_triggered_manually() -> None:
    """手动后端的 trigger 直接执行回调(供"立即运行"使用)."""
    calls: list[str] = []
    manual = ManualBackend()
    manual.add("job", interval_minutes=30, callback=lambda: calls.append("run"))

    manual.trigger("job")

    assert calls == ["run"]


def test_apscheduler_backend_reports_next_run_time(backend: ApschedulerBackend) -> None:
    """真实后端登记后会给出下一次触发时间."""
    backend.add("job-1", interval_minutes=30, callback=lambda: None)

    moment = backend.next_run_at("job-1")
    assert isinstance(moment, datetime)
    assert backend.next_run_at("missing") is None


def test_apscheduler_backend_pause_resume_and_remove(
    backend: ApschedulerBackend,
) -> None:
    """暂停/恢复/移除在真实后端上都要成功, 且移除不存在的任务不报错."""
    backend.add("job-2", interval_minutes=30, callback=lambda: None)

    backend.pause("job-2")
    backend.resume("job-2")
    backend.remove("job-2")
    backend.remove("job-2")

    assert backend.next_run_at("job-2") is None


def test_apscheduler_backend_wraps_pause_errors(
    backend: ApschedulerBackend,
) -> None:
    """对不存在任务做暂停/恢复时转成 :class:`SchedulingError`, 而不是泄漏第三方异常."""
    with pytest.raises(SchedulingError, match="定时任务操作失败"):
        backend.pause("missing-job")

    with pytest.raises(SchedulingError, match="定时任务操作失败"):
        backend.resume("missing-job")


def test_apscheduler_backend_shutdown_is_idempotent(
    backend: ApschedulerBackend,
) -> None:
    """重复释放不能报错(退出流程可能走到两次)."""
    backend.add("job-3", interval_minutes=30, callback=lambda: None)

    backend.shutdown()
    backend.shutdown()

    assert backend.next_run_at("job-3") is None


def test_default_backend_prefers_apscheduler() -> None:
    """依赖可用时默认用 apscheduler, 而不是降级成手动模式."""
    backend = default_backend()
    try:
        assert isinstance(backend, ApschedulerBackend)
    finally:
        backend.shutdown()


def test_scheduler_uses_the_injected_backend_for_pause_and_resume() -> None:
    """``enabled=False`` 登记时立刻暂停, 恢复时再交给后端恢复."""
    backend = ManualBackend()
    scheduler = BackupScheduler(backend=backend)

    entry = scheduler.schedule(11, 30, lambda: None, enabled=False)

    assert entry.enabled is False
    assert backend.jobs()["backup-11"] == (30, False)
    assert scheduler.set_enabled(11, True).enabled is True
    assert backend.jobs()["backup-11"] == (30, True)
