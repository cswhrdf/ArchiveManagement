"""Steam 云端同步清单解析与 root 映射的单元测试.

云端同步清单解析的验收判据都在这里: 脱敏 fixture(临时目录里按真实清单结构手写)下候选
路径正确; 账号目录缺失、清单损坏、root 未知或属于别的平台时只得到空结果与一条
DEBUG 日志, 不抛异常。

fixture 的形状取自本机真实清单(头部 ``ChangeNumber``/``OSType``, 每个文件一个
``root``/``size``/``sha`` 条目), 但里面的路径、账号与 AppID 都是编造的, 不含真实
用户数据。
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

import helpers
from archive_management.services import steam_cloud as steam_cloud_mod
from archive_management.services.platform_scan import ScanRoots
from archive_management.services.platforms import PlatformFamily
from archive_management.services.steam_cloud import (
    SteamCloudSource,
    cloud_root_name,
    cloud_root_path,
    parse_remotecache,
)

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("Steam 云端清单"),
    pytest.mark.story("解析云端同步清单产出存档候选"),
    pytest.mark.layer("unit"),
]

_LOGGER = "archive_management.services.steam_cloud"


def _roots(tmp_path: Path, *, platform: PlatformFamily = "windows") -> ScanRoots:
    """构造指向临时目录的探测环境(与本地探测共用同一套根目录约定)."""
    return helpers.scan_roots(tmp_path, platform=platform)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """返回本次捕获到的日志文本(按记录顺序)."""
    return [record.getMessage() for record in caplog.records]


# --------------------------------------------------------------- 清单解析


def test_entries_are_parsed_with_their_root_size_and_sha() -> None:
    files = parse_remotecache(
        '"753640"\n'
        "{\n"
        '\t"ChangeNumber"\t\t"0"\n'
        '\t"OSType"\t\t"0"\n'
        '\t"OuterWilds/Saves/save.dat"\n'
        "\t{\n"
        '\t\t"root"\t\t"2"\n'
        '\t\t"size"\t\t"2048"\n'
        '\t\t"sha"\t\t"deadbeef"\n'
        "\t}\n"
        "}\n"
    )

    assert [
        (item.relative_path, item.root_id, item.size, item.sha) for item in files
    ] == [("OuterWilds/Saves/save.dat", 2, 2048, "deadbeef")]


def test_header_fields_and_entries_without_a_root_are_skipped() -> None:
    """头部字段体内还有嵌套块, 不能被当成文件条目; 没有 root 的条目无法定位."""
    files = parse_remotecache(
        '"753640"\n'
        "{\n"
        '\t"ChangeNumber"\t\t"7"\n'
        '\t"NoRoot/save.dat"\n'
        "\t{\n"
        '\t\t"size"\t\t"1"\n'
        "\t}\n"
        '\t"ok.sav"\n'
        "\t{\n"
        '\t\t"root"\t\t"3"\n'
        "\t}\n"
        '\t"weird.sav"\n'
        "\t{\n"
        '\t\t"root"\t\t"not-a-number"\n'
        "\t}\n"
        "}\n"
    )

    assert [(item.relative_path, item.root_id) for item in files] == [("ok.sav", 3)]


def test_escaped_entry_names_are_unescaped() -> None:
    files = parse_remotecache(
        '"753640"\n{\n\t"Save\\\\Folder/save.dat"\n\t{\n\t\t"root"\t\t"3"\n\t}\n}\n'
    )

    assert files[0].relative_path == "Save\\Folder/save.dat"


def test_unknown_root_names_stay_readable() -> None:
    assert cloud_root_name(2) == "WinMyDocuments"
    assert cloud_root_name(4242) == "root 4242"


# ------------------------------------------------------------- root 映射


@pytest.mark.parametrize(
    ("platform", "root_id", "expected"),
    [
        ("windows", 2, "Documents"),
        ("windows", 3, "Local"),
        ("windows", 4, "AppData/Roaming"),
        ("windows", 9, "Saved Games"),
        ("windows", 10, "ProgramData"),
        ("windows", 12, "AppData/LocalLow"),
        ("windows", 18, ""),
        ("macos", 6, ""),
        ("macos", 7, "Local"),
        ("macos", 8, "Documents"),
        ("macos", 13, "Library/Caches"),
        ("linux", 14, ""),
        ("linux", 15, "Local"),
        ("linux", 16, ".config"),
    ],
)
def test_root_ids_map_to_the_platform_directories(
    tmp_path: Path, platform: PlatformFamily, root_id: int, expected: str
) -> None:
    resolved = cloud_root_path(root_id, _roots(tmp_path, platform=platform))

    assert resolved == tmp_path / expected


@pytest.mark.parametrize("root_id", [0, 5, 11, 17, 4242])
def test_mirror_unmapped_and_unknown_roots_have_no_local_path(
    tmp_path: Path, root_id: int
) -> None:
    """root 0 是 Steam 自己的云端镜像目录, 5/11/17 本机无从映射, 其余未知."""
    assert cloud_root_path(root_id, _roots(tmp_path)) is None


@pytest.mark.parametrize(
    ("platform", "root_id"),
    [("windows", 8), ("windows", 13), ("macos", 2), ("macos", 3), ("linux", 3)],
)
def test_roots_of_other_platforms_have_no_local_path(
    tmp_path: Path, platform: PlatformFamily, root_id: int
) -> None:
    assert cloud_root_path(root_id, _roots(tmp_path, platform=platform)) is None


def test_install_root_needs_the_game_install_dir(tmp_path: Path) -> None:
    roots = _roots(tmp_path)
    install = tmp_path / "SteamLib" / "steamapps" / "common" / "OuterWilds"

    assert cloud_root_path(1, roots) is None
    assert cloud_root_path(1, roots, install_dir=install) == install


# ------------------------------------------------------------- 候选产出


def test_documents_root_folds_into_one_directory_candidate(tmp_path: Path) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(
        steam,
        "753640",
        {"OuterWilds/Saves/a.sav": 2, "OuterWilds/Saves/b.sav": 2},
    )

    candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert [item.path_kind for item in candidates] == ["directory"]
    candidate = candidates[0]
    assert candidate.path == str(tmp_path / "Documents" / "OuterWilds" / "Saves")
    assert candidate.relative_path == "OuterWilds/Saves"
    assert candidate.reason_code == "steam_remotecache"
    assert candidate.confidence == "high"
    assert candidate.detail == "WinMyDocuments"


def test_files_written_directly_into_a_root_become_file_candidates(
    tmp_path: Path,
) -> None:
    """公共父目录为空时(存在直接写在 root 下的文件)退化成逐文件候选."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(steam, "753640", {"notes.txt": 9, "profile.sav": 9})

    candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert [(item.path_kind, item.relative_path) for item in candidates] == [
        ("file", "notes.txt"),
        ("file", "profile.sav"),
    ]
    assert candidates[0].path == str(tmp_path / "Saved Games" / "notes.txt")
    assert candidates[0].detail == "WinSavedGames"


def test_directory_candidates_are_listed_before_file_candidates(
    tmp_path: Path,
) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(
        steam, "753640", {"OuterWilds/Saves/a.sav": 2, "settings.ini": 3}
    )

    candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert [(item.path_kind, item.relative_path) for item in candidates] == [
        ("directory", "OuterWilds/Saves"),
        ("file", "settings.ini"),
    ]


def test_install_root_candidates_use_the_manifest_install_dir(tmp_path: Path) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, "753640", "Outer Wilds", "OuterWilds")
    helpers.write_remotecache(steam, "753640", {"Saves/a.sav": 1, "Saves/b.sav": 1})

    candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert [item.detail for item in candidates] == ["GameInstall"]
    assert candidates[0].path == str(
        steam / "steamapps" / "common" / "OuterWilds" / "Saves"
    )


def test_every_account_contributes_to_the_same_candidate(tmp_path: Path) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(
        steam, "753640", {"OuterWilds/Saves/a.sav": 2}, account="1"
    )
    helpers.write_remotecache(
        steam, "753640", {"OuterWilds/Saves/b.sav": 2}, account="2"
    )

    candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert [item.relative_path for item in candidates] == ["OuterWilds/Saves"]


def test_only_numeric_account_directories_are_recognised(tmp_path: Path) -> None:
    steam = helpers.steam_tree(tmp_path)
    (steam / "userdata" / helpers.STEAM_ACCOUNT).mkdir(parents=True)
    (steam / "userdata" / "anonymous").mkdir(parents=True)
    source = SteamCloudSource(_roots(tmp_path))

    assert source.accounts() == [helpers.STEAM_ACCOUNT]


def test_manifests_in_non_numeric_directories_are_ignored(tmp_path: Path) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(
        steam, "753640", {"OuterWilds/Saves/a.sav": 2}, account="anonymous"
    )
    source = SteamCloudSource(_roots(tmp_path))

    assert source.manifests("753640") == []
    assert source.candidates("753640") == []


def test_an_unreadable_userdata_directory_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``userdata`` 不可读(不存在对应目录/被同名文件占住)时只记日志."""
    steam = helpers.steam_tree(tmp_path)
    (steam / "userdata").write_text("not a directory", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        accounts = SteamCloudSource(_roots(tmp_path)).accounts()

    assert accounts == []
    assert any("无法列出 userdata" in item for item in _messages(caplog))


def test_an_empty_app_id_yields_nothing(tmp_path: Path) -> None:
    helpers.steam_tree(tmp_path)

    assert SteamCloudSource(_roots(tmp_path)).candidates("  ") == []


# ------------------------------------------------- 降级(空结果 + DEBUG 日志)


def test_a_missing_manifest_is_reported_and_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    helpers.steam_tree(tmp_path)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert candidates == []
    assert any("没有找到 AppID 753640" in item for item in _messages(caplog))


def test_a_broken_manifest_is_reported_and_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    steam = helpers.steam_tree(tmp_path)
    manifest = helpers.write_remotecache(steam, "753640", {"OuterWilds/Saves/a.sav": 2})
    manifest.write_text(
        '"753640"\n{\n\t"OuterWilds/Saves/a.sav"\n\t{\n', encoding="utf-8"
    )

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert candidates == []
    assert any("文件可能损坏" in item for item in _messages(caplog))


def test_an_unreadable_manifest_is_reported_and_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    steam = helpers.steam_tree(tmp_path)
    manifest = helpers.write_remotecache(steam, "753640", {"OuterWilds/Saves/a.sav": 2})
    manifest.unlink()
    manifest.mkdir()  # 同名目录占位: 读取必然失败

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert candidates == []
    assert any("远端清单不可读" in item for item in _messages(caplog))


def test_unresolved_roots_are_skipped_with_a_debug_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """root 1(游戏未安装)、root 0(镜像)、root 5/11(本机无从映射)全部跳过."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(
        steam,
        "753640",
        {"Saves/a.sav": 1, "notes_1": 0, "base/x.sav": 5, "cloud/y.sav": 11},
    )

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert candidates == []
    messages = _messages(caplog)
    assert any("游戏可能已卸载" in item for item in messages)
    assert sum("无法定位" in item for item in messages) == 4
    assert any("Default" in item for item in messages)


def test_absolute_and_parent_paths_are_rejected(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """清单损坏或被改写时, 候选绝不能跑到 root 之外(用户主目录/磁盘根目录)."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(
        steam,
        "753640",
        {
            "C:\\Windows\\system.ini": 2,
            "/etc/passwd": 2,
            "../../secret.sav": 2,
        },
    )

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert candidates == []
    assert sum("越界或非相对" in item for item in _messages(caplog)) == 3


def test_debug_logging_is_not_required_for_a_successful_run(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(steam, "753640", {"OuterWilds/Saves/a.sav": 2})

    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert len(candidates) == 1
    assert _messages(caplog) == []


def test_the_install_directory_is_found_after_another_app(tmp_path: Path) -> None:
    """本机装有多款游戏时按 AppID 找到对应那一款的安装目录(而不是只看第一条)."""
    steam = helpers.steam_tree(tmp_path)
    helpers.write_steam_manifest(steam, "111", "Other", "Other")
    helpers.write_steam_manifest(steam, "753640", "Outer Wilds", "OuterWilds")
    helpers.write_remotecache(steam, "753640", {"Saves/a.sav": 1})

    candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    expected = steam / "steamapps" / "common" / "OuterWilds" / "Saves"
    assert [item.path for item in candidates] == [str(expected)]


def test_a_candidate_that_escapes_the_root_is_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """即使递进来的相对片段会跑出 root, 候选也不会落到外面(第二道闸).

    正常路径下切片已经拒绝绝对路径与 ``..``, 所以用替身把这种片段直接递进去,
    钉住 ``_build_candidate`` 自己那一次边界校验.
    """
    steam = helpers.steam_tree(tmp_path)
    helpers.write_remotecache(steam, "753640", {"escape/x.sav": 2, "safe/y.sav": 2})

    def parts(raw: str) -> tuple[str, ...]:
        return ("..", "secret.sav") if raw.startswith("escape") else ("safe", "y.sav")

    monkeypatch.setattr(steam_cloud_mod, "_relative_parts", parts)

    candidates = SteamCloudSource(_roots(tmp_path)).candidates("753640")

    assert len(candidates) == 1
    assert Path(candidates[0].path).name == "y.sav"
