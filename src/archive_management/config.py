"""应用配置的加载、校验与持久化.

配置以 JSON 保存在应用配置目录, 使用 pydantic 严格校验: 未知字段直接拒绝,
防止不完整或恶意字段进入后续文件操作(PLAN 第 7 节).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from archive_management.exceptions import ConfigurationError

ThemeName = Literal["system", "light", "dark"]

CONFIG_FORMAT_VERSION = 1
CONFIG_FILENAME = "config.json"


class LoggingSettings(BaseModel):
    """日志滚动参数."""

    model_config = ConfigDict(extra="forbid")

    level: str = Field(default="INFO", pattern=r"^(DEBUG|INFO|WARNING|ERROR)$")
    max_bytes: int = Field(default=1_048_576, ge=1)
    backup_count: int = Field(default=5, ge=0)
    console: bool = True


class AppConfig(BaseModel):
    """应用级配置."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=CONFIG_FORMAT_VERSION, ge=1)
    theme: ThemeName = "system"
    logging: LoggingSettings = Field(default_factory=LoggingSettings)


def parse_config(raw: dict[str, Any]) -> AppConfig:
    """将反序列化得到的字典校验为 :class:`AppConfig`."""
    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise ConfigurationError(f"配置文件内容非法: {exc}") from exc
    if config.version != CONFIG_FORMAT_VERSION:
        raise ConfigurationError(
            f"不支持的配置文件版本 {config.version}, 期望 {CONFIG_FORMAT_VERSION}"
        )
    return config


def load_config(path: Path) -> AppConfig:
    """从 JSON 文件加载配置;文件不存在时返回默认配置, 内容非法时抛异常."""
    if not path.exists():
        return AppConfig()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"无法读取配置文件 {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError(f"配置文件 {path} 顶层必须是对象")
    return parse_config(raw)


def save_config(config: AppConfig, path: Path) -> None:
    """原子写入配置, 避免中断留下半截文件."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(config.model_dump_json(indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
