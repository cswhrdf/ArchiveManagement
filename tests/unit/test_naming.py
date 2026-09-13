"""备份目录命名的单元测试.

备份目录 = ``<slug>-<token>``: 名称部分由游戏名称规范化而来, token 是
"名称 + 存档路径"的短哈希, 用于区分同名游戏。这里锁定规范化与哈希的稳定性
(它们决定磁盘上的目录名, 一旦变化就会产生新的目录)。
"""

from __future__ import annotations

import pytest

from archive_management.services.naming import (
    DEFAULT_SLUG,
    MAX_SLUG_LENGTH,
    TOKEN_LENGTH,
    backup_relpath,
    game_folder,
    game_slug,
    storage_token,
)

pytestmark = [
    pytest.mark.backend,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("目录命名"),
    pytest.mark.story("按游戏名称定位备份目录"),
    pytest.mark.layer("unit"),
]


def test_slug_keeps_readable_name() -> None:
    assert game_slug("Outer Wilds") == "Outer_Wilds"
    assert game_slug("  Hollow_Knight  ") == "Hollow_Knight"
    assert game_slug("星际拓荒") == "星际拓荒"


def test_slug_replaces_illegal_characters() -> None:
    assert game_slug('Bad<>:"/\\|?*Name') == "Bad_Name"
    assert game_slug("a\tb\nc") == "a_b_c"
    assert game_slug("dots...") == "dots"


def test_slug_falls_back_when_empty() -> None:
    assert game_slug("") == DEFAULT_SLUG
    assert game_slug("   ") == DEFAULT_SLUG
    assert game_slug("///") == DEFAULT_SLUG


def test_slug_is_truncated() -> None:
    slug = game_slug("x" * 200)
    assert len(slug) == MAX_SLUG_LENGTH


def test_token_depends_on_name_and_paths() -> None:
    base = storage_token("Demo", [r"D:\save"])
    assert len(base) == TOKEN_LENGTH
    assert base == storage_token("demo", ["d:/SAVE"])  # 大小写与分隔符归一
    assert base == storage_token("Demo", ["d:/save"])
    assert base != storage_token("Demo2", [r"D:\save"])
    assert base != storage_token("Demo", [r"D:\other"])


def test_token_ignores_path_order() -> None:
    assert storage_token("Demo", ["a", "b"]) == storage_token("Demo", ["b", "a"])


def test_game_folder_combines_slug_and_token() -> None:
    folder = game_folder("Outer Wilds", [r"D:\Games\OuterWilds\save"])
    assert (
        folder
        == f"Outer_Wilds-{storage_token('Outer Wilds', [r'D:\Games\OuterWilds\save'])}"
    )
    assert folder.startswith("Outer_Wilds-")


def test_backup_relpath_is_folder_plus_stamp() -> None:
    assert (
        backup_relpath("Demo-1a2b3c4d", "20260913T101500", "deadbeef")
        == "Demo-1a2b3c4d/20260913T101500-deadbeef"
    )
