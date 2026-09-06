"""领域模型单元测试: 约束、序列化往返与未知字段拒绝."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from archive_management.domain import BackupFileEntry, BackupNode, Game, SaveLocation


def _utc() -> datetime:
    return datetime.now(UTC)


def test_game_requires_nonempty_name() -> None:
    with pytest.raises(ValidationError):
        Game(name="")


def test_game_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        Game.model_validate({"name": "Demo", "bogus_field": 1})


def test_game_defaults() -> None:
    game = Game(name="Demo Game")
    assert game.platform == "windows"
    assert game.enabled is True
    assert game.steam_app_id is None


def test_save_location_rejects_empty_path() -> None:
    with pytest.raises(ValidationError):
        SaveLocation(game_id=1, path="")


def test_backup_node_round_trip() -> None:
    node = BackupNode(
        game_id=1,
        parent_id=None,
        node_kind="branch",
        branch_name="bad-end",
        created_at=_utc(),
        note="尝试坏结局前的分支",
        storage_relpath="1/20260905T000000Z",
    )
    restored = BackupNode.model_validate_json(node.model_dump_json())
    assert restored == node
    assert restored.branch_name == "bad-end"


def test_backup_node_json_uses_camel_safe_keys() -> None:
    node = BackupNode(game_id=1, node_kind="manual")
    payload = node.model_dump()
    assert "parent_id" in payload
    assert payload["node_kind"] == "manual"


def test_backup_file_rejects_negative_size() -> None:
    with pytest.raises(ValidationError):
        BackupFileEntry(backup_id=1, relative_path="x", sha256="a", size=-1)
