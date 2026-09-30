"""把各分片的覆盖率数据摊平到当前目录, 并点名缺了哪一片.

为什么不用一行 ``cp coverage-data-*/.coverage.shard-* ./``:

* 少一片时 bash 只报一句 ``cp: cannot stat '...': No such file or directory``, 从日志里
  看不出缺的是哪一片, 更看不出"整轮其实不该判覆盖率";
* 下载布局会变: 只匹配到一个产物时 ``actions/download-artifact`` 直接把它解到 ``path``
  指定的目录里(不会再有 ``coverage-data-<os>-<片>`` 这层目录), 于是那条通配符匹配不到
  任何东西 —— **一个分片的数据就这么静默地没进合并**, 而后面 ``coverage xml`` 会自己
  合并剩下那一份并照着 ``fail_under`` 报"覆盖率不达标"(2026-09-30 实测: 报告里写着
  Windows 89.99%, 真相是少了一片的数据)。

所以这里按**片号**收集: 两个布局都认, 收完与 ``--expect-shards`` 逐片对数, 缺片就明确
报出来并以非 0 退出(调用方据此跳过"合并 / 判门槛 / 出报告")。

只依赖标准库, 与其它报告脚本一致(报告类作业只装 coverage 那一组依赖)。
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

# 每片的覆盖率数据文件名(与 ci.yml 里的 COVERAGE_FILE 一致).
SHARD_PREFIX = ".coverage.shard-"
# 默认的下载布局: 每个分片的产物落进以产物名命名的子目录.
DEFAULT_PATTERNS = ("coverage-data-*",)


def ensure_utf8_output() -> None:
    """把 stdout/stderr 固定成 UTF-8.

    Windows runner 的控制台是 cp1252, 打中文会直接 ``UnicodeEncodeError`` 把步骤打断
    (报告自检踩过); 这里与 ``merge_allure_results.py`` / ``verify_allure_report.py``
    用同一套写法, 顺带跳过 pytest 的 capsys 这类没有 ``reconfigure`` 的替身。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            with contextlib.suppress(OSError, ValueError):
                reconfigure(encoding="utf-8", errors="replace")


def shard_index(path: Path) -> str | None:
    """从文件名里取片号(``.coverage.shard-0`` -> ``"0"``); 取不到返回 None.

    Args:
        path: 覆盖率数据文件.

    Returns:
        片号字符串, 或 None(文件名不合规).
    """
    name = path.name
    if not name.startswith(SHARD_PREFIX):
        return None
    shard = name[len(SHARD_PREFIX) :]
    return shard or None


def _expand(pattern: str) -> list[Path]:
    """展开一个模式里的 ``.coverage.shard-*`` 文件(目录与文件布局都认).

    绝对模式要拆成"锚点 + 相对模式"才能交给 :meth:`Path.glob`(它不接受绝对模式);
    相对模式按当前工作目录展开 —— CI 与本地都是这个语义。

    Args:
        pattern: 目录名或通配模式(如 ``coverage-data-*``).

    Returns:
        匹配到的数据文件(已排序).
    """
    path = Path(pattern)
    anchor = path.anchor
    if anchor:
        candidates = Path(anchor).glob(str(path.relative_to(anchor)))
    else:
        candidates = Path().glob(pattern)
    found: list[Path] = []
    for candidate in sorted(candidates):
        if candidate.is_file() and shard_index(candidate) is not None:
            found.append(candidate)
        elif candidate.is_dir():
            found.extend(sorted(candidate.glob(f"{SHARD_PREFIX}*")))
    return [item for item in found if item.is_file()]


def find_shards(patterns: Sequence[str]) -> dict[str, Path]:
    """收集所有分片的覆盖率数据文件, 返回 ``{片号: 路径}``.

    Args:
        patterns: 要搜索的目录/模式(会连同当前工作目录一起看).

    Returns:
        片号到数据文件的映射(同片号重复出现时保留第一个, 并会在调用方提示).
    """
    found: dict[str, Path] = {}
    candidates: list[Path] = []
    for pattern in (*DEFAULT_PATTERNS, *patterns):
        candidates.extend(_expand(pattern))
    # 已经摊在工作目录里的那些也要算上: 只匹配到一个产物时下载动作会把它**直接**解到
    # path 指定的目录里(没有 coverage-data-* 这一层), 那时这里就是唯一能看见它的地方.
    candidates.extend(sorted(Path().glob(f"{SHARD_PREFIX}*")))
    for path in candidates:
        shard = shard_index(path)
        if shard is None or not path.is_file():  # pragma: no cover - 两类来源都已过滤
            continue
        found.setdefault(shard, path)
    return found


def expected_shard_list(value: str) -> list[str]:
    """把 ``--expect-shards`` 拆成片号列表(空值表示不声明).

    Args:
        value: 逗号分隔的片号, 如 ``0,1,2``.

    Returns:
        片号列表(去掉空项).
    """
    return [item.strip() for item in value.split(",") if item.strip()]


def collect(shards: dict[str, Path], *, destination: Path) -> list[str]:
    """把各片数据复制到 ``destination``(coverage combine 只扫工作目录).

    Args:
        shards: 片号到数据文件的映射.
        destination: 目标目录.

    Returns:
        复制后的文件名(已排序).
    """
    copied: list[str] = []
    for shard in sorted(shards, key=int):
        target = destination / f"{SHARD_PREFIX}{shard}"
        source = shards[shard]
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        copied.append(target.name)
    return copied


def _parse(argv: Sequence[str]) -> argparse.Namespace:
    """解析参数: ``--expect-shards`` 声明该有哪些片, 其余是额外的搜索模式.

    Args:
        argv: 命令行参数(不含程序名).

    Returns:
        解析结果.
    """
    parser = argparse.ArgumentParser(description="摊平各分片的覆盖率数据并按片对数")
    parser.add_argument(
        "--expect-shards",
        default="",
        help="声明必须有哪几个分片(逗号分隔, 如 0,1); 缺哪片会被点名并让本步骤失败",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(),
        help="把数据摊平到哪里(coverage combine 只扫工作目录, 默认当前目录)",
    )
    parser.add_argument(
        "patterns",
        nargs="*",
        help="额外的目录/通配模式(默认 coverage-data-* 与当前目录都会看)",
    )
    return parser.parse_args(list(argv))


def main(argv: Sequence[str] | None = None) -> int:
    """摊平覆盖率数据; 缺片(或一片都没有)时返回 1.

    Args:
        argv: 命令行参数(不含程序名); 不传则用 ``sys.argv``.

    Returns:
        进程退出码: 0 表示该到的片都到了, 1 表示缺片.
    """
    ensure_utf8_output()
    args = _parse(sys.argv[1:] if argv is None else argv)
    shards = find_shards(args.patterns)
    for shard in sorted(shards, key=int):
        print(f"  片 {shard}: {shards[shard]}")

    expected = expected_shard_list(args.expect_shards)
    missing = [shard for shard in expected if shard not in shards]
    if not shards:
        print(
            f"没有找到任何分片覆盖率数据({SHARD_PREFIX}*): 分片作业可能都没上传产物",
            file=sys.stderr,
        )
        return 1
    if missing:
        print(
            f"覆盖率数据缺片: 声明必须有 {','.join(expected)}, 实际只有 "
            f"{','.join(sorted(shards, key=int))}; 缺 {','.join(missing)}"
            " —— 残缺数据合出来的百分比不能用来判门槛, 请回分片作业看那片为什么没上传",
            file=sys.stderr,
        )
        return 1

    copied = collect(shards, destination=args.destination)
    print(
        f"已摊平 {len(copied)} 片覆盖率数据到 {args.destination}: {', '.join(copied)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
