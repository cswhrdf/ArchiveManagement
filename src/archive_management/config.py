"""应用配置的加载、校验与持久化.

配置以 JSON 保存在应用配置目录, 使用 pydantic 严格校验: 未知字段直接拒绝,
防止不完整或恶意字段进入后续文件操作.

校验失败有三种处理方式, 由调用方选择:

- :func:`load_config` 直接把错误抛给调用方(测试与需要严格语义的场景);
- :func:`repair_config` 逐字段修复: 非法值落回默认值、不认识的键(非设置相关的
  内容)直接删除, 其余自定义配置原样保留;
- :func:`load_or_repair_config` 在读取时直接改用上面的修复逻辑——手改配置写坏了
  不应该让应用起不来, 但也不能静默丢掉用户的改动, 因此原文件会先改名成
  ``config.json.invalid`` 保留下来; 只有整份文件都读不出来(JSON 坏了 / 顶层不是
  对象 / 版本不认识)时才整份还原为默认值。

另外, **按本机算出来的取值**(目前是校验并发数, 见 :class:`VerificationSettings`)在文件
里还没有时会被算一次并写回文件: 这类取值来自硬件探测, 每次构造配置都会重算, 不落盘就
会出现"用户没改过、数字却变了"的情况。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
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

from archive_management.domain import (
    DEFAULT_VERIFICATION_MODE,
    VerificationMode,
)
from archive_management.exceptions import ConfigurationError
from archive_management.i18n import DEFAULT_LOCALE, available_locales
from archive_management.services.hotkeys import (
    DEFAULT_BRANCH_ACCELERATOR,
    DEFAULT_SAVE_ACCELERATOR,
    combo_error,
    parse_accelerator,
)
from archive_management.services.verification import (
    MAX_PARALLEL,
    MIN_PARALLEL,
    recommended_parallel,
)

logger = logging.getLogger(__name__)

ThemeName = Literal["system", "light", "dark"]

CONFIG_FORMAT_VERSION = 1
CONFIG_FILENAME = "config.json"
# 内容非法时原配置文件的保留后缀(与 config.json 同目录).
INVALID_CONFIG_SUFFIX = ".invalid"
# 基准字号(px 口径, 1rem 的默认值): 界面上所有字号都按它等比缩放, 取值由
# ``ui.base_font_px`` 控制。放在这里是为了让"默认值"只有一个来源: 配置模型与
# 界面层都引用它(见 ui.typography)。
DEFAULT_BASE_FONT_PX = 16
# 允许用户选择的基准字号(设置窗口的下拉项).
BASE_FONT_CHOICES = (12, 14, 16, 18, 20, 24, 28)
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


class ActivationSettings(BaseModel):
    """游戏启停配置."""

    model_config = ConfigDict(extra="forbid")

    # 按进程自动启停(默认关闭): 开启后监控全部已导入(且有存档位置)的游戏, 按启动
    # 顺序接管/回落。启用态决定快捷键与定时备份落到哪一款
    # 游戏上, 因此自动接管必须由用户明确打开。
    auto: bool = False


class UiSettings(BaseModel):
    """界面外观配置."""

    model_config = ConfigDict(extra="forbid")

    # 基准字号(px 口径, 1rem): 界面里所有字号都按它等比缩放(见 ui.typography)。
    # 下限 11 保证正文仍然可读, 上限 28 避免固定宽度的行被撑破。
    base_font_px: int = Field(default=DEFAULT_BASE_FONT_PX, ge=11, le=28)
    # 记住主窗口关闭时的尺寸与位置(默认开启)。关掉后下次启动用设计尺寸, 并且**把已经
    # 记下的那一套从配置里删掉** —— 留着它会让"我明明关了"与配置里还写着位置互相矛盾
    # (见 ui.main_window._save_remember_window)。
    remember_window: bool = True


class VerificationSettings(BaseModel):
    """备份与还原的校验方式."""

    model_config = ConfigDict(extra="forbid")

    # 校验方式(取值见 domain.entities.VERIFICATION_MODES): ``sha256`` 逐文件比对内容
    # 哈希 —— 严格, 但要把快照完整读一遍; ``name`` 只核对名称与类型 —— 快, 但查不出
    # "内容被改过"。两种方式随时可切: 备份时记录的数据与选了哪种方式无关。
    mode: VerificationMode = DEFAULT_VERIFICATION_MODE
    # 同时进行的校验任务数。每个任务都要完整读一个文件, 同时跑太多会一起抢磁盘、
    # 堆内存(见 services.verification 的协程执行器)。默认值是**按本机算出来的**
    # (CPU 的一半核心与可用内存取小), 首次启动就写进配置文件, 之后一律从配置读 ——
    # 否则每次启动都重算, 用户看到的数字会无缘无故地变。
    max_parallel: int = Field(
        default_factory=recommended_parallel, ge=MIN_PARALLEL, le=MAX_PARALLEL
    )


# 记住的窗口几何的取值上限: 真机上的多屏虚拟桌面也不会超出它(坐标还可以是负的),
# 手改配置写个天文数字没有意义, 直接拒绝。
MAX_WINDOW_VALUE = 20000


class WindowSettings(BaseModel):
    """主窗口上次关闭时的尺寸与位置: 关窗时写入, 下次打开照它摆.

    **四项缺一项就当没记过** —— 只有宽高没有位置(或反过来)拼不出一个完整的窗口,
    所以每个字段都可以是 ``None``, 由 :meth:`geometry` 统一判齐。这也让"逐字段修复"
    能把写坏的那一项单独剔除、其余三项留在文件里(下次关窗再补上)。

    最大化/最小化/全屏时关闭**不写**这一项(跳过 = 保留上一次记下的值), 见
    :func:`archive_management.ui.main_window.current_window_geometry`。
    """

    model_config = ConfigDict(extra="forbid")

    width: int | None = Field(default=None, ge=1, le=MAX_WINDOW_VALUE)
    height: int | None = Field(default=None, ge=1, le=MAX_WINDOW_VALUE)
    # 位置可以是**负数**: 摆在主屏左边的显示器上时坐标就是负的, 所以这里不设 ge=0。
    x: int | None = Field(default=None, ge=-MAX_WINDOW_VALUE, le=MAX_WINDOW_VALUE)
    y: int | None = Field(default=None, ge=-MAX_WINDOW_VALUE, le=MAX_WINDOW_VALUE)

    def geometry(self) -> tuple[int, int, int, int] | None:
        """四项齐全时返回 ``(宽, 高, x, y)``; 缺一项就返回 ``None``(当作没记过)."""
        width, height, x, y = self.width, self.height, self.x, self.y
        if width is None or height is None or x is None or y is None:
            return None
        return (width, height, x, y)


class AppConfig(BaseModel):
    """应用级配置."""

    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=CONFIG_FORMAT_VERSION, ge=1)
    theme: ThemeName = "system"
    # 界面语言(同时决定游戏译名向哪种语言探测); 只允许有文案资源的 locale.
    language: str = DEFAULT_LOCALE
    logging: LoggingSettings = Field(default_factory=LoggingSettings)
    hotkeys: HotkeySettings = Field(default_factory=HotkeySettings)
    activation: ActivationSettings = Field(default_factory=ActivationSettings)
    ui: UiSettings = Field(default_factory=UiSettings)
    # 备份/还原的校验方式与并发上限(见 VerificationSettings)。
    verification: VerificationSettings = Field(default_factory=VerificationSettings)
    # 上次关闭时的窗口尺寸与位置(默认全空 = 没记过, 用设计尺寸打开)。
    window: WindowSettings = Field(default_factory=WindowSettings)

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
    """一次配置读取的结果(含被修复或还原过的字段)."""

    config: AppConfig
    # 整份文件读不出来(JSON 坏了 / 顶层不是对象 / 版本不认识)而整份还原为默认值。
    reset: bool = False
    # 逐字段修复时被删掉或还原为默认值的字段路径(含不认识的键)。
    repaired: tuple[str, ...] = ()
    # 被保留下来的原配置文件(没修复/没还原时为空)。
    backup: Path | None = None


def repair_config(
    raw: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[str, ...]]:
    """逐字段修复一份配置字典: 合法值原样保留, 非法值与陌生键剔除.

    返回(修复后的字典, 问题字段路径)。与 :func:`parse_config` 的"要么全对要么全错"
    不同: 手改配置只写错一项时不该把整份配置都丢掉, 因此每个字段单独校验一次,
    失败就落回默认值; 不认识的键(非设置相关的内容)直接删除。嵌套段递归下去。
    """
    notes: list[str] = []
    cleaned = _repair_model(AppConfig, dict(raw), "", notes)
    return cleaned, tuple(dict.fromkeys(notes))


def _repair_model(
    model: type[BaseModel], raw: dict[str, Any], prefix: str, notes: list[str]
) -> dict[str, Any]:
    """按字段清洗一段配置: 只保留认识的键, 每个叶子单独校验."""
    fields = model.model_fields
    result: dict[str, Any] = {}
    notes.extend(f"{prefix}{name}" for name in raw if name not in fields)
    # 只替换一个字段、其余取默认值: 这样验的就是这一个字段本身。
    defaults = model().model_dump(mode="json")
    for name, field in fields.items():
        if name not in raw:
            continue
        value = raw[name]
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            if not isinstance(value, dict):
                notes.append(f"{prefix}{name}")
                continue
            result[name] = _repair_model(annotation, value, f"{prefix}{name}.", notes)
            continue
        try:
            model.model_validate({**defaults, name: value})
        except ValidationError:
            notes.append(f"{prefix}{name}")
            continue
        result[name] = value
    return result


def load_or_repair_config(path: Path) -> ConfigLoad:
    """读取配置; 非法内容**逐个字段**修复, 合法的部分原样保留.

    - 文件不存在 → 默认值(不创建文件);
    - 整份读不出来(JSON 坏了 / 顶层不是对象 / 版本不认识) → 整份还原为默认值,
      原文件先改名成 ``config.json.invalid`` 保留(:attr:`ConfigLoad.reset`);
    - 能读出对象 → 逐字段修复: 不认识的键(非设置相关的内容)与非法值被剔除并落回
      默认值, 其余保留; 修复过就把清理后的内容写回文件, 同样留一份 ``.invalid``
      备份, 并把问题字段列在 :attr:`ConfigLoad.repaired` 里给界面提示用。

    配置读取必须始终能交回一份可用配置, 绝不能让应用起不来; 写回失败也只记录日志。
    """
    if not path.exists():
        return ConfigLoad(config=AppConfig())
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return _reset_to_defaults(path, reason=f"无法读取: {exc}")
    if not isinstance(raw, dict):
        return _reset_to_defaults(path, reason="顶层不是对象")
    cleaned, notes = repair_config(raw)
    try:
        config = AppConfig.model_validate(cleaned)
    except ValidationError as exc:  # pragma: no cover - 逐字段修复后不该再有非法值
        return _reset_to_defaults(path, reason=f"修复后仍不合法: {exc}")
    if config.version != CONFIG_FORMAT_VERSION:
        return _reset_to_defaults(path, reason=f"不支持的文件版本 {config.version}")
    if not notes:
        derived = _missing_derived_fields(raw)
        if derived:
            _save_derived(config, path, derived)
        return ConfigLoad(config=config)
    logger.warning("配置文件有 %d 项需要修复: %s", len(notes), ", ".join(notes))
    backup = _preserve_invalid(path)
    try:
        save_config(config, path)
    except OSError as exc:  # pragma: no cover - 取决于文件系统
        logger.warning("写回修复后的配置失败, 本次仅使用内存中的取值: %s", exc)
    return ConfigLoad(config=config, repaired=notes, backup=backup)


def _missing_derived_fields(raw: Mapping[str, Any]) -> tuple[str, ...]:
    """列出"该按本机算一次并写进文件, 但文件里还没有"的字段路径.

    目前只有一项: 校验并发数。它来自硬件探测(见
    ``services.verification.recommended_parallel``), 而 pydantic 的默认值每次构造配置
    都会重算 —— 不落盘的话, 用户改了别的设置把配置写回时这个数字会跟着当时的机器状态
    悄悄变掉, 设置里显示的与实际用的也就对不上了。
    """
    section = raw.get("verification")
    if isinstance(section, Mapping) and "max_parallel" in section:
        return ()
    return ("verification.max_parallel",)


def _save_derived(config: AppConfig, path: Path, fields: tuple[str, ...]) -> None:
    """把按本机算出来的取值写回配置文件(写不进去只记录日志, 本次仍用内存里的取值)."""
    try:
        save_config(config, path)
    except OSError as exc:
        logger.warning("写回按本机算出的配置失败(%s): %s", ", ".join(fields), exc)
        return
    logger.info("已按本机情况写入配置: %s", ", ".join(fields))


def _reset_to_defaults(path: Path, *, reason: str) -> ConfigLoad:
    """整份还原为默认值, 原文件改名保留."""
    logger.warning("配置文件无法使用(%s), 已还原为默认值", reason)
    backup = _preserve_invalid(path)
    config = AppConfig()
    try:
        save_config(config, path)
    except OSError as exc:  # pragma: no cover - 取决于文件系统
        logger.warning("写回默认配置失败, 本次仅使用内存中的默认值: %s", exc)
    return ConfigLoad(config=config, reset=True, backup=backup)


def _preserve_invalid(path: Path) -> Path | None:
    """把非法的配置文件改名保留; 失败时只记录日志并返回 None."""
    if not path.exists():  # pragma: no branch - 调用方只在文件损坏时调它
        return None  # pragma: no cover - 这条分支永远走不到
    backup = path.with_suffix(path.suffix + INVALID_CONFIG_SUFFIX)
    try:
        path.replace(backup)
    except OSError as exc:  # pragma: no cover - 取决于文件系统
        logger.warning("保留非法配置文件失败: %s", exc)
        return None
    return backup
