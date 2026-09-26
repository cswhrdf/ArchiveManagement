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
from archive_management.services import artwork as artwork_mod
from archive_management.services.artwork import (
    ICON_SIZE,
    ICON_VERSION,
    STEAM_CDN_ROOT,
    STEAM_COVER_ASSET,
    STEAM_ICON_CDN_ROOT,
    ArtworkCache,
    FetchedArtwork,
    HttpArtworkFetcher,
    artwork_cache,
    artwork_cache_at,
    cached_artwork,
    icon_version,
    local_artwork,
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
_ICO = b"\x00\x00\x01\x00" + b"\x00" * 32
_HTML = b"<html><body>not an image</body></html>"

# 一款 Steam 游戏的官方图标哈希(来自 appinfo.vdf 的 clienticon).
_ICON_HASH = "b2f863a4c63bc1c5667a8a7e3e9355ef260ce6d2"


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
    assert sniff_media_type(_ICO) == "image/x-icon"
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


def test_steam_icon_uses_the_official_hash(tmp_path: Path) -> None:
    """图标按官方 clienticon 哈希构造: 版本是哈希, 本机 ico 给路径, CDN 给同哈希."""
    local = tmp_path / f"{_ICON_HASH}.ico"
    icon = steam_icon("730", _ICON_HASH, local_path=local)

    assert icon.kind == "icon"
    assert icon.version == _ICON_HASH
    assert icon.url == f"{STEAM_ICON_CDN_ROOT}/730/{_ICON_HASH}.ico"
    assert icon.local_path == str(local)


def test_steam_artwork_without_a_hash_only_offers_the_cover() -> None:
    """拿不到官方图标哈希时只给封面: 后台裁封面兜底, 界面不会因此没图."""
    assert [item.kind for item in steam_artwork("730")] == ["cover"]

    pair = steam_artwork("730", _ICON_HASH)
    assert [item.kind for item in pair] == ["cover", "icon"]
    assert pair[0].url.endswith(STEAM_COVER_ASSET)


def test_icon_version_distinguishes_both_sources() -> None:
    """缓存文件名要说明这份图怎么来的: 官方图标用哈希, 封面裁剪用版本名."""
    official = platform_game(
        store="steam",
        game_id="730",
        name="Demo",
        artwork=(steam_cover("730"), steam_icon("730", _ICON_HASH)),
    )
    cropped = platform_game(
        store="steam", game_id="730", name="Demo", artwork=(steam_cover("730"),)
    )

    assert icon_version(official) == _ICON_HASH
    assert icon_version(cropped) == ICON_VERSION


def test_a_local_official_icon_needs_no_network(tmp_path: Path) -> None:
    """本机已有 ico(已安装的游戏通常都有)时完全离线: 一次请求也不发."""
    cache = _cache(tmp_path)
    local = tmp_path / f"{_ICON_HASH}.ico"
    local.write_bytes(_ICO)
    game = platform_game(
        store="steam",
        game_id="730",
        name="Demo",
        artwork=(steam_icon("730", _ICON_HASH, local_path=local),),
    )
    fetcher = _FakeFetcher()

    result = resolve_artwork(game, "icon", cache, fetcher=fetcher)

    assert result.source == "local"
    assert result.path is not None
    assert result.path.suffix == ".ico"
    assert fetcher.calls == []


def test_the_official_icon_is_downloaded_from_the_cdn_as_ico(tmp_path: Path) -> None:
    """本机没有图标时去 CDN 取同哈希的 .ico(声明类型是 image/x-icon)."""
    cache = _cache(tmp_path)
    game = platform_game(
        store="steam",
        game_id="730",
        name="Demo",
        artwork=(steam_icon("730", _ICON_HASH),),
    )
    fetcher = _FakeFetcher(content=_ICO, declared="image/x-icon")

    result = resolve_artwork(game, "icon", cache, fetcher=fetcher)

    assert result.source == "cdn"
    assert result.path is not None
    assert result.path.suffix == ".ico"
    assert fetcher.calls == [f"{STEAM_ICON_CDN_ROOT}/730/{_ICON_HASH}.ico"]


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
    """封面兜底: 平台给不出官方图标时, 从竖版封面裁出正方形当图标."""
    cover = tmp_path / "cover.png"
    Image.new("RGB", (300, 450), (10, 20, 30)).save(cover)

    content = square_icon(cover)

    assert content is not None
    with Image.open(io.BytesIO(content)) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)


def test_square_icon_accepts_an_official_ico(tmp_path: Path) -> None:
    """官方图标本身是方形多尺寸 ico: 取最大那张并统一到缓存的边长."""
    source = tmp_path / "icon.ico"
    Image.new("RGBA", (64, 64), (200, 30, 30, 255)).save(
        source, format="ICO", sizes=[(32, 32), (64, 64)]
    )

    content = square_icon(source)

    assert content is not None
    with Image.open(io.BytesIO(content)) as made:
        assert made.size == (ICON_SIZE, ICON_SIZE)
        # 透明通道要保留: 否则透明图标贴到深色界面上会变成黑底.
        assert made.mode == "RGBA"
        assert made.getpixel((ICON_SIZE // 2, ICON_SIZE // 2)) == (200, 30, 30, 255)


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


# -- 本地文件与下载器的边界 -------------------------------------------------


def test_http_fetcher_builds_its_own_client_when_none_is_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有注入客户端时自己建一个(注不注入都走同一条下载逻辑).

    只顶掉 ``httpx.Client`` 这个边界: 建客户端与流式下载都走真实实现, 所以
    "省略注入"这条路上真的建了客户端、也真的发出了那一次请求(不是靠替身假装).
    """
    built: list[tuple[float, bool]] = []
    requested: list[str] = []
    real_client = httpx.Client
    url = "https://cdn.example/steam/730/cover.jpg"

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200, headers={"content-type": "image/png; charset=binary"}, content=_PNG
        )

    def build(*, timeout: float, follow_redirects: bool) -> httpx.Client:
        built.append((timeout, follow_redirects))
        return real_client(
            transport=httpx.MockTransport(handler),
            timeout=timeout,
            follow_redirects=follow_redirects,
        )

    monkeypatch.setattr(httpx, "Client", build)

    fetched = HttpArtworkFetcher().fetch(url, timeout=1.0, max_bytes=64)

    assert built == [(1.0, True)]
    assert requested == [url]
    assert fetched.content == _PNG
    assert fetched.declared_media_type == "image/png"


def test_prune_of_a_missing_cache_root_returns_nothing(tmp_path: Path) -> None:
    """缓存根目录还不存在时清理是空操作(不会建一个空目录出来)."""
    cache = ArtworkCache(tmp_path / "nope")

    assert cache.prune(max_age_days=1) == []
    assert not cache.root.exists()


def test_local_artwork_skips_a_reference_whose_file_is_gone(tmp_path: Path) -> None:
    """本地引用指向的文件不存在时跳过它, 继续看后面可用的引用."""
    real = tmp_path / "header.jpg"
    real.write_bytes(_JPEG)
    game = PlatformGame(
        platform="steam",
        game_id="730",
        name="Demo",
        artwork=[
            ArtworkRef(kind="cover", url="", local_path=str(tmp_path / "gone.jpg")),
            ArtworkRef(kind="cover", url="", local_path=str(real)),
        ],
    )

    assert local_artwork(game, "cover") == real


def test_a_local_file_over_the_size_limit_is_used_as_is(tmp_path: Path) -> None:
    """本地图片超过体积上限时不收进缓存, 但界面仍然直接用它."""
    local = tmp_path / "header.jpg"
    local.write_bytes(_JPEG)
    cache = _cache(tmp_path)

    result = resolve_artwork(
        _game(local=local), "cover", cache, fetcher=_FakeFetcher(), max_bytes=1
    )

    assert result.path == local
    assert result.source == "local"
    assert "体积上限" in (result.reason or "")
    assert cache.lookup("steam", "730", "cover", "v2") is None


def test_a_local_file_that_is_not_an_image_is_used_without_caching(
    tmp_path: Path,
) -> None:
    """本地文件认不出图片类型时直接用它, 不写进缓存(下次仍会重试)."""
    local = tmp_path / "header.jpg"
    local.write_bytes(_HTML)
    cache = _cache(tmp_path)

    result = resolve_artwork(_game(local=local), "cover", cache, fetcher=_FakeFetcher())

    assert result.path == local
    assert result.source == "local"
    assert "可识别" in (result.reason or "")


def test_validate_rejects_empty_and_unsupported_payloads() -> None:
    """下载内容为空、或声明类型不受支持: 都给出具体原因而不是当成可用图片."""
    empty_reason, empty_type = artwork_mod._validate(
        FetchedArtwork(content=b""), max_bytes=64
    )
    declared_reason, declared_type = artwork_mod._validate(
        FetchedArtwork(content=_PNG, declared_media_type="text/plain"), max_bytes=64
    )

    assert empty_reason == "下载内容为空"
    assert empty_type == ""
    assert "不受支持" in declared_reason
    assert declared_type == ""
