"""CLI 入口单元测试: 版本、init 与 doctor 行为."""

from __future__ import annotations

import io
from contextlib import redirect_stdout
from pathlib import Path

from archive_management import app as app_module
from archive_management.infrastructure.database import Database


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
    assert database.schema_version() == 1
    assert (tmp_path / "config" / "config.json").is_file()
    assert (tmp_path / "data" / "backups").is_dir()


def test_init_is_idempotent(tmp_path: Path) -> None:
    first, _ = _run_with_output(["init", "--root", str(tmp_path)])
    second, _ = _run_with_output(["init", "--root", str(tmp_path)])
    assert first == 0
    assert second == 0
    database = Database(tmp_path / "data" / "archive-management.db")
    assert database.schema_version() == 1


def test_doctor_reports_database(tmp_path: Path) -> None:
    _run_with_output(["init", "--root", str(tmp_path)])
    code, output = _run_with_output(["doctor", "--root", str(tmp_path)])
    assert code == 0
    assert "数据库" in output
    assert "schema 1/1" in output


def test_default_command_runs_init(tmp_path: Path) -> None:
    code, _ = _run_with_output(["--root", str(tmp_path)])
    assert code == 0
    assert (tmp_path / "data" / "archive-management.db").is_file()
