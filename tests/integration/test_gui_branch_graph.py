"""分支视图的图画布守卫(绘制与漫游的判据).

判据全部写成**可量的数字**:

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

from archive_management.i18n import tr
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

# 框里那个"必须被裁"的长标题: **留一段长拉丁串**。缺中文字体的机器会把汉字量成近乎零宽,
# 纯汉字的"超长标题"在那里根本放得下 —— "被裁过"这个前提恒不成立, 判据就变成了空断言
# (CI 实测 Linux 上两条用例都是这么失守的)。拉丁字形任何字体都量得出宽度。
_LONG_BOX_TITLE = "恢复之前自动创建的安全点(超长的备份标题示例)" * 3 + (
    "-VeryLongBackupTitleSample0123456789" * 3
)


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
    """item 账目: 画布上的 item 数 == 框 + 线 + 文字(每框两条) + 折叠标记."""
    app, view, _hooks = graph
    view.set_items(_fan("root", 3))
    view.redraw(DARK)
    _pump(app)

    counts = view.item_counts()
    assert counts.boxes == 4
    assert counts.edges == 3
    assert counts.texts == 8
    assert counts.markers == 1, "只有根有孩子, 所以只有一个折叠标记"
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
    assert view.hovered == "root-0", (
        "悬停状态要能被问到(与 selected 一样: 守卫读它, 不翻私有字段)"
    )
    assert view.canvas.itemcget(view._box_items["root-0"], "fill") == DARK.card_hover

    view.select("root-0", DARK)
    assert view.canvas.itemcget(view._box_items["root-0"], "fill") == DARK.accent_soft

    view.set_hovered(None, DARK)
    assert view.hovered is None, "悬停走了之后状态也要清掉"
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
    """框里被裁掉的字要能悬停回看 ("截断必须能回看" 在画布上的那一条).

    画布上的文字挂不了 Tk 的 tooltip: 鼠标在画布里从一个框移到另一个框**不会**再来
    一次 ``<Enter>``, 所以提示由视图自己管 (见 ``widgets.HoverTip``)。这里量两头:
    被裁过的框悬停要挂上全文 (且文案里真的有完整标题), 没裁过的框与没悬停时都不许挂。
    """
    app, view, _hooks = graph
    long_title = _LONG_BOX_TITLE
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
    (那批"焦点必须看得见"的用例只扫 ``CTk`` 控件, 画布是它们的盲区)。
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
    long_title = _LONG_BOX_TITLE
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


# -- 折叠/展开(绘制那一半) -----------------------------------------
#
# 一块小树: root 下面 a / b, 而 a 自己有三个孩子。折叠的**入口**与命中优先级是 9.5.3
# 的内容, 这里量的是"收起来之后画布上是什么样": 账目、痕迹、以及选中一点没动。


def _traces(view: TreeView, node_id: str) -> str:
    """一个框上"看得见的说明行 + 被裁时的全文"合起来的痕迹文本.

    说明行放不下时 :func:`fit_text` 会裁掉尾巴, 而被裁的部分**挂在悬停提示里**(截断必须能回看是硬规则,
    "截断必须能回看") —— 所以判据把两处合起来看, 否则"字够不够宽"会混进判据里。
    """
    texts = view._texts[node_id]
    return f"{texts.meta}\n{texts.full}"


def _fold_items(*, current: str | None = None) -> list[BackupItem]:
    """折叠用例的夹具: root → a → (a1, a2, a3) 与 root → b."""
    return [
        _item("root"),
        _item("a", "root"),
        _item("a1", "a", minute=1, current=current == "a1"),
        _item("a2", "a", minute=2, current=current == "a2"),
        _item("a3", "a", minute=3, current=current == "a3"),
        _item("b", "root", minute=4, current=current == "b"),
    ]


def test_every_branch_box_gets_a_fold_marker(graph: Any) -> None:
    """有孩子的框各有一个折叠标记, 叶子没有(点了也没东西可藏)."""
    app, view, _hooks = graph
    view.set_items(_fold_items())
    view.redraw(DARK)
    _pump(app)

    assert set(view._marker_items) == {"root", "a"}
    assert len(view._marker_items) == view.item_counts().markers
    assert len(view.canvas.find_all()) == view.item_counts().total

    # 标记就在框的右下角: 画出来的坐标与纯函数算的矩形对得上。
    rect = view.layout.marker_rect("a")
    assert rect is not None
    coords = view.canvas.coords(view._marker_items["a"])
    assert coords[0] == pytest.approx(rect[0] + rect[2] / 2)
    assert coords[1] == pytest.approx(rect[1] + rect[3] / 2)


def test_folding_hides_the_subtree_and_writes_down_what_it_hid(graph: Any) -> None:
    """折叠: 子树从**布局**里拿掉(不是画出来再藏), 说明行写明藏了几个后代."""
    app, view, _hooks = graph
    view.set_items(_fold_items())
    view.redraw(DARK)
    _pump(app)

    assert view.toggle("a", DARK) is True
    _pump(app)
    assert view.node_ids == ("root", "a", "b")
    assert view.collapsed == frozenset({"a"})
    counts = view.item_counts()
    assert (counts.boxes, counts.edges, counts.markers) == (3, 2, 2)
    assert len(view.canvas.find_all()) == counts.total, (
        "收起来之后画布上不该留着旧 item"
    )

    assert "3 个后代" in view._texts["a"].meta, "藏了什么要写出来"
    assert view.canvas.itemcget(view._marker_items["a"], "text") == "+", (
        "收起状态该显示加号(点一下展开)"
    )

    assert view.toggle("a", DARK) is False
    _pump(app)
    assert view.node_ids == ("root", "a", "a1", "a2", "a3", "b")
    assert view.collapsed == frozenset()
    assert "3 个后代" not in view._texts["a"].meta
    assert view.canvas.itemcget(view._marker_items["a"], "text") == "-", (
        "展开状态该显示减号(点一下收起)"
    )


def test_folding_never_changes_the_selection_and_says_when_it_hides_it(
    graph: Any,
) -> None:
    """折叠**不改**选中(反向守卫), 但藏着选中项/当前节点时必须在框上说出来.

    偷偷改选是这一整套里最危险的一类语义漂移: 右侧面板与"恢复/建分支"都会跟着换目标。
    所以这条用例量两头 —— 选中一动不动, 而"藏了谁"由说明行与标记色当场告诉用户。
    """
    app, view, _hooks = graph
    view.set_items(_fold_items(current="a2"))
    view.redraw(DARK)
    _pump(app)
    view.select("a1", DARK)
    _pump(app)

    view.toggle("a", DARK)
    _pump(app)
    assert view.selected == "a1", "折叠不许改选中"
    meta = view._texts["a"].meta
    assert meta.startswith(tr("graph.contains_selected")), (
        f"藏着选中项时必须写在最前面(被裁也要先保住它): 实测 {meta!r}"
    )
    assert tr("graph.contains_current") in _traces(view, "a"), (
        "当前节点也被藏起来了, 必须写出来(说明行放不下时挂在悬停提示里)"
    )
    assert view.canvas.itemcget(view._marker_items["a"], "fill") == DARK.accent

    view.select("b", DARK)  # 选中挪到折叠框之外
    _pump(app)
    assert not view._texts["a"].meta.startswith(tr("graph.contains_selected"))
    assert view._texts["a"].meta.startswith(tr("graph.contains_current")), (
        "当前节点还在里面"
    )
    assert view.canvas.itemcget(view._marker_items["a"], "fill") == DARK.accent

    view.toggle("a", DARK)  # 展开: 两个痕迹都该消失
    _pump(app)
    assert tr("graph.contains_selected") not in view._texts["a"].meta
    assert tr("graph.contains_current") not in view._texts["a"].meta
    assert view._texts["a"].full == "", (
        f"展开之后没有要回看的东西了: {view._texts['a']!r}"
    )
    assert view.canvas.itemcget(view._marker_items["a"], "fill") == DARK.text_muted


def test_switching_data_drops_the_stale_folds(graph: Any) -> None:
    """折叠集合与现有节点**取交集**: 换一批数据之后旧 id 不该继续压着图."""
    app, view, _hooks = graph
    view.set_items(_fold_items())
    view.redraw(DARK)
    _pump(app)
    view.toggle("a", DARK)
    _pump(app)
    assert view.collapsed == frozenset({"a"})

    view.set_items(_fan("other", 2))
    view.redraw(DARK)
    _pump(app)

    assert view.collapsed == frozenset()
    assert view.node_ids == ("other", "other-0", "other-1")


# -- 折叠/展开的交互 --------------------------------------------------
#
# 三条命中各干一件事: 标记 = 折叠/展开, 框 = 选中, 空白 = 平移。入口就在框右下角,
# 键盘只做"空格/回车折叠当前选中项"(方向键/WASD 已经绑给漫游了)。


def _marker_spot(view: TreeView, node_id: str) -> tuple[int, int]:
    """某个框的折叠标记中心在**控件坐标**里的位置."""
    rect = view.layout.marker_rect(node_id)
    assert rect is not None, f"{node_id} 应该有折叠标记"
    return _widget_spot(view, rect[0] + rect[2] / 2, rect[1] + rect[3] / 2)


def test_clicking_the_marker_folds_and_expands_that_subtree(graph: Any) -> None:
    """入口就在框的右下角: 点标记 = 折叠, 再点 = 展开(框本身上不做这件事)."""
    app, view, hooks = graph
    view.set_items(_fold_items())
    view.redraw(DARK)
    _pump(app)

    _press(view, _marker_spot(view, "a"))
    _release(view, _marker_spot(view, "a"))
    _pump(app)
    assert view.collapsed == frozenset({"a"})
    assert view.node_ids == ("root", "a", "b")
    assert hooks.selected == [], "点标记不该顺手选中那个节点"

    _press(view, _marker_spot(view, "a"))  # 折叠后标记还在, 位置已经重算
    _release(view, _marker_spot(view, "a"))
    _pump(app)
    assert view.collapsed == frozenset()
    assert view.node_ids == ("root", "a", "a1", "a2", "a3", "b")


def test_pressing_the_marker_does_not_pan_or_select(graph: Any) -> None:
    """按在标记上拖动: 既不平移也不选中, 而且拖走就不算点击(不会顺手折叠)."""
    app, view, hooks = graph
    app.geometry("900x600")
    _pump(app)
    view.set_items(_fan("root", 12))  # 宽扇: 横向本来拖得动
    view.redraw(DARK)
    _pump(app)

    before = view.canvas.xview()
    spot = _marker_spot(view, "root")
    _press(view, spot)
    _drag(view, spot[0] - 120, spot[1])
    _release(view, (spot[0] - 120, spot[1]))
    _pump(app)

    assert view.canvas.xview() == before, "按在标记上不该把画面拖走"
    assert hooks.selected == []
    assert view.selected is None
    assert view.collapsed == frozenset(), "拖走之后松手不算点击标记"


def test_space_and_enter_fold_the_selected_node(graph: Any) -> None:
    """键盘: Tab 到画布之后, 空格/回车收起或展开**当前选中项**(方向键照旧是漫游).

    选中项由**宿主**决定(画布只把点击告诉它): 所以这里先真点一下框(应拿到回调),
    再让宿主把选中定下来 —— 与 main_window 里那条路径一致。
    """
    app, view, hooks = graph
    view.set_items(_fold_items())
    view.redraw(DARK)
    _pump(app)
    view.canvas.focus_set()
    _pump(app)

    _press(view, _box_spot(view, "a"))
    _release(view, _box_spot(view, "a"))
    _pump(app)
    assert [item.backup_id for item in hooks.selected] == ["a"]
    view.select("a", DARK)
    _pump(app)

    view.canvas.event_generate("<space>", when="now")
    _pump(app)
    assert view.collapsed == frozenset({"a"}), "空格没有收起选中项的子树"

    view.canvas.event_generate("<Return>", when="now")
    _pump(app)
    assert view.collapsed == frozenset(), "回车没有把子树放回来"


def test_folding_keeps_the_clicked_box_in_view(graph: Any) -> None:
    """折叠之后那个框仍在视野里("scrollregion 变小 → 偏移被夹回"会让图看着跑掉)."""
    app, view, _hooks = graph
    app.geometry("900x600")
    _pump(app)
    view.set_items(_fan("root", 12))
    view.redraw(DARK)
    _pump(app)
    view.scroll("right")  # 先滚到右边: 折叠前的偏移是"越界"的
    _pump(app)

    spot = _marker_spot(view, "root")
    _press(view, spot)
    _release(view, spot)
    _pump(app)
    assert view.collapsed == frozenset({"root"})

    box = view.layout.box("root")
    assert box is not None
    left = view.canvas.canvasx(0)
    top = view.canvas.canvasy(0)
    assert left <= box.center_x <= left + view.canvas.winfo_width(), (
        "折叠后刚点的那个框横向不在视野里"
    )
    assert top <= box.y + box.height / 2 <= top + view.canvas.winfo_height(), (
        "折叠后刚点的那个框纵向不在视野里"
    )


def test_switching_views_keeps_the_folds() -> None:
    """切到时间线再切回来, 折叠还在(它活在这个视图的内存里, 切视图不重建视图)."""
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app._open_game_detail("outer-wilds")
    _pump(app)
    view = app._tree_view
    parent = next(iter(view.layout.branches))

    view.toggle(parent, app.p)
    _pump(app)
    assert view.collapsed == frozenset({parent})

    app._switch_view(ViewKind.TIMELINE)
    _pump(app)
    app._switch_view(ViewKind.BRANCH)
    _pump(app)

    assert app._tree_view.collapsed == frozenset({parent}), "切视图不该把折叠清掉"
    assert parent in app._tree_view.node_ids


def _widget_spot(view: TreeView, canvas_x: float, canvas_y: float) -> tuple[int, int]:
    """画面坐标 -> 画布**控件坐标**(减掉滚动偏移).

    不能拿 ``canvasx(画面坐标)`` 去凑: 那个方向是"控件 -> 画面"(名字里的 canvas 指的是
    **画面**那一侧), 没滚动时两者恰好相等, 一旦拖过/滚过就整片错位 —— 实测"点标记折叠"
    就是这么点空的(点到的位置与标记差了一个滚动偏移)。
    """
    return (
        int(canvas_x - view.canvas.canvasx(0)),
        int(canvas_y - view.canvas.canvasy(0)),
    )


def _box_spot(view: TreeView, node_id: str) -> tuple[int, int]:
    """某个框中心在**控件坐标**里的位置."""
    box = view.layout.box(node_id)
    assert box is not None
    return _widget_spot(view, box.center_x, box.y + box.height / 2)


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
