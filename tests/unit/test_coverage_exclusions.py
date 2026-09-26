"""覆盖率豁免清单附件(报告首页「全局附件」)的回归用例.

背景: 项目用 ``# pragma: no cover`` / ``# pragma: no branch`` 把"走不到"的代码移出统计,
``pyproject.toml`` 的 ``[tool.coverage.report] exclude_also`` 另外排除几个代码块。豁免是
**唯一能让覆盖率数字说谎的开关**, 所以最终报告里要能看到"到底跳过了哪些行、为什么" ——
``scripts/create_allure_summary.py`` 把它们写成 ``allure-coverage-exclusions.md``。

这里锁住三件事:

- 清单来自真实数据: 标记逐条列出(文件:行号 + 种类 + 原因), ``exclude_also`` 一并附上;
- 一条豁免都没有时要**明确写出"没有"**, 而不是留一份空清单让人以为漏了;
- 缺原因的标记必须被列为"写入问题", 不能静默当成合规。
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("覆盖率豁免清单附件"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_summary() -> Any:
    """按路径加载汇总脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    spec = importlib.util.spec_from_file_location(
        "create_allure_summary", _REPO_ROOT / "scripts" / "create_allure_summary.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


summary = _load_summary()

_PYPROJECT = """[tool.coverage.report]
exclude_also = [
    "if TYPE_CHECKING:",
    "raise NotImplementedError",
]
fail_under = 95
"""
_PYPROJECT_WITHOUT_EXCLUSIONS = """[tool.coverage.report]
fail_under = 95
"""


def _write_repo(
    tmp_path: Path, source: str | None, *, pyproject: str = _PYPROJECT
) -> Path:
    """造一个最小的"仓库": ``src/`` 下的源码 + ``pyproject.toml``."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if source is not None:
        module = root / "src" / "pkg" / "mod.py"
        module.parent.mkdir(parents=True)
        module.write_text(source, encoding="utf-8")
    return root


def test_report_lists_every_pragma_with_its_reason(tmp_path: Path) -> None:
    """两种标记各一条时, 清单要给出文件:行号、种类、原因, 并附上 exclude_also."""
    root = _write_repo(
        tmp_path,
        "x = 1  # pragma: no cover - 防御分支永远跑不到\n"
        "if x:  # pragma: no branch - 只有一个方向\n"
        "    pass\n",
    )

    text = summary.coverage_exclusions_report(root)

    assert "`no cover` 1 条" in text
    assert "`no branch` 1 条" in text
    assert "`exclude_also` 2 条" in text
    assert "`pkg/mod.py:1`" in text
    assert "防御分支永远跑不到" in text
    assert "`pkg/mod.py:2`" in text
    assert "只有一个方向" in text
    assert "`if TYPE_CHECKING:`" in text
    assert "`raise NotImplementedError`" in text
    assert "写入问题" in text
    assert "没有: 每个豁免标记都写明了原因" in text


def test_report_states_explicitly_when_there_is_nothing_to_exclude(
    tmp_path: Path,
) -> None:
    """没有豁免时要写出"没有", 而不是留一份空清单."""
    root = _write_repo(tmp_path, "x = 1\n", pyproject=_PYPROJECT_WITHOUT_EXCLUSIONS)

    text = summary.coverage_exclusions_report(root)

    assert "`no cover` 0 条" in text
    assert "`no branch` 0 条" in text
    assert "本次没有任何 `# pragma: no cover` / `# pragma: no branch` 标记" in text
    assert "`exclude_also` 为空" in text


def test_report_surfaces_a_pragma_without_a_reason(tmp_path: Path) -> None:
    """缺原因的标记要列为"写入问题", 不能静默当成合规的豁免."""
    root = _write_repo(tmp_path, "x = 1  # pragma: no cover\n")

    text = summary.coverage_exclusions_report(root)

    assert "**写入问题: 1 条" in text
    assert "- `pkg/mod.py:1` — 缺少 ` - 原因`" in text


def test_report_uses_the_real_tree_when_no_source_root_is_given(tmp_path: Path) -> None:
    """不传 source_root 时按 ``<仓库根>/src`` 扫描 —— 数据来自真实源码."""
    root = _write_repo(
        tmp_path,
        "if x:  # pragma: no branch - 只有一个方向\n    pass\n",
    )

    text = summary.coverage_exclusions_report(root)

    assert "`no branch` 1 条" in text
    assert "`pkg/mod.py:1`" in text
