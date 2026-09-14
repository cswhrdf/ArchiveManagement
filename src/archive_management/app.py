"""应用启动与依赖组装.

提供可运行的命令行入口: 初始化应用目录、配置、日志与数据库, 输出健康状态检查,
并启动图形界面(:mod:`archive_management.ui`); ``run()`` 是统一的命令分派入口.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence
from pathlib import Path

from archive_management.config import (
    ConfigLoad,
    load_or_reset_config,
    save_config,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.logging_config import configure_from_settings
from archive_management.packaging import APP_DISPLAY_NAME, package_version
from archive_management.services.platforms import (
    current_platform,
    has_windows_registry,
    platform_label,
)

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器."""
    common = argparse.ArgumentParser(add_help=False)
    # 子命令与顶层共享 --root/--verbose: 用 SUPPRESS 作为默认值, 否则子解析器的
    # 默认值会盖掉写在子命令前面的取值("init --root X" 与 "--root X init" 都可用).
    common.add_argument(
        "--root",
        type=Path,
        default=argparse.SUPPRESS,
        help="将所有应用数据收敛到该根目录(便携/开发/测试模式)",
    )
    common.add_argument(
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="控制台也输出基础操作日志(默认仅写入日志文件)",
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
        help="(已默认生效) 配置文件内容非法时还原为默认值; 保留该参数仅为兼容旧用法",
    )
    subparsers.add_parser(
        "doctor",
        help="打印路径、配置与数据库健康状态",
        parents=[common],
    )
    gui_parser = subparsers.add_parser(
        "gui",
        help="启动图形界面(默认深色主题,可切换浅色)",
        parents=[common],
    )
    gui_parser.add_argument(
        "--smoke",
        type=float,
        default=None,
        help="启动后自动关闭的延时秒数,用于界面冒烟自检",
    )
    return parser


def run(argv: Sequence[str] | None = None) -> int:
    """解析命令行并分派, 返回进程退出码."""
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.version:
        print(package_version())
        return 0

    paths = ApplicationPaths.default(override_root=getattr(args, "root", None)).ensure()
    verbose = bool(getattr(args, "verbose", False))
    command = args.command
    if command is None:
        # 无参数启动时默认执行初始化: 完成应用目录、配置、日志与数据库引导.
        command = "init"
    if command == "init":
        # --force 不再需要: 配置内容非法时一律自动还原为默认值。
        return _run_init(paths, verbose=verbose)
    if command == "doctor":
        return _run_doctor(paths, verbose=verbose)
    if command == "gui":
        return _run_gui(paths, smoke=args.smoke, verbose=verbose)
    parser.error(f"未知命令: {command}")
    return 1


def _start_logging(paths: ApplicationPaths, *, verbose: bool) -> ConfigLoad:
    """读取配置并按其中的 ``logging`` 段启用日志, 返回这次读取的结果.

    顺序很关键: 日志文件上限、保留份数与是否打印到控制台都来自用户配置, 因此
    先读配置再装配日志。配置内容非法时会先还原为默认值, 还原记录就能落进刚刚
    启用的日志里, 而不是在日志装配前丢失。
    """
    loaded = load_or_reset_config(paths.config_path)
    configure_from_settings(paths.log_dir, loaded.config.logging, verbose=verbose)
    if loaded.reset:
        logger.warning("配置文件内容非法, 已还原为默认值: %s", paths.config_path)
    return loaded


def _run_init(paths: ApplicationPaths, *, verbose: bool) -> int:
    loaded = _start_logging(paths, verbose=verbose)
    save_config(loaded.config, paths.config_path)

    database = Database(paths.database_path)
    version = database.migrate()

    lines = [f"{APP_DISPLAY_NAME} {package_version()} 初始化完成"]
    if loaded.reset:
        # 不静默吞掉: 明确告诉用户配置被还原过, 以及原文件留在哪里。
        lines.append(f"配置还原 : 内容非法, 已还原为默认值(原文件: {loaded.backup})")
    lines.extend(
        [
            f"配置目录 : {paths.config_dir}",
            f"数据目录 : {paths.data_dir}",
            f"日志目录 : {paths.log_dir}",
            f"数据库   : {paths.database_path} (schema v{version})",
            f"备份根   : {paths.backup_root}",
        ]
    )
    print("\n".join(lines))
    logger.info("初始化完成, schema 版本 %d", version)
    return 0


def _run_doctor(paths: ApplicationPaths, *, verbose: bool) -> int:
    loaded = _start_logging(paths, verbose=verbose)
    family = current_platform()
    registry_note = "可用" if has_windows_registry(family) else "不适用"
    print(f"应用   : {APP_DISPLAY_NAME} {package_version()}")
    print(f"平台   : {platform_label(family)} (注册表探测: {registry_note})")
    print(f"配置目录: {paths.config_dir}")
    print(f"数据目录: {paths.data_dir}")
    print(f"日志目录: {paths.log_dir}")
    print(f"备份根  : {paths.backup_root}")

    if loaded.reset:
        print(f"配置   : 内容非法, 已还原为默认值(原文件: {loaded.backup})")
    else:
        print(f"配置   : 格式 v{loaded.config.version}, 主题 {loaded.config.theme}")

    try:
        database = Database(paths.database_path)
        print(
            f"数据库 : {paths.database_path} "
            f"(schema {database.schema_version()}/{database.latest_schema_version()})"
        )
    except Exception as exc:
        print(f"数据库 : 不可用 -> {exc}")
    return 0


def _run_gui(paths: ApplicationPaths, *, smoke: float | None, verbose: bool) -> int:
    """启动图形界面;无图形环境或缺少 tkinter 时给出清晰错误."""
    try:
        from archive_management.ui.main_window import run_gui

        return run_gui(
            smoke_seconds=smoke,
            display_name=APP_DISPLAY_NAME,
            paths=paths,
            verbose=verbose,
        )
    except Exception as exc:
        logger.error("GUI 启动失败: %s", exc)
        print(f"无法启动图形界面:{exc}\nGUI 需要可用的桌面环境与 tkinter。")
        return 2


def main() -> None:
    """控制台脚本入口."""
    raise SystemExit(run())
