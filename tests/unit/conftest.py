"""单元层的共享 fixture.

只有**逐字相同**的 fixture 定义才上提到这里(识别结论见 docs/test-refactor-plan.md);
构造特定游戏/备份状态的业务种子仍由各用例显式调用 ``helpers`` 表达。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archive_management.infrastructure.database import Database
from archive_management.ui.demo_backend import DemoArchiveService
from helpers import migrated_database
from report_support import _RESULT_IDS, _Layout


@pytest.fixture
def database(tmp_path: Path) -> Database:
    """真实 SQLite 数据库(已迁移)."""
    return migrated_database(tmp_path)


@pytest.fixture
def service() -> DemoArchiveService:
    """不模拟耗时的演示后端."""
    return DemoArchiveService(delay=0)


# 拆分 test_report_verification.py 时上提: 6 个报告测试文件共享的报告布局 fixture(见 docs/test-refactor-plan.md S8).


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
