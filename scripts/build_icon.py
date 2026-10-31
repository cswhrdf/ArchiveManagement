"""把应用图标 SVG 光栅化成位图(``app.ico`` / ``app-*.png``).

图标只有一份可编辑的真相 ``resources/app.svg``; 打包与界面读的是它的产物。本脚本
负责"画"这一半(定位资源、逐档光栅化、用 Pillow 装多尺寸 ICO、写指纹), 判断与参数
拼装都在 :mod:`archive_management.services.icon_assets` 里(那部分被用例覆盖)。

为什么光栅化要外部工具而不是 Python 库
---------------------------------------

* ``cairosvg`` 在 Windows 上要自带的 cairo 原生库(不是纯 wheel), 会给"一条命令就能
  重画图标"加一层系统依赖;
* 光栅化只发生在**改图标的时候**(产物入库), 不在软件运行时, 所以"本机有一个能画
  SVG 的 ffmpeg"就够了 —— 实测 librsvg 版 ffmpeg 出图质量与本机其它工具一致;
* 软件自身**不执行外部程序**(见 ``tests/security/test_side_effects.py``), 因此这条
  路只在开发/构建期, 打包产物里只有位图, 与 ffmpeg 无关。

找不到 ffmpeg 时并不影响开发: 位图已经入库, ``--check`` 会说"已是最新"并以 0 退出,
只有**真的改了 SVG 需要重画**时才会要求 ffmpeg(报错里给出 ``--ffmpeg`` 与环境变量
``ARCHIVE_MANAGEMENT_FFMPEG`` 两种指路方式)。

用法::

    uv run python scripts/build_icon.py            # 需要时才重画
    uv run python scripts/build_icon.py --force    # 无条件重画
    uv run python scripts/build_icon.py --check    # 只检查(不一致退 1; 不需要 ffmpeg)
    uv run python scripts/build_icon.py --stage    # 重画后把产物加回暂存区(提交钩子用)
    uv run python scripts/build_icon.py --force --out-dir <目录>   # 探针/预览: 不动仓库
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import subprocess
import sys
from pathlib import Path

from PIL import Image

# 让脚本在"项目没被装进虚拟环境"的地方也能跑: CI 的安装步骤刻意带 `--no-install-project`
# (打包靠 spec 里的 pathex 指向 src), 那种环境里 `import archive_management` 只有把 src
# 放进 sys.path 才成立 —— 与本地 .env 里的 PYTHONPATH 是同一件事, 但脚本不该依赖环境
# 变量(uv 不读 .env, 只有 VS Code 的集成终端会注入)。
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# 尾注释必须短: ruff 把中文按 2 列算宽, 这行一旦超过 88 列, isort 就会想把 import 拆成
# 括号式多行(那时尾注释会被挤进括号里, E402 的豁免反而失效)。详细原因见上面那四行注释。
from archive_management.services import icon_assets  # noqa: E402 - 见上


class IconBuildError(RuntimeError):
    """图标生成失败(工具缺失、ffmpeg 出错、出图尺寸不对)."""


def render_png(exe: str, svg: Path, target: Path, size: int) -> None:
    """跑一次 ffmpeg 出图, 并核对出图尺寸.

    核对不是多余的: 出图尺寸与环境有关(SVG 声明的画布大小、解码器能力), 尺寸不对
    时宁可当场失败, 也不要把一张糊图塞进发布包。
    """
    argv = icon_assets.ffmpeg_argv(exe, svg, target, size)
    # 参数是本模块拼出来的固定值, 不经 shell 执行。
    completed = subprocess.run(  # noqa: S603
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "无输出"
        raise IconBuildError(
            f"ffmpeg 光栅化 {size}px 失败(退出码 {completed.returncode}): {detail}"
        )
    if not target.is_file():
        raise IconBuildError(f"ffmpeg 报告成功但没有写出文件: {target.name}")
    with Image.open(target) as image:
        actual = image.size
    if actual != (size, size):
        raise IconBuildError(f"{target.name} 出图尺寸是 {actual}, 应该是 {size}x{size}")


def build(svg: Path, out_dir: Path, exe: str) -> list[Path]:
    """按 PNG 尺寸表逐档出图, 再装 ICO 并写指纹; 返回这次写出的文件."""
    if not svg.is_file():
        raise IconBuildError(f"找不到图标源文件: {svg}")
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for size, name in icon_assets.PNG_SIZES:
        target = out_dir / name
        render_png(exe, svg, target, size)
        written.append(target)
    ico = out_dir / icon_assets.ICO_NAME
    # ICO 的源图是最大那一档 PNG(只缩不放大)。
    icon_assets.render_ico(out_dir / icon_assets.PNG_SIZES[0][1], ico)
    # 窗口那颗: 档少 + BMP 编码 —— Tk 读它, 别把七档大图喂给 Tk(见 services/icon_assets)。
    window_ico = out_dir / icon_assets.WINDOW_ICO_NAME
    icon_assets.render_ico(
        out_dir / icon_assets.PNG_SIZES[0][1],
        window_ico,
        sizes=icon_assets.WINDOW_ICO_SIZES,
        bitmap=True,
    )
    icon_assets.write_stamp(svg, out_dir)
    return [*written, ico, window_ico, out_dir / icon_assets.STAMP_NAME]


def stage(paths: list[Path]) -> bool:
    """把生成的文件加回暂存区, 返回是否成功(没有 git 时返回 ``False``)."""
    git = shutil.which("git")
    if git is None:  # pragma: no cover - 正常开发环境都有 git
        return False
    completed = subprocess.run(  # noqa: S603
        [git, "add", "--", *(str(path) for path in paths)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return completed.returncode == 0


def use_utf8_output() -> None:
    """把标准输出与错误输出固定为 UTF-8(git/pre-commit 按 UTF-8 解码钩子输出)."""
    with contextlib.suppress(AttributeError, ValueError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def build_parser() -> argparse.ArgumentParser:
    """命令行参数: 只检查 / 强制重画 / 重新暂存 / 指定 ffmpeg / 换个输出目录."""
    parser = argparse.ArgumentParser(
        prog="build_icon",
        description="把应用图标 SVG 光栅化成 app.ico 与 app-*.png",
    )
    parser.add_argument("--check", action="store_true", help="只检查, 不写文件")
    parser.add_argument("--force", action="store_true", help="无视指纹重画一遍")
    parser.add_argument("--stage", action="store_true", help="把产物加回 git 暂存区")
    parser.add_argument("--ffmpeg", default=None, help="指定 ffmpeg 可执行文件")
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="产物写到这个目录而不是资源树(探针/预览用; 给了它就总是重画)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """按命令行执行, 返回退出码(0 成功 / 1 检查不一致 / 2 环境或生成失败)."""
    args = build_parser().parse_args(argv)
    root = icon_assets.resource_root()
    svg = root / icon_assets.SVG_NAME
    state = icon_assets.asset_state(root)
    if args.check:
        print(f"应用图标: {state.detail}")
        return 0 if state.fresh else 1
    # `--out-dir` 是探针/预览: 产物写到别的目录, 资源树一个字节都不动, 所以不做
    # "已是最新"的短路 —— 每次都真的画一遍。
    if args.out_dir is None and state.fresh and not args.force:
        print(f"应用图标已是最新: {state.detail}")
        return 0
    out_dir = args.out_dir or root
    exe = args.ffmpeg or icon_assets.find_ffmpeg()
    if not exe:
        hint = (
            f"把 ffmpeg 放进 PATH, 或用 --ffmpeg 指定, 或设置 {icon_assets.FFMPEG_ENV}"
        )
        print(
            f"图标需要重画, 但找不到 ffmpeg: {state.detail} —— {hint}", file=sys.stderr
        )
        return 2
    try:
        written = build(svg, out_dir, exe)
    except (IconBuildError, OSError) as exc:
        print(f"图标生成失败: {exc}", file=sys.stderr)
        return 2
    for path in written:
        print(f"已生成 {path.name}")
    if args.stage and not stage(written):
        print("已重画但未能加入暂存区: 请手动 git add 这些文件", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    use_utf8_output()
    raise SystemExit(main())
