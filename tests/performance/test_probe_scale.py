"""进程探测在大规模关注列表下的轮询成本基准.

数据规模: 120 款游戏(全部参与监控) x 300 条进程(120 条在跑 + 180 条系统噪声)。
播种前(只界面名/原始名)与播种后(再加安装目录名/主程序 exe 名)各测一遍, 指标是
"建针索引 + 一次全库匹配"的合计耗时 —— 自动启停的空闲档每 5 秒跑一次(见
``domain.activation.ACTIVATION_DELAY_LADDER``), 这就是"关注游戏很多时"的持续
开销; psutil 枚举本身不随候选数变, 不在本基准里。阈值是跨机器可用的宽裕上限,
用来拦数量级级别的回归(例如把归一化搬进内层循环、或每款游戏重复枚举进程表)。
"""

from __future__ import annotations

import pytest

from archive_management.domain import Game
from archive_management.domain.activation import build_name_ownership
from archive_management.services.processes import probe_games
from reporting import PerformanceRecorder

pytestmark = [
    pytest.mark.performance,
    pytest.mark.minor,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("游戏启停"),
    pytest.mark.story("大规模关注列表的轮询成本"),
    pytest.mark.layer("performance"),
    pytest.mark.timeout(300),
]

GAMES = 120
PROCESSES = 300
BUDGET_SECONDS = 0.25


def _library(seeded: bool) -> list[Game]:
    """构造全部参与监控的游戏; 界面名刻意是纯中文(归一化后产生不了针)."""
    return [
        Game(
            id=index + 1,
            name=f"游戏标题{chr(0x4E00 + index)}",
            original_name=f"Game Title {index:03d}",
            process_names=(f"InstallDir{index:03d}", f"GameTitle{index:03d}.exe")
            if seeded
            else (),
        )
        for index in range(GAMES)
    ]


def test_probing_a_large_watch_list_stays_within_the_budget(
    perf_recorder: PerformanceRecorder,
) -> None:
    """120 款关注游戏的单次探测(建索引 + 匹配)在预算内, 播种前后各测一遍."""
    table = [f"GameTitle{index:03d}.exe" for index in range(GAMES)]
    table += [f"SystemService{index:03d}.exe" for index in range(PROCESSES - GAMES)]

    def provider() -> list[str]:
        return list(table)

    for label, seeded in (("bare", False), ("seeded", True)):
        ownership = build_name_ownership(_library(seeded))
        with perf_recorder.duration(
            f"probe_games_{label}",
            scale=f"{GAMES} games x {PROCESSES} processes",
            budget_seconds=BUDGET_SECONDS,
        ):
            probe = probe_games(ownership, provider=provider)
        assert probe.checked is True
        assert probe.running_ids == tuple(range(1, GAMES + 1))
