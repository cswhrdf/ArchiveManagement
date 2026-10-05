"""把性能与安全测试结果汇总进 Allure 报告.

汇总要做六件事:

1. 写入环境信息(操作系统、Python、提交 SHA、测试类别、覆盖率门槛、本次涉及的
   平台), 让每份报告都能回答"这次结果是在哪个平台、哪个提交上跑出来的";
2. 把 ``performance-results.json`` / ``security-results.json`` 转成 Allure 里
   可检索的测试项: 指标表写进描述, 原始 JSON/CSV 作为附件, 保证结论可下载;
   每个平台各写一条(名称/参数/标签都带平台), 三个平台的安全结论不会互相覆盖。
   性能与安全结论项同样**由数据决定状态**: 有一条基准冲破预算就写 ``failed``; 覆盖率
   的"达没达标"由各平台的 ``Coverage report`` 项自己承担(描述开头就是结论行, 低于门槛
   直接写 ``failed``), 这里只把各平台的数字汇总进运行总账 —— 报告里不再单独多出一个
   "Coverage conclusion" 条目;
3. 写一份"运行总账"到 ``allure-run-ledger.md``(Markdown 附件: 报告里会**渲染**成
   表格与标题, 而不是丢一屏纯文本; 仓库根的 ``allurerc.mjs`` 按这个文件名把它收进报告
   首页「全局附件」): 开头一行数(应有/实有/缺) + 质量门逐项结论 + 覆盖率(各平台, 数字
   取自原始 XML) + 性能一行 + 安全各平台一行, **末尾**是「证据核对(应有 vs 实有)」——
   ① 结论项有没有进结果、② 每条结论项声明的原始文件在不在。
   Allure 原生「质量门」页签只有 ``allure run`` 会填(见 allurerc.mjs 的注释), 所以
   总账就是这份报告里"一眼看完"的入口;
4. 把"有意不统计的覆盖"写成另一份全局附件 ``allure-coverage-exclusions.md``:
   扫描 ``src/**/*.py`` 的 ``# pragma: no cover`` / ``# pragma: no branch`` 标记(逐条列出
   文件:行号、标记种类与原因)与 ``pyproject.toml`` 的 ``exclude_also`` —— 数据来自真实
   源码, 且每条豁免都必须写明原因(缺原因的会单独列为"写入问题");
5. 缺少某类结果时不报错(例如只跑了单元测试), 只是跳过该类并写进环境信息与总账;
   **但性能结果文件缺失时会写一条 broken 结论项** —— "没有这条"与"这条通过"必须能分辨;
6. 按一份**从产出方代码同步出来的清单**(``scripts/allure_catalog.py``)核对"应有 vs 实有":
   把本次运行**应该**有的结论项列全(质量检查项直接从 ``create_allure_quality.py`` 的
   ``CHECKS`` 解析出来, 不是手写清单), 逐项标"已收到 / 缺失", 每个缺失项**另写一条 broken
   结论项**并打印 ``::warning::`` —— 产物没产出/没上传/没合并进报告时, 总账里看得见,
   原生质量门也会跟着红。以前只渲染"手里有什么", 缺一整节是完全静默的(2026-10-02 漏掉
   视觉回归的产物与 ``test`` 组就是这么过去的)。
   这一节在总账**末尾**(结论先看, 审计附录在后), 但开头留一行数 —— 见 ``run_ledger``。

用法(CI 汇总 job):
``uv run python scripts/create_allure_summary.py --expect-platforms Windows,macOS,Linux``
(平台列表写**显示名**``Windows,macOS,Linux``, 与 CI 矩阵一致; ``allurerc.mjs`` 的
``environmentsTested`` 是同一个集合, 但那边**写作环境 id** —— 规则拿环境 id 比清单,
写显示名会整轮报"没测过", 见 docs/testing.md; 不传时退回"结果里出现过的平台")
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import time
import tomllib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 本脚本所在目录: 下面要用的清单模块与它同目录。``scripts`` 不是包(每处 ``uv sync`` 都带
# ``--no-install-project``), 所以先把自己这一层加进 ``sys.path`` 再 import —— 与
# ``create_allure_visual.py`` 借 ``tests/crash_capture.py`` 的做法一致。
SCRIPT_DIRECTORY = Path(__file__).resolve().parent
if str(SCRIPT_DIRECTORY) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIRECTORY))

import allure_catalog  # noqa: E402  (要先把自己所在目录加进 sys.path 才 import 得到)

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
# 本脚本自己产出的两类汇总项的身份。写成常量(而不是在调用处拼字符串)是为了让
# scripts/allure_catalog.py 能对着源码核出"本仓库到底会写出哪些身份", 以及守卫能钉住
# "清单里登记的每个身份都真的有人写" —— 见 tests/unit/test_report_verification.py。
PERFORMANCE_IDENTITY = "archive-management.performance"
SECURITY_IDENTITY = "archive-management.security"
#: 缺失结论项的类别: 报的是"这次少了哪一类结论", 不是某个具体用例的结果。
MISSING_CATEGORY = "missing"
#: 缺失结论项的严重等级: 它表示证据链断了一节, 用 major 而不是汇总项的 trivial。
MISSING_SEVERITY = "major"
#: 缺失项自己不知道会落在哪台机器上 —— ``os`` 标签写 unknown, 免得它被当成"又测了一个
#: 平台"(``os`` 标签是 ``tested_platforms()`` 认平台的地方); ``unknown`` 在平台列表里
#: 本来就被过滤掉。身份前缀来自清单(``ABSENCE_IDENTITY``): 它与预期身份错开, 缺失项
#: 自己永远不会被当成"这一族已经收到了"。
MISSING_OS_LABEL = "unknown"
MISSING_IDENTITY = allure_catalog.ABSENCE_IDENTITY
# Allure CLI 原生质量门的输出: CI 在生成报告前跑一次并留日志(见 ci.yml), 工作流会在
# 末尾追一行 `退出码: N`; 总账据此给出结论, 而不是去猜 CLI 的输出文本。
NATIVE_GATE_LOG = Path("allure-quality-gate.txt")
# 终端控制序列(CLI 输出带颜色) —— 与 create_allure_quality.py 同一个小助手(scripts 不是包)。
ANSI_SEQUENCE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
# 总账里最多展示多少行质量门原始输出(它通常只有几行).
MAX_GATE_LINES = 20
# 质量检查项的识别方式: create_allure_quality.py 给每个检查项打的标签。
QUALITY_CATEGORY_LABEL = "testCategory"
QUALITY_CATEGORY = "quality"
# 性能/安全汇总项的严重等级: 它们不验证行为, 只承载原始结论与附件, 但不打等级
# 会让报告里多出一个 no_severity 桶(和覆盖摘要项保持一致)。
SUMMARY_SEVERITY = "trivial"
# 安全用例在 Allure 结果里的层次标签(tests/conftest.py 按目录推断): 用它认出安全用例。
SECURITY_LAYER = "security"
# “没通过”的状态: broken 是夹具/环境炸了, 同样不能算通过。
FAILING_STATUSES = ("failed", "broken")
# 覆盖率门槛: 必须与 pyproject.toml 的 [tool.coverage.report] fail_under 一致 ——
# 守卫 test_coverage_fail_under_matches_pyproject 会把两处钉在一起. 汇总作业不装任何依赖,
# 所以这里写常量而不是去解析配置.
COVERAGE_THRESHOLD = "95"
# 平台缺一份覆盖率结论项时用的哨兵状态(Allure 自己的状态里没有它, 不会与真实状态撞车).
MISSING_STATUS = "missing"
# 有意不统计的覆盖(豁免清单): 与运行总账一样放在仓库根, 由 allurerc.mjs 的
# globalAttachments 收进报告首页「全局附件」页签(改名要同时改配置与 .gitignore).
COVERAGE_EXCLUSIONS_REPORT = Path("allure-coverage-exclusions.md")
# 失败现场: 把各作业失败时 collect_job_diagnostics.py 写的 summary.md 拼成一份(同属
# globalAttachments, 名字要与 allurerc.mjs 里那条一致).
FAILURE_DIAGNOSTICS_DIRECTORY = Path("failure-diagnostics")
FAILURE_DIAGNOSTICS_REPORT = Path("allure-failure-diagnostics.md")
# 兜底判定文件名(由 scripts/collect_job_diagnostics.py 写在每个诊断目录里): 附件按它把
# "结果里已经写明白"的普通失败滤掉。
FAILURE_DIAGNOSTICS_VERDICT = "verdict.json"
# 作业崩溃(兜底现场)**除了**首页附件之外还要另写一条结论项: 只挂附件的话, "这个作业是崩掉的"
# 既不进任何计数, 也没法按环境/等级筛 —— 而它恰恰是最该被统计的那一类(用户 2026-10-05)。
# 身份前缀同样刻意与清单里的预期身份错开, 免得被当成"这一族已经收到了"。
CRASH_IDENTITY = "archive-management.crash."
CRASH_CATEGORY = "diagnostics"
CRASH_SEVERITY = "critical"
# 它不来自哪台机器, 而是"作业崩溃时留下的现场" —— 与缺失结论项同理, `os` 标签不能写平台,
# 否则 tested_platforms() 会把那个平台当成"交过东西"。
CRASH_OS_LABEL = "diagnostics"
# 运行器镜像名/摘要里的字样 → 报告里的平台**显示名**(allurerc.mjs 的 matcher 认这个值).
CRASH_PLATFORM_TOKENS = (
    ("macos", "macOS"),
    ("darwin", "macOS"),
    ("windows", "Windows"),
    ("linux", "Linux"),
    ("ubuntu", "Linux"),
)
CRASH_PLATFORM_FALLBACK = "common"
# 仓库根与源码树: 豁免清单的数据必须来自真实源码 + pyproject.toml, 不能是手工清单.
REPOSITORY_ROOT = Path(__file__).resolve().parent.parent
SOURCE_ROOT_NAME = "src"
PYPROJECT_NAME = "pyproject.toml"
# 覆盖率豁免标记的解析规则: 与 tests/unit/test_coverage_pragmas.py 共用(那个守卫直接导入
# 这里), 因此不存在"两套宽松程度不同的解析器"—— 放宽一处, 另一处就把该报的问题当合规.
PRAGMA_PATTERN = re.compile(r"#\s*pragma:\s*(?P<kind>no cover|no branch)(?P<rest>.*)$")
MIN_PRAGMA_REASON_LENGTH = 5
# 占位词: 写了等于没写(按整句比较, 不做子串匹配, 否则"无头环境"会被"无"误伤).
PRAGMA_PLACEHOLDERS = frozenset(
    {"todo", "fixme", "无", "略", "……", "...", "待补", "稍后"}
)
# 允许 ``no branch`` 出现的行: 标在别处等于没标.
BRANCH_LINE_PREFIXES = (
    "if ",
    "if(",
    "elif ",
    "else:",
    "for ",
    "while ",
    "except",
    "finally",
)
# 不许把整块排除掉: 标记落在定义行上等于"这块不测".
DEFINITION_PREFIXES = ("def ", "class ", "async def ")


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


def first_line(value: object) -> str:
    """取多行文本的第一行(空值给空串), 用于把 Allure 的失败原因压成一行."""
    lines = str(value or "").strip().splitlines()
    return lines[0] if lines else ""


def _ratio(value: object) -> float | None:
    """把 XML 属性里的比率(``0.9182``)转成浮点数; 缺失或非法时给 None."""
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _percent(rate: float | None) -> str:
    """覆盖率比率写成百分比(``91.82%``); 取不到时给 ``?`` —— 不猜一个数字出来."""
    return "?" if rate is None else f"{rate * 100:.2f}%"


@dataclass(frozen=True)
class CoverageNumbers:
    """一个平台的覆盖率数字(都来自该平台结论项附带的原始 coverage.xml)."""

    line_rate: float | None
    branch_rate: float | None
    combined_rate: float | None


def _coverage_numbers(attributes: dict[str, str]) -> CoverageNumbers:
    """按 coverage.py 的口径算"合计覆盖率": (行覆盖 + 分支覆盖) / (行总数 + 分支总数).

    覆盖率门槛 ``fail_under`` 比的就是这个合计值(coverage report 的 TOTAL 一列), 不是行
    覆盖率 —— 实测某次报告 12922 行 / 2886 分支, 用这份公式算出的 TOTAL 与 CLI 打印的 98%
    一致。拿不到计数(旧格式的 XML)时退回行覆盖率, 宁可少一层信息也不要凭空算一个数。
    """
    line = _ratio(attributes.get("line-rate"))
    branch = _ratio(attributes.get("branch-rate"))
    covered_lines = _ratio(attributes.get("lines-covered"))
    covered_branches = _ratio(attributes.get("branches-covered"))
    valid_lines = _ratio(attributes.get("lines-valid"))
    valid_branches = _ratio(attributes.get("branches-valid"))
    if (
        covered_lines is None
        or covered_branches is None
        or valid_lines is None
        or valid_branches is None
    ):
        return CoverageNumbers(line, branch, line)
    valid = valid_lines + valid_branches
    combined = (covered_lines + covered_branches) / valid if valid > 0 else line
    return CoverageNumbers(line, branch, combined)


def coverage_numbers(payload: dict[str, Any], results_dir: Path) -> CoverageNumbers:
    """从覆盖率结论项附带的原始 XML 里读数字(读不到就给全 None).

    只读根节点属性: coverage.xml 动辄几百 KB, 为了几个数字引入 XML 解析不合算。**不能**
    简单取"第一个 ``>`` 之前的内容": coverage.py 写的文件带一行 ``<?xml ... ?>`` 声明,
    那样只会拿到声明本身, 各平台的覆盖率会全变成 ``?``(实测踩过).
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
        root = re.search(r"<coverage\b([^>]*)>", head)
        if root is None:
            continue
        return _coverage_numbers(dict(re.findall(r'([\w-]+)="([^"]*)"', root.group(1))))
    return CoverageNumbers(None, None, None)


def coverage_problem(numbers: CoverageNumbers, item_status: str, message: str) -> str:
    """某平台覆盖率结论的判定理由; 空串表示通过(绝不把"读不到"当通过)."""
    if item_status == MISSING_STATUS:
        return "缺少覆盖率结论项(报告作业可能没有运行, 或产物没合并进来)"
    if item_status != "passed":
        detail = f": {message}" if message else ""
        return f"覆盖率结论项状态为 {item_status}{detail}"
    if numbers.combined_rate is None:
        return "读不到覆盖率数字(结论项没有附带可解析的 coverage.xml)"
    if numbers.combined_rate * 100 < float(COVERAGE_THRESHOLD):
        return f"合计覆盖率 {_percent(numbers.combined_rate)} 低于门槛 {COVERAGE_THRESHOLD}%"
    return ""


def _coverage_items(
    results_dir: Path, payloads: list[dict[str, Any]]
) -> dict[str, tuple[CoverageNumbers, str, str]]:
    """按平台收集覆盖率结论项 ``{平台: (数字, 状态, 失败原因首行)}``.

    平台取自结论项的 ``env`` 标签(仓库根的 ``allurerc.mjs`` 按它把结果归到各环境), 缺了才
    退回 ``os`` 标签 —— 与总账一直以来的口径一致。
    """
    items: dict[str, tuple[CoverageNumbers, str, str]] = {}
    for payload in payloads:
        if payload.get("fullName") != COVERAGE_FULL_NAME:
            continue
        labels = labels_of(payload)
        platform = labels.get("env") or labels.get("os") or "default"
        details = payload.get("statusDetails")
        message = (
            first_line(details.get("message")) if isinstance(details, dict) else ""
        )
        items[platform] = (
            coverage_numbers(payload, results_dir),
            str(payload.get("status", "unknown")),
            message,
        )
    return items


def coverage_conclusion(
    results_dir: Path, payloads: list[dict[str, Any]], expected_platforms: list[str]
) -> tuple[str, list[str], list[tuple[str, str, str, str, str, str]]]:
    """覆盖率总结论 ``(状态, 失败原因, 每平台一行)``.

    每个**有用例结果的平台**(``expected_platforms``)都该有一份覆盖率结论: 缺了就要红 ——
    "文件不在"绝不能退化成一个静默的通过。数字取自各平台结论项附带的原始 ``coverage.xml``。
    """
    items = _coverage_items(results_dir, payloads)
    failures: list[str] = []
    rows: list[tuple[str, str, str, str, str, str]] = []
    for platform in sorted({*expected_platforms, *items}):
        numbers, status, message = items.get(
            platform, (CoverageNumbers(None, None, None), MISSING_STATUS, "")
        )
        problem = coverage_problem(numbers, status, message)
        if problem:
            failures.append(f"{platform}: {problem}")
        rows.append(
            (
                platform,
                _percent(numbers.line_rate),
                _percent(numbers.branch_rate),
                _percent(numbers.combined_rate),
                f"{COVERAGE_THRESHOLD}%",
                "未通过" if problem else "通过",
            )
        )
    if not rows:
        return "broken", ["本次运行没有任何覆盖率结论项, 也没有可据以判断的平台"], rows
    return ("failed" if failures else "passed"), failures, rows


def is_pragma_line(text: str) -> bool:
    """该行是否"看起来"是一条覆盖率豁免标记(写法不规范也算, 交给调用方判定)."""
    return "pragma:" in text and ("no cover" in text or "no branch" in text)


def parse_pragma(text: str) -> re.Match[str] | None:
    """按规范写法解析豁免标记; 不合规范时返回 None."""
    return PRAGMA_PATTERN.search(text)


def pragma_reason_problem(explanation: str) -> str:
    """原因文字是否合格(太短或占位词); 合格给空串."""
    normalized = explanation.removesuffix("。").removesuffix(".").lower()
    if len(explanation) < MIN_PRAGMA_REASON_LENGTH:
        return f"原因太短 ({explanation})"
    if normalized in PRAGMA_PLACEHOLDERS:
        return f"原因是占位词 ({explanation})"
    return ""


@dataclass(frozen=True)
class PragmaNote:
    """源码里的一条覆盖率豁免标记(含解析结果与合规问题)."""

    path: str
    line: int
    kind: str
    marker: str
    reason: str
    problem: str


def pragma_note(
    module: Path, source_root: Path, number: int, text: str
) -> PragmaNote | None:
    """把一行文本解析成 :class:`PragmaNote`(不是豁免标记时返回 None)."""
    match = parse_pragma(text)
    if match is None:
        return None
    marker = text.strip()
    rest = match.group("rest").strip()
    reason = rest.lstrip("- ").strip() if rest.startswith("-") else ""
    problem = _pragma_problem(marker, rest, reason, match.group("kind"))
    return PragmaNote(
        module.relative_to(source_root).as_posix(),
        number,
        match.group("kind"),
        marker,
        reason,
        problem,
    )


def _pragma_problem(marker: str, rest: str, reason: str, kind: str) -> str:
    """逐条检查规范(定义行 / 缺原因 / 原因质量 / no branch 的位置), 返回第一条问题."""
    if marker.startswith(DEFINITION_PREFIXES):
        return "标记落在函数/类定义上(等于整块不测)"
    if not rest.startswith("-"):
        return "缺少 ` - 原因`"
    problem = pragma_reason_problem(reason)
    if problem:
        return problem
    if kind == "no branch" and not marker.startswith(BRANCH_LINE_PREFIXES):
        return "`no branch` 标在了没有分支的行上"
    return ""


def source_pragma_notes(source_root: Path) -> list[PragmaNote]:
    """扫描源码树, 收集全部豁免标记(按文件与行号排序)."""
    notes: list[PragmaNote] = []
    for module in sorted(source_root.rglob("*.py")):
        for number, text in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if not is_pragma_line(text):
                continue
            note = pragma_note(module, source_root, number, text)
            if note is not None:
                notes.append(note)
    return notes


def excluded_also(project_root: Path) -> tuple[str, ...]:
    """读 ``pyproject.toml`` 的 ``[tool.coverage.report] exclude_also``(读不到给空)."""
    path = project_root / PYPROJECT_NAME
    try:
        payload = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"跳过无法解析的 {path}: {exc}", file=sys.stderr)
        return ()
    report = payload.get("tool", {}).get("coverage", {}).get("report", {})
    entries = report.get("exclude_also", []) if isinstance(report, dict) else []
    return tuple(str(item) for item in entries) if isinstance(entries, list) else ()


def _pragma_file_sections(notes: list[PragmaNote]) -> list[str]:
    """按文件列出豁免标记; 一条都没有时给出明确的"没有豁免"说明."""
    if not notes:
        return [
            "## 按文件",
            "",
            "本次没有任何 `# pragma: no cover` / `# pragma: no branch` 标记 —— "
            "没有代码被刻意跳过(覆盖率统计覆盖了全部可达代码)。",
            "",
        ]
    lines = ["## 按文件", ""]
    for path in sorted({note.path for note in notes}):
        lines.append(f"### `{path}`")
        lines.append("")
        for note in (item for item in notes if item.path == path):
            detail = f"`{note.path}:{note.line}` — `{note.kind}`"
            if note.reason:
                detail += f" — 原因: {note.reason}"
            if note.problem:
                detail += f" — **问题: {note.problem}**"
            lines.append(f"- {detail}")
        lines.append("")
    return lines


def coverage_exclusions_report(
    project_root: Path, *, source_root: Path | None = None
) -> str:
    """渲染"有意不统计的覆盖"清单(Markdown, 作为报告首页的全局附件).

    数据来自真实源码(``src/**/*.py``)与 ``pyproject.toml``, 不是手工维护的清单; 解析规则与
    ``tests/unit/test_coverage_pragmas.py`` 共用同一份实现(见模块常量区的说明)。
    """
    root = source_root if source_root is not None else project_root / SOURCE_ROOT_NAME
    notes = source_pragma_notes(root)
    excluded = excluded_also(project_root)
    covers = sum(1 for note in notes if note.kind == "no cover")
    branches = sum(1 for note in notes if note.kind == "no branch")
    problems = [note for note in notes if note.problem]
    lines = [
        "# 有意不统计的覆盖率(豁免清单)",
        "",
        "- 数据来源: `src/**/*.py` 里的 `# pragma: no cover` / `# pragma: no branch` 标记与 "
        f"`{PYPROJECT_NAME}` 的 `[tool.coverage.report] exclude_also` —— 不是手工维护的清单。",
        f"- 汇总: `no cover` {covers} 条, `no branch` {branches} 条, "
        f"`exclude_also` {len(excluded)} 条。",
        "",
    ]
    if problems:
        lines += [f"- **写入问题: {len(problems)} 条(详见文末「写入问题」)。**", ""]
    lines += _pragma_file_sections(notes)
    lines += ["## `exclude_also`(pyproject.toml)", ""]
    if excluded:
        lines += [f"- `{_cell(item)}`" for item in excluded]
    else:
        lines.append("`exclude_also` 为空: 没有额外排除的代码块。")
    lines += ["", "## 写入问题", ""]
    if problems:
        lines += [
            f"- `{note.path}:{note.line}` — {note.problem} — `{_cell(note.marker)}`"
            for note in problems
        ]
    else:
        lines.append("没有: 每个豁免标记都写明了原因。")
    return "\n".join(lines) + "\n"


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


def performance_verdict(performance: dict[str, Any]) -> tuple[str, list[str]]:
    """性能结论 ``(状态, 未达标说明)``.

    任何一条测量 ``passed`` 非真就写 ``failed``(基准冲破预算 = 性能回归), 一条测量都没有
    写 ``broken`` —— 空结果不能算通过。
    """
    measurements = [
        item for item in performance.get("measurements", []) if isinstance(item, dict)
    ]
    if not measurements:
        return "broken", ["性能结果里没有任何测量"]
    failures = [
        f"未达标: {item.get('name', '?')} [{item.get('scale', '?')}] "
        f"{item.get('metric', '?')}={item.get('value', '?')}{item.get('unit', '')}"
        f"(阈值 {'≤' if item.get('comparison') == 'max' else '≥'} "
        f"{item.get('threshold', '?')})"
        for item in measurements
        if not item.get("passed")
    ]
    return ("failed" if failures else "passed"), failures


def performance_digest(status: str, failures: list[str]) -> str:
    """性能结论项开头的"结论"段: 一眼看出这次基准过没过."""
    if status == "passed":
        return "## 结论\n\n**通过** —— 全部基准都在预算内。\n"
    lines = ["## 结论", "", f"**未通过**({len(failures)} 条):", ""]
    lines += [f"- {_cell(item)}" for item in failures]
    return "\n".join(lines) + "\n"


def performance_rows(
    performance: dict[str, Any] | None,
) -> list[tuple[str, str, str, str]]:
    """总账里的性能行 ``(平台, 基准数, 未达标数, 结论)``; 没有结果时给空列表."""
    if performance is None:
        return []
    measurements = [
        item for item in performance.get("measurements", []) if isinstance(item, dict)
    ]
    failed = sum(1 for item in measurements if not item.get("passed"))
    status, _ = performance_verdict(performance)
    verdict = {"passed": "通过", "failed": "**未通过**", "broken": "**不可用**"}[status]
    return [(platform_of(performance), str(len(measurements)), str(failed), verdict)]


def security_test_failures(results_dir: Path, platform: str) -> list[tuple[str, str]]:
    """列出某个平台**失败的安全用例** ``(名字, 原因第一行)``.

    安全用例只在 security 作业里跑(不在 pytest 分片里), 它的成败原本只体现在那个作业
    的状态上, 而汇总结论项总是写 passed —— 于是报告里完全看不出“有一条安全用例红了”
    (2026-09-25 的 Linux: 2641 条结果里有 1 条 failed, `Security findings` 却是绿的)。
    """
    failures: list[tuple[str, str]] = []
    for payload in result_payloads(results_dir):
        labels = labels_of(payload)
        if labels.get("layer") != SECURITY_LAYER or labels.get("env") != platform:
            continue
        if str(payload.get("status")) not in FAILING_STATUSES:
            continue
        error = payload.get("statusDetails") or payload.get("error") or {}
        first_line = str(error.get("message") or "").strip().splitlines()
        failures.append(
            (str(payload.get("name") or "?"), first_line[0] if first_line else "")
        )
    return failures


def security_verdict(
    results_dir: Path, platform: str, findings: list[dict[str, Any]]
) -> tuple[str, list[str]]:
    """某个平台的安全结论 ``(状态, 失败原因列表)``.

    两条判据: ① 期望被拦下、实际没拦住的结论(``blocked`` 非真); ② 真的失败的安全用例。
    任一条命中就写 ``failed`` —— 报告里那条结论项会显示为失败, 原生质量门也会跟着红,
    不会出现“用例红了而汇总结论还是绿的”。
    """
    failures = [
        f"未拦截: {item.get('scenario') or item.get('category') or '未命名场景'}"
        for item in findings
        if not item.get("blocked")
    ]
    failures += [
        f"用例失败: {name}" + (f" —— {reason}" if reason else "")
        for name, reason in security_test_failures(results_dir, platform)
    ]
    return ("failed" if failures else "passed"), failures


def security_digest(status: str, failures: list[str]) -> str:
    """安全结论项开头的“结论”段: 一眼看出这次安全测试到底过没过."""
    if status == "passed":
        return "## 结论\n\n**通过** —— 没有未拦截的结论, 也没有失败的安全用例。\n"
    lines = ["## 结论", "", f"**未通过**({len(failures)} 条):", ""]
    lines += [f"- {_cell(item)}" for item in failures]
    return "\n".join(lines) + "\n"


def security_rows(
    results_dir: Path, payloads: list[dict[str, Any]]
) -> list[tuple[str, str, str, str, str]]:
    """安全结论: (平台, 结论数, 未拦截数, 失败用例数, 结论) —— 每个平台各一行."""
    rows = []
    for payload in payloads:
        findings = list(payload.get("findings", []))
        blocked = sum(1 for item in findings if item.get("blocked"))
        platform = platform_of(payload)
        status, failures = security_verdict(results_dir, platform, findings)
        failed_cases = [item for item in failures if item.startswith("用例失败")]
        rows.append(
            (
                platform,
                str(len(findings)),
                str(len(findings) - blocked),
                str(len(failed_cases)),
                "**未通过**" if status == "failed" else "通过",
            )
        )
    return sorted(rows)


def _file_state(results_dir: Path, source: object) -> tuple[str, str]:
    """附件文件的 (大小, 状态): 已收录时给大小, 缺失时明确标出来."""
    if not isinstance(source, str) or not source:
        return "-", "缺失(结果里没写来源)"
    path = results_dir / source
    if not path.is_file():
        return "-", "**缺失**"
    size = path.stat().st_size
    return (f"{size / 1024:.1f} KB" if size >= 1024 else f"{size} B"), "已收录"


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


def native_gate_section() -> list[str]:
    """原生质量门(Allure CLI)一节: 退出码 + 原始输出.

    日志文件由 CI 在生成报告前写好(工作流会在末尾追一行 `退出码: N`); 本地直接跑汇总
    脚本时没有它, 这一节会写明"未执行", 而不是装作通过。
    """
    heading = "## 原生质量门(Allure CLI)"
    if not NATIVE_GATE_LOG.is_file():
        return [heading, "", "本次没有质量门输出(未执行或未留日志)。", ""]
    text = ANSI_SEQUENCE.sub(
        "", NATIVE_GATE_LOG.read_text(encoding="utf-8", errors="replace")
    )
    code = "?"
    body: list[str] = []
    for line in text.splitlines():
        match = re.match(r"退出码:\s*(\d+)", line.strip())
        if match:
            code = match.group(1)
        elif line.strip():
            body.append(line)
    verdict = "**通过**" if code == "0" else f"**未通过(退出码 {code})**"
    return [
        heading,
        "",
        f"- 结论: {verdict}(规则写在仓库根的 `allurerc.mjs`)",
        "- 与上面的逐项检查互补: 这里管整次运行 —— 失败数 / 用例数 / 通过率 / 平台是否齐全。",
        "",
        "```text",
        *(body[:MAX_GATE_LINES] or ["(没有输出)"]),
        "```",
        "",
    ]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数(不传就是"没有参数": 用例直接调 ``main()`` 时不碰 ``sys.argv``)."""
    parser = argparse.ArgumentParser(
        description="把性能/安全/覆盖率结论与环境信息写入 allure-results"
    )
    parser.add_argument(
        "--expect-platforms",
        default="",
        help=(
            "本次运行声明要覆盖的平台, 逗号分隔(如 Windows,macOS,Linux)。它决定总账末节"
            '「证据核对」里"应有"的那些每平台项; 不传时退回"结果里出现过的平台" ——'
            "那样整个平台的产物都没交上来时看不出来"
        ),
    )
    parser.add_argument(
        "--write-failure-diagnostics",
        action="store_true",
        help=(
            '只写报告首页的"失败现场"附件然后退出。附件的内容是各作业失败时上传的诊断摘要, '
            '所以必须排在汇总作业"下载各作业诊断"那一步之后 —— 而本脚本的主流程跑在它之前, '
            '写出来的会是空的(实测 2026-10-04: 附件一直显示"本轮没有作业失败")'
        ),
    )
    arguments = parser.parse_args([] if argv is None else list(argv))
    arguments.platforms = [
        item.strip()
        for item in str(arguments.expect_platforms).split(",")
        if item.strip()
    ]
    return arguments


def producer(key: str) -> allure_catalog.Producer:
    """取清单里的一族结论(身份与标题的唯一来源, 免得在这边再抄一遍字面量)."""
    return next(item for item in allure_catalog.CATALOG if item.key == key)


def self_written_items(
    performance: dict[str, Any] | None, security_payloads: list[dict[str, Any]]
) -> set[tuple[str, str]]:
    """本脚本自己会写下的结论项(它们不该在清单里被当成"没收到").

    性能/安全这两类结论项是**这个脚本**写的, 而"应有 vs 实有"要在它们写下来之前就采好
    (见 ``main``) —— 不把它们补进来, 每次跑都会把自己要写的那两类报成缺失。真正该盯着
    的是**原始数据文件**在不在: 它不在时 ``Performance baseline`` 会写成 broken、安全那类
    则根本不写 —— 两种情形在清单里都会如实反应。

    "安全按平台各一份": 所以补的是"有数据的那几个平台", 某个平台的 ``security-results.json``
    没交上来时, 它那一行依然是缺失(这正是我们要看见的)。
    """
    items: set[tuple[str, str]] = set()
    if performance is not None:
        items.add((PERFORMANCE_IDENTITY, ""))
    for payload in security_payloads:
        items.add((SECURITY_IDENTITY, platform_of(payload)))
    return items


def completeness_section(
    expected: list[allure_catalog.Expected],
    missing: list[allure_catalog.Expected],
    results_dir: Path,
    payloads: list[dict[str, Any]],
) -> list[str]:
    """总账**末节**「证据核对(应有 vs 实有)」: 两张表回答两个不同的问题.

    这是总账里唯一能看出"这次缺了东西"的地方。以前每一节都是"有就渲染、没有就写一句没有",
    于是产物没产出/没上传/没合并进报告时, 报告的完整度完全看不出来 —— 它只会安静地少一节
    (2026-10-02 漏掉视觉回归的产物与 ``test`` 组就是这么过去的)。这里把清单里的每一项都
    列出来(应有), 再逐项标注收到没收到(实有), 缺了就点名。

    为什么放**最后**、为什么与产物清单合并成一节: 前面几节是结论本身, 是读者真要看的;
    "证据齐不齐"是审计性的附录。但移到末尾不能牺牲"一眼看见" —— 所以 :func:`run_ledger` 把
    "应有/实有/缺"的**一行数**留在开头, 缺项时指路到这里。

    为什么仍然分成**两张表**(① 结论清单 + ② 附件文件): 它们问的是不同粒度的问题 ——
    ① 问"这一次该有的结论项有没有真的进到结果里"(节与节之间的空洞), ② 问"每条结论项声明的
    原始文件有没有跟着进到结果目录里"(结论在、证据丢)。硬合成一张表要么得把文件按结论项
    塞进一个个单元格, 要么就得做两者的笛卡尔积 —— 前者难读, 后者会把"缺结论项"这条最要命
    的信号埋进几十行附件里。

    「判定」列是刻意加上的: 每条结论只由**一个**角色判定, 职责不重叠 —— ``总账`` 表示缺失时
    本脚本会另写一条 broken 结论项(原生质量门会跟着红); ``报告自检`` 表示那条由
    ``scripts/verify_allure_report.py`` 按 ``--expect-platforms`` 逐平台对数, 总账只把它
    **列出来**, 不重复判一遍。
    """
    missing_set = set(missing)
    lines = [
        "## 证据核对(应有 vs 实有)",
        "",
        f"- 应有 {len(expected)} 项, 实有 {len(expected) - len(missing_set)} 项"
        + (f", **缺 {len(missing_set)} 项**。" if missing_set else ", 全部到齐。"),
        "- 「应有」不是手写的: `scripts/allure_catalog.py` 从产出方**解析**出来"
        "(质量检查项取自 `create_allure_quality.py` 的 `CHECKS`, 在那边加一项检查, 这里立刻"
        '多一行)。所以"产物没产出/没上传/没合并进来"都会在这里显形。',
        "- ① 核对**结论项**: 应有而没有的标 **缺失**, 并各写一条 broken 结论项 —— "
        "总账是附件, 不点开是看不到的。",
        "- ② 核对**结论项声明的附件文件**: 结论在、原始文件却在打包/下载环节丢了, "
        "只有这一列看得见(全局附件不在 `scripts/verify_allure_report.py` 的校验范围内)。",
        "",
        "### ① 结论清单",
        "",
        "| 结论项 | 预期产物 | 产出者 | 判定 | 状态 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in expected:
        judge = "总账"
        if item.producer.gate != allure_catalog.GATE_SUMMARY:
            judge = "报告自检"
        state = "**缺失**" if item in missing_set else "已收到"
        lines.append(
            f"| {_cell(item.label)} | `{_cell(item.producer.artifact)}` "
            f"| `{_cell(item.producer.script)}` / 作业 `{_cell(item.producer.job)}` "
            f"| {judge} | {state} |"
        )
    lines += [
        "",
        "### ② 结论项声明的附件文件",
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
        "> 状态列: `已收录` = 原始文件确实在结果目录里(报告里点得开); "
        "**缺失** = 结论项声明了它但文件不在 —— 说明这段证据在打包/下载环节丢了。",
    ]
    return lines


def missing_digest(message: str) -> str:
    """缺失结论项的描述: 缺了什么、去哪儿找、怎么补."""
    return (
        "## 缺少结论\n\n"
        f"{message}\n\n"
        "## 怎么办\n\n"
        "- 先看产出它的 CI 作业有没有跑、产物有没有上传(上面点了名);\n"
        "- 再看汇总作业有没有把那份产物下载下来(工作流里的 `download-artifact` 清单);\n"
        "- 最后看合并进 `allure-results` 时有没有被别的东西盖掉。\n"
    )


def missing_conclusion_results(
    results_dir: Path, missing: list[allure_catalog.Expected]
) -> list[str]:
    """给每个缺失的结论项写一条 broken 结论项, 返回日志用的告警文案.

    为什么要在报告里另写一条: 总账是一份**附件**, 不点开看不到; 而原生质量门只数结果的
    状态 —— 缺一节证据时如果什么都不写, 门禁会安静地通过。写一条 broken 项同时做三件事:
    报告首页与用例树里一眼可见、失败数跟着涨(门禁红)、CI 日志里留下告警行。

    只有 ``GATE_SUMMARY`` 的项在这里写 —— 平台用例那条由报告自检负责(分工见
    ``scripts/allure_catalog.py`` 文件头), 两边都写会让同一件事报两遍。

    身份用 ``archive-management.missing.<家族>``: 刻意不落在任何预期身份的前缀里, 这样
    "缺失项自己"绝不会被当成"这一族已经收到了"。
    """
    messages: list[str] = []
    for item in missing:
        if item.producer.gate != allure_catalog.GATE_SUMMARY:
            continue
        message = allure_catalog.describe_missing(item)
        write_result(
            results_dir,
            result_id=str(uuid.uuid4()),
            identity=f"{MISSING_IDENTITY}{item.producer.key}",
            category=MISSING_CATEGORY,
            title=f"缺少结论: {item.label}",
            description=missing_digest(message),
            attachments=[],
            platform=item.environment or "common",
            status="broken",
            status_message=message,
            severity=MISSING_SEVERITY,
            os_label=MISSING_OS_LABEL,
        )
        messages.append(message)
    return messages


def run_ledger(
    results_dir: Path,
    payloads: list[dict[str, Any]],
    performance: dict[str, Any] | None,
    security_payloads: list[dict[str, Any]],
    platforms: list[str] | None = None,
    expected: list[allure_catalog.Expected] | None = None,
) -> str:
    """把四类结论与证据核对合成一份"运行总账"(报告首页「全局附件」页签).

    ``platforms`` 是本次运行涉及、且**应该**有覆盖率结论的平台(调用方在写任何新结论项之前
    采好的那一份); 不传时退回"结果里出现过的平台"。

    ``expected`` 是本次运行**应该**有的结论项(清单展开的结果)。调用方传进来是为了让末节与
    它写下的 broken 结论项用同一个种子; 不传时按 ``known_platforms`` 现算一份 ——
    缺失项的身份前缀刻意与预期身份错开, 所以无论哪条路径, 刚写下的缺失项都不会被当成
    "这一族已经收到了"。
    """
    known_platforms = tested_platforms(results_dir) if platforms is None else platforms
    # ``unknown`` 不算平台: 缺失结论项并不来自哪台机器, 它的 ``os`` 标签就是它。
    known_platforms = [name for name in known_platforms if name not in {"unknown", ""}]
    expectation = (
        expected
        if expected is not None
        else allure_catalog.expected_items(list(known_platforms), SCRIPT_DIRECTORY)
    )
    actual = allure_catalog.actual_items(results_dir) | self_written_items(
        performance, security_payloads
    )
    missing = allure_catalog.missing_items(expectation, actual)
    expected_coverage = [
        name for name in known_platforms if name not in {"unknown", "", "default"}
    ]
    verdict = (
        f"**缺 {len(missing)} 项 —— 见文末「证据核对」**" if missing else "全部到齐"
    )
    lines = [
        "# 运行总账",
        "",
        f"- 提交: `{git_commit()[:12]}`"
        f"(分支 {os.environ.get('GITHUB_REF_NAME', 'local')}"
        f", 运行 {os.environ.get('GITHUB_RUN_ID', 'local')})",
        f"- 涉及平台: {', '.join(known_platforms) if known_platforms else 'unknown'}",
        f"- 证据核对: 应有 {len(expectation)} 项 / 实有 "
        f"{len(expectation) - len(missing)} 项 / {verdict}。",
        "- 读法: 先看各节结论(质量门 → 覆盖率 → 性能 → 安全, 每节都注明原始产物在哪条结论项"
        "里); 文末「证据核对」逐项核对这次**应该**有哪些结论项、哪些到齐了, 以及每条结论项"
        "声明的附件文件在不在。",
        "",
        quality_gate_report(
            quality_checks(results_dir),
            heading="## 质量门(与平台无关, 只在 Linux 跑一遍; 归入 `Common` 环境)",
        ),
        *native_gate_section(),
        "## 覆盖率",
        "",
    ]
    coverage_status, coverage_failures, coverage = coverage_conclusion(
        results_dir, payloads, expected_coverage
    )
    verdict = (
        "**通过**"
        if coverage_status == "passed"
        else f"**未通过({len(coverage_failures)} 条)**"
    )
    lines += [
        f"- 结论: {verdict} —— 每个有用例结果的平台都要有一份达标的覆盖率结论。",
        "- 每个平台的结论行与百分比就在它自己的 `Coverage report` 项里(描述开头); "
        "低于门槛时那一项直接是失败。",
        "- 合计覆盖率按 coverage.py 的口径算: (行覆盖 + 分支覆盖) / (行总数 + 分支总数), "
        "与 `fail_under` 比的就是它。",
        "- 有意不统计的豁免(每条标记的原因)见报告首页「全局附件」的 "
        "`allure-coverage-exclusions.md`。",
        "",
        "| 平台 | 行覆盖率 | 分支覆盖率 | 合计 | 门槛 | 结论 | 原始报告 |",
        "| --- | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    lines += [
        f"| {_cell(platform)} | {_cell(line)} | {_cell(branch)} | {_cell(combined)} "
        f"| {_cell(threshold)} | {_cell(state)} "
        "| `coverage.xml`(见结论项 `Coverage report`) |"
        for platform, line, branch, combined, threshold, state in coverage
    ]
    if not coverage:
        lines.append("| (没有数据) | - | - | - | - | - | - |")
    lines += [
        "",
        "## 性能基准(只在 Linux 执行)",
        "",
        f"- {performance_note(performance)}",
        "- 原始数据 `performance-results.json` / `.csv` "
        "见结论项 `Performance baseline`。",
        "",
    ]
    bench_rows = performance_rows(performance)
    if bench_rows:
        lines += [
            "| 平台 | 基准数 | 未达标 | 结论 |",
            "| --- | ---: | ---: | --- |",
        ]
        lines += [
            f"| {_cell(platform)} | {total} | {failed} | {state} |"
            for platform, total, failed, state in bench_rows
        ]
    else:
        lines.append(
            "本次运行没有性能结果文件(结论项 `Performance baseline` 记为 **broken**)。"
        )
    lines += ["", "## 安全测试", ""]
    security = security_rows(results_dir, security_payloads)
    if security:
        lines += [
            "| 平台 | 结论条数 | 未拦截条数 | 失败用例 | 结论 | 原始结论 |",
            "| --- | ---: | ---: | ---: | --- | --- |",
        ]
        lines += [
            f"| {_cell(env)} | {_cell(total)} | {_cell(not_blocked)} | {_cell(failed)} "
            f"| {_cell(verdict)} "
            "| `security-results.json`(见结论项 `Security findings`) |"
            for env, total, not_blocked, failed, verdict in security
        ]
        lines += [
            "",
            "- 「结论条数」是安全用例给出的结论总数; 「未拦截条数」是期望被拦下、"
            "实际没拦住的条数(应为 **0**; 非 0 说明存在安全问题, 详情见对应结论项)。",
            "- 「失败用例」是该平台上真的失败的安全用例数(broken 也算) —— "
            "它本来只体现在 security 作业的状态上, 这里一起摆出来。",
        ]
    else:
        lines.append("本次运行没有安全结论文件。")
    lines += [
        "",
        *completeness_section(expectation, missing, results_dir, payloads),
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
    identity: str,
    category: str,
    title: str,
    description: str,
    attachments: list[dict[str, str]],
    platform: str,
    status: str = "passed",
    status_message: str = "",
    severity: str = SUMMARY_SEVERITY,
    os_label: str = "",
) -> None:
    """写入一条 Allure 结果(承载该类测试的汇总信息).

    ``identity`` 是结果的 ``fullName``(如 ``archive-management.security``): 调用处传常量而
    不是在这里拼字符串 —— 清单(``scripts/allure_catalog.py``)与守卫都靠这些常量对着源码
    核对"这次到底应该写出哪些身份"。``category`` 则同时当 ``layer`` / ``testCategory`` 标签
    与 historyId 的一部分。

    ``status`` 默认 ``passed``: 但汇总项必须能**体现失败** —— 例如安全用例红了一条,
    ``Security findings`` 却写 passed, 报告里就完全看不出问题(2026-09-25 的 Linux)。

    身份**不带平台**: 三个平台的同一类汇总(覆盖率/性能/安全)是"同一份摘要、分属三个环境",
    与报告里用例结果的写法一致 —— 平台由 ``env`` 标签(配合仓库根的 ``allurerc.mjs``)
    变成 Allure 的环境, 因此标题里也不再拼平台名; ``平台`` 参数与 ``os`` 标签是兼底
    (生成端没读到报告配置时, 环境会静默退回 ``default``)。

    严重等级默认 ``trivial``: 汇总项本身不验证任何行为, 只是把原始结论与附件
    带进报告; 不打等级的话报告里会多出一个 no_severity 桶。

    ``os_label`` 默认跟 ``platform`` 一样; 只有"缺失结论项"会传一个不同的值 —— 它并不来自
    哪台机器, 而 ``os`` 标签是脚本认"这次涉及哪些平台"的依据。
    """
    timestamp = time.time_ns() // 1_000_000
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(uuid.NAMESPACE_URL, f"archive-management-{category}")
        ),
        "fullName": identity,
        "name": title,
        "status": status,
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": "Test report"},
            {"name": "os", "value": os_label or platform},
            # 与环境维度对齐: 仓库根的 allurerc.mjs 用 env 标签把结果归到各平台的环境,
            # 缺了它这些汇总项只会出现在 default 环境里(按环境筛选时就看不到了)。
            {"name": "env", "value": platform},
            {"name": "feature", "value": title},
            {"name": "epic", "value": "工程与发布"},
            {"name": "story", "value": title},
            {"name": "layer", "value": category},
            {"name": "testCategory", "value": category},
            {"name": "severity", "value": severity},
        ],
        "parameters": [{"name": "平台", "value": platform}],
        "description": description,
        "attachments": attachments,
    }
    if status != "passed" and status_message:
        # 报告里结论项会直接显示这条消息(Allure 的失败项靠 statusDetails 讲原因)。
        result["statusDetails"] = {"message": status_message, "trace": ""}
    (results_dir / f"{result_id}-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _write_performance_result(
    results_dir: Path, performance: dict[str, Any] | None
) -> None:
    """写性能结论项: 缺失结果文件时写 broken, 而不是不写(不能静默地"没有这条")."""
    platform = platform_of(performance) if performance is not None else "unknown"
    result_id = str(uuid.uuid4())
    attachments: list[dict[str, str]] = []
    measurements: list[dict[str, Any]] = []
    if performance is None:
        status = "broken"
        failures = [
            f"缺少性能结果文件: {PERFORMANCE_JSON}"
            "(性能基准作业可能没有运行, 或产物没合并进来)"
        ]
        description = performance_digest(status, failures)
    else:
        status, failures = performance_verdict(performance)
        measurements = [
            item
            for item in performance.get("measurements", [])
            if isinstance(item, dict)
        ]
        attachments = [
            item
            for item in (
                write_attachment(results_dir, PERFORMANCE_JSON, result_id),
                write_attachment(results_dir, PERFORMANCE_CSV, result_id),
            )
            if item is not None
        ]
        description = (
            performance_digest(status, failures)
            + "\n"
            + performance_table(measurements)
        )
    write_result(
        results_dir,
        result_id=result_id,
        identity=producer("performance").identity,
        category="performance",
        title=producer("performance").title,
        description=description,
        attachments=attachments,
        platform=platform,
        status=status,
        status_message="; ".join(failures),
    )
    print(f"性能基准已写入 Allure: {len(measurements)} 条测量({platform}, {status})")


def _write_security_results(results_dir: Path) -> None:
    """逐平台写安全结论项(状态由未拦截结论与失败用例共同决定)."""
    for source in security_files():
        payload = read_json(source)
        if payload is None:
            continue
        findings = list(payload.get("findings", []))
        platform = platform_of(payload)
        result_id = str(uuid.uuid4())
        status, failures = security_verdict(results_dir, platform, findings)
        attachments = [
            item
            for item in (write_attachment(results_dir, source, result_id),)
            if item is not None
        ]
        write_result(
            results_dir,
            result_id=result_id,
            identity=producer("security").identity,
            category="security",
            title=producer("security").title,
            description=security_digest(status, failures)
            + "\n"
            + security_table(findings),
            attachments=attachments,
            platform=platform,
            status=status,
            status_message="; ".join(failures),
        )
        print(
            f"安全结论已写入 Allure: {len(findings)} 条结论({platform}, "
            f"{status}, 来源 {source})"
        )


def _write_coverage_exclusions() -> None:
    """写"有意不统计的覆盖"清单(报告首页「全局附件」的一份附件)."""
    COVERAGE_EXCLUSIONS_REPORT.write_text(
        coverage_exclusions_report(REPOSITORY_ROOT), encoding="utf-8"
    )
    print(f"覆盖率豁免清单已写入 {COVERAGE_EXCLUSIONS_REPORT}")


def _verdict_of(summary: Path) -> dict[str, Any] | None:
    """读某份摘要旁边的 ``verdict.json``(没有、或解析不了都返回 None)."""
    try:
        payload = json.loads(
            (summary.parent / FAILURE_DIAGNOSTICS_VERDICT).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _job_has_a_backstop_scene(summary: Path) -> bool:
    """这个作业的摘要是不是"有兜底现场"的.

    判定由 ``collect_job_diagnostics.py`` 写下的 ``verdict.json`` 给出 —— 去解析人读的文本
    太脆。没有这份 JSON(旧产物、或收集脚本自己挂了)时按"有现场"处理: 宁可多显示一段,
    也不要在最需要证据的时候把它藏起来。
    """
    payload = _verdict_of(summary)
    if payload is None:
        return True
    return bool(payload.get("scene", True))


@dataclass(frozen=True)
class CrashScene:
    """一个"有兜底现场"的作业(进程级崩溃 / 证据缺失)."""

    #: artifact 目录名(如 ``job-diagnostics-macos-latest-0``): 报告里拿它当节标题.
    owner: str
    #: 作业标签(``verdict.json`` 里的 ``label``; 没有时退回 artifact 名).
    label: str
    #: 报告里的平台**显示名**(Windows / macOS / Linux; 认不出来时是 ``common``).
    platform: str
    #: 判定理由(``verdict.json`` 的 ``reasons``).
    reasons: tuple[str, ...]
    #: 那份 ``summary.md`` 的路径.
    summary: Path


def crash_platform(owner: str, summary: Path) -> str:
    """崩溃的作业属于哪个平台(返回报告里的**显示名**).

    报告的环境靠 ``env`` 标签 + ``allurerc.mjs`` 的 matcher, 而标签值必须是显示名
    (Windows / macOS / Linux): 先看 artifact 名(``…-macos-latest-0``), 再看摘要内容的
    ``平台:`` 一行; 都认不出来时归 ``common`` —— 宁可不进某个平台的环境, 也不给错平台。
    """
    text = ""
    with contextlib.suppress(OSError):
        text = summary.read_text(encoding="utf-8")
    haystack = f"{owner} {text}".lower()
    for token, name in CRASH_PLATFORM_TOKENS:
        if token in haystack:
            return name
    return CRASH_PLATFORM_FALLBACK


def crash_scenes(directory: Path) -> list[CrashScene]:
    """找出"有兜底现场"的作业(目录不存在时返回空表).

    **三种目录结构都要认**, 而不是只认一种:

    - ``<artifact 名>/summary.md``: ``actions/upload-artifact`` 传 ``path: job-diagnostics/``
      时, artifact 里装的是那个目录的**内容**(``summary.md`` 在根), 下载到
      ``failure-diagnostics/<artifact 名>/`` 之后就还是这一层;
    - ``summary.md`` 直接在目录根: **pattern 只匹配到一个 artifact 时**, ``download-artifact``
      不建"以产物名命名的子目录", 把内容平铺进 ``path``(多个 artifact 才逐个建)。2026-10-05
      实测: 那一轮只有 ``job-diagnostics-macos-latest-0`` 一份, 平铺之后脚本按上一条布局找,
      一条都匹配不到 —— 汇总日志写着"没有兜底现场", 报告首页的"失败现场"附件永远不出现,
      一声不吭。平铺时 artifact 名已经丢了, 节标题退回 ``verdict.json`` 里的作业标签;
    - ``<artifact 名>/job-diagnostics/summary.md``: 上传时传的是父目录才会多出这一层。

    为什么特意全部都收: 2026-10-04 的脚本只认最后一种, 于是**永远匹配不到**; 2026-10-05
    修成只认第一种, 又被"单产物平铺"撞上。两次教训是同一句话: 目录布局由 CI 的上传/下载
    行为决定, 脚本认不全一种布局, 兜底现场就**静默**消失。守卫(test_ci_diagnostics.py)按
    每种布局都造样本, 再漏就不会两边一起绿。
    """
    candidates = sorted(directory.glob("summary.md"))
    candidates += sorted(directory.glob("*/summary.md"))
    candidates += sorted(directory.glob("*/*/summary.md"))
    scenes: list[CrashScene] = []
    for path in candidates:
        if not _job_has_a_backstop_scene(path):
            continue
        verdict = _verdict_of(path) or {}
        relative = path.relative_to(directory)
        # 平铺布局下没有子目录名(即 artifact 名)可用, 节标题退回作业标签 ——
        # 崩溃的是哪个作业这件事, 标签说得一样清楚。
        owner = (
            relative.parts[0]
            if len(relative.parts) > 1
            else str(verdict.get("label") or directory.name)
        )
        scenes.append(
            CrashScene(
                owner=owner,
                label=str(verdict.get("label") or owner),
                platform=crash_platform(owner, path),
                reasons=tuple(str(item) for item in verdict.get("reasons") or ()),
                summary=path,
            )
        )
    return scenes


def diagnostics_report_text(scenes: Sequence[CrashScene]) -> str:
    """把各作业的 ``summary.md`` 拼成一份报告首页的附件."""
    parts = [
        "# 失败现场(兜底)",
        "",
        '只列判定为"有兜底现场"的作业(进程级崩溃 / 证据缺失); 普通失败的证据在结果与运行'
        "日志里, 不在这里重复。",
        "",
    ]
    for scene in scenes:
        parts.append(f"## {scene.owner}")
        parts.append("")
        parts.append(scene.summary.read_text(encoding="utf-8").strip())
        parts.append("")
    return "\n".join(parts)


def failure_diagnostics_report(directory: Path) -> str | None:
    """附件正文(没有兜底现场时返回 None).

    细节见 :func:`crash_scenes`(怎么找)与 :func:`diagnostics_report_text`(怎么拼)。
    """
    scenes = crash_scenes(directory)
    return diagnostics_report_text(scenes) if scenes else None


def _write_crash_result(scene: CrashScene, results_dir: Path) -> None:
    """给崩溃的作业写一条 ``broken`` 结论项 —— 让它进得了统计与筛选.

    为什么要另写一条(用户 2026-10-05): 只挂首页附件的话, "这个作业是崩掉的"既不进任何
    计数, 也没法按环境/等级筛, 而它恰恰是最该被统计的一类。这里做三件事: 环境用崩溃作业
    所在的平台(切到那个平台就能看到它)、等级 ``critical``、``testCategory=diagnostics``。

    ``testCategory`` 不是 ``quality`` 也不是用例: 原生质量门那条"每个平台都要有真实用例"
    只认 ``framework=pytest``, 因此它不会被算成"这个平台测过了"(同一套判据见
    ``scripts/verify_allure_report.py``)。
    """
    result_id = str(uuid.uuid4())
    attachment = write_attachment(results_dir, scene.summary, result_id)
    if attachment is not None:
        attachment["type"] = "text/markdown"
    message = "\n".join([f"作业崩溃(兜底现场): {scene.label}", *scene.reasons])
    write_result(
        results_dir,
        result_id=result_id,
        identity=f"{CRASH_IDENTITY}{scene.owner}",
        category=CRASH_CATEGORY,
        title=f"作业崩溃: {scene.label}",
        description=(
            "## 作业崩溃\n\n"
            f"{message}\n\n"
            "## 怎么办\n\n"
            "- 先看摘要(附件)里的兜底判定与现场清单, 再按 `crash-dumps/*.ips` 的原生栈定位;\n"
            "- 用例级的证据在那些作业自己的 `allure-results` 里 —— 这一条只说明**作业**没跑完。\n"
        ),
        attachments=[attachment] if attachment is not None else [],
        platform=scene.platform,
        status="broken",
        status_message=message,
        severity=CRASH_SEVERITY,
        os_label=CRASH_OS_LABEL,
    )
    print(f"作业崩溃结论项已写入: {scene.label}(环境 {scene.platform})")


def _write_failure_diagnostics() -> None:
    """写"失败现场": 一份拼起来的附件 + 每个作业一条 ``broken`` 结论项.

    **没有兜底现场时不写, 并把上一次留下的那份删掉**(用户 2026-10-04/05 的要求: 一份永远
    存在的"失败现场"只会让人以为可能崩过, 点开才发现是空的), 也不写任何结论项。
    """
    scenes = crash_scenes(FAILURE_DIAGNOSTICS_DIRECTORY)
    if not scenes:
        FAILURE_DIAGNOSTICS_REPORT.unlink(missing_ok=True)
        print(
            f"没有兜底现场: 不生成 {FAILURE_DIAGNOSTICS_REPORT}(并清掉旧的), "
            "也不写任何结论项"
        )
        return
    FAILURE_DIAGNOSTICS_REPORT.write_text(
        diagnostics_report_text(scenes), encoding="utf-8"
    )
    print(f"失败现场已写入 {FAILURE_DIAGNOSTICS_REPORT}")
    for scene in scenes:
        _write_crash_result(scene, RESULTS_DIRECTORY)


def main(argv: Sequence[str] | None = None) -> int:
    """把性能/安全/覆盖率结论与环境信息写入 allure-results."""
    ensure_utf8_output()
    arguments = parse_args(argv)
    if arguments.write_failure_diagnostics:
        # 单独一个入口: 这一步必须跑在汇总作业"下载各作业诊断"之后, 而本脚本的主流程
        # 跑在它之前(见 --write-failure-diagnostics 的说明)。
        _write_failure_diagnostics()
        return 0
    results_dir = RESULTS_DIRECTORY
    if not results_dir.is_dir():
        print(f"Allure 结果目录不存在: {results_dir}", file=sys.stderr)
        return 1

    performance = read_json(PERFORMANCE_JSON)
    security_payloads = [
        payload for payload in (read_json(path) for path in security_files()) if payload
    ]

    # ``common`` 也算掉: 质量检查与缺失结论项落在这个环境里, 它不是"某个平台" ——
    # 否则重复跑一次汇总脚本时它会混进平台列表(``os`` 标签被当成平台)。
    platforms = sorted(
        {
            *tested_platforms(results_dir),
            *([platform_of(performance)] if performance is not None else []),
            *(platform_of(payload) for payload in security_payloads),
        }
        - {"unknown", "", "common"}
    )
    # 「应有」清单必须在写任何新结论项**之前**采好: 缺失项自己也会写成结论项, 采晚了就会把
    # 刚写下的那几条当成"已经有了"(身份前缀是第二道保险, 见 missing_conclusion_results)。
    # 反过来, 本脚本要写的性能/安全那两类由 self_written_items 补进"实有"—— 它们尚未落盘,
    # 但已经确定会落盘; 真正该盯的是它们的原始数据文件在不在。
    declared = list(arguments.platforms) or platforms
    expected = allure_catalog.expected_items(declared, SCRIPT_DIRECTORY)
    missing = allure_catalog.missing_items(
        expected,
        allure_catalog.actual_items(results_dir)
        | self_written_items(performance, security_payloads),
    )
    lines = environment_lines(
        performance, security_payloads[0] if security_payloads else None, platforms
    )
    (results_dir / ENVIRONMENT_FILENAME).write_text(
        "".join(f"{key}={value}\n" for key, value in lines), encoding="utf-8"
    )

    _write_performance_result(results_dir, performance)
    _write_security_results(results_dir)
    _write_coverage_exclusions()
    for message in missing_conclusion_results(results_dir, missing):
        # CI 日志里的告警行(汇总作业的平台用例检查也在用同一条通道): 报告本身是产物, 不点开
        # 看不到, 而"这次少了哪一节证据"值得在日志里就看见。
        print(f"::warning::{message}")

    if performance is None:
        print(
            f"未找到性能结果文件, 结论项记为 broken: {PERFORMANCE_JSON}",
            file=sys.stderr,
        )
    if not security_payloads:
        print(f"未找到安全结果文件, 已跳过: {SECURITY_JSON}")

    checks = quality_checks(results_dir)
    payloads = result_payloads(results_dir)
    QUALITY_GATE_REPORT.write_text(
        run_ledger(
            results_dir, payloads, performance, security_payloads, platforms, expected
        ),
        encoding="utf-8",
    )
    artifacts = artifact_rows(results_dir, payloads)
    missing_artifacts = [row for row in artifacts if row[3] != "已收录"]
    print(
        f"运行总账已写入 {QUALITY_GATE_REPORT}: 质量门 {len(checks)} 项, "
        f"产物 {len(artifacts)} 个(缺失 {len(missing_artifacts)} 个), "
        f"结论项应有 {len(expected)} 项(缺 {len(missing)} 项)"
    )
    for owner, name, _size, state in missing_artifacts:
        print(f"警告: 原始产物不可用 —— {owner} 的 {name}({state})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
