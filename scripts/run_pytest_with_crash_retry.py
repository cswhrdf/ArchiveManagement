r"""把"死于原生崩溃或**用例超时**"的测试进程原地重跑 —— 绝不让普通失败被重试.

背景一(2026-10-05, run 37270928260): macOS 26.6.2 arm64 + Tcl/Tk 9.0 上, UI 分片死在 Tk
Aqua 位图绘制的 use-after-free(``EXC_BAD_ACCESS @0x70``, 崩在 ``-[NSCGSContext
dealloc]`` 一族, 排查记录见 ``docs/testing-ci.md``)。这是原生层的缺陷, 同一用例在
Linux/Windows 都过; 但 pytest 进程一死, 崩溃点之后的用例全部没有结果, 分片报缺。

背景二(2026-10-16, run 38758273842): Windows 的 UI 段在
``test_gui_lifecycle.py::test_run_gui_reports_every_config_state`` 上卡到 60s 超时上限
(栈停在 ``database.migrate()`` 里的 ``connection.execute(statement)`` —— runner 磁盘/fsync
的偶发, 与那只用例的 ``app.db`` 无关: 三次启动各用自己的目录, 不是锁冲突)。
``tests/timeout_guard.py`` 落完现场就 ``os._exit(1)``, 而 **1 不在 :data:`CRASH_EXIT_CODES`**
⇒ 那轮一条都不重跑 ⇒ 该段后半截的 34 条用例没跑(Windows 的 e2e 252 vs 别的平台 282),
覆盖率差 67 行 / 33 分支 ⇒ ``Coverage report(Windows)`` 99.51% < 100% 判红。
**超时和原生崩溃是同一类事故: 进程死了, 它后面的一切都没了** —— 所以这里一并接管。

超时那条路比崩溃多一步: 第一次**原样重跑**(环境卡顿多半不会再来, 这一遍就全过了); 若仍
超时, 第二遍把挂住的那条用例 ``--deselect`` 掉再跑 —— 保证其余用例与覆盖率拿到手, 而那次
超时的结论**留在报告里**(挂住是真信号, 不能让它静默消失)。

为什么敢说它**不影响测试准确率**:

* 崩溃只认"进程被信号/原生异常杀死"的退出码(见 :data:`CRASH_EXIT_CODES`); 超时**不看退出
  码**, 只认这次尝试真的留下了超时痕迹(``tests/timeout_guard.py`` 写的 ``fullName``
  以 :data:`TIMEOUT_FULL_NAME` 开头的结果, 或 ``*_timeout.dump``) —— 两者都只有超时才写。
  断言失败、用例红、门禁不过都是普通退出码且没有这两个痕迹, **一次都不会重跑**;
* 重试前把上一次已落盘的**残缺产物**清掉, 而且只清这一次**新增**的(``--watch-dir``):
  Allure 的结果文件是逐用例写的, 死了那次留下的半套结果若不清, 会与重跑结果混进同一份
  报告(同一用例出现两份结论)。前一段进程(not ui)写的结果在快照之外, 原样保留;
  覆盖率文件不用清 —— pytest-cov 只在会话**结束**才落盘, 死了那次什么都没写进去
  (前一段的数据完好, 见 ci.yml 两段式拆分的注释);
* 重试最多两次(超时那条路上); 全都失败就按最后一次的退出码收场, 证据(``faulthandler.log``、
  ``.ips``、``*_timeout.dump``)照常落在 ``crash-dumps/`` 并被 ``if: always()`` 的上传步收走。

**这是临时缓解, 不许烂尾**(用户 2026-10-05 的要求): 重试有到期日
(:data:`RETRY_UNTIL`)。到期之后:

* 这个脚本直接透传、不再重试, 并在日志里打提醒;
* ``tests/unit/test_ci_diagnostics.py`` 里那条"窗口没被忘记"的守卫**当场变红** ——
  要么移除这层包装(ci.yml 的两个 UI 步骤与 docs/testing-ci.md 的说明), 要么在确认上游
  仍没修好后**明确续期**(改 :data:`RETRY_UNTIL` 并在续期理由里写清依据)。

用法(CI, 见 ``.github/workflows/ci.yml`` 的 UI 段)::

    uv run python scripts/run_pytest_with_crash_retry.py \\
        --watch-dir allure-results -- uv run pytest ...

退出码: 第一次(或唯一一次)运行的退出码原样透传; 重试后以最后一次为准。
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 重试窗口的到期日(含当天, UTC)。放一个月: 到时候上游(python.org 的 Tcl/Tk 9.0 构建 /
# Tk 源码)要么出了补丁, 要么确认无解要换方案; Windows 那条超时(见模块说明的二)也一样 ——
# 要么 runner 的磁盘卡顿不再出现, 要么那时该换一条更根本的路子。无论哪种, 都该回来重新审视
# 这层包装 —— 守卫测试会在到期次日变红, 忘不掉。
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

# ``tests/timeout_guard.py`` 在超时时写的那条结论的 ``fullName`` 前缀(用例 nodeid 拼在后面)。
# **只有超时才写这种结果** —— 普通失败走 pytest / allure-pytest 自己那条路, 所以拿它当
# "这次尝试死于超时"的判据是硬的。两边必须一致(`tests/unit/test_ci_diagnostics.py` 里那条
# 守卫会逐字核对 timeout_guard 源码里的拼法)。
TIMEOUT_FULL_NAME = "archive-management.timeout."

# 超时留证落的 dump 文件名后缀(``<用例名>_timeout.dump``)。没配 ``--alluredir`` 时拿不到上面
# 那条结论, 就用它当兜底信号(只重跑一次, 不 deselect —— 从文件名反推不出可靠的 nodeid)。
TIMEOUT_DUMP_SUFFIX = "_timeout.dump"


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


def _remove_new_entries(
    before: dict[Path, set[str]], *, keep: frozenset[str] = frozenset()
) -> list[Path]:
    """删掉快照之后新出现的条目(文件或目录), 返回删了什么.

    ``keep`` 里的名字留着不删 —— 超时那条路要靠它把"挂住"的证据留在报告里
    (见 :func:`_timeout_evidence`)。
    """
    removed: list[Path] = []
    for directory, names in before.items():
        if not directory.is_dir():
            continue
        for entry in directory.iterdir():
            if entry.name in names or entry.name in keep:
                continue
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                with contextlib.suppress(OSError):
                    entry.unlink()
            removed.append(entry)
    return removed


def _new_entries(directory: Path, names: set[str]) -> list[Path]:
    """目录里这次新增的条目(目录不存在就给空表); 名字排序让判据稳定."""
    if not directory.is_dir():
        return []
    return [entry for entry in sorted(directory.iterdir()) if entry.name not in names]


@dataclass(frozen=True)
class TimeoutEvidence:
    """一次超时留下的两样东西: 挂住的用例 id, 以及"重跑前要留着"的那些文件名."""

    nodeids: tuple[str, ...]
    keep: frozenset[str]


def _timeout_evidence(before: dict[Path, set[str]]) -> TimeoutEvidence:
    """这次尝试新写下的"超时留证"结论(nodeid 与要留着的附件名一次读完).

    只认 ``fullName`` 以 :data:`TIMEOUT_FULL_NAME` 开头的结果 —— 那是 ``tests/timeout_guard``
    在超时时才写的东西, 普通失败不会出现。读不出来的文件直接跳过(判据宁漏不错)。

    ``keep`` 的用处: 重跑前要把"死掉那次的残缺结果"清掉(否则同一用例两份结论), 但**不能**把
    "挂住了"这条结论一起清掉 —— 挂住是真信号, 清掉就变成"那条用例静默消失"。
    """
    nodeids: list[str] = []
    keep: list[str] = []
    for directory, names in before.items():
        for entry in _new_entries(directory, names):
            if not entry.name.endswith("-result.json"):
                continue
            payload = _read_json(entry)
            if payload is None:
                continue
            full_name = str(payload.get("fullName") or "")
            if not full_name.startswith(TIMEOUT_FULL_NAME):
                continue
            nodeids.append(full_name[len(TIMEOUT_FULL_NAME) :])
            keep.append(entry.name)
            keep.extend(
                str(item["source"])
                for item in payload.get("attachments", [])
                if item.get("source")
            )
    return TimeoutEvidence(tuple(nodeids), frozenset(keep))


def _read_json(path: Path) -> dict[str, Any] | None:
    """读一个 JSON 文件; 读不动就返回 ``None``(判据宁漏不错)."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _dump_state(directory: Path) -> dict[str, tuple[int, int]]:
    """Dump 目录的 ``{文件名: (大小, mtime)}`` 快照.

    为什么不只记名字: 超时的 dump 是**故意留着**的证据(下一遍要留着它去 deselect), 所以
    "这一遍有没有新痕迹"不能只看名字 —— 同一条用例再挂一次会覆盖同名文件, 记名字会两遍都
    判成"没有新的"; 反过来, 留着不删的旧 dump 会因为"名字不在基线里"而永远看起来是新的。
    """
    if not directory.is_dir():
        return {}
    state: dict[str, tuple[int, int]] = {}
    for entry in directory.iterdir():
        with contextlib.suppress(OSError):
            stat = entry.stat()
            state[entry.name] = (stat.st_size, stat.st_mtime_ns)
    return state


def _fresh_dumps(state: dict[str, tuple[int, int]], directory: Path) -> list[Path]:
    """这一遍新出现、或内容变过的超时 dump(拿不到结果目录时的兜底信号)."""
    fresh: list[Path] = []
    for entry in _new_entries(directory, set()):
        if not entry.name.endswith(TIMEOUT_DUMP_SUFFIX):
            continue
        with contextlib.suppress(OSError):
            stat = entry.stat()
            if state.get(entry.name) != (stat.st_size, stat.st_mtime_ns):
                fresh.append(entry)
    return fresh


def _timed_out(
    before: dict[Path, set[str]], dump_dir: Path, dump_state: dict[str, tuple[int, int]]
) -> bool:
    """这次尝试是不是死于超时(看痕迹, 不看退出码)."""
    return bool(_timeout_evidence(before).nodeids) or bool(
        _fresh_dumps(dump_state, dump_dir)
    )


def _cleaned(removed: list[Path]) -> str:
    """把"清掉了什么"写成一行可读的话."""
    return ", ".join(str(path) for path in removed) or "无"


def _write_summary(title: str, lines: list[str]) -> None:
    """往运行摘要页追加一段(没配 ``GITHUB_STEP_SUMMARY`` 就只靠日志里的 ::warning::)."""
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not summary:
        return
    try:
        with Path(summary).open("a", encoding="utf-8") as handle:
            handle.write("\n".join([title, "", *lines, ""]))
    except OSError:  # pragma: no cover - 摘要文件不可写不影响重试本身
        pass


def _announce_timeout_retry(
    hung: Sequence[str], removed: list[Path], rerun_code: int | None
) -> None:
    """超时但**重跑一遍就过了**: 那条超时结论已清掉, 只留一句看得见的痕迹.

    用户 2026-10-16 的决定: 环境卡顿引起的超时, 重跑成功后不把那条"超时留证"留在报告里
    (它是那次意外卡顿的痕迹, 不是这次运行的结论); 但**不能静默** —— 日志与运行摘要都要说话。
    """
    names = ", ".join(hung) or "未知"
    cleaned = _cleaned(removed)
    print(
        f"::warning::测试进程有用例卡到超时上限({names}), 已清掉那次的残缺产物({cleaned})"
        f"、不保留那条超时结论, 并原样重跑一遍(退出码 {rerun_code}) —— 详见运行摘要",
        file=sys.stderr,
    )
    _write_summary(
        "### 用例超时重试",
        [
            f"- 第一次尝试里这些用例卡到超时上限: {names};",
            f"- 清掉了那次的残缺产物(只清本次新增的): {cleaned}; 那条 `超时留证` 结论不保留;",
            f"- 原样重跑一遍的退出码 {rerun_code} —— 超时是环境偶发, 不是用例失败。",
        ],
    )


def _announce_timeout_deselect(
    hung: Sequence[str], removed: list[Path], rerun_code: int | None, deselected: str
) -> None:
    """两遍都卡住 ⇒ 把它 ``--deselect`` 掉跑完其余用例, 那条超时结论**留在报告里**."""
    names = ", ".join(hung) or "未知"
    cleaned = _cleaned(removed)
    print(
        f"::warning::用例 {deselected} 两遍都卡到超时上限, 已清掉重跑那遍的残缺产物"
        f"({cleaned})、把它 deselect 掉后跑完了其余用例(退出码 {rerun_code});"
        "那条超时结论留在报告里 —— 它是真挂住的信号",
        file=sys.stderr,
    )
    _write_summary(
        "### 用例超时重试",
        [
            f"- 这些用例卡到超时上限: {names};",
            f"- 重跑一遍仍然超时 ⇒ 把 `{deselected}` deselect 掉再跑一遍, 先保住其余用例与覆盖率;",
            f"- 清掉了重跑那遍的残缺产物: {cleaned};",
            f"- 最终退出码 {rerun_code}; 它的 `超时留证` 结论留在报告里(挂住是真信号),",
            "  栈、线程清单与 coredumpy dump 也都在结论的附件里。",
        ],
    )


def _announce_timeout_stop(hung: Sequence[str], rerun_code: int | None) -> None:
    """又超时、但**拿不到挂住的用例 id** ⇒ 不再重跑, 残留原样留着, 如实收场."""
    names = ", ".join(hung) or "未知"
    print(
        f"::warning::测试进程又卡到超时上限({names}, 退出码 {rerun_code}), "
        "但拿不到挂住的用例 id(没配 --alluredir 时只有 dump 那个兜底信号) ⇒ 不再重跑, "
        "残留结果与 dump 原样留在产物里",
        file=sys.stderr,
    )
    _write_summary(
        "### 用例超时重试",
        [
            f"- 这些用例卡到超时上限: {names};",
            "- 重跑一遍仍然超时, 但拿不到 nodeid ⇒ 无法 deselect, 不再重跑(不猜、不删);",
            f"- 最终退出码 {rerun_code}; 超时结论与 dump 都在产物里。",
        ],
    )


def _announce(
    code: int, removed: list[Path], rerun: subprocess.CompletedProcess[bytes]
) -> None:
    """重试收场后要留下看得见的痕迹: 运行日志里的 warning 注解 + 运行摘要页的一段话."""
    cleaned = _cleaned(removed)
    print(
        f"::warning::测试进程死于原生崩溃 {describe_exit(code)}, "
        f"已清理其残缺产物({cleaned})并以重跑退出码 {rerun.returncode} 收场 —— 详见运行摘要",
        file=sys.stderr,
    )
    _write_summary(
        "### 原生崩溃重试(临时缓解)",
        [
            f"- 第一次尝试死于 {describe_exit(code)}: macOS 26 + Tcl/Tk 9.0 的原生缺陷"
            "(排查记录见 docs/testing-ci.md), 不是用例失败;",
            f"- 已清理该次落盘的残缺产物: {cleaned};",
            f"- 重跑退出码 {rerun.returncode}。",
        ],
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    """``--watch-dir`` 可重复; ``--`` 之后是要包起来的整条命令."""
    parser = argparse.ArgumentParser(
        description="测试进程死于原生崩溃或超时时重跑(普通失败原样透传)"
    )
    parser.add_argument(
        "--watch-dir",
        action="append",
        default=[],
        type=Path,
        help="重试前要清掉「本次新增内容」的目录(可重复, 如 allure-results)",
    )
    parser.add_argument(
        "--timeout-dump-dir",
        type=Path,
        default=Path("crash-dumps"),
        help="超时留证的 dump 落在哪里(判「这次是不是超时」的兼底信号, 与 conftest 的默认值一致)",
    )
    parser.add_argument(
        "command",
        nargs="+",
        help="要运行的命令(建议用 -- 与本脚本的参数隔开)",
    )
    return parser.parse_args(argv)


def _notice_expired(what: str) -> None:
    """窗口过期时的提醒(不是静默的: 透传失败, 但把"这层包装该处理了"喊出来)."""
    print(
        f"::notice::{what}, 但重试窗口已于 {RETRY_UNTIL} 过期 —— 请移除 "
        "scripts/run_pytest_with_crash_retry.py 这层包装(ci.yml 的 UI 步骤与 "
        "docs/testing-ci.md), 或确认原因依在后明确续期",
        file=sys.stderr,
    )


def _run(command: list[str]) -> subprocess.CompletedProcess[bytes]:
    """跑一次被包装的命令(命令来自 ci.yml 的包装参数)."""
    return subprocess.run(command, check=False)  # noqa: S603 - 参数由调用方给出


def _exit_code(code: int | None) -> int:
    """``subprocess`` 拿不到退出码时按失败算."""
    return code if code is not None else 1


def _retry_after_crash(
    command: list[str], before: dict[Path, set[str]], code: int, *, window_open: bool
) -> int:
    """原生崩溃: 清掉该次新增的结果后原样重跑一次(第二次仍崩就按它的退出码收场)."""
    if not window_open:
        _notice_expired(f"进程死于原生崩溃 {describe_exit(code)}")
        return code
    removed = _remove_new_entries(before)
    print(
        f"[crash-retry] 清掉崩溃这次的残缺产物({_cleaned(removed)})后原样重跑一次……",
        file=sys.stderr,
    )
    second = _run(command)
    _announce(code, removed, second)
    return _exit_code(second.returncode)


def _retry_after_timeout(
    args: argparse.Namespace,
    before: dict[Path, set[str]],
    dumps_before: dict[str, tuple[int, int]],
    code: int,
    *,
    window_open: bool,
) -> int:
    """超时: 先原样重跑一次; 还超时就把挂住的那条 deselect 掉再跑一遍(见模块说明).

    两阶段的原因: 环境卡顿多半不会连着来(这一遍就全过了, 报告干净); 真的每遍都卡住时,
    至少要把**其余用例与覆盖率**拿到手 —— 它们的缺失才是那次事故真正的代价。
    """
    first_evidence = _timeout_evidence(before)
    if not window_open:
        _notice_expired(
            "这次尝试里有用例超时(退出码 "
            f"{code}, 挂住的是 {', '.join(first_evidence.nodeids) or '未知'})"
        )
        return code
    removed = _remove_new_entries(before)
    print(
        f"[timeout-retry] 清掉超时这次的残缺产物({_cleaned(removed)}; "
        "crash-dumps 里的 dump 留着)后原样重跑一次……",
        file=sys.stderr,
    )
    # 判"第二遍有没有又超时"要用**清完之后**重拍的 dump 快照: 第一遍那个 dump 是故意留着的
    # 证据(拿它去 deselect), 拿旧基线一比会永远看起来"又超时"。
    dumps_mid = _dump_state(args.timeout_dump_dir)
    second = _run(args.command)
    if not _timed_out(before, args.timeout_dump_dir, dumps_mid):
        _announce_timeout_retry(first_evidence.nodeids, removed, second.returncode)
        return _exit_code(second.returncode)

    again = _timeout_evidence(before)
    if not again.nodeids:
        # 拿不到 nodeid(没配 --alluredir 就只有 dump 那个兜底信号) ⇒ 没法 deselect。
        # 什么都不删: 这一遍的结果(含那条超时结论)就是这场事故的全部证据。
        _announce_timeout_stop(first_evidence.nodeids, second.returncode)
        return _exit_code(second.returncode)

    # 只在真要 deselect 时才清第二遍的产物: 它的"超时留证"要留着(挂住是真信号),
    # 其余残缺结果得清掉 —— 否则同一用例在报告里两份结论, 第三遍的结果还会再叠一层。
    removed_again = _remove_new_entries(before, keep=again.keep)
    print(
        f"[timeout-retry] 又一次超时: deselect {again.nodeids[0]} 后再跑一遍, "
        "先把其余用例与覆盖率拿到手……",
        file=sys.stderr,
    )
    third = _run([*args.command, "--deselect", again.nodeids[0]])
    _announce_timeout_deselect(
        again.nodeids,
        [*removed, *removed_again],
        third.returncode,
        again.nodeids[0],
    )
    return _exit_code(third.returncode)


def main(argv: list[str] | None = None) -> int:
    """跑一次; 死于原生崩溃或超时(且窗口未过期)就把这层拉起来(见模块说明)."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    window_open = retry_window_open(dt.datetime.now(dt.UTC).date())
    before = _snapshot(args.watch_dir)
    dumps_before = _dump_state(args.timeout_dump_dir)

    first = _run(args.command)
    if is_crash_exit(first.returncode):
        return _retry_after_crash(
            args.command, before, _exit_code(first.returncode), window_open=window_open
        )
    if not _timed_out(before, args.timeout_dump_dir, dumps_before):
        # 普通失败(断言红 / 门禁不过): **一次都不重跑**。
        return _exit_code(first.returncode)
    return _retry_after_timeout(
        args,
        before,
        dumps_before,
        _exit_code(first.returncode),
        window_open=window_open,
    )


if __name__ == "__main__":
    raise SystemExit(main())
