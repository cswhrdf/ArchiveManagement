r"""本地全量验证的并行分片: 同机起 N 个 pytest 进程各跑一片, 等全部结束, 汇总退出码.

为什么需要它(2026-10-07 实测): 本地全量(unit + integration, 2726 条)单进程要 18~20
分钟, 瓶颈不在少数慢用例(``--durations=60`` 里最长的 16.9s, Top60 合计只占 ~27%),
而在"280 条 ui 用例, 每条 3~4s"的固定成本 —— 建真窗 + 演示后端 + pump 断言 + 收尾
链. pytest-xdist 帮不上忙(docs/testing.md §6: GUI 用例并行只会互相拖慢, 还引出 Tk
初始化失败), 而分片是**进程级**并行: 两个进程各跑一半, 实测 18:28 -> 12:27(省
~36%), 2726 passed + 9 skipped 与单进程逐数一致, Tk 抖动由既有的重试/守卫兜住.

为什么默认 2 片: 两个进程同时操作真实窗口, 桌面合成层互相拖慢(实测单片比独跑多出
~35% 时长), 3 片以上干扰递增(``-n 4`` 的旧数据就是倒退的), 2 片是甜点. CI 不用这个
脚本 —— 那边是机器级并行(每片一个 runner), 见 ``.github/workflows/ci.yml`` 的分片
矩阵; 这个脚本只为"本地手动跑全量"这条路服务.

用法::

    uv run python scripts/run_tests_local.py                     # 双片跑全量
    uv run python scripts/run_tests_local.py -- tests/unit -q    # 双片只跑 unit
    uv run python scripts/run_tests_local.py --shard-count 3     # 想试 3 片时

每片的完整输出落在临时目录的 ``shard-<i>.log`` 里(结束时会打印路径), 终端只打各片
的摘要尾行; 任一片非 0 退出就以非 0 收场(取各片退出码的最大值), Ctrl+C 会转发给
所有还在跑的分片进程.

注意: pytest 参数里不要再写 ``--shard-count`` / ``--shard-index`` —— 片号由本脚本
注入, 重复给会以 pytest"后值覆盖前值"的规则悄悄改掉分片口径, 所以这里直接拒绝.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from pathlib import Path

# 每片结束后往终端打的摘要行数: pytest 的收尾统计(deselected / passed / skipped /
# 耗时)就在日志的最后几行, 不必把整份日志倾倒进终端.
TAIL_LINES = 4


def _format_seconds(seconds: float) -> str:
    """把秒数写成 ``12:27`` 这样的 mm:ss(启动横幅与结束摘要共用)."""
    minutes, rest = divmod(int(seconds), 60)
    return f"{minutes}:{rest:02d}"


def _tail(path: Path, lines: int = TAIL_LINES) -> list[str]:
    """读日志末尾的几行(读不到就还一个空列表, 摘要不该因 IO 报错盖过退出码)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return text[-lines:]


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """``--shard-count`` 定并行片数; 其余位置参数原样透传给 pytest."""
    parser = argparse.ArgumentParser(
        description="本地并行分片跑 pytest(默认双片), 等全部结束并汇总各片退出码",
    )
    parser.add_argument(
        "--shard-count",
        type=int,
        default=2,
        help="并行进程数(默认 2; GUI 用例互相拖慢, 3 片以上通常得不偿失)",
    )
    parser.add_argument(
        "pytest_args",
        nargs="*",
        help="透传给 pytest 的参数(建议用 -- 与本脚本的参数隔开)",
    )
    return parser.parse_args(list(argv))


def _validate(args: argparse.Namespace) -> str | None:
    """参数不对时返回要打印的原因(对就返回 None, 让调用方直接开跑)."""
    conflicting = [
        flag
        for flag in args.pytest_args
        if flag.startswith("--shard-count") or flag.startswith("--shard-index")
    ]
    if conflicting:
        return (
            f"pytest 参数里不要再给 {conflicting} —— 片号由本脚本注入, "
            "重复给会悄悄改掉分片口径"
        )
    if args.shard_count < 1:
        return f"--shard-count 至少是 1(拿到的是 {args.shard_count})"
    return None


def _launch_shards(
    uv: str, count: int, pytest_args: list[str], log_dir: Path
) -> list[tuple[int, subprocess.Popen[bytes], Path]]:
    """逐片启动 ``uv run pytest``, 返回 (片号, 进程, 日志路径) 的列表."""
    shards: list[tuple[int, subprocess.Popen[bytes], Path]] = []
    for index in range(count):
        log_path = log_dir / f"shard-{index}.log"
        command = [
            uv,
            "run",
            "pytest",
            "--shard-count",
            str(count),
            "--shard-index",
            str(index),
            *pytest_args,
        ]
        print(f"[shard {index}] 启动: {' '.join(command)}")
        print(f"[shard {index}] 日志: {log_path}")
        # stdout/stderr 都落该片的日志文件: 终端留摘要, 完整输出可回查.
        with log_path.open("wb") as handle:
            shards.append(
                (
                    index,
                    subprocess.Popen(  # noqa: S603 - 命令固定是 uv run pytest, 参数透传自 argv
                        command,
                        stdout=handle,
                        stderr=subprocess.STDOUT,
                    ),
                    log_path,
                )
            )
        # with 块在这里关掉父进程的句柄(子进程已持有继承的副本), 片与片不共用文件.
    return shards


def _terminate_shards(
    shards: list[tuple[int, subprocess.Popen[bytes], Path]],
) -> None:
    """Ctrl+C 的收尾: 先请全部还在跑的进程退出, 请不动的再杀."""
    print("\n收到中断, 终止全部分片进程……", file=sys.stderr)
    for _, process, _ in shards:
        if process.poll() is None:
            process.terminate()
    for _, process, _ in shards:
        # 再按一次 Ctrl+C 也不打断收尾; 杀不掉的进程由系统随会话回收.
        try:
            process.wait(timeout=10)
        except (KeyboardInterrupt, subprocess.TimeoutExpired):
            process.kill()


def _report(
    shards: list[tuple[int, subprocess.Popen[bytes], Path]],
    codes: dict[int, int],
    elapsed: float,
) -> int:
    """按片号打印退出码与摘要尾行, 返回各片退出码的最大值(全 0 才是 0)."""
    logs = {index: log_path for index, _, log_path in shards}
    for index in sorted(codes):
        mark = "OK  " if codes[index] == 0 else "FAIL"
        print(f"[shard {index}] {mark} 退出码 {codes[index]}")
        for line in _tail(logs[index]):
            print(f"    {line}")
    passed = sum(1 for code in codes.values() if code == 0)
    print(
        f"合计: {passed}/{len(codes)} 片通过, 墙钟 {_format_seconds(elapsed)};"
        f" 完整日志在 {logs[0].parent}"
    )
    return max(codes.values(), default=0)


def main(argv: Sequence[str] | None = None) -> int:
    """起 N 个分片进程并行跑, 等全部结束, 以各片退出码的最大值收场."""
    args = parse_args(sys.argv[1:] if argv is None else argv)
    problem = _validate(args)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    uv = shutil.which("uv")
    if uv is None:
        print("找不到 uv —— 本脚本通过 `uv run pytest` 起分片进程", file=sys.stderr)
        return 1

    log_dir = Path(tempfile.mkdtemp(prefix="pytest-shards-"))
    started = time.monotonic()
    shards = _launch_shards(uv, args.shard_count, list(args.pytest_args), log_dir)
    try:
        codes = {index: process.wait() for index, process, _ in shards}
    except KeyboardInterrupt:
        _terminate_shards(shards)
        return 130
    return _report(shards, codes, time.monotonic() - started)


if __name__ == "__main__":
    raise SystemExit(main())
