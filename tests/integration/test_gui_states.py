"""可用性守卫(I-4: 状态与可用性).

"无效的控件要看起来无效" 必须能测, 否则只是一句口号。四条判据:

A. **禁用态必须压暗** —— 按钮的底、描边、字色都要换成调色板里的禁用色。只
   ``configure(state="disabled")`` 而不重绘的按钮会留着原来的底色: 主色/危险色的
   按钮被禁用后仍然亮着, 用户会一直点它(49 号评审一次量出 32 处)。
B. **单选/复选框的禁用字色必须来自调色板** —— 不给就落到 CustomTkinter 主题里的
   另一个灰阶(与这套界面无关的颜色)。
C. **没有"死了的控件"** —— 可点的按钮必须有回调; 单选/复选框必须绑变量(勾了不生效
   的选项与"点了没反应"的按钮是同一类问题)。
D. **忙碌期间"会启动长操作"的按钮必须禁用** —— 这些处理器的第一句多是"正在忙就直接
   返回", 点下去只会什么都不发生。判据**按回调名**而不是文案: 新加一个走 ``_submit``
   的入口却忘了置灰时会自动变红, 不需要谁记得去改清单。
E. **进行中的长操作要看得出来、并且能取消** —— 只把入口置灰不够: 用户还得知道"在跑
   什么、跑到哪、能不能停", 那就是任务卡上的「运行中」+ 进度行(含具体步骤)+ 可用的
   取消按钮; 取消**不是错误**(以 INFO 收尾), 空闲时这三样要收起。

最后一条是**兜底断言**: 判据里登记的每个长操作回调都必须真的在测量里出现过 ——
否则清单写错(或某个入口被改名/删掉)时, 判据会静默空转。
"""

from __future__ import annotations

import sys
import time
from collections import deque
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.exceptions import ArchiveManagementError
from archive_management.i18n import tr
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui import dialogs
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import (
    AppPage,
    FeedbackKind,
    ImportLocationRow,
    ImportPrompt,
    ImportTargetOption,
    TaskStatus,
    export_batch_prompt,
    exportable_games,
    import_strategies,
)
from archive_management.ui.palette import Palette

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("无效控件必须看起来无效"),
    pytest.mark.layer("e2e"),
]

# **会启动长操作的回调**: 忙碌期间点了也不会做事(处理器里第一句就是 busy 就返回),
# 因此必须置灰。清单按回调名而不是文案 —— "新加一个入口忘了置灰"会自己变红。
_LONG_OPERATION_HANDLERS = frozenset(
    {
        "_on_add_game",
        "_on_import_package",
        "_on_export",
        # 主页的批量导出按钮(它转交给主窗口, 名字是主页那一个).
        "_request_export_batch",
        "_on_backup",
        "_on_restore",
        "_on_branch",
        "_on_rename_backup",
        "_on_delete_backup",
    }
)

_CONTROL_TYPES = (ctk.CTkButton, ctk.CTkRadioButton, ctk.CTkCheckBox)

# 量到的一个控件: (界面, 类名, 文案, 状态, 底色, 描边色, 禁用字色, 回调名, variable)
_Record = tuple[str, str, str, str, str, str, str, str, str]
_RECORDS: list[_Record] = []
_CASE = {"name": ""}


def _pump(app: ctk.CTk) -> None:
    """把待处理事件跑完, 保证布局与配色都已生效."""
    for _ in range(6):
        app.update_idletasks()
        app.update()


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口: 不注册系统级快捷键, 避免遗留键盘钩子或误触发备份."""
    return ArchiveApp(
        backend,
        title="可用性测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("可用性测试禁用")),
    )


def _cget(widget: Any, option: str) -> str:
    """读一个选项; 控件不支持该选项时返回空串(便于直接比较)."""
    try:
        value = widget.cget(option)
    except Exception:
        return ""
    return "" if value is None else str(value)


def _handler_name(widget: Any) -> str:
    """取按钮回调的函数名(与按钮配色守卫同一套做法)."""
    try:
        command = widget.cget("command")
    except Exception:
        return ""
    func = getattr(command, "func", command)
    return str(getattr(func, "__name__", ""))


def _controls(root: Any, case: str) -> list[_Record]:
    """子树里全部可交互控件的测量记录."""
    found: list[_Record] = []
    queue = deque([root])
    while queue:
        widget = queue.popleft()
        if isinstance(widget, _CONTROL_TYPES):
            found.append(
                (
                    case,
                    type(widget).__name__,
                    _cget(widget, "text"),
                    _cget(widget, "state"),
                    _cget(widget, "fg_color"),
                    _cget(widget, "border_color"),
                    _cget(widget, "text_color_disabled"),
                    _handler_name(widget),
                    _cget(widget, "variable"),
                )
            )
        queue.extend(widget.winfo_children())
    return found


def _record(case: str, window: Any) -> None:
    """记下一个界面状态的全部控件(量之前先让窗口布局出来)."""
    window.update_idletasks()
    _RECORDS.extend(_controls(window, case))


def _hook(window: Any, *_args: Any, **_kwargs: Any) -> None:
    """替代 ``wait_window``: 此刻模态框已经布局, 正好量它的控件."""
    _record(_CASE["name"], window)


@pytest.fixture
def app() -> Any:
    """主窗口(模态框的量点被换到 ``wait_window`` 上)."""
    application = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(application)
    application.wait_window = _hook
    return application


def _measure(app: Any, palette: Palette) -> list[str]:
    """驱动全部界面状态, 收集问题清单(一次跑完, 失败信息里按界面逐条列出)."""
    games = list(app.backend.list_games())
    seed = games[0]

    def modal(label: str, call: Callable[[], Any]) -> None:
        _CASE["name"] = label
        before = set(app.winfo_children())
        call()
        for child in set(app.winfo_children()) - before:
            child.destroy()
        _pump(app)

    def record_main(label: str) -> None:
        _CASE["name"] = label
        _record(label, app)

    def manage(archived: bool) -> Callable[[], Any]:
        def build() -> Any:
            return ManageGameWindow(
                app,
                backend=app.backend,
                palette=palette,
                game_id=seed.game_id,
                name=seed.name,
                enabled=not archived,
                backup_location="D:\\Backups",
                archived=archived,
                on_change=lambda: None,
            )

        return build

    # 详情页与主页各自量"空闲 / 忙碌"(忙碌期间长操作入口必须收起).
    for page, page_name in (
        (AppPage.DETAIL, "详情"),
        (AppPage.HOME, "主页"),
    ):
        if page is AppPage.DETAIL:
            app._open_game_detail(seed.game_id)
        else:
            app._show_page(AppPage.HOME)
        _pump(app)
        record_main(f"主窗口({page_name}, 空闲)")
        app._set_busy(True)
        record_main(f"主窗口({page_name}, 忙碌)")
        app._set_busy(False)
    app._home_page._select("")
    _pump(app)
    record_main("主窗口(主页, 没有选中)")

    # 归档的游戏在管理窗口里"除了删除全都要置灰"—— 这是最容易漏掉重绘的一处.
    window = manage(True)()
    _pump(app)
    _record("游戏管理窗口(归档)", window._window)
    window.close()
    _pump(app)

    modal(
        "批量导出(一份都没勾)",
        lambda: dialogs.export_batch_dialog(
            app,
            palette,
            title="批量导出",
            prompt=export_batch_prompt(exportable_games(())),
            filter_label="筛选",
            list_label="勾选要导出的游戏",
            no_match_text="没有匹配",
            select_all_label="全选",
            select_all_scope="当前筛选",
            confirm_text="导出选中",
        ),
    )
    # 默认策略是"新建游戏": "合并到"那一组应当已经是置灰的.
    modal(
        "导入归档包(新建策略)",
        lambda: dialogs.import_package_dialog(
            app,
            palette,
            title="导入",
            prompt=ImportPrompt(
                summary="摘要",
                match_text="疑似同一款",
                locations=(ImportLocationRow(index=0, text="D:\\Saves", default=""),),
                targets=tuple(
                    ImportTargetOption(
                        game_id=f"g{index}", label=f"游戏{index}", selected=index == 0
                    )
                    for index in range(3)
                ),
            ),
            locations_label="存档位置",
            locations_hint="勾选",
            strategy_label="导入方式",
            strategies=import_strategies(has_targets=True),
            target_label="合并到",
            target_hint="提示",
            target_locked_hint="仅合并生效",
            confirm_text="导入",
        ),
    )
    modal(
        "恢复确认(没有安全点)",
        lambda: dialogs.restore_dialog(
            app,
            palette,
            title="恢复",
            summary="将覆盖 3 个位置",
            safety_label="恢复前创建安全点",
            safety_hint="本机不支持",
            safety_available=False,
        ),
    )
    modal(
        "编辑标签(到上限)",
        lambda: dialogs.edit_tags_dialog(
            app, palette, tags=tuple(f"标签{index}" for index in range(6))
        ),
    )
    return _problems(palette)


def _looks_disabled(record: _Record, palette: Palette) -> bool:
    """这个禁用控件是不是真的被画成了禁用态.

    按钮要三样一起换(底、描边、字); 单选/复选框没有"底色"这回事(它们的
    ``fg_color`` 是勾选态的颜色), 只看字色。
    """
    kind, fg, border, disabled_text = record[1], record[4], record[5], record[6]
    if kind != "CTkButton":
        return disabled_text.lower() == palette.text_disabled.lower()
    return (
        fg.lower() == palette.disabled_bg.lower()
        and border.lower() == palette.disabled_border.lower()
        and disabled_text.lower() == palette.text_disabled.lower()
    )


def _problems(palette: Palette) -> list[str]:
    """四条判据一次收集(只报一次全部问题, 免得"改一处跑一轮")."""
    problems: list[str] = []
    measured_handlers: set[str] = set()
    for record in _RECORDS:
        case, kind, text, state, handler, variable = (
            record[0],
            record[1],
            record[2],
            record[3],
            record[7],
            record[8],
        )
        if state not in {"normal", "disabled"}:
            problems.append(f"[{case}] {kind}「{text}」状态取值异常: {state}")
        if state == "disabled" and not _looks_disabled(record, palette):
            problems.append(f"[{case}] {kind}「{text}」禁用了却看起来可点")
        if state == "normal" and kind == "CTkButton" and not handler:
            problems.append(f"[{case}] {kind}「{text}」可点但没有回调(点了没反应)")
        if state == "normal" and kind != "CTkButton" and not variable:
            problems.append(f"[{case}] {kind}「{text}」没有 variable(勾了不生效)")
        measured_handlers.add(handler)
        busy_long_op = (
            state == "normal" and "忙碌" in case and handler in _LONG_OPERATION_HANDLERS
        )
        if busy_long_op:
            problems.append(
                f"[{case}]「{text}」({handler})是长操作入口, 忙碌期间应置灰"
            )
    missing = _LONG_OPERATION_HANDLERS - measured_handlers
    if missing:
        problems.append(
            "判据没有量到这些长操作入口(清单写错, 或入口被改名/删掉了): "
            + ", ".join(sorted(missing))
        )
    return problems


def test_unusable_controls_never_look_usable(app: Any) -> None:
    """10 个界面状态一次量完: 无效控件要看起来无效, 忙碌期间不许有"点了没反应"."""
    problems = _measure(app, app.p)
    hint = "可用性问题: " + "; ".join(problems)
    assert not problems, hint


def _wait_for(app: Any, predicate: Callable[[], bool], seconds: float = 3.0) -> bool:
    """轮询等待条件成立: 长操作在后台线程跑, 结果经队列回主线程(见 ``_poll_messages``)."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _pump(app)
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


class _LongRunningService(DemoArchiveService):
    """演示后端 + "真的在跑"那一段: 让界面上"进行中"的任务卡真的出现.

    ``DemoArchiveService.task_status`` 恒返回 ``running=False``, 所以进度行 / 取消按钮 /
    「运行中」这三点在 GUI 用例里**从来没被量过** —— I-7 的"进行中可辨识 + 可取消"就是卡在
    这里。这个替身只在测试里把 running/cancellable 报成真, 长操作自己也真的睡一会儿; 被测的
    仍是主窗口怎么画这些状态(真实后端那一半由 ``tests/unit/test_sql_backend.py`` 的取消用例兜底)。
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.running = False
        self.cancel_calls = 0

    def task_status(self, game_id: str | None = None) -> TaskStatus:
        """跑着的时候报"进行中 + 可取消 + 到了哪一步"."""
        status = super().task_status(game_id)
        if not self.running:
            return status
        return replace(
            status,
            running=True,
            cancellable=True,
            progress=0.42,
            progress_label="42%",
        )

    def cancel_active(self) -> bool:
        """取消请求: 只有真的在跑时才受理(与 ``sql_backend`` 同一条契约)."""
        if not self.running:
            return False
        self.cancel_calls += 1
        return True

    def run_backup_now(self, game_id: str) -> str:
        """慢备份: 期间报 running, 收到取消就抛"已取消"(后端自己的叫法)."""
        self.running = True
        try:
            time.sleep(max(self._delay, 0.3))
            if self.cancel_calls:
                raise ArchiveManagementError(tr("result.backup_canceled"))
            return "备份完成(替身)"
        finally:
            self.running = False


def test_busy_task_card_is_recognizable_and_cancelable() -> None:
    """判据 E: 进行中的长操作要看得出来、能取消, 取消后以 INFO 收尾.

    忙碌期间只把入口置灰是不够的 —— 用户还得知道"在跑什么、跑到哪、能不能停", 这三件事的
    落点是任务卡上的「运行中」+ 进度行(含具体步骤)+ 可用的取消按钮。取消**不是错误**:
    后端报"已取消"之后应当以 INFO 收尾(而不是红字), 卡片与取消入口也要回到收起状态。
    """
    app = gui_app(_new_app, _LongRunningService(delay=1.2))
    _pump(app)
    assert _wait_for(app, lambda: app.winfo_ismapped()), "主窗口未映射, 读不到控件状态"
    service = app.backend
    game = next(iter(service.list_games()))
    app._open_game_detail(game.game_id)
    _pump(app)

    app._on_backup()  # 替身会睡 1.2s, 期间界面一直处于"运行中"

    # 1) 进行中要看得见: 「运行中」+ 进度行(含跑到哪一步)+ 取消按钮可用。
    assert _wait_for(app, lambda: app._busy), "长操作没有进入忙碌态"
    assert _wait_for(
        app, lambda: app._task_state_label.cget("text") == tr("task.running")
    ), "进行中却没有「运行中」这行状态"
    assert app._task_progress.winfo_ismapped(), "进行中却没有进度行"
    assert app._task_progress_label.cget("text") == "42%", "进行中没有说跑到哪一步"
    assert _cget(app._cancel_btn, "state") == "normal", "进行中却没有可用的取消按钮"

    # 2) 长操作入口同时置灰 —— 判据 D 走的是手工 _set_busy, 这里量真实流程那一遍。
    entries = [
        record
        for record in _controls(app, "忙碌(真实流程)")
        if record[7] == "_on_backup"
    ]
    assert entries, "量不到备份入口(回调名变了?)"
    assert entries[0][3] == "disabled", "忙碌期间备份入口没有置灰"

    # 3) 取消: 请求要真的传到后端, 并且立刻给出"正在取消"的回执(不能让用户以为没点着)。
    app._cancel_btn.invoke()
    assert service.cancel_calls == 1, "取消请求没有传到后端"
    assert app._last_feedback[0] == FeedbackKind.PENDING, app._last_feedback
    assert tr("action.cancel_pending") in app._last_feedback[1], app._last_feedback

    # 4) 收尾: 取消不是错误(INFO), 卡片与取消入口回到收起状态。
    assert _wait_for(app, lambda: not app._busy, seconds=6.0), "取消后没有回到空闲"
    assert app._last_feedback == (FeedbackKind.INFO, tr("result.backup_canceled")), (
        app._last_feedback
    )
    assert not app._cancel_btn.winfo_ismapped(), "空闲时仍留着取消按钮"
    assert _cget(app._cancel_btn, "state") == "disabled"
