"""耗时追踪结论项: 把 trace 从用例上摘下来, 报告里只挂在一条独立结论上.

锁住四件事:

- **搬家**: 用例上的两条附件(原始 trace + 摘要)挪到 ``archive-management.profile`` 上,
  文件跟着改名(不留孤儿文件), 用例上的引用删掉、其余附件原样保留;
- **归属**: 结论按 ``env`` 标签落到各平台的环境, 标题与身份都不拼平台名;
- **可读**: Top self time 摘要内联进描述(报告里不用下载就能读);
- **没有 trace 的平台什么都不写**, 只打一行提示(按需产出, 缺了不算缺失)。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import ci_workflow
from report_support import _WORKFLOW, _load_script

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("耗时追踪结论项"),
    pytest.mark.layer("unit"),
]

_TRACE_NAME = "耗时追踪 trace · profile-on-test-x-py--test-y-123.json"
_SUMMARY_NAME = "耗时追踪摘要 · Top self time"
_SUMMARY_BODY = (
    "追踪事件 12 条, 合计 7.5 ms(按 self time 聚合)\n    7.5 ms  Database.session\n"
)


def _test_result(
    results: Path,
    *,
    slug: str = "aaaa",
    platform: str = "Linux",
    with_trace: bool = True,
) -> Path:
    """写一条"带追踪附件"的用例结果(trace 与摘要各一个文件, 外加一条无关的日志附件)."""
    (results / f"{slug}-attachment.json").write_text(
        '{"trace": true}', encoding="utf-8"
    )
    (results / f"{slug}-attachment.txt").write_text(_SUMMARY_BODY, encoding="utf-8")
    (results / f"{slug}-log.txt").write_text("log", encoding="utf-8")
    attachments: list[dict[str, str]] = [
        {"name": "log", "source": f"{slug}-log.txt", "type": "text/plain"}
    ]
    if with_trace:
        attachments[:0] = [
            {
                "name": _TRACE_NAME,
                "source": f"{slug}-attachment.json",
                "type": "application/octet-stream",
            },
            {
                "name": _SUMMARY_NAME,
                "source": f"{slug}-attachment.txt",
                "type": "text/plain",
            },
        ]
    payload = {
        "uuid": slug,
        "name": "带追踪的用例",
        "fullName": f"tests.integration.test_x#{slug}",
        "status": "passed",
        "labels": [
            {"name": "env", "value": platform},
            {"name": "os", "value": platform},
            {"name": "framework", "value": "pytest"},
        ],
        "attachments": attachments,
    }
    path = results / f"{slug}-result.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _conclusions(results: Path) -> list[dict[str, Any]]:
    """结果目录里所有 ``archive-management.profile`` 结论项."""
    module = _load_script("create_allure_profile")
    found = []
    for path in sorted(results.glob("*-result.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("fullName") == module.IDENTITY:
            found.append(payload)
    return found


def test_the_trace_moves_off_the_test_result(tmp_path: Path) -> None:
    """附件要从用例搬到结论项上: 用例只留自己的日志, 结论项拿 trace + 摘要."""
    module = _load_script("create_allure_profile")
    results = tmp_path / "allure-results"
    results.mkdir()
    source = _test_result(results)

    assert module.main(["--results", str(results)]) == 0

    conclusions = _conclusions(results)
    assert len(conclusions) == 1, f"应当只有一条结论项, 实际 {len(conclusions)}"
    names = [item["name"] for item in conclusions[0]["attachments"]]
    assert names == [_TRACE_NAME, _SUMMARY_NAME], f"结论项要带 trace 与摘要: {names}"
    for item in conclusions[0]["attachments"]:
        assert (results / item["source"]).is_file(), f"附件文件不存在: {item['source']}"
    trace = next(
        item for item in conclusions[0]["attachments"] if item["name"] == _TRACE_NAME
    )
    assert trace["type"] == "application/octet-stream", (
        "trace 仍按二进制挂(别让 Allure 渲染)"
    )
    moved = (results / trace["source"]).read_text(encoding="utf-8")
    assert moved == '{"trace": true}', "搬过去的是同一份字节"

    remaining = json.loads(source.read_text(encoding="utf-8"))["attachments"]
    assert [item["name"] for item in remaining] == ["log"], (
        f"用例上只该留它自己的附件, 实际 {remaining}"
    )
    assert not (results / "aaaa-attachment.json").exists(), "搬家后不许留孤儿文件"


def test_the_conclusion_lands_in_the_platform_environment(tmp_path: Path) -> None:
    """结论按 env 标签归环境, 标题与身份都不拼平台名(与覆盖率结论同一口径)."""
    module = _load_script("create_allure_profile")
    results = tmp_path / "allure-results"
    results.mkdir()
    _test_result(results, platform="macOS")

    module.main(["--results", str(results)])

    conclusion = _conclusions(results)[0]
    labels = {item["name"]: item["value"] for item in conclusion["labels"]}
    assert labels["env"] == "macOS", "要落到源用例所在的环境"
    assert labels["os"] == "macOS", "os 标签是脚本认平台的依据"
    assert labels["severity"] == "trivial", (
        "汇总项不打等级会让报告多一个 no_severity 桶"
    )
    assert conclusion["name"] == module.TITLE, "标题不拼平台名"
    assert "macOS" not in module.IDENTITY, "身份(历史趋势的键)也不带平台"


def test_the_summary_is_inlined_into_the_description(tmp_path: Path) -> None:
    """摘要内联进描述: 报告里不下载附件也能看到 Top self time."""
    module = _load_script("create_allure_profile")
    results = tmp_path / "allure-results"
    results.mkdir()
    _test_result(results)

    module.main(["--results", str(results)])

    description = _conclusions(results)[0]["description"]
    assert "Database.session" in description, "摘要正文要进描述"
    assert "vizviewer" in description, "要说明 trace 怎么打开"


def test_platforms_without_a_trace_get_no_item(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """没开追踪的平台什么都不写(按需产出): 不写空结论, 也不报错."""
    module = _load_script("create_allure_profile")
    results = tmp_path / "allure-results"
    results.mkdir()
    _test_result(results, with_trace=False)

    assert module.main(["--results", str(results)]) == 0
    assert _conclusions(results) == [], "没有 trace 就不该写这条结论"
    assert "没有耗时追踪附件" in capsys.readouterr().out, "要留一行可检索的提示"


def test_a_missing_trace_file_keeps_the_summary(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """trace 文件丢了时仍然建结论(只带摘要): 摘要能读, 比整条结论消失有用."""
    module = _load_script("create_allure_profile")
    results = tmp_path / "allure-results"
    results.mkdir()
    _test_result(results)
    (results / "aaaa-attachment.json").unlink()

    module.main(["--results", str(results)])

    attachments = _conclusions(results)[0]["attachments"]
    assert [item["name"] for item in attachments] == [_SUMMARY_NAME]
    assert "附件文件不存在" in capsys.readouterr().out, "丢附件要在日志里点名"


def test_the_summary_job_moves_the_trace_before_it_writes_the_ledger() -> None:
    """接线: 汇总作业要在写总账**之前**跑这一步, 且无论前面的步骤红没红都得跑.

    晚一步不会报错, 只是运行总账的 ② 产物清单里永远看不到这条结论(那份清单按
    ``archive-management.`` 前缀列结果与附件) —— 于是"报告里少了 trace"这件事没有任何
    痕迹。``if: always()`` 同理: 某一步红了不该把证据一并吞掉。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "allure-summary")

    assert "name: Attach the profile trace to Allure" in job, (
        "汇总作业要跑 scripts/create_allure_profile.py, 否则 trace 仍留在用例上"
    )
    step = job.split("name: Attach the profile trace to Allure", 1)[1].split(
        "- name:", 1
    )[0]
    assert "scripts/create_allure_profile.py" in step, "这一步就是跑那个脚本"
    assert "if: always()" in step, "前面哪一步红了也要把结论项写出来"

    order = [
        job.index("name: Attach the profile trace to Allure"),
        job.index("name: Summarize performance and security into Allure"),
    ]
    assert order == sorted(order), "要排在写运行总账之前(否则总账里看不到它)"
