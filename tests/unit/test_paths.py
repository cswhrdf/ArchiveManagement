"""应用目录单元测试: 覆盖根布局与 ensure 行为."""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.config import AppConfig
from archive_management.infrastructure.paths import (
    ApplicationPaths,
    effective_paths,
)

pytestmark = [
    pytest.mark.paths,
    pytest.mark.normal,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("应用目录"),
    pytest.mark.story("应用数据目录布局"),
    pytest.mark.layer("unit"),
]

_DB_NAME = "archive-management.db"


def test_override_root_layout(tmp_path: Path) -> None:
    paths = ApplicationPaths.default(override_root=tmp_path)
    assert paths.config_dir == tmp_path / "config"
    assert paths.data_dir == tmp_path / "data"
    assert paths.log_dir == tmp_path / "logs"
    assert paths.cache_dir == tmp_path / "cache"
    assert paths.database_path == tmp_path / "data" / _DB_NAME
    assert paths.backup_root == tmp_path / "data" / "backups"
    assert paths.exports_dir == tmp_path / "data" / "exports"
    assert paths.config_path == tmp_path / "config" / "config.json"


def test_exports_dir_sits_next_to_the_backups(tmp_path: Path) -> None:
    """自动导出的落点与备份目录是同一个父目录下的两个兄弟目录."""
    paths = ApplicationPaths.default(override_root=tmp_path)
    assert paths.exports_dir.parent == paths.backup_root.parent == paths.data_dir
    assert paths.exports_dir != paths.backup_root


def test_ensure_creates_directories(tmp_path: Path) -> None:
    paths = ApplicationPaths.default(override_root=tmp_path / "root").ensure()
    for directory in (
        paths.config_dir,
        paths.data_dir,
        paths.log_dir,
        paths.cache_dir,
        paths.backup_root,
        paths.exports_dir,
    ):
        assert directory.is_dir()


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    paths = ApplicationPaths.default(override_root=tmp_path).ensure().ensure()
    assert paths.data_dir.is_dir()


def test_default_returns_absolute_platform_paths() -> None:
    paths = ApplicationPaths.default()
    assert paths.database_path.is_absolute()
    assert paths.backup_root.is_absolute()
    assert paths.exports_dir.is_absolute()


def test_with_backup_root_only_replaces_the_backup_root(tmp_path: Path) -> None:
    """两步构造的替换语义: 只换备份根, 其余派生路径一律不动(P2b 范围红线)."""
    base = ApplicationPaths.default(override_root=tmp_path).ensure()
    outside = tmp_path / "elsewhere" / "backups"
    paths = base.with_backup_root(outside).ensure()

    assert paths.backup_root == outside.resolve()
    assert paths.data_dir == base.data_dir
    assert paths.database_path == base.database_path
    assert paths.config_path == base.config_path
    assert paths.exports_dir == base.exports_dir
    assert paths.log_dir == base.log_dir
    assert paths.cache_dir == base.cache_dir
    # frozen: 原实例不受影响, 新备份根也被 ensure 建了出来。
    assert base.backup_root == tmp_path / "data" / "backups"
    assert outside.resolve().is_dir()


def test_effective_paths_applies_the_configured_override(tmp_path: Path) -> None:
    """配置里的 storage.backup_root 生效; 没写时原样返回基础集合."""
    base = ApplicationPaths.default(override_root=tmp_path).ensure()
    assert effective_paths(base, AppConfig()) is base

    override = tmp_path / "moved-backups"
    config = AppConfig.model_validate({"storage": {"backup_root": str(override)}})
    paths = effective_paths(base, config)
    assert paths.backup_root == override.resolve()
    assert paths.backup_root.is_dir()
