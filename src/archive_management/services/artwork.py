"""游戏封面与图标的本地优先获取与缓存.

查找顺序固定为"缓存 → 平台本地文件 → CDN": 平台自己下载的图片(Steam 的
``appcache/librarycache`` 与 ``userdata/<账号>/config/grid``)最可靠也最快,
CDN 只作为兜底。下载统一走可注入的 :class:`ArtworkFetcher`(默认实现用 httpx),
并强制超时、体积上限与图片内容校验 —— 图源在外部, 不能由它决定我们写多少
字节、写什么类型的文件。

缓存按 ``<平台>/<游戏标识>/<类型>-<版本>.<扩展名>`` 命名, 落在
:attr:`ApplicationPaths.cache_dir` 下, 属于可随时删除的派生数据, 不参与备份
内容与导出包。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx

from archive_management.domain import ArtworkKind, ArtworkRef, PlatformGame
from archive_management.exceptions import ArtworkError
from archive_management.infrastructure.paths import ApplicationPaths

# 缓存目录名(位于 cache_dir 之下)与下载默认参数.
ARTWORK_DIR_NAME = "artwork"
DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024

# 允许的图片类型与其扩展名; 类型以文件头为准, 声明值只用于校验.
_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
}
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_WEBP_MARKER = b"WEBP"

ArtworkSource = Literal["cache", "local", "cdn", "stale", "none"]


@dataclass(frozen=True)
class FetchedArtwork:
    """一次下载的原始结果."""

    content: bytes
    # 服务端声明的类型(可能为空或与内容不符), 仅用于交叉校验.
    declared_media_type: str = ""


@dataclass(frozen=True)
class ArtworkResult:
    """一次封面解析的结果; ``source`` 说明这份图从哪来."""

    path: Path | None
    source: ArtworkSource = "none"
    # 未取到图(或降级复用旧图)的原因, 供界面与日志解释.
    reason: str = ""

    @property
    def found(self) -> bool:
        """是否拿到了可用的图片文件."""
        return self.path is not None


class ArtworkFetcher(Protocol):
    """下载封面字节的抽象(便于测试注入假实现)."""

    def fetch(self, url: str, *, timeout: float, max_bytes: int) -> FetchedArtwork:
        """下载 ``url``, 返回字节与服务端声明的媒体类型."""


class HttpArtworkFetcher:
    """基于 httpx 的默认下载实现."""

    def __init__(self, *, client: httpx.Client | None = None) -> None:
        """可注入现成的客户端(测试或复用连接池); 省略时每次下载新建一个."""
        self._client = client

    def fetch(self, url: str, *, timeout: float, max_bytes: int) -> FetchedArtwork:
        """下载 ``url`` 并在超过体积上限时立即放弃."""
        if self._client is not None:
            return _download(self._client, url, timeout=timeout, max_bytes=max_bytes)
        with httpx.Client(timeout=timeout, follow_redirects=True) as client:
            return _download(client, url, timeout=timeout, max_bytes=max_bytes)


class ArtworkCache:
    """封面缓存目录: 按平台/游戏/类型/版本命名, 可整体删除."""

    def __init__(self, root: Path) -> None:
        """绑定缓存根目录(通常来自 :func:`artwork_cache`)."""
        self._root = root

    @property
    def root(self) -> Path:
        """缓存根目录(``<cache_dir>/artwork``)."""
        return self._root

    def directory(self, platform: str, game_id: str) -> Path:
        """返回某个平台游戏的缓存目录."""
        return self._root / _slug(platform) / _slug(game_id)

    def path_for(
        self,
        platform: str,
        game_id: str,
        kind: ArtworkKind,
        version: str,
        extension: str,
    ) -> Path:
        """返回一份资源的缓存文件名(平台/标识/类型/版本都在路径里)."""
        return self.directory(platform, game_id) / f"{_stem(kind, version)}.{extension}"

    def lookup(
        self, platform: str, game_id: str, kind: ArtworkKind, version: str
    ) -> Path | None:
        """精确命中: 版本一致且文件存在."""
        wanted = _stem(kind, version)
        for path in self._list(platform, game_id, kind):
            if path.stem == wanted:
                return path
        return None

    def lookup_any(self, platform: str, game_id: str, kind: ArtworkKind) -> Path | None:
        """降级命中: 最新的一份缓存, 不要求版本一致(离线时复用旧图)."""
        found = self._list(platform, game_id, kind)
        if not found:
            return None
        return max(found, key=lambda path: path.stat().st_mtime)

    def store(
        self,
        platform: str,
        game_id: str,
        kind: ArtworkKind,
        version: str,
        *,
        content: bytes,
        extension: str,
    ) -> Path:
        """原子写入一份缓存文件, 并清掉同类型的旧版本."""
        target = self.path_for(platform, game_id, kind, version, extension)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.part")
        temporary.write_bytes(content)
        temporary.replace(target)
        for outdated in self._list(platform, game_id, kind):
            if outdated != target:
                outdated.unlink(missing_ok=True)
        return target

    def prune(self, *, max_age_days: int) -> list[Path]:
        """删除超过 ``max_age_days`` 天的缓存文件, 返回被删掉的文件."""
        if not self._root.is_dir():
            return []
        deadline = time.time() - max_age_days * 24 * 60 * 60
        removed: list[Path] = []
        for path in sorted(self._root.rglob("*")):
            if not path.is_file():
                continue
            try:
                expired = path.stat().st_mtime < deadline
            except OSError:  # pragma: no cover - 文件在遍历时消失
                continue
            if expired:
                path.unlink(missing_ok=True)
                removed.append(path)
        return removed

    def _list(self, platform: str, game_id: str, kind: ArtworkKind) -> list[Path]:
        """列出某个类型下已缓存的文件."""
        directory = self.directory(platform, game_id)
        if not directory.is_dir():
            return []
        return sorted(directory.glob(f"{_slug(kind)}-*"))


def artwork_cache(paths: ApplicationPaths) -> ArtworkCache:
    """返回应用缓存目录下的封面缓存."""
    return ArtworkCache(paths.cache_dir / ARTWORK_DIR_NAME)


def local_artwork(game: PlatformGame, kind: ArtworkKind) -> Path | None:
    """返回平台数据里指向的本地图片路径(Steam 的 librarycache/grid)."""
    for reference in game.artwork:
        if reference.kind != kind or not reference.local_path:
            continue
        path = Path(reference.local_path)
        if path.is_file():
            return path
    return None


def sniff_media_type(data: bytes) -> str | None:
    """按文件头判断图片类型, 认不出返回 ``None``.

    只认文件头而不信 HTTP 头: 声明的类型由外部服务决定, 文件头由实际内容决定,
    两者冲突时以后者为准。
    """
    for signature, media_type in _SIGNATURES:
        if data.startswith(signature):
            return media_type
    if len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == _WEBP_MARKER:
        return "image/webp"
    return None


def resolve_artwork(
    game: PlatformGame,
    kind: ArtworkKind,
    cache: ArtworkCache,
    *,
    fetcher: ArtworkFetcher | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_bytes: int = DEFAULT_MAX_BYTES,
) -> ArtworkResult:
    """按"缓存 → 平台本地文件 → CDN"的顺序解析一份封面.

    缓存未命中、本地也没有、下载又失败时会退回缓存里任意版本的旧图
    (``source="stale"``): 图源不可用不应该让界面变成一片空白。
    """
    reference = _reference_for(game, kind)
    version = "" if reference is None else reference.version
    cached = cache.lookup(game.platform, game.game_id, kind, version)
    if cached is not None and _usable(cached):
        return ArtworkResult(path=cached, source="cache")
    local = local_artwork(game, kind)
    if local is not None:
        return _cache_local(
            game, kind, local, cache, version=version, max_bytes=max_bytes
        )
    path, reason = _from_cdn(
        game,
        kind,
        cache,
        version=version,
        reference=reference,
        fetcher=fetcher,
        timeout=timeout,
        max_bytes=max_bytes,
    )
    if path is not None:
        return ArtworkResult(path=path, source="cdn")
    stale = cache.lookup_any(game.platform, game.game_id, kind)
    if stale is not None and _usable(stale):
        return ArtworkResult(path=stale, source="stale", reason=reason)
    return ArtworkResult(path=None, source="none", reason=reason)


def _cache_local(
    game: PlatformGame,
    kind: ArtworkKind,
    local: Path,
    cache: ArtworkCache,
    *,
    version: str,
    max_bytes: int,
) -> ArtworkResult:
    """把平台本地的图片收进缓存; 读不到或写不进去就退而直接用本地文件."""
    try:
        content = local.read_bytes()
    except OSError as exc:  # pragma: no cover - 平台文件在读取时消失
        return ArtworkResult(path=local, source="local", reason=str(exc))
    if len(content) > max_bytes:
        return ArtworkResult(path=local, source="local", reason="本地封面超过体积上限")
    media_type = sniff_media_type(content)
    if media_type is None:
        return ArtworkResult(path=local, source="local", reason="内容不是可识别的图片")
    try:
        stored = cache.store(
            game.platform,
            game.game_id,
            kind,
            version,
            content=content,
            extension=_EXTENSIONS[media_type],
        )
    except OSError as exc:
        return ArtworkResult(path=local, source="local", reason=str(exc))
    return ArtworkResult(path=stored, source="local")


def _from_cdn(
    game: PlatformGame,
    kind: ArtworkKind,
    cache: ArtworkCache,
    *,
    version: str,
    reference: ArtworkRef | None,
    fetcher: ArtworkFetcher | None,
    timeout: float,
    max_bytes: int,
) -> tuple[Path | None, str]:
    """从 CDN 取一份封面, 通过校验后写入缓存; 失败时返回原因."""
    if reference is None or not reference.url:
        return (None, "平台数据里没有可用的图片地址")
    active = HttpArtworkFetcher() if fetcher is None else fetcher
    try:
        fetched = active.fetch(reference.url, timeout=timeout, max_bytes=max_bytes)
    except ArtworkError as exc:
        return (None, str(exc))
    reason, media_type = _validate(fetched, max_bytes=max_bytes)
    if reason:
        return (None, reason)
    try:
        stored = cache.store(
            game.platform,
            game.game_id,
            kind,
            version,
            content=fetched.content,
            extension=_EXTENSIONS[media_type],
        )
    except OSError as exc:
        return (None, str(exc))
    return (stored, "")


def _validate(fetched: FetchedArtwork, *, max_bytes: int) -> tuple[str, str]:
    """校验下载内容, 返回 ``(拒绝原因, 媒体类型)``; 通过时原因为空串."""
    if not fetched.content:
        return ("下载内容为空", "")
    if len(fetched.content) > max_bytes:
        return (f"封面超过体积上限({max_bytes} 字节)", "")
    media_type = sniff_media_type(fetched.content)
    if media_type is None:
        return ("内容不是可识别的图片", "")
    declared = fetched.declared_media_type
    if declared and declared not in _EXTENSIONS:
        return (f"内容类型不受支持: {declared}", "")
    return ("", media_type)


def _usable(path: Path) -> bool:
    """缓存文件是否仍是一张可识别的图片(内容损坏时丢弃并重取)."""
    try:
        with path.open("rb") as handle:
            head = handle.read(16)
    except OSError:  # pragma: no cover - 文件在读取时消失
        return False
    return sniff_media_type(head) is not None


def _reference_for(game: PlatformGame, kind: ArtworkKind) -> ArtworkRef | None:
    """返回平台数据里指定类型的资源引用."""
    for reference in game.artwork:
        if reference.kind == kind:
            return reference
    return None


def _download(
    client: httpx.Client, url: str, *, timeout: float, max_bytes: int
) -> FetchedArtwork:
    """流式下载, 边下边判体积上限, 避免把超大响应整个读进内存."""
    chunks: list[bytes] = []
    size = 0
    try:
        with client.stream("GET", url, timeout=timeout) as response:
            response.raise_for_status()
            declared = response.headers.get("content-type", "").split(";")[0].strip()
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > max_bytes:
                    raise ArtworkError(f"封面超过体积上限({max_bytes} 字节)")
                chunks.append(chunk)
    except httpx.HTTPError as exc:
        raise ArtworkError(f"下载封面失败: {exc}") from exc
    return FetchedArtwork(content=b"".join(chunks), declared_media_type=declared)


def _stem(kind: ArtworkKind, version: str) -> str:
    """返回缓存文件名的主体: ``<类型>-<版本>``."""
    return f"{_slug(kind)}-{_slug(version)}"


def _slug(value: str) -> str:
    """把外部标识收敛成安全的文件名片段(去掉分隔符与 ``..``)."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]", "_", value.strip()).strip(".")
    return cleaned[:64] or "unknown"
