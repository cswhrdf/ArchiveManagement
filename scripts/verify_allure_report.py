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
4. 环境列表(``widgets/environments.json``)里有非 ``default`` 的环境: 平台是靠
   ``env`` 标签 + 仓库根的 ``allurerc.mjs`` 变成 Allure 环境的, 生成时没读到配置
   就会静默退回单个 ``default`` 环境(详见 :func:`environment_problems`);
5. 结果文件声明的**附件**都在(覆盖率/性能/安全/质量汇总项把原始报告挂在条目上,
   附件文件与结果文件同在 ``allure-results`` 里, 详见 :func:`attachment_problems`);
6. ``--expect-platforms`` 指定的每个平台环境里都要有**真实用例**结果 —— 覆盖率/安全/质量
   这些脚本生成的汇总项也带平台的 ``env``, 只看"环境存在"会把它们当成"这个平台测过了"
   (详见 :func:`platform_test_problems`; pytest 作业传自己那个平台, 汇总作业传三个平台);
7. ``--zip`` 额外把报告打成单个 ``<报告目录>.zip``: 单个文件发布不会出现"整个目录
   被悄悄丢掉"的静默损坏, 并打印条目数与 SHA256 便于人工核对.

报告目录不存在视为"本次没有结果, 未生成报告"(退出码 0); 其余情况缺资源即退出码 1.

用法:

- CI(pytest-report 作业): ``uv run python scripts/verify_allure_report.py allure-report
  --results allure-results --zip --expect-platforms "${{ matrix.platform }}"``
- CI(allure-summary 作业): 同上, 但 ``--expect-platforms Windows,macOS,Linux``
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
# 环境列表: 报告里有非 default 的环境, 才说明平台真的成了 Allure 3 的"环境"维度.
ENVIRONMENTS_WIDGET = "environments.json"
DEFAULT_ENVIRONMENT = "default"

# 报告里的 JSON 对象(只做按键取值, 具体字段仍逐个校验类型).
JsonObject = dict[str, object]
# 每个控件数据都可能同时存在 ``widgets/<name>.json``(无环境)与
# ``widgets/<环境>/<name>.json``(按环境), 两处任意一处存在即可.
REQUIRED_WIDGETS = ("statistic.json", "tree.json")
RESULT_FILE_SUFFIX = "-result.json"
# "真实用例"的判据: ``framework`` 标签由 allure-pytest 自己写, 脚本生成的汇总项
# (覆盖率/性能/安全/质量检查)一律不带。必须与仓库根 ``allurerc.mjs`` 里质量门规则集的
# ``filter`` 判据一致 —— 两处都在回答"这条结果是用例还是脚本产物",
# tests/unit/test_report_verification.py 有守卫把两处钉在一起。
REAL_TEST_LABEL = "framework"
REAL_TEST_VALUE = "pytest"
# 输出里最多列出这么多条缺失文件名, 其余用总数概括, 避免刷屏.
MAX_REPORTED_MISSING = 5


@dataclass(frozen=True)
class ReportFacts:
    """报告目录的可计数事实(写进日志, 便于跨运行比较)."""

    indexed_results: int
    detail_files: int
    env_groups: int
    result_files: int | None
    environments: tuple[str, ...] = ()
    attachments: int | None = None
    archive_entries: int | None = None
    # 逐平台的 (环境名, 用例条数, 汇总项条数); 没要求 ``--expect-platforms`` 时为空。
    tests_by_environment: tuple[tuple[str, int, int], ...] = ()
    # 各平台分片作业自报的产物清单; 没传 ``--manifest`` 时为空。
    manifests: tuple[ManifestFacts, ...] = ()


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


def declared_attachments(results_dir: Path | None) -> list[Path]:
    """列出结果文件里声明的附件文件路径(按路径去重排序; 未提供目录时为空)."""
    if results_dir is None or not results_dir.is_dir():
        return []
    found: set[Path] = set()
    for path in sorted(results_dir.glob(f"*{RESULT_FILE_SUFFIX}")):
        payload = load_object(path)
        if payload is None:
            continue
        entries = payload.get("attachments")
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if isinstance(entry, dict):
                source = entry.get("source")
                if isinstance(source, str) and source.strip():
                    found.add(results_dir / source)
    return sorted(found)


def attachment_problems(declared: Sequence[Path]) -> list[str]:
    """要求结果里声明的附件真的存在.

    覆盖率/性能/安全/质量的汇总项都把原始报告作为**附件**挂在条目上; 附件文件与
    结果文件同在 ``allure-results`` 里, 一旦在打包或下载环节被丢掉, 报告里只会剩下
    一个打不开的附件 —— 与"详情页变空"是同一类静默损坏, 因此在这里显式校验。
    """
    missing = [path for path in declared if not path.is_file()]
    if not missing:
        return []
    shown = ", ".join(path.name for path in missing[:MAX_REPORTED_MISSING])
    return [
        f"缺少 {len(missing)} 个结果附件(allure-results 里的结果引用了它们): "
        f"例如: {shown}"
    ]


def environment_entries(report_dir: Path) -> list[tuple[str, str]] | None:
    """返回报告里的 (环境 id, 显示名) 列表; 文件缺失或无法解析时返回 None."""
    path = report_dir / "widgets" / ENVIRONMENTS_WIDGET
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, list):
        return None
    return [
        (str(entry["id"]), str(entry.get("name") or entry["id"]))
        for entry in payload
        if isinstance(entry, dict) and entry.get("id") is not None
    ]


def environment_ids(report_dir: Path) -> set[str] | None:
    """返回报告里的环境 id 集合; 文件缺失或无法解析时返回 None."""
    entries = environment_entries(report_dir)
    return None if entries is None else {env_id for env_id, _ in entries}


def environment_alias_lookup(report_dir: Path) -> dict[str, str]:
    """{环境 id / 显示名(大小写不敏感): 显示名} —— 结果上的 ``environment`` 两种都可能写."""
    lookup: dict[str, str] = {}
    for env_id, name in environment_entries(report_dir) or []:
        lookup[env_id.lower()] = name
        lookup[name.lower()] = name
    return lookup


def indexed_environments(report_dir: Path) -> dict[str, str]:
    """返回结果 id → 结果索引里记的环境(原始取值, 未归一化)."""
    payload = load_object(report_dir / "test-results.json")
    if payload is None:
        return {}
    by_id = payload.get("byId")
    if not isinstance(by_id, dict):
        return {}
    return {
        str(key): str(entry["environment"])
        for key, entry in by_id.items()
        if isinstance(entry, dict) and isinstance(entry.get("environment"), str)
    }


def is_real_test(report_dir: Path, result_id: str) -> bool:
    """这条结果是不是真实用例(详情文件里带 allure-pytest 写的 ``framework=pytest``)."""
    payload = load_object(report_dir / DETAIL_DIRECTORY / f"{result_id}.json")
    if payload is None:
        return False
    labels = payload.get("labels")
    if not isinstance(labels, list):
        return False
    return any(
        isinstance(label, dict)
        and label.get("name") == REAL_TEST_LABEL
        and label.get("value") == REAL_TEST_VALUE
        for label in labels
    )


def platform_test_counts(
    report_dir: Path, platforms: Sequence[str]
) -> dict[str, tuple[int, int]]:
    """统计指定各平台环境里的 (真实用例条数, 脚本生成的汇总项条数).

    只读这些平台的详情文件: 校验脚本没必要为了一个数字去解析整份报告(三平台合计三千多
    条结果时, 全部读一遍也要几秒)。
    """
    if not platforms:
        return {}
    wanted = {name.lower(): name for name in platforms}
    lookup = environment_alias_lookup(report_dir)
    tests = dict.fromkeys(platforms, 0)
    summaries = dict.fromkeys(platforms, 0)
    for result_id, raw in indexed_environments(report_dir).items():
        name = wanted.get(lookup.get(raw.lower(), raw).lower())
        if name is None:
            continue
        if is_real_test(report_dir, result_id):
            tests[name] += 1
        else:
            summaries[name] += 1
    return {name: (tests[name], summaries[name]) for name in platforms}


@dataclass(frozen=True)
class ManifestFacts:
    """一份产物清单(分片作业里的合并脚本写的): 分片条数与缺失分片."""

    path: Path
    platform: str
    results: int
    files: int
    shards: int
    missing_shards: tuple[str, ...]

    @property
    def label(self) -> str:
        """日志里怎么称呼这份清单(有平台名就用它, 否则用文件名)."""
        return self.platform or self.path.name


def _match_manifest_paths(pattern: str) -> list[Path]:
    """展开产物清单的模式.

    绝对模式要拆成"锚点 + 相对模式"才能交给 :meth:`Path.glob`(它不接受绝对模式)。
    与 ``merge_allure_results.py`` 里的展开逻辑同一套 —— scripts 不是包, 所以各留一份。
    """
    path = Path(pattern)
    anchor = path.anchor
    if anchor:
        candidates = Path(anchor).glob(str(path.relative_to(anchor)))
    else:
        candidates = Path().glob(pattern)
    return sorted(candidate for candidate in candidates if candidate.is_file())


def as_int(value: object) -> int:
    """把清单里的数值安全地转成 int(缺失或类型不对就当 0)."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def as_str_tuple(value: object) -> tuple[str, ...]:
    """把清单里的字符串数组安全地转成元组(类型不对就当空)."""
    if not isinstance(value, list):
        return ()
    return tuple(str(item) for item in value)


def load_manifests(pattern: str) -> tuple[tuple[ManifestFacts, ...], list[str]]:
    """读取产物清单(``--manifest`` 给的通配模式), 返回 (清单, 问题).

    模式匹配不到文件也算问题: 清单没上传/没下载到时, "少一片"同样看不出来 ——
    而这正是这些清单存在的理由。
    """
    if not pattern:
        return (), []
    paths = _match_manifest_paths(pattern)
    if not paths:
        return (), [
            f"没找到产物清单: {pattern}"
            "(分片作业的 --manifest 没上传或没下载到, 那样缺片就看不出来了)"
        ]
    facts: list[ManifestFacts] = []
    problems: list[str] = []
    for path in paths:
        payload = load_object(path)
        if payload is None:
            problems.append(f"产物清单无法解析: {path}")
            continue
        sources = payload.get("sources")
        facts.append(
            ManifestFacts(
                path=path,
                platform=str(payload.get("platform") or ""),
                results=as_int(payload.get("total_results")),
                files=as_int(payload.get("total_files")),
                shards=len(sources) if isinstance(sources, list) else 0,
                missing_shards=as_str_tuple(payload.get("missing_shards")),
            )
        )
    return tuple(facts), problems


def manifest_problems(
    manifests: Sequence[ManifestFacts],
    *,
    result_files: int | None,
    counts: dict[str, tuple[int, int]],
) -> list[str]:
    """把"分片自报的条数"与"最终收集到的条数"对齐(这一项就是方案 D).

    ``--expect-platforms`` 只能证明"这个平台有用例结果", 证明不了"三个分片都到齐了":
    少一片时环境、通过率、格式自检全都正常, 报告只是安静地少一部分用例(2026-09-21 实测:
    删掉 Linux 的 1166 条用例后, 不过滤的 ``environmentsTested`` 照样通过)。产物清单
    (``merge_allure_results.py`` 写、报告作业上传) 在这里对上三件事:

    1. 声明必须有的片号都到了 —— 缺片只有这里能看出来;
    2. 各分片自报的结果数**不超过**最终结果目录里的结果文件数(超了说明合并之后掉过数据);
    3. 报告里每个平台的用例数**不少于**该平台分片自报的结果数(少了两者必有一个不对)。
    """
    problems: list[str] = [
        f"{manifest.label} 缺少分片 {', '.join(manifest.missing_shards)} 的结果"
        "(分片作业的产物没上传或没合并进来, 该平台会少一部分用例)"
        for manifest in manifests
        if manifest.missing_shards
    ]
    declared = sum(manifest.results for manifest in manifests)
    if manifests and result_files is not None and declared > result_files:
        problems.append(
            f"结果数与产物清单对不上: 各分片自报共 {declared} 条结果, "
            f"而结果目录里只有 {result_files} 个(合并之后掉过数据)"
        )
    for manifest in manifests:
        if not manifest.label or manifest.label not in counts:
            continue
        tests = counts[manifest.label][0]
        if tests < manifest.results:
            problems.append(
                f"{manifest.label} 的报告里只有 {tests} 条用例, 少于分片自报的 "
                f"{manifest.results} 条(报告生成时掉过数据)"
            )
    return problems


def platform_test_problems(
    counts: dict[str, tuple[int, int]], platforms: Sequence[str]
) -> list[str]:
    """要求每个指定平台的环境里都有真实用例结果(脚本生成的汇总项不算).

    "环境存在"很容易被非用例的东西满足: 覆盖率/性能/安全汇总项、平台专属的质量检查都带
    平台的 ``env``, 于是"某个平台的用例全没合并进来"在报告里看不出来(2026-09-21 用真实
    数据验过: 删掉 Linux 的 1166 条用例后, 不过滤的 ``environmentsTested`` 照样通过)。
    质量门那边用规则集上的 ``filter`` 表达同一件事, 这里再补一道是为了能**逐个平台报数**
    (日志里直接看到 "Linux 1166 用例 / 3 汇总项"), 也方便在 pytest 作业里按单个平台自查。
    """
    rows = [(name, counts.get(name, (0, 0))) for name in platforms]
    missing = [name for name, (tests, _) in rows if tests == 0]
    if not missing:
        return []
    detail = "; ".join(
        f"{name}: {tests} 用例 / {summaries} 汇总项"
        for name, (tests, summaries) in rows
    )
    return [
        f"这些平台里没有用例结果: {', '.join(missing)} "
        f"(脚本生成的汇总项不算用例; 逐平台: {detail})"
    ]


def environment_problems(report_dir: Path) -> list[str]:
    """要求报告里存在非 default 的环境, 否则环境维度是静默失效的.

    ``env`` 标签要变成 Allure 的环境, 需要生成报告时读到仓库根的 ``allurerc.mjs``;
    一旦没读到(例如在别的目录执行 ``allure generate``), 所有结果都会落回隐式的
    ``default``, 三平台结果又退化成"只能从参数/套件名里认平台", 而且**不会有任何
    报错** —— 所以这里把它当成报告不完整。
    """
    ids = environment_ids(report_dir)
    if ids is None:
        return [f"缺少或无法解析 widgets/{ENVIRONMENTS_WIDGET}"]
    if ids - {DEFAULT_ENVIRONMENT}:
        return []
    return [
        "报告里只有 default 环境: 生成时没读到仓库根的 allurerc.mjs"
        "(或结果缺 env 标签), 多平台结果会退化成只能靠参数/套件名辨认"
    ]


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
    report_dir: Path,
    results_dir: Path | None = None,
    *,
    expected_platforms: Sequence[str] = (),
    manifest_pattern: str = "",
) -> tuple[ReportFacts | None, list[str]]:
    """校验报告完整性: 返回 (计数事实, 问题清单); 报告不存在时返回 (None, []).

    ``expected_platforms`` 指定哪些平台环境里必须各有真实用例结果(见
    :func:`platform_test_problems`); 不传就不做这项检查(pytest 作业传自己那个平台,
    汇总作业传三个平台)。``manifest_pattern`` 是产物清单的通配模式, 用来对齐"分片自报的
    条数"与"最终收集到的条数"(见 :func:`manifest_problems`)。
    """
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
    problems.extend(environment_problems(report_dir))
    counts = platform_test_counts(report_dir, expected_platforms)
    problems.extend(platform_test_problems(counts, expected_platforms))
    manifests, manifest_load_problems = load_manifests(manifest_pattern)
    problems.extend(manifest_load_problems)
    declared = declared_attachments(results_dir)
    problems.extend(attachment_problems(declared))
    result_files = count_result_files(results_dir)
    problems.extend(
        manifest_problems(manifests, result_files=result_files, counts=counts)
    )
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
        environments=tuple(sorted(environment_ids(report_dir) or ())),
        attachments=None if results_dir is None else len(declared),
        tests_by_environment=tuple(
            (name, tests, summaries) for name, (tests, summaries) in counts.items()
        ),
        manifests=manifests,
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
    attachments_text = "未提供" if facts.attachments is None else str(facts.attachments)
    print(f"报告目录: {report_dir}")
    print(f"结果索引: {facts.indexed_results} 条")
    print(f"详情文件: {facts.detail_files} 个 (data/test-results)")
    print(f"用例分组: {facts.env_groups} 个 (data/test-env-groups)")
    print(f"环境: {', '.join(facts.environments) or '未识别'}")
    print(f"结果附件: {attachments_text} 个")
    print(f"allure-results 结果文件: {results_text}")
    if facts.tests_by_environment:
        # 逐平台的用例/汇总项构成: “某个平台只剩下汇总项”在数字上是一眼可见的。
        rows = ", ".join(
            f"{name} {tests} 用例 + {summaries} 汇总项"
            for name, tests, summaries in facts.tests_by_environment
        )
        print(f"按平台用例: {rows}")
    if facts.manifests:
        # 分片自报的条数: “少一片”在数字上是一眼可见的(环境与通过率都看不出来)。
        rows = ", ".join(
            f"{manifest.label} {manifest.results} 条结果({manifest.shards} 片)"
            + (
                f", 缺片 {'/'.join(manifest.missing_shards)}"
                if manifest.missing_shards
                else ""
            )
            for manifest in facts.manifests
        )
        print(f"分片产物清单: {rows}")


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
    parser.add_argument(
        "--expect-platforms",
        default="",
        help=(
            "要求这些环境里各有至少一条真实用例结果(逗号分隔); "
            "pytest 作业传自己那个平台, 汇总作业传 Windows,macOS,Linux"
        ),
    )
    parser.add_argument(
        "--manifest",
        default="",
        help=(
            "产物清单(合并脚本写的 JSON)的通配模式, 如 allure-manifest.json 或 "
            'allure-manifests/*.json; 用来对齐"分片自报的条数"与最终条数'
        ),
    )
    return parser.parse_args(argv)


def expected_platforms(args: argparse.Namespace) -> tuple[str, ...]:
    """把 ``--expect-platforms`` 拆成平台名(空值表示不做这项检查)."""
    return tuple(
        name.strip() for name in str(args.expect_platforms).split(",") if name.strip()
    )


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口: 校验报告, 可选用 ``--zip`` 打包后再发布."""
    ensure_utf8_output()
    args = parse_args(argv)
    report_dir = Path(args.report)
    facts, problems = verify_report(
        report_dir,
        args.results,
        expected_platforms=expected_platforms(args),
        manifest_pattern=str(args.manifest),
    )
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
