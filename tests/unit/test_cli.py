"""CLI 入口单元测试: 版本、init 与 doctor 行为."""

from __future__ import annotations

import io
import json
import logging
import os
from contextlib import redirect_stdout
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

import pytest

import archive_management.app as app_module
from archive_management.config import AppConfig, load_config
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.paths import ApplicationPaths

pytestmark = [
    pytest.mark.cli,
    pytest.mark.normal,
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


def test_init_repairs_only_the_invalid_config_entries(tmp_path: Path) -> None:
    """配置里只有一项写坏时 init 只剔除它、其余自定义内容保留, 并把原文件改名保留."""
    config_path = tmp_path / "config" / "config.json"
    config_path.parent.mkdir(parents=True)
    payload = json.dumps({"version": 1, "theme": "neon", "language": "en"})
    config_path.write_text(payload, encoding="utf-8")

    code, output = _run_with_output(["init", "--root", str(tmp_path)])

    assert code == 0
    assert "初始化完成" in output
    assert "配置修复" in output
    backup = tmp_path / "config" / "config.json.invalid"
    assert backup.read_text(encoding="utf-8") == payload
    repaired = load_config(config_path)
    # 非法取值回到默认值, 同一次写入里的合法自定义项没有被牵连丢掉
    assert repaired.theme == AppConfig().theme
    assert repaired.language == "en"


def test_doctor_reports_restored_config(tmp_path: Path) -> None:
    """doctor 遇到非法配置也会还原, 并在输出里说明."""
    _run_with_output(["init", "--root", str(tmp_path)])
    config_path = tmp_path / "config" / "config.json"
    config_path.write_text("{", encoding="utf-8")

    code, output = _run_with_output(["doctor", "--root", str(tmp_path)])

    assert code == 0
    assert "内容非法" in output
    assert load_config(config_path) == AppConfig()


def test_doctor_writes_its_report_into_the_log_too(tmp_path: Path) -> None:
    """打包版没有控制台(``console=False``): 报告必须也落进日志.

    否则用户双击/从命令行跑 ``doctor`` 时, 屏幕上与文件里都留不下东西 —— 冒烟在 CI
    runner 上抓到"医生卡住"时也是靠这份日志看它停在哪一行。
    """
    _run_with_output(["init", "--root", str(tmp_path)])
    _run_with_output(["doctor", "--root", str(tmp_path)])

    try:
        log = (tmp_path / "logs" / "archive-management.log").read_text(encoding="utf-8")
    finally:
        _detach_log_handlers()
    hint = "doctor 的报告要同时写日志(打包版没有控制台)"
    assert "配置目录" in log, hint
    assert "数据库 :" in log, hint


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


def test_a_packaged_build_opens_the_window_when_started_without_arguments(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """打包版没写子命令 = 开界面.

    双击的正是这条路: 窗口化 exe 没有控制台, 老默认命令 ``init`` 只会把报告打进虚空,
    用户那里就是"屏幕上什么都没有"(2026-10-11 报的就是这个)。

    **路径要换到临时目录**: 本用例的 argv 里不能写 ``--root``(要的就是"一个参数都没有"那
    条路), 而缺省路径是**真实的用户数据目录** —— 那里只要开着一个实例(用户正在用这个软件
    时就是这样), 单实例闸门就会把界面挡掉, 用例跟着变成"看运气"(2026-10-13 实测撞上过)。
    """
    import archive_management.ui.main_window as main_window

    seen: list[float | None] = []

    def _fake_gui(*_args: object, **kwargs: Any) -> int:
        seen.append(kwargs.get("smoke_seconds"))
        return 0

    monkeypatch.setattr(main_window, "run_gui", _fake_gui)
    monkeypatch.setattr(app_module, "is_packaged", lambda: True)
    # 先把真身留住再换: 换的是**类**上的 classmethod, 直接在里面再调自己会无限递归.
    real_default = ApplicationPaths.default
    monkeypatch.setattr(
        ApplicationPaths,
        "default",
        classmethod(lambda _cls, **_kwargs: real_default(override_root=tmp_path)),
    )
    monkeypatch.setattr("sys.argv", ["ArchiveManagement.exe"])

    assert app_module.run() == 0
    assert seen == [None], "打包版没写子命令时应当走 gui"


def test_only_global_flags_still_open_the_window_in_a_packaged_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """只给全局参数(``--root``)也算"没说子命令": 判据是解析后的 command, 不是 argv 空不空."""
    import archive_management.ui.main_window as main_window

    roots: list[Path] = []

    def _fake_gui(*_args: object, **kwargs: Any) -> int:
        roots.append(kwargs["paths"].config_dir.parent)
        return 0

    monkeypatch.setattr(main_window, "run_gui", _fake_gui)
    monkeypatch.setattr(app_module, "is_packaged", lambda: True)
    monkeypatch.setattr("sys.argv", ["ArchiveManagement.exe", "--root", str(tmp_path)])

    assert app_module.run() == 0
    assert roots == [tmp_path], "--root 应当原样传进界面那一路"


def test_explicit_arguments_still_win_in_a_packaged_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """打包版里显式写子命令时不许被"默认开界面"顶掉(``ArchiveManagement.exe init``)."""
    monkeypatch.setattr(app_module, "is_packaged", lambda: True)
    monkeypatch.setattr(
        "sys.argv", ["ArchiveManagement.exe", "init", "--root", str(tmp_path)]
    )

    assert app_module.run() == 0
    assert "初始化完成" in capsys.readouterr().out


def test_the_source_default_stays_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """源码运行不受影响: 没写子命令仍只做一次初始化."""
    monkeypatch.setattr(app_module, "is_packaged", lambda: False)
    assert app_module.default_command() == "init"


# ---------------------------------------------------------------- 单实例闸门
#
# 打包版同时只允许开一个界面进程(2026-10-12 要求): 两个进程各持同一个数据库、备份根与
# 快捷键注册会互相踩, 用户看到的是"点了没反应"这类没有报错的怪事。闸门只在 ``sys.frozen``
# 下生效, 开发与用例(每片都在自己进程里建真窗口)不受影响。


def _hold_the_instance_lock(tmp_path: Path) -> Any:
    """占着某个 ``--root`` 的实例锁(返回出去的那把锁由调用方释放)."""
    from archive_management.services import single_instance

    paths = ApplicationPaths.default(override_root=tmp_path)
    lock = single_instance.InstanceLock(single_instance.lock_path(paths))
    assert lock.acquire() is True
    return lock


def test_a_second_packaged_instance_asks_the_running_window_to_come_forward(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """第二个实例不开窗也不弹提示, 而是请已经开着的那个把窗口叫到前台.

    双击的意图是"我要用这个软件", 而它已经开着了(用户 2026-10-12 的要求: "重复打开窗口应该直接
    跳转到上次打开的窗口") —— 所以第二次启动写一条"到前台"请求就退出, 退出码 3 与 argparse 的 2
    分开。请求文件与锁同一个数据目录, 所以 `--root` 便携实例各叫各的窗口。
    """
    import archive_management.ui.main_window as main_window
    from archive_management.services import single_instance

    monkeypatch.setattr(
        main_window, "run_gui", lambda *_a, **_k: pytest.fail("第二个实例不该建界面")
    )
    monkeypatch.setattr(app_module, "is_packaged", lambda: True)
    monkeypatch.delenv(single_instance.ALLOW_ENV, raising=False)
    # 没有真窗口去处理请求: 别让"等回执"把这条例用拖满 2 秒(那条路自己有单测)。
    monkeypatch.setattr(single_instance, "HANDLE_WAIT_SECONDS", 0.0)

    running = _hold_the_instance_lock(tmp_path)
    try:
        code = app_module.run(["gui", "--root", str(tmp_path)])
    finally:
        running.release()
        _detach_log_handlers()

    assert code == single_instance.ALREADY_RUNNING_EXIT
    assert code == 3, "退出码要和 argparse 的 2 分开"
    paths = ApplicationPaths.default(override_root=tmp_path)
    token = single_instance.read_focus_request(
        single_instance.focus_request_path(paths)
    )
    assert f"pid={os.getpid()}" in token, "第二个实例必须留下「请到前台」的请求"
    assert "at=" in token
    # 窗口化 exe 没有控制台: 这一行是"第二次双击叫醒了窗口"唯一的证据(含权限与回执的结果)。
    log_text = (tmp_path / "logs" / "archive-management.log").read_text(
        encoding="utf-8"
    )
    assert "已请它把窗口叫到前台" in log_text
    assert "handled=False" in log_text, "没有活在跑时等不到回执, 日志要写明"


def test_the_first_packaged_instance_holds_the_lock_only_while_the_window_lives(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """窗口活着时锁必须一直在手; 窗口关掉后必须还回来(否则用户下次双击会被自己挡住)."""
    import archive_management.ui.main_window as main_window
    from archive_management.services import single_instance

    paths = ApplicationPaths.default(override_root=tmp_path)
    lock_file = single_instance.lock_path(paths)
    observed: list[bool] = []

    def _fake_gui(*_args: object, **_kwargs: Any) -> int:
        observed.append(single_instance.InstanceLock(lock_file).acquire())
        return 0

    monkeypatch.setattr(main_window, "run_gui", _fake_gui)
    monkeypatch.setattr(app_module, "is_packaged", lambda: True)
    monkeypatch.delenv(single_instance.ALLOW_ENV, raising=False)

    assert app_module.run(["gui", "--root", str(tmp_path)]) == 0

    assert observed == [False], "界面期间第二个实例必须拿不到锁"
    available = single_instance.InstanceLock(lock_file)
    assert available.acquire() is True, "窗口关掉后锁要还回来"
    available.release()


def test_source_runs_may_open_several_windows_at_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """源码运行(开发/用例)不受闸门约束: 分片跑用例时每片都要能建真窗口."""
    import archive_management.ui.main_window as main_window

    opened: list[str] = []

    def _fake_gui(*_args: object, **_kwargs: Any) -> int:
        opened.append("gui")
        return 0

    monkeypatch.setattr(main_window, "run_gui", _fake_gui)
    monkeypatch.setattr(app_module, "is_packaged", lambda: False)

    running = _hold_the_instance_lock(tmp_path)
    try:
        assert app_module.run(["gui", "--root", str(tmp_path)]) == 0
    finally:
        running.release()

    assert opened == ["gui"], "源码运行即使锁被占着也要照常开界面"


def test_the_escape_hatch_lets_a_second_packaged_instance_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """诊断用逃生门: 显式设了环境变量就照开(打包版同时开两个做对比时用)."""
    import archive_management.ui.main_window as main_window
    from archive_management.services import single_instance

    opened: list[str] = []

    def _fake_gui(*_args: object, **_kwargs: Any) -> int:
        opened.append("gui")
        return 0

    monkeypatch.setattr(main_window, "run_gui", _fake_gui)
    monkeypatch.setattr(app_module, "is_packaged", lambda: True)
    monkeypatch.setenv(single_instance.ALLOW_ENV, "1")

    running = _hold_the_instance_lock(tmp_path)
    try:
        assert app_module.run(["gui", "--root", str(tmp_path)]) == 0
    finally:
        running.release()

    assert opened == ["gui"], "逃生门打开时不该拦"
