"""CLI 入口单元测试: 版本、init 与 doctor 行为."""

from __future__ import annotations

import io
import json
import logging
from contextlib import redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

import archive_management.app as app_module
from archive_management.config import AppConfig, load_config
from archive_management.infrastructure.database import Database

pytestmark = [
    pytest.mark.cli,
    pytest.mark.critical,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("命令行入口"),
    pytest.mark.story("初始化与自检命令"),
    pytest.mark.layer("unit"),
]


def _run_with_output(argv: list[str]) -> tuple[int, str]:
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        code = app_module.run(argv)
    return code, buffer.getvalue()


def test_version_flag(tmp_path: Path) -> None:
    code, output = _run_with_output(["--root", str(tmp_path), "--version"])
    assert code == 0
    assert output.strip()


def test_init_creates_database_and_config(tmp_path: Path) -> None:
    code, output = _run_with_output(["init", "--root", str(tmp_path)])
    assert code == 0
    assert "初始化完成" in output

    database = Database(tmp_path / "data" / "archive-management.db")
    assert database.schema_version() == database.latest_schema_version()
    assert (tmp_path / "config" / "config.json").is_file()
    assert (tmp_path / "data" / "backups").is_dir()


def test_init_is_idempotent(tmp_path: Path) -> None:
    first, _ = _run_with_output(["init", "--root", str(tmp_path)])
    second, _ = _run_with_output(["init", "--root", str(tmp_path)])
    assert first == 0
    assert second == 0
    database = Database(tmp_path / "data" / "archive-management.db")
    assert database.schema_version() == database.latest_schema_version()


def test_doctor_reports_database(tmp_path: Path) -> None:
    _run_with_output(["init", "--root", str(tmp_path)])
    code, output = _run_with_output(["doctor", "--root", str(tmp_path)])
    assert code == 0
    assert "数据库" in output
    latest = Database(
        tmp_path / "data" / "archive-management.db"
    ).latest_schema_version()
    assert f"schema {latest}/{latest}" in output


def test_default_command_runs_init(tmp_path: Path) -> None:
    code, _ = _run_with_output(["--root", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "data" / "archive-management.db").is_file()


def test_root_before_subcommand_is_not_overridden(tmp_path: Path) -> None:
    """`--root X init` 与 `init --root X` 必须等价(子解析器不能盖掉前置取值)."""
    code, output = _run_with_output(["--root", str(tmp_path), "init"])

    assert code == 0
    assert "初始化完成" in output
    assert (tmp_path / "data" / "archive-management.db").is_file()
    assert (tmp_path / "logs" / "archive-management.log").is_file()


def test_init_restores_invalid_config_to_defaults(tmp_path: Path) -> None:
    """配置内容非法时 init 自动还原为默认值, 并把原文件改名保留."""
    config_path = tmp_path / "config" / "config.json"
    config_path.parent.mkdir(parents=True)
    payload = json.dumps({"version": 1, "theme": "neon"})
    config_path.write_text(payload, encoding="utf-8")

    code, output = _run_with_output(["init", "--root", str(tmp_path)])

    assert code == 0
    assert "初始化完成" in output
    assert "配置还原" in output
    backup = tmp_path / "config" / "config.json.invalid"
    assert backup.read_text(encoding="utf-8") == payload
    assert load_config(config_path) == AppConfig()


def test_doctor_reports_restored_config(tmp_path: Path) -> None:
    """doctor 遇到非法配置也会还原, 并在输出里说明."""
    _run_with_output(["init", "--root", str(tmp_path)])
    config_path = tmp_path / "config" / "config.json"
    config_path.write_text("{", encoding="utf-8")

    code, output = _run_with_output(["doctor", "--root", str(tmp_path)])

    assert code == 0
    assert "内容非法" in output
    assert load_config(config_path) == AppConfig()


def _detach_log_handlers() -> None:
    """移除并关闭当前装配的日志处理器, 避免影响其他用例."""
    root = logging.getLogger("archive_management")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def test_init_applies_logging_settings_from_config(tmp_path: Path) -> None:
    """配置里的 logging 段真的生效: 滚动上限与保留份数取自 config.json."""
    _run_with_output(["init", "--root", str(tmp_path)])
    config_path = tmp_path / "config" / "config.json"
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    payload["logging"]["max_bytes"] = 4096
    payload["logging"]["backup_count"] = 2
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    try:
        code, _ = _run_with_output(["init", "--root", str(tmp_path)])
        assert code == 0
        file_handler = logging.getLogger("archive_management").handlers[0]
        assert isinstance(file_handler, RotatingFileHandler)
        assert file_handler.maxBytes == 4096
        assert file_handler.backupCount == 2
    finally:
        _detach_log_handlers()


def test_verbose_flag_keeps_init_working(tmp_path: Path) -> None:
    code, output = _run_with_output(["--verbose", "init", "--root", str(tmp_path)])

    assert code == 0
    assert "初始化完成" in output


def test_gui_command_returns_two_when_gui_unavailable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import archive_management.ui.main_window as main_window

    def _boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("no display")

    monkeypatch.setattr(main_window, "run_gui", _boom)
    code = app_module.run(["gui", "--root", str(tmp_path), "--smoke", "0"])
    assert code == 2


def test_gui_command_returns_zero_when_gui_succeeds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import archive_management.ui.main_window as main_window

    monkeypatch.setattr(main_window, "run_gui", lambda *_a, **_k: 0)
    code = app_module.run(["gui", "--root", str(tmp_path)])
    assert code == 0


def test_main_module_entrypoint_version(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import runpy

    monkeypatch.setattr("sys.argv", ["archive-management", "--version"])
    with pytest.raises(SystemExit) as excinfo:
        runpy.run_module("archive_management.__main__", run_name="__main__")
    capsys.readouterr()
    assert excinfo.value.code == 0
