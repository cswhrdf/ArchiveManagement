"""全局快捷键服务的单元测试.

覆盖三层内容: **组合键模型**(解析/规范化/校验)、**按键映射**(pynput 与 Tk 的
按键如何变成令牌, 含小键盘与主键盘数字的区分)、**失败回退**(注册失败、无权限、
无图形环境都收敛为带原因的 :class:`HotkeyState`, 不抛出到调用方)。
"""

from __future__ import annotations

from collections.abc import Callable
from types import SimpleNamespace

import pytest

from archive_management.exceptions import HotkeyError
from archive_management.services.hotkeys import (
    DEFAULT_BRANCH_ACCELERATOR,
    DEFAULT_SAVE_ACCELERATOR,
    GlobalHotkeyService,
    HotkeyBinding,
    HotkeyCombo,
    HotkeyState,
    PynputBackend,
    UnavailableBackend,
    combo_error,
    combo_from_pressed,
    default_backend,
    format_accelerator,
    listener_trusted,
    parse_accelerator,
    tk_token,
    to_pynput_accelerator,
)
from archive_management.services.platforms import PlatformFamily

# 实现里默认键已分成"保存/创建分支"两个; 这里给保存键一个短名字方便断言。
DEFAULT_ACCELERATOR = DEFAULT_SAVE_ACCELERATOR

pytestmark = [
    pytest.mark.domain,
    pytest.mark.normal,
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
        self.suspended = 0
        self.resumed = 0
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

    def suspend(self) -> None:
        """记录暂停次数."""
        self.suspended += 1

    def resume(self) -> None:
        """记录恢复次数."""
        self.resumed += 1


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


# ------------------------------------------------------------------ 组合键模型


def test_default_accelerators_are_platform_uniform() -> None:
    """两个默认快捷键都以 Win 键开头 + 两个按键, 三平台键位一致, 且单手可及."""
    assert DEFAULT_ACCELERATOR == "<win>+<alt>+s"
    assert DEFAULT_BRANCH_ACCELERATOR == "<win>+<alt>+z"
    for accelerator in (DEFAULT_ACCELERATOR, DEFAULT_BRANCH_ACCELERATOR):
        combo = parse_accelerator(accelerator)
        assert combo is not None
        assert combo.tokens[0] == "win"
        assert len(combo.tokens) == 3
        assert combo_error(combo) is None


def test_parse_accelerator_normalizes_order_and_case() -> None:
    """组合键与顺序、大小写无关: 统一按修饰键在前规范化."""
    combo = parse_accelerator("<ALT>+<Win>+S")

    assert combo == HotkeyCombo(modifiers=("win", "alt"), key="s")
    assert combo is not None
    assert combo.to_accelerator() == "<win>+<alt>+s"


@pytest.mark.parametrize(
    "text",
    [
        "<win>+<alt>+<f5>",
        "<win>+<alt>+<esc>",
        "<win>+<alt>+中",
        "<win>+<alt>+1",
        "<win>+<alt>+0",
        "<win>+<alt>+<num1>",
    ],
)
def test_parse_accelerator_rejects_keys_outside_the_whitelist(text: str) -> None:
    """数字键已被彻底移除: 它们会与 Windows 原生快捷键冲突."""
    assert parse_accelerator(text) is None
    assert combo_error(None) == "unknown_key"


def test_combo_error_requires_modifier_and_letter() -> None:
    """必须含 Win/Ctrl/Alt/Shift 之一, 且必须至少有一个字母键."""
    assert combo_error(parse_accelerator("s")) == "no_modifier"
    assert combo_error(parse_accelerator("<win>")) == "no_letter"
    assert combo_error(parse_accelerator("<win>+<ctrl>")) == "no_letter"
    assert combo_error(parse_accelerator("<win>+s")) is None
    assert combo_error(parse_accelerator("<ctrl>+<shift>+a")) is None
    # 两个修饰键 + 字母仍然合法, 只是更不容易按
    assert combo_error(parse_accelerator("<win>+<alt>+s")) is None
    # 完全没有按键(防御性分支): 由界面提示重新按下
    assert combo_error(HotkeyCombo()) == "too_few"


def test_combo_from_pressed_reports_reason() -> None:
    combo, reason = combo_from_pressed(["win", "alt", "s"])
    assert reason is None
    assert combo is not None
    assert combo.to_accelerator() == "<win>+<alt>+s"

    failed, reason = combo_from_pressed(["win"])
    assert failed is None
    assert reason == "no_letter"

    failed, reason = combo_from_pressed(["s"])
    assert failed is None
    assert reason == "no_modifier"


# -------------------------------------------------- 内部格式 → pynput 格式


@pytest.mark.parametrize(
    ("accelerator", "expected"),
    [
        ("<win>+<alt>+s", "<cmd>+<alt>+s"),
        ("<alt>+<win>+s", "<cmd>+<alt>+s"),
        ("<ctrl>+<shift>+a", "<ctrl>+<shift>+a"),
        ("<win>+z", "<cmd>+z"),
    ],
)
def test_to_pynput_accelerator_translates_win(accelerator: str, expected: str) -> None:
    """pynput 没有 Key.win: 注册时必须翻译成它的 Key.cmd."""
    assert to_pynput_accelerator(accelerator) == expected


def test_to_pynput_accelerator_keeps_unparsable_text() -> None:
    assert to_pynput_accelerator("not+a+combo+at+all") == "not+a+combo+at+all"


@pytest.mark.parametrize(
    ("accelerator", "platform", "expected"),
    [
        ("<win>+<alt>+s", "windows", "Win Alt S"),
        ("<win>+<alt>+s", "linux", "Win Alt S"),
        ("<win>+<alt>+s", "macos", "⌘⌥S"),
        ("<ctrl>+<shift>+a", "windows", "Ctrl Shift A"),
        ("<win>+<alt>+z", "macos", "⌘⌥Z"),
        ("", "windows", ""),
    ],
)
def test_format_accelerator_renders_readable_labels(
    accelerator: str, platform: PlatformFamily, expected: str
) -> None:
    assert format_accelerator(accelerator, platform) == expected


# ------------------------------------------------------------------ 按键映射


@pytest.mark.parametrize(
    ("keysym", "expected"),
    [
        ("Win_L", "win"),
        ("Super_L", "win"),
        ("Meta_L", "win"),
        ("Command", "win"),
        ("Control_L", "ctrl"),
        ("Alt_L", "alt"),
        ("Option_L", "alt"),
        ("Shift_L", "shift"),
        ("s", "s"),
        ("S", "s"),
        ("Z", "z"),
    ],
)
def test_tk_token_maps_supported_keys(keysym: str, expected: str) -> None:
    assert tk_token(keysym) == expected


@pytest.mark.parametrize(
    "keysym", ["F5", "Escape", "中", "1", "KP_1", "KP_9", "space", "Plus"]
)
def test_tk_token_rejects_unsupported_keys(keysym: str) -> None:
    """数字(含小键盘)、功能键与空格都不在白名单里."""
    assert tk_token(keysym) is None


def test_listener_trust_is_optimistic_without_the_flag() -> None:
    """只有 macOS 的监听器带 IS_TRUSTED; 其它平台缺失时不得当成未授权."""
    assert listener_trusted(SimpleNamespace(IS_TRUSTED=True)) is True
    assert listener_trusted(SimpleNamespace(IS_TRUSTED=False)) is False
    assert listener_trusted(object()) is True


class FakeListener:
    """模拟 pynput 监听器: 记录启动/停止与信任状态."""

    def __init__(self, *, trusted: bool = True) -> None:
        self.IS_TRUSTED = trusted
        self.started = False
        self.stopped = 0

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped += 1


class FakeKeyboard:
    """只提供 ``GlobalHotKeys`` 的键盘模块替身."""

    def __init__(self, listener: FakeListener) -> None:
        self.listener = listener
        # 每次创建监听器时记下 pynput 收到的加速键表(用于断言 <win> → <cmd>).
        self.created: list[dict[str, Callable[[], None]]] = []

    def GlobalHotKeys(  # noqa: N802 - 必须与 pynput 的工厂方法同名
        self, bindings: dict[str, Callable[[], None]]
    ) -> FakeListener:
        """返回预先准备好的监听器并记录本次注册的加速键."""
        self.created.append(bindings)
        return self.listener


def _fake_backend(*, trusted: bool = True) -> tuple[PynputBackend, FakeKeyboard]:
    """构造一个不起真实监听器的 :class:`PynputBackend`."""
    try:
        backend = PynputBackend()
    except HotkeyError as exc:  # pragma: no cover - 取决于运行环境
        pytest.skip(f"pynput 不可用: {exc}")
    fake = FakeKeyboard(FakeListener(trusted=trusted))
    # 只有替换键盘模块才能既走真实注册流程, 又不起真实的系统监听器.
    backend._keyboard = fake
    return backend, fake


def test_backend_registers_the_translated_accelerator() -> None:
    """注册时交给 pynput 的是 ``<cmd>`` 写法, 内部仍按 ``<win>`` 记账."""
    backend, fake = _fake_backend()

    handle = backend.register("<alt>+<win>+s", lambda: None)

    assert list(fake.created[-1]) == ["<cmd>+<alt>+s"]
    assert handle == "<win>+<alt>+s"
    assert fake.listener.started is True


def test_backend_treats_equivalent_spellings_as_the_same_binding() -> None:
    backend, _fake = _fake_backend()
    backend.register("<win>+<alt>+s", lambda: None)

    with pytest.raises(HotkeyError):
        backend.register("<alt>+<win>+S", lambda: None)


def test_untrusted_listener_is_reported_as_an_actionable_failure() -> None:
    """未授权时必须当作注册失败并停掉监听器, 否则界面会显示"已注册"却不触发."""
    backend, fake = _fake_backend(trusted=False)

    state = GlobalHotkeyService(backend=backend).register(
        HotkeyBinding("save_now", "<win>+<alt>+s"), lambda: None
    )

    assert fake.listener.started is True
    assert fake.listener.stopped == 1
    assert state.registered is False
    assert state.error is not None
    assert "辅助功能" in state.error


@pytest.mark.parametrize("accelerator", ["s", "a+b", "<win>", "<win>+<ctrl>"])
def test_backend_rejects_combinations_without_a_modifier_or_letter(
    accelerator: str,
) -> None:
    backend, _fake = _fake_backend()

    with pytest.raises(HotkeyError):
        backend.register(accelerator, lambda: None)


def test_suspend_keeps_bindings_and_defers_the_listener() -> None:
    """录制新快捷键期间不应监听: 暂停后改绑定不会重新起监听器."""
    backend, fake = _fake_backend()
    backend.register("<win>+<alt>+s", lambda: None)
    assert len(fake.created) == 1

    backend.suspend()
    assert fake.listener.stopped == 1
    backend.register("<win>+<alt>+z", lambda: None)
    assert len(fake.created) == 1

    backend.resume()
    assert len(fake.created) == 2
    assert set(fake.created[-1]) == {"<cmd>+<alt>+s", "<cmd>+<alt>+z"}
    backend.resume()  # 幂等
    assert len(fake.created) == 2


def test_service_suspend_and_resume_delegate_to_backend() -> None:
    backend = FakeBackend()
    service = GlobalHotkeyService(backend=backend)

    service.suspend()
    service.resume()

    assert (backend.suspended, backend.resumed) == (1, 1)


def test_accelerators_lists_registered_bindings_only() -> None:
    backend = FakeBackend()
    service = GlobalHotkeyService(backend=backend)
    service.register(HotkeyBinding("save_now", DEFAULT_ACCELERATOR), lambda: None)
    service.register(
        HotkeyBinding("branch", DEFAULT_BRANCH_ACCELERATOR, enabled=False), lambda: None
    )

    assert service.accelerators() == {"save_now": DEFAULT_ACCELERATOR}


def test_the_unavailable_backend_accepts_unregister_calls() -> None:
    """不可用后端的注销是空实现: 调用方不用先问它是不是真后端."""
    UnavailableBackend("无图形环境").unregister(object())


def test_backend_unregister_of_an_unknown_handle_keeps_the_listener() -> None:
    """注销没注册过的句柄: 直接返回, 不重建监听器(重建会停掉正在跑的监听线程)."""
    backend, fake = _fake_backend()
    backend.register("<win>+<alt>+s", lambda: None)
    created = len(fake.created)

    backend.unregister("never-registered")

    assert len(fake.created) == created
    assert fake.listener.stopped == 0


def _refuse(_bindings: dict[str, Callable[[], None]]) -> FakeListener:
    """模拟键盘模块拒绝创建监听器."""
    raise ValueError("pynput 拒绝")


def test_backend_unregister_survives_a_listener_that_cannot_be_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """注销时重建监听器失败只记警告: 绑定已经撤下, 不能把异常抛给调用方.

    留第二条绑定是有意的: 表空了 ``_restart`` 会提前返回, 那样根本走不到"重建失败"。
    """
    backend, fake = _fake_backend()
    backend.register("<win>+<alt>+s", lambda: None)
    backend.register("<win>+<alt>+z", lambda: None)
    monkeypatch.setattr(fake, "GlobalHotKeys", _refuse)

    backend.unregister("<win>+<alt>+s")  # 不抛


def test_format_accelerator_keeps_unparsable_text() -> None:
    """解析不出来的文本原样返回: 不猜也不丢信息(界面靠它显示用户自己填的内容)."""
    assert format_accelerator("not+a+combo+at+all") == "not+a+combo+at+all"


def test_backend_resume_survives_a_listener_that_cannot_be_rebuilt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """恢复监听失败也只记警告, 且暂停态已经解除(不会再重建一次)."""
    backend, fake = _fake_backend()
    backend.register("<win>+<alt>+s", lambda: None)
    backend.suspend()
    original = fake.GlobalHotKeys
    monkeypatch.setattr(fake, "GlobalHotKeys", _refuse)

    backend.resume()  # 不抛
    monkeypatch.setattr(fake, "GlobalHotKeys", original)
    created = len(fake.created)
    backend.resume()

    assert len(fake.created) == created, "已经不是在暂停态了, 不该重建"


def test_backend_reports_a_broken_keyboard_module_as_a_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """键盘模块自己抛错(不是 HotkeyError)也要收敛成"注册失败"并带上原因."""
    backend, fake = _fake_backend()
    monkeypatch.setattr(fake, "GlobalHotKeys", _refuse)

    with pytest.raises(HotkeyError, match="pynput 拒绝"):
        backend.register("<win>+<alt>+s", lambda: None)


def test_the_wrapped_callback_runs_and_swallows_its_own_errors() -> None:
    """交给 pynput 的是包装后的回调: 它要真的执行用户的回调, 且单个回调出错不外泄.

    (监听线程里漏出的异常会让后续按键全部失效, 所以这里量"不抛"本身就是判据。)
    """
    backend, fake = _fake_backend()
    calls: list[str] = []

    def explode() -> None:
        calls.append("called")
        raise RuntimeError("动作炸了")

    backend.register("<win>+<alt>+s", explode)
    wrapped = fake.created[-1]["<cmd>+<alt>+s"]

    wrapped()  # 不抛

    assert calls == ["called"]
