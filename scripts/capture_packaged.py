r"""打包版与开发版的逐屏比对(本机工具, 不进 CI 门禁).

用法(仓库根目录, 单文件产物先本地构建好)::

    uv run python scripts/capture_packaged.py still      # L1 同窗连拍(抓取链路自检)
    uv run python scripts/capture_packaged.py restart    # L2 开发版重启双拍(跨进程噪声)
    uv run python scripts/capture_packaged.py compare    # L3 三对画面 dev vs exe 逐屏比对

比对算尺直接借 ``scripts/create_allure_visual.py`` 的 ``compare_one`` /
``write_diff``(phash + SSIM), 判据因此与视觉基线门禁同源。但那套阈值是在 Linux CI
上按"同渲染噪声 = 0(逐位相同)"标定的, Windows 双进程的噪声没人量过 —— 所以先跑
``still`` / ``restart`` 两级标定, 干净才沿用阈值, 不干净按实测分布另定(2026-10-19
在 Windows 11 / 100% 缩放上实测: L1/L2 都是距离 0 / SSIM 1.0000, 阈值原样沿用)。

只拍 3 张画面: 01 主页列表(默认启动) / 02 海报视图(种子预置 layout=POSTER) /
07 空库(只 init 不灌数据)。其余画面(详情/折叠分支/发现/启停分区、浅色主题)都要
点击或切页签才到得了, 静默抓拍够不着, 不在清单里。输出默认放在
仓库外(``%TEMP%\am-j4-visual\`` 下), 验收材料不进仓库。
"""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

from archive_management.domain import HomeLayout
from archive_management.packaging import APP_DISPLAY_NAME

if TYPE_CHECKING:
    from PIL import Image

if sys.platform == "win32":  # 本工具只在 Windows 上跑(模块 docstring 已写明)
    from ctypes import wintypes

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = REPO_ROOT / "dist" / "ArchiveManagement" / "ArchiveManagement.exe"
RULER_PATH = REPO_ROOT / "scripts" / "create_allure_visual.py"
SMOKE_SECONDS = 18.0
POLL_INTERVAL = 0.3
POLL_TIMEOUT = 25.0
SETTLE_SECONDS = 2.5
STILL_GAP_SECONDS = 1.0
# 种子数据里的时刻全部取固定值: 画面上的时间列在"同一时段的两次抓拍"里必须同串,
# 相对时间标签("N 天前")按天取整, 两侧背靠背抓拍不会跨天。
SEED_MOMENT = datetime(2026, 10, 18, 21, 30, tzinfo=UTC)
# (名称, 来源, 备份数): 覆盖"多备份/单备份/两份"三档, 列表与海报两种形态都有得看。
SEED_GAMES = (
    ("星海拓荒者", "steam", 3),
    ("雾都列车员", "epic", 1),
    ("像素牧场物语", "manual", 2),
)


@dataclass(frozen=True)
class ScreenSpec:
    """一张待拍画面的种子参数."""

    name: str
    layout: HomeLayout
    with_data: bool


# 主题不进清单是量出来的: GUI 启动固定深色(sql_backend 的 _theme 初始化为
# _DEFAULT_THEME), set_theme 只改内存不落盘, config.theme 与启动外观无关 ——
# 切浅色只能点设置页(输入合成), 静默抓拍够不着。
SCREENS = (
    ScreenSpec("01-home-list-dark", HomeLayout.LIST, True),
    ScreenSpec("02-home-poster-dark", HomeLayout.POSTER, True),
    ScreenSpec("07-home-empty-dark", HomeLayout.LIST, False),
)

_ruler: ModuleType | None = None


def _load_ruler() -> ModuleType:
    """按文件路径加载算尺模块(其 compare_one 自己会惰性导入 visual 组依赖).

    不走 ``sys.path``/CWD: 两个脚本同目录不等于同包, 按路径加载最不依赖现场。
    模块底部有 ``__main__`` 守卫, 加载不会触发它的抓图流程。
    """
    global _ruler
    if _ruler is None:
        spec = importlib.util.spec_from_file_location(
            "create_allure_visual", RULER_PATH
        )
        if spec is None or spec.loader is None:  # pragma: no cover - 路径在就不会
            raise RuntimeError(f"加载不了算尺模块: {RULER_PATH}")
        module = importlib.util.module_from_spec(spec)
        # 必须先注册再 exec: dataclasses 的 KW_ONLY 探测要靠
        # sys.modules.get(cls.__module__) 拿到模块命名空间。
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _ruler = module
    return _ruler


def compare_pair(name: str, actual: Path, baseline: Path) -> tuple[str, str, str]:
    """借门禁的 compare_one 比一对图, 返回 (距离文本, SSIM 文本, 判词)."""
    verdict = _load_ruler().compare_one(name, actual, baseline)
    distance = "—" if verdict.hash_distance is None else str(verdict.hash_distance)
    ssim = "—" if verdict.ssim is None else f"{verdict.ssim:.4f}"
    return distance, ssim, verdict.problem or "通过"


def warn_unless_100_percent() -> None:
    """钉住 100% 缩放, 不是 100% 就给一条 CI 风格的警告.

    读注册表的 ``AppliedDPI`` 而不是 ``GetDpiForSystem``: 本进程没声明 DPI 感知,
    后者会被系统虚拟化骗成 96, 注册表这份才是真实生效值。
    """
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop\WindowMetrics"
    ) as key:
        applied = int(winreg.QueryValueEx(key, "AppliedDPI")[0])
    if applied != 96:
        print(
            f"::warning::系统缩放 {applied} DPI(非 100%), 本工具的比对口径要求 100%。"
        )


def _titled_windows(title_prefix: str) -> dict[int, str]:
    """枚举可见且标题带指定前缀的顶层窗口, 返回 {句柄: 标题}.

    **不按进程过滤**是量出来的结论: Windows 上 venv 的 ``python.exe`` 是重定向器
    (PEP 405), 真解释器是它的**子进程**, Tk 窗口归孙进程所有 —— "子进程 pid"永远
    对不上窗口。改用"起前快照 + 新窗匹配"(见 ``running_window``)。
    """
    user32 = ctypes.windll.user32
    found: dict[int, str] = {}

    def collect(handle: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(handle):
            return True
        length = user32.GetWindowTextLengthW(handle)
        if length <= 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(handle, buffer, length + 1)
        if buffer.value.startswith(title_prefix):
            found[handle] = buffer.value
        return True

    # 不写成 @WINFUNCTYPE 装饰器: 那条路在 mypy strict 下会被判 "untyped
    # decorator"(经元类实例化的路径拿不到类型); 先拿原型再显式包一层, 走的才是
    # typeshed 里 CFuncPtr.__new__(callable) 的有类型重载。回调对象留在局部名
    # callback 上, 枚举期间不会被回收(原装饰写法同样靠局部名保活)。
    callback = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(
        collect
    )
    user32.EnumWindows(callback, 0)
    return found


def _client_size(handle: int) -> int:
    """返回客户区宽 x 高(拿不到按 0 计, 仅供选窗比较)."""
    rect = _client_rect(handle)
    return 0 if rect is None else rect[0] * rect[1]


def _client_rect(handle: int) -> tuple[int, int, int, int] | None:
    """返回 (客户区宽, 客户区高, 客户区相对窗口左上角的 x, y); 失败返回 None."""
    user32 = ctypes.windll.user32
    window_rect = wintypes.RECT()
    client_rect = wintypes.RECT()
    if not user32.GetWindowRect(handle, ctypes.byref(window_rect)):
        return None
    if not user32.GetClientRect(handle, ctypes.byref(client_rect)):
        return None
    origin = wintypes.POINT(0, 0)
    if not user32.ClientToScreen(handle, ctypes.byref(origin)):
        return None
    offset_x = origin.x - window_rect.left
    offset_y = origin.y - window_rect.top
    return (
        client_rect.right - client_rect.left,
        client_rect.bottom - client_rect.top,
        offset_x,
        offset_y,
    )


def capture_client_area(handle: int) -> Image.Image:
    """PrintWindow 按句柄抓窗口, 裁到客户区后返回.

    PIL 的 ``grab(window=...)`` 抓的是整窗(含标题栏), 而标题栏图标/边框不属于
    "画面本身"、两侧也不保证逐位相同 —— 统一裁到客户区把这个变量消掉(PIL 若
    给到的已经是客户区尺寸, 原样返回)。
    """
    from PIL import ImageGrab

    rect = _client_rect(handle)
    if rect is None:
        raise RuntimeError("拿不到窗口矩形(GetWindowRect/GetClientRect 失败)")
    width, height, offset_x, offset_y = rect
    image = ImageGrab.grab(window=handle)
    if image.size != (width, height):
        image = image.crop((offset_x, offset_y, offset_x + width, offset_y + height))
    if image.size != (width, height):
        message = f"客户区 {width}x{height}, 抓到 {image.size[0]}x{image.size[1]}"
        raise RuntimeError(message)
    return image


@contextmanager
def running_window(command: list[str]) -> Iterator[int]:
    """起子进程并等它的主窗口出现, 让出窗口句柄; 退出时确保进程树结束.

    认窗方式是"起前快照 + 新窗匹配": 先记下桌面上已有的同前缀窗口(比如用户自己
    开着的那份), 起进程后轮询**新出现**的窗口 —— 不看 pid(见 ``_titled_windows``
    的说明)。窗口用 ``--smoke`` 兜底自退; 正常路径抓完图就终止整棵进程树(venv
    重定向器的子进程要靠 /T 才杀得到), 每次都是全新克隆的临时 root, 不需要优雅
    关闭留下的现场。
    """
    existing = set(_titled_windows(APP_DISPLAY_NAME))
    process = subprocess.Popen(  # noqa: S603 - 命令与参数都写死在本文件里
        command,
        cwd=REPO_ROOT,
    )
    deadline = time.monotonic() + POLL_TIMEOUT
    handle: int | None = None
    while time.monotonic() < deadline:
        candidates = {
            found: title
            for found, title in _titled_windows(APP_DISPLAY_NAME).items()
            if found not in existing
        }
        if candidates:
            # 同前缀多窗口时取客户区最大的那个: 主窗口一定比对话框大。
            handle = max(candidates, key=_client_size)
            break
        if process.poll() is not None:
            raise RuntimeError(
                f"窗口没出现进程就退了(退出码 {process.returncode}): {command}"
            )
        time.sleep(POLL_INTERVAL)
    if handle is None:
        _kill_tree(process)
        raise RuntimeError(f"等窗口超时({POLL_TIMEOUT:.0f}s): {command}")
    try:
        time.sleep(SETTLE_SECONDS)
        yield handle
    finally:
        _kill_tree(process)


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """终止整棵进程树并等它退干净; 单杀 launcher 会留下真解释器进程."""
    taskkill = shutil.which("taskkill")
    if process.poll() is None and taskkill is not None:
        killed = subprocess.run(  # noqa: S603 - taskkill 是 Windows 自带, 参数只有本进程的 pid
            [taskkill, "/PID", str(process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        if killed.returncode != 0:  # pragma: no cover - taskkill 失败才走
            process.kill()
    elif process.poll() is None:  # pragma: no cover - 系统里没有 taskkill 才走
        process.kill()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover - 树杀失败才走
        process.kill()


def shoot(command: list[str], target: Path) -> Path:
    """起进程 → 等窗 → 拍客户区存盘 → 终止, 返回图片路径."""
    with running_window(command) as handle:
        capture_client_area(handle).save(target)
    return target


def dev_command(root: Path) -> list[str]:
    """开发版启动命令(与 CI 冒烟同款: --root 收敛 + --smoke 兜底自退)."""
    return [
        sys.executable,
        "-m",
        "archive_management",
        "gui",
        "--root",
        str(root),
        "--smoke",
        str(SMOKE_SECONDS),
    ]


def exe_command(exe: Path, root: Path) -> list[str]:
    """打包版启动命令."""
    return [str(exe), "gui", "--root", str(root), "--smoke", str(SMOKE_SECONDS)]


def seed_root(root: Path, *, layout: HomeLayout, with_data: bool) -> None:
    """按真实管道灌一份种子 root(init 配置/建库 → 写游戏与备份 → 预置主页形态).

    dev 与 exe 不共用这一份, 而是各拿一份克隆(``clone_root``): 同源由"同一份
    种子的两个副本"保证, 任何一侧也不会把另一侧的运行痕迹写回种子。
    """
    from archive_management.config import load_or_repair_config, save_config
    from archive_management.domain import Game, SaveLocation
    from archive_management.domain.entities import BackupNode
    from archive_management.domain.home import HomeFilter
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.paths import ApplicationPaths
    from archive_management.infrastructure.repository import (
        BackupRepository,
        GameRepository,
        HomeRepository,
        SaveLocationRepository,
    )

    paths = ApplicationPaths.default(override_root=root).ensure()
    loaded = load_or_repair_config(paths.config_path)
    save_config(loaded.config, paths.config_path)
    database = Database(paths.database_path)
    database.migrate()

    games = GameRepository(database)
    locations = SaveLocationRepository(database)
    backups = BackupRepository(database)
    if with_data:
        for index, (name, origin, save_count) in enumerate(SEED_GAMES):
            game = games.add(
                Game(
                    name=name,
                    origin=origin,
                    enabled=index == 0,
                    created_at=SEED_MOMENT - timedelta(days=30),
                    last_activity_at=SEED_MOMENT - timedelta(days=index),
                )
            )
            if game.id is None:  # pragma: no cover - add 总是回填 id
                raise RuntimeError("GameRepository.add 没有回填 id")
            locations.add(
                SaveLocation(game_id=game.id, path=f"C:/Saves/{name}", is_primary=True)
            )
            for order in range(save_count):
                backups.add(
                    BackupNode(
                        game_id=game.id,
                        created_at=SEED_MOMENT - timedelta(days=7 * order),
                        title=f"每周备份 {order + 1}",
                        storage_relpath=f"seed/{index + 1}/{order + 1}",
                    )
                )
    HomeRepository(database).save_state(HomeFilter(layout=layout))


def clone_root(seed: Path, target: Path) -> Path:
    """把种子 root 复制成另一份; 已存在时先清掉(重跑不残留上一轮的运行痕迹)."""
    if target.exists():
        shutil.rmtree(target)
    shutil.copytree(seed, target)
    return target


def write_summary(out: Path, title: str, rows: list[tuple[str, str, str, str]]) -> Path:
    """把逐条结论写成 markdown(本地留档, 不进仓库)."""
    lines = [
        f"# {title}",
        "",
        "| 画面 | phash 距离 | SSIM | 结论 |",
        "|---|---|---|---|",
    ]
    lines.extend(
        f"| {name} | {distance} | {ssim} | {verdict} |"
        for name, distance, ssim, verdict in rows
    )
    target = out / "summary.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target


def cmd_still(out: Path) -> int:
    """L1: 同一窗口连拍两张 —— 抓取链路自检, 期望逐位相同(距离 0 / SSIM 1)."""
    seed = out / "seed"
    seed_root(seed, layout=HomeLayout.LIST, with_data=True)
    root = clone_root(seed, out / "root-dev")
    with running_window(dev_command(root)) as handle:
        capture_client_area(handle).save(out / "still-a.png")
        time.sleep(STILL_GAP_SECONDS)
        capture_client_area(handle).save(out / "still-b.png")
    distance, ssim, verdict = compare_pair(
        "still", out / "still-b.png", out / "still-a.png"
    )
    print(f"still: 距离={distance} SSIM={ssim} → {verdict}")
    write_summary(out, "L1 同窗连拍", [("still", distance, ssim, verdict)])
    return 0 if verdict == "通过" else 1


def cmd_restart(out: Path) -> int:
    """L2: 开发版重启两次各拍一张 —— 量"跨进程"噪声(L3 的噪声底)."""
    seed = out / "seed"
    seed_root(seed, layout=HomeLayout.LIST, with_data=True)
    first = shoot(dev_command(clone_root(seed, out / "root-a")), out / "restart-a.png")
    second = shoot(dev_command(clone_root(seed, out / "root-b")), out / "restart-b.png")
    distance, ssim, verdict = compare_pair("restart", second, first)
    print(f"restart: 距离={distance} SSIM={ssim} → {verdict}")
    write_summary(out, "L2 开发版重启双拍", [("restart", distance, ssim, verdict)])
    return 0 if verdict == "通过" else 1


def cmd_compare(out: Path, exe: Path) -> int:
    """L3: 三对画面成对抓拍 dev/exe, 借门禁算尺逐对比对."""
    if not exe.is_file():
        print(f"找不到打包产物: {exe}(先按 docs/development.md 本地构建)。")
        return 2
    rows: list[tuple[str, str, str, str]] = []
    for spec in SCREENS:
        seed = out / spec.name / "seed"
        seed_root(seed, layout=spec.layout, with_data=spec.with_data)
        dev_shot = shoot(
            dev_command(clone_root(seed, out / spec.name / "root-dev")),
            out / f"{spec.name}-dev.png",
        )
        exe_shot = shoot(
            exe_command(exe, clone_root(seed, out / spec.name / "root-exe")),
            out / f"{spec.name}-exe.png",
        )
        distance, ssim, verdict = compare_pair(spec.name, exe_shot, dev_shot)
        if verdict != "通过":
            _load_ruler().write_diff(exe_shot, dev_shot, out / f"{spec.name}-diff.png")
        rows.append((spec.name, distance, ssim, verdict))
        print(f"{spec.name}: 距离={distance} SSIM={ssim} → {verdict}")
    summary = write_summary(out, f"逐屏比对(dev vs {exe.name})", rows)
    print(f"结论表: {summary}")
    return 0 if all(row[3] == "通过" for row in rows) else 1


def build_parser() -> argparse.ArgumentParser:
    """构造命令行参数解析器."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--out", type=Path, default=None, help="输出目录(默认在系统临时目录下)"
    )
    parser = argparse.ArgumentParser(
        prog="capture_packaged",
        description="打包版与开发版的逐屏比对(本机工具)",
        parents=[common],
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)
    subparsers.add_parser("still", help="L1 同窗连拍(抓取链路自检)", parents=[common])
    subparsers.add_parser(
        "restart", help="L2 开发版重启双拍(跨进程噪声)", parents=[common]
    )
    compare_parser = subparsers.add_parser(
        "compare", help="L3 三对画面 dev vs exe 逐屏比对", parents=[common]
    )
    compare_parser.add_argument(
        "--exe",
        type=Path,
        default=DEFAULT_EXE,
        help=f"打包产物路径(默认 {DEFAULT_EXE})",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """命令行入口."""
    if sys.platform != "win32":
        print("本比对只在 Windows 上做(打包产物是 Windows 的)。")
        return 2
    args = build_parser().parse_args(argv)
    warn_unless_100_percent()
    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    out = args.out or (
        Path(tempfile.gettempdir()) / "am-j4-visual" / f"{args.mode}-{stamp}"
    )
    out.mkdir(parents=True, exist_ok=True)
    if args.mode == "still":
        result = cmd_still(out)
    elif args.mode == "restart":
        result = cmd_restart(out)
    else:
        result = cmd_compare(out, Path(args.exe))
    print(f"输出目录: {out}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
