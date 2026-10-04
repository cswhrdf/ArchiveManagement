"""Allure 报告完整性校验脚本的回归用例.

背景: 报告是静态站点, 用例详情页要按需取 ``data/test-results/<结果 id>.json``。
CI 里出现过"报告只剩通过/失败, 点开用例是空的" —— 该目录在发布链路上被丢掉了,
而生成阶段本身是好的(用同一个 allure 版本本地生成就有这些文件)。这里锁住四件事:

- 缺详情目录 / 缺单个详情文件都必须判为不完整;
- 结果条数与 ``allure-results`` 不一致(合并或生成掉数据)必须判为不完整;
- 没有报告时(本次没跑出结果)不算失败;
- 环境维度必须生效: 只剩单个 ``default`` 环境(生成时没读到 ``allurerc.mjs``)要判为不完整;
- 结果声明的附件必须真的在(覆盖率/性能/安全汇总项把原始报告挂在条目上);
- CI 只在自检通过后发布, 且发布的是单个 zip。
"""

from __future__ import annotations

import importlib.util
import io
import json
import re
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import ci_workflow

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"
_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"
_ALLURE_CONFIG = _REPO_ROOT / "allurerc.mjs"
_RESULT_IDS = ("aaa111", "bbb222")


def _load_script(name: str) -> Any:
    """按路径加载 ``scripts/`` 下的脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    spec = importlib.util.spec_from_file_location(
        name, _REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_verifier() -> Any:
    """按路径加载校验脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    return _load_script("verify_allure_report")


# 模块级加载一次: 脚本无副作用(入口在 __main__ 守卫里)。
verifier = _load_verifier()


#: ``environments`` 里一项的开头: ``    windows: {`` 后面紧跟 ``      name: "Windows",``。
_ENVIRONMENT_ENTRY = re.compile(
    r"^\s{4}([a-z][\w-]*):\s*\{\s*name:\s*\"([^\"]+)\"", re.MULTILINE
)


def required_environment_ids() -> tuple[str, ...]:
    """读出 ``allurerc.mjs`` 里质量门 ``environmentsTested`` 要的那几个**环境 id**.

    为什么盯的是 id: 质量门比的是每条结果的 ``environment``, 那是环境身份里的 **id**
    (``environments`` 的键, 小写)。写成平台显示名("Windows")**一个都比不上** —— 症状是
    门禁一次报全三个"未被测试", 而三条平台的用例其实都交齐了(2026-10-04 的汇总报告就是
    这么红的; 本地用三平台最小结果复现过, 见 PLAN.md §48.6)。
    """
    gate = _ALLURE_CONFIG.read_text(encoding="utf-8").split("qualityGate:", 1)[1]
    matched = re.search(r"environmentsTested:\s*\[([^\]]+)\]", gate)
    assert matched is not None, "配置里要有 environmentsTested"
    return tuple(name.strip().strip('"') for name in matched.group(1).split(","))


def declared_environment_names() -> dict[str, str]:
    """``environments`` 里声明的 环境 id → 显示名(把门禁要的 id 翻成平台名用)."""
    block = _ALLURE_CONFIG.read_text(encoding="utf-8").split("environments: {", 1)[1]
    return dict(_ENVIRONMENT_ENTRY.findall(block))


def required_platforms() -> tuple[str, ...]:
    """质量门要求的平台**显示名**(与 CI 矩阵、汇总作业的 ``--expect-platforms`` 对照).

    CI 里三处引用它: 配置里的 ``environmentsTested``、汇总作业的 ``--expect-platforms``
    与各矩阵的平台列表 —— 由这里的守卫核对着一致。配置里写的是环境 id(见
    :func:`required_environment_ids`), 这里按 ``environments`` 的声明翻成显示名 ——
    顺带把"门禁要的 id 到底声明过没有"也钉住: 写错一个 id 就没得翻, 当场红。
    """
    names = declared_environment_names()
    ids = required_environment_ids()
    missing = [env_id for env_id in ids if env_id not in names]
    assert missing == [], (
        f"environmentsTested 里的 id 必须在 environments 里声明过: {missing}"
    )
    return tuple(names[env_id] for env_id in ids)


@dataclass(frozen=True)
class _Layout:
    """一份结构完整的报告与配套结果目录, 便于用例按需破坏其中一部分."""

    report: Path
    results: Path


@pytest.fixture
def layout(tmp_path: Path) -> _Layout:
    """造一个"生成阶段正常"的报告: 索引/详情/分组/控件数据与结果文件一一对应."""
    report = tmp_path / "allure-report"
    details = report / "data" / "test-results"
    groups = report / "data" / "test-env-groups"
    widgets = report / "widgets"
    for directory in (details, groups, widgets):
        directory.mkdir(parents=True)
    (report / "index.html").write_text("<html></html>", encoding="utf-8")
    (report / "app-abc123.js").write_text("// bundle", encoding="utf-8")
    (report / "summary.json").write_text(
        json.dumps({"stats": {"total": len(_RESULT_IDS)}}), encoding="utf-8"
    )
    (report / "test-results.json").write_text(
        json.dumps(
            {"byId": {rid: {"id": rid, "status": "passed"} for rid in _RESULT_IDS}}
        ),
        encoding="utf-8",
    )
    for name in ("statistic.json", "tree.json"):
        (widgets / name).write_text("{}", encoding="utf-8")
    # 环境列表: 平台以"环境"形式出现(没有非 default 环境会被判为不完整).
    (widgets / "environments.json").write_text(
        json.dumps(
            [{"id": "default", "name": "default"}, {"id": "windows", "name": "Windows"}]
        ),
        encoding="utf-8",
    )
    for rid in _RESULT_IDS:
        (details / f"{rid}.json").write_text(
            json.dumps({"id": rid, "steps": []}), encoding="utf-8"
        )
    (groups / "group1.json").write_text(
        json.dumps(
            {
                "id": "group1",
                "fullName": "tests.unit.test_probe#test_case",
                "testResultsByEnv": {rid: rid for rid in _RESULT_IDS},
            }
        ),
        encoding="utf-8",
    )
    results = tmp_path / "allure-results"
    results.mkdir()
    for rid in _RESULT_IDS:
        (results / f"{rid}-result.json").write_text("{}", encoding="utf-8")
    # 汇总项(覆盖率/性能/安全/质量)把原始报告作为附件挂在结果上: 附件文件也要跟着到达。
    attachment = results / f"{_RESULT_IDS[0]}-attachment.xml"
    attachment.write_text("<coverage/>", encoding="utf-8")
    (results / f"{_RESULT_IDS[0]}-result.json").write_text(
        json.dumps(
            {
                "uuid": _RESULT_IDS[0],
                "attachments": [
                    {
                        "name": "coverage.xml",
                        "source": attachment.name,
                        "type": "application/xml",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return _Layout(report=report, results=results)


def test_complete_report_has_no_problems(layout: _Layout) -> None:
    """结构完整时不应报任何问题, 且计数事实与磁盘一致."""
    facts, problems = verifier.verify_report(layout.report, layout.results)

    assert problems == []
    assert facts is not None
    assert facts.indexed_results == len(_RESULT_IDS)
    assert facts.detail_files == len(_RESULT_IDS)
    assert facts.env_groups == 1
    assert facts.result_files == len(_RESULT_IDS)
    assert facts.attachments == 1


def test_missing_detail_directory_fails_with_exit_code(
    layout: _Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """整个 data/test-results 丢失必须判为不完整(就是线上报告打不开详情的形态)."""
    shutil.rmtree(layout.report / "data" / "test-results")

    exit_code = verifier.main([str(layout.report), "--results", str(layout.results)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "data/test-results" in captured.err
    assert f"缺少 {len(_RESULT_IDS)} 个用例详情文件" in captured.err
    # 计数事实写在标准输出里: 日志一眼能看出报告退化成了"只有通过/失败"。
    assert "详情文件: 0 个" in captured.out


def test_single_missing_detail_file_is_named(layout: _Layout) -> None:
    """只丢一个详情文件时, 问题描述要点出是哪个结果 id."""
    (layout.report / "data" / "test-results" / f"{_RESULT_IDS[0]}.json").unlink()

    _, problems = verifier.verify_report(layout.report)

    assert len(problems) == 1
    assert _RESULT_IDS[0] in problems[0]


def test_result_count_mismatch_is_reported(layout: _Layout) -> None:
    """结果目录条数与报告索引不一致说明合并/生成阶段掉过数据."""
    (layout.results / "ccc333-result.json").write_text("{}", encoding="utf-8")

    _, problems = verifier.verify_report(layout.report, layout.results)

    assert any("结果数不一致" in problem for problem in problems)


def test_dangling_group_reference_is_reported(layout: _Layout) -> None:
    """用例分组引用了索引里没有的结果 id 时也要报错."""
    (layout.report / "data" / "test-env-groups" / "group1.json").write_text(
        json.dumps({"id": "group1", "testResultsByEnv": {"default": "zzz999"}}),
        encoding="utf-8",
    )

    _, problems = verifier.verify_report(layout.report)

    assert any("zzz999" in problem for problem in problems)


def test_a_nested_report_directory_is_reported(layout: _Layout) -> None:
    """报告目录里嵌套的另一份报告要报出来: 那是生成时目录已存在的痕迹.

    实测(Allure 3.20.0): 输出目录里已经有报告时, 新报告被写进 ``awesome/``, 顶层
    ``index.html`` 留成上一次那张 —— 发布出去的正是顶层这份, 于是报告"看起来正常、其实
    是旧的": 本次运行不在里面, 历史趋势也停在上一次(用户报的"历史记录不可见"就是它,
    Allure issue #691)。所以这里必须红, 而不是安静地发一份旧报告。
    """
    nested = layout.report / "awesome"
    nested.mkdir()
    (nested / "index.html").write_text("<!doctype html>", encoding="utf-8")

    _, problems = verifier.verify_report(layout.report)

    assert any("awesome" in problem for problem in problems), problems
    assert verifier.main([str(layout.report)]) == 1


def test_single_default_environment_is_reported(layout: _Layout) -> None:
    """只剩 default 环境时判为不完整: 平台不再作为环境出现(且不会有任何报错)."""
    (layout.report / "widgets" / "environments.json").write_text(
        json.dumps([{"id": "default", "name": "default"}]), encoding="utf-8"
    )

    _, problems = verifier.verify_report(layout.report)

    assert any("只有 default 环境" in problem for problem in problems)


def test_missing_environment_widget_is_reported(layout: _Layout) -> None:
    """环境列表本身缺失(报告结构不对)也要报错."""
    (layout.report / "widgets" / "environments.json").unlink()

    _, problems = verifier.verify_report(layout.report)

    assert any("environments.json" in problem for problem in problems)


def _write_platform_report(layout: _Layout) -> None:
    """把 fixture 的报告改造成三平台形态: 一个 Windows 用例 + 一个 macOS 汇总项."""
    windows_id, macos_id = _RESULT_IDS
    (layout.report / "widgets" / "environments.json").write_text(
        json.dumps(
            [
                {"id": "default", "name": "default"},
                {"id": "windows", "name": "Windows"},
                {"id": "macos", "name": "macOS"},
                {"id": "linux", "name": "Linux"},
            ]
        ),
        encoding="utf-8",
    )
    (layout.report / "test-results.json").write_text(
        json.dumps(
            {
                "byId": {
                    windows_id: {
                        "id": windows_id,
                        "status": "passed",
                        "environment": "Windows",
                    },
                    macos_id: {
                        "id": macos_id,
                        "status": "passed",
                        "environment": "macOS",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    details = layout.report / "data" / "test-results"
    (details / f"{windows_id}.json").write_text(
        json.dumps(
            {"id": windows_id, "labels": [{"name": "framework", "value": "pytest"}]}
        ),
        encoding="utf-8",
    )
    # macOS 只有一条脚本生成的汇总项(没有 framework 标签): 环境存在, 用例一条都没有。
    (details / f"{macos_id}.json").write_text(
        json.dumps(
            {"id": macos_id, "labels": [{"name": "testCategory", "value": "coverage"}]}
        ),
        encoding="utf-8",
    )


def test_platform_with_only_summary_items_is_reported(
    layout: _Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """要求每个平台里都有真实用例: 只剩汇总项时必须判为不完整.

    覆盖率/性能/安全汇总项、平台专属的质量检查都带平台的 ``env``, 所以"缺一整个平台的
    用例"在报告里看上去和三平台齐全一模一样(2026-09-21 用真实数据验过: 删掉 Linux 的
    1166 条用例后, 不过滤的 ``environmentsTested`` 照样通过)。这里校验我们自己那一道。
    """
    _write_platform_report(layout)

    facts, problems = verifier.verify_report(
        layout.report, expected_platforms=("Windows", "macOS", "Linux")
    )

    assert facts is not None
    message = next(problem for problem in problems if "没有用例结果" in problem)
    assert "macOS" in message
    assert "Linux" in message
    assert "Windows" not in message.split("(")[0], "Windows 有用例, 不该被列进去"
    # 逐平台条数要能写出来: 一眼看出"macOS 只有汇总项"。
    assert facts.tests_by_environment == (
        ("Windows", 1, 0),
        ("macOS", 0, 1),
        ("Linux", 0, 0),
    )

    # 这个检查只按调用方声明的平台范围做: 只要 Windows 时就没有问题。
    _, only_windows = verifier.verify_report(
        layout.report, expected_platforms=("Windows",)
    )
    assert [item for item in only_windows if "没有用例结果" in item] == []

    verifier.report_facts(facts, layout.report)
    assert "按平台用例: Windows 1 用例 + 0 汇总项" in capsys.readouterr().out


def test_required_platforms_match_the_ci_matrix() -> None:
    """要求哪些平台要有用例: 配置、汇总作业的参数、CI 矩阵三处必须是同一个集合.

    这三处一旦不一致就会变成两种错法: 矩阵里跑了而没要求 -> 那个平台的产物丢了没人发现;
    要求了而矩阵里没跑 -> 报告必然不完整, 门禁无意义地红。
    macOS 曾在开发阶段屏蔽(省额度), 2026-09-30 恢复后三处一起加回来(见 PLAN.md 第 11.9 节)。

    汇总脚本自己也吃同一份清单(``create_allure_summary.py --expect-platforms``): 它决定运行
    总账末节「证据核对」里"应有"的那些每平台项 —— 少列一个平台, 那个平台整族的结论(用例/
    覆盖率/安全)就没人点名, 而报告看上去仍然完整。所以是**同一份清单的第三处拷贝**, 一起钉住。
    这个列表还与工作流里每个作业的矩阵"每轮都跑"是一件事: 按平台展开的结论族不许对平台做
    有条件排除(见 ``test_every_platform_conclusion_is_produced_on_every_run``)。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    required = set(required_platforms())
    assert required == {"Windows", "macOS", "Linux"}, (
        "要求哪几个平台要有用例: 恢复/屏蔽某个平台时这一条要跟着改(见 PLAN.md 第 11.9 节)"
    )

    summary = workflow.split("name: Check every platform contributed tests", 1)[1]
    command = summary.split("run:", 1)[1].splitlines()[0]
    passed = re.search(r"--expect-platforms\s+(\S+)", command)
    assert passed is not None, "汇总作业要传 --expect-platforms"
    assert set(passed.group(1).split(",")) == required, (
        "汇总作业的平台列表与 allurerc.mjs 不一致"
    )

    invoked = re.search(
        r"create_allure_summary\.py[^\n]*--expect-platforms\s+(\S+)", workflow
    )
    assert invoked is not None, (
        "汇总脚本要传 --expect-platforms(运行总账按它算「应有」)"
    )
    assert set(invoked.group(1).split(",")) == required, (
        "运行总账的平台清单与 allurerc.mjs 不一致"
    )

    # CI 矩阵里实际跑测试的平台: pytest 与 pytest-report 用 include 逐条列(条目里的 os 是
    # runner, platform 是报告里的环境名), security 还是 os 列表。三处都必须是同一个集合 ——
    # 一处不一致就会变成两种错法: 矩阵里跑了而没要求 -> 那个平台的产物丢了没人发现;
    # 要求了而矩阵里没跑 -> 报告必然不完整, 门禁无意义地红。
    for job in ("pytest", "pytest-report"):
        platforms = {
            entry["platform"] for entry in ci_workflow.matrix_entries(workflow, job)
        }
        assert platforms == required, (
            f"{job} 的矩阵 {sorted(platforms)} 与要求的平台 {sorted(required)} 不一致"
        )

    listed = re.search(r"os: \[([^\]]+)\]", ci_workflow.job_block(workflow, "security"))
    assert listed is not None, "security 没有矩阵平台列表"
    platforms = {
        ci_workflow.OS_PLATFORMS[item.strip()] for item in listed.group(1).split(",")
    }
    assert platforms == required, (
        f"security 的矩阵 {sorted(platforms)} 与要求的平台 {sorted(required)} 不一致"
    )


def test_the_pages_actions_are_a_compatible_pair() -> None:
    """发布站点的两个 action 必须成对(上传 ≥ v3 且 部署 ≥ v4): 单边降级会让部署拿不到产物.

    版本事实(2026-09-30 从两个 action 的 release 页核对, 这是 12.4 要的"版本先例"):
    ``actions/upload-pages-artifact`` 有 v3.0.1 / v4.0.0 / v5.0.0,
    ``actions/deploy-pages`` 有 v3.0.2 / v4.0.5 / v5.0.1;
    官方在 release note 里写死了兼容关系 —— deploy-pages **v3 及以上**只吃
    upload-pages-artifact **v3 及以上**(或 upload-artifact v4+)上传的产物。
    所以升级要两边都在"新的那一侧", 不能只动一个。
    """
    block = ci_workflow.job_block(
        _WORKFLOW.read_text(encoding="utf-8"),
        "allure-summary",
    )

    uploaded = re.search(r"actions/upload-pages-artifact@v(\d+)", block)
    deployed = re.search(r"actions/deploy-pages@v(\d+)", block)
    assert uploaded is not None, "汇总作业要上传 Pages 产物"
    assert deployed is not None, "汇总作业要部署到 Pages"

    assert int(uploaded.group(1)) >= 3, (
        "上传侧要 ≥ v3: deploy-pages 只吃 v3 及以上上传的产物"
    )
    assert int(deployed.group(1)) >= 4, "部署侧要 ≥ v4(v3 那版已被 v4 取代)"


def test_every_platform_conclusion_is_produced_on_every_run() -> None:
    """按平台展开的结论族, 它的产出作业不能对平台"有条件排除"(2026-10-02 的误报就这么来的).

    那条误报: security 的 macOS 曾写在矩阵 ``exclude`` 里(只在 push 到默认分支时跑), 而清单
    仍在所有运行里要求 ``Security findings(macOS)`` —— 于是每次 PR 与 ``dev`` 推送都报一条
    "缺少结论: Security findings(macOS)", 而那一份按设计就不该有。**假警报比不报更坏**:
    看多了就没人当回事了。

    现在选的是"不降频"(用户 2026-10-02 的决定): 三个平台每轮都跑。代价要认 —— macOS runner
    按 Linux 的 10 倍计价。所以这条守卫咬两件事: ① security 的矩阵里没有 ``exclude``、三个
    平台都在; ② 更强的一条: **任何**按平台展开的结论族, 它的产出作业都不许有条件排除 ——
    再有人加回一条 "某个平台只在某个事件里跑", 这里立刻红, 而不是等到报告里冒出一条假缺失。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = ci_workflow.job_block(workflow, "security")

    listed = re.search(r"os: \[([^\]]+)\]", block)
    assert listed is not None, "security 的矩阵里要有平台列表"
    runners = {item.strip() for item in listed.group(1).split(",")}
    assert runners == set(ci_workflow.OS_PLATFORMS), (
        f"security 要在每个平台上都跑(报告也按三个平台要求): {sorted(runners)}"
    )
    assert not re.search(r"^\s*exclude:", block, re.MULTILINE), (
        "security 不做平台降频: 有条件排除就会在别的运行里少一份结论, "
        "而总账会把它报成缺失(要么一起改清单, 要么别排除)"
    )

    catalog = _load_script("allure_catalog")
    for item in catalog.CATALOG:
        if item.scope != catalog.PER_PLATFORM:
            continue
        job = ci_workflow.job_block(workflow, item.job)
        assert not re.search(r"^\s*exclude:", job, re.MULTILINE), (
            f"{item.key} 是每平台一份的结论, 但它的产出作业 {item.job} 会按条件排除平台 —— "
            f"那一种运行里总账会把少的那一份报成缺失, 而它按设计就不该有"
        )


def test_expect_platforms_flag_decides_the_run(
    layout: _Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--expect-platforms`` 能直接决定自检成败(CI 就是靠它按平台自查)."""
    _write_platform_report(layout)

    exit_code = verifier.main(
        [
            str(layout.report),
            "--results",
            str(layout.results),
            "--expect-platforms",
            "macOS,Linux",
        ]
    )
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "这些平台里没有用例结果" in captured.err
    assert "macOS" in captured.err
    # 不传就不做这项检查: 本地核对下载的 artifact 时不必知道是哪个平台。
    assert verifier.main([str(layout.report), "--results", str(layout.results)]) == 0


def _write_manifest(
    path: Path,
    *,
    platform: str = "Windows",
    results: int = 1,
    missing: tuple[str, ...] = (),
) -> Path:
    """写一份产物清单(字段与 merge_allure_results.write_manifest 一致)."""
    path.write_text(
        json.dumps(
            {
                "platform": platform,
                "expected_shards": ["0", "1", "2"],
                "missing_shards": list(missing),
                "sources": [
                    {"name": "shard-0", "shard": "0", "files": 1, "results": 1}
                ],
                "total_files": results,
                "total_results": results,
                "overwritten": 0,
            }
        ),
        encoding="utf-8",
    )
    return path


def test_manifest_flags_missing_shards_and_count_mismatches(
    layout: _Layout, tmp_path: Path
) -> None:
    """产物清单要能发现"少一片"与"条数对不上"—— 这是环境维度看不出来的部分.

    三条判据: 声明必须有的片号到齐了、各分片自报的条数不超过最终结果数、报告里每个平台的
    用例数不少于该平台分片自报的条数。
    """
    _write_platform_report(layout)
    manifest = _write_manifest(
        tmp_path / "allure-manifest.json",
        platform="Windows",
        results=5,
        missing=("1",),
    )

    _, problems = verifier.verify_report(
        layout.report,
        layout.results,
        expected_platforms=("Windows", "macOS"),
        manifest_pattern=str(manifest),
    )

    assert any("缺少分片 1" in problem for problem in problems), problems
    assert any("结果数与产物清单对不上" in problem for problem in problems), problems
    assert any("少于分片自报的 5 条" in problem for problem in problems), problems


def test_manifest_numbers_are_printed_and_absent_pattern_fails(
    layout: _Layout, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """清单的条数要打进日志; 模式一个文件都没匹配到也要报错(清单没上传等于没有检查)."""
    _write_platform_report(layout)
    manifest = _write_manifest(tmp_path / "allure-manifest.json", results=1)

    facts, problems = verifier.verify_report(
        layout.report,
        layout.results,
        expected_platforms=("Windows",),
        manifest_pattern=str(manifest),
    )

    assert problems == []
    assert facts is not None
    verifier.report_facts(facts, layout.report)
    assert "分片产物清单: Windows 1 条结果(1 片)" in capsys.readouterr().out

    _, missing = verifier.verify_report(
        layout.report,
        layout.results,
        expected_platforms=("Windows",),
        manifest_pattern=str(tmp_path / "allure-manifests" / "*.json"),
    )
    assert any("没找到产物清单" in problem for problem in missing), missing


def test_declared_attachment_must_exist(
    layout: _Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """结果里声明的附件被丢掉时必须报错(否则报告里只剩一个打不开的附件)."""
    (layout.results / f"{_RESULT_IDS[0]}-attachment.xml").unlink()

    exit_code = verifier.main([str(layout.report), "--results", str(layout.results)])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "缺少 1 个结果附件" in captured.err
    assert "-attachment.xml" in captured.err
    assert "结果附件: 1 个" in captured.out


def test_absent_report_is_not_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """本次没有结果时报告不会生成, 此时应跳过校验并成功退出."""
    exit_code = verifier.main([str(tmp_path / "missing-report")])

    assert exit_code == 0
    assert "跳过校验" in capsys.readouterr().out


def test_archive_packages_every_file(
    layout: _Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--archive`` 必须把目录里每个文件都装进 tar.gz, 并打印条目数与 SHA256."""
    exit_code = verifier.main(
        [str(layout.report), "--results", str(layout.results), "--archive"]
    )
    output = capsys.readouterr().out
    archive = layout.report.parent / f"{layout.report.name}.tar.gz"

    assert exit_code == 0
    assert archive.is_file()
    on_disk = sorted(
        path.relative_to(layout.report).as_posix()
        for path in layout.report.rglob("*")
        if path.is_file()
    )
    with tarfile.open(archive, "r:gz") as bundle:
        packed = sorted(
            member.name.removeprefix(f"{layout.report.name}/")
            for member in bundle.getmembers()
            if member.isfile()
        )
    assert packed == on_disk
    assert f"{len(on_disk)} 个条目" in output
    assert "SHA256:" in output


def test_ci_publishes_only_verified_report() -> None:
    """CI 必须先自检报告再发布, 并且发布单个 zip(整目录被静默丢掉的情况不会再出现)."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    # 只看命令行: 注释里也会指路到这个脚本(例如说明 zip 里的目录结构), 那是文档而不是门禁。
    code = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )
    verifications = code.count("scripts/verify_allure_report.py")

    assert verifications >= 3, "两个作业的结构自检 + 汇总作业的平台用例检查"
    # 发布 zip 的前提是**出 zip 的那次自检**通过: 两个作业各一份。
    commands = [
        line.strip()
        for line in code.splitlines()
        if line.strip().startswith("run:") and "verify_allure_report.py" in line
    ]
    assert len(commands) == verifications, "自检点数量与命令数量对不上"
    archive_checks = [command for command in commands if "--archive" in command]
    assert len(archive_checks) == 2, "pytest 与汇总作业各出一份归档报告"
    # 挂在"出 zip 的那次自检通过"上的地方有三处: 两个作业各一次 `Upload ... Allure report`,
    # 再加汇总作业里"把 zip 解开发布到 Pages"的第一步(2026-09-30 合并发布作业时新增)。
    # 只数命令行(注释里也会引这句话当说明), 所以先把注释剥掉再数。
    gated = code.count("steps.verify-report.outcome == 'success'")
    assert gated == len(archive_checks) + 1, f"自检与发布挂钩的地方对不上: {gated}"
    assert workflow.count("path: allure-report.tar.gz") == len(archive_checks)
    assert "path: allure-report/" not in workflow, "报告目录不再直接发布"
    # 平台用例检查: pytest 作业的报告传矩阵里的平台(报告作业固定跑在 Ubuntu 上,
    # runner.os 会说 Linux), 汇总作业传汇总要求的两个平台 —— 两处都要有, 少一处就等于
    # 少一道检查(只数 `run:` 行, 注释不算)。
    platform_checks = [
        command for command in commands if "--expect-platforms" in command
    ]
    assert len(platform_checks) == 2, "pytest 作业与汇总作业各要检查一次平台用例"
    per_platform_flag = '--expect-platforms "${{ matrix.platform }}"'
    assert sum(per_platform_flag in c for c in platform_checks) == 1
    # 汇总作业要在这份合并报告里要求三个平台都有**用例**结果(不是只要"环境存在")。
    summary_flag = "--expect-platforms Windows,macOS,Linux"
    assert sum(summary_flag in c for c in platform_checks) == 1
    # 汇总作业里那条不拦发布(报告正是用来看"哪个平台没数据"的地方), 所以它的结论必须有
    # 地方接手: 末尾的门禁结论步骤要带上它, 否则失败了也没人管。
    step = workflow.split("name: Check every platform contributed tests", 1)[1]
    assert "continue-on-error: true" in step.split("\n      - name:", 1)[0]
    verdict = workflow.split("name: Report quality gate verdict", 1)[-1]
    assert "steps.verify-platforms.outcome == 'failure'" in verdict, (
        "平台用例检查的结论必须由末尾的门禁结论步骤接手"
    )


def test_ci_judges_the_coverage_only_on_complete_shard_data() -> None:
    """覆盖率链条: 缺片要点名、不合并、不判门槛、不出残缺报告, 并且作业照样红.

    2026-09-30 的 CI 实测: 少一个分片的覆盖率产物时, ``cp coverage-data-*/.coverage.shard-*``
    只报一句 ``cannot stat``, 后面 ``coverage xml`` 会自己合并剩下那份并照 ``fail_under``
    报"覆盖率不达标" —— 报告里写着"Windows 89.99% 未达标", 而真相是数据不全。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "pytest-report")
    code = "\n".join(
        line for line in job.splitlines() if not line.strip().startswith("#")
    )

    assert "cp coverage-data-*" not in code, "通配符抄数据看不出缺的是哪一片"
    collect = code.split("name: Collect coverage data", 1)[1].split("- name:", 1)[0]
    assert "scripts/collect_coverage_data.py" in collect, "要按片号收集"
    assert '--expect-shards "${{ matrix.expect_shards }}"' in collect, (
        "收集要按矩阵声明核对片号(声明了却没到的那些才是缺片)"
    )
    assert "continue-on-error: true" in collect, (
        "缺片时要让后面的步骤按 outcome 各自决定"
    )
    assert re.search(r"^\s+id: collect-coverage$", collect, re.M), (
        "后面的步骤要靠这个 id 判断数据全不全"
    )

    # 合并 / 判门槛 / 出报告 / 挂结论四件都只在"片到齐"时做。
    for step in (
        "Combine coverage data",
        "Enforce the coverage threshold",
        "Write the coverage report",
        "Attach coverage report to Allure",
    ):
        body = code.split(f"name: {step}", 1)[1].split("- name:", 1)[0]
        assert "steps.collect-coverage.outcome == 'success'" in body, (
            f"{step} 没有按'片到齐'放行: 残缺数据会变成一个看起来'覆盖率掉了'的结论"
        )
    # 门槛只在一处判: `coverage xml` 自己也会执行 fail_under, 那会让同一个失败被报两次,
    # 而且文案把"缺片"说成"覆盖率不够"。
    assert "coverage xml -o coverage.xml --fail-under=0" in code, (
        "写报告的那一步不该再判一次门槛"
    )
    # 缺片的结论必须落在作业状态上: 收集那步是 continue-on-error, 所以要有接手的一步。
    gap = code.split("name: Report the shard gap", 1)
    assert len(gap) == 2, "缺片要有一道专门让作业变红的门禁"
    assert "steps.collect-coverage.outcome != 'success'" in gap[1]
    assert "exit 1" in gap[1]


def test_collect_coverage_data_names_the_missing_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺片要点名(哪一片)并以非 0 退出, 而且**不摊平**(下游据此跳过硬门槛)."""
    module = _load_script("collect_coverage_data")
    monkeypatch.chdir(tmp_path)
    shard0 = tmp_path / "coverage-data-windows-latest-0" / ".coverage.shard-0"
    shard0.parent.mkdir()
    shard0.write_bytes(b"a")

    exit_code = module.main(["--expect-shards", "0,1"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "缺 1" in captured.err, "失败信息要点名缺的是哪一片"
    assert "0,1" in captured.err, "也要说明声明了哪几片"
    assert not (tmp_path / ".coverage.shard-0").exists(), "缺片时不该摊平"
    assert "片 0" in captured.out, "找到了哪几片也要打出来(便于与日志里的产物清单对数)"


def test_collect_coverage_data_accepts_the_flat_download_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只匹配到一个产物时下载动作会把它直接解到当前目录(没有 coverage-data-* 这层).

    这正是那次 CI 失败的另一半: 通配符 `coverage-data-*/...` 一个都匹配不到, 于是那一片
    静默地没进合并。
    """
    module = _load_script("collect_coverage_data")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".coverage.shard-0").write_bytes(b"a")
    nested = tmp_path / "coverage-data-windows-latest-1" / ".coverage.shard-1"
    nested.parent.mkdir()
    nested.write_bytes(b"b")

    assert module.main(["--expect-shards", "0,1"]) == 0
    assert (tmp_path / ".coverage.shard-1").exists(), "子目录里的那片要摊平到工作目录"
    assert (tmp_path / ".coverage.shard-0").read_bytes() == b"a"


def test_report_summary_items_declare_a_severity() -> None:
    """脚本生成的汇总项要带严重等级与环境标签, 名称里不再拼平台名.

    严重等级: 缺了会在报告里多出一个 ``no_severity`` 桶;
    环境标签: 缺了会被归到 ``default``, 按环境筛选时就看不到性能/安全/覆盖率结论;
    名称不拼平台: 平台现在由"环境"表达(见 docs/testing.md 第 5 节), 写进名称会让报告里
    多出三个同名的独立条目。
    """
    scripts = (
        _REPO_ROOT / "scripts" / "create_allure_coverage.py",
        _REPO_ROOT / "scripts" / "create_allure_summary.py",
    )

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert '"name": "severity"' in text, f"{script.name} 未给汇总项写 severity 标签"
        assert '"name": "env"' in text, f"{script.name} 未给汇总项写 env 标签"

    summary = scripts[1].read_text(encoding="utf-8")
    # 标题的唯一来源是清单(scripts/allure_catalog.py), 写入处只引用它 —— 于是"标题里拼没拼
    # 平台名"只需要在一处盯住; 同时也要求写入处不要再各抄一份字面量(那样又会分叉出两份)。
    titles = {item.key: item.title for item in _load_script("allure_catalog").CATALOG}
    assert titles["performance"] == "Performance baseline"
    assert titles["security"] == "Security findings"
    assert 'title="Performance baseline"' not in summary, "标题不该在写入处再抄一份"
    for name in titles.values():
        assert not any(
            platform in name for platform in ("Windows", "Linux", "macOS")
        ), f"标题里不能拼平台名(平台由环境表达): {name}"


_COVERAGE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<coverage line-rate="0.9" branch-rate="0.8" lines-covered="9" lines-valid="10"
          branches-covered="8" branches-valid="10">
  <packages>
    <package name="archive_management" line-rate="0.9" branch-rate="0.8">
      <classes>
        <class name="widgets.py" filename="src/archive_management/ui/widgets.py">
          <lines>
            <line number="1" hits="1"/>
            <line number="2" hits="0"/>
          </lines>
        </class>
      </classes>
    </package>
  </packages>
</coverage>
"""

# 达标的一份(合计 (99+99)/(100+100) = 99% > fail_under 95): 结论行的"通过"分支靠它。
_COVERAGE_XML_PASSING = """<?xml version="1.0" encoding="UTF-8"?>
<coverage line-rate="0.99" branch-rate="0.99" lines-covered="99" lines-valid="100"
          branches-covered="99" branches-valid="100">
  <packages/>
</coverage>
"""


def test_coverage_item_lands_in_the_platform_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖率汇总项要归到本平台的"环境"里, 名称与身份都不再拼平台名.

    三个平台各跑一次这个脚本, 结果在汇总报告里合并: 靠 ``env`` 标签(配合仓库根的
    ``allurerc.mjs``)归到 Windows/macOS/Linux 三个环境; 平台若写进名称或身份, 报告里
    就会多出三个同名的独立条目 —— 与"用环境区分平台"的初衷相反。
    """
    module = _load_script("create_allure_coverage")
    results = tmp_path / "allure-results"
    coverage_xml = tmp_path / "coverage.xml"
    coverage_xml.write_text(_COVERAGE_XML_PASSING, encoding="utf-8")
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "COVERAGE_XML", coverage_xml)

    module.main([])

    written = next(results.glob("*-result.json"))
    payload = json.loads(written.read_text(encoding="utf-8"))
    platform = module.platform_name()
    labels = {label["name"]: label["value"] for label in payload["labels"]}

    assert payload["status"] == "passed"
    assert payload["name"] == "Coverage report"
    assert labels["env"] == platform
    assert labels["os"] == platform
    assert labels["severity"] == "trivial"
    assert payload["parameters"] == [{"name": "Platform", "value": platform}]
    assert platform not in payload["fullName"]
    assert platform not in payload["historyId"]
    assert "按包统计" in payload["description"]
    # 结论行就在描述开头: 报告里不用再去另一条结论项里对数字。
    assert payload["description"].startswith("## 结论")
    assert "当前覆盖率 99.00% 大于预期覆盖率 95%, 验证通过。" in payload["description"]


def test_coverage_item_fails_the_platform_when_below_the_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """低于门槛时这一项直接是失败, 并把结论写成失败原因(报告里的红/绿必须是真结论).

    门槛用 ``fail_under`` 的口径: 合计覆盖率 =(行覆盖 + 分支覆盖)/(行总数 + 分支总数),
    这份夹具是 (9+8)/(10+10) = 85%。
    """
    module = _load_script("create_allure_coverage")
    results = tmp_path / "allure-results"
    coverage_xml = tmp_path / "coverage.xml"
    coverage_xml.write_text(_COVERAGE_XML, encoding="utf-8")
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "COVERAGE_XML", coverage_xml)
    monkeypatch.setattr(module, "RAW_REPORT_FILES", (coverage_xml,))

    module.main([])

    payload = json.loads(next(results.glob("*-result.json")).read_text("utf-8"))
    assert payload["status"] == "failed"
    assert payload["statusDetails"]["message"].startswith("当前覆盖率 85.00%")
    assert "验证未通过" in payload["description"]
    # 原始报告照旧带上: 结论红了更要能下载下来看细节。
    assert [item["name"] for item in payload["attachments"]] == ["coverage.xml"]


def test_coverage_threshold_comes_from_pyproject() -> None:
    """结论文案里的门槛必须与 pyproject 的 fail_under 同源(读配置, 不另写一份常量)."""
    import tomllib

    module = _load_script("create_allure_coverage")
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    fail_under = pyproject["tool"]["coverage"]["report"]["fail_under"]

    assert module.coverage_threshold() == float(fail_under)
    assert module.verdict_text(0.96, module.coverage_threshold()).endswith("验证通过。")
    assert "验证未通过" in module.verdict_text(0.9, module.coverage_threshold())


def test_coverage_item_platform_comes_from_the_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--platform`` 压过宿主平台: 报告作业在 Ubuntu 上合并别的平台的数据.

    CI 的报告作业固定跑在 Ubuntu(合并、覆盖率汇总、报告生成都是纯文件操作), 而它处理的
    是 Windows / Linux 各自的分片数据 —— 用宿主平台判断的话, 两个平台的覆盖率结论都会
    被标成 Linux, 报告里"按平台看覆盖率"就失去意义(与平台专属检查同一个坑)。
    """
    module = _load_script("create_allure_coverage")
    results = tmp_path / "allure-results"
    coverage_xml = tmp_path / "coverage.xml"
    coverage_xml.write_text(_COVERAGE_XML, encoding="utf-8")
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "COVERAGE_XML", coverage_xml)

    module.main(["--platform", "Windows"])

    payload = json.loads(next(results.glob("*-result.json")).read_text("utf-8"))
    labels = {label["name"]: label["value"] for label in payload["labels"]}

    assert labels["env"] == "Windows"
    assert labels["os"] == "Windows"
    assert payload["parameters"] == [{"name": "Platform", "value": "Windows"}]
    # 不传就还是宿主平台(本地手动跑不需要记平台名)。
    assert module.resolve_platform(None) == module.platform_name()


def test_coverage_item_carries_the_raw_report_as_an_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """原始覆盖率报告要作为附件随条目一起进报告(与性能/安全汇总项同一做法)."""
    module = _load_script("create_allure_coverage")
    results = tmp_path / "allure-results"
    coverage_xml = tmp_path / "coverage.xml"
    coverage_xml.write_text(_COVERAGE_XML, encoding="utf-8")
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "COVERAGE_XML", coverage_xml)
    monkeypatch.setattr(module, "RAW_REPORT_FILES", (coverage_xml,))

    module.main([])

    payload = json.loads(next(results.glob("*-result.json")).read_text("utf-8"))
    attachment = payload["attachments"]
    assert [item["name"] for item in attachment] == ["coverage.xml"]
    assert attachment[0]["type"] == "application/xml"
    assert (results / attachment[0]["source"]).read_bytes() == coverage_xml.read_bytes()
    assert "## 原始报告" in payload["description"]


def test_coverage_item_attaches_only_the_raw_reports_that_exist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """存在哪个原始报告就带哪个: 多出来的终端文本照带, 缺失的不报错也不要占位."""
    module = _load_script("create_allure_coverage")
    results = tmp_path / "allure-results"
    coverage_xml = tmp_path / "coverage.xml"
    coverage_xml.write_text(_COVERAGE_XML, encoding="utf-8")
    text_report = tmp_path / "coverage-report.txt"
    text_report.write_text("Name  Stmts  Miss\n", encoding="utf-8")
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "COVERAGE_XML", coverage_xml)
    monkeypatch.setattr(
        module,
        "RAW_REPORT_FILES",
        (coverage_xml, text_report, tmp_path / "coverage.json"),
    )

    module.main([])

    payload = json.loads(next(results.glob("*-result.json")).read_text("utf-8"))
    assert [item["name"] for item in payload["attachments"]] == [
        "coverage.xml",
        "coverage-report.txt",
    ]
    assert [item["type"] for item in payload["attachments"]] == [
        "application/xml",
        "text/plain",
    ]


def test_coverage_item_without_the_raw_file_has_no_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖率文件缺失时条目照旧写成 broken, 但不挂一个不存在的附件."""
    module = _load_script("create_allure_coverage")
    results = tmp_path / "allure-results"
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "COVERAGE_XML", tmp_path / "coverage.xml")
    monkeypatch.setattr(module, "RAW_REPORT_FILES", (tmp_path / "coverage.xml",))

    module.main([])

    payload = json.loads(next(results.glob("*-result.json")).read_text("utf-8"))
    assert payload["status"] == "broken"
    assert payload["attachments"] == []
    assert "原始报告" not in payload["description"]


def test_quality_items_record_pass_and_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """质量门禁项: 逐项写成结果(状态跟随退出码), 带 env 标签与原始输出附件.

    用例用 ``sys.executable -c`` 冒充两项检查, 免得在用例里真跑 ruff/mypy
    (几秒到一分钟)。
    """
    module = _load_script("create_allure_quality")
    results = tmp_path / "allure-results"
    checks = (
        module.Check("ok", "OK check", (sys.executable, "-c", "print('all good')")),
        module.Check(
            "bad", "Bad check", (sys.executable, "-c", "import sys; sys.exit(3)")
        ),
    )
    monkeypatch.setattr(module, "CHECKS", checks)

    exit_code = module.main(["--results-dir", str(results)])

    assert exit_code == 1, "有检查未通过时脚本必须以非 0 退出(否则质量门禁形同虚设)"
    items = {
        str(payload["name"]): payload
        for payload in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(results.glob("*-result.json"))
        )
    }
    assert set(items) == {"OK check", "Bad check"}

    passed = items["OK check"]
    failed = items["Bad check"]
    platform = module.platform_name()
    assert passed["status"] == "passed"
    assert failed["status"] == "failed"
    assert "退出码 3" in failed["statusDetails"]["message"]
    assert "all good" in passed["description"]

    for payload in items.values():
        labels = {label["name"]: label["value"] for label in payload["labels"]}
        assert labels["severity"] == "trivial"
        assert payload["attachments"], "原始输出要作为附件带进报告"
        assert (results / payload["attachments"][0]["source"]).is_file()
        # 公共检查归入显式声明的环境, 不带 os 标签与平台参数, 见
        # test_quality_items_use_the_declared_common_environment; 执行主机只写进描述。
        assert labels["env"] == module.QUALITY_ENVIRONMENT
        assert "os" not in labels
        assert payload.get("parameters", []) == []
        assert f"本次执行于 {platform}" in payload["description"]


def test_quality_items_cover_every_ci_gate() -> None:
    """三组门禁都在脚本里, 且 CI 分三处调用它们: 漏一项报告就缺项."""
    module = _load_script("create_allure_quality")
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    core = [check.key for check in module.CHECKS if check.group == "core"]
    platform = [check.key for check in module.CHECKS if check.group == "platform"]
    analysis = [check.key for check in module.CHECKS if check.group == "analysis"]
    assert core == ["ruff-check", "ruff-format", "mypy"]
    assert platform == ["mypy-win32", "mypy-darwin"]
    assert analysis == ["deptry", "bandit", "pip-audit", "radon", "xenon"]
    # 平台专属分支: 这两项必须带着 --platform, 否则等于把宿主平台又跑了一遍。
    for check in (item for item in module.CHECKS if item.key.startswith("mypy-")):
        assert "--platform" in check.command
        assert check.host_platform is not None, "平台专属检查要声明自己在哪个平台跑"
    assert "scripts/create_allure_quality.py --group core" in workflow
    assert "scripts/create_allure_quality.py --group platform" in workflow
    assert "scripts/create_allure_quality.py --group analysis" in workflow
    assert "allure-results-quality" in workflow
    assert "allure-results-analysis" in workflow
    # 旧的散装命令不应再单独出现(否则同一批检查会跑两遍).
    assert "run: uv run ruff check ." not in workflow
    assert "run: uv run mypy" not in workflow


def test_complexity_gate_matches_ruffs_mccabe_limit() -> None:
    """复杂度门槛与 pyproject 里 Ruff 的 mccabe 上限必须是同一个数值.

    Radon 比 Ruff 的 C901 多数 ``with``/``assert``/布尔运算, 所以门槛放宽就会与
    Ruff 分叉, 收紧则会违背"两把尺子同分"的约定 —— 这条守卫把两者钉在一起。
    """
    import tomllib

    module = _load_script("create_allure_quality")
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    limit = pyproject["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"]
    assert limit == module.MAX_COMPLEXITY
    # 10 分对应 Radon 的 B 级(11 分起是 C 级).
    rank = module.COMPLEXITY_RANK
    assert rank == "B"
    xenon = next(check for check in module.CHECKS if check.key == "xenon")
    assert "--max-absolute" in xenon.command
    assert rank in xenon.command


def test_pre_commit_covers_the_dependency_check() -> None:
    """deptry 是唯一同时进本地钩子与 CI 的新工具(用户要求), 配置不能掉."""
    config = _PRE_COMMIT.read_text(encoding="utf-8")
    assert "uv run deptry" in config
    # 依赖清单变了也要重查, 所以钩子的触发范围不止 .py。
    assert "pyproject\\.toml" in config
    assert "uv\\.lock" in config


def test_main_survives_a_cp1252_console(
    layout: _Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """英文版 Windows(CI runner) 的控制台是 cp1252: 打印中文不能崩.

    实际踩过: ``UnicodeEncodeError: 'charmap' codec can't encode characters``
    把报告自检直接打断。脚本自己把输出切成 UTF-8, 因此换成 cp1252 流也能跑完。
    """
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    exit_code = verifier.main([str(layout.report), "--results", str(layout.results)])
    stream.flush()
    printed = buffer.getvalue().decode("utf-8")

    assert exit_code == 0
    assert "报告资源完整" in printed


def _write_result(
    results: Path, slug: str, name: str, status: str, category: str
) -> None:
    """写一条极简 Allure 结果(两条断言用得到: 名称、状态与类别标签)."""
    payload = {
        "uuid": slug,
        "name": name,
        "status": status,
        "labels": [{"name": "testCategory", "value": category}],
    }
    (results / f"{slug}-result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_quality_gate_report_aggregates_only_quality_checks(tmp_path: Path) -> None:
    """总评只算质量检查项: 性能/安全汇总项不能被当成门禁条目."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_result(results, "ruff", "ruff check", "passed", "quality")
    _write_result(results, "mypy", "mypy", "failed", "quality")
    _write_result(results, "perf", "Performance baseline", "passed", "performance")

    assert module.quality_checks(results) == [
        ("mypy", "failed"),
        ("ruff check", "passed"),
    ]
    report = module.quality_gate_report(module.quality_checks(results))
    assert "未通过(1/2 项)" in report
    assert "- 未通过: mypy" in report
    assert "Performance" not in report


def test_quality_gate_report_without_checks() -> None:
    """没跑质量作业的运行(例如本地只跑单元测试)不能说成"通过"."""
    module = _load_script("create_allure_summary")
    report = module.quality_gate_report([])
    assert "不适用" in report
    assert "通过" not in report.replace("不适用", "")


def test_global_attachment_matches_what_the_summary_writes() -> None:
    """首页「全局附件」靠配置里的 glob 匹配 —— 文件名与脚本写的必须一致.

    两份都要在: 运行总账(``QUALITY_GATE_REPORT``)与"有意不统计的覆盖"豁免清单
    (``COVERAGE_EXCLUSIONS_REPORT``)。写错一个名字不会报错, 只是报告里少一份附件。
    """
    module = _load_script("create_allure_summary")
    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    matched = re.search(r"globalAttachments:\s*\[([^\]]*)\]", config)
    assert matched is not None, "配置里要有 globalAttachments"
    # 数组写成多行时会有尾随逗号与缩进, 过滤掉空项再看集合。
    names = {
        name
        for item in matched.group(1).split(",")
        if (name := item.strip().strip('"')) and not name.startswith("...")
    }
    assert names == {
        module.QUALITY_GATE_REPORT.name,
        module.COVERAGE_EXCLUSIONS_REPORT.name,
    }
    # 失败现场那条是**条件**收的(没有兜底现场时那份文件根本不存在), 所以不在这里的集合里
    # —— 两处一起由 tests/unit/test_ci_diagnostics.py 守着。
    assert module.FAILURE_DIAGNOSTICS_REPORT.name in config


def _version_tuple(text: str) -> tuple[int, ...]:
    """把 ``"3.19.1"`` 这样的版本号拆成可比大小的元组(段数不同的也能比)."""
    return tuple(int(part) for part in text.split("."))


def test_per_platform_reports_skip_the_native_gate() -> None:
    """逐平台的报告生成要用**不带质量门**的配置 —— 3.20.0 起 generate 阶段也会跑门.

    门里的 ``environmentsTested`` 只在**汇总**那份报告里成立; ``pytest-report`` 每次都只合并
    一个平台, 用主配置生成会退 1(实测 3.20.0: 同样输入在 3.19.1 下能生成成功)。汇总作业
    继续用主配置 —— 它本来就是跑门的地方。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    config = (_REPO_ROOT / "allurerc.per-platform.mjs").read_text(encoding="utf-8")

    assert 'from "./allurerc.mjs"' in config, "设置只有一处真相: 从主配置继承"
    assert "qualityGate" in config, "它要说明自己摘掉的是什么"
    assert (
        "allure generate allure-results --output allure-report "
        "--config allurerc.per-platform.mjs" in workflow
    )
    # 汇总作业那句仍不带 --config: 它要用主配置里的门。
    assert "allure generate allure-results --output allure-report\n" in workflow


def test_the_coverage_conclusion_is_published() -> None:
    """覆盖率结论必须**单独上传** —— 合并后的 ``allure-results/`` 不再上传(见 PLAN §41).

    2026-10-04 实测: 少了这一份, 汇总报告里三个平台全部报“缺少结论: Coverage report”,
    而报告作业自己是绿的 —— 这类“漏了个消费者”在作业状态上看不出来, 只能靠守卫钉。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    assert "--publish-dir allure-results-coverage" in workflow, (
        "结论要复制到可上传的目录"
    )
    assert "name: allure-results-coverage-${{ matrix.platform }}" in workflow, (
        "三个平台的这个作业都跑在 ubuntu 上, 用 matrix.os 会撞名"
    )
    # 上传了还得被收走: 汇总作业那**一个** pattern 要能匹配到它。
    assert "pattern: allure-results-*" in workflow


def test_native_quality_gate_is_configured_and_pinned() -> None:
    """Allure 原生质量门: 规则写在配置里, CI 跑它并用退出码定成败, CLI 钉在 3.18.0.

    曾经的结论是"不用原生门禁": 3.13~3.17 一旦配了 ``historyPath`` 就静默放行(退 0
    且不输出任何内容 —— 本地历史流句柄悬空, ``AllureReport.done()`` 永不返回, issue
    #895), 而本仓库必须有 ``historyPath``。3.18.0 的 PR #962 修好后它才可用, 所以这里
    同时锁住"配了规则"与"CLI 钉在修好的版本上" —— 版本一旦被改回浮动标签或降到 3.18
    以下, 门禁会静默失效而没人发现。

    另外: ``allure generate`` 不执行校验, 所以首页「质量门」页签仍靠 ``allure run`` 填;
    CI 把这里门的输出写进日志, 由运行总账收进报告首页「全局附件」。
    """
    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    assert re.search(r"^\s*qualityGate\s*:", config, re.MULTILINE) is not None
    for rule in ("maxFailures", "successRate", "environmentsTested"):
        assert rule in config, f"配置里缺少规则 {rule}"
    minimum = re.search(r"版本要求 ≥ ([0-9.]+)", config)
    assert minimum is not None, "注释里要写明版本要求与原因"
    assert "#895" in config, "注释里要留 issue 号, 便于日后重测"

    workflow = _WORKFLOW.read_text(encoding="utf-8")
    gate = "allure quality-gate --config allurerc.mjs allure-results"
    assert gate in workflow, "CI 要真的跑原生质量门"
    # 版本判据是**两处的关系**, 不是写死一个号: 工作流里钉的版本必须 ≥ 配置里写的最低要求,
    # 而且工作流里只能有一处出处。2026-10-03 的 CI 就红在这条上 —— 工作流已经升到 3.19.1,
    # 而断言还在找 3.18.0(两处都是人肉同步的注释, 不同步时没有东西会提醒)。
    pinned = set(re.findall(r"npm install --global allure@([0-9.]+)", workflow))
    assert len(pinned) == 1, f"CLI 版本必须只有一处出处: {sorted(pinned)}"
    version = pinned.pop()
    # 两种写法都允许, 但都必须真的满足下限:
    #   * 钉住的完整版本(``3.19.1``): 静态就能判;
    #   * 浮动标签(``3``): 必须在 CI 里**运行期**判定, 否则 3.13~3.17 那种"静默放行门槛"
    #     会静默回来(2026-10-04: 用户有意把版本改成动态取最新, 下限因此从静态改成运行期)。
    parts = _version_tuple(version)
    if len(parts) < 2:
        # 浮动标签: 下限静态判不了, 必须在 CI 里**运行期**比一次, 而且过期时要响亮地红
        # (3.13~3.17 配 historyPath 会静默放行质量门, 那种失效在报告里看不出来)。
        assert "allure --version" in workflow, "浮动标签必须当场打印实际版本"
        assert "::error::Allure CLI" in workflow, "浮动标签要在 CI 里真的比一次下限"
        assert parts[0] >= _version_tuple(minimum.group(1))[0], (
            f"CI 用的浮动标签 {version} 连最低要求的主版本都不到"
        )
    else:
        assert parts >= _version_tuple(minimum.group(1)), (
            f"CI 钉的 {version} 低于配置里要求的最低版本 {minimum.group(1)}"
        )
    assert "Report quality gate verdict" in workflow, "门禁结论要能决定作业成败"
    # 先跑门禁(留日志给总账), 再生成报告: 顺序反了日志就进不了报告。
    assert workflow.index(gate) < workflow.index("Generate final Allure report")
    # 还得**先于写运行总账那一步**: 总账在 "Summarize performance and security into Allure"
    # 里拼, 它把门禁日志收进首页「全局附件」的「原生质量门」一节 —— 2026-10-04 之前
    # 顺序是反的, 那一节永远写着"本次没有质量门输出"。
    assert workflow.index(gate) < workflow.index(
        "Summarize performance and security into Allure"
    ), "门禁日志先落地, 运行总账才收得到它"


def test_quality_gate_asks_every_platform_for_real_tests() -> None:
    """要求三个平台各自都有真实用例, 而不是靠会漂的绝对计数.

    ``minTestsCount: 3000`` 这样的常量会随用例规模往**更松**的方向漂: 实测签名是
    3P+154(每平台 P 条用例), 每平台涨到 1400 上下之后, 缺一整个平台的运行也仍然高于
    3000 —— 规则静默失效, 而且失效时没有任何信号。所以改成滤掉脚本生成的结论项后要求
    三个环境里都还有用例(环境维度上一个平台都没有时会直接报"未被测试")。

    判据必须是**正向**的(必须有 ``framework=pytest``), 否则会悄悄变绿: 反向判据
    ("不能带 testCategory")遇到将来某个脚本忘了打标签, 那条汇总项就会被当成真实用例,
    规则从此失去意义 —— 而正向判据漏判时会直接变红。因此这里同时锁住两件事: 配置里
    按 ``framework`` 选, 而 ``scripts/`` 下的产物一律不写这个标签。
    """
    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    # 注释里可以拿它当反例说明(所以只禁止它作为**规则**出现)。
    assert re.search(r"^\s*minTestsCount\s*:", config, re.MULTILINE) is None, (
        "绝对计数会随规模变松, 已换成环境维度"
    )
    gate = config.split("qualityGate:", 1)[1]
    assert "filter:" in gate, "只看真实用例的规则集要带 filter"
    # 判据必须与校验脚本一致: 两处都在回答"这条结果是用例还是脚本产物"。
    assert verifier.REAL_TEST_LABEL == "framework"
    assert verifier.REAL_TEST_VALUE == "pytest"
    assert f'name === "{verifier.REAL_TEST_LABEL}"' in gate
    assert f'value === "{verifier.REAL_TEST_VALUE}"' in gate
    # 期望值必须是**环境 id**(小写): 门禁比的是结果的 `environment`(环境身份里的 id),
    # 写平台显示名一个都比不上(2026-10-04: 那样会一次报全三个"未被测试", 而三条平台的
    # 用例其实都在; 本地用三平台最小结果复现过, 见 PLAN.md §48.6)。
    ids = required_environment_ids()
    assert ids == ("windows", "macos", "linux"), (
        "要求哪几个平台要有用例: 改这里要同步 CI 矩阵与 --expect-platforms(见同文件那条守卫)"
    )
    assert all(env_id == env_id.lower() for env_id in ids), (
        "环境 id 是小写的; 写成显示名('Windows')会导致规则一个都比不上"
    )
    assert declared_environment_names()["windows"] == "Windows"
    # 规则集要有 id: 门禁失败时输出的是 `<规则集 id>/<规则名>`, 一眼看出是哪条不过。
    assert re.search(r'id:\s*"[\w-]+"', gate) is not None

    # 只有"写结果"的脚本受这条约束(校验脚本要**读**这个标签, 不在其列: 它自己就是
    # 用 framework=pytest 判断"这条结果是用例还是脚本产物"的那个实现)。
    writers = (
        "create_allure_quality",
        "create_allure_coverage",
        "create_allure_summary",
        "create_allure_visual",
    )
    for script in writers:
        text = (_REPO_ROOT / "scripts" / f"{script}.py").read_text(encoding="utf-8")
        assert '"framework"' not in text, (
            f"{script}.py 写了 framework 标签: 脚本生成的结论项会被当成真实用例"
        )


def test_run_ledger_has_every_section_and_flags_missing_artifacts(
    tmp_path: Path,
) -> None:
    """运行总账要覆盖四类结论与产物清单, 而且看得出原始文件缺失.

    总账是报告首页唯一能回答"这次运行产出了什么、原始数据在哪"的地方, 也是全局附件
    不在 ``verify_allure_report.py`` 校验范围内时补上的那道核对。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_result(results, "ruff", "Ruff check", "passed", "quality")
    (results / "cov-attachment.xml").write_text(
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="0.9182" branch-rate="0.75" version="7.5.0">\n</coverage>\n',
        encoding="utf-8",
    )
    (results / "cov-result.json").write_text(
        json.dumps(
            {
                "uuid": "cov",
                "name": "Coverage report",
                "fullName": "archive-management.coverage",
                "status": "passed",
                "labels": [{"name": "env", "value": "Windows"}],
                "attachments": [
                    {"name": "coverage.xml", "source": "cov-attachment.xml"},
                    {"name": "coverage.json", "source": "missing.json"},
                ],
            }
        ),
        encoding="utf-8",
    )
    performance = {
        "environment": {"os_family": "Linux"},
        "measurements": [{"name": "backup", "passed": True}],
    }
    security = [
        {"environment": {"os_family": "Linux"}, "findings": [{"blocked": True}]}
    ]

    payloads = module.result_payloads(results)
    ledger = module.run_ledger(results, payloads, performance, security)

    assert ledger.startswith("# 运行总账")
    for section in (
        "## 质量门",
        "## 覆盖率",
        "## 性能基准",
        "## 安全测试",
        "## 证据核对(应有 vs 实有)",
        "### ① 结论清单",
        "### ② 结论项声明的附件文件",
    ):
        assert section in ledger
    # 读序: 结论在前, 审计性的核对在末尾 —— 但"缺不漏"的那一行数留在开头。
    assert ledger.index("## 安全测试") < ledger.index("## 证据核对(应有 vs 实有)")
    assert ledger.rstrip().endswith("说明这段证据在打包/下载环节丢了。")
    assert "- 证据核对: 应有 " in ledger, "开头要留一行数, 不然移到末尾就看不出来了"
    assert "91.82%" in ledger, "覆盖率数字要取自原始 XML"
    assert "75.00%" in ledger, "分支覆盖率也要写出来"
    assert "?" not in ledger.split("## 覆盖率")[1].split("##")[0], "覆盖率不该是问号"
    assert "| 平台 | 结论条数 | 未拦截条数 | 失败用例 | 结论 | 原始结论 |" in ledger, (
        "表头要说清每一列的含义"
    )
    assert "已收录" in ledger, "产物清单的状态列要用能看懂的词"
    assert "| Windows |" in ledger, "覆盖率按平台各一行"
    assert "全部达标(1 项)" in ledger, "性能基准给一行结论"
    assert "| Linux | 1 | 0 | 0 | 通过 |" in ledger, (
        "安全那一行要给出结论数、未拦截数、失败用例与结论"
    )
    assert "**缺失**" in ledger, "声明了却找不到的原始文件必须被标出来"


def _write_security_case(
    results: Path, *, status: str, platform: str = "Linux", name: str = "某条安全用例"
) -> None:
    """写一条安全用例的结果(layer=security): 汇总侧靠这两个标签认出它."""
    payload = {
        "uuid": "sec-case",
        "name": name,
        "status": status,
        "labels": [
            {"name": "layer", "value": "security"},
            {"name": "env", "value": platform},
        ],
        "statusDetails": {"message": "AssertionError: 越界变更"},
    }
    (results / "sec-case-result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_security_summary_reflects_a_failed_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """安全用例红了, 汇总结论项就必须跟着红(2026-09-25 的 Linux 就是这样漏掉的).

    安全用例只在 security 作业里跑(不在 pytest 分片里), 它的成败原本只体现在那个作业
    的状态上 —— 报告里 2641 条结果有 1 条 failed, 而 `Security findings` 那条结论项
    仍然写 passed, 于是“哪一条挂了”在报告里完全查不到。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    findings = tmp_path / "security-findings" / "Linux"
    findings.mkdir(parents=True)
    (findings / "security-results.json").write_text(
        json.dumps(
            {
                "environment": {"os_family": "Linux"},
                "findings": [
                    {
                        "category": "side_effects",
                        "scenario": "真实流水线只改两处",
                        "input_summary": "备份+恢复+删除存档位置",
                        "expected": "只动应用区域与存档位置",
                        "actual": "恢复时越界删了 slot.dat",
                        "blocked": False,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_security_case(
        results, status="failed", name="The pipeline only changes the app area"
    )
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "SECURITY_JSON", tmp_path / "security-results.json")
    monkeypatch.setattr(
        module, "SECURITY_FINDINGS_DIRECTORY", tmp_path / "security-findings"
    )
    monkeypatch.setattr(
        module, "PERFORMANCE_JSON", tmp_path / "performance-results.json"
    )
    monkeypatch.setattr(
        module, "QUALITY_GATE_REPORT", tmp_path / "allure-run-ledger.md"
    )
    monkeypatch.setattr(
        module, "COVERAGE_EXCLUSIONS_REPORT", tmp_path / "allure-coverage-exclusions.md"
    )
    monkeypatch.setattr(module, "NATIVE_GATE_LOG", tmp_path / "allure-quality-gate.txt")

    assert module.main() == 0

    written = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in results.glob("*-result.json")
    ]
    item = next(
        payload for payload in written if payload["name"] == "Security findings"
    )

    assert item["status"] == "failed", "有安全用例红了, 汇总项不能还是绿的"
    assert "**未通过**" in item["description"], "描述开头就要给出结论"
    assert "The pipeline only changes the app area" in item["description"]
    assert "越界删了 slot.dat" in item["description"], "未拦截的结论也要摆出来"
    assert "越界变更" in item["statusDetails"]["message"], "原因要能直接看到"


def test_security_summary_stays_green_when_everything_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全部拦下且用例都通过时仍然是绿的(别把结论项写成永远失败)."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    findings = tmp_path / "security-findings" / "Linux"
    findings.mkdir(parents=True)
    (findings / "security-results.json").write_text(
        json.dumps(
            {
                "environment": {"os_family": "Linux"},
                "findings": [{"scenario": "路径越界", "blocked": True}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    _write_security_case(results, status="passed", name="某条安全用例")
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "SECURITY_JSON", tmp_path / "security-results.json")
    monkeypatch.setattr(
        module, "SECURITY_FINDINGS_DIRECTORY", tmp_path / "security-findings"
    )
    monkeypatch.setattr(
        module, "PERFORMANCE_JSON", tmp_path / "performance-results.json"
    )
    monkeypatch.setattr(
        module, "QUALITY_GATE_REPORT", tmp_path / "allure-run-ledger.md"
    )
    monkeypatch.setattr(
        module, "COVERAGE_EXCLUSIONS_REPORT", tmp_path / "allure-coverage-exclusions.md"
    )
    monkeypatch.setattr(module, "NATIVE_GATE_LOG", tmp_path / "allure-quality-gate.txt")

    assert module.main() == 0

    written = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in results.glob("*-result.json")
    ]
    item = next(
        payload for payload in written if payload["name"] == "Security findings"
    )

    assert item["status"] == "passed"
    assert "**通过**" in item["description"]
    assert "statusDetails" not in item


def test_security_ledger_row_carries_the_verdict(tmp_path: Path) -> None:
    """总账里的安全行要说清: 几条结论、几条没拦住、几个用例失败、到底过没过."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_security_case(results, status="broken")
    payloads = module.result_payloads(results)
    security = [
        {"environment": {"os_family": "Linux"}, "findings": [{"blocked": True}]}
    ]

    ledger = module.run_ledger(results, payloads, None, security)

    assert "| Linux | 1 | 0 | 1 | **未通过** |" in ledger


def _patch_summary_paths(
    module: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, results: Path
) -> None:
    """把汇总脚本的全部输入/输出指到临时目录(否则会写进仓库根)."""
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(
        module, "PERFORMANCE_JSON", tmp_path / "performance-results.json"
    )
    monkeypatch.setattr(module, "PERFORMANCE_CSV", tmp_path / "performance-results.csv")
    monkeypatch.setattr(module, "SECURITY_JSON", tmp_path / "security-results.json")
    monkeypatch.setattr(
        module, "SECURITY_FINDINGS_DIRECTORY", tmp_path / "security-findings"
    )
    monkeypatch.setattr(
        module, "QUALITY_GATE_REPORT", tmp_path / "allure-run-ledger.md"
    )
    monkeypatch.setattr(
        module, "COVERAGE_EXCLUSIONS_REPORT", tmp_path / "allure-coverage-exclusions.md"
    )
    monkeypatch.setattr(module, "NATIVE_GATE_LOG", tmp_path / "allure-quality-gate.txt")


def _measurement(*, passed: bool, value: float = 1.5) -> dict[str, Any]:
    """一条性能测量(阈值 1.0 秒, 越大越差): 与 performance-results.json 同形."""
    return {
        "name": "主页基准",
        "scale": "2000 款",
        "metric": "duration",
        "value": value,
        "unit": "seconds",
        "threshold": 1.0,
        "comparison": "max",
        "passed": passed,
    }


def _write_performance(tmp_path: Path, *measurements: dict[str, Any]) -> Path:
    """写一份性能结果文件(平台按 Linux 记, 与 CI 的 quality 作业一致)."""
    path = tmp_path / "performance-results.json"
    path.write_text(
        json.dumps(
            {
                "category": "performance",
                "environment": {"os_family": "Linux"},
                "measurements": list(measurements),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return path


def _summary_item(results: Path, name: str) -> dict[str, Any]:
    """从结果目录里取一条脚本生成的结论项."""
    return next(
        payload
        for payload in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(results.glob("*-result.json"))
        )
        if payload["name"] == name
    )


def _write_coverage_item(
    results: Path,
    *,
    platform: str,
    line_rate: float,
    branch_rate: float,
    status: str = "passed",
    result_id: str = "cov",
) -> None:
    """写一条覆盖率结论项: 数字直接写在 XML 根属性上(与 coverage.py 的输出同形)."""
    attachment = results / f"{result_id}-attachment.xml"
    attachment.write_text(
        '<?xml version="1.0" ?>\n'
        f'<coverage line-rate="{line_rate}" branch-rate="{branch_rate}" '
        f'lines-covered="{int(line_rate * 100)}" lines-valid="100" '
        f'branches-covered="{int(branch_rate * 100)}" branches-valid="100">\n'
        "</coverage>\n",
        encoding="utf-8",
    )
    (results / f"{result_id}-result.json").write_text(
        json.dumps(
            {
                "uuid": result_id,
                "name": "Coverage report",
                "fullName": "archive-management.coverage",
                "status": status,
                "labels": [{"name": "env", "value": platform}],
                "attachments": [
                    {
                        "name": "coverage.xml",
                        "source": attachment.name,
                        "type": "application/xml",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


def test_performance_conclusion_fails_a_breached_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """有一条基准冲破预算, 性能结论项就必须红(带具体数字)."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _patch_summary_paths(module, monkeypatch, tmp_path, results)
    monkeypatch.setattr(
        module,
        "PERFORMANCE_JSON",
        _write_performance(tmp_path, _measurement(passed=False)),
    )

    assert module.main() == 0

    item = _summary_item(results, "Performance baseline")
    assert item["status"] == "failed", "基准未达标时结论项不能还是绿的"
    assert "**未通过**" in item["description"]
    assert "1.5" in item["description"]
    assert "1.0" in item["description"]
    assert "未达标" in item["statusDetails"]["message"]


def test_performance_conclusion_stays_green_when_every_budget_is_met(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """全部达标时仍然是绿的(别把结论项写成永远失败)."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _patch_summary_paths(module, monkeypatch, tmp_path, results)
    monkeypatch.setattr(
        module,
        "PERFORMANCE_JSON",
        _write_performance(tmp_path, _measurement(passed=True)),
    )

    assert module.main() == 0

    item = _summary_item(results, "Performance baseline")
    assert item["status"] == "passed"
    assert "**通过**" in item["description"]
    assert "statusDetails" not in item


def test_performance_conclusion_is_broken_when_the_result_file_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """性能结果文件缺失时写 broken 并给出原因 —— 不能"没有这条"就算通过."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _patch_summary_paths(module, monkeypatch, tmp_path, results)

    assert module.main() == 0

    item = _summary_item(results, "Performance baseline")
    assert item["status"] == "broken"
    assert "缺少性能结果文件" in item["statusDetails"]["message"]


def test_coverage_conclusion_fails_a_platform_below_the_threshold(
    tmp_path: Path,
) -> None:
    """覆盖率低于 fail_under 的平台要判红, 并把具体数字写出来."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_coverage_item(results, platform="Windows", line_rate=0.9, branch_rate=0.9)

    status, failures, rows = module.coverage_conclusion(
        results, module.result_payloads(results), ["Windows"]
    )

    assert status == "failed"
    assert any("Windows" in item and "90.00%" in item for item in failures)
    assert rows == [("Windows", "90.00%", "90.00%", "90.00%", "95%", "未通过")]


def test_coverage_conclusion_stays_green_above_the_threshold(tmp_path: Path) -> None:
    """达标的运行保持绿色: 合计覆盖率按 coverage.py 的口径算."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_coverage_item(results, platform="Linux", line_rate=0.99, branch_rate=0.99)

    status, failures, rows = module.coverage_conclusion(
        results, module.result_payloads(results), ["Linux"]
    )

    assert status == "passed"
    assert failures == []
    assert rows == [("Linux", "99.00%", "99.00%", "99.00%", "95%", "通过")]


def test_coverage_conclusion_is_red_when_a_platform_has_no_item(
    tmp_path: Path,
) -> None:
    """有用例结果的平台缺一份覆盖率结论时必须红(文件不在不等于通过)."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_coverage_item(results, platform="Windows", line_rate=0.99, branch_rate=0.99)

    status, failures, rows = module.coverage_conclusion(
        results, module.result_payloads(results), ["Windows", "Linux"]
    )

    assert status == "failed"
    assert any("Linux" in item and "缺少覆盖率结论项" in item for item in failures)
    assert {row[0]: row[5] for row in rows} == {"Windows": "通过", "Linux": "未通过"}


def test_coverage_conclusion_is_red_when_the_numbers_cannot_be_read(
    tmp_path: Path,
) -> None:
    """结论项在、但附带的 coverage.xml 丢了时必须红 —— "读不到数字"不等于通过."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_coverage_item(results, platform="Windows", line_rate=0.99, branch_rate=0.99)
    (results / "cov-attachment.xml").unlink()

    status, failures, rows = module.coverage_conclusion(
        results, module.result_payloads(results), ["Windows"]
    )

    assert status == "failed"
    assert any("Windows" in item and "读不到覆盖率数字" in item for item in failures)
    assert rows == [("Windows", "?", "?", "?", "95%", "未通过")]


def test_coverage_conclusion_without_any_data_is_broken(tmp_path: Path) -> None:
    """一条覆盖率结论项都没有时记 broken —— 不能因为"没有可比的东西"就写通过."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()

    status, failures, rows = module.coverage_conclusion(
        results, module.result_payloads(results), []
    )

    assert status == "broken"
    assert any("没有任何覆盖率结论项" in item for item in failures)
    assert rows == []


def test_the_summary_writes_no_separate_coverage_conclusion_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """覆盖率结论写在各平台的 `Coverage report` 项里, 汇总脚本不再单出一条结论项.

    以前汇总脚本会另写一条 `Coverage conclusion`(平台 unknown) —— 同一个数字在报告里
    出现两次, 还得两条对着看。现在结论行跟着平台自己那条走, 汇总只把它写进运行总账。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _patch_summary_paths(module, monkeypatch, tmp_path, results)
    (results / "case-result.json").write_text(
        json.dumps(
            {
                "uuid": "case",
                "name": "某个用例",
                "status": "passed",
                "labels": [{"name": "os", "value": "Windows"}],
            }
        ),
        encoding="utf-8",
    )
    _write_coverage_item(results, platform="Windows", line_rate=0.9, branch_rate=0.9)

    assert module.main() == 0

    names = [
        json.loads(path.read_text(encoding="utf-8"))["name"]
        for path in sorted(results.glob("*-result.json"))
    ]
    assert "Coverage conclusion" not in names
    assert not hasattr(module, "COVERAGE_CONCLUSION_TITLE")
    # 结论仍然看得见: 总账里逐平台列出数字与达标结论, 并指向各平台自己那一项。
    ledger = module.run_ledger(
        results, module.result_payloads(results), None, [], ["Windows"]
    )
    assert "## 覆盖率" in ledger
    assert "`Coverage report`" in ledger
    assert "| Windows |" in ledger


def test_coverage_fail_under_matches_pyproject() -> None:
    """覆盖率门槛与 pyproject 的 fail_under 必须是同一个数(两处打架时会自相矛盾)."""
    import tomllib

    module = _load_script("create_allure_summary")
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    fail_under = pyproject["tool"]["coverage"]["report"]["fail_under"]
    assert str(fail_under) == module.COVERAGE_THRESHOLD


def test_summary_writes_the_coverage_exclusions_attachment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """汇总脚本要写出"有意不统计的覆盖"清单(报告首页的全局附件)."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _patch_summary_paths(module, monkeypatch, tmp_path, results)

    assert module.main() == 0

    text = (tmp_path / "allure-coverage-exclusions.md").read_text(encoding="utf-8")
    assert "# 有意不统计的覆盖率(豁免清单)" in text
    # 数据来自真实源码 + pyproject: 两节都要在(本仓库确实有豁免, 所以按文件一节非空).
    assert "## 按文件" in text
    assert "## `exclude_also`(pyproject.toml)" in text


def test_run_ledger_columns_carry_the_performance_and_coverage_verdicts(
    tmp_path: Path,
) -> None:
    """总账里的性能/覆盖率一节要给出计数与结论(与安全列同一风格)."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_coverage_item(results, platform="Windows", line_rate=0.9, branch_rate=0.9)
    payloads = module.result_payloads(results)
    breached = {
        "environment": {"os_family": "Linux"},
        "measurements": [_measurement(passed=False)],
    }

    failing = module.run_ledger(results, payloads, breached, [], ["Windows"])

    assert "- 结论: **未通过(1 条)**" in failing
    assert "| 平台 | 行覆盖率 | 分支覆盖率 | 合计 | 门槛 | 结论 | 原始报告 |" in failing
    assert "| Windows | 90.00% | 90.00% | 90.00% | 95% | 未通过 |" in failing
    assert "| 平台 | 基准数 | 未达标 | 结论 |" in failing
    assert "| Linux | 1 | 1 | **未通过** |" in failing

    healthy_results = tmp_path / "healthy-results"
    healthy_results.mkdir()
    _write_coverage_item(
        healthy_results, platform="Windows", line_rate=0.99, branch_rate=0.99
    )
    healthy = module.run_ledger(
        healthy_results,
        module.result_payloads(healthy_results),
        {
            "environment": {"os_family": "Linux"},
            "measurements": [_measurement(passed=True)],
        },
        [],
        ["Windows"],
    )

    assert "- 结论: **通过**" in healthy
    assert "| Windows | 99.00% | 99.00% | 99.00% | 95% | 通过 |" in healthy
    assert "| Linux | 1 | 0 | 通过 |" in healthy


def test_quality_items_use_the_declared_common_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """质量检查是公共内容: 结论项带 env=common, 且配置里确实声明了匹配它的环境.

    写成 ``env=Linux`` 会让它变成"某个平台的质量检查"(误导); 完全不写则落进隐式的
    ``default``, 与环境选择器里的名字对不上。这里跑真实写入路径, 并把脚本里的标签值
    与 ``allurerc.mjs`` 的 matcher 钉在一起 —— 两边写岔了报告里会静默退回 default。
    """
    module = _load_script("create_allure_quality")
    probe = module.Check(
        "probe", "Probe check", (sys.executable, "-c", "print('ok')"), "core"
    )
    monkeypatch.setattr(module, "CHECKS", (probe,))
    results = tmp_path / "allure-results"

    assert module.main(["--group", "core", "--results-dir", str(results)]) == 0

    payload = json.loads(
        next(results.glob("*-result.json")).read_text(encoding="utf-8")
    )
    labels = {label["name"]: label["value"] for label in payload["labels"]}
    assert labels["testCategory"] == "quality"
    assert labels["env"] == module.QUALITY_ENVIRONMENT, "公共检查要归入显式声明的环境"
    assert "os" not in labels, "os 标签会把公共检查说成某个平台的结果"
    assert payload.get("parameters", []) == [], "不要再带平台参数"
    assert "env=common" in payload["description"], "描述里要说明它归入哪个环境"

    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    assert 'name: "Common"' in config, "配置里要声明这个环境"
    matcher = f'value === "{module.QUALITY_ENVIRONMENT}"'
    assert matcher in config, f"配置里缺少匹配 {matcher} 的 matcher"


def test_platform_specific_checks_use_the_platform_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台专属检查要落进**那个平台**的环境里, 而不是 Common.

    ``mypy --platform win32`` 验的是 Windows 专属代码路径, 结论放在 ``Common`` 里会被混进
    "与平台无关"的一堆结论中; 放进 Windows 环境才能和那个平台的测试结果一起看(环境选择器
    一筛就只剩这个平台的东西)。环境标签由 ``Check.host_platform`` 推出, 所以"标签"与
    "该在哪台机器上跑"是同一个事实, 不会各自漂移; 本用例把派生结果与 ``allurerc.mjs`` 里
    的环境名、matcher 钉在一起。
    """
    module = _load_script("create_allure_quality")
    assert module.PLATFORM_ENVIRONMENTS == {"win32": "Windows", "darwin": "macOS"}

    # 真实的检查表: 两条平台专属的 mypy 检查必须带上各自平台的环境标签。
    environments = {check.key: check.environment for check in module.CHECKS}
    assert environments["mypy-win32"] == "Windows"
    assert environments["mypy-darwin"] == "macOS"
    assert environments["mypy"] == module.QUALITY_ENVIRONMENT
    assert environments["ruff-check"] == module.QUALITY_ENVIRONMENT

    results = tmp_path / "allure-results"
    probes = tuple(
        module.Check(
            f"probe-{flag}",
            f"Probe check ({flag})",
            (sys.executable, "-c", "print('ok')"),
            group="platform",
            host_platform=flag,
        )
        for flag in module.PLATFORM_ENVIRONMENTS
    )
    monkeypatch.setattr(module, "CHECKS", probes)
    # 探针归各自平台所有, 而当前机器只可能是其中之一: 把"当前平台"固定成 win32,
    # 于是这条用例在任何平台上都跑同一套流程(另一条探针走"跳过"分支)。
    original_select = module.select_checks
    monkeypatch.setattr(
        module,
        "select_checks",
        lambda group, _host: original_select(group, "win32"),
    )

    assert module.main(["--group", "platform", "--results-dir", str(results)]) == 0

    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    payloads = [
        json.loads(item.read_text(encoding="utf-8"))
        for item in sorted(results.glob("*-result.json"))
    ]
    assert len(payloads) == 1, "只有当前平台那一支会产出结果"
    payload = payloads[0]
    labels = {label["name"]: label["value"] for label in payload["labels"]}
    assert labels["testCategory"] == "quality"
    # 平台由 env 表达: 不带 os 标签(也不带平台参数), 执行主机只写进描述。
    assert "os" not in labels
    assert payload.get("parameters", []) == []
    # 跑在 win32 上, 所以结果的环境就是 Windows(而不是 Common)。
    assert labels["env"] == module.PLATFORM_ENVIRONMENTS["win32"]
    assert f"env={module.PLATFORM_ENVIRONMENTS['win32']}" in payload["description"]
    for environment in module.PLATFORM_ENVIRONMENTS.values():
        assert f'name: "{environment}"' in config, f"配置里要声明 {environment} 环境"
        matcher = f'value === "{environment}"'
        assert matcher in config, f"配置里缺少匹配 {matcher} 的 matcher"


def test_quality_job_runs_on_one_platform() -> None:
    """公共检查(ruff/mypy/静态分析/性能基准)只在一个平台上跑一遍, 而且在一个作业里.

    跑多平台只会得到多份一样的结论; 拆成多个作业则要多付一整套 checkout / uv / 依赖同步
    的固定开销。所以三批公共检查在同一个 Ubuntu 作业里依次跑。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  quality:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 quality job"
    job = block.group(1)
    assert "runs-on: ubuntu-latest" in job
    assert "matrix" not in job, "质量作业不应按平台展开"
    assert "--group core" in job, "公共质量门禁在这个作业里跑"
    assert "--group analysis" in job, "静态分析也在这个作业里跑"
    assert "pytest tests/performance" in job, "性能基准也在这个作业里跑"
    assert "allure-results-performance" in job, "性能结果照旧要上传(汇总作业要读)"
    assert "\n  analysis:\n" not in workflow, "不要再把静态分析拆成独立作业"
    assert "\n  performance:\n" not in workflow, "不要再把性能基准拆成独立作业"


def test_the_visual_gate_refuses_a_frame_that_cannot_draw_cjk(tmp_path: Path) -> None:
    """字体画不出汉字的画面**不许当基线**: 结论照写(带探针), 候选基线一个都不产出.

    出处(2026-10-02): CI 的候选基线里整排按钮是空的(汉字一个都没画出来), 而那一轮在报告里
    是**通过** —— 判据只比"这轮与基线像不像", 基线本身是坏的时候它看不出来。所以脚本先跑
    字体探针, 拿它当门禁。
    """
    module = _load_script("create_allure_visual")
    broken = {"cjk_width": 0, "actual": "fixed"}
    assert module.font_problem(broken), "汉字量不出宽度时必须判不可用"
    assert module.font_problem({"cjk_width": 40, "actual": "fixed"}), (
        "落到核心位图字体也一样不可用(没有抗锯齿)"
    )
    assert not module.font_problem({"cjk_width": 40, "actual": "Noto Sans CJK SC"})

    work = tmp_path / "work"
    work.mkdir()
    (work / "01-画面.png").write_bytes(b"\x89PNG\r\n\x1a\nfake bytes, good enough")
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    results = tmp_path / "results"
    results.mkdir()

    comparison = module.process_screen(
        "01-画面.png",
        order=0,
        work_dir=work,
        baselines=tmp_path / "baselines",
        candidates=candidates,
        results_dir=results,
        host="Linux",
        skipped=(),
        probe={
            **broken,
            "cjk_candidates": [],
            "families": 3,
            "platform": "Linux",
            "python": "3.12.0",
            "tk": "8.6.14",
            "tk_module": "/usr/lib/python3.12/lib-dynload/_tkinter.cpython-312-x86_64-linux-gnu.so",
            "tk_libraries": ["/usr/lib/x86_64-linux-gnu/libtk8.6.so.0"],
            "requested": "Noto Sans CJK SC",
            "ascii_width": 20,
        },
        blocked=module.font_problem(broken),
    )

    assert not comparison.passed
    assert list(candidates.iterdir()) == [], "画不出汉字的图不该被当成候选基线"
    payload = json.loads(
        next(results.glob("*-result.json")).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed", "必须标红: 这一轮的画面不可信"
    assert "汉字量出来是 0 宽" in payload["statusDetails"]["message"]
    names = [attachment["name"] for attachment in payload["attachments"]]
    assert "fonts.txt" in names, "探针要作为附件进报告(不然下一次还得猜)"
    assert "actual.png" in names, "坏图也要留着: 人要看它坏成什么样"
    # 修法与现场一起写进证据: "用哪个 Tk"是这道门禁的关键输入, 光有版本号说明不了它是哪一份。
    assert "_tkinter" in payload["statusDetails"]["message"], (
        "判不可用时要把当前用的是哪个 `_tkinter` 一起写出来"
    )


def test_each_visual_result_gets_its_own_start_time(tmp_path: Path) -> None:
    """同一轮里每张画面的开始时间必须**各不相同**: 报告里的列表顺序就是它.

    出处(用户 2026-10-03 实测): 报告里视觉回归那一节的顺序是 `01, 03, 02, 05, 04, 06` —— 两两
    成对交换。现场是结果数据里的 `start`: 02 与 03、04 与 05 **完全相同**, 因为每写一条结论都
    现取一次毫秒时间戳, 而相邻两张撞在同一毫秒里 —— 同值在 Allure 里没有稳定的先后。修法是
    "基准 + 序号"(见 :func:`write_result`), 这条用例把那个不变量钉住: 改回"各取一次时间"就红。
    """
    module = _load_script("create_allure_visual")
    work = tmp_path / "work"
    work.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    for index in range(3):
        (work / f"{index:02d}-画面.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        module.process_screen(
            f"{index:02d}-画面.png",
            order=index,
            work_dir=work,
            baselines=tmp_path / "baselines",
            candidates=candidates,
            results_dir=results,
            host="Linux",
            skipped=(),
            probe=None,
        )

    rows = sorted(
        (
            json.loads(path.read_text(encoding="utf-8"))
            for path in results.glob("*-result.json")
        ),
        key=lambda payload: str(payload["name"]),
    )
    assert [row["name"] for row in rows] == [
        "视觉回归 · 00-画面",
        "视觉回归 · 01-画面",
        "视觉回归 · 02-画面",
    ]
    starts = [int(row["start"]) for row in rows]
    # 白盒口径: 钉住"每张的开始时间 = 基准 + 序号"这个不变量本身 —— 只看"互不相同"不够,
    # 写三张结果之间的文件 IO 本来就可能跨过一毫秒, 那样改回"各取一次时间"照样是绿的
    # (咬合验证时实测到过)。
    base = int(module._RUN_STARTED_AT)  # pyright: ignore[reportPrivateUsage]
    assert starts == [base, base + 1, base + 2], (
        f"每张的开始时间应当等于基准 + 序号, 实测 {starts}(基准 {base})"
    )


def test_the_probe_records_which_tk_is_in_use() -> None:
    """探针要记下 `_tkinter` 模块与 Tcl/Tk 库的**路径**, 而不只是版本号.

    出处(2026-10-02): 报告里写着"Linux / 3.12.3 / 8.6.14", 而 3.12.3 是**发行版解释器**的版本
    —— 那一步当时为了拿发行版的 Tk 把解释器也换掉了, 于是报告里出现一个项目里哪里都不用的
    Python 版本。教训是"解释器版本对了不代表 Tk 也对了": 同一个 Tk 8.6 可能来自发行版(走
    Xft/fontconfig, 看得见 TTF 与汉字), 也可能来自解释器自带的副本(只认 X11 核心位图字体)。
    所以探针把两条路径都写下来, 下一次出问题时不用再猜。
    """
    module = _load_script("create_allure_visual")
    libraries = module.loaded_tk_libraries()
    assert isinstance(libraries, list)
    if not sys.platform.startswith("linux"):
        assert libraries == [], "非 Linux 没有 /proc/self/maps: 给空表, 不要编一份出来"

    module_path = "/usr/lib/python3.12/lib-dynload/_tkinter.cpython-312.so"
    library = "/usr/lib/x86_64-linux-gnu/libtk8.6.so.0"
    probe = {
        "platform": "Linux",
        "python": "3.12.11",
        "tk": "8.6.14",
        "tk_module": module_path,
        "tk_libraries": [library],
        "requested": "Noto Sans CJK SC",
        "actual": "Noto Sans CJK SC",
        "families": 12,
        "cjk_candidates": ["Noto Sans CJK SC"],
        "cjk_width": 40,
        "ascii_width": 20,
    }
    text = module.format_probe(probe)

    assert "Tk 模块" in text, "要写出 `_tkinter` 的来路"
    assert module_path in text
    assert "Tcl/Tk 动态库" in text, "要写出真正加载到的库"
    assert library in text
    assert not module.font_problem(probe), "这一份探针就是「可用」的样子"


def test_visual_regression_runs_in_the_quality_job_and_reaches_the_report() -> None:
    """视觉回归(感知哈希 + SSIM)是公共质量作业的一道门禁, 结论要进 Common 环境并进总账.

    四件事必须同时成立, 缺一件这套机制就退化成"跑了但没人看得见":

    ① 脚本在**质量作业**里跑(与 ruff / 静态分析 / 性能基准同一个作业: 它同样与平台无关,
       单独开作业只是多付一整套 checkout / uv / 依赖同步的固定开销);
    ② 它要**真实显示**: runner 上先装 xvfb, 命令走 ``xvfb-run`` —— Tk 在无头环境里起不来,
       而"起不来"绝不能被记成"画面没问题";
    ③ 它**不按平台展开**: 基线图入库、只在 Linux 采, 三个平台各采一套会把"字体度量不同"
       变成一堆假红(见 PLAN.md 17.4 的落地结论);
    ④ 结论项带 ``env=common`` + ``testCategory=quality``: 前者让它落进报告里显式声明的
       Common 环境(而不是某个平台), 后者让运行总账的质量检查表与"工程门禁:质量检查未通过"
       分类都能看见它 —— 这就是"视觉回归也要体现到报告里"的落点。
    """
    module = _load_script("create_allure_visual")
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "quality")

    assert "scripts/create_allure_visual.py" in job, "脚本要在质量作业里跑"
    assert "xvfb-run -a" in job, "无头 runner 上必须经 xvfb 起显示"
    assert "xvfb" in job, "对应的系统包也要装"
    assert "allure-results-visual" in job, "结论要作为产物上传"
    assert "tests/visual-baselines" in job, "基线图入库, 从仓库里读"
    # 基线**不能**放 ui-review/: 那个目录被它自己的 .gitignore 挡在仓库外, CI 上根本不存在
    # (曾经因为复用 ui-review/capture.py, 这一步在 CI 上以"找不到复用脚本"退出 2)。
    # 只看命令: 注释里正拿这件事当反面说明(与本仓库其它几条守卫同一个口径)。
    code = "\n".join(
        line for line in job.splitlines() if not line.strip().startswith("#")
    )
    assert "ui-review" not in code, (
        "门禁不许依赖 ui-review/(它不进仓库, 且只是开发阶段的临时物)"
    )
    assert "matrix" not in job, "视觉回归不按平台展开: 基线只按一种渲染采"
    assert "font" in job, "要装 CJK 字体: 否则汉字被量成零宽, 画面与基线对不上"
    # 但"装了字体"还不够: 用的那个 Tk 必须看得到 fontconfig/FreeType。uv 托管的 CPython 里
    # 带的 Tcl/Tk 是 python-build-standalone 自己编的, 在 X11 上只走**核心位图字体**(实测它的
    # libtcl9tk9.0.so 只有 XLoadQueryFont 那套符号), 汉字一个都画不出来 —— 2026-10-02 的候选
    # 基线就是这么空的。修法是**换 Tk 而不换解释器**: 把发行版 python3-tk 的 `_tkinter` 模块
    # (它链发行版 libtk8.6/Xft) 放在 PYTHONPATH 最前面。
    assert "python3-tk" in job, "要装发行版的 Tk: uv 托管那份在 X11 上画不出汉字"
    assert "tcl8.6" in job, "发行版 Tk 的库包也要装"
    assert "tk8.6" in job, "发行版 Tk 的脚本包也要装"
    assert "tk-xft" in job, "发行版那份 `_tkinter` 要留一份给这一步用"
    visual_step = job.split("name: Run the visual regression", 1)[1].split(
        "\n      - name:", 1
    )[0]
    # 解释器必须是**项目版**: 之前这一步用 /usr/bin/python3.12 去拿发行版的 Tk, 而 Ubuntu
    # 24.04 的系统 Python 是 3.12.3 —— 报告里"平台 / Python / Tk"那行因此写着一个项目里哪里
    # 都不用的版本。现在只换 `_tkinter` 模块, 解释器仍归 uv 管(与其余作业同版本)。
    assert "--python-preference" not in visual_step, (
        "视觉回归不许把解释器换成发行版的: 报告里的 Python 一栏要是这一轮真正的解释器"
    )
    assert "--python /usr" not in visual_step, "同上: 解释器归 uv 管, 换的只是 Tk"
    assert 'PYTHONPATH="$RUNNER_TEMP/tk-xft' in visual_step, (
        "发行版的 `_tkinter` 要排在 sys.path 最前面, 否则换不上"
    )
    assert "_tkinter.__file__" in visual_step, (
        "换没换上要当场自检: 不然会拿着一张位图字体的图去比基线"
    )
    assert "UV_PROJECT_ENVIRONMENT" in visual_step, (
        "那一份装进独立环境, 别动本作业其余步骤共用的 .venv"
    )

    summary = ci_workflow.job_block(workflow, "allure-summary")
    assert "pattern: allure-results-*" in summary, (
        "汇总作业要把这份结论收进报告(现在是一份 pattern 把各作业的结果一起收全)"
    )

    # 标签决定归属: 改动这两处会让结论落错环境或从总账里消失。
    assert module.ENVIRONMENT == "common"
    assert (module.CATEGORY_LABEL, module.CATEGORY_VALUE) == ("testCategory", "quality")
    # 判据必须**两条都在**(哈希管"整体变了没有", SSIM 管细粒度), 且门槛是有界的数字。
    assert 0 <= module.MAX_HASH_DISTANCE <= 64, "哈希距离上限要在 64 位以内"
    assert 0.0 < module.MIN_SSIM <= 1.0, "SSIM 下限要在 0~1 之间"


def test_report_job_does_not_depend_on_the_host_platform() -> None:
    """报告作业跑在 Ubuntu 上, 平台必须由矩阵给出 —— 不能看 runner.os.

    合并、覆盖率汇总、报告生成与自检都是纯文件操作, 所以两个平台都在 Ubuntu 上跑(省掉
    Windows runner 的 2 倍计价与那些 pwsh 分支)。但"这份结果算哪个平台的"由数据决定:
    再用 ``runner.os`` 当平台名, 两个平台的结论都会被标成 Linux(两台机器都是 Linux)。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "pytest-report")

    assert "runs-on: ubuntu-latest" in job
    # 只看命令, 不看注释(注释里会拿 runner.os 当反面说明)。
    steps = job.split("steps:", 1)[1]
    commands = "\n".join(
        line for line in steps.splitlines() if not line.strip().startswith("#")
    )
    assert "runner.os" not in commands, (
        "报告作业里不能再按 runner.os 分支: 两台都是 Linux, 平台只能来自矩阵"
    )
    platforms = {
        entry["platform"]
        for entry in ci_workflow.matrix_entries(workflow, "pytest-report")
    }
    assert platforms == set(required_platforms()), (
        "报告作业的平台必须与质量门要求的平台一致"
    )


def test_platform_checks_only_run_on_their_own_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """平台专属检查只能跑在自己那一支上, 跑不了的分组要明确报错而不是默默放过.

    ``--platform`` 是"检查哪一支代码", 不是"在哪台机器上跑": 放到别的平台上照样退 0,
    报告里却会出现一条"产自 Ubuntu 的 Windows 结论"。所以脚本按 ``Check.host_platform``
    自己挑: 不匹配就跳过并写出理由; 显式要一个在当前平台全都跑不了的分组时以退出码 2
    报错 —— 悄悄什么都不做等于这道门禁不存在。
    """
    module = _load_script("create_allure_quality")
    original = module.select_checks
    plain = module.Check(
        "probe-plain", "Probe plain", (sys.executable, "-c", "print('ok')")
    )
    win32_probe = module.Check(
        "probe-win32",
        "Probe win32",
        (sys.executable, "-c", "print('ok')"),
        group="platform",
        host_platform="win32",
    )
    monkeypatch.setattr(module, "CHECKS", (plain, win32_probe))

    # 直接看挑选逻辑: 在 win32 上只跑自己那一支, 在 linux 上整组都不适用(all 仍能跑公共检查)。
    assert [item.key for item in original("platform", "win32")[0]] == ["probe-win32"]
    assert original("platform", "linux")[0] == ()
    assert [item.key for item in original("platform", "linux")[1]] == ["probe-win32"]
    assert [item.key for item in original("all", "linux")[0]] == ["probe-plain"]
    assert [item.key for item in original("all", "linux")[1]] == ["probe-win32"]

    monkeypatch.setattr(
        module, "select_checks", lambda group, _host: original(group, "linux")
    )

    # 跑全部: 不适用的那支跳过并说明理由, 其余照常跑。
    results = tmp_path / "allure-results"
    assert module.main(["--results-dir", str(results)]) == 0
    written = [
        json.loads(item.read_text(encoding="utf-8"))["name"]
        for item in sorted(results.glob("*-result.json"))
    ]
    assert written == ["Probe plain"]
    printed = capsys.readouterr().out
    assert "跳过 Probe win32" in printed
    assert "只在 Windows 上执行" in printed
    assert "(当前平台 " in printed, "跳过时要写清当前平台"

    # 显式点名一个在当前平台跑不了的分组: 退出码 2, 且不写任何"通过"的结论。
    wrong_platform = tmp_path / "wrong-platform"
    assert (
        module.main(["--group", "platform", "--results-dir", str(wrong_platform)]) == 2
    )
    assert not list(wrong_platform.glob("*-result.json"))
    assert "平台专属检查必须在对应平台上跑" in capsys.readouterr().err


def test_platform_check_runs_in_a_job_on_its_own_platform() -> None:
    """平台专属检查要真的跑在对应平台上, 且结论要被汇总作业收走.

    ``mypy --platform win32`` / ``darwin`` 只把类型检查指向某支代码, 并不校验执行环境:
    在 Ubuntu 上跑出来的结论挂到 Windows 环境里就是假的归属。所以它放在 pytest 作业的
    **片 0**、且仅限 Windows/macOS(单独开作业只是多付一套固定开销 —— 而且其中一个是
    最贵的 macOS runner)。这里把"跑在哪个平台"与"标着哪个平台"钉在一起。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    block = re.search(r"\n  pytest:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 pytest job"
    job = block.group(1)
    step = job.split("name: Run the platform-specific check", 1)[1]
    step_condition = step.split("run:", 1)[0]
    assert "matrix.shard == 0" in step_condition, "只需一片跑: 多跑只是重复"
    assert "runner.os != 'Linux'" in step_condition, "平台专属检查不该跑到 Ubuntu 上"
    assert "always()" in step_condition, "测试失败也要给出这条结论"
    assert "--group platform" in step
    assert "allure-results-quality-platform-${{ matrix.os }}" in job
    # 公共质量作业不再代跑平台专属检查(否则同一批检查会跑两遍, 归属还重复)。
    quality = re.search(r"\n  quality:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert quality is not None
    assert "--group platform" not in quality.group(1)
    assert "\n  quality-platform:\n" not in workflow, "也不该再有独立的平台检查作业"

    # 汇总作业必须能等到它, 并把产物收进报告。
    summary = re.search(r"\n  allure-summary:\n(.*)", workflow, re.DOTALL)
    assert summary is not None, "ci.yml 里找不到 allure-summary job"
    assert "pattern: allure-results-*" in summary.group(1), (
        "汇总要用一份 pattern 收全各作业的结果(平台专属检查那份也在里面)"
    )


def test_ci_cancels_superseded_runs_and_skips_docs_only_pushes() -> None:
    """连续 push 要取消被取代的运行; 纯文档 push 不必跑整套 CI.

    旧的一轮没人看, 却跟新的一轮一样贵(约 21 个作业)。路径过滤**只加在 push 上**:
    PR 被过滤掉会让分支保护里的必需检查永远停在 pending, 反而合不了。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    assert re.search(r"^concurrency:\n", workflow, re.MULTILINE) is not None
    assert "cancel-in-progress: true" in workflow
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in workflow, (
        "按 ref 分组: PR 与 dev/main 各管各的"
    )
    push_block = workflow.split("\n  push:\n", 1)[1].split("\n  pull_request:", 1)[0]
    assert "paths-ignore:" in push_block
    assert '"**/*.md"' in push_block
    assert '"docs/**"' in push_block
    pull_block = workflow.split("\n  pull_request:", 1)[1].split("\njobs:", 1)[0]
    assert "paths-ignore" not in pull_block, "PR 不能用路径过滤: 必需检查会停在 pending"


def test_report_jobs_do_not_start_for_a_superseded_run() -> None:
    """被取代的运行别再花时间做报告: 两个报告作业的条件都要排除“整轮被取消”.

    ``always()`` 的语义是“即使被取消也返回 true”, 所以只写 ``always()`` 会让已经被
    取代的那一轮照样启动合并/汇总作业 —— 那正是“取消了却还在跑报告”的来源。这里同时
    钉住两件事: ``always()`` 必须在(依赖作业失败时仍要出报告, 这是原有意图), 只是得
    配上 ``!cancelled()``。
    """
    text = ci_workflow.workflow_text()

    for job in ("pytest-report", "allure-summary"):
        condition = ci_workflow.job_condition(text, job)
        assert "always()" in condition, f"{job} 少了 always(): 依赖作业失败时也要出报告"
        assert "!cancelled()" in condition, f"{job} 在整轮被取消后仍会启动"


def test_the_report_is_published_to_pages_from_the_default_branch_only() -> None:
    """汇总报告发布到 GitHub Pages: 与汇总**合成一个作业**, 只从默认分支的 push 发布, 发解开的目录。

    发布本身很便宜, 但几个前置条件漏了就会出问题:
    ① 只在 push 上发布 —— PR 上发布等于把未评审的内容放上站点, 而来自 fork 的 PR 也
       拿不到 `pages: write`(那会变成一条恒红的检查);
    ② 只在默认分支上发布 —— `dev` 的推送会把站点来回覆盖;
    ③ Pages 要的是**目录**: 直接传 zip 的话站点根就变成一个压缩包, 打开网址只会下载文件 ——
       所以必须真的出现解压, 且上传路径指向解出来的报告目录;
    ④ 没通过报告自检就不发布(`steps.verify-report.outcome == 'success'`)。
    另外发布没有额外凭据: 顶层只给了 `contents: read` / `actions: read`, 所以这个作业要自己
    声明 `pages: write` + `id-token: write`(OIDC, 不必发长期凭据), 并保留 `contents: read`
    —— 作业级 permissions 是**覆盖**而不是叠加, 漏了它 checkout 会直接失败。

    **这条同时守卫“合并”这件事**(2026-09-30): 汇总与发布必须留在同一个作业里, 所以下面除了
    正向断言, 还反向断言"没有第二个发布作业" —— 重新拆开会让这条红, 而不是静默多付一整套固定开销。
    """
    workflow = ci_workflow.workflow_text()
    assert "\n  deploy-pages:\n" not in workflow, (
        "发布不再是独立作业: 它与汇总合成一个(否则要多付一整套 checkout / uv / npm)"
    )
    job = ci_workflow.job_block(workflow, "allure-summary")

    assert ci_workflow.needs_of(workflow, "allure-summary") == {
        "quality",
        "pytest-report",
        "security",
    }, "汇总要等齐三批产物(报告还没传完就开始合并会静默少一部分)"
    assert "Generate final Allure report" in job, "汇总本身还在这里"
    assert "Deploy to GitHub Pages" in job, "发布也在这里(两者一个作业)"

    # 触发条件写在发布的第一步上(后面两步跟随它的 ready 输出)。
    step = job.split("name: Unpack the report for Pages", 1)[1].split("run:", 1)[0]
    assert "always()" in step, step
    assert "!cancelled()" in step, "整轮被取消后不该再启动发布"
    assert "github.event_name == 'push'" in step, "PR 上不该发布站点"
    assert "default_branch" in step, "只从默认分支发布, dev 的推送不该覆盖站点"
    assert "steps.verify-report.outcome == 'success'" in step, "没通过自检的报告不发布"

    publish = job.split("name: Unpack the report for Pages", 1)[1]
    assert "pages: write" in job, "发布 Pages 需要它(顶层只给了只读的 contents/actions)"
    assert "id-token: write" in job, "deploy-pages 用 OIDC 换一次性的部署权限"
    assert "contents: read" in job, "作业级 permissions 是覆盖: 漏了它 checkout 会失败"
    assert "environment:" in job, "站点要挂在 github-pages 环境上(作业 URL 也来自它)"
    assert "github-pages" in job, "environment 的名字必须是 github-pages"
    assert "allure-report.tar.gz" in publish, "发布的是自检通过后打好的那个归档"
    assert "tar -xzf" in publish, "Pages 要目录: 必须先把归档解开"
    assert "path: site/allure-report" in publish, "上传的必须是解出来的报告目录"


def test_report_artifacts_do_not_duplicate_the_whole_result_set() -> None:
    """同一个平台的结果集只能上传**一次** —— 合并后的副本再传一份是白花的额度。

    现场(2026-10-04, 用户要求减少上传): `allure-resources-<平台>` 里除了历史与产物清单,
    还带着一整份合并后的 `allure-results/`(每个平台一万多个文件), 而它**没有任何消费者**:
    两个报告作业取回历史时都只 `cp .../.allure/history.jsonl`; 汇总作业收的是同一批文件
    (`scripts/merge_allure_results.py` 按文件名搬到一处, 不改内容), 直接下分片产物即可。
    `allure-resources-final` 里那份更贵 —— 它是所有平台的合集, 是本工作流里最大的单笔上传。
    """
    workflow = ci_workflow.workflow_text()

    for step_name, artifact in (
        ("Upload Allure resources", "allure-resources-${{ matrix.os }}"),
        ("Upload final Allure resources", "allure-resources-final"),
    ):
        # 按**步骤名**取(而不是产物名): 产物名在"下载上一次历史"那一步里也会出现, 而它排在
        # 上传之前 —— 按产物名切会切到那一步上, 断言就变成在检查下载了。
        upload = workflow.split(f"name: {step_name}", 1)[1].split("- name:", 1)[0]
        # 只看真正生效的行: 这一步的注释里正拿 `allure-results/` 当反面说明(与本仓库其它几条
        # 守卫同一个口径 —— 注释是文档, 不是配置)。
        body = "\n".join(
            line for line in upload.splitlines() if not line.strip().startswith("#")
        )
        assert f"name: {artifact}" in body, f"{step_name} 上传的是 {artifact}"
        assert "allure-results/" not in body, (
            f"{artifact} 又带上了整份结果集: 分片产物已经传过一遍了"
        )
        assert ".allure/history.jsonl" in body, "历史文件必须在(趋势靠它)"
        assert "include-hidden-files: true" in body, "历史文件是隐藏文件, 要显式放行"

    # 汇总作业要**一次**把各作业的结果收全: 以前是五次按名字下载 + 从合并副本里摊平。
    summary = ci_workflow.job_block(workflow, "allure-summary")
    assert "pattern: allure-results-*" in summary, (
        "汇总要能一份 pattern 收全(分片 + 质量/分析/性能/平台检查/视觉/安全)"
    )
    assert "merge-multiple: false" in summary, (
        "各片要落在自己目录里: 摊平那一步按目录收, 缺了哪一片看得出"
    )
    for gone in (
        "name: Download performance Allure results",
        "name: Download security Allure results",
        "name: Download quality Allure results",
        "name: Download analysis Allure results",
        "name: Download visual Allure results",
        "name: Download platform check Allure results",
    ):
        assert gone not in summary, f"{gone} 已被一份 pattern 取代, 留着就是重复下载"


def test_htmlcov_is_not_uploaded_by_every_platform() -> None:
    """HTML 覆盖率报告不再三份都传: 同一份数据已经在报告里以 `coverage.xml` 附件挂着。

    两道口径要一起守住: 不再上传它之后, 本地的产物也不能留着不管 —— 生成步骤只写 XML,
    诊断清单里也不该还挂着它(否则每次失败都报一句"那个目录不存在", 而那是正常的)。
    只看**命令与上传路径**(注释里正拿这件事当反面说明, 与本仓库其它几条守卫同一个口径)。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )

    assert "htmlcov" not in code, "已经不上传了, 命令与清单里都不该再出现"
    assert "coverage html" not in code, (
        "别再生成本地产物了: 传不上去, 只会让人以为有人看"
    )
    assert "coverage xml -o coverage.xml --fail-under=0" in code


def test_history_trends_survive_the_pages_deploy() -> None:
    """历史趋势靠 artifact 往返 —— 发布到 Pages 既不能提供它, 也不该把它弄丢。

    机制(这条守卫要钉住的不变式): 两个报告作业各自"找上一次**成功**运行的同名产物 → 取回
    `.allure/history.jsonl` → 生成报告(读它并追加本次一行) → 把新的 history.jsonl 重新
    传进产物"。于是趋势的寿命 = artifact 的寿命(与站点无关), 而链条的每一环都要求那一轮的
    `conclusion` 是 success —— 这也解释了为什么"发布 Pages"这个非门禁动作必须
    `continue-on-error`: 它失败会让整轮离开成功集合, 于是这一轮刚写好的历史行再也不会
    被下一轮读走(表现是趋势曲线缺一走)。

    合并成一个作业之后这条要多守一件事: `continue-on-error` **只能落在发布那几步上**,
    不能挂到作业上 —— 挂上去会把质量门的结论一起吞掉(那样门禁失败也只是条绿记录)。
    """
    workflow = ci_workflow.workflow_text()

    for job in ("pytest-report", "allure-summary"):
        block = ci_workflow.job_block(workflow, job)
        assert "Resolve previous successful run" in block, f"{job} 要先找上一轮成功运行"
        assert 'conclusion === "success"' in block, f"{job} 只把成功运行当基线"
        hint = f"{job} 要取回上一次的历史文件"
        assert "cp .previous-allure-resources/.allure/history.jsonl" in block, hint
        assert "include-hidden-files: true" in block, (
            f"{job} 要把新的历史文件传回产物(它是隐藏文件, 必须显式放行)"
        )

    summary = ci_workflow.job_block(workflow, "allure-summary")
    deploy_keys = summary.split("steps:", 1)[0]
    assert "continue-on-error" not in deploy_keys, (
        "合并后它不能挂在作业上: 那会把质量门的结论一起吞掉(只该落在发布那几步上)"
    )
    # 发布三步的窗口终点是"失败诊断"那一步: 它排在门禁结论**之前**(失败时才跑, 只收现场),
    # 并不属于"发布"。
    publish = summary.split("name: Unpack the report for Pages", 1)[1].split(
        "name: Collect failure diagnostics", 1
    )[0]
    for block in publish.split("\n      - name: "):
        assert "continue-on-error: true" in block, (
            "发布这几步都要带 continue-on-error: 发布失败不该让整轮离开'成功'集合"
            f"(历史基线只认成功的运行): {block[:200]}"
        )
    assert "history.jsonl" not in publish, "站点只是副本: 发布那几步不该碰历史文件"


def test_ci_keeps_the_evidence_of_a_hard_crash() -> None:
    """进程级崩溃(SIGSEGV/SIGABRT) 留不进 Allure —— 必须靠 artifact 把 ``crash-dumps`` 带出来.

    2026-10-03 的 macOS 分片实测: 段错误发生在界面的 ``update_idletasks`` 里, 进程被内核
    杀掉, coredumpy(挂在"用例失败"这个 Python 钩子上)根本没机会跑, pytest-cov 也来不及
    落盘覆盖率 —— 事后只剩"那一次运行的日志"这一个地方可查。所以这个目录要当产物上传,
    并在同一个作业里回显进运行日志(调试时最顺手的地方还是日志)。
    """
    job = ci_workflow.job_block(ci_workflow.workflow_text(), "pytest")

    upload = job.split("name: Upload crash dumps", 1)[1].split("\n      - name:", 1)[0]
    assert "if: always()" in upload, "用例失败/进程崩溃后仍然要上传"
    assert "path: crash-dumps/" in upload
    assert "if-no-files-found: ignore" in upload, (
        "大多数分片本来就没有现场: 不该因此把作业变红(与覆盖率产物'缺了就报错'的取舍不同)"
    )

    echo = job.split("name: Echo crash dumps into the run log", 1)[1]
    assert "cat " in echo, "崩溃日志要回显进运行日志(不该逼着人先下载 artifact)"


def test_tkinter_check_still_fails_the_job_when_tcl_is_broken() -> None:
    """Tkinter 自检要保留"修不好就红"的语义(只是从"每次重装"改成"失败才重装").

    先普通安装解释器、自检失败时才 `--reinstall` 是省额度用的, 但**不能**把这道守卫削弱:
    那一步失败的 `continue-on-error` 必须由紧随其后的修复步骤接手(它再复检一次, 且没有
    continue-on-error), 否则 Tcl 坏掉只会表现为一堆 GUI 用例 skip, 覆盖率门槛变成难定位的失败。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  pytest:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 pytest job"
    job = block.group(1)

    setup = job.split("name: Set up Python", 1)[1].split("- name:", 1)[0]
    # 只看命令本身(注释里会提到 --reinstall 作为反面说明)。
    setup_command = setup.split("run:", 1)[1].strip()
    assert setup_command.startswith("uv python install 3.12")
    assert "--reinstall" not in setup_command, "不应无条件重装解释器(那等于每次都重下)"

    check = job.split("name: Verify Tkinter works (Windows/macOS)", 1)[1]
    check_head = check.split("run:", 1)[0]
    assert "id: tkinter" in check_head, "修复步骤要靠这个 id 判断自检结果"
    assert "continue-on-error: true" in check_head

    repair = job.split("name: Repair the interpreter and re-check Tkinter", 1)[1]
    repair_head = repair.split("run:", 1)[0]
    repair_body = repair.split("run:", 1)[1].split("- name:", 1)[0]
    assert "steps.tkinter.outcome == 'failure'" in repair_head, "只在自检失败时重装"
    # 只看步骤自己的键(注释里会写"这一步没有 continue-on-error"来解释语义)。
    repair_keys = [
        line.strip()
        for line in repair_head.splitlines()
        if not line.strip().startswith("#")
    ]
    assert "continue-on-error: true" not in repair_keys, "修复步骤自己要能拦住提交"
    assert "uv python install --reinstall 3.12" in repair_body
    assert "uv sync --locked" in repair_body, "重装解释器后让虚拟环境跟上"
    assert repair_body.count("import tkinter") == 1, "修完必须复检一次"


def test_quality_output_reaches_the_report_without_control_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告里的原始输出不能带终端控制码(实测 deptry 会带 ANSI 颜色 → 显示成乱码).

    探针检查会真的打印 ANSI 颜色与 ``\\r`` 进度行, 走的是与真实检查完全相同的
    写入路径: 附件与描述里都不该剩下控制码, 进度行只保留最后一段。
    """
    module = _load_script("create_allure_quality")
    script = "print('\\x1b[1m\\x1b[32mok\\x1b[m'); print('half\\rfinal')"
    probe = module.Check("probe", "Probe check", (sys.executable, "-c", script), "core")
    monkeypatch.setattr(module, "CHECKS", (probe,))
    results = tmp_path / "allure-results"

    assert module.main(["--group", "core", "--results-dir", str(results)]) == 0

    payload = json.loads(
        next(results.glob("*-result.json")).read_text(encoding="utf-8")
    )
    attached = (results / payload["attachments"][0]["source"]).read_text(
        encoding="utf-8"
    )
    assert "\x1b" not in attached, f"附件里还有终端控制码: {attached!r}"
    assert "\x1b" not in payload["description"], "描述里也不该有控制码"
    assert "ok" in attached
    assert "final" in attached
    # subprocess 读文本时走 universal newlines, ``\r`` 已被翻成 ``\n``; 助手自身仍要能
    # 收敛残留的 ``\r``(工具直接写字节 / 用了别的新行模式时会出现), 以及剥掉颜色码。
    assert module.sanitize_output("half\rfinal") == "final"
    assert module.sanitize_output("\x1b[32mok\x1b[m") == "ok"


def test_run_ledger_artifact_list_covers_summary_items_only(tmp_path: Path) -> None:
    """产物清单只列脚本生成的汇总结论项: 用例自带的附件上千个, 列进来就是噪声."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    (results / "cov.xml").write_text(
        '<coverage line-rate="1" branch-rate="1"/>', encoding="utf-8"
    )
    (results / "cov-result.json").write_text(
        json.dumps(
            {
                "uuid": "cov",
                "name": "Coverage report",
                "fullName": "archive-management.coverage",
                "status": "passed",
                "labels": [{"name": "env", "value": "Linux"}],
                "attachments": [{"name": "coverage.xml", "source": "cov.xml"}],
            }
        ),
        encoding="utf-8",
    )
    (results / "t1-result.json").write_text(
        json.dumps(
            {
                "uuid": "t1",
                "name": "某个用例",
                "fullName": "tests.unit.test_demo#test_something",
                "status": "passed",
                "attachments": [{"name": "screenshot.png", "source": "shot.png"}],
            }
        ),
        encoding="utf-8",
    )

    rows = module.artifact_rows(results, module.result_payloads(results))

    assert [row[1] for row in rows] == ["coverage.xml"], "只该列汇总结论项的原始产物"
    assert rows[0][0].startswith("Coverage report")


def test_run_ledger_includes_the_native_quality_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """总账要收录原生质量门: 有日志时报出退出码与原始输出, 没日志时明说未执行.

    CI 会在生成报告前跑一次门禁并把输出(末尾带一行 ``退出码: N``)留在仓库根; 总账
    据此给出结论 —— 不去猜 CLI 的输出文本。本地直接跑汇总脚本时没有这份日志, 那一节
    要写"未执行", 而不是让读者以为门禁通过了。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    (results / "t1-result.json").write_text(
        json.dumps(
            {
                "uuid": "t1",
                "name": "某个用例",
                "fullName": "demo#case",
                "status": "passed",
                "labels": [{"name": "env", "value": "Linux"}],
            }
        ),
        encoding="utf-8",
    )
    payloads = module.result_payloads(results)

    # 没有日志: 写明未执行, 不能看起来像通过。
    empty = module.run_ledger(results, payloads, None, [])
    assert "## 原生质量门(Allure CLI)" in empty
    assert "未执行" in empty

    # 有日志: 退出码 1 → 未通过; 颜色码要剥掉, 退出码那行翻成结论后不再照抄。
    monkeypatch.chdir(tmp_path)
    (tmp_path / "allure-quality-gate.txt").write_text(
        "\x1b[31mQuality Gate failed with following issues:\x1b[m\n"
        "maxFailures: 1 exceeds 0\n"
        "退出码: 1\n",
        encoding="utf-8",
    )
    ledger = module.run_ledger(results, payloads, None, [])

    assert "**未通过(退出码 1)**" in ledger
    assert "Quality Gate failed" in ledger
    assert "\x1b" not in ledger, "CLI 输出的颜色码不能进报告"
    assert "退出码: 1" not in ledger, "退出码翻成结论后不该再原文照抄"


def test_the_gate_category_matches_what_the_scripts_write() -> None:
    """ "工程门禁"分类、写入脚本与运行总账必须用同一对类别标签。

    三处分别是: ``allurerc.mjs`` 的 ``categories`` 规则(挑选质量检查结果)、
    ``scripts/create_allure_quality.py`` 写结果时打的标签、``scripts/create_allure_summary.py``
    收集这些结果时的判据。任一处改名而另外两处没跟着改都不会报错 —— 只会静默地"门禁失败
    不再进那个分类"或"运行总账里不再列质量检查", 所以要有守卫把三处钉在一起。
    """
    writer = _load_script("create_allure_quality")
    summary = _load_script("create_allure_summary")
    gate = _ALLURE_CONFIG.read_text(encoding="utf-8").split("gate-quality-check", 1)[1]
    matched = re.search(r'labels:\s*\{\s*([A-Za-z]+):\s*"([^"]+)"\s*\}', gate)
    assert matched is not None, "配置里要有按类别标签挑选的分类规则"

    label, value = matched.group(1), matched.group(2)
    assert label == writer.CATEGORY_LABEL == summary.QUALITY_CATEGORY_LABEL
    assert value == writer.CATEGORY_VALUE == summary.QUALITY_CATEGORY


# -- 结论清单(应有 vs 实有): 清单要与产出方代码、CI 作业以及运行总账同步 ------------------


def _conclusion_table(ledger: str) -> str:
    """从总账里切出末节「证据核对」的 ① 结论清单表.

    「证据核对」是**末节**(2026-10-02 调整读序: 前面是结论本身, 证据齐不齐是审计附录),
    它与产物清单合并成一节、下面挂着 ①② 两张表, 所以切片要按小节标题而不是按 ``##``。
    """
    assert "### ① 结论清单" in ledger, "总账要有「证据核对」末节的 ① 结论清单"
    return ledger.split("### ① 结论清单", 1)[1].split("### ②", 1)[0]


def test_the_evidence_section_sits_at_the_end_with_a_verdict_up_front(
    tmp_path: Path,
) -> None:
    """结论清单移到文末、与产物清单合成一节, 但"缺不漏"的一行数必须留在开头.

    总账是报告首页的全局附件: 读者先看的是结论(质量门/覆盖率/性能/安全), "这次该有的证据
    齐不齐"是审计性的附录, 放前面会挡路。代价是**移走之后不能看不见** —— 开头那行
    "应有 N 项 / 实有 M 项 / 缺 K 项(见文末「证据核对」)"就是为此保留的, 所以两头都要守。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_platform_result(
        results,
        "cov-win",
        ("env", "Windows"),
        ("os", "Windows"),
        full_name="archive-management.coverage",
    )
    ledger = module.run_ledger(results, module.result_payloads(results), None, [])

    body = ledger.splitlines()
    verdict = [line for line in body if line.startswith("- 证据核对:")]
    assert len(verdict) == 1, "开头要有一行数(移到末尾才不至于看不见)"
    assert "应有 " in verdict[0], f"那行数要给出应有/实有/缺: {verdict[0]}"
    assert "实有 " in verdict[0], f"那行数要给出应有/实有/缺: {verdict[0]}"
    assert "缺 " in verdict[0], f"那行数要给出应有/实有/缺: {verdict[0]}"
    assert "见文末「证据核对」" in verdict[0], "缺了要指路到文末那一节"
    # 末节必须真的在末尾: ① 表在 ② 表之后不再有任何二级标题。
    tail = ledger.split("## 证据核对(应有 vs 实有)", 1)[1]
    assert "## 结论清单" not in ledger, "旧标题不能残留(一节两处会让读者以为有两份)"
    assert "\n## " not in tail, "「证据核对」后面不该还有二级节"
    # 一行数里的数目要与 ① 表里的行数对得上(那行是给不点开的人也看得见的)。
    rows = [
        line
        for line in _conclusion_table(ledger).splitlines()
        if line.startswith("| ") and not line.startswith("| ---")
    ][1:]
    assert rows, "① 表要有数据行"
    received = sum(line.rstrip().endswith("已收到 |") for line in rows)
    assert f"应有 {len(rows)} 项" in verdict[0], "应有项数要等于 ① 表的行数"
    assert f"实有 {received} 项" in verdict[0], (
        f"开头那行数要与 ① 表的结论一致: {verdict[0]}"
    )


def _upstream_jobs(workflow: str, job: str) -> set[str]:
    """某个作业的上游(``needs`` 的传递闭包): 只有这些作业的产物它才拿得到.

    头一个作业(如 ``pytest``)没有 ``needs`` —— 上游为空, 不是错。
    """

    def declared(name: str) -> set[str]:
        block = ci_workflow.job_block(workflow, name)
        if not re.search(r"^\s*needs:", block, re.MULTILINE):
            return set()
        return ci_workflow.needs_of(workflow, name)

    found: set[str] = set()
    pending = list(declared(job))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(declared(current) - found)
    return found


def test_the_catalog_is_in_sync_with_the_scripts_that_write_conclusions() -> None:
    """结论清单必须与产出方代码**双向**同步 —— 这条就是"自动同步"的守卫.

    为什么不能靠人维护: 清单决定总账里"应有"的一栏, 少了登记就等于那一类结论缺了也不会被
    点名(2026-10-02: 视觉回归的产物与 ``test`` 组都没上传, 报告里安静地少了两节)。
    两个方向都要拦: 代码里写了新身份而清单没登记 -> 缺了看不出; 清单登记了没人写的身份 ->
    总账会一直报一项永远不会出现的缺失。

    清单本身也是"从代码里解析出来的": 质量检查项来自 ``create_allure_quality.py`` 的
    ``CHECKS``(见下一条守卫), 所以在这个脚本里加一项检查不需要回来改清单。
    """
    catalog = _load_script("allure_catalog")
    uncatalogued, unwritten = catalog.identity_gaps(_REPO_ROOT / "scripts")

    assert not uncatalogued, (
        f"这些身份会被写进 Allure 结果, 但清单里没登记(缺了也不会被总账点名): "
        f"{sorted(uncatalogued)} —— 加进 scripts/allure_catalog.py 的 CATALOG"
    )
    assert not unwritten, (
        f"清单登记了这些身份, 但没有任何脚本会写它们(总账会永远报缺失): "
        f"{sorted(unwritten)}"
    )


def test_every_expected_conclusion_names_a_live_script_and_an_upstream_job() -> None:
    """清单里每条结论的"产出脚本 / 上传作业"必须真的存在, 而且那个作业在汇总作业的上游.

    否则清单会慢慢变成一份"看上去很详细"的文档: 作业改了名、脚本搬了家、或者汇总作业
    不再 ``needs`` 那个作业(产物根本下不下来), 都不会有人发现 —— 而总账只会照着清单
    说"应该有", 报出来的缺失无法定位。
    """
    catalog = _load_script("allure_catalog")
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    workflow_jobs = ci_workflow.jobs(workflow)
    upstream = _upstream_jobs(workflow, "allure-summary")

    for item in catalog.CATALOG:
        assert (_REPO_ROOT / item.script).is_file(), (
            f"{item.key} 声明的产出脚本不存在: {item.script}"
        )
        assert item.job in workflow_jobs, (
            f"{item.key} 声明的上传作业不在工作流里: {item.job}"
        )
        assert "actions/upload-artifact" in workflow_jobs[item.job], (
            f"{item.job} 已经不上传任何产物, {item.key} 的缺失会被误报"
        )
        assert item.job in upstream, (
            f"{item.key} 的产物由 {item.job} 上传, 但汇总作业拿不到它"
            f"(不在 needs 链上: {sorted(upstream)})"
        )


def test_expected_items_expand_every_family_and_every_quality_check() -> None:
    """「应有」清单的展开: 每族结论都要出现, 质量检查逐项展开且跟着 ``CHECKS`` 走.

    逐项展开是关键 —— 只写"质量检查这一族"的话, ``bandit`` 那一项没产出时会被同一族的
    别的检查顶上(家族前缀匹配的经典错法), 缺失永远看不出来。
    """
    catalog = _load_script("allure_catalog")
    scripts = _REPO_ROOT / "scripts"
    platforms = ["Windows", "macOS", "Linux"]
    items = catalog.expected_items(platforms, scripts)

    families = {item.producer.key for item in items}
    assert families == {item.key for item in catalog.CATALOG}, (
        "每族结论都要在「应有」里"
    )

    checks = catalog.quality_checks(scripts)
    assert checks, "要从 create_allure_quality.py 里解析出检查项"
    common = {f"archive-management.quality.{check.key}" for check in checks}
    assert {item.identity for item in items} >= common, "每一项检查都要逐项展开"

    platform_checks = [check for check in checks if check.host_platform]
    environments = catalog.platform_environments(scripts)
    assert (
        environments == _load_script("create_allure_quality").PLATFORM_ENVIRONMENTS
    ), "平台映射要从 create_allure_quality.py 解析出来, 且与它自己的常量一致"
    expected_platform_rows = {
        (item.identity, item.environment)
        for item in items
        if item.producer.key == "quality-platform"
    }
    assert expected_platform_rows == {
        (
            f"archive-management.quality.{check.key}",
            environments[check.host_platform or ""],
        )
        for check in platform_checks
    }, "平台专属检查要落在它自己的平台上"

    # 每平台一项的结论按声明的平台各展开一份; 声明少一个平台, 那个平台就整族消失。
    per_platform = {family.key: 0 for family in catalog.CATALOG}
    for item in items:
        if item.environment:
            per_platform[item.producer.key] += 1
    assert per_platform["tests"] == len(platforms)
    assert per_platform["coverage"] == len(platforms)
    assert per_platform["security"] == len(platforms), (
        "security 每轮三个平台都跑(不做降频), 所以三个平台各要一行"
    )


def _write_platform_result(
    results: Path, slug: str, *labels: tuple[str, str], full_name: str = ""
) -> None:
    """写一条带标签的结果(占位用例与汇总结论项共用: 身份与所属环境都靠标签认)."""
    payload: dict[str, Any] = {
        "uuid": slug,
        "name": slug,
        "status": "passed",
        "labels": [{"name": name, "value": value} for name, value in labels],
    }
    if full_name:
        payload["fullName"] = full_name
    (results / f"{slug}-result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def test_missing_conclusions_are_loud_in_the_ledger_and_as_broken_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """产物没交上来时必须**点名**: 总账里标缺失, 报告里另写一条 broken 结论项.

    以前总账每一节都是"有就渲染、没有就写一句没有", 于是"少了哪一节"完全看不出来 ——
    报告永远是完整的, 只是内容少一块。这里锁定三件事: 应有/实行的逐项对照、缺失项在表里
    标 **缺失**、以及每个缺失项各写一条 broken 结论项(报告里一眼可见, 原生质量门跟着红)。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    # 现象: 只有 Windows 交了产物, macOS/Linux 的覆盖率没了; 视觉回归整族没上传;
    # 质量检查只交了一项(其余检查项各自算缺失, 不会被同一族顶掉)。
    _write_platform_result(
        results,
        "cov-win",
        ("env", "Windows"),
        ("os", "Windows"),
        full_name="archive-management.coverage",
    )
    _write_platform_result(
        results,
        "case-win",
        ("env", "Windows"),
        ("os", "Windows"),
        ("framework", "pytest"),
    )
    _write_platform_result(
        results,
        "ruff",
        ("env", "common"),
        ("os", "Linux"),
        ("testCategory", "quality"),
        full_name="archive-management.quality.ruff-check",
    )
    # 性能/安全数据齐全: 它们的结论项由本脚本自己写, 不能被报成缺失(自报自缺的经典错法)。
    (tmp_path / "performance-results.json").write_text(
        json.dumps(
            {
                "platform": "Linux",
                "environment": {"os_family": "Linux"},
                "measurements": [],
            }
        ),
        encoding="utf-8",
    )
    findings = tmp_path / "security-findings" / "Linux"
    findings.mkdir(parents=True)
    (findings / "security-results.json").write_text(
        json.dumps({"environment": {"os_family": "Linux"}, "findings": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(
        module, "PERFORMANCE_JSON", tmp_path / "performance-results.json"
    )
    monkeypatch.setattr(module, "PERFORMANCE_CSV", tmp_path / "performance-results.csv")
    monkeypatch.setattr(module, "SECURITY_JSON", tmp_path / "security-results.json")
    monkeypatch.setattr(
        module, "SECURITY_FINDINGS_DIRECTORY", tmp_path / "security-findings"
    )
    monkeypatch.setattr(
        module, "QUALITY_GATE_REPORT", tmp_path / "allure-run-ledger.md"
    )
    monkeypatch.setattr(
        module, "COVERAGE_EXCLUSIONS_REPORT", tmp_path / "allure-coverage-exclusions.md"
    )
    monkeypatch.setattr(module, "NATIVE_GATE_LOG", tmp_path / "allure-quality-gate.txt")

    assert module.main(["--expect-platforms", "Windows,macOS,Linux"]) == 0, (
        "缺失不改汇总脚本的退出码: 判定交给原生质量门(我们写下的 broken 结论项会让它红)"
    )

    ledger = (tmp_path / "allure-run-ledger.md").read_text(encoding="utf-8")
    table = _conclusion_table(ledger)
    assert "| Coverage report(macOS) |" in table
    assert "**缺失**" in table
    row = next(line for line in table.splitlines() if "Coverage report(macOS)" in line)
    assert row.rstrip().endswith("**缺失** |"), f"缺的那一行要点名: {row}"
    received = next(
        line for line in table.splitlines() if "Coverage report(Windows)" in line
    )
    assert received.rstrip().endswith("已收到 |"), f"交上来的那行不能误报: {received}"
    # 视觉回归整族没有产物: 一行一项(它是公共项, 不分平台)。
    assert "| 视觉回归 |" in table
    assert "**缺失**" in next(
        line for line in table.splitlines() if line.startswith("| 视觉回归 |")
    )
    # 质量检查逐项判定: 交了的算收到, 没交的各自点名(不被同一族别的检查顶掉)。
    assert "| Ruff check |" in table
    for title in ("Bandit 安全扫描", "Xenon 复杂度门槛"):
        assert "**缺失**" in next(
            line for line in table.splitlines() if line.startswith(f"| {title}")
        ), f"{title} 没交产物却没被点名"
    # 本脚本自己写的那两类不能报缺失。
    for title in ("Performance baseline", "Security findings(Linux)"):
        assert "已收到" in next(
            line for line in table.splitlines() if line.startswith(f"| {title}")
        ), f"{title} 由本脚本自己写, 不该被报成缺失"

    written = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(results.glob("*-result.json"))
    ]
    missing_items = {
        payload["name"]: payload
        for payload in written
        if str(payload.get("fullName", "")).startswith(module.MISSING_IDENTITY)
    }
    assert "缺少结论: Coverage report(macOS)" in missing_items
    assert "缺少结论: 视觉回归" in missing_items
    assert "缺少结论: Performance baseline" not in missing_items
    broken = missing_items["缺少结论: Coverage report(macOS)"]
    assert broken["status"] == "broken", "缺失项要是 broken, 否则质量门不会跟着红"
    assert broken["stage"] == "finished"
    assert "coverage-data" in broken["statusDetails"]["message"], (
        "缺什么要写在失败原因里"
    )
    assert broken["labels"][2]["value"] == "macOS", "缺失项要落在缺的那个平台上"
    # 总账与日志都要看得见(报告是产物, 不点开看不到)。
    printed = capsys.readouterr().out
    assert "::warning::缺少结论: Coverage report(macOS)" in printed
    assert "::warning::缺少结论: Performance baseline" not in printed
    assert "结论项应有" in printed, "控制台也要给出应有/缺失的数目"

    # 再跑一次(现场已经多了那几条 broken 项): 结论不能变 —— 缺失项自己不能被当成"收到了"。
    assert module.main(["--expect-platforms", "Windows,macOS,Linux"]) == 0
    again = (tmp_path / "allure-run-ledger.md").read_text(encoding="utf-8")
    assert _conclusion_table(again) == table, "重复跑汇总脚本时结论清单必须稳定"
