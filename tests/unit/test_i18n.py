"""i18n 文案加载与格式化的单元测试."""

from __future__ import annotations

import json
import re
from importlib.resources import files

import pytest

from archive_management import i18n as i18n_module
from archive_management.i18n import available_locales, current_locale, tr

pytestmark = [
    pytest.mark.i18n,
    pytest.mark.minor,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("国际化"),
    pytest.mark.story("中英文文案与格式化"),
    pytest.mark.layer("unit"),
]


def _catalog(locale: str) -> dict[str, str]:
    base = files("archive_management").joinpath("resources", "i18n")
    raw: dict[str, str] = json.loads(
        base.joinpath(f"{locale}.json").read_text(encoding="utf-8")
    )
    return raw


def _keys(locale: str) -> set[str]:
    return set(_catalog(locale))


def test_locales_available() -> None:
    assert "zh-CN" in available_locales()
    assert "en" in available_locales()


def test_default_locale_is_chinese() -> None:
    assert current_locale() == "zh-CN"
    assert tr("topbar.add_game") == "+ 添加游戏"


def test_tr_formats_placeholders() -> None:
    assert tr("game.locations_detail", count=3, backups=5) == "3 个存档位置 · 5 个备份"


def test_tr_missing_key_returns_key() -> None:
    assert tr("no.such.key") == "no.such.key"


def test_zh_and_en_catalogs_have_same_keys() -> None:
    assert _keys("zh-CN") == _keys("en")


def test_set_locale_unsupported_raises_and_keeps_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(i18n_module, "available_locales", lambda: ("zh-CN",))
    with pytest.raises(ValueError):
        i18n_module.set_locale("en")
    assert i18n_module.current_locale() == "zh-CN"


def test_tr_returns_raw_text_when_placeholder_missing() -> None:
    raw = tr("game.locations_detail", count=3)
    assert "{backups}" in raw


def test_read_raises_when_resource_is_not_object(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        json,
        "loads",
        lambda *_a, **_k: ["not", "dict"],
    )
    with pytest.raises(ValueError):
        i18n_module._read("zh-CN")


def test_read_raises_on_non_string_value(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        json,
        "loads",
        lambda *_a, **_k: {"key": 1},
    )
    with pytest.raises(ValueError):
        i18n_module._read("zh-CN")


# ---- 中文文案的标点统一用全角 -----------------------------------------
#
# 判据(与那轮转换用的规则一致): **紧跟中文的半角 ``,;:`` 一律改全角**, 且全角标点后面不再
# 留空格(全角自带间距)。照旧用半角的是 ``1,000`` / ``12:30`` / ``C:\Users`` 这类 —— 它们的
# 左边是 ASCII, 属于英文/路径/数字里的标点, 不算"中文句内"。
#
# 正则里写码位(``\uff0c``)而不是直接写那几个全角字符: 仓库的 ruff 规则 RUF001 禁止在 .py
# 里出现全角标点 —— 它的本意正是防住"看着像逗号, 其实不是"的字符混进代码, 所以这里
# 不给自己开例外, 改文案时只动 JSON。
_CHINESE_THEN_HALF_WIDTH = re.compile(r"[\u4e00-\u9fff\u300d\u201d][,;:]")
_FULL_WIDTH_THEN_SPACE = re.compile(r"[\uff0c\uff1b\uff1a] ")
_ANY_FULL_WIDTH = re.compile(r"[\uff0c\uff1b\uff1a]")


def test_chinese_copy_uses_full_width_punctuation() -> None:
    """中文句内的 ``,;:`` 必须是全角: 半角跟在汉字后面是"断句错位"."""
    offenders = {
        key: value
        for key, value in _catalog("zh-CN").items()
        if _CHINESE_THEN_HALF_WIDTH.search(value)
    }

    assert offenders == {}


def test_no_space_follows_full_width_punctuation() -> None:
    """全角标点后面不留空格: 再空格就是"只换了一半"(界面上会豁开一道口子)."""
    offenders = {
        key: value
        for key, value in _catalog("zh-CN").items()
        if _FULL_WIDTH_THEN_SPACE.search(value)
    }

    assert offenders == {}


def test_english_copy_does_not_use_full_width_punctuation() -> None:
    """英文文案里出现全角标点通常是复制中文时串了语言(两套标点不混用)."""
    offenders = {
        key: value
        for key, value in _catalog("en").items()
        if _ANY_FULL_WIDTH.search(value)
    }

    assert offenders == {}
