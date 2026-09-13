"""本地游戏探测服务的单元测试(阶段 E-1).

覆盖 Steam/Epic/GOG/Battle.net 四类来源的探测规则、监控目录扫描、路径健康
判定与去重策略。全部探测都在临时目录里构造"平台目录结构", 注册表用替身注入,
不读写真实注册表, 也不依赖本机是否安装过任何平台客户端。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archive_management.services.platform_scan import (
    HKCU,
    HKLM,
    LocalGameScanner,
    NullRegistry,
    ScanRoots,
    WinRegistry,
    default_roots,
    parse_vdf_pairs,
    path_health,
    vdf_first,
)

pytestmark = [
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("本地游戏探测"),
    pytest.mark.story("扫描本机已安装游戏"),
    pytest.mark.layer("unit"),
]


class FakeRegistry:
    """可注入的注册表替身: 只回答预先登记好的键值."""

    def __init__(
        self,
        *,
        values: dict[tuple[str, str], dict[str, str]] | None = None,
        subkeys: dict[tuple[str, str], list[str]] | None = None,
    ) -> None:
        """按 ``(hive, subkey)`` 登记值与子键."""
        self._values = values or {}
        self._subkeys = subkeys or {}

    def values(self, hive: str, subkey: str) -> dict[str, str]:
        return dict(self._values.get((hive, subkey), {}))

    def subkeys(self, hive: str, subkey: str) -> list[str]:
        return list(self._subkeys.get((hive, subkey), []))


class BrokenRegistry:
    """总是抛错的注册表替身: 验证单个来源失败不会中断整体探测."""

    def values(self, hive: str, subkey: str) -> dict[str, str]:
        raise RuntimeError(f"注册表不可用: {hive}\\{subkey}")

    def subkeys(self, hive: str, subkey: str) -> list[str]:
        raise RuntimeError(f"注册表不可用: {hive}\\{subkey}")


def _roots(tmp_path: Path, registry: object | None = None) -> ScanRoots:
    """构造指向临时目录的探测环境(注册表缺省为空实现)."""
    return ScanRoots(
        program_data=tmp_path / "ProgramData",
        program_files=tmp_path / "Program Files",
        program_files_x86=tmp_path / "Program Files (x86)",
        local_app_data=tmp_path / "Local",
        user_profile=tmp_path,
        registry=registry if registry is not None else NullRegistry(),  # type: ignore[arg-type]
    )


def _steam_tree(tmp_path: Path, *, libraries: int = 1) -> tuple[Path, list[Path]]:
    """在临时目录里造出 Steam 主目录与 ``libraries-1`` 个附加库目录."""
    steam = tmp_path / "Steam"
    (steam / "steamapps" / "common").mkdir(parents=True)
    roots = [steam]
    for index in range(1, libraries):
        extra = tmp_path / f"SteamLib{index}"
        (extra / "steamapps" / "common").mkdir(parents=True)
        roots.append(extra)
    return steam, roots


def _write_manifest(library: Path, appid: str, name: str, installdir: str) -> None:
    """写入一个 Steam 应用清单(appmanifest)."""
    (library / "steamapps" / f"appmanifest_{appid}.acf").write_text(
        '"AppState"\n{\n'
        f'\t"appid"\t\t"{appid}"\n'
        f'\t"name"\t\t"{name}"\n'
        f'\t"installdir"\t\t"{installdir}"\n'
        "}\n",
        encoding="utf-8",
    )


def _write_libraryfolders(steam: Path, libraries: list[Path]) -> None:
    """写入 libraryfolders.vdf, 登记全部库目录."""
    body = '"libraryfolders"\n{\n'
    for index, library in enumerate(libraries):
        escaped = str(library).replace("\\", "\\\\")
        body += f'\t"{index}"\n\t{{\n\t\t"path"\t\t"{escaped}"\n\t}}\n'
    body += "}\n"
    (steam / "steamapps" / "libraryfolders.vdf").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------- VDF


def test_parse_vdf_pairs_reads_flat_key_values() -> None:
    pairs = parse_vdf_pairs('"name" "Outer Wilds"\n"installdir" "OuterWilds"')
    assert pairs == [("name", "Outer Wilds"), ("installdir", "OuterWilds")]
    assert vdf_first(pairs, "NAME") == "Outer Wilds"
    assert vdf_first(pairs, "missing") is None


# ------------------------------------------------------------------- Steam


def test_steam_manifests_from_all_library_folders_are_discovered(
    tmp_path: Path,
) -> None:
    steam, libraries = _steam_tree(tmp_path, libraries=2)
    _write_manifest(libraries[0], "753640", "Outer Wilds", "OuterWilds")
    _write_manifest(libraries[1], "1145360", "Hades", "Hades")
    _write_libraryfolders(steam, libraries)
    registry = FakeRegistry(
        values={(HKCU, r"Software\Valve\Steam"): {"SteamPath": str(steam)}}
    )

    candidates = LocalGameScanner(_roots(tmp_path, registry)).scan()

    by_name = {item.name: item for item in candidates}
    assert set(by_name) == {"Outer Wilds", "Hades"}
    assert by_name["Outer Wilds"].source == "steam"
    assert by_name["Outer Wilds"].confidence == "high"
    assert by_name["Outer Wilds"].reason_code == "steam_manifest"
    assert by_name["Hades"].install_dir == str(
        libraries[1] / "steamapps" / "common" / "Hades"
    )


def test_steam_without_registry_entry_yields_nothing(tmp_path: Path) -> None:
    _steam_tree(tmp_path)
    assert LocalGameScanner(_roots(tmp_path)).scan() == []


def test_broken_registry_does_not_break_other_sources(tmp_path: Path) -> None:
    """注册表不可读时仍能从清单目录发现游戏(探测失败不阻断其它来源)."""
    epic_manifests = (
        tmp_path / "ProgramData" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    )
    epic_manifests.mkdir(parents=True)
    (epic_manifests / "celeste.item").write_text(
        json.dumps({"DisplayName": "Celeste", "InstallLocation": str(tmp_path)}),
        encoding="utf-8",
    )

    candidates = LocalGameScanner(_roots(tmp_path, BrokenRegistry())).scan()

    assert [item.name for item in candidates] == ["Celeste"]
    assert candidates[0].source == "epic"


# -------------------------------------------------------------------- Epic


def test_epic_manifests_are_discovered_and_broken_files_skipped(
    tmp_path: Path,
) -> None:
    manifests = (
        tmp_path / "ProgramData" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    )
    manifests.mkdir(parents=True)
    (manifests / "celeste.item").write_text(
        json.dumps({"DisplayName": "Celeste", "InstallLocation": str(tmp_path)}),
        encoding="utf-8",
    )
    (manifests / "broken.item").write_text("{not json", encoding="utf-8")
    (manifests / "incomplete.item").write_text(
        json.dumps({"DisplayName": "NoLocation"}), encoding="utf-8"
    )

    candidates = LocalGameScanner(_roots(tmp_path)).scan()

    assert [item.name for item in candidates] == ["Celeste"]
    assert candidates[0].reason_code == "epic_manifest"
    assert candidates[0].detail == "celeste.item"


# --------------------------------------------------------------- 注册表来源


def test_gog_and_battlenet_registry_entries_are_discovered(tmp_path: Path) -> None:
    gog_root = r"SOFTWARE\WOW6432Node\GOG.com\Games"
    bnet_root = r"SOFTWARE\WOW6432Node\Blizzard Entertainment"
    registry = FakeRegistry(
        values={
            (HKLM, f"{gog_root}\\1207664623"): {
                "gameName": "The Witcher 3",
                "path": str(tmp_path / "witcher"),
            },
            (HKLM, f"{bnet_root}\\Overwatch"): {
                "InstallLocation": str(tmp_path / "overwatch")
            },
        },
        subkeys={(HKLM, gog_root): ["1207664623"], (HKLM, bnet_root): ["Overwatch"]},
    )

    candidates = LocalGameScanner(_roots(tmp_path, registry)).scan()

    sources = {item.name: item.source for item in candidates}
    assert sources == {"The Witcher 3": "gog", "Overwatch": "battle_net"}
    assert all(item.confidence == "high" for item in candidates)


def test_win_registry_degrades_to_empty_results() -> None:
    """真实注册表读取器对不存在的键返回空结果, 不抛异常."""
    reader = WinRegistry()
    assert reader.values(HKLM, r"SOFTWARE\\DefinitelyMissing\\ArchiveManagement") == {}
    assert reader.subkeys(HKCU, r"SOFTWARE\\DefinitelyMissing\\ArchiveManagement") == []
    assert reader.values("HKXX", "any") == {}


def test_default_roots_reads_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PROGRAMDATA", r"X:\ProgramData")
    monkeypatch.setenv("LOCALAPPDATA", r"X:\Local")
    roots = default_roots(registry=NullRegistry())
    assert roots.program_data == Path(r"X:\ProgramData")
    assert roots.local_app_data == Path(r"X:\Local")


# --------------------------------------------------------------- 监控目录


def test_monitored_children_become_candidates(tmp_path: Path) -> None:
    games = tmp_path / "Games"
    (games / "Stardew").mkdir(parents=True)
    (games / "Factorio").mkdir()
    (games / ".hidden").mkdir()
    (games / "notes.txt").write_text("x", encoding="utf-8")

    candidates = LocalGameScanner(_roots(tmp_path)).scan(monitored=[str(games)])

    assert [item.name for item in candidates] == ["Factorio", "Stardew"]
    assert all(item.source == "monitored" for item in candidates)
    assert all(item.confidence == "medium" for item in candidates)
    assert candidates[0].reason_code == "monitored_child"
    assert candidates[0].detail == str(games)


def test_monitored_directory_without_children_is_itself_a_candidate(
    tmp_path: Path,
) -> None:
    lone = tmp_path / "LoneGame"
    lone.mkdir()

    candidates = LocalGameScanner(_roots(tmp_path)).scan(monitored=[str(lone)])

    assert [item.name for item in candidates] == ["LoneGame"]
    assert candidates[0].reason_code == "monitored_root"


def test_monitored_directory_that_disappeared_is_ignored(tmp_path: Path) -> None:
    missing = tmp_path / "gone"
    assert LocalGameScanner(_roots(tmp_path)).scan(monitored=[str(missing)]) == []


def test_same_path_keeps_the_more_trusted_source(tmp_path: Path) -> None:
    """平台清单与监控目录指向同一路径时只保留一条, 且优先平台清单."""
    steam, libraries = _steam_tree(tmp_path)
    _write_manifest(libraries[0], "753640", "Outer Wilds", "OuterWilds")
    common = libraries[0] / "steamapps" / "common"
    (common / "OuterWilds").mkdir()
    registry = FakeRegistry(
        values={(HKCU, r"Software\Valve\Steam"): {"SteamPath": str(steam)}}
    )

    candidates = LocalGameScanner(_roots(tmp_path, registry)).scan(
        monitored=[str(common)]
    )

    assert len(candidates) == 1
    assert candidates[0].source == "steam"


# --------------------------------------------------------------- 路径健康


def test_path_health_detects_missing_file_and_directory(tmp_path: Path) -> None:
    assert path_health(str(tmp_path)) == "ok"
    assert path_health(str(tmp_path / "nope")) == "missing"
    saved = tmp_path / "save.dat"
    saved.write_text("x", encoding="utf-8")
    assert path_health(str(saved)) == "not_directory"


def test_path_health_rejects_dangerous_locations() -> None:
    assert path_health(str(Path.cwd().anchor)) == "unsafe"
    assert path_health(str(Path.home())) == "unsafe"


def test_missing_install_directory_is_reported_as_missing(tmp_path: Path) -> None:
    epic_manifests = (
        tmp_path / "ProgramData" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    )
    epic_manifests.mkdir(parents=True)
    (epic_manifests / "gone.item").write_text(
        json.dumps(
            {"DisplayName": "Uninstalled", "InstallLocation": str(tmp_path / "gone")}
        ),
        encoding="utf-8",
    )

    candidates = LocalGameScanner(_roots(tmp_path)).scan()

    assert [item.health for item in candidates] == ["missing"]
