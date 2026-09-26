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
import io
import os
import shutil
import subprocess
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
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


# “fd → 目录”的两条常见路径(Linux 用 /proc, macOS 用 /dev); 读到就接回绝对路径。
_FD_PATH_TEMPLATES = ("/proc/self/fd/{fd}", "/dev/fd/{fd}")


def _resolve_dir_fd(dir_fd: object, path: str) -> str:
    """把 `dir_fd` 相对的路径还原成绝对路径.

    `shutil.rmtree` 在支持 `dir_fd` 的平台上(POSIX)是**打开目录后按条目名**逐个删的
    (`os.unlink(条目名, dir_fd=目录 fd)`), 于是记账看到的是一个裸文件名 —— 那不是越界,
    只是相对基准没被一起记下来(Windows 不支持 `dir_fd`, 所以这一支只在 Linux/macOS 上
    出现: 2026-09-25 的 CI 就是在这里红的)。

    :data:`_FD_PATH_TEMPLATES` 能读回那个目录的真实路径; 全都读不到时**原样返回**
    裸文件名 —— 于是它会被判成越界并响亮地失败, 而不是被悄悄当成“合法”.
    """
    if dir_fd is None:
        return path
    try:
        fd = int(str(dir_fd))
    except ValueError:
        return path
    for template in _FD_PATH_TEMPLATES:
        try:
            base = os.path.realpath(template.format(fd=fd))
        except OSError:  # pragma: no cover - 读不到就退回裸文件名
            continue
        if base and Path(base).is_dir():
            return str(Path(base) / path)
    return path


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
    `/dev/fd`), 而 macOS 的临时目录本身是符号链接(`/var` → `/private/var`), 拿 `tmp_path`
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


def test_resolve_dir_fd_joins_the_directory_when_it_can_be_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """记账器还原 `dir_fd` 相对路径: 读得到那个目录就接回绝对路径, 读不到就原样返回.

    真的 fd 那一支由 Linux/macOS 上的自检用例覆盖(Windows 不支持 `dir_fd`); 这里用
    可控的“fd → 目录”映射把还原逻辑本身钉住 —— 读不到时**必须**原样返回, 否则一次
    认不出来的相对删除会被当成合法操作而静默放行。
    """
    import sys

    folder = tmp_path / "root"
    folder.mkdir()
    fake = folder / "fd-3"
    fake.mkdir()
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
