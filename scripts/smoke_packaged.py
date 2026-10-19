r"""产物冒烟: 编出来的包必须真的能跑(进 CI, 由 release-build.yml 的 build 作业调用).

判据 = 退出码 + 侧效应, **不读 stdout**: 窗口化产物(console=False)没有控制台, "它真的
干活了"的证据只能取进程行为与落盘结果。每份产物(onedir / onefile)五条:

① 无参数启动: 打包版的默认命令是 ``gui``(``app.default_command``), 双击就该开窗口。
   系统目录整体重定向进临时沙箱(platformdirs 自带的 ``WIN_PD_OVERRIDE_*`` 覆盖,
   两个变量都指到同一处), 判据 = 沙箱日志里出现"界面启动"且进程仍活着(窗口开着),
   看完杀整棵树 —— 全程不碰真实的应用目录;
② ``--version``: 退出码 0;
③ ``doctor --root <临时>``: 退出码 0, 且该 root 下 config/data/logs 三处真的出现;
④ ``gui --root <临时> --smoke 3``: 退出码 0, 且日志里出现"界面启动";
⑤ 单实例闸门: 同一个临时 root 先起第一个 ``gui --smoke 30``, 等它的"界面启动"落地后
   再起第二个 —— 第二个必须以退出码 3 退出(不开新窗, 只把已开的那个请到前台), 且
   整份日志**只有一条**"界面启动"。

每条都限时(窗口起不来时 ``gui`` 会一直等, 用外部超时兜住); onefile 首次启动要解包到
``%TEMP%\_MEIxxxx``, 比较慢, 超时按最坏情况给足。用法::

    uv run python scripts/smoke_packaged.py     # dist/ 下自动找两份产物
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

#: "窗口真的起来了"在日志里的那句(``ui.main_window`` 启动时写)。
_GUI_STARTED_MARK = "界面启动"
#: 日志文件名(``--root`` 下落 ``logs/<它>``; 无参数启动落在沙箱里, 用 rglob 找)。
_LOG_NAME = "archive-management.log"
#: platformdirs 的系统目录覆盖(把 CSIDL_APPDATA / CSIDL_LOCAL_APPDATA 都指进沙箱)。
_SANDBOX_OVERRIDES = ("WIN_PD_OVERRIDE_APPDATA", "WIN_PD_OVERRIDE_LOCAL_APPDATA")
#: 第二个实例的退出码(``services.single_instance.ALREADY_RUNNING_EXIT``, 与 argparse
#: 的 2、GUI 起不来的 2 区分开)。
_ALREADY_RUNNING_EXIT = 3
#: 等"界面启动"落地的上限(秒): onedir 冷启动约 2s, onefile 首启还要解包, CI runner
#: 更慢 —— 按最坏情况给足, 把"还在启动"误判成"没起来"比多等一会儿贵。
_LOG_WAIT_SECONDS = 90.0
#: 一条自退命令(doctor / gui --smoke)的整体超时(秒)。
_CMD_TIMEOUT_SECONDS = 120.0
#: 单实例闸门里第二个进程的超时(秒): 闸门正常时它 ~2s 内就退; 闸门坏了它顶多撑到
#: 自己的 --smoke 兜底(30s)再退 0 —— 两种情况都在限时内出结论。
_SECOND_INSTANCE_TIMEOUT_SECONDS = 60.0
#: 确认"窗口开着"前的静置(秒): 日志落地后再等一眼, 排除"刚写完启动日志就退了"。
_ALIVE_SETTLE_SECONDS = 1.0

#: 一条判据的结论: (过没过, 给人看的一句话)。
Verdict = tuple[bool, str]


def _utf8_console() -> None:
    """把标准流切成 UTF-8(Windows runner 控制台是 cp1252, 打中文会打断步骤)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


def _kill_tree(pid: int) -> None:
    """杀整棵进程树(onefile 是引导进程拉子进程的形状, 单杀会留活口)."""
    taskkill = shutil.which("taskkill")
    if taskkill is None:  # pragma: no cover - 系统里没有 taskkill 才走
        return
    subprocess.run(  # noqa: S603 - taskkill 是 Windows 自带, 参数只有本进程的 pid
        [taskkill, "/T", "/F", "/PID", str(pid)],
        capture_output=True,
        check=False,
    )


def _read_log(path: Path) -> str:
    """读一份日志(读不到/被写手占着都当空串, 由调用方的判据去判红绿)."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _sandbox_env(sandbox: Path) -> dict[str, str]:
    """子进程环境: 两个系统目录覆盖都指进沙箱, 无参数启动写不进真实目录."""
    env = os.environ.copy()
    for name in _SANDBOX_OVERRIDES:
        env[name] = str(sandbox)
    return env


def _launch(
    exe: Path, args: Sequence[str], env: dict[str, str] | None = None
) -> subprocess.Popen[bytes]:
    """起一个产物进程(stdout/stderr 丢弃: 窗口化产物没有可读的控制台)."""
    return subprocess.Popen(  # noqa: S603 - exe 是本地刚构建的产物, 参数写死在本文件里
        [str(exe), *args],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _run_timed(
    exe: Path, args: Sequence[str], *, timeout: float = _CMD_TIMEOUT_SECONDS
) -> int:
    """限时跑一条自退命令, 返回退出码; 超时先杀树再抛 TimeoutError(判据层转失败)."""
    proc = _launch(exe, args)
    try:
        return proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_tree(proc.pid)
        raise TimeoutError(f"超过 {timeout:.0f}s 未退出(args={list(args)})") from None


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


def check_no_args(exe: Path, sandbox: Path) -> Verdict:
    """①无参数启动: 默认命令 = gui, 双击就该开窗口(判据 = 沙箱日志 + 进程仍活着)."""
    sandbox.mkdir(parents=True, exist_ok=True)
    proc = _launch(exe, [], env=_sandbox_env(sandbox))
    try:
        log = _wait_startup_in_sandbox(proc, sandbox)
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
        return True, f"沙箱日志出现'{_GUI_STARTED_MARK}'且进程仍活着: {log}"
    finally:
        _kill_tree(proc.pid)


def check_version(exe: Path) -> Verdict:
    """②--version: 退出码 0(不看 stdout —— 窗口化产物没有控制台)."""
    try:
        returncode = _run_timed(exe, ["--version"])
    except TimeoutError as exc:
        return False, str(exc)
    if returncode != 0:
        return False, f"--version 退出码 {returncode}, 应为 0"
    return True, "退出码 0"


def check_doctor(exe: Path, root: Path) -> Verdict:
    """③doctor --root <临时>: 退出码 0 且三处目录真的出现("能建目录"的侧效应)."""
    try:
        returncode = _run_timed(exe, ["doctor", "--root", str(root)])
    except TimeoutError as exc:
        return False, str(exc)
    if returncode != 0:
        return False, f"doctor 退出码 {returncode}, 应为 0"
    missing = [
        name for name in ("config", "data", "logs") if not (root / name).is_dir()
    ]
    if missing:
        return False, f"root 下缺目录: {missing}(doctor 该建的没建)"
    return True, "退出码 0, config/data/logs 三处都在"


def check_gui_smoke(exe: Path, root: Path) -> Verdict:
    """④gui --root <临时> --smoke 3: 退出码 0 且日志里出现"界面启动"."""
    try:
        returncode = _run_timed(exe, ["gui", "--root", str(root), "--smoke", "3"])
    except TimeoutError as exc:
        return False, str(exc)
    if returncode != 0:
        return False, f"gui --smoke 退出码 {returncode}, 应为 0"
    log = root / "logs" / _LOG_NAME
    if _GUI_STARTED_MARK not in _read_log(log):
        return False, f"{log} 里没有'{_GUI_STARTED_MARK}'(窗口没真起来)"
    return True, "退出码 0, 日志含'界面启动'"


def check_single_instance(exe: Path, root: Path) -> Verdict:
    """⑤单实例闸门: 第二个实例不开新窗、退 3, 日志里"界面启动"只有一条."""
    root.mkdir(parents=True, exist_ok=True)
    log = root / "logs" / _LOG_NAME
    first = _launch(exe, ["gui", "--root", str(root), "--smoke", "30"])
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
            )
        except TimeoutError as exc:
            return False, f"第二个实例没有按时退出: {exc}"
        if second_code != _ALREADY_RUNNING_EXIT:
            return False, (
                f"第二个实例退出码 {second_code}, 应为 {_ALREADY_RUNNING_EXIT}"
                "(闸门失效: 它开了自己的窗口)"
            )
        count = _read_log(log).count(_GUI_STARTED_MARK)
        if count != 1:
            return False, f"日志里'{_GUI_STARTED_MARK}'出现 {count} 次, 应只有 1 次"
        return True, f"第二个退 {_ALREADY_RUNNING_EXIT}, '界面启动'只有一条"
    finally:
        _kill_tree(first.pid)


def run_battery(label: str, exe: Path, workspace: Path) -> list[tuple[str, Verdict]]:
    """对一份产物跑完五条判据, 返回逐条结论(判据名 → (过没过, 说明))."""
    checks: list[tuple[str, Verdict]] = [
        ("无参数启动", check_no_args(exe, workspace / f"{label}-sandbox")),
        ("--version", check_version(exe)),
        ("doctor", check_doctor(exe, workspace / f"{label}-doctor")),
        ("gui --smoke", check_gui_smoke(exe, workspace / f"{label}-smoke")),
        (
            "单实例闸门",
            check_single_instance(exe, workspace / f"{label}-single"),
        ),
    ]
    return checks


def main(argv: Sequence[str] | None = None) -> int:
    """入口: 找产物 → 逐份跑五条判据 → 逐条打印 → 有红就退 1."""
    _utf8_console()
    parser = argparse.ArgumentParser(
        description="产物冒烟: 编出来的包必须真的能跑(判据 = 退出码 + 侧效应)"
    )
    parser.add_argument(
        "--onedir",
        type=Path,
        default=Path("dist/ArchiveManagement/ArchiveManagement.exe"),
        help="onedir 产物的 exe(默认按 pyinstaller 的输出位置找)",
    )
    parser.add_argument(
        "--onefile",
        type=Path,
        default=Path("dist/ArchiveManagement-onefile.exe"),
        help="onefile 产物(默认按 pyinstaller 的输出位置找)",
    )
    args = parser.parse_args(argv)
    if sys.platform != "win32":
        print("产物冒烟只在 Windows 上做(打包产物是 Windows 的)。")
        return 2
    artifacts: list[tuple[str, Path]] = [
        ("onedir", args.onedir),
        ("onefile", args.onefile),
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
    try:
        for label, exe in artifacts:
            print(f"\n=== {label}: {exe}")
            for name, (ok, detail) in run_battery(label, exe, workspace):
                verdict = "通过" if ok else "失败"
                print(f"{name}: {verdict}({detail})")
                failed += 0 if ok else 1
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    if failed:
        print(f"::error::产物冒烟 {failed} 条判据未过 —— 跑不起来的包不上传。")
        return 1
    print("\n产物冒烟: 10 条判据全过(2 份产物, 每份 5 条)。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
