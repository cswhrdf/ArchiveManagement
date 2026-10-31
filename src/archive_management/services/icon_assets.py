"""应用图标: ``resources/app.svg`` 是唯一真相, 位图(``.ico`` / ``.png``)是它的产物.

为什么要"算在这里、跑在别处": 本模块只管**算**(定位文件、比对指纹、拼命令行参数、
用 Pillow 装多尺寸 ICO); 真正的光栅化交给 ``scripts/build_icon.py`` —— 软件自身
**不执行任何外部程序**(``tests/security/test_side_effects.py`` 有动态与静态两道闸),
所以这里不出现任何"执行"入口, 只返回参数。

流水线(改图标只需动一处)::

    app.svg  --(ffmpeg / librsvg)-->  app-256.png  app-64.png
             --(Pillow, 装多尺寸)--> app.ico  app-window.ico
             --(sha256 指纹)------>  app-icon.stamp

* ``app.ico``: Windows 的 exe 资源(打包用), 里面装 16/24/32/48/64/128/256 七档 ——
  少了 256 会在资源管理器大图标下变糊;
* ``app-window.ico``: **窗口/任务栏**图标(Tk 的 ``wm iconbitmap``)。**故意另做一颗小图**:
  实测(2026-10-11)把这颗七档大图交给 Tk 读写会让 Windows 上的 Tk 在长会话后于重绘时
  踩到已释放内存(access violation, 见 ``ui/window_icon.py`` 与 ``docs/development.md``),
  而这颗只有 16/24/32/48 **四档 BMP 编码**(Tk 的 Windows 图标读写器本来吃的就是 DIB);
* ``app-256.png``: 非 Windows 的窗口图标(``iconphoto``)与说明书配图;
* ``app-64.png``: 界面里的品牌位(顶栏那个 30px 的方块, 2x 余量);
* ``app-icon.stamp``: 记住这批位图出自哪一版 SVG, 于是"改了 SVG 忘了重画"可以有
  一条明确的判据(守卫与启动日志都读它)。

**为什么不用 ffmpeg 输出 ICO**: 实测本机 ffmpeg(9.0.2, 带 ``--enable-librsvg``)
``-f ico`` 只写出**一档**尺寸(BMP 编码, 64x64), 而 Windows 需要多档; Pillow 本来就
是运行依赖(``CTkImage`` 要它), 由它装配 ICO 既不多一个依赖, 也不额外要求外部工具。
"""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path

from PIL import Image

_PACKAGE = "archive_management"
_RESOURCE_DIR = "resources"

#: 唯一真相: 人改这个文件, 位图全部由它生成.
SVG_NAME = "app.svg"
#: 多尺寸 Windows 图标(exe 的 PE 资源; 由 Pillow 装, PNG 编码).
ICO_NAME = "app.ico"
#: 窗口/任务栏用的那颗(Tk 的 ``wm iconbitmap`` 只读它): 档少、BMP 编码, 见模块 docstring.
WINDOW_ICO_NAME = "app-window.ico"
#: 记录"这批位图出自哪一版 SVG"的指纹.
STAMP_NAME = "app-icon.stamp"
#: 各尺寸 PNG: ``(边长, 文件名)``; 256 是 ICO 的源图, 64 给界面里的品牌位.
PNG_SIZES: tuple[tuple[int, str], ...] = ((256, "app-256.png"), (64, "app-64.png"))
#: ``app.ico`` 里要装的档位(覆盖 16px 列表视图到 256px 大图标).
ICO_SIZES: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)
#: ``app-window.ico`` 的档位: 标题栏 16、缩放 24/32、高 DPI 任务栏 48 就够.
WINDOW_ICO_SIZES: tuple[int, ...] = (16, 24, 32, 48)
#: 让"本机的 ffmpeg 不在 PATH 上"也能用(指到具体的可执行文件).
FFMPEG_ENV = "ARCHIVE_MANAGEMENT_FFMPEG"
#: 光栅化用的滤镜: 由大往小缩(源图 256), 所以不放大、不会糊.
SCALE_FLAGS = "lanczos"


@dataclass(frozen=True)
class AssetState:
    """位图与 SVG 是否一致, 附一句给人看的理由."""

    fresh: bool
    detail: str


@dataclass(frozen=True)
class IconFiles:
    """当前可用的图标文件(定位不到的那一项是 ``None``, 界面按"没有就退回落样式"处理).

    ``ico`` 是给 exe 做 PE 资源那颗(由 ``packaging/archive-management.spec`` 读) ——
    界面这边用不到它(窗口走 ``window_ico``), 报告出来是为了日志与守卫能核对整套位图。
    """

    ico: Path | None
    window_ico: Path | None
    window_png: Path | None
    logo_png: Path | None


def resource_root() -> Path:
    """图标资源所在目录(开发树与打包后的 ``_MEIPASS`` 都是真实目录, 与 i18n 同一套定位)."""
    return Path(str(files(_PACKAGE).joinpath(_RESOURCE_DIR)))


def asset_path(name: str, root: Path | None = None) -> Path:
    """资源树里某个资产的路径(不保证存在)."""
    return (root or resource_root()) / name


def required_assets(root: Path | None = None) -> tuple[Path, ...]:
    """生成物清单: 少一个就说明"这套位图不完整"."""
    base = root or resource_root()
    return (
        base / ICO_NAME,
        base / WINDOW_ICO_NAME,
        *(base / name for _, name in PNG_SIZES),
    )


def fingerprint(svg: Path) -> str:
    """SVG 内容的 sha256(十六进制); 位图与本指纹一起构成"这一版".

    只认内容、不认换行风格: SVG 是 XML 文本, CRLF 与 LF 语义相同但字节不同,
    而工作区是哪一种由每个贡献者的 ``core.autocrlf`` 决定(仓库历史上没有统一的 eol
    约束)。直接对工作区字节取哈希, "同一次重画"就会在不同平台上得到不同指纹 ——
    2026-10-15 的 CI 即此: stamp 在 Windows(CRLF 工作区)上写入, macOS/Linux 按 LF
    检出, 两个平台的守卫全红(位图本身没有任何问题)。指纹与检出平台解耦后, stamp
    在哪台机器上写都一样; ``.gitattributes`` 再把资源文本的 eol 钉死, 双保险。
    """
    return hashlib.sha256(svg.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def asset_state(root: Path | None = None) -> AssetState:
    """位图是否与 SVG 一致(缺失 / 指纹不符都算"不一致", 并写明是哪一种)."""
    base = root or resource_root()
    svg = base / SVG_NAME
    if not svg.is_file():
        return AssetState(False, f"缺少图标源文件 {SVG_NAME}")
    missing = [path.name for path in required_assets(base) if not path.is_file()]
    if missing:
        return AssetState(False, "缺少位图: " + ", ".join(missing))
    stamp = base / STAMP_NAME
    wanted = fingerprint(svg)
    if not stamp.is_file() or stamp.read_text(encoding="utf-8").strip() != wanted:
        return AssetState(False, "位图不是这份 SVG 的最新产物")
    return AssetState(True, f"{SVG_NAME} {wanted[:12]} 与位图一致")


def summary() -> str:
    """启动日志用的一行状态(不一致时直接给出该跑哪条命令)."""
    state = asset_state()
    tail = (
        "" if state.fresh else " —— 运行 `uv run python scripts/build_icon.py` 重新生成"
    )
    return f"应用图标: {state.detail}{tail}"


def _existing(base: Path, name: str) -> Path | None:
    """资源树里存在就返回路径, 否则 ``None``(缺资源是正常状态, 不抛异常)."""
    path = base / name
    return path if path.is_file() else None


def icon_files(root: Path | None = None) -> IconFiles:
    """当前可用的图标文件; 一个都没有时每一项都是 ``None``(界面退回落样式)."""
    base = root or resource_root()
    return IconFiles(
        ico=_existing(base, ICO_NAME),
        window_ico=_existing(base, WINDOW_ICO_NAME),
        window_png=_existing(base, PNG_SIZES[0][1]),
        logo_png=_existing(base, PNG_SIZES[1][1]),
    )


def ffmpeg_argv(exe: str, svg: Path, png: Path, size: int) -> list[str]:
    """光栅化一次的参数(只拼不跑): ``svg`` --scale--> ``size`` 见方的 ``png``."""
    return [
        exe,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(svg),
        "-vf",
        f"scale={size}:{size}:flags={SCALE_FLAGS}",
        "-pix_fmt",
        "rgba",
        "-frames:v",
        "1",
        "-update",
        "1",
        str(png),
    ]


def find_ffmpeg(which: Callable[[str], str | None] = shutil.which) -> str | None:
    """定位 ffmpeg: 先看环境变量, 再查 PATH; 找不到返回 ``None``(不报错)."""
    override = os.environ.get(FFMPEG_ENV, "").strip()
    if override:
        return override if Path(override).is_file() else None
    return which("ffmpeg")


def render_ico(
    source: Path,
    target: Path,
    *,
    sizes: Sequence[int] = ICO_SIZES,
    bitmap: bool = False,
) -> None:
    """把 ``source``(正方形 PNG)装成多尺寸 ICO; 各档由 Pillow 从源图缩出来.

    ``bitmap=True`` 时用 BMP/DIB 编码(Pillow 默认对带 alpha 的图写 PNG 档) —— 窗口那颗
    走这条路, 因为 Tk 的 Windows 图标读写器吃的就是 DIB, 而实测反复喂 PNG 多档大图会
    把 Tk 弄崩(见模块 docstring)。
    """
    with Image.open(source) as image:
        image.load()
        options: dict[str, object] = {"sizes": [(size, size) for size in sizes]}
        if bitmap:
            options["bitmap_format"] = "bmp"
        image.save(target, format="ICO", **options)


def write_stamp(svg: Path, root: Path | None = None) -> None:
    """记下这批位图出自哪一版 SVG(生成成功后最后一步)."""
    asset_path(STAMP_NAME, root).write_text(fingerprint(svg) + "\n", encoding="utf-8")
