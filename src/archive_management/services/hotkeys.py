"""全局快捷键服务.

快捷键用于在游戏内快速操作: **全局保存**立即为当前游戏创建一次备份, **创建
分支**在选中节点上分出一条新分支(不弹窗, 使用默认分支名). 系统级快捷键注册很
容易失败——被其它程序占用、缺少辅助功能权限、无图形环境(如 CI/无头运行)——因此
本模块的约定是**永不抛到调用方**: :meth:`GlobalHotkeyService.register` 把结果
收敛成 :class:`HotkeyState`, 失败时给出可展示的原因, 应用继续以"无快捷键"的
方式工作(失败回退).

``pynput`` 在后台线程监听键盘, 因此回调只应做投递动作(例如把"保存"请求
放到线程安全队列), 不能直接操作 Tk 控件.

组合键格式(内部表示, 与 ``pynput`` 语法略有差异):

- 修饰键 ``<win>`` ``<ctrl>`` ``<alt>`` ``<shift>``: ``<win>`` 在 Windows/Linux
  上是 Win/Super 键, 在 macOS 上是 Command 键, 因此同一个组合在三个平台上位置
  一致(默认组合统一以 Win 键开头, 见 :data:`DEFAULT_ACCELERATORS`);
- 主键: 只能是字母 ``a``-``z``(忽略大小写)。

**为什么不允许数字键**: Windows 把 ``Win+数字``(以及 ``Win+Shift+数字``)
占为任务栏与窗口管理的原生快捷键, 含数字的组合会被系统先一步吃掉, 用户看到的
是"注册成功却永不触发"; 因此白名单里彻底去掉数字与小键盘数字。

注册时把 ``<win>`` 翻译成 ``pynput`` 认识的 ``<cmd>``(见
:func:`to_pynput_accelerator`), 组合匹配交回 ``pynput.keyboard.GlobalHotKeys``。

平台差异: macOS 需要把应用加入"辅助功能"白名单后才能真正收到按键, 未授权时
``pynput`` 会先起一个收不到事件的监听器, 因此 :class:`PynputBackend` 会在启动后
检查 :func:`listener_trusted`, 把这种情况改成明确的注册失败原因。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from archive_management.exceptions import HotkeyError
from archive_management.services.platforms import PlatformFamily, current_platform

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------ 按键白名单

# 修饰键: 规范化顺序即展示顺序, 组合里至少要有其中一个.
MODIFIER_TOKENS: tuple[str, ...] = ("win", "ctrl", "alt", "shift")
# 主键: 只允许字母(忽略大小写); 数字会与系统原生快捷键冲突, 因此不开放.
LETTER_TOKENS: tuple[str, ...] = tuple("abcdefghijklmnopqrstuvwxyz")

ALLOWED_TOKENS: frozenset[str] = frozenset(MODIFIER_TOKENS + LETTER_TOKENS)

# 默认快捷键: 三平台统一为"Win 键开头 + 两个按键", 且都落在左手单手可达的区域。
DEFAULT_SAVE_ACCELERATOR = "<win>+<alt>+s"
DEFAULT_BRANCH_ACCELERATOR = "<win>+<alt>+z"
# 动作名(与 HotkeyBinding.name 对应).
ACTION_SAVE_NOW = "save_now"
ACTION_CREATE_BRANCH = "create_branch"
# 动作名 → 默认组合键.
DEFAULT_ACCELERATORS: dict[str, str] = {
    ACTION_SAVE_NOW: DEFAULT_SAVE_ACCELERATOR,
    ACTION_CREATE_BRANCH: DEFAULT_BRANCH_ACCELERATOR,
}

# 组合键不合法的原因代码(界面按 ``hotkey.err_<code>`` 取文案).
REASON_EMPTY = "too_few"
REASON_NO_MODIFIER = "no_modifier"
REASON_NO_LETTER = "no_letter"
REASON_UNKNOWN = "unknown_key"

# 加速键令牌 → 界面可读标签; macOS 另外使用符号写法.
_TOKEN_LABELS: dict[str, str] = {
    "win": "Win",
    "ctrl": "Ctrl",
    "alt": "Alt",
    "shift": "Shift",
}
_MACOS_TOKEN_SYMBOLS: dict[str, str] = {
    "win": "⌘",
    "ctrl": "⌃",
    "alt": "⌥",
    "shift": "⇧",
}

# 令牌 → pynput 的修饰键名: pynput 没有 Key.win, 它的 Key.cmd 就是同一个键.
_PYNPUT_MODIFIER_NAMES: dict[str, str] = {
    "win": "cmd",
    "ctrl": "ctrl",
    "alt": "alt",
    "shift": "shift",
}

# macOS 未授权时的可操作提示(与系统设置里的措词保持一致).
_MACOS_TRUST_HINT = (
    "macOS 未授予输入监控权限: 请在“系统设置 → 隐私与安全性 → 辅助功能”中"
    "允许本应用, 然后重新注册快捷键"
)


# ------------------------------------------------------------------ 组合键模型


@dataclass(frozen=True)
class HotkeyCombo:
    """一个已规范化的组合键(修饰键在前, 主键在后)."""

    modifiers: tuple[str, ...] = ()
    key: str | None = None

    @property
    def tokens(self) -> tuple[str, ...]:
        """返回组合里的全部令牌(修饰键在前, 主键在后)."""
        return self.modifiers if self.key is None else (*self.modifiers, self.key)

    def to_accelerator(self) -> str:
        """还原为加速键文本(``<win>+<alt>+s``)."""
        return "+".join(token_text(token) for token in self.tokens)


def token_text(token: str) -> str:
    """返回令牌在加速键文本里的写法."""
    if token in MODIFIER_TOKENS:
        return f"<{token}>"
    return token


def to_pynput_accelerator(accelerator: str) -> str:
    """把内部加速键文本翻译成 ``pynput`` 的写法(``<win>`` → ``<cmd>``).

    ``pynput`` 的 ``Key`` 里没有 ``win``, 但它的 ``Key.cmd`` 在 Windows/Linux
    上就是 Win/Super 键、在 macOS 上是 Command 键, 与我们的 ``win`` 令牌同义。
    """
    combo = parse_accelerator(accelerator)
    if combo is None:
        return accelerator
    parts = [
        f"<{_PYNPUT_MODIFIER_NAMES.get(token, token)}>" for token in combo.modifiers
    ]
    if combo.key is not None:
        parts.append(combo.key)
    return "+".join(parts)


def combo_from_tokens(tokens: Iterable[str]) -> HotkeyCombo | None:
    """把令牌集合规范化为组合键; 含白名单外的令牌或超过一个主键时返回 None."""
    unique = set(tokens)
    if not unique <= ALLOWED_TOKENS:
        return None
    base = sorted(token for token in unique if token not in MODIFIER_TOKENS)
    if len(base) > 1:
        return None
    return HotkeyCombo(
        modifiers=tuple(token for token in MODIFIER_TOKENS if token in unique),
        key=base[0] if base else None,
    )


def parse_accelerator(text: str) -> HotkeyCombo | None:
    """解析加速键文本; 含未知令牌时返回 None."""
    tokens = [raw.strip().strip("<>").lower() for raw in text.split("+")]
    return combo_from_tokens([token for token in tokens if token])


def combo_error(combo: HotkeyCombo | None) -> str | None:
    """返回组合键不满足规则的原因代码; 合法时返回 None.

    规则: 必须包含 ``win``/``ctrl``/``alt``/``shift`` 之一, **并且**至少有一个
    字母键。两条同时满足就天然是"组合按"(至少两个按键), 因此不需要再单独
    判断按键个数; 数字与小键盘数字不在白名单里(会与系统原生快捷键冲突),
    解析时就会变成未知令牌。
    """
    if combo is None:
        return REASON_UNKNOWN
    if not combo.tokens:
        return REASON_EMPTY
    if not combo.modifiers:
        return REASON_NO_MODIFIER
    if combo.key is None:
        return REASON_NO_LETTER
    return None


def format_accelerator(
    accelerator: str | HotkeyCombo, platform: PlatformFamily | None = None
) -> str:
    """把组合键转成界面可读写法.

    ``<win>+<alt>+s`` → macOS 上 ``⌘⌥S``, 其它平台 ``Win Alt S``。无法解析的
    文本原样返回, 不猜测也不丢信息。
    """
    combo = (
        accelerator
        if isinstance(accelerator, HotkeyCombo)
        else parse_accelerator(accelerator)
    )
    if combo is None:
        # ``combo`` 为空只可能来自"传进来的是字符串"那一支(见上面的三元式), 因此下面的
        # 判断对按类型调用的调用方恒为真; 另一条只是"传了别的类型"时的兜底, 用例不构造它。
        if isinstance(accelerator, str):  # pragma: no branch - 只可能为真
            return accelerator
        return ""  # pragma: no cover - 类型之外的值才走这里
    family = platform or current_platform()
    if family == "macos":
        return "".join(
            _MACOS_TOKEN_SYMBOLS.get(token, _TOKEN_LABELS.get(token, token.upper()))
            for token in combo.tokens
        )
    return " ".join(_TOKEN_LABELS.get(token, token.upper()) for token in combo.tokens)


# ------------------------------------------------------------- 系统按键 → 令牌

# Tk 的 keysym → 令牌: 修饰键名在不同平台上并不统一, 这里把常见写法都收进来.
_TK_MODIFIER_KEYSYMS: dict[str, str] = {
    "win_l": "win",
    "win_r": "win",
    "super_l": "win",
    "super_r": "win",
    "meta_l": "win",
    "meta_r": "win",
    "command": "win",
    "command_l": "win",
    "command_r": "win",
    "control_l": "ctrl",
    "control_r": "ctrl",
    "ctrl_l": "ctrl",
    "ctrl_r": "ctrl",
    "alt_l": "alt",
    "alt_r": "alt",
    "option_l": "alt",
    "option_r": "alt",
    "shift_l": "shift",
    "shift_r": "shift",
}


def tk_token(keysym: str) -> str | None:
    """把 Tk 键盘事件的 ``keysym`` 映射为令牌(白名单之外返回 None).

    界面录制快捷键时只依赖 ``keysym``: 白名单里已经没有任何需要按"键码"区分的
    按键(数字与小键盘数字都已排除), 因此不再需要按平台查键码表。字母键忽略
    大小写, 其余按键(数字、功能键、小键盘等)一律返回 None, 由界面提示"不支持的
    按键"。
    """
    lowered = keysym.lower()
    modifier = _TK_MODIFIER_KEYSYMS.get(lowered)
    if modifier is not None:
        return modifier
    if len(keysym) == 1 and lowered in ALLOWED_TOKENS:
        return lowered
    return None


def combo_from_pressed(pressed: Sequence[str]) -> tuple[HotkeyCombo | None, str | None]:
    """把录制期间累计的令牌收敛成组合键, 失败时返回原因代码.

    返回 ``(组合, None)`` 表示录制成功; ``(None, 原因代码)`` 表示不满足规则。
    """
    combo = combo_from_tokens(pressed)
    reason = combo_error(combo)
    return (None, reason) if reason is not None else (combo, None)


def listener_trusted(listener: object) -> bool:
    """判断监听器是否已获得输入监控权限.

    只有 macOS 的 ``pynput`` 监听器会带 ``IS_TRUSTED`` 属性, 其它平台没有这个
    概念, 因此缺失时视为已授权(不引入无谓的失败分支)。``pynput`` 在 ``start()``
    返回前会先设置该属性, 因此在启动之后读取是可靠的。
    """
    return getattr(listener, "IS_TRUSTED", True) is not False


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

    def suspend(self) -> None:
        """暂停监听但保留绑定(界面录制新快捷键期间使用)."""
        ...

    def resume(self) -> None:
        """恢复监听, 幂等."""
        ...

    def stop(self) -> None:
        """停止监听线程并清空绑定, 幂等."""
        ...


class PynputBackend:
    """基于 ``pynput.keyboard.GlobalHotKeys`` 的全局快捷键后端.

    组合匹配交给 ``pynput`` (它内部维护按下集合, 全部松开后重新武装); 本类只负责
    组合校验、``<win>`` → ``<cmd>`` 的语法翻译、注册表重建与 macOS 权限检查。
    """

    def __init__(self) -> None:
        """创建后端; 缺少 ``pynput`` 时抛出 :class:`HotkeyError`."""
        try:
            from pynput import keyboard
        except Exception as exc:  # pragma: no cover - 依赖/无头环境的降级路径
            raise HotkeyError(f"全局快捷键不可用: {exc}") from exc
        self._keyboard = keyboard
        self._listener: object | None = None
        self._bindings: dict[str, Callable[[], None]] = {}
        self._suspended = False

    def register(self, accelerator: str, callback: Callable[[], None]) -> object:
        """注册快捷键; 组合不合法或同键重复注册视为失败.

        同义写法(``<win>+<alt>+s`` 与 ``<cmd>+<alt>+s``)在内部会被规范化成同一种
        绑定, 因此重复注册同样会被拦下。
        """
        combo = parse_accelerator(accelerator)
        if combo_error(combo) is not None:
            raise HotkeyError(
                f"快捷键必须包含 Win/Ctrl/Alt/Shift 之一且至少有一个字母: {accelerator}"
            )
        key = combo.to_accelerator() if combo is not None else accelerator
        if key in self._bindings:
            raise HotkeyError(f"快捷键已被占用: {key}")
        self._bindings[key] = callback
        try:
            self._restart()
        except HotkeyError:
            self._bindings.pop(key, None)
            raise
        return key

    def unregister(self, handle: object) -> None:
        """注销快捷键并重建监听器."""
        key = str(handle)
        if self._bindings.pop(key, None) is None:
            return
        try:
            self._restart()
        except HotkeyError as exc:
            logger.warning("重建快捷键监听失败: %s", exc)

    def suspend(self) -> None:
        """暂停监听(保留绑定): 录制新快捷键时不能让按键触发真实动作."""
        self._suspended = True
        self._stop_listener()

    def resume(self) -> None:
        """恢复监听, 幂等."""
        if not self._suspended:
            return
        self._suspended = False
        try:
            self._restart()
        except HotkeyError as exc:
            logger.warning("恢复快捷键监听失败: %s", exc)

    def stop(self) -> None:
        """停止监听线程并清空绑定, 幂等."""
        self._suspended = False
        self._bindings.clear()
        self._stop_listener()

    def _restart(self) -> None:
        """用当前绑定重建监听器(GlobalHotKeys 不支持运行期增删)."""
        self._stop_listener()
        if not self._bindings or self._suspended:
            return
        try:
            listener = self._keyboard.GlobalHotKeys(
                {
                    to_pynput_accelerator(accelerator): self._guard(
                        accelerator, callback
                    )
                    for accelerator, callback in self._bindings.items()
                }
            )
            listener.start()
        except Exception as exc:
            raise HotkeyError(f"快捷键注册失败: {exc}") from exc
        if not listener_trusted(listener):
            # macOS 未授权时监听器会启动但收不到按键: 停掉它并当作注册失败,
            # 否则界面会显示"已注册"却永远不触发。
            self._stop_listener_by(listener)
            raise HotkeyError(_MACOS_TRUST_HINT)
        self._listener = listener

    @staticmethod
    def _guard(accelerator: str, callback: Callable[[], None]) -> Callable[[], None]:
        """包装回调: 单个快捷键出错不能影响监听线程."""

        def run() -> None:
            try:
                callback()
            except Exception as exc:  # pragma: no cover - 回调异常只记录
                logger.warning("快捷键 %s 的回调失败: %s", accelerator, exc)

        return run

    @staticmethod
    def _stop_listener_by(listener: object) -> None:
        """停止一个尚未登记的监听器(权限检查失败时清理用)."""
        try:
            listener.stop()  # type: ignore[attr-defined]
        except Exception as exc:  # pragma: no cover - 停止期异常只记录
            logger.warning("停止快捷键监听失败: %s", exc)

    def _stop_listener(self) -> None:
        """停止当前监听器但不清理绑定表."""
        listener, self._listener = self._listener, None
        if listener is not None:
            self._stop_listener_by(listener)


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

    def suspend(self) -> None:
        """空实现."""

    def resume(self) -> None:
        """空实现."""

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
    """快捷键注册表: 统一管理注册、注销、录制暂停与失败回退."""

    def __init__(self, *, backend: HotkeyBackend | None = None) -> None:
        """绑定快捷键后端(默认 :func:`default_backend`)."""
        self._backend: HotkeyBackend = backend or default_backend()
        self._states: dict[str, HotkeyState] = {}
        self._handles: dict[str, object] = {}
        self._callbacks: dict[str, Callable[[], None]] = {}
        self._closed = False

    def register(
        self, binding: HotkeyBinding, callback: Callable[[], None]
    ) -> HotkeyState:
        """注册一个快捷键, 返回注册结果(失败时不抛出异常)."""
        if self._closed:
            return HotkeyState(binding, False, "快捷键服务已释放")
        self._callbacks[binding.name] = callback
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
        self._callbacks.pop(name, None)
        if handle is not None:
            self._backend.unregister(handle)
        return state is not None

    def suspend(self) -> None:
        """暂停监听(界面录制新快捷键期间使用), 幂等."""
        self._backend.suspend()

    def resume(self) -> None:
        """恢复监听, 幂等."""
        self._backend.resume()

    def states(self) -> tuple[HotkeyState, ...]:
        """返回全部注册结果(按绑定名称排序)."""
        return tuple(self._states[name] for name in sorted(self._states))

    def accelerators(self) -> Mapping[str, str]:
        """返回动作名 → 已生效的加速键文本(未注册成功的动作不在其中)."""
        return {
            name: state.accelerator
            for name, state in self._states.items()
            if state.registered
        }

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
        self._callbacks.clear()
        self._backend.stop()

    @property
    def available(self) -> bool:
        """返回是否存在可用的快捷键后端."""
        return not isinstance(self._backend, UnavailableBackend)
