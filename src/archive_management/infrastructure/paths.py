"""跨平台应用目录.

默认基于 :mod:`platformdirs` 解析系统目录;传入 ``override_root`` 时
(便携模式、开发或测试)将所有数据收敛到指定根目录, 既避免污染系统
目录, 也让集成测试完全可控.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from platformdirs import user_cache_dir, user_config_dir, user_data_dir, user_log_dir

from archive_management.config import CONFIG_FILENAME
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
        return self.data_dir / "backups"

    @property
    def exports_dir(self) -> Path:
        """应用自有导出根目录: 自动导出的归档包默认落在这里(备份目录的旁边)."""
        return self.data_dir / "exports"

    def ensure(self) -> ApplicationPaths:
        """创建全部目录(含备份根与导出根), 幂等."""
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
