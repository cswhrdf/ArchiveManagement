"""Allure 报告完整性校验脚本的回归用例.

背景: 报告是静态站点, 用例详情页要按需取 ``data/test-results/<结果 id>.json``。
CI 里出现过"报告只剩通过/失败, 点开用例是空的" —— 该目录在发布链路上被丢掉了,
而生成阶段本身是好的(用同一个 allure 版本本地生成就有这些文件)。这里锁住四件事:

- 缺详情目录 / 缺单个详情文件都必须判为不完整;
- 结果条数与 ``allure-results`` 不一致(合并或生成掉数据)必须判为不完整;
- 没有报告时(本次没跑出结果)不算失败;
- CI 只在自检通过后发布, 且发布的是单个 zip。
"""

from __future__ import annotations

import importlib.util
import io
import json
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
_SCRIPT = _REPO_ROOT / "scripts" / "verify_allure_report.py"
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
_RESULT_IDS = ("aaa111", "bbb222")


def _load_verifier() -> Any:
    """按路径加载校验脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    spec = importlib.util.spec_from_file_location("verify_allure_report", _SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
    """脚本生成的汇总项要带严重等级, 否则报告里会多出一个 no_severity 桶."""
    scripts = (
        _REPO_ROOT / "scripts" / "create_allure_coverage.py",
        _REPO_ROOT / "scripts" / "create_allure_summary.py",
    )

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert '"name": "severity"' in text, f"{script.name} 未给汇总项写 severity 标签"


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
