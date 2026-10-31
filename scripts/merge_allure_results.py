r"""把多个 Allure 结果目录合并成一份.

CI 把同一平台的套件分片跑在多个作业上, 每个作业各自产出 ``allure-results-<平台>-<片>``。
报告作业要把它们合成一份目录再生成报告 —— 不然报告只反映最后一片。

Allure 的每条结果就是一个以 uuid 命名的 ``*-result.json``, 附件同样是 uuid 命名, 所以
"合并"就是把文件搬进同一个目录: 分片之间不会重名。缺某个分片目录(例如该片在跑之前就挂了,
没来得及上传产物)不报错, 只跳过并提示 —— 报告仍由剩余分片生成, 失败本身已经体现在作业状态里。

参数按**通配模式**展开: CI 在 pwsh 里不会自己展开 ``allure-results-windows-*``(Linux/macOS 的
bash 会), 所以统一交给脚本展开; 日志里逐片给出文件数, 少一片能一眼看出来。

**产物清单(``--manifest``)**: 上面那句"不报错"留下了一个盲区 —— 少一片时合并与后续步骤
都正常, 报告只是静默地少了一部分用例(环境、通过率、上游自检都看不出来)。所以合并时可以
写一份 JSON: 逐分片的文件数与**结果**条数、合并合计、以及 ``--expect-shards`` 声明必须有
而实际没找到的片号。这份清单由报告作业上传, 最终由 ``verify_allure_report.py --manifest``
与"分片自报的条数"对齐(见那里的说明)。

用法(CI 报告作业, 也可本地复核分片结果):

    uv run python scripts/merge_allure_results.py --output allure-results \\
        --platform Windows --expect-shards 0,1,2 --manifest allure-manifest.json \\
        "allure-results-windows-*"
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

RESULTS_DIRECTORY = Path("allure-results")
# 分片目录名以片号结尾(``allure-results-ubuntu-latest-0``): 清单靠它把目录对回矩阵里的片号。
SHARD_SUFFIX = re.compile(r"-(\d+)$")
# 每条结果就是一个 ``*-result.json``(附件、容器文件不算结果).
RESULT_FILE_SUFFIX = "-result.json"


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
class SourceCount:
    """一个分片目录搬进来的文件数, 以及其中算作"一条结果"的 ``*-result.json`` 数."""

    name: str
    files: int
    results: int
    shard: str | None = None


@dataclass(frozen=True)
class MergeSummary:
    """合并结果: 逐片文件数 + 合计, 以及重名覆盖次数."""

    per_source: tuple[SourceCount, ...]
    overwritten: int

    @property
    def copied(self) -> int:
        """搬进来的文件总数."""
        return sum(item.files for item in self.per_source)

    @property
    def results(self) -> int:
        """搬进来的结果条数(清单与报告就按这个数对齐)."""
        return sum(item.results for item in self.per_source)


def shard_index(name: str) -> str | None:
    """从目录名末尾取片号(``allure-results-ubuntu-latest-0`` → ``"0"``); 取不到返回 None."""
    matched = SHARD_SUFFIX.search(name)
    return matched.group(1) if matched else None


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
    per_source: list[SourceCount] = []
    overwritten = 0
    for source in sources:
        files = 0
        results = 0
        for path in sorted(source.iterdir()):
            if not path.is_file():
                continue
            if (output / path.name).exists():
                overwritten += 1
            shutil.copy2(path, output / path.name)
            files += 1
            if path.name.endswith(RESULT_FILE_SUFFIX):
                results += 1
        per_source.append(
            SourceCount(
                name=source.name,
                files=files,
                results=results,
                shard=shard_index(source.name),
            )
        )
    return MergeSummary(per_source=tuple(per_source), overwritten=overwritten)


def missing_shards(summary: MergeSummary, expected: Sequence[str]) -> list[str]:
    """返回声明必须有、实际却没找到的片号.

    这是分片丢数据的唯一可靠判据: 只看文件数或环境是看不出来的 —— 少一片的运行与齐全的
    运行在报告里长得一样(环境还在、通过率还高), 只是少了一部分用例。
    """
    found = {item.shard for item in summary.per_source if item.shard is not None}
    return [shard for shard in expected if shard not in found]


def write_manifest(
    path: Path,
    *,
    platform: str,
    summary: MergeSummary,
    expected: Sequence[str],
    missing: Sequence[str],
) -> None:
    """写产物清单: 谁在哪个平台合并了哪些分片、各多少条结果、缺了哪几片."""
    payload = {
        "platform": platform,
        "expected_shards": list(expected),
        "missing_shards": list(missing),
        "sources": [
            {
                "name": item.name,
                "shard": item.shard,
                "files": item.files,
                "results": item.results,
            }
            for item in summary.per_source
        ],
        "total_files": summary.copied,
        "total_results": summary.results,
        "overwritten": summary.overwritten,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


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
        "--platform",
        default="",
        help="本平台展示名(写进产物清单, CI 传 runner.os)",
    )
    parser.add_argument(
        "--expect-shards",
        default="",
        help=(
            "声明必须有哪几个分片(逗号分隔, 如 0,1,2); 缺哪片会写进产物清单并与矩阵列表一致"
        ),
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="产物清单的输出路径(JSON); 不传就不写",
    )
    parser.add_argument(
        "sources",
        nargs="+",
        help="各分片的结果目录或通配模式; 没匹配到的模式会被跳过并提示",
    )
    return parser.parse_args(list(argv))


def expected_shard_list(value: str) -> list[str]:
    """把 ``--expect-shards`` 拆成片号列表(空值表示不声明)."""
    return [item.strip() for item in value.split(",") if item.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    """合并结果目录; 一个文件都没搬进来时返回 1(说明没有可汇报的结果)."""
    args = _parse(sys.argv[1:] if argv is None else argv)
    sources, missing = expand_sources(args.sources)
    summary = merge_directories(sources, output=args.output)
    for item in summary.per_source:
        shard = f"片 {item.shard}" if item.shard else "片号无法从目录名识别"
        print(f"  {item.name}: {item.files} 个文件({item.results} 条结果, {shard})")
    print(
        f"已合并 {len(summary.per_source)} 个分片目录, {summary.copied} 个文件"
        f"({summary.results} 条结果) -> {args.output}"
    )
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

    expected = expected_shard_list(args.expect_shards)
    absent = missing_shards(summary, expected)
    if args.manifest is not None:
        write_manifest(
            args.manifest,
            platform=args.platform,
            summary=summary,
            expected=expected,
            missing=absent,
        )
        print(f"产物清单已写入 {args.manifest}")
    if absent:
        print(
            f"警告: 声明必须有的分片里缺 {', '.join(absent)} —— 该平台的用例会静默少一部分"
            "(产物清单已记录, 校验脚本会报出来)",
            file=sys.stderr,
        )
    return 0 if summary.copied else 1


if __name__ == "__main__":
    ensure_utf8_output()
    raise SystemExit(main())
