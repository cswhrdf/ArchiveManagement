"""定期备份调度与重复任务保护的单元测试(阶段 D 第 3 条)."""

from __future__ import annotations

import pytest

from archive_management.exceptions import SchedulingError
from archive_management.services.scheduler import (
    MIN_INTERVAL_MINUTES,
    BackupScheduler,
    ManualBackend,
    format_interval,
    parse_interval,
)

pytestmark = [
    pytest.mark.domain,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("定时调度"),
    pytest.mark.story("定时备份"),
    pytest.mark.layer("unit"),
]


@pytest.mark.parametrize(
    ("text", "expected"),
    [("30m", 30), ("2h", 120), ("1d", 1440), ("45", 45), (" 15M ", 15)],
)
def test_parse_interval_accepts_documented_forms(text: str, expected: int) -> None:
    assert parse_interval(text) == expected


@pytest.mark.parametrize("text", ["", "   ", "abc", "0m", "-5", "90s"])
def test_parse_interval_rejects_invalid_input(text: str) -> None:
    with pytest.raises(SchedulingError):
        parse_interval(text)


@pytest.mark.parametrize(
    ("minutes", "expected"), [(30, "30m"), (60, "1h"), (1440, "1d"), (90, "90m")]
)
def test_format_interval_roundtrips(minutes: int, expected: str) -> None:
    assert format_interval(minutes) == expected
    if minutes % 60 == 0 or minutes < 60:
        assert parse_interval(format_interval(minutes)) == minutes


def test_schedule_replaces_job_instead_of_duplicating() -> None:
    backend = ManualBackend()
    scheduler = BackupScheduler(backend=backend)
    scheduler.schedule(1, 30, lambda: None)
    scheduler.schedule(1, 60, lambda: None)

    assert list(backend.jobs()) == ["backup-1"]
    assert backend.jobs()["backup-1"] == (60, True)
    assert scheduler.get(1) is not None
    assert scheduler.get(1).interval_minutes == 60  # type: ignore[union-attr]


def test_schedule_rejects_too_small_interval() -> None:
    scheduler = BackupScheduler(backend=ManualBackend())
    with pytest.raises(SchedulingError):
        scheduler.schedule(1, MIN_INTERVAL_MINUTES - 1, lambda: None)


def test_trigger_runs_callback_and_tracks_running_games() -> None:
    calls: list[str] = []
    scheduler = BackupScheduler(backend=ManualBackend())
    scheduler.schedule(7, 30, lambda: calls.append("run"))

    assert scheduler.running_games() == ()
    assert scheduler.trigger(7) is True
    assert calls == ["run"]
    assert scheduler.running_games() == ()


def test_duplicate_trigger_is_skipped_not_queued() -> None:
    """同一次运行中再次触发应被记为跳过, 而不是并发执行."""
    scheduler: BackupScheduler

    def reentrant() -> None:
        scheduler.trigger(3)

    scheduler = BackupScheduler(backend=ManualBackend())
    scheduler.schedule(3, 30, reentrant)

    scheduler.trigger(3)

    assert scheduler.skipped_count(3) == 1


def test_callback_error_is_recorded_and_cleared() -> None:
    failures = {"fail": True}

    def flaky() -> None:
        if failures["fail"]:
            raise RuntimeError("磁盘写入失败")

    scheduler = BackupScheduler(backend=ManualBackend())
    scheduler.schedule(5, 30, flaky)

    scheduler.trigger(5)
    assert scheduler.last_error(5) == "磁盘写入失败"

    failures["fail"] = False
    scheduler.trigger(5)
    assert scheduler.last_error(5) is None


def test_trigger_unknown_game_raises() -> None:
    scheduler = BackupScheduler(backend=ManualBackend())
    with pytest.raises(SchedulingError):
        scheduler.trigger(99)


def test_set_enabled_pauses_and_resumes() -> None:
    backend = ManualBackend()
    scheduler = BackupScheduler(backend=backend)
    scheduler.schedule(2, 30, lambda: None)

    paused = scheduler.set_enabled(2, False)
    assert paused.enabled is False
    assert backend.jobs()["backup-2"] == (30, False)

    resumed = scheduler.set_enabled(2, True)
    assert resumed.enabled is True
    assert backend.jobs()["backup-2"] == (30, True)


def test_set_enabled_unknown_game_raises() -> None:
    scheduler = BackupScheduler(backend=ManualBackend())
    with pytest.raises(SchedulingError):
        scheduler.set_enabled(404, True)


def test_unschedule_removes_job_and_reports_existence() -> None:
    backend = ManualBackend()
    scheduler = BackupScheduler(backend=backend)
    scheduler.schedule(4, 30, lambda: None)

    assert scheduler.unschedule(4) is True
    assert scheduler.unschedule(4) is False
    assert backend.jobs() == {}
    assert scheduler.entries() == ()


def test_shutdown_is_idempotent_and_blocks_new_jobs() -> None:
    backend = ManualBackend()
    scheduler = BackupScheduler(backend=backend)
    scheduler.schedule(6, 30, lambda: None)

    scheduler.shutdown()
    scheduler.shutdown()

    assert backend.jobs() == {}
    assert scheduler.entries() == ()
    with pytest.raises(SchedulingError):
        scheduler.schedule(6, 30, lambda: None)


def test_entries_are_sorted_by_game_id() -> None:
    scheduler = BackupScheduler(backend=ManualBackend())
    scheduler.schedule(9, 30, lambda: None)
    scheduler.schedule(2, 30, lambda: None)
    assert [entry.game_id for entry in scheduler.entries()] == [2, 9]
