"""应用启动与依赖组装.

阶段 A 提供可运行的命令行入口: 初始化应用目录、配置、日志与数据库,
并提供健康状态检查.图形界面(:mod:`archive_management.ui`)在阶段 F
接入;届时 ``run()`` 继续作为统一的命令分派入口.

验收标准(PLAN 6 阶段 A): 空应用可以启动并创建数据库.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from archive_management.config import AppConfig, load_config, save_config
from archive_management.exceptions import ConfigurationError
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.logging_config import configure_logging
from archive_management.packaging import APP_DISPLAY_NAME, package_version

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--root",
        type=Path,
        default=None,
        help="将所有应用数据收敛到该根目录(便携/开发/测试模式)",
    )
    parser = argparse.ArgumentParser(
        prog="archive-management",
        description=APP_DISPLAY_NAME,
        parents=[common],
    )
    parser.add_argument(
        "--version",
        action="store_true",
        help="显示版本号后退出",
    )
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser(
        "init",
        help="初始化目录、配置、日志与数据库",
        parents=[common],
    )
    init_parser.add_argument(
        "--force",
        action="store_true",
        help="配置文件存在且非法时用默认配置覆盖",
    )
    subparsers.add_parser(
        "doctor",
        help="打印路径、配置与数据库健康状态",
        parents=[common],
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """解析命令行并分派, 返回进程退出码."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(package_version())
        return 0

    paths = ApplicationPaths.default(override_root=args.root).ensure()
    command = args.command
    if command is None:
        # 阶段 A 默认执行初始化: 无参数启动即完成空应用引导.
        command = "init"
    if command == "init":
        return _run_init(paths, force=bool(getattr(args, "force", False)))
    if command == "doctor":
        return _run_doctor(paths)
    parser.error(f"未知命令: {command}")
    return 1


def _load_or_create_config(paths: ApplicationPaths, *, force: bool) -> AppConfig:
    """读取配置;不存在时生成默认配置, 非法时按 ``force`` 决定是否覆盖."""
    config_path = paths.config_path
    try:
        config = load_config(config_path)
    except ConfigurationError:
        if not force:
            raise
        logger.warning("配置文件非法, 使用 --force 以默认配置覆盖: %s", config_path)
        config = AppConfig()
    return config


def _run_init(paths: ApplicationPaths, *, force: bool) -> int:
    configure_logging(paths.log_dir)
    config = _load_or_create_config(paths, force=force)
    save_config(config, paths.config_path)

    database = Database(paths.database_path)
    version = database.migrate()

    summary = (
        f"{APP_DISPLAY_NAME} {package_version()} 初始化完成\n"
        f"配置目录 : {paths.config_dir}\n"
        f"数据目录 : {paths.data_dir}\n"
        f"日志目录 : {paths.log_dir}\n"
        f"数据库   : {paths.database_path} (schema v{version})\n"
        f"备份根   : {paths.backup_root}"
    )
    print(summary)
    logger.info("初始化完成, schema 版本 %d", version)
    return 0


def _run_doctor(paths: ApplicationPaths) -> int:
    configure_logging(paths.log_dir)
    print(f"应用   : {APP_DISPLAY_NAME} {package_version()}")
    print(f"配置目录: {paths.config_dir}")
    print(f"数据目录: {paths.data_dir}")
    print(f"日志目录: {paths.log_dir}")
    print(f"备份根  : {paths.backup_root}")

    try:
        config = load_config(paths.config_path)
        print(f"配置   : 格式 v{config.version}, 主题 {config.theme}")
    except ConfigurationError as exc:
        print(f"配置   : 无法读取 -> {exc}")

    try:
        database = Database(paths.database_path)
        print(
            f"数据库 : {paths.database_path} "
            f"(schema {database.schema_version()}/{database.latest_schema_version()})"
        )
    except Exception as exc:
        print(f"数据库 : 不可用 -> {exc}")
    return 0


def main() -> None:
    """控制台脚本入口."""
    raise SystemExit(run())
