"""全局快捷键服务(阶段 D 第 4 条).

快捷键用于在游戏内快速保存当前状态(PLAN 第 1 节). 系统级快捷键注册很容
易失败——被其它程序占用、缺少辅助功能权限、无图形环境(如 CI/无头运行)——
因此本模块的约定是**永不抛到调用方**: :meth:`GlobalHotkeyService.register`
把结果收敛成 :class:`HotkeyState`, 失败时给出可展示的原因, 应用继续以
"无快捷键"的方式工作(失败回退).

``pynput`` 在后台线程监听键盘, 因此回调只应做投递动作(例如把"保存"请求
放到线程安全队列), 不能直接操作 Tk 控件.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from archive_management.exceptions import HotkeyError

logger = logging.getLogger(__name__)

DEFAULT_ACCELERATOR = "<ctrl>+<alt>+s"


@dataclass(frozen=True)
class HotkeyBinding:
    """一个快捷键绑定配置."""

    name: str
    accelerator: str
    enabled: bool = True


@dataclass(frozen=True)
class HotkeyState:
    """一个快捷键的注册结果(含失败原因)."""

    binding: HotkeyBinding
    registered: bool
    error: str | None = None

    @property
    def accelerator(self) -> str:
        """返回加速键文本."""
        return self.binding.accelerator

    @property
    def name(self) -> str:
        """返回绑定的逻辑名称."""
        return self.binding.name


class HotkeyBackend(Protocol):
    """底层快捷键实现: 只负责注册/注销与监听线程生命周期."""

    def register(self, accelerator: str, callback: Callable[[], None]) -> object:
        """注册快捷键并返回句柄; 失败时抛出 :class:`HotkeyError`."""
        ...

    def unregister(self, handle: object) -> None:
        """注销快捷键; 句柄无效时静默返回."""
        ...

    def stop(self) -> None:
        """停止监听线程, 幂等."""
        ...


class PynputBackend:
    """基于 ``pynput`` 的全局快捷键后端."""

    def __init__(self) -> None:
        """创建后端; 缺少 ``pynput`` 时抛出 :class:`HotkeyError`."""
        try:
            from pynput import keyboard
        except Exception as exc:  # pragma: no cover - 依赖/无头环境的降级路径
            raise HotkeyError(f"全局快捷键不可用: {exc}") from exc
        self._keyboard = keyboard
        self._listener: object | None = None
        self._bindings: dict[str, Callable[[], None]] = {}

    def register(self, accelerator: str, callback: Callable[[], None]) -> object:
        """注册快捷键; 同键重复注册视为失败."""
        if accelerator in self._bindings:
            raise HotkeyError(f"快捷键已被占用: {accelerator}")
        self._bindings[accelerator] = callback
        try:
            self._restart()
        except HotkeyError:
            self._bindings.pop(accelerator, None)
            raise
        return accelerator

    def unregister(self, handle: object) -> None:
        """注销快捷键并重建监听器."""
        key = str(handle)
        if self._bindings.pop(key, None) is None:
            return
        try:
            self._restart()
        except HotkeyError as exc:
            logger.warning("重建快捷键监听失败: %s", exc)

    def stop(self) -> None:
        """停止监听线程, 幂等."""
        listener, self._listener = self._listener, None
        self._bindings.clear()
        if listener is None:
            return
        try:
            listener.stop()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - 停止期异常只记录
            logger.warning("停止快捷键监听失败: %s", exc)

    def _restart(self) -> None:
        """用当前绑定重建监听器(GlobalHotKeys 不支持运行期增删)."""
        self._stop_listener()
        if not self._bindings:
            return
        try:
            listener = self._keyboard.GlobalHotKeys(dict(self._bindings))
            listener.start()
        except Exception as exc:
            raise HotkeyError(f"快捷键注册失败: {exc}") from exc
        self._listener = listener

    def _stop_listener(self) -> None:
        """停止当前监听器但不清理绑定表."""
        listener, self._listener = self._listener, None
        if listener is None:
            return
        try:
            listener.stop()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - 停止期异常只记录
            logger.warning("停止快捷键监听失败: %s", exc)


class UnavailableBackend:
    """占位后端: 明确告知快捷键不可用, 用于无头/无权限环境的失败回退."""

    def __init__(self, reason: str) -> None:
        """记录不可用原因."""
        self.reason = reason

    def register(self, accelerator: str, callback: Callable[[], None]) -> object:
        """始终失败并给出原因."""
        del accelerator, callback
        raise HotkeyError(self.reason)

    def unregister(self, handle: object) -> None:
        """空实现."""
        del handle

    def stop(self) -> None:
        """空实现."""


def default_backend() -> HotkeyBackend:
    """返回默认快捷键后端; 不可用时降级为 :class:`UnavailableBackend`."""
    try:
        return PynputBackend()
    except HotkeyError as exc:
        logger.warning("全局快捷键不可用, 已降级: %s", exc)
        return UnavailableBackend(str(exc))


class GlobalHotkeyService:
    """快捷键注册表: 统一管理注册、注销与失败回退."""

    def __init__(self, *, backend: HotkeyBackend | None = None) -> None:
        """绑定快捷键后端(默认 :func:`default_backend`)."""
        self._backend: HotkeyBackend = backend or default_backend()
        self._states: dict[str, HotkeyState] = {}
        self._handles: dict[str, object] = {}
        self._closed = False

    def register(
        self, binding: HotkeyBinding, callback: Callable[[], None]
    ) -> HotkeyState:
        """注册一个快捷键, 返回注册结果(失败时不抛出异常)."""
        if self._closed:
            return HotkeyState(binding, False, "快捷键服务已释放")
        if not binding.enabled:
            state = HotkeyState(binding, False, None)
            self._states[binding.name] = state
            return state
        try:
            handle = self._backend.register(binding.accelerator, callback)
        except HotkeyError as exc:
            state = HotkeyState(binding, False, str(exc))
        except Exception as exc:
            state = HotkeyState(binding, False, f"快捷键注册失败: {exc}")
        else:
            self._handles[binding.name] = handle
            state = HotkeyState(binding, True, None)
        self._states[binding.name] = state
        return state

    def unregister(self, name: str) -> bool:
        """注销指定绑定; 原本不存在时返回 False."""
        state = self._states.pop(name, None)
        handle = self._handles.pop(name, None)
        if handle is not None:
            self._backend.unregister(handle)
        return state is not None

    def states(self) -> tuple[HotkeyState, ...]:
        """返回全部注册结果(按绑定名称排序)."""
        return tuple(self._states[name] for name in sorted(self._states))

    def get(self, name: str) -> HotkeyState | None:
        """返回某个绑定的注册结果."""
        return self._states.get(name)

    def shutdown(self) -> None:
        """停止监听并清空绑定; 幂等, 供应用退出时调用."""
        if self._closed:
            return
        self._closed = True
        self._handles.clear()
        self._states.clear()
        self._backend.stop()

    @property
    def available(self) -> bool:
        """返回是否存在可用的快捷键后端."""
        return not isinstance(self._backend, UnavailableBackend)
