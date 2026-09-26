"""批量导入必须逐条复核内层包哈希: 清单里的哈希对不上就不许落库.

这条守卫盯的是一个很容易被"优化"掉的地方: 批量体检为了快**不**校验哈希
(``read_batch_package`` 默认 ``verify_hashes=False``), 真正导入那一步必须传
``True``。写这条用例时批量导入确实漏了这一步 —— 外层清单里每个内层包的
``sha256`` 根本没被核对过。

篡改方式刻意选成"只改外层清单里记录的哈希, 内层包一个字节都不动":
这样除了那次哈希复核, 没有任何别的检查会把这份包拦下来(大小、路径、压缩比、
内层清单与内容全都自洽), 所以去掉导入那一步的 ``verify_hashes=True`` 这条用例
**必然**红 —— 换成"改内层字节"就做不到这一点, 那会被内层自己的校验兜住.
"""

from __future__ import annotations

import json
import zipfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

import helpers
from archive_management.application.backup import BackupService
from archive_management.application.export import ExportService
from archive_management.application.imports import (
    BatchImportChoice,
    ImportService,
)
from archive_management.exceptions import ArchiveManagementError
from archive_management.infrastructure.repository import (
    BackupRepository,
    GameRepository,
    SaveLocationRepository,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.critical,
    pytest.mark.epic("备份与分支"),
    pytest.mark.feature("导入导出"),
    pytest.mark.story("批量导出与导入"),
    pytest.mark.layer("integration"),
]

#: 清单在包里的条目名(外层包); 用后缀找是为了不把常量抄成第二份事实.
MANIFEST_SUFFIX = "manifest.json"


def _entries(archive: zipfile.ZipFile) -> list[tuple[str, bytes]]:
    """把包里的条目整个读进内存(条目不多, 用的是真实的小包)."""
    return [(info.filename, archive.read(info.filename)) for info in archive.infolist()]


def _rewrite(batch: Path, payloads: Sequence[tuple[str, bytes]]) -> None:
    """用给定的条目集合重写这个包(同名覆盖, 条目顺序保持不变)."""
    with zipfile.ZipFile(batch, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in payloads:
            archive.writestr(name, payload)


def _spoil_recorded_hash(batch: Path) -> str:
    """把外层清单里第一个内层包的 sha256 改成等长的错误值, 其余一字不动.

    返回改的是哪个内层条目名, 供调用方在做断言时说清"改的是哪一款".
    """
    with zipfile.ZipFile(batch) as archive:
        items = _entries(archive)
    manifest_name = next(name for name, _ in items if name.endswith(MANIFEST_SUFFIX))
    manifest: dict[str, Any] = json.loads(dict(items)[manifest_name])
    # 清单里每款游戏是一行平铺的字典: entry 是内层条目名, sha256 是那份内层包的哈希.
    row = manifest["games"][0]
    recorded = str(row["sha256"])
    row["sha256"] = "0" * len(recorded)
    rewritten = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    _rewrite(
        batch,
        [
            (name, rewritten if name == manifest_name else payload)
            for name, payload in items
        ],
    )
    return str(row["entry"])


def _build_batch(root: Path) -> Path:
    """在源机器上造两款带备份的游戏, 并批量导出成一个包."""
    database = helpers.migrated_database(root / "source")
    backup_root = root / "backups"
    backups = BackupService(database, backup_root=backup_root)
    for index, name in enumerate(("Demo", "Other")):
        save = helpers.make_save_folder(root, index=index, content=f"state-{index}")
        game_id = helpers.add_game(database, name, path=save)
        backups.create_backup(game_id, title="第一次")
    destination = root / "batch.archive.zip"
    ExportService(database, backup_root=backup_root).export_games(
        [item.id for item in GameRepository(database).list() if item.id is not None],
        destination,
    )
    return destination


def test_a_spoiled_recorded_hash_is_refused_before_anything_is_written(
    tmp_path: Path,
) -> None:
    """清单里的哈希对不上时必须拒绝导入, 且一份内容都不许落盘.

    这里先体检(体检不校验哈希, 这正是"快"的代价), 再篡改清单, 然后导入 ——
    顺序与界面完全一致(**体检与导入之间隔着一个用户确认**), 所以它锁住的就是
    "真正落库之前必须复核过"这条纪律。
    """
    batch = _build_batch(tmp_path)
    target_root = tmp_path / "target"
    database = helpers.migrated_database(target_root)
    backup_root = target_root / "backups"
    service = ImportService(database, backup_root=backup_root)
    inspection = service.inspect_batch(batch)
    choices = {
        item.entry: BatchImportChoice(strategy="new") for item in inspection.games
    }

    spoiled = _spoil_recorded_hash(batch)
    assert spoiled.endswith(".zip"), f"篡改的是内层条目: {spoiled}"

    with pytest.raises(ArchiveManagementError):
        service.import_batch(inspection, choices)

    assert GameRepository(database).list() == []
    assert not [item for game in GameRepository(database).list() for item in [game]]
    assert not backup_root.exists() or list(backup_root.glob("*/*")) == []


def test_the_same_batch_still_imports_before_the_hash_is_spoiled(
    tmp_path: Path,
) -> None:
    """对照组: 没被篡改的**同一份包**能正常导入(证明上一条红的是篡改, 不是环境)."""
    batch = _build_batch(tmp_path)
    target_root = tmp_path / "target"
    database = helpers.migrated_database(target_root)
    backup_root = target_root / "backups"
    service = ImportService(database, backup_root=backup_root)
    inspection = service.inspect_batch(batch)
    choices: Mapping[str, BatchImportChoice] = {
        item.entry: BatchImportChoice(strategy="new") for item in inspection.games
    }

    result = service.import_batch(inspection, choices)

    assert result.games == 2
    assert result.nodes == 2
    games = GameRepository(database).list()
    assert sorted(game.name for game in games) == ["Demo", "Other"]
    assert (
        sum(
            len(BackupRepository(database).list_for_game(game.id))
            for game in games
            if game.id is not None
        )
        == 2
    )
    assert (
        sum(
            len(SaveLocationRepository(database).list_for_game(game.id))
            for game in games
            if game.id is not None
        )
        == 0
    ), "选择里没给位置映射: 按既有语义一条位置都不写"
    assert len(list(backup_root.glob("*/*"))) == 2
