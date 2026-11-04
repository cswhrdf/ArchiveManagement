"""游戏进程探测的单元测试.

用注入的进程名提供者覆盖匹配、降级与多候选行为; 最后一条用真实
``psutil`` 验证默认提供者可用(断言宽松, 不依赖具体进程名)。
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from archive_management.domain import Game
from archive_management.domain.activation import build_name_ownership, process_names
from archive_management.services.processes import (
    _executable_size,
    install_process_candidates,
    probe_game_process,
    probe_games,
    probe_processes,
    psutil_process_names,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("进程探测"),
    pytest.mark.story("恢复前检查游戏进程"),
    pytest.mark.layer("unit"),
]


def test_probing_the_whole_library_without_needles_skips_the_process_table() -> None:
    """库里没有可监控的游戏时不枚举进程表(没有判断依据就不该付这份开销)."""

    def explode() -> list[str]:
        raise AssertionError("不该枚举进程表")

    probe = probe_games(build_name_ownership([]), provider=explode)

    assert probe.checked is False
    assert probe.running_ids == ()


def test_process_names_skips_entries_without_a_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """psutil 报出的进程没有名字时跳过它: 取不到名字的进程不该进匹配集合."""
    fake = SimpleNamespace(
        process_iter=lambda _attrs: [
            SimpleNamespace(info={"name": "game.exe"}),
            SimpleNamespace(info=None),
            SimpleNamespace(info={"name": None}),
        ]
    )
    monkeypatch.setitem(sys.modules, "psutil", fake)

    assert list(psutil_process_names()) == ["game.exe"]


def test_probe_matches_process_with_different_spelling() -> None:
    probe = probe_game_process("Outer Wilds", provider=lambda: ["OuterWilds.exe"])

    assert probe.checked is True
    assert probe.running is True
    assert probe.matches == ("OuterWilds.exe",)


def test_probe_reports_not_running_for_unrelated_processes() -> None:
    probe = probe_game_process("Demo", provider=lambda: ["explorer.exe", "svn.exe"])

    assert probe.checked is True
    assert probe.running is False
    assert probe.matches == ()


def test_probe_ignores_too_short_names() -> None:
    probe = probe_game_process("ab", provider=lambda: ["ab.exe"])

    assert probe.checked is False
    assert probe.running is False


def test_probe_strips_macos_app_bundle_suffix() -> None:
    """macOS 的应用包名(``.app``)与 Windows 的 ``.exe`` 一样要在比较前去掉."""
    probe = probe_game_process("Outer Wilds", provider=lambda: ["OuterWilds.app"])

    assert probe.checked is True
    assert probe.running is True
    assert probe.matches == ("OuterWilds.app",)


def test_probe_degrades_when_provider_fails() -> None:
    def broken() -> list[str]:
        raise OSError("无法枚举进程")

    probe = probe_game_process("Demo", provider=broken)

    assert probe.checked is False
    assert probe.running is False


def test_probe_processes_checks_every_candidate_once() -> None:
    calls: list[int] = []

    def provider() -> list[str]:
        calls.append(1)
        return ["OuterWilds.exe", "OtherGame.exe"]

    probe = probe_processes(["星际拓荒", "OuterWilds"], provider=provider)

    assert calls == [1]
    assert probe.running is True
    assert probe.matches == ("OuterWilds.exe",)


def test_install_candidates_seed_directory_name_and_largest_executables(
    tmp_path: Path,
) -> None:
    """播种 = 安装目录名 + 顶层可执行按大小降序; 崩溃处理器与过短名被滤掉."""
    install = tmp_path / "Oddsparks"
    install.mkdir()
    (install / "OddsparksGame.exe").write_bytes(b"x" * 40)
    (install / "Launcher.exe").write_bytes(b"x" * 20)
    (install / "UnityCrashHandler64.exe").write_bytes(b"x" * 60)
    (install / "ab.exe").write_bytes(b"x" * 30)
    # 名字带逗号的 exe 存不进拼接列(v16), 播种前就要滤掉.
    (install / "Foo,Bar.exe").write_bytes(b"x" * 50)

    candidates = install_process_candidates(install, "windows")

    assert candidates == ("Oddsparks", "OddsparksGame.exe", "Launcher.exe")


def test_install_candidates_are_capped_at_five(tmp_path: Path) -> None:
    """超上限时按"目录名 + 最大的可执行"截断: 候选越多跨游戏撞针风险越大."""
    install = tmp_path / "Bundle"
    install.mkdir()
    for index in range(7):
        (install / f"Tool{index}.exe").write_bytes(b"x" * ((index + 1) * 10))

    candidates = install_process_candidates(install, "windows")

    assert candidates == ("Bundle", "Tool6.exe", "Tool5.exe", "Tool4.exe", "Tool3.exe")


def test_install_candidates_skip_comma_names_and_missing_directories(
    tmp_path: Path,
) -> None:
    """含逗号的名字存不进拼接列(v16); 目录不存在时只剩目录名(健康由发现层判定)."""
    comma = tmp_path / "My, Game"
    comma.mkdir()

    assert install_process_candidates(comma, "windows") == ()
    assert install_process_candidates(tmp_path / "Gone", "windows") == ("Gone",)


def test_install_candidates_only_take_the_directory_name_on_linux(
    tmp_path: Path,
) -> None:
    """Linux 没有可靠的可执行扩展名约定: 只取目录名, 不做目录扫描."""
    install = tmp_path / "Hades"
    install.mkdir()
    (install / "Hades.exe").write_bytes(b"x" * 10)

    assert install_process_candidates(install, "linux") == ("Hades",)


def test_install_candidates_take_app_bundles_on_macos(tmp_path: Path) -> None:
    """macOS 取顶层的 ``.app`` 应用包名(进程表里报的就是它)."""
    install = tmp_path / "Hades"
    install.mkdir()
    (install / "Hades.app").mkdir()
    (install / "CrashReportClient.app").mkdir()

    assert install_process_candidates(install, "macos") == ("Hades", "Hades.app")


def test_executable_size_degrades_to_last_place_when_stat_fails() -> None:
    """取不到大小(文件刚好消失)时按 -1 参与排序: 播种不因个别文件失败."""
    assert _executable_size(Path("Z:/definitely/not/here.exe")) == -1


def test_seeded_process_names_join_the_matching_candidates() -> None:
    """播种的候选名参与匹配: 界面名与原始名都漏时靠它识别(B 条的主场景)."""
    game = Game(
        id=7,
        name="星露谷物语",
        original_name="Stardew Valley",
        process_names=("Stardew Valley", "StardewModdingAPI.exe"),
    )

    assert process_names(game) == (
        "星露谷物语",
        "Stardew Valley",
        "StardewModdingAPI.exe",
    )


def test_seeded_needles_shared_by_two_games_are_dropped() -> None:
    """两款游戏播种了同一个候选名: 这根针整体进 conflicts(宁可不判断, 不误报)."""
    ownership = build_name_ownership(
        [
            Game(id=1, name="甲游戏", process_names=("SharedLauncher.exe",)),
            Game(id=2, name="乙游戏", process_names=("SharedLauncher.exe",)),
        ]
    )

    assert ownership.needles == {}
    assert ownership.conflicts == ("sharedlauncher",)


def test_default_provider_uses_psutil() -> None:
    # 测试进程自身就是 python*, 只要 psutil 可用就一定能看到它.
    probe = probe_game_process("python")

    assert probe.checked is True
