"""跨平台应用目录.

默认基于 :mod:`platformdirs` 解析系统目录;传入 ``override_root`` 时
(便携模式、开发或测试)将所有数据收敛到指定根目录, 既避免污染系统
目录, 也让集成测试完全可控.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Literal

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_log_dir

from archive_management.config import CONFIG_FILENAME, AppConfig
from archive_management.packaging import APP_NAME

_CONFIG_SUBDIR = "config"
_DATA_SUBDIR = "data"
_LOG_SUBDIR = "logs"
_CACHE_SUBDIR = "cache"
_DB_FILENAME = "archive-management.db"


@dataclass(frozen=True)
class ApplicationPaths:
    """一组相互关联的应用目录与文件路径(全部为规范化绝对路径)."""

    config_dir: Path
    data_dir: Path
    log_dir: Path
    cache_dir: Path
    # 备份根覆盖(None = 内置 data_dir/backups), 只经 with_backup_root() 设置。
    # 其余派生路径(数据库/配置/导出/封面)一律不跟着变 —— 那是 P2b 的范围红线:
    # 只搬备份, 不搬数据目录, 避免"配置在新目录、锁在旧目录"的状态分裂。
    backup_root_override: Path | None = None

    @property
    def database_path(self) -> Path:
        """返回 SQLite 数据库文件路径."""
        return self.data_dir / _DB_FILENAME

    @property
    def config_path(self) -> Path:
        """返回 JSON 配置文件路径."""
        return self.config_dir / CONFIG_FILENAME

    @property
    def backup_root(self) -> Path:
        """应用自有备份存储根目录;绝不直接写用户原始目录."""
        if self.backup_root_override is not None:
            return self.backup_root_override
        return self.data_dir / "backups"

    def with_backup_root(self, path: Path) -> ApplicationPaths:
        """返回备份根改为 ``path`` 的新实例(规范化绝对路径), 其余字段不变.

        frozen dataclass 不能原地改, 这里按"替换"语义生成新实例; 调用方随后
        ``ensure()`` 会把新备份根建出来(幂等)。
        """
        return replace(self, backup_root_override=Path(path).expanduser().resolve())

    @property
    def exports_dir(self) -> Path:
        """应用自有导出根目录: 自动导出的归档包默认落在这里(备份目录的旁边)."""
        return self.data_dir / "exports"

    @property
    def artwork_dir(self) -> Path:
        """用户自己指定的封面/图标根目录.

        存在 ``data_dir`` 而不是 ``cache_dir``: 缓存会按年龄/容量清理, 而用户选的那张
        图是用户数据, 不该被清理掉。
        """
        return self.data_dir / "artwork"

    def ensure(self) -> ApplicationPaths:
        """创建全部目录(含备份根、导出根与自定义图片根), 幂等."""
        for directory in (
            self.config_dir,
            self.data_dir,
            self.log_dir,
            self.cache_dir,
            self.backup_root,
            self.exports_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        return self

    @classmethod
    def default(cls, *, override_root: Path | None = None) -> ApplicationPaths:
        """构造路径集合.

        ``override_root`` 为 ``None`` 时使用平台约定的系统目录;否则以该目录
        为根, 在其下组织 config/data/logs/cache 子目录.
        """
        if override_root is not None:
            root = Path(override_root)
            return cls(
                config_dir=root / _CONFIG_SUBDIR,
                data_dir=root / _DATA_SUBDIR,
                log_dir=root / _LOG_SUBDIR,
                cache_dir=root / _CACHE_SUBDIR,
            )
        appauthor: Literal[False] = False
        return cls(
            config_dir=Path(user_config_dir(APP_NAME, appauthor=appauthor)),
            data_dir=Path(user_data_dir(APP_NAME, appauthor=appauthor)),
            log_dir=Path(user_log_dir(APP_NAME, appauthor=appauthor)),
            cache_dir=Path(user_cache_dir(APP_NAME, appauthor=appauthor)),
        )


def effective_paths(base: ApplicationPaths, config: AppConfig) -> ApplicationPaths:
    """两步构造的第二步: 按配置里的 ``storage.backup_root`` 得出生效路径集合.

    第一步(基础 paths)必须先建好 —— 读配置本身需要它; 读出配置后再决定备份根
    是否覆盖。``--root`` 便携模式的压制不在这里: 调用方(CLI)明确指定 ``--root``
    时根本不调这个函数, 备份根跟随 ``--root``。
    """
    override = config.storage.backup_root
    if override is None:
        return base
    return base.with_backup_root(Path(override)).ensure()
