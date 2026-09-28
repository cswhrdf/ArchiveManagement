"""界面度量刻度的静态守卫: 页面不许再写刻度外的魔数.

判据只有三条(读源码的 ast, 不启动界面):

1. ``corner_radius`` 的值必须是 :data:`archive_management.ui.metrics.RADIUS_SCALE` 里的档位;
2. ``CTkFont(size=...)`` 的值必须是 :data:`archive_management.ui.typography.FONT_SCALE` 里的档位;
3. ``padx`` / ``pady`` 里的每个整数都必须是 :data:`metrics.SPACE_SCALE` 里的刻度.

一个**具名常量**是允许的出口: 像"勾选行下面那行提示与复选框文字对齐"这种量出来的缩进
(24 + 22 的方框宽)没法落在刻度上, 写成带注释的模块级常量即可被 grep 到、有理由可查。
守卫拦的是"伸手就写的数字"。

三条判据各自还有一条**兜底断言**: 扫到的站点数必须与登记的数字一致 —— 否则"0 处违规"
可能只是扫描本身跑空了(刻度模块改名、glob 写错、kwarg 改名都会让守卫安静地失效)。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from archive_management.ui.metrics import RADIUS_NAMES, RADIUS_SCALE, SPACE_SCALE
from archive_management.ui.typography import FONT_SCALE

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("视觉一致性"),
    pytest.mark.story("度量刻度"),
    pytest.mark.layer("unit"),
]

_UI_DIR = Path(__file__).resolve().parents[2] / "src" / "archive_management" / "ui"
# 刻度自己的定义处: 这两个模块里的数字就是刻度本身, 不参与判据。
_SCALE_MODULES = {"metrics.py", "typography.py"}
_SPACE_KEYS = {"padx", "pady"}
# 登记: 本轮(2026-09-27) 扫到的站点数, 用来证明判据不是在空转。
# 圆角从 66 变 65 是 I-5 第二轮把管理窗口那一处字面量改成了 ``RADIUS_MD`` 常量。
# 模块数从 19 变 21 是 I-6 新增的 ``contrast.py``(对比度算法)与 ``keyboard.py``
# (键盘可用性): 两者都是纯逻辑, 不含间距/圆角/字号字面量, 所以三个站点数不变。
# 模块数从 21 变 23 是 I-9 新增的 ``tree_layout.py``(布局纯函数)与 ``tree_view.py``
# (分支图画布): 两者同样一个间距/圆角/字号**字面量**都没有 —— 图的尺寸是
# ``tree_layout`` 里那组具名常量(框宽/行高/间距), 字号走 ``typography.font``。
_EXPECTED_FILES = 23
_EXPECTED_RADIUS_SITES = 65
# 字号站点 169 → 168: 同上一处 —— 被删掉的那行提示自己也带一个 `CTkFont(size=11)`。
_EXPECTED_FONT_SITES = 168
# 间距站点 775 → 778: I-9 给主窗口的左侧主体加了一个同格叠放的图画布容器,
# 它的 ``grid(padx=8, pady=(0, 8))`` 与滚动容器那一处对齐(3 个整数都在刻度上)。
# 间距站点 778 → 775: 编辑备份信息对话框删掉了"描述会显示在备份卡片上"那行提示
# (分支图里没有卡片, 这句在默认视图里就是错的), 它自己那``padx=24``/``pady=(0, 12)``
# 也随之消失; 描述框下面那条计数标签的 ``pady`` 从 ``(0, 10)`` 改成 ``(0, 20)``
# 补上被删掉的那段留白, 整数个数不变。
_EXPECTED_SPACE_SITES = 775


def _integers(node: ast.expr) -> list[int]:
    """取出表达式里的整数字面量(含元组与一元负号)."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return [node.value]
    if isinstance(node, ast.Tuple):
        found: list[int] = []
        for element in node.elts:
            found.extend(_integers(element))
        return found
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return [-value for value in _integers(node.operand)]
    return []


def _ui_sources() -> list[Path]:
    return sorted(
        path for path in _UI_DIR.glob("*.py") if path.name not in _SCALE_MODULES
    )


def _space_offenders(path: Path, node: ast.Call) -> tuple[list[str], int]:
    """这一次调用里的间距字面量: 越界的写清楚是谁, 顺便报站点数."""
    found: list[str] = []
    count = 0
    for keyword in node.keywords:
        if keyword.arg not in _SPACE_KEYS:
            continue
        values = _integers(keyword.value)
        count += len(values)
        found.extend(
            f"{path.name}:{node.lineno} {keyword.arg}={value}"
            for value in values
            if value not in SPACE_SCALE
        )
    return found, count


def _radius_offenders(path: Path, node: ast.Call) -> tuple[list[str], int]:
    """这一次调用里的圆角字面量: 不在档位上的点名, 并给出该写哪个常量."""
    found: list[str] = []
    count = 0
    for keyword in node.keywords:
        if keyword.arg != "corner_radius":
            continue
        values = _integers(keyword.value)
        count += len(values)
        found.extend(
            f"{path.name}:{node.lineno} corner_radius={value}"
            f" (该写 {RADIUS_NAMES.get(value, 'RADIUS_*')})"
            for value in values
            if value not in RADIUS_SCALE
        )
    return found, count


def _font_offenders(path: Path, node: ast.Call) -> tuple[list[str], int]:
    """这一次调用里的字号字面量(只认 ``CTkFont(size=...)``)."""
    if not ast.unparse(node.func).endswith("CTkFont"):
        return [], 0
    found: list[str] = []
    count = 0
    for keyword in node.keywords:
        if keyword.arg != "size":
            continue
        values = _integers(keyword.value)
        count += len(values)
        found.extend(
            f"{path.name}:{node.lineno} size={value}"
            for value in values
            if value not in FONT_SCALE
        )
    return found, count


def _scan() -> tuple[list[str], list[str], list[str], int, int, int]:
    """扫一遍界面源码, 返回 (间距违规, 圆角违规, 字号违规, 三个站点数)."""
    offenders: tuple[list[str], list[str], list[str]] = ([], [], [])
    sites = [0, 0, 0]
    scans = (_space_offenders, _radius_offenders, _font_offenders)
    for path in _ui_sources():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for index, scan in enumerate(scans):
                found, count = scan(path, node)
                sites[index] += count
                offenders[index].extend(found)
    return offenders[0], offenders[1], offenders[2], sites[0], sites[1], sites[2]


def test_scan_reaches_every_page() -> None:
    """兜底: 扫描要真的扫到每个界面模块, 数字也要与登记的一致."""
    files = _ui_sources()
    _, _, _, space_sites, radius_sites, font_sites = _scan()
    assert len(files) == _EXPECTED_FILES, [path.name for path in files]
    assert space_sites == _EXPECTED_SPACE_SITES, "间距站点的数量变了, 判据跟着改"
    assert radius_sites == _EXPECTED_RADIUS_SITES, "圆角站点的数量变了, 判据跟着改"
    assert font_sites == _EXPECTED_FONT_SITES, "字号站点的数量变了, 判据跟着改"


def test_spacing_values_are_on_the_scale() -> None:
    """``padx`` / ``pady`` 只许用刻度上的值(0/2/4/6/8/10/12/16/20/24)."""
    space_bad, _, _, _, _, _ = _scan()
    assert space_bad == [], "这些间距没落在刻度上: " + ", ".join(space_bad)


def test_corner_radius_is_one_of_the_tiers() -> None:
    """``corner_radius`` 只许用四档圆角, 而且要在报错里说清该写哪一个."""
    _, radius_bad, _, _, _, _ = _scan()
    assert radius_bad == [], "这些圆角不在档位上: " + ", ".join(radius_bad)


def test_font_sizes_are_on_the_ladder() -> None:
    """字号只许用阶梯上的档位(见 ``typography.FONT_*``)."""
    _, _, font_bad, _, _, _ = _scan()
    assert font_bad == [], "这些字号不在阶梯上: " + ", ".join(font_bad)


def test_scale_values_are_what_the_docs_claim() -> None:
    """刻度本身也不许悄悄变长: 加一档等于把"少量刻度"变成"什么数都行"."""
    assert SPACE_SCALE == (0, 2, 4, 6, 8, 10, 12, 16, 20, 24)
    assert RADIUS_SCALE == (0, 4, 6, 8, 10)
    assert FONT_SCALE == (10, 11, 12, 13, 15, 16, 20, 26, 28)
    assert RADIUS_NAMES[RADIUS_SCALE[3]] == "RADIUS_MD"
