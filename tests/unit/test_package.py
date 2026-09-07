"""包结构冒烟测试: 所有子包均可导入且版本号可解析."""

from __future__ import annotations

import importlib
from importlib.metadata import PackageNotFoundError

import pytest

_SUBPACKAGES = (
    "archive_management.application",
    "archive_management.domain",
    "archive_management.infrastructure",
    "archive_management.services",
    "archive_management.ui",
)


def test_all_subpackages_importable() -> None:
    for name in _SUBPACKAGES:
        assert importlib.import_module(name) is not None


def test_package_version_available() -> None:
    from archive_management import __version__

    assert isinstance(__version__, str)
    assert len(__version__) > 0


def test_display_name_available() -> None:
    from archive_management import APP_DISPLAY_NAME

    assert APP_DISPLAY_NAME


def test_package_version_falls_back_when_uninstalled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import archive_management.packaging as pkg

    def _raise(_name: str) -> str:
        raise PackageNotFoundError(_name)

    monkeypatch.setattr(pkg, "_distribution_version", _raise)
    pkg.package_version.cache_clear()
    try:
        assert pkg.package_version() == "0.0.0.dev0"
    finally:
        pkg.package_version.cache_clear()
