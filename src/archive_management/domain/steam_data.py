"""Steam 数据配置模型 (阶段 C 预留).

阶段 F 才会接入网络读取; 本模块只定义从本地数据文件读取时需要的
版本化模型与字段校验边界(PLAN 6-F), 不包含任何网络访问逻辑.
"""

from __future__ import annotations

from pydantic import Field, field_validator

from archive_management.domain.entities import _RowModel

STEAM_DATA_FORMAT_VERSION = 1


class SteamGameEntry(_RowModel):
    """Steam 数据文件中的单个游戏条目."""

    app_id: int = Field(gt=0)
    name: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def _strip_name(cls, value: str) -> str:
        """去除首尾空白; 内容为空视为非法."""
        value = value.strip()
        if not value:
            raise ValueError("name 不能为空")
        return value


class SteamDataFile(_RowModel):
    """一份版本化的本地 Steam 数据文件."""

    version: int = STEAM_DATA_FORMAT_VERSION
    games: list[SteamGameEntry] = Field(default_factory=list)
