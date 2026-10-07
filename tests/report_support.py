"""
由 test_report_verification.py 拆分出的共享测试基建(被多个主题文件引用), 拆分原则与映射见 docs/test-refactor-plan.md。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


# 本模块位于 tests/ 根(比原 tests/unit/test_report_verification.py 少一层), 锚点相应上移一层.
_REPO_ROOT = Path(__file__).resolve().parents[1]


_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


_PYPROJECT = _REPO_ROOT / "pyproject.toml"


_ALLURE_CONFIG = _REPO_ROOT / "allurerc.mjs"


_RESULT_IDS = ("aaa111", "bbb222")


def _load_script(name: str) -> Any:
    """按路径加载 ``scripts/`` 下的脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    spec = importlib.util.spec_from_file_location(
        name, _REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_verifier() -> Any:
    """按路径加载校验脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    return _load_script("verify_allure_report")


verifier = _load_verifier()


_ENVIRONMENT_ENTRY = re.compile(
    r"^\s{4}([a-z][\w-]*):\s*\{\s*name:\s*\"([^\"]+)\"", re.MULTILINE
)


def required_environment_ids() -> tuple[str, ...]:
    """读出 ``allurerc.mjs`` 里质量门 ``environmentsTested`` 要的那几个**环境 id**.

    为什么盯的是 id: 质量门比的是每条结果的 ``environment``, 那是环境身份里的 **id**
    (``environments`` 的键, 小写)。写成平台显示名("Windows")**一个都比不上** —— 症状是
    门禁一次报全三个"未被测试", 而三条平台的用例其实都交齐了(2026-10-04 的汇总报告就是
    这么红的; 本地用三平台最小结果复现过, 见 PLAN.md §48.6)。
    """
    gate = _ALLURE_CONFIG.read_text(encoding="utf-8").split("qualityGate:", 1)[1]
    matched = re.search(r"environmentsTested:\s*\[([^\]]+)\]", gate)
    assert matched is not None, "配置里要有 environmentsTested"
    return tuple(name.strip().strip('"') for name in matched.group(1).split(","))


def declared_environment_names() -> dict[str, str]:
    """``environments`` 里声明的 环境 id → 显示名(把门禁要的 id 翻成平台名用)."""
    block = _ALLURE_CONFIG.read_text(encoding="utf-8").split("environments: {", 1)[1]
    return dict(_ENVIRONMENT_ENTRY.findall(block))


def required_platforms() -> tuple[str, ...]:
    """质量门要求的平台**显示名**(与 CI 矩阵、汇总作业的 ``--expect-platforms`` 对照).

    CI 里三处引用它: 配置里的 ``environmentsTested``、汇总作业的 ``--expect-platforms``
    与各矩阵的平台列表 —— 由这里的守卫核对着一致。配置里写的是环境 id(见
    :func:`required_environment_ids`), 这里按 ``environments`` 的声明翻成显示名 ——
    顺带把"门禁要的 id 到底声明过没有"也钉住: 写错一个 id 就没得翻, 当场红。
    """
    names = declared_environment_names()
    ids = required_environment_ids()
    missing = [env_id for env_id in ids if env_id not in names]
    assert missing == [], (
        f"environmentsTested 里的 id 必须在 environments 里声明过: {missing}"
    )
    return tuple(names[env_id] for env_id in ids)


@dataclass(frozen=True)
class _Layout:
    """一份结构完整的报告与配套结果目录, 便于用例按需破坏其中一部分."""

    report: Path
    results: Path


def _write_platform_report(layout: _Layout) -> None:
    """把 fixture 的报告改造成三平台形态: 一个 Windows 用例 + 一个 macOS 汇总项."""
    windows_id, macos_id = _RESULT_IDS
    (layout.report / "widgets" / "environments.json").write_text(
        json.dumps(
            [
                {"id": "default", "name": "default"},
                {"id": "windows", "name": "Windows"},
                {"id": "macos", "name": "macOS"},
                {"id": "linux", "name": "Linux"},
            ]
        ),
        encoding="utf-8",
    )
    (layout.report / "test-results.json").write_text(
        json.dumps(
            {
                "byId": {
                    windows_id: {
                        "id": windows_id,
                        "status": "passed",
                        "environment": "Windows",
                    },
                    macos_id: {
                        "id": macos_id,
                        "status": "passed",
                        "environment": "macOS",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    details = layout.report / "data" / "test-results"
    (details / f"{windows_id}.json").write_text(
        json.dumps(
            {"id": windows_id, "labels": [{"name": "framework", "value": "pytest"}]}
        ),
        encoding="utf-8",
    )
    # macOS 只有一条脚本生成的汇总项(没有 framework 标签): 环境存在, 用例一条都没有。
    (details / f"{macos_id}.json").write_text(
        json.dumps(
            {"id": macos_id, "labels": [{"name": "testCategory", "value": "coverage"}]}
        ),
        encoding="utf-8",
    )


def _write_result(
    results: Path, slug: str, name: str, status: str, category: str
) -> None:
    """写一条极简 Allure 结果(两条断言用得到: 名称、状态与类别标签)."""
    payload = {
        "uuid": slug,
        "name": name,
        "status": status,
        "labels": [{"name": "testCategory", "value": category}],
    }
    (results / f"{slug}-result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _conclusion_table(ledger: str) -> str:
    """从总账里切出末节「证据核对」的 ① 结论清单表.

    「证据核对」是**末节**(2026-10-02 调整读序: 前面是结论本身, 证据齐不齐是审计附录),
    它与产物清单合并成一节、下面挂着 ①② 两张表, 所以切片要按小节标题而不是按 ``##``。
    """
    assert "### ① 结论清单" in ledger, "总账要有「证据核对」末节的 ① 结论清单"
    return ledger.split("### ① 结论清单", 1)[1].split("### ②", 1)[0]


def _write_platform_result(
    results: Path, slug: str, *labels: tuple[str, str], full_name: str = ""
) -> None:
    """写一条带标签的结果(占位用例与汇总结论项共用: 身份与所属环境都靠标签认)."""
    payload: dict[str, Any] = {
        "uuid": slug,
        "name": slug,
        "status": "passed",
        "labels": [{"name": name, "value": value} for name, value in labels],
    }
    if full_name:
        payload["fullName"] = full_name
    (results / f"{slug}-result.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
