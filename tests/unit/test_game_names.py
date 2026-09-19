"""游戏译名探测的单元测试(语言映射、响应解析、缓存与回落)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from archive_management.services.game_names import (
    NAME_CACHE_FILENAME,
    NameCache,
    name_cache_at,
    parse_app_name,
    resolve_name,
    store_language,
    tidy_name,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("游戏译名"),
    pytest.mark.story("按语言探测游戏名称"),
    pytest.mark.layer("unit"),
]

_APP_ID = "937310"
# 商店接口的真实响应形状(只保留我们读的字段).
_PAYLOAD = {
    _APP_ID: {
        "success": True,
        "data": {"type": "game", "name": "无尽塔防 2", "steam_appid": 937310},
    }
}


class _StubFetcher:
    """固定返回一份译名的替身, 并记录每次调用的参数."""

    def __init__(self, name: str | None) -> None:
        """绑定要返回的名称(``None`` 表示取不到)."""
        self._name = name
        self.calls: list[tuple[str, str]] = []

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """记录调用并返回预设结果."""
        del timeout, max_bytes
        self.calls.append((app_id, language))
        return self._name


class _StubResponse:
    """最小可用的 httpx 响应替身."""

    def __init__(self, payload: object) -> None:
        """记住要返回的 JSON 内容."""
        self.content = json.dumps(payload).encode("utf-8")

    def raise_for_status(self) -> None:
        """替身永远成功."""
        return

    def json(self) -> object:
        """返回构造时给的对象."""
        return _PAYLOAD


class _StubClient:
    """最小可用的 httpx.Client 替身: 记录参数并返回预设响应."""

    def __init__(self, payload: object = _PAYLOAD) -> None:
        """绑定要返回的响应体."""
        self.payload = payload
        self.calls: list[dict[str, str]] = []

    def get(self, url: str, *, params: dict[str, str], timeout: float) -> _StubResponse:
        """记录请求参数并返回响应."""
        del url, timeout
        self.calls.append(dict(params))
        return _StubResponse(self.payload)


def test_store_language_maps_known_locales_only() -> None:
    """只有认识的语言才去问商店: 其余语言直接回落原名."""
    assert store_language("zh-CN") == "schinese"
    assert store_language("en") == "english"
    assert store_language("fr") is None


def test_parse_app_name_reads_the_localized_name() -> None:
    assert parse_app_name(_PAYLOAD, _APP_ID) == "无尽塔防 2"


def test_parse_app_name_collapses_readability_whitespace() -> None:
    """商店给的排版空白(换行/制表符/双空格)不进缓存, 单词间的单个空格保留."""
    payload = {
        _APP_ID: {
            "success": True,
            "data": {"name": "  Deep\tRock\n\nGalactic  ", "type": "game"},
        }
    }

    assert parse_app_name(payload, _APP_ID) == "Deep Rock Galactic"
    assert tidy_name("无尽塔防\r\n 2 ") == "无尽塔防 2"
    assert tidy_name("   ") == ""


def test_parse_app_name_rejects_unusable_payloads() -> None:
    """没有商店页/结构不符/名字为空都不算译名, 由调用方保留探测到的原名."""
    assert parse_app_name({_APP_ID: {"success": False}}, _APP_ID) is None
    assert parse_app_name({_APP_ID: {"success": True}}, _APP_ID) is None
    assert parse_app_name({_APP_ID: {"success": True, "data": {}}}, _APP_ID) is None
    assert (
        parse_app_name({_APP_ID: {"success": True, "data": {"name": "  "}}}, _APP_ID)
        is None
    )
    assert parse_app_name([], _APP_ID) is None
    assert parse_app_name(_PAYLOAD, "999") is None


def test_name_cache_round_trips_and_is_per_locale(tmp_path: Path) -> None:
    """缓存按 AppID + 语言分条: 换语言会各自记一份, 互不覆盖."""
    cache = name_cache_at(tmp_path)

    assert cache.get(_APP_ID, "zh-CN") is None
    cache.put(_APP_ID, "zh-CN", "无尽塔防 2")
    cache.put(_APP_ID, "en", "Infinitode 2")

    assert cache.get(_APP_ID, "zh-CN") == "无尽塔防 2"
    assert cache.get(_APP_ID, "en") == "Infinitode 2"
    # 缓存只给程序读: 单行紧凑 JSON, 没有缩进、分隔空格与收尾换行.
    raw = (tmp_path / NAME_CACHE_FILENAME).read_text(encoding="utf-8")
    assert raw == '{"937310:zh-CN":"无尽塔防 2","937310:en":"Infinitode 2"}'


def test_name_cache_rewrites_a_pretty_file_compactly(tmp_path: Path) -> None:
    """旧版本写下的可读格式(缩进 + 换行)在下次写入时被整份改回单行."""
    target = tmp_path / NAME_CACHE_FILENAME
    target.write_text('{\n  "937310:zh-CN": "无尽塔防 2"\n}\n', encoding="utf-8")

    cache = NameCache(target)
    cache.put("730", "zh-CN", "反恐精英")

    raw = target.read_text(encoding="utf-8")
    assert "\n" not in raw
    assert ": " not in raw
    assert raw == '{"937310:zh-CN":"无尽塔防 2","730:zh-CN":"反恐精英"}'


def test_name_cache_cleans_whitespace_from_older_entries(tmp_path: Path) -> None:
    """历史条目里带着的排版空白在读出时就被归一."""
    (tmp_path / NAME_CACHE_FILENAME).write_text(
        '{"937310:zh-CN":"无尽塔防\\n 2"}', encoding="utf-8"
    )

    assert name_cache_at(tmp_path).get("937310", "zh-CN") == "无尽塔防 2"


def test_name_cache_survives_a_corrupt_file(tmp_path: Path) -> None:
    """缓存文件坏了当作空缓存: 派生数据不能让功能卡死."""
    (tmp_path / NAME_CACHE_FILENAME).write_text("{ not json", encoding="utf-8")

    assert name_cache_at(tmp_path).get(_APP_ID, "zh-CN") is None


def test_name_cache_forget_removes_one_game(tmp_path: Path) -> None:
    """删游戏时清掉它的译名, 别的游戏不受影响."""
    cache = NameCache(tmp_path / NAME_CACHE_FILENAME)
    cache.put(_APP_ID, "zh-CN", "无尽塔防 2")
    cache.put("730", "zh-CN", "反恐精英")

    cache.forget(_APP_ID)

    assert cache.get(_APP_ID, "zh-CN") is None
    assert cache.get("730", "zh-CN") == "反恐精英"
    # 再删一次是幂等的.
    cache.forget(_APP_ID)
    assert cache.get("730", "zh-CN") == "反恐精英"


def test_resolve_name_prefers_the_cache(tmp_path: Path) -> None:
    """缓存命中不再联网(语言切换时只对缺条目的游戏发请求)."""
    cache = name_cache_at(tmp_path)
    cache.put(_APP_ID, "zh-CN", "无尽塔防 2")
    fetcher = _StubFetcher("不该被调用")

    found = resolve_name(_APP_ID, "zh-CN", cache, fetcher=fetcher)

    assert found == "无尽塔防 2"
    assert fetcher.calls == []


def test_resolve_name_fetches_and_caches(tmp_path: Path) -> None:
    """没缓存时问一次商店, 拿到就写进缓存(下次不再联网)."""
    cache = name_cache_at(tmp_path)
    fetcher = _StubFetcher("无尽塔防 2")

    found = resolve_name(_APP_ID, "zh-CN", cache, fetcher=fetcher)

    assert found == "无尽塔防 2"
    assert fetcher.calls == [(_APP_ID, "schinese")]
    assert cache.get(_APP_ID, "zh-CN") == "无尽塔防 2"


def test_resolve_name_refresh_ignores_the_cache(tmp_path: Path) -> None:
    """``refresh=True`` 时忽略缓存重取一次(切换语言就是靠它)."""
    cache = name_cache_at(tmp_path)
    cache.put(_APP_ID, "zh-CN", "旧译名")
    fetcher = _StubFetcher("新译名")

    found = resolve_name(_APP_ID, "zh-CN", cache, fetcher=fetcher, refresh=True)

    assert found == "新译名"
    assert cache.get(_APP_ID, "zh-CN") == "新译名"


def test_resolve_name_returns_none_without_a_translation(tmp_path: Path) -> None:
    """取不到译名(或语言没有映射)时返回 None, 由调用方保留探测到的原名."""
    cache = name_cache_at(tmp_path)

    assert resolve_name(_APP_ID, "zh-CN", cache, fetcher=_StubFetcher(None)) is None
    assert resolve_name(_APP_ID, "fr", cache, fetcher=_StubFetcher("Nom")) is None


def test_http_fetcher_asks_the_store_api_with_the_language() -> None:
    """默认实现按 ``l=<商店语言>`` 请求, 并从响应里取出名称."""
    from archive_management.services.game_names import HttpNameFetcher

    client: Any = _StubClient()

    name = HttpNameFetcher(client=client).fetch(
        _APP_ID, language="schinese", timeout=1.0, max_bytes=1024
    )

    assert name == "无尽塔防 2"
    assert client.calls == [{"appids": _APP_ID, "l": "schinese", "filters": "basic"}]
