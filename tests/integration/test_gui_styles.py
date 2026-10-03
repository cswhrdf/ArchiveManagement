"""按钮配色的守卫(I-2: 语义色与危险动作).

判据有三条, 缺一条都守不住"配色是有含义的"这件事:

A. **每个按钮都必须显式上色** —— ``fg_color`` 必须来自调色板的 token; 留着
   CustomTkinter 默认那个蓝(``['#3B8ED0', '#1F6AA5']``)就说明这个按钮没上过色,
   它不随主题变化, 一眼就看得出不是这套界面的一部分(48 号评审)。
B. **同一行至多一个主色** —— 主色 = "这一屏最该做的事"; 一行里两个主色等于没有主次,
   而"取消"永远不穿主色(24/38 号评审)。
C. **危险色的按钮集合必须正好等于预期** —— 删除类动作穿危险色、别的按钮不许穿;
   而且危险色与 :func:`archive_management.ui.widgets.button_colors` 是同一份定义,
   所以"改坏这一处"必然让本用例变红。

做法: 驱动每个窗口/对话框(模态框在 ``wait_window`` 处量), 把树里每个
``CTkButton`` 的 (所在容器, 文案, 回调名, 底色, 字色, 状态) 收集起来, 最后统一断言 ——
一次跑完 19 个界面, 失败信息里按界面逐条列出问题。

最后一条判据是**按回调名**而不是按文案的: 回调里带 ``delete``/``remove`` 这类词的按钮
就是破坏性动作, 它的颜色必须落在 :data:`widgets.DESTRUCTIVE_STYLES` 范围内 ——
这样"新加一个删除按钮却上了主色"会**自动**变红, 不需要谁记得去改上面那张文案表。
"""

from __future__ import annotations

import dataclasses
import sys
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.i18n import tr
from archive_management.infrastructure.database import Database
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui import dialogs
from archive_management.ui import schedule_window as sched_mod
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.manage_window import ManageGameWindow
from archive_management.ui.models import (
    BatchImportPrompt,
    BatchImportRow,
    ImportLocationRow,
    ImportPrompt,
    ImportTargetOption,
    export_batch_prompt,
    exportable_games,
    import_strategies,
)
from archive_management.ui.palette import Palette
from archive_management.ui.sql_backend import SqlArchiveService
from archive_management.ui.widgets import (
    ACTION_STYLES,
    BUTTON_STYLES,
    DESTRUCTIVE_STYLES,
    ActionKind,
    ButtonStyle,
    button_colors,
    style_for,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("按钮配色按调色板与动作性质"),
    pytest.mark.layer("e2e"),
]

# 每个界面里**应该**穿危险色的按钮文案(其余界面都是空集: 一个都不许穿).
# 这张表是"危险动作"的正式清单 —— 新增破坏性按钮时也必须在这里登记, 否则用例会红。
_EXPECTED_DANGER: dict[str, set[str]] = {
    "确认框(普通)": set(),
    "确认框(危险)": {"删除"},
    "信息框": set(),
    "新增游戏(输入框)": set(),
    "编辑标签": set(),
    "编辑备份信息": set(),
    "恢复确认": set(),
    "定时备份配置": set(),
    "新增定时任务": set(),
    "批量导出": set(),
    "导入归档包": set(),
    "批量导入": set(),
    "导入游戏": set(),
    "设置窗口": set(),
    "定时任务窗口(未选中)": set(),
    "定时任务窗口(选中一行)": {"删除"},
    "游戏管理窗口": {"删除", "删除原始存档位置", "删除游戏"},
    # 探测结果页的"清空结果"是破坏性的(一次清掉全部待处理/已忽略的记录), 2026-10-03 登记。
    "主窗口(详情页)": {"删除备份", tr("discovery.clear_scan")},
    "主窗口(主页)": {"删除备份", tr("discovery.clear_scan")},
}

_SOFT_DANGER_TEXTS = {tr("discovery.dir_remove")}
_EXPECTED_DANGER_SOFT: dict[str, set[str]] = {
    case: (
        set() if case not in {"主窗口(详情页)", "主窗口(主页)"} else _SOFT_DANGER_TEXTS
    )
    for case in _EXPECTED_DANGER
}

_RECORDS: list[_Painted] = []
_STATE: dict[str, Any] = {"case": ""}


def _pump(app: ctk.CTk) -> None:
    """把待处理事件跑完, 保证布局与尺寸都已生效."""
    for _ in range(6):
        app.update_idletasks()
        app.update()


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口: 不注册系统级快捷键, 避免遗留键盘钩子或误触发备份."""
    return ArchiveApp(
        backend,
        title="配色测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("配色测试禁用")),
    )


@dataclasses.dataclass(frozen=True)
class _Painted:
    """一个按钮在量到那一刻的样子."""

    case: str
    parent: str
    text: str
    handler: str
    color: str
    text_color: str
    state: str


def _palette_roles(palette: Palette) -> dict[str, str]:
    """把调色板里每个颜色值反向映射成 token 名(值重复时取先出现的那个名字)."""
    roles: dict[str, str] = {"transparent": "transparent"}
    for name in dir(palette):
        if name.startswith("_"):
            continue
        value = getattr(palette, name)
        if isinstance(value, str) and value.startswith("#"):
            roles.setdefault(value.lower(), name)
    return roles


def _buttons(root: Any) -> list[tuple[str, str, str, str, str, str]]:
    """子树里所有 CTkButton 的 (容器路径, 文案, 回调名, fg_color, text_color, state)."""
    import customtkinter as ctk

    found: list[tuple[str, str, str, str, str, str]] = []
    queue = deque([root])
    while queue:
        widget = queue.popleft()
        if isinstance(widget, ctk.CTkButton):
            found.append(
                (
                    str(widget.winfo_parent()),
                    str(widget.cget("text")),
                    _handler_name(widget.cget("command")),
                    str(widget.cget("fg_color")),
                    str(widget.cget("text_color")),
                    str(widget.cget("state")),
                )
            )
        queue.extend(widget.winfo_children())
    return found


def _defocus(window: Any) -> None:
    """把焦点从窗口里的控件挪回窗口自己(量配色要的是"没聚焦"的样子).

    **量之前必须失焦**: 聚焦时实底按钮会换成对应的软底配色(用户要的"看得见"),
    那时量到的不是"这个界面把按钮画成了什么样"。CI 的时序恰好是"窗口一建出来
    定焦就已经生效"(本地相反), 于是同一个实现会在 CI 里报"危险色按钮少了一颗"
    —— 那是量具的错, 不是配色的错。
    """
    for _ in range(3):
        window.update_idletasks()
        focused = window.focus_get()
        if focused is None or focused is window:
            return
        window.focus_force()
        window.update()


def _record(case: str, window: Any) -> None:
    """记下一个界面的全部按钮(量之前先让窗口真的布局出来、并把焦点放下)."""
    window.update_idletasks()
    _defocus(window)
    for parent, text, handler, color, text_color, state in _buttons(window):
        _RECORDS.append(_Painted(case, parent, text, handler, color, text_color, state))


def _hook(window: Any, *_args: Any, **_kwargs: Any) -> None:
    """替代 ``wait_window``: 此刻模态框已经布局, 正好量按钮配色."""
    _record(_STATE["case"], window)


@pytest.fixture
def app() -> Any:
    """主窗口(模态框的量点被换到 ``wait_window`` 上)."""
    application = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(application)
    _STATE["palette"] = application.p
    application.wait_window = _hook
    return application


def sequence(*steps: Callable[[], Any]) -> Callable[[], None]:
    """把几个步骤拼成一个驱动函数.

    lambda 里用逗号连接两句话会返回元组, mypy 会抱怨驱动函数不是 ``-> None``,
    所以这里显式包一层。
    """

    def run() -> None:
        for step in steps:
            step()

    return run


def _selected_schedule_window(app: Any) -> Callable[[], Any]:
    """开定时任务窗口并选中一行."""

    def open_window() -> Any:
        window = app._open_schedule_window()
        _pump(app)
        if window is not None and window._items:
            window._select(window._items[0].game_id)
            _pump(app)
        return window

    return open_window


def _cases(app: Any) -> list[tuple[str, Callable[[], None]]]:
    """19 个界面的驱动脚本: 每个都把窗口建出来量一遍再收掉."""
    palette = app.p
    games = list(app.backend.list_games())
    schedules = list(app.backend.list_schedules())
    seed = games[0]
    many_games = tuple(
        dataclasses.replace(seed, game_id=f"g{index}", name=f"游戏{index:02d}")
        for index in range(6)
    )
    locations = tuple(
        ImportLocationRow(index=index, text=f"D:\\Saves\\L{index}", default="")
        for index in range(3)
    )
    targets = tuple(
        ImportTargetOption(
            game_id=f"g{index}", label=f"游戏{index}", selected=index == 0
        )
        for index in range(3)
    )
    batch_rows = tuple(
        BatchImportRow(
            entry=f"游戏{index}.archive.zip",
            name=f"游戏{index}",
            meta="Windows · 1234 · 3 备份",
            match_text=tr("dialog.import_match", name=f"游戏{index}"),
            locations=(ImportLocationRow(index=0, text="D:\\Saves", default=""),),
            strategies=import_strategies(has_targets=bool(targets)),
            targets=targets,
            strategy="new",
            target_game_id=targets[0].game_id if targets else None,
        )
        for index in range(3)
    )

    def modal(label: str, call: Callable[[], Any]) -> None:
        """跑一个模态框(量点在 ``_hook`` 里), 收尾把新窗口销毁掉."""
        _STATE["case"] = label
        before = set(app.winfo_children())
        call()
        for child in set(app.winfo_children()) - before:
            child.destroy()
        _pump(app)

    def popup(label: str, call: Callable[[], Any]) -> None:
        """跑一个**非模态**弹窗(信息框不调 ``wait_window``, 得直接量)."""
        _STATE["case"] = label
        before = set(app.winfo_children())
        call()
        _pump(app)
        for child in set(app.winfo_children()) - before:
            _record(label, child)
            child.destroy()
        _pump(app)

    def workspace(label: str, opener: Callable[[], Any]) -> None:
        """跑一个常驻窗口(非模态, 自己就能量)."""
        window = opener()
        _pump(app)
        if window is not None:
            _record(label, window._window)
            window.close()
        _pump(app)

    return [
        (
            "确认框(普通)",
            lambda: modal(
                "确认框(普通)",
                lambda: dialogs.confirm_dialog(
                    app,
                    palette,
                    title="提示",
                    message="确定继续?",
                    detail="D:\\Backups\\x.zip",
                    confirm_text="继续",
                ),
            ),
        ),
        (
            "确认框(危险)",
            lambda: modal(
                "确认框(危险)",
                lambda: dialogs.confirm_dialog(
                    app,
                    palette,
                    title="删除",
                    message="确定删除?",
                    detail="D:\\x.zip",
                    confirm_text="删除",
                    danger=True,
                ),
            ),
        ),
        (
            "信息框",
            lambda: popup(
                "信息框",
                lambda: dialogs.info_dialog(
                    app, palette, title="提示", message="已完成"
                ),
            ),
        ),
        (
            "新增游戏(输入框)",
            lambda: modal(
                "新增游戏(输入框)",
                lambda: dialogs.ask_text(
                    app,
                    palette,
                    title=tr("dialog.add_game_title"),
                    text=tr("dialog.add_game_prompt"),
                    browse=lambda: None,
                ),
            ),
        ),
        (
            "编辑标签",
            lambda: modal(
                "编辑标签",
                lambda: dialogs.edit_tags_dialog(
                    app, palette, tags=("标签一", "标签二")
                ),
            ),
        ),
        (
            "编辑备份信息",
            lambda: modal(
                "编辑备份信息",
                lambda: dialogs.edit_backup_dialog(
                    app,
                    palette,
                    title="重命名",
                    name_label="名称",
                    desc_label="描述",
                    initial_name="手动备份",
                    initial_desc="说明",
                ),
            ),
        ),
        (
            "恢复确认",
            lambda: modal(
                "恢复确认",
                lambda: dialogs.restore_dialog(
                    app,
                    palette,
                    title="恢复",
                    summary="将覆盖 3 个位置",
                    safety_label="恢复前创建安全点",
                    safety_hint="推荐",
                    safety_available=True,
                    danger_note="游戏正在运行",
                ),
            ),
        ),
        (
            "定时备份配置",
            lambda: modal(
                "定时备份配置",
                lambda: dialogs.schedule_dialog(
                    app,
                    palette,
                    title="定时",
                    interval_label="周期",
                    interval_prompt="如 60m",
                    keep_label="保留",
                    keep_prompt="最多 60 份",
                    current="当前: 未配置",
                    initial_interval="60",
                    initial_keep="3",
                ),
            ),
        ),
        (
            "新增定时任务",
            lambda: modal(
                "新增定时任务",
                lambda: sched_mod.add_schedule_dialog(
                    app, palette, candidates=schedules, blocked=[]
                ),
            ),
        ),
        (
            "批量导出",
            lambda: modal(
                "批量导出",
                lambda: dialogs.export_batch_dialog(
                    app,
                    palette,
                    title="批量导出",
                    prompt=export_batch_prompt(exportable_games(many_games)),
                    filter_label="筛选",
                    list_label="勾选要导出的游戏",
                    no_match_text="没有匹配",
                    select_all_label="全选",
                    select_all_scope="当前筛选",
                    confirm_text="导出选中",
                ),
            ),
        ),
        (
            "导入归档包",
            lambda: modal(
                "导入归档包",
                lambda: dialogs.import_package_dialog(
                    app,
                    palette,
                    title="导入",
                    prompt=ImportPrompt(
                        summary="摘要",
                        match_text="疑似同一款",
                        locations=locations,
                        targets=targets,
                    ),
                    locations_label="存档位置",
                    locations_hint="勾选",
                    strategy_label="导入方式",
                    strategies=import_strategies(has_targets=bool(targets)),
                    target_label="合并到",
                    target_hint="提示",
                    target_locked_hint="仅合并生效",
                    confirm_text="导入",
                ),
            ),
        ),
        (
            "批量导入",
            lambda: modal(
                "批量导入",
                lambda: dialogs.batch_import_dialog(
                    app,
                    palette,
                    title="批量导入",
                    prompt=BatchImportPrompt(
                        summary="6 款游戏", hint="逐游戏选择", rows=batch_rows
                    ),
                    locations_label="存档位置",
                    locations_hint="勾选",
                    strategy_label="导入方式",
                    target_label="合并到",
                    confirm_text="导入",
                ),
            ),
        ),
        (
            "导入游戏",
            lambda: modal(
                "导入游戏",
                lambda: dialogs.import_game_dialog(
                    app,
                    palette,
                    title="导入游戏",
                    name_label="名称",
                    initial_name="游戏",
                    paths_label="存档路径",
                    paths_hint="勾选",
                    initial_paths=("D:\\Saves",),
                    confirm_text="导入",
                    browse=lambda: None,
                ),
            ),
        ),
        ("设置窗口", lambda: workspace("设置窗口", app._open_settings)),
        (
            "定时任务窗口",
            sequence(
                lambda: workspace("定时任务窗口(未选中)", app._open_schedule_window),
                lambda: workspace(
                    "定时任务窗口(选中一行)", _selected_schedule_window(app)
                ),
            ),
        ),
        (
            "游戏管理窗口",
            lambda: workspace(
                "游戏管理窗口",
                lambda: ManageGameWindow(
                    app,
                    backend=app.backend,
                    palette=palette,
                    game_id=seed.game_id,
                    name=seed.name,
                    enabled=True,
                    backup_location="D:\\Backups",
                    on_change=lambda: None,
                ),
            ),
        ),
        (
            "主窗口(详情页)",
            sequence(
                lambda: app._open_game_detail(seed.game_id),
                # 先选中一个备份: "删除备份"只在选中节点后才是可用态, 而禁用态的按钮
                # 反推不出它穿的是哪一档(禁用=禁用色, 由 test_gui_states.py 管)。
                lambda: app._select_backup(app._items[0]) if app._items else None,
                lambda: _pump(app),
                lambda: _record("主窗口(详情页)", app),
            ),
        ),
        (
            "主窗口(主页)",
            sequence(
                lambda: app._show_page(app._page),
                lambda: _pump(app),
                lambda: _record("主窗口(主页)", app),
            ),
        ),
    ]


def _uncolored_problems(
    case: str, painted_list: list[_Painted], roles: dict[str, str]
) -> list[str]:
    """没上色的按钮: 底色读出来不是任何 token, 就是从来没被上过色."""
    return [
        f"[{case}]「{painted.text}」没上色: fg_color={painted.color} "
        "(CustomTkinter 默认色, 不随主题变化)"
        for painted in painted_list
        if roles.get(painted.color.lower()) is None
    ]


def _row_problems(painted_list: list[_Painted], roles: dict[str, str]) -> list[str]:
    """同一行(同一父容器)里出现多个主色按钮的问题清单."""
    accents: dict[str, list[str]] = {}
    for painted in painted_list:
        if roles.get(painted.color.lower()) == "accent":
            accents.setdefault(painted.parent, []).append(painted.text)
    return [
        f" 同一行出现 {len(texts)} 个主色按钮: " + ", ".join(texts)
        for texts in accents.values()
        if len(texts) > 1
    ]


def _secondary_problems(
    case: str,
    painted_list: list[_Painted],
    roles: dict[str, str],
    secondary_only: set[str],
) -> list[str]:
    """取消/关闭这类次要动作不许穿主色或危险色."""
    return [
        f"[{case}]「{painted.text}」是次要动作, 却穿了 {roles[painted.color.lower()]} 色"
        for painted in painted_list
        if painted.text in secondary_only
        and roles.get(painted.color.lower()) in {"accent", "danger"}
    ]


def _danger_buttons(
    painted_list: list[_Painted], danger_color: str
) -> tuple[set[str], set[str]]:
    """按危险色把按钮分成两类: (底色就是危险色, 只有文字是危险色).

    **禁用态的按钮不参与这条判据**: 它此刻的颜色是禁用色(禁用必须看起来禁用是
    ``test_gui_states.py`` 管的事), 反推不出"它本来穿哪一档"。因此驱动脚本要让
    被登记为危险色的按钮**处于可用态**(主窗口那两例先选中一个备份)。
    """
    solid: set[str] = set()
    soft: set[str] = set()
    for painted in painted_list:
        if painted.state == "disabled":
            continue
        if painted.color.lower() == danger_color:
            solid.add(painted.text)
        elif painted.text_color.lower() == danger_color:
            # "软危险": 中性底 + 危险色文字(破坏性动作在同一行里不喧宾夺主)。
            soft.add(painted.text)
    return solid, soft


def _mismatch(case: str, actual: set[str], expected: set[str], what: str) -> str | None:
    """集合对不上时给一句话; 一致返回 None."""
    if actual == expected:
        return None
    return (
        f"[{case}] {what}对不上: 实际 {sorted(actual) or '无'}, "
        f"预期 {sorted(expected) or '无'}"
    )


def _observed_destructive_handlers() -> set[str]:
    """本次量到的"回调名里带删除词"的那些回调."""
    return {
        painted.handler
        for painted in _RECORDS
        if any(hint in painted.handler.lower() for hint in _DESTRUCTIVE_HINTS)
    }


def _destructive_problems(
    case: str, painted_list: list[_Painted], palette: Palette
) -> list[str]:
    """**破坏性动作的颜色范围**: 回调名带 delete/remove 的按钮只允许穿危险色.

    这条判据不看文案, 所以它拦的是"将来新加的删除按钮" —— 文案改了、图标换了、
    位置挪了都还管得住。禁用态的破坏性按钮不算违规(那时颜色是禁用色)。
    """
    problems: list[str] = []
    for painted in painted_list:
        name = painted.handler.lower()
        if not any(hint in name for hint in _DESTRUCTIVE_HINTS):
            continue
        if painted.state == "disabled":
            continue
        style = _style_of(palette, painted)
        if style in DESTRUCTIVE_STYLES:
            continue
        problems.append(
            f"[{case}]「{painted.text}」的回调 {painted.handler} 是删除类动作, "
            f"却穿了 {_root_cause(palette, painted)}; "
            f"允许的只有 {list(DESTRUCTIVE_STYLES)}"
        )
    return problems


def _kind_of(palette: Palette, painted: _Painted) -> ActionKind | None:
    """把实测颜色反推回**动作性质**(与 :func:`_style_of` 同一套对比)."""
    style = _style_of(palette, painted)
    if style is None:
        return None
    return _STYLE_KINDS.get(style)


def _important_action_problems(palette: Palette) -> list[str]:
    """**重要功能键的配色索引**: 登记的回调必须穿登记的那一档颜色.

    I-10 的要求是"重要的功能按键也要有颜色, 而且这条关联要被测试钉住"。做法与
    I-2 的破坏性清单一致: 判定按**回调名**, 不按文案 —— 改了文案/换了位置仍管得住,
    新增一个重要功能却忘了登记(或上错颜色)会直接变红。

    禁用态的不参与(那时颜色是禁用色, 反推不出本色), 但**每个登记项至少要有一条
    可用态的观察**, 否则这条判据会对着一个从未出现过的名字空转。
    """
    problems: list[str] = []
    seen: set[str] = set()
    for painted in _RECORDS:
        allowed = _IMPORTANT_ACTIONS.get(painted.handler)
        if allowed is None:
            continue
        if painted.state == "disabled":
            continue
        seen.add(painted.handler)
        actual = _kind_of(palette, painted)
        if actual in allowed:
            continue
        problems.append(
            f"[{painted.case}] 重要功能「{painted.text}」的回调 {painted.handler} "
            f"只允许穿 {sorted(allowed)}"
            f"(={sorted(style_for(kind) for kind in allowed)}), 实际是 "
            f"{_root_cause(palette, painted)}"
        )
    missing = sorted(set(_IMPORTANT_ACTIONS) - seen)
    if missing:
        problems.append(f"登记为重要功能的回调没有量到可用态的那一下: {missing}")
    return problems


def _problems(palette: Palette) -> list[str]:
    """按六条判据扫一遍记录, 返回人话的问题清单."""
    roles = _palette_roles(palette)
    danger_color = palette.danger.lower()
    # 取消/关闭永远是次要动作: 穿了主色就说明"主次"没了(24/38 号评审)。
    secondary_only = {
        tr("dialog.cancel"),
        tr("dialog.close"),
        tr("schedule.close"),
        tr("settings.close"),
    }
    by_case: dict[str, list[_Painted]] = {}
    for painted in _RECORDS:
        by_case.setdefault(painted.case, []).append(painted)

    problems: list[str] = []
    for case, painted_list in by_case.items():
        problems.extend(_uncolored_problems(case, painted_list, roles))
        problems.extend(
            f"[{case}]{item}" for item in _row_problems(painted_list, roles)
        )
        problems.extend(_secondary_problems(case, painted_list, roles, secondary_only))
        problems.extend(_destructive_problems(case, painted_list, palette))
        solid, soft = _danger_buttons(painted_list, danger_color)
        for actual, expected, what in (
            (solid, _EXPECTED_DANGER.get(case, set()), "危险色按钮"),
            (soft, _EXPECTED_DANGER_SOFT.get(case, set()), "软危险(危险色文字)按钮"),
        ):
            problem = _mismatch(case, actual, expected, what)
            if problem is not None:
                problems.append(problem)
    return problems


# 回调名里出现这些词, 就认为这个按钮做的是破坏性动作(删除类)。
# 注意: 只认**具名**回调 —— 草稿/视图内的行移除用的是闭包或 lambda(名字是
# ``<lambda>``), 本就不在范围内; 若哪天改成 ``partial(remove_row, ...)``, 用例会红
# 并要求你明确表态(登记到危险范围里, 或者把动作名改成不带这些词的)。
_DESTRUCTIVE_HINTS = ("delete", "remove", "purge", "erase", "trash", "unlink")

# 全应用里**破坏性动作**的正式清单(按回调名)。这是"危险操作"这件事的锚点:
# 颜色判据不看文案, 看的就是这份名字 —— 新增一个删除按钮时, 要么它落在危险色
# 范围内(照现有写法), 要么这条用例会红并逼你表态。
_DESTRUCTIVE_HANDLERS = {
    "_on_delete_backup",  # 主窗口 · 删除备份
    "_on_delete_game",  # 游戏管理窗口 · 删除游戏
    "_on_delete_origin",  # 游戏管理窗口 · 删除原始存档位置
    "_on_remove",  # 游戏管理窗口 · 删除位置 / 定时任务窗口 · 删除任务
    "_on_remove_dir",  # 游戏发现 · 删除监控目录
}

# **重要功能键的配色索引**(I-10): 回调名 -> 它**允许**的动作性质集合。
# 这是"哪些算重要功能"的正式清单: 新增/改名时要么登记进来, 要么上面那条兜底
# 断言会红并逼你表态。破坏性动作也在这里出现(它们的性质就是 destructive)。
#
# 为什么是"集合"而不是单一性质: 同一个功能会在两个位置各有一个入口 —— 页面级的
# 主入口穿主色, 卡片/行内的次要入口穿次色(实测: 导入归档包/批量导出/创建分支
# 各有两个入口)。集合把这两个合法档位写下来, 而不是放宽成一个什么都能塞的篓子。
_IMPORTANT_ACTIONS: dict[str, frozenset[ActionKind]] = {
    "_on_add_game": frozenset({"primary"}),  # 主页 · 添加游戏
    "_on_backup": frozenset({"primary"}),  # 详情页 · 立即备份
    "_on_export": frozenset({"primary"}),  # 详情页 · 导出游戏
    "_on_restore": frozenset({"primary"}),  # 详情页 · 恢复到此节点
    "_on_scan": frozenset({"primary"}),  # 游戏发现 · 重新扫描
    "_on_detail": frozenset({"primary", "secondary"}),  # 主页 · 打开详情
    "_on_import_package": frozenset({"primary", "secondary"}),
    "_request_export_batch": frozenset({"primary", "secondary"}),
    "_on_branch": frozenset({"primary", "secondary"}),
    "_on_delete_backup": frozenset({"destructive"}),
    "_on_delete_game": frozenset({"destructive"}),
    "_on_delete_origin": frozenset({"destructive"}),
    "_on_remove": frozenset({"destructive", "destructive_soft"}),
    "_on_remove_dir": frozenset({"destructive", "destructive_soft"}),
}

# 样式 -> 性质: 从实测颜色把按钮反推回性质时用(与 widgets.ACTION_STYLES 互为反正).
_STYLE_KINDS: dict[ButtonStyle, ActionKind] = {
    mapped: kind for kind, mapped in ACTION_STYLES.items()
}


def _handler_name(command: Any) -> str:
    """按钮回调的函数名(``partial`` 就取它包的原函数; 无名回调退回到 repr)."""
    inner = getattr(command, "func", command)
    name = getattr(inner, "__name__", "")
    return str(name) if name else repr(command)


def _root_cause(palette: Palette, painted: _Painted) -> str:
    """违规时给出人话的原因: 既给样式名也给实际颜色."""
    style = _style_of(palette, painted)
    return style if style is not None else f"未登记的配色 {painted.color}"


def _style_of(palette: Palette, painted: _Painted) -> ButtonStyle | None:
    """把实测的底色/字色反推回样式名.

    配色只有 :func:`button_colors` 一处定义, 所以这里直接遍历全部样式对比即可 ——
    "这个按钮穿的是什么样式"不靠猜。禁用态的颜色不在任何样式里, 返回 None。
    """
    for style in BUTTON_STYLES:
        colors = button_colors(palette, style)
        if (
            painted.color.lower() == colors.fg.lower()
            and painted.text_color.lower() == colors.text.lower()
        ):
            return style
    return None


def test_windows_paint_buttons_by_palette_and_action_kind(app: Any) -> None:
    """19 个界面: 按钮都上色、一行至多一个主色、危险色只出现在破坏性动作上."""
    _RECORDS.clear()
    cases = _cases(app)
    for label, drive in cases:
        _STATE["case"] = label
        drive()

    labels = [painted.case for painted in _RECORDS]
    missing = sorted(set(_EXPECTED_DANGER) - set(labels))
    assert not missing, f"这些界面没量到(驱动脚本漏了?): {missing}"
    # 兜底: 证明"破坏性动作"这条判据真的看到了东西(否则它可能一直在空转)。
    observed = _observed_destructive_handlers()
    assert observed == _DESTRUCTIVE_HANDLERS, (
        "破坏性动作清单与实测对不上: "
        f"多出来的 {sorted(observed - _DESTRUCTIVE_HANDLERS)} / "
        f"没量到的 {sorted(_DESTRUCTIVE_HANDLERS - observed)}"
    )

    problems = _problems(app.p)
    problems.extend(_important_action_problems(app.p))
    hint = (
        f"按钮配色不合规 {len(problems)} 处"
        f"(共 {len(set(labels))} 个界面, {len(_RECORDS)} 个按钮):\n"
        + "\n".join(f"{index}. {item}" for index, item in enumerate(problems, 1))
    )
    assert not problems, hint


def _widget_count(root: Any) -> int:
    """整棵控件树的控件数(兑底用)."""
    count = 0
    queue = deque([root])
    while queue:
        widget = queue.popleft()
        count += 1
        queue.extend(widget.winfo_children())
    return count


def _visible_pair_colors(root: Any) -> list[str]:
    """子树里"看得见却还穿着 CustomTkinter 主题对色"的控件.

    ``fg_color`` 读到 ``['gray86', 'gray17']`` 这种**主题对**(不是具体颜色)时, 说明这个
    控件从来没被上过色 —— 它跟着系统明暗走, 与这套调色板无关。宽度或高度量出来 <= 1 的
    控件看不见, 不算(界面里确实有若干"占位用的空标签", 它们是 0 尺寸的)。
    """
    offenders: list[str] = []
    queue = deque([root])
    while queue:
        widget = queue.popleft()
        try:
            fg_color = str(widget.cget("fg_color"))
        except Exception:
            fg_color = ""
        visible = widget.winfo_width() > 1 and widget.winfo_height() > 1
        if fg_color.startswith("[") and visible:
            offenders.append(f"{widget.winfo_class()} {widget} fg_color={fg_color}")
        queue.extend(widget.winfo_children())
    return offenders


def test_empty_library_chrome_uses_the_palette(tmp_path: Path) -> None:
    """空库首次启动: 顶栏、页面容器、状态栏与它们里面的按钮都必须穿调色板色.

    回归(2026-09-27 实测): ``UiKit`` 建的容器只在 ``kit.apply()`` 之后才上色, 而调用它的
    四处都在"选中游戏 / 切视图 / 切主题"这些**动作**里 —— 空库时一个都走不到, 于是顶栏与
    状态栏停在 CustomTkinter 的默认灰(实测 ``#2b2b2b``), 顶栏三个按钮还是默认蓝
    (``#1F6AA5``)。有游戏时会被 ``_load_first_game`` 的选中动作顺手刷上色, 所以只有
    "空库首次启动"这一种状态看得出问题。截图见 ``ui-review/screens/01``~``04``。
    """
    database = Database(tmp_path / "empty.db")
    database.migrate()
    service = SqlArchiveService(database, backup_root=tmp_path / "backups")
    application = gui_app(_new_app, service)
    try:
        _pump(application)
        palette = application.p
        chrome = [str(child.cget("fg_color")) for child in application.winfo_children()]
        assert chrome == [palette.topbar, palette.background, palette.raised], (
            "顶栏/页面容器/状态栏必须是三个调色板色(顺序: 顶栏, 页面, 状态栏)"
        )
        offenders = _visible_pair_colors(application)
        assert not offenders, "这些控件还穿着 CustomTkinter 的默认色:\n" + "\n".join(
            offenders
        )
        # 兜底: 控件树要真的建出来了, 否则上面两条判据是在空树上空转。
        assert _widget_count(application) > 300
    finally:
        application.destroy()
