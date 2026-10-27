"""
产物冒烟结论: 判据标识稳定、冒烟脚本落 JSON 的形状、writer 转 Allure 结果(通过/失败双态与环境归属)、缺报告时的安静退场。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from report_support import _load_script

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


def test_the_smoke_battery_defines_stable_check_identities() -> None:
    """五条判据各配一个稳定标识: 它拼进报告身份, 重复会让结论互相顶掉、缺配会让脚本崩."""
    smoke = _load_script("smoke_packaged")
    keys = [key for key, _ in smoke.CHECK_IDENTITIES]

    assert len(keys) == 5, "判据就是五条: 少一条 run_battery 的 strict 配对会当场崩"
    assert len(set(keys)) == len(keys), "判据标识必须唯一(同名的结果项会互相顶掉)"
    assert all(name for _, name in smoke.CHECK_IDENTITIES), "每条判据都要有显示名"
    # 标识是身份的一部分, 只许是小写连字符(写别的进 fullName 会变得不可读)。
    assert all(key == key.lower() and " " not in key for key in keys)


def test_smoke_reports_carry_the_full_verdicts(tmp_path: Path) -> None:
    """write_report 落的 JSON 要带平台与逐条结论(标识/显示名/结论/说明)."""
    smoke = _load_script("smoke_packaged")
    artifacts = [
        {
            "label": "onedir",
            "exe": "dist/ArchiveManagement/ArchiveManagement.exe",
            "checks": [
                {
                    "key": "version",
                    "name": "--version",
                    "passed": True,
                    "detail": "退出码 0",
                },
                {
                    "key": "doctor",
                    "name": "doctor",
                    "passed": False,
                    "detail": "root 下缺 logs",
                },
            ],
        }
    ]
    report = tmp_path / "smoke-report.json"

    smoke.write_report(report, artifacts)

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["platform"] == smoke.REPORT_PLATFORM, (
        "平台写进 JSON, writer 才知道归属"
    )
    assert payload["artifacts"] == artifacts, "逐条结论要原样落盘(失败也在里面)"


def _fake_report(path: Path) -> None:
    """造一份两判据(一过一红)的冒烟结论, 覆盖 onedir 与 onefile 两个产物."""
    path.write_text(
        json.dumps(
            {
                "platform": "Windows",
                "artifacts": [
                    {
                        "label": "onedir",
                        "checks": [
                            {
                                "key": "version",
                                "name": "--version",
                                "passed": True,
                                "detail": "退出码 0",
                            }
                        ],
                    },
                    {
                        "label": "onefile",
                        "checks": [
                            {
                                "key": "doctor",
                                "name": "doctor",
                                "passed": False,
                                "detail": "root 下缺 logs",
                            }
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def test_smoke_results_record_pass_and_fail(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """writer 双态: 通过/失败都写成结果项, 失败带原因; 标签决定环境归属与失败分类.

    环境标签(``env=Windows``)是结论跟着平台用例走的前提 —— 漏写会落进隐式 default 环境,
    ``scripts/verify_allure_report.py`` 按平台对数时直接判不完整(2026-10-14 的坑)。
    """
    writer = _load_script("create_allure_smoke")
    report = tmp_path / "smoke-report.json"
    _fake_report(report)
    results = tmp_path / "allure-results-smoke-windows"

    exit_code = writer.main(["--report", str(report), "--results-dir", str(results)])

    assert exit_code == 0, "writer 不是门禁(门禁是 smoke_packaged.py 的退出码), 恒 0"
    by_identity = {
        payload["fullName"]: payload
        for payload in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(results.glob("*-result.json"))
        )
    }
    assert set(by_identity) == {
        "archive-management.smoke.onedir.version",
        "archive-management.smoke.onefile.doctor",
    }, "每条判据一个结果项, 身份由产物标识 + 判据标识拼成"
    passed = by_identity["archive-management.smoke.onedir.version"]
    failed = by_identity["archive-management.smoke.onefile.doctor"]
    assert passed["status"] == "passed"
    assert failed["status"] == "failed", "失败也要照写 —— 报告要看得见哪条没过"
    assert "root 下缺 logs" in failed["statusDetails"]["message"]
    for payload in (passed, failed):
        labels = {item["name"]: item["value"] for item in payload["labels"]}
        assert labels["env"] == "Windows", "结论要归进那个平台的环境, 不能落 default"
        assert labels["testCategory"] == "smoke", "分类规则(allurerc.mjs)按它挑冒烟结果"
        assert "framework" not in labels, (
            "不许冒充真实用例(质量门 tests-on-every-platform 的判据)"
        )
        assert payload["historyId"], "稳定的历史键: 同一判据跨运行才能接上趋势"
    source = failed["attachments"][0]["source"]
    assert (results / source).read_text(encoding="utf-8") == report.read_text(
        encoding="utf-8"
    ), "原始 JSON 整份挂附件, 判据的完整上下文点得开"
    assert "2 条判据" in capsys.readouterr().out


def test_a_report_without_a_platform_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """报告里没有平台字段时不写任何结果项 —— 环境归属不明, 宁缺毋假."""
    writer = _load_script("create_allure_smoke")
    report = tmp_path / "smoke-report.json"
    report.write_text(
        json.dumps(
            {
                "artifacts": [
                    {"label": "onedir", "checks": [{"key": "version", "passed": True}]}
                ]
            }
        ),
        encoding="utf-8",
    )
    results = tmp_path / "out"

    assert writer.main(["--report", str(report), "--results-dir", str(results)]) == 0
    assert list(results.glob("*-result.json")) == [], "归属不明的结论不落盘"
    assert "::warning::" in capsys.readouterr().err


def test_an_absent_report_is_not_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """结论文件不在(构建更早红掉/冒烟被跳过)时安静退场: 门禁成败不归这一步管.

    "该有而没有"由汇总作业的条件期望(``--expect-smoke-platforms``)点名 —— 那才是正主。
    """
    writer = _load_script("create_allure_smoke")
    results = tmp_path / "out"

    missing = tmp_path / "missing.json"
    assert (
        writer.main(["--report", str(missing), "--results-dir", str(results)]) == 0
    ), "缺文件不是这一步的失败(几乎总是更早的失败造成的)"
    assert not list(results.glob("*-result.json"))
    assert "::warning::" in capsys.readouterr().out
