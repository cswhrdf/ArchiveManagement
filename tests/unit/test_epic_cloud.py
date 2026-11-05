"""Epic 启动器云存档缓存解析与宏路径映射的单元测试.

验收判据与 Steam 云清单同一条: 本机文件由平台客户端维护、指向真实存档路径。
这里用手写的 manifest 复现实测到的容器形态(二进制头 + 明文
``CloudSaveFolder${宏}/...`` + 结构字节), 覆盖三种结局:

- 宏路径展开后的目录真实存在 → 产出一条 ``medium`` 置信度的候选;
- 宏未登记、路径越界、展开后不存在 → 只少一条并记 DEBUG 日志;
- 平台/缓存/清单缺失或损坏 → 空结果, 不抛异常, 不阻断手动添加。

fixture 里的账号与 AppName 都是编造的短串, 不含真实用户数据。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

import helpers
from archive_management.services.epic_cloud import (
    EpicCloudSource,
    macro_roots,
    parse_cloud_save_folders,
)
from archive_management.services.platform_scan import ScanRoots

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("Epic 云存档缓存"),
    pytest.mark.story("解析云存档缓存产出存档候选"),
    pytest.mark.layer("unit"),
]

_LOGGER = "archive_management.services.epic_cloud"
_APP_NAME = "EpicDemo"


def _roots(tmp_path: Path) -> ScanRoots:
    """构造 Windows 口径的探测环境(临时目录充当 ``%LOCALAPPDATA%`` 的父目录)."""
    return helpers.scan_roots(tmp_path)


def _messages(caplog: pytest.LogCaptureFixture) -> list[str]:
    """返回本次捕获到的日志文本(按记录顺序)."""
    return [record.getMessage() for record in caplog.records]


def _cloud_cache(tmp_path: Path, app_name: str = _APP_NAME) -> Path:
    """返回 helper 写出来的那份 CloudCache 目录(用例要往里塞损坏文件时用)."""
    return (
        tmp_path
        / "Local"
        / "EpicGamesLauncher"
        / "Saved"
        / "Saves"
        / helpers.EPIC_ACCOUNT
        / app_name
        / "CloudCache"
    )


# --------------------------------------------------------------- 容器明文解析


def test_manifest_plaintext_paths_are_extracted_and_deduped() -> None:
    """容器里的明文宏路径按出现顺序去重; 二进制结构字节不会被并进路径."""
    data = (
        b"\x0c\xc0\xbe\x44\x00\x01Profile.sav"
        b"CloudSaveFolder${AppData}/Demo/Full/SaveGames/\x00"
        b"CloudSaveFolder${AppData}/Demo/Full/SaveGames/\x00"
        b"CloudSaveFolder${MyDocuments}/Demo/Saves/\x01"
    )

    assert parse_cloud_save_folders(data) == [
        "${AppData}/Demo/Full/SaveGames/",
        "${MyDocuments}/Demo/Saves/",
    ]


def test_a_path_at_the_end_of_the_file_is_still_read() -> None:
    """明文段一直写到文件尾(后面没有结构字节)时同样能取出来."""
    assert parse_cloud_save_folders(b"CloudSaveFolder${UserProfile}/Save") == [
        "${UserProfile}/Save"
    ]


def test_a_manifest_without_the_prefix_yields_nothing() -> None:
    """格式漂移(找不到前缀)时返回空结果, 由调用方记日志降级."""
    assert parse_cloud_save_folders(b"\x0c\xc0\xbe\x44IncludeList.txt") == []
    # 前缀后面没有内容: 空段不算一条候选.
    assert parse_cloud_save_folders(b"CloudSaveFolder") == []


# --------------------------------------------------------------- 宏 → 本机目录


def test_macro_roots_follow_the_documented_table(tmp_path: Path) -> None:
    """宏到本机目录只有这张显式映射表: 未在本表里的宏一律不猜."""
    roots = _roots(tmp_path)
    local = tmp_path / "Local"
    roaming = tmp_path / "AppData" / "Roaming"
    documents = tmp_path / "Documents"
    install = tmp_path / "Games" / "Demo"

    assert macro_roots("AppData", roots, install_dir=None) == [local, roaming]
    assert macro_roots("LocalAppData", roots, install_dir=None) == [local, roaming]
    assert macro_roots("MyDocuments", roots, install_dir=None) == [documents]
    assert macro_roots("Personal", roots, install_dir=None) == [documents]
    assert macro_roots("UserProfile", roots, install_dir=None) == [tmp_path]
    assert macro_roots("InstalledDir", roots, install_dir=install) == [install]
    assert macro_roots("InstalledDir", roots, install_dir=None) == []


def test_unknown_macros_are_dropped_with_a_debug_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        roots = macro_roots("OddMacro", _roots(tmp_path), install_dir=None)

    assert roots == []
    assert any("OddMacro" in item for item in _messages(caplog))


# --------------------------------------------------------------- 候选产出


def test_cloud_cache_turns_into_a_save_candidate(tmp_path: Path) -> None:
    """实测形态的缓存: 宏展开到 ``%LOCALAPPDATA%`` 下真实存在的目录."""
    saves = tmp_path / "Local" / "Demo" / "Full" / "SaveGames"
    saves.mkdir(parents=True)
    manifest = helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${AppData}/Demo/Full/SaveGames/"
    )

    candidates = EpicCloudSource(_roots(tmp_path)).candidates(_APP_NAME)

    assert len(candidates) == 1
    item = candidates[0]
    assert item.path == str(saves)
    assert item.reason_code == "epic_cloudcache"
    assert item.path_kind == "directory"
    # 格式没有公开契约、GUID 映射还有缺口: 证据可信但确定性低于 Steam 云清单.
    assert item.confidence == "medium"
    assert item.detail == manifest.name
    assert item.relative_path == "${AppData}/Demo/Full/SaveGames/"


def test_the_second_macro_root_is_used_when_the_first_is_missing(
    tmp_path: Path,
) -> None:
    """``${AppData}`` 的首选根不存在时退到次选(Roaming), 由存在性验证挑出来."""
    install = tmp_path / "Games" / "Demo"
    install.mkdir(parents=True)
    roaming = tmp_path / "AppData" / "Roaming" / "Demo" / "Saves"
    roaming.mkdir(parents=True)
    helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${AppData}/Demo/Saves/"
    )

    candidates = EpicCloudSource(_roots(tmp_path)).candidates(
        _APP_NAME, install_dir=install
    )

    assert [item.path for item in candidates] == [str(roaming)]


def test_candidates_are_deduped_across_accounts_and_manifests(tmp_path: Path) -> None:
    """同一目录被多个账号、多份时间戳的清单记着时只出一条候选."""
    saves = tmp_path / "Local" / "Demo" / "SaveGames"
    saves.mkdir(parents=True)
    for account, stamp in (
        ("acctone", "20261105T100000"),
        ("acctone", "20261105T110000"),
        ("accttwo", "20261105T120000"),
    ):
        helpers.write_epic_cloud_cache(
            tmp_path / "Local",
            _APP_NAME,
            "${AppData}/Demo/SaveGames/",
            account=account,
            stamp=stamp,
        )
    # 同一账号下其它游戏的缓存与"指到本游戏但没有 CloudCache"的目录都不算候选.
    other = (
        tmp_path
        / "Local"
        / "EpicGamesLauncher"
        / "Saved"
        / "Saves"
        / "acctone"
        / "OtherGame"
        / "CloudCache"
    )
    other.mkdir(parents=True)
    (
        tmp_path
        / "Local"
        / "EpicGamesLauncher"
        / "Saved"
        / "Saves"
        / "accttwo"
        / _APP_NAME
    ).mkdir(parents=True, exist_ok=True)

    candidates = EpicCloudSource(_roots(tmp_path)).candidates(_APP_NAME)

    assert [item.path for item in candidates] == [str(saves)]


def test_the_install_dir_finds_the_app_name_when_it_is_missing(
    tmp_path: Path,
) -> None:
    """游戏标识缺失时按安装目录回查 ``*.item`` 清单, 而不是按路径段猜."""
    install = tmp_path / "Games" / "Demo"
    install.mkdir(parents=True)
    saves = tmp_path / "Local" / "Demo" / "Saves"
    saves.mkdir(parents=True)
    helpers.write_epic_item(helpers.epic_tree(tmp_path), _APP_NAME, "Demo", install)
    helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${AppData}/Demo/Saves/"
    )

    candidates = EpicCloudSource(_roots(tmp_path)).candidates("", install_dir=install)

    assert [item.path for item in candidates] == [str(saves)]


def test_the_installed_dir_macro_needs_the_install_dir(tmp_path: Path) -> None:
    """``${InstalledDir}`` 只在调用方给了安装目录时才可解析."""
    install = tmp_path / "Games" / "Demo"
    (install / "Saves").mkdir(parents=True)
    helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${InstalledDir}/Saves/"
    )
    source = EpicCloudSource(_roots(tmp_path))

    assert source.candidates(_APP_NAME) == []
    assert [
        item.path for item in source.candidates(_APP_NAME, install_dir=install)
    ] == [str(install / "Saves")]


@pytest.mark.parametrize(
    "raw",
    [
        "${AppData}",  # 只有宏名, 没有子路径
        "${AppData}/",  # 子路径为空
        "${AppData}/../escape",  # 越界
        "${AppData}/d:/Windows",  # 绝对路径段
        "${AppData}/${Other}/Saves",  # 未展开的宏
        "${UnknownMacro}/Saves",  # 未登记(不可映射)的宏
        "C:/Saves",  # 根本不是宏路径
        "${AppData}/NotThere/Saves",  # 展开后目录不存在
    ],
)
def test_unusable_manifest_paths_are_dropped(tmp_path: Path, raw: str) -> None:
    """越界、未展开、未登记与不存在的路径一律不产出候选(宁缺勿错)."""
    helpers.write_epic_cloud_cache(tmp_path / "Local", _APP_NAME, raw)

    assert EpicCloudSource(_roots(tmp_path)).candidates(_APP_NAME) == []


# --------------------------------------------------------------- 降级(只有日志)


def test_unreadable_or_junk_manifests_only_shrink_the_result(tmp_path: Path) -> None:
    """读不出的清单(同名目录)与没有明文路径的清单只让结果变少."""
    saves = tmp_path / "Local" / "Demo" / "SaveGames"
    saves.mkdir(parents=True)
    manifest = helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${AppData}/Demo/SaveGames/"
    )
    (manifest.parent / "00000000T000000.manifest").mkdir()  # 同名目录占位
    (manifest.parent / "junk.manifest").write_bytes(b"\x0c\xc0\xbe\x44\x00")

    candidates = EpicCloudSource(_roots(tmp_path)).candidates(_APP_NAME)

    assert [item.path for item in candidates] == [str(saves)]


def test_an_unlistable_saves_root_is_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``Saved/Saves`` 被同名文件占住时只记日志(不抛异常)."""
    saves = tmp_path / "Local" / "EpicGamesLauncher" / "Saved"
    saves.mkdir(parents=True)
    (saves / "Saves").write_text("not a directory", encoding="utf-8")

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        caches = EpicCloudSource(_roots(tmp_path)).cloud_caches(_APP_NAME)

    assert caches == []
    assert any("无法列出 Epic 云存档目录" in item for item in _messages(caplog))


def test_an_unlistable_account_directory_is_skipped(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """某个账号目录列不出来时跳过它, 其它账号照常产出候选."""
    saves = tmp_path / "Local" / "Demo" / "SaveGames"
    saves.mkdir(parents=True)
    helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${AppData}/Demo/SaveGames/", account="acctone"
    )
    blocked = tmp_path / "Local" / "EpicGamesLauncher" / "Saved" / "Saves" / "accttwo"
    blocked.mkdir(parents=True)
    original = Path.iterdir

    def iterdir(self: Path) -> Iterator[Path]:
        if self == blocked:
            raise PermissionError("拒绝访问")
        return original(self)

    monkeypatch.setattr(Path, "iterdir", iterdir)

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = EpicCloudSource(_roots(tmp_path)).candidates(_APP_NAME)

    assert [item.path for item in candidates] == [str(saves)]
    assert any("无法列出 Epic 账号目录" in item for item in _messages(caplog))


def test_only_windows_reads_the_launcher_cache(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """macOS 侧位置未实测、Linux 没有官方启动器: 两处都显式返回空."""
    helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${AppData}/Demo/SaveGames/"
    )
    for platform in ("macos", "linux"):
        with caplog.at_level(logging.DEBUG, logger=_LOGGER):
            candidates = EpicCloudSource(
                helpers.scan_roots(tmp_path, platform=platform)
            ).candidates(_APP_NAME)

        assert candidates == []

    assert any("仅接入 Windows" in item for item in _messages(caplog))


def test_an_empty_game_identifier_yields_nothing(tmp_path: Path) -> None:
    helpers.write_epic_cloud_cache(
        tmp_path / "Local", _APP_NAME, "${AppData}/Demo/SaveGames/"
    )

    assert EpicCloudSource(_roots(tmp_path)).candidates("   ") == []


def test_a_missing_cache_is_reported_and_yields_nothing(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """没开云存档、启动器没装或游戏已卸载时只记 DEBUG 日志."""
    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = EpicCloudSource(_roots(tmp_path)).candidates(_APP_NAME)

    assert candidates == []
    assert any(_APP_NAME in item for item in _messages(caplog))


def test_an_unknown_install_dir_has_no_app_name(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """安装清单里对不上安装目录时回查失败, 整条链路退回空结果."""
    install = tmp_path / "Games" / "Demo"
    install.mkdir(parents=True)
    helpers.write_epic_item(
        helpers.epic_tree(tmp_path), "OtherApp", "Other", tmp_path / "Games" / "Other"
    )

    with caplog.at_level(logging.DEBUG, logger=_LOGGER):
        candidates = EpicCloudSource(_roots(tmp_path)).candidates(
            "", install_dir=install
        )

    assert candidates == []
    assert any("找不到安装目录" in item for item in _messages(caplog))
