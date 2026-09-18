"""游戏平台数据的版本化模型.

平台适配器(见 :mod:`archive_management.services.platform_adapters`)把各平台的
本地数据翻译成这里定义的模型, 上层(应用服务与界面)只依赖这套模型, 因此平台
差异被挡在适配器内部。

约定:

- **版本化**: 每份数据都带 ``version``; 版本不兼容时 :func:`parse_platform_game`
  显式抛错, 不静默降级成"缺字段的半个对象";
- **候选不是位置**: 存档路径出现在 ``save_paths`` 里只代表"有可信来源的候选",
  用户确认之前不得写入 ``save_locations``(与
  :class:`~archive_management.domain.GameCandidate` 同一套约定);
- **可解释**: 每条候选都带 ``reason_code`` 与 ``detail``, 界面据此说明来源;
- **纯数据**: 模型不触碰文件系统, 也不发起网络请求。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Literal

from pydantic import Field, ValidationError, field_validator

from archive_management.domain.discovery import Confidence
from archive_management.domain.entities import PathKind, _RowModel
from archive_management.exceptions import PlatformIntegrationError

# 平台数据格式版本: 字段增删改都必须同步调整, 让旧数据显式失败而不是被猜着读。
PLATFORM_DATA_FORMAT_VERSION = 1

# 本期支持的平台标识(与 domain.discovery.DiscoverySource 的前四项一致):
# 只有 steam 有真正的适配器实现, 其余平台走"暂不支持"的降级路径。
PlatformId = Literal["steam", "epic", "gog", "ubisoft"]
PLATFORM_IDS: tuple[PlatformId, ...] = ("steam", "epic", "gog", "ubisoft")

# 资源类型: 海报卡片用封面, 列表/标题用图标。
ArtworkKind = Literal["cover", "icon"]
ARTWORK_KINDS: tuple[ArtworkKind, ...] = ("cover", "icon")


class ArtworkRef(_RowModel):
    """封面或图标的引用.

    ``url`` 与 ``local_path`` 至少有一个: 本地已有的资源直接给路径, 本地缺失时
    给远端地址并由缓存层(阶段 G-4)决定何时下载; ``version`` 参与缓存命名, 地址
    规则变化时可以让旧缓存自然失效。
    """

    kind: ArtworkKind
    url: str = ""
    local_path: str = ""
    version: str = ""


class SavePathCandidate(_RowModel):
    """一条来自平台可信来源的存档路径候选.

    ``relative_path`` 保留"相对来源根"的原始写法(例如 Steam 云端清单里的
    ``Teardown/savegame.xml``): 它既能向用户解释候选是怎么来的, 也是来源规则
    变化时排查问题的依据。
    """

    path: str = Field(min_length=1)
    # 得出该候选的规则代码(如 steam_remotecache), 供界面解释来源。
    reason_code: str = ""
    # 附加说明(来源文件、平台账号等), 不包含凭据。
    detail: str = ""
    confidence: Confidence = "high"
    path_kind: PathKind = "directory"
    relative_path: str = ""


class PlatformGame(_RowModel):
    """一个平台游戏的身份、安装信息与候选存档路径.

    ``game_id`` 是**平台自己的标识**(Steam 为 AppID 字符串): 用它去查云端清单、
    缓存资源, 也用它做平台内的去重; 与本地数据库自增 id 无关。
    """

    version: int = PLATFORM_DATA_FORMAT_VERSION
    platform: PlatformId
    game_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    install_dir: str = ""
    artwork: list[ArtworkRef] = Field(default_factory=list)
    save_paths: list[SavePathCandidate] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """去除首尾空白; 内容为空视为非法(与 Steam 数据文件同一规则)."""
        value = value.strip()
        if not value:
            raise ValueError("name 不能为空")
        return value


def parse_platform_game(payload: Mapping[str, object]) -> PlatformGame:
    """解析一份平台游戏数据.

    版本不兼容或字段非法时抛出 :class:`PlatformIntegrationError`: 平台数据来自
    外部(文件、缓存、未来的 API), 宁可显式失败也不要让缺字段的对象流进备份流程。
    """
    version = payload.get("version", PLATFORM_DATA_FORMAT_VERSION)
    if version != PLATFORM_DATA_FORMAT_VERSION:
        raise PlatformIntegrationError(f"不支持的平台数据版本: {version}")
    try:
        return PlatformGame.model_validate(payload)
    except ValidationError as exc:
        raise PlatformIntegrationError(f"平台数据字段非法: {exc}") from exc
