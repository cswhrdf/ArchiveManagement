"""i18n 文案加载与格式化的单元测试."""

from __future__ import annotations

import json
from importlib.resources import files

from archive_management.i18n import available_locales, current_locale, tr


def _keys(locale: str) -> set[str]:
    base = files("archive_management").joinpath("resources", "i18n")
    raw = json.loads(base.joinpath(f"{locale}.json").read_text(encoding="utf-8"))
    return set(raw)


def test_locales_available() -> None:
    assert "zh-CN" in available_locales()
    assert "en" in available_locales()


def test_default_locale_is_chinese() -> None:
    assert current_locale() == "zh-CN"
    assert tr("sidebar.my_games") == "我的游戏"


def test_tr_formats_placeholders() -> None:
    assert tr("game.locations_detail", count=3, backups=5) == "3 个存档位置 · 5 个备份"


def test_tr_missing_key_returns_key() -> None:
    assert tr("no.such.key") == "no.such.key"


def test_zh_and_en_catalogs_have_same_keys() -> None:
    assert _keys("zh-CN") == _keys("en")
