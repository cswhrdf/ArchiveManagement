"""全局快捷键服务的单元测试(阶段 D 第 4 条).

快捷键注册很容易失败(被占用、无权限、无图形环境), 因此测试重点是**失败
回退**: 任何失败都收敛为带原因的 :class:`HotkeyState`, 不抛出到调用方。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from archive_management.exceptions import HotkeyError
from archive_management.services.hotkeys import (
    DEFAULT_ACCELERATOR,
    GlobalHotkeyService,
    HotkeyBinding,
    HotkeyState,
    UnavailableBackend,
    default_backend,
)

pytestmark = [
    pytest.mark.domain,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("全局快捷键"),
    pytest.mark.story("快捷键触发备份"),
    pytest.mark.layer("unit"),
]


class FakeBackend:
    """可编程的快捷键后端替身."""

    def __init__(self, *, fail_with: str | None = None) -> None:
        self.registered: dict[str, Callable[[], None]] = {}
        self.unregistered: list[object] = []
        self.stopped = 0
        self.fail_with = fail_with

    def register(self, accelerator: str, callback: Callable[[], None]) -> object:
        """模拟注册; fail_with 非空时抛出 HotkeyError."""
        if self.fail_with is not None:
            raise HotkeyError(self.fail_with)
        if accelerator in self.registered:
            raise HotkeyError(f"快捷键已被占用: {accelerator}")
        self.registered[accelerator] = callback
        return accelerator

    def unregister(self, handle: object) -> None:
        """记录注销的句柄."""
        self.unregistered.append(handle)
        self.registered.pop(str(handle), None)

    def stop(self) -> None:
        """记录停止次数."""
        self.stopped += 1


def test_register_reports_success() -> None:
    backend = FakeBackend()
    service = GlobalHotkeyService(backend=backend)
    state = service.register(
        HotkeyBinding("save_now", DEFAULT_ACCELERATOR), lambda: None
    )

    assert state.registered is True
    assert state.error is None
    assert state.name == "save_now"
    assert state.accelerator == DEFAULT_ACCELERATOR
    assert DEFAULT_ACCELERATOR in backend.registered


def test_register_failure_falls_back_without_raising() -> None:
    service = GlobalHotkeyService(backend=FakeBackend(fail_with="权限不足"))
    state = service.register(
        HotkeyBinding("save_now", DEFAULT_ACCELERATOR), lambda: None
    )

    assert state.registered is False
    assert state.error == "权限不足"
    assert service.available is True


def test_register_wraps_unexpected_errors() -> None:
    class BrokenBackend(FakeBackend):
        def register(self, accelerator: str, callback: Callable[[], None]) -> object:
            raise RuntimeError("listener crashed")

    service = GlobalHotkeyService(backend=BrokenBackend())
    state = service.register(HotkeyBinding("save_now", "ctrl+s"), lambda: None)
    assert state.registered is False
    assert state.error is not None
    assert "listener crashed" in state.error


def test_service_keeps_working_after_one_failure() -> None:
    """单个快捷键失败不应影响后续注册(失败回退)."""
    backend = FakeBackend()
    service = GlobalHotkeyService(backend=backend)
    service.register(HotkeyBinding("taken", "ctrl+s"), lambda: None)
    failed = service.register(HotkeyBinding("dup", "ctrl+s"), lambda: None)
    fine = service.register(HotkeyBinding("free", "ctrl+alt+s"), lambda: None)

    assert failed.registered is False
    assert failed.error is not None
    assert fine.registered is True
    assert [state.name for state in service.states()] == ["dup", "free", "taken"]


def test_disabled_binding_is_not_sent_to_backend() -> None:
    backend = FakeBackend()
    service = GlobalHotkeyService(backend=backend)
    state = service.register(
        HotkeyBinding("save_now", DEFAULT_ACCELERATOR, enabled=False), lambda: None
    )
    assert state.registered is False
    assert state.error is None
    assert backend.registered == {}


def test_unregister_removes_binding() -> None:
    backend = FakeBackend()
    service = GlobalHotkeyService(backend=backend)
    service.register(HotkeyBinding("save_now", DEFAULT_ACCELERATOR), lambda: None)

    assert service.unregister("save_now") is True
    assert service.unregister("save_now") is False
    assert backend.unregistered == [DEFAULT_ACCELERATOR]
    assert service.states() == ()
    assert service.get("save_now") is None


def test_shutdown_stops_backend_once_and_is_idempotent() -> None:
    backend = FakeBackend()
    service = GlobalHotkeyService(backend=backend)
    service.register(HotkeyBinding("save_now", DEFAULT_ACCELERATOR), lambda: None)

    service.shutdown()
    service.shutdown()

    assert backend.stopped == 1
    assert service.states() == ()
    blocked = service.register(HotkeyBinding("late", "ctrl+q"), lambda: None)
    assert blocked.registered is False
    assert blocked.error is not None


def test_unavailable_backend_reports_reason() -> None:
    service = GlobalHotkeyService(backend=UnavailableBackend("无图形环境"))
    state = service.register(
        HotkeyBinding("save_now", DEFAULT_ACCELERATOR), lambda: None
    )

    assert state.registered is False
    assert state.error == "无图形环境"
    assert service.available is False


def test_default_backend_never_raises() -> None:
    """默认后端在无头/无权限环境下也必须返回可用的对象."""
    backend = default_backend()
    assert backend is not None
    backend.stop()


def test_hotkey_state_defaults() -> None:
    state = HotkeyState(HotkeyBinding("x", "ctrl+x"), True)
    assert state.error is None
    assert state.name == "x"
