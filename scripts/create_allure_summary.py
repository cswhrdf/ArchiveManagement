"""把性能与安全测试结果汇总进 Allure 报告.

汇总要做三件事:

1. 写入环境信息(操作系统、Python、提交 SHA、测试类别、覆盖率门槛、本次涉及的
   平台), 让每份报告都能回答"这次结果是在哪个平台、哪个提交上跑出来的";
2. 把 ``performance-results.json`` / ``security-results.json`` 转成 Allure 里
   可检索的测试项: 指标表写进描述, 原始 JSON/CSV 作为附件, 保证结论可下载;
   每个平台各写一条(名称/参数/标签都带平台), 三个平台的安全结论不会互相覆盖;
3. 缺少某类结果时不报错(例如只跑了单元测试), 只是跳过该类并写进环境信息。

用法(CI 汇总 job): ``uv run python scripts/create_allure_summary.py``
"""

from __future__ import annotations

import contextlib
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
# CI 把三个平台的同名结果分目录下载(merge-multiple: false), 这里逐个收集,
# 否则后写的那份会盖掉先写的, 汇总报告里就只剩一个平台的安全结论。
SECURITY_FINDINGS_DIRECTORY = Path("security-findings")
RESULT_FILE_SUFFIX = "-result.json"

ENVIRONMENT_FILENAME = "environment.properties"
# 性能/安全汇总项的严重等级: 它们不验证行为, 只承载原始结论与附件, 但不打等级
# 会让报告里多出一个 no_severity 桶(和覆盖摘要项保持一致)。
SUMMARY_SEVERITY = "trivial"
# 覆盖率门槛与 pytest 配置保持一致(低于该值 pytest 已经失败, 这里只作记录).
COVERAGE_THRESHOLD = "80"


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8(Windows 控制台默认 cp1252, 打印中文会崩).

    ``scripts/verify_allure_report.py`` 里有同样的一份: 两个脚本都是独立入口,
    不互相导入(scripts 不是包)。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


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


def security_files() -> list[Path]:
    """列出所有待汇总的安全结果文件(单文件 + 各平台子目录), 按路径去重排序."""
    candidates = [
        SECURITY_JSON,
        *SECURITY_FINDINGS_DIRECTORY.glob(f"*/{SECURITY_JSON.name}"),
    ]
    return sorted({path for path in candidates if path.is_file()})


def platform_of(payload: dict[str, Any] | None) -> str:
    """从结果文件的环境信息里取平台展示名, 与 pytest 结果上的 ``os`` 标签一致.

    结果文件里 ``os`` 是 ``platform.platform()`` 的长串(如 ``Windows-11-...``),
    ``os_family`` 才是归一化的平台族, 因此优先用它, 再映射成展示名。
    """
    info = (payload or {}).get("environment", {})
    raw = str(info.get("os_family") or "").lower()
    if raw.startswith("win"):
        return "Windows"
    if raw.startswith(("darwin", "mac")):
        return "macOS"
    if raw.startswith("linux"):
        return "Linux"
    return str(info.get("os") or "unknown")


def tested_platforms(results_dir: Path) -> list[str]:
    """扫描合并进来的结果, 列出本次运行涉及的所有平台.

    合并报告里环境信息只能写一份, 单看 ``os`` 会误以为全部结果都来自汇总 job 的
    机器; 各平台的 pytest 结果带着平台参数与 ``os`` 标签, 这里把它们汇总出来。
    """
    found: set[str] = set()
    for path in results_dir.rglob(f"*{RESULT_FILE_SUFFIX}"):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        labels = payload.get("labels", []) if isinstance(payload, dict) else []
        found.update(
            str(label.get("value"))
            for label in labels
            if isinstance(label, dict) and label.get("name") == "os"
        )
    return sorted(found)


def environment_lines(
    performance: dict[str, Any] | None,
    security: dict[str, Any] | None,
    platforms: list[str],
) -> list[tuple[str, str]]:
    """组装环境信息(平台、Python、提交、测试类别与门槛)."""
    payload = performance if performance is not None else (security or {})
    info = payload.get("environment", {})
    return [
        ("os", str(info.get("os", "unknown"))),
        # 展示名与用例上的平台参数/os 标签保持一致(Windows/Linux/macOS).
        ("os.family", platform_of(payload)),
        (
            "tested.platforms",
            ", ".join(platforms) if platforms else "unknown",
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
    platform: str,
) -> None:
    """写入一条 passed 状态的 Allure 结果(承载该类测试的汇总信息).

    全名与 historyId 都带平台: 三个平台的结论在报告里各占一行, 也能各自与
    历史运行对上(同平台的趋势连得上, 不会被当成彼此的"重试")。

    严重等级固定为 ``trivial``: 汇总项本身不验证任何行为, 只是把原始结论与附件
    带进报告; 不打等级的话报告里会多出一个 no_severity 桶。
    """
    timestamp = time.time_ns() // 1_000_000
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"archive-management-{category}-{platform}")
        ),
        "fullName": f"archive-management.{category}.{platform}",
        "name": title,
        "status": "passed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": "Test report"},
            {"name": "os", "value": platform},
            {"name": "feature", "value": title},
            {"name": "epic", "value": "工程与发布"},
            {"name": "story", "value": title},
            {"name": "layer", "value": category},
            {"name": "testCategory", "value": category},
            {"name": "severity", "value": SUMMARY_SEVERITY},
        ],
        "parameters": [{"name": "平台", "value": platform}],
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
    security_payloads = [
        payload for payload in (read_json(path) for path in security_files()) if payload
    ]

    platforms = sorted(
        {
            *tested_platforms(results_dir),
            *([platform_of(performance)] if performance is not None else []),
            *(platform_of(payload) for payload in security_payloads),
        }
        - {"unknown"}
    )
    lines = environment_lines(
        performance, security_payloads[0] if security_payloads else None, platforms
    )
    (results_dir / ENVIRONMENT_FILENAME).write_text(
        "".join(f"{key}={value}\n" for key, value in lines), encoding="utf-8"
    )

    if performance is not None:
        measurements = list(performance.get("measurements", []))
        platform = platform_of(performance)
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
            title=f"Performance baseline · {platform}",
            description=performance_table(measurements),
            attachments=attachments,
            platform=platform,
        )
        print(f"性能基准已写入 Allure: {len(measurements)} 条测量({platform})")

    for source in security_files():
        payload = read_json(source)
        if payload is None:
            continue
        findings = list(payload.get("findings", []))
        platform = platform_of(payload)
        result_id = str(uuid.uuid4())
        attachments = [
            item
            for item in (write_attachment(results_dir, source, result_id),)
            if item is not None
        ]
        write_result(
            results_dir,
            result_id=result_id,
            category="security",
            title=f"Security findings · {platform}",
            description=security_table(findings),
            attachments=attachments,
            platform=platform,
        )
        print(
            f"安全结论已写入 Allure: {len(findings)} 条结论({platform}, 来源 {source})"
        )

    if performance is None:
        print(f"未找到性能结果文件, 已跳过: {PERFORMANCE_JSON}")
    if not security_payloads:
        print(f"未找到安全结果文件, 已跳过: {SECURITY_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
