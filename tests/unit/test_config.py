"""配置模块单元测试: 默认值、往返序列化与非法输入拒绝."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archive_management.config import AppConfig, load_config, parse_config, save_config
from archive_management.exceptions import ConfigurationError

pytestmark = [pytest.mark.config, pytest.mark.critical]


def test_missing_file_yields_default(tmp_path: Path) -> None:
    config = load_config(tmp_path / "not-there.json")
    assert config == AppConfig()
    assert config.version == 1
    assert config.theme == "system"


def test_save_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = AppConfig(theme="dark")
    save_config(original, path)
    assert path.exists()
    assert load_config(path) == original


def test_parse_rejects_unknown_field(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 1, "theme": "system", "unexpected": True}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_parse_rejects_unsupported_version(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 99, "theme": "system"}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_parse_rejects_invalid_theme(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "theme": "neon"}), encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_parse_rejects_invalid_log_level(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 1, "logging": {"level": "TRACE"}}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_load_rejects_malformed_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("{ not valid json", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_load_rejects_non_object_json(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ConfigurationError):
        load_config(path)


def test_parse_config_returns_valid_config() -> None:
    config = parse_config({"version": 1, "theme": "light"})
    assert config.theme == "light"
