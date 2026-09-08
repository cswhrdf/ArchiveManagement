"""Steam 数据读取边界 (阶段 C 预留, 阶段 F 落地).

定义读取"Steam 数据配置文件"的统一接口; 本阶段只提供从本地 JSON
文件读取并校验的默认实现, 不发起任何网络请求(PLAN 6-F). 读取失败、
版本不符或字段缺失时抛出 :class:`SteamIntegrationError`, 不应影响
手动添加游戏与路径管理流程.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from archive_management.domain import STEAM_DATA_FORMAT_VERSION, SteamDataFile
from archive_management.exceptions import SteamIntegrationError


@runtime_checkable
class SteamDataReader(Protocol):
    """读取 Steam 游戏数据配置的边界接口."""

    def read(self, source: Path) -> SteamDataFile:
        """从给定来源读取并校验一份 Steam 数据文件."""
        ...


class LocalSteamDataReader:
    """从本地 JSON 文件读取版本化 Steam 数据的实现."""

    def read(self, source: Path) -> SteamDataFile:
        """读取并校验数据文件.

        文件缺失、无法解析或字段非法时抛出 :class:`SteamIntegrationError`.
        """
        if not source.is_file():
            raise SteamIntegrationError(f"Steam 数据文件不存在: {source}")
        try:
            raw = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SteamIntegrationError(f"Steam 数据文件无法解析: {exc}") from exc
        try:
            data = SteamDataFile.model_validate(raw)
        except ValidationError as exc:
            raise SteamIntegrationError(f"Steam 数据文件字段非法: {exc}") from exc
        if data.version != STEAM_DATA_FORMAT_VERSION:
            raise SteamIntegrationError(f"不支持的 Steam 数据版本: {data.version}")
        return data
