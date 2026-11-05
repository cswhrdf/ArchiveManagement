"""平台适配器的契约测试.

本模块的约定是"同一套断言跑所有适配器": 本模块用一个用例遍历
``default_adapters`` 里的每个平台, 检查标识、支持标记、返回类型与"不抛异常"的
降级行为; 再用 Steam 的用例验证真实链路(应用清单 → ``PlatformGame``, 云端清单
→ 存档候选), 以及清单损坏、平台数据错配时的降级。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import archive_management.services.platform_adapters as adapters_mod
import helpers
from archive_management.domain import (
    PLATFORM_DATA_FORMAT_VERSION,
    PLATFORM_IDS,
    PlatformGame,
    PlatformId,
    SavePathCandidate,
)
from archive_management.services.platform_adapters import (
    EpicAdapter,
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
# Epic 的游戏标识(平台自己的 AppName, 真实启动器里是一段 GUID 文本).
_EPIC_APP_NAME = "EpicCeleste"
# 一款游戏的官方图标哈希(appinfo.vdf 的 clienticon).
_ICON_HASH = "b2f863a4c63bc1c5667a8a7e3e9355ef260ce6d2"


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


def test_only_steam_and_epic_are_supported_today(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    """Steam 三项全支持, Epic 到存档路径推断; 其余平台必须显式说明原因."""
    assert adapters["steam"].supported is True
    assert adapters["steam"].unsupported_reason == ""
    assert adapters["epic"].supported is True
    assert adapters["epic"].unsupported_reason == ""
    assert adapters["epic"].supports_save_paths is True
    assert adapters["epic"].supports_artwork is False
    unsupported = {name for name, item in adapters.items() if not item.supported}
    assert unsupported == set(PLATFORM_IDS) - {"steam", "epic"}
    assert all(adapters[name].unsupported_reason for name in unsupported)


def test_all_adapters_share_one_contract(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    """契约: 每个适配器都返回自己平台的游戏、候选与图片引用."""
    for platform, adapter in adapters.items():
        assert isinstance(adapter, PlatformAdapter)
        games = adapter.list_games()
        assert isinstance(games, list)
        assert all(game.platform == platform for game in games)
        probe = PlatformGame(platform=platform, game_id=_APP_ID, name="Outer Wilds")
        candidates = adapter.save_candidates(probe)
        assert isinstance(candidates, list)
        assert all(item.path and item.reason_code for item in candidates)
        refs = adapter.artwork_refs(probe)
        assert isinstance(refs, tuple)
        assert all(reference.kind and reference.version for reference in refs)
        if not adapter.supported:
            assert games == []
            assert candidates == []
            assert refs == ()
            assert adapter.supports_save_paths is False
            assert adapter.supports_artwork is False


def test_steam_declares_capabilities_and_artwork_refs(
    adapters: dict[PlatformId, PlatformAdapter],
) -> None:
    """Steam 声明支持存档探测与取图, 并按公开 CDN 规则给出封面引用."""
    steam = adapters["steam"]
    game = steam.list_games()[0]

    assert steam.supports_save_paths is True
    assert steam.supports_artwork is True
    refs = steam.artwork_refs(game)
    assert [item.kind for item in refs] == ["cover"]
    assert _APP_ID in refs[0].url


def test_steam_offers_the_official_icon_when_appinfo_knows_it(tmp_path: Path) -> None:
    """官方图标: 本机已有 ico 时给本地路径, 没有时给 CDN 上的同哈希地址."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, _APP_ID, "Outer Wilds", "OuterWilds")
    helpers.write_steam_appinfo(
        steam, {_APP_ID: {"name": "Outer Wilds", "clienticon": _ICON_HASH}}
    )
    local = helpers.write_steam_icon(steam, _ICON_HASH)
    adapter = SteamAdapter(helpers.scan_roots(tmp_path))
    game = PlatformGame(platform="steam", game_id=_APP_ID, name="Outer Wilds")

    refs = adapter.artwork_refs(game)

    assert [item.kind for item in refs] == ["cover", "icon"]
    assert refs[1].version == _ICON_HASH
    assert refs[1].local_path == str(local)
    assert refs[1].url.endswith(f"/{_APP_ID}/{_ICON_HASH}.ico")


def test_steam_offers_only_the_cover_without_appinfo(tmp_path: Path) -> None:
    """读不到 appinfo(未装、版本不认识)时只给封面: 后台裁封面兜底."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, _APP_ID, "Outer Wilds", "OuterWilds")
    adapter = SteamAdapter(helpers.scan_roots(tmp_path))
    game = PlatformGame(platform="steam", game_id=_APP_ID, name="Outer Wilds")

    refs = adapter.artwork_refs(game)

    assert [item.kind for item in refs] == ["cover"]


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


# --------------------------------------------------------------------- Epic


def _epic_environment(tmp_path: Path) -> Path:
    """造出"安装清单 + 云存档缓存 + 真实存档目录", 返回安装目录."""
    install = tmp_path / "Games" / "Celeste"
    install.mkdir(parents=True)
    helpers.write_epic_item(
        helpers.epic_tree(tmp_path), _EPIC_APP_NAME, "Celeste", install
    )
    (tmp_path / "Local" / "Celeste" / "Saves").mkdir(parents=True)
    helpers.write_epic_cloud_cache(
        tmp_path / "Local", _EPIC_APP_NAME, "${AppData}/Celeste/Saves/"
    )
    return install


def test_epic_games_come_from_the_install_manifests(tmp_path: Path) -> None:
    """游戏列表来自 ``*.item`` 清单; AppName 就是平台标识, 缺它的旧清单跳过."""
    install = _epic_environment(tmp_path)
    helpers.write_epic_item(
        helpers.epic_tree(tmp_path),
        "OldStyle",
        "Old Style",
        tmp_path / "Games" / "OldStyle",
        with_app_name=False,
    )

    games = EpicAdapter(helpers.scan_roots(tmp_path)).list_games()

    assert [(game.game_id, game.name, game.install_dir) for game in games] == [
        (_EPIC_APP_NAME, "Celeste", str(install))
    ]
    assert all(game.platform == "epic" for game in games)


def test_epic_save_candidates_come_from_the_cloud_cache(tmp_path: Path) -> None:
    install = _epic_environment(tmp_path)
    adapter = EpicAdapter(helpers.scan_roots(tmp_path))

    candidates = adapter.save_candidates(adapter.list_games()[0])

    assert [item.path for item in candidates] == [
        str(tmp_path / "Local" / "Celeste" / "Saves")
    ]
    assert [item.reason_code for item in candidates] == ["epic_cloudcache"]
    assert candidates[0].confidence == "medium"
    assert install.is_dir()


def test_epic_offers_no_artwork(tmp_path: Path) -> None:
    """Epic 没有公开的本机封面/图标清单: 声明不支持并返回空引用."""
    adapter = EpicAdapter(helpers.scan_roots(tmp_path))
    game = PlatformGame(platform="epic", game_id=_EPIC_APP_NAME, name="Celeste")

    assert adapter.supports_artwork is False
    assert adapter.artwork_refs(game) == ()


def test_epic_save_candidates_ignore_data_from_another_platform(
    tmp_path: Path,
) -> None:
    other = PlatformGame(platform="steam", game_id=_APP_ID, name="Outer Wilds")

    assert EpicAdapter(helpers.scan_roots(tmp_path)).save_candidates(other) == []


def test_a_failing_epic_environment_only_shrinks_the_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """清单读不出来时只少条目; 缓存解析炸掉时只少候选, 都不抛给界面."""

    def broken(_roots: ScanRoots) -> list[object]:
        raise RuntimeError("清单目录不可读")

    monkeypatch.setattr(adapters_mod, "read_epic_items", broken)
    adapter = EpicAdapter(helpers.scan_roots(tmp_path))

    assert adapter.list_games() == []

    monkeypatch.undo()
    _epic_environment(tmp_path)
    game = PlatformGame(platform="epic", game_id=_EPIC_APP_NAME, name="Celeste")
    failing = EpicAdapter(helpers.scan_roots(tmp_path), cloud=BrokenCloud())

    assert failing.save_candidates(game) == []


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
