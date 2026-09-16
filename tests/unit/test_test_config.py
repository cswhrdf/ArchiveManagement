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
- 合并报告里每条结果都看得出平台, 标题也没有残留的参数化转义: 三个平台的用例实际
  由不同机器跑出, 平台必须写进用例身份, 否则只会显示"同一个用例重试了多次"。
"""

from __future__ import annotations

import ast
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import allure
import pytest
from allure_pytest.utils import allure_name

import conftest

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("测试配置约束"),
    pytest.mark.layer("unit"),
]

# 与 pyproject.toml 的 addopts 保持一致: 单个用例最长 60 秒.
_EXPECTED_TIMEOUT_SECONDS = 60
# 四层 Allure 标签: 缺任何一层都会让报告对应控件少数据.
_REQUIRED_LABELS = ("layer", "epic", "feature", "story")
# 严重等级(闭集): 每个模块必须显式声明恰好一个, 分布不允许退化到单一等级.
_SEVERITY_MARKERS = ("blocker", "critical", "normal", "minor", "trivial")
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


def test_every_test_module_declares_exactly_one_severity(
    pytestconfig: pytest.Config,
) -> None:
    """每个模块恰好声明一个严重等级, 且五个等级都要有模块使用.

    等级既是报告里的"影响面"分级, 也是 pre-commit 选子集的依据(blocker+critical):
    模块不声明就会退回目录兜底, 全挤在一个等级上则说明没有按影响面分过级。
    """
    root = Path(str(pytestconfig.rootpath)) / "tests"
    modules = sorted(root.rglob("test_*.py"))
    assert modules, "未找到任何测试模块"
    declared = {
        module.relative_to(root).as_posix(): sorted(
            set(_SEVERITY_MARKERS) & _declared_markers(module)
        )
        for module in modules
    }
    wrong = {name: found for name, found in declared.items() if len(found) != 1}
    assert not wrong, f"每个模块必须声明恰好一个严重等级: {wrong}"
    used = {level for found in declared.values() for level in found}
    assert not set(_SEVERITY_MARKERS) - used, (
        f"以下严重等级没有任何模块使用, 报告分布会失真: "
        f"{sorted(set(_SEVERITY_MARKERS) - used)}"
    )


# --- 汇总报告: 标题可读性与平台可区分 ------------------------------------------

# 参数化用例的中文 id 会被 pytest 转义成 ``\uXXXX``, 报告标题要用可读文字.
_ESCAPED_NAME = "test_rejects_hostile_field[\\u5e03\\u5c14\\u5b57\\u6bb5-payload1]"
_EXPECTED_TITLE = "Rejects hostile field[布尔字段-payload1]"


class _DynamicRecorder:
    """记录 ``allure.dynamic`` 调用的替身(真实实现要跑完整个会话才能观察结果)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    def __getattr__(self, name: str) -> Callable[..., None]:
        def record(*args: Any) -> None:
            self.calls.append((name, args))

        return record

    def arguments(self, name: str) -> tuple[Any, ...]:
        """返回最后一次 ``name`` 调用的参数."""
        matches = [arguments for call, arguments in self.calls if call == name]
        assert matches, f"未调用 allure.dynamic.{name}: {self.calls}"
        return matches[-1]


class _ProbeItem:
    """只实现 ``_configure_allure`` 读取的那几个属性的最小替身."""

    def __init__(self, name: str, function: object | None = None) -> None:
        self.name = name
        self.obj = function
        self.function = function
        self.funcargs: dict[str, object] = {}
        self.module = None
        self.fspath = Path("tests/unit/test_probe.py")

    def get_closest_marker(self, name: str) -> None:
        return None

    def iter_markers(self) -> list[object]:
        return []


def _probe_function() -> None:
    """被写入 Allure 标题的"用例函数"(占位, 只需是个可写属性的对象)."""


def test_readable_restores_unicode_escapes() -> None:
    """参数化 id 里的转义要还原成原字符, 其余文本保持不变."""
    assert conftest._readable("field[\\u5e03\\u5c14-1]") == "field[布尔-1]"
    assert conftest._readable("field[\\U0001F600]") == "field[😀]"
    # 反斜杠在 id 里是双写的, 还原成原始路径才看得懂.
    assert conftest._readable("C:\\\\Users\\\\ycswh") == "C:\\Users\\ycswh"
    # 已经是可读文字时不能解码: unicode_escape 会把 UTF-8 字节按 latin-1 解读成乱码.
    assert conftest._readable("主题取值非法") == "主题取值非法"
    assert conftest._readable("plain ascii") == "plain ascii"
    # 截断的转义不能抛错.
    assert conftest._readable("field[\\u12]") == "field[\\u12]"


def test_title_is_readable_after_allure_pytest_renaming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """标题要写在 allure-pytest 会读取的位置, 否则报告里会变成一串转义编码.

    allure-pytest 在 fixture 跑完后用 ``allure_name()`` 重新命名用例, 因此只调
    ``allure.dynamic.title`` 会被 ``item.name`` 覆盖 —— 这条用例走的就是它取名时
    真实的取值路径.
    """
    monkeypatch.setattr(conftest, "_ALLURE_DYNAMIC", _DynamicRecorder())
    item = _ProbeItem(_ESCAPED_NAME, _probe_function)

    conftest._configure_allure(cast(pytest.Item, item))

    assert conftest._title_of(cast(pytest.Item, item)) == _EXPECTED_TITLE
    assert allure_name(cast(pytest.Item, item), {}, None) == _EXPECTED_TITLE


def test_title_follows_each_parametrized_case(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """参数化用例共用同一个函数对象: 每个用例都必须刷新标题.

    否则后面的用例会沿用第一个用例的标题(报告里十几个参数只显示同一个名字),
    以及花括号标题被当成格式化模板直接报错.
    """
    monkeypatch.setattr(conftest, "_ALLURE_DYNAMIC", _DynamicRecorder())

    def probe() -> None:
        """探针函数(多个参数共用这个对象)."""

    first = _ProbeItem("test_probe[case\\u4e00]", probe)
    second = _ProbeItem("test_probe[rejects {invalid" + "]", probe)

    conftest._configure_allure(cast(pytest.Item, first))
    assert allure_name(cast(pytest.Item, first), {}, None) == "Probe[case一]"

    conftest._configure_allure(cast(pytest.Item, second))
    assert allure_name(cast(pytest.Item, second), {}, None) == "Probe[rejects {invalid]"


def test_explicit_allure_title_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    """用例用 ``@allure.title`` 指定标题时不覆盖(与 allure-pytest 行为一致)."""
    monkeypatch.setattr(conftest, "_ALLURE_DYNAMIC", _DynamicRecorder())

    def probe() -> None:
        """探针函数."""

    setattr(probe, "__allure_display_name__", "自定义标题")  # noqa: B010
    item = _ProbeItem("test_probe", probe)

    conftest._configure_allure(cast(pytest.Item, item))

    assert allure_name(cast(pytest.Item, item), {}, None) == "自定义标题"


def test_allure_result_carries_the_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """平台要写成参数(进用例身份), 并同步写标签与套件名(便于按平台查看).

    平台不是参数时, 三个平台跑出的同名结果在合并报告里只会显示成
    "同一个用例重试了多次"; 写成参数才会被当成各自独立的用例.
    """
    recorder = _DynamicRecorder()
    monkeypatch.setattr(conftest, "_ALLURE_DYNAMIC", recorder)
    item = _ProbeItem("test_carries_platform", _probe_function)

    conftest._configure_allure(cast(pytest.Item, item))

    platform = conftest._current_platform_label()
    assert platform in {"Windows", "Linux", "macOS"}
    assert recorder.arguments("parameter") == ("平台", platform)
    assert recorder.arguments("label") == ("os", platform)
    assert str(recorder.arguments("parent_suite")[0]).endswith(f"· {platform}")
