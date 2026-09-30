"""窗口尺寸守卫: 矮屏下窗口不能被屏幕切掉, 按钮行必须留在窗口内.

屏高/屏宽用替身模拟(``monkeypatch tkinter.Misc.winfo_screen*``): 真机上只有一块屏,
而 1366x768 / 1920x900 这类矮屏正是"窗口比屏幕还高、底部按钮看不见"的高发区
(optimizations.csv 的 16 号设置窗口 / 28 号导入弹窗 / 29 号批量导入)。用替身比等一台
矮屏机器靠谱, 也不受开发机分辨率影响。

判据分两类(与评审表里的验收口径一致, 不要合并成一条):

1. **模态对话框** —— 硬线(窗口 + 标题栏不出屏幕)之外还要守评审约定的舒适线: 总高
   不超过屏幕可用高度的 80%(35 号)。另外还要求正文区**真的滚起来了**(内容高于视口),
   否则"把正文区压矮"这种糊法能骗过高度断言、内容却永远看不见了;
2. **工作区窗口**(主窗口/设置/定时/管理) —— 只守硬线, 因为它们各有最小尺寸或固定
   尺寸: 主窗口 720 的下限是布局的硬要求, 屏幕再矮也不能往下压。

"窗口 + 标题栏"里的标题栏按 :data:`TITLE_MARGIN` 折算: ``winfo_height`` 量到的是
客户区, 不含窗口管理器画的标题栏。
"""

from __future__ import annotations

import dataclasses
import time
import tkinter
from collections import deque
from collections.abc import Callable
from typing import Any

import pytest

from gui_support import gui_app

try:
    import tkinter

    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.config import (
    AppConfig,
    WindowSettings,
    load_config,
    save_config,
)
from archive_management.i18n import tr
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui import dialogs
from archive_management.ui import schedule_window as sched_mod
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import (
    SCREEN_MARGIN,
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
    ArchiveApp,
    initial_window_size,
)
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

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("窗口尺寸与自适应"),
    pytest.mark.layer("e2e"),
]

# 三个基准屏高: 1366x768 笔记本、1920x900、1920x1080。
SCREENS = (768, 900, 1080)
# 基准屏宽(替换真机的宽度, 让宽度方向的结论与开发机无关)。
SCREEN_WIDTH = 1366
# 标题栏高度: winfo_height 量到的是客户区, 不含它。
TITLE_MARGIN = 48
# 模态对话框的舒适线: 评审约定的"弹窗不超过屏幕可用高度的 80%"。
DIALOG_COMFORT = 0.8

# 屏高/屏宽替身(用例里改它, 替身函数读它)。
SCREEN: dict[str, int] = {"height": 1080, "width": SCREEN_WIDTH}

# 撑高的对话框用例: 驱动器签名统一为 ``(app, palette)``, 返回值是对话框的选择结果
# (用例不关心, 所以这里是 Any)。
_DialogCase = Callable[[ArchiveApp, Any], Any]


def _hard_limit() -> int:
    """硬线: 窗口 + 标题栏不出屏幕."""
    return SCREEN["height"] - TITLE_MARGIN


def _comfort_limit() -> int:
    """模态对话框的舒适线."""
    return min(_hard_limit(), int(SCREEN["height"] * DIALOG_COMFORT))


def _pump(app: ctk.CTk) -> None:
    """把待处理事件跑完, 保证布局与尺寸都已生效."""
    for _ in range(6):
        app.update_idletasks()
        app.update()


def _wait_mapped(app: ctk.CTk, seconds: float = 3.0) -> bool:
    """等到主窗口真的被映射出来.

    CustomTkinter 是**延时** deiconify 的(5ms 定时器), 而位置的读数只有映射之后才有
    意义 —— 未映射时 ``winfo_x/y`` 报的不是窗口管理器摆的那个位置。
    """
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _pump(app)
        if app.winfo_ismapped():
            return True
        time.sleep(0.01)
    return bool(app.winfo_ismapped())


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口: 不注册系统级快捷键, 避免遗留键盘钩子或误触发备份."""
    return ArchiveApp(
        backend,
        title="尺寸测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("尺寸测试禁用")),
    )


def _cut_children(window: Any) -> list[str]:
    """窗口的**直接子控件**里越出窗口下/右边界的那些(名字 + 越出多少像素).

    只看直接子控件: 滚动区内部的控件超出视口是它该干的事。按钮行与底部说明都在
    窗口的直接子控件里, 因此"按钮被切掉"一定能被这里抓到。
    """
    window.update()
    left, top = window.winfo_rootx(), window.winfo_rooty()
    right = left + int(window.winfo_width())
    bottom = top + int(window.winfo_height())
    cut: list[str] = []
    for child in window.winfo_children():
        child_right = child.winfo_rootx() + int(child.winfo_width())
        child_bottom = child.winfo_rooty() + int(child.winfo_height())
        below, beyond = child_bottom - bottom, child_right - right
        if below > 1 or beyond > 1:
            cut.append(f"{child.__class__.__name__}(下{below}, 右{beyond})")
    return cut


def _scrollable_ancestor(widget: Any) -> Any:
    """往上找一个"带滚动区"的祖先(没有就返回 None)."""
    node: Any = widget
    while node is not None:
        if hasattr(node, "_parent_canvas"):
            return node
        parent = node.winfo_parent()
        node = node.nametowidget(parent) if parent else None
    return None


def _descendants(widget: Any) -> int:
    """子树里的控件个数."""
    total = 0
    queue = deque(widget.winfo_children())
    while queue:
        node = queue.popleft()
        total += 1
        queue.extend(node.winfo_children())
    return total


def _content_area(window: Any) -> Any:
    """对话框的正文区容器: 直接子控件里控件最多的那一块.

    比按钮行那两三个控件多得多, 因此不用依赖创建顺序, 也不用猜哪一个是正文区。
    """
    children = window.winfo_children()
    if not children:
        return None
    return max(children, key=_descendants)


def _first_label(window: Any) -> Any:
    """这块区域里最上面那行**有内容**的文本(按创建顺序广度优先找).

    跳过空文本的标签: 弹窗里第一个 CTkLabel 实测是个空文本的装饰件(直接挂在正文容器
    上, 不在滚动区里), 拿它当"正文第一行"会得到错误结论。
    """
    queue = deque([window])
    while queue:
        widget = queue.popleft()
        if isinstance(widget, ctk.CTkLabel) and str(widget.cget("text")).strip():
            return widget
        queue.extend(widget.winfo_children())
    return None


def _children_summary(window: Any) -> str:
    """窗口直接子控件的类名(失败信息里够用, 不必把整棵树倒出来)."""
    names = [type(child).__name__ for child in window.winfo_children()]
    return ", ".join(names) if names else "(空)"


def _body_extent(body: Any) -> tuple[int, int]:
    """正文区的(内容高度, 视口高度)."""
    canvas = body._parent_canvas
    canvas.update_idletasks()
    box = canvas.bbox("all")
    content = 0 if box is None else int(box[3]) - int(box[1])
    return content, int(canvas.winfo_height())


def _body_visible_fraction(body: Any) -> float:
    """正文区里"可见部分占内容的比例": 1.0 表示内容全看得见、用不着滚.

    用 Tk 自己算的 ``yview`` 而不是自己拿两个高度相除: 它算的是真实几何, 也不受字体
    影响(内容被拉丁/中日韩字体撑高一点, 比值依旧 < 1)。
    """
    canvas = body._parent_canvas
    canvas.update_idletasks()
    first, last = canvas.yview()
    return float(last) - float(first)


def _assert_fits(window: Any, label: str, *, limit: int) -> None:
    """断言窗口高度不超上限、且没有直接子控件被窗口自己切掉.

    先 ``update()`` 把窗口真正布局出来再量: 模态钩子刚触发时窗口只是"请求尺寸"已经算好,
    内部的子控件还停在初始值(实测正文容器仍是 canvas 默认的 200、按钮行 h=1), 那时量
    越界会变成空断言。
    """
    window.update()
    height = int(window.winfo_height())
    cut = _cut_children(window)
    hint = (
        f"{label}: 屏高 {SCREEN['height']} 下窗口高 {height}, 上限 {limit}; "
        f"被切掉的子控件: {cut or '无'}"
    )
    assert height <= limit, hint
    assert not cut, hint


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> ArchiveApp:
    """建一次主窗口, 屏高/屏宽由替身提供(用例里改 SCREEN 即可换屏)."""
    monkeypatch.setattr(
        tkinter.Misc, "winfo_screenheight", lambda _self, *a: SCREEN["height"]
    )
    monkeypatch.setattr(
        tkinter.Misc, "winfo_screenwidth", lambda _self, *a: SCREEN["width"]
    )
    # 建窗口时按最矮的基准屏(768): 主窗口的打开尺寸就是构造里定的, 用一个装不下设计
    # 尺寸的屏高建它才能验证"打开时就夹住了"(见 test_main_window_opens_within_the_screen)。
    SCREEN["height"] = SCREENS[0]
    application: ArchiveApp = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(application)
    # 打开尺寸 = 构造里设完 geometry 后的实际宽高(见夹具上面那段: 屏高按 768 给)。
    _OPENED["size"] = (int(application.winfo_width()), int(application.winfo_height()))
    assert int(application.winfo_screenheight()) == SCREEN["height"], "屏高替身没生效"
    assert int(application.winfo_screenwidth()) == SCREEN["width"], "屏宽替身没生效"
    return application


def _measure_dialog(app: ArchiveApp, label: str, spec: _DialogCase) -> None:
    """开一个模态对话框并当场量它(``wait_window`` 被换成量尺寸).

    换掉 ``wait_window`` 的量法与 ``ui-review/capture.py`` 抓截图走同一条路: 那时窗口
    刚被居中并 update 过, 正是可以量的时刻。量完把新开的窗口销毁, 免得残留影响下一例。
    """
    before = set(app.winfo_children())
    app.wait_window = lambda window, *a, **kw: _assert_fits(
        window, label, limit=_comfort_limit()
    )
    spec(app, app.p)
    for child in set(app.winfo_children()) - before:
        child.destroy()
    _pump(app)


def _dialog_cases() -> list[tuple[str, _DialogCase]]:
    """会撑高的对话框: 带滚动正文区 / 长列表 / 长文本的那几个."""
    demo = DemoArchiveService(delay=0)
    schedules = list(demo.list_schedules())
    seed = next(iter(demo.list_games()))

    many_games = tuple(
        dataclasses.replace(seed, game_id=f"g{index}", name=f"游戏{index:02d}")
        for index in range(40)
    )
    locations = tuple(
        ImportLocationRow(
            index=index,
            text=f"D:\\Saves\\Level{index}",
            default=f"D:\\Saves\\Level{index}",
        )
        for index in range(12)
    )
    targets = tuple(
        ImportTargetOption(
            game_id=f"g{index}", label=f"游戏{index}", selected=index == 0
        )
        for index in range(8)
    )
    batch_rows = tuple(
        BatchImportRow(
            entry=f"游戏{index}.archive.zip",
            name=f"游戏{index}",
            meta=tr(
                "dialog.import_batch_row_meta",
                platform="Windows",
                app_id="1234",
                backups=3,
                files=21,
                size="4.2 MB",
            ),
            match_text=tr("dialog.import_match", name=f"游戏{index}"),
            locations=(ImportLocationRow(index=0, text="D:\\Saves", default=""),),
            strategies=import_strategies(has_targets=bool(targets)),
            targets=targets,
            strategy="new",
            target_game_id=targets[0].game_id if targets else None,
        )
        for index in range(6)
    )

    cases: list[tuple[str, _DialogCase]] = [
        (
            "19-新增游戏",
            lambda app, palette: dialogs.ask_text(
                app,
                palette,
                title=tr("dialog.add_game_title"),
                text=tr("dialog.add_game_prompt"),
            ),
        ),
        (
            "21-编辑标签(6 个 = 上限)",
            lambda app, palette: dialogs.edit_tags_dialog(
                app, palette, tags=tuple(f"标签{index}" for index in range(6))
            ),
        ),
        (
            "22-编辑备份信息(200 字描述)",
            lambda app, palette: dialogs.edit_backup_dialog(
                app,
                palette,
                title=tr("dialog.rename_title", name="测试游戏"),
                name_label=tr("dialog.rename_label"),
                desc_label=tr("dialog.describe_label"),
                initial_name="手动备份",
                initial_desc="描述" * 100,
            ),
        ),
        (
            "23-恢复确认(8 条目标)",
            lambda app, palette: dialogs.restore_dialog(
                app,
                palette,
                title=tr("dialog.restore_title"),
                summary=tr(
                    "dialog.restore_summary",
                    name="测试游戏",
                    title="手动备份",
                    files=12,
                    size="1.4 MB",
                    targets="".join(
                        f"· D:\\Saves\\Level{index}\n" for index in range(8)
                    ),
                ),
                safety_label=tr("dialog.restore_safety"),
                safety_hint=tr("dialog.restore_safety_hint"),
                safety_available=True,
                danger_note=tr(
                    "dialog.restore_process", matches="TestGame.exe, TestGame"
                ),
            ),
        ),
        (
            "25-定时备份配置",
            lambda app, palette: dialogs.schedule_dialog(
                app,
                palette,
                title=tr("dialog.schedule_title", name="测试游戏"),
                interval_label=tr("dialog.schedule_interval_label"),
                interval_prompt=tr("dialog.schedule_prompt"),
                keep_label=tr("dialog.keep_auto_label"),
                keep_prompt=tr("dialog.keep_auto_prompt", max=60),
                current=tr("dialog.schedule_current", current=tr("task.unscheduled")),
                initial_interval="60",
                initial_keep="3",
            ),
        ),
        (
            "26-新增定时任务",
            lambda app, palette: sched_mod.add_schedule_dialog(
                app,
                palette,
                candidates=[item for item in schedules if item.can_schedule]
                or schedules,
                blocked=[item for item in schedules if not item.has_locations],
            ),
        ),
        (
            "27-批量导出(40 款)",
            lambda app, palette: dialogs.export_batch_dialog(
                app,
                palette,
                title=tr("dialog.export_batch_title"),
                prompt=export_batch_prompt(exportable_games(many_games)),
                filter_label=tr("dialog.export_batch_filter"),
                list_label=tr("dialog.export_batch_list"),
                no_match_text=tr("dialog.export_batch_no_match"),
                select_all_label=tr("dialog.export_batch_select_all"),
                select_all_scope=tr("dialog.export_batch_select_all_scope"),
                confirm_text=tr("dialog.export_batch_confirm"),
            ),
        ),
        (
            "28-导入归档包(12 位置 + 8 目标)",
            lambda app, palette: dialogs.import_package_dialog(
                app,
                palette,
                title=tr("dialog.import_title"),
                prompt=ImportPrompt(
                    summary=tr(
                        "dialog.import_summary",
                        name=seed.name,
                        platform="Windows",
                        app_id="1234",
                        backups=6,
                        files=48,
                        size="12.4 MB",
                    ),
                    match_text=tr("dialog.import_match", name=seed.name),
                    locations=locations,
                    targets=targets,
                ),
                locations_label=tr("dialog.import_locations"),
                locations_hint=tr("dialog.import_locations_hint"),
                strategy_label=tr("dialog.import_strategy"),
                strategies=import_strategies(has_targets=bool(targets)),
                target_label=tr("dialog.import_target"),
                target_hint=tr("dialog.import_target_hint"),
                target_locked_hint=tr("dialog.import_target_locked"),
                confirm_text=tr("dialog.import_confirm"),
            ),
        ),
        (
            "29-批量导入(6 款)",
            lambda app, palette: dialogs.batch_import_dialog(
                app,
                palette,
                title=tr("dialog.import_batch_title"),
                prompt=BatchImportPrompt(
                    summary=tr(
                        "dialog.import_batch_summary",
                        games=len(batch_rows),
                        backups=18,
                        files=126,
                        size="25 MB",
                    ),
                    hint=tr("dialog.import_batch_hint"),
                    rows=batch_rows,
                ),
                locations_label=tr("dialog.import_locations"),
                locations_hint=tr("dialog.import_locations_hint"),
                strategy_label=tr("dialog.import_strategy"),
                target_label=tr("dialog.import_target"),
                confirm_text=tr("dialog.import_confirm"),
            ),
        ),
        (
            "导入游戏(12 条路径)",
            lambda app, palette: dialogs.import_game_dialog(
                app,
                palette,
                title=tr("dialog.import_title"),
                name_label=tr("dialog.candidate_import_prompt"),
                initial_name=seed.name,
                paths_label=tr("dialog.import_paths"),
                paths_hint=tr("dialog.import_paths_hint"),
                initial_paths=tuple(f"D:\\Saves\\Level{index}" for index in range(12)),
                confirm_text=tr("dialog.import_confirm"),
            ),
        ),
    ]
    return cases


# 主窗口**构造完那一瞬间**的几何(打开尺寸), 由 app 夹具记下。
_OPENED: dict[str, tuple[int, int]] = {}


def test_main_window_opens_within_the_screen(app: ArchiveApp) -> None:
    """主窗口的打开尺寸: 装得下就用设计尺寸, 装不下按屏幕夹, 但不低于最小尺寸."""
    # 先验证构造里**真的用了**这个尺寸策略: 夹具是在 768 高的屏上建的窗口, 所以打开
    # 尺寸不该还是设计尺寸。少了这条, "策略函数写了、构造里却还用常量 1360x860" 就能
    # 绕过整个守卫(实测这么做时其余断言全绿)。
    opened_width, opened_height = _OPENED["size"]
    opened_hint = (
        f"屏高 {SCREENS[0]} 下主窗口的打开尺寸是 {opened_width}x{opened_height}, "
        f"设计尺寸是 {WINDOW_DEFAULT_SIZE}: 构造里要用 initial_window_size 而不是常量"
    )
    assert opened_height <= SCREENS[0] - TITLE_MARGIN, opened_hint
    assert opened_width <= SCREEN["width"] - TITLE_MARGIN, opened_hint

    for screen in SCREENS:
        SCREEN["height"] = screen
        width, height = initial_window_size(app)
        app.geometry(f"{width}x{height}")
        _pump(app)

        hint = (
            f"屏高 {screen} 下主窗口被算成 {width}x{height}: "
            f"设计尺寸 {WINDOW_DEFAULT_SIZE}、最小尺寸 {WINDOW_MIN_SIZE}"
        )
        # 装不下就得夹(这是本用例存在的理由: 1360x860 在 768/900 高的屏幕上会出屏)。
        assert height <= _hard_limit(), hint
        assert width <= SCREEN["width"] - TITLE_MARGIN, hint
        # 但不许夹过头: 最小尺寸是布局的硬下限。
        assert height >= WINDOW_MIN_SIZE[1], hint
        assert width >= WINDOW_MIN_SIZE[0], hint
        # 屏幕够大时保持设计高度 —— 防止"把默认尺寸改小"当成修好了。
        if SCREEN["height"] - 96 >= WINDOW_DEFAULT_SIZE[1]:
            assert height == WINDOW_DEFAULT_SIZE[1], hint
        else:
            assert height < WINDOW_DEFAULT_SIZE[1], hint

        _assert_fits(app, "00-主窗口", limit=_hard_limit())


# 记住的窗口几何(1920x1080 的屏、1400x900 摆在中偏左上).
_REMEMBERED = WindowSettings(width=1400, height=900, x=160, y=120)


def _geometry_app(paths: ApplicationPaths) -> Callable[[Any], ArchiveApp]:
    """造一个"带配置目录"的主窗口工厂(记住的几何写在那个目录的 config.json 里)."""

    def build(backend: ArchiveService) -> ArchiveApp:
        return ArchiveApp(
            backend,
            title="窗口几何测试",
            hotkeys=GlobalHotkeyService(backend=UnavailableBackend("几何测试禁用")),
            paths=paths,
        )

    return build


def _desktop_holds(app: ctk.CTk, geometry: tuple[int, int, int, int]) -> bool:
    """真桌面(留出任务栏余量)装得下这套几何吗.

    GitHub 的 Windows/macOS runner 桌面只有约 1024x768(减去任务栏更矮), 而主窗口有
    1200x720 的**最小尺寸** —— 那种桌面上"记住的 1400x900"一定会被窗口管理器压回屏幕
    内。所以断言位置之前先问这一句: 装不下时位置由窗口管理器决定, 拿它当断言等于在测
    窗口管理器(2026-09-30 的 CI 就是这么红的: `(1200, 749) == (1400, 900)`)。
    """
    width, height, x, y = geometry
    screen_w = int(app.winfo_screenwidth())
    screen_h = int(app.winfo_screenheight())
    return x + width <= screen_w and y + height <= screen_h - TITLE_MARGIN


def test_the_main_window_returns_to_the_remembered_geometry(tmp_path: Any) -> None:
    """上次关窗时记下的尺寸与位置, 下次打开照它摆; 关窗时再写回一份新的.

    **不替换屏幕尺寸**: 记住的几何会先被 ``open_window_geometry`` 夹进当前屏幕, 所以
    期望值按规格(屏幕 - :data:`SCREEN_MARGIN`、不低于最小尺寸)直接算出来, 在本机与 CI
    上都成立 —— 原来把屏幕替身成 1920x1080 再断言"窗口就是 1400x900", 在 CI 上测的其实
    是窗口管理器能不能摆下那个尺寸。
    """
    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    config = AppConfig(theme="dark", language="en")
    config.window = _REMEMBERED
    save_config(config, paths.config_path)

    app = gui_app(_geometry_app(paths), DemoArchiveService(delay=0))
    assert _wait_mapped(app), "窗口未映射, 位置读数没有意义"

    saved = _REMEMBERED.geometry()
    assert saved is not None, "夹具必须给整套几何(少一项就测不到位置)"
    saved_width, saved_height, saved_x, saved_y = saved
    screen = (int(app.winfo_screenwidth()), int(app.winfo_screenheight()))
    size = (
        max(WINDOW_MIN_SIZE[0], min(saved_width, screen[0] - SCREEN_MARGIN)),
        max(WINDOW_MIN_SIZE[1], min(saved_height, screen[1] - SCREEN_MARGIN)),
    )
    position = (
        min(saved_x, max(0, screen[0] - size[0])),
        min(saved_y, max(0, screen[1] - size[1])),
    )
    actual = (
        int(app.winfo_width()),
        int(app.winfo_height()),
        int(app.winfo_x()),
        int(app.winfo_y()),
    )
    hint = (
        f"桌面 {screen[0]}x{screen[1]} 下, 记住的几何 {saved} 应当被夹成 "
        f"{size}@{position}, 实测 {actual[:2]}@{actual[2:]}"
    )
    assert actual[:2] == size, hint
    if _desktop_holds(app, (*size, *position)):
        assert actual[2:] == position, hint

    # 用户拖到别处并改了尺寸 → 关窗时把**实际**几何写回配置, 其余字段一个不丢。
    app.geometry("1500x820+240+150")
    _pump(app)
    moved = (
        int(app.winfo_width()),
        int(app.winfo_height()),
        int(app.winfo_x()),
        int(app.winfo_y()),
    )
    app._on_close()

    written = load_config(paths.config_path)
    assert written.window.geometry() == moved, (
        "关窗要写回窗口**当时**的几何(桌面装不下 1500x820 时写回的应当是被夹过的那套)"
    )
    assert (written.theme, written.language) == ("dark", "en"), (
        "写回几何不能把别的字段冲掉(读-改-写)"
    )


def test_a_maximized_window_is_not_remembered(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """最大化时关窗**跳过这一项**: 配置里留着的还是上次用户摆的那套几何.

    实测最大化时 ``winfo_*`` 报的是整块屏幕(3440x1369+-8+-8) —— 记下来下次打开就
    不是用户摆的那个大小了。这里直接把 ``state()`` 换成 ``zoomed``: 真去点最大化按钮
    在无窗口管理器的 CI 上不可靠, 而判定点就在这一个调用上。
    """
    monkeypatch.setattr(tkinter.Misc, "winfo_screenheight", lambda _self, *a: 1080)
    monkeypatch.setattr(tkinter.Misc, "winfo_screenwidth", lambda _self, *a: 1920)
    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    config = AppConfig(theme="dark")
    config.window = _REMEMBERED
    save_config(config, paths.config_path)

    app = gui_app(_geometry_app(paths), DemoArchiveService(delay=0))
    assert _wait_mapped(app), "窗口未映射, 位置读数没有意义"
    app.geometry("1500x820+240+150")  # 摆一套"本来会被记下来"的几何
    _pump(app)

    monkeypatch.setattr(app, "state", lambda: "zoomed")
    app._on_close()

    written = load_config(paths.config_path)
    assert written.window.geometry() == _REMEMBERED.geometry(), (
        "最大化关闭时不该写这一项(跳过 = 保留上次记下的值)"
    )


# 导入弹窗那个撑得最高的用例(滚动用例复用它, 免得两处 prompt 长度对不上)。
_IMPORT_DIALOG_CASE = "28-导入归档包(12 位置 + 8 目标)"


def _case(label: str) -> _DialogCase:
    """按标签取一个用例(只给滚动用例用)."""
    for name, spec in _dialog_cases():
        if name == label:
            return spec
    raise AssertionError(f"没有这个对话框用例: {label}")


# 比三个基准屏都矮的"夹取生效"屏高: 正文区顶到上限的对话框在它上面必须真的被收.
_CLAMP_SCREEN = 700
# 正文区顶到上限的那个对话框(内容最多的一个).
_CLAMP_DIALOG = "29-批量导入(6 款)"


def test_dialogs_are_clamped_to_the_measured_comfort_line(app: ArchiveApp) -> None:
    """装潢高度按**实测**算: 正文区顶到上限时, 整窗仍然落在舒适线内.

    正文区之外那部分(标题/提示行/按钮行)原来按 ``_DIALOG_CHROME_HEIGHT = 52`` 估,
    而它其实由字体与窗口管理器决定 —— 实测同一份代码 Linux 上比 Windows 高 7 像素,
    于是**只有 Linux 的 CI** 会报"768 高屏上弹窗 621 > 614"。这条用例把那个场景搬到
    一个更矮的屏上(本机上也会溢出), 于是漏掉那次实测夹取就会直接变红。
    """
    SCREEN["height"] = _CLAMP_SCREEN
    _measure_dialog(app, _CLAMP_DIALOG, _case(_CLAMP_DIALOG))


def test_import_dialog_scrolls_instead_of_getting_squashed(app: ArchiveApp) -> None:
    """导入弹窗在矮屏上靠正文区滚动腾地方: 内容真的高于视口, 不是被裁掉.

    高度断言容易被"把正文区压矮"骗过(高度达标了、内容却永远看不见), 因此这里再加一条
    行为断言: 768 高的屏幕上正文区只能显示内容的一部分(yview 比值 < 1)。

    用例复用 28 号那个 prompt —— 换成一段手写的短摘要就测不到了(内容本来就装得下)。
    """
    SCREEN["height"] = 768
    captured: dict[str, float] = {}

    def capture(window: Any, *args: Any, **kwargs: Any) -> None:
        area = _content_area(window)
        assert area is not None, "导入弹窗应该有正文区"
        first = _first_label(area)
        assert first is not None, "导入弹窗的正文区里应该有文本"
        body = _scrollable_ancestor(first)
        assert body is not None, (
            "导入弹窗的第一行内容必须挂在可滚动的正文区里, 否则窗口变矮时下面的内容"
            "点不到(实测把正文区换成不可滚的容器就会这样)。正文区容器的直接子控件: "
            f"{_children_summary(area)}"
        )
        window.update()
        content, viewport = _body_extent(body)
        captured["visible"] = _body_visible_fraction(body)
        captured["content"] = content
        captured["viewport"] = viewport
        _assert_fits(window, "28-导入归档包", limit=_comfort_limit())

    before = set(app.winfo_children())
    app.wait_window = capture
    _case(_IMPORT_DIALOG_CASE)(app, app.p)
    for child in set(app.winfo_children()) - before:
        child.destroy()
    _pump(app)

    visible = captured.get("visible", 1.0)
    hint = (
        f"768 高的屏幕上导入弹窗的正文区应该滚起来(可见比例 < 1), 实测可见 "
        f"{visible:.3f}(内容 {captured.get('content')}px / 视口 "
        f"{captured.get('viewport')}px)"
    )
    assert 0.0 < visible < 1.0, hint


def test_workspace_windows_fit_the_screen(app: ArchiveApp) -> None:
    """设置/定时/管理窗口在矮屏上都不出屏幕(它们各有固定或最小尺寸)."""
    for screen in SCREENS:
        SCREEN["height"] = screen

        settings = app._open_settings()
        _pump(app)
        assert settings is not None, "设置窗口没打开"
        _assert_fits(settings._window, "16-设置窗口", limit=_hard_limit())
        settings.close()
        _pump(app)

        schedule = app._open_schedule_window()
        _pump(app)
        assert schedule is not None, "定时任务窗口没打开"
        _assert_fits(schedule._window, "18-定时任务窗口", limit=_hard_limit())
        schedule.close()
        _pump(app)

        games = list(app.backend.list_games())
        assert games, "演示后端应该至少有一款游戏"
        manage = ManageGameWindow(
            app,
            backend=app.backend,
            palette=app.p,
            game_id=games[0].game_id,
            name=games[0].name,
            enabled=True,
            backup_location="D:\\Backups",
            on_change=lambda: None,
        )
        try:
            manage.refresh()
            _pump(app)
            _assert_fits(manage._window, "15-游戏管理窗口", limit=_hard_limit())
        finally:
            manage.close()
            _pump(app)
