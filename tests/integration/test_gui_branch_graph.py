"""分支视图的图画布守卫(I-9.2 / I-9.3 的判据).

判据来自 PLAN 的 I-9 拆分表, 全部写成**可量的数字**:

1. **item 账目**: 画布上的 item 数 == 框数 + 连线数 + 文字条数(每框两条) —— 三个都是
   确定性数字, 比计时稳, 因此渲染基准用它;
2. **几何**: 最深一层的框不越出 ``scrollregion``; 父框中心 = 首末孩子中心的中点;
3. **选中**: 选中框的描边 = 调色板强调色; 换主题之后重画一遍仍然成立;
4. **漫游**: 拖空白真的平移; 拖到框上不改变选中; 方向键/WASD 与四边箭头都能平移;
   四条箭头"按需显示"(装得下就收起, 与"按需显示滚动条"同一条纪律) —— **量控件本身**
   (``winfo_manager``), 不是量几何: 几何只说明"那边还有内容";
5. **切换**: 分支视图与时间线互不残留; 换筛选之后形状跟着变。
"""

from __future__ import annotations

from typing import Any

import pytest

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.ui import keyboard
from archive_management.ui.backend import ArchiveService
from archive_management.ui.contrast import contrast_ratio
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.models import BackupItem, ViewKind
from archive_management.ui.palette import DARK, LIGHT, Palette
from archive_management.ui.tree_view import TreeView
from helpers import utc_moment

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("分支视图的图"),
    pytest.mark.layer("e2e"),
]


def _pump(app: ctk.CTk, rounds: int = 6) -> None:
    """把待处理事件跑完(布局与重绘都是事件驱动的)."""
    for _ in range(rounds):
        app.update_idletasks()
        app.update()


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """建主窗口: 不注册系统级快捷键(测试环境里没有可用的热键后端)."""
    from archive_management.services.hotkeys import (
        GlobalHotkeyService,
        UnavailableBackend,
    )

    return ArchiveApp(
        backend,
        title="分支图测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("分支图测试禁用")),
    )


class _Hooks:
    """收集图上的选中与悬停回调(视图把这些交给宿主, 用例只看有没有被叫到)."""

    def __init__(self) -> None:
        """初始为空."""
        self.selected: list[BackupItem] = []
        self.hovered: list[str | None] = []

    def on_select(self, item: BackupItem) -> None:
        """记一次选中."""
        self.selected.append(item)

    def on_hover(self, node_id: str | None) -> None:
        """记一次悬停."""
        self.hovered.append(node_id)


@pytest.fixture
def graph() -> Any:
    """一个装在真窗口里的图画布(几何/漫游这类判据直接量它).

    自己开一个 ``CTkToplevel`` 而不是塞进主窗口: 主窗口的子控件是 ``grid`` 管理的,
    往里 ``pack`` 会被 Tk 直接拒掉("cannot use geometry manager pack inside . which
    already has slaves managed by grid")。
    """
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = ctk.CTkToplevel(app)
    window.geometry("900x600")
    window.deiconify()
    hooks = _Hooks()
    view = TreeView(window, on_select=hooks.on_select, on_hover=hooks.on_hover)
    view.frame.pack(fill="both", expand=True)
    _pump(app)
    try:
        yield app, view, hooks
    finally:
        app.destroy()


def _item(
    node_id: str,
    parent: str | None = None,
    *,
    title: str = "",
    minute: int = 0,
    current: bool = False,
) -> BackupItem:
    """造一个备份节点(几何用例只需要 id/parent/标题)."""
    return BackupItem(
        backup_id=node_id,
        title=title or node_id,
        created_dt=utc_moment(1, minute),
        created_label=f"今天 09:{minute:02d}",
        auto=False,
        branch_label="主线",
        size_label="1 MB",
        verified=True,
        parent_id=parent,
        is_current=current,
    )


def _chain(root: str, count: int) -> list[BackupItem]:
    """一条链(每层一个孩子)."""
    items = [_item(root)]
    for index in range(1, count):
        items.append(_item(f"{root}{index}", items[-1].backup_id, minute=index))
    return items


def _fan(root: str, leaves: int) -> list[BackupItem]:
    """一个扇(根 + 一堆孩子)."""
    return [_item(root)] + [
        _item(f"{root}-{index}", root, minute=index) for index in range(leaves)
    ]


def test_canvas_item_count_matches_the_accounting(graph: Any) -> None:
    """item 账目: 画布上的 item 数 == 框 + 线 + 文字(每框两条)."""
    app, view, _hooks = graph
    view.set_items(_fan("root", 3))
    view.redraw(DARK)
    _pump(app)

    counts = view.item_counts()
    assert counts.boxes == 4
    assert counts.edges == 3
    assert counts.texts == 8
    assert len(view.canvas.find_all()) == counts.total


def test_the_deepest_row_stays_inside_the_scroll_region(graph: Any) -> None:
    """最深一层的框不越出 ``scrollregion``: 画得出来也要拖得到."""
    app, view, _hooks = graph
    view.set_items(_chain("a", 5))
    view.redraw(DARK)
    _pump(app)

    scroll = [float(part) for part in str(view.canvas.cget("scrollregion")).split()]
    deepest = max(box.bottom for box in view.layout.boxes)
    rightmost = max(box.x + box.width for box in view.layout.boxes)
    assert deepest <= scroll[3], "最后一层被画到 scrollregion 外面了"
    assert rightmost <= scroll[2], "最右边一列被画到 scrollregion 外面了"


def test_a_parent_is_centered_over_its_children(graph: Any) -> None:
    """父框中心 = 首末孩子中心的中点(图看起来像组织图, 而不是一列卡片)."""
    app, view, _hooks = graph
    view.set_items(_fan("root", 3))
    view.redraw(DARK)
    _pump(app)

    root = view.layout.box("root")
    first = view.layout.box("root-0")
    last = view.layout.box("root-2")
    assert root is not None
    assert first is not None
    assert last is not None
    assert root.center_x == pytest.approx((first.center_x + last.center_x) / 2)


def test_the_selected_box_uses_the_accent_outline_and_follows_the_theme(
    graph: Any,
) -> None:
    """选中的框描边 = 调色板强调色; 换主题重画之后仍然成立."""
    app, view, _hooks = graph
    view.set_items(_fan("root", 2))
    view.redraw(DARK)
    _pump(app)

    view.select("root-1", DARK)
    box_item = view._box_items["root-1"]
    assert view.canvas.itemcget(box_item, "outline") == DARK.accent

    view.redraw(LIGHT)  # 换主题 = 整幅重画
    _pump(app)
    box_item = view._box_items["root-1"]
    assert view.canvas.itemcget(box_item, "outline") == LIGHT.accent
    assert view.canvas.itemcget(box_item, "fill") == LIGHT.accent_soft


def test_hover_and_selection_use_the_same_priority_as_cards(graph: Any) -> None:
    """悬停只提底色、选中优先于悬停(与卡片同一条规则: 选中 > 悬停 > 常规)."""
    app, view, _hooks = graph
    view.set_items(_fan("root", 2))
    view.redraw(DARK)
    _pump(app)

    view.set_hovered("root-0", DARK)
    assert view.canvas.itemcget(view._box_items["root-0"], "fill") == DARK.card_hover

    view.select("root-0", DARK)
    assert view.canvas.itemcget(view._box_items["root-0"], "fill") == DARK.accent_soft

    view.set_hovered(None, DARK)
    assert view.canvas.itemcget(view._box_items["root-0"], "fill") == DARK.accent_soft


def test_arrows_appear_only_when_that_direction_has_more_content(graph: Any) -> None:
    """四边箭头"按需显示": 装得下就收起, 装不下就露出来.

    判据量的是**控件本身**(``visible_arrows`` 读 ``winfo_manager``)而不是几何 ——
    几何只回答"那边还有没有内容", 拿它当判据会放过"收起之后再也放不回来"的缺陷。
    """
    app, view, _hooks = graph
    app.geometry("900x600")
    _pump(app)

    view.set_items(_chain("a", 3))  # 一条链: 横向装得下, 纵向装得下
    view.redraw(DARK)
    _pump(app)
    assert view.visible_arrows() == set(), "内容装得下时不该露出任何箭头"

    view.set_items(_fan("root", 12))  # 一个宽扇: 横向一定装不下
    view.redraw(DARK)
    _pump(app)
    assert "right" in view.visible_arrows()

    # 箭头必须在画布**之后**创建: Tk 的堆叠顺序就是创建顺序, 否则会被画布盖住。
    children = view.frame.winfo_children()
    assert children.index(view._arrows["right"]) > children.index(view.canvas)


def test_arrows_come_back_after_being_hidden(graph: Any) -> None:
    """收起过的箭头必须还能再露出来(踩过的坑).

    ``place()`` **无参调用只查询配置**: 实测 ``place_forget()`` 之后再 ``place()``,
    ``winfo_manager()`` 仍是空串 —— 于是箭头一旦被收起就再也回不来(而当时那条拿
    几何当判据的用例照样绿)。修法是显示时把落点再给一遍, 所以这里来回换两次形状。
    """
    app, view, _hooks = graph
    app.geometry("900x600")
    _pump(app)

    view.set_items(_chain("a", 3))  # 先装得下 -> 四条都收起
    view.redraw(DARK)
    _pump(app)
    assert view.visible_arrows() == set()

    view.set_items(_fan("root", 12))  # 换成宽扇 -> 右边那条要真的回到布局里
    view.redraw(DARK)
    _pump(app)
    assert view._arrows["right"].winfo_manager() == "place"

    view.set_items(_chain("b", 3))  # 又换成装得下的形状 -> 真的收起
    view.redraw(DARK)
    _pump(app)
    assert view.visible_arrows() == set()


def test_dragging_blank_space_pans_the_view(graph: Any) -> None:
    """按住空白处拖动 = 平移画面."""
    app, view, _hooks = graph
    app.geometry("900x600")
    _pump(app)
    view.set_items(_fan("root", 12))
    view.redraw(DARK)
    _pump(app)

    before = view.canvas.xview()
    view.set_hovered(None, DARK)
    _press(view, _blank_spot(view))
    _drag(view, _blank_spot(view)[0] - 120, _blank_spot(view)[1])
    _pump(app)

    assert view.canvas.xview()[0] > before[0], "拖动没有把画面挪走"


def test_dragging_from_a_box_does_not_change_the_selection(graph: Any) -> None:
    """从框上拖走不算"点击": 选中不变(拖到框上不能变成选中)."""
    app, view, hooks = graph
    view.set_items(_fan("root", 3))
    view.redraw(DARK)
    _pump(app)

    spot = _box_spot(view, "root-2")
    _press(view, spot)
    _drag(view, spot[0] + 80, spot[1] + 40)
    _release(view, (spot[0] + 80, spot[1] + 40))
    _pump(app)

    assert hooks.selected == [], "从框上拖走不该选中它"
    assert view.selected is None


def test_clicking_a_box_selects_it(graph: Any) -> None:
    """点一下框 = 选中那个节点(与卡片的点击同义)."""
    app, view, hooks = graph
    view.set_items(_fan("root", 3))
    view.redraw(DARK)
    _pump(app)

    spot = _box_spot(view, "root-1")
    _press(view, spot)
    _release(view, spot)
    _pump(app)

    assert [item.backup_id for item in hooks.selected] == ["root-1"]


def test_direction_keys_and_arrows_both_pan(graph: Any) -> None:
    """方向键与"点箭头"都能平移(两种入口走同一条 ``scroll``)."""
    app, view, _hooks = graph
    app.geometry("900x600")
    _pump(app)
    view.set_items(_fan("root", 12))
    view.redraw(DARK)
    _pump(app)

    before = view.canvas.xview()[0]
    view._on_key("right")
    _pump(app)
    by_key = view.canvas.xview()[0]
    assert by_key > before

    view.scroll("right")
    _pump(app)
    assert view.canvas.xview()[0] > by_key

    view.scroll("left")
    view.scroll("left")
    _pump(app)
    assert view.canvas.xview()[0] < by_key


def test_direction_keys_and_wasd_pan_through_the_real_binding(graph: Any) -> None:
    """漫游的键盘入口走**真实绑定**(合成按键), 不是直接调内部函数.

    两个踩过的坑都钉在这里:

    1. 绑定会把事件对象一起传进来(``partial`` 只预填方向), 少一个参数的表现是 Tk
       回调里报 ``TypeError: takes 2 positional arguments but 3 were given``, 界面上
       只是"按了没反应" —— 直接调 ``_on_key("right")`` 是量不到这个的;
    2. Tk 里 ``Shift+d`` 的 keysym 是 ``D``, ``<d>`` **收不到**它 —— 所以大小写各绑一条。
    """
    app, view, _hooks = graph
    app.geometry("900x600")
    _pump(app)
    view.set_items(_fan("root", 12))  # 横向装不下、纵向装得下
    view.redraw(DARK)
    _pump(app)
    view.focus_canvas()

    for key in ("<Right>", "<d>", "<D>"):
        before = view.canvas.xview()[0]
        view.canvas.event_generate(key)
        _pump(app)
        assert view.canvas.xview()[0] > before, f"{key} 没有把画面往右挪"

    for key in ("<Left>", "<a>", "<A>"):
        before = view.canvas.xview()[0]
        view.canvas.event_generate(key)
        _pump(app)
        assert view.canvas.xview()[0] < before, f"{key} 没有把画面往左挪"

    view.set_items(_chain("n", 20))  # 换成一条长链: 纵向装不下、横向装得下
    view.redraw(DARK)
    _pump(app)

    for key in ("<Down>", "<s>", "<S>"):
        before = view.canvas.yview()[0]
        view.canvas.event_generate(key)
        _pump(app)
        assert view.canvas.yview()[0] > before, f"{key} 没有把画面往下挪"

    for key in ("<Up>", "<w>", "<W>"):
        before = view.canvas.yview()[0]
        view.canvas.event_generate(key)
        _pump(app)
        assert view.canvas.yview()[0] < before, f"{key} 没有把画面往上挪"


def test_switching_views_leaves_no_residue() -> None:
    """切视图不残留: 分支视图显示画布、时间线显示卡片, 两者不同时在屏上."""
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._open_game_detail("outer-wilds")
    _pump(app)

    assert app._view == ViewKind.BRANCH
    assert app._branch_host.grid_info() != {}, "分支视图该显示图画布"
    assert app._list_scroll.grid_info() == {}, "分支视图不该同时显示滚动列表"
    assert app._tree_view.node_ids, "图上该有节点"
    assert app._cards == {}, "分支视图不建卡片"

    app._switch_view(ViewKind.TIMELINE)
    _pump(app)
    assert app._cards, "时间线仍然用卡片"
    assert app._branch_host.grid_info() == {}, "时间线不该同时显示图画布"

    app._switch_view(ViewKind.BRANCH)
    _pump(app)
    assert app._tree_view.node_ids == ("b1", "b2", "b3", "b5")


def test_a_clipped_box_can_be_read_in_full_on_hover(graph: Any) -> None:
    """框里被裁掉的字要能悬停回看 (I-3 "截断必须能回看" 在画布上的那一条).

    画布上的文字挂不了 Tk 的 tooltip: 鼠标在画布里从一个框移到另一个框**不会**再来
    一次 ``<Enter>``, 所以提示由视图自己管 (见 ``widgets.HoverTip``)。这里量两头:
    被裁过的框悬停要挂上全文 (且文案里真的有完整标题), 没裁过的框与没悬停时都不许挂。
    """
    app, view, _hooks = graph
    long_title = "恢复之前自动创建的安全点(超长的备份标题示例)" * 3
    view.set_items([_item("root", title=long_title), _item("kid", "root")])
    view.redraw(DARK)
    _pump(app)

    assert view._texts["root"].full, "夹具的长标题必须长到真的被裁"
    assert long_title in view._texts["root"].full, "提示里必须有原文"

    view.set_hovered("root", DARK)
    _pump(app)
    assert view.tip_text == view._texts["root"].full
    assert view._tip.visible

    assert view._texts["kid"].full == "", "短标题的框不该被裁 (判据的前提)"
    view.set_hovered("kid", DARK)
    _pump(app)
    assert view.tip_text == "", "没被裁的框不该弹提示"
    assert not view._tip.visible

    view.set_hovered(None, DARK)
    _pump(app)
    assert view.tip_text == ""

    view.set_hovered("root", DARK)  # 再挂一次, 然后换数据
    view.set_items(_fan("root", 3))
    assert view.tip_text == "", "换了数据还挂着旧节点的提示"


def test_the_canvas_shows_a_focus_ring_when_it_has_focus(graph: Any) -> None:
    """画布自己拿焦点时要有看得见的环 (它是普通 ``tkinter.Canvas``, 不在 keyboard 的补丁范围里).

    画布 ``takefocus=1`` —— Tab 能走到它身上。没有这条环, 焦点落在哪里就看不出来
    (I-6 那批"焦点必须看得见"的用例只扫 ``CTk`` 控件, 画布是它们的盲区)。
    """
    app, view, _hooks = graph
    view.set_items(_fan("root", 3))
    view.redraw(DARK)
    _pump(app)

    canvas = view.canvas
    assert int(canvas.cget("highlightthickness")) >= 3, "画布没有焦点环的宽度"
    assert str(canvas.cget("highlightcolor")) == DARK.focus_ring
    assert str(canvas.cget("highlightbackground")) != DARK.focus_ring, (
        "失焦时不该还亮着环"
    )
    # 环要真的看得见: 用仓库自己那条非文字判据量一遍 (≥ 3:1).
    ratio = contrast_ratio(DARK.focus_ring, DARK.well)
    assert ratio >= keyboard.FOCUS_RING_MINIMUM, f"环对底色只有 {ratio:.2f}:1"

    view.redraw(LIGHT)  # 换主题: 环的颜色跟着走
    _pump(app)
    assert str(canvas.cget("highlightcolor")) == LIGHT.focus_ring
    light_ratio = contrast_ratio(LIGHT.focus_ring, LIGHT.well)
    assert light_ratio >= keyboard.FOCUS_RING_MINIMUM


def test_the_arrows_are_keyboard_operable_and_get_out_of_the_way(graph: Any) -> None:
    """露出来的箭头真的能用键盘按; 收起的箭头真的退出了画面 (连焦点都拿不到).

    ``keyboard`` 给 ``CTkButton`` 的构造器打了补丁, 所以箭头天生就在 Tab 链里 ——
    这条用例量的是**它露出来的时候真的能按下去** (按一下画面真的动了), 以及**收起来
    的时候真的不在画面里** (实测: ``place_forget`` 掉的控件连 ``focus_set`` 都拿不
    到焦点, 而 Tk 的 Tab 遍历只走已映射的控件)。

    合成按键只送给**有焦点**的那个控件 (``test_gui_keyboard`` 里的同一条经验):
    所以按之前先把焦点真的给到它, 就像 Tab 走过来那样。
    """
    app, view, _hooks = graph
    app.geometry("900x600")
    _pump(app)
    view.set_items(_fan("root", 12))  # 一个宽扇: 横向一定装不下
    view.redraw(DARK)
    _pump(app)

    arrow = view._arrows["right"]
    assert "right" in view.visible_arrows()
    assert keyboard.is_reachable(arrow), "箭头没接上键盘线"
    assert arrow.winfo_ismapped(), "露出来的箭头没在画面里"
    inner = keyboard.focus_target(arrow)
    assert inner is not None, "箭头没有承聚焦的内部控件"
    inner.focus_set()
    _pump(app)

    before = view.canvas.xview()
    inner.event_generate("<space>", when="now")
    _pump(app)
    assert view.canvas.xview()[0] > before[0], "空格没能按下右边的箭头"

    view.set_items(_chain("a", 3))  # 装得下了: 箭头要真的退出去
    view.redraw(DARK)
    _pump(app)
    assert view.visible_arrows() == set()
    assert not arrow.winfo_ismapped(), "收起的箭头还留在画面里"
    arrow.focus_set()
    assert app.focus_get() is not arrow, "收起的箭头不该还能拿到焦点"


def test_a_tip_never_lands_off_screen_and_survives_a_dead_anchor(graph: Any) -> None:
    """提示不许跑出屏幕: 落点在屏底时翻到上方; 锚点已销毁时静默收场(不抛).

    这两条都是"看起来不会发生"的分支, 但真发生时用户看到的是一个跑到屏幕外 / 一个
    TclError 弹在 Tk 回调里(界面表现为"鼠标划过去没反应")。所以都量一遍。
    """
    app, view, _hooks = graph
    tip = view._tip
    screen_height = int(view.canvas.winfo_screenheight())

    tip.show("底部提示", root_x=10, root_y=screen_height - 4)
    _pump(app)
    assert tip.visible
    assert tip.text == "底部提示"
    assert tip._window is not None
    assert tip._window.winfo_y() < screen_height - 4, "提示没有翻到落点上方"

    tip.hide()
    assert not tip.visible
    assert tip.text == ""

    view.canvas.destroy()  # 锚点没了: show 必须静默收场
    _pump(app)
    tip.show("锚点没了", root_x=10, root_y=10)
    assert not tip.visible
    assert tip.text == ""


def test_the_box_text_is_fitted_into_the_box(graph: Any) -> None:
    """框里的文字要么放得下、要么带省略号(与列表/海报同一条规矩).

    画布上的文字**不受 Tk 的 wraplength 保护**(一整段没有空格的中日韩文字 Tk 断不开),
    所以这是它自己的那一条判据: 每条文字的实测像素宽必须落在框宽之内。
    """
    app, view, _hooks = graph
    long_title = "恢复之前自动创建的安全点(超长的备份标题示例)" * 3
    view.set_items([_item("root", title=long_title), _item("kid", "root")])
    view.redraw(DARK)
    _pump(app)

    width = int(view.layout.metrics.box_width - view.layout.metrics.text_inset * 2)
    for node_id, texts in view._texts.items():
        pairs = ((view._title_font, texts.title), (view._meta_font, texts.meta))
        for font, text in pairs:
            for line in text.splitlines():
                assert font.measure(line) <= width, f"{node_id}: {line!r} 比框还宽"
    assert "…" in view._texts["root"].title, "夹具的长标题必须长到真的被裁"


def test_the_graph_follows_the_source_filter() -> None:
    """筛选改变后形状跟着变: 只看自动备份时图上只剩那一份最新的自动备份."""
    from archive_management.ui.models import SourceFilter

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._open_game_detail("outer-wilds")
    _pump(app)
    before = app._tree_view.node_ids

    app._filter_source.set(SourceFilter.AUTO.label)
    app._on_filter_change(SourceFilter.AUTO.label)
    _pump(app)
    assert app._tree_view.node_ids == ("b3",), "只剩自动备份时图上只有它"

    # 演示数据里没有安全点: 显式筛安全点会落到"这一屏没有节点"的空状态,
    # 而空状态要回到滚动容器里显示(画布上也就不该留着上一批框)。
    app._filter_source.set(SourceFilter.SAFETY.label)
    app._on_filter_change(SourceFilter.SAFETY.label)
    _pump(app)
    assert before != app._tree_view.node_ids
    assert app._tree_view.node_ids == ()
    assert app._list_scroll.grid_info() != {}, "空状态要显示在滚动容器里"
    assert app._branch_host.grid_info() == {}, "空状态不该还露着图画布"


def test_selecting_on_the_graph_updates_the_right_panel() -> None:
    """点图上的框与点卡片是同一件事: 右侧面板与动作状态都要跟着变."""
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._open_game_detail("outer-wilds")
    _pump(app)
    item = next(item for item in app._items if item.backup_id == "b5")

    app._tree_view._on_select(item)
    _pump(app)

    assert app._tree_view.selected == "b5"
    assert str(app._selected_name.cget("text")) == item.display_title
    assert str(app._restore_btn.cget("state")) == "normal"


def _box_spot(view: TreeView, node_id: str) -> tuple[int, int]:
    """某个框中心在**控件坐标**里的位置(用 Tk 自己的换算, 免得手算滚动偏移)."""
    box = view.layout.box(node_id)
    assert box is not None
    return (
        int(view.canvas.canvasx(box.center_x)),
        int(view.canvas.canvasy(box.y + box.height / 2)),
    )


def _blank_spot(view: TreeView) -> tuple[int, int]:
    """一块没有框的空白(图的最右下角之外)."""
    width = max(1, view.canvas.winfo_width())
    height = max(1, view.canvas.winfo_height())
    return width - 4, height - 4


def _press(view: TreeView, spot: tuple[int, int]) -> None:
    """按下(走真实处理函数, 不合成事件)."""
    view._on_press(_event(spot))


def _drag(view: TreeView, x: int, y: int) -> None:
    """拖动."""
    view._on_motion(_event((x, y)))


def _release(view: TreeView, spot: tuple[int, int]) -> None:
    """松开."""
    view._on_release(_event(spot))


def _event(spot: tuple[int, int]) -> Any:
    """造一个只带 x/y 的事件对象(处理函数只需要这两个字段)."""

    class _Event:
        def __init__(self, x: int, y: int) -> None:
            self.x = x
            self.y = y

    return _Event(*spot)


def _palette_keys(palette: Palette) -> set[str]:
    """调色板里所有颜色值(用来证明"图画布只用了调色板的颜色")."""
    return {
        value
        for name in dir(palette)
        if not name.startswith("_")
        for value in (getattr(palette, name),)
        if isinstance(value, str) and value.startswith("#")
    }
