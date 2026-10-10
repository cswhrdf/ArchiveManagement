"""游戏进程探测的单元测试.

用注入的进程名提供者覆盖匹配、降级与多候选行为; 最后一条用真实
``psutil`` 验证默认提供者可用(断言宽松, 不依赖具体进程名)。
"""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from archive_management.domain.activation import build_name_ownership
from archive_management.services.processes import (
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


def test_default_provider_uses_psutil() -> None:
    # 测试进程自身就是 python*, 只要 psutil 可用就一定能看到它.
    probe = probe_game_process("python")

    assert probe.checked is True
