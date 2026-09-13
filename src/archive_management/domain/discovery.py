"""本地游戏探测与监控目录的领域模型(阶段 E-1).

探测结果刻意与"游戏"分开保存: 扫描到的东西只是**候选**, 用户确认之前不能
进入游戏库(PLAN 阶段 E-1 第 5 条, 与阶段 C "不能静默信任猜测路径"一致)。
因此这里有两个模型:

- :class:`MonitoredDirectory`: 用户自行添加的监控目录, 用于覆盖主流平台
  无法识别、路径自定义或平台客户端未安装的情况(PLAN 阶段 E-1 第 3 条);
- :class:`GameCandidate`: 一次探测得到的候选游戏及其来源、可信度、路径健康
  状态与处理进度(新发现/已导入/已忽略)。

模型是纯数据, 不触碰文件系统; 探测与校验在
:mod:`archive_management.services.platform_scan` 与
:mod:`archive_management.application.discovery` 中完成。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from typing import Literal

from archive_management.domain.entities import _RowModel

# 候选的来源平台/渠道: 前四项来自安装目录/注册表探测, monitored 来自用户
# 自行添加的监控目录, manual 保留给手工录入的候选.
DiscoverySource = Literal["steam", "epic", "gog", "battle_net", "monitored", "manual"]
# 候选可信度: 有平台清单/注册表背书的是 high, 监控目录子项是 medium, 其余 low.
Confidence = Literal["high", "medium", "low"]
# 候选的处理进度: 用户导入后置 imported, 忽略后置 ignored.
CandidateStatus = Literal["new", "imported", "ignored"]
# 候选/监控目录路径的健康状态: 只有 ok 才能直接用于后续操作.
PathHealth = Literal["ok", "missing", "not_directory", "unreadable", "unsafe"]

CONFIDENCE_RANK: dict[str, int] = {"high": 3, "medium": 2, "low": 1}
# 同一来源的权威性: 平台清单优先于用户监控目录(去重时保留更权威的一条).
SOURCE_RANK: dict[str, int] = {
    "steam": 5,
    "epic": 4,
    "gog": 4,
    "battle_net": 4,
    "monitored": 2,
    "manual": 1,
}


class MonitoredDirectory(_RowModel):
    """一个由用户添加、参与周期扫描的目录."""

    id: int | None = None
    path: str
    enabled: bool = True
    note: str = ""
    created_at: datetime | None = None
    last_scan_at: datetime | None = None
    # 上次扫描时该目录的健康状态(ok/missing/unreadable/...); 未扫描过为空.
    last_scan_status: str | None = None


class GameCandidate(_RowModel):
    """一次探测得到的候选游戏(用户确认前不进入游戏库)."""

    id: int | None = None
    name: str
    # 探测到的安装根目录(绝对路径); 用户可在此处"修正路径"。
    install_dir: str
    source: DiscoverySource = "manual"
    confidence: Confidence = "medium"
    # 得出该候选的规则代码(如 steam_manifest), 供界面解释来源.
    reason_code: str = ""
    # 附加说明(例如来源清单文件名或监控目录).
    detail: str = ""
    found_at: datetime | None = None
    status: CandidateStatus = "new"
    health: PathHealth = "ok"
    # 已导入的游戏 id(导入后才有值); 导入的游戏被删除时置空.
    game_id: int | None = None


def candidate_sort_key(candidate: GameCandidate) -> tuple[int, int, str]:
    """探测结果排序: 可信度高的在前, 同级按来源权威性与名称."""
    return (
        -CONFIDENCE_RANK.get(candidate.confidence, 0),
        -SOURCE_RANK.get(candidate.source, 0),
        candidate.name.casefold(),
    )


def dedupe_candidates(candidates: Iterable[GameCandidate]) -> list[GameCandidate]:
    """按安装路径去重并排序.

    同一路径可能同时来自平台清单与用户监控目录: 保留可信度(其次来源权威性)
    更高的一条, 避免界面出现重复条目; 路径比较忽略大小写与尾部斜杠。
    """
    best: dict[str, GameCandidate] = {}
    for candidate in candidates:
        key = candidate.install_dir.rstrip("\\/").casefold()
        current = best.get(key)
        if current is None or candidate_sort_key(candidate) < candidate_sort_key(
            current
        ):
            best[key] = candidate
    return sorted(best.values(), key=candidate_sort_key)
