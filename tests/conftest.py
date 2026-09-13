"""测试严重等级与 Allure 元数据(含四层标签).

严重等级使用 Allure 官方分级: blocker/critical/normal/minor/trivial/no_severity.
解析优先级: 用例或模块上显式标注的等级(如 ``@pytest.mark.blocker``)优先, 否则
按目录默认(unit=critical, integration=normal, 其余 no_severity)。本地 CI
(pre-commit)通过 ``--min-severity`` 只保留 normal 以上(不含 normal)即
blocker+critical 的用例; GitHub Actions 仍运行全部用例。

Allure 标签语义(与 Allure 3 报告控件一一对应):

- ``epic``: 产品级模块, 取值限定在 ``_EPICS``(基础工程/数据持久化/界面框架/
  游戏与存档位置/备份与分支/工程与发布);
- ``feature``: 功能模块, 与生产模块一一对应(如 快照服务、数据仓储);
- ``story``: 具体用户场景(如 删除备份节点、创建备份与分支);
- ``layer``: 测试层次, ``unit`` / ``integration`` / ``e2e``, 供"按层耗时"直方图使用。

测试模块用 ``pytestmark`` 声明默认标签, 单个用例可用同名标记覆盖
(``@pytest.mark.story("...")``)。``layer`` 未声明时按 ``integration`` 标记或目录推断,
保证直方图不会缺数据; 声明的 ``epic``/``layer`` 必须属于闭集, 拼错在收集期直接报错,
以避免报告里冒出只有一个用例的畸形分类。
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import allure
import pytest

from archive_management.services.audit import AUDIT_LOGGER_NAME

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

# 测试层次: 闭集, 供 Allure "按层耗时" 直方图分组使用.
_LAYERS = ("unit", "integration", "e2e")
# 层次在 suite 视图里的展示名(Allure 3 部分控件只认 suite 标签, 双保险).
_LAYER_SUITES = {
    "unit": "单元测试 unit",
    "integration": "集成测试 integration",
    "e2e": "端到端测试 e2e",
}
# 产品级模块: 闭集, 新增产品级模块时在此登记.
_EPICS = (
    "基础工程",
    "数据持久化",
    "界面框架",
    "游戏与存档位置",
    "备份与分支",
    "工程与发布",
)
# 标签取值约束: None 表示自由文本(功能模块与场景随功能增长).
_LABEL_ALLOWED: dict[str, tuple[str, ...] | None] = {
    "epic": _EPICS,
    "feature": None,
    "story": None,
    "layer": _LAYERS,
}
_METADATA_MARKERS = frozenset(_LABEL_ALLOWED)
# 元数据与等级标记不再重复当作 tag(其余标记如 backend/ui 保留为标签).
_TAG_EXCLUDED = _METADATA_MARKERS | set(_SEVERITY_LEVELS) | {"integration"}
_UNCLASSIFIED = "未分类"
# allure.dynamic 的函数没有类型标注, 统一按 Any 调用, 避免满屏 no-untyped-call 忽略.
_ALLURE_DYNAMIC: Any = allure.dynamic


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


def _where(item: pytest.Item) -> str:
    """定位串, 用于拼写错误提示."""
    return f"{item.fspath}::{item.name}"


def _label_value(
    item: pytest.Item, name: str, allowed: tuple[str, ...] | None = None
) -> str | None:
    """取最近一层同名标记的取值(用例级覆盖模块级), 并校验闭集取值."""
    marker = item.get_closest_marker(name)
    if marker is None:
        return None
    if not marker.args:
        raise pytest.UsageError(
            f'{_where(item)}: {name} 标记缺少取值, 请写成 pytest.mark.{name}("...")'
        )
    value = str(marker.args[0])
    if allowed is not None and value not in allowed:
        raise pytest.UsageError(
            f"{_where(item)}: 未知的 {name} {value!r}, 可用取值: {' / '.join(allowed)}"
        )
    return value


def _default_layer(item: pytest.Item) -> str:
    """未显式声明 layer 时的兵底: integration 标记或目录名, 否则 unit."""
    if item.get_closest_marker("integration"):
        return "integration"
    if "integration" in Path(str(item.fspath)).parts:
        return "integration"
    return "unit"


def _validate_metadata(item: pytest.Item) -> None:
    """收集期校验标签取值, 拼错时立即失败而不是静默多出分类."""
    for name, allowed in _LABEL_ALLOWED.items():
        _label_value(item, name, allowed)


def pytest_collection_modifyitems(
    config: pytest.Config, items: list[pytest.Item]
) -> None:
    """附加严重等级, 校验 Allure 标签, 并按 --min-severity 过滤."""
    minimum = config.getoption("--min-severity")
    min_rank = _SEVERITY_RANK[minimum] if minimum is not None else None
    kept: list[pytest.Item] = []
    for item in items:
        _validate_metadata(item)
        severity = _resolve_severity(item)
        item.add_marker(getattr(pytest.mark, severity))

        if min_rank is not None and _SEVERITY_RANK[severity] < min_rank:
            continue
        kept.append(item)

    if min_rank is not None:
        deselected = [item for item in items if item not in kept]
        items[:] = kept
        if deselected:
            config.hook.pytest_deselected(items=deselected)


def _configure_allure(item: pytest.Item) -> None:
    """把名称、描述、四层标签、分类与严重等级写入 Allure 元数据."""
    title = item.name.removeprefix("test_").replace("_", " ").capitalize()
    module = getattr(item, "module", None)
    module_description = inspect.getdoc(module) or "ArchiveManagement 测试用例"

    epic = _label_value(item, "epic", _EPICS) or _UNCLASSIFIED
    feature = _label_value(item, "feature") or _UNCLASSIFIED
    story = _label_value(item, "story") or title
    layer = _label_value(item, "layer", _LAYERS) or _default_layer(item)

    _ALLURE_DYNAMIC.title(title)
    _ALLURE_DYNAMIC.description(f"{module_description}\n\n验证行为: {title}。")
    # epic/feature/story 驱动 "按产品级模块/功能模块/用户场景的稳定性分布" 控件.
    _ALLURE_DYNAMIC.epic(epic)
    _ALLURE_DYNAMIC.feature(feature)
    _ALLURE_DYNAMIC.story(story)
    # allure-pytest 没有 layer 装饰器, 只能写原始标签(Allure 3 "按层耗时" 直方图读它).
    _ALLURE_DYNAMIC.label("layer", layer)
    # suite 三层与 layer/epic/feature 对齐, 作为只认 suite 标签的控件兵底.
    _ALLURE_DYNAMIC.parent_suite(_LAYER_SUITES.get(layer, layer))
    _ALLURE_DYNAMIC.suite(epic)
    _ALLURE_DYNAMIC.sub_suite(feature)

    tags = sorted({marker.name for marker in item.iter_markers()} - _TAG_EXCLUDED)
    if tags:
        _ALLURE_DYNAMIC.tag(*tags)

    severity = _resolve_severity(item)
    if severity != "no_severity":
        _ALLURE_DYNAMIC.severity(_ALLURE_LEVELS[severity])


@pytest.fixture(autouse=True)
def _allure_metadata(request: pytest.FixtureRequest) -> None:
    """在每个测试开始后写入 Allure 元数据。"""
    _configure_allure(request.node)


class _RecordingHandler(logging.Handler):
    """把审计日志采样到内存列表, 供用例断言。"""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


@pytest.fixture
def audit_log() -> Iterator[list[str]]:
    """捕获 ``archive_management.audit`` 的日志行.

    用户操作只写日志文件与内存, 不落数据库, 因此用例通过日志行断言
    "操作是否被记录"; 夹具临时把审计日志器降到 DEBUG 并捕获全部行。
    """
    logger = logging.getLogger(AUDIT_LOGGER_NAME)
    handler = _RecordingHandler()
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    try:
        yield handler.messages
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate
