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
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

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


def test_zip_packages_every_file(
    layout: _Layout, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--zip`` 必须把目录里每个文件都装进 zip, 并打印条目数与 SHA256."""
    exit_code = verifier.main(
        [str(layout.report), "--results", str(layout.results), "--zip"]
    )
    output = capsys.readouterr().out
    archive = layout.report.with_suffix(".zip")

    assert exit_code == 0
    assert archive.is_file()
    on_disk = sorted(
        path.relative_to(layout.report).as_posix()
        for path in layout.report.rglob("*")
        if path.is_file()
    )
    with zipfile.ZipFile(archive) as bundle:
        packed = sorted(
            name.removeprefix(f"{layout.report.name}/") for name in bundle.namelist()
        )
    assert packed == on_disk
    assert f"{len(on_disk)} 个条目" in output
    assert "SHA256:" in output


def test_ci_publishes_only_verified_report() -> None:
    """CI 必须先自检报告再发布, 并且发布单个 zip(整目录被静默丢掉的情况不会再出现)."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    verifications = workflow.count("scripts/verify_allure_report.py")

    assert verifications >= 3, "两个作业的结构自检 + 汇总作业的平台用例检查"
    # 发布 zip 的前提是**出 zip 的那次自检**通过: 两个作业各一份。
    commands = [
        line.strip()
        for line in workflow.splitlines()
        if line.strip().startswith("run:") and "verify_allure_report.py" in line
    ]
    assert len(commands) == verifications, "自检点数量与命令数量对不上"
    zip_checks = [command for command in commands if "--zip" in command]
    assert len(zip_checks) == 2, "pytest 与汇总作业各出一份 zip 报告"
    assert workflow.count("steps.verify-report.outcome == 'success'") == len(zip_checks)
    assert workflow.count("path: allure-report.zip") == len(zip_checks)
    assert "path: allure-report/" not in workflow, "报告目录不再直接发布"
    # 平台用例检查: pytest 作业传自己那个平台(runner.os 就是 Windows/macOS/Linux),
    # 汇总作业传三个平台 —— 两处都要有, 少一处就等于少一道检查(只数 `run:` 行, 注释不算)。
    platform_checks = [
        command for command in commands if "--expect-platforms" in command
    ]
    assert len(platform_checks) == 2, "pytest 作业与汇总作业各要检查一次平台用例"
    assert (
        sum('--expect-platforms "${{ runner.os }}"' in c for c in platform_checks) == 1
    )
    assert (
        sum("--expect-platforms Windows,macOS,Linux" in c for c in platform_checks) == 1
    )
    # 汇总作业里那条不拦发布(报告正是用来看"哪个平台没数据"的地方), 所以它的结论必须有
    # 地方接手: 末尾的门禁结论步骤要带上它, 否则失败了也没人管。
    step = workflow.split("name: Check every platform contributed tests", 1)[1]
    assert "continue-on-error: true" in step.split("\n      - name:", 1)[0]
    verdict = workflow.split("name: Report quality gate verdict", 1)[-1]
    assert "steps.verify-platforms.outcome == 'failure'" in verdict, (
        "平台用例检查的结论必须由末尾的门禁结论步骤接手"
    )


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
    assert 'title="Performance baseline"' in summary, "性能汇总项的标题不应再拼平台名"
    assert 'title="Security findings"' in summary, "安全汇总项的标题不应再拼平台名"


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
    coverage_xml.write_text(_COVERAGE_XML, encoding="utf-8")
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(module, "COVERAGE_XML", coverage_xml)

    module.main()

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
    assert "Coverage by package" in payload["description"]


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

    module.main()

    payload = json.loads(next(results.glob("*-result.json")).read_text("utf-8"))
    attachment = payload["attachments"]
    assert [item["name"] for item in attachment] == ["coverage.xml"]
    assert attachment[0]["type"] == "application/xml"
    assert (results / attachment[0]["source"]).read_bytes() == coverage_xml.read_bytes()
    assert "## Raw report" in payload["description"]


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

    module.main()

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

    module.main()

    payload = json.loads(next(results.glob("*-result.json")).read_text("utf-8"))
    assert payload["status"] == "broken"
    assert payload["attachments"] == []
    assert "Raw report" not in payload["description"]


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
    """首页「全局附件」靠配置里的 glob 匹配 —— 文件名与脚本写的必须一致."""
    module = _load_script("create_allure_summary")
    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    assert f'globalAttachments: ["{module.QUALITY_GATE_REPORT.name}"]' in config


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
    assert "3.18.0" in config, "注释里要写明版本要求与原因"
    assert "#895" in config, "注释里要留 issue 号, 便于日后重测"

    workflow = _WORKFLOW.read_text(encoding="utf-8")
    gate = "allure quality-gate --config allurerc.mjs allure-results"
    assert gate in workflow, "CI 要真的跑原生质量门"
    assert "npm install --global allure@3.18.0" in workflow, "CLI 必须钉在修好的版本"
    assert "npm install --global allure@3\n" not in workflow, (
        "不能再用浮动标签 allure@3"
    )
    assert "Report quality gate verdict" in workflow, "门禁结论要能决定作业成败"
    # 先跑门禁(留日志给总账), 再生成报告: 顺序反了日志就进不了报告。
    assert workflow.index(gate) < workflow.index("Generate final Allure report")


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
    assert 'environmentsTested: ["Windows", "macOS", "Linux"]' in gate
    # 规则集要有 id: 门禁失败时输出的是 `<规则集 id>/<规则名>`, 一眼看出是哪条不过。
    assert re.search(r'id:\s*"[\w-]+"', gate) is not None

    # 只有"写结果"的脚本受这条约束(校验脚本要**读**这个标签, 不在其列: 它自己就是
    # 用 framework=pytest 判断"这条结果是用例还是脚本产物"的那个实现)。
    writers = (
        "create_allure_quality",
        "create_allure_coverage",
        "create_allure_summary",
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
        "## 产物清单",
    ):
        assert section in ledger
    assert "91.82%" in ledger, "覆盖率数字要取自原始 XML"
    assert "75.00%" in ledger, "分支覆盖率也要写出来"
    assert "?" not in ledger.split("## 覆盖率")[1].split("##")[0], "覆盖率不该是问号"
    assert "| 平台 | 结论条数 | 未拦截条数 | 原始结论 |" in ledger, "表头要说清两列含义"
    assert "已收录" in ledger, "产物清单的状态列要用能看懂的词"
    assert "| Windows |" in ledger, "覆盖率按平台各一行"
    assert "全部达标(1 项)" in ledger, "性能基准给一行结论"
    assert "| Linux | 1 | 0 |" in ledger, "安全那一行要给出结论数与未拦截数"
    assert "**缺失**" in ledger, "声明了却找不到的原始文件必须被标出来"


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
    """公共检查只在 Ubuntu 跑一遍(跑三平台只会得到三份一样的结论)."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  quality:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 quality job"
    job = block.group(1)
    assert "runs-on: ubuntu-latest" in job
    assert "matrix" not in job, "质量作业不应再按平台展开"


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
    """平台专属检查要放在对应平台的作业里跑, 结论还要被汇总作业收走.

    以前的写法是在 Ubuntu 上把 ``--platform win32`` 与 ``--platform darwin`` 各跑一遍:
    检查本身能过, 但报告里那两条结论标着 Windows / macOS 却产自 Linux —— 环境选择器一筛,
    "哪个平台的专属代码路径出问题"这件事就被说错了。现在拆成独立作业(按平台展开),
    这里把"跑在哪个平台"与"标着哪个平台"钉在一起。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    block = re.search(r"\n  quality-platform:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 quality-platform job"
    job = block.group(1)
    assert "os: [windows-latest, macos-latest]" in job, "两个平台各跑自己那一支"
    assert "runs-on: ${{ matrix.os }}" in job
    assert "ubuntu-latest" not in job, "平台专属检查不该再跑到 Ubuntu 上"
    assert "scripts/create_allure_quality.py --group platform" in job
    assert "allure-results-quality-platform-${{ matrix.os }}" in job
    assert "save-cache: false" in job, "依赖缓存只由 pytest 第 0 片写入"

    # 公共质量作业不再代跑平台专属检查(否则同一批检查会跑两遍, 归属还重复)。
    quality = re.search(r"\n  quality:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert quality is not None
    assert "--group platform" not in quality.group(1)

    # 汇总作业必须等这个作业, 并把它的产物收进报告。
    summary = re.search(r"\n  allure-summary:\n(.*)", workflow, re.DOTALL)
    assert summary is not None, "ci.yml 里找不到 allure-summary job"
    assert "quality-platform" in summary.group(1), "汇总作业要等平台专属检查跑完"
    assert "pattern: allure-results-quality-platform-*" in summary.group(1)


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
