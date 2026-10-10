"""Steam 官方图标(appinfo.vdf 的 clienticon)读取用例.

这份文件是 Steam 的私有缓存格式, 所以用例的重点不在"能读出哈希", 而在**读不出
来时绝不出错**: 版本不认识、字符串表截断、条目长度越界、值不是哈希、该游戏不在
文件里 —— 每一种都返回空结果, 由界面回落封面裁剪与占位图。另外锁住"只认自己那个
键序号": 同一个条目里还有 clienttga 这类同样长度的哈希, 不能抓错。

``tests/helpers.appinfo_bytes`` 按真实结构造文件(头 + 条目 + 末尾字符串表), 因此
用例不依赖本机装没装 Steam。
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Any

import pytest

import helpers
from archive_management.services import steam_appinfo
from archive_management.services.steam_appinfo import (
    APPINFO_RELATIVE,
    GAME_ICONS_RELATIVE,
    icon_path,
    load_steam_icons,
    read_clienticons,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("封面与图标"),
    pytest.mark.story("官方图标来源"),
    pytest.mark.layer("unit"),
]

_ICON = "b2f863a4c63bc1c5667a8a7e3e9355ef260ce6d2"
_OTHER = "4e033a9c80e92f33552059abfa91dbe23af40267"
_KEYS = (*helpers.STEAM_APPINFO_KEYS, "clienttga")


def _write(tmp_path: Path, entries: dict[str, dict[str, str]], **kwargs: Any) -> Path:
    """在临时目录里造一份 appinfo.vdf 并返回路径."""
    path = tmp_path / APPINFO_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(helpers.appinfo_bytes(entries, keys=_KEYS, **kwargs))
    return path


def _entries() -> dict[str, dict[str, str]]:
    """两款游戏: 第一款带图标与一个同为 40 位十六进制的干扰字段."""
    return {
        "3273290": {"common": "", "name": "Neptunia", "clienticon": _ICON},
        "1366540": {"name": "Dyson", "clienticon": _OTHER, "clienttga": _ICON},
    }


def test_reads_the_clienticon_of_every_app(tmp_path: Path) -> None:
    """按 AppID 取出各自的图标哈希, 不会被同长度的 clienttga 带偏."""
    table = read_clienticons(_write(tmp_path, _entries()))

    assert table == {"3273290": _ICON, "1366540": _OTHER}


def test_missing_file_returns_nothing(tmp_path: Path) -> None:
    """Steam 没装、文件被删掉: 当作没有官方图标, 不抛异常."""
    assert read_clienticons(tmp_path / "nowhere" / "appinfo.vdf") == {}


def test_unknown_version_returns_nothing(tmp_path: Path) -> None:
    """版本不认识(旧格式内联键名)时放弃解析, 而不是按新格式硬读."""
    path = _write(tmp_path, _entries(), magic=0x07564428)

    assert read_clienticons(path) == {}


def test_broken_string_table_returns_nothing(tmp_path: Path) -> None:
    """字符串表偏移越界(文件截断)时返回空结果."""
    path = _write(tmp_path, _entries(), table_offset=1 << 30)

    assert read_clienticons(path) == {}


def test_string_table_without_the_icon_key_returns_nothing(tmp_path: Path) -> None:
    """字符串表里没有 ``clienticon`` 这个键名时不做任何猜测."""
    keys = tuple(key for key in _KEYS if key != "clienticon")
    path = tmp_path / APPINFO_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(
        helpers.appinfo_bytes({"1": {"name": "Demo", "clienttga": _ICON}}, keys=keys)
    )

    assert read_clienticons(path) == {}


def test_app_not_in_the_file_returns_nothing(tmp_path: Path) -> None:
    """文件里没有这款游戏(没装、也不在已缓存的清单里)."""
    table = read_clienticons(_write(tmp_path, _entries()))

    assert "730" not in table


def test_a_value_that_is_not_a_hash_is_ignored(tmp_path: Path) -> None:
    """值不是 40 位十六进制时视为没有图标, 不把半截字符串当哈希."""
    entries = {"1": {"name": "Demo", "clienticon": "not-a-hash"}}

    assert read_clienticons(_write(tmp_path, entries)) == {}


def test_truncated_entries_only_lose_the_later_apps(tmp_path: Path) -> None:
    """条目长度越界时到此为止: 前面已经读到的游戏仍然算数."""
    good = helpers.appinfo_bytes(_entries(), keys=_KEYS)
    table_start = struct.unpack_from("<Q", good, 8)[0]
    # 在条目区末尾插一条声称 1 MiB 的条目, 并把字符串表偏移同步往后挪.
    data = bytearray(good)
    struct.pack_into("<Q", data, 8, table_start + 8)
    data[table_start:table_start] = struct.pack("<II", 999, 1 << 20)
    path = tmp_path / APPINFO_RELATIVE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(bytes(data))

    table = read_clienticons(path)

    assert table == {"3273290": _ICON, "1366540": _OTHER}
    assert "999" not in table


def test_changed_file_is_read_again(tmp_path: Path) -> None:
    """Steam 换图标后要读出新哈希: 缓存按"路径 + 大小 + 修改时间"失效."""
    path = _write(tmp_path, {"1": {"clienticon": _ICON}})
    assert read_clienticons(path) == {"1": _ICON}

    # 第二份文件多一条游戏(大小跟着变), 缓存必须重新解析.
    path.write_bytes(
        helpers.appinfo_bytes(
            {"1": {"clienticon": _OTHER}, "2": {"name": "Extra"}}, keys=_KEYS
        )
    )

    assert read_clienticons(path) == {"1": _OTHER}


def test_icon_path_points_into_the_steam_icon_directory(tmp_path: Path) -> None:
    """本机图标位置是 ``steam/games/<哈希>.ico``(是否存在由调用方判断)."""
    root = tmp_path / "Steam"

    assert icon_path(root, _ICON) == root / GAME_ICONS_RELATIVE / f"{_ICON}.ico"


def test_load_steam_icons_finds_local_files(tmp_path: Path) -> None:
    """汇总入口: 本机已有图标文件时给出路径, 没有时只给哈希(走 CDN)."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_appinfo(steam, _entries(), keys=_KEYS)
    local = helpers.write_steam_icon(steam, _ICON)

    icons = load_steam_icons(helpers.scan_roots(tmp_path))

    assert icons["3273290"].clienticon == _ICON
    assert icons["3273290"].local_path == local
    # 第二款游戏的图标文件不在本机: 哈希仍然可用, 交给 CDN 兜底.
    assert icons["1366540"].local_path is None


def test_load_steam_icons_without_steam_returns_nothing(tmp_path: Path) -> None:
    """本机没有 Steam 时返回空表, 界面回落封面裁剪."""
    assert load_steam_icons(helpers.scan_roots(tmp_path)) == {}


# -- 多份 appinfo 与解析细节 ---------------------------------------------------


def test_the_first_root_wins_for_the_same_app(tmp_path: Path) -> None:
    """同一个 AppID 出现在两份 appinfo 里时先找到的那份生效(互为镜像)."""
    first = helpers.steam_tree(tmp_path)
    second = tmp_path / "Program Files" / "Steam"
    (second / "steamapps").mkdir(parents=True)
    helpers.write_steam_appinfo(first, {"730": {"clienticon": _ICON}}, keys=_KEYS)
    helpers.write_steam_appinfo(second, {"730": {"clienticon": _OTHER}}, keys=_KEYS)

    icons = load_steam_icons(helpers.scan_roots(tmp_path))

    assert icons["730"].clienticon == _ICON


def test_read_cached_does_not_cache_an_empty_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """解析不出任何图标时不留下缓存条目(不把"空"当成结果记住)."""
    monkeypatch.setattr(steam_appinfo, "_cache", {})
    empty = _write(tmp_path, {"730": {"name": "No icon here"}})

    assert steam_appinfo._read_cached(empty) == {}
    assert steam_appinfo._cache == {}


def test_read_cached_reuses_the_parsed_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """命中缓存时不再解析文件(Steam 更新让大小/时间戳变了才会重解析).

    缓存命中那两行在 2026-10-10 的覆盖率报告里是三平台共同的缺口 —— 之前只有
    "空表不入缓存"与"到上限清空"两条, 谁都没连着读两次同一份文件。
    """
    monkeypatch.setattr(steam_appinfo, "_cache", {})
    appinfo = _write(tmp_path, _entries())
    parsed = {"calls": 0}
    original = steam_appinfo.read_clienticons

    def counting(path: Path) -> dict[str, str]:
        parsed["calls"] += 1
        return original(path)

    monkeypatch.setattr(steam_appinfo, "read_clienticons", counting)

    first = steam_appinfo._read_cached(appinfo)
    second = steam_appinfo._read_cached(appinfo)

    assert first, "第一遍就该读出图标"
    assert first == second, "两次读到的该是同一份解析结果"
    assert parsed["calls"] == 1, "第二次应当直接命中缓存, 不再解析文件"


def test_read_cached_clears_the_cache_at_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """缓存到上限时整体清空再写入(不无界增长)."""
    monkeypatch.setattr(steam_appinfo, "_cache", {})
    appinfo = _write(tmp_path, _entries())
    for index in range(steam_appinfo._CACHE_LIMIT):
        steam_appinfo._cache[(f"key-{index}", 0, 0)] = {}

    table = steam_appinfo._read_cached(appinfo)

    assert table["3273290"] == _ICON
    assert len(steam_appinfo._cache) == 1


def test_parse_rejects_data_shorter_than_the_header(tmp_path: Path) -> None:
    """文件连文件头都不够时视为没有图标(不越界读)."""
    assert steam_appinfo._parse(b"short", tmp_path / APPINFO_RELATIVE) == {}


def test_string_table_requires_a_terminator() -> None:
    """字符串表里的键名没有结束符时返回 None(整份数据不完整)."""
    table = struct.pack("<I", 2) + b"name" + b"tail-without-a-terminator"
    data = struct.pack("<IIQ", helpers.STEAM_APPINFO_MAGIC, 1, 16) + table

    assert steam_appinfo._string_table(data, 16) is None


def test_clienticon_requires_a_terminated_hash() -> None:
    """匹配到的值后面没有结束符时不当成图标(宁可不给, 也不截半截哈希)."""
    pattern = bytes([1]) + struct.pack("<I", 3)

    assert steam_appinfo._clienticon(pattern + b"abc", pattern) is None
