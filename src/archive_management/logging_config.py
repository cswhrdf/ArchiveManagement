"""日志配置.

日志写入应用日志目录下的滚动文件, 可选择镜像到控制台.日志中绝不记录
API Key、完整敏感路径或文件内容(PLAN 第 7 节).
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
    console: bool = True,
    max_bytes: int = 1_048_576,
    backup_count: int = 5,
) -> None:
    """配置 ``archive_management`` 命名空间日志器.

    重复调用是幂等的: 先移除并关闭已挂载的处理器再重建, 保证测试与应用
    重启不会堆积重复日志.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_ROOT_LOGGER)
    logger.setLevel(level)
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
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

    logger.propagate = False
