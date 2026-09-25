"""Tk 相关跳过的纪律: 只允许"已知环境问题"跳过, 其它一律当失败.

背景(2026-09-25 从 CI 报告里发现): GUI 用例的环境守卫把建窗口期的 ``TclError``
统一写成 ``pytest.skip("tk 环境不可用: ...")``。于是**任何**建窗口期报错都会变成
一次跳过 —— 报告里只看到"跳过了", 用例等于不存在::

    Skipped: tk 环境不可用: image "pyimage1" does not exist

`test_poster_markers_only_get_a_backing_over_a_real_cover` 就是这样在 Linux 与
Windows 上**都**被跳过的(没人注意, 因为跳过不会让任何东西变红)。

规则: 只有**已知的环境类症状**(见 :data:`KNOWN_TK_SKIP_MARKERS`)才允许跳过;
原因里出现别的字样(图像名失效、控件已销毁、窗口不存在……)说明问题在用例或被测
代码一侧, 必须按**失败**处理。这条规则由 ``tests/conftest.py`` 的钩子兜底, 因此
不要求每个 ``except TclError`` 守卫自己写对 —— 漏写也拦得住。

纯函数放在这里(本模块不依赖 pytest/ctk), 由 ``tests/unit/test_gui_retry.py`` 守卫。
"""

from __future__ import annotations

# 守卫统一使用的原因前缀(见 tests/integration/test_gui_*.py 与 tests/gui_support.py)。
TK_SKIP_PREFIX = "tk 环境不可用"

# 允许跳过的"已知环境问题": 解释器找不到 Tcl 库数据, 或者根本没有显示环境。
KNOWN_TK_SKIP_MARKERS = (
    # uv 托管的 standalone 构建偶尔缺 Tcl 数据文件(上游 astral-sh/uv#7036)。
    "tcl_findlibrary",
    "init.tcl",
    # 真的没有显示环境(无头 Linux 且没有 xvfb)。
    "no display name",
    "couldn't connect to display",
    "no $display environment variable",
)

# 把跳过改成失败时给出的说明(必须说清"为什么这不是环境问题")。
_FAILURE_TEMPLATE = (
    "这条跳过不是已知的 Tk 环境问题, 因此按失败处理(用例不许静默消失)。\n"
    "原始原因: {reason}\n"
    "判定依据: 只有 {markers} 这几种症状才算环境问题; 其它 TclError 说明问题在用例\n"
    "或被测代码一侧 —— 要么修掉, 要么删掉这条用例, 不要留着一条永远不跑的用例。\n"
    "规则见 tests/tk_guard.py。"
)


def skip_reason(longrepr: object) -> str:
    """从 pytest 的 ``report.longrepr`` 里取出跳过原因.

    跳过报告里它通常是 ``(文件, 行号, 原因)`` 三元组; 模块级跳过等场景直接是字符串。
    只取原因那一项, 失败说明里才不会带上一串定位信息。
    """
    if isinstance(longrepr, tuple) and len(longrepr) == 3:
        return str(longrepr[2])
    return str(longrepr)


def is_known_tk_skip(reason: str) -> bool:
    """判断这条跳过原因是否属于"已知的 Tk 环境问题"(大小写不敏感)."""
    text = reason.casefold()
    return any(marker in text for marker in KNOWN_TK_SKIP_MARKERS)


def unknown_tk_skip_message(reason: str) -> str | None:
    """需要拦下的跳过: 以 :data:`TK_SKIP_PREFIX` 开头但不在已知清单里.

    返回给 pytest 的失败说明; 不需要拦下(不是 Tk 跳过, 或确实是已知环境问题)时
    返回 ``None``。
    """
    if TK_SKIP_PREFIX not in reason:
        return None
    if is_known_tk_skip(reason):
        return None
    return _FAILURE_TEMPLATE.format(
        reason=reason.replace("\n", " ").strip(),
        markers=" / ".join(KNOWN_TK_SKIP_MARKERS),
    )
