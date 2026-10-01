"""界面视觉回归(感知哈希 + SSIM), 并把结论写进 Allure.

为什么需要它: 布局/配色/文字裁剪这类问题**不会让任何用例变红** —— 界面照样能跑, 单元与
集成用例也照样通过, 只有人去看才发现"这行字被裁了""这个面板歪了"。这个脚本把"界面的长相"
变成一条可比较的结论: 起一个真实窗口、截图、与前一轮的基线图比, 结果与图一起进报告。

## 判据(两条, 分工不同)

- **感知哈希**(``imagehash.phash``, 默认 8x8 → 64 位)给汉明距离: 对"整体长相变了没有"
  敏感, 而且与逐像素比对不同 —— 它容忍抗锯齿/亚像素渲染这类噪声;
- **结构相似度**(``skimage.metrics.structural_similarity``)给 0~1 的相似度: 抓细粒度退化
  (某处文字重排、颜色偏移)。两者都通过才算这一张通过。

两条阈值都在下面的常量里, 也能用命令行覆盖。**它们是从"两次相同渲染应当完全相同"
(距离 0 / SSIM 1.0)留出余量取的初始值** —— 等 CI 上真跑出几轮同渲染的噪声分布之后再收紧,
不要凭印象改: 太松等于没判据, 太紧会让 runner 镜像更新(字体/显卡驱动变化)变成随机红。

## 基线从哪来(为什么"缺基线"不算失败)

基线图**入库**(``ui-review/baselines/``), 因为视觉回归的价值就在于"界面变了要有人看一眼
再合" —— 基线跟着代码走, 才在 PR 里看得见"这版把基线图也改了"。基线缺失时(第一次上线、
或新增了一张画面)本脚本**不比较**, 只把这次抓到的图写成候选基线(``--candidates``),
并在描述里写明"基线尚未入库", 由人工确认后提交。理由: 让"没有基线"直接红, 会把第一轮
永远卡住; 让它静默绿, 又会把"没判据"混进"通过"。所以取中间: 状态通过、描述把话说清楚、
候选图作为附件上传。

## 复用而不是另写一套

截图与建窗口那套复用 ``ui-review/capture.py``(它本来就把 :mod:`crash_capture` 的
``window_box`` / ``grab_png`` 包好了)。代价是**改评审脚本会改画面**: 那是这套机制应有的
反应 —— 先看报告里那张图, 确认是有意改动, 再重采基线。

## 只拍空库

空库那一组(主页海报/列表两个视图 + 发现页 + 启停页)不需要任何种子数据、不联网、不读
``dev-data``, 每次跑出来的东西完全一样。有数据的那些画面依赖 dev-data 与后台补名/补封面,
不适合当跨运行比较的输入。

用法::

    # CI(质量作业, Linux + xvfb)
    uv run python scripts/create_allure_visual.py \
        --results-dir allure-results-visual \
        --baselines ui-review/baselines \
        --candidates visual-baselines-candidates

    # 本地(装了显示环境就能跑; 缺基线时只产出候选基线)
    uv run python scripts/create_allure_visual.py
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import platform
import shutil
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
CAPTURE_SCRIPT = REPO_ROOT / "ui-review" / "capture.py"
DEFAULT_RESULTS_DIRECTORY = Path("allure-results-visual")
DEFAULT_BASELINES = Path("ui-review") / "baselines"
DEFAULT_CANDIDATES = Path("visual-baselines-candidates")

# 感知哈希的规模: 8 → 64 位(与 imagehash 的默认一致, 也是这类比较的通用取值)。
HASH_SIZE = 8
# 通过门槛: 两条都满足才算这一张通过。
MAX_HASH_DISTANCE = 6
MIN_SSIM = 0.98

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


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8(Windows 控制台是 cp1252, 打中文会中断步骤).

    ``scripts`` 不是包, 所以这个助手在几个脚本里各留一份(不互相导入)。
    """
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError, OSError):
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]


def load_capture_module() -> ModuleType:
    """按路径加载 ``ui-review/capture.py``(它不是包, 目录名还带连字符)."""
    if not CAPTURE_SCRIPT.is_file():
        raise FileNotFoundError(f"找不到复用脚本: {CAPTURE_SCRIPT}")
    spec = importlib.util.spec_from_file_location("ui_review_capture", CAPTURE_SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover - 路径存在时的防御
        raise ImportError(f"无法加载 {CAPTURE_SCRIPT}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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
        """没有基线时不算失败(只记录候选基线); 有基线时两条判据都要过."""
        if self.baseline is None:
            return True
        return not self.problem


def display_available() -> bool:
    """Linux 上有没有可用的 X 显示(CI 由 xvfb 提供)."""
    if not sys.platform.startswith("linux"):
        return True
    return bool(os.environ.get("DISPLAY"))


def capture_screens(
    capture: ModuleType, screens_dir: Path
) -> tuple[Any, list[str], list[str]]:
    """建窗口、拍空库那一组画面, 返回 ``(app, 产出的文件名, 被跳过的说明)``."""
    screens_dir.mkdir(parents=True, exist_ok=True)
    root = capture.prepare_root("empty")
    app = capture.build_app(root)
    recorder = capture.Recorder(app, screens=screens_dir)
    recorder.install_wait_hook()
    try:
        capture.capture_empty(recorder)
    finally:
        with contextlib.suppress(Exception):
            app._on_close()
    skipped = [f"{name}: {reason}" for name, reason in recorder.skipped]
    return app, list(recorder.produced), skipped


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


def describe(comparison: Comparison, skipped: Sequence[str]) -> str:
    """构建展示在 Allure 里的可读摘要(把"判据是什么、差在哪"写清楚)."""
    lines = [
        "## 界面视觉回归",
        "",
        f"- 判据: 感知哈希(64 位)距离 ≤ {MAX_HASH_DISTANCE} 且 SSIM ≥ {MIN_SSIM}",
    ]
    if comparison.baseline is None:
        lines += [
            "- 结论: **基线尚未入库**, 本次只产出候选基线(见附件 `actual.png`), "
            "确认无误后提交到 `ui-review/baselines/`, 下一次运行才开始真正比较。",
        ]
    else:
        lines += [
            f"- 感知哈希距离: {comparison.hash_distance}",
            f"- SSIM: {comparison.ssim if comparison.ssim is None else f'{comparison.ssim:.4f}'}",
            f"- 结论: {'通过' if comparison.passed else f'未通过 —— {comparison.problem}'}",
        ]
    if skipped:
        lines += ["", "## 本次跳过的画面", "", *[f"- {item}" for item in skipped]]
    return "\n".join(lines) + "\n"


def write_result(
    results_dir: Path,
    comparison: Comparison,
    *,
    result_id: str,
    platform: str,
    skipped: Sequence[str],
) -> None:
    """写入一条视觉回归结果(状态跟随比对结论)."""
    timestamp = time.time_ns() // 1_000_000
    attachments = [
        write_attachment(
            results_dir, result_id, "actual", comparison.actual.read_bytes()
        )
    ]
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
            f"{describe(comparison, skipped)}\n- 本次执行于 {platform}。\n"
        ),
        "attachments": attachments,
    }
    if not comparison.passed:
        result["statusDetails"] = {
            "message": f"{comparison.name} 与基线不一致: {comparison.problem}"
        }
    (results_dir / f"{result_id}-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def platform_name() -> str:
    """当前平台的显示名(与 tests/conftest.py 写进 env 标签的取值同一套)."""
    system = platform.system()
    return {"Windows": "Windows", "Darwin": "macOS", "Linux": "Linux"}.get(
        system, system or "Unknown"
    )


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


def capture_or_error(work_dir: Path) -> tuple[list[str], list[str]] | None:
    """建窗口并截图; 出问题时把原因打出来并返回 None(调用方据此以退出码 2 结束).

    "起不来"与"画面不对"必须分开: Tk 起不来是**环境**问题(缺显示/缺字体), 记成"通过"
    等于把这道门禁废掉; 而它也不该与"与基线不一致"共用同一个退出码。
    """
    try:
        capture = load_capture_module()
    except (FileNotFoundError, ImportError) as exc:
        print(f"::error::{exc}", file=sys.stderr)
        return None
    if work_dir.exists():
        shutil.rmtree(work_dir)
    try:
        _app, produced, skipped = capture_screens(capture, work_dir)
    except Exception as exc:  # Tk 起不来 / 应用装配失败: 明确报成环境问题
        print(f"::error::界面截图失败: {type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if not produced:
        print("::error::一张画面都没抓到: 视觉回归没有输入。", file=sys.stderr)
        return None
    for note in skipped:
        print(f"[跳过] {note}")
    return list(produced), skipped


def process_screen(
    file_name: str,
    *,
    work_dir: Path,
    baselines: Path,
    candidates: Path,
    results_dir: Path,
    host: str,
    skipped: Sequence[str],
) -> Comparison:
    """处理一张画面: 存候选基线 + 写 Allure 结果, 返回比对结论."""
    actual = work_dir / file_name
    comparison = compare_one(Path(file_name).stem, actual, baselines / file_name)
    # 候选基线**总是**写一份: 基线缺失时它就是入库的候选, 基线存在时它记录了本轮的画面
    # (比对失败时尤其要看它)。
    shutil.copyfile(actual, candidates / file_name)
    write_result(
        results_dir,
        comparison,
        result_id=str(uuid.uuid4()),
        platform=host,
        skipped=skipped,
    )
    verdict = "通过" if comparison.passed else "未通过"
    if comparison.baseline is None:
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
    captured = capture_or_error(Path("visual-work"))
    if captured is None:
        return 2
    produced, skipped = captured

    args.results_dir.mkdir(parents=True, exist_ok=True)
    args.candidates.mkdir(parents=True, exist_ok=True)
    host = platform_name()
    comparisons = [
        process_screen(
            file_name,
            work_dir=Path("visual-work"),
            baselines=args.baselines,
            candidates=args.candidates,
            results_dir=args.results_dir,
            host=host,
            skipped=skipped,
        )
        for file_name in sorted(produced)
    ]
    return report_outcome(comparisons, args.candidates)


if __name__ == "__main__":
    raise SystemExit(main())
