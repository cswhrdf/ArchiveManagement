"""日志配置.

日志写入应用日志目录下的滚动文件, 并可镜像到控制台。文件始终记录全部
级别的操作(含基础操作的 DEBUG), 控制台默认只显示 INFO 及以上的高风险
操作, 需要时放宽 ``level`` 即可看到基础操作。日志中绝不记录 API Key、
完整敏感路径或文件内容(PLAN 第 7 节).
"""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

_ROOT_LOGGER = "archive_management"
_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_LOG_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_logging(
    log_dir: Path,
    *,
    level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    console: bool = True,
    max_bytes: int = 1_048_576,
    backup_count: int = 5,
) -> None:
    """配置 ``archive_management`` 命名空间日志器.

    ``level`` 是控制台级别(默认 INFO: 基础操作不打印), ``file_level`` 是
    文件级别(默认 DEBUG: 所有用户操作都落盘, 便于事后追溯)。重复调用是
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
