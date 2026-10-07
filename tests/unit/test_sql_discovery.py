"""
发现与候选: 监控目录 CRUD、扫描导入、忽略/恢复/迁移、候选建议与来源选择、本地化名行。拆自 test_sql_backend.py(见 docs/test-refactor-plan.md S9)。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

import archive_management.ui.sql_backend as sql_mod
from archive_management.domain import (
    GameCandidate,
    PlatformGame,
)
from archive_management.exceptions import (
    ArchiveManagementError,
)
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.repository import (
    CandidateRepository,
    GameRepository,
)
from archive_management.services.game_names import name_cache_at

# 一张最小的 PNG 文件头(封面缓存只认文件头就能判定可用).
from sql_support import (
    _service,
    _steam_service,
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


def test_discovery_rows_carry_probed_save_paths(tmp_path: Path) -> None:
    """探测结果行里就带着该游戏的存档路径建议(只探测, 不写库)."""
    save = tmp_path / "saves"
    save.mkdir()
    (save / "slot.dat").write_text("x", encoding="utf-8")
    service, _game_id, database = _steam_service(tmp_path, str(save))
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    item = next(row for row in service.list_candidates() if row.name == "哈迪斯")

    assert item.save_supported is True
    assert [path.path for path in item.save_paths] == [str(save)]
    assert item.save_label == tr("discovery.save_found", count=1)
    # 只是建议: 存档位置仍然要等用户在导入对话框里确认.
    assert service.list_locations("1") == []


def test_discovery_rows_say_when_the_platform_is_unsupported(tmp_path: Path) -> None:
    """监控目录这类非平台来源不猜测存档路径, 行里明说需要手动添加."""
    service, _game_id, database = _steam_service(tmp_path)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(name="哈迪斯", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "哈迪斯")

    assert item.save_supported is False
    assert item.save_paths == ()
    assert item.save_label == tr(
        "discovery.save_unsupported", platform=item.source_label
    )


def test_discovery_rows_read_the_localized_name_from_the_cache(
    tmp_path: Path,
) -> None:
    """探测结果行显示缓存里的当前语言译名(扫描时已补好), 没缓存就回落原名."""
    cache_dir = tmp_path / "cache"
    name_cache_at(cache_dir).put("1145360", "zh-CN", "哈迪斯")
    service, _game_id, database = _steam_service(tmp_path, cache_dir=cache_dir)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(
            name="Hades",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )

    item = next(row for row in service.list_candidates() if row.name == "Hades")

    assert item.localized_name == "哈迪斯"
    assert item.display_name == "哈迪斯"


def test_discovery_rows_keep_the_detected_name_without_a_translation(
    tmp_path: Path,
) -> None:
    """没有译名(或不是平台清单来源)的候选直接显示探测到的名称."""
    service, _game_id, database = _steam_service(tmp_path)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(database).upsert(
        GameCandidate(name="Hades", install_dir=str(install), source="monitored")
    )

    item = next(row for row in service.list_candidates() if row.name == "Hades")

    assert item.localized_name == ""
    assert item.display_name == "Hades"


def test_import_candidate_writes_the_confirmed_save_paths(tmp_path: Path) -> None:
    """导入时把用户确认的路径写成存档位置: 平台候选保留来源, 新增的记手动."""
    save = tmp_path / "saves"
    save.mkdir()
    (save / "slot.dat").write_text("x", encoding="utf-8")
    extra = tmp_path / "extra"
    extra.mkdir()
    service, _game_id, database = _steam_service(tmp_path, str(save))
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    candidate, _created = CandidateRepository(database).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )
    assert candidate.id is not None

    summary = service.import_candidate(
        str(candidate.id), save_paths=(str(save), str(extra))
    )

    assert summary.saved_paths == 2
    game = GameRepository(database).get(int(summary.game_id))
    assert game is not None
    assert game.steam_app_id == 1145360
    sources = {
        item.path: item.source for item in service.list_locations(summary.game_id)
    }
    assert sources == {str(save): "steam", str(extra): "manual"}


def test_monitored_directories_crud(tmp_path: Path) -> None:
    """监控目录的增删改查都经后端暴露给界面, 并带实时路径状态."""
    service = _service(tmp_path)
    games = tmp_path / "Games"
    games.mkdir()

    created = service.add_monitored_directory(str(games), note="自定义")
    assert created.path == str(games)
    assert created.health == "ok"
    assert [item.directory_id for item in service.list_monitored_directories()] == [
        created.directory_id
    ]

    disabled = service.set_monitored_enabled(created.directory_id, False)
    assert disabled.enabled is False
    assert disabled.state_label == tr("discovery.dir_off")

    updated = service.update_monitored_directory(created.directory_id, note="改过")
    assert updated.note == "改过"

    service.remove_monitored_directory(created.directory_id)
    assert service.list_monitored_directories() == []


def test_scan_finds_monitored_games_and_imports_them(tmp_path: Path) -> None:
    """扫描会把监控目录里的游戏写进候选表, 导入后成为游戏记录."""
    service = _service(tmp_path)
    games = tmp_path / "Games"
    (games / "Hades").mkdir(parents=True)
    service.add_monitored_directory(str(games))

    report = service.scan_candidates()

    assert report.monitored == 1
    assert report.active == 1
    ours = next(
        item
        for item in service.list_candidates()
        if item.install_dir == str(games / "Hades")
    )
    assert ours.source == "monitored"
    assert ours.status == "new"
    assert ours.importable is True

    summary = service.import_candidate(ours.candidate_id, name="哈迪斯")

    assert summary.name == "哈迪斯"
    assert [game.game_id for game in service.list_games()] == [summary.game_id]
    imported = service.list_candidates(status="imported")
    assert [item.candidate_id for item in imported] == [ours.candidate_id]
    assert imported[0].game_id == summary.game_id
    # 已导入的候选不能再导入一次.
    with pytest.raises(ArchiveManagementError):
        service.import_candidate(ours.candidate_id)


def test_candidate_ignore_restore_and_relocate(tmp_path: Path) -> None:
    service = _service(tmp_path)
    games = tmp_path / "Games"
    (games / "Hades").mkdir(parents=True)
    service.add_monitored_directory(str(games))
    service.scan_candidates()
    candidate = next(
        item
        for item in service.list_candidates()
        if item.install_dir == str(games / "Hades")
    )

    ignored = service.set_candidate_ignored(candidate.candidate_id, True)
    assert ignored.status == "ignored"
    # 用集合判断而不是取第一条: 扫描本机时也可能会带出别的已忽略候选
    # (例如平台官方工具被默认隐藏).
    ignored_ids = {
        item.candidate_id for item in service.list_candidates(status="ignored")
    }
    assert candidate.candidate_id in ignored_ids

    restored = service.set_candidate_ignored(candidate.candidate_id, False)
    assert restored.status == "new"

    moved = tmp_path / "Elsewhere" / "Hades"
    moved.mkdir(parents=True)
    relocated = service.relocate_candidate(candidate.candidate_id, str(moved))
    assert relocated.install_dir == str(moved)
    assert relocated.health == "ok"

    watched = service.add_candidate_as_monitored(candidate.candidate_id)
    assert watched.path == str(tmp_path / "Elsewhere")


def test_backend_rejects_unknown_discovery_ids(tmp_path: Path) -> None:
    service = _service(tmp_path)

    with pytest.raises(ArchiveManagementError, match="未知监控目录"):
        service.set_monitored_enabled("not-a-number", False)
    with pytest.raises(ArchiveManagementError, match="未知监控目录"):
        service.remove_monitored_directory("42")
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        service.import_candidate("42")
    with pytest.raises(ArchiveManagementError, match="未知探测结果"):
        service.set_candidate_ignored("42", True)
    with pytest.raises(ArchiveManagementError, match="不支持的筛选条件"):
        service.list_candidates(status="bogus")


def test_candidate_listing_with_and_without_a_status_filter(tmp_path: Path) -> None:
    """候选列表的口径: 不传/空串/"all" = 全部, 具体状态只留那一种, 非法值报错."""
    service = _service(tmp_path)
    everything = service.list_candidates()

    assert service.list_candidates(status=None) == everything
    assert service.list_candidates(status="") == everything
    assert service.list_candidates(status="all") == everything
    assert service.list_candidates(status="ignored") == []

    with pytest.raises(ArchiveManagementError):
        service.list_candidates(status="suggested")


def test_suggest_for_game_returns_nothing_without_a_platform_adapter(
    tmp_path: Path,
) -> None:
    """手填/监控目录来源没有平台适配器: 不猜存档位置(导入照常进行)."""
    service = _service(tmp_path)
    game_id = service.add_game("手填游戏").game_id
    game = GameRepository(service._database).get(int(game_id))
    assert game is not None

    assert service._suggest_for_game(game) == {}


def test_suggest_for_game_skips_candidates_without_a_stored_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """候选落库后仍拿不到 id 时跳过它(而不是往映射里塞一条 id 为 None 的记录)."""
    from types import SimpleNamespace

    import archive_management.application.candidates as candidate_cases

    service, game_id, _database = _steam_service(tmp_path)
    game = GameRepository(service._database).get(int(game_id))
    assert game is not None
    report = SimpleNamespace(candidates=(SimpleNamespace(id=None, path=str(tmp_path)),))
    monkeypatch.setattr(candidate_cases, "suggest_candidates", lambda *a, **k: report)

    assert service._suggest_for_game(game) == {}


def test_clear_scan_results_reports_the_count_and_refreshes_the_revision(
    tmp_path: Path,
) -> None:
    """清空探测结果: 给出删掉的条数, 并让数据版本往前走(界面据此重读列表)."""
    service = _service(tmp_path)
    install = tmp_path / "Games" / "Hades"
    install.mkdir(parents=True)
    CandidateRepository(Database(tmp_path / "app.db")).upsert(
        GameCandidate(
            name="哈迪斯",
            install_dir=str(install),
            source="steam",
            reason_code="steam_manifest",
            detail="appmanifest_1145360.acf",
        )
    )
    assert len(service.list_candidates()) == 1
    revision = service._data_revision

    assert service.clear_scan_results() == 1

    assert service.list_candidates() == []
    assert service._data_revision > revision


def test_a_broken_adapter_only_costs_the_save_path_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """适配器推断存档路径时炸了: 只让这一款没有建议(界面显示"需手动添加"), 不阻断扫描."""
    service = _service(tmp_path)

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("适配器炸了")

    adapter = SimpleNamespace(supports_save_paths=True, save_candidates=refuse)
    monkeypatch.setattr(service, "_adapter_for", lambda _platform: adapter)
    monkeypatch.setattr(
        sql_mod,
        "_platform_game",
        lambda _candidate: PlatformGame(
            name="哈迪斯", platform="steam", game_id="1145360"
        ),
    )

    assert (
        service._save_suggestions(cast(GameCandidate, SimpleNamespace(name="哈迪斯")))
        == ()
    )
