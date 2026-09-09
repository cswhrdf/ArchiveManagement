"""Create a readable Allure result from pytest-cov's Cobertura XML."""

from __future__ import annotations

import json
import sys
import time
import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

RESULTS_DIRECTORY = Path("allure-results")
COVERAGE_XML = Path("coverage.xml")


def percentage(value: str | None) -> str:
    """Format a Cobertura ratio as a percentage."""
    if value is None:
        return "-"
    return f"{float(value) * 100:.2f}%"


def count_text(attributes: dict[str, str], covered: str, valid: str) -> str:
    """Format covered and valid item counts from XML attributes."""
    covered_count = attributes.get(covered, "-")
    valid_count = attributes.get(valid, "-")
    return f"{covered_count}/{valid_count}"


def markdown_cell(value: str) -> str:
    """Keep values safe inside a Markdown table cell."""
    return value.replace("|", "\\|").replace("\n", " ")


def package_line_counts(package: ET.Element) -> str:
    """Count covered and total lines below one Cobertura package."""
    lines = package.findall("./classes/class/lines/line")
    covered = sum(int(line.attrib.get("hits", "0")) > 0 for line in lines)
    return f"{covered}/{len(lines)}"


def build_description(root: ET.Element) -> str:
    """Build the human-readable coverage summary shown in Allure."""
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


def main() -> None:
    """Create one passed Allure result containing the coverage summary."""
    if not COVERAGE_XML.exists():
        print(f"Coverage file not found: {COVERAGE_XML}", file=sys.stderr)
        raise SystemExit(1)

    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    root = ET.parse(COVERAGE_XML).getroot()  # noqa: S314
    result_id = str(uuid.uuid4())
    timestamp = time.time_ns() // 1_000_000
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(uuid.uuid5(uuid.NAMESPACE_URL, "archive-management-coverage")),
        "fullName": "archive-management.coverage",
        "name": "Coverage report",
        "status": "passed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": "Coverage"},
            {"name": "feature", "value": "Test coverage"},
        ],
        "description": build_description(root),
    }
    result_path = RESULTS_DIRECTORY / f"{result_id}-result.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
