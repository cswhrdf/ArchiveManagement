"""根据 pytest-cov 生成的 Cobertura XML 创建可读的 Allure 结果.

除了把覆盖率摘要写进描述, **原始覆盖率报告会作为附件一并放进 Allure 结果目录**
(默认是 ``coverage.xml``) —— 与性能/安全汇总项(``scripts/create_allure_summary.py``)
同一做法: 报告里能直接下载原始数据, 而不只是看到一张渲染过的表。HTML 报告是整站,
仍旧由 CI 的 ``coverage-<os>`` artifact 提供。

结论归入哪个平台由 ``--platform`` 决定(不传则按当前主机判断): CI 的报告作业固定跑在
Ubuntu 上, 却要合并**各个平台**的分片数据, 所以必须显式传 —— 否则每个平台的覆盖率结论
都会被标成 Linux。
"""

from __future__ import annotations

import argparse
import json
import platform
import time
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from pathlib import Path
from typing import Any

RESULTS_DIRECTORY = Path("allure-results")
COVERAGE_XML = Path("coverage.xml")
# 作为附件带进报告的原始覆盖率报告: 存在哪个带哪个。Cobertura XML 是主数据源,
# 另外两个是可选的附加输出 —— 以后在 CI 里加 `coverage.json`、或把终端的
# `--cov-report=term-missing` 输出重定向成文件时, 不用改代码就会一并带上。
RAW_REPORT_FILES = (COVERAGE_XML, Path("coverage-report.txt"), Path("coverage.json"))
# 附件媒体类型(Allure 用它决定预览方式); 未登记的后缀按纯文本处理。
ATTACHMENT_MEDIA_TYPES = {
    "json": "application/json",
    "xml": "application/xml",
    "txt": "text/plain",
    "csv": "text/csv",
    "html": "text/html",
}
# 覆盖率摘要项的严重等级: 它不验证行为, 只把 Cobertura 报告带进 Allure;
# 不打等级会让报告出现一个只有摘要项的 no_severity 桶。
COVERAGE_SEVERITY = "trivial"


def platform_name() -> str:
    """返回**当前主机**的显示名称(Windows/Linux/macOS).

    这个名称写进 ``env`` 与 ``os`` 标签、以及 ``Platform`` 参数:

    - ``env`` 标签 + 仓库根的 ``allurerc.mjs`` 把它变成 Allure 的"环境", 每个平台的
      覆盖率摘要各归各自的环境(报告顶部可切换, 用例详情页的环境分页能逐个对照),
      因此**标题里不再拼平台名**;
    - ``平台`` 参数与 ``os`` 标签是兼底: 生成报告时没读到报告配置的话, 环境会静默退回
      ``default``, 那时至少还能从参数/标签看出结果来自哪台机器。
    """
    system = platform.system()
    return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(
        system, system or "Unknown"
    )


def resolve_platform(requested: str | None) -> str:
    """决定这条覆盖率结论算哪个平台的.

    默认按当前主机判断(本地手动跑一次就够); CI 的报告作业用 ``--platform`` 显式指定 ——
    它固定跑在 Ubuntu 上, 却要合并**各个平台**的分片数据: 用宿主平台的名字会把每个平台的
    覆盖结论都标成 Linux(与平台专属检查同一个坑: 结论的归属要由数据决定, 不能由执行位置
    决定)。
    """
    return requested or platform_name()


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数."""
    parser = argparse.ArgumentParser(
        description="把覆盖率报告写成 Allure 结果(含原始报告附件)"
    )
    parser.add_argument(
        "--platform",
        default=None,
        help=(
            "这条结论属于哪个平台(Windows/Linux/macOS); 不传则按当前主机判断。"
            "报告作业跑在 Ubuntu 上但合并的是别的平台的数据, 必须显式传。"
        ),
    )
    return parser.parse_args(argv)


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
    """构建展示在 Allure 中的可读覆盖率摘要(中文, 与仓库其它文档口径一致)."""
    attributes = root.attrib
    lines = [
        "## 覆盖率摘要",
        "",
        "| 指标 | 结果 |",
        "| --- | ---: |",
        f"| 行覆盖率 | {percentage(attributes.get('line-rate'))} |",
        f"| 分支覆盖率 | {percentage(attributes.get('branch-rate'))} |",
        f"| 已覆盖行数 | {count_text(attributes, 'lines-covered', 'lines-valid')} |",
        f"| 已覆盖分支数 | {count_text(attributes, 'branches-covered', 'branches-valid')} |",
        "",
        "## 按包统计",
        "",
        "| 包 | 行覆盖率 | 已覆盖行数 | 分支覆盖率 |",
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
        lines.append("| 没有包数据 | - | - | - |")
    return "\n".join(lines)


def write_attachment(source: Path, result_id: str) -> dict[str, str] | None:
    """把一份原始报告复制成 Allure 附件(与性能/安全汇总项同一做法).

    附件名保留原文件名(``coverage.xml``), 报告里一眼能看出带的是哪份原始数据;
    文件不存在时返回 None(例如手改过的运行只产出了 XML)。
    """
    if not source.is_file():
        return None
    suffix = source.suffix.lstrip(".") or "txt"
    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    target = RESULTS_DIRECTORY / f"{result_id}-attachment.{suffix}"
    target.write_bytes(source.read_bytes())
    return {
        "name": source.name,
        "source": target.name,
        "type": ATTACHMENT_MEDIA_TYPES.get(suffix, "text/plain"),
    }


def raw_report_attachments(result_id: str) -> list[dict[str, str]]:
    """收集所有存在的原始报告附件(按 :data:`RAW_REPORT_FILES` 的顺序)."""
    return [
        item
        for item in (write_attachment(source, result_id) for source in RAW_REPORT_FILES)
        if item is not None
    ]


def with_raw_report_note(description: str, attachments: list[dict[str, str]]) -> str:
    """在描述末尾补一段“原始报告已作为附件”的说明(没有附件时原样返回)."""
    if not attachments:
        return description
    names = ", ".join(f"`{item['name']}`" for item in attachments)
    return (
        f"{description}\n\n"
        "## 原始报告\n\n"
        f"- 本条附带: {names}。\n"
        "- HTML 报告作为 CI 产物 `coverage-<os>` 发布。\n"
    )


def write_result(result: dict[str, Any], result_id: str) -> None:
    """将一条覆盖率结果写入 Allure 结果目录."""
    RESULTS_DIRECTORY.mkdir(parents=True, exist_ok=True)
    result_path = RESULTS_DIRECTORY / f"{result_id}-result.json"
    result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


def main(argv: Sequence[str] | None = None) -> None:
    """创建一条包含覆盖率摘要与原始报告附件的 Allure 结果."""
    result_id = str(uuid.uuid4())
    timestamp = time.time_ns() // 1_000_000
    name = resolve_platform(parse_args(argv).platform)
    attachments = raw_report_attachments(result_id)
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
            {"name": "os", "value": name},
            {"name": "env", "value": name},
            {"name": "severity", "value": COVERAGE_SEVERITY},
        ],
        "parameters": [{"name": "Platform", "value": name}],
        "description": "",
        "attachments": attachments,
    }
    if not COVERAGE_XML.exists():
        result["status"] = "broken"
        result["statusDetails"] = {"message": f"覆盖率文件不存在: {COVERAGE_XML}"}
        result["description"] = with_raw_report_note(
            f"## 覆盖率不可用\n\n覆盖率命令没有产出 `{COVERAGE_XML}`。",
            attachments,
        )
        write_result(result, result_id)
        return

    try:
        root = ET.parse(COVERAGE_XML).getroot()  # noqa: S314
    except (ET.ParseError, OSError) as exc:
        result["status"] = "broken"
        result["statusDetails"] = {"message": f"无法读取覆盖率 XML: {exc}"}
        result["description"] = with_raw_report_note(
            f"## 覆盖率不可用\n\n覆盖率文件无法解析: `{exc}`",
            attachments,
        )
        write_result(result, result_id)
        return

    result["description"] = with_raw_report_note(build_description(root), attachments)
    write_result(result, result_id)


if __name__ == "__main__":
    main()
