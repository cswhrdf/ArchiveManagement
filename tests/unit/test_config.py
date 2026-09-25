"""配置模块单元测试: 默认值、往返序列化与非法输入拒绝."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

import archive_management.config as config_module
from archive_management.config import (
    BASE_FONT_CHOICES,
    ActivationSettings,
    AppConfig,
    HotkeySettings,
    LoggingSettings,
    load_config,
    load_or_repair_config,
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


def test_language_defaults_to_chinese_and_round_trips(tmp_path: Path) -> None:
    """语言默认简体中文, 能写进配置再读回来."""
    path = tmp_path / "config.json"
    assert AppConfig().language == "zh-CN"

    save_config(AppConfig(language="en"), path)

    assert load_config(path).language == "en"


def test_parse_rejects_an_unsupported_language(tmp_path: Path) -> None:
    """没有文案资源的语言直接拒绝: 否则界面会整屏显示 i18n 的 key."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"version": 1, "language": "fr"}), encoding="utf-8")

    with pytest.raises(ConfigurationError):
        load_config(path)


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


def test_debug_logging_defaults_to_off() -> None:
    """调试日志默认关闭: 日志里只保留 INFO 及以上的操作."""
    assert LoggingSettings().debug is False
    assert AppConfig().logging.debug is False


def test_auto_activation_defaults_to_off() -> None:
    """按进程自动启停默认关闭: 启用态决定快捷键与定时备份落到哪一款游戏上."""
    assert ActivationSettings().auto is False
    assert AppConfig().activation.auto is False


def test_activation_switch_round_trips(tmp_path: Path) -> None:
    """开关能写回文件并原样读回."""
    path = tmp_path / "config.json"
    config = AppConfig()
    config.activation.auto = True

    save_config(config, path)

    assert load_config(path).activation.auto is True


@pytest.mark.parametrize(
    "payload",
    [{"watch_processes": True}, {"auto_activation": True}, {"auto": True, "x": 1}],
)
def test_parse_rejects_unknown_activation_fields(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    """严格校验同样适用于新加的这一段: 拼错的字段要被拒绝, 而不是静默忽略."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 1, "activation": payload}),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError):
        load_config(path)


@pytest.mark.parametrize(
    "payload",
    [{"level": "DEBUG"}, {"level": "ERROR"}, {"verbose": True}],
)
def test_parse_rejects_unknown_logging_fields(
    tmp_path: Path, payload: dict[str, object]
) -> None:
    """严格校验: 除 debug/max_bytes/backup_count/console 之外的字段一律拒绝.

    升级前用过的 ``level`` 也在拒绝之列 —— 不做兼容读入, 手改配置写错就得被看见。
    """
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"version": 1, "logging": payload}),
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


# ---------------------------------------------------- 非法配置逐字段修复与整份还原


@pytest.mark.parametrize(
    "payload",
    [
        "{",
        json.dumps({"version": 99}),
        json.dumps([1, 2, 3]),
    ],
)
def test_load_or_repair_resets_when_the_whole_file_is_unusable(
    tmp_path: Path, payload: str
) -> None:
    """整份文件都读不出来(JSON 坏了 / 顶层不是对象 / 版本不认识)时: 还原为默认值."""
    path = tmp_path / "config.json"
    path.write_text(payload, encoding="utf-8")

    loaded = load_or_repair_config(path)

    assert loaded.reset is True
    assert loaded.repaired == ()
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

    loaded = load_or_repair_config(path)

    assert loaded.reset is False
    assert loaded.repaired == ()
    assert loaded.backup is None
    assert loaded.config.theme == "dark"
    assert not (tmp_path / "config.json.invalid").exists()


@pytest.mark.parametrize(
    ("payload", "notes"),
    [
        ({"theme": "neon"}, ("theme",)),
        ({"unexpected": True}, ("unexpected",)),
        ({"logging": {"evil": 1}}, ("logging.evil",)),
        ({"logging": {"debug": "maybe"}}, ("logging.debug",)),
        ({"hotkeys": {"save": "1"}}, ("hotkeys.save",)),
        ({"ui": {"base_font_px": 99}}, ("ui.base_font_px",)),
        ({"ui": "big"}, ("ui",)),
    ],
)
def test_load_or_repair_keeps_valid_fields_and_drops_only_bad_ones(
    tmp_path: Path, payload: dict[str, Any], notes: tuple[str, ...]
) -> None:
    """只写坏一项时: 非法项剔除并落回默认值, 其余自定义配置原样保留."""
    path = tmp_path / "config.json"
    raw = {"version": 1, "theme": "dark", "language": "en", **payload}
    path.write_text(json.dumps(raw), encoding="utf-8")

    loaded = load_or_repair_config(path)

    assert loaded.reset is False
    assert loaded.repaired == notes
    # 合法的自定义配置一点没丢(language 一定还在), 非法项落回默认值而不是原样带着走
    assert loaded.config.theme == ("system" if "theme" in payload else "dark")
    assert loaded.config.language == "en"
    assert loaded.config.ui.base_font_px in BASE_FONT_CHOICES
    assert loaded.config.logging.max_bytes == LoggingSettings().max_bytes
    assert loaded.backup == tmp_path / "config.json.invalid"
    # 清理后的内容已写回文件: 文件里不再有不认识的键
    assert "unexpected" not in load_config(path).model_dump()
    assert load_config(path) == loaded.config


def test_load_or_reset_does_not_create_a_missing_file(tmp_path: Path) -> None:
    """首次运行(没有配置文件)只返回默认值, 不凭空建文件."""
    path = tmp_path / "config.json"

    loaded = load_or_repair_config(path)

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
    path.write_text(json.dumps([1, 2, 3]), encoding="utf-8")

    loaded = load_or_repair_config(path)

    assert loaded.reset is True
    assert loaded.config == AppConfig()
