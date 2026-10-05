r"""把"死于原生崩溃"的测试进程原地重跑一次 —— 只认崩溃, 绝不碰普通失败.

背景(2026-10-05, run 37270928260): macOS 26.6.2 arm64 + Tcl/Tk 9.0 上, UI 分片死在 Tk
Aqua 位图绘制的 use-after-free(``EXC_BAD_ACCESS @0x70``, 崩在 ``-[NSCGSContext
dealloc]`` 一族, 排查记录见 ``docs/testing.md``)。这是原生层的缺陷, 同一用例在
Linux/Windows 都过; 但 pytest 进程一死, 崩溃点之后的用例全部没有结果, 分片报缺。

定位更新(同日第二轮实证): 那次崩溃是**确定性**的 —— 部署重试后重跑的那一轮又崩在
同一处, ``faulthandler.log`` 里出现两条 ``Fatal Python error``、体积翻倍到 1.1 GiB。
所以本包装**救不了它**; 根治靠依赖层规避(ci.yml 的 macOS 片改用 setup-python 装的
python.org 构建, 捆 Tcl/Tk 8.6)。这层包装保留下来兜**偶发**崩溃: 退出码闸门保证普通
失败永远不会被重试, 留着它不亏。

为什么敢说它**不影响测试准确率**:

* 只有"进程被信号/原生异常杀死"这一类退出码才触发重试(见 :data:`CRASH_EXIT_CODES`)。
  断言失败、用例红、门禁不过都是普通退出码, **一次都不会重跑** —— 真回归不会被重试
  稀释或掩盖;
* 重试前把崩溃那次已落盘的**残缺产物**清掉, 而且只清这一次**新增**的(``--watch-dir``):
  Allure 的结果文件是逐用例写的, 崩溃进程留下的半套结果若不清, 会与重跑结果混进同一份
  报告(同一用例出现两份结论)。前一段进程(not ui)写的结果在快照之外, 原样保留;
  覆盖率文件不用清 —— pytest-cov 只在会话**结束**才落盘, 崩溃的那次什么都没写进去
  (前一段的数据完好, 见 ci.yml 两段式拆分的注释);
* 只重跑一次: 第二次仍崩就按它的退出码失败, 证据(``faulthandler.log``、``.ips``)照常
  落在 ``crash-dumps/`` 并被 ``if: always()`` 的上传步收走。

**这是临时缓解, 不许烂尾**(用户 2026-10-05 的要求): 重试有到期日
(:data:`RETRY_UNTIL`)。到期之后:

* 这个脚本直接透传、不再重试, 并在日志里打提醒;
* ``tests/unit/test_ci_diagnostics.py`` 里那条"窗口没被忘记"的守卫**当场变红** ——
  要么移除这层包装(ci.yml 的两个 UI 步骤与 docs/testing.md 的说明), 要么在确认上游
  仍没修好后**明确续期**(改 :data:`RETRY_UNTIL` 并在续期理由里写清依据)。

用法(CI, 见 ``.github/workflows/ci.yml`` 的 UI 段)::

    uv run python scripts/run_pytest_with_crash_retry.py \\
        --watch-dir allure-results -- uv run pytest ...

退出码: 第一次(或唯一一次)运行的退出码原样透传; 重试后以第二次为准。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import shutil
import subprocess
import sys
from pathlib import Path

# 重试窗口的到期日(含当天, UTC)。放一个月: 到时候上游(python.org 的 Tcl/Tk 9.0 构建 /
# Tk 源码)要么出了补丁, 要么确认无解要换方案; 无论哪种, 都该回来重新审视这层包装 ——
# 守卫测试会在到期次日变红, 忘不掉。
RETRY_UNTIL = dt.date(2026, 11, 5)

# "进程被信号/原生异常杀死"的退出码:
# * 负值: ``subprocess`` 直接报告的信号死亡(SIGSEGV=-11 这一类);
# * ``128 + 信号``: shell / ``uv run`` 模拟的信号死亡 —— macOS 分片那族 Tk 崩溃正是
#   SIGTRAP=133 / SIGSEGV=139;
# * 两个大值: Windows 的 NTSTATUS(读违例 0xC0000005 与 fail-fast 0xC0000409), Python
#   进程原生崩坏时 ``subprocess`` 拿到的就是它们。
_SIGNALS_BY_CODE: dict[int, str] = {
    -11: "SIGSEGV",
    -7: "SIGBUS",
    -6: "SIGABRT",
    -5: "SIGTRAP",
    -4: "SIGILL",
}
CRASH_EXIT_CODES = (
    frozenset(_SIGNALS_BY_CODE)
    | {128 + abs(code) for code in _SIGNALS_BY_CODE}
    | {0xC0000005, 0xC0000409}
)


def is_crash_exit(code: int | None) -> bool:
    """这个退出码是不是"进程被原生信号/异常杀死"(而不是正常的用例失败)."""
    return code is not None and code in CRASH_EXIT_CODES


def describe_exit(code: int) -> str:
    """把崩溃退出码写成 ``SIGSEGV(139)`` 这样的短标签(重试横幅与运行摘要用)."""
    name = _SIGNALS_BY_CODE.get(code)
    if name is None and code >= 128:
        name = _SIGNALS_BY_CODE.get(128 - code)
    if name is not None:
        return f"{name}({code})"
    return f"0x{code:X}"


def retry_window_open(today: dt.date) -> bool:
    """重试窗口是否仍开着(到期日当天含在窗口里; 过期由守卫测试负责催办)."""
    return today <= RETRY_UNTIL


def _snapshot(directories: list[Path]) -> dict[Path, set[str]]:
    """记下每个被看护目录**此刻**已有的条目名: 重试前只清这之后新出现的."""
    return {
        directory: {entry.name for entry in directory.iterdir()}
        if directory.is_dir()
        else set()
        for directory in directories
    }


def _remove_new_entries(before: dict[Path, set[str]]) -> list[Path]:
    """删掉快照之后新出现的条目(文件或目录), 返回删了什么."""
    removed: list[Path] = []
    for directory, names in before.items():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.name in names:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    entry.unlink()
            removed.append(entry)
    return removed


def _announce(
    code: int, removed: list[Path], rerun: subprocess.CompletedProcess[bytes]
) -> None:
    """重试收场后要留下看得见的痕迹: 运行日志里的 warning 注解 + 运行摘要页的一段话."""
    cleaned = ", ".join(str(path) for path in removed) or "无"
    print(
        f"::warning::测试进程死于原生崩溃 {describe_exit(code)}, "
        f"已清理其残缺产物({cleaned})并以重跑退出码 {rerun.returncode} 收场 —— 详见运行摘要",
        file=sys.stderr,
    )
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    lines = [
        "### 原生崩溃重试(临时缓解)",
        "",
        f"- 第一次尝试死于 {describe_exit(code)}: macOS 26 + Tcl/Tk 9.0 的原生缺陷"
        "(排查记录见 docs/testing.md), 不是用例失败;",
        f"- 已清理该次落盘的残缺产物: {cleaned};",
        f"- 重跑退出码 {rerun.returncode}。",
        "",
    ]
    try:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("\n".join(lines))
    except OSError:  # pragma: no cover - 摘要文件不可写不影响重试本身
        pass


def parse_args(argv: list[str]) -> argparse.Namespace:
    """``--watch-dir`` 可重复; ``--`` 之后是要包起来的整条命令."""
    parser = argparse.ArgumentParser(
        description="只在测试进程死于原生信号/异常时重跑一次(普通失败原样透传)"
    )
    parser.add_argument(
        "--watch-dir",
        action="append",
        default=[],
        type=Path,
        help="重试前要清掉「本次新增内容」的目录(可重复, 如 allure-results)",
    )
    parser.add_argument(
        "command",
        nargs="+",
        help="要运行的命令(建议用 -- 与本脚本的参数隔开)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """跑一次; 死于原生崩溃(且窗口未过期)就清掉残缺产物再原样重跑一次."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    today = dt.datetime.now(dt.UTC).date()
    window_open = retry_window_open(today)

    before = _snapshot(args.watch_dir)
    first = subprocess.run(args.command, check=False)  # noqa: S603 - 命令来自 ci.yml 的包装参数

    if not is_crash_exit(first.returncode):
        return first.returncode if first.returncode is not None else 1
    if not window_open:
        # 过期不是静默的: 透传失败, 但把「这层包装该处理了」喊出来。
        print(
            f"::notice::进程死于原生崩溃 {describe_exit(first.returncode)}, 但重试窗口已于 "
            f"{RETRY_UNTIL} 过期 —— 请移除 scripts/run_pytest_with_crash_retry.py 这层包装"
            "(ci.yml 的 UI 步骤与 docs/testing.md), 或确认上游未修后明确续期",
            file=sys.stderr,
        )
        return first.returncode

    removed = _remove_new_entries(before)
    print(
        f"[crash-retry] 清掉崩溃这次的残缺产物({', '.join(str(p) for p in removed) or '无'})"
        "后原样重跑一次……",
        file=sys.stderr,
    )
    second = subprocess.run(args.command, check=False)  # noqa: S603 - 同上, 原样重跑
    _announce(first.returncode, removed, second)
    return second.returncode if second.returncode is not None else 1


if __name__ == "__main__":
    raise SystemExit(main())
