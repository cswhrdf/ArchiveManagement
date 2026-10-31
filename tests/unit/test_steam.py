"""Steam 本地数据读取器单元测试."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from archive_management.exceptions import SteamIntegrationError
from archive_management.services.steam import LocalSteamDataReader

pytestmark = [
    pytest.mark.steam,
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("Steam 数据读取"),
    pytest.mark.story("读取 Steam 库"),
    pytest.mark.layer("unit"),
]


def _write(source: Path, payload: object) -> None:
    source.write_text(json.dumps(payload), encoding="utf-8")


def test_read_valid_file(tmp_path: Path) -> None:
    source = tmp_path / "steam.json"
    _write(
        source,
        {"version": 1, "games": [{"app_id": 480, "name": "The Witness"}]},
    )
    data = LocalSteamDataReader().read(source)
    assert len(data.games) == 1
    assert data.games[0].app_id == 480


def test_read_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(SteamIntegrationError):
        LocalSteamDataReader().read(tmp_path / "absent.json")


def test_read_invalid_json_raises(tmp_path: Path) -> None:
    source = tmp_path / "broken.json"
    source.write_text("{not json", encoding="utf-8")
    with pytest.raises(SteamIntegrationError):
        LocalSteamDataReader().read(source)


def test_read_unsupported_version_raises(tmp_path: Path) -> None:
    source = tmp_path / "future.json"
    _write(source, {"version": 99, "games": []})
    with pytest.raises(SteamIntegrationError):
        LocalSteamDataReader().read(source)


def test_read_invalid_schema_raises(tmp_path: Path) -> None:
    source = tmp_path / "bad-schema.json"
    _write(source, {"version": 1, "games": [{"app_id": 0, "name": ""}]})
    with pytest.raises(SteamIntegrationError):
        LocalSteamDataReader().read(source)
