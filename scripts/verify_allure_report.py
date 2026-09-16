"""校验 Allure 报告的静态资源是否打包齐全.

报告是一套静态站点: 用例详情页打开时才去取 ``data/test-results/<结果 id>.json``.
只要这个目录在发布、传输或解压环节被丢掉, 报告就只剩汇总数字与用例树 --
界面能看到用例通过与否, 点开用例却是空的(2026-09-16 的 CI artifact 就是这个症状).
本脚本把这个隐式依赖变成显式校验, 供 CI 发布前与本地下载 artifact 后各跑一次.

校验内容:

1. 必需资源: ``index.html`` / 单个 ``app-*.js`` / ``summary.json`` /
   ``test-results.json`` / ``widgets/**/statistic.json`` / ``widgets/**/tree.json``;
2. 结果索引(``test-results.json`` 的 ``byId``)里每个结果都要有详情文件
   ``data/test-results/<id>.json``, 且条数与 ``allure-results`` 里的结果文件一致;
3. 用例分组(``data/test-env-groups/*.json``)引用的结果 id 都能在索引里找到;
4. ``--zip`` 额外把报告打成单个 ``<报告目录>.zip``: 单个文件发布不会出现"整个目录
   被悄悄丢掉"的静默损坏, 并打印条目数与 SHA256 便于人工核对.

报告目录不存在视为"本次没有结果, 未生成报告"(退出码 0); 其余情况缺资源即退出码 1.

用法:

- CI: ``uv run python scripts/verify_allure_report.py allure-report --results allure-results --zip``
- 本地核对下载的 artifact: ``uv run python scripts/verify_allure_report.py allure-report``
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import sys
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

DETAIL_DIRECTORY = Path("data") / "test-results"
GROUP_DIRECTORY = Path("data") / "test-env-groups"
REQUIRED_FILES = ("index.html", "summary.json", "test-results.json")

# 报告里的 JSON 对象(只做按键取值, 具体字段仍逐个校验类型).
JsonObject = dict[str, object]
# 每个控件数据都可能同时存在 ``widgets/<name>.json``(无环境)与
# ``widgets/<环境>/<name>.json``(按环境), 两处任意一处存在即可.
REQUIRED_WIDGETS = ("statistic.json", "tree.json")
RESULT_FILE_SUFFIX = "-result.json"
# 输出里最多列出这么多条缺失文件名, 其余用总数概括, 避免刷屏.
MAX_REPORTED_MISSING = 5


@dataclass(frozen=True)
class ReportFacts:
    """报告目录的可计数事实(写进日志, 便于跨运行比较)."""

    indexed_results: int
    detail_files: int
    env_groups: int
    result_files: int | None
    archive_entries: int | None = None


def load_object(path: Path) -> JsonObject | None:
    """读取 JSON 对象; 文件不存在、无法解析或顶层不是对象时返回 None."""
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def indexed_result_ids(report_dir: Path) -> set[str] | None:
    """返回结果索引里的结果 id 集合; 索引缺失或损坏时返回 None."""
    payload = load_object(report_dir / "test-results.json")
    if payload is None:
        return None
    by_id = payload.get("byId")
    if not isinstance(by_id, dict):
        return None
    return {str(key) for key in by_id}


def grouped_result_ids(report_dir: Path) -> set[str]:
    """返回用例分组引用的全部结果 id(分组目录缺失时为空集合)."""
    referenced: set[str] = set()
    for path in sorted((report_dir / GROUP_DIRECTORY).glob("*.json")):
        payload = load_object(path)
        if payload is None:
            continue
        by_env = payload.get("testResultsByEnv")
        if isinstance(by_env, dict):
            referenced.update(str(value) for value in by_env.values())
    return referenced


def count_result_files(results_dir: Path | None) -> int | None:
    """统计 allure-results 里的结果文件数; 未提供目录时返回 None."""
    if results_dir is None:
        return None
    return sum(1 for _ in results_dir.glob(f"*{RESULT_FILE_SUFFIX}"))


def static_problems(report_dir: Path) -> list[str]:
    """校验报告入口文件与控件数据(缺少这些连汇总都渲染不出来)."""
    problems = [
        f"缺少 {name}" for name in REQUIRED_FILES if not (report_dir / name).is_file()
    ]
    bundles = sorted(report_dir.glob("app-*.js"))
    if len(bundles) != 1:
        problems.append(f"app-*.js 数量异常: {len(bundles)}(应为 1)")
    problems.extend(missing_widget_problems(report_dir))
    return problems


def missing_widget_problems(report_dir: Path) -> list[str]:
    """列出缺失的控件数据(每个控件都同时存在无环境与按环境两种路径)."""
    widgets = report_dir / "widgets"
    return [
        f"缺少 widgets/**/{name}"
        for name in REQUIRED_WIDGETS
        if not (widgets / name).is_file() and not list(widgets.glob(f"*/{name}"))
    ]


def detail_filenames(report_dir: Path) -> int:
    """返回详情目录里的文件数(目录缺失时返回 0)."""
    return sum(1 for _ in (report_dir / DETAIL_DIRECTORY).glob("*.json"))


def missing_detail_problems(report_dir: Path, expected: set[str]) -> list[str]:
    """列出索引里有、磁盘上没有的详情文件(报告详情页的硬依赖)."""
    directory = report_dir / DETAIL_DIRECTORY
    missing = sorted(
        name for name in expected if not (directory / f"{name}.json").is_file()
    )
    if not missing:
        return []
    shown = ", ".join(missing[:MAX_REPORTED_MISSING])
    return [
        f"缺少 {len(missing)} 个用例详情文件 data/test-results/*.json "
        f"(报告只能显示通过与否, 点开用例是空的); 例如: {shown}"
    ]


def group_reference_problems(
    report_dir: Path, expected: set[str], grouped: set[str]
) -> list[str]:
    """校验分组引用与结果索引一致(不一致说明报告数据被截断过)."""
    dangling = sorted(grouped - expected)
    if not dangling:
        return []
    shown = ", ".join(dangling[:MAX_REPORTED_MISSING])
    return [
        f"{report_dir / GROUP_DIRECTORY} 里有 {len(dangling)} 个结果 id "
        f"不在结果索引中; 例如: {shown}"
    ]


def verify_report(
    report_dir: Path, results_dir: Path | None = None
) -> tuple[ReportFacts | None, list[str]]:
    """校验报告完整性: 返回 (计数事实, 问题清单); 报告不存在时返回 (None, [])."""
    if not report_dir.is_dir():
        return None, []
    problems = static_problems(report_dir)
    expected = indexed_result_ids(report_dir)
    if expected is None:
        problems.append("无法解析 test-results.json 的结果索引(byId)")
        expected = set()
    problems.extend(missing_detail_problems(report_dir, expected))
    problems.extend(
        group_reference_problems(report_dir, expected, grouped_result_ids(report_dir))
    )
    result_files = count_result_files(results_dir)
    if result_files is not None and result_files != len(expected):
        problems.append(
            f"结果数不一致: allure-results 有 {result_files} 个结果文件, "
            f"报告索引只有 {len(expected)} 条(合并或生成阶段掉过数据)"
        )
    facts = ReportFacts(
        indexed_results=len(expected),
        detail_files=detail_filenames(report_dir),
        env_groups=sum(1 for _ in (report_dir / GROUP_DIRECTORY).glob("*.json")),
        result_files=result_files,
    )
    return facts, problems


def sha256_of(path: Path) -> str:
    """返回文件的 SHA256(用于核对下载到的 artifact 与 CI 产出是否一致)."""
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def package_report(report_dir: Path, archive: Path) -> tuple[int, str]:
    """把报告目录打成单个 zip, 返回 (条目数, SHA256).

    单个文件发布可以避免"整个 ``data/test-results`` 目录在传输/解压时被静默丢掉"
    这类损坏: 文件要么完整到达, 要么直接报错。
    """
    files = [path for path in sorted(report_dir.rglob("*")) if path.is_file()]
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in files:
            bundle.write(path, Path(report_dir.name) / path.relative_to(report_dir))
    with zipfile.ZipFile(archive) as bundle:
        entries = len(bundle.namelist())
    if entries != len(files):
        raise ValueError(
            f"打包不完整: 目录内 {len(files)} 个文件, zip 里只有 {entries} 个"
        )
    return entries, sha256_of(archive)


def report_facts(facts: ReportFacts, report_dir: Path) -> None:
    """打印计数事实, 让日志本身就能回答"报告是不是完整的"."""
    results_text = "未提供" if facts.result_files is None else str(facts.result_files)
    print(f"报告目录: {report_dir}")
    print(f"结果索引: {facts.indexed_results} 条")
    print(f"详情文件: {facts.detail_files} 个 (data/test-results)")
    print(f"用例分组: {facts.env_groups} 个 (data/test-env-groups)")
    print(f"allure-results 结果文件: {results_text}")


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8.

    Windows(尤其是英文版 CI runner)的控制台默认是 cp1252, 直接打印中文会抛
    ``UnicodeEncodeError`` 把整个校验打断 —— 2026-09-17 的 CI 就是在这里挂的。
    改成 UTF-8(并容错替换)即可; pytest 的 capsys 等替身没有 ``reconfigure``,
    取不到就跳过。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """解析命令行参数."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        nargs="?",
        default="allure-report",
        help="报告目录(默认 allure-report)",
    )
    parser.add_argument(
        "--results",
        type=Path,
        default=None,
        help="allure-results 目录, 用于核对结果条数",
    )
    parser.add_argument(
        "--zip",
        action="store_true",
        help="校验通过后把报告打包成 <报告目录>.zip",
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口: 校验报告, 可选用 ``--zip`` 打包后再发布."""
    ensure_utf8_output()
    args = parse_args(argv)
    report_dir = Path(args.report)
    facts, problems = verify_report(report_dir, args.results)
    if facts is None:
        print(f"报告目录不存在, 视为本次没有结果, 跳过校验: {report_dir}")
        return 0
    report_facts(facts, report_dir)
    if problems:
        for problem in problems:
            print(f"报告资源不完整: {problem}", file=sys.stderr)
        return 1
    if args.zip:
        archive = report_dir.with_suffix(".zip")
        entries, digest = package_report(report_dir, archive)
        size_mib = archive.stat().st_size / (1024 * 1024)
        print(f"已打包: {archive} ({entries} 个条目, {size_mib:.1f} MiB)")
        print(f"SHA256: {digest}")
    print("报告资源完整: 用例详情可正常打开")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
