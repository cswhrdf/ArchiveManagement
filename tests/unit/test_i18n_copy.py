"""i18n 文案与调用点的一致性守卫(I-3).

两条规则, 都是"文案与代码对不对得上", 光看 JSON 看不出来的那种:

A. **占位符必须在调用点被替换**: 文案里的 ``{name}`` 少传一个, 用户界面上就会直接
   显示 ``{name}`` 这种字样。24 号评审那条就是这个病 —— 详情页写着"可点「恢复到某
   节点」切换", 而按钮其实叫「恢复到此节点」, 打眼一看像文案写错, 根子是那个名字
   本来是 ``{button}`` 占位符。做法: 遍历 ``src/**`` 里所有 ``tr(...)`` 调用, 把字面
   键的占位符与实参名逐一对比; 键不是字面量、或者调用点用了 ``**kwargs`` 的, 按
   "看不出来" 跳过 —— 判据宁可漏报, 也不为了凑数字去猜。

B. **文案里「」引用的名字必须真实存在**: 中文文案引用面板/按钮时会写「游戏设置」
   这类名字, 名字写错用户就找不到地方(「恢复到某节点」正是如此)。做法: 取出每条
   文案里「」内的片段, 要求它是某条文案的**原文、子串、或按 ``→`` 拼出来的层级
   路径**(每一段各自满足前面两条) —— 这样既容得下"游戏设置 → 定时备份"这种两级
   引用, 又拦得住根本不存在的名字。
"""

from __future__ import annotations

import ast
import json
import re
from importlib.resources import files
from pathlib import Path
from typing import cast

import pytest

pytestmark = [
    pytest.mark.i18n,
    pytest.mark.minor,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("国际化"),
    pytest.mark.story("文案与调用点一致"),
    pytest.mark.layer("unit"),
]

# 占位符与中文引号都写成转义: 源码里直接出现全角标点会被 RUF001 拦(仓库约定)。
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_QUOTED = re.compile("\u300c([^\u300d]+)\u300d")
# 层级引用用这两个符号连接: 「游戏设置 → 定时备份」。
_HIERARCHY = "\u2192"


def _catalog(locale: str) -> dict[str, str]:
    """读出某个 locale 的文案表."""
    base = files("archive_management").joinpath("resources", "i18n")
    raw = json.loads(base.joinpath(f"{locale}.json").read_text(encoding="utf-8"))
    return cast("dict[str, str]", raw)


def _source_files() -> list[Path]:
    """产品代码(界面文案的调用点都在里面)."""
    root = Path(__file__).resolve().parents[2] / "src" / "archive_management"
    return sorted(root.rglob("*.py"))


def _tr_calls() -> list[tuple[Path, ast.Call]]:
    """所有 ``tr(...)` 调用点(带上所在文件, 便于报错定位)."""
    found: list[tuple[Path, ast.Call]] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name == "tr" and node.args:
                found.append((path, node))
    return found


def _unfilled_placeholders() -> list[str]:
    """A: 字面键的占位符没被实参补全的调用点."""
    catalogs = {locale: _catalog(locale) for locale in ("zh-CN", "en")}
    problems: list[str] = []
    for path, call in _tr_calls():
        first = call.args[0]
        if not isinstance(first, ast.Constant) or not isinstance(first.value, str):
            continue  # 键是变量: 看不出来, 跳过
        needed: set[str] = set()
        for catalog in catalogs.values():
            text = catalog.get(first.value)
            if text is not None:
                needed |= set(_PLACEHOLDER.findall(text))
        if not needed:
            continue
        named = {keyword.arg for keyword in call.keywords if keyword.arg}
        expands = any(keyword.arg is None for keyword in call.keywords)
        if expands:
            continue  # **kwargs: 实参名在这儿看不到
        missing = sorted(needed - named)
        if missing:
            problems.append(
                f"{path.name}:{call.lineno} tr({first.value!r}) 少了 {missing}"
            )
    return problems


def _matches_reference(name: str, values: list[str]) -> bool:
    """这个名字是不是"真实存在的文案"(原文、子串, 或层级路径的每一段)."""
    if any(name == value or name in value for value in values):
        return True
    parts = [part.strip() for part in name.split(_HIERARCHY)]
    return len(parts) > 1 and all(_matches_reference(part, values) for part in parts)


def _ghost_references() -> list[str]:
    """B: 中文文案里引用了、但实际不存在的面板/按钮名.

    **比对自己以外的文案**: 引号里的名字就写在它自己那条文案里, 所以"拿全表做子串
    比对"必然成立 —— 那个自匹配会让任何名字都显得"存在"(实测踩过: 把
    ``{button}`` 换成写死的「恢复到某节点」, 守卫照样绿)。判据因此只认真实存在的
    名字, 不认"自己说了算"。
    """
    catalog = _catalog("zh-CN")
    problems: list[str] = []
    for key, text in sorted(catalog.items()):
        others = [value for other, value in catalog.items() if other != key]
        for name in _QUOTED.findall(text):
            if "{" in name:
                continue  # 名字是拼出来的(占位符), 由 A 那条规则管
            if _matches_reference(name, others):
                continue
            problems.append(f"{key} 引用了不存在的名字「{name}」")
    return problems


def test_every_placeholder_is_filled_at_the_call_site() -> None:
    """A: 没有哪个 ``tr()`` 调用点漏传占位符."""
    problems = _unfilled_placeholders()
    hint = (
        f"有 {len(problems)} 个调用点漏传占位符"
        "(界面上会直接显示 {name} 这种字样):\n"
        + "\n".join(f"{index}. {item}" for index, item in enumerate(problems, 1))
    )
    assert not problems, hint


def test_quoted_names_exist_in_the_catalog() -> None:
    """B: 文案里「」引用的名字必须在文案表里找得到."""
    problems = _ghost_references()
    hint = (
        f"有 {len(problems)} 处引用了不存在的名字"
        "(用户按这个名字找不到对应的地方):\n"
        + "\n".join(f"{index}. {item}" for index, item in enumerate(problems, 1))
    )
    assert not problems, hint


def _placeholder_names(locale: str) -> dict[str, set[str]]:
    """某个 locale 里每个键的占位符名(供两份对照)."""
    return {
        key: set(_PLACEHOLDER.findall(text)) for key, text in _catalog(locale).items()
    }


@pytest.mark.parametrize(("locale", "other"), [("zh-CN", "en"), ("en", "zh-CN")])
def test_both_locales_agree_on_placeholder_names(locale: str, other: str) -> None:
    """中英两份的占位符名必须一致: 不然后一种语言格式化时会直接少一块."""
    mine = _placeholder_names(locale)
    theirs = _placeholder_names(other)
    problems = [
        f"{key}: {locale}={sorted(names)} 而 {other}={sorted(theirs.get(key, set()))}"
        for key, names in sorted(mine.items())
        if names != theirs.get(key, set())
    ]
    hint = f"两份文案的占位符名对不上 {len(problems)} 处:\n" + "\n".join(problems)
    assert not problems, hint
