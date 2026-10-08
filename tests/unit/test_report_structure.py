"""
报告目录结构校验(verify_report 脚本): 缺文件/数量不符/悬空引用/嵌套目录/环境缺失逐项点名, 退出码可判。拆自 test_report_verification.py(见 docs/test-refactor-plan.md S8)。
"""

from __future__ import annotations

import json
import re
import shutil
import tarfile
from pathlib import Path

import pytest

import ci_workflow
from report_support import (
    _RESULT_IDS,
    _WORKFLOW,
    _Layout,
    _write_platform_report,
    required_platforms,
    verifier,
)

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


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
    macOS 曾在开发阶段屏蔽(省额度), 2026-09-30 恢复后三处一起加回来。

    汇总脚本自己也吃同一份清单(``create_allure_summary.py --expect-platforms``): 它决定运行
    总账末节「证据核对」里"应有"的那些每平台项 —— 少列一个平台, 那个平台整族的结论(用例/
    覆盖率/安全)就没人点名, 而报告看上去仍然完整。所以是**同一份清单的第三处拷贝**, 一起钉住。
    这个列表还与工作流里每个作业的矩阵"每轮都跑"是一件事: 按平台展开的结论族不许对平台做
    有条件排除(见 ``test_every_platform_conclusion_is_produced_on_every_run``)。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    required = set(required_platforms())
    assert required == {"Windows", "macOS", "Linux"}, (
        "要求哪几个平台要有用例: 恢复/屏蔽某个平台时这一条要跟着改"
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
