"""文案质量守卫(I-3: 未替换占位符 / 静默截断 / 空面板).

I-3 的三条规则都要"能量出来"才守得住, 所以这里把 19 个界面状态跑一遍, 收集每个
文本控件**实际显示出来的文字**, 再按下面四条断言:

A. **不得出现未替换的占位符**: 显示文本里出现 ``{`` 或 ``}`` 就说明某处 ``tr()``
   少传了参数(24 号评审: 面板里写着"可点「恢复到某节点」切换", 而按钮其实叫
   「恢复到此节点」—— 那是个没被替换的占位符)。
B. **被省略号截掉的文字必须挂悬停提示**: "能换行就换行, 换行还放不下才补省略号"是
   既有规则(I-1 守卫保证), 但只剩省略号的尾巴必须还能看到 —— 否则用户永远读不到
   完整内容(这就是"半句话"能被接受的前提)。
C. **我们自己折行的文本不许以孤立的标点收尾**: 末行只有一个逗号/句号会读成"话没
   说完"(39 号评审)。只检查带 ``\\n`` 的文本(那是 ``chips_lines`` 这类自己折的),
   Tk 自己折行的看不到, 不假装能查。
D. **空面板必须给说法**: 详情页概要卡在没有排期时不许只摆一个短横线/两个字的空框,
   要么给解释性文案, 要么给一个能点的入口(20 号评审)。

做法与 ``test_gui_styles.py`` 同构: 驱动窗口 → 收集 → 汇总一次断言。
"""

from __future__ import annotations

import dataclasses
import sys
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

from archive_management.i18n import tr
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
    HomeSection,
    ImportLocationRow,
    ImportPrompt,
    ImportTargetOption,
    export_batch_prompt,
    exportable_games,
    import_strategies,
)
from archive_management.ui.widgets import tooltip_text

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("文案完整到达用户"),
    pytest.mark.layer("e2e"),
]

# 长内容: 真实世界里出现过的长名称(中英混排) + 很深的安装路径 + 四个长标签。没有
# 它,"被截断的东西必带悬停提示"这条判据会**空转**(演示数据短, 一处省略号都不会有)。
_LONG_NAME = (
    "Kaiju Princess 2: Poochi Q ASMR - A Magic Ticket That Grants Any Desire - "
    "超长的游戏名称示例"
) * 2
_LONG_PATH = "D:\\SteamLibrary\\steamapps\\common\\" + "VeryLongFolderName\\" * 6
_LONG_TITLE = "恢复之前自动创建的安全点(超长的备份标题示例)" * 2
_LONG_NOTE = "这是一条故意写得很长很长的描述, 用来撑出省略号, 再长一点, 再长一点吧" * 2
_LONG_TAGS = (
    "超长的标签名示例一",
    "超长的标签名示例二",
    "超长的标签名示例三",
    "超长的标签名示例四",
)


class _LongTextService(DemoArchiveService):
    """演示后端, 但把每处可能变长的文案都换成长的(名称/路径/标签/标题/描述)."""

    def __init__(self) -> None:
        super().__init__(delay=0)

    def list_games(self) -> list[Any]:
        return [replace(game, name=_LONG_NAME) for game in super().list_games()]

    def get_detail(self, game_id: str) -> Any:
        detail = super().get_detail(game_id)
        return replace(detail, name=_LONG_NAME, main_location=_LONG_PATH)

    def list_locations(self, game_id: str) -> list[Any]:
        return [
            replace(location, path=_LONG_PATH)
            for location in super().list_locations(game_id)
        ]

    def list_backups(self, game_id: str) -> list[Any]:
        return [
            replace(backup, title=_LONG_TITLE, sub=_LONG_NOTE)
            for backup in super().list_backups(game_id)
        ]

    def list_schedules(self) -> list[Any]:
        return [
            replace(item, game_name=_LONG_NAME) for item in super().list_schedules()
        ]

    def load_home(self) -> Any:
        return self._rename(super().load_home())

    def apply_home_filter(self, active: Any) -> Any:
        return self._rename(super().apply_home_filter(active))

    def _rename(self, board: Any) -> Any:
        return replace(
            board,
            games=tuple(
                replace(game, name=_LONG_NAME, tags=_LONG_TAGS) for game in board.games
            ),
        )


# 我们自己折行时常用来分隔的语气标点: 末行只有这些就等于"话没说完"。
# 全角标点用转义写: 源码里直接出现全角标点会被 RUF001 拦(仓库约定, 与 i18n
# 里的文案不同 —— 那里是数据, 这里是代码)。
_LONE_PUNCTUATION = set(",.!?;:").union("\u3002\uff01\uff1f\uff1b\uff1a\u3001\uff0c")


@dataclasses.dataclass(frozen=True)
class _Text:
    """一个文本控件在量到那一刻的样子."""

    case: str
    widget: str
    text: str
    wraplength: int
    width: int
    tooltip: str


_RECORDS: list[_Text] = []
_STATE: dict[str, Any] = {"case": ""}


class _EmptyLibraryService(_LongTextService):
    """库里一款游戏都没有的演示后端(主页只能渲染空状态)."""

    def load_home(self) -> Any:
        board = super().load_home()
        return replace(board, games=(), stats=replace(board.stats, total=0, archived=0))

    def apply_home_filter(self, active: Any) -> Any:
        return self.load_home()


def _pump(app: ctk.CTk) -> None:
    """把待处理事件跑完, 保证布局与尺寸都已生效."""
    for _ in range(6):
        app.update_idletasks()
        app.update()


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口: 不注册系统级快捷键, 避免遗留键盘钩子或误触发备份."""
    return ArchiveApp(
        backend,
        title="文案测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("文案测试禁用")),
    )


def _texts(root: Any) -> list[tuple[str, str, int, int, str]]:
    """子树里所有文本控件的 (类型, 文案, 换行宽, 实际宽, 提示文案)."""
    found: list[tuple[str, str, int, int, str]] = []
    queue = deque([root])
    while queue:
        widget = queue.popleft()
        if isinstance(widget, ctk.CTkLabel):
            try:
                text = str(widget.cget("text"))
                wrap = int(widget.cget("wraplength") or 0)
                hint = tooltip_text(widget)
            except Exception:  # pragma: no cover - 已销毁的控件
                queue.extend(widget.winfo_children())
                continue
            if text:
                found.append(
                    (type(widget).__name__, text, wrap, widget.winfo_width(), hint)
                )
        queue.extend(widget.winfo_children())
    return found


def _record(case: str, window: Any) -> None:
    """记下一个界面的全部文本(量之前先让窗口真的布局出来)."""
    window.update_idletasks()
    for widget, text, wrap, width, hint in _texts(window):
        _RECORDS.append(_Text(case, widget, text, wrap, width, hint))


def _hook(window: Any, *_args: Any, **_kwargs: Any) -> None:
    """替代 ``wait_window``: 此刻模态框已经布局, 正好量文本."""
    _record(_STATE["case"], window)


@pytest.fixture
def app() -> Any:
    """主窗口(模态框的量点被换到 ``wait_window`` 上, 数据全是长文案)."""
    application = gui_app(_new_app, _LongTextService())
    _pump(application)
    _STATE["palette"] = application.p
    application.wait_window = _hook
    return application


def sequence(*steps: Callable[[], Any]) -> Callable[[], None]:
    """把几个步骤拼成一个驱动函数(lambda 里用逗号会返回元组, mypy 会抱怨)."""

    def run() -> None:
        for step in steps:
            step()

    return run


def _selected_schedule_window(app: Any) -> Callable[[], Any]:
    """开定时任务窗口并选中一行(行内说明与状态才会填满)."""

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
            "确认框",
            lambda: modal(
                "确认框",
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
                    message="确定删除?该操作不可撤销",
                    detail="D:\\Backups\\2026-09-27-manual.zip",
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
                lambda: workspace("定时任务窗口", app._open_schedule_window),
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


def _unfilled_placeholders() -> list[str]:
    """A: 显示文本里残留 ``{``/``}`` 的控件."""
    return [
        f"[{item.case}] {item.widget} 显示「{item.text}」"
        for item in _RECORDS
        if "{" in item.text or "}" in item.text
    ]


def _unreachable_tails() -> list[str]:
    """B: 被省略号截掉却没有悬停提示的文本."""
    problems: list[str] = []
    for item in _RECORDS:
        if not item.text.endswith("…"):
            continue
        if not item.tooltip:
            problems.append(
                f"[{item.case}] {item.widget} 被截成「{item.text}」且没有悬停提示"
            )
    return problems


def _lone_punctuation_tails() -> list[str]:
    """C: 自己折行的文本, 末行只剩标点(读起来像"话没说完")."""
    problems: list[str] = []
    for item in _RECORDS:
        if "\n" not in item.text:
            continue
        last = item.text.split("\n")[-1].strip()
        if last and set(last) <= _LONE_PUNCTUATION:
            problems.append(
                f"[{item.case}] {item.widget} 末行只剩标点「{last}」: {item.text!r}"
            )
    return problems


def _problems() -> list[str]:
    """按三条规则扫一遍记录, 返回人话的问题清单."""
    problems: list[str] = []
    problems.extend(_unfilled_placeholders())
    problems.extend(_unreachable_tails())
    problems.extend(_lone_punctuation_tails())
    return problems


# 统计列的"无数据"只能是词, 不能退化成一个符号 —— CSV 20 号那条"像坏掉的组件"
# 就是面板里只摆了一个短横线。这里只钉这三条具体的值, 不搞含糊的启发式: 表格单元
# 里的"—"(没有备份时)本来就该保留。
_BARE_MARKS = {"", "-", "--", "\u2014", "\u2013", "?", "\uff1f"}


def test_dashboard_stats_never_show_a_bare_mark(app: Any) -> None:
    """概要卡的三条统计值不许只留一个符号, 未排期时必须说清"未配置"."""
    games = list(app.backend.list_games())
    app._open_game_detail(games[0].game_id)
    _pump(app)
    values = {
        "最近备份": str(app._stat_recent_value.cget("text")),
        "备份总数": str(app._stat_total_value.cget("text")),
        "下次自动备份": str(app._stat_next_value.cget("text")),
    }
    bad = {name: text for name, text in values.items() if text.strip() in _BARE_MARKS}
    assert not bad, f"统计列退化成一个符号了: {bad}"
    # 未排期时不能只给一个符号或空串, 必须能看出"是没配置, 不是坏了"。
    if not app._next_scheduled:
        text = str(app._stat_next_value.cget("text"))
        assert text == tr("hero.next_none"), text


def _live_empty_action(panel: Any) -> Any:
    """在空状态里现找一个"能点的主操作"(找不到返回 None)."""

    def walk(widget: Any) -> Any:
        for child in widget.winfo_children():
            if isinstance(child, ctk.CTkButton) and str(child.cget("text")) == tr(
                "home.empty_go_discovery"
            ):
                return child
            found = walk(child)
            if found is not None:
                return found
        return None

    return walk(panel._list_box)


def test_empty_library_state_is_centred_and_actionable() -> None:
    """空库的空状态: 卡片内居中 + 一个当场能点的主操作(02 号评审的判据).

    原先只有左上角两行小字占着整张卡片, 看着像渲染失败。这里不改"字写得好不好",
    只钉住判据本身: 空状态**居中**、卡片里有**可点的主操作**、表头收起。
    """
    application = gui_app(_new_app, _EmptyLibraryService())
    _pump(application)
    panel = application._home_page
    # 这一页会被后台刷新重建(空库也一样), 所以每次都从**活着的**控件树里现找,
    # 不缓存上一次渲染的按钮引用(否则拿到的是已销毁的路径, 读属性直接 TclError)。
    action = _live_empty_action(panel)
    assert action is not None, "空库的空状态里没有任何可点的动作"
    assert str(action.cget("text")) == tr("home.empty_go_discovery")

    holder = action.master
    assert holder.pack_info()["expand"] in (1, "1", True), (
        "空状态没有撑满卡片(没有居中)"
    )
    centred = [
        str(widget.cget("anchor"))
        for widget in holder.winfo_children()
        if isinstance(widget, ctk.CTkLabel)
    ]
    assert centred, "空状态里一条说明文字都没有"
    assert set(centred) == {"center"}, f"空状态的文字没有居中: {centred}"
    assert not panel._head.winfo_ismapped(), "空库时表头不该还摆着"

    # 这个主操作要真的能用: 点一下应当切到「游戏发现」分区。
    action.invoke()
    _pump(application)
    assert panel._section is HomeSection.DISCOVERY


def test_visible_copy_is_complete_and_reachable(app: Any) -> None:
    """19 个界面: 没有未替换的占位符, 截断的都看得到全文, 折行不落孤标点."""
    _RECORDS.clear()
    for label, drive in _cases(app):
        _STATE["case"] = label
        drive()

    labels = {item.case for item in _RECORDS}
    # 前提: 夹具必须真的把某些文本顶到"只能截断"那一步, 否则 B/C 两条判据是空的。
    ellipsized = [item for item in _RECORDS if item.text.endswith("…")]
    assert len(ellipsized) >= 3, (
        "夹具没造出被截断的文本, "
        f"'截断必带提示'这条判据形同虚设(只有 {len(ellipsized)} 处省略号)"
    )

    problems = _problems()
    hint = (
        f"文案不合规 {len(problems)} 处(共 {len(labels)} 个界面, "
        f"{len(_RECORDS)} 条文本):\n"
        + "\n".join(f"{index}. {item}" for index, item in enumerate(problems, 1))
    )
    assert not problems, hint
