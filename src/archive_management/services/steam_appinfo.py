"""Steam 官方游戏图标(``clienticon``)的读取.

Steam 自己那份方形小图标(桌面快捷方式/任务栏用的那张)只存在于客户端缓存里:
``appcache/appinfo.vdf`` 为每个 AppID 记下 ``clienticon`` 哈希, 图标本身是
``steam/games/<哈希>.ico``。公开接口都没有这个字段 —— 商店 ``appdetails`` 只有
header/capsule/background, ``appmanifest_*.acf`` 里也没有图标 —— 所以"用 Steam
官方图标而不是封面裁剪"必须解析这份文件。

**只做一件事**: 从字符串表里拿到 ``clienticon`` 的键序号, 再在**该 AppID 自己的
条目**里找 ``<字符串类型><键序号><40 位十六进制>`` 这个组合。不做通用二进制
KV 解析 —— 通用解析要跟着 Steam 的内部格式一路演进, 而这里只需要一个字段;
版本/结构认不出时**当作没有图标**, 记 DEBUG 日志, 由界面回落封面裁剪与占位图。

读到的哈希有两处可用, 顺序是"本机优先, CDN 兜底":

- 本机 ``steam/games/<哈希>.ico``(离线可用, 已安装/已缓存过的游戏通常都有);
- ``cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps/<appid>/<哈希>.ico``。
"""

from __future__ import annotations

import logging
import re
import struct
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from archive_management.services.platform_scan import ScanRoots, steam_roots

logger = logging.getLogger(__name__)

# Steam 主目录下的相对路径: 元数据文件与图标目录。
APPINFO_RELATIVE = Path("appcache") / "appinfo.vdf"
GAME_ICONS_RELATIVE = Path("steam") / "games"

# 图标哈希在 appinfo 里的键名。
CLIENT_ICON_KEY = "clienticon"

# appinfo.vdf 的魔数: 低 24 位是标识, 最低字节是格式版本。0x29 起键名不再内联,
# 而是放进文件末尾的字符串表、条目里只留序号 —— 旧版本(内联键名)直接放弃。
APPINFO_MAGIC = 0x07564429
APPINFO_VERSION = 0x29
# 文件头: 魔数 / universe / 字符串表偏移(8 字节)。
_HEADER_SIZE = 16
# 单条应用条目的头: appid / 该条目后面跟的字节数。
_ENTRY_HEADER = struct.Struct("<II")
# 条目里的字符串值: 类型字节(0x01 = 字符串) + 键序号 + 值。
_STRING_TYPE = 0x01
# 缓存上限: 一份 appinfo 解析一次就够, 但不要把多个 Steam 主目录的整表都留着。
_CACHE_LIMIT = 4
_HASH_PATTERN = re.compile(rb"[0-9a-f]{40}")
_cache: dict[tuple[str, int, int], dict[str, str]] = {}


@dataclass(frozen=True)
class SteamIcons:
    """一款 Steam 游戏官方图标的可用来源(本机文件与 CDN 哈希)."""

    clienticon: str
    local_path: Path | None


def icon_path(root: Path, clienticon: str) -> Path:
    """返回某款游戏在本机 Steam 目录下的图标文件路径(不判断是否存在)."""
    return root / GAME_ICONS_RELATIVE / f"{clienticon}.ico"


def read_clienticons(appinfo: Path) -> dict[str, str]:
    """读出 ``appinfo.vdf`` 里全部 AppID 的 ``clienticon`` 哈希.

    ``appinfo`` 不存在、版本不认识、结构被截断时返回空字典(记 DEBUG 日志):
    调用方据此认为"平台给不出官方图标", 而不是拿到半份错误的数据。
    """
    try:
        data = appinfo.read_bytes()
    except OSError as exc:
        logger.debug("读取 Steam appinfo 失败(%s): %s", appinfo, exc)
        return {}
    return _parse(data, appinfo)


def load_steam_icons(roots: ScanRoots) -> dict[str, SteamIcons]:
    """按 Steam 主目录汇总官方图标来源, 键是 AppID(字符串).

    同一个 AppID 在多份 appinfo 里出现时, 先找到的那份生效 —— 本机各个 Steam
    主目录的数据互为镜像, 不一致只可能来自更新时机不同, 与"用哪一份"无关。
    """
    found: dict[str, SteamIcons] = {}
    for root in steam_roots(roots):
        table = _read_cached(root / APPINFO_RELATIVE)
        for app_id, clienticon in table.items():
            if app_id in found:
                continue
            local = icon_path(root, clienticon)
            found[app_id] = SteamIcons(
                clienticon=clienticon,
                local_path=local if local.is_file() else None,
            )
    return found


def _read_cached(appinfo: Path) -> dict[str, str]:
    """按"路径 + 大小 + 修改时间"缓存解析结果(Steam 更新后自动失效)."""
    try:
        stat = appinfo.stat()
    except OSError:
        return {}
    key = (str(appinfo), stat.st_size, stat.st_mtime_ns)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    table = read_clienticons(appinfo)
    if table:
        if len(_cache) >= _CACHE_LIMIT:
            _cache.clear()
        _cache[key] = table
    return table


def _parse(data: bytes, source: Path) -> dict[str, str]:
    """解析文件字节, 返回 ``AppID -> clienticon``."""
    if len(data) < _HEADER_SIZE:
        logger.debug("Steam appinfo 过短, 视为没有图标: %s", source)
        return {}
    magic, _universe, table_offset = struct.unpack_from("<IIQ", data, 0)
    if magic != APPINFO_MAGIC:
        logger.debug(
            "Steam appinfo 版本不认识(0x%08x), 视为没有图标: %s", magic, source
        )
        return {}
    table = _string_table(data, table_offset)
    if table is None:
        logger.debug("Steam appinfo 字符串表不完整, 视为没有图标: %s", source)
        return {}
    try:
        index = table.index(CLIENT_ICON_KEY)
    except ValueError:
        logger.debug("Steam appinfo 字符串表里没有 %s: %s", CLIENT_ICON_KEY, source)
        return {}
    pattern = _STRING_TYPE.to_bytes(1, "little") + struct.pack("<I", index)
    icons: dict[str, str] = {}
    for app_id, blob in _entries(data, table_offset):
        found = _clienticon(blob, pattern)
        if found is not None:
            icons[str(app_id)] = found
    return icons


def _string_table(data: bytes, table_offset: int) -> list[str] | None:
    """读出字符串表里的键名; 偏移越界或条目超出文件时返回 ``None``."""
    if table_offset + 4 > len(data):
        return None
    (count,) = struct.unpack_from("<I", data, table_offset)
    names: list[str] = []
    position = table_offset + 4
    for _ in range(count):
        end = data.find(b"\0", position)
        if end < 0:
            return None
        names.append(data[position:end].decode("utf-8", "replace"))
        position = end + 1
    return names


def _entries(data: bytes, table_offset: int) -> Iterator[tuple[int, bytes]]:
    """逐个产出 ``(appid, 条目字节)``; 结构不完整时到此为止.

    条目区紧跟在文件头之后、字符串表之前, 每条是 ``appid`` + 长度 + 内容。遇到
    长度越界或 appid 0 就停下: 与其猜后面是什么, 不如少读几个游戏。
    """
    offset = _HEADER_SIZE
    while offset + _ENTRY_HEADER.size <= table_offset:
        app_id, size = _ENTRY_HEADER.unpack_from(data, offset)
        end = offset + _ENTRY_HEADER.size + size
        if app_id == 0 or end > len(data):
            return
        yield app_id, data[offset + _ENTRY_HEADER.size : end]
        offset = end


def _clienticon(blob: bytes, pattern: bytes) -> str | None:
    """在条目里找 ``clienticon`` 的值; 找不到或不是 40 位十六进制时返回 ``None``.

    值必须是**紧跟该键**的 40 位十六进制串(后面跟字符串结束符): 只认键序号与
    长度两个约束, 其它字段(哪怕是同样长度的哈希)都不会被误当成图标。
    """
    start = blob.find(pattern)
    if start < 0:
        return None
    value_start = start + len(pattern)
    end = blob.find(b"\0", value_start)
    if end < 0:
        return None
    value = blob[value_start:end]
    if _HASH_PATTERN.fullmatch(value) is None:
        return None
    return value.decode("ascii")
