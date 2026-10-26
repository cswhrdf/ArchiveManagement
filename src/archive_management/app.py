"""应用启动与依赖组装.

提供可运行的命令行入口: 初始化应用目录、配置、日志与数据库, 输出健康状态检查,
并启动图形界面(:mod:`archive_management.ui`); ``run()`` 是统一的命令分派入口.
"""

from __future__ import annotations

import argparse
import contextlib
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from archive_management.config import (
    ConfigLoad,
    load_or_repair_config,
    save_config,
)
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.logging_config import configure_from_settings
from archive_management.packaging import APP_DISPLAY_NAME, package_version
from archive_management.services import single_instance
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


def is_packaged() -> bool:
    """这次运行是不是打包产物(PyInstaller 会把 ``sys.frozen`` 置真)."""
    return bool(getattr(sys, "frozen", False))


def default_command() -> str:
    """没有写明子命令时跑什么.

    打包版是 ``console=False``(没有控制台), 而 CLI 的老默认是 ``init`` —— 它把报告打进
    一个不存在的控制台然后退出, 用户双击之后"屏幕上什么都没有", 看起来就是打不开
    (2026-10-11 报的就是这个)。所以**打包版的"没说子命令" = 开界面**; 命令行用户显式写
    ``init``/``doctor`` 时一切照旧, 源码运行(``python -m archive_management``)也照旧。

    判据取"解析后子命令为空", 不是"argv 为空": 只给全局参数(如 ``--root``)时也要开界面。
    """
    return "gui" if is_packaged() else "init"


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
        command = default_command()
    if command == "init":
        # --force 不再需要: 配置内容非法时一律自动还原为默认值。
        return _run_init(paths, verbose=verbose)
    if command == "doctor":
        return _run_doctor(paths, verbose=verbose)
    if command == "gui":
        # ``smoke`` 只有写明 ``gui`` 子命令时才有(默认命令补出来的那次没有) → getattr.
        return _run_gui(paths, smoke=getattr(args, "smoke", None), verbose=verbose)
    parser.error(f"未知命令: {command}")  # pragma: no cover - 子命令已校验
    return 1  # pragma: no cover - 上面不返回


def _report(line: str) -> None:
    """报告一行: 同时写日志与控制台.

    打包版是 ``console=False``(没有控制台), 用户从快捷方式或命令行跑 ``doctor``/``init``
    时 ``print`` 的内容哪儿也不去 —— 日志是这种环境下唯一留得下的现场。所以两处都写,
    而且是**一行一发**: 真卡在中间时, 日志末尾就能看出卡在哪一行(冒烟也靠这个抓现场)。
    """
    logger.info("%s", line)
    print(line, flush=True)


def _start_logging(paths: ApplicationPaths, *, verbose: bool) -> ConfigLoad:
    """读取配置并按其中的 ``logging`` 段启用日志, 返回这次读取的结果.

    顺序很关键: 日志文件上限、保留份数与是否打印到控制台都来自用户配置, 因此
    先读配置再装配日志。配置内容有问题时会先剔除非法部分(整份读不出来才全体
    还原), 处理记录就能落进刚刚启用的日志里, 而不是在日志装配前丢失。
    """
    loaded = load_or_repair_config(paths.config_path)
    configure_from_settings(paths.log_dir, loaded.config.logging, verbose=verbose)
    if loaded.reset:
        logger.warning("配置文件内容非法, 已还原为默认值: %s", paths.config_path)
    elif loaded.repaired:
        logger.warning(
            "配置文件有 %d 处内容非法, 已剔除并还原为默认值: %s",
            len(loaded.repaired),
            ", ".join(loaded.repaired),
        )
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
    elif loaded.repaired:
        # 只重写被修复的字段, 其余自定义配置原样保留。
        fields = ", ".join(loaded.repaired)
        lines.append(f"配置修复 : {len(loaded.repaired)} 处非法, 已剔除({fields})")
    lines.extend(
        [
            f"配置目录 : {paths.config_dir}",
            f"数据目录 : {paths.data_dir}",
            f"日志目录 : {paths.log_dir}",
            f"数据库   : {paths.database_path} (schema v{version})",
            f"备份根   : {paths.backup_root}",
        ]
    )
    for line in lines:
        _report(line)
    logger.info("初始化完成, schema 版本 %d", version)
    return 0


def _run_doctor(paths: ApplicationPaths, *, verbose: bool) -> int:
    loaded = _start_logging(paths, verbose=verbose)
    family = current_platform()
    registry_note = "可用" if has_windows_registry(family) else "不适用"
    _report(f"应用   : {APP_DISPLAY_NAME} {package_version()}")
    _report(f"平台   : {platform_label(family)} (注册表探测: {registry_note})")
    _report(f"配置目录: {paths.config_dir}")
    _report(f"数据目录: {paths.data_dir}")
    _report(f"日志目录: {paths.log_dir}")
    _report(f"备份根  : {paths.backup_root}")

    if loaded.reset:
        _report(f"配置   : 内容非法, 已还原为默认值(原文件: {loaded.backup})")
    else:
        _report(f"配置   : 格式 v{loaded.config.version}, 主题 {loaded.config.theme}")

    try:
        database = Database(paths.database_path)
        _report(
            f"数据库 : {paths.database_path} "
            f"(schema {database.schema_version()}/{database.latest_schema_version()})"
        )
    except Exception as exc:
        _report(f"数据库 : 不可用 -> {exc}")
    return 0


def _run_gui(paths: ApplicationPaths, *, smoke: float | None, verbose: bool) -> int:
    """启动图形界面;无图形环境或缺少 tkinter 时给出清晰错误.

    **打包版同一时间只允许开一个**(见 :mod:`archive_management.services.single_instance`):
    两个进程各持同一个数据库/备份根/快捷键注册会互相踩, 用户看到的是"点了没反应"这类
    怪事。

    **第二个实例不给提示也不开新窗**(用户 2026-10-12 的要求: "重复打开应该直接跳转到上次
    打开的窗口"): 双击的意图就是"我要用这个软件", 而它已经开着了 —— 于是这里写下一条
    "到前台"请求(已经在跑的那个在它的 100ms 轮询里看到就把窗口叫回来)并以退出码 3 退出。
    不再弹“已经多开了”的提示: 那是一道用户多余的确认。

    闸门只在 ``sys.frozen`` 下生效: 开发与用例需要同时开多个窗口(分片跑集成用例时每片
    都在自己的进程里建真窗口) —— 而且集成用例直接调 ``ui.main_window.run_gui``, 不经过
    这里。``ARCHIVE_MANAGEMENT_ALLOW_MULTI=1`` 是诊断用的逃生门。
    """
    if not is_packaged() or single_instance.multi_instance_allowed():
        return _start_gui(paths, smoke=smoke, verbose=verbose)
    lock = single_instance.InstanceLock(single_instance.lock_path(paths))
    if not lock.acquire():
        _hand_over_to_running_instance(paths, lock)
        return single_instance.ALREADY_RUNNING_EXIT
    try:
        return _start_gui(paths, smoke=smoke, verbose=verbose)
    finally:
        lock.release()


def _start_gui(paths: ApplicationPaths, *, smoke: float | None, verbose: bool) -> int:
    """真的去起窗口(异常一律收敛成"起不来"的返回码)."""
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


def _note_second_launch(
    paths: ApplicationPaths,
    lock: single_instance.InstanceLock,
    token: str,
    *,
    granted: bool,
    handled: bool,
) -> None:
    """第二个实例: 把日志按配置装好再记一行 —— 窗口化 exe 没有控制台, 日志是唯一证据.

    正常退出路径上的这一行与 ``ui.main_window`` 里那行(收到请求了/已把窗口叫回来)成对, 事后
    对日志就能看出"哪一次双击叫醒了窗口、有没有成"。两个布尔标记直接写进日志: 窗口没到前台时,
    一眼就能分清是"权限没交出去"(``granted=False``)还是"对方没响应/太忙"(``handled=False``)。

    读配置/装日志失败只当默认, **绝不能因此拦住退出**。
    """
    with contextlib.suppress(Exception):
        loaded = load_or_repair_config(paths.config_path)
        configure_from_settings(paths.log_dir, loaded.config.logging)
    logger.info(
        "已有实例在运行(%s): %s; 已请它把窗口叫到前台(%s, granted=%s handled=%s)",
        lock.path,
        lock.holder() or "未知",
        token,
        granted,
        handled,
    )


def _hand_over_to_running_instance(
    paths: ApplicationPaths, lock: single_instance.InstanceLock
) -> None:
    """第二个实例: 把前台交给已经在跑的那个, 然后自己退出(用户 2026-10-12 的要求).

    三步: ①写下"请到前台"请求(对方 100ms 轮询里就能看到); ②Windows 上把"允许提到前台"的
    权利也交出去 —— 后台进程自己抢不到前台(实测), 而用户刚双击起来的这个进程手上有; ③等对方
    真的抬起来(请求文件被清掉就是回执)再退, 因为那份权限可能随本进程退出而失效。

    三步都失败也照常退出: 用户那边只表现为"窗口没跳到前面", 不该退回"打了没反应"。
    """
    request_path = single_instance.focus_request_path(paths)
    token = single_instance.request_focus(paths)
    granted = single_instance.hand_over_foreground(
        single_instance.holder_pid(lock.holder())
    )
    handled = single_instance.wait_until_handled(request_path)
    _note_second_launch(paths, lock, token, granted=granted, handled=handled)


def main() -> None:
    """控制台脚本入口."""
    raise SystemExit(run())
