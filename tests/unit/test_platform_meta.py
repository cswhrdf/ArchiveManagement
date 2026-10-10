"""平台数据模型(版本化)的单元测试.

锁住平台元数据的数据契约: 版本字段、必填字段、未知字段的处理, 以及"版本不兼容
必须显式报错"这条约定 —— 平台数据来自外部文件与缓存, 缺字段的半个对象绝不能
静默流进备份流程。
"""

from __future__ import annotations

import pytest

from archive_management.domain import (
    PLATFORM_DATA_FORMAT_VERSION,
    ArtworkRef,
    PlatformGame,
    SavePathCandidate,
    parse_platform_game,
)
from archive_management.exceptions import PlatformIntegrationError

pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("平台数据模型"),
    pytest.mark.story("解析平台游戏数据"),
    pytest.mark.layer("unit"),
]


def _payload(**overrides: object) -> dict[str, object]:
    """返回一份最小的合法平台数据(可用关键字覆盖任意字段)."""
    payload: dict[str, object] = {
        "version": PLATFORM_DATA_FORMAT_VERSION,
        "platform": "steam",
        "game_id": "753640",
        "name": "Outer Wilds",
    }
    payload.update(overrides)
    return payload


def test_model_fills_in_the_version_and_optional_fields() -> None:
    game = PlatformGame(platform="steam", game_id="753640", name="Outer Wilds")

    assert game.version == PLATFORM_DATA_FORMAT_VERSION
    assert game.install_dir == ""
    assert game.artwork == []
    assert game.save_paths == []


def test_parse_platform_game_accepts_a_complete_payload() -> None:
    game = parse_platform_game(
        _payload(
            install_dir=r"D:\Steam\steamapps\common\OuterWilds",
            artwork=[{"kind": "cover", "url": "https://cdn.example/1.jpg"}],
            save_paths=[
                {
                    "path": r"D:\Steam\steamapps\common\OuterWilds\Saves",
                    "reason_code": "steam_remotecache",
                    "relative_path": "OuterWilds/Saves",
                }
            ],
        )
    )

    assert game.artwork[0].kind == "cover"
    assert game.artwork[0].version == ""
    assert game.save_paths[0].confidence == "high"
    assert game.save_paths[0].path_kind == "directory"


def test_unknown_version_is_reported_instead_of_silently_downgraded() -> None:
    with pytest.raises(PlatformIntegrationError, match="不支持的平台数据版本"):
        parse_platform_game(_payload(version=PLATFORM_DATA_FORMAT_VERSION + 1))


def test_missing_required_field_is_reported() -> None:
    payload = _payload()
    del payload["game_id"]

    with pytest.raises(PlatformIntegrationError, match="平台数据字段非法"):
        parse_platform_game(payload)


def test_unknown_field_is_rejected() -> None:
    """平台数据是外部输入: 多出来的字段宁可报错, 也不要带着它往下走."""
    with pytest.raises(PlatformIntegrationError, match="平台数据字段非法"):
        parse_platform_game(_payload(unexpected="x"))


def test_blank_game_name_is_rejected() -> None:
    with pytest.raises(ValueError, match="不能为空"):
        PlatformGame(platform="steam", game_id="753640", name="   ")


def test_artwork_and_candidate_defaults_are_safe() -> None:
    artwork = ArtworkRef(kind="icon")
    candidate = SavePathCandidate(path="saves")

    assert artwork.url == ""
    assert artwork.local_path == ""
    assert candidate.confidence == "high"
    assert candidate.detail == ""
    assert candidate.relative_path == ""
