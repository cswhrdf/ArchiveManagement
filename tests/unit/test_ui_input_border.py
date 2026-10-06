"""输入控件边界的静态守卫: 每个输入控件都必须用 ``input_border``.

为什么单独守这一条: 输入框的边界是 WCAG 1.4.11 要求 3:1 的**信息载体**("这里可以输入"),
而面板/卡片的外框是装饰性描边、共用 ``border`` 就够了(见
:mod:`tests.unit.test_ui_contrast` 的输入边界那一档)。两者混用时实测最差只有
**1.20:1** —— 边界等于看不见, 但界面照常能跑、测试也全绿, 只有人眼看得出来。

新加一个输入控件、或者把某个输入框的边界改回 ``border``, 这条守卫都会当场点名。读源码的
``ast``, 不启动界面(与 ``test_ui_metrics.py`` 同一手法)。

兜底断言是"扫到的输入控件数量": 否则"0 处违规"可能只是扫描跑空了 —— 类名改名、glob
写错、kwarg 改名都会让它安静地失效。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("主题与配色"),
    pytest.mark.story("对比度达标"),
    pytest.mark.layer("unit"),
]

_UI_DIR = Path(__file__).resolve().parents[2] / "src" / "archive_management" / "ui"
# 会画边界的输入控件. ``CTkOptionMenu`` 也在内: 它不支持 ``border_width``/``border_color``
# (实测 ``cget`` 直接抛 ``ValueError``), 所以本仓库不用它 —— 若哪天用了, 这条会提醒
# "它没有边界可设", 而不是静默放过。
_INPUT_TYPES = frozenset({"CTkEntry", "CTkComboBox", "CTkOptionMenu", "CTkTextbox"})
# 登记: 扫到的输入控件站点数(加/删一个输入控件时要跟着改 —— 数字变了正说明这件事)。
# 21 → 22: 设置窗口新增“备份校验方式”下拉框(2026-10-04, 校验方式可选)。
_EXPECTED_INPUT_SITES = 22


def _ui_sources() -> list[Path]:
    """界面源码(不含 :mod:`ui.contrast` 这类纯逻辑模块 —— 它们本来就不建控件)."""
    return sorted(_UI_DIR.glob("*.py"))


def _is_input_call(node: ast.Call) -> bool:
    """这次调用是不是在造输入控件(``ctk.CTkEntry(...)`` / ``self.kit.entry(...)``)."""
    name = ast.unparse(node.func).rsplit(".", 1)[-1]
    return name in _INPUT_TYPES


def _boundary_offenders(path: Path, node: ast.Call) -> list[str]:
    """这一次构造里"边界没指向 ``input_border``"的说法(空列表表示没问题)."""
    keyword = next((item for item in node.keywords if item.arg == "border_color"), None)
    if keyword is None:
        return [f"{path.name}:{node.lineno} 没给 border_color"]
    if not ast.unparse(keyword.value).endswith(".input_border"):
        return [f"{path.name}:{node.lineno} border_color={ast.unparse(keyword.value)}"]
    return []


def _scan() -> tuple[list[str], int]:
    """扫一遍界面源码, 返回(违规清单, 输入控件站点数)."""
    offenders: list[str] = []
    sites = 0
    for path in _ui_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not _is_input_call(node):
                continue
            sites += 1
            offenders.extend(_boundary_offenders(path, node))
    return offenders, sites


def test_scan_reaches_every_input_control() -> None:
    """兜底: 站点数必须与登记一致 —— 否则"0 处违规"可能只是扫描空了."""
    _, sites = _scan()
    assert sites == _EXPECTED_INPUT_SITES, "输入控件的数量变了, 判据跟着改"


def test_every_input_control_uses_the_input_boundary() -> None:
    """每个输入控件的边界都必须来自 ``input_border``(不是装饰性的 ``border``)."""
    offenders, _ = _scan()
    assert offenders == [], "这些输入控件的边界没指向 input_border: " + ", ".join(
        offenders
    )
