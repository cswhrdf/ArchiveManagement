r"""把产物冒烟的逐条判据写进 Allure.

为什么单独一个脚本: 冒烟的结论原本只有一个退出码 —— 打包门禁红了, 但"哪一条判据没过、
产物各自什么表现"只在 CI 日志里, 汇总报告看不到。``scripts/smoke_packaged.py`` 用
``--report`` 把逐条判据落成 ``smoke-report.json``, 这一步把它转成报告里的结果项(每条判据
一条, 发布产物那一组单文件 exe), 于是"这次提交编出来的包能不能起"在
报告里是一条可检索、可按环境筛的结论。

与 :mod:`create_allure_quality` 同一套做法(执行方在 ``smoke_packaged.py``, 这里只做转换):

- ``env`` 标签写**平台显示名**(取自报告 JSON —— smoke_packaged.py 按当前平台落
  ``Windows`` / ``Linux``): 冒烟结论跟着那个平台的用例走, 落进仓库根 ``allurerc.mjs`` 里
  声明的对应环境 —— 写错或漏写会被 ``scripts/verify_allure_report.py`` 与清单核对判成
  "结果不完整"(2026-10-14 的坑: 汇总项落进隐式 ``default`` 环境, 按平台对数时找不到);
- 身份(``fullName``)由"产物标识 + 判据标识"拼成(``archive-management.smoke.<产物>.<判据>``),
  两个取值都来自 ``smoke_packaged.py`` 落的 JSON —— 改判据的显示名不断历史, 改标识会断
  (趋势里变成新条目, 旧的接不上);
- 失败也照写(``failed`` 状态 + 原因进 ``statusDetails``): 报告要看得见哪条没过, 所以上传
  步挂 ``if: always()`` 让失败详情也进报告 —— 门禁本身的成败仍由 ``smoke_packaged.py``
  的退出码与打包门禁作业(``packaging-gate``)负责, 这里不重复判。

结果目录名按平台各一份(``allure-results-smoke-windows`` / ``allure-results-smoke-linux``):
匹配汇总作业的 ``pattern: allure-results-*``(一份 pattern 自动收走), 但**不**匹配每平台
报告作业的 ``allure-results-<os>-*`` —— 那边多收一份只会把冒烟结论错并进单平台的报告。
平台段跟着调用方(release-build.yml 的 build / build-linux 作业)走, 平台清单收在
``scripts/allure_catalog.py`` 一处, 不要散在工作流里。

用法(CI, ``release-build.yml`` 的 build / build-linux 作业, 冒烟步之后; 目录名是两平台各传各的)::

    uv run python scripts/create_allure_smoke.py \
        --report smoke-report.json --results-dir allure-results-smoke-windows
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

DEFAULT_REPORT = Path("smoke-report.json")
DEFAULT_RESULTS_DIRECTORY = Path("allure-results-smoke-windows")
#: 结论项身份的前缀: 清单(``scripts/allure_catalog.py``)按它反查"这一族谁在产出"。
IDENTITY_PREFIX = "archive-management.smoke."
#: 冒烟是打包门禁本身: 它红了发布就该停, 但不涉及数据安全 —— 按仓库的等级口径给 critical
#: (blocker 留给数据丢失/损坏, 见 docs/testing.md 第 1 节)。
SMOKE_SEVERITY = "critical"
#: 结论项的类别标签: 仓库根 allurerc.mjs 的"工程门禁:产物冒烟未通过"分类规则按这一对取值
#: 挑选冒烟结果, 运行总账(create_allure_summary.py)也按这个前缀收集它们 —— 三处必须一致。
CATEGORY_LABEL = "testCategory"
CATEGORY_VALUE = "smoke"
SUITE_NAME = "Smoke"
FEATURE_NAME = "产物冒烟"
EPIC_NAME = "工程与发布"


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8(Windows runner 控制台是 cp1252, 打中文会打断步骤)."""
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def read_report(path: Path) -> dict[str, Any] | None:
    """读取冒烟结论 JSON; 不存在或无法解析时返回 None(调用方据此"什么都不写")."""
    if not path.is_file():
        print(f"::warning::冒烟结论文件不存在: {path}(冒烟步没跑, 或没传 --report)。")
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"::warning::冒烟结论文件无法解析: {path}: {exc}", file=sys.stderr)
        return None
    return payload if isinstance(payload, dict) else None


def build_description(platform: str, artifact: str, check: dict[str, Any]) -> str:
    """构建展示在 Allure 里的可读摘要(判据 + 结论 + 失败原因)."""
    name = str(check.get("name") or check.get("key") or "-")
    passed = bool(check.get("passed"))
    detail = str(check.get("detail") or "")
    lines = [
        "## 产物冒烟",
        "",
        f"- 产物: `{artifact}`",
        f"- 平台: {platform}",
        f"- 判据: {name}",
        f"- 结论: {'通过' if passed else '未通过'}",
    ]
    if detail:
        lines += ["", "```text", detail, "```"]
    lines += [
        "",
        "判据清单与限时常量见 `scripts/smoke_packaged.py`(这一步只做转换, 不执行判据)。",
    ]
    return "\n".join(lines)


def write_attachment(results_dir: Path, report: Path, result_id: str) -> dict[str, str]:
    """把整份冒烟结论 JSON 复制成附件(判据的完整上下文都在它里面)."""
    target = results_dir / f"{result_id}-attachment.json"
    target.write_bytes(report.read_bytes())
    return {"name": report.name, "source": target.name, "type": "application/json"}


def write_result(
    results_dir: Path,
    *,
    platform: str,
    artifact: str,
    check: dict[str, Any],
    result_id: str,
    report: Path,
) -> None:
    """写入一条冒烟判据的结果(状态跟随判据结论, 原始 JSON 整份挂附件)."""
    key = str(check.get("key") or "unknown")
    name = str(check.get("name") or key)
    passed = bool(check.get("passed"))
    detail = str(check.get("detail") or "")
    timestamp = time.time_ns() // 1_000_000
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"archive-management-smoke-{artifact}-{key}")
        ),
        "fullName": f"{IDENTITY_PREFIX}{artifact}.{key}",
        "name": f"{name}({artifact})",
        "status": "passed" if passed else "failed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": SUITE_NAME},
            {"name": "epic", "value": EPIC_NAME},
            {"name": "feature", "value": FEATURE_NAME},
            {"name": "story", "value": name},
            {"name": "env", "value": platform},
            {"name": CATEGORY_LABEL, "value": CATEGORY_VALUE},
            {"name": "severity", "value": SMOKE_SEVERITY},
        ],
        "description": build_description(platform, artifact, check),
        "attachments": [write_attachment(results_dir, report, result_id)],
    }
    if not passed:
        result["statusDetails"] = {"message": detail or f"{name} 未通过"}
    (results_dir / f"{result_id}-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数."""
    parser = argparse.ArgumentParser(
        description="把产物冒烟的逐条判据转成 Allure 结果项"
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
        help=f"smoke_packaged.py 落的冒烟结论 JSON(默认 {DEFAULT_REPORT})",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIRECTORY,
        help=f"Allure 结果目录(默认 {DEFAULT_RESULTS_DIRECTORY})",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """把冒烟结论 JSON 转成结果项; 结论文件不在时不写任何东西(退出码仍为 0).

    退出码恒 0: 这一步不是门禁(``smoke_packaged.py`` 的退出码才是), 而且"冒烟没跑"几乎
    总是**更早的失败**(构建红在冒烟之前)的结果 —— 再叠一个红只会把真正的失败原因刷下去。
    "该有而没有"由汇总作业的运行总账按**条件期望**点名(``--expect-smoke-platforms``),
    这才是那件事的正主。
    """
    ensure_utf8_output()
    args = parse_args(argv)
    report = read_report(args.report)
    if report is None:
        return 0
    platform = str(report.get("platform") or "").strip()
    if not platform:
        print(
            "::warning::冒烟结论里没有 platform 字段 —— 环境归属不明, 不写结果项。",
            file=sys.stderr,
        )
        return 0
    results_dir = args.results_dir
    results_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    artifacts = report.get("artifacts")
    for artifact in artifacts if isinstance(artifacts, list) else []:
        if not isinstance(artifact, dict):
            continue
        label = str(artifact.get("label") or "unknown")
        checks = artifact.get("checks")
        for check in checks if isinstance(checks, list) else []:
            if not isinstance(check, dict):
                continue
            write_result(
                results_dir,
                platform=platform,
                artifact=label,
                check=check,
                result_id=str(uuid.uuid4()),
                report=args.report,
            )
            written += 1
    if not written:
        print("::warning::冒烟结论里一条判据都没有 —— 不写结果项。", file=sys.stderr)
        return 0
    print(f"产物冒烟结论已写入 {results_dir}: {written} 条判据(平台 {platform})。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
