"""领域模型.

领域层只包含纯数据模型与序列化格式, 不调用 Tkinter、HTTP 或文件系统
模型使用 pydantic 严格校验, 确保来自数据库行、JSON 或用户输入的字段
在进入文件操作流程前都是完整且受控的
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PathKind = Literal["file", "directory"]
SaveSource = Literal["steam", "manual"]
NodeKind = Literal["manual", "branch", "auto"]
FileKind = Literal["file", "symlink", "directory"]

# 备份/恢复的校验方式:
#
# - ``sha256``: 逐文件比对内容哈希(严格; 备份时边拷边算, 还原前要重新读一遍快照);
# - ``name``:   只核对清单里的名称/类型(快; 不读快照内容, 因此查不出"内容被改过").
#
# 取值会落进配置文件与数据库, 因此**只增不改**: 将来新增方式(CRC32 之类)时, 老备份
# 记录里没有它的数据, 读取侧一律按 :data:`DEFAULT_VERIFICATION_MODE` 回落。
VerificationMode = Literal["sha256", "name"]
DEFAULT_VERIFICATION_MODE: VerificationMode = "sha256"
VERIFICATION_MODES: tuple[VerificationMode, ...] = ("sha256", "name")


def normalize_verification_mode(value: object) -> VerificationMode:
    """把配置/数据库里读到的值收敛成一个可用取值.

    空串与不认识的值都回落到 :data:`DEFAULT_VERIFICATION_MODE`: 数据库里备份节点的
    ``verify_mode`` 在升级前的行上是空串(见 ``infrastructure.schema`` 的 v15), 手改
    配置文件也可能写进任何东西 —— 这两种情况都必须回到"最严格且数据最全"的 sha256,
    而不是静默降级成只校验名称。
    """
    if isinstance(value, str) and value in VERIFICATION_MODES:
        return value
    return DEFAULT_VERIFICATION_MODE


class _RowModel(BaseModel):
    """所有领域模型的公共基类: 拒绝未知字段, 支持按属性构造."""

    model_config = ConfigDict(extra="forbid", from_attributes=True)


class Game(_RowModel):
    """一个被管理的游戏及其启用状态."""

    id: int | None = None
    name: str = Field(min_length=1)
    steam_app_id: int | None = None
    platform: str = "windows"
    # 默认停用: 同一时刻只允许一款游戏处于启用态(快捷键与定时备份只对它生效),
    # 因此新建的游戏都需要用户明确启用, 不做默认选中。
    enabled: bool = False
    created_at: datetime | None = None
    # 录入时识别到的原始名称(重命名不会改写), 供界面作为额外信息展示.
    original_name: str = ""
    # 上一次由程序写入的译名(用户改名时清空): 只有名称还等于它时才允许被新译名
    # 覆盖 —— 否则切换语言后, 程序自己写的译名会被当成"用户改的名字"而不再更新。
    localized_name: str = ""
    # 备份根目录下该游戏实际使用的目录名(首次备份时确定, 之后保持不变).
    storage_key: str = ""
    # 游戏来源平台: steam/epic/gog/ubisoft/monitored/manual(主页分类用).
    origin: str = "manual"
    # 用户自定义标签(主页分类), 落库时按逗号拼接, 因此标签内不含逗号.
    tags: tuple[str, ...] = ()
    # 归档等价于"从主页收起来": 记录与备份都保留, 只是默认列表不再显示.
    archived: bool = False
    # 最近一次与该游戏相关的动作(备份/恢复/修改)发生的时间.
    last_activity_at: datetime | None = None


class SaveLocation(_RowModel):
    """一个游戏关联的存档文件/文件夹路径."""

    id: int | None = None
    game_id: int
    path: str = Field(min_length=1)
    path_kind: PathKind = "directory"
    source: SaveSource = "manual"
    is_primary: bool = False
    last_checked_at: datetime | None = None
    last_check_status: str | None = None


class BackupNode(_RowModel):
    """分支树/时间线上的一个备份节点."""

    id: int | None = None
    game_id: int
    parent_id: int | None = None
    node_kind: NodeKind = "manual"
    branch_name: str | None = None
    created_at: datetime | None = None
    # 备份名称(用户可编辑); 为空时展示层回退到分支名或类型默认名.
    title: str = ""
    # 备份描述/备注(用户可编辑, 长度上限由用例校验).
    note: str = ""
    content_hash: str | None = None
    # 相对应用备份根目录的路径, 保证备份存储可整体迁移.
    storage_relpath: str | None = None
    # 恢复前自动创建的安全点: 只在时间线展示, 不进入分支树的线路关系.
    is_safety: bool = False
    # 生成这份快照时用的校验方式: 名称模式下清单里的 sha256 先留空(备份更快), 由
    # 后台协程补齐; 升级前创建的老备份在库里是空串, 读出来按 sha256 看待。
    verify_mode: VerificationMode = DEFAULT_VERIFICATION_MODE

    @field_validator("verify_mode", mode="before")
    @classmethod
    def _normalize_verify_mode(cls, value: object) -> VerificationMode:
        """把库里的空串(升级前的老行)与陌生取值一律收敛成 sha256.

        收敛放在模型上而不是各个读取方: 备份列表/恢复预检/导出都会构造它, 只要有一处
        漏了回落, 老备份就会被当成"没有可用的校验数据"。
        """
        return normalize_verification_mode(value)


class BackupFileEntry(_RowModel):
    """备份内单个文件的清单项, 供恢复前校验."""

    id: int | None = None
    backup_id: int
    relative_path: str = Field(min_length=1)
    size: int = Field(default=0, ge=0)
    # 内容哈希; **允许为空串**: 名称模式下的备份先不留哈希(后台协程随后补齐), 补齐前
    # 这一列就是空的 —— 它与"升级前的老存档没有新校验数据"是同一种状态(见
    # ``services.snapshot.SnapshotEntry.sha256``)。
    sha256: str = ""
    file_kind: FileKind = "file"


class ScheduledJob(_RowModel):
    """一个定期备份任务的配置与运行状态."""

    id: int | None = None
    game_id: int | None = None
    schedule: str = Field(min_length=1)
    enabled: bool = True
    last_run_at: datetime | None = None
    next_run_at: datetime | None = None
    last_error: str | None = None
    # 自动备份是特殊备份: 只保留最近 N 份(默认 3, 允许用户自定义).
    keep_auto: int = Field(default=3, ge=1, le=50)
