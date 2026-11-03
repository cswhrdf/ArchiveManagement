"""
存储位置(P2b): 备份根覆盖的校验与规范化、总览口径、两个后端的写回语义与"打开位置"。

范围红线: 只允许改备份根; 数据库/配置/日志/导出/封面一律留在内置位置 —— 用例
里反复断言"改了备份根之后其余位置纹丝不动"。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from archive_management.application.storage import (
    REASON_SAVE_PATH_CLASH,
    backup_root_error,
    normalize_backup_root,
)
from archive_management.config import AppConfig, load_config, save_config
from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.logging_config import LOG_FILENAME
from archive_management.services import reveal
from archive_management.services.scheduler import BackupScheduler, ManualBackend
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.models import StorageOverview, storage_error_text
from archive_management.ui.sql_backend import SqlArchiveService

pytestmark = [
    pytest.mark.backend,
    pytest.mark.critical,
    pytest.mark.epic("数据持久化"),
    pytest.mark.feature("存储位置设置"),
    pytest.mark.story("备份根覆盖"),
    # 真实 SQLite + 配置文件 + 文件系统, 按层定义归入 integration.
    pytest.mark.layer("integration"),
]


def _builtin_paths(tmp_path: Path) -> ApplicationPaths:
    """一组全部落在 tmp_path 下的内置路径(避免碰真实系统目录)."""
    return ApplicationPaths(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        cache_dir=tmp_path / "cache",
    )


def _storage_service(
    tmp_path: Path,
    *,
    config_path: Path | None = None,
    builtin: ApplicationPaths | None = None,
    backup_root: Path | None = None,
) -> SqlArchiveService:
    """构造带手动调度后端的真实服务(sql_support._service 不带 builtin_paths).

    ``backup_root`` 默认取独立目录; 生产里会话根与内置根一致(无覆盖时), 要验证
    "恢复后待重启提示消失"就显式传内置根。
    """
    database = Database(tmp_path / "app.db")
    database.migrate()
    return SqlArchiveService(
        database,
        backup_root=backup_root if backup_root is not None else tmp_path / "backups",
        scheduler=BackupScheduler(backend=ManualBackend()),
        process_provider=lambda: [],
        config_path=config_path,
        builtin_paths=builtin,
    )


# ------------------------------------------------------------ 校验与规范化


def test_blank_input_restores_the_builtin_location() -> None:
    """空输入(含纯空白) = 恢复内置位置: 校验放行, 规范化为空串(配置里存 None)."""
    assert backup_root_error("", protected=(Path("D:/app/data"),)) is None
    assert backup_root_error("   ", protected=(Path("D:/app/data"),)) is None
    assert normalize_backup_root("") == ""
    assert normalize_backup_root("  ") == ""


def test_relative_or_dangerous_targets_are_rejected(tmp_path: Path) -> None:
    """相对路径、备份自身/数据目录、以及把它们包进去的父目录都不许当新根."""
    data = tmp_path / "data"
    backups = data / "backups"
    assert backup_root_error("newroot", protected=(data, backups)) == "not_absolute"
    assert backup_root_error(str(backups), protected=(data, backups)) == "protected"
    # 数据目录本身也在保护名单里(数据库/配置/日志都在它下面), 直接命中同样拒绝.
    assert backup_root_error(str(data), protected=(data, backups)) == "protected"
    # tmp_path 是 data 的父目录: 新根会把它包进去, 算"包含受保护目录".
    assert (
        backup_root_error(str(tmp_path), protected=(data, backups))
        == "contains_protected"
    )


def test_save_path_clash_beats_writability(tmp_path: Path) -> None:
    """与某款游戏的原始存档位置相同: 那会让"备份绝不写进原始目录"失效, 必须拒绝."""
    save = tmp_path / "save"
    save.mkdir()
    # 名单里先放一条不匹配的: 逐条比对(而不是只看第一条)才能走到命中的那条。
    other = tmp_path / "other-save"
    reason = backup_root_error(
        str(save),
        protected=(tmp_path / "backups",),
        save_paths=(str(other), str(save)),
    )
    assert reason == REASON_SAVE_PATH_CLASH


def test_unwritable_target_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目标(或其最近已存在父目录)写不进去就拒绝: chmod 在 Windows 上不可靠, 替身注入."""
    monkeypatch.setattr(
        "archive_management.application.storage.is_writable_target",
        lambda _raw: False,
    )
    assert (
        backup_root_error(str(tmp_path / "newroot"), protected=(Path("D:/b"),))
        == "not_writable"
    )


def test_normalize_resolves_to_an_absolute_string(tmp_path: Path) -> None:
    """规范化把候选变成绝对路径文本(带首尾空白也认)."""
    normalized = normalize_backup_root(f"  {tmp_path / 'new root'} ")
    assert normalized == str((tmp_path / "new root").resolve())
    assert Path(normalized).is_absolute()


# ------------------------------------------------------------ 展示模型


def _overview(current: str, following: str) -> StorageOverview:
    return StorageOverview(
        backup_root=current,
        next_backup_root=following,
        database_path="db",
        config_path="cfg",
        log_file="log",
        exports_dir="exports",
        artwork_dir="artwork",
    )


def test_restart_pending_reflects_the_two_roots() -> None:
    """ "重启后"提示只在当前根与下次根不一致时出现(状态由行的有无表达)."""
    assert _overview("D:/a", "D:/a").restart_pending is False
    assert _overview("D:/a", "E:/b").restart_pending is True


def test_storage_error_text_maps_known_codes_and_passes_unknown_through() -> None:
    """已知原因代码翻成用户文案; 未知代码原样返回(比一串键名可读)."""
    assert storage_error_text("not_absolute") == tr("settings.storage_err_not_absolute")
    assert storage_error_text("not_absolute") != "not_absolute"
    assert storage_error_text("some_future_code") == "some_future_code"


# ------------------------------------------------------------ 真实后端


def test_sql_backend_writes_the_override_and_keeps_builtin_paths(
    tmp_path: Path,
) -> None:
    """接受合法候选: 只写配置, 当前会话根不动, 内置位置字段不随覆盖移动."""
    builtin = _builtin_paths(tmp_path)
    config_path = builtin.config_path
    config_path.parent.mkdir(parents=True)
    save_config(AppConfig(), config_path)
    service = _storage_service(tmp_path, config_path=config_path, builtin=builtin)
    new_root = tmp_path / "elsewhere"

    service.set_backup_root(str(new_root))

    stored = load_config(config_path).storage.backup_root
    assert stored is not None
    assert Path(stored) == new_root.resolve()
    overview = service.storage_overview()
    assert overview.backup_root == str(tmp_path / "backups"), "当前会话根不许动"
    assert Path(overview.next_backup_root) == new_root.resolve()
    assert overview.restart_pending is True
    # P2b 红线: 数据库/配置/日志/导出/封面都还是内置位置。
    assert Path(overview.database_path) == builtin.database_path
    assert Path(overview.config_path) == builtin.config_path
    assert Path(overview.log_file) == builtin.log_dir / LOG_FILENAME
    assert Path(overview.exports_dir) == builtin.exports_dir
    assert Path(overview.artwork_dir) == builtin.artwork_dir


def test_sql_backend_restoring_writes_none_and_clears_the_pending_hint(
    tmp_path: Path,
) -> None:
    """恢复默认(空输入): 配置里存 None, "下次启动"回到内置根, 待重启提示消失."""
    builtin = _builtin_paths(tmp_path)
    config_path = builtin.config_path
    config_path.parent.mkdir(parents=True)
    save_config(AppConfig(), config_path)
    # 会话根与内置根一致(生产无覆盖时的常态): 恢复后两口径重合, 提示才该消失.
    service = _storage_service(
        tmp_path,
        config_path=config_path,
        builtin=builtin,
        backup_root=builtin.backup_root,
    )

    service.set_backup_root(str(tmp_path / "elsewhere"))
    service.set_backup_root("")

    assert load_config(config_path).storage.backup_root is None
    overview = service.storage_overview()
    assert Path(overview.next_backup_root) == builtin.backup_root
    assert overview.restart_pending is False


def test_sql_backend_rejects_a_save_path_clash_without_touching_config(
    tmp_path: Path,
) -> None:
    """候选与某款游戏的原始存档位置相同: 抛带原因代码的 ValueError, 配置不动."""
    config_path = tmp_path / "config.json"
    save_config(AppConfig(), config_path)
    service = _storage_service(tmp_path, config_path=config_path)
    game_id = service.add_game("Demo").game_id
    save = tmp_path / "save"
    save.mkdir()
    service.add_location(game_id, path=str(save), kind="directory")

    with pytest.raises(ValueError, match="save_path_clash"):
        service.set_backup_root(str(save))

    assert load_config(config_path).storage.backup_root is None


def test_sql_backend_without_a_config_path_refuses_instead_of_lying(
    tmp_path: Path,
) -> None:
    """没有配置文件可写(未注入 config_path): 报 no_config, 不静默"成功"."""
    service = _storage_service(tmp_path)

    with pytest.raises(ValueError, match="no_config"):
        service.set_backup_root(str(tmp_path / "somewhere"))


def test_sql_backend_overview_reads_the_override_back_from_config(
    tmp_path: Path,
) -> None:
    """总览的"下次启动"每次现读配置: 重启前另开的会话也能看到待生效的覆盖."""
    builtin = _builtin_paths(tmp_path)
    config_path = builtin.config_path
    config_path.parent.mkdir(parents=True)
    config = AppConfig()
    config.storage.backup_root = str((tmp_path / "moved").resolve())
    save_config(config, config_path)
    service = _storage_service(tmp_path, config_path=config_path, builtin=builtin)

    overview = service.storage_overview()

    assert Path(overview.next_backup_root) == (tmp_path / "moved").resolve()
    assert overview.restart_pending is True


def test_sql_backend_overview_without_paths_falls_back_to_the_session_root(
    tmp_path: Path,
) -> None:
    """纯测试装配(没注入 config_path 也没注入内置路径): 总览回落到当前会话根。

    位置字段一律空串而不是伪造一个路径: 设置页据此收起"打开位置"一排; "下次
    启动"与当前会话根一致, 也就不该有待重启提示。
    """
    service = _storage_service(tmp_path)

    overview = service.storage_overview()

    session_root = str(tmp_path / "backups")
    assert overview.backup_root == session_root
    assert overview.next_backup_root == session_root
    assert overview.restart_pending is False
    assert overview.database_path == ""
    assert overview.config_path == ""
    assert overview.log_file == ""
    assert overview.exports_dir == ""
    assert overview.artwork_dir == ""


# ------------------------------------------------------------ 演示后端


def test_demo_backend_keeps_the_override_in_memory_only(tmp_path: Path) -> None:
    """演示后端: 覆盖只在内存里, 但校验与总览口径与真实后端一致."""
    demo = DemoArchiveService(delay=0)
    assert demo.storage_overview().restart_pending is False

    demo.set_backup_root(str(tmp_path / "demo-root"))
    after = demo.storage_overview()
    assert Path(after.next_backup_root) == (tmp_path / "demo-root").resolve()
    assert after.restart_pending is True

    demo.set_backup_root("")
    assert demo.storage_overview().restart_pending is False

    with pytest.raises(ValueError, match="not_absolute"):
        demo.set_backup_root("relative/path")


# ------------------------------------------------------------ 打开位置


def test_reveal_falls_back_to_the_nearest_existing_ancestor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """目录还不存在(下次启动才建)时打开最近的已存在父目录(Windows 分支)."""
    opened: list[str] = []
    monkeypatch.setattr(reveal, "current_platform", lambda: "windows")
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    here = tmp_path / "a"
    here.mkdir()

    reveal.reveal_in_file_manager(str(here / "b" / "c"))

    assert opened == [str(here)]


def test_reveal_uses_the_platform_file_manager_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """macOS 走 ``open``, Linux 走 ``xdg-open``; 已存在的目录原样传给命令."""
    ran: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False) -> None:
        ran.append(args)

    monkeypatch.setattr("subprocess.run", fake_run)
    for family, command in (("macos", "open"), ("linux", "xdg-open")):
        ran.clear()
        monkeypatch.setattr(reveal, "current_platform", lambda f=family: f)
        reveal.reveal_in_file_manager(str(tmp_path))
        assert ran == [[command, str(tmp_path)]]
    # 文件行不打开文件本体: macOS 选中它(``open -R``), Linux 没有统一的"选中"
    # 惯例、退到打开其父目录 —— 两边落点都是"所在目录"。
    log = tmp_path / "app.log"
    log.write_text("x", encoding="utf-8")
    ran.clear()
    monkeypatch.setattr(reveal, "current_platform", lambda: "macos")
    reveal.reveal_in_file_manager(str(log))
    assert ran == [["open", "-R", str(log)]]
    ran.clear()
    monkeypatch.setattr(reveal, "current_platform", lambda: "linux")
    reveal.reveal_in_file_manager(str(log))
    assert ran == [["xdg-open", str(tmp_path)]]


def test_reveal_selects_an_existing_file_on_windows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Windows 对文件行走资源管理器的"选中"(``explorer /select,``), 绝不直接打开文件."""
    monkeypatch.setattr(reveal, "current_platform", lambda: "windows")
    ran: list[list[str]] = []

    def fake_run(args: list[str], *, check: bool = False) -> None:
        ran.append(args)

    monkeypatch.setattr("subprocess.run", fake_run)
    opened: list[str] = []
    monkeypatch.setattr(os, "startfile", opened.append, raising=False)
    target = tmp_path / "archive-management.db"
    target.write_bytes(b"x")

    reveal.reveal_in_file_manager(str(target))

    assert ran == [["explorer", f"/select,{target}"]]
    assert opened == [], "文件不能被当作目录用 os.startfile 打开本体"


def test_reveal_swallows_open_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """打开失败只记日志: 便利动作不值得打断用户(不许把异常抛进界面)."""
    monkeypatch.setattr(reveal, "current_platform", lambda: "windows")

    def boom(_path: str) -> None:
        raise OSError("拒绝访问")

    monkeypatch.setattr(os, "startfile", boom, raising=False)
    reveal.reveal_in_file_manager(str(tmp_path))  # 不抛即通过
