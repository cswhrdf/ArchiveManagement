"""封面获取与缓存的单元测试(阶段 G-4).

覆盖七类场景: 缓存命中、本地命中、CDN 命中、超时、超限、内容类型不符、内容
损坏; 另加离线复用旧图、缓存清理与"缓存不落在备份目录里"的约定。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from archive_management.domain import ArtworkRef, PlatformGame
from archive_management.exceptions import ArtworkError
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.artwork import (
    ArtworkCache,
    FetchedArtwork,
    HttpArtworkFetcher,
    artwork_cache,
    resolve_artwork,
    sniff_media_type,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("封面与图标"),
    pytest.mark.story("CDN 下载与缓存"),
    pytest.mark.layer("unit"),
]

_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
_HTML = b"<html><body>not an image</body></html>"


class _FakeFetcher:
    """可控制返回内容/声明类型/失败原因的下载替身."""

    def __init__(
        self, *, content: bytes = _PNG, declared: str = "image/png", error: str = ""
    ) -> None:
        self._content = content
        self._declared = declared
        self._error = error
        self.calls: list[str] = []

    def fetch(self, url: str, *, timeout: float, max_bytes: int) -> FetchedArtwork:
        """记录调用并按配置返回或抛错."""
        self.calls.append(url)
        if self._error:
            raise ArtworkError(self._error)
        return FetchedArtwork(content=self._content, declared_media_type=self._declared)


def _cache(tmp_path: Path) -> ArtworkCache:
    return ArtworkCache(tmp_path / "cache" / "artwork")


def _game(
    *,
    local: Path | None = None,
    url: str = "https://cdn.example/steam/730/cover.jpg",
    version: str = "v2",
    app_id: str = "730",
) -> PlatformGame:
    """构造一个带封面引用的平台游戏."""
    artwork = [
        ArtworkRef(
            kind="cover",
            url=url,
            local_path="" if local is None else str(local),
            version=version,
        )
    ]
    return PlatformGame(platform="steam", game_id=app_id, name="Demo", artwork=artwork)


def test_sniff_media_type_recognises_supported_images() -> None:
    assert sniff_media_type(_PNG) == "image/png"
    assert sniff_media_type(_JPEG) == "image/jpeg"
    assert sniff_media_type(b"RIFF\x00\x00\x00\x00WEBPVP8 ") == "image/webp"
    assert sniff_media_type(_HTML) is None


def test_cached_artwork_is_reused_without_touching_the_network(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    cache.store("steam", "730", "cover", "v2", content=_PNG, extension="png")
    fetcher = _FakeFetcher()

    result = resolve_artwork(_game(), "cover", cache, fetcher=fetcher)

    assert result.source == "cache"
    assert result.found
    assert fetcher.calls == []


def test_local_file_wins_over_the_cdn(tmp_path: Path) -> None:
    local = tmp_path / "librarycache" / "730" / "header.jpg"
    local.parent.mkdir(parents=True)
    local.write_bytes(_JPEG)
    cache = _cache(tmp_path)
    fetcher = _FakeFetcher()

    result = resolve_artwork(_game(local=local), "cover", cache, fetcher=fetcher)

    assert result.source == "local"
    assert fetcher.calls == []
    assert result.path == cache.path_for("steam", "730", "cover", "v2", "jpg")


def test_downloaded_artwork_is_cached_with_a_descriptive_name(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    fetcher = _FakeFetcher(content=_JPEG, declared="image/jpeg")

    result = resolve_artwork(_game(), "cover", cache, fetcher=fetcher)

    assert result.source == "cdn"
    assert fetcher.calls == ["https://cdn.example/steam/730/cover.jpg"]
    assert result.path == cache.root / "steam" / "730" / "cover-v2.jpg"
    assert result.path is not None
    assert result.path.read_bytes() == _JPEG
    assert cache.lookup("steam", "730", "cover", "v2") is not None


def test_timeout_falls_back_to_the_cached_previous_version(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    older = cache.store("steam", "730", "cover", "v1", content=_PNG, extension="png")
    fetcher = _FakeFetcher(error="下载封面失败: timed out")

    result = resolve_artwork(_game(version="v2"), "cover", cache, fetcher=fetcher)

    assert result.source == "stale"
    assert result.path == older
    assert "timed out" in result.reason


def test_offline_without_any_cache_returns_nothing(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    fetcher = _FakeFetcher(error="下载封面失败: 无法连接")

    result = resolve_artwork(_game(), "cover", cache, fetcher=fetcher)

    assert result.source == "none"
    assert result.found is False
    assert result.reason
    assert not cache.root.exists()


def test_oversized_download_is_rejected(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    fetcher = _FakeFetcher()

    result = resolve_artwork(_game(), "cover", cache, fetcher=fetcher, max_bytes=8)

    assert result.source == "none"
    assert "体积上限" in result.reason
    assert cache.lookup("steam", "730", "cover", "v2") is None


def test_wrong_content_type_is_rejected(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    declared_html = _FakeFetcher(content=_HTML, declared="text/html")

    rejected = resolve_artwork(_game(), "cover", cache, fetcher=declared_html)

    assert rejected.source == "none"
    assert "不是可识别的图片" in rejected.reason
    lying = _FakeFetcher(content=_HTML, declared="image/png")
    assert resolve_artwork(_game(), "cover", cache, fetcher=lying).source == "none"


def test_corrupt_content_never_replaces_a_cached_file(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    good = cache.store("steam", "730", "cover", "v2", content=_PNG, extension="png")
    fetcher = _FakeFetcher(content=_HTML, declared="image/png")

    result = resolve_artwork(_game(version="v3"), "cover", cache, fetcher=fetcher)

    assert result.source == "stale"
    assert result.path == good
    assert good.read_bytes() == _PNG


def test_corrupt_cache_file_is_ignored(tmp_path: Path) -> None:
    """缓存内容坏掉时不再复用, 而是要重新下载."""
    cache = _cache(tmp_path)
    broken = cache.store(
        "steam", "730", "cover", "v2", content=b"garbage", extension="png"
    )
    fetcher = _FakeFetcher(content=_JPEG, declared="image/jpeg")

    result = resolve_artwork(_game(), "cover", cache, fetcher=fetcher)

    assert result.source == "cdn"
    assert result.path is not None
    assert result.path.read_bytes() == _JPEG
    # 旧文件坏掉后按新类型重写(扩展名随实际内容), 损坏的那份被清掉.
    assert result.path.parent == broken.parent
    assert broken.exists() is False


def test_prune_removes_only_expired_files(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    stale = cache.store("steam", "730", "cover", "v1", content=_PNG, extension="png")
    local = cache.store("steam", "999", "icon", "v1", content=_JPEG, extension="jpg")
    aged = stale.stat().st_mtime - 30 * 24 * 60 * 60
    os.utime(stale, (aged, aged))

    removed = cache.prune(max_age_days=7)

    assert removed == [stale]
    assert stale.exists() is False
    assert local.exists() is True


def test_cache_lives_outside_the_backup_root(tmp_path: Path) -> None:
    paths = ApplicationPaths.default(override_root=tmp_path / "app")
    cache = artwork_cache(paths)

    assert cache.root.parent == paths.cache_dir
    assert paths.backup_root != cache.root
    assert paths.backup_root not in cache.root.parents


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    """返回一个只走内存路由的 httpx 客户端(不发起真实请求)."""
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_http_fetcher_streams_bytes_and_the_declared_type() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, headers={"content-type": "image/png; charset=binary"}, content=_PNG
        )

    with _client(handler) as client:
        fetched = HttpArtworkFetcher(client=client).fetch(
            "https://cdn.example/cover.png", timeout=1.0, max_bytes=1024
        )

    assert fetched.content == _PNG
    assert fetched.declared_media_type == "image/png"


def test_http_fetcher_stops_reading_oversized_responses() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_PNG)

    with _client(handler) as client:
        fetcher = HttpArtworkFetcher(client=client)
        with pytest.raises(ArtworkError, match="体积上限"):
            fetcher.fetch("https://cdn.example/cover.png", timeout=1.0, max_bytes=4)


def test_http_fetcher_turns_http_errors_into_artwork_errors() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404)

    with _client(handler) as client:
        fetcher = HttpArtworkFetcher(client=client)
        with pytest.raises(ArtworkError, match="下载封面失败"):
            fetcher.fetch("https://cdn.example/cover.png", timeout=1.0, max_bytes=1024)


def test_resolution_survives_an_error_response(tmp_path: Path) -> None:
    """CDN 报错时仍然走"旧图复用"的降级路径."""
    cache = _cache(tmp_path)
    older = cache.store("steam", "730", "cover", "v1", content=_PNG, extension="png")

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with _client(handler) as client:
        result = resolve_artwork(
            _game(), "cover", cache, fetcher=HttpArtworkFetcher(client=client)
        )

    assert result.source == "stale"
    assert result.path == older


def test_external_identifiers_cannot_escape_the_cache_root(tmp_path: Path) -> None:
    cache = _cache(tmp_path)

    path = cache.path_for("../../etc", "../../..", "cover", "../../x", "png")

    assert cache.root in path.parents
    assert ".." not in path.relative_to(cache.root).parts
