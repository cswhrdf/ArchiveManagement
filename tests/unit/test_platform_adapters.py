"""平台适配器的契约测试.

阶段 G-1 要求"同一套断言跑所有适配器": 本模块用一个用例遍历
``default_adapters`` 里的每个平台, 检查标识、支持标记、返回类型与"不抛异常"的
降级行为; 再用 Steam 的用例验证真实链路(应用清单 → ``PlatformGame``, 云端清单
→ 存档候选), 以及清单损坏、平台数据错配时的降级。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import helpers
from archive_management.domain import (
    PLATFORM_DATA_FORMAT_VERSION,
    PLATFORM_IDS,
    PlatformGame,
    PlatformId,
    SavePathCandidate,
)
from archive_management.services.platform_adapters import (
    PlatformAdapter,
    SteamAdapter,
    UnsupportedPlatformAdapter,
    default_adapters,
)
from archive_management.services.platform_scan import ScanRoots

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("平台适配器"),
    pytest.mark.story("按平台读取游戏与存档候选"),
    pytest.mark.layer("unit"),
]

_LOGGER = "archive_management.services.platform_adapters"
_APP_ID = "753640"


class BrokenRegistry:
    """总是抛错的注册表替身: 模拟探测环境坏掉(权限、注册表损坏)的情况."""

    def values(self, hive: str, subkey: str) -> dict[str, str]:
        raise RuntimeError(f"注册表不可用: {hive}\\{subkey}")

    def subkeys(self, hive: str, subkey: str) -> list[str]:
        raise RuntimeError(f"注册表不可用: {hive}\\{subkey}")


class BrokenCloud:
    """解析必然失败的存档候选来源替身."""

    def candidates(
        self, app_id: str, *, install_dir: Path | None = None
    ) -> list[SavePathCandidate]:
        del install_dir
        raise RuntimeError(f"清单解析失败: {app_id}")


@pytest.fixture
def adapters(tmp_path: Path) -> dict[PlatformId, PlatformAdapter]:
    """一套完整可用的适配器: Steam 的清单与云端同步清单都在临时目录里."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, _APP_ID, "Outer Wilds", "OuterWilds")
    helpers.write_remotecache(steam, _APP_ID, {"OuterWilds/Saves/a.sav": 2})
    return default_adapters(helpers.scan_roots(tmp_path))


# --------------------------------------------------------------- 平台矩阵


def test_every_platform_is_registered_once(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    assert set(adapters) == set(PLATFORM_IDS)
    for platform, adapter in adapters.items():
        assert adapter.platform == platform


def test_only_steam_is_supported_today(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    assert adapters["steam"].supported is True
    assert adapters["steam"].unsupported_reason == ""
    unsupported = {name for name, item in adapters.items() if not item.supported}
    assert unsupported == set(PLATFORM_IDS) - {"steam"}
    assert all(adapters[name].unsupported_reason for name in unsupported)


def test_all_adapters_share_one_contract(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    """契约: 每个适配器都返回自己平台的游戏与候选, 未实现平台只返回空结果."""
    for platform, adapter in adapters.items():
        assert isinstance(adapter, PlatformAdapter)
        games = adapter.list_games()
        assert isinstance(games, list)
        assert all(game.platform == platform for game in games)
        probe = PlatformGame(platform=platform, game_id=_APP_ID, name="Outer Wilds")
        candidates = adapter.save_candidates(probe)
        assert isinstance(candidates, list)
        assert all(item.path and item.reason_code for item in candidates)
        if not adapter.supported:
            assert games == []
            assert candidates == []


def test_adapter_contract_is_verified_at_runtime(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    """``runtime_checkable`` 让调用方(以及上面的契约用例)能做 isinstance 判断."""
    assert isinstance(adapters["steam"], PlatformAdapter)
    assert isinstance(UnsupportedPlatformAdapter("epic"), PlatformAdapter)


# ------------------------------------------------------------------- Steam


def test_games_carry_the_app_id_taken_from_the_manifest(
    tmp_path: Path, adapters: dict[PlatformId, PlatformAdapter]
) -> None:
    games = adapters["steam"].list_games()

    assert [game.game_id for game in games] == [_APP_ID]
    assert games[0].name == "Outer Wilds"
    assert games[0].version == PLATFORM_DATA_FORMAT_VERSION
    assert games[0].install_dir == str(
        tmp_path
        / "Program Files (x86)"
        / "Steam"
        / "steamapps"
        / "common"
        / "OuterWilds"
    )


def test_manifests_without_an_app_id_are_skipped(tmp_path: Path) -> None:
    """没有 AppID 的清单无法与平台数据对应, 不进列表(而不是带着空 id 往下走)."""
    steam = helpers.steam_tree(tmp_path)
    manifest = helpers.write_steam_manifest(steam, _APP_ID, "Outer Wilds", "OuterWilds")
    manifest.rename(manifest.with_name("appmanifest_manual.acf"))

    adapter = default_adapters(helpers.scan_roots(tmp_path))["steam"]

    assert adapter.list_games() == []


def test_a_broken_manifest_only_shrinks_the_list(tmp_path: Path) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, _APP_ID, "Outer Wilds", "OuterWilds")
    (steam / "steamapps" / "appmanifest_999999.acf").write_text(
        "{ not a manifest", encoding="utf-8"
    )

    adapter = default_adapters(helpers.scan_roots(tmp_path))["steam"]

    assert [game.game_id for game in adapter.list_games()] == [_APP_ID]


def test_an_empty_environment_yields_no_games(tmp_path: Path) -> None:
    """Steam 未安装时不去猜路径, 只返回空列表."""
    adapter = default_adapters(helpers.scan_roots(tmp_path))["steam"]

    assert adapter.list_games() == []


def test_save_candidates_come_from_the_cloud_manifest(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    game = adapters["steam"].list_games()[0]

    candidates = adapters["steam"].save_candidates(game)

    assert [item.relative_path for item in candidates] == ["OuterWilds/Saves"]
    assert [item.path_kind for item in candidates] == ["directory"]
    assert candidates[0].detail == "WinMyDocuments"


def test_save_candidates_ignore_data_from_another_platform(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    other = PlatformGame(platform="epic", game_id="celeste", name="Celeste")

    assert adapters["steam"].save_candidates(other) == []


def test_unsupported_adapters_log_the_platform(
    caplog: pytest.LogCaptureFixture,
) -> None:
    adapter = UnsupportedPlatformAdapter("gog")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        assert adapter.list_games() == []

    assert any("gog" in record.getMessage() for record in caplog.records)


def test_unsupported_adapter_keeps_a_custom_reason() -> None:
    adapter = UnsupportedPlatformAdapter("epic", reason="Epic 适配器尚未接入")

    assert adapter.supported is False
    assert adapter.platform == "epic"
    assert adapter.unsupported_reason == "Epic 适配器尚未接入"


# ------------------------------------------------------- 适配器边界不抛异常


def test_a_failing_environment_only_shrinks_the_game_list(tmp_path: Path) -> None:
    """探测环境坏掉(注册表不可用)时适配器只返回空列表, 不把异常抛给界面."""
    helpers.steam_tree(tmp_path)
    roots = helpers.scan_roots(tmp_path, registry=BrokenRegistry())

    assert SteamAdapter(roots).list_games() == []


def test_a_failing_candidate_source_only_shrinks_the_candidates(
    tmp_path: Path,
) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, _APP_ID, "Outer Wilds", "OuterWilds")
    adapter = SteamAdapter(helpers.scan_roots(tmp_path), cloud=BrokenCloud())

    game = adapter.list_games()[0]

    assert adapter.save_candidates(game) == []


def test_default_adapters_reuse_the_same_roots(tmp_path: Path) -> None:
    """适配器与本地探测共用同一套根目录: 传什么环境就用什么环境."""
    roots: ScanRoots = helpers.scan_roots(tmp_path)
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, _APP_ID, "Outer Wilds", "OuterWilds")

    assert [game.game_id for game in default_adapters(roots)["steam"].list_games()] == [
        _APP_ID
    ]
