"""i18n 文案加载与格式化的单元测试."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from archive_management import i18n as i18n_module
from archive_management.i18n import available_locales, current_locale, tr

pytestmark = [
    pytest.mark.i18n,
    pytest.mark.critical,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("国际化"),
    pytest.mark.story("中英文文案与格式化"),
    pytest.mark.layer("unit"),
]


def _keys(locale: str) -> set[str]:
    base = files("archive_management").joinpath("resources", "i18n")
    raw = json.loads(base.joinpath(f"{locale}.json").read_text(encoding="utf-8"))
    return set(raw)


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
