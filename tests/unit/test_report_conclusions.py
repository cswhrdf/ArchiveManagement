"""
结论计算: 安全/性能/覆盖率结论的分红绿破三态、覆盖率项的附件与阈值来源、台账列与证据。拆自 test_report_verification.py(见 docs/test-refactor-plan.md S8)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from report_support import (
    _PYPROJECT,
    _WORKFLOW,
    _conclusion_table,
    _load_script,
    _write_platform_result,
)

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


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


_COVERAGE_XML_PASSING = """<?xml version="1.0" encoding="UTF-8"?>
<coverage line-rate="1.0" branch-rate="1.0" lines-covered="100" lines-valid="100"
          branches-covered="100" branches-valid="100">
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
    assert (
        "当前覆盖率 100.00% 大于预期覆盖率 100%, 验证通过。" in payload["description"]
    )


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
    assert module.verdict_text(1.0, module.coverage_threshold()).endswith("验证通过。")
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


def test_the_coverage_conclusion_is_published() -> None:
    """覆盖率结论必须**单独上传** —— 合并后的 ``allure-results/`` 不再上传.

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
    assert rows == [("Windows", "90.00%", "90.00%", "90.00%", "100%", "未通过")]


def test_coverage_conclusion_stays_green_above_the_threshold(tmp_path: Path) -> None:
    """达标的运行保持绿色: 合计覆盖率按 coverage.py 的口径算."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_coverage_item(results, platform="Linux", line_rate=1.0, branch_rate=1.0)

    status, failures, rows = module.coverage_conclusion(
        results, module.result_payloads(results), ["Linux"]
    )

    assert status == "passed"
    assert failures == []
    assert rows == [("Linux", "100.00%", "100.00%", "100.00%", "100%", "通过")]


def test_coverage_conclusion_is_red_when_a_platform_has_no_item(
    tmp_path: Path,
) -> None:
    """有用例结果的平台缺一份覆盖率结论时必须红(文件不在不等于通过)."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_coverage_item(results, platform="Windows", line_rate=1.0, branch_rate=1.0)

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
    assert rows == [("Windows", "?", "?", "?", "100%", "未通过")]


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
    assert "| Windows | 90.00% | 90.00% | 90.00% | 100% | 未通过 |" in failing
    assert "| 平台 | 基准数 | 未达标 | 结论 |" in failing
    assert "| Linux | 1 | 1 | **未通过** |" in failing

    healthy_results = tmp_path / "healthy-results"
    healthy_results.mkdir()
    _write_coverage_item(
        healthy_results, platform="Windows", line_rate=1.0, branch_rate=1.0
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
    assert "| Windows | 100.00% | 100.00% | 100.00% | 100% | 通过 |" in healthy
    assert "| Linux | 1 | 0 | 通过 |" in healthy


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
