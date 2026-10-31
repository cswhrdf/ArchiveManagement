"""快捷键服务的降级路径.

正常注册/注销由 ``test_hotkeys.py`` 覆盖; 这里只补"环境不给力"的几种情形:
注销一个已经不在表里的句柄、重建监听失败、依赖完全不可用时整体降级为
"不可用后端"。约定是**任何环境问题都收敛为带原因的 :class:`HotkeyState`
或降级后端, 不把异常抛给界面**。
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from archive_management.exceptions import HotkeyError
from archive_management.services import hotkeys as hotkeys_mod
from archive_management.services.hotkeys import (
    DEFAULT_SAVE_ACCELERATOR,
    GlobalHotkeyService,
    HotkeyBinding,
    UnavailableBackend,
    default_backend,
)

pytestmark = [
    pytest.mark.domain,
    pytest.mark.normal,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("全局快捷键"),
    pytest.mark.story("快捷键降级"),
    pytest.mark.layer("unit"),
]


class _FlakyBackend:
    """可编程替身: 支持在注销后重建监听时失败."""

    def __init__(self, *, fail_resume: bool = False) -> None:
        self.registered: dict[str, Callable[[], None]] = {}
        self.unregistered: list[object] = []
        self.fail_resume = fail_resume
        self.stopped = 0

    def register(self, accelerator: str, callback: Callable[[], None]) -> object:
        """登记并返回句柄."""
        self.registered[accelerator] = callback
        return accelerator

    def unregister(self, handle: object) -> None:
        """记录注销句柄."""
        self.unregistered.append(handle)
        self.registered.pop(str(handle), None)

    def stop(self) -> None:
        """记录停止."""
        self.stopped += 1

    def suspend(self) -> None:
        """空实现(本组用例不涉及)."""

    def resume(self) -> None:
        """按需模拟"重建监听失败"."""
        if self.fail_resume:
            raise HotkeyError("无法重建键盘监听")


def test_unregister_unknown_handle_is_ignored() -> None:
    """注销一个不在表里的句柄时安静返回, 不触碰后端."""
    backend = _FlakyBackend()
    service = GlobalHotkeyService(backend=backend)

    service.unregister("并不存在的句柄")

    assert backend.unregistered == []
    assert backend.stopped == 0


def test_unregister_survives_listener_restart_failure() -> None:
    """注销后重建监听失败只记日志: 界面上的绑定必须照常消失."""
    backend = _FlakyBackend()
    service = GlobalHotkeyService(backend=backend)
    service.register(HotkeyBinding("save_now", DEFAULT_SAVE_ACCELERATOR), lambda: None)
    backend.fail_resume = True

    assert service.unregister("save_now") is True

    assert backend.unregistered == [DEFAULT_SAVE_ACCELERATOR]
    assert service.get("save_now") is None


def test_default_backend_degrades_when_listener_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """依赖不可用(无权限/无图形环境)时降级为"不可用后端"并附带原因."""
    monkeypatch.setattr(
        hotkeys_mod,
        "PynputBackend",
        lambda: (_ for _ in ()).throw(HotkeyError("需要输入监控权限")),
    )

    backend = default_backend()

    assert isinstance(backend, UnavailableBackend)
    assert "输入监控权限" in backend.reason
