"""把性能与安全测试结果汇总进 Allure 报告.

汇总要做三件事:

1. 写入环境信息(操作系统、Python、提交 SHA、测试类别、覆盖率门槛), 让每份
   报告都能回答"这次结果是在哪个平台、哪个提交上跑出来的";
2. 把 ``performance-results.json`` / ``security-results.json`` 转成 Allure 里
   可检索的测试项: 指标表写进描述, 原始 JSON/CSV 作为附件, 保证结论可下载;
3. 缺少某类结果时不报错(例如只跑了单元测试), 只是跳过该类并写进环境信息。

用法(CI 汇总 job): ``uv run python scripts/create_allure_summary.py``
"""

from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

RESULTS_DIRECTORY = Path("allure-results")
PERFORMANCE_JSON = Path("performance-results.json")
PERFORMANCE_CSV = Path("performance-results.csv")
SECURITY_JSON = Path("security-results.json")

ENVIRONMENT_FILENAME = "environment.properties"
# 覆盖率门槛与 pytest 配置保持一致(低于该值 pytest 已经失败, 这里只作记录).
COVERAGE_THRESHOLD = "80"


def read_json(path: Path) -> dict[str, Any] | None:
    """读取 JSON 结果文件; 不存在或无法解析时返回 None."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"跳过无法解析的结果文件 {path}: {exc}", file=sys.stderr)
        return None
    return payload if isinstance(payload, dict) else None


def git_commit() -> str:
    """返回提交 SHA: 优先用 CI 提供的环境变量, 否则直接读 .git/HEAD."""
    from_env = os.environ.get("GITHUB_SHA", "").strip()
    if from_env:
        return from_env
    root = Path(__file__).resolve().parent.parent
    try:
        head = (root / ".git" / "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    if head.startswith("ref: "):
        try:
            return (
                (root / ".git" / head.removeprefix("ref: "))
                .read_text(encoding="utf-8")
                .strip()
            )
        except OSError:
            return "unknown"
    return head


def environment_lines(
    performance: dict[str, Any] | None, security: dict[str, Any] | None
) -> list[tuple[str, str]]:
    """组装环境信息(平台、Python、提交、测试类别与门槛)."""
    payload = performance if performance is not None else (security or {})
    info = payload.get("environment", {})
    return [
        ("os", str(info.get("os", "unknown"))),
        (
            "os.family",
            str(info.get("os_family", os.environ.get("RUNNER_OS", "unknown"))),
        ),
        ("python.version", str(info.get("python", "unknown"))),
        ("python.implementation", str(info.get("python_implementation", "unknown"))),
        ("git.commit", git_commit()),
        ("git.branch", os.environ.get("GITHUB_REF_NAME", "local")),
        ("ci.run_id", os.environ.get("GITHUB_RUN_ID", "local")),
        ("test.category.unit_integration", "tests/unit + tests/integration"),
        (
            "test.category.performance",
            "已执行" if performance is not None else "未执行(不单独执行性能测试的运行)",
        ),
        (
            "test.category.security",
            "已执行" if security is not None else "未执行(不单独执行安全测试的运行)",
        ),
        ("coverage.fail_under", COVERAGE_THRESHOLD),
    ]


def _cell(value: object) -> str:
    """把取值安全地放进 Markdown 表格单元格."""
    return str(value).replace("|", "\\|").replace("\n", " ")


def performance_table(measurements: list[dict[str, Any]]) -> str:
    """把性能测量渲染成 Markdown 表(名称/规模/指标/取值/阈值/结论)."""
    lines = [
        "## 性能基准",
        "",
        "| 基准 | 数据规模 | 指标 | 取值 | 阈值 | 结论 |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            [
                _cell(item.get("name", "-")),
                _cell(item.get("scale", "-")),
                _cell(item.get("metric", "-")),
                f"{_cell(item.get('value', '-'))} {_cell(item.get('unit', ''))}",
                f"{'≤' if item.get('comparison') == 'max' else '≥'} "
                f"{_cell(item.get('threshold', '-'))}",
                "通过" if item.get("passed") else "未通过",
            ]
        )
        + " |"
        for item in measurements
    )
    return "\n".join(lines)


def security_table(findings: list[dict[str, Any]]) -> str:
    """把安全结论渲染成 Markdown 表(场景/输入/期望/实际/是否拦截)."""
    lines = [
        "## 安全测试结论",
        "",
        "| 类别 | 场景 | 输入 | 期望拦截行为 | 实际结果 | 拦截 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    lines.extend(
        "| "
        + " | ".join(
            [
                _cell(item.get("category", "-")),
                _cell(item.get("scenario", "-")),
                f"`{_cell(item.get('input_summary', '-'))}`",
                _cell(item.get("expected", "-")),
                _cell(item.get("actual", "-")),
                "是" if item.get("blocked") else "否",
            ]
        )
        + " |"
        for item in findings
    )
    return "\n".join(lines)


def write_attachment(
    results_dir: Path, source: Path, result_id: str
) -> dict[str, str] | None:
    """把结果文件复制成 Allure 附件并返回附件条目(文件缺失时返回 None)."""
    if not source.is_file():
        return None
    suffix = source.suffix.lstrip(".") or "txt"
    target = results_dir / f"{result_id}-attachment.{suffix}"
    target.write_bytes(source.read_bytes())
    media_type = "application/json" if suffix == "json" else "text/csv"
    return {"name": source.name, "source": target.name, "type": media_type}


def write_result(
    results_dir: Path,
    *,
    result_id: str,
    category: str,
    title: str,
    description: str,
    attachments: list[dict[str, str]],
) -> None:
    """写入一条 passed 状态的 Allure 结果(承载该类测试的汇总信息)."""
    timestamp = time.time_ns() // 1_000_000
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"archive-management-{category}")
        ),
        "fullName": f"archive-management.{category}",
        "name": title,
        "status": "passed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": "Test report"},
            {"name": "feature", "value": title},
            {"name": "epic", "value": "工程与发布"},
            {"name": "story", "value": title},
            {"name": "layer", "value": category},
            {"name": "testCategory", "value": category},
        ],
        "description": description,
        "attachments": attachments,
    }
    (results_dir / f"{result_id}-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def main() -> int:
    """把性能/安全结果与环境信息写入 allure-results."""
    results_dir = RESULTS_DIRECTORY
    if not results_dir.is_dir():
        print(f"Allure 结果目录不存在: {results_dir}", file=sys.stderr)
        return 1

    performance = read_json(PERFORMANCE_JSON)
    security = read_json(SECURITY_JSON)

    lines = environment_lines(performance, security)
    (results_dir / ENVIRONMENT_FILENAME).write_text(
        "".join(f"{key}={value}\n" for key, value in lines), encoding="utf-8"
    )

    if performance is not None:
        measurements = list(performance.get("measurements", []))
        result_id = str(uuid.uuid4())
        attachments = [
            item
            for item in (
                write_attachment(results_dir, PERFORMANCE_JSON, result_id),
                write_attachment(results_dir, PERFORMANCE_CSV, result_id),
            )
            if item is not None
        ]
        write_result(
            results_dir,
            result_id=result_id,
            category="performance",
            title="Performance baseline",
            description=performance_table(measurements),
            attachments=attachments,
        )
        print(f"性能基准已写入 Allure: {len(measurements)} 条测量")

    if security is not None:
        findings = list(security.get("findings", []))
        result_id = str(uuid.uuid4())
        attachments = [
            item
            for item in (write_attachment(results_dir, SECURITY_JSON, result_id),)
            if item is not None
        ]
        write_result(
            results_dir,
            result_id=result_id,
            category="security",
            title="Security findings",
            description=security_table(findings),
            attachments=attachments,
        )
        print(f"安全结论已写入 Allure: {len(findings)} 条结论")

    missing = [
        str(path) for path in (PERFORMANCE_JSON, SECURITY_JSON) if not path.is_file()
    ]
    if missing:
        print(f"未找到以下结果文件, 已跳过: {', '.join(missing)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
