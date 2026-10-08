"""UiKit 无头单元测试.

用假控件替换 customtkinter 控件, 验证控件创建、样式映射与主题重绘,
不需要显示环境(ubuntu CI 无头也能覆盖全部按钮样式分支)。
"""

from __future__ import annotations

import tkinter as tk
from types import SimpleNamespace
from typing import Any, cast

import customtkinter as ctk
import pytest

import archive_management.ui.widgets as widgets
from archive_management.ui.palette import DARK, LIGHT
from archive_management.ui.widgets import BUTTON_STYLES, DESTRUCTIVE_STYLES

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("控件主题重绘"),
    pytest.mark.layer("unit"),
]


# 度量包装只用到窗口的**缩放系数**(用例把它钉死), 所以这里给个类型正确、不会被碰的替身.
_NO_WINDOW = cast("tk.Misc", None)


class _FakeFont:
    """假字体: 未缩放的字号量出来的尺寸(用于验证度量要过窗口缩放)."""

    def __init__(self, width: int, linespace: int) -> None:
        self._width = width
        self._linespace = linespace

    def measure(self, _text: str) -> int:
        """未缩放的文字宽度."""
        return self._width

    def metrics(self, option: str) -> int:
        """未缩放的行高(只支持 linespace, 与真实契约一致)."""
        assert option == "linespace"
        return self._linespace


def _scaled(monkeypatch: pytest.MonkeyPatch, scale: float) -> Any:
    """把窗口缩放钉成 ``scale`` 后的度量包装(用假字体, 不建窗口)."""
    monkeypatch.setattr(widgets, "window_scaling", lambda _window: scale)
    return widgets.measured_font(_FakeFont(width=231, linespace=14), _NO_WINDOW)


class _FakeAnchor:
    """只回答"提示定位"用到的四个几何量的锚点替身(不建窗口)."""

    def __init__(self, *, x: int, y: int, height: int, screen_height: int) -> None:
        self._x = x
        self._y = y
        self._height = height
        self._screen_height = screen_height

    def winfo_rootx(self) -> int:
        """控件左边界(屏幕坐标)."""
        return self._x

    def winfo_rooty(self) -> int:
        """控件上边界(屏幕坐标)."""
        return self._y

    def winfo_height(self) -> int:
        """控件高度."""
        return self._height

    def winfo_screenheight(self) -> int:
        """屏幕高度(定位要按它判断下方放不放得下)."""
        return self._screen_height


class _FakeTip:
    """只回答"需要多高"的提示窗口替身."""

    def __init__(self, height: int) -> None:
        """绑定这个提示需要的高度."""
        self._height = height

    def update_idletasks(self) -> None:
        """真实实现要在这里刷一次几何(否则拿到的高度是 1); 替身无事可做."""

    def winfo_reqheight(self) -> int:
        """提示窗口需要的高度."""
        return self._height


def test_the_tooltip_opens_below_the_anchor_when_it_fits() -> None:
    """空间够时提示在控件**下方**一个间隙处, 左边界与控件对齐."""
    anchor = cast(
        "ctk.CTkBaseClass", _FakeAnchor(x=100, y=200, height=30, screen_height=900)
    )

    position = widgets._tooltip_position(anchor, cast("tk.Toplevel", _FakeTip(40)))

    assert position == f"+100+{200 + 30 + widgets._TOOLTIP_GAP}"


def test_the_tooltip_flips_above_when_there_is_no_room_below() -> None:
    """下方放不下时改到控件**上方** —— 这是提示贴着屏幕底边时唯一不缩水的出路."""
    anchor = cast(
        "ctk.CTkBaseClass", _FakeAnchor(x=100, y=860, height=30, screen_height=900)
    )

    position = widgets._tooltip_position(anchor, cast("tk.Toplevel", _FakeTip(40)))

    assert position == f"+100+{860 - 40 - widgets._TOOLTIP_GAP}"


def test_measured_font_scales_both_width_and_line_height(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """渲染字号下的**宽度与行高**都要过缩放.

    125% 实测: ``CTkFont(size=12)`` 量一条 231px、linespace 14, 而控件里真正用的字体
    量同一条 288px、linespace 18。只缩放 ``measure`` 是不够的 —— 海报卡片"一行名也占
    两行高"靠的就是 ``metrics("linespace")``, 少缩放 1.25 倍就会把两行的高度算成一行。
    """
    scaled = _scaled(monkeypatch, 1.25)

    assert scaled.measure("任意") == 289, "宽度没按缩放换算"
    assert scaled.metrics("linespace") == 18, "行高没按缩放换算"


def test_measured_font_is_a_no_op_when_the_window_is_unscaled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """缩放为 1 时原样返回: 包装一层只会白白多一次四舍五入."""
    font = _FakeFont(width=231, linespace=14)
    monkeypatch.setattr(widgets, "window_scaling", lambda _window: 1.0)

    assert widgets.measured_font(font, _NO_WINDOW) is font


class _FakeCtkWidget:
    """记录 configure 调用的假 customtkinter 控件."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        self.master = master
        self.kwargs = dict(kwargs)
        self.handlers: dict[str, Any] = {}
        # 每次 configure 的参数: 用例靠它数"到底写了几次"(写回路就是写得太频).
        self.configure_calls: list[dict[str, Any]] = []
        # 假宽度: 默认 0(= 还没布局), 用例可以自己设.
        self.width = 0

    def configure(self, **kwargs: Any) -> None:
        self.configure_calls.append(dict(kwargs))
        self.kwargs.update(kwargs)

    def bind(self, sequence: str, func: Any, add: str | None = None) -> None:
        """记录回调(忽略 add, 测试只关心函数本身)."""
        self.handlers[sequence] = func

    def winfo_width(self) -> int:
        """假宽度(见 ``width``)."""
        return self.width

    def cget(self, option: str) -> Any:
        """按真实控件的契约读回选项(``state`` 默认 ``normal``: 重绘要按它取色)."""
        if option in self.kwargs:
            return self.kwargs[option]
        return "normal" if option == "state" else None


@pytest.fixture
def kit(monkeypatch: pytest.MonkeyPatch) -> widgets.UiKit:
    """把 ctk 控件替换为假实现, 返回空的 UiKit."""
    monkeypatch.setattr(
        ctk,
        "CTkFrame",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(
        ctk,
        "CTkLabel",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(
        ctk,
        "CTkButton",
        lambda master=None, **kwargs: _FakeCtkWidget(master=master, **kwargs),
    )
    monkeypatch.setattr(ctk, "CTkFont", lambda **_kwargs: object())
    return widgets.UiKit()


class _FakeScrollableFrame(_FakeCtkWidget):
    """假滚动容器: 记录主题重绘时对内部 canvas 背景的更新."""

    def __init__(self, master: Any = None, **kwargs: Any) -> None:
        super().__init__(master=master, **kwargs)
        self.canvas_bg: str | None = None


@pytest.fixture
def scroll_kit(monkeypatch: pytest.MonkeyPatch) -> widgets.UiKit:
    """把 CTkScrollableFrame 替换为假实现, 返回空的 UiKit."""
    monkeypatch.setattr(
        ctk,
        "CTkScrollableFrame",
        lambda master=None, **kwargs: _FakeScrollableFrame(master=master, **kwargs),
    )
    return widgets.UiKit()


def test_scroll_frame_repaints_background_on_theme_change(
    scroll_kit: widgets.UiKit,
) -> None:
    """回归: 滚动列表的背景必须跟随主题, 否则浅/深色切换后颜色错位.

    CustomTkinter 只在构造时把内层 canvas 的背景取为父容器当时的颜色,
    因此 ``scroll_frame`` 必须在每次重绘时用调色板重设 ``fg_color``。
    """
    frame = scroll_kit.scroll_frame(scroll_kit, bg_key="panel")
    scroll_kit.apply(DARK)

    assert frame.kwargs["fg_color"] == DARK.panel
    assert frame.kwargs["scrollbar_button_color"] == DARK.border


def test_scroll_frame_ignores_destroyed_widget(
    scroll_kit: widgets.UiKit,
) -> None:
    """回归: 已销毁的滚动容器不应让重绘抛出 TclError."""
    frame = scroll_kit.scroll_frame(scroll_kit, bg_key="sidebar")

    def boom(**_kwargs: Any) -> None:
        raise tk.TclError("bad window path name")

    frame.configure = boom
    scroll_kit.apply(DARK)
    scroll_kit.apply(DARK)


def test_frame_recolored_with_border(kit: widgets.UiKit) -> None:
    frame = kit.frame(kit, bg_key="panel", border_key="border")
    kit.apply(DARK)
    assert frame.kwargs["fg_color"] == DARK.panel
    assert frame.kwargs["border_color"] == DARK.border


def test_frame_without_border_key_has_no_border_color(kit: widgets.UiKit) -> None:
    frame = kit.frame(kit, bg_key="raised")
    kit.apply(DARK)
    assert frame.kwargs["fg_color"] == DARK.raised
    assert "border_color" not in frame.kwargs


def test_label_maps_each_style_color(kit: widgets.UiKit) -> None:
    labels = {
        style: kit.label(kit, style, style=style)
        for style in ("primary", "body", "muted", "hint", "h2")
    }
    kit.apply(DARK)
    assert labels["primary"].kwargs["text_color"] == DARK.text_primary
    assert labels["body"].kwargs["text_color"] == DARK.text_body
    assert labels["muted"].kwargs["text_color"] == DARK.text_muted
    assert labels["hint"].kwargs["text_color"] == DARK.text_hint
    assert labels["h2"].kwargs["text_color"] == DARK.text_primary


def test_buttons_painted_by_each_style(kit: widgets.UiKit) -> None:
    buttons = {
        style: kit.button(kit, style, style=style)
        for style in ("accent", "danger", "soft", "ghost")
    }
    kit.apply(DARK)
    assert buttons["accent"].kwargs["fg_color"] == DARK.accent
    assert buttons["danger"].kwargs["fg_color"] == DARK.danger
    assert buttons["soft"].kwargs["fg_color"] == DARK.accent_soft
    ghost = buttons["ghost"]
    assert ghost.kwargs["fg_color"] == DARK.raised
    assert ghost.kwargs["border_color"] == DARK.border


@pytest.mark.parametrize("palette", [DARK, LIGHT])
def test_destructive_styles_stay_inside_the_danger_range(palette: Any) -> None:
    """ "危险操作的颜色范围"就是这两种样式, 而且两套主题下都真的带危险色.

    这条把"删除类按钮的颜色必须落在危险色范围内"钉在 `widgets` 层:
    范围变了 (例如把主色也算进去) 会立刻变红。
    """
    assert set(DESTRUCTIVE_STYLES) <= set(BUTTON_STYLES)
    assert "accent" not in DESTRUCTIVE_STYLES, "主色不是危险色"
    for style in DESTRUCTIVE_STYLES:
        colors = widgets.button_colors(palette, style)
        assert palette.danger in (colors.fg, colors.text), f"{style} 没带危险色"


def test_every_action_kind_maps_to_its_own_style(kit: widgets.UiKit) -> None:
    """动作性质与样式**一一对应**, 且 ``kind=`` 真的按性质取色.

    两个性质共用一种样式的话, "从实测颜色反推性质"这条判据就不再成立(守卫靠的就是这个反推),
    所以这里把一一对应本身钉住; 顺便验一下 `UiKit.button(kind=...)` 与 `style=` 等价。
    """
    styles = [widgets.style_for(kind) for kind in widgets.ACTION_KINDS]
    assert len(set(styles)) == len(widgets.ACTION_KINDS), "性质与样式必须一一对应"
    assert set(styles) <= set(BUTTON_STYLES)

    by_kind = kit.button(kit, "按性质", kind="primary")
    by_style = kit.button(kit, "按样式", style="accent")
    kit.apply(DARK)
    assert by_kind.kwargs["fg_color"] == by_style.kwargs["fg_color"] == DARK.accent
    assert by_kind.kwargs["fg_color"] == widgets.button_colors(DARK, "accent").fg


def test_button_styles_share_one_source_and_danger_is_its_own_color(
    kit: widgets.UiKit,
) -> None:
    """四种样式的配色只有一处定义, 且危险色不等于主色(评审时定的).

    "破坏性动作穿危险色"要成立, 前提是危险色**只**在一个地方定义: 这里把它钉住 ——
    :func:`widgets.button_colors` 是唯一出处, 登记式的按钮与就地创建的按钮
    (对话框成对的取消/确认、窗口页脚)取到的必须是同一份颜色。
    """
    styles: tuple[widgets.ButtonStyle, ...] = ("accent", "danger", "soft", "ghost")
    colors = {style: widgets.button_colors(DARK, style) for style in styles}

    assert colors["danger"].fg == DARK.danger
    assert colors["danger"].text == DARK.danger_text
    assert colors["danger"].fg != colors["accent"].fg, "危险色不能与主色同色"
    assert colors["ghost"].border == DARK.border, "次要按钮要描边"
    assert colors["accent"].border is None, "主色/危险色不描边"
    triples = {(item.fg, item.hover, item.text) for item in colors.values()}
    assert len(triples) == len(styles), "四种样式必须互相区分"

    manual = ctk.CTkButton(kit)
    widgets.paint_button_style(manual, DARK, "danger")
    registered = kit.button(kit, "删除", style="danger")
    kit.apply(DARK)
    for key in ("fg_color", "hover_color", "text_color"):
        assert manual.kwargs[key] == registered.kwargs[key], (
            f"临时按钮的 {key} 与登记式不一致"
        )
    assert manual.kwargs["fg_color"] == colors["danger"].fg


def test_button_registers_and_repaints(kit: widgets.UiKit) -> None:
    button = kit.button(kit, "动作", style="accent", command=None, width=88, height=30)
    kit.apply(DARK)
    assert button.kwargs["width"] == 88
    assert button.kwargs["height"] == 30
    assert button.kwargs["fg_color"] == DARK.accent


def test_register_returns_unsubscribe_skips_inactive(kit: widgets.UiKit) -> None:
    calls: list[Any] = []
    kit.register(lambda p: calls.append("kept"))
    unsubscribe = kit.register(lambda p: calls.append("removed"))
    kit.apply(DARK)
    assert calls == ["kept", "removed"]
    unsubscribe()
    kit.apply(DARK)
    assert calls == ["kept", "removed", "kept"]


def test_register_unsubscribe_is_idempotent(kit: widgets.UiKit) -> None:
    calls: list[Any] = []
    unsubscribe = kit.register(lambda p: calls.append("x"))
    unsubscribe()
    unsubscribe()
    kit.apply(DARK)
    assert calls == []


def test_apply_skips_repaint_raising_tcl_error(kit: widgets.UiKit) -> None:
    """重绘已销毁控件抛 TclError 时应被跳过而非中断整批重绘."""
    painted: list[str] = []
    kit.register(lambda p: painted.append("before"))
    kit.register(lambda _p: (_ for _ in ()).throw(tk.TclError("bad window path")))
    kit.register(lambda p: painted.append("after"))
    kit.apply(DARK)
    assert painted == ["before", "after"]


def test_poster_backing_uses_the_panel_colour_only_over_a_real_cover() -> None:
    """海报标记的底衬: 有封面图才加底衬, 回落名称占位时必须是彻底透明.

    这条规则原先只在"真有一张封面图"的界面用例里验过, 而那条用例在 CI 上一直被
    跳过(见 tests/tk_guard.py) —— 抽成纯函数后不建窗口也能钉住两个方向。
    """
    from archive_management.ui.home_page import poster_backing

    assert poster_backing(None, DARK) == "transparent", "没有封面时不该有背景色"
    assert poster_backing(object(), DARK) == DARK.panel, "有封面时用面板色做底衬"


def test_selection_colours_use_the_soft_accent_not_the_primary_green() -> None:
    """选中 = 淡底 + 描边, 且不能与主按钮的实心强调色同色(评审时定的).

    列表与海报共用这一条规则: "我选中了谁"和"哪里能点"必须是两件事, 否则同一屏里
    出现两种绿, 用户分不出哪个是状态、哪个是操作。
    """
    from archive_management.ui.palette import DARK, LIGHT

    for palette in (DARK, LIGHT):
        background, border = palette.selection_colors(True)
        assert background == palette.accent_soft
        assert border == palette.accent_soft_border
        assert background != palette.accent, "选中不能与主按钮同色"
        assert palette.selection_colors(False) == (palette.card, palette.card_border)


def test_scrollbar_is_needed_only_when_the_content_overflows() -> None:
    """滚动条只在内容真的超出视口时出现, 并容忍布局取整(评审时定的)."""
    assert widgets.scrollbar_needed(900, 520) is True
    assert widgets.scrollbar_needed(300, 520) is False
    # 取整误差(1~2px)不算溢出: 否则会立起一条拖不动的滚动条.
    assert widgets.scrollbar_needed(401, 400) is False
    assert widgets.scrollbar_needed(403, 400) is True


class _FakeCanvas:
    """只提供 ``sync_scrollbar`` 需要的两个读数的假画布."""

    def __init__(self, content_height: int, viewport_height: int) -> None:
        self.content_height = content_height
        self.viewport_height = viewport_height

    def bbox(self, _tag: str) -> tuple[int, int, int, int] | None:
        """内容在画布里的包围盒(只用到上下边界)."""
        return (0, 0, 100, self.content_height)

    def winfo_height(self) -> int:
        """视口高度."""
        return self.viewport_height


class _FakeScrollbar:
    """记录 grid/grid_forget 的假滚动条.

    ``forgot`` / ``removed`` 分开记: 两者都能收起滚动条, 但只有 ``grid_forget`` 会
    **同时清掉 CTkBaseClass 记下的最后一次几何调用**(见 ``_hide_scrollbar``)。
    """

    def __init__(self, visible: bool, *, reports_mapped: bool | None = None) -> None:
        self.visible = visible
        self.requested_height: int | None = None
        self._desired_height: int | None = None
        self.forgot = False
        self.removed = False
        # ``winfo_ismapped`` 的读数: 真窗口里它与"在不在布局里"不是一回事 —— 容器刚
        # 建好、窗口还没画出来、所在分区藏着时读到的都是 0。
        self._reports_mapped = visible if reports_mapped is None else reports_mapped

    def winfo_ismapped(self) -> bool:
        """Tk 报告的映射状态."""
        return self._reports_mapped

    def grid(self, **_kwargs: Any) -> None:
        """显示."""
        self.visible = True
        self._reports_mapped = True

    def grid_forget(self) -> None:
        """收起(并让 CTk 忘掉摆放参数)."""
        self.visible = False
        self._reports_mapped = False
        self.forgot = True

    def grid_remove(self) -> None:
        """收起(只收起, 不告诉 CTk)."""
        self.visible = False
        self._reports_mapped = False
        self.removed = True

    def _set_dimensions(
        self, width: int | None = None, height: int | None = None
    ) -> None:
        """CTk 内部用它设定请求尺寸(压制滚动条高度时会调用)."""
        self.requested_height = height


def _scroll_frame(content_height: int, viewport_height: int, *, visible: bool) -> Any:
    """组装一个只有内部读数的假滚动容器."""
    return SimpleNamespace(
        _parent_canvas=_FakeCanvas(content_height, viewport_height),
        _scrollbar=_FakeScrollbar(visible),
    )


def test_sync_scrollbar_hides_and_shows_by_content_height() -> None:
    """``sync_scrollbar`` 按内容高度收起/恢复滚动条, 并返回它是否应当可见."""
    fitting = _scroll_frame(300, 520, visible=True)
    assert widgets.sync_scrollbar(fitting) is False
    assert fitting._scrollbar.visible is False, "内容装得下时要收起滚动条"

    overflowing = _scroll_frame(900, 520, visible=False)
    overflowing._scrollbar.visible = True  # CTk 建容器时本来就是摆上的
    assert widgets.sync_scrollbar(overflowing) is True
    assert overflowing._scrollbar.visible is True, "内容溢出时要恢复滚动条"


def test_the_first_verdict_assumes_the_bar_is_shown() -> None:
    """回归: 首次判定必须当成"已显示", 否则滚动条永远收不起来.

    ``CTkScrollableFrame`` 建容器时一定把滚动条摆进布局, 而"是否已映射"在窗口还没画出来、
    或所在分区/子页是隐藏的时候读到的总是 0。拿它当初值就会得出"已经收起了"的错误结论,
    之后内容装得下时因为"决策没变"而什么都不做 —— 评审时发现页两个列表、定时任务
    窗口、编辑标签弹窗右侧的常驻灰条就是这么来的。
    """
    frame = _scroll_frame(300, 520, visible=True)
    frame._scrollbar._reports_mapped = False  # 窗口还没画出来, 读数还是"未映射"

    assert widgets.sync_scrollbar(frame) is False
    assert frame._scrollbar.forgot is True, "装得下就必须真的把它摘掉"


def test_hiding_uses_grid_forget_so_ctk_cannot_put_it_back() -> None:
    """收起用 ``grid_forget``: ``grid_remove`` 不会清掉 CTk 记的几何调用.

    ``CTkBaseClass`` 会把最后一次几何调用记下来, 缩放/外观变化时重放一遍
    (见 ``CTkBaseClass._set_scaling``) —— 用 ``grid_remove`` 收起时那条记录还在,
    已经收起的滚动条会被重新摆回去, 而尺寸没变就不会再有一次判定来纠正它。
    """
    frame = _scroll_frame(300, 520, visible=True)

    widgets.sync_scrollbar(frame)

    assert frame._scrollbar.forgot is True, "应当用 grid_forget"
    assert frame._scrollbar.removed is False, "不能只用 grid_remove"


def test_sync_scrollbar_swallows_a_destroyed_widget() -> None:
    """控件已销毁时(TclError)只报"不需要滚动条", 不能把异常抛给重绘路径."""
    broken = _scroll_frame(900, 520, visible=True)

    def boom(_tag: str) -> tuple[int, int, int, int] | None:
        raise tk.TclError("bad window path name")

    broken._parent_canvas.bbox = boom

    assert widgets.sync_scrollbar(broken) is False
    assert broken._scrollbar.visible is True, "异常路径什么都不改"


@pytest.mark.blocker
def test_sync_scrollbar_ignores_reentrant_notifications() -> None:
    """改几何会引出新的 Configure: 重入时必须直接返回, 否则递归到崩溃.

    编辑标签页连点"添加标签"(内容刚好越过视口、要生成滚动条)时实测报
    ``maximum recursion depth exceeded``。
    """
    frame = _scroll_frame(300, 520, visible=True)
    widgets.sync_scrollbar(frame)  # 先装得下 → 收起, 把状态带入"已收起"
    frame._parent_canvas.content_height = 900  # 内容涨到溢出 → 走显示路径
    calls: list[bool] = []

    def reenter(**_kwargs: Any) -> None:
        """模拟 Tk 在几何变化时同步回调我们的判定."""
        calls.append(widgets.sync_scrollbar(frame))
        frame._scrollbar.visible = True

    frame._scrollbar.grid = reenter

    assert widgets.sync_scrollbar(frame) is True
    # 内层那次是被自己引出的通知: 直接返回 False, 不再翻一次.
    assert calls == [False]
    assert frame._scrollbar.visible is True


def test_sync_scrollbar_hides_whenever_the_content_fits() -> None:
    """装得下就必须收起 —— 哪怕滚动条本来就在.

    "只小一点点也保持现状"这种保守规则在按内容定高的窗口上是有害的: 内容高度恰好等于
    视口, 永远达不到"再低 8px", 滚动条从此收不起来(设置窗口实测, 评审时定的)。
    """
    fits_but_shown = _scroll_frame(516, 520, visible=True)
    assert widgets.sync_scrollbar(fits_but_shown) is False
    assert fits_but_shown._scrollbar.visible is False, "装得下时要收起滚动条"


def test_sync_scrollbar_keeps_a_shown_bar_for_a_small_overflow() -> None:
    """刚刚溢出(2~8px)且已经显示时保持显示: 这才是滞回真正要挡的那一段."""
    just_over = _scroll_frame(526, 520, visible=True)
    assert widgets.sync_scrollbar(just_over) is True
    assert just_over._scrollbar.visible is True


def test_sync_scrollbar_does_not_pop_up_for_a_tiny_overflow() -> None:
    """只多一点点(不超过 8px)不该立起一条滚动条: 否则会在边界上来回抽动."""
    tiny = _scroll_frame(300, 520, visible=True)
    assert widgets.sync_scrollbar(tiny) is False, "先装得下 → 收起"
    tiny._parent_canvas.content_height = 526  # 只多 6px

    assert widgets.sync_scrollbar(tiny) is False
    assert tiny._scrollbar.visible is False


class _CoupledFrame:
    """画布与滚动条互相影响的假容器(用来验证判定会收敛而不是来回翻).

    滚动条出现会让画布**变窄**, 内容因此只可能更高; 收起会让画布变宽, 内容只可能更矮。
    这条单调性是"不会在两个状态之间反复抽动"的依据, 这里把它写进假件, 让不收敛能被
    用例抓住(编辑标签页连点"添加标签"曾实测抽到崩溃)。
    """

    def __init__(self, base_content: int, *, visible: bool) -> None:
        self._base = base_content
        self.visible = visible
        self._scrollbar = SimpleNamespace(
            winfo_ismapped=lambda: self.visible,
            grid=lambda **_kwargs: setattr(self, "visible", True),
            grid_forget=lambda: setattr(self, "visible", False),
            grid_remove=lambda: setattr(self, "visible", False),
            _set_dimensions=lambda **_kwargs: None,
        )
        self._parent_canvas = SimpleNamespace(
            bbox=lambda _tag: (0, 0, 100, self._base + (16 if self.visible else 0)),
            winfo_height=lambda: 520,
        )


@pytest.mark.blocker
def test_sync_scrollbar_converges_instead_of_flipping() -> None:
    """两种起点各跑两轮: 第一轮定下来的状态第二轮不再改(不能来回翻)."""
    overflowing = _CoupledFrame(500, visible=True)
    assert widgets.sync_scrollbar(overflowing) is False, "内容装得下 → 收起"
    assert overflowing.visible is False
    overflowing._base = 530  # 内容变高 → 溢出
    assert widgets.sync_scrollbar(overflowing) is True
    assert overflowing.visible is True, "溢出时应当显示滚动条"
    assert widgets.sync_scrollbar(overflowing) is True, "第二轮不该又翻回去"

    fitting = _CoupledFrame(500, visible=True)
    assert widgets.sync_scrollbar(fitting) is False
    assert fitting.visible is False, "装得下时应当收起滚动条"
    assert widgets.sync_scrollbar(fitting) is False, "第二轮不该又翻回来"


def test_show_scrollbar_restores_the_grid_position() -> None:
    """重新显示必须给出摆放参数: ``grid()`` 不带参数在 Tk 里只是查询."""
    frame = _scroll_frame(300, 520, visible=True)
    frame._parent_frame = SimpleNamespace(
        cget=lambda name: 8 if name == "corner_radius" else 0
    )
    frame._border_width = 0
    options: dict[str, Any] = {}
    assert widgets.sync_scrollbar(frame) is False, "先装得下 → 收起"
    frame._scrollbar.grid = lambda **kwargs: options.update(kwargs)
    frame._parent_canvas.content_height = 900  # 内容涨到溢出 → 必须放回来

    widgets.sync_scrollbar(frame)

    assert options["row"] == 1
    assert options["column"] == 1
    assert options["sticky"] == "nsew"
    assert options["pady"] == 8
    # 显示路径同样要压住高度请求: 只加参数而不压高度时, 一显示就又把视口撑高.
    assert frame._scrollbar.requested_height == 1


class _FakeContainer(_FakeCtkWidget):
    """记录 bind 回调的假容器(方便手动触发 <Configure>)."""

    def __init__(self, scrollbar: _FakeScrollbar | None = None) -> None:
        super().__init__()
        self.idle_calls = 0
        # 排上的首帧回调: wraplength 的判定就挂在它上面(用例要自己跑一次).
        self.idle_callbacks: list[Any] = []
        if scrollbar is not None:
            self._scrollbar = scrollbar

    def after_idle(self, func: Any) -> None:
        """记录首帧回调(测试不跑事件循环, 只确认它被排上了)."""
        self.idle_calls += 1
        self.idle_callbacks.append(func)


@pytest.mark.blocker
def test_auto_scrollbar_pins_the_scrollbar_height_to_one_pixel() -> None:
    """真窗口故障的根因: 滚动条的高度请求必须压到 1px(默认 200px 会撑高视口).

    CTk 的滚动条默认请求 200px 高, 而它与画布同处第 1 行: 一显示就把这一行撑到
    200, 视口跟着变高 → 内容又装得下 → 收起 → 视口变矮 → 又溢出 → 再显示 ……
    实测就在边界上无限抽动(用户报的"编辑标签页抽搐/报错"就是这个机制, 那时还伴随
    ``maximum recursion depth exceeded``)。戳穿它的判据就是"高度请求 == 1"。
    """
    bar = _FakeScrollbar(visible=True)
    frame = _FakeContainer(scrollbar=bar)

    widgets.auto_scrollbar(frame)

    assert bar.requested_height == 1, "构造时就该把滚动条的高度请求压到 1px"
    assert bar._desired_height == 1


def test_auto_scrollbar_binds_only_the_container_without_a_canvas() -> None:
    """替身/精简控件没有内层画布时只绑容器自己的 <Configure>, 不能因此抛错."""
    frame = _FakeContainer()

    widgets.auto_scrollbar(frame)

    assert set(frame.handlers) == {"<Configure>"}
    assert frame.idle_calls == 1, "首帧还要判定一次滚动条"


def test_track_wraplength_follows_the_width_and_ignores_a_dead_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """成段说明按实际宽度换行; 控件销毁时的回调不能往外抛.

    判定是**延后到 idle** 的: ``<Configure>`` 只负责排一次, 量宽度发生在布局停下之后
    (事件回调里读到的是旧值)。容器或标签销毁时也会发 ``<Configure>``, 那时控件已经
    没了 —— 不兜住 ``TclError`` 就会在 stderr 留下一串 ``invalid command name ...``。
    """
    container = _FakeContainer()
    container.width = 800
    label = _FakeCtkWidget()
    widgets.track_wraplength(container, label, inset=20)

    assert label.kwargs["wraplength"] == 240
    # 标签还没布局(宽度 0) → 退回按容器宽度减 inset 估。
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 780, "说明文字要跟着可用宽度换行"

    def boom(**_kwargs: Any) -> None:
        raise tk.TclError("bad window path name")

    monkeypatch.setattr(label, "configure", boom)
    container.width = 900  # 换一个值: 否则"宽度没变就不动"会直接返回, 跑不到 configure
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()


def test_track_wraplength_never_exceeds_the_width_the_label_got() -> None:
    """换行上限是**标签自己分到的宽度**, 而且同一轮只排一次判定.

    实测(监控目录页): 容器 1312 / 标签实得 928(同一行摆着四个按钮, 占掉约 350),
    按容器算出来的 wraplength 是 1122 —— 文字按 1122 排成**一行**, 标签只有 928,
    于是尾部约 8 个字被 Tk 硬裁掉(既不换行也没省略号)。
    """
    container = _FakeContainer()
    container.width = 1312
    label = _FakeCtkWidget()
    label.width = 928
    widgets.track_wraplength(container, label, inset=190)

    container.handlers["<Configure>"](SimpleNamespace(width=1))
    scheduled = container.idle_calls
    # 连续事件只排一次(合并): 同时量容器与标签, 不能混着新旧两个读数写。
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    assert container.idle_calls == scheduled, "同一轮只排一次判定"
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 928, "不能超过标签自己分到的宽度"

    # 窗口拉宽: 容器与标签一起变大。
    container.width = 1600
    label.width = 1240
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 1240

    # 容器比标签还窄时取容器那个(两者取小), 但不低于 minimum。
    container.width = 500
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 310, "取小值: 500 - 190 = 310"

    container.width = 300
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 240, "300 - 190 = 110 也要抬到 minimum 240"


def test_track_wraplength_starts_where_it_is_told_and_reports_real_changes() -> None:
    """落位宽度由调用点给; 换行**真的**变了才回调一次(高度按内容算的窗口靠它重收).

    设置窗口那类"高度按内容算"的窗口在建起来那一瞬间就要定高, 而可用宽度要等映射之后
    才量得准 —— 所以调用点给一个接近面板宽度的落位(这里 240), 量准之后才换成真值;
    落位与下限(120)差得远时, 少了这一步窗口就会先长一下再弹回去。
    """
    container = _FakeContainer()
    container.width = 428
    label = _FakeCtkWidget()
    changes: list[int] = []
    widgets.track_wraplength(
        container,
        label,
        inset=32,
        minimum=120,
        initial=240,
        on_change=lambda: changes.append(len(changes)),
    )

    assert label.kwargs["wraplength"] == 240, "还没量之前按调用点给的落位排"
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 396, "量准之后按 428 - 32 排"
    assert len(changes) == 1, "换行变了要回调一次"

    # 同一宽度再量一次不许再响: 否则"改文本 → 新 Configure → 再改"会互相追。
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert len(changes) == 1, "宽度没变就不许回调"

    # 比下限还窄的一格: 夹到 minimum(仍然算一次真变化)。
    container.width = 130
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 120
    assert len(changes) == 2

    # 落位不许低于下限(下限是"再窄也不能比它更窄"的兜底)。
    squeezed = _FakeCtkWidget()
    widgets.track_wraplength(container, squeezed, minimum=120, initial=40)
    assert squeezed.kwargs["wraplength"] == 120


def test_track_wraplength_follows_a_narrowed_cell_without_writing_in_a_loop() -> None:
    """同一格里别的控件变宽把说明挤窄时容器一个事件都不来 —— 标签自己要能补上.

    实测(设置窗口): 把右列的下拉框撑宽 40px, 面板宽度仍是 448(容器静默), 而说明那一格
    从 244 掉到 204 —— 那时候只有标签自己会发 ``<Configure>``。判定读的是**实得宽度**,
    所以这里能量到 204; 而换行改了高度之后再发一次事件时宽度没变, 不许再写(否则就是
    来回写的回路, 见过两千多次写把界面抽住)。
    """
    container = _FakeContainer()
    container.width = 448
    label = _FakeCtkWidget()
    label.width = 244
    # 下限必须显式给 120: 默认的 240 会把被挤窄的那一格抬回 240, 正是这一处要挡的坑。
    widgets.track_wraplength(container, label, inset=32, minimum=120, initial=240)

    assert set(label.handlers) == {"<Configure>"}, "标签那一侧也要能触发判定"
    container.handlers["<Configure>"](SimpleNamespace(width=1))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 244

    # 右列控件变宽 → 这一格被挤窄; 容器尺寸没变, 一个事件都没有。
    label.width = 204
    label.handlers["<Configure>"](SimpleNamespace(width=204))
    container.idle_callbacks[-1]()
    assert label.kwargs["wraplength"] == 204, "说明要跟着被挤窄的那一格重排"

    # 换行改了高度 → 标签会再发事件(事件里的宽度还不是这里要看的那个): 不许再写。
    writes = len(label.configure_calls)
    for _ in range(3):
        label.handlers["<Configure>"](SimpleNamespace(width=204))
        container.idle_callbacks[-1]()
    assert len(label.configure_calls) == writes, "宽度没变就不许再写"


def test_paint_button_disabled_dims_background_text_and_border() -> None:
    """禁用态必须与可用态明显不同: 底色/文字/描边一起压暗(评审时定的)."""
    button = _FakeCtkWidget()
    widgets.paint_button_disabled(button, DARK)

    assert button.kwargs["fg_color"] == DARK.disabled_bg
    assert button.kwargs["text_color"] == DARK.text_disabled
    assert button.kwargs["border_color"] == DARK.disabled_border
    # CustomTkinter 在禁用时**只**读 text_color_disabled: 它必须一起写.
    assert button.kwargs["text_color_disabled"] == DARK.text_disabled
    assert button.kwargs["fg_color"] != DARK.raised

    widgets.paint_button_enabled(
        button,
        DARK,
        fg_color=DARK.raised,
        text_color=DARK.text_body,
        hover_color=DARK.item_hover,
        border_color=DARK.border,
    )
    assert button.kwargs["fg_color"] == DARK.raised
    assert button.kwargs["text_color"] == DARK.text_body
    assert button.kwargs["border_width"] == 1


def test_card_surface_colors_put_selection_before_hover() -> None:
    """卡片/行的配色规则: 选中 > 悬停 > 常规, 而且**悬停不改选中的样子**.

    这是"我选中了谁"与"哪里能点"两件事的落点: 选中用浅底 + 描边, 悬停只提底色;
    鼠标划过已选中的卡片时必须还是选中的样子, 否则用户会以为自己刚才选错了。
    """
    assert widgets.card_surface_colors(DARK, selected=True, hovered=False) == (
        DARK.accent_soft,
        DARK.accent_soft_border,
    )
    assert widgets.card_surface_colors(DARK, selected=True, hovered=True) == (
        DARK.accent_soft,
        DARK.accent_soft_border,
    ), "悬停不能盖掉选中"
    assert widgets.card_surface_colors(DARK, selected=False, hovered=True) == (
        DARK.card_hover,
        DARK.card_border,
    )
    assert widgets.card_surface_colors(DARK, selected=False, hovered=False) == (
        DARK.card,
        DARK.card_border,
    )
    # 两套主题都要成立: 规则不是"深色主题下碰巧看起来对"。
    assert widgets.card_surface_colors(LIGHT, selected=False, hovered=True) == (
        LIGHT.card_hover,
        LIGHT.card_border,
    )
    assert widgets.card_surface_colors(LIGHT, selected=True, hovered=True) == (
        LIGHT.accent_soft,
        LIGHT.accent_soft_border,
    )


# --------------------------------------------------- 退路: 控件已销毁、空文案与未登记的按钮


class _DeadContainer(_FakeContainer):
    """已经销毁的假容器: 连排队都做不到(``after_idle`` 报 TclError)."""

    def after_idle(self, func: Any) -> None:
        """真控件销毁后 ``after_idle`` 就是这个反应."""
        raise tk.TclError("bad window path name")


class _FakeTipHost(_FakeCtkWidget):
    """挂悬停提示的假控件: 记下排过/撤过的计时器(真 Tk 才有事件循环)."""

    def __init__(self) -> None:
        super().__init__()
        self.queued: list[Any] = []
        self.cancelled: list[str] = []

    def after(self, _delay: int, callback: Any) -> str:
        """记下排队的弹出任务."""
        self.queued.append(callback)
        return f"job{len(self.queued)}"

    def after_cancel(self, job: str) -> None:
        """记下被撤掉的任务."""
        self.cancelled.append(job)


def test_a_button_built_under_a_palette_remembers_it(
    kit: widgets.UiKit, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配色已经定下来时, 新建的按钮顺手把"这套色"记在自己身上(焦点环要用)."""
    remembered: list[Any] = []
    monkeypatch.setattr(
        widgets, "remember_palette", lambda widget, palette: remembered.append(widget)
    )
    kit.apply(DARK)

    button = kit.button(kit, "动作", style="accent")

    assert remembered == [button]


def test_repainting_an_unregistered_button_is_ignored(kit: widgets.UiKit) -> None:
    """没登记过的按钮不属于这套配色: 重画时直接忽略(不猜它该是什么样子)."""
    stranger = _FakeCtkWidget()

    kit.repaint_button(cast(Any, stranger), DARK)

    assert stranger.configure_calls == []


def test_the_scrollbar_visibility_fix_is_applied_only_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补丁是进程级的: 第二次调用什么都不做(否则会把包装器套第二层).

    模块导入时就调过一次, 所以这里先把标志复位 —— 并在用例结束时把 ``_draw`` 也还原,
    免得它把后面用例里的滚动条行为一起改了。
    """
    monkeypatch.setattr(widgets, "_APPLIED_SCROLLBAR_FIX", False)
    monkeypatch.setattr(ctk.CTkScrollbar, "_draw", ctk.CTkScrollbar._draw)

    assert widgets.apply_scrollbar_visibility_fix() is True
    assert widgets.apply_scrollbar_visibility_fix() is False


def test_auto_scrollbar_gives_up_when_the_container_cannot_queue() -> None:
    """容器已销毁时不再排队判定(销毁过程也会发 ``<Configure>``), 也不把 TclError 抛出去."""
    frame = _DeadContainer()

    widgets.auto_scrollbar(frame)
    frame.handlers["<Configure>"](SimpleNamespace())  # 再来一次事件也不该炸


def test_track_wraplength_gives_up_when_the_container_cannot_queue() -> None:
    """换行判定同样要兜住"容器已销毁": 销毁期间的事件不能把它变成异常."""
    container = _DeadContainer()
    label = _FakeCtkWidget()

    widgets.track_wraplength(container, label, inset=20)
    container.handlers["<Configure>"](SimpleNamespace())


def test_track_fit_gives_up_when_the_label_is_dead(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """标签已销毁时重裁直接作废(销毁过程也会发 ``<Configure>``), 不往外抛 TclError."""
    container = _FakeContainer()
    container.width = 400
    label = _FakeCtkWidget()
    monkeypatch.setattr(widgets, "measured_font", lambda *_args: _FakeFont(60, 14))

    def boom(**_kwargs: Any) -> None:
        raise tk.TclError("bad window path name")

    monkeypatch.setattr(label, "configure", boom)

    widgets.track_fit(container, cast(Any, label), ("很长的一段说明文字",))
    container.handlers["<Configure>"](SimpleNamespace())


def test_tooltip_text_gives_up_when_the_provider_raises() -> None:
    """文案是函数(随选中项变)而它自己炸了时给空串: 缺一句提示不该打断渲染."""

    def boom() -> str:
        raise RuntimeError("文案还没准备好")

    widget = SimpleNamespace(**{widgets._TOOLTIP_ATTR: boom})

    assert widgets.tooltip_text(cast(Any, widget)) == ""


def test_a_queued_tooltip_is_cancelled_when_the_pointer_leaves() -> None:
    """移出时必须撤掉排队的那次弹出 —— 否则鼠标早就走了, 提示才慢悠悠地冒出来."""
    widget = _FakeTipHost()
    widgets.attach_tooltip(cast(Any, widget), "备份目录名")

    widget.handlers["<Enter>"](SimpleNamespace())
    widget.handlers["<Leave>"](SimpleNamespace())

    assert widget.cancelled == ["job1"]


def test_an_empty_tooltip_text_shows_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """文案为空时不弹空框(文案是到点那一刻才读的, 可能正好被换空了)."""
    opened: list[str] = []

    def remember(_widget: Any, text: str) -> Any:
        """记下"该弹出的那句文案", 不去碰真窗口."""
        opened.append(text)
        return object()

    monkeypatch.setattr(widgets, "_open_tooltip", remember)
    empty = _FakeTipHost()
    widgets.attach_tooltip(cast(Any, empty), "")
    named = _FakeTipHost()
    widgets.attach_tooltip(cast(Any, named), "备份目录名")

    empty.handlers["<Enter>"](SimpleNamespace())
    empty.queued[-1]()
    named.handlers["<Enter>"](SimpleNamespace())
    named.queued[-1]()

    assert opened == ["备份目录名"], "空文案不该弹出空框, 有文案的照弹"


class _DeadTipWindow:
    """提示窗口替身: 一被问尺寸就报"窗口已经没了"(TclError)."""

    def __init__(self) -> None:
        self.destroyed = 0

    def update_idletasks(self) -> None:
        """ничего."""

    def winfo_reqwidth(self) -> int:
        """已销毁的窗口就是这个反应."""
        raise tk.TclError("bad window path name")

    def destroy(self) -> None:
        """记一次销毁."""
        self.destroyed += 1


def test_hover_tip_ignores_empty_text_and_survives_a_dead_window() -> None:
    """空文案 = 收起; 量尺寸时窗口已经没了则收摊(而不是把 TclError 抛给调用方)."""
    tip = widgets.HoverTip(
        cast(Any, _FakeAnchor(x=0, y=0, height=10, screen_height=800))
    )
    tip.show("", root_x=0, root_y=0)
    assert tip.visible is False

    window = _DeadTipWindow()
    tip._window = cast(Any, window)
    tip._label = None  # 标签也没了 → 跳过"只换文案"那一支

    tip.show("提示", root_x=10, root_y=10)

    assert window.destroyed == 1
    assert tip.visible is False
    assert tip.text == ""
