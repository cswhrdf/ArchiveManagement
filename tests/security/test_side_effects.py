"""副作用边界: 除"删除存档位置"外, 软件只能**读**自己目录之外的数据.

用户要求(2026-09-25): 软件会保存大量用户数据, 因此安全测试必须禁止它对自己目录之外
的任何区域做**写 / 执行 / 删除**(只读可以); 唯一的例外是"删除原始存档位置"这个由用户
显式触发、且需要输入游戏名确认的功能 —— 它也只允许删除。

做法: 把进程内的**变更入口全部换成记账版本**(内置 ``open`` 的写模式、``os`` 的创建/删除/
改名、``shutil`` 的复制删除、``subprocess``/``os.system`` 之类的执行入口), 语义不变、原函数
照常执行, 然后跑一遍**真实流水线**(建库 → 加游戏与存档位置 → 备份 → 恢复 → 删除位置),
用记录下来的路径断言上面那条规则。

三个阶段分开记账, 因此除了"写到哪"还能锁住两条更强的规则: **备份阶段对原始存档目录
只读**、**删除阶段对存档目录只做"移入回收站"这一件事**。
"""

from __future__ import annotations

import builtins
import importlib
import io
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import archive_management.application.locations as locations_mod
from archive_management.infrastructure.database import Database
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.sql_backend import SqlArchiveService

pytestmark = [
    pytest.mark.security,
    pytest.mark.critical,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("不可信输入防护"),
    pytest.mark.story("只动自己的目录与用户指定的存档位置"),
    pytest.mark.layer("security"),
    pytest.mark.timeout(120),
]

_CATEGORY = "side_effects"
# 打开文件的写模式字符: 命中任一个就是"会产生变更"。
_WRITING_MODES = frozenset("wax+")
# os 上"创建 / 删除 / 改名"这一族函数的记账种类。
_OS_MUTATIONS = {
    "mkdir": "create",
    "makedirs": "create",
    "remove": "delete",
    "unlink": "delete",
    "rmdir": "delete",
    "rename": "rename",
    "replace": "rename",
    "truncate": "write",
    "chmod": "write",
    "symlink": "create",
    "link": "create",
}
# shutil 上会产生变更的函数(源与目标都记账)。
_SHUTIL_MUTATIONS = {
    "rmtree": "delete",
    "move": "rename",
    "copytree": "create",
    "copy": "create",
    "copy2": "create",
    "copyfile": "create",
}
# "执行外部程序"的入口: 命中即失败(软件不需要执行任何外部命令)。
_EXECUTION_ENTRY_POINTS = (
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_call",
    "subprocess.check_output",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.execvp",
    "os.spawnv",
    "os.spawnve",
)


class ExecutedError(AssertionError):
    """软件试图执行外部程序(不允许): 记账后立即抛出, 避免真的执行."""


@dataclass(frozen=True)
class _Call:
    """一次被记录下来的变更入口."""

    phase: str
    kind: str
    path: str


class SideEffectRecorder:
    """把变更入口换成记账版本(不改语义), 并提供分阶段记账."""

    def __init__(self) -> None:
        """建立一张空账."""
        self.calls: list[_Call] = []
        self.executions: list[str] = []
        self._phase: str | None = None

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """给这一段代码打上阶段名(没进阶段时的变更不记账 —— 那是用例自己的准备工作)."""
        previous = self._phase
        self._phase = name
        try:
            yield
        finally:
            self._phase = previous

    def note(self, kind: str, path: object) -> None:
        """手工记一笔(用于外部后端, 例如被替换掉的"回收站")."""
        self._record(kind, path)

    def _record(self, kind: str, path: object) -> None:
        if self._phase is not None:
            self.calls.append(_Call(self._phase, kind, str(path)))

    def paths(self, *, phase: str | None = None, kind: str | None = None) -> list[str]:
        """按阶段/种类过滤出被记下的路径."""
        return [
            call.path
            for call in self.calls
            if (phase is None or call.phase == phase)
            and (kind is None or call.kind == kind)
        ]

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """替换内置 ``open``、``os``、``shutil`` 与执行入口."""
        self._install_open(monkeypatch)
        for name, kind in _OS_MUTATIONS.items():
            self._install_pair(monkeypatch, os, name, kind)
        for name, kind in _SHUTIL_MUTATIONS.items():
            self._install_pair(monkeypatch, shutil, name, kind)
        for dotted in _EXECUTION_ENTRY_POINTS:
            self._install_execution(monkeypatch, dotted)

    def _install_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """只拦"写模式"的 open(读模式属于允许的只读操作).

        ``Path.write_text`` / ``write_bytes`` 走的是 ``io.open`` 而不是 ``builtins.open``,
        两个入口都要装 —— 只装一个会让记账漏掉最常见的写入路径(漏掉的后果是
        "因为没有记录而通过", 正是这类用例最危险的失效方式)。
        """
        original = builtins.open

        def wrapper(file: Any, mode: str = "r", *args: Any, **kwargs: Any) -> Any:
            if any(char in mode for char in _WRITING_MODES):
                self._record("write", file)
            return original(file, mode, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", wrapper)
        monkeypatch.setattr(io, "open", wrapper)

    def _install_pair(
        self,
        monkeypatch: pytest.MonkeyPatch,
        module: Any,
        name: str,
        kind: str,
    ) -> None:
        """给模块级函数装上记账外壳(源与目标都记, 例如 rename 的两端)."""
        original = getattr(module, name)

        def wrapper(path: Any, *args: Any, **kwargs: Any) -> Any:
            self._record(kind, _resolve_dir_fd(kwargs.get("dir_fd"), str(path)))
            if args and isinstance(args[0], (str, os.PathLike)):
                # 第二个位置参数常是目标(rename/replace): 它用的是 dst_dir_fd。
                target = _resolve_dir_fd(kwargs.get("dst_dir_fd"), str(args[0]))
                self._record(kind, target)
            return original(path, *args, **kwargs)

        monkeypatch.setattr(module, name, wrapper)

    def _install_execution(self, monkeypatch: pytest.MonkeyPatch, dotted: str) -> None:
        """执行入口: 记下来然后抛错(不真的执行)."""
        module_name, function_name = dotted.rsplit(".", 1)
        module = {"os": os, "subprocess": subprocess}[module_name]
        original = getattr(module, function_name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            self.executions.append(dotted)
            self._record("execute", args[0] if args else dotted)
            raise ExecutedError(f"软件不应执行外部程序: {dotted}")

        wrapper.__doc__ = original.__doc__
        monkeypatch.setattr(module, function_name, wrapper)


# 平台能力: `os.unlink` 接受 `dir_fd`(POSIX 才有, Windows 的 os 没有这一支)。
#
# 记下来的是**导入期**那个函数对象: recorder 夹具会把 `os.unlink` 换成记账包装函数,
# 而 `os.supports_dir_fd` 里放的是原始的内置函数对象 —— 在夹具装好之后用
# `os.unlink in os.supports_dir_fd` 现查, 在**每个**平台上都是假。2026-09-26 的 CI 报告里
# 那条 `dir_fd` 自检在 Linux 与 Windows 上都写着 `Skipped: 本平台不支持 dir_fd` 就是这么
# 来的(守卫自己把自己关掉了, 所以谁也没发现)。平台能力与"此刻谁挂在这个名字上"必须
# 分开问。
_ORIGINAL_UNLINK = os.unlink


def _dir_fd_is_available() -> bool:
    """本平台是否支持 `dir_fd`(问的是 :data:`_ORIGINAL_UNLINK` 那个原始函数)."""
    return _ORIGINAL_UNLINK in os.supports_dir_fd


# “fd → 目录”的两条常见路径模板(Linux 用 /proc, macOS 用 /dev)。
# 注意 macOS 的 `/dev/fd/<fd>` **不是符号链接**, realpath 会原样返回 —— 所以它不够用,
# 还要问内核, 见 :func:`_fd_directory`。
_FD_PATH_TEMPLATES = ("/proc/self/fd/{fd}", "/dev/fd/{fd}")
# “指回某个 fd”的路径前缀: 它们**不是**真实位置。
#
# macOS 的 `/dev/fd/<fd>` 是 fdesc 节点(不是符号链接, `realpath` 原样返回), 而它 `isdir()`
# 又是真的 —— 记账器会以为自己拿到了绝对路径, 于是把一次**合法**删除记成
# `/dev/fd/13/slot.dat` 并判成越界(2026-09-30 与 2026-10-03 的 macOS CI 现场, 两条安全
# 用例都红在这里)。Linux 的 `/proc/self/fd/<fd>` 是符号链接, `realpath` 正常能解开;
# 解不开时它同样只是“又一次指回 fd”, 也不能当答案。
_FD_PATH_PREFIXES = ("/dev/fd/", "/proc/self/fd/")
# macOS 上“问出 fd 指向哪个目录”只能走 `fcntl.F_GETPATH`; Windows 没有 fcntl 这个模块
# (那一支也走不到 —— `dir_fd` 只有 POSIX 支持)。用 importlib 拿它: 写成 try/except import
# 反而要多一个 type: ignore, 而这里只需要“有就拿来用”。
_fcntl = importlib.import_module("fcntl") if sys.platform != "win32" else None
# F_GETPATH 要一个至少 MAXPATHLEN 的缓冲区(macOS 上是 1024 字节)。
_FD_PATH_BUFFER = 1024


def _looks_like_an_fd_path(path: str) -> bool:
    """这个候选是不是“又一次指回某个 fd”(见 :data:`_FD_PATH_PREFIXES`).

    这种路径在 macOS 上 `isdir()` 为真, 所以只查“是不是目录”挡不住它 —— 必须单独判形状。
    """
    return any(path.startswith(prefix) for prefix in _FD_PATH_PREFIXES)


def _fd_path_from_kernel(fd: int) -> str | None:
    """问内核这个 fd 指向哪个目录(``fcntl.F_GETPATH``, macOS 的正路); 问不到返回 None.

    缓冲区用**可变**的那种(``bytearray``): 内核是把路径写进我们这块内存里, 只读缓冲区
    拿不回结果(那种失败是静默的: 缓冲区留空, 于是记账退回裸文件名)。
    """
    module = _fcntl
    if module is None:
        return None
    command = getattr(module, "F_GETPATH", None)
    if command is None:  # pragma: no cover - 非 macOS 的 POSIX 上没有这个命令
        return None
    buffer = bytearray(_FD_PATH_BUFFER)
    try:
        module.fcntl(fd, command, buffer)
    except OSError:  # fd 已经关掉时内核会报 EBADF
        return None
    base = os.fsdecode(bytes(buffer).split(b"\0", 1)[0])
    return base or None


def _fd_path_from_cwd(fd: int) -> str | None:
    """切到那个 fd 再读回工作目录(``fchdir`` + ``getcwd``); 走不通返回 None.

    与 F_GETPATH 是两条**不同**的内核路径(macOS 上只有这两条能给出真实位置, 模板那条在
    那边只会得到 `/dev/fd/<fd>`), 所以两边都试。无论成败都把工作目录切回去 —— 记账器
    绝不该改变被测代码看到的工作目录(而且解析发生在原函数**之前**, 切不回来就会改变
    被测代码执行时的相对路径语义)。
    """
    fchdir = getattr(os, "fchdir", None)
    if fchdir is None:  # pragma: no cover - Windows 没有这一支
        return None
    try:
        origin = Path.cwd()
        fchdir(fd)
    except OSError:  # fd 不是目录 / 已经关掉 / 当前目录已删
        return None
    try:
        return str(Path.cwd())
    except OSError:  # pragma: no cover - 读不回来
        return None
    finally:
        with suppress(OSError):
            os.chdir(origin)


def _fd_directory(fd: int) -> str | None:
    """问出 ``fd`` 指向的那个目录; 问不到返回 None(调用方退回裸文件名, 于是响亮地判越界).

    三条路依次问, 且候选必须**既不是“指回 fd”的路径, 又真的是个目录**:

    * `fcntl.F_GETPATH`(macOS 的正路)把真实路径写进缓冲区;
    * 路径模板 —— Linux 走 `/proc/self/fd/<fd>`(它是指向目标的符号链接, `realpath` 就能
      解开; macOS 上这条只会得到被拒的 `/dev/fd/<fd>`);
    * `fchdir` + `getcwd` 兜底 —— 会切一轮工作目录, 所以排在最后(前面两条都没结果时才用)。

    为什么宁可返回 None 也不接受 `/dev/fd/<fd>`: 那个路径在 macOS 上 `isdir()` 为真,
    记下来会让一次**合法**删除被判成越界变更(2026-09-30 / 2026-10-03 的 macOS CI);
    而退回裸文件名时用例会**响亮地**报“越界”, 至少不会把错的当成对的。
    """
    for candidate in _fd_directory_candidates(fd):
        if not candidate or _looks_like_an_fd_path(candidate):
            continue
        if Path(candidate).is_dir():
            return candidate
    return None


def _fd_directory_candidates(fd: int) -> Iterator[str | None]:
    """上面三条路依次给出的候选(每条都可能给不出东西)."""
    yield _fd_path_from_kernel(fd)
    for template in _FD_PATH_TEMPLATES:
        marker = template.format(fd=fd)
        try:
            yield os.path.realpath(marker)
        except OSError:  # pragma: no cover - 读不到就试下一条
            yield None
    yield _fd_path_from_cwd(fd)


def _resolve_dir_fd(dir_fd: object, path: str) -> str:
    """把 `dir_fd` 相对的路径还原成绝对路径.

    `shutil.rmtree` 在支持 `dir_fd` 的平台上(POSIX)是**打开目录后按条目名**逐个删的
    (`os.unlink(条目名, dir_fd=目录 fd)`), 于是记账看到的是一个裸文件名 —— 那不是越界,
    只是相对基准没被一起记下来(Windows 不支持 `dir_fd`, 所以这一支只在 Linux/macOS 上
    出现: 2026-09-25 的 CI 就是在这里红的)。

    :func:`_fd_directory` 能问回那个目录的真实路径; 全都问不到时**原样返回**裸文件名
    —— 于是它会被判成越界并响亮地失败, 而不是被悄悄当成“合法”.
    """
    if dir_fd is None:
        return path
    try:
        fd = int(str(dir_fd))
    except ValueError:
        return path
    base = _fd_directory(fd)
    return path if base is None else str(Path(base) / path)


def _inside(root: Path, path: str) -> bool:
    """判断路径是否落在某个根目录里(按解析后的绝对路径比较)."""
    try:
        Path(path).resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


@pytest.fixture
def recorder(monkeypatch: pytest.MonkeyPatch) -> SideEffectRecorder:
    """装上记账版本的变更入口."""
    instance = SideEffectRecorder()
    instance.install(monkeypatch)
    return instance


def _service(app_root: Path) -> SqlArchiveService:
    """构造真实后端; 应用自己的数据(库与备份)全部收敛在 ``app_root`` 下."""
    database = Database(app_root / "data" / "archive-management.db")
    database.migrate()
    return SqlArchiveService(
        database,
        backup_root=app_root / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
    )


def test_the_recorder_notices_a_stray_write_outside_the_app_area(
    tmp_path: Path, recorder: SideEffectRecorder
) -> None:
    """自检: 记账器真的看得见越界写入(否则下面的用例会"因为没有记录而通过")."""
    outside = tmp_path / "outside" / "stray.txt"
    (tmp_path / "outside").mkdir()

    with recorder.phase("probe"):
        outside.write_text("x", encoding="utf-8")

    assert recorder.paths(kind="write") == [str(outside)]


def test_the_recorder_resolves_paths_relative_to_a_directory_fd(
    tmp_path: Path, recorder: SideEffectRecorder
) -> None:
    """自检: POSIX 上按 `dir_fd` 删条目时要还原成绝对路径, 不能记成越界.

    `shutil.rmtree` 在支持 `dir_fd` 的平台上就是"打开目录 + `os.unlink(条目名, dir_fd=fd)`",
    所以记账器必须把相对基准还原回来 —— 否则一次合法删除会被判成越界变更(2026-09-25 的
    Linux CI 就是这样红的: `越界变更: [_Call(phase='restore', kind='delete', path='slot.dat')]`)。

    平台能力经 :func:`_dir_fd_is_available` 问(导入期快照), **不能**在这里写
    `os.unlink in os.supports_dir_fd` —— 那个名字此刻已经被记账器换掉了, 现查必然为假。
    真的没有 `dir_fd` 这一支的平台(Windows)明确跳过, 原因里写清缺的是哪一项能力。

    路径按**解析后**的形式比较: 记账器还原出来的是 `realpath`(经 `/proc/self/fd` 或
    问内核), 而 macOS 的临时目录本身是符号链接(`/var` → `/private/var`), 拿 `tmp_path`
    的原始字符串比会在 macOS 上假报失败。
    """
    if not _dir_fd_is_available():
        pytest.skip(
            "本平台不支持 dir_fd: os.supports_dir_fd 里没有 os.unlink"
            "(Windows 的 os.unlink 不接受 dir_fd 参数, 这条自检没有可测的行为)"
        )
    folder = tmp_path / "root"
    folder.mkdir()
    victim = folder / "child.dat"
    victim.write_text("x", encoding="utf-8")

    with recorder.phase("probe"):
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.unlink("child.dat", dir_fd=fd)
        finally:
            os.close(fd)

    recorded = recorder.paths(phase="probe", kind="delete")
    expected = str(victim.resolve())

    assert recorded, "删了文件却没记账"
    assert expected in recorded, f"没还原成绝对路径: {recorded}"
    assert all(_inside(tmp_path, path) for path in recorded), f"记成了越界: {recorded}"


def test_the_directory_fd_falls_back_to_the_kernel_when_the_templates_do_not_resolve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """自检: 路径模板解不开时要问 `fcntl.F_GETPATH`(macOS 只有这条路).

    2026-09-30 的 macOS CI 正是在这里红的: 记下来的路径是 `/dev/fd/13/slot.dat` ——
    `realpath` 对 `/dev/fd/13` **原样返回**(它不是符号链接), 而那个路径在 macOS 上
    `isdir()` 是真的, 于是记账器以为自己拿到了绝对路径, 一次合法删除被判成越界变更。
    真机行为没法在 Windows 上复现, 所以这里把两件事换成替身: realpath 原样返回(模拟 macOS
    那种“解不开但不报错”的路径)与一个只认 `F_GETPATH` 的假 fcntl。

    2026-10-03 的 macOS CI 又在同一个地方红了(那次是 `/dev/fd/27/slot.dat`), 所以缓冲区
    改成可变的那种并用本用例把它的契约钉住: 内核往我们这块内存里写, 只读缓冲区拿不回来。
    """
    folder = tmp_path / "root"
    folder.mkdir()
    victim = folder / "child.dat"
    victim.write_text("x", encoding="utf-8")
    monkeypatch.setattr(os.path, "realpath", lambda value, *a, **kw: value)

    def fake_fcntl(fd: int, command: int, buffer: Any) -> int:
        assert command == fake.F_GETPATH, "不是 F_GETPATH 就问不出真实路径"
        assert isinstance(buffer, bytearray), "缓冲区必须是可变的, 否则写不回来"
        buffer[:] = os.fsencode(folder) + b"\0"
        return 0

    fake = SimpleNamespace(F_GETPATH=50, fcntl=fake_fcntl)
    monkeypatch.setattr(sys.modules[__name__], "_fcntl", fake)

    assert _fd_directory(3) == str(folder), "没有问内核要真实路径"
    assert _resolve_dir_fd(3, victim.name) == str(victim), "还是没接回绝对路径"


def test_installing_the_recorder_does_not_change_the_platform_capability(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """自检: 装上记账器不会改变"本平台是否支持 `dir_fd`"的答案.

    记账器把 `os.unlink` 换成记账包装函数, 而 `os.supports_dir_fd` 里放的是**原始的内置
    函数对象** —— 任何"用当前挂在 `os` 上的那个名字现查"的写法, 都会在装上记账器之后翻成
    假, 于是上面那条 `dir_fd` 自检在**每个**平台上都被跳过(2026-09-26 的 CI 报告: Linux
    与 Windows 都是 `Skipped: 本平台不支持 dir_fd`)。这条自检在装着记账器的状态下再问一次。
    """
    before = _dir_fd_is_available()
    SideEffectRecorder().install(monkeypatch)

    assert os.unlink is not _ORIGINAL_UNLINK, "自检前提: 记账器没有替换 os.unlink"
    assert _dir_fd_is_available() is before, "记账器改变了平台能力判定"


def test_an_fd_path_is_never_accepted_as_a_real_location() -> None:
    """“指回 fd”的路径不能当答案 —— macOS 的 `/dev/fd/<fd>` 就是这一种.

    它在 macOS 上 `isdir()` 为真(所以只查“是不是目录”挡不住), 而记下来会把一次合法删除
    判成越界(2026-09-30 / 2026-10-03 的 macOS CI 现场)。这条判形状。
    """
    assert _looks_like_an_fd_path("/dev/fd/13")
    assert _looks_like_an_fd_path("/proc/self/fd/13")
    assert not _looks_like_an_fd_path("/private/var/folders/36/root")
    assert not _looks_like_an_fd_path("/home/user/dev/fd/13"), "只是个名字像"


def test_a_candidate_that_only_points_back_to_the_fd_is_skipped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """模板给出“指回 fd”的路径时继续往下问, 全都问不到就返回 None(不准记成 fd 路径)."""
    folder = tmp_path / "root"
    (folder / "fd-3").mkdir(parents=True)
    # 只留模板这一条路: 另外两条会去问**当前进程**里那个编号的 fd(与用例无关)。
    monkeypatch.setattr(sys.modules[__name__], "_fcntl", None)
    monkeypatch.setattr(sys.modules[__name__], "_fd_path_from_cwd", lambda _fd: None)

    monkeypatch.setattr(
        sys.modules[__name__],
        "_FD_PATH_TEMPLATES",
        ("/dev/fd/{fd}", str(folder / "fd-{fd}")),
    )
    assert _fd_directory(3) == str(folder / "fd-3"), "应跳过 fd 路径, 用下一条模板"

    monkeypatch.setattr(sys.modules[__name__], "_FD_PATH_TEMPLATES", ("/dev/fd/{fd}",))
    assert _fd_directory(3) is None, "问不到真实位置时必须是 None(响亮地判越界)"


def test_resolve_dir_fd_joins_the_directory_when_it_can_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记账器还原 `dir_fd` 相对路径: 读得到那个目录就接回绝对路径, 读不到就原样返回.

    真的 fd 那一支由 Linux/macOS 上的自检用例覆盖(Windows 不支持 `dir_fd`); 这里用
    可控的“fd → 目录”映射把还原逻辑本身钉住 —— 读不到时**必须**原样返回, 否则一次
    认不出来的相对删除会被当成合法操作而静默放行。
    """
    folder = tmp_path / "root"
    folder.mkdir()
    fake = folder / "fd-3"
    fake.mkdir()
    # 只留模板这一条路(见上一条用例的同一句说明)。
    monkeypatch.setattr(sys.modules[__name__], "_fcntl", None)
    monkeypatch.setattr(sys.modules[__name__], "_fd_path_from_cwd", lambda _fd: None)
    monkeypatch.setattr(
        sys.modules[__name__], "_FD_PATH_TEMPLATES", (str(folder / "fd-{fd}"),)
    )

    assert _resolve_dir_fd(3, "child.dat") == str(fake / "child.dat")
    assert _resolve_dir_fd("3", "child.dat") == str(fake / "child.dat")
    assert _resolve_dir_fd(None, "child.dat") == "child.dat", "没有 dir_fd 就不动"
    assert _resolve_dir_fd(99, "child.dat") == "child.dat", "读不到就原样(会被判越界)"
    assert _resolve_dir_fd("不是数字", "child.dat") == "child.dat"


def test_the_pipeline_only_changes_the_app_area_and_the_save_location(
    tmp_path: Path, recorder: SideEffectRecorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """真实流水线的变更只落在两处: 应用自己的目录、用户指定的存档位置."""
    app_root = tmp_path / "app"
    save_root = tmp_path / "user-save"
    save = save_root / "slot"
    save.mkdir(parents=True)
    (save / "slot.dat").write_text("state-v1", encoding="utf-8")

    service = _service(app_root)
    game_id = service.add_game("边界游戏").game_id
    location = service.add_location(game_id, path=str(save), kind="directory")

    with recorder.phase("backup"):
        service.run_backup_now(game_id)
    backup_id = service.list_backups(game_id)[0].backup_id

    # 制造"尚未备份的新进度", 否则恢复会按"内容未变化"跳过。
    (save / "slot.dat").write_text("state-v2", encoding="utf-8")
    with recorder.phase("restore"):
        service.run_restore(game_id, backup_id, safety_point=False, force=True)

    # "回收站"用替身: 记账 + 真的把目录移进应用区域内(不污染开发者机器的回收站)。
    moved: list[str] = []
    trash_root = app_root / "trash"

    def fake_trash(path: str) -> None:
        recorder.note("trash", path)
        moved.append(path)
        target = trash_root / Path(path).name
        target.parent.mkdir(parents=True, exist_ok=True)
        Path(path).rename(target)

    monkeypatch.setattr(locations_mod, "send_to_trash", fake_trash)
    with recorder.phase("delete"):
        service.delete_save_location(location.location_id, confirm_name="边界游戏")

    # ① 一次都没有执行外部程序(软件不需要, 也绝不允许)。
    assert recorder.executions == [], "软件执行了外部程序"

    # ② 任何变更都必须在"应用自己的目录"或"用户指定的存档位置"里。
    stray = [
        call
        for call in recorder.calls
        if not _inside(app_root, call.path) and not _inside(save_root, call.path)
    ]
    assert stray == [], f"越界变更: {stray}"

    # ③ 备份阶段对原始存档目录**只读** —— 一次都不写、不删、不改名。
    assert recorder.paths(phase="backup") == [
        path for path in recorder.paths(phase="backup") if _inside(app_root, path)
    ], "备份过程动了原始存档目录之外/之内不该动的东西"

    # ④ 删除阶段对存档目录只做"移入回收站"这一件事(应用区域内的落地不算越界)。
    # 同一个路径会被记两笔: 回收站后端的那一笔, 以及真的改名时源路径的那一笔。
    assert moved == [str(save)], "删除功能没有把存档目录交给回收站后端"
    assert {
        path for path in recorder.paths(phase="delete") if _inside(save_root, path)
    } == {str(save)}, "删除阶段还动了存档目录里的别的东西"

    # ⑤ 恢复只写回原始存档位置, 不动应用自己的备份内容。
    assert all(
        _inside(save_root, path)
        for path in recorder.paths(phase="restore")
        if not _inside(app_root, path)
    ), "恢复写到了存档位置之外的地方"


def test_backup_never_writes_into_the_save_location(
    tmp_path: Path, recorder: SideEffectRecorder
) -> None:
    """备份是"读存档、写自己的快照": 存档目录里不该出现任何新东西."""
    app_root = tmp_path / "app"
    save = tmp_path / "user-save" / "slot"
    save.mkdir(parents=True)
    (save / "slot.dat").write_text("state-v1", encoding="utf-8")

    service = _service(app_root)
    game_id = service.add_game("只读游戏").game_id
    service.add_location(game_id, path=str(save), kind="directory")

    with recorder.phase("backup"):
        service.run_backup_now(game_id)

    touched = [
        call for call in recorder.calls if _inside(tmp_path / "user-save", call.path)
    ]
    assert touched == [], f"备份动了原始存档目录: {touched}"


def test_no_external_program_is_executed_in_the_source_tree() -> None:
    """静态兜底: 源码里不出现"执行外部程序"的入口.

    动态用例只覆盖它跑到的那条路径; 这条扫描保证**整棵树**里没有这类调用,
    将来有人加一个 `subprocess.run` 也会在这里被拦下。
    """
    source_root = Path(__file__).resolve().parents[2] / "src"
    banned = (
        "subprocess.",
        "os.system(",
        "os.popen(",
        "os.execv",
        "os.execp",
        "os.spawn",
        "shell=True",
    )
    offenders: list[str] = []
    for module in sorted(source_root.rglob("*.py")):
        text = module.read_text(encoding="utf-8")
        offenders.extend(
            f"{module.relative_to(source_root)}: {token}"
            for token in banned
            if token in text
        )

    assert offenders == [], f"源码里出现执行外部程序的入口: {offenders}"
