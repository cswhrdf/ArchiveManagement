r"""产物冒烟: 编出来的包必须真的能跑(进 CI, 由 release-build.yml 的 build / build-linux 作业调用).

判据 = 退出码 + 侧效应, **不读 stdout**: 窗口化产物(console=False)没有控制台, "它真的
干活了"的证据只能取进程行为与落盘结果。发布产物是**一份单文件可执行**(onefile; Windows
上是 ``ArchiveManagement.exe``, Linux 上是无后缀的 ``ArchiveManagement``), 五条判据:

① 无参数启动: 打包版的默认命令是 ``gui``(``app.default_command``), 双击就该开窗口。
   系统目录整体重定向进临时沙箱(**按平台**: Windows 用 platformdirs 自带的
   ``WIN_PD_OVERRIDE_*`` 覆盖, Linux 盖住四个 ``XDG_*_HOME`` —— platformdirs 在 Linux 上
   全按 XDG 约定解析, 四个都指到同一处等于整体沙箱), 判据 = 沙箱日志里出现"界面启动"
   且进程仍活着(窗口开着),
   **顺手量冷启动耗时**(Popen → 日志落地)并卡上限(``_COLD_START_LIMITS_SECONDS``),
   看完杀整棵树 —— 全程不碰真实的应用目录;
② ``--version``: 退出码 0;
③ ``doctor --root <临时>``: 退出码 0, 且该 root 下 config/data/logs 三处真的出现;
④ ``gui --root <临时> --smoke 3``: 退出码 0, 且日志里出现"界面启动";
⑤ 单实例闸门: 同一个临时 root 先起第一个 ``gui --smoke 30``, 等它的"界面启动"落地后
   再起第二个 —— 第二个必须以退出码 3 退出(不开新窗, 只把已开的那个请到前台), 且
   整份日志**只有一条**"界面启动"。

每条都限时(窗口起不来时 ``gui`` 会一直等, 用外部超时兜住), 超时按 CI runner 的最坏
情况给足。**限时命令一律落现场**: 子进程的
stdout/stderr 写成文件, 超时或非零退出把这两份与 ``--root`` 下应用日志的末尾一并塞进
失败消息 —— 窗口化产物没有控制台, 不给现场就只能重跑一轮去猜(2026-10-26 那次就是这样)。
``--report`` 把逐条判据落成 JSON(失败也写), 由 ``scripts/create_allure_smoke.py`` 转成
Allure 结果项并入 CI 的汇总报告 —— 退出码管门禁, 报告管"哪一条没过"。用法::

    uv run python scripts/smoke_packaged.py     # Windows: dist/ArchiveManagement.exe
    uv run python scripts/smoke_packaged.py     # Linux:   dist/ArchiveManagement
    uv run python scripts/smoke_packaged.py --report smoke-report.json

Linux 上跑 GUI 两条判据需要图形显示: 有桌面环境的机器直接可用, 无显示的环境(服务器/
CI)用 ``xvfb-run -a`` 把本脚本整个包起来 —— release-build.yml 的 build-linux 作业就是
这么做的; 没有显示时脚本会先打一行 ``::warning::`` 再照跑(GUI 判据会红, 现场里有 Tk
的报错)。macOS 打包未纳入, 平台闸门只放行 Windows 与 Linux。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO

#: "窗口真的起来了"在日志里的那句(``ui.main_window`` 启动时写)。
_GUI_STARTED_MARK = "界面启动"
#: 日志文件名(``--root`` 下落 ``logs/<它>``; 无参数启动落在沙箱里, 用 rglob 找)。
_LOG_NAME = "archive-management.log"
#: 各平台"把系统目录整体指进沙箱"的环境变量。Windows 用 platformdirs 自带的
#: ``WIN_PD_OVERRIDE_*`` 覆盖(两个都指到同一处); Linux 没有 testing 覆盖, 改用 XDG
#: 约定本身 —— platformdirs 在 Linux 上把 config/data/cache/log(=state)四个目录全按
#: ``XDG_*_HOME`` 解析, 四个都盖住等于整体沙箱(值必须给**绝对路径**, platformdirs 见
#: 到相对路径会退回用户主目录 —— 沙箱路径来自 tempfile, 天然绝对)。
_SANDBOX_OVERRIDES: dict[str, tuple[str, ...]] = {
    "win32": ("WIN_PD_OVERRIDE_APPDATA", "WIN_PD_OVERRIDE_LOCAL_APPDATA"),
    "linux": ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"),
}
#: 各平台的产物文件名(同一份 spec, PyInstaller 在 Windows 上给 .exe、Linux 上不加后缀;
#: 键与 :data:`REPORT_PLATFORMS` 一致 —— 将来加平台两处一起动)。macOS 打包未纳入, 不在表里。
PRODUCT_FILENAMES: dict[str, str] = {
    "win32": "ArchiveManagement.exe",
    "linux": "ArchiveManagement",
}
#: POSIX 上把子进程起在**独立会话**里(setsid), 超时/收尾用 killpg 一次带走整组 ——
#: Windows 上 False(``start_new_session`` 是 POSIX 专属参数, Windows 忽略它, 显式 False
#: 免得读代码的人以为 Windows 也开了新会话)。
_NEW_SESSION = sys.platform != "win32"
#: 第二个实例的退出码(``services.single_instance.ALREADY_RUNNING_EXIT``, 与 argparse
#: 的 2、GUI 起不来的 2 区分开)。
_ALREADY_RUNNING_EXIT = 3
#: 等"界面启动"落地的上限(秒): 本机冷启动约 2s, CI runner 更慢 —— 按最坏情况给足,
#: 把"还在启动"误判成"没起来"比多等一会儿贵。
_LOG_WAIT_SECONDS = 90.0
#: 一条自退命令(doctor / gui --smoke)的整体超时(秒)。
_CMD_TIMEOUT_SECONDS = 120.0
#: 单实例闸门里第二个进程的超时(秒): 闸门正常时它 ~2s 内就退; 闸门坏了它顶多撑到
#: 自己的 --smoke 兜底(30s)再退 0 —— 两种情况都在限时内出结论。
_SECOND_INSTANCE_TIMEOUT_SECONDS = 60.0
#: 确认"窗口开着"前的静置(秒): 日志落地后再等一眼, 排除"刚写完启动日志就退了"。
_ALIVE_SETTLE_SECONDS = 1.0
#: 冷启动上限(秒): Popen 到"界面启动"落地。本机实测(2026-10-24, Windows x64 / 不压 UPX,
#: 同一条判据连跑 3 轮) **onefile 2.0 s**(对照 onedir 0.5 s) —— onefile 多出来的是把整份包
#: 解到 %TEMP%\_MEIxxxx 的开销; 2026-10-31 起发布产物回到 onefile(见 spec 末尾的说明)。
#: 上限按 CI runner 的最坏情况留 10 倍余量: 真超了就是依赖出问题了(又往包里塞大件), 不是
#: 抖动; 它不是性能基准 —— "启动→窗口→关窗"的总时长是另一件事(onefile 3.6 s, 见
#: docs/development.md)。注意探测是 0.5 s 一轮, 量到的数最多比真实值大一轮。
_COLD_START_LIMITS_SECONDS = {"onefile": 20.0}


def cold_start_problem(label: str, elapsed: float) -> str | None:
    """冷启动超限就返回一句原因(给判据拼消息), 没超返回 ``None``."""
    limit = _COLD_START_LIMITS_SECONDS[label]
    if elapsed <= limit:
        return None
    return f"冷启动 {elapsed:.1f}s 超过上限 {limit:.0f}s"


#: 一条判据的结论: (过没过, 给人看的一句话)。
Verdict = tuple[bool, str]

#: 冒烟结论落盘时的平台显示名(与报告环境的取名一致, 见 ``create_allure_smoke.py`` 与
#: allurerc.mjs 的 environments —— 写错会让结论落进隐式 default 环境, 报告自检判不完整)。
#: 按当前平台从表里取, 不做现场探测: 产物是在哪个平台上编的, 冒烟就跑在那个平台上。
#: macOS 打包未纳入: 表里没有它, ``REPORT_PLATFORM`` 在 darwin 上取到空串 —— 但平台闸门
#: (:func:`main`)在那之前就把 macOS 拦下了, 空串只会出现在"被加载进测试"的场合。
REPORT_PLATFORMS: dict[str, str] = {"win32": "Windows", "linux": "Linux"}
REPORT_PLATFORM = REPORT_PLATFORMS.get(sys.platform, "")

#: 每条判据的固定标识 + 显示名。标识进 Allure 结论项的 ``fullName``(由
#: ``scripts/create_allure_smoke.py`` 拼接), **改标识会断掉报告里的历史趋势** —— 显示名
#: 可以随便改, 标识不要动。顺序即判据执行顺序, 与 :func:`run_battery` 里的调用顺序一致。
CHECK_IDENTITIES: tuple[tuple[str, str], ...] = (
    ("no-args", "无参数启动"),
    ("version", "--version"),
    ("doctor", "doctor"),
    ("gui-smoke", "gui --smoke"),
    ("single-instance", "单实例闸门"),
)


@dataclass(frozen=True)
class SmokeCheck:
    """一条判据的结论: 标识(进报告身份)、显示名、过没过、给人看的说明."""

    key: str
    name: str
    passed: bool
    detail: str


def _utf8_console() -> None:
    """把标准流切成 UTF-8(Windows runner 控制台是 cp1252, 打中文会打断步骤)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _kill_tree(pid: int) -> None:
    """杀整棵进程树(单杀父进程会留子进程活口: 应用自己也可能再拉进程).

    Windows 用 ``taskkill /T /F``(系统自带, 按进程树杀); POSIX 用 ``killpg`` —— 子进程由
    :func:`_launch` / :func:`_run_timed` 起在**独立会话**里(``start_new_session``), 所以
    组 id 就是它自己的 pid, 一次 ``killpg`` 带走它和它拉出来的所有进程。进程组里有人
    先退了(比如 --smoke 自己到点)会让 getpgid/killpg 抛 ProcessLookupError —— 收尾路径
    上吞掉它, 进程没了本来就是目的达成。
    """
    if sys.platform == "win32":  # platform: windows - taskkill 按进程树杀
        taskkill = shutil.which("taskkill")
        if taskkill is None:  # pragma: no cover - 系统里没有 taskkill 才走
            return
        subprocess.run(  # noqa: S603 - taskkill 是 Windows 自带, 参数只有本进程的 pid
            [taskkill, "/T", "/F", "/PID", str(pid)],
            capture_output=True,
            check=False,
        )
        return
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(os.getpgid(pid), signal.SIGKILL)  # platform: linux - killpg 杀整组


def _read_log(path: Path) -> str:
    """读一份日志(读不到/被写手占着都当空串, 由调用方的判据去判红绿)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sandbox_env(sandbox: Path) -> dict[str, str]:
    """子进程环境: 当前平台的系统目录覆盖都指进沙箱, 无参数启动写不进真实目录."""
    env = os.environ.copy()
    for name in _SANDBOX_OVERRIDES.get(sys.platform, ()):
        env[name] = str(sandbox)
    return env


def _temp_env(base: dict[str, str] | None, temp_dir: Path) -> dict[str, str]:
    r"""把子进程的 TEMP/TMP/TMPDIR 都指进本次工作目录.

    子进程在本次判据里写的任何临时文件都随 ``shutil.rmtree(workspace)`` 一起清掉, 不去
    撑用户的临时目录。这条纪律是 onefile 时代落下的(2026-10-24, J-7②: onefile 解包到
    ``%TEMP%\_MEIxxxx``, 被杀留 ~75 MB 残渣; 2026-10-26 ③ doctor 超时被杀又留一个
    ``_MEI000004cc2``) —— onefile 已于 2026-10-28 停产, 但"冒烟起过的进程, 临时文件归
    冒烟自己收"仍然成立, 所以**每一条起产物的判据**都继续走这里。
    三套拼写一起盖: Windows 认 ``TEMP``/``TMP``, Linux 上 onefile 解到 ``$TMPDIR`` 下的
    ``_MEIxxxx`` —— 少盖一个平台就有一边的残渣堵不住。
    """
    env = dict(os.environ) if base is None else dict(base)
    temp_dir.mkdir(parents=True, exist_ok=True)
    for name in ("TEMP", "TMP", "TMPDIR"):
        env[name] = str(temp_dir)
    return env


def _launch(
    exe: Path, args: Sequence[str], env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """起一个产物进程(stdout/stderr 丢弃: 窗口化产物没有可读的控制台).

    POSIX 上带 ``start_new_session``(见 :data:`_NEW_SESSION`): 超时/收尾靠 killpg 杀整组。
    """
    return subprocess.Popen(  # noqa: S603 - exe 是本地刚构建的产物, 参数写死在本文件里
        [str(exe), *args],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=_NEW_SESSION,
    )


def _tail(path: Path, *, lines: int = 6) -> str:
    """取一份文本的末尾几行(读不到当空串), 只用于往失败消息里带现场."""
    kept = [line.strip() for line in _read_log(path).splitlines() if line.strip()]
    return " | ".join(kept[-lines:])


def _evidence_tail(evidence: Path, log: Path | None = None) -> str:
    """失败现场: 子进程 stdout/stderr 的末尾, 加上应用日志的末尾(取不到的项不占位)."""
    parts: list[str] = []
    for name, path in (
        ("stdout", evidence / "out.txt"),
        ("stderr", evidence / "err.txt"),
    ):
        text = _tail(path)
        if text:
            parts.append(f"{name}={text}")
    if log is not None:
        text = _tail(log)
        parts.append(f"日志={text}" if text else f"日志={log} 还是空的")
    return "; ".join(parts)


def _with_tail(message: str, evidence: Path, log: Path | None = None) -> str:
    """给一句失败消息补上现场(一点现场都没拿到就原样返回)."""
    tail = _evidence_tail(evidence, log)
    return f"{message}; {tail}" if tail else message


def _run_timed(
    exe: Path,
    args: Sequence[str],
    *,
    timeout: float = _CMD_TIMEOUT_SECONDS,
    env: dict[str, str] | None = None,
    evidence: Path | None = None,
    log: Path | None = None,
) -> int:
    """限时跑一条自退命令, 返回退出码; 超时先杀树再抛 TimeoutError(判据层转失败).

    ``evidence`` 给一个目录时, 子进程的 stdout/stderr 落成 ``out.txt`` / ``err.txt``;
    ``log`` 给应用日志路径时, 超时的消息里带上它的末尾。窗口化产物没有控制台, **卡住时
    这两处就是唯一的现场** —— 少了它们只能重跑一轮去猜它卡在哪。
    """
    with contextlib.ExitStack() as stack:
        captured: list[IO[bytes]] = []
        if evidence is not None:
            evidence.mkdir(parents=True, exist_ok=True)
            captured = [
                stack.enter_context((evidence / "out.txt").open("wb")),
                stack.enter_context((evidence / "err.txt").open("wb")),
            ]
        proc = subprocess.Popen(  # noqa: S603 - 见 _launch
            [str(exe), *args],
            env=env,
            stdout=captured[0] if captured else subprocess.DEVNULL,
            stderr=captured[1] if captured else subprocess.DEVNULL,
            start_new_session=_NEW_SESSION,
        )
        try:
            return proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc.pid)
            detail = f"超过 {timeout:.0f}s 未退出(args={list(args)})"
            if evidence is None:
                raise TimeoutError(detail) from None
            raise TimeoutError(_with_tail(detail, evidence, log)) from None


def _wait_startup_in_sandbox(
    proc: subprocess.Popen[bytes], sandbox: Path
) -> Path | None:
    """等沙箱里出现含"界面启动"的日志; 进程先退/到时都没等到返回 ``None``."""
    deadline = time.monotonic() + _LOG_WAIT_SECONDS
    while time.monotonic() < deadline:
        for candidate in sandbox.rglob(_LOG_NAME):
            if _GUI_STARTED_MARK in _read_log(candidate):
                return candidate
        if proc.poll() is not None:
            return None
        time.sleep(0.5)
    return None


def _wait_startup_in_log(proc: subprocess.Popen[bytes], log: Path) -> bool:
    """等指定日志文件出现"界面启动"; 进程先退/到时都没等到返回 ``False``."""
    deadline = time.monotonic() + _LOG_WAIT_SECONDS
    while time.monotonic() < deadline:
        if _GUI_STARTED_MARK in _read_log(log):
            return True
        if proc.poll() is not None:
            return False
        time.sleep(0.5)
    return False


def check_no_args(exe: Path, sandbox: Path, *, label: str) -> Verdict:
    """①无参数启动: 默认命令 = gui, 双击就该开窗口(判据 = 沙箱日志 + 进程还活着 + 冷启动上限)."""
    sandbox.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    proc = _launch(exe, [], env=_temp_env(_sandbox_env(sandbox), sandbox / "temp"))
    try:
        log = _wait_startup_in_sandbox(proc, sandbox)
        elapsed = time.monotonic() - started
        if log is None:
            if proc.poll() is not None:
                return False, f"进程提前退出(退出码 {proc.returncode}), 没等到启动日志"
            return (
                False,
                f"{_LOG_WAIT_SECONDS:.0f}s 内沙箱里没等到含'{_GUI_STARTED_MARK}'的日志",
            )
        time.sleep(_ALIVE_SETTLE_SECONDS)
        if proc.poll() is not None:
            return False, f"写完启动日志后进程没撑住(退出码 {proc.returncode})"
        slow = cold_start_problem(label, elapsed)
        if slow is not None:
            return False, f"{slow}(窗口确实起来了, 但慢到该查了)"
        limit = _COLD_START_LIMITS_SECONDS[label]
        return (
            True,
            f"沙箱日志出现'{_GUI_STARTED_MARK}'且进程仍活着, 冷启动 {elapsed:.1f}s"
            f"(上限 {limit:.0f}s): {log}",
        )
    finally:
        _kill_tree(proc.pid)


def check_version(exe: Path, work: Path) -> Verdict:
    """②--version: 退出码 0(不看 stdout —— 窗口化产物没有控制台)."""
    try:
        returncode = _run_timed(
            exe,
            ["--version"],
            env=_temp_env(None, work / "temp"),
            evidence=work / "evidence",
        )
    except TimeoutError as exc:
        return False, str(exc)
    if returncode != 0:
        return False, _with_tail(
            f"--version 退出码 {returncode}, 应为 0", work / "evidence"
        )
    return True, "退出码 0"


def check_doctor(exe: Path, work: Path) -> Verdict:
    """③doctor --root <临时>: 退出码 0 且三处目录真的出现("能建目录"的侧效应)."""
    root = work / "root"
    log = root / "logs" / _LOG_NAME
    evidence = work / "evidence"
    try:
        returncode = _run_timed(
            exe,
            ["doctor", "--root", str(root)],
            env=_temp_env(None, work / "temp"),
            evidence=evidence,
            log=log,
        )
    except TimeoutError as exc:
        return False, str(exc)
    if returncode != 0:
        return False, _with_tail(f"doctor 退出码 {returncode}, 应为 0", evidence, log)
    missing = [
        name for name in ("config", "data", "logs") if not (root / name).is_dir()
    ]
    if missing:
        return False, f"root 下缺目录: {missing}(doctor 该建的没建)"
    return True, "退出码 0, config/data/logs 三处都在"


def check_gui_smoke(exe: Path, work: Path) -> Verdict:
    """④gui --root <临时> --smoke 3: 退出码 0 且日志里出现"界面启动"."""
    root = work / "root"
    log = root / "logs" / _LOG_NAME
    evidence = work / "evidence"
    try:
        returncode = _run_timed(
            exe,
            ["gui", "--root", str(root), "--smoke", "3"],
            env=_temp_env(None, work / "temp"),
            evidence=evidence,
            log=log,
        )
    except TimeoutError as exc:
        return False, str(exc)
    if returncode != 0:
        return False, _with_tail(
            f"gui --smoke 退出码 {returncode}, 应为 0", evidence, log
        )
    if _GUI_STARTED_MARK not in _read_log(log):
        return False, f"{log} 里没有'{_GUI_STARTED_MARK}'(窗口没真起来)"
    return True, "退出码 0, 日志含'界面启动'"


def check_single_instance(exe: Path, root: Path) -> Verdict:
    """⑤单实例闸门: 第二个实例不开新窗、退 3, 日志里"界面启动"只有一条."""
    root.mkdir(parents=True, exist_ok=True)
    log = root / "logs" / _LOG_NAME
    env = _temp_env(None, root / "temp")
    evidence = root / "evidence"
    first = _launch(exe, ["gui", "--root", str(root), "--smoke", "30"], env=env)
    try:
        if not _wait_startup_in_log(first, log):
            return False, (
                f"第一个实例 {_LOG_WAIT_SECONDS:.0f}s 内没写出'{_GUI_STARTED_MARK}'"
            )
        try:
            second_code = _run_timed(
                exe,
                ["gui", "--root", str(root), "--smoke", "30"],
                timeout=_SECOND_INSTANCE_TIMEOUT_SECONDS,
                env=env,
                evidence=evidence,
                log=log,
            )
        except TimeoutError as exc:
            return False, f"第二个实例没有按时退出: {exc}"
        if second_code != _ALREADY_RUNNING_EXIT:
            return False, _with_tail(
                f"第二个实例退出码 {second_code}, 应为 {_ALREADY_RUNNING_EXIT}"
                "(闸门失效: 它开了自己的窗口)",
                evidence,
                log,
            )
        count = _read_log(log).count(_GUI_STARTED_MARK)
        if count != 1:
            return False, f"日志里'{_GUI_STARTED_MARK}'出现 {count} 次, 应只有 1 次"
        return True, f"第二个退 {_ALREADY_RUNNING_EXIT}, '界面启动'只有一条"
    finally:
        _kill_tree(first.pid)


def run_battery(label: str, exe: Path, workspace: Path) -> list[SmokeCheck]:
    """对一份产物跑完五条判据, 逐条给出结论(标识与显示名见 :data:`CHECK_IDENTITIES`)."""
    verdicts: tuple[Verdict, ...] = (
        check_no_args(exe, workspace / f"{label}-sandbox", label=label),
        check_version(exe, workspace / f"{label}-version"),
        check_doctor(exe, workspace / f"{label}-doctor"),
        check_gui_smoke(exe, workspace / f"{label}-smoke"),
        check_single_instance(exe, workspace / f"{label}-single"),
    )
    # strict=True: 判据条数与标识条数对不上(加了一条判据忘了配标识)当场炸, 免得身份错位。
    return [
        SmokeCheck(key, name, ok, detail)
        for (key, name), (ok, detail) in zip(CHECK_IDENTITIES, verdicts, strict=True)
    ]


def write_report(path: Path, artifacts: list[dict[str, object]]) -> None:
    """把逐条判据的结论落成 JSON(给 ``scripts/create_allure_smoke.py`` 转成 Allure 结果).

    **失败也要写**: "哪一条没过"要进汇总报告, 而不是一个只有退出码的红作业 —— 所以这一步
    在数完失败之后、返回退出码之前执行, 与成败无关。判据的标识(``key``)是报告身份的一部分,
    详见 :data:`CHECK_IDENTITIES` 的说明。
    """
    path.write_text(
        json.dumps(
            {"platform": REPORT_PLATFORM, "artifacts": artifacts},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _default_product() -> Path:
    """当前平台的产物默认路径(dist/ 下, spec 的 name= 两边一致、后缀由平台决定)."""
    return Path("dist") / PRODUCT_FILENAMES[sys.platform]


def _warn_about_missing_display() -> None:
    """Linux 上没有图形显示时先打一行提醒(GUI 两条判据必红, 提前把原因说清).

    照跑而不是提前退: 判据会红, 而失败现场里有 Tk 的 ``no display`` 报错 —— 现场比这里
    的一行字更有用; 但不提醒的话, "为什么 GUI 判据全红"得翻到现场末尾才知道。
    """
    if sys.platform != "linux":  # platform: windows macos - 只有 Linux 侧需要这条提醒
        return
    if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
        return
    print(
        "::warning::当前没有图形显示(DISPLAY 与 WAYLAND_DISPLAY 都为空) —— GUI 两条判据"
        "会起不来; 无显示的环境(服务器/CI)请用 `xvfb-run -a` 把本脚本整个包起来。"
    )


def main(argv: Sequence[str] | None = None) -> int:
    """入口: 找产物 → 逐份跑五条判据 → 逐条打印 → 有红就退 1."""
    _utf8_console()
    parser = argparse.ArgumentParser(
        description="产物冒烟: 编出来的包必须真的能跑(判据 = 退出码 + 侧效应)"
    )
    parser.add_argument(
        "--exe",
        type=Path,
        default=None,
        help="单文件产物的路径(默认按 pyinstaller 的输出位置与当前平台找: "
        "dist/ArchiveManagement.exe 或 dist/ArchiveManagement)",
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=None,
        help="把逐条判据的结论写成这份 JSON(给 create_allure_smoke.py 转成 Allure 结果)",
    )
    args = parser.parse_args(argv)
    if sys.platform not in PRODUCT_FILENAMES:
        # macOS 打包未纳入: 闸门挡在解析之后, 让"为什么不能跑"带上 --help 的路径。
        print("产物冒烟只在 Windows 与 Linux 上做(当前平台还没有打包产物)。")
        return 2
    _warn_about_missing_display()
    artifacts: list[tuple[str, Path]] = [
        ("onefile", args.exe or _default_product()),
    ]
    missing = [str(path) for _, path in artifacts if not path.is_file()]
    if missing:
        print(
            "::error::找不到产物: " + str(missing) + "(先 uv run pyinstaller "
            "--noconfirm --clean packaging/archive-management.spec)"
        )
        return 1
    workspace = Path(tempfile.mkdtemp(prefix="am-smoke-"))
    failed = 0
    report_artifacts: list[dict[str, object]] = []
    try:
        for label, exe in artifacts:
            print(f"\n=== {label}: {exe}")
            checks = run_battery(label, exe, workspace)
            for check in checks:
                verdict = "通过" if check.passed else "失败"
                print(f"{check.name}: {verdict}({check.detail})")
                failed += 0 if check.passed else 1
            report_artifacts.append(
                {
                    "label": label,
                    "exe": str(exe),
                    "checks": [
                        {
                            "key": check.key,
                            "name": check.name,
                            "passed": check.passed,
                            "detail": check.detail,
                        }
                        for check in checks
                    ],
                }
            )
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    if args.report is not None:
        write_report(args.report, report_artifacts)
        print(f"冒烟结论已写入 {args.report}")
    if failed:
        print(f"::error::产物冒烟 {failed} 条判据未过 —— 跑不起来的包不上传。")
        return 1
    print(
        f"\n产物冒烟: {len(report_artifacts) * len(CHECK_IDENTITIES)} 条判据全过"
        f"({len(report_artifacts)} 份产物 x {len(CHECK_IDENTITIES)} 条)。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
