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

import contextlib
import io
import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import httpx
from PIL import Image

from archive_management.domain import (
    ARTWORK_KINDS,
    ArtworkKind,
    ArtworkRef,
    PlatformGame,
    PlatformId,
)
from archive_management.exceptions import ArtworkError, ArtworkImageError
from archive_management.infrastructure.paths import ApplicationPaths

logger = logging.getLogger(__name__)

# 缓存目录名(位于 cache_dir 之下)与下载默认参数.
ARTWORK_DIR_NAME = "artwork"
DEFAULT_TIMEOUT = 5.0
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
# Steam 公开 CDN 的地址规则与海报用的竖版封面资源名(无凭据, 缺失即占位).
STEAM_CDN_ROOT = "https://cdn.cloudflare.steamstatic.com/steam/apps"
STEAM_COVER_ASSET = "library_600x900.jpg"
# 官方图标(``clienticon``)的 CDN 规则: 哈希来自本机 appinfo.vdf, 见
# :mod:`archive_management.services.steam_appinfo`。本机 ``steam/games/<哈希>.ico``
# 优先(离线可用), CDN 只作兜底 —— 与封面同一条“本地优先”。
STEAM_ICON_CDN_ROOT = (
    "https://cdn.cloudflare.steamstatic.com/steamcommunity/public/images/apps"
)
STEAM_ICON_ASSET = "ico"
# 图标缓存版本: 官方图标直接用它的哈希(Steam 换图标就换文件名, 不会拿旧图当新
# 图), 只有平台给不出官方图标时才回落“封面裁方形” —— 那一种用下面这个版本名,
# 缓存文件名要如实说明这份图是怎么来的。
ICON_VERSION = "cover-square-256"
ICON_SIZE = 256

# 允许的图片类型与其扩展名; 类型以文件头为准, 声明值只用于校验.
# ico 是官方图标自带的格式(多尺寸, 取最大那张), 下载后会被转成 PNG 再进图标缓存。
_EXTENSIONS: dict[str, str] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/x-icon": "ico",
    "image/vnd.microsoft.icon": "ico",
}
_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"\x00\x00\x01\x00", "image/x-icon"),
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

    def forget(self, platform: str, game_id: str) -> list[Path]:
        """删除一款游戏的全部缓存图片, 返回被删掉的文件.

        游戏被删除时调用: 缓存是可再生的派生数据, 留着只会占地方, 而且下次遇到
        同名/同 AppID 的游戏会误用旧图。目录空了就顺手删掉, 不堆空文件夹。
        """
        directory = self.directory(platform, game_id)
        if not directory.is_dir():
            return []
        removed = sorted(path for path in directory.iterdir() if path.is_file())
        for path in removed:
            path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            directory.rmdir()
        return removed

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


def artwork_cache_at(cache_dir: Path) -> ArtworkCache:
    """返回给定缓存目录下的封面缓存(便于只持有 ``cache_dir`` 的调用方)."""
    return ArtworkCache(cache_dir / ARTWORK_DIR_NAME)


def artwork_cache(paths: ApplicationPaths) -> ArtworkCache:
    """返回应用缓存目录下的封面缓存."""
    return artwork_cache_at(paths.cache_dir)


# ------------------------------------------------------- 用户自己指定的图片
# 存在 **data_dir** 而不是 cache_dir: 缓存会按年龄/容量清理, 用户挑的那张图是用户数据。
USER_ARTWORK_DIR_NAME = "artwork"
# 归一化后的统一容器: 存 PNG(与图标缓存一致), 界面与校验只认这一种。
USER_ARTWORK_SUFFIX = ".png"
# 单张原图的上限与其长边上限(海报卡片最大也只画 ~250 宽, 再大只是白占磁盘与内存)。
MAX_USER_IMAGE_BYTES = 12 * 1024 * 1024
MAX_USER_COVER_SIDE = 1200
# 失败原因代码: 界面按代码取文案(服务层不拼人类语言, 见 ArtworkImageError)。
NOT_AN_IMAGE = "not_an_image"
TOO_LARGE = "too_large"
UNREADABLE = "unreadable"
WRITE_FAILED = "write_failed"


class UserArtworkStore:
    """用户自己指定/上传的封面与图标(按**游戏 id** 存, 与平台取图缓存分开).

    为什么要单独一份: 平台取图由适配器与 CDN 决定, 手动添加的游戏压根没有平台图
    (``_artwork_game`` 会返回 None) —— 用户自己挑的图必须能覆盖它, 而且不能被
    "缓存清理"带走。搜索/下载那条路仍然只写缓存, 两不会互相覆盖。
    """

    def __init__(self, root: Path) -> None:
        """绑定根目录(通常来自 :func:`user_artwork_store`)."""
        self._root = root

    @property
    def root(self) -> Path:
        """根目录(``<data_dir>/artwork``)."""
        return self._root

    def directory(self, game_id: str) -> Path:
        """返回一款游戏的自定义图片目录."""
        return self._root / _slug(game_id)

    def path_for(self, game_id: str, kind: ArtworkKind) -> Path:
        """返回用户那份图的落点(固定 PNG, 不按版本命名 —— 它永远只有一份)."""
        return self.directory(game_id) / f"{_slug(kind)}{USER_ARTWORK_SUFFIX}"

    def find(self, game_id: str, kind: ArtworkKind) -> Path | None:
        """用户给过这张图吗(存在且还是一张可识别的图片时返回路径)."""
        path = self.path_for(game_id, kind)
        return path if _usable(path) else None

    def save(self, game_id: str, kind: ArtworkKind, source: Path) -> Path:
        """校验并归一化用户选的文件, 原子写入, 返回落点.

        失败抛 :class:`ArtworkImageError`(带原因代码): 不可用的图片不进库, 免得界面上
        出现一张黑图却没人知道为什么。
        """
        content = normalized_image(source, kind)
        target = self.path_for(game_id, kind)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(f"{target.name}.part")
            temporary.write_bytes(content)
            temporary.replace(target)
        except OSError as exc:
            raise ArtworkImageError(WRITE_FAILED, str(exc)) from exc
        return target

    def clear(self, game_id: str, kind: ArtworkKind | None = None) -> list[Path]:
        """恢复默认: 删掉用户指定的那份(``kind=None`` 清这款游戏的全部), 返回被删的文件."""
        removed: list[Path] = []
        for item in ARTWORK_KINDS if kind is None else (kind,):
            path = self.path_for(game_id, item)
            if path.is_file():
                path.unlink(missing_ok=True)
                removed.append(path)
        with contextlib.suppress(OSError):  # 空了就别留空目录
            self.directory(game_id).rmdir()
        return removed


def user_artwork_store(paths: ApplicationPaths) -> UserArtworkStore:
    """返回应用数据目录下的自定义图片存储."""
    return UserArtworkStore(paths.artwork_dir)


def normalized_image(source: Path, kind: ArtworkKind) -> bytes:
    """把用户选的文件读成"可以直接用"的 PNG 字节(校验 + 归一化).

    三道闸, 各有自己的原因代码: 读得到(``unreadable``)、真的是一张图(``not_an_image``)、
    没超体积上限(``too_large``)。图标复用 :func:`square_icon`(与官方图标同一条路: 居中
    裁方 + 固定边长 + 保留透明通道), 封面保留长宽比、只把长边压到
    :data:`MAX_USER_COVER_SIDE` 以内。
    """
    try:
        if not source.is_file():
            raise ArtworkImageError(UNREADABLE, str(source))
        size = source.stat().st_size
    except OSError as exc:
        raise ArtworkImageError(UNREADABLE, str(exc)) from exc
    if size > MAX_USER_IMAGE_BYTES:
        raise ArtworkImageError(TOO_LARGE, f"{size} > {MAX_USER_IMAGE_BYTES}")
    if kind == "icon":
        content = square_icon(source)
        if content is None:
            raise ArtworkImageError(NOT_AN_IMAGE, str(source))
        return content
    try:
        with Image.open(source) as image:
            picture = _keep_transparency(image)
            picture.thumbnail(
                (MAX_USER_COVER_SIDE, MAX_USER_COVER_SIDE), Image.Resampling.LANCZOS
            )
            buffer = io.BytesIO()
            picture.save(buffer, format="PNG")
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ArtworkImageError(NOT_AN_IMAGE, str(exc)) from exc
    return buffer.getvalue()


def local_artwork(game: PlatformGame, kind: ArtworkKind) -> Path | None:
    """返回平台数据里指向的本地图片路径(Steam 的 librarycache/grid)."""
    for reference in game.artwork:
        if reference.kind != kind or not reference.local_path:
            continue
        path = Path(reference.local_path)
        if path.is_file():
            return path
    return None


def cached_artwork(
    game: PlatformGame, kind: ArtworkKind, cache: ArtworkCache
) -> Path | None:
    """界面渲染用: 只查缓存与平台本地文件, 不发起网络请求.

    顺序是"缓存 → 平台本地文件": 缓存里那份是已经归一化好的成品(图标会被转成
    固定边长的方形 PNG), 与后台补齐时的判断一致; 平台本地文件只作为"还没补上"
    时的即时来源(装好的游戏通常马上就有图可看)。详情页与海报卡片是同步渲染的,
    一次网络请求会把界面卡住; 下载统一交给 :func:`resolve_artwork`(可由后台
    线程调用)。
    """
    cached = cache.lookup_any(game.platform, game.game_id, kind)
    if cached is not None and _usable(cached):
        return cached
    return local_artwork(game, kind)


def platform_game(
    *,
    store: PlatformId,
    game_id: str,
    name: str,
    artwork: Sequence[ArtworkRef] = (),
    install_dir: str = "",
) -> PlatformGame:
    """把“库里的游戏”包装成平台数据模型(封面相关接口只认 ``PlatformGame``)."""
    return PlatformGame(
        platform=store,
        game_id=game_id,
        name=name,
        install_dir=install_dir,
        artwork=list(artwork),
    )


def steam_cover(app_id: str) -> ArtworkRef:
    """按公开 CDN 规则构造 Steam 封面引用(无凭据).

    资源名同时充当缓存版本: 以后换封面规则只要改 :data:`STEAM_COVER_ASSET`,
    缓存文件名就会跟着变, 不会拿旧图当新图。
    """
    return ArtworkRef(
        kind="cover",
        url=f"{STEAM_CDN_ROOT}/{app_id}/{STEAM_COVER_ASSET}",
        version=STEAM_COVER_ASSET,
    )


def steam_icon(
    app_id: str, clienticon: str, *, local_path: Path | None = None
) -> ArtworkRef:
    """按官方图标哈希构造图标引用(本机文件优先, CDN 兜底).

    哈希来自本机 ``appinfo.vdf`` 的 ``clienticon`` 字段: 本机 ``steam/games/``
    里通常已经有同名 ``.ico``(完全离线可用), 没有才去 CDN 取同哈希的资源。
    ``version`` 直接用哈希 —— 缓存文件名跟着官方图标走。
    """
    return ArtworkRef(
        kind="icon",
        url=f"{STEAM_ICON_CDN_ROOT}/{app_id}/{clienticon}.{STEAM_ICON_ASSET}",
        local_path="" if local_path is None else str(local_path),
        version=clienticon,
    )


def steam_artwork(
    app_id: str, clienticon: str = "", *, local_path: Path | None = None
) -> tuple[ArtworkRef, ...]:
    """返回一款 Steam 游戏的图片引用: 封面 + 能拿到官方图标时的图标.

    拿不到 ``clienticon``(没有 appinfo、版本不认识、游戏不在里面)时只返回封面:
    后台会用封面裁一块方形当图标, 界面不会因为少一个哈希而变空。
    """
    if not clienticon:
        return (steam_cover(app_id),)
    return (steam_cover(app_id), steam_icon(app_id, clienticon, local_path=local_path))


def icon_version(game: PlatformGame) -> str:
    """图标缓存版本: 有官方图标用它的哈希, 否则用封面裁剪的版本名."""
    reference = _reference_for(game, "icon")
    if reference is None or not reference.version:
        return ICON_VERSION
    return reference.version


def square_icon(source: Path, *, size: int = ICON_SIZE) -> bytes | None:
    """把一张图片裁成方形并编码成 PNG, 供界面当图标使用.

    两种来源共用它: **官方图标**(``clienticon`` 的多尺寸 ``.ico``, 本身就是方的)
    与平台给不出官方图标时的**封面裁剪**。统一转 PNG 的理由: 界面与缓存校验只认
    位图格式, 而两类来源的尺寸差异很大(16x16 到 256x256), 统一到 :data:`ICON_SIZE`
    后头像位不会忽大忽小。有透明通道就保留(官方图标常带透明), 贴到深色界面上
    不会变成黑底。裁不开(文件损坏/格式不支持)时返回 ``None``, 由调用方回落占位。
    """
    try:
        with Image.open(source) as image:
            picture = _keep_transparency(image)
            side = min(picture.size)
            left = (picture.width - side) // 2
            # 略向上偏移: 竖版封面的主体(角色/场景)通常在上半部分; 本身是方形的
            # 官方图标算出来就是 0, 不会被动到。
            top = max((picture.height - side) // 2 - side // 8, 0)
            square = picture.crop((left, top, left + side, top + side))
            resized = square.resize((size, size), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            resized.save(buffer, format="PNG")
    except (OSError, ValueError) as exc:
        logger.warning("图片无法裁成图标(%s): %s", source, exc)
        return None
    return buffer.getvalue()


def _keep_transparency(image: Image.Image) -> Image.Image:
    """有透明通道就保留(RGBA), 否则转成不透明位图(封面是拼合过的 jpg)."""
    if image.mode in {"RGBA", "LA"} or (
        image.mode == "P" and "transparency" in image.info
    ):
        return image.convert("RGBA")
    return image.convert("RGB")


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
