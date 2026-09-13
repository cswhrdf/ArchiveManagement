"""应用目录单元测试: 覆盖根布局与 ensure 行为."""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.infrastructure.paths import ApplicationPaths

pytestmark = [
    pytest.mark.paths,
    pytest.mark.critical,
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
    assert paths.config_path == tmp_path / "config" / "config.json"


def test_ensure_creates_directories(tmp_path: Path) -> None:
    paths = ApplicationPaths.default(override_root=tmp_path / "root").ensure()
    for directory in (
        paths.config_dir,
        paths.data_dir,
        paths.log_dir,
        paths.cache_dir,
        paths.backup_root,
    ):
        assert directory.is_dir()


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    paths = ApplicationPaths.default(override_root=tmp_path).ensure().ensure()
    assert paths.data_dir.is_dir()


def test_default_returns_absolute_platform_paths() -> None:
    paths = ApplicationPaths.default()
    assert paths.database_path.is_absolute()
    assert paths.backup_root.is_absolute()
