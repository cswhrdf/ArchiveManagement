"""把 ``.allure/history.jsonl`` 修成"当前这份 Allure 认得出"的样子.

为什么需要这一步: 报告的趋势按 ``retryHash`` **精确匹配**(见 allurerc.mjs 的
``historyPath`` 一段), 而 ``retryHash`` 的形状会跟着 Allure 的版本/配置变 —— 采用
environments 那次把它从 ``<testCaseHash>.<parametersHash>`` 变成三段, 于是**所有旧快照
一条也接不上**: 报告里每条用例只剩"本次运行"一个点(2026-10-05 实测 7231 条结果里 7226 条
历史长度为 1, 而那一条还是本次运行自己重复追加的快照)。丢失的是**匹配**, 不是数据 ——
快照一直躺在历史文件里。

三件事, 顺序固定:

1. **去重**: 同一轮运行可能被追加两份快照(2026-10-05 定位: ``allure quality-gate``
   子命令同样遵循 allurerc 的 ``appendHistory``, 与随后的 ``allure generate`` 各追加
   一次, 实测两份相隔 2 秒到 26 秒; CI 已在质量门那一步把历史文件藏起来, 这里的
   去重是本地连跑两次 generate 等场景的兜底), 键集合相同或互相包含且时间相近的只留
   信息更全的那一份 —— 否则"本次结果"会冒充"上一次历史", 让人以为历史还在;
2. **重键**: 旧快照的键与当前形状不同(多出的维度是**追加在末尾**的)时按"唯一同名头部"
   补全(``a.b`` -> ``a.b.c``); 头部对上多个当前键时(采用 environments 之后, 同一用例在
   三个平台各有一个键)再按条目自己的 ``environment`` 对准平台 —— 旧快照写显示名
   (``macOS``)、新快照写 id(``macos``), 比较时统一小写。没有这一步时全部旧条目会被当成
   "补不出唯一值"丢掉, 连接数归零, 兜底反而清空整份历史。补不出唯一值的条目丢掉;
   已是当前段数的键原样保留, 段数比当前还多的键同样丢弃 —— 把长键砍成前缀会悄悄并掉
   平台之间的区别, 留着也永远匹配不上, 只会让历史文件里堆积永远用不到的数据;
3. **兜底**: 重键之后**没有任何一份旧快照**能与最新快照相连 ⇒ 清空历史重新开始。这一步
   一定写进修复记录(``allure-history-repair.md``), 不许静默发生 —— 静默正是这次丢历史的
   代价来源。

用法(CI 在把历史拷回 ``.allure/`` 之后、``allure generate`` 之前跑; 用动态的
``allure@3`` 时把实际版本一起记进修复记录, 下次形状再变能一眼对上):

``uv run python scripts/repair_allure_history.py --allure-version "$(allure --version)"``

本地想看会做什么而不改文件: 加 ``--dry-run``。
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# 历史文件与修复记录的默认位置(与 allurerc.mjs 的 ``historyPath``、根目录附件的约定一致).
HISTORY_PATH = Path(".allure/history.jsonl")
REPAIR_REPORT = Path("allure-history-repair.md")
# 两份快照相隔这么久以内时, 才可能是**同一轮运行**的两份(实测: 重复生成相隔 5 秒到 26 秒;
# 而不同轮运行之间是小时级).
DUPLICATE_WINDOW_SECONDS = 300.0
# 再加上"键集合重合度": 同一轮的两次生成之间可能又跑完/跳过了几条用例(实测两份相差 5 条),
# 所以不能用"严格互相包含"判 -- 但真的不同轮(例如本地连跑两次)重合度也没这么高。
SAME_RUN_OVERLAP = 0.9
RESULTS_FIELD = "testResults"
TIMESTAMP_FIELD = "timestamp"
# 最新快照比现在旧这么多天时, 修复记录要写出来: 基线被冻结(期间没有任何一轮运行被记进
# 历史)时键形状对不上, 趋势会整体消失 —— 这正是 2026-10-05 那次"每条用例只剩自己的点"
# 的另一半病因(基线只认成功运行, 连续失败时期链条不动)。
STALE_BASELINE_DAYS = 3.0
# 修复记录里写的文件名(与 allurerc.mjs 的 glob、.gitignore 三处必须一致).
_REPORT_NAME = REPAIR_REPORT.name


@dataclass
class Snapshot:
    """一份历史快照: 原始 JSON 载荷 + ``身份键 -> 该次运行的记录``."""

    payload: dict[str, Any]
    results: dict[str, Any]

    @property
    def timestamp(self) -> float:
        """快照时间戳, 统一成**秒**(Allure 写进历史文件的是毫秒 —— 实测 1790008444155).

        读不出来时给 0: 只影响"这两份是不是同一轮", 不影响要不要修键。
        """
        try:
            return float(self.payload.get(TIMESTAMP_FIELD) or 0) / 1000.0
        except (TypeError, ValueError):
            return 0.0

    def keys(self) -> set[str]:
        """这份快照里所有身份键."""
        return set(self.results)

    def shapes(self) -> set[int]:
        """这份快照用到的键段数(混合形状的文件里可以不止一种)."""
        return {_parts(key) for key in self.results}


@dataclass
class RepairReport:
    """一次修复的结果(报告里的那份 markdown 就是它渲染出来的)."""

    read: int = 0
    duplicates: int = 0
    kept: int = 0
    old_shape: int | None = None
    new_shape: int | None = None
    rekeyed: int = 0
    unmatched: int = 0
    connected: int = 0
    older_snapshots: int = 0
    broken_lines: int = 0
    reset: bool = False
    reason: str = ""
    allure_version: str = ""
    # 最新快照距今几天(基线是不是被冻结了, 修复记录里要能看出来)。
    newest_age_days: float | None = None

    @property
    def changed(self) -> bool:
        """这次到底改了没有(没改就不该留修复记录, 免得报告里挂一份空附件).

        第一次运行(历史文件还不存在)不算"修过": 那时什么都不该留下。
        """
        if not self.read:
            return False
        return bool(self.reset or self.duplicates or self.rekeyed or self.unmatched)

    def summary(self) -> str:
        """一行日志(CI 里方便扫一眼)."""
        verdict = "已重置(重新开始记历史)" if self.reset else "趋势已恢复"
        stale = ""
        if (
            self.newest_age_days is not None
            and self.newest_age_days > STALE_BASELINE_DAYS
        ):
            stale = f"; 最新快照 {int(self.newest_age_days)} 天前(期间没有一轮运行被记进历史)"
        return (
            f"历史修复: {verdict}; 读入 {self.read} 份, 去重 {self.duplicates} 份, "
            f"重键 {self.rekeyed} 条, 丢弃 {self.unmatched} 条, "
            f"可连接旧快照 {self.connected}/{self.older_snapshots} 份{stale}"
        )


def _parts(key: str) -> int:
    """身份键有几段(``a.b`` -> 2)."""
    return len(key.split(".")) if key else 0


def _prefixes(key: str) -> list[str]:
    """键的所有前缀(``a.b.c`` -> ``["a.b", "a"]``): 用来找"是不是只差末尾维度"."""
    parts = key.split(".") if key else []
    return [".".join(parts[:index]) for index in range(len(parts) - 1, 0, -1)]


def _environment_of(value: object) -> str:
    """条目自己的环境标识, 统一小写后返回.

    旧快照写显示名(``macOS``)、新快照写 id(``macos``): 只差大小写时视为同一环境;
    字段缺失/不是字符串时返回空串, 调用方退回"唯一前缀"的判定。
    """
    if isinstance(value, dict):
        environment = value.get("environment")
        if isinstance(environment, str) and environment:
            return environment.casefold()
    return ""


def _resolve_key(
    key: str,
    value: object,
    target: int,
    reference: set[str],
    index: Mapping[str, set[str]],
    environments: Mapping[str, str],
) -> str | None:
    """把一个键映射成当前形状: 补不出唯一值时返回 ``None``(调用方计数并丢弃)."""
    parts = _parts(key)
    if parts == target:
        return key  # 已是当前段数: 原样保留(不许再按前缀改名)
    if parts > target:
        return None  # 比当前形状还长: 砍成前缀会并掉平台之间的区别
    found = _candidates(key, reference, index)
    if len(found) > 1:
        wanted = _environment_of(value)
        if wanted:
            narrowed = {
                candidate
                for candidate in found
                if environments.get(candidate) == wanted
            }
            if narrowed:
                found = narrowed
    if len(found) != 1:
        return None
    return next(iter(found))


def load_snapshots(path: Path) -> tuple[list[Snapshot], int]:
    """读历史文件: 返回(快照, 认不出来的行数).

    一行认不出来(不是 JSON 对象 / 没有 ``testResults``)就跳过它并计数 —— 文件被未来的
    Allure 换成别的容器格式时, 这里会变成"一行都读不出来", 交给调用方走重置兜底。
    """
    if not path.exists():
        return [], 0
    snapshots: list[Snapshot] = []
    broken = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            broken += 1
            continue
        results = payload.get(RESULTS_FIELD) if isinstance(payload, dict) else None
        if not isinstance(results, dict):
            broken += 1
            continue
        snapshots.append(Snapshot(payload=payload, results=dict(results)))
    return snapshots, broken


def _same_run(left: Snapshot, right: Snapshot, window: float) -> bool:
    """两份快照是不是**同一轮运行**的重复追加(时间相近 + 键集合重合度够高).

    时间近是前提: 真正不同的两轮运行相隔小时级。同一轮的两次生成之间可能有几条用例
    多跑/少跑(实测相差 5 条), 所以再退一步用重合度判, 而不是要求严格互相包含。
    """
    if abs(left.timestamp - right.timestamp) > window:
        return False
    left_keys, right_keys = left.keys(), right.keys()
    if not left_keys or not right_keys:
        return False
    overlap = len(left_keys & right_keys) / max(len(left_keys), len(right_keys))
    return overlap >= SAME_RUN_OVERLAP


def drop_duplicates(
    snapshots: Sequence[Snapshot], window: float = DUPLICATE_WINDOW_SECONDS
) -> tuple[list[Snapshot], int]:
    """去掉同一轮运行的重复快照, 保留信息更全的那一份, 返回(留下的, 去重掉的份数)."""
    kept: list[Snapshot] = []
    dropped = 0
    for snapshot in snapshots:
        previous = kept[-1] if kept else None
        if previous is not None and _same_run(previous, snapshot, window):
            if previous.keys() <= snapshot.keys():
                kept[-1] = snapshot  # 后一份更全: 换掉前一份
            dropped += 1
            continue
        kept.append(snapshot)
    return kept, dropped


def _prefix_index(reference: Iterable[str]) -> dict[str, set[str]]:
    """``前缀 -> 以它开头的键`` 的索引(键最多三段, 索引很小)."""
    index: dict[str, set[str]] = {}
    for key in reference:
        for prefix in _prefixes(key):
            index.setdefault(prefix, set()).add(key)
    return index


def _candidates(
    key: str, reference: set[str], index: Mapping[str, set[str]]
) -> set[str]:
    """与 ``key`` 只差末尾几段的**当前形状**键(两个方向都找: 少了维度或多了维度)."""
    found = set(index.get(key, ()))
    for prefix in _prefixes(key):
        if prefix in reference:
            found.add(prefix)
    return found


def _reference_index(
    snapshots: Sequence[Snapshot], target: int
) -> tuple[set[str], dict[str, str]]:
    """收集"已经是当前形状"的键作参考, 顺带记下每个键的环境(重键消歧用)."""
    reference: set[str] = set()
    environments: dict[str, str] = {}
    for snapshot in snapshots:
        if target in snapshot.shapes():
            for key, value in snapshot.results.items():
                reference.add(key)
                environment = _environment_of(value)
                if environment and key not in environments:
                    environments[key] = environment
    return reference, environments


def rekey(snapshots: Sequence[Snapshot]) -> tuple[int, int]:
    """把旧形状的键补成当前形状, 返回(重键条数, 补不出唯一值而丢掉的条数).

    当前形状取**最新快照**的键段数: 它由最近一次生成追加, 也就是这份 CLI 现在会算出来的
    形状。参考集合是"所有已经是当前形状的键" —— 于是重键只用到文件自身的信息, 不需要
    复刻 Allure 的哈希算法(那也复刻不了)。

    已知局限(见 ci.yml 的"Resolve previous run with Allure history"): 键形状刚变的第一轮,
    基线里还没有新形状的快照, 这里会把旧形状误当成"当前" —— 所以历史基线必须每轮都被
    消费, 链条一冻结(基线停在形状变更之前), 这一轮的报告就只剩自己的点。

    前缀对上多个当前键时(同一用例在三个平台各有一个键), 按条目自己的 ``environment``
    对准平台; 对不上时才按"补不出唯一值"丢弃。
    """
    if not snapshots:
        return 0, 0
    target = max(snapshots[-1].shapes() or {0})
    reference, environments = _reference_index(snapshots, target)
    if not reference or target <= 0:
        return 0, 0
    index = _prefix_index(reference)
    rekeyed = 0
    unmatched = 0
    for snapshot in snapshots:
        if snapshot.shapes() == {target}:
            continue  # 已经是当前形状
        rebuilt: dict[str, Any] = {}
        for key, value in snapshot.results.items():
            resolved = _resolve_key(key, value, target, reference, index, environments)
            if resolved is None:
                unmatched += 1  # 补不出唯一值 / 比当前形状还长: 留着也匹配不上
                continue
            rekeyed += 0 if resolved == key else 1
            rebuilt[resolved] = value
        snapshot.results = rebuilt
    return rekeyed, unmatched


def repair(
    path: Path, window: float = DUPLICATE_WINDOW_SECONDS, allure_version: str = ""
) -> tuple[RepairReport, list[Snapshot]]:
    """按"去重 -> 重键 -> 判定/兜底"修一遍, 返回(报告, 结果快照)."""
    snapshots, broken = load_snapshots(path)
    report = RepairReport(
        read=len(snapshots), broken_lines=broken, allure_version=allure_version
    )
    if not snapshots:
        if broken:
            report.reset = True
            report.reason = (
                f"历史文件里 {broken} 行都不是当前格式, 认不出来"
                " (未来的 Allure 换了容器格式时会这样)"
            )
        return report, []

    kept, duplicates = drop_duplicates(snapshots, window)
    report.duplicates = duplicates
    report.newest_age_days = (time.time() - kept[-1].timestamp) / 86_400.0
    report.new_shape = max(kept[-1].shapes() or {0})
    old_shapes = {shape for snapshot in kept for shape in snapshot.shapes()}
    report.old_shape = min(old_shapes) if old_shapes else None
    rekeyed, unmatched = rekey(kept)
    report.rekeyed = rekeyed
    report.unmatched = unmatched

    newest = kept[-1].keys()
    report.older_snapshots = len(kept) - 1
    report.connected = sum(1 for snapshot in kept[:-1] if snapshot.keys() & newest)
    if report.older_snapshots > 0 and report.connected == 0:
        report.reset = True
        report.reason = (
            f"重键之后没有任何一份旧快照能与最新快照相连"
            f" (旧键 {report.old_shape} 段 / 当前 {report.new_shape} 段, "
            "身份算法本身变了, 补不出对应关系)"
        )
        return report, []
    report.kept = len(kept)
    return report, kept


def write_history(path: Path, snapshots: Sequence[Snapshot]) -> None:
    """写回历史文件(空列表 = 清空它, 让趋势从现在重新开始).

    **必须把修好的 ``testResults`` 拼回载荷再写**: ``Snapshot.results`` 是单独一份
    (load 时就抄了一份), 只改它然后原样 dump ``payload`` 等于什么都没改 -- 实测过,
    第二次跑会得出一样的数字(不幂等)。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    if not snapshots:
        path.write_text("", encoding="utf-8")
        return
    lines = []
    for snapshot in snapshots:
        payload = dict(snapshot.payload)
        payload[RESULTS_FIELD] = snapshot.results
        lines.append(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def repair_text(report: RepairReport) -> str:
    """修复记录的正文(报告首页的全局附件就是它)."""
    verdict = "历史趋势已重置, 从现在重新开始记" if report.reset else "历史趋势已恢复"
    lines = [
        "# Allure 历史修复记录",
        "",
        "生成报告之前自动修了一遍历史文件(见 `scripts/repair_allure_history.py`), "
        "因为报告的趋势是按身份键**精确匹配**的 —— 键的形状变了, 旧数据就一条也接不上。",
        "",
        f"**结论**: {verdict}。",
        "",
        f"- 快照: 读入 {report.read} 份, 去重 {report.duplicates} 份, 保留 {report.kept} 份",
        f"- 身份键: 旧形状 {report.old_shape} 段 -> 当前 {report.new_shape} 段; "
        f"重键 {report.rekeyed} 条, 丢弃 {report.unmatched} 条(补不出唯一值)",
        f"- 旧快照能连上最新快照的: {report.connected}/{report.older_snapshots} 份",
    ]
    if (
        report.newest_age_days is not None
        and report.newest_age_days > STALE_BASELINE_DAYS
    ):
        lines.append(
            f"- 最新快照: {int(report.newest_age_days)} 天前 —— 期间没有任何一轮运行被"
            "记进历史(基线被冻结时, 键形状对不上, 趋势会整体消失)"
        )
    if report.broken_lines:
        lines.append(f"- 认不出来的行: {report.broken_lines} 行")
    if report.allure_version:
        lines.append(
            f"- Allure CLI: {report.allure_version}(CI 用的是动态版本, 形状会跟着它变)"
        )
    if report.reason:
        lines += ["", f"**为什么重置**: {report.reason}。"]
    lines += [
        "",
        "## 这一步为什么存在",
        "",
        "报告的历史点由 Allure 按 `retryHash` 匹配: 身份键里每多一个维度"
        "(例如采用 environments 之后的 `environmentHash`), 旧快照的键就整体对不上, "
        "于是看起来像是过去的记录全丢了 —— 数据其实都还在历史文件里, 丢的是**匹配**。",
        "所以这里做三件事: 同一轮运行被追加两次的快照去重(否则本次结果会冒充历史), "
        "旧形状的键按唯一前缀补成当前形状(前缀对上多个平台的键时按条目自己的 environment "
        "对准), 实在补不出来时清空历史重新开始并在这里写明。",
        "要改这段判断, 先看 `tests/unit/test_ci_history.py` —— 三种走向都钉在那里。",
        "",
    ]
    return "\n".join(lines)


def _parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    """命令行: 默认就地修 ``.allure/history.jsonl`` 并写根目录的修复记录."""
    parser = argparse.ArgumentParser(
        description="修复 Allure 历史文件里的身份键与重复快照"
    )
    parser.add_argument("--history", type=Path, default=HISTORY_PATH)
    parser.add_argument("--note", type=Path, default=REPAIR_REPORT)
    parser.add_argument("--allure-version", default="", help="写进修复记录的 CLI 版本")
    parser.add_argument(
        "--duplicate-window",
        type=float,
        default=DUPLICATE_WINDOW_SECONDS,
        help="多久以内、键集合互相包含的两份快照算同一轮运行",
    )
    parser.add_argument("--dry-run", action="store_true", help="只看结论, 不改文件")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """修历史文件并写修复记录; 没有可修的就清掉上一次留下的记录."""
    args = _parse_args(argv)
    report, snapshots = repair(
        args.history, window=args.duplicate_window, allure_version=args.allure_version
    )
    print(report.summary())
    if args.dry_run:
        if report.reason:
            print(f"  原因: {report.reason}")
        return 0
    if report.changed:
        if report.reset:
            write_history(args.history, [])
        elif snapshots:
            write_history(args.history, snapshots)
        args.note.write_text(repair_text(report), encoding="utf-8")
        print(f"  修复记录: {args.note}")
    elif args.note.exists():
        # 这一轮没修任何东西: 删掉上一次的记录, 否则报告里会挂着一份过期的"历史修复"
        # (与失败现场附件同一条理由: 永远挂着的空/旧附件只会误导).
        args.note.unlink()
        print(f"  已删除过期的修复记录: {args.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
