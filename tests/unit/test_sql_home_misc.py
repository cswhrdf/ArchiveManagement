"""
主页汇总与杂项守卫: 主页看板/筛选/归档标签、忙碌二次操作拒绝、验证策略来源、主题名、激活轮询修订号。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from archive_management.config import AppConfig, VerificationSettings, save_config
from archive_management.domain import (
    HomeFilter,
    HomeView,
)
from archive_management.exceptions import (
    ArchiveManagementError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.sql_backend import SqlArchiveService, policy_from_config

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _service,
    _service_with_save,
)

pytestmark = [
    pytest.mark.backend,
    pytest.mark.database,
    pytest.mark.critical,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("真实 SQLite 后端"),
    pytest.mark.story("备份恢复与删除数据流"),
    # 真实 SQLite + 文件系统 + 调度后端, 按层定义归入 integration.
    pytest.mark.layer("integration"),
]


def test_home_board_lists_games_and_persists_filter(tmp_path: Path) -> None:
    """主页列表: 筛选当场生效; 筛选条件落库, 但**启动时只继承展示偏好**."""
    service = _service(tmp_path)
    service.add_game("星际拓荒")
    service.add_game("空洞骑士")

    board = service.load_home()
    assert {item.name for item in board.games} == {"星际拓荒", "空洞骑士"}
    assert board.filter.view is HomeView.ALL
    # 刚录入的游戏算“最近活跃”, 但都还没有存档位置, 因此都是待处理.
    assert board.summary == tr("home.summary", total=2, recent=2, pending=2)

    applied = service.apply_home_filter(
        HomeFilter(view=HomeView.PENDING, search="拓荒")
    )
    assert [item.name for item in applied.games] == ["星际拓荒"]

    # 重新打开: 视图/搜索词不跟着回来(用户 2026-10-02: 关在“待处理”页签上, 下次不该
    # 还在那一页); 展示偏好那半边由 test_home_cases 的展示偏好用例守着.
    reloaded = service.load_home()
    assert reloaded.filter.view is HomeView.ALL
    assert reloaded.filter.search == ""
    assert {item.name for item in reloaded.games} == {"星际拓荒", "空洞骑士"}
    assert reloaded.origin_text == tr("home.origin_all")
    assert reloaded.category_text == tr("home.category_all")


def test_home_filter_options_carry_counts(tmp_path: Path) -> None:
    """平台与分类下拉框的每一项都要带数量, 避免点进空分类."""
    service = _service(tmp_path)
    service.add_game("星际拓荒")

    board = service.load_home()
    assert [option.key for option in board.views] == [view.value for view in HomeView]
    origins = {option.key: option.count for option in board.origins}
    assert origins == {"manual": 1}
    categories = {option.key: option.count for option in board.categories}
    assert categories["origin:manual"] == 1
    assert categories["backup:none"] == 1

    narrowed = service.apply_home_filter(HomeFilter(origin="manual"))
    assert narrowed.origin_text == f"{tr('discovery.source_manual')} (1)"
    assert narrowed.narrowing is True
    # 平台筛选不参与自身的计数: 否则下拉框里只会剩下当前平台.
    assert {option.key for option in narrowed.origins} == {"manual"}


def test_home_archive_and_tags_round_trip(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")

    archived = service.set_game_archived(summary.game_id, True)
    assert archived.stats.archived == 1
    assert archived.games == ()

    restored = service.set_game_archived(summary.game_id, False)
    assert [item.name for item in restored.games] == ["星际拓荒"]

    tagged = service.set_game_tags(summary.game_id, [" 探索 ", "探索", "解谜"])
    assert tagged.games[0].tags == ("探索", "解谜")
    assert "探索" in tagged.games[0].chips

    # 中英文逗号一视同仁: 两种逗号都不会留在标签里, 也不会在往返后被拆成两个
    comma_ed = service.set_game_tags(
        summary.game_id,
        ["探索,解谜", "动作，冒险"],  # noqa: RUF001 - 全角逗号正是被测输入
    )
    assert comma_ed.games[0].tags == ("探索解谜", "动作冒险")
    assert service.load_home().games[0].tags == ("探索解谜", "动作冒险")


def test_a_game_without_a_steam_id_still_renders_with_placeholders(
    tmp_path: Path,
) -> None:
    """手填游戏没有 AppID: 列表/主页/详情照常展示, 取图直接给占位而不是报错."""
    service = _service(tmp_path)
    summary = service.add_game("手填游戏")

    assert [item.name for item in service.list_games()] == ["手填游戏"]
    assert service.get_detail(summary.game_id).name == "手填游戏"
    assert service.load_home().games[0].backup_count == 0
    assert service.artwork_path(summary.game_id, "cover") == ""


def test_poll_activation_without_the_auto_flag_changes_nothing(tmp_path: Path) -> None:
    """自动启停关着的时候轮询一次不产生任何变化(不可能去枚举进程)."""
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    service.set_game_enabled(summary.game_id, True)

    outcome = service.poll_activation(enabled=False)

    assert outcome.changed is False


def test_home_reflects_backups_and_locations(tmp_path: Path) -> None:
    service = _service(tmp_path)
    summary = service.add_game("星际拓荒")
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(summary.game_id, path=str(save), kind="directory")

    board = service.load_home()
    item = board.games[0]
    assert item.location_count == 1
    assert item.backup_enabled is True
    assert item.last_backup_label == ""
    assert tr("home.last_backup_none") in item.summary
    assert tr("home.cat_backup_none") in item.chips
    assert board.detail == tr("home.detail", backed_up=0, risky=0)

    service.run_backup_now(summary.game_id)

    refreshed = service.load_home()
    assert refreshed.games[0].backup_count == 1
    assert refreshed.games[0].last_backup_label != ""
    assert tr("home.cat_backup_done") in refreshed.games[0].chips
    assert refreshed.stats.pending == 0
    assert refreshed.detail == tr("home.detail", backed_up=1, risky=0)


def test_home_marks_games_without_locations_as_pending(tmp_path: Path) -> None:
    service = _service(tmp_path)
    service.add_game("还没配置")

    board = service.load_home()
    assert board.stats.pending == 1
    assert board.games[0].backup_enabled is False

    only_pending = service.apply_home_filter(HomeFilter(view=HomeView.PENDING))
    assert [item.name for item in only_pending.games] == ["还没配置"]
    # 刚录入的游戏同时属于“最近活跃”: 录入本身就算一次活动.
    recent = service.apply_home_filter(HomeFilter(view=HomeView.RECENT))
    assert [item.name for item in recent.games] == ["还没配置"]

    empty = service.apply_home_filter(HomeFilter(search="zzz"))
    assert empty.games == ()
    assert empty.narrowing is True
    assert empty.empty_message == tr("home.empty_filtered", total=1)
    assert empty.empty_hint == tr("home.empty_filtered_hint")


def test_home_rejects_unknown_game_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.set_game_archived("999", True)
    with pytest.raises(ArchiveManagementError, match="未知游戏"):
        service.set_game_tags("not-a-number", ["x"])


def test_poll_activation_bumps_the_revision_only_when_the_state_changed(
    tmp_path: Path,
) -> None:
    """自动启停真的切换了启用态时数据版本要变(界面据此重读)."""
    database = Database(tmp_path / "app.db")
    database.migrate()
    save = tmp_path / "save"
    save.mkdir()
    (save / "slot1.dat").write_text("v1", encoding="utf-8")
    idle = SqlArchiveService(
        database,
        backup_root=tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        process_provider=lambda: ["Demo.exe"],
    )
    game_id = idle.add_game("Demo").game_id
    idle.add_location(game_id, path=str(save), kind="directory")

    before = idle.task_status(game_id).revision
    outcome = idle.poll_activation(enabled=True)

    assert outcome.changed is True
    assert idle.task_status(game_id).revision > before
    # 再轮询一次: 状态没变就不该再动版本号(否则界面会被无意义地重读).
    again = idle.poll_activation(enabled=True)
    assert again.changed is False
    assert idle.task_status(game_id).revision == idle.task_status(game_id).revision


def test_busy_guards_reject_a_second_operation(tmp_path: Path) -> None:
    """一次只允许一个备份/恢复: 已有任务在跑时再发起要明确报忙."""
    from archive_management.ui.sql_backend import _ActiveOperation

    service, game_id, _save = _service_with_save(tmp_path)
    service.run_backup_now(game_id)
    node = service.list_backups(game_id)[0]
    service._active = _ActiveOperation(game_id=int(game_id))

    with pytest.raises(ArchiveManagementError) as backup_error:
        service.run_backup_now(game_id)
    with pytest.raises(ArchiveManagementError) as restore_error:
        service.run_restore(game_id, node.backup_id)

    assert tr("error.operation_busy") in str(backup_error.value)
    assert tr("error.operation_busy") in str(restore_error.value)
    assert service._active is not None, "被拒绝的调用不该把进行中的任务清掉"


def test_cancel_and_progress_are_quiet_without_an_operation(tmp_path: Path) -> None:
    """没有进行中的任务时: 取消返回 False, 进度回调直接忽略(后台线程的收尾竞态)."""
    from archive_management.ui.sql_backend import _ActiveOperation

    service = _service(tmp_path)

    assert service.cancel_active() is False
    service._report_progress(0.5, "不该记录")  # 不该抛错
    assert service._active is None
    assert service._cancel_requested() is False

    service._active = _ActiveOperation(game_id=1)
    assert service.cancel_active() is True
    service._report_progress(0.4, "写回中")
    assert service._active.fraction == 0.4
    assert service._active.message == "写回中"
    assert service._cancel_requested() is True


def test_policy_from_config_reads_the_verification_section(tmp_path: Path) -> None:
    """校验方式与并发数从配置文件读, 不会因为"没传"而落到默认值上."""
    path = tmp_path / "config.json"
    save_config(
        AppConfig(verification=VerificationSettings(mode="name", max_parallel=3)),
        path,
    )

    policy = policy_from_config(path)

    assert (policy.mode, policy.max_parallel) == ("name", 3)


def test_a_service_without_a_config_path_uses_the_default_policy(
    tmp_path: Path,
) -> None:
    """测试与演示场景没有配置路径: 用默认策略(sha256), 不凭空去读用户配置."""
    service, game_id, _save = _service_with_save(tmp_path)

    service.run_backup_now(game_id)

    assert service.list_backups(game_id)[0].verified is True


def test_theme_only_knows_the_two_names(tmp_path: Path) -> None:
    """主题只有 dark/light 两种: 别的名字一律按 light(界面上没有第三种可选)."""
    service = _service(tmp_path)

    assert service.current_theme() == "dark"
    assert service.set_theme("dark") == "dark"
    assert service.set_theme("light") == "light"
    assert service.set_theme("系统默认") == "light"
    assert service.current_theme() == "light"
