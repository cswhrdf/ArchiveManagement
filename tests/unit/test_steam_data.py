"""Steam 数据配置模型的单元测试."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from archive_management.domain.steam_data import (
    STEAM_DATA_FORMAT_VERSION,
    SteamDataFile,
    SteamGameEntry,
)

pytestmark = [
    pytest.mark.steam,
    pytest.mark.critical,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("Steam 数据模型"),
    pytest.mark.story("Steam 配置建模"),
    pytest.mark.layer("unit"),
]


def test_steam_data_file_defaults_to_current_version() -> None:
    data = SteamDataFile()
    assert data.version == STEAM_DATA_FORMAT_VERSION
    assert data.games == []


def test_steam_data_file_parses_valid_games() -> None:
    data = SteamDataFile.model_validate(
        {
            "version": 1,
            "games": [
                {"app_id": 480, "name": "The Witness"},
                {"app_id": 105600, "name": "Terraria"},
            ],
        }
    )
    assert len(data.games) == 2
    assert data.games[0].app_id == 480
    assert data.games[1].name == "Terraria"


def test_steam_game_entry_rejects_non_positive_app_id() -> None:
    with pytest.raises(ValidationError):
        SteamGameEntry.model_validate({"app_id": 0, "name": "Demo"})


def test_steam_game_entry_rejects_empty_name() -> None:
    with pytest.raises(ValidationError):
        SteamGameEntry.model_validate({"app_id": 480, "name": "   "})


def test_steam_data_file_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        SteamDataFile.model_validate({"version": 1, "games": [], "extra": True})
