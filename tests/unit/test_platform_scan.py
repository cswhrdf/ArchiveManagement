"""本地游戏探测服务的单元测试.

覆盖 Steam/Epic/GOG/Ubisoft 四类来源的探测规则、监控目录扫描、路径健康
判定与去重策略, 以及 Windows/macOS/Linux 三个平台的差异: 注册表只存在于
Windows, Steam/Epic 各平台的清单目录不同, 监控目录在所有平台都可用。
全部探测都在临时目录里构造"平台目录结构", 注册表用替身注入, 不读写真实
注册表, 也不依赖本机是否安装过任何平台客户端。
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from pathlib import Path

import pytest

from archive_management.services.platform_scan import (
    HKCU,
    HKLM,
    LocalGameScanner,
    NullRegistry,
    RegistryReader,
    ScanRoots,
    WinRegistry,
    default_roots,
    parse_vdf_pairs,
    path_health,
    read_steam_installs,
    registry_paths,
    steam_libraries,
    vdf_first,
)
from archive_management.services.platforms import PlatformFamily

pytestmark = [
    pytest.mark.normal,
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


def _roots(
    tmp_path: Path,
    registry: RegistryReader | None = None,
    *,
    platform: PlatformFamily = "windows",
) -> ScanRoots:
    """构造指向临时目录的探测环境(注册表缺省为空实现).

    ``platform`` 决定使用哪套目录规则: 默认按 Windows 构造, 非 Windows 平台的
    用例显式传入 ``macos``/``linux``。
    """
    return ScanRoots(
        platform=platform,
        program_data=tmp_path / "ProgramData",
        program_files=tmp_path / "Program Files",
        program_files_x86=tmp_path / "Program Files (x86)",
        local_app_data=tmp_path / "Local",
        user_profile=tmp_path,
        registry=registry if registry is not None else NullRegistry(),
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


def test_gog_and_ubisoft_registry_entries_are_discovered(tmp_path: Path) -> None:
    gog_root = r"SOFTWARE\WOW6432Node\GOG.com\Games"
    ubisoft_root = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"
    registry = FakeRegistry(
        values={
            (HKLM, f"{gog_root}\\1207664623"): {
                "gameName": "The Witcher 3",
                "path": str(tmp_path / "witcher"),
            },
            # Ubisoft 只保证 InstallDir 有值: 没有游戏名时退回目录名.
            (HKLM, f"{ubisoft_root}\\1000"): {
                "InstallDir": str(tmp_path / "Anno 1800")
            },
        },
        subkeys={(HKLM, gog_root): ["1207664623"], (HKLM, ubisoft_root): ["1000"]},
    )

    candidates = LocalGameScanner(_roots(tmp_path, registry)).scan()

    sources = {item.name: item.source for item in candidates}
    assert sources == {"The Witcher 3": "gog", "Anno 1800": "ubisoft"}
    assert all(item.confidence == "high" for item in candidates)


def test_ubisoft_prefers_game_name_over_folder_name(tmp_path: Path) -> None:
    """Ubisoft 子键里有游戏名时用它, 并记录安装 id 供界面解释来源."""
    root = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"
    registry = FakeRegistry(
        values={
            (HKLM, f"{root}\\2000"): {
                "InstallDir": str(tmp_path / "AC Valhalla"),
                "GameName": "Assassin's Creed Valhalla",
            },
            # 缺 InstallDir 的子键没有可用安装目录, 整条跳过.
            (HKLM, f"{root}\\2001"): {"GameName": "No Location"},
        },
        subkeys={(HKLM, root): ["2000", "2001"]},
    )

    candidates = LocalGameScanner(_roots(tmp_path, registry)).scan()

    assert [item.name for item in candidates] == ["Assassin's Creed Valhalla"]
    assert candidates[0].reason_code == "ubisoft_registry"
    assert candidates[0].detail == "2000"


def test_win_registry_degrades_to_empty_results() -> None:
    """真实注册表读取器对不存在的键返回空结果, 不抛异常."""
    reader = WinRegistry()
    assert reader.values(HKLM, r"SOFTWARE\\DefinitelyMissing\\ArchiveManagement") == {}
    assert reader.subkeys(HKCU, r"SOFTWARE\\DefinitelyMissing\\ArchiveManagement") == []
    assert reader.values("HKXX", "any") == {}


def test_default_roots_windows_reads_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PROGRAMDATA", r"X:\ProgramData")
    monkeypatch.setenv("LOCALAPPDATA", r"X:\Local")
    roots = default_roots(registry=NullRegistry(), platform="windows")
    assert roots.platform == "windows"
    assert roots.program_data == Path(r"X:\ProgramData")
    assert roots.local_app_data == Path(r"X:\Local")


def test_default_roots_windows_uses_the_registry_reader() -> None:
    """注册表只存在于 Windows, 因此只有这一平台默认启用真实读取器."""
    roots = default_roots(platform="windows", env={})
    assert isinstance(roots.registry, WinRegistry)


def test_default_roots_macos_uses_library_and_shared_dirs() -> None:
    """macOS 的应用数据在 ``~/Library/Application Support``, 且没有注册表."""
    roots = default_roots(platform="macos", env={"HOME": "/Users/demo"})

    assert roots.platform == "macos"
    assert roots.user_profile == Path("/Users/demo")
    assert roots.local_app_data == Path("/Users/demo/Library/Application Support")
    assert roots.shared_root == Path("/Users/Shared")
    assert isinstance(roots.registry, NullRegistry)


def test_default_roots_linux_uses_xdg_dirs() -> None:
    """Linux 用 XDG 目录, 没有共享目录概念, 也没有注册表."""
    roots = default_roots(platform="linux", env={"HOME": "/home/demo"})

    assert roots.platform == "linux"
    assert roots.user_profile == Path("/home/demo")
    assert roots.local_app_data == Path("/home/demo/.local/share")
    assert roots.shared_root == roots.program_data
    assert isinstance(roots.registry, NullRegistry)

    custom = default_roots(
        platform="linux", env={"HOME": "/home/demo", "XDG_DATA_HOME": "/data"}
    )
    assert custom.local_app_data == Path("/data")


# --------------------------------------------------------------- 平台适配


def test_macos_steam_is_found_in_application_support(tmp_path: Path) -> None:
    support = tmp_path / "Library" / "Application Support"
    steam = support / "Steam"
    (steam / "steamapps" / "common").mkdir(parents=True)
    _write_manifest(steam, "753640", "Outer Wilds", "OuterWilds")

    roots = replace(
        _roots(tmp_path, platform="macos"),
        local_app_data=support,
        user_profile=tmp_path,
    )

    candidates = LocalGameScanner(roots).scan()

    assert [item.name for item in candidates] == ["Outer Wilds"]
    assert candidates[0].source == "steam"
    assert candidates[0].install_dir == str(
        steam / "steamapps" / "common" / "OuterWilds"
    )


def test_linux_steam_is_found_in_native_and_flatpak_locations(tmp_path: Path) -> None:
    """Linux 上 Steam 可能来自原生安装, 也可能被封进 Flatpak 沙箱."""
    flatpak = tmp_path / ".var" / "app" / "com.valvesoftware.Steam" / "data" / "Steam"
    (flatpak / "steamapps" / "common").mkdir(parents=True)
    _write_manifest(flatpak, "1145360", "Hades", "Hades")
    native = tmp_path / ".local" / "share" / "Steam"
    (native / "steamapps" / "common").mkdir(parents=True)
    _write_manifest(native, "646570", "Slay the Spire", "SlayTheSpire")

    roots = replace(
        _roots(tmp_path, platform="linux"),
        local_app_data=tmp_path / ".local" / "share",
    )

    candidates = LocalGameScanner(roots).scan()

    assert [item.name for item in candidates] == ["Hades", "Slay the Spire"]
    assert all(item.source == "steam" for item in candidates)


def test_windows_steam_falls_back_to_the_default_install_dir(tmp_path: Path) -> None:
    """注册表没有 Steam 登记时, 仍能发现默认安装目录下的库(例如手动安装)."""
    steam = tmp_path / "Program Files (x86)" / "Steam"
    (steam / "steamapps" / "common").mkdir(parents=True)
    _write_manifest(steam, "753640", "Outer Wilds", "OuterWilds")

    candidates = LocalGameScanner(_roots(tmp_path)).scan()

    assert [item.name for item in candidates] == ["Outer Wilds"]


@pytest.mark.parametrize("platform", ["macos", "linux"])
def test_registry_sources_are_windows_only(
    tmp_path: Path, platform: PlatformFamily
) -> None:
    """macOS/Linux 没有注册表: 即使替身能回答, 也不应产生 GOG/Ubisoft 候选."""
    gog_root = r"SOFTWARE\WOW6432Node\GOG.com\Games"
    ubisoft_root = r"SOFTWARE\WOW6432Node\Ubisoft\Launcher\Installs"
    registry = FakeRegistry(
        values={
            (HKLM, f"{gog_root}\\1207664623"): {
                "gameName": "The Witcher 3",
                "path": str(tmp_path / "witcher"),
            },
            (HKLM, f"{ubisoft_root}\\1000"): {
                "InstallDir": str(tmp_path / "Anno 1800")
            },
        },
        subkeys={(HKLM, gog_root): ["1207664623"], (HKLM, ubisoft_root): ["1000"]},
    )

    candidates = LocalGameScanner(_roots(tmp_path, registry, platform=platform)).scan()

    assert candidates == []


def test_epic_manifests_are_found_in_the_macos_shared_dir(tmp_path: Path) -> None:
    manifests = tmp_path / "Epic Games" / "EpicGamesLauncher" / "Data" / "Manifests"
    manifests.mkdir(parents=True)
    (manifests / "celeste.item").write_text(
        json.dumps({"DisplayName": "Celeste", "InstallLocation": str(tmp_path)}),
        encoding="utf-8",
    )

    roots = replace(_roots(tmp_path, platform="macos"), user_shared=tmp_path)
    candidates = LocalGameScanner(roots).scan()

    assert [item.name for item in candidates] == ["Celeste"]
    assert candidates[0].source == "epic"


def test_epic_source_is_skipped_on_linux(tmp_path: Path) -> None:
    """Linux 没有官方 Epic 启动器: 该来源不做无意义的目录猜测."""
    manifests = tmp_path / "Local" / "Epic" / "EpicGamesLauncher" / "Data" / "Manifests"
    manifests.mkdir(parents=True)
    (manifests / "celeste.item").write_text(
        json.dumps({"DisplayName": "Celeste", "InstallLocation": str(tmp_path)}),
        encoding="utf-8",
    )

    assert LocalGameScanner(_roots(tmp_path, platform="linux")).scan() == []


@pytest.mark.parametrize("platform", ["windows", "macos", "linux"])
def test_monitored_directories_work_on_every_platform(
    tmp_path: Path, platform: PlatformFamily
) -> None:
    games = tmp_path / "Games"
    (games / "Stardew").mkdir(parents=True)

    candidates = LocalGameScanner(_roots(tmp_path, platform=platform)).scan(
        monitored=[str(games)]
    )

    assert [item.name for item in candidates] == ["Stardew"]
    assert candidates[0].source == "monitored"


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


# ------------------------------------------------------- 健康判定与清单读取


def test_path_health_reports_an_unreadable_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录在但读不了: 归到 unreadable(而不是 ok), 界面才能提示去改权限."""
    target = tmp_path / "saves"
    target.mkdir()
    real_access = os.access

    def deny(path: str | Path, mode: int) -> bool:
        return False if Path(path) == target else real_access(path, mode)

    monkeypatch.setattr(os, "access", deny)

    assert path_health(str(target)) == "unreadable"


def test_registry_paths_are_empty_off_windows(tmp_path: Path) -> None:
    """注册表读取只在 Windows 生效: 其它平台显式返回空, 不假装查过."""
    roots = _roots(tmp_path, platform="linux")

    assert registry_paths(roots, HKCU, r"Software\Valve\Steam", ("SteamPath",)) == []


def test_steam_libraries_skips_an_empty_registered_path(tmp_path: Path) -> None:
    """libraryfolders.vdf 里登记的路径是空串时跳过(不产出一条不可用的库)."""
    steam, _libraries = _steam_tree(tmp_path)
    extra = tmp_path / "SteamLib"
    (extra / "steamapps").mkdir(parents=True)
    (steam / "steamapps" / "libraryfolders.vdf").write_text(
        f'"path" ""\n"1" "{extra.as_posix()}"\n', encoding="utf-8"
    )

    assert steam_libraries(steam) == [steam, extra]


def test_steam_installs_skip_a_manifest_that_cannot_be_read(tmp_path: Path) -> None:
    """清单读不出内容(这里是同名目录)时跳过该条, 不影响其它游戏."""
    steam, _libraries = _steam_tree(tmp_path)
    _write_manifest(steam, "753640", "Outer Wilds", "OuterWilds")
    (steam / "steamapps" / "appmanifest_000000.acf").mkdir()
    registry = FakeRegistry(
        values={(HKCU, r"Software\Valve\Steam"): {"SteamPath": str(steam)}}
    )

    installs = read_steam_installs(_roots(tmp_path, registry))

    assert [item.name for item in installs] == ["Outer Wilds"]


def test_read_json_returns_none_for_a_missing_or_non_object_file(
    tmp_path: Path,
) -> None:
    """JSON 读不出来或顶层不是对象时返回 None(该来源整条跳过)."""
    listed = tmp_path / "list.item"
    listed.write_text("[1, 2]", encoding="utf-8")

    assert LocalGameScanner._read_json(tmp_path / "gone.item") is None
    assert LocalGameScanner._read_json(listed) is None


def test_gog_entries_without_a_path_are_skipped(tmp_path: Path) -> None:
    """GOG 子键缺安装路径时跳过(不产出一条没有目录的候选)."""
    gog_root = r"SOFTWARE\WOW6432Node\GOG.com\Games"
    registry = FakeRegistry(
        values={(HKLM, f"{gog_root}\\42"): {"gameName": "Demo"}},
        subkeys={(HKLM, gog_root): ["42"]},
    )

    assert LocalGameScanner(_roots(tmp_path, registry)).scan() == []
