"""测试基础设施自身的配置校验.

用"配置即断言"的用例锁住几道容易被误删的门槛:

- 每个用例都有最长执行时间(``--timeout``), 卡住的用例不会让整套测试挂死;
- 严重等级过滤仍与 Allure 元数据挂钩(``--min-severity`` 由 conftest 提供);
- 每个测试模块都声明了 epic/feature/story/layer 四层 Allure 标签,
  保证报告里"按产品级模块/功能/场景的稳定性分布"与"按层耗时"不会出现空分类;
- 默认收集范围只含单元与集成测试, 性能/安全测试只在 CI 执行;
- 跨模块共享的测试辅助模块(``tests/helpers.py``、``tests/reporting.py``)可导入,
  避免各模块重复维护同一批构造器; mypy 也要能解析它们(见下一条)。
- ``pyproject.toml`` 的 ``mypy_path`` 包含 ``tests`` 目录: pre-commit 与编辑器会用
  "只检查改动文件"的方式词用 mypy, 此时 ``files`` 配置不生效, 找不到共享模块的
  导入会被静默当成 ``Any``(表现为 ``no-any-return`` 误报)。
"""

from __future__ import annotations

import ast
import tomllib
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
# 默认只跑单元与集成测试: 性能/安全测试只在 CI 执行.
_DEFAULT_SUITES = ("tests/unit", "tests/integration")
# 只在 CI 执行的测试类别: 目录名与 -m 标记名一致.
_CI_ONLY_SUITES = ("performance", "security")
# 跨模块共享的测试辅助模块.
_SHARED_MODULES = ("helpers.py", "reporting.py")


def test_default_run_excludes_ci_only_suites(pytestconfig: pytest.Config) -> None:
    """默认收集范围只含单元与集成测试, 性能/安全测试需要显式指定."""
    testpaths = [
        str(item).replace("\\", "/") for item in pytestconfig.getini("testpaths")
    ]
    assert testpaths == list(_DEFAULT_SUITES)


def test_shared_test_helpers_are_importable(pytestconfig: pytest.Config) -> None:
    """共享辅助模块必须可导入: 否则每个模块只能各写一份构造器."""
    root = Path(str(pytestconfig.rootpath))
    for name in _SHARED_MODULES:
        assert (root / "tests" / name).is_file(), f"缺少共享测试模块 tests/{name}"
    # getini 会把相对条目解析为绝对路径, 因此只比较目录名.
    pythonpath = [Path(str(item)).name for item in pytestconfig.getini("pythonpath")]
    assert "tests" in pythonpath, "pythonpath 需包含 tests 才能 import helpers"

    import helpers  # 局部导入: 只用它验证运行时可见性

    assert helpers.utc_moment(3, 21).hour == 21


def test_mypy_path_covers_the_shared_test_modules(pytestconfig: pytest.Config) -> None:
    """mypy 必须能解析 tests 目录下的共享模块, 单文件检查也不能退化成 Any."""
    root = Path(str(pytestconfig.rootpath))
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    mypy_path = str(config["tool"]["mypy"]["mypy_path"])
    entries = [entry.replace("\\", "/").rstrip("/") for entry in mypy_path.split(":")]

    assert any(entry.endswith("/src") for entry in entries), "mypy_path 缺少 src"
    assert any(
        entry.endswith("/tests") for entry in entries
    ), "mypy_path 缺少 tests: 单独检查测试文件时 import helpers 会变成 Any"


def test_ci_only_suites_exist_and_are_marked(pytestconfig: pytest.Config) -> None:
    """性能/安全测试目录存在且已登记标记, CI 才能按类别单独执行."""
    root = Path(str(pytestconfig.rootpath)) / "tests"
    markers = "\n".join(pytestconfig.getini("markers"))
    for suite in _CI_ONLY_SUITES:
        modules = sorted((root / suite).rglob("test_*.py"))
        assert modules, f"tests/{suite} 下没有测试模块"
        assert f"{suite}:" in markers, f"pyproject 未登记 {suite} 标记"
        declared = {
            marker for module in modules for marker in _declared_markers(module)
        }
        assert suite in declared, f"tests/{suite} 的模块缺少 {suite} 标记"


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
