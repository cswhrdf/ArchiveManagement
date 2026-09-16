"""根据 pytest-cov 生成的 Cobertura XML 创建可读的 Allure 结果."""

from __future__ import annotations

import json
import platform
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

RESULTS_DIRECTORY = Path("allure-results")
COVERAGE_XML = Path("coverage.xml")
# 覆盖率摘要项的严重等级: 它不验证行为, 只把 Cobertura 报告带进 Allure;
# 不打等级会让报告出现一个只有摘要项的 no_severity 桶。
COVERAGE_SEVERITY = "trivial"


def platform_name() -> str:
    """返回覆盖率测量平台的显示名称.

    每个平台都会生成自己的覆盖率摘要. 如果不将平台写入身份信息
    (fullName / historyId / parameter), 合并报告会将它们显示为同一测试的重试,
    而不是三个平台各自独立的结果.
    """
    system = platform.system()
    return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(
        system, system or "Unknown"
    )


def percentage(value: str | None) -> str:
    """将 Cobertura 比例格式化为百分比."""
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def count_text(attributes: dict[str, str], covered: str, valid: str) -> str:
    """根据 XML 属性格式化已覆盖项数和有效项总数."""
    covered_count = attributes.get(covered, "-")
    valid_count = attributes.get(valid, "-")
    return f"{covered_count}/{valid_count}"


def markdown_cell(value: str) -> str:
    """确保值可以安全放入 Markdown 表格单元格."""
    return value.replace("|", "\\|").replace("\n", " ")


def package_line_counts(package: ET.Element) -> str:
    """统计一个 Cobertura 包中的已覆盖行数和总行数."""
    lines = package.findall("./classes/class/lines/line")
    covered = sum(int(line.attrib.get("hits", "0")) > 0 for line in lines)
    return f"{covered}/{len(lines)}"


def build_description(root: ET.Element) -> str:
    """构建展示在 Allure 中的可读覆盖率摘要."""
    attributes = root.attrib
    lines = [
        "## Coverage summary",
        "",
        "| Metric | Result |",
        "| --- | ---: |",
        f"| Line coverage | {percentage(attributes.get('line-rate'))} |",
        f"| Branch coverage | {percentage(attributes.get('branch-rate'))} |",
        f"| Lines covered | {count_text(attributes, 'lines-covered', 'lines-valid')} |",
        f"| Branches covered | {count_text(attributes, 'branches-covered', 'branches-valid')} |",
        "",
        "## Coverage by package",
        "",
        "| Package | Line coverage | Lines covered | Branch coverage |",
        "| --- | ---: | ---: | ---: |",
    ]

    packages = root.findall("./packages/package")
    for package in packages:
        package_attributes = package.attrib
        lines.append(
            "| "
            + " | ".join(
                [
                    markdown_cell(package_attributes.get("name", "-")),
                    percentage(package_attributes.get("line-rate")),
                    package_line_counts(package),
                    percentage(package_attributes.get("branch-rate")),
                ]
            )
            + " |"
        )

    if not packages:
        lines.append("| No package data | - | - | - |")
    return "\n".join(lines)


def write_result(result: dict[str, Any], result_id: str) -> None:
    """将一条覆盖率结果写入 Allure 结果目录."""
    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIRECTORY / f"{result_id}-result.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def main() -> None:
    """创建一条包含覆盖率摘要的 Allure 结果."""
    result_id = str(uuid.uuid4())
    timestamp = time.time_ns() // 1_000_000
    name = platform_name()
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"archive-management-coverage-{name}")
        ),
        "fullName": f"archive-management.coverage.{name}",
        "name": f"Coverage report · {name}",
        "status": "passed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": "Coverage"},
            {"name": "feature", "value": "Test coverage"},
            {"name": "os", "value": name},
            {"name": "severity", "value": COVERAGE_SEVERITY},
        ],
        "parameters": [{"name": "Platform", "value": name}],
        "description": "",
    }
    if not COVERAGE_XML.exists():
        result["status"] = "broken"
        result["statusDetails"] = {
            "message": f"Coverage file not found: {COVERAGE_XML}"
        }
        result["description"] = (
            "## Coverage unavailable\n\n"
            f"The coverage command did not produce `{COVERAGE_XML}`."
        )
        write_result(result, result_id)
        return

    try:
        root = ET.parse(COVERAGE_XML).getroot()  # noqa: S314
    except (ET.ParseError, OSError) as exc:
        result["status"] = "broken"
        result["statusDetails"] = {"message": f"Unable to read coverage XML: {exc}"}
        result["description"] = (
            "## Coverage unavailable\n\n"
            f"The coverage file could not be parsed: `{exc}`"
        )
        write_result(result, result_id)
        return

    result["description"] = build_description(root)
    write_result(result, result_id)


if __name__ == "__main__":
    main()
