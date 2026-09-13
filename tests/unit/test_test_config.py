"""测试基础设施自身的配置校验.

用"配置即断言"的用例锁住几道容易被误删的门槛:

- 每个用例都有最长执行时间(``--timeout``), 卡住的用例不会让整套测试挂死;
- 严重等级过滤仍与 Allure 元数据挂钩(``--min-severity`` 由 conftest 提供);
- 每个测试模块都声明了 epic/feature/story/layer 四层 Allure 标签,
  保证报告里"按产品级模块/功能/场景的稳定性分布"与"按层耗时"不会出现空分类。
"""

from __future__ import annotations

import ast
from pathlib import Path

import allure
import pytest

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("测试配置约束"),
    pytest.mark.layer("unit"),
]

# 与 pyproject.toml 的 addopts 保持一致: 单个用例最长 60 秒.
_EXPECTED_TIMEOUT_SECONDS = 60
# 四层 Allure 标签: 缺任何一层都会让报告对应控件少数据.
_REQUIRED_LABELS = ("layer", "epic", "feature", "story")


def test_every_test_has_a_maximum_runtime(pytestconfig: pytest.Config) -> None:
    """全局超时必须开启, 否则卡住的用例会一直等下去."""
    option = pytestconfig.getoption("timeout")
    assert option is not None, "缺少 --timeout: 测试可能永久挂起"
    assert float(option) == _EXPECTED_TIMEOUT_SECONDS
    assert pytestconfig.getoption("timeout_method") == "thread"


def test_timeout_marker_can_override_default(pytestconfig: pytest.Config) -> None:
    """插件需支持按用例覆盖超时(慢用例可用 @pytest.mark.timeout 放宽)."""
    assert pytestconfig.pluginmanager.has_plugin("timeout")


def _declared_markers(path: Path) -> set[str]:
    """不导入被测模块, 直接解析文件里模块级 pytestmark 的标记名."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(target, ast.Name) and target.id == "pytestmark"
            for target in node.targets
        ):
            continue
        for child in ast.walk(node.value):
            if (
                isinstance(child, ast.Attribute)
                and isinstance(child.value, ast.Attribute)
                and isinstance(child.value.value, ast.Name)
                and child.value.value.id == "pytest"
                and child.value.attr == "mark"
            ):
                names.add(child.attr)
    return names


def test_every_test_module_declares_allure_labels(
    pytestconfig: pytest.Config,
) -> None:
    """每个测试模块都要声明四层 Allure 标签, 否则报告会出现空分类."""
    root = Path(str(pytestconfig.rootpath)) / "tests"
    modules = sorted(root.rglob("test_*.py"))
    assert modules, "未找到任何测试模块"
    incomplete = {
        module.relative_to(root).as_posix(): sorted(
            set(_REQUIRED_LABELS) - _declared_markers(module)
        )
        for module in modules
        if set(_REQUIRED_LABELS) - _declared_markers(module)
    }
    assert not incomplete, f"缺少 Allure 标签的测试模块: {incomplete}"


def test_layer_label_is_written_as_raw_label() -> None:
    """allure-pytest 没有 layer 装饰器: 只能通过原始标签写 layer(易被误改)."""
    assert hasattr(allure.dynamic, "label")
    assert not hasattr(allure, "layer")
