"""审计日志与日志装配的单元测试.

用户操作不落数据库, 只写日志文件: 高风险操作用 INFO(默认打印 + 落盘),
基础操作用 DEBUG(默认不记录, 打开设置里的调试开关或 ``--verbose`` 时才写入)。这里同时验证字段渲染、
路径脱敏、敏感字段隐藏与截断规则。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from logging.handlers import RotatingFileHandler
from pathlib import Path

import pytest

from archive_management.config import DEFAULT_LOG_MAX_BYTES, LoggingSettings
from archive_management.logging_config import (
    apply_debug,
    configure_from_settings,
    configure_logging,
)
from archive_management.services.audit import (
    _MAX_FIELD_LENGTH,
    AUDIT_LOGGER_NAME,
    LEVEL_BASIC,
    LEVEL_OPERATION,
    log_action,
    log_failure,
    redacted_path,
)

pytestmark = [
    pytest.mark.backend,
    pytest.mark.normal,
    pytest.mark.epic("基础工程"),
    pytest.mark.feature("审计日志"),
    pytest.mark.story("记录用户操作"),
    pytest.mark.layer("unit"),
]

_LOGGER = logging.getLogger(AUDIT_LOGGER_NAME)


class _Capture(logging.Handler):
    """收集日志记录(级别与文本)."""

    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        """返回渲染后的日志文本."""
        return [record.getMessage() for record in self.records]


@pytest.fixture
def captured() -> Iterator[_Capture]:
    """挂上捕获处理器并把审计日志器降到 DEBUG."""
    handler = _Capture()
    previous_level = _LOGGER.level
    _LOGGER.addHandler(handler)
    _LOGGER.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        _LOGGER.removeHandler(handler)
        handler.close()
        _LOGGER.setLevel(previous_level)


def test_high_risk_operation_logs_at_info(captured: _Capture) -> None:
    log_action("backup.create", game_id=1, kind="manual")

    assert [record.levelno for record in captured.records] == [LEVEL_OPERATION]
    assert captured.messages() == ["backup.create game_id=1 kind=manual"]


def test_basic_operation_logs_at_debug(captured: _Capture) -> None:
    log_action("ui.switch_view", basic=True, view="timeline")

    assert [record.levelno for record in captured.records] == [LEVEL_BASIC]
    assert captured.messages() == ["ui.switch_view view=timeline"]


def test_failure_marks_action_suffix_and_level(captured: _Capture) -> None:
    log_failure("restore", game_id=2, error="磁盘已满")

    record = captured.records[0]
    assert record.levelno == logging.ERROR
    assert "restore.failed" in record.getMessage()


def test_fields_are_sorted_and_empty_values_skipped(captured: _Capture) -> None:
    log_action("backup.create", zeta=1, alpha=2, note=None, title="")

    assert captured.messages() == ["backup.create alpha=2 zeta=1"]


def test_sensitive_fields_are_hidden(captured: _Capture) -> None:
    # 用变量传入, 避免把"看起来像密码"的字面量写进调用(S106).
    value = "placeholder-value"
    log_action("config.update", api_key=value, theme="dark")
    log_action("config.update", token=value)

    assert captured.messages() == [
        "config.update api_key=<hidden> theme=dark",
        "config.update token=<hidden>",
    ]


def test_booleans_and_multiline_text_are_rendered_inline(captured: _Capture) -> None:
    log_action("backup.create", safety=True, note="第一行\n第二行")

    assert captured.messages() == ['backup.create note="第一行 第二行" safety=true']


def test_long_values_are_truncated(captured: _Capture) -> None:
    log_action("note.update", note="x" * (_MAX_FIELD_LENGTH + 50))

    message = captured.messages()[0]
    assert message.endswith("...")
    assert len(message) <= len("note.update note=") + _MAX_FIELD_LENGTH + 2


def test_actions_are_skipped_when_logger_disabled() -> None:
    """控制台不高频写日志时也不该产生开销: 级别不足直接返回."""
    previous_level = _LOGGER.level
    _LOGGER.setLevel(logging.CRITICAL + 1)
    try:
        # 不抛异常即视为通过(DEBUG/INFO 记录被短路, 没有记录产生).
        log_action("ui.select", basic=True, backup_id=1)
        log_action("backup.create", game_id=1)
    finally:
        _LOGGER.setLevel(previous_level)


def test_redacted_path_keeps_last_two_segments() -> None:
    assert redacted_path(r"D:\Games\OuterWilds\save") == "OuterWilds/save"
    assert redacted_path("/home/user/save/slot1.dat") == "save/slot1.dat"
    assert redacted_path("save") == "save"
    assert redacted_path("") == ""


def test_logging_writes_debug_to_file_but_info_to_console(tmp_path: Path) -> None:
    """底层装配: 不显式传级别时文件记全部级别、控制台只看高风险操作.

    用户可见的策略(调试开关)在 :func:`configure_from_settings` 里, 它会给两边
    传同一个级别; 这里验证的是更下面那层的默认值。
    """
    configure_logging(tmp_path / "logs")
    try:
        root = logging.getLogger("archive_management")
        file_handler, console_handler = root.handlers
        assert file_handler.level == logging.DEBUG
        assert console_handler.level == logging.INFO
        assert root.level == logging.DEBUG

        log_action("ui.select", basic=True, backup_id=7)
        log_action("backup.create", game_id=1)
        logging.getLogger("archive_management").info("普通日志")
        for handler in root.handlers:
            handler.flush()
        text = (tmp_path / "logs" / "archive-management.log").read_text(
            encoding="utf-8"
        )
        assert "ui.select backup_id=7" in text
        assert "backup.create game_id=1" in text
    finally:
        # 清掉本次装配的处理器, 避免影响其他用例.
        configure_logging(tmp_path / "logs-again", console=False)


def test_verbose_logging_lowers_console_level_to_debug(tmp_path: Path) -> None:
    configure_logging(tmp_path / "logs", level=logging.DEBUG)
    try:
        root = logging.getLogger("archive_management")
        assert [handler.level for handler in root.handlers] == [
            logging.DEBUG,
            logging.DEBUG,
        ]
    finally:
        configure_logging(tmp_path / "logs-again", console=False)


def _detach_handlers() -> None:
    """移除并关闭当前装配的日志处理器, 避免用例之间互相影响."""
    root = logging.getLogger("archive_management")
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()


def test_default_log_file_limit_is_100_mib(tmp_path: Path) -> None:
    """默认单文件上限 100 MiB: 上限太小会让刚发生的问题很快被轮转掉."""
    settings = LoggingSettings()
    assert settings.max_bytes == 100 * 1024 * 1024
    assert settings.max_bytes == DEFAULT_LOG_MAX_BYTES
    assert settings.backup_count == 5


def test_configure_from_settings_honours_user_limits(tmp_path: Path) -> None:
    """配置里的 logging 段决定滚动上限、保留份数、控制台开关与调试开关."""
    settings = LoggingSettings(
        debug=True, max_bytes=8 * 1024 * 1024, backup_count=2, console=True
    )
    configure_from_settings(tmp_path / "logs", settings)
    try:
        root = logging.getLogger("archive_management")
        file_handler, console_handler = root.handlers
        assert isinstance(file_handler, RotatingFileHandler)
        assert file_handler.maxBytes == 8 * 1024 * 1024
        assert file_handler.backupCount == 2
        assert file_handler.level == logging.DEBUG
        assert console_handler.level == logging.DEBUG
    finally:
        _detach_handlers()


def test_verbose_forces_debug_for_this_run(tmp_path: Path) -> None:
    """--verbose 是“本次启动强制开启调试日志”: 文件与控制台都放宽到 DEBUG."""
    configure_from_settings(
        tmp_path / "logs", LoggingSettings(debug=False), verbose=True
    )
    try:
        root = logging.getLogger("archive_management")
        assert [handler.level for handler in root.handlers] == [
            logging.DEBUG,
            logging.DEBUG,
        ]
    finally:
        _detach_handlers()

    # console=False 是显式设置: 即使加 --verbose 也不装配控制台处理器。
    configure_from_settings(
        tmp_path / "logs", LoggingSettings(console=False), verbose=True
    )
    try:
        root = logging.getLogger("archive_management")
        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], RotatingFileHandler)
    finally:
        _detach_handlers()


def test_debug_switch_controls_what_reaches_the_file(tmp_path: Path) -> None:
    """关闭调试开关时日志里就没有 DEBUG 级记录, 开启后才写进去."""
    logs = tmp_path / "logs"
    configure_from_settings(logs, LoggingSettings(debug=False))
    try:
        root = logging.getLogger("archive_management")
        assert [handler.level for handler in root.handlers] == [
            logging.INFO,
            logging.INFO,
        ]

        log_action("ui.select", basic=True, backup_id=7)
        log_action("backup.create", game_id=1)
        for handler in root.handlers:
            handler.flush()
        text = (logs / "archive-management.log").read_text(encoding="utf-8")
        assert "ui.select backup_id=7" not in text
        assert "backup.create game_id=1" in text
    finally:
        _detach_handlers()

    # 同一个目录上重新装配并打开开关: 这一轮 DEBUG 那条必须落盘.
    configure_from_settings(logs, LoggingSettings(debug=True))
    try:
        root = logging.getLogger("archive_management")
        assert [handler.level for handler in root.handlers] == [
            logging.DEBUG,
            logging.DEBUG,
        ]

        log_action("ui.select", basic=True, backup_id=8)
        for handler in root.handlers:
            handler.flush()
        text = (logs / "archive-management.log").read_text(encoding="utf-8")
        assert "ui.select backup_id=8" in text
    finally:
        _detach_handlers()


def test_apply_debug_changes_the_levels_without_rebuilding_handlers(
    tmp_path: Path,
) -> None:
    """设置里拨开关立即生效: 只改门槛, 不重建处理器(不重开日志文件)."""
    configure_from_settings(tmp_path / "logs", LoggingSettings(debug=False))
    try:
        root = logging.getLogger("archive_management")
        handlers = list(root.handlers)
        assert [handler.level for handler in handlers] == [logging.INFO] * 2

        apply_debug(True)

        assert list(root.handlers) == handlers
        assert [handler.level for handler in handlers] == [logging.DEBUG] * 2
        assert root.level == logging.DEBUG

        apply_debug(False)

        assert list(root.handlers) == handlers
        assert [handler.level for handler in handlers] == [logging.INFO] * 2
        assert root.level == logging.INFO
    finally:
        _detach_handlers()


def test_apply_debug_works_without_a_console(tmp_path: Path) -> None:
    """console=False 是显式选择: 拨开关只影响文件处理器, 不会把控制台输出打开."""
    configure_from_settings(tmp_path / "logs", LoggingSettings(console=False))
    try:
        root = logging.getLogger("archive_management")

        apply_debug(True)

        assert len(root.handlers) == 1
        assert isinstance(root.handlers[0], RotatingFileHandler)
        assert root.handlers[0].level == logging.DEBUG
    finally:
        _detach_handlers()
