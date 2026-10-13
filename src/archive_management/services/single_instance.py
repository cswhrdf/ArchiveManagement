"""单实例闸门: 打包版同一时间只允许开一个界面进程.

**为什么需要它**(用户 2026-10-12 的要求): 两个进程各自开着同一个数据库、备份根与快捷键
注册, 就会出现"两边都以为自己说了算"的情况 —— 自动启停轮询、定时任务、备份缓存与
快捷键注册都会互相踩, 用户看到的是"点了没反应""状态突然回退"这类没有明确报错的怪事。
所以**编译版不允许开第二个** —— 但第二次启动不会开新窗、也不弹提示: 它把**已经开着的**
那个窗口叫到前台, 自己退出(用户 2026-10-12 的原话: "重复打开窗口应该直接跳转到上次打开的
窗口")。

**为什么只在打包版启用**(``app.is_packaged()``): 开发与用例需要同时开多个窗口 ——
分片跑集成用例时每片都在自己的进程里建真窗口, 源码运行(``python -m archive_management``)
也是开发者的机器。``ARCHIVE_MANAGEMENT_ALLOW_MULTI=1`` 是诊断用的逃生门(想同时开两个
打包版时用, 见 ``docs/development.md``)。

**实现是"文件 + 操作系统级锁"**(``msvcrt.locking`` / ``fcntl.flock``): 锁跟着进程走,
进程一旦消失(正常退出、崩溃、被任务管理器杀掉)操作系统就释放它, 因此**不会留下把应用
永久锁在门外的陈旧锁** —— 用"建文件即上锁"那种做法就得自己清理陈旧锁, 而清理本身又
要判断"那个 pid 还在不在", 多一层猜。锁文件落在**数据目录**里(``instance.lock``), 于是
``--root`` 便携实例各用各的锁, 互不干扰。

**第二个实例不开窗, 而是把已经开着的那个叫到前台**(用户 2026-10-12 的要求): 走的是数据
目录里的请求文件(``instance.focus``) —— 第二个进程写下 ``pid=… at=…``、在 Windows 上再用
``AllowSetForegroundWindow`` 把"允许提到前台"的权利交给对方(后台进程自己抢不到前台, 实测),
然后**等对方把文件清掉**(那就是回执)再退出; 已经开着的那个在它的 100ms 轮询里看到请求就
``deiconify + lift + focus_force``, **抬完才清文件** —— 清早了对方以为没人处理, 不清则每轮
轮询都抢一次前台。为什么用"文件 + 轮询"而不是窗口消息/跨进程句柄: 一是**不执行外部程序**
(仓库的硬规则), 二是这条路在三平台上完全一样, 不需要每个平台一套窗口系统 API。
"""

from __future__ import annotations

import contextlib
import ctypes
import importlib
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any, Protocol, cast

from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.packaging import package_version

#: 锁文件名(落在数据目录下; ``--root`` 便携实例各用各的).
LOCK_NAME = "instance.lock"
#: "请把窗口叫到前台"请求文件的文件名(与锁同一个目录).
FOCUS_NAME = "instance.focus"
#: 第二个实例等"请求被处理掉"的上限(秒): 它交出去的前台权限可能随它退出而失效, 所以要等
#: 已经开着的那个把窗口抬起来(文件被清掉就是回执)再退。
HANDLE_WAIT_SECONDS = 2.0
#: 轮询请求文件的间隔(秒).
_POLL_SECONDS = 0.02
#: 回退时每个平台留一句进程状态的写法(与 ``holder_pid`` 共用).
_PID_PATTERN = re.compile(r"pid=(\d+)")
#: 诊断用逃生门: 设成 1 时不拦第二个实例(打包版调试/演示时用).
ALLOW_ENV = "ARCHIVE_MANAGEMENT_ALLOW_MULTI"
#: 第二个实例的退出码(与 argparse 的 2、GUI 起不来的 2 区分开).
ALREADY_RUNNING_EXIT = 3
#: 环境变量里算"关掉闸门"的写法.
_TRUTHY = ("1", "true", "yes", "on")
#: 上锁的字节位置: 故意落在文件**末尾之外**.
#:
#: 为什么不是第 0 字节: Windows 的 ``msvcrt.locking`` 会连"读"一起挡住被锁区间, 锁 0 号
#: 字节时**第二个实例连"是被谁挡住的"都读不出来**(实测: ``holder()`` 返回空串), 那行
#: 字就白写了。锁到 4096 之后, 0 号开始的记录照旧可读, 而锁本身一样有效。文件短于这个
#: 位置时锁自动延伸到末尾之后(实测可行), 所以第一次运行的空文件也没问题。
_LOCK_OFFSET = 4096


class _WinLock(Protocol):
    """``msvcrt`` 里用到的那部分(只为标注; 运行时是真模块)."""

    LK_NBLCK: int
    LK_UNLCK: int

    def locking(self, file_descriptor: int, mode: int, length: int) -> None: ...


class _PosixLock(Protocol):
    """``fcntl`` 里用到的那部分."""

    LOCK_EX: int
    LOCK_NB: int
    LOCK_UN: int

    def flock(self, file_descriptor: int, operation: int) -> None: ...


class _User32(Protocol):
    """``user32`` 里用到的那部分(只为标注; 运行时是真 DLL)."""

    def AllowSetForegroundWindow(self, process_id: int) -> int: ...  # noqa: N802 - Win32 原名


#: ``ctypes`` 上那个加载器的属性名(非 Windows 没有它, 经 ``getattr`` 取).
_WIN_DLL_ATTR = "WinDLL"
#: ``user32`` 的库名.
_USER32 = "user32"


def lock_path(paths: ApplicationPaths) -> Path:
    """锁文件落点(数据目录里, 与 ``--root`` 一起走)."""
    return paths.data_dir / LOCK_NAME


def read_focus_request(path: Path) -> str:
    """读"请到前台"请求(**只读不删**); 没有请求/读不到/内容为空都返回空串.

    读与清分开是为了让两边能对暗号: 已经开着的那个先抬窗口、抬完再清, 而第二个实例等的正是
    "文件被清掉"这个回执(:func:`wait_until_handled`)。
    """
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def clear_focus_request(path: Path) -> None:
    """清掉请求文件(抬完窗口后的回执); 清不掉也不抛异常.

    **必须清**: 文件留着的话每一轮轮询都会把窗口抬一次, 用户手动切到别的软件后又被抢回来。
    """
    with contextlib.suppress(OSError):
        path.unlink(missing_ok=True)


def wait_until_handled(path: Path, *, timeout: float | None = None) -> bool:
    """等已经开着的那个把请求处理掉(文件没了就是回执); 超时返回 ``False``.

    第二个实例要等这一下才退: Windows 的前台权限可能随"交出去的那个进程退出"而失效, 于是
    它得活到对方真的把窗口抬起来。超时不当错误 —— 对方忙/不在响应时也照常退出。
    """
    deadline = time.monotonic() + (HANDLE_WAIT_SECONDS if timeout is None else timeout)
    while path.exists():
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_SECONDS)
    return True


def holder_pid(holder: str) -> int | None:
    """从锁文件那句"谁占着"里取出 pid(取不到返回 ``None``)."""
    match = _PID_PATTERN.search(holder)
    return int(match.group(1)) if match else None


def hand_over_foreground(pid: int | None) -> bool:
    """Windows: 把"允许把自己提到前台"的权利交给已经开着的那个进程.

    为什么需要这一步(2026-10-12 实测): Windows 有"前台锁定"—— 后台进程直接让 Tk
    ``deiconify``/``lift`` 只会把窗口恢复成可见, **前台焦点抢不过来**(实测: 第二个实例开完
    之后前台仍是编辑器)。而**用户刚双击起来的这个进程**手上有这个权利, 可以用
    ``AllowSetForegroundWindow`` 把它转交给对方 —— 对方随后的 ``deiconify + lift +
    focus_force`` 就会真的落到前台。

    其它平台没有这道闸(X11 由窗口管理器决定, macOS 由系统处理), 返回 ``False`` 表示"没做
    这一步"。取不到 pid、没权限、DLL 加载失败都只当没交出去: 这一步失败不该影响退出。
    """
    if pid is None:
        return False
    if sys.platform != "win32":
        return False  # platform: linux macos - 这两个平台不需要交前台权限
    return _allow_windows_foreground(pid)  # platform: windows - Win32 独有


def _load_user32() -> _User32:
    """加载 ``user32``(只有 Windows 会走到这里).

    ``ctypes.WinDLL`` 在非 Windows 的 typeshed 里压根不存在(按平台收的), 所以经 ``getattr``
    取 —— 与 ``services/platform_scan`` 读注册表同一套写法: 让另一平台的 mypy 也过。
    """
    dll: Any = getattr(ctypes, _WIN_DLL_ATTR, None)  # platform: windows - WinDLL 专有
    return cast(_User32, dll(_USER32))  # platform: windows - user32 专有


def _grant_foreground(user32: _User32, pid: int) -> bool:
    """调一次 ``AllowSetForegroundWindow``(返回值 0 表示没交出去)."""
    return bool(user32.AllowSetForegroundWindow(pid))  # platform: windows - Win32 专有


def _allow_windows_foreground(pid: int) -> bool:
    """真的去调 ``user32.AllowSetForegroundWindow``(只在 Windows 上被调到)."""
    try:
        return _grant_foreground(_load_user32(), pid)  # platform: windows - Win32 专有
    except OSError:  # platform: windows - 加载或调用失败只当没交出去
        return False  # platform: windows - 交不出去也要照常退出


def multi_instance_allowed() -> bool:
    """诊断用逃生门是否打开(``ARCHIVE_MANAGEMENT_ALLOW_MULTI=1``)."""
    return os.environ.get(ALLOW_ENV, "").strip().lower() in _TRUTHY


def focus_request_path(paths: ApplicationPaths) -> Path:
    """前台请求文件的落点: "请把窗口叫到前台", 与锁一样跟着 ``--root`` 走."""
    return paths.data_dir / FOCUS_NAME


def request_focus(paths: ApplicationPaths) -> str:
    """第二个实例: 请已经开着的那个把窗口叫到前台; 返回写进去的令牌.

    令牌写成一句人能读的话(``pid=… at=…``): 它在两边的日志里都出现, 事后对着看就知道
    "哪一次双击叫醒了窗口"。写不进去也不报错 —— 这一步失败只应表现为"窗口没跳到前台",
    不该把第二个实例的退出路径搞挂(调用方照样要退出, 退出码 3)。
    """
    path = focus_request_path(paths)
    token = f"pid={os.getpid()} at={datetime.now(UTC).isoformat(timespec='seconds')}"
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
    return token


class InstanceLock:
    """跨进程的文件锁: 声明"这个数据目录已经有一个界面进程".

    用法: ``with InstanceLock(lock_path(paths)) as locked: ...`` —— ``locked`` 为 ``False``
    表示别人已经占着(此时不要启动界面); 退出 ``with`` 就释放。
    """

    def __init__(self, path: Path) -> None:
        """记录锁文件落点(此时还没碰文件系统)."""
        self.path = path
        self._handle: IO[bytes] | None = None

    def acquire(self) -> bool:
        """试着拿锁; 已被别的进程占着时返回 ``False``(不抛异常)."""
        if self._handle is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        if not _lock(handle):
            handle.close()
            return False
        self._handle = handle
        _stamp(handle)
        return True

    def release(self) -> None:
        """释放锁(幂等); 进程消失时操作系统也会释放, 所以崩溃不留死锁."""
        handle, self._handle = self._handle, None
        if handle is None:
            return
        with contextlib.suppress(OSError):
            _unlock(handle)
        with contextlib.suppress(OSError):
            handle.close()

    def holder(self) -> str:
        """读锁文件里那句"谁占着"(读不到返回空串; 只用于提示与排查).

        锁落在 :data:`_LOCK_OFFSET`, 与这行字所在的位置不重叠, 所以**锁被别的进程占着时
        也读得到** —— 这正是第二个实例要用的那一面。
        """
        with contextlib.suppress(OSError):
            return self.path.read_text(encoding="utf-8").strip()
        return ""

    def __enter__(self) -> bool:
        """进入 ``with``: 拿锁并把结果交给调用方."""
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        """退出 ``with``: 释放锁."""
        self.release()


def _stamp(handle: IO[bytes]) -> None:
    """往锁文件里记一句"谁占着"(pid + 版本), 方便人工核对.

    位置固定在文件开头, 而锁在 :data:`_LOCK_OFFSET` —— 于是这句话在锁被占着的时候也读得到
    (:meth:`InstanceLock.holder` 依赖这一点)。
    """
    with contextlib.suppress(OSError):
        handle.seek(0)
        handle.truncate()
        handle.write(f"pid={os.getpid()} version={package_version()}\n".encode())
        handle.flush()


def _lock(handle: IO[bytes]) -> bool:
    """独占锁住 :data:`_LOCK_OFFSET` 处的那个字节; 已被别人占着返回 ``False``.

    两个平台都要**显式指引位置**(Windows 的 ``locking`` 从当前文件位置算起, 文件又是
    以 ``a+b`` 打开的 —— 不引就锁到上一次读写停留的地方去了)。锁的区间可以超出文件
    末尾, 所以第一次运行(空锁文件)也没问题(实测)。
    """
    try:
        handle.seek(_LOCK_OFFSET)
        if sys.platform == "win32":  # platform: windows - msvcrt 只在 Windows 上有
            module = cast("_WinLock", importlib.import_module("msvcrt"))
            module.locking(handle.fileno(), module.LK_NBLCK, 1)
        else:  # platform: linux macos - POSIX 用 flock
            module = cast("_PosixLock", importlib.import_module("fcntl"))
            module.flock(handle.fileno(), module.LOCK_EX | module.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle: IO[bytes]) -> None:
    """解开 :func:`_lock` 上的锁(句柄关掉也会释放, 这里是显式收尾)."""
    handle.seek(_LOCK_OFFSET)
    if sys.platform == "win32":  # platform: windows - msvcrt 只在 Windows 上有
        module = cast("_WinLock", importlib.import_module("msvcrt"))
        module.locking(handle.fileno(), module.LK_UNLCK, 1)
    else:  # platform: linux macos - POSIX 关掉句柄即释放, 这里显式解锁
        module = cast("_PosixLock", importlib.import_module("fcntl"))
        module.flock(handle.fileno(), module.LOCK_UN)
