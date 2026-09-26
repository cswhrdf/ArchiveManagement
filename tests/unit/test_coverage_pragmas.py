"""覆盖率豁免标记(``# pragma: no cover`` / ``# pragma: no branch``)的守卫用例.

为什么需要: 豁免标记是**唯一能让覆盖率数字说谎的开关** —— 写错一个标记, 那几行就
永远不再被统计, 而且没有任何一步会变红。因此规则写成用例:

1. 只认规范写法 ``# pragma: no cover`` / ``# pragma: no branch``(少一个空格、写成
   ``#pragma`` 都会被当普通注释, 于是"以为豁免了实际没豁免");
2. **每个标记都必须说明为什么可以跳过** —— 以 `` - `` 分隔, 后面至少 5 个字,
   且不能是 ``TODO``/``无`` 这类占位词;
3. 不许把整个函数/类排除掉(标记落在 ``def``/``class`` 行上): 那等于整块不测;
4. ``no branch`` 只许标在带分支的行上(``if``/``elif``/``else``/``for``/``while``/
   ``except``/``finally``) —— 标在别处不会起任何作用;
5. 顺带锁住"仓库里真的还在用豁免, 而不是这些规则被悄悄绕过"(否则规则可能静默失效)。

语义区分(写新标记时先想清楚要用哪一种):

- ``no cover``: 这块代码**永远不会被执行**(例如"刚写入的行必然可读"这类防御分支),
  整块(含 ``if`` 行与它的块)都不计入行与分支;
- ``no branch``: 代码会执行, 但**只会走一个方向**(例如"前缀不可能是整串"),
  因此不报"分支只覆盖了一半"。

解析规则**只有一份**: 定义在 ``scripts/create_allure_summary.py`` 里(它据此生成报告首页的
``allure-coverage-exclusions.md``), 本模块直接复用同一批常量与函数 —— 否则会出现两套宽松程度
不同的解析器: 一处放宽, 另一处就把该报的问题当成合规。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("覆盖率豁免"),
    pytest.mark.story("豁免标记必须写明原因"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SRC = _REPO_ROOT / "src"


def _load_summary() -> Any:
    """按路径加载汇总脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    spec = importlib.util.spec_from_file_location(
        "create_allure_summary",
        _REPO_ROOT / "scripts" / "create_allure_summary.py",
    )
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# 与报告附件共用同一份规则(见模块 docstring)。
summary = _load_summary()
# 规范写法 + 原因: `# pragma: no cover - 原因`。
_PRAGMA = summary.PRAGMA_PATTERN
# 允许 ``no branch`` 出现的行: 标在别处等于没标。
_BRANCH_LINES = summary.BRANCH_LINE_PREFIXES


def _python_files() -> list[Path]:
    """源码树里的全部 Python 文件."""
    return sorted(_SRC.rglob("*.py"))


def _pragma_lines() -> list[tuple[Path, int, str]]:
    """收集 (文件, 行号, 整行) 形式的豁免标记(未注明原因时 stop 在调用方)."""
    found: list[tuple[Path, int, str]] = []
    for module in _python_files():
        for number, text in enumerate(
            module.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if summary.is_pragma_line(text):
                found.append((module, number, text))
    return found


def _relative(module: Path) -> str:
    """相对源码树的路径, 便于在失败信息里定位."""
    return str(module.relative_to(_SRC))


def test_there_are_pragmas_to_check() -> None:
    """自检: 一条豁免标记都没有时, 下面的规则会"因为没有对象而全绿"."""
    assert _pragma_lines(), "源码里没有任何 pragma 标记: 规则失去了检查对象"


def test_every_pragma_uses_the_canonical_spelling() -> None:
    """写法必须规范: 少个空格或写成 ``#pragma`` 都是普通注释, 豁免会静默失效."""
    offenders = [
        f"{_relative(module)}:{number}: {text.strip()}"
        for module, number, text in _pragma_lines()
        if summary.parse_pragma(text) is None
    ]

    assert offenders == [], f"pragma 写法不规范: {offenders}"


def test_every_pragma_states_why_it_can_be_skipped() -> None:
    """每个豁免都必须写明原因(长度下限 + 不是占位词); 判定复用脚本里的同一份规则."""
    offenders: list[str] = []
    for module, number, text in _pragma_lines():
        match = _PRAGMA.search(text)
        if match is None:
            continue
        reason = match.group("rest").strip()
        if not reason.startswith("-"):
            offenders.append(
                f"{_relative(module)}:{number}: 缺少 ` - 原因` ({text.strip()})"
            )
            continue
        problem = summary.pragma_reason_problem(reason.lstrip("- ").strip())
        if problem:
            offenders.append(f"{_relative(module)}:{number}: {problem}")

    assert offenders == [], f"豁免标记没有说明原因: {offenders}"


def test_no_pragma_excludes_a_whole_function_or_class() -> None:
    """不许把 ``def``/``class`` 整块排除: 那等于"这块不测了", 而不是"这条分支走不到"."""
    offenders = [
        f"{_relative(module)}:{number}: {text.strip()}"
        for module, number, text in _pragma_lines()
        if text.strip().startswith(("def ", "class ", "async def "))
    ]

    assert offenders == [], f"豁免标记落在函数/类定义上: {offenders}"


def test_no_branch_pragmas_sit_on_lines_without_a_branch() -> None:
    """``no branch`` 只能标在带分支的行上, 否则它既不生效又给人"已经处理过"的错觉."""
    offenders: list[str] = []
    for module, number, text in _pragma_lines():
        match = _PRAGMA.search(text)
        if match is None or match.group("kind") != "no branch":
            continue
        stripped = text.strip()
        if not stripped.startswith(_BRANCH_LINES):
            offenders.append(f"{_relative(module)}:{number}: {stripped}")

    assert offenders == [], f"`no branch` 标在了没有分支的行上: {offenders}"
