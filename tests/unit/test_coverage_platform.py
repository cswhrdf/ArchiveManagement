"""平台专属标记(``# platform: ...``)与生成覆盖率配置的守卫用例.

为什么需要: 这套机制决定了"每个平台的覆盖率到底算了哪些行", 而它**改的是数字本身** ——
写错一处, 某个平台的行就永远不再被统计, 却没有任何一步会变红。规则与 ``pragma`` 那套一脉
相承(写法规范、必须写明原因、不许落在 ``def``/``class`` 行上), 另外多两条:

- 排除只针对**别的平台**: 在自己平台上该行照常统计(否则"平台专属"就退化成整块不测);
- 生成的配置必须**完整派生**自 ``pyproject.toml``: ``exclude_also`` 这类项在 coverage 里是
  替换而不是合并, 漏掉 ``branch``/``source``/``fail_under`` 会让数字悄悄变成另一回事(实测:
  手工写一份只有 ``exclude_also`` 的 rc, ``branch`` 会变回 ``false``)。
"""

from __future__ import annotations

import configparser
import importlib.util
import os
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率豁免"),
    pytest.mark.story("平台专属代码只在别的平台排除"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFTEST = _REPO_ROOT / "tests" / "conftest.py"


def _load_platform_module() -> Any:
    """按路径加载生成脚本(``scripts`` 不在 pythonpath 里, 与其它脚本守卫同一个做法)."""
    spec = importlib.util.spec_from_file_location(
        "coverage_platform", _REPO_ROOT / "scripts" / "coverage_platform.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


coverage_platform = _load_platform_module()


def test_the_documented_spelling_parses() -> None:
    """``# platform: windows - 原因``(可列多个平台)要能解析出平台与原因."""
    assert coverage_platform.parse_marker(
        'if sys.platform == "win32":  # platform: windows - 只有 Windows 有注册表'
    ) == (("windows",), "只有 Windows 有注册表")
    assert coverage_platform.parse_marker(
        "try:  # platform: linux macos - 只有 POSIX 有 fcntl"
    ) == (("linux", "macos"), "只有 POSIX 有 fcntl")


def test_plain_field_comments_are_not_mistaken_for_markers() -> None:
    """提及 platform 的普通注释不算标记(实测撞过一次: ``ExcludedProgram.platform`` 那行)."""
    line = '    platform: str = ""  # 空 = 不限来源平台'
    assert coverage_platform.marker_problems(line) == []
    assert coverage_platform.parse_marker(line) is None
    assert [item for item in coverage_platform.marker_lines() if item[2] == line] == []


def test_bad_markers_are_reported() -> None:
    """平台名不认识、缺原因、原因太短或占位词都要被点名(规则与 pragma 守卫同源)."""
    assert (
        coverage_platform.marker_problems("# platform: windows - 只有它有注册表") == []
    )
    assert coverage_platform.marker_problems("# platform: beos - 只有它有注册表")
    assert coverage_platform.marker_problems("# platform: windows - 很短")
    assert coverage_platform.marker_problems("# platform: windows - todo")
    assert coverage_platform.marker_problems("# platform windows 只有 Windows")


def test_exclusions_cover_only_the_other_platforms() -> None:
    """排除正则只该命中"打给别的平台"的标记(``search`` 口径, 所以不加锚点)."""
    on_windows = coverage_platform.exclusion_pattern("windows")
    assert "linux" in on_windows
    assert "macos" in on_windows
    assert "windows" not in on_windows

    linux_marker = "# platform: linux - 只有 POSIX 有 fcntl"
    windows_marker = "# platform: windows - 只有 Windows 有注册表"
    assert re_search(on_windows, linux_marker)
    assert not re_search(on_windows, windows_marker)

    on_linux = coverage_platform.exclusion_pattern("linux")
    assert re_search(on_linux, windows_marker)
    assert not re_search(on_linux, linux_marker)


def test_the_generated_config_survives_a_round_trip_through_configparser(
    tmp_path: Path,
) -> None:
    """生成的排除规则必须能被 configparser 读回来, 而且真的命中标记行.

    实测踩过: 规则以 ``#`` 开头时 configparser 会把它当**注释整行丢掉** —— 文件里看得见,
    实际一条都不生效(平台的标记行照旧进统计)。所以这里验的是"读回来还在且能命中",
    而不是"写出来了"。
    """
    target = coverage_platform.write_platform_config(
        tmp_path / "rc", platform="windows"
    )

    parsed = configparser.ConfigParser()
    parsed.read(target, encoding="utf-8")
    rules = [rule.strip() for rule in parsed["report"]["exclude_also"].splitlines()]

    assert coverage_platform.exclusion_pattern("windows").strip() in rules
    assert "if TYPE_CHECKING:" in rules, "基线里的豁免也要留下"
    marker = "if x:  # platform: linux - 只有 POSIX 有 fcntl"
    assert any(re_search(rule, marker) for rule in rules if rule)


def re_search(pattern: str, text: str) -> bool:
    """``re.search`` 的小包装(保持断言读起来是"命中/不命中")."""
    return re.search(pattern, text) is not None


def test_the_platform_config_is_only_effective_through_the_coverage_cli() -> None:
    """平台判定必须走 `coverage` 命令行 —— 这是这套排除能生效的唯一一条路.

    实测(2026-10-06): pytest-cov 在导入根 conftest **之前**就把 Coverage 建好了, 所以 conftest
    里设的 ``COVERAGE_RCFILE`` 对 `pytest --cov` 不起作用(那样拿到的是合并口径);
    ``coverage run -m pytest`` + ``coverage report`` 则立刻生效(同一份数据, 缺行 11 → 8)。
    因此 CI 的判定步骤用命令行: 写 rc 到 ``GITHUB_ENV`` + ``coverage report --show-missing``,
    出 XML 那一步同理。这里把这条路径钉住: 谁把它改成 `pytest --cov`, 排除就静默失效了。
    """
    workflow = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert "COVERAGE_RCFILE=$PWD/.coverage-platform.rc" in workflow
    assert "uv run coverage report" in workflow
    assert "uv run coverage xml" in workflow


def test_the_generated_file_is_not_mistaken_for_parallel_data() -> None:
    """生成物不能落在 ``.coverage.*`` 里: 那是 coverage 的并行数据 glob.

    ``parallel = true`` 时 ``coverage combine``/``report`` 会把 ``.coverage.*`` 全部当数据文件
    去读, 一个 rc 混进去就报 "file is not a database"(2026-10-06 实测), 而 CI 的判定正是
    "先 combine 再三平台各自 report"。
    """
    name = coverage_platform.DEFAULT_OUTPUT.name

    assert not name.startswith(".coverage."), f"{name} 会被当成并行数据文件"
    assert name in (_REPO_ROOT / ".gitignore").read_text(encoding="utf-8")


def test_generated_config_keeps_every_pyproject_value(tmp_path: Path) -> None:
    """生成的配置要带上 pyproject 的全部取值, 并追加本平台的排除正则."""
    sections = coverage_platform.coverage_sections()
    rendered = coverage_platform.render_config("windows", sections=sections)

    assert "[run]" in rendered
    assert "[report]" in rendered
    assert "branch = true" in rendered
    assert "relative_files = true" in rendered
    # 门槛跟着 pyproject 走(2026-10-06 提到 100% 时这里红过一次: 别再写死数字).
    assert f"fail_under = {sections['report']['fail_under']}" in rendered
    assert "source =" in rendered
    assert coverage_platform.exclusion_pattern("windows") in rendered
    # 基线里的豁免一条都不能少(它们是替换关系, 漏掉就等于解除了豁免)。
    for entry in sections["report"]["exclude_also"]:
        assert entry in rendered

    written = coverage_platform.write_platform_config(
        tmp_path / "platform.rc", platform="windows"
    )
    assert written.read_text(encoding="utf-8") == rendered


def test_the_session_installs_the_config_before_pytest_cov_starts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """conftest 要在导入期就装上配置, 且已经设过 ``COVERAGE_RCFILE`` 时不覆盖别人的选择."""
    text = _CONFTEST.read_text(encoding="utf-8")
    assert "_install_platform_coverage_config()" in text, "conftest 没装上平台配置"
    assert "importlib.util" in text, "装配置要先按路径加载脚本"

    monkeypatch.setenv("COVERAGE_RCFILE", str(tmp_path / "manual.rc"))
    assert coverage_platform.setup_environment() is None, "手工指定的配置优先"
    monkeypatch.delenv("COVERAGE_RCFILE")
    monkeypatch.setattr(coverage_platform, "DEFAULT_OUTPUT", tmp_path / "generated.rc")
    installed = coverage_platform.setup_environment()
    assert installed is not None, "没设过时要自己生成"
    assert installed.is_file()
    assert os.environ["COVERAGE_RCFILE"] == str(installed), "要把它交给 coverage"


def test_no_marker_sits_on_a_def_or_class_line() -> None:
    """平台标记不许落在 ``def``/``class`` 行上(那等于"这块不测了")."""
    offenders = [
        f"{module.relative_to(_REPO_ROOT)}:{number}: {line.strip()}"
        for module, number, line in coverage_platform.marker_lines()
        if line.strip().startswith(("def ", "class ", "async def "))
    ]
    assert offenders == [], f"平台标记落在函数/类定义上: {offenders}"


def test_every_platform_marker_states_why() -> None:
    """仓库里每一处平台标记都要合规(写法 + 原因), 违规时逐条点名."""
    offenders = [
        f"{module.relative_to(_REPO_ROOT)}:{number}: {problem}"
        for module, number, line in coverage_platform.marker_lines()
        for problem in coverage_platform.marker_problems(line)
    ]
    assert offenders == [], f"平台标记不合规: {offenders}"
