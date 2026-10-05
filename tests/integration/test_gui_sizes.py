"""窗口尺寸守卫: 矮屏下窗口不能被屏幕切掉, 按钮行必须留在窗口内.

屏高/屏宽用替身模拟(``monkeypatch tkinter.Misc.winfo_screen*``): 真机上只有一块屏,
而 1366x768 / 1920x900 这类矮屏正是"窗口比屏幕还高、底部按钮看不见"的高发区
(评审时点出的设置窗口 / 导入弹窗 / 批量导入三处)。用替身比等一台
矮屏机器靠谱, 也不受开发机分辨率影响。

判据分两类(与评审时定的验收口径一致, 不要合并成一条):

1. **模态对话框** —— 硬线(窗口 + 标题栏不出屏幕)之外还要守评审约定的舒适线: 总高
   不超过屏幕可用高度的 80%(35 号)。另外还要求正文区**真的滚起来了**(内容高于视口),
   否则"把正文区压矮"这种糊法能骗过高度断言、内容却永远看不见了;
2. **工作区窗口**(主窗口/设置/定时/管理) —— 只守硬线, 因为它们各有最小尺寸或固定
   尺寸: 主窗口 720 的下限是布局的硬要求, 屏幕再矮也不能往下压。

"窗口 + 标题栏"里的标题栏按 :data:`TITLE_MARGIN` 折算: ``winfo_height`` 量到的是
客户区, 不含窗口管理器画的标题栏。

**单位约定(2026-10-02)**: 屏高/屏宽替身是**物理**像素, 而窗口尺寸单位是**逻辑**像素
(CTk 写 ``geometry`` 时会乘上窗口缩放, 本机 125% 的屏上物理 = 逻辑 x 1.25)。因此这里
所有上限都在**逻辑**像素这一侧算(:func:`_hard_limit` / :func:`_comfort_limit`)、量到的
窗口尺寸也在 :func:`_assert_fits` 里换成逻辑再比 —— 两边不换到同一套, 125% 的开发机上
会报出一批"窗口出屏"的假红(实测过: 648 逻辑的窗口被当成 810 > 768)。
"""

from __future__ import annotations

import dataclasses
import re
import time
import tkinter
from collections import deque
from collections.abc import Callable, Iterator
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
from archive_management.ui import manage_window as manage_mod
from archive_management.ui import schedule_window as sched_mod
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import (
    SCREEN_MARGIN,
    WINDOW_DEFAULT_SIZE,
    WINDOW_MIN_SIZE,
    ArchiveApp,
    current_window_geometry,
    initial_window_size,
    open_window_geometry,
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
from archive_management.ui.widgets import window_scaling

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


def _hard_limit(window: Any) -> int:
    """硬线: 窗口 + 标题栏不出屏幕(**逻辑**像素).

    屏高替身是物理像素、窗口尺寸是逻辑像素(见模块开头的单位约定): 拿物理屏高去比逻辑
    窗口高, 在 125% 的开发机上会把本该合格的窗口全判成出屏。上限一律换成逻辑像素再比。
    """
    return round((SCREEN["height"] - TITLE_MARGIN) / window_scaling(window))


def _comfort_limit(window: Any) -> int:
    """模态对话框的舒适线(**逻辑**像素)."""
    scale = window_scaling(window)
    return min(
        _hard_limit(window), round(int(SCREEN["height"]) * DIALOG_COMFORT / scale)
    )


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


def _walk(widget: Any) -> Iterator[Any]:
    """深度优先遍历控件树(含自己)."""
    yield widget
    for child in widget.winfo_children():
        yield from _walk(child)


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


def _label_problems(window: Any) -> list[str]:
    """对话框里的"成段说明"两类毛病(空列表 = 没问题).

    ① **文字放不下**: 会自己换行的标签(``wraplength`` > 0)该比的是它自己算出来的请求
    宽度, 而不是整段文字的宽度 —— 断过行的标签拿整段去比就是假红(与
    ``test_gui_text_fit`` 同一套判据)。超出控件宽度时 Tk 直接裁掉且**不补省略号**。
    **只看真的显示出来的标签**: 弹窗里没被切过去的那些页(hidden ``grid_remove``)还
    留着布局中途的旧宽度(实测 152, 需要 296), 拿它们量就是假红。
    """
    problems: list[str] = []
    queue = deque([window])
    while queue:
        widget = queue.popleft()
        queue.extend(widget.winfo_children())
        if not isinstance(widget, ctk.CTkLabel):
            continue
        text = str(widget.cget("text"))
        width = int(widget.winfo_width())
        wrap = int(widget.cget("wraplength") or 0)
        if not text.strip() or width <= 1 or wrap <= 0:
            continue
        if not widget.winfo_ismapped():
            continue
        needed = int(widget.winfo_reqwidth())
        if needed > width:
            problems.append(f"被硬裁: {width} < {needed} | {text[:50]!r}")
            continue
        font = widget.cget("font")
        tail = text.rstrip()
        lone = tail[-1:] in _LONE_PUNCTUATION
        if lone and font.measure(tail) > wrap >= font.measure(tail[:-1]):
            problems.append(f"末行只剩标点「{tail[-1:]}」: {text[:50]!r}")
    return problems


# 语气标点里"孤立在末行会读成话没说完"的那几个(与 test_gui_copy_quality 同一份口径)。
# 全角标点用转义写: 源码里直接出现全角标点会被 RUF001 拦(仓库约定)。
_LONE_PUNCTUATION = set(",.!?;:").union("\u3002\uff01\uff1f\uff1b\uff1a\u3001\uff0c")


def _settle(window: Any, *, seconds: float = 0.5) -> None:
    """等窗口真的映射出来并布局完再量.

    CTk 的 ``deiconify`` 是**延后 5ms** 的, 只 ``update()`` 一次量到的是布局中途的数字:
    实测弹窗里的标签还停在 152 宽(需要 296)、整个窗口甚至还没 mapped —— 拿它量就是
    一堆假红。这里跑一小段事件循环(有空转上限), 让那次延后映射与随之而来的重排落地。
    """
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        window.update()
        time.sleep(0.02)


def _assert_fits(window: Any, label: str, *, limit: int, settle: bool = True) -> None:
    """断言窗口高度不超上限、且没有直接子控件被窗口自己切掉、说明文字都排好了.

    ``limit`` 与量到的窗口高度都是**逻辑**像素(见 :func:`_hard_limit`)。先 ``update()``
    把窗口真正布局出来再量: 模态钩子刚触发时窗口只是"请求尺寸"已经算好, 内部的子控件还
    停在初始值(实测正文容器仍是 canvas 默认的 200、按钮行 h=1), 那时量越界会变成空断言。
    """
    if settle:
        _settle(window)
    window.update()
    scale = window_scaling(window)
    height = round(int(window.winfo_height()) / scale)
    cut = _cut_children(window)
    hint = (
        f"{label}: 屏高 {SCREEN['height']}(缩放 {scale})下窗口高 {height} 逻辑像素"
        f"(= {window.winfo_height()} 物理), 上限 {limit}; "
        f"请求高度 {window.winfo_reqheight()}; 被切掉的子控件: {cut or '无'}"
    )
    assert height <= limit, hint
    assert not cut, hint
    problems = _label_problems(window)
    assert not problems, f"{label}: 说明文字没排好\n" + "\n".join(problems)


def _body_of(window: Any) -> Any:
    """弹窗里那个可滚动的正文区(**递归**找: 有的套在卡片容器里)."""
    return next(
        (child for child in _walk(window) if isinstance(child, ctk.CTkScrollableFrame)),
        None,
    )


def _assert_dialog_fits(window: Any, label: str) -> None:
    """模态弹窗的尺寸约定: 先守舒适线, 够不着时才退守"已经尽力 + 不出屏".

    舒适线(屏高的 80%)不是总能达到的: 屏幕矮 + 高 DPI 时, 正文区已经压到
    ``_DIALOG_BODY_MIN`` 也仍然超线(2026-10-02 实测: 700 高的屏、125% 缩放下批量导入
    的每个部件都已在最小尺寸, 合计仍是 523 逻辑 = 654 物理 > 舒适线 448 逻辑)。那时可
    断言的最强口径是两条: ① 正文区已经在**下限**(夹取使完了全部手段), ② 整窗不超过
    屏幕本身。少了①, "夹取被删掉"就能蒙过去 —— 那时正文区停在内容高度、整窗也在屏内。
    """
    comfort = _comfort_limit(window)
    scale = window_scaling(window)
    _settle(window)
    height = round(int(window.winfo_height()) / scale)
    if height <= comfort:
        _assert_fits(window, label, limit=comfort, settle=False)
        return
    body = _body_of(window)
    floor = None if body is None else float(body.cget("height"))
    screen = round(int(SCREEN["height"]) / scale)
    hint = (
        f"{label}: 屏高 {SCREEN['height']}(缩放 {scale})下窗口高 {height} 逻辑像素, "
        f"舒适线 {comfort}、屏幕 {screen}; 正文区高度 {floor}, "
        f"下限 {dialogs._DIALOG_BODY_MIN}"
    )
    assert body is not None, (
        "超了舒适线却没有可滚动的正文区 —— 没有可收紧的地方\n" + hint
    )
    assert floor is not None, hint
    assert floor <= dialogs._DIALOG_BODY_MIN, (
        f"超了舒适线, 但正文区还没收到下限(夹取没起作用)\n{hint}"
    )
    assert height <= screen, "超了舒适线, 而且窗口比屏幕还高\n" + hint


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
    # 打开尺寸 = 构造里设完 geometry 后**读回的**尺寸(逻辑像素, 见 open_window_geometry)。
    opened = current_window_geometry(application, scale=window_scaling(application))
    assert opened is not None
    assert opened.width is not None
    assert opened.height is not None
    _OPENED["size"] = (opened.width, opened.height)
    assert int(application.winfo_screenheight()) == SCREEN["height"], "屏高替身没生效"
    assert int(application.winfo_screenwidth()) == SCREEN["width"], "屏宽替身没生效"
    return application


def _measure_dialog(app: ArchiveApp, label: str, spec: _DialogCase) -> None:
    """开一个模态对话框并当场量它(``wait_window`` 被换成量尺寸).

    换掉 ``wait_window`` 的量法与界面评审的抓图脚本走同一条路: 那时窗口
    刚被居中并 update 过, 正是可以量的时刻。量完把新开的窗口销毁, 免得残留影响下一例。
    """
    before = set(app.winfo_children())
    app.wait_window = lambda window, *a, **kw: _assert_dialog_fits(window, label)
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
    # 打开尺寸是**逻辑**像素(CTk 的 geometry 单位), 屏高替身是**物理**像素: 两边先换到
    # 同一套再比 —— 125% 的屏上物理 = 逻辑 x 1.25, 直接比会得出"窗口超出屏幕"的假结论
    # (真正的约束是最小尺寸: 它在高 DPI 下本来就装不进矮屏, 那无解)。
    scale = window_scaling(app)
    opened_hint = (
        f"屏高 {SCREENS[0]}(缩放 {scale})下主窗口的打开尺寸是 "
        f"{opened_width}x{opened_height}, 设计尺寸是 {WINDOW_DEFAULT_SIZE}: "
        f"构造里要用 initial_window_size 而不是常量"
    )
    logical_limit = round((SCREENS[0] - TITLE_MARGIN) / scale)
    logical_width_limit = round((SCREEN["width"] - TITLE_MARGIN) / scale)
    real_width, real_height = _real_screen(app)
    assert opened_height >= min(WINDOW_MIN_SIZE[1], real_height), opened_hint
    assert opened_height <= max(WINDOW_MIN_SIZE[1], logical_limit), opened_hint
    assert opened_width >= min(WINDOW_MIN_SIZE[0], real_width), opened_hint
    assert opened_width <= max(WINDOW_MIN_SIZE[0], logical_width_limit), opened_hint

    for screen in SCREENS:
        SCREEN["height"] = screen
        scale = window_scaling(app)
        # 屏高替身是**物理**像素, 而窗口尺寸是逻辑像素: 先换到同一套(见模块开头的单位约定)。
        # ``fit_window_size`` 用的是 ``屏幕 - SCREEN_MARGIN``, 与 _hard_limit 的
        # ``TITLE_MARGIN`` 是两个口径, 所以两条分开算。
        screen_height = round(screen / scale)
        screen_width = round(SCREEN["width"] / scale)
        height_budget = screen_height - SCREEN_MARGIN
        width_budget = screen_width - SCREEN_MARGIN
        width, height = initial_window_size(app, scale=scale)
        app.geometry(f"{width}x{height}")
        _pump(app)

        hint = (
            f"屏高 {screen}(缩放 {scale}, 可用 {screen_height})下主窗口被算成 "
            f"{width}x{height}: 设计尺寸 {WINDOW_DEFAULT_SIZE}、最小尺寸 "
            f"{WINDOW_MIN_SIZE}、安全边距 {SCREEN_MARGIN}"
        )
        # 尺寸策略: 装得下就用设计尺寸, 装不下就夹到"屏幕 - 安全边距", 但都**不低于最小
        # 尺寸**。高 DPI 的矮屏上"窗口比屏幕还高"就是这条策略允许的结果(最小尺寸是布局
        # 硬下限), 所以这里逐条查策略, 而不是拿窗口去比屏幕(那在 125% 上必假红)。
        if height_budget >= WINDOW_DEFAULT_SIZE[1]:
            # 屏幕够大时保持设计高度 —— 防止"把默认尺寸改小"当成修好了。
            assert height == WINDOW_DEFAULT_SIZE[1], hint
        else:
            assert height == max(WINDOW_MIN_SIZE[1], height_budget), hint
        if width_budget >= WINDOW_DEFAULT_SIZE[0]:
            assert width == WINDOW_DEFAULT_SIZE[0], hint
        else:
            assert width == max(WINDOW_MIN_SIZE[0], width_budget), hint

        # 窗口本体: 不超过"硬线或最小尺寸里更大的那个", 且没有控件被切、说明文字都排好了。
        _assert_fits(
            app,
            "00-主窗口",
            limit=max(_hard_limit(app), WINDOW_MIN_SIZE[1]),
        )


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


def _geometry_inside_the_desktop(
    app: ctk.CTk, geometry: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """把一套几何收进**真桌面**(留出标题栏/任务栏余量), 位置尽量保留.

    为什么需要它: 用例想验的是"写进去的逻辑尺寸能原样读回来", 而桌面装不下那个尺寸时
    窗口管理器会把它压回屏幕内 —— 那时读到的是 WM 的决定, 不是我们的换算(2026-10-03 的
    Windows CI 实测: 写 1500x820 读回 1200x749, 因为 runner 桌面小, 而窗口最小宽度
    1200 又顶住了)。尺寸按**逻辑**像素给, 所以能放下的尺寸要拿缩放把屏幕换算回来。
    """
    width, height, x, y = geometry
    scale = window_scaling(app)
    room_width = int(app.winfo_screenwidth()) - x
    room_height = int(app.winfo_screenheight()) - y - TITLE_MARGIN
    return (
        min(width, max(1, round(room_width / scale))),
        min(height, max(1, round(room_height / scale))),
        x,
        y,
    )


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


# 真实桌面的读数(**没被替身换掉的**那两个原函数): 窗口管理器按真桌面夹窗口, 而屏宽/屏高
# 替身只管我们读到的值 —— 要判断"窗口是不是被 WM 夹过"必须问真桌面。
_REAL_SCREEN_WIDTH = tkinter.Misc.winfo_screenwidth
_REAL_SCREEN_HEIGHT = tkinter.Misc.winfo_screenheight


def _real_screen(window: tkinter.Misc) -> tuple[int, int]:
    """这台机器**真实**的桌面尺寸(物理像素), 不含替身.

    CI 的 Windows/macOS runner 桌面只有约 1024x768, 而主窗口的最小尺寸是 1200x720 ——
    "打开时不低于最小尺寸"这种断言在那种桌面上必然被窗口管理器否定(2026-10-03 的 macOS
    分片: 打开尺寸 1024x720, 而用例要求 ≥ 1200)。所以判据改成"不低于**最小尺寸与真桌面
    里更小的那个**": 宽桌面上照旧咬得住(低于最小尺寸就红), 窄桌面上接受 WM 的夹取。
    """
    return int(_REAL_SCREEN_WIDTH(window)), int(_REAL_SCREEN_HEIGHT(window))


def _geometry_numbers(text: str) -> tuple[int, int, int, int]:
    """把 ``"1400x900+160+120"`` 拆成四个数(用例自己解析, 不依赖被测函数的内部格式)."""
    width, height, x, y = (int(item) for item in re.findall(r"-?\d+", text))
    return width, height, x, y


def test_the_main_window_returns_to_the_remembered_geometry(tmp_path: Any) -> None:
    """上次关窗时记下的尺寸与位置, 下次打开照它摆; 关窗时再写回一份新的.

    **不替换屏幕尺寸**: 记住的几何会先被 ``open_window_geometry`` 夹进当前屏幕, 所以
    期望值按规格(屏幕 - :data:`SCREEN_MARGIN`、不低于最小尺寸)直接算出来, 在本机与 CI
    上都成立 —— 原来把屏幕替身成 1920x1080 再断言"窗口就是 1400x900", 在 CI 上测的其实
    是窗口管理器能不能摆下那个尺寸。

    桌面比期望尺寸还小时同理: CI 的 macOS runner 只有 1024 宽, 而窗口最小尺寸是 1200,
    窗口管理器一定会把它压回屏幕内 —— 那是 WM 的行为, 不是这里的期望值(实测 CI 报的是
    ``(1024, 720) == (1200, 720)``)。因此尺寸按"装得下就按规格、装不下接受屏幕尺寸"判,
    与位置那条 :func:`_desktop_holds` 同一条理由。
    """
    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    config = AppConfig(theme="dark", language="en")
    config.window = _REMEMBERED
    save_config(config, paths.config_path)

    app = gui_app(_geometry_app(paths), DemoArchiveService(delay=0))
    assert _wait_mapped(app), "窗口未映射, 位置读数没有意义"

    saved = _REMEMBERED.geometry()
    assert saved is not None, "夹具必须给整套几何(少一项就测不到位置)"
    screen = (int(app.winfo_screenwidth()), int(app.winfo_screenheight()))
    # 尺寸按**应用自己的单位(逻辑像素)**比: CTk 写 ``geometry`` 时会乘上窗口缩放
    # (125% 的屏上物理宽 = 逻辑宽 x 1.25), 所以物理像素与配置里存的本来就不是一个数。
    scale = window_scaling(app)
    expected_text = open_window_geometry(_REMEMBERED, screen=screen, scale=scale)
    expected_width, expected_height, expected_x, expected_y = _geometry_numbers(
        expected_text
    )
    current = current_window_geometry(app, scale=scale)
    assert current is not None, "窗口是 normal 状态, 应当读到一整套几何"
    hint = (
        f"桌面 {screen[0]}x{screen[1]}(缩放 {scale})下, 记住的几何 {saved} 应当被夹成 "
        f"{expected_text}, 实际读到 {current.geometry()}"
    )
    expected_widths = (
        {expected_width, screen[0]} if screen[0] < expected_width else {expected_width}
    )
    expected_heights = (
        {expected_height, screen[1]}
        if screen[1] < expected_height
        else {expected_height}
    )
    assert current.width in expected_widths, hint
    assert current.height in expected_heights, hint
    physical = (
        round(expected_width * scale),
        round(expected_height * scale),
        expected_x,
        expected_y,
    )
    if _desktop_holds(app, physical):
        assert (current.x, current.y) == (expected_x, expected_y), hint

    # 用户拖到别处并改了尺寸 → 读出来必须还是同一套单位(写进去 1500x820, 读回来就得是
    # 1500x820)。这一条就是 2026-10-02 用户报的"记住的几何没生效"的上游: 读成物理值
    # 之后再被乘一次缩放, 于是每重启一次窗口大一圈。
    #
    # 先把最小尺寸放开: 这条判据只关心单位换算(写逻辑值 → 读逻辑值), 而窗口最小尺寸
    # 1200x720 在 CI 的桌面上会把请求尺寸顶回去 —— 那样量的就是 WM 了。放开之后用
    # "真桌面装得下"的尺寸来验, 判据在任何机器上都成立。
    app.minsize(1, 1)
    wanted = _geometry_inside_the_desktop(app, (1500, 820, 240, 150))
    app.geometry("{}x{}+{}+{}".format(*wanted))
    _pump(app)
    moved = current_window_geometry(app, scale=window_scaling(app))
    assert moved is not None
    assert (moved.width, moved.height) == (wanted[0], wanted[1]), (
        f"读到的是逻辑像素(与写进去的同一套), 实测 {moved.geometry()} "
        f"(写进去 {wanted[0]}x{wanted[1]})"
    )
    app._on_close()

    written = load_config(paths.config_path)
    assert written.window.geometry() == moved.geometry(), (
        "关窗要写回窗口**当时**的几何(桌面装不下请求尺寸时写回的应当是被夹过的那套)"
    )
    assert (written.theme, written.language) == ("dark", "en"), (
        "写回几何不能把别的字段冲掉(读-改-写)"
    )

    # 拿刚写回的配置再开一次: 尺寸必须一模一样 —— 记下的几何与打开的几何是同一套单位,
    # 所以"记住 → 打开 → 再记住"是恒等变换(改坏成物理像素会在这里逐轮变大)。
    again = gui_app(_geometry_app(paths), DemoArchiveService(delay=0))
    assert _wait_mapped(again), "窗口未映射, 位置读数没有意义"
    reopened = current_window_geometry(again, scale=window_scaling(again))
    assert reopened is not None
    after = _geometry_numbers(
        open_window_geometry(written.window, screen=screen, scale=window_scaling(again))
    )
    # 真桌面比算出来的尺寸还窄时(CI 的 macOS runner 1024 宽, 而最小宽度 1200), 窗口管理器
    # 会把窗口夹回桌面 —— 读到的是 WM 的决定, 不是我们的换算(实测 CI: 1024x720 vs 期望
    # 1200x720)。判据与上面那个 expected_widths 同一条理由。
    real_width, real_height = _real_screen(again)
    width_options = {after[0], real_width} if real_width < after[0] else {after[0]}
    height_options = {after[1], real_height} if real_height < after[1] else {after[1]}
    assert reopened.width in width_options, (
        f"重开一次尺寸就变了: {reopened.geometry()} != {after}"
    )
    assert reopened.height in height_options, (
        f"重开一次尺寸就变了: {reopened.geometry()} != {after}"
    )
    again._on_close()


def test_the_geometry_switch_stops_remembering_and_clears_the_saved_value(
    tmp_path: Any,
) -> None:
    """设置里的"记住窗口大小与位置"关掉后: 关窗不写, 并且把已记下的几何删掉.

    用户 2026-10-02 的两条要求: 加一个开关(默认开), 关掉时"如果配置中有记录了位置大小
    信息要一并删除"。两条都要能验: ① 开关关着时关窗**不动**配置里的几何; ② 从设置窗口
    关掉开关时, 已记下的那一套当场消失(而不是留着等下次打开又生效)。
    """
    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    config = AppConfig(theme="dark", language="en")
    config.window = _REMEMBERED
    config.ui.remember_window = False
    save_config(config, paths.config_path)

    app = gui_app(_geometry_app(paths), DemoArchiveService(delay=0))
    assert _wait_mapped(app)
    app.geometry("1500x820+240+150")
    _pump(app)
    app._on_close()

    written = load_config(paths.config_path)
    assert written.window.geometry() == _REMEMBERED.geometry(), (
        "开关关着时关窗不该改写记住的几何"
    )

    # 反过来: 开着关窗会写, 而把开关关掉时那份几何要当场被清掉。
    reloaded = load_config(paths.config_path)
    reloaded.window = _REMEMBERED
    reloaded.ui.remember_window = True
    save_config(reloaded, paths.config_path)
    app = gui_app(_geometry_app(paths), DemoArchiveService(delay=0))
    assert _wait_mapped(app)
    assert app._on_remember_window_change(False) is None, "关掉开关应当成功"
    cleared = load_config(paths.config_path)
    assert cleared.window.geometry() is None, "关掉开关要把已记下的位置与大小一并删除"
    assert cleared.ui.remember_window is False, "开关本身上要落盘"

    app.geometry("1200x760+40+40")
    _pump(app)
    app._on_close()
    assert load_config(paths.config_path).window.geometry() is None, (
        "关掉开关之后关窗也不许再写"
    )


def test_the_manage_window_hugs_its_content() -> None:
    """游戏设置窗口的高度按内容算: 底部按钮与窗口下沿之间不留大片空白.

    出处(2026-10-02 用户反馈): 原来内容高度有个 440 的下限, 位置少的时候窗口比内容高一截,
    底部按钮下面空出一大块。现在只按内容定高 —— 判据是"窗口高度不超过 内容 + 页脚 +
    间距 + 上下内边距"且"关闭按钮下面剩的空白很小", 两条一起看才拦得住"把下限调小一点"这种
    糊法。

    2026-10-03 起正文是**滚动区**、关闭按钮在**固定页脚**里(外观一节让内容可能高过屏幕),
    所以关闭按钮的 y 是相对页脚的: 量它离窗口下沿多远要先把页脚自己的位置加上。

    高度判据为什么不再拿"窗口自己的请求"当唯一基准: 实测它在两台机器上含义不同 ——
    Windows runner 上窗口最终 648 而**那一刻**读到的请求只有 580(没把页脚与外边距算进去),
    本机(125% 缩放)上两者相等。所以上限写成"请求 + 页脚 + 间距 + 上下内边距": 本机必然
    满足, CI 上也能满足, 而"窗口比内容多出一大截"(老的 440 下限就是这种)照样会被拦下。
    """
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
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
            window = manage._window
            close_btn = manage._close_btn
            height = int(window.winfo_height())
            requested = int(window.winfo_reqheight())
            footer = int(manage._footer.winfo_reqheight())
            # 上限里要把"页脚 + 间距 + 上下内边距"一并算进去: 实测 Windows runner 上窗口
            # 最终 648 而**那一刻**窗口自己的请求只有 580 —— 差的 68 正好是这一串(本机两者
            # 相等, 所以只在 CI 上露头)。只拿请求当上限, 这条判据就会随 runner 变红。
            # 单位统一用**物理**像素: ``height`` / ``requested`` / ``footer`` 量到的都是物理值,
            # 常量那么按缩放换算。
            scale = window_scaling(window)
            chrome = (
                footer
                + round(manage_mod._FOOTER_GAP * scale)
                + round(manage_mod._WINDOW_PAD_Y * 2 * scale)
            )
            assert requested - 8 <= height <= requested + chrome + 8, (
                f"窗口高度应当贴着内容: 实测 {height}, 窗口请求 {requested}, "
                f"页脚与内边距 {chrome}(页脚 {footer})"
            )
            button_bottom = int(manage._footer.winfo_y()) + int(
                close_btn.winfo_y() + close_btn.winfo_height()
            )
            below = height - button_bottom
            assert 0 <= below <= 40, f"关闭按钮下面留了 {below}px 的空白"
        finally:
            manage.close()
            _pump(app)
    finally:
        app._on_close()


def _wait_settled_parent(app: Any, *, seconds: float = 3.0) -> None:
    """等主窗口落地且几何不再变, 再留几拍给弹窗自己的重校落地.

        判据与 ``dialogs._settle_centering`` 的收工条件同向(已映射 + 几何不再变), 但**不能**
        完全照搬: 实现还有个"位置确实对"的条件与轮数上限(40 x 25ms ≈ 1s, 见
    dialogs._CENTER_PASSES),
        所以这里的等待窗口要比它长、末尾再放几拍 —— 两个上限的关系是"实现先收工, 测试后断言"。
        固定睡一小段会在慢机器上量到中间态(实测整组跑时量到过 -407px, 单独跑却是 0)。
    """
    deadline = time.monotonic() + seconds
    seen = dialogs._parent_box(app)
    stable = 0
    while time.monotonic() < deadline:
        _pump(app)
        current = dialogs._parent_box(app)
        stable = stable + 1 if current == seen else 0
        seen = current
        if app.winfo_ismapped() and stable >= 3:
            break
        time.sleep(0.01)
    # 重校是 after(...) 排定的: 再放几拍事件循环(跨过实现的检查间隔)让它落地。
    for _ in range(12):
        _pump(app)
        time.sleep(dialogs._CENTER_DELAY_MS / 1000)


def _assert_centered(parent: Any, window: Any, *, tolerance: int = 12) -> None:
    """弹窗客户区中心要落在父窗口中心上(容差留给窗口管理器的取整).

    纵向多一条:**父窗口比弹窗大不了多少时"贴父窗口上沿"也算过**. 那一刻理想偏移
    ``(父高 - 弹窗高) / 2`` 只有十几像素, 而 CI 的小桌面上那正好是唯一可行的摆法
    (2026-10-04 的 macOS CI: 父窗口 1024x720、弹窗 600x680, 理想偏移 20px、实际贴边 0px,
    偏差刚好等于理想偏移)。横向不适用: 弹窗的宽远小于父窗口, 横向没有这个问题 ——
    放宽纵向而不是把容差整体调大, 是为了不让"真的偏了几百像素"那种回归蒙混过关
    (|dy| 仍然要 ≤ 那个十几像素的量级)。
    """
    dx = (window.winfo_rootx() + window.winfo_width() / 2) - (
        parent.winfo_rootx() + parent.winfo_width() / 2
    )
    dy = (window.winfo_rooty() + window.winfo_height() / 2) - (
        parent.winfo_rooty() + parent.winfo_height() / 2
    )
    hint = (
        f"弹窗偏离父窗口中心 {dx:+.0f}px / {dy:+.0f}px; "
        f"弹窗 {window.winfo_width()}x{window.winfo_height()} "
        f"@{window.winfo_rootx()},{window.winfo_rooty()}; "
        f"父窗口 {parent.winfo_width()}x{parent.winfo_height()} "
        f"@{parent.winfo_rootx()},{parent.winfo_rooty()}"
    )
    vertical_limit = max(
        tolerance, (parent.winfo_height() - window.winfo_height()) // 2
    )
    assert abs(dx) <= tolerance, f"横向{hint}"
    assert abs(dy) <= vertical_limit, f"纵向{hint}"


def test_the_first_dialog_of_a_cold_start_is_centered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """冷启动第一次开弹窗也要居中: 那一刻父窗口还没落地(见 dialogs._settle_centering).

    出处(用户 2026-10-03): 软件启动后第一次打开"游戏详情 → 游戏设置", 窗口不在软件
    中间而偏右。现场实测: 那一刻父窗口**还没映射**, ``winfo_width()`` 只有布局前的 200
    (恢复后的 1700 要等映射之后), 按 200 算居中就偏出几百像素 —— 而弹窗自己的尺寸是
    对的, 所以单看弹窗量不出任何异常。

    **两种环境都要过**: "建完窗口不进事件循环就开弹窗"这一手在 Linux/Xvfb 上根本拦不住
    —— 那里主窗口构造完就已经映射了(实测 2026-10-03 的 Linux CI: 用例红在"前提"那句
    断言上, 而产品行为是对的)。所以前提只作为**记录**(写进失败提示), 判据统一是"弹窗
    居中": 父窗口还没落地时它测的是"跟上去", 已经落地时它测的是"一次就摆对"。

    bite 口径: 把 ``dialogs._settle_centering`` 的调用去掉(即"按未映射的父窗口居中
    一次就算完"), 这一幕实测偏 500+ 像素, 12px 的容差拦得住。
    """
    monkeypatch.setattr(
        tkinter.Misc, "winfo_screenheight", lambda _self, *a: SCREEN["height"]
    )
    monkeypatch.setattr(
        tkinter.Misc, "winfo_screenwidth", lambda _self, *a: SCREEN["width"]
    )
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        app.update_idletasks()  # 只算布局, 不进事件循环(Linux 上这已经够它映射了)
        cold = not app.winfo_ismapped()
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
            built_before_mapped = not app.winfo_ismapped()
            _wait_settled_parent(app)  # 主窗口到这里才真正落地并恢复尺寸
            _assert_centered(app, manage._window)
            if not cold or not built_before_mapped:
                # 这条环境没给出"冷启动"那一幕(见 docstring): 写在结论里, 不静默。
                print(
                    f"[信息] 本次环境上主窗口已提前映射(cold={cold}, "
                    f"弹窗建时未映射={built_before_mapped}), 这条只验证了「一次就摆对」"
                )
        finally:
            manage.close()
            _pump(app)
    finally:
        app._on_close()


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


# 说明文字最多、最容易"末行只剩标点"的那个对话框(评审时就是它)。
_HINT_DIALOG = "27-批量导出(40 款)"


def test_dialog_hints_follow_the_width_they_get(app: ArchiveApp) -> None:
    """对话框里的成段说明按**实际分到的宽度**换行(写死宽度会把末尾的句号挤成孤行).

    评审时量到那条筛选说明需要 470px: 写死 460 时末尾的"。"会被挤到第二行独自站着
    (``_label_problems`` 的第二条判据); 跟着对话框给的宽度走之后一行放得下。
    """
    SCREEN["height"] = 768
    _measure_dialog(app, _HINT_DIALOG, _case(_HINT_DIALOG))


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
        _assert_dialog_fits(window, "28-导入归档包")

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
        _assert_fits(
            settings._window, "16-设置窗口", limit=_hard_limit(settings._window)
        )
        settings.close()
        _pump(app)

        schedule = app._open_schedule_window()
        _pump(app)
        assert schedule is not None, "定时任务窗口没打开"
        _assert_fits(
            schedule._window, "18-定时任务窗口", limit=_hard_limit(schedule._window)
        )
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
            _assert_fits(
                manage._window, "15-游戏管理窗口", limit=_hard_limit(manage._window)
            )
        finally:
            manage.close()
            _pump(app)
