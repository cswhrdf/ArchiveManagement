"""平台适配器: 把各平台的本地数据翻译成版本化的平台模型.

平台适配的边界约定: 领域层、应用层与界面只认
:class:`~archive_management.domain.PlatformGame` 与
:class:`~archive_management.domain.SavePathCandidate`, 平台差异(目录规则、清单
格式、云端清单)全部挡在适配器内部。本期只有 Steam 有真正的实现, 其余平台注册
"暂不支持"的**显式降级**(空结果 + 原因), 而不是让调用方以为查询过。

约定:

- **契约一致**: 所有适配器都实现同一个 :class:`PlatformAdapter` 协议, 契约用例
  用同一套断言逐个跑(见 ``tests/unit/test_platform_adapters.py``);
- **不抛异常**: 适配器是界面直接调用的边界, 平台数据损坏、客户端未安装、文件
  权限不足都只让结果变少并记 DEBUG 日志。需要"显式失败"的地方(版本不兼容)由
  :func:`archive_management.domain.parse_platform_game` 负责, 不由适配器静默兜住;
- **可注入**: 适配器接收 :class:`~archive_management.services.platform_scan.ScanRoots`,
  测试可以在临时目录里造出平台结构, 既不依赖本机安装过平台客户端, 也不依赖
  运行在对应平台上;
- **复用而非重写**: Steam 的清单解析复用
  :mod:`archive_management.services.platform_scan`, 云端清单解析复用
  :mod:`archive_management.services.steam_cloud`。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol, runtime_checkable

from archive_management.domain import (
    PLATFORM_IDS,
    ArtworkRef,
    PlatformGame,
    PlatformId,
    SavePathCandidate,
)
from archive_management.services.artwork import steam_artwork
from archive_management.services.pathcheck import normalize_path
from archive_management.services.platform_scan import (
    ScanRoots,
    read_steam_installs,
)
from archive_management.services.steam_appinfo import (
    SteamIcons,
    load_steam_icons,
)
from archive_management.services.steam_cloud import SteamCloudSource

logger = logging.getLogger(__name__)

# 未实现平台的默认说明; 界面接入时再按 i18n 键映射成人话。
DEFAULT_UNSUPPORTED_REASON = "暂未实现"


@runtime_checkable
class PlatformAdapter(Protocol):
    """一个游戏平台的适配能力: 身份、游戏列表与可信存档候选."""

    @property
    def platform(self) -> PlatformId:
        """平台标识(与 ``domain.discovery.DiscoverySource`` 的前四项一致)."""
        ...

    @property
    def supported(self) -> bool:
        """本平台是否已实现(False 表示只保留边界, 走"暂不支持"降级)."""
        ...

    @property
    def unsupported_reason(self) -> str:
        """未实现时的原因说明; 已实现时为空字符串."""
        ...

    @property
    def supports_save_paths(self) -> bool:
        """是否支持从平台数据推断存档路径(False 表示只能手动添加)."""
        ...

    @property
    def supports_artwork(self) -> bool:
        """是否支持提供封面/图标(False 表示界面只能显示占位图)."""
        ...

    def list_games(self) -> list[PlatformGame]:
        """返回本平台已安装的游戏; 读取失败时返回空列表并记 DEBUG 日志."""
        ...

    def save_candidates(self, game: PlatformGame) -> list[SavePathCandidate]:
        """返回该游戏的存档路径候选; 拿不到可信来源时返回空列表."""
        ...

    def artwork_refs(self, game: PlatformGame) -> tuple[ArtworkRef, ...]:
        """返回该游戏的图片资源引用(封面等); 不支持的平台返回空元组."""


@runtime_checkable
class SaveCandidateSource(Protocol):
    """能产出存档候选的来源.

    Steam 的实现是
    :class:`~archive_management.services.steam_cloud.SteamCloudSource`(读云端
    同步清单); 抽成协议是为了让适配器依赖"能力"而不是具体来源 —— 用例可以注入
    必然失败的替身, 验证适配器边界只降级、不把异常抛给界面。
    """

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        """返回该 AppID 的存档路径候选."""
        ...


class SteamAdapter:
    """Steam 适配器.

    游戏列表来自本机应用清单(``appmanifest_*.acf``, 顺带补齐 AppID), 存档候选
    来自云端同步清单(``userdata/<account>/<appid>/remotecache.vdf``): 两者都是
    Steam 自己维护的本机文件, 不需要网络。

    图片: 封面按公开 CDN 规则取; **官方图标**要先从 ``appcache/appinfo.vdf``
    读出 ``clienticon`` 哈希, 再去本机 ``steam/games/<哈希>.ico`` 拿(离线可用),
    本机没有才去 CDN 取同哈希的资源。拿不到哈希时只给封面, 后台会裁封面兜底。
    """

    def __init__(
        self,
        roots: ScanRoots,
        *,
        cloud: SaveCandidateSource | None = None,
        icons: Mapping[str, SteamIcons] | None = None,
    ) -> None:
        """绑定探测环境; 存档候选与官方图标来源都可注入, 便于用例替换成替身."""
        self._roots = roots
        self._cloud = SteamCloudSource(roots) if cloud is None else cloud
        self._icons = icons
        self._icons_loaded = icons is not None

    @property
    def platform(self) -> PlatformId:
        """平台标识: ``steam``."""
        return "steam"

    @property
    def supported(self) -> bool:
        """Steam 适配器已实现."""
        return True

    @property
    def unsupported_reason(self) -> str:
        """已实现, 因此没有原因说明."""
        return ""

    @property
    def supports_save_paths(self) -> bool:
        """Steam 能读云端同步清单推断存档路径."""
        return True

    @property
    def supports_artwork(self) -> bool:
        """Steam 有公开无凭据的 CDN 地址规则, 可以取封面."""
        return True

    def artwork_refs(self, game: PlatformGame) -> tuple[ArtworkRef, ...]:
        """返回封面与官方图标引用; 拿不到图标哈希时只给封面(界面裁封面兜底).

        图标哈希来自本机 ``appinfo.vdf``: 解析一次后缓存在适配器实例里, 所以
        每款游戏只付一次字典查找。
        """
        if game.platform != "steam":  # pragma: no branch - 只有非 Steam 会命中
            return ()
        if not game.game_id:  # pragma: no cover - id 非空由 PlatformGame 保证
            return ()
        icons = self._steam_icons().get(game.game_id)
        if icons is None:
            return steam_artwork(game.game_id)
        return steam_artwork(
            game.game_id, icons.clienticon, local_path=icons.local_path
        )

    def _steam_icons(self) -> Mapping[str, SteamIcons]:
        """本机各 Steam 主目录登记的官方图标; 第一次用到时才解析 ``appinfo.vdf``."""
        if not self._icons_loaded:
            self._icons = load_steam_icons(self._roots)
            self._icons_loaded = True
        return {} if self._icons is None else self._icons

    def list_games(self) -> list[PlatformGame]:
        """返回本机已安装的 Steam 游戏.

        没有 AppID 的清单(文件名不符合 ``appmanifest_<数字>.acf``)会被跳过: 没有
        平台标识就无法继续查云端清单或缓存资源, 留着只会产出一条半成品数据。
        """
        try:
            installs = read_steam_installs(self._roots)
        except Exception as exc:  # 适配器边界: 意外错误只降级, 不抛给界面
            logger.debug("读取 Steam 应用清单失败: %s", exc)
            return []
        return [
            PlatformGame(
                platform="steam",
                game_id=install.app_id,
                name=install.name,
                install_dir=normalize_path(str(install.install_dir)),
            )
            for install in installs
            if install.app_id
        ]

    def save_candidates(self, game: PlatformGame) -> list[SavePathCandidate]:
        """返回该游戏的存档路径候选(来自云端同步清单)."""
        if game.platform != "steam":
            logger.debug("Steam 适配器收到其它平台的数据, 已忽略: %s", game.platform)
            return []
        try:
            return self._cloud.candidates(
                game.game_id, install_dir=self._install_dir(game)
            )
        except Exception as exc:  # 同上: 清单损坏不应影响手动管理
            logger.debug("解析 Steam 云端清单失败(%s): %s", game.game_id, exc)
            return []

    @staticmethod
    def _install_dir(game: PlatformGame) -> Path | None:
        """返回记录里的安装目录(root 1 的解析依据); 缺省时交给云端来源查找."""
        return Path(game.install_dir) if game.install_dir else None


class UnsupportedPlatformAdapter:
    """未实现平台的显式降级.

    保留这个类(而不是干脆不注册平台)有两个好处: 平台矩阵在日志与界面里是完整的
    ——用户看到的是"Epic 暂不支持", 而不是"什么都没有"; 契约用例也能用同一套断言
    覆盖降级路径。
    """

    def __init__(self, platform: PlatformId, *, reason: str = "") -> None:
        """记录平台标识与降级原因."""
        self._platform = platform
        self._reason = reason or DEFAULT_UNSUPPORTED_REASON

    @property
    def platform(self) -> PlatformId:
        """平台标识."""
        return self._platform

    @property
    def supported(self) -> bool:
        """未实现."""
        return False

    @property
    def unsupported_reason(self) -> str:
        """降级原因(界面据此告诉用户为什么没有结果)."""
        return self._reason

    @property
    def supports_save_paths(self) -> bool:
        """未实现, 不推断存档路径."""
        return False

    @property
    def supports_artwork(self) -> bool:
        """未实现, 不提供封面与图标."""
        return False

    def artwork_refs(self, game: PlatformGame) -> tuple[ArtworkRef, ...]:
        """始终返回空元组, 并记一条 DEBUG 日志."""
        logger.debug(
            "%s 适配器%s, 未产出图片资源(%s)", self._platform, self._reason, game.name
        )
        return ()

    def list_games(self) -> list[PlatformGame]:
        """始终返回空列表, 并记一条 DEBUG 日志."""
        logger.debug("%s 适配器%s, 未返回游戏列表", self._platform, self._reason)
        return []

    def save_candidates(self, game: PlatformGame) -> list[SavePathCandidate]:
        """始终返回空列表, 并记一条 DEBUG 日志."""
        logger.debug(
            "%s 适配器%s, 未产出存档候选(%s)", self._platform, self._reason, game.name
        )
        return []


def default_adapters(roots: ScanRoots) -> dict[PlatformId, PlatformAdapter]:
    """构建平台标识到适配器的映射(本期只有 Steam 有实现)."""
    adapters: dict[PlatformId, PlatformAdapter] = {"steam": SteamAdapter(roots)}
    for platform in PLATFORM_IDS:
        adapters.setdefault(platform, UnsupportedPlatformAdapter(platform))
    return adapters


def adapter_for(
    adapters: Mapping[PlatformId, PlatformAdapter], platform: str
) -> PlatformAdapter | None:
    """按来源标识取适配器; 非平台来源(监控目录/手动添加)返回 ``None``.

    界面与后端都通过它判断"能不能自动探测": 拿不到适配器, 或适配器不支持某项
    能力, 就一律走手动路径, 而不是拿别的平台的数据凑一个结果。
    """
    for name, adapter in adapters.items():
        if name == platform:
            return adapter
    return None
