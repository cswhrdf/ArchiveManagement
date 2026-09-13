"""CLI 入口单元测试: 版本、init 与 doctor 行为."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

import pytest

import archive_management.app as app_module
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
