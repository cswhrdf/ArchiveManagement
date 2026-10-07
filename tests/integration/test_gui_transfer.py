"""
导入导出包的 GUI 端到端: 单包导出/导入、批量勾选、取消与损坏包不落盘。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from gui_support import gui_app

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

import archive_management.ui.main_window as main_mod
from archive_management.application.imports import (
    STRATEGY_MERGE,
    STRATEGY_NEW,
    STRATEGY_SKIP,
)
from archive_management.i18n import tr
from archive_management.services.pathcheck import normalize_path
from archive_management.ui.models import (
    BatchExportChoice,
    BatchImportSelection,
    FeedbackKind,
    ImportChoice,
)
from button_support import (
    _drain,
    _feedback_kind,
    _home_item,
    _new_app,
    _patch_dialogs,
    _pump,
    _real_service_with_one_backup,
    _service_backups,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.smoke,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("按钮端到端操作"),
    pytest.mark.layer("e2e"),
]

# 后台操作等待上限: 恢复/备份会真读写文件, 覆盖率与 CI 环境下会明显变慢.


def test_gui_export_writes_the_package_the_user_picked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """点「导出游戏」后落地的必须是用户选的那个文件, 反馈里带真实计数."""
    from archive_management.services.export_format import read_package
    from archive_management.ui.models import FeedbackKind, size_label

    service, game_id, save_dir = _real_service_with_one_backup(monkeypatch, tmp_path)
    destination = tmp_path / "saves" / "真实游戏.archive.zip"
    seen: dict[str, Any] = {}

    def save_file(**kwargs: Any) -> str:
        """记录对话框入参并返回用户"选定"的路径."""
        seen.update(kwargs)
        destination.parent.mkdir(exist_ok=True)
        return str(destination)

    monkeypatch.setattr(main_mod, "pick_save_file", save_file)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game_id)

        app._on_export()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        assert destination.is_file(), "导出包必须写在用户选的路径上"
        assert not (save_dir / destination.name).exists(), "不该写到存档目录里"
        contents = read_package(destination, verify_hashes=True)
        assert contents.game["name"] == "真实游戏"
        assert len(contents.config_list("backups")) == 1
        # 保存对话框预填的名字按游戏名派生(用户仍可改目录, 这里就改到了 saves/).
        assert str(seen["initialfile"]).endswith(".archive.zip")
        assert app._last_feedback[1] == tr(
            "result.export_done",
            name="真实游戏",
            file=destination.name,
            backups=1,
            files=len(contents.entries),
            size=size_label(sum(entry.size for entry in contents.entries)),
        )
    finally:
        app.destroy()


def test_gui_export_cancelled_by_the_picker_writes_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """保存对话框取消: 不写任何文件、不进忙碌态, 反馈说明是用户取消的."""
    from archive_management.ui.models import FeedbackKind

    service, game_id, _save = _real_service_with_one_backup(
        monkeypatch, tmp_path, export_path=None
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game_id)

        app._on_export()
        _pump(app)

        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_canceled")
        assert app._busy is False
        assert list(tmp_path.glob("*.zip")) == []
        assert list(tmp_path.glob("*.partial-*")) == []
    finally:
        app.destroy()


def _exported_package(tmp_path: Path, *, app_id: int | None = 730) -> Path:
    """造一台源机器并导出包(一款游戏 + 一个存档位置 + 一次备份).

    导入侧一律用真实后端: 演示后端既不写文件也不写库, 证明不了"游戏与备份真的
    出现了"。源游戏带 AppID 时导入体检才能匹配到库里的同一款(合并用例要用)。
    """
    from archive_management.domain import Game
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.repository import GameRepository
    from archive_management.ui.sql_backend import SqlArchiveService

    root = tmp_path / "source"
    database = Database(root / "app.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=root / "backups")
    game = GameRepository(database).add(
        Game(
            name="源游戏",
            steam_app_id=app_id,
            platform="windows",
            origin="steam",
            enabled=True,
        )
    )
    assert game.id is not None
    save = root / "save"
    save.mkdir(parents=True)
    (save / "slot1.dat").write_text("state-0", encoding="utf-8")
    service.add_location(str(game.id), path=str(save), kind="directory")
    service.run_backup_now(str(game.id))
    package = tmp_path / "源游戏.archive.zip"
    service.run_export(str(game.id), str(package))
    return package


def _empty_real_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **dialogs: Any
) -> tuple[Any, Any]:
    """一个真实的空后端(库里没有游戏)并打好对话框替身; 返回后端与数据库."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch, **dialogs)
    database = Database(tmp_path / "import.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=tmp_path / "import-backups")
    return service, database


def test_gui_import_package_creates_the_game_and_selects_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """点「导入归档包…」: 选包 → 体检 → 冲突对话框确认 → 游戏与备份真的出现."""
    package = _exported_package(tmp_path)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> ImportChoice:
        """记录界面拼给对话框的文案与选项, 并按用户的选择返回."""
        seen.update(kwargs)
        return ImportChoice(
            strategy=STRATEGY_NEW,
            target_game_id=None,
            locations={0: str(target_save)},
        )

    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package)
    )
    monkeypatch.setattr(main_mod, "import_package_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        games = service.list_games()
        assert [game.name for game in games] == ["源游戏"]
        assert app._game_id == games[0].game_id, "导入完要选中新建的游戏"
        assert len(_service_backups(service, games[0].game_id)) == 1
        # 用户映射到哪个目录就写哪个目录(不是包里那台机器的路径).
        assert [item.path for item in service.list_locations(games[0].game_id)] == [
            normalize_path(str(target_save))
        ]
        # 提示里的计数来自真实后端, 不是界面自己编的.
        assert "已导入「源游戏」" in app._last_feedback[1]
        assert "备份 1 份" in app._last_feedback[1]
        assert _home_item(app._home_page, games[0].game_id).backup_count == 1
        # 对话框拿到的是这个包的摘要; 库里没有可合并的游戏时不给"合并"选项.
        assert "源游戏" in seen["prompt"].summary
        assert [key for key, _text in seen["strategies"]] == [
            STRATEGY_NEW,
            STRATEGY_SKIP,
        ]
        assert seen["prompt"].targets == ()
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_import_package_merges_into_the_matched_game(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """库里已有同平台同 AppID 的游戏: 预选它合并, 导入后选中的也是它."""
    from archive_management.domain import Game
    from archive_management.infrastructure.repository import GameRepository

    package = _exported_package(tmp_path, app_id=730)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    (target_save / "slot1.dat").write_text("state-1", encoding="utf-8")
    service, database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package)
    )
    existing = GameRepository(database).add(
        Game(
            name="库里的源游戏",
            steam_app_id=730,
            platform="windows",
            origin="steam",
            enabled=True,
        )
    )
    assert existing.id is not None
    game_id = str(existing.id)
    service.add_location(game_id, path=str(target_save), kind="directory")
    service.run_backup_now(game_id)
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> ImportChoice:
        seen.update(kwargs)
        return ImportChoice(
            strategy=STRATEGY_MERGE,
            target_game_id=game_id,
            locations={0: str(target_save)},
        )

    monkeypatch.setattr(main_mod, "import_package_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        app._select_game(game_id)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        assert [game.name for game in service.list_games()] == ["库里的源游戏"]
        assert len(service.list_backups(game_id)) == 2, "合并是追加一份, 不是另建游戏"
        assert app._game_id == game_id, "导入完要停在合并的目标游戏上"
        assert "已导入" in app._last_feedback[1]
        assert seen["prompt"].match_text == tr(
            "dialog.import_match", name="库里的源游戏"
        )
        assert [option.game_id for option in seen["prompt"].targets] == [game_id]
        assert [option.selected for option in seen["prompt"].targets] == [True]
        assert [key for key, _text in seen["strategies"]] == [
            STRATEGY_NEW,
            STRATEGY_MERGE,
            STRATEGY_SKIP,
        ]
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_import_picker_cancelled_imports_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """没选文件: 连体检都不做, 不进忙碌态也不弹对话框."""
    opened: list[Any] = []
    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=None
    )
    monkeypatch.setattr(
        main_mod, "import_package_dialog", lambda *args, **kwargs: opened.append(args)
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _pump(app)

        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert app._busy is False
        assert opened == []
        assert service.list_games() == []

        # 忙碌时再点一次: 连文件选择框都不该弹(异步流程不能被插队).
        picked: list[Any] = []
        monkeypatch.setattr(
            main_mod, "pick_file", lambda **kwargs: picked.append(kwargs)
        )
        app._busy = True
        app._on_import_package()
        app._busy = False

        assert picked == []
    finally:
        app.destroy()


def test_gui_import_of_a_broken_package_reports_an_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """选了一个不是归档包的文件: 报错、不进忙碌态, 也不弹冲突对话框."""
    broken = tmp_path / "broken.zip"
    broken.write_text("definitely not a zip", encoding="utf-8")
    opened: list[Any] = []
    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(broken)
    )
    monkeypatch.setattr(
        main_mod, "import_package_dialog", lambda *args, **kwargs: opened.append(args)
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.ERROR
        assert app._busy is False, "读包失败也要把忙碌状态收回去"
        assert opened == []
        assert service.list_games() == []
    finally:
        app.destroy()


def test_gui_import_dialog_cancelled_imports_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """体检完用户又说取消: 什么都不写, 且不留下忙碌状态."""
    package = _exported_package(tmp_path)
    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package), import_choice=None
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert app._busy is False
        assert service.list_games() == []
        assert list((tmp_path / "import-backups").glob("**/*")) == []
    finally:
        app.destroy()


def _real_service_with_games(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    names: Sequence[str],
) -> tuple[Any, list[str]]:
    """真实后端 + 若干款"各带一个存档位置与一次备份"的游戏(批量导出用例用).

    批量导出必须落在真实后端上才能断言"包真的写出来了"; 每个游戏的内容不同, 否则
    第二次备份会被"存档未变化"跳过。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    database = Database(tmp_path / "batch.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=tmp_path / "batch-backups")
    game_ids: list[str] = []
    for index, name in enumerate(names):
        game_id = service.add_game(name).game_id
        save_dir = tmp_path / f"save-{index}"
        save_dir.mkdir()
        (save_dir / "slot1.dat").write_text(f"state-{index}", encoding="utf-8")
        service.add_location(game_id, path=str(save_dir), kind="directory")
        service.run_backup_now(game_id)
        game_ids.append(game_id)
    return service, game_ids


def _exported_batch(tmp_path: Path, *, count: int = 2) -> Path:
    """造一台源机器并导出一个批量包(两款游戏各一次备份)."""
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    root = tmp_path / "batch-source"
    database = Database(root / "app.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=root / "backups")
    game_ids: list[str] = []
    for index in range(count):
        game_id = service.add_game(f"源游戏{index}").game_id
        save = root / f"save-{index}"
        save.mkdir(parents=True)
        (save / "slot1.dat").write_text(f"state-{index}", encoding="utf-8")
        service.add_location(game_id, path=str(save), kind="directory")
        service.run_backup_now(game_id)
        game_ids.append(game_id)
    package = tmp_path / "batch.archive.zip"
    service.run_export_batch(game_ids, str(package))
    return package


def _batch_entries(package: Path) -> tuple[str, ...]:
    """批量包里的内层条目名(按清单顺序; 跳过/合并的选择要用它当键)."""
    from archive_management.services.export_format import read_batch_package

    with read_batch_package(package) as contents:
        return tuple(item.entry for item in contents.games)


def test_gui_export_batch_writes_the_package_the_user_ticked(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """主页「批量导出…」: 多选对话框 → 保存位置 → 真的写出批量包, 反馈带真实计数."""
    from archive_management.services.export_format import read_batch_package
    from archive_management.ui.models import size_label

    service, game_ids = _real_service_with_games(
        monkeypatch, tmp_path, ["甲游戏", "乙游戏"]
    )
    destination = tmp_path / "saves" / "batch.archive.zip"
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> BatchExportChoice:
        """记录界面拼给对话框的候选, 并按"用户只勾了第二款"返回."""
        seen.update(kwargs)
        return BatchExportChoice(game_ids=(game_ids[1],))

    def save_file(**kwargs: Any) -> str:
        """记录保存框的入参并返回用户"选定"的路径."""
        seen.update(kwargs)
        destination.parent.mkdir(exist_ok=True)
        return str(destination)

    monkeypatch.setattr(main_mod, "export_batch_dialog", choose)
    monkeypatch.setattr(main_mod, "pick_save_file", save_file)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._home_page._export_batch_btn.invoke()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        assert destination.is_file(), "批量包必须写在用户选的路径上"
        with read_batch_package(destination, verify_hashes=True) as contents:
            names = [item.name for item in contents.games]
            files = sum(len(item.package.entries) for item in contents.games)
            size = sum(
                entry.size for item in contents.games for entry in item.package.entries
            )
        assert names == ["乙游戏"], "只该导出用户勾选的那一款"
        assert app._last_feedback[1] == tr(
            "result.export_batch_done",
            games=1,
            file=destination.name,
            backups=1,
            files=files,
            size=size_label(size),
        )
        # 对话框拿到的是全部候选, 默认文件名带批次与后缀.
        assert [option.name for option in seen["prompt"].options] == [
            "甲游戏",
            "乙游戏",
        ]
        assert str(seen["initialfile"]).startswith("batch-1games-")
        assert str(seen["initialfile"]).endswith(".archive.zip")
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_export_batch_cancel_paths_write_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """对话框取消 / 一个都没勾 / 保存框取消: 三种都不写文件、不进忙碌态."""
    service, game_ids = _real_service_with_games(monkeypatch, tmp_path, ["甲游戏"])

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        def run(dialog_result: Any) -> None:
            """换一个对话框返回值再点一次「批量导出…」."""
            monkeypatch.setattr(
                main_mod, "export_batch_dialog", lambda *_a, **_k: dialog_result
            )
            app._home_page._export_batch_btn.invoke()
            _drain(app)
            assert app._busy is False

        run(None)
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_canceled")

        run(BatchExportChoice(game_ids=()))
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_none")

        # 保存框取消: 对话框已经确认过(勾了一款), 但文件选择框被取消.
        run(BatchExportChoice(game_ids=(game_ids[0],)))
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_canceled")

        assert list(tmp_path.glob("*.archive.zip")) == []
        assert list(tmp_path.glob("**/*.partial-*")) == []
    finally:
        app.destroy()


def test_gui_export_batch_hides_archived_games_and_reports_an_empty_library(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """归档的游戏不提供; 一款可导的都没有时连对话框都不弹."""
    service, game_ids = _real_service_with_games(
        monkeypatch, tmp_path, ["甲游戏", "乙游戏"]
    )
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> BatchExportChoice:
        seen.update(kwargs)
        return BatchExportChoice(game_ids=())

    monkeypatch.setattr(main_mod, "export_batch_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        service.set_game_archived(game_ids[1], True)

        app._on_export_batch()

        assert [option.name for option in seen["prompt"].options] == ["甲游戏"]

        # 只剩下归档的那一款时: 直接说明原因, 不弹一个空对话框.
        opened: list[Any] = []
        monkeypatch.setattr(
            main_mod, "export_batch_dialog", lambda *args, **_k: opened.append(args)
        )
        service.set_game_archived(game_ids[0], True)

        app._on_export_batch()

        assert opened == []
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.export_batch_empty")
    finally:
        app.destroy()


def test_gui_import_batch_imports_the_whole_batch_per_choice(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """批量导入: 游戏与备份真的出现, 逐款的策略与位置映射都被照办."""
    from archive_management.application.imports import (
        STRATEGY_NEW,
        STRATEGY_SKIP,
    )

    package = _exported_batch(tmp_path)
    entries = _batch_entries(package)
    target_save = tmp_path / "target-save"
    target_save.mkdir()
    seen: dict[str, Any] = {}

    def choose(*_args: Any, **kwargs: Any) -> BatchImportSelection:
        """记录界面拼给对话框的逐行选项, 并按"第一行新建+映射, 第二行跳过"返回."""
        seen.update(kwargs)
        return BatchImportSelection(
            choices={
                entries[0]: ImportChoice(
                    strategy=STRATEGY_NEW,
                    target_game_id=None,
                    locations={0: str(target_save)},
                ),
                entries[1]: ImportChoice(
                    strategy=STRATEGY_SKIP, target_game_id=None, locations={}
                ),
            }
        )

    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=str(package)
    )
    monkeypatch.setattr(main_mod, "batch_import_dialog", choose)

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        app._on_import_package()
        _drain(app)

        assert _feedback_kind(app) == FeedbackKind.SUCCESS
        games = service.list_games()
        assert [game.name for game in games] == ["源游戏0"], "跳过那一款不该进库"
        assert len(_service_backups(service, games[0].game_id)) == 1
        assert [item.path for item in service.list_locations(games[0].game_id)] == [
            normalize_path(str(target_save))
        ]
        # 只新建了一款, 因此导入完就停在它上面.
        assert app._game_id == games[0].game_id
        assert "已导入 2 款游戏" in app._last_feedback[1]
        assert "整款跳过 1 款" in app._last_feedback[1]
        assert _home_item(app._home_page, games[0].game_id).backup_count == 1
        # 对话框拿到的逐行数据: 两款游戏各一行, 存档位置已经预填到本机路径上.
        rows = seen["prompt"].rows
        assert [row.entry for row in rows] == list(entries)
        assert rows[0].locations[0].default == normalize_path(
            str(tmp_path / "batch-source" / "save-0")
        )
        assert app._busy is False
    finally:
        app.destroy()


def test_gui_import_batch_cancel_paths_import_nothing_extra(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """取消的三条路径: 没选包 / 对话框取消 / 导入中途取消(已导完的保留)."""
    from archive_management.application.imports import STRATEGY_NEW

    package = _exported_batch(tmp_path)
    entries = _batch_entries(package)
    target_save = tmp_path / "target-save"
    target_save.mkdir()

    service, _database = _empty_real_service(
        monkeypatch, tmp_path, import_package_path=None
    )

    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)

        # 1) 没选包: 连体检都不做.
        app._on_import_package()
        _pump(app)
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert service.list_games() == []
        assert app._busy is False

        # 2) 对话框取消: 什么都不导入.
        monkeypatch.setattr(main_mod, "pick_file", lambda **_k: str(package))
        monkeypatch.setattr(main_mod, "batch_import_dialog", lambda *_a, **_k: None)
        app._on_import_package()
        _drain(app)
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("action.import_canceled")
        assert service.list_games() == []
        assert app._busy is False

        # 3) 取消探针一响: 整批停下, 界面给出"已取消"的提示且不留在忙碌态.
        #
        #    这里**不抢时序**: 早先这条路径靠 time.sleep 造出来的窗口"卡在导入进行中",
        #    满负载(全量 + 覆盖率)下工作线程会先把整批导完, 于是断言"其余的不再导入"
        #    红过(实测 `assert ['源游戏0', '源游戏1'] == ['源游戏0']`)。
        #    "取消落在第一款之后、已导完的保留"那条**按游戏边界**的语义由
        #    tests/integration/test_pipeline_batch.py 的
        #    test_cancelling_between_games_keeps_the_finished_one_and_writes_nothing_partial
        #    确定性地钉住(探针看数据库, 不靠时间); 这一段守的是界面侧的接线:
        #    探针一响 → 后端回"已取消" → 窗口回到非忙碌态。
        def probe() -> bool:
            """取消探针的替身: 第一条就问一次取消, 导入立刻停下."""
            return True

        monkeypatch.setattr(service, "_cancel_requested", probe)
        monkeypatch.setattr(
            main_mod,
            "batch_import_dialog",
            lambda *_a, **_k: BatchImportSelection(
                choices={
                    entry: ImportChoice(
                        strategy=STRATEGY_NEW,
                        target_game_id=None,
                        locations={0: str(target_save)},
                    )
                    for entry in entries
                }
            ),
        )

        app._on_import_package()
        _drain(app)

        assert service.list_games() == [], "取消之后一款都不该落库"
        assert _feedback_kind(app) == FeedbackKind.INFO
        assert app._last_feedback[1] == tr("result.import_batch_canceled")
        assert app._busy is False
    finally:
        app.destroy()


def test_export_batch_is_ignored_while_busy(monkeypatch: pytest.MonkeyPatch) -> None:
    """忙碌时批量导出直接返回: 连"勾选游戏"的多选对话框都不弹."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    asked: list[str] = []
    monkeypatch.setattr(
        main_mod, "export_batch_dialog", lambda *_a, **_k: asked.append("dialog")
    )
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        _pump(app)
        app._set_busy(True)

        app._on_export_batch()

        assert asked == [], "忙碌时不该弹出批量导出对话框"
        app._set_busy(False)
        assert app._busy is False
    finally:
        app.destroy()
