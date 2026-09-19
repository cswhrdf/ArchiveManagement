"""把性能与安全测试结果汇总进 Allure 报告.

汇总要做四件事:

1. 写入环境信息(操作系统、Python、提交 SHA、测试类别、覆盖率门槛、本次涉及的
   平台), 让每份报告都能回答"这次结果是在哪个平台、哪个提交上跑出来的";
2. 把 ``performance-results.json`` / ``security-results.json`` 转成 Allure 里
   可检索的测试项: 指标表写进描述, 原始 JSON/CSV 作为附件, 保证结论可下载;
   每个平台各写一条(名称/参数/标签都带平台), 三个平台的安全结论不会互相覆盖;
3. 写一份"运行总账"到 ``allure-run-ledger.md``(Markdown 附件: 报告里会**渲染**成
   表格与标题, 而不是丢一屏纯文本; 仓库根的 ``allurerc.mjs`` 按这个文件名把它收进报告
   首页「全局附件」): 质量门逐项结论 + 覆盖率(各平台, 数字取自原始 XML) + 性能一行
   + 安全各平台一行 + 产物清单(只列脚本生成的汇总结论项, 逐项核对原始文件在不在)。
   Allure 原生「质量门」页签只有 ``allure run`` 会填(见 allurerc.mjs 的注释), 所以
   总账就是这份报告里"一眼看完"的入口;
4. 缺少某类结果时不报错(例如只跑了单元测试), 只是跳过该类并写进环境信息与总账。

用法(CI 汇总 job): ``uv run python scripts/create_allure_summary.py``
"""

from __future__ import annotations

import contextlib
import json
import os
import re
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
# 运行总账: 由本脚本汇总各作业的结论写成, 再由仓库根 allurerc.mjs 的 globalAttachments
# 收进报告首页「全局附件」页签 —— 文件名与配置里的 glob 必须一致(改名要同时改配置)。
QUALITY_GATE_REPORT = Path("allure-run-ledger.md")
# 覆盖率项的全名: 它没有 testCategory 标签(见 create_allure_coverage.py), 用全名认。
COVERAGE_FULL_NAME = "archive-management.coverage"
# 质量检查项的识别方式: create_allure_quality.py 给每个检查项打的标签。
QUALITY_CATEGORY_LABEL = "testCategory"
QUALITY_CATEGORY = "quality"
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


def quality_checks(results_dir: Path) -> list[tuple[str, str]]:
    """收集质量检查项: 带 ``testCategory=quality`` 标签的汇总结果, 返回 (标题, 状态).

    按标题去重并排序: 同一个检查在多个作业里都会写一份(如三个平台都跑了 ruff),
    结论一致时合并成一行, 不一致时后者覆盖前者 —— 但 CI 里质量作业只跑 Ubuntu,
    所以实际只有一份。
    """
    checks: dict[str, str] = {}
    for path in sorted(results_dir.rglob(f"*{RESULT_FILE_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        labels = payload.get("labels", [])
        if not isinstance(labels, list) or not any(
            isinstance(label, dict)
            and label.get("name") == QUALITY_CATEGORY_LABEL
            and label.get("value") == QUALITY_CATEGORY
            for label in labels
        ):
            continue
        checks[str(payload.get("name", path.name))] = str(
            payload.get("status", "unknown")
        )
    return sorted(checks.items())


def quality_gate_report(
    checks: list[tuple[str, str]], *, heading: str = "# 质量门结论"
) -> str:
    """渲染质量门总评(Markdown, 供报告首页「全局附件」页签阅读).

    结论只看检查项的状态: 有一条不是 ``passed`` 就是未通过。这里**不**决定作业成败
    —— 各检查作业本身已经用退出码把关了, 本脚本只负责把结论写进报告(汇总作业要
    在作业失败的情形下也能生成报告, 所以这里即便未通过也返回 0)。

    ``heading`` 供总账把它降成二级标题(见 :func:`run_ledger`)。
    """
    if not checks:
        return (
            f"{heading}\n\n"
            "本次运行没有质量检查结果(未执行 ruff / mypy / pytest 等检查), "
            "结论: 不适用。\n"
        )
    failed = [name for name, status in checks if status != "passed"]
    if failed:
        headline = f"未通过({len(failed)}/{len(checks)} 项)"
    else:
        headline = f"通过(共 {len(checks)} 项)"
    lines = [
        heading,
        "",
        f"- 结论: **{headline}**",
        "- 来源: 各作业的质量检查项(scripts/create_allure_quality.py), 汇总作业聚合",
    ]
    if failed:
        lines.append(f"- 未通过: {', '.join(failed)}")
    lines += [
        "",
        "| 检查 | 结论 |",
        "| --- | --- |",
    ]
    lines += [
        f"| {_cell(name)} | {'通过' if status == 'passed' else f'未通过({status})'} |"
        for name, status in checks
    ]
    return "\n".join(lines) + "\n"


def result_payloads(results_dir: Path) -> list[dict[str, Any]]:
    """读取结果目录里所有可解析的 ``*-result.json``(顺序稳定, 便于断言与阅读)."""
    payloads: list[dict[str, Any]] = []
    for path in sorted(results_dir.rglob(f"*{RESULT_FILE_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    return payloads


def labels_of(payload: dict[str, Any]) -> dict[str, str]:
    """把结果上的标签摊平成 ``{名字: 取值}``(同名取最后一个)."""
    return {
        str(label.get("name")): str(label.get("value", ""))
        for label in (payload.get("labels") or [])
        if isinstance(label, dict)
    }


def percentage(value: object) -> str:
    """覆盖率比率(``0.9182``)写成百分比(``91.82%``); 缺失或非法时给 ``?``."""
    try:
        return f"{float(str(value)) * 100:.2f}%"
    except (TypeError, ValueError):
        return "?"


def coverage_rates(payload: dict[str, Any], results_dir: Path) -> tuple[str, str]:
    """从覆盖率项的原始 XML 里取 (行覆盖率, 分支覆盖率).

    只读文件头部的根节点属性: 为了两个数字引入 XML 解析不合算(coverage.xml 动辄几百
    KB), 而根节点上的 ``line-rate`` / ``branch-rate`` 就足以回答"这份报告多少覆盖率"。
    """
    for attachment in payload.get("attachments") or []:
        if not isinstance(attachment, dict):
            continue
        source = attachment.get("source")
        if not isinstance(source, str) or not source.endswith(".xml"):
            continue
        path = results_dir / source
        if not path.is_file():
            continue
        head = path.read_text(encoding="utf-8", errors="replace")[:4096]
        attributes = dict(re.findall(r'([\w-]+)="([^"]*)"', head.split(">", 1)[0]))
        return percentage(attributes.get("line-rate")), percentage(
            attributes.get("branch-rate")
        )
    return "?", "?"


def coverage_rows(
    results_dir: Path, payloads: list[dict[str, Any]]
) -> list[tuple[str, str, str]]:
    """覆盖率结论: (平台, 行覆盖率, 分支覆盖率) —— 每个平台各一行."""
    rows = []
    for payload in payloads:
        if payload.get("fullName") != COVERAGE_FULL_NAME:
            continue
        labels = labels_of(payload)
        env = labels.get("env") or labels.get("os") or "default"
        line, branch = coverage_rates(payload, results_dir)
        rows.append((env, line, branch))
    return sorted(rows)


def performance_note(performance: dict[str, Any] | None) -> str:
    """性能基准一行结论: 平台 + 达标项数(没跑性能测试时直接说明未执行)."""
    if performance is None:
        return "本次运行没有性能结果(未执行性能测试)。"
    measurements = list(performance.get("measurements", []))
    passed = sum(1 for item in measurements if item.get("passed"))
    if measurements and passed == len(measurements):
        verdict = f"全部达标({passed} 项)"
    else:
        verdict = f"{passed}/{len(measurements)} 项达标"
    return f"平台 {platform_of(performance)}: **{verdict}**"


def security_rows(payloads: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """安全结论: (平台, 结论数, 未拦截数) —— 每个平台各一行."""
    rows = []
    for payload in payloads:
        findings = list(payload.get("findings", []))
        blocked = sum(1 for item in findings if item.get("blocked"))
        rows.append(
            (platform_of(payload), str(len(findings)), str(len(findings) - blocked))
        )
    return sorted(rows)


def _file_state(results_dir: Path, source: object) -> tuple[str, str]:
    """附件文件的 (大小, 状态): 在结果目录里就报大小, 不在就明确标成缺失."""
    if not isinstance(source, str) or not source:
        return "-", "缺失(结果里没写来源)"
    path = results_dir / source
    if not path.is_file():
        return "-", "**缺失**"
    size = path.stat().st_size
    return (f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"), "在"


def artifact_rows(
    results_dir: Path, payloads: list[dict[str, Any]]
) -> list[tuple[str, str, str, str]]:
    """产物清单: (所属结论项, 文件名, 大小, 状态).

    只看**脚本生成的汇总结论项**(``fullName`` 以 ``archive-management.`` 开头): 用例
    自己的附件动辄上千个(截图/日志), 全列进来会把总账变成几百 KB 的噪声, 而且它们已经
    挂在各自用例的详情页上了。这里要回答的是"这次运行的大件产物都到位了吗"。

    汇总报告把各结论项的原始文件挂在该结论项上; 全局附件本身**不**在
    ``scripts/verify_allure_report.py`` 的校验范围内(它只核对结果声明的附件), 所以
    总账里逐项列出文件在不在, 首页因此也有一份可核对的清单。
    """
    rows = []
    for payload in payloads:
        if not str(payload.get("fullName") or "").startswith("archive-management."):
            continue
        title = str(payload.get("name") or payload.get("fullName") or "-")
        env = labels_of(payload).get("env", "")
        owner = f"{title}({env})" if env else title
        for attachment in payload.get("attachments") or []:
            if not isinstance(attachment, dict):
                continue
            name = str(attachment.get("name") or attachment.get("source") or "-")
            size, state = _file_state(results_dir, attachment.get("source"))
            rows.append((owner, name, size, state))
    return sorted(rows)


def run_ledger(
    results_dir: Path,
    payloads: list[dict[str, Any]],
    performance: dict[str, Any] | None,
    security_payloads: list[dict[str, Any]],
) -> str:
    """把四类结论与产物清单合成一份"运行总账"(报告首页「全局附件」页签)."""
    platforms = tested_platforms(results_dir)
    lines = [
        "# 运行总账",
        "",
        f"- 提交: `{git_commit()[:12]}`"
        f"(分支 {os.environ.get('GITHUB_REF_NAME', 'local')}"
        f", 运行 {os.environ.get('GITHUB_RUN_ID', 'local')})",
        f"- 涉及平台: {', '.join(platforms) if platforms else 'unknown'}",
        "- 下面四节是本次运行的结论, 每节都注明原始产物在哪条结论项里; "
        "末尾的产物清单逐项核对文件是否存在。",
        "",
        quality_gate_report(
            quality_checks(results_dir),
            heading="## 质量门(与平台无关, 只在 Linux 跑一遍; 归入 `Common` 环境)",
        ),
        "## 覆盖率",
        "",
    ]
    coverage = coverage_rows(results_dir, payloads)
    if coverage:
        lines += [
            "| 平台 | 行覆盖率 | 分支覆盖率 | 原始报告 |",
            "| --- | ---: | ---: | --- |",
        ]
        lines += [
            f"| {_cell(env)} | {_cell(line)} | {_cell(branch)} | "
            "`coverage.xml`(见结论项 `Coverage report`) |"
            for env, line, branch in coverage
        ]
    else:
        lines.append("本次运行没有覆盖率结论项。")
    lines += [
        "",
        "## 性能基准(只在 Linux 执行)",
        "",
        f"- {performance_note(performance)}",
        "- 原始数据 `performance-results.json` / `.csv` "
        "见结论项 `Performance baseline`。",
        "",
        "## 安全测试",
        "",
    ]
    security = security_rows(security_payloads)
    if security:
        lines += [
            "| 平台 | 结论数 | 未拦截 | 原始结论 |",
            "| --- | ---: | ---: | --- |",
        ]
        lines += [
            f"| {_cell(env)} | {_cell(total)} | {_cell(not_blocked)} | "
            "`security-results.json`(见结论项 `Security findings`) |"
            for env, total, not_blocked in security
        ]
    else:
        lines.append("本次运行没有安全结论文件。")
    lines += [
        "",
        "## 产物清单",
        "",
        "| 结论项 | 文件 | 大小 | 状态 |",
        "| --- | --- | ---: | --- |",
    ]
    lines += [
        f"| {_cell(owner)} | `{_cell(name)}` | {_cell(size)} | {_cell(state)} |"
        for owner, name, size, state in artifact_rows(results_dir, payloads)
    ]
    lines += [
        "",
        "- 说明: 全局附件不在 `scripts/verify_allure_report.py` 的校验范围内"
        "(它只核对结果声明的附件), 上面的状态列就是补上的那道核对; "
        "出现 **缺失** 说明报告里少了原始数据。",
    ]
    return "\n".join(lines) + "\n"


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

    身份**不带平台**: 三个平台的同一类汇总(覆盖率/性能/安全)是"同一份摘要、分属三个环境",
    与报告里用例结果的写法一致 —— 平台由 ``env`` 标签(配合仓库根的 ``allurerc.mjs``)
    变成 Allure 的环境, 因此标题里也不再拼平台名; ``平台`` 参数与 ``os`` 标签是兼底
    (生成端没读到报告配置时, 环境会静默退回 ``default``)。

    严重等级固定为 ``trivial``: 汇总项本身不验证任何行为, 只是把原始结论与附件
    带进报告; 不打等级的话报告里会多出一个 no_severity 桶。
    """
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
            {"name": "os", "value": platform},
            # 与环境维度对齐: 仓库根的 allurerc.mjs 用 env 标签把结果归到各平台的环境,
            # 缺了它这些汇总项只会出现在 default 环境里(按环境筛选时就看不到了)。
            {"name": "env", "value": platform},
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
            title="Performance baseline",
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
            title="Security findings",
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

    checks = quality_checks(results_dir)
    payloads = result_payloads(results_dir)
    QUALITY_GATE_REPORT.write_text(
        run_ledger(results_dir, payloads, performance, security_payloads),
        encoding="utf-8",
    )
    artifacts = artifact_rows(results_dir, payloads)
    missing = [row for row in artifacts if row[3] != "在"]
    print(
        f"运行总账已写入 {QUALITY_GATE_REPORT}: 质量门 {len(checks)} 项, "
        f"产物 {len(artifacts)} 个(缺失 {len(missing)} 个)"
    )
    for owner, name, _size, state in missing:
        print(f"警告: 原始产物不可用 —— {owner} 的 {name}({state})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
