"""测试严重等级与 Allure 元数据.

严重等级使用 Allure 官方分级: blocker/critical/normal/minor/trivial/no_severity.
解析优先级: 用例或模块上显式标注的等级(如 ``@pytest.mark.blocker``)优先, 否则
按目录默认(unit=critical, integration=normal, 其余 no_severity)。本地 CI
(pre-commit)通过 ``--min-severity`` 只保留 normal 以上(不含 normal)即
blocker+critical 的用例; GitHub Actions 仍运行全部用例。
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import allure
import pytest

_TestObject = Callable[..., Any]

_SEVERITY_LEVELS = ("blocker", "critical", "normal", "minor", "trivial")
_SEVERITY_RANK = {
    "blocker": 5,
    "critical": 4,
    "normal": 3,
    "minor": 2,
    "trivial": 1,
    "no_severity": 0,
}
_ALLURE_LEVELS = {
    "blocker": allure.severity_level.BLOCKER,
    "critical": allure.severity_level.CRITICAL,
    "normal": allure.severity_level.NORMAL,
    "minor": allure.severity_level.MINOR,
    "trivial": allure.severity_level.TRIVIAL,
}


def pytest_addoption(parser: pytest.Parser) -> None:
    """注册 --min-severity: 本地只保留不低于该级别的用例."""
    parser.addoption(
        "--min-severity",
        action="store",
        default=None,
        choices=list(_SEVERITY_LEVELS),
        help="只保留严重等级不低于该级别的测试(本地 CI 用 critical)",
    )


def _resolve_severity(item: pytest.Item) -> str:
    """解析用例严重等级: 显式标记优先, 否则按目录默认."""
    for level in _SEVERITY_LEVELS:
        if item.get_closest_marker(level):
            return level
    parts = Path(str(item.fspath)).parts
    if "unit" in parts:
        return "critical"
    if "integration" in parts:
        return "normal"
    return "no_severity"


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """附加严重等级/Allure 元数据, 并按 --min-severity 过滤."""
    minimum = config.getoption("--min-severity")
    min_rank = _SEVERITY_RANK[minimum] if minimum is not None else None
    kept: list[pytest.Item] = []
    for item in items:
        severity = _resolve_severity(item)
        item.add_marker(getattr(pytest.mark, severity))

        title = item.name.removeprefix("test_").replace("_", " ").capitalize()
        module = getattr(item, "module", None)
        module_description = inspect.getdoc(module) or "ArchiveManagement 测试用例"
        description = f"{module_description}\n\n验证行为: {title}。"
        item.add_marker(pytest.mark.allure_description(description))
        test_object = cast(_TestObject, getattr(item, "obj", None))
        allure.title(title)(test_object)  # type: ignore[no-untyped-call]
        allure.description(description)(test_object)  # type: ignore[no-untyped-call]

        if min_rank is not None and _SEVERITY_RANK[severity] < min_rank:
            continue
        kept.append(item)

    if min_rank is not None:
        deselected = [item for item in items if item not in kept]
        items[:] = kept
        if deselected:
            config.hook.pytest_deselected(items=deselected)


def _configure_allure(item: pytest.Item) -> None:
    """把测试名称、严重等级和分类写入 Allure 元数据."""
    title = item.name.removeprefix("test_").replace("_", " ").capitalize()
    module = getattr(item, "module", None)
    module_description = inspect.getdoc(module) or "ArchiveManagement 测试用例"
    excluded = {"allure_description", "integration"} | set(_SEVERITY_LEVELS)
    categories = {
        marker.name for marker in item.iter_markers() if marker.name not in excluded
    }
    category = sorted(categories)[0] if categories else "uncategorized"

    allure.dynamic.title(title)  # type: ignore[no-untyped-call]
    allure.dynamic.description(  # type: ignore[no-untyped-call]
        f"{module_description}\n\n验证行为: {title}。"
    )
    allure.dynamic.epic("ArchiveManagement")  # type: ignore[no-untyped-call]
    allure.dynamic.feature(category)  # type: ignore[no-untyped-call]
    allure.dynamic.story(title)  # type: ignore[no-untyped-call]
    allure.dynamic.tag(category)  # type: ignore[no-untyped-call]

    severity = _resolve_severity(item)
    if severity != "no_severity":
        allure.dynamic.severity(  # type: ignore[no-untyped-call]
            _ALLURE_LEVELS[severity]
        )


@pytest.fixture(autouse=True)
def _allure_metadata(request: pytest.FixtureRequest) -> None:
    """在每个测试开始后写入 Allure 元数据。"""
    _configure_allure(request.node)
