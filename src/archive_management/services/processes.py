"""游戏进程探测.

可选地检测游戏进程, 避免游戏运行时恢复或覆盖存档; 检测失败时仍允许用户明确
强制执行. 这里把进程枚举封装成可注入的提供者: 默认实现
惰性导入 ``psutil``, 导入或枚举失败时返回 ``checked=False``(而不是抛错),
让调用方区分"确认没在运行"和"无法确认"两种状态。

两个入口:

- :func:`probe_processes` / :func:`probe_game_process`: 拿一串候选名判断"有没有
  在运行"(恢复前的单款检查用它);
- :func:`probe_games`: 全库监控用 —— **只枚举一次**进程表, 按
  :class:`~archive_management.domain.activation.NameOwnership` 把命中的进程名归到
  **唯一**的一款游戏上; 一个进程名能归给多款游戏(含互相包含)时整体丢弃, 宁可不判断。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from archive_management.domain.activation import NameOwnership
from archive_management.domain.process_names import (
    MAX_PROCESS_NAMES,
    MIN_NAME_LENGTH,
    name_needles,
    names_match,
    normalize_name,
)

# 进程名提供者: 返回当前可见的进程名; 失败时抛出异常.
ProcessNameProvider = Callable[[], Iterable[str]]


@dataclass(frozen=True)
class ProcessProbe:
    """一次游戏进程探测的结果."""

    checked: bool
    running: bool
    matches: tuple[str, ...] = ()


# "没探测"的结果: 无法确认是否在运行。它与"确认没在运行"是两回事 —— 自动启停
# 只在 checked 为 True 时才敢根据 running 下结论。
UNCHECKED_PROBE = ProcessProbe(checked=False, running=False)


def probe_game_process(
    name: str, *, provider: ProcessNameProvider | None = None
) -> ProcessProbe:
    """判断与游戏名匹配的进程是否在运行(单名称便捷入口)."""
    return probe_processes([name], provider=provider)


@dataclass(frozen=True)
class GameProbe:
    """一次"全库游戏"探测的结果.

    ``running_ids`` 只包含"能确定归属"的游戏(按 id 升序, 便于用例断言与入队顺序
    确定); ``ambiguous`` 是因为一个进程名同时命中多款游戏而被丢弃的进程名,
    ``conflicts`` 是因为候选名本身被多款游戏共享而无法使用的针(由名称索引给出)。
    """

    checked: bool
    running_ids: tuple[int, ...] = ()
    matches: Mapping[int, tuple[str, ...]] = field(default_factory=dict)
    ambiguous: tuple[str, ...] = ()
    conflicts: tuple[str, ...] = ()


# 全库探测的"没探测"结果: 库内没有任何可用候选名(例如名字全是中文)时用它。
UNCHECKED_GAME_PROBE = GameProbe(checked=False)


def _clean_game_probe(ownership: NameOwnership) -> GameProbe:
    """构造"没有探测"的全库结果(带上名称冲突信息, 界面要用)."""
    return GameProbe(checked=False, conflicts=ownership.conflicts)


def probe_games(
    ownership: NameOwnership, *, provider: ProcessNameProvider | None = None
) -> GameProbe:
    """判断哪些游戏正在运行(只枚举一次进程表).

    每个进程名先归一化一次, 再找出它命中的所有针: 恰好只指向一款游戏才记账,
    否则整条丢弃(记进 ``ambiguous``) —— 全库匹配下"互相包含"远比单款时容易误报。
    """
    if not ownership.needles:
        return _clean_game_probe(ownership)
    raw_names = _read_process_names(
        provider if provider is not None else psutil_process_names
    )
    if raw_names is None:
        return _clean_game_probe(ownership)
    hits: dict[int, list[str]] = {}
    ambiguous: list[str] = []
    for raw in raw_names:
        normalized = normalize_name(raw)
        owners = {
            ownership.needles[needle]
            for needle in ownership.needles
            if names_match(needle, normalized)
        }
        if not owners:
            continue
        if len(owners) > 1:
            ambiguous.append(raw)
            continue
        hits.setdefault(owners.pop(), []).append(raw)
    return GameProbe(
        checked=True,
        running_ids=tuple(sorted(hits)),
        matches={game_id: tuple(sorted(names)) for game_id, names in hits.items()},
        ambiguous=tuple(sorted(set(ambiguous))),
        conflicts=ownership.conflicts,
    )


def _read_process_names(read: ProcessNameProvider) -> list[str] | None:
    """枚举当前进程名; 提供者异常时返回 None(调用方按"没检查到"处理)."""
    try:
        return [raw for raw in read() if raw]
    except Exception:
        return None


def _matches_any(raw: str, needles: Sequence[str]) -> bool:
    """进程名与任一候选匹配(归一化后互相包含)."""
    normalized = normalize_name(raw)
    return any(names_match(needle, normalized) for needle in needles)


def probe_processes(
    names: Sequence[str], *, provider: ProcessNameProvider | None = None
) -> ProcessProbe:
    """判断多个候选名称中是否有进程在运行(只枚举一次进程表).

    匹配规则是"归一化后互相包含": 候选 ``Outer Wilds`` 能匹配到
    ``OuterWilds.exe``; 名称过短或为空时直接跳过, 避免误报。
    """
    needles = name_needles(names)
    if not needles:
        return ProcessProbe(checked=False, running=False)
    raw_names = _read_process_names(
        provider if provider is not None else psutil_process_names
    )
    if raw_names is None:
        return ProcessProbe(checked=False, running=False)
    matches = sorted({raw for raw in raw_names if _matches_any(raw, needles)})
    return ProcessProbe(checked=True, running=bool(matches), matches=tuple(matches))


# 播种时的截断上限沿用 domain.process_names 的 MAX_PROCESS_NAMES: 候选越多, 跨游戏
# 撞针(NameOwnership"共享即弃")把现有判断挤掉的风险越大 —— 5 个足够覆盖
# "目录名 + 主程序 + 常见启动变体"。上限与游戏设置里的手动编辑共用(见 v16 设计)。

# 已知的辅助程序前缀(归一化后匹配): 崩溃处理器/上报器不是游戏本体, 却常年挂在
# 进程表里, 把它们当候选只会制造误报.
_HELPER_EXECUTABLE_PREFIXES = ("unitycrashhandler", "crashreportclient")


def _executable_size(path: Path) -> int:
    """返回可执行的大小; 取不到时给 -1(排序仍成立, 拿不到大小的排最后)."""
    try:
        return path.stat().st_size
    except OSError:
        return -1


def _is_main_executable(name: str) -> bool:
    """过滤当不了进程候选的名字: 含逗号(存不进拼接列)、归一化后过短、辅助程序."""
    if "," in name:
        return False
    normalized = normalize_name(name)
    if len(normalized) < MIN_NAME_LENGTH:
        return False
    return not normalized.startswith(_HELPER_EXECUTABLE_PREFIXES)


def install_process_candidates(install_dir: Path, platform: str) -> tuple[str, ...]:
    """从安装目录一次性派生进程名候选(导入发现候选建游戏时播种).

    组成: 安装目录名(全平台) + 顶层可执行名(Windows 是 ``*.exe``, macOS 是
    ``*.app``; Linux 没有可靠的扩展名约定, 只取目录名)。可执行按**大小降序**排、
    再截到上限 —— 主程序几乎总是比同目录的附带工具大, 超出上限时优先留下它。
    目录不存在/不可读时只剩目录名: 播种是尽力而为, 路径健康由发现层另行判定,
    这里不报错也不猜。含逗号与归一化后过短的名字在上游就被丢弃, 保证落库的
    逗号拼接存储(见 infrastructure.schema 的 v16)不丢信息。
    """
    names: list[str] = []
    dirname = install_dir.name.strip()
    if dirname and "," not in dirname:
        names.append(dirname)
    pattern = {"windows": "*.exe", "macos": "*.app"}.get(platform)
    if pattern is not None and install_dir.is_dir():
        entries = sorted(
            (
                entry
                for entry in install_dir.glob(pattern)
                if _is_main_executable(entry.name)
            ),
            key=_executable_size,
            reverse=True,
        )
        names.extend(entry.name for entry in entries)
    return tuple(dict.fromkeys(names))[:MAX_PROCESS_NAMES]


def psutil_process_names() -> Iterable[str]:
    """用 psutil 枚举当前进程名(惰性导入)."""
    import psutil

    names: list[str] = []
    for process in psutil.process_iter(["name"]):
        value = (process.info or {}).get("name")
        if isinstance(value, str):
            names.append(value)
    return names


__all__ = [
    "UNCHECKED_GAME_PROBE",
    "UNCHECKED_PROBE",
    "GameProbe",
    "ProcessNameProvider",
    "ProcessProbe",
    "install_process_candidates",
    "probe_game_process",
    "probe_games",
    "probe_processes",
    "psutil_process_names",
]
