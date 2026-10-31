"""日志配置.

日志写入应用日志目录下的滚动文件, 并可镜像到控制台。**调试开关**(设置窗口里的
“启用调试日志”, 默认关闭)决定两边的最低级别: 关闭时只记 INFO 及以上的高风险
操作, 开启后 DEBUG 级的基础操作也会落盘(排查问题时才需要, 平时没必要把日志
写满)。``--verbose`` 是“本次启动强制开启”的临时手段。日志中绝不记录凭据、
完整敏感路径或文件内容. 单文件上限与保留份数默认取
:data:`archive_management.config.DEFAULT_LOG_MAX_BYTES` 与 5 份。
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from archive_management.config import DEFAULT_LOG_MAX_BYTES, LoggingSettings

_ROOT_LOGGER = "archive_management"
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def apply_debug(enabled: bool) -> None:
    """按调试开关立即调整日志文件与控制台的级别(设置里拨完马上生效).

    不重建处理器(那会重开日志文件): 只改门槛。**日志器自身的级别也要一起改**
    —— 它还兼作“要不要往下传”的开关, 只改处理器的话 INFO 门槛会把 DEBUG 记录
    挡在处理器之前, 开了调试开关也看不到东西。控制台被关掉(``console=False``)
    时只有文件处理器受影响, 不会因此把控制台输出打开。
    """
    level = logging.DEBUG if enabled else logging.INFO
    logger = logging.getLogger(_ROOT_LOGGER)
    logger.setLevel(level)
    for handler in logger.handlers:
        handler.setLevel(level)


def configure_from_settings(
    log_dir: Path, settings: LoggingSettings, *, verbose: bool = False
) -> None:
    """按用户配置里的 ``logging`` 段装配日志.

    ``debug`` 开关决定文件与控制台的最低级别(关闭时 INFO, 开启时 DEBUG);
    ``--verbose`` 是“本次启动强制开启调试日志”, **不会**覆盖 ``console=False``
    这种显式设置; 日志文件的上限与保留份数完全由配置决定(默认单文件 100 MB /
    保留 5 份)。
    """
    level = logging.DEBUG if verbose or settings.debug else logging.INFO
    configure_logging(
        log_dir,
        level=level,
        file_level=level,
        console=settings.console,
        max_bytes=settings.max_bytes,
        backup_count=settings.backup_count,
    )


def configure_logging(
    log_dir: Path,
    *,
    level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    console: bool = True,
    max_bytes: int = DEFAULT_LOG_MAX_BYTES,
    backup_count: int = 5,
) -> None:
    """配置 ``archive_management`` 命名空间日志器.

    ``level`` 是控制台级别(默认 INFO: 基础操作不打印), ``file_level`` 是
    文件级别(默认 DEBUG)。这是底层装配: **用户可见的策略(调试开关)在
    :func:`configure_from_settings` 里**, 它会给两边传同一个级别。重复调用是
    幂等的: 先移除并关闭已挂载的处理器再重建, 保证测试与应用重启不会堆积
    重复日志。
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_ROOT_LOGGER)
    # 日志器自身要放到最宽松的级别, 否则处理器收不到 DEBUG 记录.
    logger.setLevel(min(level, file_level))
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_LOG_DATE_FORMAT)
    file_handler = RotatingFileHandler(
        log_dir / "archive-management.log",
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(level)
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.propagate = False
