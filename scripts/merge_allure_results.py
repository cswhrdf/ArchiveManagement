"""把多个 Allure 结果目录合并成一份.

CI 把同一平台的套件分片跑在多个作业上, 每个作业各自产出 ``allure-results-<平台>-<片>``。
报告作业要把它们合成一份目录再生成报告 —— 不然报告只反映最后一片。

Allure 的每条结果就是一个以 uuid 命名的 ``*-result.json``, 附件同样是 uuid 命名, 所以
"合并"就是把文件搬进同一个目录: 分片之间不会重名。缺某个分片目录(例如该片在跑之前就挂了,
没来得及上传产物)不报错, 只跳过并提示 —— 报告仍由剩余分片生成, 失败本身已经体现在作业状态里。

参数按**通配模式**展开: CI 在 pwsh 里不会自己展开 ``allure-results-windows-*``(Linux/macOS 的
bash 会), 所以统一交给脚本展开; 日志里逐片给出文件数, 少一片能一眼看出来。

用法(CI 报告作业, 也可本地复核分片结果):

    uv run python scripts/merge_allure_results.py --output allure-results \
        "allure-results-linux-*"
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

RESULTS_DIRECTORY = Path("allure-results")


def ensure_utf8_output() -> None:
    """把标准输出/错误切成 UTF-8(Windows 控制台默认 cp1252, 打印中文会崩).

    ``create_allure_summary.py`` / ``verify_allure_report.py`` 里有同样的一份:
    三个脚本都是独立入口, 不互相导入(scripts 不是包)。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


@dataclass(frozen=True)
class MergeSummary:
    """合并结果: 逐片文件数 + 合计, 以及重名覆盖次数."""

    per_source: tuple[tuple[Path, int], ...]
    overwritten: int

    @property
    def copied(self) -> int:
        """搬进来的文件总数."""
        return sum(count for _source, count in self.per_source)


def expand_sources(patterns: Sequence[str]) -> tuple[list[Path], list[str]]:
    """展开通配模式, 返回 (匹配到的目录(去重、有序), 没匹配到的模式).

    字面目录名就是一个只匹配自己的模式, 因此调用方不需要区分两种写法。模式匹配到文件而
    不是目录(例如 ``allure-results-*`` 撞上同名文件)按"没匹配到"处理, 免得后面静默丢数据。
    """
    found: dict[Path, None] = {}
    missing: list[str] = []
    for pattern in patterns:
        matches = _expand(pattern)
        if matches:
            found.update(dict.fromkeys(matches))
        else:
            missing.append(pattern)
    return list(found), missing


def _expand(pattern: str) -> list[Path]:
    """展开单个模式, 只保留目录.

    绝对模式要拆成"锚点 + 相对模式"才能交给 :meth:`Path.glob`(它不接受绝对模式);
    相对模式则按当前工作目录展开 —— CI 与本地都是这个语义。
    """
    path = Path(pattern)
    anchor = path.anchor
    if anchor:
        candidates = Path(anchor).glob(str(path.relative_to(anchor)))
    else:
        candidates = Path().glob(pattern)
    return sorted(candidate for candidate in candidates if candidate.is_dir())


def merge_directories(sources: Sequence[Path], *, output: Path) -> MergeSummary:
    """把各分片目录里的文件复制进 ``output``, 返回逐片统计.

    重名理论上不会发生(uuid 命名), 但真出现了说明结果目录被复用或分片编号撞了,
    所以要计数并提示, 而不是悄悄盖掉一条结果。
    """
    output.mkdir(parents=True, exist_ok=True)
    per_source: list[tuple[Path, int]] = []
    overwritten = 0
    for source in sources:
        count = 0
        for path in sorted(source.iterdir()):
            if not path.is_file():
                continue
            if (output / path.name).exists():
                overwritten += 1
            shutil.copy2(path, output / path.name)
            count += 1
        per_source.append((source, count))
    return MergeSummary(per_source=tuple(per_source), overwritten=overwritten)


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    """解析参数: ``--output`` 为目标目录, 位置参数为各分片结果目录(支持通配)."""
    parser = argparse.ArgumentParser(description="合并多个 Allure 结果目录")
    parser.add_argument(
        "--output",
        type=Path,
        default=RESULTS_DIRECTORY,
        help=f"合并后的结果目录(默认 {RESULTS_DIRECTORY})",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        help="各分片的结果目录或通配模式; 没匹配到的模式会被跳过并提示",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """合并结果目录; 一个文件都没搬进来时返回 1(说明没有可汇报的结果)."""
    args = _parse(sys.argv[1:] if argv is None else argv)
    sources, missing = expand_sources(args.sources)
    summary = merge_directories(sources, output=args.output)
    for source, count in summary.per_source:
        print(f"  {source}: {count} 个文件")
    print(f"已合并 {len(sources)} 个分片目录, {summary.copied} 个文件 -> {args.output}")
    if missing:
        print(
            "以下模式没匹配到目录, 已跳过(分片目录缺失/命名变化都会走到这里): "
            + ", ".join(missing),
            file=sys.stderr,
        )
    if summary.overwritten:
        print(
            f"警告: {summary.overwritten} 个文件重名并被覆盖, 分片结果可能被复用",
            file=sys.stderr,
        )
    return 0 if summary.copied else 1


if __name__ == "__main__":
    ensure_utf8_output()
    raise SystemExit(main())


if __name__ == "__main__":
    ensure_utf8_output()
    raise SystemExit(main())
