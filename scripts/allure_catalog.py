"""这次运行**应该**产出哪些汇总结论 —— 一份从产出方代码同步出来的清单.

## 为什么需要它

运行总账过去只渲染"手里有什么": 有性能结果文件就画性能一行, 没有就写一句"本次运行没有性能
结果"。于是**产物没产出/没上传/没下载**这类缺失在报告里根本看不出来 —— 报告永远是"完整的",
只是安静地少了一节(2026-10-02 漏掉视觉回归产物与 `test` 组就是这么过去的)。这里把
"应该有什么"变成可核对的数据, 总账据此给出"应有 vs 实有", 并给每个缺失项写一条 broken
结论项(报告里一眼可见, 原生质量门也会跟着红)。

## 怎么"自动同步"

清单**不是**手写的一张大表, 而是尽量从产出方推出来:

- 质量检查项从 ``scripts/create_allure_quality.py`` 的 ``CHECKS`` 里**解析**出来(AST, 不导入
  —— 那个脚本要跑 ruff/mypy, 而汇总作业一行依赖都没装);
- 每个家族只声明自己的: 身份(结果里的 ``fullName`` 或标签判据)、范围(公共 / 每平台)、
  **预期产物名**、产出脚本、上传它的 CI 作业、以及由谁判定它"在不在";
- :func:`identity_gaps` 反向扫 ``scripts/create_allure_*.py``, 把代码里出现、清单里却查不到的
  身份报出来 —— 于是"新加一类结论"必然会被守卫拦下, 不会静默漏掉。

## 谁负责判定

每条结论只由**一个**角色判定, 职责不重叠: ``GATE_SUMMARY`` 由本清单 + 运行总账判定(缺了就写
broken 结论项); ``GATE_VERIFY`` 交给 ``scripts/verify_allure_report.py`` 的报告自检(平台用例
那条 —— 它已经在 CI 里按 ``--expect-platforms`` 逐平台对数了, 这里只负责把它**列出来**)。
"""

from __future__ import annotations

import ast
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: 脚本产出的汇总结论项的身份前缀(见各脚本的 ``fullName``).
IDENTITY_PREFIX = "archive-management."
#: "缺少结论"那类结果的身份前缀。它记的是**缺了什么**, 不是一个预期会出现的结论 ——
#: 所以它不参与"应有/实有"的比对(对比时它既不会满足任何预期项, 同步守卫也跳过它)。
#: 使用它的地方: ``scripts/create_allure_summary.py`` 为每个缺失项写一条 broken 结论项。
ABSENCE_IDENTITY = f"{IDENTITY_PREFIX}missing."
#: 结果文件名后缀(`<uuid>-result.json`).
RESULT_FILE_SUFFIX = "-result.json"
#: allure-pytest 自己打的框架标签: 用它把"真实用例"与"脚本产出的汇总结论项"分开.
FRAMEWORK_LABEL = "framework"
FRAMEWORK_VALUE = "pytest"
#: 结果归到哪个 Allure 环境(仓库根的 allurerc.mjs 按它分环境).
ENV_LABEL = "env"

#: 清单里"每平台一项"的家族按声明的平台展开(见 :func:`expected_items`)。
COMMON = "common"
#: 每个有用例结果的平台都该有一份.
#: (平台专属检查落在哪个环境里不写在这里: 那是 ``create_allure_quality.py`` 的事实, 由
#: :func:`platform_environments` 现场解析 —— 它加一个平台分支, 清单里就自动多出"应有"。)
PER_PLATFORM = "platform"

# -- 谁判定 "在不在" ---------------------------------------------------------
#: 本清单 + 运行总账判定(缺了就写 broken 结论项).
GATE_SUMMARY = "summary"
#: 交给 scripts/verify_allure_report.py 的自检判定(它已经在 CI 里按平台对数).
GATE_VERIFY = "verify"

#: 身份怎么认.
#: - ``exact``: 结果的 ``fullName`` 与身份相等(覆盖率/性能/安全这类一条一项的);
#: - ``prefix``: ``fullName`` 以身份开头(视觉回归每张画面一条、质量检查每项一条);
#: - ``label``: 认标签(真实用例只有 ``framework=pytest`` 这一个判据).
MATCH_EXACT = "exact"
MATCH_PREFIX = "prefix"
MATCH_LABEL = "label"


@dataclass(frozen=True)
class Producer:
    """一类汇总结论的产出方、它的预期产物、以及由谁判定它"在不在"."""

    key: str
    title: str
    identity: str
    scope: str
    artifact: str
    script: str
    job: str
    match: str = MATCH_EXACT
    gate: str = GATE_SUMMARY
    detail: str = ""


#: 期望清单. 顺序即总账里的行序(先"用例", 再"脚本产出的结论")。
CATALOG: tuple[Producer, ...] = (
    Producer(
        key="tests",
        title="平台用例结果",
        identity=FRAMEWORK_VALUE,
        scope=PER_PLATFORM,
        artifact="allure-results-<os>-<片>",
        script="tests/conftest.py",
        job="pytest",
        match=MATCH_LABEL,
        gate=GATE_VERIFY,
        detail="真实用例(带 framework=pytest 标签), 由各分片作业上传、报告作业合并",
    ),
    Producer(
        key="coverage",
        title="Coverage report",
        identity=f"{IDENTITY_PREFIX}coverage",
        scope=PER_PLATFORM,
        artifact="coverage-data-<os>-<片> → coverage.xml",
        script="scripts/create_allure_coverage.py",
        job="pytest-report",
        detail="每个平台的覆盖率结论与原始 coverage.xml",
    ),
    Producer(
        key="performance",
        title="Performance baseline",
        identity=f"{IDENTITY_PREFIX}performance",
        scope=COMMON,
        artifact="performance-results.json",
        script="scripts/create_allure_summary.py",
        job="quality",
        detail="性能基准(只在 Linux 执行)",
    ),
    Producer(
        key="security",
        title="Security findings",
        identity=f"{IDENTITY_PREFIX}security",
        scope=PER_PLATFORM,
        artifact="security-results.json",
        script="scripts/create_allure_summary.py",
        job="security",
        detail="安全测试结论(每个平台一份)",
    ),
    Producer(
        key="visual",
        title="视觉回归",
        identity=f"{IDENTITY_PREFIX}visual.",
        scope=COMMON,
        artifact="allure-results-visual",
        script="scripts/create_allure_visual.py",
        job="quality",
        match=MATCH_PREFIX,
        detail="每张画面一条(感知哈希 + SSIM)",
    ),
    Producer(
        key="quality",
        title="质量门禁检查(与平台无关)",
        identity=f"{IDENTITY_PREFIX}quality.",
        scope=COMMON,
        artifact="allure-results-quality / -analysis",
        script="scripts/create_allure_quality.py",
        job="quality",
        match=MATCH_PREFIX,
        detail="ruff / mypy / deptry / bandit / pip-audit / radon / xenon",
    ),
    Producer(
        key="quality-platform",
        title="平台专属类型检查",
        identity=f"{IDENTITY_PREFIX}quality.",
        scope=PER_PLATFORM,
        artifact="allure-results-quality-platform-<os>",
        script="scripts/create_allure_quality.py",
        job="pytest",
        match=MATCH_PREFIX,
        detail="mypy --platform win32 / darwin, 在各自的平台上执行",
    ),
)


@dataclass(frozen=True)
class QualityCheck:
    """从 ``create_allure_quality.py`` 解析出来的一项检查."""

    key: str
    title: str
    group: str
    host_platform: str | None


@dataclass(frozen=True)
class Expected:
    """一条**应该有**的结论: 身份 + 该出现在哪个环境(公共项留空)."""

    producer: Producer
    identity: str
    title: str
    environment: str

    @property
    def label(self) -> str:
        """总账里显示的一行标题(带环境才带)."""
        return f"{self.title}({self.environment})" if self.environment else self.title


def literal_identities(source: Path) -> set[str]:
    """从一份脚本源码里取出它写进结果的 ``archive-management.<...>`` 身份.

    AST 而不是正则: 要同时认 ``"archive-management.coverage"`` 这种整串常量与
    ``f"archive-management.visual.{name}"`` 这种前缀(取字符串常量的第一段)。**不导入**这些
    脚本 —— 它们各自有自己的重依赖(ruff/mypy/customtkinter), 而汇总作业不装任何依赖。
    """
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value.startswith(IDENTITY_PREFIX):
                found.add(node.value)
        elif isinstance(node, ast.JoinedStr) and node.values:
            head = node.values[0]
            if (
                isinstance(head, ast.Constant)
                and isinstance(head.value, str)
                and head.value.startswith(IDENTITY_PREFIX)
            ):
                found.add(head.value)
    return found


def identity_gaps(scripts_directory: Path) -> tuple[set[str], set[str]]:
    """清单与代码的**双向**差异 ``(代码里有但没登记, 登记了但没人产出)``.

    两个方向都要报: 前者是"新加了一类结论忘了登记"(总账不认识它, 缺了也不会报), 后者是
    "改掉了产出方而清单没跟着改"(总账会一直报一项永远不存在的缺失)。

    认标签的那类身份(真实用例的 ``framework=pytest``)不参与后一个方向: 它是 ``allure-pytest``
    自己写上去的, 不属于 ``create_allure_*.py`` 的产物。
    """
    discovered: set[str] = set()
    for source in sorted(scripts_directory.glob("create_allure_*.py")):
        discovered |= literal_identities(source)
    # 代码里常见"只看脚本产出的结论项"这种**过滤**写法(``fullName`` 以 ``archive-management.``
    # 开头, 见 ``create_allure_summary.py`` 的 ``artifact_rows``): 它说的是"任意一族", 不是一个
    # 身份 —— 拿它去比, "登记了但没人产出"就永远是空的(它是**所有**身份的前缀)。
    discovered = {pattern for pattern in discovered if pattern != IDENTITY_PREFIX}
    registered = {producer.identity for producer in CATALOG}
    from_labels = {
        producer.identity for producer in CATALOG if producer.match == MATCH_LABEL
    }
    # 代码里的**前缀身份**(如 ``archive-management.quality.``)由"某项登记身份以它开头"覆盖;
    # 登记身份则由"某个代码前缀是它的前缀"覆盖 —— 两个方向都比前缀, 因为一族结论的身份在
    # 代码里往往只写到家族那一层(具体后缀是运行期拼的)。
    unregistered = {
        pattern
        for pattern in discovered
        if not pattern.startswith(ABSENCE_IDENTITY)
        and not any(identity.startswith(pattern) for identity in registered)
    }
    never_written = {
        identity
        for identity in registered - from_labels
        if not any(
            identity.startswith(pattern) or pattern.startswith(identity)
            for pattern in discovered
        )
    }
    return unregistered, never_written


def quality_checks(scripts_directory: Path) -> tuple[QualityCheck, ...]:
    """解析 ``create_allure_quality.py`` 的 ``CHECKS``, 列出每一项检查.

    自动同步的关键一步: 在那个脚本里加一项检查, 总账的"应有"清单会立刻把它算进去 ——
    不需要任何人回来维护这张表。
    """
    source = scripts_directory / "create_allure_quality.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "CHECKS"
            for target in node.targets
        ):
            continue
        checks = []
        for element in getattr(node.value, "elts", []):
            if not isinstance(element, ast.Call) or len(element.args) < 2:
                continue
            key = _string(element.args[0])
            title = _string(element.args[1])
            if key is None or title is None:
                continue
            keywords = {
                item.arg: item.value
                for item in element.keywords
                if item.arg is not None
            }
            checks.append(
                QualityCheck(
                    key=key,
                    title=title,
                    group=_string(keywords.get("group")) or "core",
                    host_platform=_string(keywords.get("host_platform")),
                )
            )
        return tuple(checks)
    return ()


def _string(node: ast.expr | None) -> str | None:
    """取一个字符串字面量的值(不是字面量时给 None)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def platform_environments(scripts_directory: Path) -> dict[str, str]:
    """解析 ``create_allure_quality.py`` 的 ``PLATFORM_ENVIRONMENTS``(宿主平台 → 环境名).

    "平台专属检查该落在哪个环境里"是那份脚本的事实(它自己用它给结果打 ``env`` 标签): 抄一份
    到清单里迟早分叉 —— 它加一个平台分支, 清单里就少一项"应有", 那项检查缺失也就没人报。
    字典本身是字面量, 所以 ``literal_eval`` 就够(不执行任何代码)。
    """
    source = scripts_directory / "create_allure_quality.py"
    tree = ast.parse(source.read_text(encoding="utf-8"), filename=str(source))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [target.id for target in node.targets if isinstance(target, ast.Name)]
        if "PLATFORM_ENVIRONMENTS" not in targets:
            continue
        try:
            resolved = ast.literal_eval(node.value)
        except ValueError:
            continue
        if isinstance(resolved, dict):
            return {str(key): str(value) for key, value in resolved.items()}
    return {}


def expected_items(platforms: list[str], scripts_directory: Path) -> list[Expected]:
    """按清单 + 解析出来的质量检查, 展开本次运行**应该有**的全部结论项.

    ``platforms`` 是本次运行声明要覆盖的平台(来自 CI 的 ``--expect-platforms``): 每平台一项
    的结论按它展开 —— 某个平台整体没把产物交上来时, 它的每一项都会在"应有"里占一行, 于是
    **缺的是哪个平台**一眼看清。公共项(与平台无关)只出现一次, 环境留空: 它最终落在哪个环境
    里由产出方自己决定, 核对时也不看环境。

    行序就是清单序(`CATALOG`), 一族结论连着出现; 质量检查逐项展开 —— 在
    ``create_allure_quality.py`` 里加一项检查, 这里立刻跟着多一行, 不需要谁回来维护。
    """
    quality = quality_checks(scripts_directory)
    environments = platform_environments(scripts_directory)
    items: list[Expected] = []
    for producer in CATALOG:
        if producer.key == "quality":
            items.extend(
                Expected(
                    producer,
                    f"{IDENTITY_PREFIX}quality.{check.key}",
                    check.title,
                    "",
                )
                for check in quality
                if check.host_platform is None
            )
            continue
        if producer.key == "quality-platform":
            for check in quality:
                environment = environments.get(check.host_platform or "", "")
                # 平台专属检查只在**声明要跑那个平台**时才要求它出现(平台没跑不算缺失).
                if environment and environment in platforms:
                    items.append(
                        Expected(
                            producer,
                            f"{IDENTITY_PREFIX}quality.{check.key}",
                            check.title,
                            environment,
                        )
                    )
            continue
        if producer.scope == PER_PLATFORM:
            items.extend(
                Expected(producer, producer.identity, producer.title, environment)
                for environment in platforms
            )
        else:
            items.append(Expected(producer, producer.identity, producer.title, ""))
    return items


def _labels(payload: dict[str, Any]) -> dict[str, str]:
    """把结果上的标签摊平成 ``{名字: 取值}``."""
    return {
        str(label.get("name")): str(label.get("value", ""))
        for label in (payload.get("labels") or [])
        if isinstance(label, dict)
    }


def actual_items(results_directory: Path) -> set[tuple[str, str]]:
    """扫描结果目录, 给出**实际收到**的 ``(身份, 环境)`` 集合.

    身份: 结果的 ``fullName``(脚本产出的结论项), 或 ``framework=pytest``(真实用例)。
    """
    found: set[tuple[str, str]] = set()
    for path in sorted(results_directory.rglob(f"*{RESULT_FILE_SUFFIX}")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        labels = _labels(payload)
        environment = labels.get(ENV_LABEL, "")
        if labels.get(FRAMEWORK_LABEL) == FRAMEWORK_VALUE:
            found.add((FRAMEWORK_VALUE, environment))
        full_name = str(payload.get("fullName") or "")
        if full_name:
            found.add((full_name, environment))
    return found


def _matches(item: Expected, identity: str) -> bool:
    """一条实际身份算不算这一项(按清单声明的认法).

    注意比的是**这一项**的身份(``item.identity``), 不是它所属家族的 ``producer.identity``:
    质量检查是一族一项一条(``archive-management.quality.ruff-check``), 拿家族前缀 ``.quality.``
    去比会把"有任意一项检查"当成"这一项检查也在", 于是缺的那几项永远不会显形。
    """
    if item.producer.match == MATCH_PREFIX:
        return identity.startswith(item.identity)
    return identity == item.identity


def missing_items(
    expected: list[Expected], actual: set[tuple[str, str]]
) -> list[Expected]:
    """在**应有**里挑出**实有**里没有的(公共项不看环境, 每平台项必须环境对上)."""
    missing: list[Expected] = []
    for item in expected:
        found = any(
            _matches(item, identity)
            and (not item.environment or environment == item.environment)
            for identity, environment in actual
        )
        if not found:
            missing.append(item)
    return missing


def describe_missing(item: Expected) -> str:
    """一句"缺了什么、去哪儿找"的话(写进 broken 结论项与总账)."""
    producer = item.producer
    where = f"{producer.script}"
    if producer.job:
        where += f", 由 CI 作业 {producer.job} 上传"
    return (
        f"缺少结论: {item.label} —— 预期产物 `{producer.artifact}`"
        f"({producer.detail or producer.title}); 产出方: {where}。"
        "产物没产出/没上传/没合并进报告时都会走到这里。"
    )
