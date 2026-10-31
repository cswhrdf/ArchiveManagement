"""
被 skip 的用例也必须带 env/os 标签(否则结果会落进 Allure 隐式的 default 环境).

成因与修法见 ``tests/conftest.py`` 的 ``_attach_environment_labels``: "这条用例跑在哪台
机器上"是**收集期标记**, 不是夹具里写的 ``allure.dynamic`` 标签 —— ``pytest.mark.skip`` /
``skipif`` 在夹具之前就求值了, 夹具写的标签一条都落不上。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from archive_management.services.platforms import current_platform, platform_label

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]

#: 跑在仓库**外面**的探针用例: 一条被 ``skipif`` 跳过(夹具一个都不跑), 一条正常执行.
#: 必须放在仓库外, 否则 pytest 自己的收集会把这条用例算进正式用例数。
_PROBE_MODULE = '''"""临时会话: 检查被跳过的那条是否也带上了环境标签。"""

from __future__ import annotations

import pytest


@pytest.mark.skipif(True, reason="探针: skipif 在夹具之前求值")
def test_skipped_before_fixtures() -> None:
    """这条不会执行: autouse 夹具不会跑, 标签只能来自收集期标记。"""


def test_this_one_runs() -> None:
    """对照组: 正常执行的用例."""
    assert True
'''


def _run_probe(tmp_path: Path) -> dict[str, dict[str, Any]]:
    """在 tmp_path 里跑一次真会话(借用仓库的根 conftest), 返回 id → 结果."""
    (tmp_path / "test_probe.py").write_text(_PROBE_MODULE, encoding="utf-8")
    results = tmp_path / "allure-results"
    environment = {
        **os.environ,
        # 仓库的 conftest 是**插件**(`-p conftest`), 它自己要 import tests/ 下的模块与 src.
        "PYTHONPATH": os.pathsep.join(
            (str(_REPO_ROOT / "src"), str(_REPO_ROOT / "tests"))
        ),
        "PYTHONUTF8": "1",
    }
    completed = subprocess.run(  # noqa: S603 - 参数全是常量, 解释器与用例都是本仓库自己的
        [
            sys.executable,
            "-m",
            "pytest",
            "test_probe.py",
            "-q",
            "-p",
            "conftest",
            "-p",
            "no:cacheprovider",
            # 探针会话没有仓库的 markers 注册(没有配置文件), 这条警告是预期的。
            "-W",
            "ignore::pytest.PytestUnknownMarkWarning",
            f"--alluredir={results}",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    payloads = {
        path.name: json.loads(path.read_text(encoding="utf-8"))
        for path in results.glob("*-result.json")
    }
    assert len(payloads) == 2, f"探针会话应当产出 2 条结果, 实际 {len(payloads)}"
    return payloads


def test_a_skipped_test_still_carries_the_environment_labels(tmp_path: Path) -> None:
    """真跑一次 pytest: ``skipif`` 跳过的那条也要带 env/os, 且两者都只有一份.

    为什么必须真跑: 这个缺陷的成因是"夹具一个都没轮到", 只有 pytest 自己的跳过机制才会
    精确复现(``_pytest.mark`` 的 skipping 插件在 ``pytest_runtest_setup`` 最前面抛
    ``Skipped``)。实测现场(2026-10-14 run 38594250358): Linux/macOS 的"合并测试报告"都判
    "报告生成时掉过数据"(2870 < 2871, 缺的就是被跳过的
    ``test_gui_window_icon.py::test_a_child_window_ends_up_with_our_icon``), 汇总报告里
    两份这种结果又被生成器当成同一次尝试并掉(8837 个结果文件 vs 8836 条索引) ⇒ 自检不过
    ⇒ 最终报告没有上传。

    只带一份也是判据: 标记与夹具**两处都写**时标签会翻倍(环境维度出现重复条目)。
    """
    payloads = _run_probe(tmp_path)

    expected = platform_label(current_platform())
    statuses: dict[str, list[str]] = {}
    for name, payload in payloads.items():
        labels: dict[str, list[str]] = {}
        for label in payload.get("labels", []):
            labels.setdefault(label["name"], []).append(label["value"])
        status = str(payload["status"])
        statuses.setdefault(status, []).append(name)
        assert labels.get("env") == [expected], f"{name} 的 env 标签不对: {labels}"
        assert labels.get("os") == [expected], f"{name} 的 os 标签不对: {labels}"

    # 前提自检: 探针确实造出了"一条跳过 + 一条执行", 否则上面的断言是空转。
    assert sorted(statuses) == ["passed", "skipped"], statuses
