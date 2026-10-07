"""游戏主页在大规模数据下的响应与内存基准.

数据规模: 2000 款游戏 x 每款 2 个存档位置 + 3 个备份节点。测量指标: 主页聚合
查询、事实装载、筛选排序、展示模型映射与 UI 后端读取的耗时, 以及一次完整主页
装载的内存峰值。阈值是"跨机器可用"的宽裕上限, 用来拦截数量级级别的回归
(例如退化成按游戏逐个查询的 N+1、或每次筛选都重新装载全部事实)。

失败风险: 主页取数引入 N+1 查询、筛选函数引入全量深拷贝、展示模型保留了多余的
大对象, 都会让游戏数增长时的响应与内存显著变差。
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from archive_management.application.home import (
    build_report,
    filter_home,
    load_facts,
    load_home,
)
from archive_management.domain import GameFacts, HomeFilter, HomeView
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import HomeRepository
from archive_management.ui.models import home_board
from archive_management.ui.sql_backend import SqlArchiveService
from helpers import BASE_MOMENT, migrated_database, seed_home_games
from reporting import PerformanceRecorder

pytestmark = [
    pytest.mark.performance,
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("主页规模基准"),
    pytest.mark.story("大规模游戏库的主页响应"),
    pytest.mark.layer("performance"),
    pytest.mark.timeout(300),
]

# 数据规模: 主页要能承受"几千款游戏"的真实库存量级.
GAME_COUNT = 2000
SCALE = f"{GAME_COUNT} 款游戏 x 2 存档位置 x 3 备份节点"
# 时间基准固定, 让"最近活跃/长期未更新"的筛选结果可复现.
NOW = BASE_MOMENT + timedelta(days=1)


def _display_stamp(moment: datetime) -> str:
    """把时间格式化为展示文本(基准只要求可调用且返回字符串)."""
    return moment.strftime("%Y-%m-%d %H:%M")


@pytest.fixture(scope="module")
def home_database(tmp_path_factory: pytest.TempPathFactory) -> Database:
    """一次性写入规模数据(造数据本身不计入基准)."""
    root = tmp_path_factory.mktemp("home-scale")
    database = migrated_database(root)
    seed_home_games(database, GAME_COUNT)
    return database


@pytest.fixture(scope="module")
def loaded_facts(home_database: Database) -> list[GameFacts]:
    """主页事实只装载一次, 供需要复用输入的基准使用."""
    return load_facts(home_database)


def test_home_rows_aggregation_stays_constant(
    home_database: Database, perf_recorder: PerformanceRecorder
) -> None:
    """主页聚合查询与游戏数无关: 固定条数 SQL, 不会按游戏逐条查."""
    with perf_recorder.duration("home.rows_query", scale=SCALE, budget_seconds=5.0):
        rows = HomeRepository(home_database).list_rows()

    assert len(rows) == GAME_COUNT
    assert sum(len(row.locations) for row in rows) == GAME_COUNT * 2
    assert all(row.backup_count == 3 for row in rows)


def test_home_load_and_report_within_budget(
    home_database: Database, perf_recorder: PerformanceRecorder
) -> None:
    """装载事实与组装主页报告的耗时上限."""
    with perf_recorder.duration("home.load_facts", scale=SCALE, budget_seconds=10.0):
        facts = load_facts(home_database)
    with perf_recorder.duration("home.build_report", scale=SCALE, budget_seconds=3.0):
        report = build_report(facts, HomeFilter(), now=NOW)

    assert len(facts) == GAME_COUNT
    # 归档游戏只出现在"已归档"视图, 因此默认视图的 total 不含它们.
    assert report.stats.total + report.stats.archived == GAME_COUNT


def test_home_filters_and_sorting_within_budget(
    home_database: Database,
    loaded_facts: list[GameFacts],
    perf_recorder: PerformanceRecorder,
) -> None:
    """五种常用筛选(搜索/待处理/最近/平台/归档)全部在预算内完成."""
    cases = [
        HomeFilter(search="游戏 0001"),
        HomeFilter(view=HomeView.PENDING),
        HomeFilter(view=HomeView.RECENT),
        HomeFilter(origin="steam"),
        HomeFilter(view=HomeView.ARCHIVED),
    ]
    scale = f"{SCALE} x {len(cases)} 种筛选"
    with perf_recorder.duration("home.filter_home", scale=scale, budget_seconds=4.0):
        reports = [
            filter_home(home_database, case, facts=loaded_facts, now=NOW)
            for case in cases
        ]

    assert len(reports[0].games) >= 1  # "游戏 0001" 至少命中自身
    assert len(reports[0].games) <= 10  # 且只命中前缀相同的少数几款
    assert len(reports[4].games) > 0  # 归档视图非空
    assert all(len(report.games) <= GAME_COUNT for report in reports)


def test_home_board_uses_bounded_memory(
    home_database: Database, perf_recorder: PerformanceRecorder
) -> None:
    """完整主页装载(取数 + 报告 + 展示模型)的耗时与内存峰值上限."""
    with (
        perf_recorder.peak_memory(
            "home.load_home_memory", scale=SCALE, budget_mib=400.0
        ),
        perf_recorder.duration("home.load_home", scale=SCALE, budget_seconds=15.0),
    ):
        report = load_home(home_database, now=NOW)
        board = home_board(report, stamp=_display_stamp)

    assert len(board.games) == board.stats.total  # 默认视图不含归档游戏
    assert board.stats.total + board.stats.archived == GAME_COUNT
    assert board.summary
    assert board.detail
    assert any(option.count for option in board.views)


def test_backend_home_response_time(
    home_database: Database, tmp_path: Path, perf_recorder: PerformanceRecorder
) -> None:
    """UI 后端(真实 SQLite)的主页与任务卡响应: 界面轮询直接依赖该路径."""
    service = SqlArchiveService(home_database, backup_root=tmp_path / "backups")
    with (
        perf_recorder.peak_memory(
            "backend.load_home_memory", scale=SCALE, budget_mib=400.0
        ),
        perf_recorder.duration("backend.load_home", scale=SCALE, budget_seconds=20.0),
    ):
        board = service.load_home()
    with perf_recorder.duration("backend.task_status", scale=SCALE, budget_seconds=2.0):
        status = service.task_status(None)

    assert len(board.games) == board.stats.total
    assert board.stats.total + board.stats.archived == GAME_COUNT
    assert status.backend_ok is True
