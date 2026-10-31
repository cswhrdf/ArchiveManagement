"""把代码质量门禁的结论写进 Allure.

为什么单独一个脚本: 这些门禁的结论原来只出现在 CI 日志里, 汇总报告里看不到 ——
报告要能回答"这次提交在质量门禁上过了吗", 结论就得变成报告里的一条结果。

门禁分三组(用 ``--group`` 选):

- ``core``: Ruff 检查 / Ruff 格式 / mypy 类型(宿主平台那一次) —— 公共检查(与路径分隔符、
  显示、字体都无关), CI 只在 Ubuntu 跑一遍(CI 的 quality job);
- ``platform``: mypy 的 ``--platform win32`` 与 ``--platform darwin``(补上宿主那次收窄掉
  的另外两支) —— **各自在那个平台上执行**(CI 的 quality-platform job 按平台展开)。
  ``--platform`` 只是把类型检查指向某支代码, 并不校验执行环境; 结论要挂进那个平台的环境,
  就得真的在那台机器上产出 —— 否则报告里"Windows 的结论"其实产自 Ubuntu, 环境归属是假的;
- ``analysis``: deptry 依赖卫生 / Bandit 安全扫描 / pip-audit 依赖漏洞 / Radon 复杂度报告 /
  Xenon 复杂度门槛 —— 与平台无关, 只在 Ubuntu 跑一遍(CI 的 analysis job)。

复杂度门槛取 :data:`MAX_COMPLEXITY` = 10, 与 pyproject 里 Ruff 的
``[tool.ruff.lint.mccabe] max-complexity`` **同一个数值**; 注意 Radon 会把 ``with``/
``assert``/布尔运算也算作分支, 所以同一段代码在 Radon 下会比 Ruff 的 C901 高 2~5 分。

做法与 :mod:`create_allure_coverage` 一致: 执行检查命令, 把**原始输出**作为附件带进结果;
环境标签则分两类 ——

- **公共检查**(ruff / 格式 / 宿主平台 mypy / 静态分析)写 ``env=common``: 它们与平台无关,
  于是落进仓库根 ``allurerc.mjs`` 里**显式声明**的 ``Common`` 环境(matcher 匹配
  ``env=common``), 而不是某个平台的环境(那会误导成"Linux 上的质量检查");
- **平台专属检查**(``mypy --platform win32`` / ``darwin``)写各自平台的 ``env``
  (``Windows`` / ``macOS``): 它们验的是那个平台专属代码路径的类型, 而且**就在那个平台上
  执行**(:attr:`Check.host_platform`)—— 于是"标着 Windows 的结论一定产自 Windows"由结构
  保证, 而不是靠两处常量恰好写得一致。

执行主机只写进描述作为排障线索; 面向人的总结在报告首页的运行总账里(见
:mod:`create_allure_summary`)。

任一项检查失败时脚本以非 0 退出(质量门禁照常拦住提交), 但**先把结论写进结果目录**,
于是报告里能看到"哪一项没过 + 完整输出"。

用法:

- CI(quality job): ``--group core --results-dir allure-results-quality``;
- CI(quality-platform job, Windows 与 macOS 各一份): ``--group platform``;
- CI(analysis job): ``--group analysis --results-dir allure-results-analysis``;
- 本地: 不带参数(跑全部, 写进 ``allure-results``)。平台专属检查只跑当前平台那一支
  (另一支给出"跳过"的理由); 显式要一个在当前平台跑不了的分组时以退出码 2 报错, 免得
  有人把它放到错误的平台上却什么也没检查。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_RESULTS_DIRECTORY = Path("allure-results")
# 质量门禁项本身不验证行为, 只是把结论与原始输出带进报告;
# 不打等级会让报告多出一个 no_severity 桶(与覆盖率/性能/安全汇总项一致)。
QUALITY_SEVERITY = "trivial"
# 公共检查的环境标签: 质量检查与平台无关, 用一个显式声明的环境装它们(见 allurerc.mjs
# 里的 `common` matcher) —— 既不是某个平台的环境, 也不是隐式的 default。改这里要同步
# 改配置, tests/unit/test_report_verification.py 有守卫把两处钉在一起。
QUALITY_ENVIRONMENT = "common"
# 结论项的类别标签: allurerc.mjs 的"工程门禁:质量检查未通过"分类规则按**这一对取值**
# 挑选质量检查结果, 运行总账(create_allure_summary.py)也按它收集这些项 —— 三处必须一致。
# 守卫在 tests/unit/test_report_verification.py (改配置里的取值要同步改这里)。
CATEGORY_LABEL = "testCategory"
CATEGORY_VALUE = "quality"
# 平台专属检查: ``sys.platform`` 取值 → 报告的"环境"名。取值必须与平台展示名
# (`platform_name()` / 用例的 env 标签)以及 allurerc.mjs 的 matcher 一致 ——
# 守卫会把三处钉在一起。
PLATFORM_ENVIRONMENTS = {"win32": "Windows", "darwin": "macOS"}
# 终端控制序列(颜色 / 光标): 有些工具即使输出被重定向也会带上(实测 deptry 写成附件
# 后是 ``\x1b[1m\x1b[32mSuccess! No dependency issues found.\x1b[m``), 在报告的纯文本
# 查看器里就显示成乱码。进报告前剥掉, 控制台日志仍打印原样输出(CI 日志会渲染颜色)。
ANSI_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# 单项检查的超时: mypy 在 CI 上是分钟级, 给足余量但别把作业挂死。
CHECK_TIMEOUT_SECONDS = 600
# 描述里最多展示这么多行原始输出(完整输出走附件, 不会截断).
MAX_DESCRIPTION_LINES = 40
# 复杂度门槛: 与 pyproject 里 Ruff 的 mccabe max-complexity 保持同一个数值(10 分)。
# Radon 的等级对应 1-5 / 6-10 / 11-20 / 21-30 / 31-40 / 41+, 所以"不超过 10 分"写出来
# 就是"最差只能到 B 级"; 模块级与平均复杂度不设限(Ruff 并不检查那两项)。
MAX_COMPLEXITY = 10
COMPLEXITY_RANK = "B"


@dataclass(frozen=True)
class Check:
    """一项质量门禁: 报告里显示的名称 + 要执行的命令 + 所属分组 + 归入的环境."""

    key: str
    title: str
    command: tuple[str, ...]
    group: str = "core"
    # 结论写进哪个环境: 公共检查写 QUALITY_ENVIRONMENT, 平台专属检查由 host_platform 推出。
    environment: str = QUALITY_ENVIRONMENT
    # 必须在哪个平台(``sys.platform`` 取值)上执行; None 表示与平台无关, 哪个平台都能跑。
    # 一旦设上, ``environment`` 就由它推出 —— 环境归属不再是一个可以写岔的独立常量。
    host_platform: str | None = None

    def __post_init__(self) -> None:
        """平台专属检查的环境标签由 ``host_platform`` 推出, 不留两处可以写岔的常量."""
        if self.host_platform is not None:
            object.__setattr__(
                self, "environment", PLATFORM_ENVIRONMENTS[self.host_platform]
            )


# 门禁分组: core 与 analysis 与平台无关(各跑一次), platform 按平台各跑自己那一支。
CHECK_GROUPS = ("core", "platform", "analysis")

# 顺序即报告里每条结果的写入顺序。
CHECKS = (
    Check("ruff-check", "Ruff check", ("ruff", "check", ".")),
    Check("ruff-format", "Ruff format check", ("ruff", "format", "--check", ".")),
    Check("mypy", "Mypy type check", ("mypy",)),
    # 平台专属分支: 宿主平台那次只会收窄到自己那一支(sys.platform), 所以另跑两次把
    # Windows/macOS 的专属代码路径也纳入类型检查。这两条**在各自的平台上执行**
    # (host_platform), 结论才归入那个平台的环境 —— 环境标签与执行平台是同一件事推出来的。
    Check(
        "mypy-win32",
        "Mypy type check (win32)",
        ("mypy", "--platform", "win32"),
        group="platform",
        host_platform="win32",
    ),
    Check(
        "mypy-darwin",
        "Mypy type check (darwin)",
        ("mypy", "--platform", "darwin"),
        group="platform",
        host_platform="darwin",
    ),
    Check("deptry", "Deptry 依赖卫生检查", ("deptry", "."), "analysis"),
    Check("bandit", "Bandit 安全扫描", ("bandit", "-r", "src"), "analysis"),
    Check("pip-audit", "pip-audit 依赖漏洞扫描", ("pip-audit",), "analysis"),
    Check(
        "radon",
        "Radon 复杂度报告",
        ("radon", "cc", "-s", "--min", COMPLEXITY_RANK, "src"),
        "analysis",
    ),
    Check(
        "xenon",
        "Xenon 复杂度门槛",
        (
            "xenon",
            "--max-absolute",
            COMPLEXITY_RANK,
            "--max-modules",
            "F",
            "--max-average",
            "F",
            "src",
        ),
        "analysis",
    ),
)


@dataclass(frozen=True)
class CheckOutcome:
    """一项检查的执行结果."""

    check: Check
    exit_code: int
    output: str
    missing_executable: bool = False

    @property
    def passed(self) -> bool:
        """退出码为 0 才算通过(可执行文件缺失也算未通过)."""
        return not self.missing_executable and self.exit_code == 0


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8.

    Windows runner 的控制台是 cp1252, 直接打印中文会抛 ``UnicodeEncodeError`` 把步骤打断
    (报告自检踩过一次)。``scripts`` 不是包, 所以这个助手在几个脚本里各留一份(不互相导入);
    取不到 ``reconfigure`` 的替身(例如 pytest 的 ``capsys``)时跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def platform_name() -> str:
    """返回当前平台的显示名(与 ``tests/conftest.py`` 写进 ``env`` 标签的取值一致)."""
    system = platform.system()
    return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(
        system, system or "Unknown"
    )


def command_text(check: Check) -> str:
    """检查命令的可读写法(写进描述, 方便直接复制到终端复现)."""
    return " ".join(check.command)


def run_check(check: Check) -> CheckOutcome:
    """执行一项检查并收集输出(可执行文件缺失不算崩, 记为未通过)."""
    executable = shutil.which(check.command[0])
    if executable is None:
        return CheckOutcome(
            check,
            exit_code=127,
            output=f"未找到可执行文件: {check.command[0]}(依赖是否已安装?)",
            missing_executable=True,
        )
    try:
        # 命令来自本模块的固定常量(CHECKS), 不经 shell 执行, 因此无需校验拼接。
        completed = subprocess.run(  # noqa: S603
            [executable, *check.command[1:]],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=CHECK_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return CheckOutcome(
            check, exit_code=124, output=f"检查超时(>{CHECK_TIMEOUT_SECONDS} 秒)"
        )
    return CheckOutcome(
        check,
        exit_code=completed.returncode,
        output=f"{completed.stdout}{completed.stderr}",
    )


def sanitize_output(text: str) -> str:
    r"""去掉终端控制序列, 只留可读文本(进附件与描述前调用).

    两件事: 剥掉 ANSI 颜色/光标码(否则在报告里是乱码, 见 :data:`ANSI_SEQUENCE`),
    以及把进度条用 ``\r`` 刷出的多段内容收成最后一段(只留最终状态)。
    """
    cleaned = ANSI_SEQUENCE.sub("", text).replace("\r\n", "\n")
    return "\n".join(line.rpartition("\r")[2] or line for line in cleaned.split("\n"))


def build_description(outcome: CheckOutcome) -> str:
    """构建展示在 Allure 里的可读摘要(含原始输出的开头若干行)."""
    lines = [
        "## 代码质量门禁",
        "",
        f"- 命令: `{command_text(outcome.check)}`",
        f"- 退出码: {outcome.exit_code}",
        f"- 结论: {'通过' if outcome.passed else '未通过'}",
        "",
        "## 原始输出",
        "",
    ]
    body = [
        line for line in sanitize_output(outcome.output).splitlines() if line.strip()
    ]
    if not body:
        lines.append("(没有输出)")
        return "\n".join(lines)
    lines.extend(["```text", *body[:MAX_DESCRIPTION_LINES], "```"])
    if len(body) > MAX_DESCRIPTION_LINES:
        lines.append(f"(只展示前 {MAX_DESCRIPTION_LINES} 行, 完整输出见附件)")
    return "\n".join(lines)


def write_attachment(
    results_dir: Path, outcome: CheckOutcome, result_id: str
) -> dict[str, str]:
    """把完整输出写成附件, 返回附件条目.

    写进报告的是 :func:`sanitize_output` 之后的文本: 附件是给人读的证据, 不该带
    终端控制码(控制台那份不受影响)。
    """
    target = results_dir / f"{result_id}-attachment.txt"
    target.write_text(sanitize_output(outcome.output), encoding="utf-8")
    return {
        "name": f"{outcome.check.key}.txt",
        "source": target.name,
        "type": "text/plain",
    }


def environment_note(check: Check) -> str:
    """说明这条结论归入哪个环境(公共检查 / 平台专属检查的写法不同)."""
    if check.host_platform is None:
        return (
            "- 与平台无关的公共检查: CI 只在 Linux 跑一遍, "
            f"结论归入报告的 `Common` 环境(`env={QUALITY_ENVIRONMENT}`)。\n"
        )
    return (
        f"- 平台专属检查: 只在 `{check.environment}` 上执行, 结论归入报告的 "
        f"`{check.environment}` 环境(`env={check.environment}`), 与那个平台的测试结果"
        "一起看。\n"
    )


def write_result(
    results_dir: Path, outcome: CheckOutcome, *, result_id: str, platform: str
) -> None:
    """写入一条质量门禁结果(状态跟随检查退出码).

    环境标签取自检查本身(``Check.environment``): 公共检查落进报告里显式声明的 ``Common``
    环境(见 ``allurerc.mjs`` 的 ``common`` matcher), 平台专属检查落进那个平台的环境
    (``Windows`` / ``macOS``) —— 它们只在那个平台上执行, 所以结论与环境选择器、与各平台的
    测试结果都能对上。执行主机写进描述(对平台专属检查它就是那个平台, 对公共检查是 Ubuntu)。
    """
    timestamp = time.time_ns() // 1_000_000
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"archive-management-quality-{outcome.check.key}"
            )
        ),
        "fullName": f"archive-management.quality.{outcome.check.key}",
        "name": outcome.check.title,
        "status": "passed" if outcome.passed else "failed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": "Quality"},
            {"name": "epic", "value": "工程与发布"},
            {"name": "feature", "value": "代码质量门禁"},
            {"name": "story", "value": outcome.check.title},
            {"name": "env", "value": outcome.check.environment},
            {"name": CATEGORY_LABEL, "value": CATEGORY_VALUE},
            {"name": "severity", "value": QUALITY_SEVERITY},
        ],
        "description": (
            f"{build_description(outcome)}\n\n"
            f"{environment_note(outcome.check)}"
            f"- 本次执行于 {platform}。\n"
        ),
        "attachments": [write_attachment(results_dir, outcome, result_id)],
    }
    if not outcome.passed:
        result["statusDetails"] = {
            "message": f"{outcome.check.title} 未通过(退出码 {outcome.exit_code})"
        }
    (results_dir / f"{result_id}-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def select_checks(
    group: str, host_platform: str
) -> tuple[tuple[Check, ...], tuple[Check, ...]]:
    """按分组挑出检查, 返回 ``(要跑的, 因平台不符而跳过的)``.

    平台专属检查只在它自己的平台上执行: ``--platform`` 是"检查哪一支代码", 不是"在哪台机器
    上跑", 所以把 ``mypy --platform win32`` 放到 Ubuntu 上跑出来的结论, 挂到 Windows 环境里
    就是假的归属(报告里看不出, 但环境选择器一筛就把两件事混成了一件事)。
    """
    chosen = tuple(check for check in CHECKS if group in ("all", check.group))
    applicable: list[Check] = []
    skipped: list[Check] = []
    for check in chosen:
        if check.host_platform is None or check.host_platform == host_platform:
            applicable.append(check)
        else:
            skipped.append(check)
    return tuple(applicable), tuple(skipped)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数."""
    parser = argparse.ArgumentParser(
        description="执行代码质量门禁并把结论写入 Allure 结果目录"
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIRECTORY,
        help=f"Allure 结果目录(默认 {DEFAULT_RESULTS_DIRECTORY})",
    )
    parser.add_argument(
        "--group",
        choices=("all", *CHECK_GROUPS),
        default="all",
        help=(
            "只跑某一组门禁(core: ruff/格式/宿主平台 mypy / platform: 平台专属 mypy / "
            "analysis: 依赖与安全扫描; 默认 all)"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行所选分组的门禁并写入结果; 任一项未通过时返回 1.

    退出码: 0 全部通过; 1 有检查未通过; 2 要的分组在当前平台上没有可跑的检查(平台专属
    检查被放错了平台 —— 默默跳过等于什么也没检查, 所以这里明确报错)。
    """
    ensure_utf8_output()
    args = parse_args(argv)
    results_dir: Path = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    platform = platform_name()
    host_platform = sys.platform
    failed: list[str] = []

    checks, skipped = select_checks(args.group, host_platform)
    for check in skipped:
        print(
            f"跳过 {check.title}: 平台专属检查只在 {check.environment} 上执行"
            f"(当前平台 {host_platform})"
        )
    if not checks:
        print(
            f"{args.group} 组的检查在当前平台({host_platform})上都不适用: "
            "平台专属检查必须在对应平台上跑, 请换平台或换个分组。",
            file=sys.stderr,
        )
        return 2

    for check in checks:
        outcome = run_check(check)
        print(f"=== {check.title}({command_text(check)})===")
        print(outcome.output.rstrip())
        print(
            f"--- 退出码 {outcome.exit_code}: {'通过' if outcome.passed else '未通过'}"
        )
        write_result(
            results_dir, outcome, result_id=str(uuid.uuid4()), platform=platform
        )
        if not outcome.passed:
            failed.append(check.title)

    if failed:
        print(f"质量门禁未通过: {', '.join(failed)}", file=sys.stderr)
        return 1
    print(f"质量门禁全部通过, 结果已写入 {results_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
