"""轻量 i18n: 界面文案从配置资源加载.

文案资源放在 ``resources/i18n/<locale>.json``(例如 ``zh-CN``、``en``),
默认中文。本模块只依赖标准库,业务层不应直接依赖展示语言;文案查询在
界面/展示层进行,便于后续接入运行期切换语言。
"""

from __future__ import annotations

import json
from importlib.resources import files

_PACKAGE = "archive_management"
_RESOURCE_DIR = ("resources", "i18n")
DEFAULT_LOCALE = "zh-CN"

_catalog: dict[str, str] = {}
_locale: str = DEFAULT_LOCALE


def _read(locale: str) -> dict[str, str]:
    """读取某个 locale 的文案资源为扁平的键值字典."""
    base = files(_PACKAGE).joinpath(*_RESOURCE_DIR)
    raw = json.loads(base.joinpath(f"{locale}.json").read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError(f"i18n 资源不是对象: {locale}")
    result: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise ValueError(f"i18n 资源包含非字符串键值: {locale}")
        result[key] = value
    return result


def available_locales() -> tuple[str, ...]:
    """返回资源目录中可用的 locale 列表."""
    base = files(_PACKAGE).joinpath(*_RESOURCE_DIR)
    locales: list[str] = []
    for entry in base.iterdir():
        if entry.name.endswith(".json"):
            locales.append(entry.name[: -len(".json")])
    return tuple(sorted(locales))


def current_locale() -> str:
    """返回当前生效的 locale 名."""
    return _locale


def set_locale(locale: str) -> str:
    """切换当前 locale, 返回生效名; 不支持的 locale 抛错."""
    global _catalog
    global _locale
    if locale not in available_locales():
        raise ValueError(f"不支持的 locale: {locale}")
    _locale = locale
    _catalog = _read(locale)
    return _locale


def tr(key: str, **kwargs: object) -> str:
    """按当前 locale 取文案; 支持 ``str.format`` 风格的占位符."""
    text = _catalog.get(key)
    if text is None:
        return key
    if not kwargs:
        return text
    try:
        return text.format(**kwargs)
    except (KeyError, IndexError, ValueError):
        return text


set_locale(DEFAULT_LOCALE)
