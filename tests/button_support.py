"""
由 test_gui_buttons.py 拆分出的共享测试基建(被多个主题文件引用), 拆分原则与映射见 docs/test-refactor-plan.md。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from gui_support import close_child_windows

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入

except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

import archive_management.ui.discovery_page as disc_mod
import archive_management.ui.home_page as home_page_mod
import archive_management.ui.main_window as main_mod
import archive_management.ui.manage_window as mgr_mod
import archive_management.ui.schedule_window as sched_mod
from archive_management.exceptions import HotkeyError
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui.backend import ArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import (
    BatchExportChoice,
    BatchImportSelection,
    FeedbackKind,
    ImportChoice,
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


_DRAIN_TIMEOUT_SECONDS = 20.0


def _patch_dialogs(
    monkeypatch: pytest.MonkeyPatch,
    *,
    ask_text: str = "",
    ask_text_queue: list[str] | None = None,
    branch_name: str = "测试分支",
    confirm: bool = True,
    edit_result: tuple[str, str] | None = ("新名字", "新描述"),
    schedule_result: tuple[str, str] | None = ("", "3"),
    restore_result: bool | None = True,
    import_result: tuple[str, tuple[str, ...]] | None = ("导入名", ()),
    tags_result: tuple[str, ...] | None = ("解谜",),
    export_path: str | None = None,
    import_package_path: str | None = None,
    import_choice: ImportChoice | None = None,
    export_batch_choice: BatchExportChoice | None = None,
    batch_import_choice: BatchImportSelection | None = None,
) -> None:
    """把模态对话框替换为自动应答, 避免 wait_window 阻塞测试线程.

    需要输入路径/文本的测试应通过 ``ask_text`` 显式传入由 ``tmp_path``
    派生的跨平台路径; 未触发文本输入的动作无需关心该默认空值.
    ``ask_text_queue`` 用于一次动作会连续弹出多个输入框的场景(按顺序取值);
    ``edit_result`` / ``schedule_result`` / ``restore_result`` 分别对应单窗口
    编辑对话框、定时任务对话框与恢复选项对话框的返回值(``None`` 表示取消),
    其中 ``restore_result`` 表示是否勾选"恢复前先创建安全点"; ``export_path``
    是导出时保存对话框的返回值(``None`` 表示用户取消), 需要真的写出包的用例
    自己传 ``tmp_path`` 下的路径。``import_package_path`` 与 ``import_choice``
    同理对应"选要导入的包"与导入冲突对话框(``None`` 就是用户取消);
    ``export_batch_choice`` 与 ``batch_import_choice`` 是批量导出与批量导入
    对话框的返回值(``None`` 同样表示取消)。
    """
    answers = list(ask_text_queue or [])

    def next_text(*_args: Any, **_kwargs: Any) -> str:
        if answers:
            return answers.pop(0)
        return ask_text

    monkeypatch.setattr(main_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(main_mod, "ask_text", next_text)
    monkeypatch.setattr(main_mod, "ask_branch_name", lambda *_a, **_k: branch_name)
    monkeypatch.setattr(main_mod, "info_dialog", lambda *_a, **_k: None)
    monkeypatch.setattr(main_mod, "edit_backup_dialog", lambda *_a, **_k: edit_result)
    monkeypatch.setattr(main_mod, "restore_dialog", lambda *_a, **_k: restore_result)
    monkeypatch.setattr(mgr_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(mgr_mod, "ask_text", next_text)
    monkeypatch.setattr(mgr_mod, "info_dialog", lambda *_a, **_k: None)
    # 定时备份配置入口已移到游戏设置与全局任务窗口, 两者都经 schedule_window 弹框.
    monkeypatch.setattr(sched_mod, "schedule_dialog", lambda *_a, **_k: schedule_result)
    monkeypatch.setattr(sched_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(sched_mod, "info_dialog", lambda *_a, **_k: None)
    # 游戏发现已合并进游戏主页的页面, 同样需要文本输入/确认/提示的自动应答.
    monkeypatch.setattr(disc_mod, "ask_text", next_text)
    monkeypatch.setattr(disc_mod, "confirm_dialog", lambda *_a, **_k: confirm)
    monkeypatch.setattr(disc_mod, "info_dialog", lambda *_a, **_k: None)
    # 导入对话框返回 (名称, 存档路径): 用它可以检查"确认的路径才会写库".
    monkeypatch.setattr(disc_mod, "import_game_dialog", lambda *_a, **_k: import_result)
    # 游戏主页窗口需要文本输入(存档位置)、标签编辑对话框与错误提示的自动应答.
    monkeypatch.setattr(home_page_mod, "ask_text", next_text)
    monkeypatch.setattr(
        home_page_mod, "edit_tags_dialog", lambda *_a, **_k: tags_result
    )
    monkeypatch.setattr(home_page_mod, "info_dialog", lambda *_a, **_k: None)
    # 导出先弹系统保存对话框(主线程阻塞), 同样要自动应答.
    monkeypatch.setattr(main_mod, "pick_save_file", lambda *_a, **_k: export_path)
    # 导入先弹系统文件选择框, 体检后再弹冲突对话框(两者同样要自动应答).
    monkeypatch.setattr(main_mod, "pick_file", lambda *_a, **_k: import_package_path)
    monkeypatch.setattr(
        main_mod, "import_package_dialog", lambda *_a, **_k: import_choice
    )
    # 批量导出/导入的对话框同样要替身(它们也是模态框).
    monkeypatch.setattr(
        main_mod, "export_batch_dialog", lambda *_a, **_k: export_batch_choice
    )
    monkeypatch.setattr(
        main_mod, "batch_import_dialog", lambda *_a, **_k: batch_import_choice
    )


def _pump(app: ArchiveApp) -> None:
    app.update_idletasks()
    app.update()


def _drain(app: ArchiveApp) -> None:
    """等后台线程结果经消息队列回到主线程(忙碌标志复位且消息已分发).

    恢复与会话级的备份会真的读写文件, 在带覆盖率或高负载机器上会明显变慢,
    因此这里等的是"条件成立"(忙碌标志复位), 而不是固定睡多久; 超时时给出
    指向审计日志的失败信息, 免得后续断言莫名失败。
    """
    deadline = time.monotonic() + _DRAIN_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        _pump(app)
        app._poll_messages()  # 把后台结果搬回主线程
        _pump(app)
        if not getattr(app, "_busy", False):
            return
        time.sleep(0.005)
    raise AssertionError(
        f"后台操作在 {_DRAIN_TIMEOUT_SECONDS:.0f} 秒内未完成: "
        "若属于备份/恢复, 请查审计日志(backup.* / restore.*)确认是否已失败"
    )


def _service_backups(service: ArchiveService, game_id: str) -> list[Any]:
    """读取真实后端的备份列表, 数量不足时给出可诊断的失败信息.

    后台备份失败(例如 Windows 上临时目录被扫描器占用)时, 直接写
    ``service.list_backups(...)[0]`` 只会抛 IndexError, 看不出"备份根本没成功";
    审计日志里对应的是 ``backup.create.failed``。
    """
    items = service.list_backups(game_id)
    if not items:
        raise AssertionError(
            "预期至少一个备份, 实际为空: 后台备份未成功, 请查审计日志 backup.create.failed"
        )
    return list(items)


def _feedback_kind(app: ArchiveApp) -> FeedbackKind:
    """返回最近一次反馈的等级.

    经函数返回可避免 mypy 对 ``app._last_feedback[0]`` 这个索引表达式做
    字面量收窄(收窄后再比较其它等级会被判为 non-overlapping)。
    """
    return app._last_feedback[0]


def _home_item(page: Any, game_id: str) -> Any:
    """取出主页列表里的某个游戏项(顺便收窄可选类型)."""
    board = page._board
    assert board is not None
    return next(entry for entry in board.games if entry.game_id == game_id)


def _label_texts(widget: Any) -> list[str]:
    """递归收集控件树里所有标签的文案(便于断言界面不再显示某些内容)."""
    import customtkinter as ctk

    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkLabel):
            found.append(str(child.cget("text")))
        found.extend(_label_texts(child))
    return found


def _button_texts(widget: Any) -> list[str]:
    """递归收集控件树里所有按钮的文案(便于断言某个动作已从界面移除)."""
    import customtkinter as ctk

    found: list[str] = []
    for child in widget.winfo_children():
        if isinstance(child, ctk.CTkButton):
            found.append(str(child.cget("text")))
        found.extend(_button_texts(child))
    return found


def _new_app(
    backend: ArchiveService,
    *,
    hotkeys: GlobalHotkeyService | None = None,
    paths: ApplicationPaths | None = None,
) -> ArchiveApp:
    """构造主窗口; 无显示环境时抛出 TclError 由调用方转 skip.

    缺省不注册系统级快捷键, 避免遗留键盘钩子或误触发备份; 需要验证"注册成功"
    的用例自行传入带替身后端的服务。
    """
    return ArchiveApp(
        backend,
        title="按钮测试",
        hotkeys=(
            hotkeys
            if hotkeys is not None
            else GlobalHotkeyService(backend=UnavailableBackend("测试环境禁用"))
        ),
        paths=paths,
    )


class _RecordingBackend:
    """记录注册项的后端替身: 用于验证快捷键注册与持久化, 不碰真实系统快捷键."""

    def __init__(self) -> None:
        self.registered: dict[str, str] = {}
        self.suspends = 0
        self.resumes = 0

    def register(self, accelerator: str, callback: Any) -> object:
        """模拟注册; 重复注册当作失败."""
        if accelerator in self.registered:
            raise HotkeyError(f"快捷键已被占用: {accelerator}")
        self.registered[accelerator] = accelerator
        return accelerator

    def unregister(self, handle: object) -> None:
        """移除注册项."""
        self.registered.pop(str(handle), None)

    def suspend(self) -> None:
        """记录暂停次数."""
        self.suspends += 1

    def resume(self) -> None:
        """记录恢复次数."""
        self.resumes += 1

    def stop(self) -> None:
        """空实现."""


def _location_ids(app: ArchiveApp, game_id: str) -> list[str]:
    """读取某个游戏当前的存档位置 id 列表."""
    return [item.location_id for item in app.backend.list_locations(game_id)]


def _wait_for(
    app: ArchiveApp, predicate: Callable[[], bool], seconds: float = 3.0
) -> bool:
    """轮询等待条件成立; 录制收尾用的是 ``after`` 计时器, 需要真实事件循环."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _pump(app)
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _open_manager(
    app: ArchiveApp,
    *,
    game_id: str,
    name: str,
    enabled: bool = True,
    backup_location: str,
    on_change: Callable[[], None] | None = None,
) -> Any:
    from archive_management.ui.manage_window import ManageGameWindow
    from archive_management.ui.palette import Palette

    manager: Any = ManageGameWindow(
        app,
        backend=app.backend,
        palette=Palette.for_theme(app._theme),
        game_id=game_id,
        name=name,
        enabled=enabled,
        backup_location=backup_location,
        # 缺省不刷新: 需要验证主窗口反应的用例传入 app._refresh_after_manage.
        on_change=on_change or (lambda: None),
    )
    _pump(app)
    return manager


def _real_service_with_one_backup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    export_path: str | None = None,
) -> tuple[ArchiveService, str, Path]:
    """建一个真实后端(一款游戏 + 一个存档位置 + 一次备份)并打好对话框替身.

    返回后端、游戏 id 与存档目录。导出用例需要"真的写出文件", 所以这里不能用演示
    后端: 它不碰文件系统, 无法验证包是否落在用户选的路径上。
    """
    from archive_management.infrastructure.database import Database
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch, export_path=export_path)
    db = Database(tmp_path / "export.db")
    db.migrate()
    service = SqlArchiveService(db, backup_root=tmp_path / "backups")
    game = service.add_game("真实游戏")
    save_dir = tmp_path / "save"
    save_dir.mkdir()
    (save_dir / "slot1.dat").write_text("progress", encoding="utf-8")
    service.add_location(game.game_id, path=str(save_dir), kind="directory")
    service.run_backup_now(game.game_id)
    return service, game.game_id, save_dir


def _close_toplevels(app: Any) -> None:
    """关掉用例自己打开的附属窗口(主窗口留给夹具收尾).

    与 ``gui_support.close_child_windows`` 是同一件事 —— 夹具收尾时也会先做这一步,
    这里留个薄封装, 供"中途想控制时机"的用例用: Toplevel 在 Tcl 侧挂在根窗口下、
    在 Python 侧挂在锚点控件下, 先按 Python 的账把它们拆干净, 主窗口的销毁链就少一处
    半路断掉的机会(断了会把根留在会话里, 后面用例贴图就报
    ``image "pyimageN" does not exist`` —— 机制与查法见
    ``.github/instructions/gui-tests-on-ci.instructions.md`` 第 11 条)。
    """
    close_child_windows(app)
