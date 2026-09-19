"""封面获取与缓存的单元测试(阶段 G-4).

覆盖七类场景: 缓存命中、本地命中、CDN 命中、超时、超限、内容类型不符、内容
损坏; 另加离线复用旧图、缓存清理与"缓存不落在备份目录里"的约定。
"""

from __future__ import annotations

import io
import os
from collections.abc import Callable
from pathlib import Path

import httpx
import pytest
from PIL import Image

from archive_management.domain import ArtworkRef, PlatformGame
from archive_management.exceptions import ArtworkError
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.artwork import (
    ICON_SIZE,
    ICON_VERSION,
    STEAM_CDN_ROOT,
    STEAM_COVER_ASSET,
    ArtworkCache,
    FetchedArtwork,
    HttpArtworkFetcher,
    artwork_cache,
    artwork_cache_at,
    cached_artwork,
    platform_game,
    resolve_artwork,
    sniff_media_type,
    square_icon,
    steam_artwork,
    steam_cover,
    steam_icon,
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


def test_steam_cover_uses_the_public_cdn_rule(tmp_path: Path) -> None:
    """封面按公开 CDN 规则构造, 资源名同时是缓存版本."""
    reference = steam_cover("730")
    assert reference.kind == "cover"
    assert reference.url == (f"{STEAM_CDN_ROOT}/730/{STEAM_COVER_ASSET}")
    assert reference.version == STEAM_COVER_ASSET


def test_steam_icon_is_derived_from_the_cover() -> None:
    """图标不再是下载资源: 引用不带地址, 由封面裁出来."""
    icon = steam_icon("730")
    assert icon.kind == "icon"
    assert icon.url == ""
    assert icon.version == ICON_VERSION

    pair = steam_artwork("730")
    assert [reference.kind for reference in pair] == ["cover", "icon"]
    assert pair[0].url.endswith(STEAM_COVER_ASSET)


def test_forget_removes_every_cached_image_of_a_game(tmp_path: Path) -> None:
    """删游戏时清缓存: 该游戏的封面与图标都要删掉, 空目录也不留."""
    cache = artwork_cache_at(tmp_path / "cache")
    cache.store(
        "steam", "730", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )
    cache.store("steam", "730", "icon", ICON_VERSION, content=_PNG, extension="png")
    cache.store(
        "steam", "999", "cover", STEAM_COVER_ASSET, content=_PNG, extension="png"
    )

    removed = cache.forget("steam", "730")

    assert len(removed) == 2
    assert cache.lookup_any("steam", "730", "cover") is None
    assert cache.directory("steam", "730").exists() is False
    # 别的游戏不受影响; 再删一次是幂等的(不报错).
    assert cache.lookup_any("steam", "999", "cover") is not None
    assert cache.forget("steam", "730") == []


def test_square_icon_crops_the_cover_into_a_square(tmp_path: Path) -> None:
    """方形图标: 从竖版封面裁出正方形(Steam 的 logo.png 是标题图, 不适合当图标)."""
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)

    content = square_icon(cover)

    assert content is not None
    with Image.open(io.BytesIO(content)) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)


def test_square_icon_returns_none_for_a_broken_file(tmp_path: Path) -> None:
    """裁不开的文件不抛异常, 由调用方回落占位."""
    broken = tmp_path / "broken.png"
    broken.write_bytes(b"not an image")

    assert square_icon(broken) is None


def test_cached_artwork_never_touches_the_network(tmp_path: Path) -> None:
    """渲染用接口只查缓存: 没有缓存就是没有, 不会自己去下载."""
    cache = _cache(tmp_path)
    game = platform_game(
        store="steam", game_id="730", name="Demo", artwork=(steam_cover("730"),)
    )

    assert cached_artwork(game, "cover", cache) is None

    stored = cache.store("steam", "730", "cover", "v1", content=_PNG, extension="png")
    assert cached_artwork(game, "cover", cache) == stored


def test_cached_artwork_skips_a_corrupt_file(tmp_path: Path) -> None:
    """缓存内容坏掉时渲染接口也当作没有图, 让界面走占位."""
    cache = _cache(tmp_path)
    cache.store("steam", "730", "cover", "v1", content=b"garbage", extension="png")
    game = platform_game(store="steam", game_id="730", name="Demo")

    assert cached_artwork(game, "cover", cache) is None


def test_external_identifiers_cannot_escape_the_cache_root(tmp_path: Path) -> None:
    cache = _cache(tmp_path)

    path = cache.path_for("../../etc", "../../..", "cover", "../../x", "png")

    assert cache.root in path.parents
    assert ".." not in path.relative_to(cache.root).parts
