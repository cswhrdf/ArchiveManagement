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

    assert verifications >= 2, "pytest 与汇总作业都要自检报告"
    assert workflow.count("steps.verify-report.outcome == 'success'") == verifications
    assert workflow.count("path: allure-report.zip") == verifications
    assert "path: allure-report/" not in workflow, "报告目录不再直接发布"


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
    """两组门禁都在脚本里, 且 CI 分两处调用它们: 漏一项报告就缺项."""
    module = _load_script("create_allure_quality")
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    core = [check.key for check in module.CHECKS if check.group == "core"]
    analysis = [check.key for check in module.CHECKS if check.group == "analysis"]
    assert core == ["ruff-check", "ruff-format", "mypy", "mypy-win32", "mypy-darwin"]
    assert analysis == ["deptry", "bandit", "pip-audit", "radon", "xenon"]
    # 平台专属分支: 这两项必须带着 --platform, 否则等于把宿主平台又跑了一遍。
    for check in (item for item in module.CHECKS if item.key.startswith("mypy-")):
        assert "--platform" in check.command
    assert "scripts/create_allure_quality.py --group core" in workflow
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
    for rule in ("maxFailures", "minTestsCount", "successRate", "environmentsTested"):
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


def test_quality_job_runs_on_one_platform() -> None:
    """公共检查只在 Ubuntu 跑一遍(跑三平台只会得到三份一样的结论)."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  quality:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 quality job"
    job = block.group(1)
    assert "runs-on: ubuntu-latest" in job
    assert "matrix" not in job, "质量作业不应再按平台展开"


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
