"""领域模型.

领域层只包含纯数据模型与序列化格式, 不调用 Tkinter、HTTP 或文件系统
模型使用 pydantic 严格校验, 确保来自数据库行、JSON 或用户输入的字段
在进入文件操作流程前都是完整且受控的
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

PathKind = Literal["file", "directory"]
SaveSource = Literal["steam", "manual"]
NodeKind = Literal["manual", "branch", "auto"]
FileKind = Literal["file", "symlink", "directory"]


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


class BackupFileEntry(_RowModel):
    """备份内单个文件的清单项, 供恢复前校验."""

    id: int | None = None
    backup_id: int
    relative_path: str = Field(min_length=1)
    size: int = Field(default=0, ge=0)
    sha256: str = Field(min_length=1)
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
