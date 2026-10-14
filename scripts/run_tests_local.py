r"""本地全量验证的并行分片: 同机起 N 个 pytest 进程各跑一片, 等全部结束, 汇总退出码.

为什么需要它(2026-10-07 实测): 本地全量(unit + integration, 2726 条)单进程要 18~20
分钟, 瓶颈不在少数慢用例(``--durations=60`` 里最长的 16.9s, Top60 合计只占 ~27%),
而在"280 条 ui 用例, 每条 3~4s"的固定成本 —— 建真窗 + 演示后端 + pump 断言 + 收尾
链. pytest-xdist 帮不上忙(docs/testing.md §6: GUI 用例并行只会互相拖慢, 还引出 Tk
初始化失败), 而分片是**进程级**并行: 两个进程各跑一半, 实测 18:28 -> 12:27(省
~36%), 2726 passed + 9 skipped 与单进程逐数一致, Tk 抖动由既有的重试/守卫兜住.
共享 UI 池的窗口在它那组用例结束时就地关闭(``tests/ui_sharing.py`` 的"用完即
关"), 两片不会再各挂几扇用不着的共享窗互拖 —— 那正是"并行没赚到位"的一大来源.

为什么默认 2 片: 两个进程同时操作真实窗口, 桌面合成层互相拖慢(实测单片比独跑多出
~35% 时长), 3 片以上干扰递增(``-n 4`` 的旧数据就是倒退的), 2 片是甜点. CI 不用这个
脚本 —— 那边是机器级并行(每片一个 runner), 见 ``.github/workflows/ci.yml`` 的分片
矩阵; 这个脚本只为"本地手动跑全量"这条路服务.

用法::

    uv run python scripts/run_tests_local.py                     # 双片跑全量
    uv run python scripts/run_tests_local.py -- tests/unit -q    # 双片只跑 unit
    uv run python scripts/run_tests_local.py --shard-count 3     # 想试 3 片时
    uv run python scripts/run_tests_local.py --coverage          # 连覆盖率门禁一起跑

``--coverage`` 是给"改完要核**覆盖率门禁**"这条路用的(``fail_under = 100``, 且判据
按平台口径 —— ``pytest --cov`` 套不上平台排除, 必须走 ``coverage run``): 每片用
``coverage run --rcfile=.coverage-platform.rc -m pytest`` 起, 各自的 ``COVERAGE_FILE``
写成 ``.coverage.shard-<片号>``(同一份文件被两个进程写会互相冲掉), 全部结束后在日志
目录里 ``coverage combine`` 成一份再 ``coverage report -m``, 报告尾行与退出码一起进
汇总。那份 rc 不存在时脚本会先 ``coverage_platform.py write`` 生成它(未入库的派生物)。

透传参数里的 ``--record-durations=PATH`` 会被特殊处理: 两片不能写同一份文件(后写的
会覆盖先写的), 所以每片各记 ``shard-<i>-durations.json``, 全部结束后自动合并(重叠取
中位数)写到 PATH —— 全量跑一次即可刷新分片权重的实测数据(见 ``tests/durations.py``
与 ``tests/sharding.py``)::

    uv run python scripts/run_tests_local.py -- --record-durations=tests/durations.json

每片的完整输出落在临时目录的 ``shard-<i>.log`` 里(结束时会打印路径), 终端只打各片
的摘要尾行; 任一片非 0 退出就以非 0 收场(取各片退出码的最大值), Ctrl+C 会转发给
所有还在跑的分片进程.

注意: pytest 参数里不要再写 ``--shard-count`` / ``--shard-index`` —— 片号由本脚本
注入, 重复给会以 pytest"后值覆盖前值"的规则悄悄改掉分片口径, 所以这里直接拒绝.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType

# 每片结束后往终端打的摘要行数: pytest 的收尾统计(deselected / passed / skipped /
# 耗时)就在日志的最后几行, 不必把整份日志倾倒进终端。
TAIL_LINES = 4

# 覆盖率模式用的三样: 平台口径的 rc(由 coverage_platform.py 生成, 未入库)、每片的数据
# 文件名、以及 combine/report 要用的数据基名(未指定时 coverage 默认写仓库根下的
# ``.coverage``)。
COVERAGE_RC = Path(__file__).resolve().parents[1] / ".coverage-platform.rc"
COVERAGE_SHARD_NAME = ".coverage.shard-{index}"


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8(Windows 控制台默认 cp1252, 打印中文会崩).

    ``merge_allure_results.py`` 等脚本各持一份: 都是独立入口, 不互相导入
    (scripts 不是包)。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


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
        "--coverage",
        action="store_true",
        help="各片用 coverage run 起(每片一份数据), 收尾 combine + report 并计入退出码",
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


def _split_record_target(pytest_args: list[str]) -> Path | None:
    """从透传参数里摘出 ``--record-durations`` 的目标路径(两种写法都认, 没有则 None)."""
    for index, flag in enumerate(pytest_args):
        if flag == "--record-durations" and index + 1 < len(pytest_args):
            return Path(pytest_args[index + 1])
        if flag.startswith("--record-durations="):
            return Path(flag.split("=", 1)[1])
    return None


def _shard_pytest_args(
    pytest_args: list[str], target: Path | None, log_dir: Path, index: int
) -> list[str]:
    """把本片的 ``--record-durations`` 改写到片私有文件, 其余参数原样保留.

    两片写同一份文件会互相覆盖(后结束的那份把先结束的清掉), 所以记录目标存在时,
    每片改写为 ``<日志目录>/shard-<i>-durations.json``, 合并交给收尾一步做。
    """
    if target is None:
        return list(pytest_args)
    shard_path = log_dir / f"shard-{index}-durations.json"
    rewritten: list[str] = []
    skip_value = False
    for flag in pytest_args:
        if skip_value:
            skip_value = False
            rewritten.append(str(shard_path))
            continue
        if flag == "--record-durations":
            rewritten.append(flag)
            skip_value = True
            continue
        if flag.startswith("--record-durations="):
            rewritten.append(f"--record-durations={shard_path}")
            continue
        rewritten.append(flag)
    return rewritten


def _load_durations_module() -> ModuleType:
    """按路径加载 ``tests/durations.py``(scripts 不在 pythonpath 里, 不能直接 import).

    已经导入过(pytest 里跑守卫用例)就复用那份: 再 spec-load 会造出第二个同名模块
    对象, 行为一样但身份不同, 没必要。
    """
    existing = sys.modules.get("durations")
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        "durations",
        Path(__file__).resolve().parents[1] / "tests" / "durations.py",
    )
    if spec is None or spec.loader is None:
        raise RuntimeError("加载 tests/durations.py 失败: importlib 没给出模块规格")
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _merge_recorded_durations(target: Path, log_dir: Path, count: int) -> None:
    """把各片的耗时记录合并(重叠取中位数)写到目标路径; 一片都没有就不覆盖.

    片失败也照样合并: 失败与否是用例结论, 记录的是"每条用例跑了多久", 两件事互不
    拖累。硬崩溃确实会少那一片的文件, 少了哪片就少哪片(没跑到的条目在分片时退回
    目录单价, 见 ``tests/sharding.py``)。
    """
    durations = _load_durations_module()
    maps = []
    for index in range(count):
        shard_path = log_dir / f"shard-{index}-durations.json"
        if shard_path.exists():
            maps.append(durations.parse(shard_path.read_text(encoding="utf-8")))
    if not maps:
        print(f"[durations] 一份分片记录都没找到, 不写 {target}", file=sys.stderr)
        return
    merged = durations.merge(*maps)
    durations.write(target, merged, recorded_at=datetime.now(UTC))
    print(f"[durations] 已合并 {len(maps)} 片、{len(merged)} 条用例的耗时 -> {target}")


def _ensure_coverage_rc() -> str | None:
    """覆盖率模式: rc 不在就先按当前平台生成一份(返回要打印的错误, 好了就 None).

    ``.coverage-platform.rc`` 是 ``coverage_platform.py`` 从 pyproject 派生的未入库文件
    (平台 pragma 的排除规则在里面) —— 少了它 ``coverage run`` 会用默认口径, 覆盖率会
    "莫名其妙不够"。这里自动补一次, 用户不必记那一步。
    """
    if COVERAGE_RC.exists():
        return None
    script = Path(__file__).resolve().parent / "coverage_platform.py"
    print(f"[coverage] 先生成 {COVERAGE_RC.name}(平台口径的排除规则)")
    result = subprocess.run(  # noqa: S603 - 命令与参数都写死在仓库内
        [sys.executable, str(script), "write"],
        check=False,
    )
    if result.returncode != 0 or not COVERAGE_RC.exists():
        return f"生成 {COVERAGE_RC.name} 失败 —— 手动跑 `python scripts/coverage_platform.py write`"
    return None


def _shard_command(
    uv: str, count: int, index: int, pytest_args: list[str], *, coverage: bool
) -> list[str]:
    """拼出一片的命令行(纯函数, 便于直接断言).

    普通模式就是 ``uv run pytest``; 覆盖率模式在中间包一层
    ``coverage run --rcfile=<平台口径的 rc> -m pytest``。
    """
    wrapper = ["coverage", "run", f"--rcfile={COVERAGE_RC}", "-m"] if coverage else []
    return [
        uv,
        "run",
        *wrapper,
        "pytest",
        "--shard-count",
        str(count),
        "--shard-index",
        str(index),
        *pytest_args,
    ]


def _shard_coverage_env(log_dir: Path, index: int) -> dict[str, str]:
    """覆盖率模式下该片要额外设的环境变量(纯函数).

    每片写**自己**的数据文件: 同一个 ``COVERAGE_FILE`` 被两个进程写会互相冲掉
    (只剩后写的那个), 合并时就会少一半数据。
    """
    return {"COVERAGE_FILE": str(log_dir / COVERAGE_SHARD_NAME.format(index=index))}


def _launch_shards(
    uv: str, count: int, pytest_args: list[str], log_dir: Path, *, coverage: bool
) -> list[tuple[int, subprocess.Popen[bytes], Path]]:
    """逐片启动 pytest(或 ``coverage run … pytest``), 返回 (片号, 进程, 日志路径)."""
    shards: list[tuple[int, subprocess.Popen[bytes], Path]] = []
    record_target = _split_record_target(pytest_args)
    for index in range(count):
        log_path = log_dir / f"shard-{index}.log"
        command = _shard_command(
            uv,
            count,
            index,
            _shard_pytest_args(pytest_args, record_target, log_dir, index),
            coverage=coverage,
        )
        env = dict(os.environ)
        if coverage:
            env.update(_shard_coverage_env(log_dir, index))
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
                        env=env,
                    ),
                    log_path,
                )
            )
        # with 块在这里关掉父进程的句柄(子进程已持有继承的副本), 片与片不共用文件.
    return shards


def _merge_coverage(uv: str, count: int, log_dir: Path) -> int:
    """合并各片覆盖率数据并出报告; 返回 coverage 的退出码(0 达标 / 非 0 不达标或失败).

    ``combine`` 以 ``COVERAGE_FILE`` 为**基名**去收 ``<基名>.*`` —— 所以这里的基名就是
    日志目录里的 ``.coverage``, 各片写的是它下面的 ``.coverage.shard-<片>``; 报告跑完把尾行
    打出来(它们就是 TOTAL 与门槛结论)。
    """
    missing = [
        index
        for index in range(count)
        if not (log_dir / COVERAGE_SHARD_NAME.format(index=index)).exists()
    ]
    if len(missing) == count:
        print("[coverage] 一片数据都没有, 跳过合并", file=sys.stderr)
        return 1
    if missing:
        print(f"[coverage] 缺片 {missing} 的数据(那一片没跑完)", file=sys.stderr)
    log_path = log_dir / "coverage.log"
    env = dict(os.environ)
    env["COVERAGE_FILE"] = str(log_dir / ".coverage")
    with log_path.open("wb") as handle:
        combined = subprocess.run(  # noqa: S603 - 命令与参数都写死在仓库内
            [uv, "run", "coverage", "combine", f"--rcfile={COVERAGE_RC}"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
        reported = subprocess.run(  # noqa: S603 - 同上
            [uv, "run", "coverage", "report", "-m", f"--rcfile={COVERAGE_RC}"],
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=env,
            check=False,
        )
    print(
        f"[coverage] 合并退出码 {combined.returncode}, 报告退出码 {reported.returncode}"
    )
    for line in _tail(log_path, lines=6):
        print(f"    {line}")
    if combined.returncode != 0:
        return combined.returncode
    return reported.returncode


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
    *,
    coverage_code: int | None = None,
) -> int:
    """按片号打印退出码与摘要尾行, 返回各片与覆盖率退出码的最大值(全 0 才是 0)."""
    logs = {index: log_path for index, _, log_path in shards}
    for index in sorted(codes):
        mark = "OK  " if codes[index] == 0 else "FAIL"
        print(f"[shard {index}] {mark} 退出码 {codes[index]}")
        for line in _tail(logs[index]):
            print(f"    {line}")
    if coverage_code is not None:
        mark = "OK  " if coverage_code == 0 else "FAIL"
        print(f"[coverage] {mark} 退出码 {coverage_code}")
    passed = sum(1 for code in codes.values() if code == 0)
    steps = [*codes.values(), *([] if coverage_code is None else [coverage_code])]
    tail = (
        ""
        if coverage_code is None
        else f", 覆盖率{'过' if coverage_code == 0 else '没过'}"
    )
    print(
        f"合计: {passed}/{len(codes)} 片通过{tail}, 墙钟 {_format_seconds(elapsed)};"
        f" 完整日志在 {logs[0].parent}"
    )
    return max(steps, default=0)


def main(argv: Sequence[str] | None = None) -> int:
    """起 N 个分片进程并行跑, 等全部结束, 以各片退出码的最大值收场."""
    ensure_utf8_output()
    args = parse_args(sys.argv[1:] if argv is None else argv)
    problem = _validate(args)
    if problem is not None:
        print(problem, file=sys.stderr)
        return 2
    uv = shutil.which("uv")
    if uv is None:
        print("找不到 uv —— 本脚本通过 `uv run pytest` 起分片进程", file=sys.stderr)
        return 1
    if args.coverage:
        problem = _ensure_coverage_rc()
        if problem is not None:
            print(problem, file=sys.stderr)
            return 2

    log_dir = Path(tempfile.mkdtemp(prefix="pytest-shards-"))
    started = time.monotonic()
    shards = _launch_shards(
        uv, args.shard_count, list(args.pytest_args), log_dir, coverage=args.coverage
    )
    try:
        codes = {index: process.wait() for index, process, _ in shards}
    except KeyboardInterrupt:
        _terminate_shards(shards)
        return 130
    record_target = _split_record_target(list(args.pytest_args))
    if record_target is not None:
        # 合并放在退出码判定之前: 用例失败不该拖着耗时数据一起丢。
        _merge_recorded_durations(record_target, log_dir, args.shard_count)
    coverage_code: int | None = None
    if args.coverage:
        # 覆盖率结论也要进退出码(门槛不达标时 coverage report 自己退非 0)。
        coverage_code = _merge_coverage(uv, args.shard_count, log_dir)
    return _report(
        shards, codes, time.monotonic() - started, coverage_code=coverage_code
    )


if __name__ == "__main__":
    raise SystemExit(main())
