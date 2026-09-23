"""应用配置的加载、校验与持久化.

配置以 JSON 保存在应用配置目录, 使用 pydantic 严格校验: 未知字段直接拒绝,
防止不完整或恶意字段进入后续文件操作.

校验失败有两种处理方式, 由调用方选择:

- :func:`load_config` 直接把错误抛给调用方(测试与需要严格语义的场景);
- :func:`load_or_reset_config` 把文件**还原为默认值**并返回默认配置——手改配置
  写坏了不应该让应用起不来, 但也不能静默丢掉用户的改动, 因此原文件会先改名成
  ``config.json.invalid`` 保留下来。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

from archive_management.exceptions import ConfigurationError
from archive_management.i18n import DEFAULT_LOCALE, available_locales
from archive_management.services.hotkeys import (
    DEFAULT_BRANCH_ACCELERATOR,
    DEFAULT_SAVE_ACCELERATOR,
    combo_error,
    parse_accelerator,
)

logger = logging.getLogger(__name__)

ThemeName = Literal["system", "light", "dark"]

CONFIG_FORMAT_VERSION = 1
CONFIG_FILENAME = "config.json"
# 内容非法时原配置文件的保留后缀(与 config.json 同目录).
INVALID_CONFIG_SUFFIX = ".invalid"
# 日志滚动文件的上限(默认 100 MB): 单个日志文件写满后就轮转, 保留 backup_count 份。
# 开发调试时会写入大量 DEBUG 级基础操作, 上限太小会导致刚发生的问题很快被轮转掉。
DEFAULT_LOG_MAX_BYTES = 100 * 1024 * 1024


class LoggingSettings(BaseModel):
    """日志滚动参数.

    与其他配置段一样严格校验: 除下面这几个字段之外一律拒绝 —— 升级前用过的
    ``level`` 也在拒绝之列, 不做兼容读入。
    """

    model_config = ConfigDict(extra="forbid")

    # 调试日志开关(默认关闭): 关闭时日志文件与控制台都只留 INFO 及以上, 开启后
    # DEBUG 级的基础操作也会被记录 —— 排查问题时才需要, 平时没必要把日志写满。
    debug: bool = False
    max_bytes: int = Field(default=DEFAULT_LOG_MAX_BYTES, ge=1)
    backup_count: int = Field(default=5, ge=0)
    console: bool = True


class HotkeySettings(BaseModel):
    """全局快捷键配置(加速键文本格式见 :mod:`archive_management.services.hotkeys`)."""

    model_config = ConfigDict(extra="forbid")

    save: str = DEFAULT_SAVE_ACCELERATOR
    branch: str = DEFAULT_BRANCH_ACCELERATOR

    @field_validator("save", "branch")
    @classmethod
    def _validate_accelerator(cls, value: str) -> str:
        """拒绝无法解析或不满足组合规则的快捷键, 避免手改配置后无法注册."""
        if combo_error(parse_accelerator(value)) is not None:
            raise ValueError(f"不合法的快捷键: {value}")
        return value


class AppConfig(BaseModel):
    """应用级配置."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=CONFIG_FORMAT_VERSION, ge=1)
    theme: ThemeName = "system"
    # 界面语言(同时决定游戏译名向哪种语言探测); 只允许有文案资源的 locale.
    language: str = DEFAULT_LOCALE
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    hotkeys: HotkeySettings = Field(default_factory=HotkeySettings)

    @field_validator("language")
    @classmethod
    def _validate_language(cls, value: str) -> str:
        """拒绝没有文案资源的语言: 否则界面会整屏显示 i18n 的 key."""
        if value not in available_locales():
            raise ValueError(f"不支持的语言: {value}")
        return value


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


@dataclass(frozen=True)
class ConfigLoad:
    """一次配置读取的结果(含是否因内容非法而还原过)."""

    config: AppConfig
    # 内容非法时被还原为默认值; 界面据此提示用户, 而不是让他以为改动"没生效"。
    reset: bool = False
    # 被保留下来的非法配置文件(没有还原时为空)。
    backup: Path | None = None


def load_or_reset_config(path: Path) -> ConfigLoad:
    """读取配置; 内容非法时把文件还原为默认值并返回默认配置.

    非法内容不会被静默丢掉: 原文件先改名成 ``config.json.invalid`` 保留下来, 方便用户
    对照自己改错了什么。文件不存在时只返回默认值(不创建文件); 写回失败也只记录日志
    ——配置读取必须始终能交回一份可用配置, 绝不能让应用起不来。
    """
    try:
        return ConfigLoad(config=load_config(path))
    except ConfigurationError as exc:
        logger.warning("配置文件内容非法, 已还原为默认值: %s", exc)
        backup = _preserve_invalid(path)
        config = AppConfig()
        try:
            save_config(config, path)
        except OSError as write_exc:  # pragma: no cover - 取决于文件系统
            logger.warning("写回默认配置失败, 本次仅使用内存中的默认值: %s", write_exc)
        return ConfigLoad(config=config, reset=True, backup=backup)


def _preserve_invalid(path: Path) -> Path | None:
    """把非法的配置文件改名保留; 失败时只记录日志并返回 None."""
    if not path.exists():
        return None
    backup = path.with_suffix(path.suffix + INVALID_CONFIG_SUFFIX)
    try:
        path.replace(backup)
    except OSError as exc:  # pragma: no cover - 取决于文件系统
        logger.warning("保留非法配置文件失败: %s", exc)
        return None
    return backup
