"""界面视觉回归(感知哈希 + SSIM), 并把结论写进 Allure.

为什么需要它: 布局/配色/文字裁剪这类问题**不会让任何用例变红** —— 界面照样能跑, 单元与
集成用例也照样通过, 只有人去看才发现"这行字被裁了""这个面板歪了"。这个脚本把"界面的长相"
变成一条可比较的结论: 起一个真实窗口、截图、与前一轮的基线图比, 结果与图一起进报告。

## 判据(两条, 分工不同)

- **感知哈希**(``imagehash.phash``, 默认 8x8 → 64 位)给汉明距离: 对"整体长相变了没有"
  敏感, 而且与逐像素比对不同 —— 它容忍抗锯齿/亚像素渲染这类噪声;
- **结构相似度**(``skimage.metrics.structural_similarity``)给 0~1 的相似度: 抓细粒度退化
  (某处文字重排、颜色偏移)。两者都通过才算这一张通过。

两条阈值都在下面的常量里, 也能用命令行覆盖。**它们按 CI 上实测的分布定**, 不再是最初那个
"同渲染应当完全相同"的估数(2026-10-05 第一次拿到分布, 见 PLAN §18.4): 七张画面里**同渲染的
噪声实测为 0** —— 四张与基线逐位相同(哈希 0 / SSIM 1.0000); 而"真实界面变化"最小到
SSIM **0.9819**(主页工具栏/筛选那一行整体位移), 已知的真缺陷是 **0.9722**(§40.3 的 11px
行高错)。所以取 `MIN_SSIM = 0.995`(离"完全相同"留 0.005 余量, 而上面两类"真要人看一眼"
的都仍判红)与 `MAX_HASH_DISTANCE = 2`(原来的 6 太松: 实测里真实变化只到 2-4, 它其实从没
参与过判定)。改之前先去看那份实测, **不要凭印象改**: 太松等于没判据, 太紧会把 runner 镜像
更新(字体/显卡驱动变化)变成随机红 —— 那时该先确认"是新基线还是阈值", 而不是直接调数字。

## 基线从哪来(为什么"缺基线"不算失败)

基线图**入库**(``tests/visual-baselines/``), 因为视觉回归的价值就在于"界面变了要有人看一眼
再合" —— 基线跟着代码走, 才在 PR 里看得见"这版把基线图也改了"。基线缺失时(第一次上线、
或新增了一张画面)本脚本**不比较**, 只把这次抓到的图写成候选基线(``--candidates``),
并在描述里写明"基线尚未入库", 由人工确认后提交。理由: 让"没有基线"直接红, 会把第一轮
永远卡住; 让它静默绿, 又会把"没判据"混进"通过"。所以取中间: 状态通过、描述把话说清楚、
候选图作为附件上传。

## 依赖谁(为什么不再复用 ui-review/)

建窗口与切状态这套是**本脚本自己**的(空库那一组画面就是它的全部输入), 抓图原语复用
``tests/crash_capture.py`` 里已被 CI 验证的那两个函数(``window_box`` / ``grab_png``)。

``ui-review/`` 那条路已经拆掉: 那个目录被它自己的 ``.gitignore`` 整个挡在仓库外(CI 上
根本不存在), 而且只是开发阶段的临时物、迟早要删 —— 门禁不该依赖一个仓库里没有的文件。

## 拍什么(两套画面: 有数据的 + 空库)

**有数据的那一套**用**演示后端**(``ui/demo_backend.py``): 它的数据是模块级常量(名字/平台/
备份/时间都是固定值), 不联网、不读 ``dev-data``、不写盘, 每次跑出来完全一样 —— 与空库
一样适合跨运行比较, 但**覆盖到了真正的布局**(列表的列宽与名称裁剪、状态列、海报网格、
详情页表头、发现与启停两个分区)。空库那几张画面里一行数据都没有, 上面这些一个都没被
覆盖到 —— 而视觉回归要拦的正是它们。最后一张仍然是**空库**(删光演示数据), 保留"一条
数据都没有时界面长什么样"这条。

**画面名带着 `NN-` 编号, 而报告里的列表顺序就是它**: 处理顺序是 ``sorted(produced)``
(即字母序), 而每张结果的开始时间由"基准 + 序号"给出(见 :func:`write_result`) —— 两件事
合起来保证"报告里的先后 = 编号大小"。所以**新加一张画面时编号要接在末尾**, 或者重排
编号时一并改 ``tests/visual-baselines/README.md`` 里的表(两者必须一致)。

## 前提: 画面得先画得出汉字

这一轮用的 Tk 决定了画面可不可信, 所以先跑一个**字体探针**, 并且拿它当门禁:

- 汉字量出来是 0 宽, 或字体落到了 X11 核心位图字体(``fixed`` 这类) → 这一轮的图**不可用**:
  结论标红、**不产出候选基线**, 以退出码 2 结束(与"Tk 起不来"同一类环境问题)。
- 出处: 2026-10-02 的 CI 画面上整排按钮是空的(汉字一个都没画出来), 拉丁字形也是位图
  字体(锯齿), 而这一轮在报告里是**通过** —— 判据只比"这轮与基线像不像", 基线本身是坏的
  时候它看不出来。uv 装的那份 Linux CPython 里的 Tcl/Tk 是 python-build-standalone 自己
  编的(实测 ``python/lib/libtcl9tk9.0.so`` 的符号里只有 ``XLoadQueryFont`` 那套, 没有
  fontconfig/FreeType), 装多少 TTF 都用不上; 发行版的 ``python3-tk`` 才走 Xft/fontconfig。
- 做法(见 ``ci.yml`` 的视觉回归那一步): **解释器不变**(还是 uv 托管的那份, 与其余作业
  同一个版本), 只把 ``_tkinter`` 这个扩展模块换成发行版的 —— 它自己会去加载发行版
  libtk8.6/libfontconfig。探针把这一步的结果写进 ``fonts.txt``(模块与库的路径), 所以
  "到底用的哪一份 Tk"在报告里是可见的, 不用猜。

用法::
    # CI(质量作业, Linux + xvfb)
    uv run python scripts/create_allure_visual.py \
        --results-dir allure-results-visual \
        --baselines tests/visual-baselines \
        --candidates visual-baselines-candidates

    # 本地(装了显示环境就能跑; 缺基线时只产出候选基线)
    uv run python scripts/create_allure_visual.py
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import sys
import time
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from archive_management.domain import HomeLayout
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import GlobalHotkeyService, UnavailableBackend
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.home_page import HomePage
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import AppPage, DiscoveryPage, HomeSection

REPO_ROOT = Path(__file__).resolve().parents[1]
TESTS_DIRECTORY = REPO_ROOT / "tests"
WORK_DIRECTORY = Path("visual-work")
DEFAULT_RESULTS_DIRECTORY = Path("allure-results-visual")
# 基线图入库, 放 tests/ 下: 它是这道门禁的**输入数据**, 跟着代码走改动才在 PR 里看得见。
# 刻意不放 ui-review/: 那个目录被它自己的 .gitignore 挡在仓库外(见模块说明)。
DEFAULT_BASELINES = Path("tests") / "visual-baselines"
DEFAULT_CANDIDATES = Path("visual-baselines-candidates")

# 与产品默认一致: 1360x860 是主窗口打开时的尺寸(最小尺寸是 1200x720)。
WINDOW_SIZE = "1360x860+40+20"
# 每次 pump 的时长(CustomTkinter 的延迟重绘与后台读数要时间落地)与冷启动那一次。
PUMP_SECONDS = 0.4
SETTLE_SECONDS = 0.5
STARTUP_SECONDS = 1.2
# 标题与"快捷键不可用"的原因**都会出现在界面里**(状态栏那一行), 因此会被拍进基线 ——
# 所以它们是具名常量而不是随手写的字符串: 改这两行 = 改画面 = 要重采基线。
WINDOW_TITLE = "存档管理 · 视觉回归"
HOTKEY_UNAVAILABLE_REASON = "视觉回归不注册全局快捷键"

# 感知哈希的规模: 8 → 64 位(与 imagehash 的默认一致, 也是这类比较的通用取值)。
# 想更敏感(抓局部小位移)可以提到 16(256 位), 但那会改变距离的量纲 —— 要连着距离上限
# 一起改, 并且先用一轮实测重新定那个上限(另见文件头"判据"那段)。
HASH_SIZE = 8
# 通过门槛: 两条都满足才算这一张通过。取自 2026-10-05 的 CI 实测分布(见文件头说明)。
MAX_HASH_DISTANCE = 2
MIN_SSIM = 0.995

# 结论项的标签: 与环境/分类那两处配置配套。
# - env=common: 结论与平台无关(它验的是"这个提交画出来的界面"), 归报告的 Common 环境;
# - testCategory=quality: 工程门禁的一类 —— 失败会落进 allurerc.mjs 的"工程门禁:质量检查
#   未通过"分类, 也由 create_allure_summary.py 收进运行总账的质量检查表。
ENVIRONMENT = "common"
CATEGORY_LABEL = "testCategory"
CATEGORY_VALUE = "quality"
SEVERITY = "trivial"
# 刻意不写 allure-pytest 自己的那个"真实用例"标签: 本脚本产出的是结论项, 不是用例
# (质量门第二条规则按它挑"真实用例", tests/unit/test_report_verification.py 有守卫)。
SUITE = "Visual"

# 差异图的放大倍数: 原始差值很小(几个色阶)时人眼在报告里看不出来, 放大后才能看出"变化在哪"。
DIFF_GAIN = 6

# 字体探针用的两串取样文本: 前者看"有没有汉字字形", 后者看"有没有真正的中文字体"。
CJK_SAMPLE = "存档管理视觉回归"
ASCII_SAMPLE = "ArchiveManagement 0123456789"
# X11 的**核心位图字体**族名: 落到它们身上说明这一轮画的不是界面字体(没有抗锯齿, 也没有汉字)。
BITMAP_FAMILIES = frozenset(
    {"fixed", "6x13", "6x12", "9x15", "10x20", "cursor", "nil2", "clean"}
)


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8(Windows 控制台是 cp1252, 打中文会中断步骤).

    ``scripts`` 不是包, 所以这个助手在几个脚本里各留一份(不互相导入)。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def load_grab_helpers() -> ModuleType:
    """加载 ``tests/crash_capture.py`` 里的抓图原语(它不在包里, 所以先把 tests 加进 sys.path).

    只借它的 ``window_box`` / ``grab_png``: 这两个函数里有两处**踩过坑**的平台细节
    (Windows 按窗口句柄抓, 既拍得到被别的窗口盖住的部分, 又避开显示缩放导致的错位;
    Linux 走 ``xdisplay``), 复制一份迟早会分叉。它们已经在 Linux 分片的 GUI 用例里
    每天被验证着。
    """
    module_path = TESTS_DIRECTORY / "crash_capture.py"
    if not module_path.is_file():
        raise FileNotFoundError(f"找不到抓图模块: {module_path}")
    if str(TESTS_DIRECTORY) not in sys.path:
        sys.path.insert(0, str(TESTS_DIRECTORY))
    import crash_capture

    return crash_capture


class Pumpable(Protocol):
    """能被泵事件循环的东西(只要 Tk 那两口子, 不必知道具体是哪个控件).

    ``pump`` 只做"让界面把待办的绘制跑完"这一件事, 所以用结构类型表达"我需要什么",
    而不是把它绑到某个具体窗口类上。
    """

    def update_idletasks(self) -> None:
        """把所有待处理的几何/绘制任务立刻做完."""
        ...

    def update(self) -> None:
        """处理一次挂起的事件(重绘、回调)."""
        ...


@dataclass(frozen=True)
class Comparison:
    """一张画面的比对结果."""

    name: str
    actual: Path
    baseline: Path | None
    hash_distance: int | None
    ssim: float | None
    problem: str

    @property
    def compared(self) -> bool:
        """真有基线、且两条判据都得出了数字, 才算"比较过"."""
        return self.baseline is not None and self.hash_distance is not None

    @property
    def passed(self) -> bool:
        """有任何 problem 就不通过(没基线时 problem 是空的 —— 那种情况只记录候选基线)."""
        return not self.problem


def display_available() -> bool:
    """Linux 上有没有可用的 X 显示(CI 由 xvfb 提供)."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY"))


def noop(*_args: object, **_kwargs: object) -> None:
    """什么都不做的替身(配合 :func:`silence_network` 用)."""


def pump(widget: Pumpable, seconds: float = PUMP_SECONDS) -> None:
    """把事件循环泵一会儿(不进入 mainloop: 脚本要自己控制节奏)."""
    deadline = time.monotonic() + seconds
    while True:
        widget.update_idletasks()
        widget.update()
        if time.monotonic() >= deadline:
            return
        time.sleep(0.02)


def silence_network(backend: object) -> None:
    """关掉启动期的联网动作(补译名 / 补封面 / 补图标).

    它们会真发 HTTP 请求, 也会写缓存目录 —— 视觉回归不该依赖网络, 也不该让"缓存是冷是热"
    变成画面的一部分(那会让同样的代码拍出不同的图)。
    """
    for name in ("prefetch_names", "prefetch_artwork", "prefetch_covers"):
        if hasattr(backend, name):
            setattr(backend, name, noop)


def build_demo_app(root: Path) -> ArchiveApp:
    """用**演示后端**建一个主窗口: 有游戏/备份/候选, 但全部是内存里的固定数据.

    为什么不用"真实 SQLite + 空库": 空库那几张画面里一行数据都没有, 界面真正的布局(列表
    列宽与名称裁剪、状态列、海报网格、详情页表头)压根没被覆盖到 —— 而视觉回归要拦的正是
    那些。演示后端的数据是模块级常量(名字/平台/备份/时间都是固定值), 不联网、不读
    ``dev-data``、不写盘, 每次跑出来完全一样, 因此同样适合跨运行比较。

    ``paths`` 仍然指向工作目录: 应用的日志与配置要落在能整目录删掉的地方。
    """
    paths = ApplicationPaths.default(override_root=root).ensure()
    backend = DemoArchiveService(delay=0)
    silence_network(backend)
    app = ArchiveApp(
        backend,
        title=WINDOW_TITLE,
        hotkeys=GlobalHotkeyService(
            backend=UnavailableBackend(HOTKEY_UNAVAILABLE_REASON)
        ),
        paths=paths,
    )
    app.geometry(WINDOW_SIZE)
    app.deiconify()
    app.lift()
    pump(app, STARTUP_SECONDS)
    return app


def empty_the_library(app: ArchiveApp, page: HomePage) -> None:
    """把演示数据删光并停在"游戏库 + 列表视图"的空状态上.

    演示后端不碰文件系统(见它的 ``delete_game``): 这里删的只是内存里的记录, 于是同一个
    进程、同一个窗口就能拍到"一条数据都没有"的画面 —— 不必再建第二个 Tk 根(一个进程只
    建一个, 见 :func:`build_demo_app`)。
    """
    for summary in list(app.backend.list_games()):
        destination = app.backend.delete_export_path(summary.game_id)
        app.backend.delete_game(summary.game_id, destination)
    page._show_section(HomeSection.LIBRARY)
    show_home_filter(app, layout=HomeLayout.LIST)
    pump(app, SETTLE_SECONDS)


def show_home_filter(app: ArchiveApp, *, layout: HomeLayout) -> None:
    """切换主页的展示形态(走页面自己的 apply, 与用户点一下等价)."""
    page = app._home_page
    page._selected = None
    page._apply(replace(page._filter, layout=layout))
    pump(app)


def shot(
    app: ArchiveApp, screens_dir: Path, name: str, grab: ModuleType
) -> tuple[str | None, str]:
    """拍一张窗口截图并落盘, 返回 ``(文件名 | None, 说明)``; 拿不到画面时文件名为 None."""
    box = grab.window_box(app)
    if box is None:
        return None, "窗口未映射或尺寸为 0, 拿不到画面"
    png, note = grab.grab_png(app, box)
    if png is None:
        return None, note
    (screens_dir / f"{name}.png").write_bytes(png)
    return f"{name}.png", note


def _record(
    app: ArchiveApp,
    screens_dir: Path,
    name: str,
    grab: ModuleType,
    produced: list[str],
    skipped: list[str],
) -> None:
    """拍一张并把结果登记进 produced / skipped(拿不到画面不是"通过", 是要报出来的)."""
    file_name, note = shot(app, screens_dir, name, grab)
    if file_name is None:
        skipped.append(f"{name}: {note}")
        print(f"[跳过] {name}: {note}")
        return
    produced.append(file_name)
    print(f"[截图] {file_name} ({note})")


#: 找可折叠分支时最多试几款游戏(演示数据里第一款可能只有一条链或只有一个备份)。
_FOLD_SEARCH_LIMIT = 6


def record_a_folded_subtree(
    app: ArchiveApp,
    page: HomePage,
    screens_dir: Path,
    grab: ModuleType,
    produced: list[str],
    skipped: list[str],
) -> None:
    """再拍一张**折叠态**: 挨个打开游戏, 拿第一款"图里真有分支可折"的拍.

    演示数据里第一款游戏可能只有一条链、甚至只有一个备份 —— 那时拍出来的"折叠态"与没折
    一样(实测 2026-10-03: 直接拿 ``next(iter(page._rows))`` 拍时结果里少了一张, `skipped`
    里写着"图里没有可折叠的分支")。拍完把折叠还原, 后面的画面都要看展开态。
    """
    name = "04-详情页-分支图折叠态-演示数据"
    for game_id in list(page._rows)[:_FOLD_SEARCH_LIMIT]:
        page._open(game_id)
        pump(app, SETTLE_SECONDS)
        graph = app._tree_view
        foldable = next(
            (node_id for node_id in graph.node_ids if graph.can_toggle(node_id)), None
        )
        if foldable is None:
            continue
        graph.toggle(foldable, app.p)
        pump(app, SETTLE_SECONDS)
        _record(app, screens_dir, name, grab, produced, skipped)
        graph.toggle(foldable, app.p)  # 还原: 后面几张要看展开态
        pump(app)
        return
    skipped.append(f"{name}: 演示数据里没有可折叠的分支")


def capture_screens(
    screens_dir: Path, grab: ModuleType
) -> tuple[list[str], list[str], dict[str, Any]]:
    """建窗口、拍两套画面(有数据的 + 空库), 返回 ``(产出的文件名, 被跳过的说明, 字体探针)``.

    调用方负责先把工作目录清干净(见 :func:`capture_or_error`): 这里只管往里写。
    """
    screens_dir.mkdir(parents=True, exist_ok=True)
    app = build_demo_app(WORK_DIRECTORY / "demo-root")
    produced: list[str] = []
    skipped: list[str] = []
    try:
        page = app._home_page
        show_home_filter(app, layout=HomeLayout.LIST)
        pump(app, SETTLE_SECONDS)
        _record(app, screens_dir, "01-主页-列表视图-演示数据", grab, produced, skipped)
        show_home_filter(app, layout=HomeLayout.POSTER)
        pump(app, SETTLE_SECONDS)
        _record(app, screens_dir, "02-主页-海报视图-演示数据", grab, produced, skipped)

        # 详情页与主页是两套布局(表头/概要卡/备份列表都只在那里), 单独拍一张。
        show_home_filter(app, layout=HomeLayout.LIST)
        page._open(next(iter(page._rows)))
        pump(app, SETTLE_SECONDS)
        _record(app, screens_dir, "03-详情页-演示数据", grab, produced, skipped)
        # 折叠态是新画面(I-9.5): 右下角的标记、说明行里的"N 个后代"与收起来之后的形状
        # 都只能在这里看到 —— 它保住的是"折叠没把别的框挤歪"。编号紧随 03(处理顺序是
        # 字母序, 见 main), 所以报告里的先后与编号一致。
        record_a_folded_subtree(app, page, screens_dir, grab, produced, skipped)
        app._show_page(AppPage.HOME)

        page._show_section(HomeSection.DISCOVERY)
        page._discovery._show_page(DiscoveryPage.CANDIDATES)
        pump(app, SETTLE_SECONDS)
        _record(app, screens_dir, "05-主页-游戏发现-演示数据", grab, produced, skipped)

        page._show_section(HomeSection.ACTIVATION)
        pump(app, SETTLE_SECONDS)
        _record(app, screens_dir, "06-主页-游戏启停-演示数据", grab, produced, skipped)

        # 最后一张: "一条数据都没有"时界面长什么样(空状态文案 + 连表头一起收起来)。
        empty_the_library(app, page)
        _record(app, screens_dir, "07-主页-列表视图-空库", grab, produced, skipped)
        probe = probe_fonts(app)
    finally:
        with contextlib.suppress(Exception):
            app._on_close()
    return produced, skipped, probe


def compare_one(name: str, actual: Path, baseline: Path | None) -> Comparison:
    """把一张实际截图与它的基线比一次(没基线时只登记)."""
    if baseline is None or not baseline.is_file():
        return Comparison(name, actual, None, None, None, "")
    from PIL import Image

    with Image.open(baseline) as base_image, Image.open(actual) as actual_image:
        if base_image.size != actual_image.size:
            problem = (
                f"画面尺寸变了: 基线 {base_image.size[0]}x{base_image.size[1]}, "
                f"本次 {actual_image.size[0]}x{actual_image.size[1]}"
            )
            return Comparison(name, actual, baseline, None, None, problem)
        import imagehash
        import numpy as np
        from skimage.metrics import structural_similarity

        distance = int(
            imagehash.phash(base_image, hash_size=HASH_SIZE)
            - imagehash.phash(actual_image, hash_size=HASH_SIZE)
        )
        grey_base = np.asarray(base_image.convert("L"), dtype="float64")
        grey_actual = np.asarray(actual_image.convert("L"), dtype="float64")
        # scikit-image 带 `py.typed`(所以 mypy 能看见这个函数), 但 `structural_similarity`
        # 自己没写标注 —— strict 下的 `no-untyped-call` 会把"调用它"判成错。按仓库既有做法
        # 给出**指定错误码**的最小忽略: 只放过这一次调用, 而不是把整个 skimage 当成 Any。
        # 上游哪天补上标注, `warn_unused_ignores` 会把这一行报出来, 到时删掉即可。
        similarity = structural_similarity(  # type: ignore[no-untyped-call]
            grey_base, grey_actual, data_range=255.0
        )
        ssim = float(similarity)
    problems = []
    if distance > MAX_HASH_DISTANCE:
        problems.append(f"感知哈希距离 {distance} > {MAX_HASH_DISTANCE}")
    if ssim < MIN_SSIM:
        problems.append(f"SSIM {ssim:.4f} < {MIN_SSIM}")
    return Comparison(name, actual, baseline, distance, ssim, "; ".join(problems))


def write_diff(actual: Path, baseline: Path, target: Path) -> None:
    """把"变了哪里"画成一张放大后的差异图(报告里能直接看)."""
    from PIL import Image, ImageChops

    with Image.open(baseline) as base_image, Image.open(actual) as actual_image:
        diff = ImageChops.difference(
            base_image.convert("RGB"), actual_image.convert("RGB")
        )
    diff.point(lambda value: min(255, value * DIFF_GAIN)).save(target)


def write_attachment(
    results_dir: Path, result_id: str, name: str, body: bytes | str
) -> dict[str, str]:
    """把一个附件写进结果目录, 返回附件条目(图片按 PNG 内联渲染)."""
    suffix = ".png" if isinstance(body, bytes) else ".txt"
    media = "image/png" if isinstance(body, bytes) else "text/plain"
    target = results_dir / f"{result_id}-{name}{suffix}"
    if isinstance(body, bytes):
        target.write_bytes(body)
    else:
        target.write_text(body, encoding="utf-8")
    return {"name": f"{name}{suffix}", "source": target.name, "type": media}


def describe(
    comparison: Comparison,
    skipped: Sequence[str],
    probe: Mapping[str, Any] | None = None,
) -> str:
    """构建展示在 Allure 里的可读摘要(把"判据是什么、差在哪"写清楚)."""
    lines = [
        "## 界面视觉回归",
        "",
        f"- 判据: 感知哈希(64 位)距离 ≤ {MAX_HASH_DISTANCE} 且 SSIM ≥ {MIN_SSIM}",
    ]
    if comparison.baseline is None and comparison.problem:
        lines += [
            "- 结论: **本轮画面不可用, 不作为候选基线** —— " + comparison.problem,
        ]
    elif comparison.baseline is None:
        lines += [
            "- 结论: **基线尚未入库**, 本次只产出候选基线(见附件 `actual.png`), "
            "确认无误后提交到 `tests/visual-baselines/`, 下一次运行才开始真正比较。",
        ]
    else:
        lines += [
            f"- 感知哈希距离: {comparison.hash_distance}",
            f"- SSIM: {comparison.ssim if comparison.ssim is None else f'{comparison.ssim:.4f}'}",
            f"- 结论: {'通过' if comparison.passed else f'未通过 —— {comparison.problem}'}",
        ]
    if skipped:
        lines += ["", "## 本次跳过的画面", "", *[f"- {item}" for item in skipped]]
    if probe is not None:
        lines += ["", format_probe(probe).rstrip("\n")]
    return "\n".join(lines) + "\n"


#: 本次运行的基准时间戳(毫秒)。每张画面的开始时间 = 它 + **序号** —— 见 :func:`write_result`
#: 里那句"为什么要加序号"(2026-10-03 报告里视觉结论的顺序与编号对不上就是它引起的)。
_RUN_STARTED_AT = time.time_ns() // 1_000_000


def write_result(
    results_dir: Path,
    comparison: Comparison,
    *,
    order: int,
    result_id: str,
    platform: str,
    skipped: Sequence[str],
    probe: Mapping[str, Any] | None = None,
) -> None:
    """写入一条视觉回归结果(状态跟随比对结论).

    ``order`` 是这张画面在**本轮**里的序号(从 0 开始), 它决定结果的开始时间。

    为什么要它: 报告里的列表按开始时间排, 而“每张各取一次 ``time.time_ns()``”会让相邻
    两张落在**同一毫秒**里(实测 2026-10-03: 02 与 03、04 与 05 的时间戳完全相同) —— 同值
    在 Allure 里没有稳定的先后, 用户看到的列表于是成了 `01, 03, 02, 05, 04, 06`, 与画面
    编号对不上。改成"同一个基准 + 序号"之后每张的 start 严格递增, 列表顺序就是编号顺序。
    """
    timestamp = _RUN_STARTED_AT + order
    attachments = [
        write_attachment(
            results_dir, result_id, "actual", comparison.actual.read_bytes()
        )
    ]
    if probe is not None:
        attachments.append(
            write_attachment(results_dir, result_id, "fonts", format_probe(probe))
        )
    if comparison.baseline is not None:
        attachments.append(
            write_attachment(
                results_dir, result_id, "baseline", comparison.baseline.read_bytes()
            )
        )
        diff_target = results_dir / f"{result_id}-diff.png"
        with contextlib.suppress(Exception):
            write_diff(comparison.actual, comparison.baseline, diff_target)
        if diff_target.is_file():
            attachments.append(
                {"name": "diff.png", "source": diff_target.name, "type": "image/png"}
            )
    result: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(
            uuid.uuid5(
                uuid.NAMESPACE_URL, f"archive-management-visual-{comparison.name}"
            )
        ),
        "fullName": f"archive-management.visual.{comparison.name}",
        "name": f"视觉回归 · {comparison.name}",
        "status": "passed" if comparison.passed else "failed",
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": SUITE},
            {"name": "epic", "value": "工程与发布"},
            {"name": "feature", "value": "界面视觉回归"},
            {"name": "story", "value": comparison.name},
            {"name": "env", "value": ENVIRONMENT},
            {"name": CATEGORY_LABEL, "value": CATEGORY_VALUE},
            {"name": "severity", "value": SEVERITY},
        ],
        "description": (
            f"{describe(comparison, skipped, probe)}\n- 本次执行于 {platform}。\n"
        ),
        "attachments": attachments,
    }
    if comparison.problem:
        result["statusDetails"] = {"message": comparison.problem}
    (results_dir / f"{result_id}-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def platform_name() -> str:
    """当前平台的显示名(与 tests/conftest.py 写进 env 标签的取值同一套)."""
    system = platform.system()
    return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(
        system, system or "Unknown"
    )


def tk_module_path() -> str:
    """装着 Tk 的那个扩展模块(``_tkinter``)来自哪个文件: 换了它就是换了 Tk 的来源.

    用 ``sys.modules`` 而不是自己 import: 到跑探针时应用已经建了窗口, 谁加载的就是谁 ——
    这里只是把它记下来(实测路径是"发行版模块有没有真的换上去"的唯一证据)。
    """
    module = sys.modules.get("_tkinter")
    return str(getattr(module, "__file__", None) or "<built-in>")


def loaded_tk_libraries() -> list[str]:
    """当前进程真正加载到的 Tcl/Tk 动态库(Linux 读 ``/proc/self/maps``, 其它平台给空表).

    为什么值得记下来: "用哪个 Tk"正是这道门禁的关键输入, 而**版本号不够** —— 同一个
    Tk 8.6 既可能来自发行版(走 Xft/fontconfig, 看得见 TTF 与汉字), 也可能来自解释器自带的
    副本(只认 X11 核心位图字体)。把库路径写进探针, 下一次出问题时不用再靠猜。
    """
    if not sys.platform.startswith("linux"):
        return []
    try:
        maps = Path("/proc/self/maps").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    found = set()
    for line in maps.splitlines():
        matched = re.search(r"(/\S*(?:libtcl|libtk)[\w.+-]*\.so[\w.]*)", line)
        if matched:
            found.add(matched.group(1))
    return sorted(found)


def probe_fonts(app: ArchiveApp) -> dict[str, Any]:
    """问清楚"这一轮到底用什么字体画的", 返回一份可核对的事实.

    它是这道门禁的**前提**, 而不是日志里的装饰: 同一份代码在"能画出汉字"与"画不出汉字"
    的字体环境下会得到两张完全不同的图 —— 而后者看上去像"界面坏了", 实际是环境问题。
    后面三项(模块与库的路径)是"这份环境从那来的": 解释器版本对了不代表 Tk 也对了。
    """
    from archive_management.ui import typography

    sample = typography.font(typography.FONT_HINT)
    families = typography.installed_families()
    return {
        "platform": platform_name(),
        "python": platform.python_version(),
        "tk": str(app.tk.call("info", "patchlevel")),
        "tk_module": tk_module_path(),
        "tk_libraries": loaded_tk_libraries(),
        "requested": typography.current_family() or "(Tk 默认)",
        "actual": str(sample.actual("family")),
        "families": len(families),
        "cjk_candidates": [
            name for name in families if "Noto" in name or "CJK" in name
        ][:6],
        "cjk_width": int(sample.measure(CJK_SAMPLE)),
        "ascii_width": int(sample.measure(ASCII_SAMPLE)),
    }


def font_problem(probe: Mapping[str, Any]) -> str:
    """字体环境能不能画出这张图; 能就返回空串, 不能就返回一句"照着做就能好"的话."""
    fix = (
        "请把发行版的 Tk 换进来(``ci.yml`` 视觉回归那一步用 PYTHONPATH 把发行版 python3-tk "
        f"的 ``_tkinter`` 换上去); 本轮实际用的是: {probe.get('tk_module', '<未知>')}"
        f" + {probe.get('tk_libraries') or '<未知>'}"
    )
    if int(probe["cjk_width"]) <= 0:
        return (
            "汉字量出来是 0 宽(画面里汉字一个都不会画出来): 这一轮用的 Tk 只看得到 X11 "
            f"核心位图字体, 装多少 TTF(如 fonts-noto-cjk)也用不上 —— {fix}。"
        )
    if str(probe["actual"]).casefold() in BITMAP_FAMILIES:
        return f'字体落到了核心位图字体 {probe["actual"]!r}(没有抗锯齿, 画面会"锯齿"): {fix}。'
    return ""


def format_probe(probe: Mapping[str, Any]) -> str:
    """把探针结果排成一段文本(控制台与报告附件共用同一份)."""
    candidates = "、".join(probe["cjk_candidates"]) or "无"
    libraries = ", ".join(probe["tk_libraries"]) or "(非 Linux: 不适用)"
    lines = [
        "## 字体环境(画面能不能信, 先看这里)",
        "",
        f"- 平台 / Python / Tk: {probe['platform']} / {probe['python']} / {probe['tk']}",
        f"- Tk 模块(`_tkinter`): {probe['tk_module']}",
        f"- Tcl/Tk 动态库: {libraries}",
        f"- 选中字体族: {probe['requested']}; 实际解析到: {probe['actual']}",
        f"- 系统报告的字体族数: {probe['families']}(含中日韩关键字的前几个: {candidates})",
        f"- 汉字量宽: {probe['cjk_width']}px; 拉丁量宽: {probe['ascii_width']}px",
        "",
        f"- 判定: {font_problem(probe) or '可用(汉字能量出宽度, 且不是位图字体)'}",
    ]
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """解析命令行参数."""
    parser = argparse.ArgumentParser(
        description="界面视觉回归并把结论写入 Allure 结果目录"
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=DEFAULT_RESULTS_DIRECTORY,
        help=f"Allure 结果目录(默认 {DEFAULT_RESULTS_DIRECTORY})",
    )
    parser.add_argument(
        "--baselines",
        type=Path,
        default=DEFAULT_BASELINES,
        help=f"基线图目录(默认 {DEFAULT_BASELINES}; 缺失时只产出候选基线)",
    )
    parser.add_argument(
        "--candidates",
        type=Path,
        default=DEFAULT_CANDIDATES,
        help=f"候选基线落点(默认 {DEFAULT_CANDIDATES}, 已加进 .gitignore)",
    )
    parser.add_argument(
        "--max-hash-distance",
        type=int,
        default=MAX_HASH_DISTANCE,
        help="感知哈希距离上限",
    )
    parser.add_argument("--min-ssim", type=float, default=MIN_SSIM, help="SSIM 下限")
    return parser.parse_args(argv)


def capture_or_error(
    work_dir: Path,
) -> tuple[list[str], list[str], dict[str, Any]] | None:
    """建窗口并截图; 出问题时把原因打出来并返回 None(调用方据此以退出码 2 结束).

    "起不来"与"画面不对"必须分开: Tk 起不来是**环境**问题(缺显示/缺字体), 记成"通过"
    等于把这道门禁废掉; 而它也不该与"与基线不一致"共用同一个退出码。
    """
    try:
        grab = load_grab_helpers()
    except (FileNotFoundError, ImportError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return None
    if work_dir.exists():
        shutil.rmtree(work_dir)
    try:
        produced, skipped, probe = capture_screens(work_dir, grab)
    except Exception as exc:  # Tk 起不来 / 应用装配失败: 明确报成环境问题
        print(f"::error::界面截图失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if not produced:
        print("::error::一张画面都没抓到: 视觉回归没有输入。", file=sys.stderr)
        return None
    # 被跳过的画面在 :func:`_record` 里已经逐条打过了(就地打更容易对上时间线)。
    return produced, skipped, probe


def process_screen(
    file_name: str,
    *,
    order: int,
    work_dir: Path,
    baselines: Path,
    candidates: Path,
    results_dir: Path,
    host: str,
    skipped: Sequence[str],
    probe: Mapping[str, Any] | None = None,
    blocked: str = "",
) -> Comparison:
    """处理一张画面: 存候选基线 + 写 Allure 结果, 返回比对结论.

    ``order`` 是它在**本轮**里的序号(报告里的列表顺序靠它, 见 :func:`write_result`)。

    ``blocked`` 非空表示"这一轮的画面不可信"(如字体画不出汉字): 仍然写结论(带上探针,
    让人看得到图坏成什么样), 但**不比较、不写候选基线** —— 一张画不出汉字的图被当成
    基线提交进去, 下几轮就开始拿它当"正确"了。
    """
    actual = work_dir / file_name
    name = Path(file_name).stem
    if blocked:
        comparison = Comparison(name, actual, None, None, None, blocked)
    else:
        comparison = compare_one(name, actual, baselines / file_name)
        # 候选基线**总是**写一份: 基线缺失时它就是入库的候选, 基线存在时它记录了本轮的画面
        # (比对失败时尤其要看它)。
        shutil.copyfile(actual, candidates / file_name)
    write_result(
        results_dir,
        comparison,
        order=order,
        result_id=str(uuid.uuid4()),
        platform=host,
        skipped=skipped,
        probe=probe,
    )
    verdict = "通过" if comparison.passed else "未通过"
    if blocked:
        verdict = "画面不可用(见字体探针)"
    elif comparison.baseline is None:
        verdict = "无基线(已产出候选)"
    ssim = comparison.ssim if comparison.ssim is None else round(comparison.ssim, 4)
    print(
        f"[{verdict}] {comparison.name} (哈希 {comparison.hash_distance} / SSIM {ssim})"
    )
    return comparison


def report_outcome(comparisons: Sequence[Comparison], candidates: Path) -> int:
    """打印汇总并按结论给出退出码(有未通过的就退 1)."""
    compared = [item for item in comparisons if item.baseline is not None]
    failures = [f"{item.name}: {item.problem}" for item in comparisons if item.problem]
    print(
        f"\n[完成] 画面 {len(comparisons)} 张, 其中与基线比对 {len(compared)} 张, "
        f"未通过 {len(failures)} 张。候选基线: {candidates}"
    )
    for item in failures:
        print(f"::error::视觉回归未通过 —— {item}", file=sys.stderr)
    return 1 if failures else 0


def main(argv: Sequence[str] | None = None) -> int:
    """跑一次视觉回归: 截图 → 与基线比 → 写结果目录; 任一张不过以退出码 1 结束."""
    ensure_utf8_output()
    args = parse_args(argv)

    if not display_available():
        print(
            "::error::没有可用的显示(DISPLAY 未设置): 视觉回归需要 X 显示, "
            "Linux 上请用 xvfb-run 或让 CI 先安装 xvfb。",
            file=sys.stderr,
        )
        return 2
    captured = capture_or_error(WORK_DIRECTORY)
    if captured is None:
        return 2
    produced, skipped, probe = captured

    # 探针先落到 CI 日志里(报告里还有一份附件): 画面可不可信, 看这一眼就知道。
    print(format_probe(probe))
    blocked = font_problem(probe)

    args.results_dir.mkdir(parents=True, exist_ok=True)
    args.candidates.mkdir(parents=True, exist_ok=True)
    host = platform_name()
    comparisons = [
        process_screen(
            file_name,
            order=order,
            work_dir=WORK_DIRECTORY,
            baselines=args.baselines,
            candidates=args.candidates,
            results_dir=args.results_dir,
            host=host,
            skipped=skipped,
            probe=probe,
            blocked=blocked,
        )
        for order, file_name in enumerate(sorted(produced))
    ]
    if blocked:
        print(f"::error::视觉回归不可信 —— {blocked}", file=sys.stderr)
        print(
            "::error::这一轮不产出候选基线: 画不出汉字的图不该被当成基线。",
            file=sys.stderr,
        )
        return 2
    return report_outcome(comparisons, args.candidates)


if __name__ == "__main__":
    raise SystemExit(main())
