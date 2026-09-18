"""把代码质量门禁(ruff check / ruff format / mypy)的结论写进 Allure.

为什么单独一个脚本: 这三项门禁的结论原来只出现在 CI 日志里, 汇总报告里看不到 ——
报告要能回答"这次提交在质量门禁上过了吗", 结论就得变成报告里的一条结果。

做法与 :mod:`create_allure_coverage` 一致: 执行检查命令, 把**原始输出**作为附件带进结果,
并写 ``env`` 标签 —— 仓库根的 ``allurerc.mjs`` 用它把结果归到当前平台的环境。CI 里只有
Ubuntu 那份会作为 artifact 上传(质量结论与平台无关), 所以汇总报告里只有 Linux 一份。

任一项检查失败时脚本以非 0 退出(质量门禁照常拦住提交), 但**先把结论写进结果目录**,
于是报告里能看到"哪一项没过 + 完整输出"。

用法:

- CI(quality job): ``uv run python scripts/create_allure_quality.py --results-dir allure-results-quality``;
- 本地: ``uv run python scripts/create_allure_quality.py``(默认写进 ``allure-results``)。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import platform
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
# 单项检查的超时: mypy 在 CI 上是分钟级, 给足余量但别把作业挂死。
CHECK_TIMEOUT_SECONDS = 600
# 描述里最多展示这么多行原始输出(完整输出走附件, 不会截断).
MAX_DESCRIPTION_LINES = 40


@dataclass(frozen=True)
class Check:
    """一项质量门禁: 报告里显示的名称 + 要执行的命令."""

    key: str
    title: str
    command: tuple[str, ...]


# 与 CI 里原来的三条命令一一对应(顺序即报告里的顺序).
CHECKS = (
    Check("ruff-check", "Ruff check", ("ruff", "check", ".")),
    Check("ruff-format", "Ruff format check", ("ruff", "format", "--check", ".")),
    Check("mypy", "Mypy type check", ("mypy",)),
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
    body = [line for line in outcome.output.splitlines() if line.strip()]
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
    """把完整输出写成附件, 返回附件条目."""
    target = results_dir / f"{result_id}-attachment.txt"
    target.write_text(outcome.output, encoding="utf-8")
    return {
        "name": f"{outcome.check.key}.txt",
        "source": target.name,
        "type": "text/plain",
    }


def write_result(
    results_dir: Path, outcome: CheckOutcome, *, result_id: str, platform: str
) -> None:
    """写入一条质量门禁结果(状态跟随检查退出码).

    身份**不带平台**: 平台由 ``env`` 标签(配合仓库根的 ``allurerc.mjs``)变成 Allure 的环境,
    与用例结果、覆盖率/性能/安全汇总项同一写法; ``Platform`` 参数与 ``os`` 标签是兜底
    (生成端没读到报告配置时, 环境会静默退回 ``default``)。
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
            {"name": "os", "value": platform},
            {"name": "env", "value": platform},
            {"name": "testCategory", "value": "quality"},
            {"name": "severity", "value": QUALITY_SEVERITY},
        ],
        "parameters": [{"name": "Platform", "value": platform}],
        "description": build_description(outcome),
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
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """执行全部门禁并写入结果; 任一项未通过时返回 1."""
    ensure_utf8_output()
    args = parse_args(argv)
    results_dir: Path = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    platform = platform_name()
    failed: list[str] = []

    for check in CHECKS:
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
