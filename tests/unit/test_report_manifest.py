"""
分片清单(manifest)与平台结论完整性: 缺片/计数不符点名, 缺省模式失败, expect-platforms 决定结论。拆自 test_report_verification.py(见 docs/test-refactor-plan.md S8)。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

import ci_workflow
from report_support import (
    _WORKFLOW,
    _Layout,
    _load_script,
    _write_platform_report,
    verifier,
)

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


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
