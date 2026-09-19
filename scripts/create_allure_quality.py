"""把代码质量门禁的结论写进 Allure.

为什么单独一个脚本: 这些门禁的结论原来只出现在 CI 日志里, 汇总报告里看不到 ——
报告要能回答"这次提交在质量门禁上过了吗", 结论就得变成报告里的一条结果。

门禁分两组(用 ``--group`` 选):

- ``core``: Ruff 检查 / Ruff 格式 / mypy 类型(宿主平台 加 ``--platform win32`` 与
  ``--platform darwin``, 共三次: 宿主那次只收窄自己那一支, 另外两次才盖住 Windows /
  macOS 专属分支) —— 公共检查(与路径分隔符、显示、字体都无关), CI 只在 Ubuntu 跑一遍
  (CI 的 quality job);
- ``analysis``: deptry 依赖卫生 / Bandit 安全扫描 / pip-audit 依赖漏洞 / Radon 复杂度报告 /
  Xenon 复杂度门槛 —— 与平台无关, 只在 Ubuntu 跑一遍(CI 的 analysis job)。

复杂度门槛取 :data:`MAX_COMPLEXITY` = 10, 与 pyproject 里 Ruff 的
``[tool.ruff.lint.mccabe] max-complexity`` **同一个数值**; 注意 Radon 会把 ``with``/
``assert``/布尔运算也算作分支, 所以同一段代码在 Radon 下会比 Ruff 的 C901 高 2~5 分。

做法与 :mod:`create_allure_coverage` 一致: 执行检查命令, 把**原始输出**作为附件带进结果;
区别在于环境标签写死 ``env=common`` —— 这些检查是公共内容, 与平台无关, 于是它们落进
仓库根 ``allurerc.mjs`` 里**显式声明**的 ``Common`` 环境(matcher 匹配 ``env=common``),
既不是某个平台的环境(那会误导成"Linux 上的质量检查"), 也不是隐式的 ``default``。
执行主机只写进描述作为排障线索; 面向人的总结在报告首页的运行总账里(见
:mod:`create_allure_summary`)。

任一项检查失败时脚本以非 0 退出(质量门禁照常拦住提交), 但**先把结论写进结果目录**,
于是报告里能看到"哪一项没过 + 完整输出"。

用法:

- CI(quality job): ``--group core --results-dir allure-results-quality``;
- CI(analysis job): ``--group analysis --results-dir allure-results-analysis``;
- 本地: 不带参数(跑全部, 写进 ``allure-results``)。
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
    """一项质量门禁: 报告里显示的名称 + 要执行的命令 + 所属分组."""

    key: str
    title: str
    command: tuple[str, ...]
    group: str = "core"


# 门禁分组: core 与平台相关(三平台各跑一遍), analysis 与平台无关(只跑一次)。
CHECK_GROUPS = ("core", "analysis")

# 顺序即报告里每条结果的写入顺序。
CHECKS = (
    Check("ruff-check", "Ruff check", ("ruff", "check", ".")),
    Check("ruff-format", "Ruff format check", ("ruff", "format", "--check", ".")),
    Check("mypy", "Mypy type check", ("mypy",)),
    # 平台专属分支: 宿主平台那次只会收窄到自己那一支(sys.platform), 所以另跑两次把
    # Windows/macOS 的专属代码路径也纳入类型检查(CI 在 Ubuntu 上跑, 结论一样成立)。
    Check("mypy-win32", "Mypy type check (win32)", ("mypy", "--platform", "win32")),
    Check("mypy-darwin", "Mypy type check (darwin)", ("mypy", "--platform", "darwin")),
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


def write_result(
    results_dir: Path, outcome: CheckOutcome, *, result_id: str, platform: str
) -> None:
    """写入一条质量门禁结果(状态跟随检查退出码).

    结论项带 ``env=common``: 质量检查是公共内容, 与平台无关, 于是它落进报告里显式声明
    的 ``Common`` 环境(见 ``allurerc.mjs`` 的 ``common`` matcher), 而不是某个平台的环境
    (那会误导成"Linux 环境里的检查")。执行主机只写进描述, 作为排障线索。
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
            {"name": "env", "value": QUALITY_ENVIRONMENT},
            {"name": "testCategory", "value": "quality"},
            {"name": "severity", "value": QUALITY_SEVERITY},
        ],
        "description": (
            f"{build_description(outcome)}\n\n"
            "- 与平台无关的公共检查: CI 只在 Linux 跑一遍, "
            f"结论归入报告的 `Common` 环境(`env={QUALITY_ENVIRONMENT}`)。\n"
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
        help="只跑某一组门禁(core: ruff/格式/mypy / analysis: 依赖与安全扫描; 默认 all)",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行所选分组的门禁并写入结果; 任一项未通过时返回 1."""
    ensure_utf8_output()
    args = parse_args(argv)
    results_dir: Path = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    platform = platform_name()
    failed: list[str] = []

    checks = [check for check in CHECKS if args.group in ("all", check.group)]
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
