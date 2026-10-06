"""切主题后"有没有留下另一套调色板的颜色"的守卫.

12 号实测反馈: 设置窗口里「全局保存」「创建分支」这两行动作名在切主题后变成几乎
看不见的淡灰 —— 它们当初是**匿名创建**的标签(``ctk.CTkLabel(...).grid(...)``),
从来没进 :meth:`SettingsWindow.restyle` 的重绘表, 于是留着**上一套主题**的正文色:
浅色主题的深灰落在深色面板上, 对比度低到看不出文字。

判据只有一条, 但它对**任何**新控件都生效: 切主题之后, 窗口里每个控件的颜色都必须
来自**当前**这套调色板。做法是取当前调色板的全部颜色值当"合法集合", 逐个控件比对
``text_color`` / ``fg_color`` / ``border_color`` / ``progress_color``。
"""

from __future__ import annotations

import sys
from collections import deque
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from gui_support import gui_app

try:
    import customtkinter as ctk
except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.services.hotkeys import (
    GlobalHotkeyService,
    UnavailableBackend,
)
from archive_management.ui import contrast, main_window
from archive_management.ui.backend import ArchiveService
from archive_management.ui.demo_backend import DemoArchiveService
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.palette import DARK, LIGHT, Palette
from archive_management.ui.settings_window import SettingsWindow

pytestmark = [
    pytest.mark.integration,
    pytest.mark.ui,
    pytest.mark.normal,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("端到端界面流程"),
    pytest.mark.story("命名主题重绘"),
    pytest.mark.layer("e2e"),
]

# 允许出现的"特殊值": CTk 用它表示"跟父控件同色", 不是颜色。
TRANSPARENT = "transparent"

# 要量的颜色选项: 文字色与底色 —— 这两样是"看不见"的直接来源。
COLOR_OPTIONS = ("text_color", "fg_color", "border_color", "progress_color")
# 整机那一条只量文字色: 主窗口里有**故意与主题无关**的底色 —— 封面占位用的
# `_TONE_COLORS`(orange/blue/green 三档, 见 `main_window`), 它代表"这张卡片没有图",
# 本来就不该跟主题走。设置窗口那些底色全部出自调色板, 所以量它的那条四样都判。
TEXT_OPTIONS = ("text_color",)
# 封面/占位用的"与主题无关"色调(`main_window._TONE_COLORS`): 底色是这三个之一的
# 控件整块都不跟主题走 —— 占位字是写死的白字也一样。所以它们不进判据; 兜底断言
# "它们与两套调色板都不撞色", 否则这条豁免会掩盖真的陈旧色。
THEME_INDEPENDENT = frozenset(main_window._TONE_COLORS.values())

# 输入控件(边界要单独量的一类): 实测 CTkEntry / CTkComboBox 的默认 ``border_width``
# 是 **2**, 也就是"确实画着一条线"; CTkTextbox 我们自己写 1。``CTkOptionMenu`` 不支持
# 这两个选项(``cget`` 直接报 ``ValueError``), 本仓库不用它。
INPUT_TYPES = (ctk.CTkEntry, ctk.CTkComboBox, ctk.CTkTextbox)


def palette_values(palette: Palette) -> set[str]:
    """当前调色板里全部 ``#rrggbb`` 值(小写)."""
    values: set[str] = set()
    for name in dir(palette):
        if name.startswith("_"):
            continue
        value = getattr(palette, name)
        if isinstance(value, str) and contrast.is_hex(value):
            values.add(value.lower())
    return values


def shared_values(first: Palette, second: Palette) -> set[str]:
    """两套调色板共用的颜色值.

    非空说明这条判据"分不出新旧主题"—— 那时必须换判据(而不是让它假装通过)。
    """
    return palette_values(first) & palette_values(second)


def _ctk_widgets(root: Any) -> list[Any]:
    """子树里全部 CTk 控件(跳过 Tk 的内部 Canvas/Entry)."""
    found: list[Any] = []
    queue: deque[Any] = deque([root])
    while queue:
        widget = queue.popleft()
        if isinstance(widget, ctk.CTkBaseClass):
            found.append(widget)
        queue.extend(widget.winfo_children())
    return found


def _option(widget: Any, option: str) -> str | None:
    """读一个选项(控件不支持这个选项时给 ``None``)."""
    with suppress(Exception):
        return str(widget.cget(option))
    return None


def _label_of(widget: Any) -> str:
    """控件的可读名字(失败信息里要能一眼找到是谁)."""
    text = _option(widget, "text") or ""
    return f"{type(widget).__name__}({text!r})"


def stale_color_problems(
    root: Any, palette: Palette, *, options: tuple[str, ...] = COLOR_OPTIONS
) -> list[str]:
    """返回"颜色不属于当前调色板"的控件清单.

    只比对**具体颜色**(``#rrggbb``): 主题对(``['gray86', 'gray17']``)有它自己的判据
    (见 ``test_gui_styles._visible_pair_colors``)。
    """
    allowed = palette_values(palette)
    found: list[str] = []
    for widget in _ctk_widgets(root):
        if _option(widget, "fg_color") in THEME_INDEPENDENT:
            continue  # 封面占位色调: 它和被它反衬的文字整块与主题无关
        for option in options:
            value = _option(widget, option)
            if value is None or value == TRANSPARENT:
                continue
            if not contrast.is_hex(value):
                continue
            if value.lower() not in allowed:
                found.append(
                    f"{_label_of(widget)} 的 {option}={value} 不属于当前调色板"
                )
    return found


def input_boundary_problems(root: Any, palette: Palette) -> list[str]:
    """返回"输入控件的边界不是 ``input_border``"的控件清单.

    :func:`stale_color_problems` 只问"这个颜色属不属于当前调色板"—— ``border`` 与
    ``input_border`` 都是合法值, 所以它分不出"输入框用了面板那份浅灰边界"。
    """
    found: list[str] = []
    for widget in _ctk_widgets(root):
        if not isinstance(widget, INPUT_TYPES):
            continue
        value = _option(widget, "border_color")
        if value is None or value.lower() != palette.input_border.lower():
            found.append(f"{_label_of(widget)} 的 border_color={value}")
    return found


def _new_app(backend: ArchiveService) -> ArchiveApp:
    """构造主窗口: 不注册系统级快捷键, 避免遗留键盘钩子或误触发备份."""
    return ArchiveApp(
        backend,
        title="重绘测试",
        hotkeys=GlobalHotkeyService(backend=UnavailableBackend("重绘测试禁用")),
    )


def _settings_window(app: ArchiveApp) -> SettingsWindow:
    """直接构造设置窗口(它的重绘表最长, 最容易被漏)."""
    return SettingsWindow(
        app,
        palette=app.p,
        theme=app._theme,
        language=app._language,
        base_font_px=app._base_font_px,
        verification_mode=app._verification_mode,
        verification_parallel=app._verification_parallel,
        debug=app._debug,
        activation=app._activation,
        remember_window=app._remember_window,
        shortcuts=app._shortcuts,
        on_toggle_theme=app._on_toggle_theme,
        on_apply_language=app._on_language_change,
        on_apply_font_size=app._on_font_size_change,
        on_apply_verification=lambda _mode: None,
        on_apply_debug=app._on_debug_change,
        on_apply_activation=app._on_activation_change,
        on_apply_remember_window=lambda _enabled: None,
        on_apply_shortcut=lambda _action, _accelerator: None,
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )


def test_the_two_palettes_share_no_colour() -> None:
    """兜底: 两套调色板没有共用颜色, 否则"切主题后发现旧颜色"这条量不出东西."""
    assert shared_values(LIGHT, DARK) == set()


def test_the_theme_independent_colours_do_not_collide_with_the_palettes() -> None:
    """兜底: 被豁免的封面占位色调不许与调色板里的任何值相同, 否则豁免会掩盖旧色."""
    assert not THEME_INDEPENDENT & (palette_values(LIGHT) | palette_values(DARK))


def test_switching_theme_repaints_every_widget() -> None:
    """设置窗口切主题之后, 一个控件都不许留着上一套主题的颜色."""
    application = gui_app(_new_app, DemoArchiveService(delay=0))
    window: SettingsWindow | None = None
    try:
        for _ in range(3):
            application.update_idletasks()
            application.update()
        window = _settings_window(application)
        window._window.update_idletasks()
        # 夹具必须造出"必须触发"的条件: 两套调色板来回切, 每次都量一遍。
        for palette in (DARK, LIGHT, DARK):
            window.restyle(palette)
            window._window.update_idletasks()
            problems = stale_color_problems(window._window, palette)
            assert not problems, "切主题后这些控件还留着别的主题的颜色:\n" + "\n".join(
                problems
            )
        # 兜底: 窗口里真的要量到控件, 否则上面几条在空树上空转(实测 39 个)。
        assert len(_ctk_widgets(window._window)) >= 30
    finally:
        if window is not None:
            window.close()
        application.destroy()


def test_toggling_the_theme_repaints_the_whole_application() -> None:
    """按"切换主题"按钮之后(真实路径), 整个主窗口也不许留旧色.

    量的是真实动作(:meth:`ArchiveApp._on_toggle_theme`), 而不是直接调某个 ``restyle``:
    主页各页面是按调色板逐一定色的, 换主题后要靠 ``kit.apply`` 与
    ``_home_page.apply_palette`` 重绘 —— 漏掉谁会在这里现行。
    """
    application = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        for _ in range(3):
            application.update_idletasks()
            application.update()
        # 夹具必须造出"必须触发"的条件: 真的来回切一次主题, 每次都量一遍。
        for _ in range(2):
            application._on_toggle_theme()
            for _ in range(3):
                application.update_idletasks()
                application.update()
            problems = stale_color_problems(
                application, application.p, options=TEXT_OPTIONS
            )
            assert not problems, (
                "切主题后主窗口里这些控件还留着别的主题的颜色:\n" + "\n".join(problems)
            )
        # 兜底: 主窗口里真的要量到控件, 否则上面几条在空树上空转。
        assert len(_ctk_widgets(application)) >= 200
    finally:
        application.destroy()


def test_toggling_the_theme_keeps_the_input_boundary() -> None:
    """切主题后输入框的边界仍必须是 ``input_border``.

    上一条只问"颜色属不属于当前调色板" —— 它分不出"输入框用了面板那份浅灰边界"(浅色
    主题实测 1.20:1, 边界等于看不见, 而界面照样能跑)。这条把输入控件单独挑出来量: 主页
    的搜索框与筛选下拉是按当前调色板造的, 主题切换后重建/重绘都得把边界带对。
    """
    application = gui_app(_new_app, DemoArchiveService(delay=0))
    try:
        for _ in range(3):
            application.update_idletasks()
            application.update()
        # 夹具必须造出"必须触发"的条件: 来回切两次主题, 每次都量一遍。
        for _ in range(2):
            application._on_toggle_theme()
            for _ in range(3):
                application.update_idletasks()
                application.update()
            problems = input_boundary_problems(application, application.p)
            assert not problems, (
                "切主题后这些输入框的边界不是 input_border:\n" + "\n".join(problems)
            )
        # 兜底: 真的要量到输入控件, 否则上面在空树上空转(主页有搜索框 + 两个筛选下拉)。
        inputs = [w for w in _ctk_widgets(application) if isinstance(w, INPUT_TYPES)]
        assert len(inputs) >= 3, [type(w).__name__ for w in inputs]
    finally:
        application.destroy()
