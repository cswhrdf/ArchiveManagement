"""配置模块单元测试: 默认值、往返序列化与非法输入拒绝."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import archive_management.config as config_module
from archive_management.config import (
    AppConfig,
    HotkeySettings,
    load_config,
    load_or_reset_config,
    parse_config,
    save_config,
)
from archive_management.exceptions import ConfigurationError
from archive_management.services.hotkeys import (
    DEFAULT_BRANCH_ACCELERATOR,
    DEFAULT_SAVE_ACCELERATOR,
)

pytestmark = [
    pytest.mark.config,
    pytest.mark.normal,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("应用配置"),
    pytest.mark.story("配置读写与校验"),
    pytest.mark.layer("unit"),
]


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


# ---------------------------------------------------------------- 快捷键


def test_default_hotkeys_are_shipped_defaults() -> None:
    config = AppConfig()
    assert config.hotkeys == HotkeySettings()
    assert config.hotkeys.save == DEFAULT_SAVE_ACCELERATOR
    assert config.hotkeys.branch == DEFAULT_BRANCH_ACCELERATOR


def test_config_without_hotkeys_keeps_defaults(tmp_path: Path) -> None:
    """旧配置文件没有 hotkeys 字段时回落到默认组合, 不需要迁移."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "theme": "dark"}), encoding="utf-8")

    config = load_config(path)

    assert config.theme == "dark"
    assert config.hotkeys.save == DEFAULT_SAVE_ACCELERATOR


def test_hotkeys_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    original = AppConfig(theme="light")
    original.hotkeys = HotkeySettings(save="<win>+<ctrl>+a", branch="<ctrl>+<shift>+b")

    save_config(original, path)

    assert load_config(path) == original


@pytest.mark.parametrize(
    "value", ["", "s", "<ctrl>", "<win>+<alt>+<f5>", "<win>+<alt>+1", "<win>+1"]
)
def test_parse_rejects_invalid_hotkeys(tmp_path: Path, value: str) -> None:
    """手改配置写入非法组合键时直接报错, 而不是注册失败后才被发现."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 1, "hotkeys": {"save": value}}), encoding="utf-8"
    )
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


# ------------------------------------------------------------ 非法配置自动还原


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        json.dumps({"version": 99}),
        json.dumps({"version": 1, "theme": "neon"}),
        json.dumps({"version": 1, "unexpected": True}),
        json.dumps({"version": 1, "hotkeys": {"save": "1"}}),
        json.dumps([1, 2, 3]),
    ],
)
def test_load_or_reset_restores_defaults_for_invalid_content(
    tmp_path: Path, payload: str
) -> None:
    """内容非法时: 返回默认配置、把文件写回默认值, 并保留原文件备查."""
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")

    loaded = load_or_reset_config(path)

    assert loaded.reset is True
    assert loaded.config == AppConfig()
    assert loaded.backup == tmp_path / "config.json.invalid"
    assert loaded.backup.read_text(encoding="utf-8") == payload
    # 文件本身已经变成合法且等于默认值
    assert load_config(path) == AppConfig()
    assert path.read_text(encoding="utf-8") == (
        AppConfig().model_dump_json(indent=2) + "\n"
    )


def test_load_or_reset_keeps_a_valid_config_untouched(tmp_path: Path) -> None:
    path = tmp_path / "config.json"
    save_config(AppConfig(theme="dark"), path)

    loaded = load_or_reset_config(path)

    assert loaded.reset is False
    assert loaded.backup is None
    assert loaded.config.theme == "dark"
    assert not (tmp_path / "config.json.invalid").exists()


def test_load_or_reset_does_not_create_a_missing_file(tmp_path: Path) -> None:
    """首次运行(没有配置文件)只返回默认值, 不凭空建文件."""
    path = tmp_path / "config.json"

    loaded = load_or_reset_config(path)

    assert loaded.reset is False
    assert loaded.config == AppConfig()
    assert not path.exists()


def test_load_or_reset_survives_a_write_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """磁盘不可写时仍要能交回一份可用配置, 不能把异常抛给启动流程."""

    def _boom(_config: AppConfig, _path: Path) -> None:
        raise OSError("只读文件系统")

    monkeypatch.setattr(config_module, "save_config", _boom)
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "theme": "neon"}), encoding="utf-8")

    loaded = load_or_reset_config(path)

    assert loaded.reset is True
    assert loaded.config == AppConfig()
