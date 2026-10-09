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
import re
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import allure
import coverage
import pytest
from allure_pytest.utils import allure_name

import ci_workflow
import conftest
import tk_guard
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
# 不碰 uv 的作业(它们只用 runner 自带的工具与官方 action: 下产物、路径过滤、上传)。
# 2026-10-06: 报告不再发布 Pages, 但 PR 侧新增了两个轻量门禁作业 —— `changes` 只跑
# `dorny/paths-filter`、`required-check` 只 `echo` 一行, 都不需要 Python 环境, 所以
# 按这条登记; 列表不会自己变长: 守卫会反向检查"登记了的作业真的一条 uv 命令都没有"。
_UV_FREE_JOBS: set[str] = {"changes", "required-check"}
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


def test_the_project_is_installed_as_an_editable_package(
    pytestconfig: pytest.Config,
) -> None:
    """ "装成可编辑包"与 CI 里的 `--no-install-project` 是配套的: 少任何一个口径就不一致.

    `[tool.uv] package = true` 让 `uv sync` 往 venv 里放一个指向 `src` 的 `.pth`(源码不进
    site-packages), 于是 CLI 在任意目录都能跑, 不再需要 `PYTHONPATH`/仓库根的 `.env`;
    CI 反过来: 用例靠 pytest 的 `pythonpath` 导入源码, 每处 `uv sync` 都带
    `--no-install-project`, 只有质量作业装一次并跑 CLI 冒烟(见 .github/workflows/ci.yml)。
    """
    root = Path(str(pytestconfig.rootpath))
    with (root / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    package = config["tool"]["uv"]["package"]
    hint = "安装方式变了: 请同步调整 CI 的 --no-install-project 与文档里的 CLI 说明"
    assert package is True, hint

    lock = (root / "uv.lock").read_text(encoding="utf-8")
    hint = "锁文件里根项目不是可编辑安装: 改完 [tool.uv] 要重新 uv lock"
    assert 'source = { editable = "." }' in lock, hint

    hint = "仓库根的 .env 是旧方案(PYTHONPATH=src)的残留: 可编辑安装后不再需要"
    assert not (root / ".env").exists(), hint


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
    # 视觉回归要 imagehash + scikit-image(那一组单独拆出来: 它带整套 numpy/scipy/networkx,
    # 混进质量组等于让每个分片实例都下一遍)。
    (r"create_allure_visual\.py", "visual"),
    # 视觉回归还要 **test** 组: 它复用 ``tests/crash_capture.py`` 的抓图原语, 而那个模块
    # ``import allure``(附件 API)。少装它时环境看起来很正常, 直到运行的一瞬间报
    # "No module named 'allure'"(2026-10-02 实测)。
    (r"create_allure_visual\.py", "test"),
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


def _sync_by_environment(job_body: str) -> dict[str, list[str]]:
    """把作业里的 ``uv sync`` 按"这一步装到哪个环境"归类.返回 ``环境名 -> 命令行列表``.

    同一个作业可以故意装有**两份环境**: 视觉回归那一步要**换一份 Tk**(uv 托管的那份 Python
    自带的 Tcl/Tk 在 X11 上只走核心位图字体, 汉字一个都画不出来), 于是它用
    ``UV_PROJECT_ENVIRONMENT`` 指到另一个目录、只装它需要的那几组。

    "每次 uv sync 必须一致"这条规矩守的是"别在**同一个**环境里来回换组"(后一次会把前一次
    刚装好的组删掉), 所以先按环境分组再比。
    """
    by_environment: dict[str, list[str]] = {}
    for step in re.split(r"\n\s*- (?:name|uses|run):", job_body):
        lines = re.findall(r"uv sync[^\n]*", _without_comments(step))
        if not lines:
            continue
        target = re.search(r"UV_PROJECT_ENVIRONMENT:\s*(\S+)", step)
        key = target.group(1) if target else "(作业共用)"
        by_environment.setdefault(key, []).extend(lines)
    return by_environment


@dataclass
class _EnvironmentPlan:
    """作业里被 ``uv sync`` 装出来的一个环境(作业共用那个 + 任何 ``UV_PROJECT_ENVIRONMENT``)."""

    syncs: list[str] = field(default_factory=list)
    """装到这个环境里的 ``uv sync`` 命令行(已去注释)."""

    steps: list[str] = field(default_factory=list)
    """在这个环境里跑的步骤正文(用来算"它需要哪些组")."""


def _environments(job_body: str) -> dict[str, _EnvironmentPlan]:
    """按"这一步在哪个环境里跑"归类作业正文.

    同一个作业可以故意装有**两份环境**: 视觉回归那一步要**换一份 Tk**(uv 托管的那份 Python
    自带的 Tcl/Tk 在 X11 上只走核心位图字体, 汉字一个都画不出来), 于是它用
    ``UV_PROJECT_ENVIRONMENT`` 指到另一个目录并单独 sync 一次。

    分开归类之后两条规矩才能各自落到实处: ① "同一个环境里的多次 sync 必须一致"(后一次会把
    前一次装好的组删掉); ② "每个环境都要装齐**装在里面那些步骤**需要的组" —— ② 不按环境分
    的话, 独立环境会因为作业别处装了别的组而偷幸过关(2026-10-02 就是这么漏掉 allure 的)。
    """
    plans: dict[str, _EnvironmentPlan] = {}
    for step in re.split(r"\n\s*- (?:name|uses|run):", job_body):
        target = re.search(r"UV_PROJECT_ENVIRONMENT:\s*(\S+)", step)
        plan = plans.setdefault(
            target.group(1) if target else "(作业共用)", _EnvironmentPlan()
        )
        plan.steps.append(step)
        plan.syncs.extend(re.findall(r"uv sync[^\n]*", _without_comments(step)))
    return plans


def test_ci_installs_only_the_dependency_groups_each_job_needs() -> None:
    """CI 每个作业只装自己需要的那一组依赖, 而且每组都在 `uv sync` 里显式列出来.

    一趟 CI 有十几个作业实例, 每个都装全套开发依赖(bandit / pip-audit / pyinstaller ...)
    是最容易省掉的固定开销 —— 尤其冷缓存那一轮(锁文件一变, uv 缓存就得重建)。
    两条判据**按环境**判(同一作业可以有两份环境, 见 :func:`_environments`): ① 装在里面那些
    步骤需要的组必须都在那次 `uv sync` 里列出来; ② 同一个环境里的每次 `uv sync` 必须是同一套
    参数(否则修复步骤那一次会把刚装好的组又删掉)。

    **不碰 uv 的作业**在 :data:`_UV_FREE_JOBS` 里登记(清单现在是空的: 原来那个只做下载/解压/发布的
    Pages 作业已与汇总合并); 登记项会被反向自查"真的一条 uv 命令都没有", 免得它变成绕过分组检查的后门。
    """
    for workflow in (ci_workflow.WORKFLOW, ci_workflow.RELEASE_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        for name, body in ci_workflow.jobs(text).items():
            commands = _without_comments(body)
            environments = _environments(body)
            synced = [line for plan in environments.values() for line in plan.syncs]

            if name in _UV_FREE_JOBS:
                hint = f"{name} 登记为不用 uv, 却出现了 uv 命令: 要么让它 sync, 要么别用 uv"
                assert "uv " not in commands, hint
                continue

            assert synced, f"{workflow.name} 的 {name} 没有 uv sync"
            for target, plan in environments.items():
                hint = (
                    f"{name} 的环境 {target} 里有步骤, 但这个环境一次 uv sync 都没有:"
                    f" {plan.steps[0][:120]}"
                )
                assert plan.syncs, hint
                assert len(set(plan.syncs)) == 1, (
                    f"{name} 在同一个环境({target})里的每次 uv sync 必须一致"
                    f"(后一次会把前一次装好的组删掉): {plan.syncs}"
                )
                # 这一条**按环境**判: 独立环境不会因为作业别处装了别的组就偷幸过关。
                listed = set(re.findall(r"--group ([\w-]+)", plan.syncs[0]))
                needed = {
                    group
                    for pattern, group in _CI_COMMAND_GROUPS
                    if re.search(pattern, "\n".join(plan.steps))
                }
                hint = (
                    f"{name} 的环境 {target} 缺依赖组: {sorted(needed - listed)}"
                    f"(它跑了需要这些组的命令)"
                )
                assert needed <= listed, hint
                hint = f"{name} 的环境 {target} 引用了不存在的依赖组: {sorted(listed)}"
                assert listed <= set(_dev_groups()), hint

            for line in synced:
                assert "--locked" in line, f"{name} 的 uv sync 没用 --locked: {line}"
                # 默认组是给本地开发用的: CI 里它会把整套依赖静默装回来(实测一次装回 51 个包)。
                hint = f"{name} 的 uv sync 没关掉默认组: {line}"
                assert "--no-default-groups" in line, hint


def test_ci_only_installs_the_project_where_the_cli_smoke_needs_it() -> None:
    """CI 不重复安装项目: 每处 `uv sync` 都带 `--no-install-project`, 只有跑 CLI 的例外.

    项目装成可编辑包(见 pyproject 的 [tool.uv])是给本地/任意目录跑 CLI 用的; CI 的用例靠
    pytest 的 `pythonpath` 导入源码、静态检查靠 `mypy_path`, 都不需要装它 —— 不带
    `--no-install-project` 时每个作业都要多下一次构建后端(hatchling)并构建一遍, 十几个实例
    的重复开销。反过来也不能全都不装: 至少要有一个作业真的装上并跑一次 CLI, 否则“可编辑
    安装可用”就没人验证了。
    """
    installed_somewhere = False
    for workflow in (ci_workflow.WORKFLOW, ci_workflow.RELEASE_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        for name, body in ci_workflow.jobs(text).items():
            commands = _without_comments(body)
            synced = re.findall(r"uv sync[^\n]*", commands)
            if "python -m archive_management" in commands:
                installed_somewhere = True
                hint = f"{name} 要跑 CLI, 就不能跳过安装项目: {synced}"
                assert all("--no-install-project" not in line for line in synced), hint
            else:
                hint = f"{name} 不跑 CLI, 每处 uv sync 都该带 --no-install-project: {synced}"
                assert all("--no-install-project" in line for line in synced), hint
    hint = "没有作业真的装上项目并跑一次 CLI: 可编辑安装就没人验证了"
    assert installed_somewhere, hint


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


def test_every_uv_job_installs_uv_and_keeps_the_cache_inputs_on_it() -> None:
    """用了 uv 的作业必须先装 uv; 依赖缓存的开关只能挂在 setup-uv 那一步上.

    2026-10-10 实测: 汇总作业里 `enable-cache` / `save-cache` 被挂到了 ``actions/checkout``
    下面, 而它的 `Install uv` 步骤整个不见了。这两种错都不会让 CI 红:
      * GitHub 对**未知输入**只告警不报错(告警里列出的是 checkout 认得的那些键), 于是缓存
        参数静默失效 —— "缓存热时省不了几秒"这条本来就不指望它, 但"配了个没人读的开关"更糟;
      * 少了 setup-uv 时, 后面那句 `uv python install` 才会以 "command not found" 挂掉,
        而那已经是几十行之后的事了。
    所以这里把两条不变式都钉住: ① 作业正文里出现 uv 命令就必须有 `astral-sh/setup-uv`;
    ② `enable-cache` / `save-cache` 只许出现在 setup-uv 那一步里。
    """
    for workflow in (ci_workflow.WORKFLOW, ci_workflow.RELEASE_WORKFLOW):
        text = workflow.read_text(encoding="utf-8")
        for name, body in ci_workflow.jobs(text).items():
            commands = _without_comments(body)
            if re.search(r"\buv (?:run|sync|python|pip)\b", commands):
                hint = f"{name} 里用了 uv 命令, 却没有 astral-sh/setup-uv 这一步"
                assert "astral-sh/setup-uv" in body, hint
            for step in re.split(r"\n\s*- (?:name|uses|run):", body):
                step_text = _without_comments(step)
                if re.search(r"(?:enable|save)-cache:", step_text):
                    hint = (
                        f"{name} 把依赖缓存的开关挂在非 setup-uv 的步骤上了"
                        f"(GitHub 只告警不报错, 缓存其实没生效): {step_text.strip()[:120]}"
                    )
                    assert "astral-sh/setup-uv" in step_text, hint


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


def _allowed_environment_ids() -> list[str]:
    """读出配置里的环境 id 白名单(启动期校验用)."""
    matched = re.search(r"allowedEnvironments:\s*\[([^\]]*)\]", _allure_config_text())
    assert matched is not None, "allurerc.mjs 要有 allowedEnvironments"
    return re.findall(r'"([^"]+)"', matched.group(1))


def _category_rules() -> str:
    """截出配置里的 ``categories`` 块(三条分类规则都在这段里).

    要在 categories 这一层自己的收尾花括号处收住: 后面的 ``qualityGate`` 里也有 ``id:``
    (``tests-on-every-platform``), 不收住就会把它当成分类型规则。
    """
    block = _allure_config_text().split("categories: {", 1)[1]
    return block.split("\n  },\n", 1)[0]


def test_report_config_declares_every_environment_id_as_allowed() -> None:
    """``allowedEnvironments`` 必须恰好等于 ``environments`` 声明的键集合。

    实测(Allure 3.18.0): 少列一个 id 时 ``allure generate`` 以 Internal Error 退出, 原文是
    ``config.environments: environment id "common" is not listed in allowedEnvironments``。
    于是"加平台漏改一处"从**静默少一个环境**变成**报告生成不出来、且点名是哪个 id**。
    这条清单也要跟着 CI 的矩阵与汇总作业的 ``--expect-platforms`` 一起改。
    """
    ids = re.findall(r"^\s{4}([A-Za-z0-9_-]+): \{", _environment_block(), re.MULTILINE)
    allowed = _allowed_environment_ids()

    assert allowed == ids, f"允许清单 {allowed} 与环境 id {ids} 不一致"
    assert allowed == ["windows", "macos", "linux", "common"], (
        "环境清单变了要同步 CI 的三个矩阵与汇总作业的 --expect-platforms"
    )


def test_report_config_categorises_the_known_environment_problems() -> None:
    """失败归类要盖住仓库记录过的环境问题, 且只登记这三条。

    为什么需要这条守卫: 分类规则靠**错误文本**匹配, 而 Tk 症状的权威清单在
    ``tests/tk_guard.KNOWN_TK_SKIP_MARKERS``。新增一种症状(或改了拼写)却忘了同步分类规则时,\n    那条失败会掉进默认分类 —— 读报告的人会以为是用例本身坏了。这里把两处钉在一起。
    """
    rules = _category_rules()
    ids = re.findall(r'^\s{8}id: "([^"]+)"', rules, re.MULTILINE)

    assert set(ids) == {
        "env-tk-library",
        "env-transient-database",
        "gate-quality-check",
    }, f"分类规则变了要同步 docs/testing.md 与本用例: {ids}"
    assert len(ids) == len(set(ids)), "每条规则要有唯一 id(同名会被合并)"
    # Tk 那条用数组匹配器(message / trace 任一命中), 两条正则都要盖住全部已知症状。
    # 顺手把 JS 的 flags 也读出来: 规则必须大小写不敏感, 否则 `tcl_findLibrary`
    # 这种驼峰拼法会漏(而 tests/tk_guard 的白名单正是按不敏感比对的)。
    found = re.findall(
        r":\s*/([^/\n]+)/([a-z]*),", rules.split("env-transient-database", 1)[0]
    )
    assert len(found) == 2, f"Tk 规则要有 message 与 trace 两条正则: {found}"
    for pattern, flags in found:
        assert "i" in flags, f"Tk 规则的正则要带 i(大小写不敏感): /{pattern}/{flags}"
        compiled = re.compile(pattern, re.IGNORECASE)
        missing = [m for m in tk_guard.KNOWN_TK_SKIP_MARKERS if not compiled.search(m)]
        assert not missing, f"这些已知症状没进分类规则 /{pattern}/: {missing}"
