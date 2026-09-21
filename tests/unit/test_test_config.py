"""测试基础设施自身的配置校验.

用"配置即断言"的用例锁住几道容易被误删的门槛:

- 每个用例都有最长执行时间(``--timeout``), 卡住的用例不会让整套测试挂死;
- 严重等级过滤仍与 Allure 元数据挂钩(``--min-severity`` 由 conftest 提供);
- 每个测试模块都声明了 epic/feature/story/layer 四层 Allure 标签,
  保证报告里"按产品级模块/功能/场景的稳定性分布"与"按层耗时"不会出现空分类;
- 默认收集范围只含单元与集成测试, 性能/安全测试只在 CI 执行;
- 跨模块共享的测试辅助模块(``tests/helpers.py``、``tests/reporting.py``、``tests/ci_workflow.py``)
  可导入, 避免各模块重复维护同一批构造器(最后一个负责解析 CI 工作流文本, 报告与分片
  两组守卫都用它); mypy 也要能解析它们(见下一条)。
- ``pyproject.toml`` 的 ``mypy_path`` 包含 ``tests`` 目录: pre-commit 与编辑器会用
  "只检查改动文件"的方式词用 mypy, 此时 ``files`` 配置不生效, 找不到共享模块的
  导入会被静默当成 ``Any``(表现为 ``no-any-return`` 误报)。
- 合并报告里每条结果都看得出平台: 平台写进"参数 + os 标签 + env 标签", 其中 env 标签
  由仓库根的 ``allurerc.mjs`` 映射成 Allure 3 的"环境"维度(三平台的结果互为独立条目,
  用例详情页的环境分页里能逐个对照); 标题同时还原参数化转义。
"""

from __future__ import annotations

import ast
import os
import re
import tomllib
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import allure
import coverage
import pytest
from allure_pytest.utils import allure_name

import ci_workflow
import conftest
from archive_management.services.platforms import PLATFORM_LABELS

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("测试配置约束"),
    pytest.mark.layer("unit"),
]

# 仓库根目录: 报告配置(allurerc.mjs)与本文件所在的 tests/unit 差两级.
_REPO_ROOT = Path(__file__).resolve().parents[2]


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
    has_tests = any(entry.endswith("/tests") for entry in entries)
    hint = "mypy_path 缺少 tests: 单独检查测试文件时 import helpers 会变成 Any"
    assert has_tests, hint


def test_the_cli_runs_without_installing_the_project(
    pytestconfig: pytest.Config,
) -> None:
    """ "不作为包安装"与仓库根的 ``.env`` 是配套的: 少任何一个, CLI 都导入不了自身.

    实测(2026-09-18): 只有 ``[tool.uv] package = false`` 时, README/docs 里的
    ``uv run python -m archive_management ...`` 会以 ``No module named
    archive_management`` 结束; ``.env`` 里的 ``PYTHONPATH=src`` 由 ``uv run`` 自动
    加载, 命令才成立。改安装方式就必须同步改 ``.env`` 与文档里的说明。
    """
    root = Path(str(pytestconfig.rootpath))
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    package = config["tool"]["uv"]["package"]
    assert package is False, "安装方式变了: 请同步调整 .env 与文档里的 CLI 说明"

    lines = (root / ".env").read_text(encoding="utf-8").splitlines()
    entries = {
        key.strip(): value.strip()
        for key, _, value in (line.partition("=") for line in lines if "=" in line)
        if not key.strip().startswith("#")
    }
    paths = [item for item in entries.get("PYTHONPATH", "").split(os.pathsep) if item]
    hint = (
        ".env 必须提供 PYTHONPATH=src, 否则 uv run python -m archive_management 会失败"
    )
    assert "src" in paths, hint


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


def test_coverage_data_from_another_platform_can_be_combined(
    pytestconfig: pytest.Config, tmp_path: Path
) -> None:
    """各平台的覆盖率数据要能在报告作业(Ubuntu)上合并: 记相对路径, 分隔符自动归一.

    报告生成是纯文件操作, 所以两个平台的数据都在 Ubuntu 上合并(见 docs/testing.md 第 6 节),
    而 Windows 记下的名字是反斜杠形式。``relative_files = true`` 让两边的名字都相对仓库根,
    ``coverage combine`` 再把分隔符换成本机的那一种 —— 少了这个设置会变成"合并成功但一个
    文件都对不上"(覆盖率报告空掉, 而 Windows 上跑这条用例看不出来, 只有 Linux 才暴露)。
    """
    root = Path(str(pytestconfig.rootpath))
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    assert config["tool"]["coverage"]["run"]["relative_files"] is True, (
        "报告作业要在 Ubuntu 上合并 Windows 的分片数据: 绝对路径在那台机器上还原不出来"
    )

    # 造一份"在另一台机器上产出的"数据: Windows 记下的是反斜杠形式的相对路径。
    recorded = "\\".join(["src", "archive_management", "config.py"])
    source = tmp_path / ".coverage.other"
    data = coverage.CoverageData(basename=str(source))
    data.add_lines({recorded: {1: 1}})
    data.write()

    combined = tmp_path / ".coverage.combined"
    coverage.Coverage(data_file=str(combined)).combine(data_paths=[str(source)])
    reader = coverage.Coverage(data_file=str(combined))
    reader.load()
    measured = reader.get_data().measured_files()

    assert len(measured) == 1
    assert Path(measured.pop()).is_file(), (
        '合并后的路径要对上本机的真实文件(否则 coverage report 只会报"没有数据")'
    )


def test_ci_matrix_parser_accepts_both_layouts() -> None:
    """工作流文本解析要认两种排版: 行内 ``- { os: ..., shard: 0 }`` 与多行写法.

    守卫读的是工作流**文本**, 而编辑器会在两种排版之间重排(本仓库的 ``needs:`` 就被摊成过
    多行)。解析助手认不下另一种写法时守卫会误报 —— 那比没有守卫更糟, 所以这里两种都钉住。
    """
    inline = (
        "\n  demo:\n    strategy:\n      matrix:\n        include:\n"
        "          - { os: ubuntu-latest, platform: Linux, shard: 0, shards: 3 }\n"
        "          - { os: windows-latest, platform: Windows, shard: 0, shards: 2 }\n"
        "    steps:\n      - name: 无关的步骤\n"
    )
    block = (
        "\n  demo:\n    strategy:\n      matrix:\n        include:\n"
        "          - os: ubuntu-latest\n            platform: Linux\n"
        "            shard: 0\n            shards: 3\n"
        "          - os: windows-latest\n            platform: Windows\n"
        "            shard: 0\n            shards: 2\n"
        "    steps:\n      - name: 无关的步骤\n"
    )

    expected = [
        {"os": "ubuntu-latest", "platform": "Linux", "shard": "0", "shards": "3"},
        {"os": "windows-latest", "platform": "Windows", "shard": "0", "shards": "2"},
    ]
    assert ci_workflow.matrix_entries(inline, "demo") == expected
    assert ci_workflow.matrix_entries(block, "demo") == expected


def test_timeout_marker_can_override_default(pytestconfig: pytest.Config) -> None:
    """插件需支持按用例覆盖超时(慢用例可用 @pytest.mark.timeout 放宽)."""
    assert pytestconfig.pluginmanager.has_plugin("timeout")


# 作业里的命令 → 它需要的依赖组(见 pyproject 的 [dependency-groups] 注释)。
# 按"命令 ↔ 组"而不是手写一张作业清单: 手写的清单会在改动命令时过期, 而这里只要某个作业
# 开始跑 pytest / 调质量门禁脚本, 就会自动要求对应的组。
_CI_COMMAND_GROUPS = (
    (r"\buv run pytest\b", "test"),
    (r"\buv run coverage\b", "coverage"),
    (r"\buv run ruff\b", "quality"),
    (r"\buv run mypy\b", "quality"),
    (r"\buv run pyinstaller\b", "package"),
    # 质量门禁脚本的三组各跑什么: core/platform 是 ruff/mypy(质量组), analysis 是那 5 个工具。
    (r"create_allure_quality\.py --group (?:core|platform)", "quality"),
    (r"create_allure_quality\.py --group analysis", "analysis"),
)
# 本地提交钩子与常见本地命令要用的工具: 默认组装不下它们, 本地就跑不了。
_LOCAL_TOOLS = (
    "ruff",
    "mypy",
    "pre-commit",
    "deptry",
    "pytest",
    "coverage",
    "pyinstaller",
)


def _without_comments(text: str) -> str:
    """去掉整行注释(注释里会拿命令当例子说明, 不能算作"这个作业跑了它")."""
    return "\n".join(
        line for line in text.splitlines() if not line.strip().startswith("#")
    )


def _dev_groups() -> dict[str, list[str]]:
    """读出 pyproject 的开发依赖分组."""
    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        groups = tomllib.load(handle)["dependency-groups"]
    return cast("dict[str, list[str]]", groups)


def test_ci_installs_only_the_dependency_groups_each_job_needs() -> None:
    """CI 每个作业只装自己需要的那一组依赖, 而且每组都在 `uv sync` 里显式列出来.

    一趟 CI 有十几个作业实例, 每个都装全套开发依赖(bandit / pip-audit / pyinstaller ...)
    是最容易省掉的固定开销 —— 尤其冷缓存那一轮(锁文件一变, uv 缓存就得重建)。
    两条判据: ① 作业里出现的命令所需的那几组必须都被某次 `uv sync` 装上(缺了会以
    "Failed to spawn: xxx" 响亮地失败, 但仍值得在本地拦住); ② 同一作业里的每次 `uv sync`
    必须是同一套参数(否则修复步骤那一次会把刚装好的组又删掉)。
    """
    for workflow in (ci_workflow.WORKFLOW, ci_workflow.RELEASE_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        for name, body in ci_workflow.jobs(text).items():
            commands = _without_comments(body)
            synced = re.findall(r"uv sync[^\n]*", commands)

            assert synced, f"{workflow.name} 的 {name} 没有 uv sync"
            assert len(set(synced)) == 1, f"{name} 里每次 uv sync 必须一致: {synced}"
            command = synced[0]
            assert "--locked" in command, f"{name} 的 uv sync 没用 --locked"
            # 默认组是给本地开发用的: CI 里它会把整套依赖静默装回来(实测一次装回 51 个包)。
            assert "--no-default-groups" in command, f"{name} 的 uv sync 没关掉默认组"

            listed = set(re.findall(r"--group ([\w-]+)", command))
            hint = f"{name} 引用了不存在的依赖组: {sorted(listed)}"
            assert listed <= set(_dev_groups()), hint

            needed = {
                group
                for pattern, group in _CI_COMMAND_GROUPS
                if re.search(pattern, commands)
            }
            hint = (
                f"{name} 缺依赖组: {sorted(needed - listed)} (它跑了需要这些组的命令)"
            )
            assert needed <= listed, hint


def test_uv_run_does_not_silently_sync_the_default_groups() -> None:
    """CI 里必须关掉 `uv run` 的隐式 sync —— 否则上一条守卫的收益会被它悄悄吃掉.

    `uv run` 默认先 sync 一次, 而 sync 装的是**默认组**: 实测在"只装了测试组"的环境里跑一次
    `uv run pytest`, uv 又装回 51 个包(ruff / mypy / bandit / pip-audit / pyinstaller 全回来了)。
    所以两个工作流都在顶层设 `UV_NO_SYNC=1`, 环境只由每个作业自己那句 `uv sync` 决定。
    """
    for workflow in (ci_workflow.WORKFLOW, ci_workflow.RELEASE_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        head = text.split("\njobs:", 1)[0]

        hint = (
            f"{workflow.name} 顶层 env 缺少 UV_NO_SYNC: uv run 会按默认组装回全套依赖"
        )
        assert "UV_NO_SYNC" in head, hint


def test_local_sync_still_installs_every_group() -> None:
    """本地 `uv sync` 必须仍然是"全部装上": 分组只是给 CI 省带宽, 不能改变本地用法.

    所以 pyproject 的 `[tool.uv] default-groups` 要覆盖**所有**组 —— 否则本地 `uv sync`
    之后 `uv run ruff` / `uv run pytest` / 提交钩子会报 "Failed to spawn"。
    """
    with (_REPO_ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    groups = config["dependency-groups"]
    defaults = list(config["tool"]["uv"]["default-groups"])

    assert set(defaults) == set(groups), (
        f"默认组 {sorted(defaults)} 与声明的组 {sorted(groups)} 不一致"
    )
    declared = {
        name.split("[")[0].split(">")[0].split("=")[0].strip()
        for group in groups.values()
        for name in group
    }
    missing = [tool for tool in _LOCAL_TOOLS if tool not in declared]
    assert not missing, f"这些工具没被任何组声明: {missing}"


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

    def all_arguments(self, name: str) -> list[tuple[Any, ...]]:
        """返回所有 ``name`` 调用的参数(同名方法被调用多次时用, 例如多个标签)."""
        return [arguments for call, arguments in self.calls if call == name]


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
    """平台要写成参数 + os/env 标签, 并同步写套件名(便于按平台/环境查看).

    参数与套件名是"生成端没读到 allurerc.mjs"时的兼底(那时环境会退化成 default),
    env 标签则是平台成为 Allure 3 "环境"维度的入口; 两者缺一都会让三平台结果
    在合并报告里难以分辨。
    """
    recorder = _DynamicRecorder()
    monkeypatch.setattr(conftest, "_ALLURE_DYNAMIC", recorder)
    item = _ProbeItem("test_carries_platform", _probe_function)

    conftest._configure_allure(cast(pytest.Item, item))

    platform = conftest._current_platform_label()
    assert platform in {"Windows", "Linux", "macOS"}
    assert recorder.arguments("parameter") == ("平台", platform)
    labels = recorder.all_arguments("label")
    assert ("os", platform) in labels
    assert ("env", platform) in labels
    assert str(recorder.arguments("parent_suite")[0]).endswith(f"· {platform}")


def _allure_config_text() -> str:
    """读取仓库根的 Allure 报告配置(环境映射与历史设置都在这里)."""
    return (_REPO_ROOT / "allurerc.mjs").read_text(encoding="utf-8")


def _environment_block() -> str:
    """截出配置里的 ``environments`` 块(块内的键就是环境 id, 缩进用于区分层级)."""
    return _allure_config_text().split("environments: {", 1)[-1]


def test_report_config_maps_every_platform_to_an_environment() -> None:
    """``allurerc.mjs`` 必须为每个平台声明一个环境 matcher, 另加一个公共环境。

    环境不会只因为结果上有 ``env`` 标签就生效: Allure 3 在结果没有显式 environment 字段
    时会退到配置里的 matcher, 匹配不到就落到隐式的 default —— 那时三平台的结果会重新
    退化成"只能从参数/套件名里认平台"。这里锁住"代码能生成的每个平台展示名都在配置里
    有对应环境", 并顺便校验环境 id 合法(Allure 只接受 latin 字母/数字/下划线/连字符).

    额外允许**一个非平台环境** ``Common``: 质量检查与静态分析跟平台无关(CI 只跑一遍),
    结论项带 ``env=common`` 归到它而不是某个平台的环境; 覆盖平台专属代码分支的两条 mypy
    检查(``--platform win32`` / ``darwin``)则**在对应平台上执行**并带那个平台的 ``env``
    (见 ``scripts/create_allure_quality.py`` 的 ``PLATFORM_ENVIRONMENTS``)。多出别的环境名
    则判失败。

    断言只看 ``environments`` 块内部: 配置顶层也有 ``name``(报告标题), 用整份文本去
    匹配会把它当成环境名。
    """
    block = _environment_block()
    names = re.findall(r'^\s{6}name: "([^"]+)"', block, re.MULTILINE)
    hint = f"allurerc.mjs 环境名 {names} 与平台名 {sorted(PLATFORM_LABELS.values())} 不一致"

    assert set(PLATFORM_LABELS.values()) <= set(names), hint
    assert set(names) - set(PLATFORM_LABELS.values()) == {"Common"}, hint
    # 三个平台用显示名当标签值、公共环境用 id(common): matcher 的取值集合在这里锭住。
    matched = set(re.findall(r'value === "([^"]+)"', block))
    assert matched == {*PLATFORM_LABELS.values(), "common"}, (
        f"matcher 取值不一致: {matched}"
    )
    env_ids = re.findall(r"^\s{4}([A-Za-z0-9_-]+): \{", block, re.MULTILINE)
    assert env_ids == ["windows", "macos", "linux", "common"]
    assert all(re.fullmatch(r"[A-Za-z0-9_-]+", env_id) for env_id in env_ids)


def test_report_config_keeps_the_history_settings() -> None:
    """历史趋势设置也要留在仓库的 ``allurerc.mjs`` 里。

    CI 会在生成报告前把上一次成功运行的历史文件下载回 ``.allure/history.jsonl``;
    如果配置里没有 ``historyPath``/``appendHistory``, 下载会变成白做(报告里不再有趋势),
    而这两项以前是写死在 CI 的三处报告生成步骤里的。
    """
    config = _allure_config_text()

    assert 'historyPath: "./.allure/history.jsonl"' in config
    assert "appendHistory: true" in config
