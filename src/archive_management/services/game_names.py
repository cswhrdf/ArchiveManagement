r"""游戏名称的本地化: 按当前界面语言向平台问一次译文.

Steam 的商店接口是公开、无凭据的: ``store.steampowered.com/api/appdetails`` 带上
``l=<语言>`` 就返回该语言下的名称。拿不到(离线、限流、该语言没有译文、结构不符)时
一律返回 ``None``, 由调用方保留**探测到的原名** —— 不猜、不翻、不拼。

结果按 ``<AppID>:<locale>`` 记在应用缓存目录的单行紧凑 JSON 里: 只给程序读, 因此
不写缩进、换行与分隔空格; 名称里的空白也会归一(换行/制表符/连续空格压成单个半角
空格), 免得商店返回的排版空白进入界面。切换语言时只对真正缺条目的游戏联网, 重启
后与离线时都能复用上一次的结果。缓存属于可随时删除的派生数据。

界面 locale(``zh-CN``/``en``)与商店语言代码(``schinese``/``english``)是两回事,
映射表放在 :data:`_STORE_LANGUAGES`; 没有映射的语言不探测, 直接走原名回落。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol

import httpx

from archive_management.i18n import DEFAULT_LOCALE

logger = logging.getLogger(__name__)

STORE_API = "https://store.steampowered.com/api/appdetails"
DEFAULT_TIMEOUT = 5.0
# 商店响应里我们只要一个名字, 但同一份响应还带描述等字段: 给一个宽松的上限兜底.
DEFAULT_MAX_BYTES = 512 * 1024
# 缓存文件名(位于 cache_dir 之下); 内容是扁平的 ``<AppID>:<locale> -> 名称``.
NAME_CACHE_FILENAME = "game-names.json"

# 界面 locale → Steam 商店语言代码; 缺映射的语言(如繁体以外的其它语言)不探测.
_STORE_LANGUAGES: Mapping[str, str] = {
    "zh-CN": "schinese",
    "zh-TW": "tchinese",
    "en": "english",
    "ja": "japanese",
}


def store_language(locale: str) -> str | None:
    """把界面 locale 映射为商店语言代码; 没有对应语言时返回 ``None``."""
    return _STORE_LANGUAGES.get(locale)


def tidy_name(raw: str) -> str:
    """把名称里的空白归一: 换行/制表符/连续空格都压成单个半角空格.

    商店返回的名称可能带着为排版服务的空白(换行、制表符、双空格), 这些字符直接
    进界面只会把行撑开; 单词之间的单个空格是名称的一部分, 保留。
    """
    return " ".join(raw.split())


def parse_app_name(payload: object, app_id: str) -> str | None:
    """从 appdetails 响应里取出该 AppID 的名称; 结构不符或没有译名时返回 ``None``.

    响应形如 ``{"<appid>": {"success": true, "data": {"name": "...", ...}}}``;
    ``success`` 为 false(该 AppID 没有商店页)或没有 ``data`` 时都视为"没有译名"。
    """
    if not isinstance(payload, dict):
        return None
    entry = payload.get(app_id)
    if not isinstance(entry, dict) or not entry.get("success"):
        return None
    data = entry.get("data")
    if not isinstance(data, dict):
        return None
    name = data.get("name")
    if not isinstance(name, str):
        return None
    cleaned = tidy_name(name)
    return cleaned or None


class NameFetcher(Protocol):
    """能按语言取一次游戏名称的来源(协议化以便用例注入替身)."""

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """返回该 AppID 在该语言下的名称; 取不到时返回 ``None``."""
        ...


class HttpNameFetcher:
    """用公开商店接口取名的默认实现(httpx, 可注入客户端).

    与封面下载同一套纪律: 强制超时、限制响应体积、只在边界上降级(失败返回 None,
    绝不把网络异常抛给调用方)。
    """

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        """绑定 HTTP 客户端; 省略时按需创建一个."""
        self._client = client

    def fetch(
        self, app_id: str, *, language: str, timeout: float, max_bytes: int
    ) -> str | None:
        """向商店接口要一次名称; 失败只记 DEBUG 日志并返回 None."""
        params: dict[str, str] = {
            "appids": app_id,
            "l": language,
            "filters": "basic",
        }
        try:
            client = self._client if self._client is not None else httpx.Client()
            response = client.get(STORE_API, params=params, timeout=timeout)
            response.raise_for_status()
            if len(response.content) > max_bytes:
                logger.debug("商店响应超过体积上限, 已忽略: %s", app_id)
                return None
            payload: Any = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.debug("取游戏译名失败(%s): %s", app_id, exc)
            return None
        return parse_app_name(payload, app_id)


class NameCache:
    """按 ``<AppID>:<locale>`` 记住译名的 JSON 缓存(可随时删除).

    文件损坏或内容不符时当作空缓存: 这是可再生的派生数据, 不能让坏文件把功能卡死。
    """

    def __init__(self, path: Path) -> None:
        """绑定缓存文件路径(文件不存在时按空缓存处理)."""
        self._path = path
        self._entries: dict[str, str] | None = None

    @staticmethod
    def _key(app_id: str, locale: str) -> str:
        """缓存键: AppID 与语言一起定位一条译名."""
        return f"{app_id}:{locale}"

    def _load(self) -> dict[str, str]:
        """读取缓存文件(只读一次, 之后走内存).

        旧版本写下的条目可能带着排版空白: 这里顺手归一, 避免历史缓存把空白带回
        界面(下一次写入整份文件时就是干净的了)。
        """
        if self._entries is not None:
            return self._entries
        entries: dict[str, str] = {}
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = None
        if isinstance(raw, dict):
            entries = {
                key: tidy_name(value)
                for key, value in raw.items()
                if isinstance(key, str) and isinstance(value, str)
            }
        self._entries = entries
        return entries

    def get(self, app_id: str, locale: str) -> str | None:
        """取缓存的译名; 没有条目时返回 ``None``."""
        return self._load().get(self._key(app_id, locale))

    def put(self, app_id: str, locale: str, name: str) -> None:
        """写入一条译名并落盘(写不进去只记日志)."""
        entries = self._load()
        entries[self._key(app_id, locale)] = name
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_name(f"{self._path.name}.tmp")
            temporary.write_text(_dump(entries), encoding="utf-8")
            temporary.replace(self._path)
        except OSError as exc:
            logger.debug("写译名缓存失败: %s", exc)

    def forget(self, app_id: str) -> None:
        """删掉某个 AppID 的全部译名(游戏被删除时调用)."""
        entries = self._load()
        removed = [key for key in entries if key.split(":")[0] == app_id]
        if not removed:
            return
        for key in removed:
            entries.pop(key, None)
        try:
            self._path.write_text(_dump(entries), encoding="utf-8")
        except OSError as exc:  # pragma: no cover - 磁盘不可写时不清也不影响使用
            logger.debug("清理译名缓存失败: %s", exc)


def _dump(entries: dict[str, str]) -> str:
    """紧凑 JSON: 缓存只给程序读, 因此不写缩进、换行与多余空格."""
    return json.dumps(entries, ensure_ascii=False, separators=(",", ":"))


def name_cache_at(cache_dir: Path) -> NameCache:
    """返回给定缓存目录下的译名缓存(便于只持有 ``cache_dir`` 的调用方)."""
    return NameCache(cache_dir / NAME_CACHE_FILENAME)


def resolve_name(
    app_id: str,
    locale: str = DEFAULT_LOCALE,
    cache: NameCache | None = None,
    *,
    fetcher: NameFetcher | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
    refresh: bool = False,
) -> str | None:
    """取该 AppID 在给定语言下的名称; 取不到时返回 ``None``(调用方保留原名).

    ``cache`` 命中就直接返回(不再联网) —— 缓存按语言分条, 所以切换语言只会命中
    新语言自己的记录; 只有 ``refresh=True`` 才忽略缓存重取一次(诊断用)。
    """
    if cache is not None and not refresh:
        cached = cache.get(app_id, locale)
        if cached is not None:
            return cached
    language = store_language(locale)
    if language is None:
        return None
    active = HttpNameFetcher() if fetcher is None else fetcher
    found = active.fetch(
        app_id, language=language, timeout=timeout, max_bytes=max_bytes
    )
    if found is None:
        return None
    if cache is not None:
        cache.put(app_id, locale, found)
    return found
