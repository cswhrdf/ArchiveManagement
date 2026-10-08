"""
设置窗口与配置: 主题/快捷键记录、语言切换重建、配置修复、应用失败回滚。拆自 test_gui_buttons.py(见 docs/test-refactor-plan.md S7)。
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from gui_support import gui_app

try:
    import tkinter  # noqa: F401 - 校验 tkinter 可导入
    from tkinter import TclError

except Exception as exc:  # pragma: no cover - 取决于运行环境
    pytest.skip(f"GUI 依赖不可用: {exc}", allow_module_level=True)

from archive_management.i18n import current_locale, set_locale, tr
from archive_management.infrastructure.paths import ApplicationPaths
from archive_management.services.hotkeys import (
    ACTION_CREATE_BRANCH,
    ACTION_SAVE_NOW,
    DEFAULT_BRANCH_ACCELERATOR,
    DEFAULT_SAVE_ACCELERATOR,
    GlobalHotkeyService,
    format_accelerator,
)
from archive_management.ui.main_window import ArchiveApp
from archive_management.ui.widgets import scaled_px, scrollbar_needed
from button_support import (
    _drain,
    _label_texts,
    _new_app,
    _patch_dialogs,
    _pump,
    _RecordingBackend,
    _wait_for,
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


def test_settings_changes_without_a_config_path_stay_in_memory() -> None:
    """没有配置路径时(测试/未初始化场景): 各设置写回口都只存内存, 不写文件也不报错."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    assert app._paths is None

    app._save_language("en")
    app._save_debug(True)
    app._save_activation(True)
    app._save_font_size(20)
    app._save_shortcuts()

    # 没有任何配置路径: 写回口只改内存态, 不写文件也不报错。
    assert app._base_font_px >= 11


class _KeyEvent:
    """最简键盘事件替身(录制流程只用到 ``keysym``)."""

    def __init__(self, keysym: str) -> None:
        self.keysym = keysym


def test_settings_shortcut_recording_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """快捷键录制的状态机: 开始/取消/切换动作/无效键/合法组合/未录制时收尾."""
    from archive_management.services.hotkeys import combo_from_pressed, tk_token
    from archive_management.ui.demo_backend import DemoArchiveService

    # 自检: 下面的 keysym 与 token 映射变了的活, 这条用例要立刻失败而不是静默走过。
    keysyms = ("Super_L", "s")
    tokens = [tk_token(name) for name in keysyms]
    assert None not in tokens, (
        f"keysym 映射变了: {list(zip(keysyms, tokens, strict=True))}"
    )
    assert combo_from_pressed([token for token in tokens if token]) is not None, (
        "Win + 字母 应当是一个合法组合"
    )

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    applied: list[tuple[str, str]] = []
    window = _settings_window(app, applied)

    # ① 没在录制时按/放键: 直接放行。
    assert window._on_key_press(_KeyEvent("s")) == "break"
    assert window._on_key_release(_KeyEvent("s")) == "break"
    # ② 没在录制时收尾: 什么都不做。
    window._finish_capture()

    # ③ 点一下开始录制, 再点一下取消(保留原值, 不调用应用回调)。
    window._toggle_capture(ACTION_SAVE_NOW)
    assert window._capturing == ACTION_SAVE_NOW
    assert window.shortcut_text(ACTION_SAVE_NOW) == tr("settings.recording")
    window._toggle_capture(ACTION_SAVE_NOW)
    assert window._capturing is None
    assert applied == []

    # ④ 录制中切到另一行动作: 上一行先收尾(取消), 新的一行进入录制。
    window._toggle_capture(ACTION_SAVE_NOW)
    window._toggle_capture(ACTION_CREATE_BRANCH)
    assert window._capturing == ACTION_CREATE_BRANCH
    assert applied == []

    # ⑤ 按一个不在白名单里的键: 只提示, 不参与组合。
    window._on_key_press(_KeyEvent("F13"))
    assert window._error != ""
    # ⑥ 同一个键按两次: 第二次不重复累计(松开全部键后才安排收尾)。
    for name in ("Super_L", "s", "s"):
        window._on_key_press(_KeyEvent(name))
    window._on_key_release(_KeyEvent("s"))
    assert window._held, "还有键按着, 不该安排收尾"
    window._on_key_release(_KeyEvent("Super_L"))
    assert window._pending_finish is not None, "全部松开后应当安排延迟收尾"
    window._finish_when_idle()

    # ⑦ 合法组合被应用: 记录新组合、清掉错误提示。
    assert len(applied) == 1
    action, accelerator = applied[0]
    assert action == ACTION_CREATE_BRANCH
    assert accelerator == window._shortcuts[ACTION_CREATE_BRANCH]
    assert window._error == ""

    # ⑧ 只按字母(没有辅助键): 组合非法, 保留原值并给出原因。
    before = dict(window._shortcuts)
    window._toggle_capture(ACTION_SAVE_NOW)
    window._on_key_press(_KeyEvent("s"))
    window._on_key_release(_KeyEvent("s"))
    window._finish_when_idle()
    assert window._error != ""
    assert window._shortcuts == before
    assert len(applied) == 1, "非法组合不该调用应用回调"

    # ⑨ 录制中关闭窗口: 先收尾再销毁, 不留悬挂的计时器。
    window._toggle_capture(ACTION_SAVE_NOW)
    window.close()
    assert window._capturing is None


def test_hotkey_is_ignored_while_the_game_is_disabled(
    monkeypatch: pytest.MonkeyPatch, audit_log: list[str]
) -> None:
    """停用游戏的快捷键不触发备份, 只给出反馈(只有启用的那一款响应快捷键)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    app.backend.set_game_enabled("shanhai", False)
    app._select_game("shanhai")
    before = len(app.backend.list_backups("shanhai"))

    # "save_now" 即 services.hotkeys.ACTION_SAVE_NOW, 这里按主窗口收到的消息投递.
    app._messages.put(("hotkey", "save_now"))
    app._poll_messages()

    assert len(app.backend.list_backups("shanhai")) == before
    assert "hotkey.skipped" in " ".join(audit_log)


def test_settings_window_language_switch_closes_the_window() -> None:
    """设置窗口里换语言: 交给主窗口处理后关掉自己(主窗口会整体重建)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    switched: list[str] = []

    def apply(locale: str) -> str | None:
        switched.append(locale)
        return None

    window._on_apply_language = apply
    window._on_language_selected("English")
    _pump(app)

    assert switched == ["en"]
    assert window._window.winfo_exists() == 0


def test_settings_window_toggles_debug_logging() -> None:
    """设置窗口里拨调试开关: 交给主窗口应用, 并就地更新状态说明."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    applied: list[bool] = []

    def apply(enabled: bool) -> str | None:
        applied.append(enabled)
        return None

    window._on_apply_debug = apply
    window._debug_switch.select()
    window._on_debug_toggled()
    _pump(app)

    assert applied == [True]
    assert window._debug is True
    assert window._debug_label.cget("text") == tr(
        "settings.debug_state", state=tr("settings.debug_on")
    )


def test_settings_window_reverts_the_switch_when_applying_fails() -> None:
    """应用失败时说明原因, 并把开关拨回实际生效的状态(不能看着像拨成了)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    assert window._debug is False

    window._on_apply_debug = lambda enabled: "写配置失败"
    window._debug_switch.select()
    window._on_debug_toggled()
    _pump(app)

    assert window._debug is False
    assert bool(window._debug_switch.get()) is False
    assert "写配置失败" in window._debug_label.cget("text")


def test_settings_window_toggles_auto_activation() -> None:
    """设置窗口里拨自动启停开关: 交给主窗口应用, 并就地更新状态说明."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    assert window._activation is False

    window._activation_switch.select()
    window._on_activation_toggled()
    _pump(app)

    assert app._activation is True
    assert window._activation is True
    assert window._activation_label.cget("text") == tr(
        "settings.activation_state", state=tr("settings.activation_on")
    )


def test_settings_window_reverts_the_activation_switch_when_applying_fails() -> None:
    """应用失败时把自动启停开关拨回实际生效的状态, 并说明原因."""
    from archive_management.ui.demo_backend import DemoArchiveService

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])

    window._on_apply_activation = lambda enabled: "写配置失败"
    window._activation_switch.select()
    window._on_activation_toggled()
    _pump(app)

    assert window._activation is False
    assert bool(window._activation_switch.get()) is False
    assert "写配置失败" in window._activation_label.cget("text")


def test_auto_activation_polls_only_when_switched_on(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """自动启停只在开关打开时轮询: 关着时一次进程表都不枚举, 打开后后台轮询启动.

    用真实的 SQL 后端(注入可数的假进程表), 因此覆盖"界面定时器 → 后台线程 →
    服务层判断"这整条接线, 而不是只测策略本身。
    """
    from archive_management.domain import Game, SaveLocation
    from archive_management.infrastructure.database import Database
    from archive_management.infrastructure.repository import (
        GameRepository,
        SaveLocationRepository,
    )
    from archive_management.ui.sql_backend import SqlArchiveService

    _patch_dialogs(monkeypatch)
    calls: list[str] = []

    def provider() -> list[str]:
        calls.append("probe")
        return []

    db = Database(tmp_path / "activation.db")
    db.migrate()
    game = GameRepository(db).add(Game(name="Demo", enabled=True))
    assert game.id is not None
    # 没有存档位置的游戏不参与监控, 这里必须给上一条路径。
    SaveLocationRepository(db).add(SaveLocation(game_id=game.id, path="C:/Saves/Demo"))
    service = SqlArchiveService(
        db, backup_root=tmp_path / "backups", process_provider=provider
    )
    try:
        app = _new_app(service)
    except TclError as exc:
        pytest.skip(f"tk 环境不可用: {exc}")
    try:
        _pump(app)
        assert app._activation is False
        # 关着的时候多跑几轮定时器: 一次都不该探测. 开关打开后立刻轮询一次
        # (不用等满一个间隔), 否则"刚打开却没反应"看起来就像坏了.
        for _index in range(6):
            app._poll_messages()
            _pump(app)
            time.sleep(0.02)
        assert calls == []

        app._on_activation_change(True)
        assert _wait_for(app, lambda: bool(calls), seconds=3.0)
        assert _wait_for(app, lambda: not app._activation_busy, seconds=3.0)
    finally:
        app.destroy()


def test_startup_fills_localized_names_once() -> None:
    """启动时补一次译名探测, 且不是强制刷新(走缓存, 只对缺条目的游戏联网)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    class _RecordingNames(DemoArchiveService):
        """记录译名预取调用(演示后端自身不联网)."""

        def __init__(self) -> None:
            super().__init__(delay=0)
            self.calls: list[bool] = []

        def prefetch_names(self, *, refresh: bool = False) -> None:
            """记下每次调用是否要求强制刷新."""
            self.calls.append(refresh)

    service = _RecordingNames()
    app = gui_app(_new_app, service)
    _pump(app)

    assert service.calls == [False]

    # 切换语言同样不忽略缓存(译名按语言分条, 新语言缺条目的才联网).
    assert app._on_language_change("en") is None
    _pump(app)

    assert service.calls == [False, False]


def test_switching_language_rebuilds_the_ui_and_reprobes_names(
    tmp_path: Path,
) -> None:
    """切换语言: 界面按新语言整体重建, 译名重新探测, 选择写回配置文件."""
    from archive_management.config import load_config
    from archive_management.i18n import current_locale
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    app = gui_app(_new_app, DemoArchiveService(delay=0), paths=paths)
    _pump(app)
    old_page = app._home_page
    assert app._language == "zh-CN"

    error = app._on_language_change("en")
    _pump(app)

    assert error is None
    assert current_locale() == "en"
    assert app._language == "en"
    # 文案是构建时取的: 换语言必须整体重建, 主页对象会换成新的.
    assert app._home_page is not old_page
    assert tr("settings.language") == "Language"
    assert app._feedback_label.cget("text") == tr(
        "settings.language_switched", language="English"
    )
    # 选择写回配置文件: 重启后仍是英文.
    assert load_config(paths.config_path).language == "en"


def _settings_window(app: ArchiveApp, applied: list[tuple[str, str]]) -> Any:
    """直接构造设置窗口(不走主窗口的窗口复用逻辑), 便于测试录制交互."""
    from archive_management.ui.settings_window import SettingsWindow

    def apply(action: str, accelerator: str) -> str | None:
        applied.append((action, accelerator))
        return None

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
        on_apply_verification=app._on_verification_change,
        on_apply_debug=app._on_debug_change,
        on_apply_activation=app._on_activation_change,
        on_apply_remember_window=lambda _enabled: None,
        on_apply_shortcut=apply,
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )


def _press(window: Any, keysym: str, keycode: int) -> None:
    """模拟按下并松开一个键(录制只依赖 keysym/keycode)."""
    window._on_key_press(SimpleNamespace(keysym=keysym, keycode=keycode))
    window._on_key_release(SimpleNamespace(keysym=keysym, keycode=keycode))


def test_settings_window_holds_theme_and_hotkey_shortcuts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置窗口提供主题切换与两个快捷键, 且不再展示定时任务配置."""

    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    applied: list[tuple[str, str]] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    before = app._theme
    window = _settings_window(app, applied)

    # 定时任务相关的信息不在设置里.
    labels = _label_texts(window._container)
    assert all("定时" not in text for text in labels)
    assert any("外观" in text for text in labels)
    # 两个快捷键按钮显示当前组合(可读写法), 而不是 pynput 的原始语法.
    assert window.shortcut_text(ACTION_SAVE_NOW) == format_accelerator(
        DEFAULT_SAVE_ACCELERATOR
    )
    assert window.shortcut_text(ACTION_CREATE_BRANCH) == format_accelerator(
        DEFAULT_BRANCH_ACCELERATOR
    )

    # 点按钮即切换主题, 按钮文案与当前主题标签同步更新.
    window._toggle_btn.invoke()
    _pump(app)
    assert app._theme != before
    assert app.backend.current_theme() == app._theme
    assert window._toggle_text() in {
        tr("theme.to_light"),
        tr("theme.to_dark"),
    }
    assert tr(f"theme.name_{app._theme}") in window._theme_label.cget("text")

    # 界面字号/界面语言两个下拉框也要跟着换配色: 它们不在 UiKit 的重绘表里, 漏在
    # restyle 之外时同一个窗口里会留着上一套主题的底色(用户实测: 要重开窗口才恢复)。
    from archive_management.ui.palette import Palette

    palette = Palette.for_theme(app._theme)
    for box in (window._font_box, window._language_box):
        assert box.cget("fg_color") == palette.input_bg
        # 边界走**输入控件**那一份颜色: 面板/卡片那份 ``border`` 只有 1.28:1,
        # 用它是"边界看不见"的老毛病(见 palette 里 input_border 的取值说明)。
        assert box.cget("border_color") == palette.input_border
        assert box.cget("button_color") == palette.raised
        assert box.cget("text_color") == palette.text_body
        # 展开后的那层菜单同样要换: 只改外框时点开还是旧配色。
        menu = box._dropdown_menu
        assert menu.cget("fg_color") == palette.panel
        assert menu.cget("text_color") == palette.text_body


def test_settings_window_fits_its_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """设置窗口的高度按内容算: 底部说明与"关闭"按钮必须在窗口里, 说明也不能被裁.

    说明文字会随语言换行(实测中文 630px、英文 644px), 写死高度时"关闭"按钮会被推到
    窗口外面 —— 用户看不到也点不到; 界面语言的说明还会被右侧下拉框挤掉一截。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    # 窗口要先真的被映射/布局, 下面的尺寸才有意义(winfo_width 在未布局时是 1).
    assert _wait_for(app, lambda: window._note_label.winfo_width() > 1), "窗口未布局"

    frame = window._window
    frame.update_idletasks()
    bottom = frame.winfo_rooty() + frame.winfo_height()
    for name, widget in (
        ("说明", window._note_label),
        ("关闭按钮", window._close_btn),
    ):
        edge = widget.winfo_rooty() + widget.winfo_height()
        hint = f"{name}被推出窗口: {edge} > {bottom}"
        assert edge <= bottom, hint
    for name, label in (
        ("外观说明", window._appearance_hint),
        ("界面语言说明", window._language_hint),
        ("快捷键说明", window._shortcut_hint),
    ):
        cut = label.winfo_reqwidth() - label.winfo_width()
        hint = f"{name}被裁掉 {cut}px(换行宽度超过了可用宽度)"
        assert cut <= 1, hint


def test_settings_window_hints_follow_a_narrow_column(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """说明的换行宽度要跟着**它自己那一格**走: 一格只有 228px 时不能还按 240 排.

    CI 的 macOS runner 上说明那一格比 Windows 窄 16px(控件度量不同), 那时写死的
    ``wraplength=240`` 会让 Tk 按 240 排成一行, 再在标签边界处硬裁掉右边 12px: 既不换行
    也没有省略号, 后半句直接看不到(2026-10-01 的 :func:`test_settings_window_fits_its_content`
    报的就是"界面语言说明被裁掉 12px")。本机桌面宽, 原样永远量不出那一格, 所以这里把右列
    两个下拉框各撑宽 16px 来复现同一个窄列 —— 量的是"说明有没有跟着这一格换行", 不是控件
    宽度本身。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, [])
    assert _wait_for(app, lambda: window._note_label.winfo_width() > 1), "窗口未布局"

    # 右列变宽 = 左边那一格被挤窄(macOS 上由控件度量造成, 这里手动复现)。
    window._language_box.configure(width=156)
    window._font_box.configure(width=156)

    narrow = (
        ("外观说明", window._appearance_hint),
        ("界面语言说明", window._language_hint),
        ("日志说明", window._logging_hint),
        ("自动启停说明", window._activation_hint),
    )
    wide = (
        ("界面语言当前值", window._language_label),
        ("调试状态", window._debug_label),
        ("启停状态", window._activation_label),
        ("快捷键说明", window._shortcut_hint),
        ("页脚说明", window._note_label),
    )
    # 界面语言的说明文案在 240 宽度下正好要 240px —— 报错里那 12px 就是从这里来的,
    # 所以它这一格一窄过 240 就是"按 240 排、右边被裁"的现场。
    language = window._language_hint
    # 240 是**设计**值(逻辑像素), 而 ``winfo_width`` 是物理像素: 125% 的屏上窄列
    # 实测 285(=228 逻辑), 拿逻辑值直接比就是假红。
    narrow_limit = scaled_px(language, 240)
    assert _wait_for(app, lambda: 1 < language.winfo_width() < narrow_limit), (
        f"界面语言说明这一格没被挤窄: {language.winfo_width()}"
    )
    # 窄列已经成立, 现在等说明按这一格重排完(延后到 idle 才量宽)。
    cut = language.winfo_reqwidth() - language.winfo_width()
    assert _wait_for(
        app, lambda: language.winfo_reqwidth() <= language.winfo_width()
    ), (
        f"说明的换行宽度没跟着被挤窄的那一格收窄: 这一格 {language.winfo_width()}px, "
        f"说明要 {language.winfo_reqwidth()}px(裁掉 {cut}px)"
    )
    for name, label in narrow + wide:
        cut = label.winfo_reqwidth() - label.winfo_width()
        assert cut <= 1, f"{name}被裁掉 {cut}px(换行宽度没跟着这一格走)"


def test_settings_window_scrollbar_only_when_the_content_overflows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """屏幕够高(窗口按内容定高)时不该立着一条拖不动的滚动条(评审时定的).

    窗口没映射时 ``winfo_ismapped()`` 恒为 0(假绿), 因此先等到窗口真的在屏幕上,
    再断言滚动条与“内容是否溢出”一致。
    """
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    # 把屏幕报得很高: 窗口高度会等于内容高度, 滚动区刚好装下.
    monkeypatch.setattr(app, "winfo_screenheight", lambda: 4000)
    window = _settings_window(app, [])
    assert _wait_for(app, lambda: window._window.winfo_ismapped()), "窗口未映射"

    canvas = window._body._parent_canvas
    region = canvas.bbox("all")
    content = 0 if region is None else int(region[3]) - int(region[1])
    assert scrollbar_needed(content, int(canvas.winfo_height())) is False
    assert _wait_for(app, lambda: not window._body._scrollbar.winfo_ismapped()), (
        "内容装得下却仍立着滚动条"
    )


def test_settings_window_scrollbar_appears_when_the_content_does_not_fit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """屏幕不够高时窗口被夹住, 内容必须能滚动(不能把底部说明与"关闭"推出去)."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    monkeypatch.setattr(app, "winfo_screenheight", lambda: 520)
    window = _settings_window(app, [])
    assert _wait_for(app, lambda: window._window.winfo_ismapped()), "窗口未映射"

    canvas = window._body._parent_canvas
    region = canvas.bbox("all")
    content = 0 if region is None else int(region[3]) - int(region[1])
    assert scrollbar_needed(content, int(canvas.winfo_height())) is True
    assert _wait_for(app, lambda: window._body._scrollbar.winfo_ismapped()), (
        "内容溢出却没有滚动条"
    )
    frame = window._window
    frame.update_idletasks()
    bottom = frame.winfo_rooty() + frame.winfo_height()
    edge = window._close_btn.winfo_rooty() + window._close_btn.winfo_height()
    assert edge <= bottom, f"关闭按钮被推出窗口: {edge} > {bottom}"


def test_settings_window_records_a_pressed_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """点击按键区域后按下组合键即录制: 组合键交给回调并立即显示在按钮上."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    applied: list[tuple[str, str]] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, applied)

    window._toggle_capture(ACTION_CREATE_BRANCH)
    assert window.shortcut_text(ACTION_CREATE_BRANCH) == tr("settings.recording")

    _press(window, "Control_L", 17)
    _press(window, "Win_L", 91)
    _press(window, "Z", 90)

    assert _wait_for(app, lambda: bool(applied))
    assert applied == [(ACTION_CREATE_BRANCH, "<win>+<ctrl>+z")]
    assert window.shortcut_text(ACTION_CREATE_BRANCH) == format_accelerator(
        "<win>+<ctrl>+z"
    )
    assert window._shortcut_error.cget("text") == ""


def test_settings_window_rejects_a_single_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """只按修饰键或只按字母都不算合法组合: 给出原因且不修改快捷键."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    applied: list[tuple[str, str]] = []
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = _settings_window(app, applied)

    window._toggle_capture(ACTION_SAVE_NOW)
    _press(window, "Win_L", 91)

    assert _wait_for(app, lambda: bool(window._shortcut_error.cget("text")))
    assert applied == []
    assert window._shortcut_error.cget("text") == tr("hotkey.err_no_letter")
    assert window.shortcut_text(ACTION_SAVE_NOW) == format_accelerator(
        DEFAULT_SAVE_ACCELERATOR
    )

    # 只按字母缺少修饰键, 同样不生效.
    window._toggle_capture(ACTION_SAVE_NOW)
    _press(window, "s", 83)

    assert _wait_for(app, lambda: bool(window._shortcut_error.cget("text")))
    assert applied == []
    assert window._shortcut_error.cget("text") == tr("hotkey.err_no_modifier")

    # 数字键不在白名单里: 直接提示"不支持的按键".
    window._toggle_capture(ACTION_SAVE_NOW)
    _press(window, "1", 49)

    assert _wait_for(
        app,
        lambda: window._shortcut_error.cget("text") == tr("hotkey.err_unknown_key"),
    )


def test_custom_hotkey_is_persisted_and_reloaded(tmp_path: Path) -> None:
    """自定义快捷键写入配置文件, 下次启动仍然生效."""
    from archive_management.config import load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    hotkeys = GlobalHotkeyService(backend=_RecordingBackend())
    app = gui_app(_new_app, DemoArchiveService(delay=0), hotkeys=hotkeys, paths=paths)
    try:
        _pump(app)
        assert app._apply_shortcut(ACTION_CREATE_BRANCH, "<win>+<ctrl>+z") is None
        assert app._shortcuts[ACTION_CREATE_BRANCH] == "<win>+<ctrl>+z"
        assert load_config(paths.config_path).hotkeys.branch == "<win>+<ctrl>+z"
    finally:
        app.destroy()

    reloaded = _new_app(
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
        paths=paths,
    )
    assert reloaded._shortcuts[ACTION_CREATE_BRANCH] == "<win>+<ctrl>+z"
    assert reloaded._shortcuts[ACTION_SAVE_NOW] == DEFAULT_SAVE_ACCELERATOR


def test_invalid_config_entries_are_repaired_on_startup(tmp_path: Path) -> None:
    """启动时只写坏一项: 剔除那一项并提示用户, 而不是把整份配置都冲掉."""
    from archive_management.config import load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    paths.config_path.write_text(
        '{"version": 1, "theme": "neon", "language": "en"}', encoding="utf-8"
    )
    before = current_locale()
    app = gui_app(
        _new_app,
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
        paths=paths,
    )
    assert app._shortcuts[ACTION_SAVE_NOW] == DEFAULT_SAVE_ACCELERATOR
    assert app._shortcuts[ACTION_CREATE_BRANCH] == DEFAULT_BRANCH_ACCELERATOR
    # 提示是按"读配置那一刻"的语言渲染的(之后才切到配置里的语言)
    set_locale(before)
    assert app._last_feedback[1] == tr("config.repaired", count=1, fields="theme")
    assert (paths.config_dir / "config.json.invalid").is_file()
    repaired = load_config(paths.config_path)
    assert repaired.theme == "system"
    assert repaired.language == "en"


def test_unreadable_config_is_reset_to_defaults_on_startup(tmp_path: Path) -> None:
    """整份文件都读不出来时: 还原为默认值并提示用户, 而不是静默回落."""
    from archive_management.config import AppConfig, load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    paths.config_path.write_text("{", encoding="utf-8")
    before = current_locale()
    app = gui_app(
        _new_app,
        DemoArchiveService(delay=0),
        hotkeys=GlobalHotkeyService(backend=_RecordingBackend()),
        paths=paths,
    )
    set_locale(before)
    assert app._last_feedback[1] == tr("config.reset")
    assert (paths.config_dir / "config.json.invalid").is_file()
    assert load_config(paths.config_path) == AppConfig()


def test_failed_hotkey_registration_keeps_the_previous_combination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """后端不可用时注册失败: 给出原因并保留原组合, 不让用户无声地失去快捷键."""
    from archive_management.ui.demo_backend import DemoArchiveService

    _patch_dialogs(monkeypatch)
    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    failure = app._apply_shortcut(ACTION_SAVE_NOW, "<win>+<ctrl>+q")

    assert failure is not None
    assert app._shortcuts[ACTION_SAVE_NOW] == DEFAULT_SAVE_ACCELERATOR


def test_settings_window_skips_unchanged_values_and_rolls_failures_back() -> None:
    """设置窗口: 值没变/选择无效就不动作, 写回失败要把控件拨回实际状态并说明原因."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.settings_window import SettingsWindow

    applied: list[tuple[str, object]] = []

    def last() -> tuple[str, object]:
        """最近一次写回(经函数返回, 避开 mypy 对元组索引做字面量收窄)."""
        assert applied
        return applied[-1]

    def fail_language(locale: str) -> str | None:
        applied.append(("language", locale))
        return "语言失败"

    def fail_font(size: int) -> str | None:
        applied.append(("font", size))
        return "字号失败"

    def fail_debug(wanted: bool) -> str | None:
        applied.append(("debug", wanted))
        return "调试失败"

    def fail_activation(wanted: bool) -> str | None:
        applied.append(("activation", wanted))
        return "启停失败"

    def fail_verification(mode: str) -> str | None:
        applied.append(("verification", mode))
        return "校验方式失败"

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = SettingsWindow(
        app,
        palette=app.p,
        theme=app._theme,
        language=app._language,
        base_font_px=16,
        verification_mode="sha256",
        verification_parallel=2,
        debug=True,
        activation=True,
        remember_window=True,
        shortcuts=app._shortcuts,
        on_toggle_theme=lambda: app._theme,
        on_apply_language=fail_language,
        on_apply_font_size=fail_font,
        on_apply_verification=fail_verification,
        on_apply_debug=fail_debug,
        on_apply_activation=fail_activation,
        on_apply_remember_window=lambda _enabled: None,
        on_apply_shortcut=lambda action, accelerator: None,
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )
    try:
        # ① 语言: 不认识的值与"当前语言"都不写回; 换一种语言则回调报错 → 显示原因.
        window._on_language_selected("不认识")
        window._on_language_selected(window._locale_label(app._language))
        other_locale = next(
            label
            for label, locale in window._locales.items()
            if locale != app._language
        )
        window._on_language_selected(other_locale)
        assert last() == ("language", window._locales[other_locale])
        assert tr("settings.language_failed", reason="语言失败") in _label_texts(
            window._container
        )

        # ② 字号: 不认识/与当前一致都不写回; 选新字号但回调报错 → 下拉拨回当前值.
        current_label = window._font_label_of(16)
        other_label = next(
            label for label in window._font_box.cget("values") if label != current_label
        )
        window._on_font_selected("不认识")
        window._on_font_selected(current_label)
        window._on_font_selected(other_label)
        kind, size = last()
        assert kind == "font"
        assert size != 16, "选中的确实是另一个字号"
        assert window._font_box.get() == current_label

        # ③ 调试开关: 开关初值与实际一致时不动; 关掉后回调报错 → 开关拨回"开".
        window._on_debug_toggled()
        window._debug_switch.deselect()
        window._on_debug_toggled()
        assert last() == ("debug", False)
        assert bool(window._debug_switch.get()) is True
        assert tr("settings.debug_failed", reason="调试失败") in _label_texts(
            window._container
        )

        # ④ 自动启停同一套.
        window._on_activation_toggled()
        window._activation_switch.deselect()
        window._on_activation_toggled()
        assert bool(window._activation_switch.get()) is True
        assert tr("settings.activation_failed", reason="启停失败") in _label_texts(
            window._container
        )

        # ⑤ 校验方式: 不认识的值与"当前方式"都不写回; 换一种但回调报错 → 拨回并说明.
        current = window._verification_label("sha256")
        window._on_verification_selected("不认识")
        window._on_verification_selected(current)
        window._on_verification_selected(window._verification_label("name"))
        assert last() == ("verification", "name")
        assert window._verification_box.get() == current
        assert tr(
            "settings.verification_failed", reason="校验方式失败"
        ) in _label_texts(window._container)

        # ⑥ 录制收尾的定时器可能比录制活得久: 没在录制时什么都不做.
        window._finish_when_idle()
        assert window._capturing is None
    finally:
        window.close()
        app.destroy()


def test_settings_writes_reach_the_config_file(tmp_path: Path) -> None:
    """有配置路径时四个写回口真的落盘(字号 / 调试日志 / 自动启停 / 校验方式)."""
    from archive_management.config import load_config
    from archive_management.ui.demo_backend import DemoArchiveService

    paths = ApplicationPaths.default(override_root=tmp_path).ensure()
    app = gui_app(_new_app, DemoArchiveService(delay=0), paths=paths)
    try:
        _pump(app)
        assert app._paths is not None

        assert app._on_font_size_change(18) is None
        assert load_config(paths.config_path).ui.base_font_px == 18

        assert app._on_debug_change(True) is None
        assert load_config(paths.config_path).logging.debug is True
        app._on_debug_change(False)
        assert load_config(paths.config_path).logging.debug is False

        app._on_activation_change(True)
        _drain(app)
        assert load_config(paths.config_path).activation.auto is True
        app._on_activation_change(False)
        _drain(app)
        assert load_config(paths.config_path).activation.auto is False

        # 校验方式写回配置文件: 下一次备份/还原现读一次配置就按新方式走.
        assert app._on_verification_change("name") is None
        assert load_config(paths.config_path).verification.mode == "name"
    finally:
        app.destroy()


def test_settings_window_keeps_a_successful_font_and_reports_a_failed_shortcut() -> (
    None
):
    """字号改成功就停在新值上(不回拨); 快捷键注册失败要把原因写进提示行."""
    from archive_management.ui.demo_backend import DemoArchiveService
    from archive_management.ui.settings_window import SettingsWindow

    app = gui_app(_new_app, DemoArchiveService(delay=0))
    _pump(app)
    window = SettingsWindow(
        app,
        palette=app.p,
        theme=app._theme,
        language=app._language,
        base_font_px=16,
        verification_mode="sha256",
        verification_parallel=2,
        debug=False,
        activation=False,
        remember_window=True,
        shortcuts=app._shortcuts,
        on_toggle_theme=lambda: app._theme,
        on_apply_language=lambda _locale: None,
        on_apply_font_size=lambda _size: None,
        on_apply_verification=lambda _mode: None,
        on_apply_debug=lambda _enabled: None,
        on_apply_activation=lambda _enabled: None,
        on_apply_remember_window=lambda _enabled: None,
        on_apply_shortcut=lambda _action, _accelerator: "快捷键被占用",
        on_capture_start=lambda: None,
        on_capture_end=lambda: None,
    )
    try:
        # ① 字号: 回调返回 None 表示成功, 下拉停在新值上。
        current = window._font_label_of(16)
        other = next(
            label for label in window._font_box.cget("values") if label != current
        )
        # 真实流程是"下拉框先选中新值, 再触发命令", 这里照做。
        window._font_box.set(other)
        window._on_font_selected(other)
        assert window._font_box.get() == other, "成功时不该把下拉拨回旧值"

        # ② 快捷键: 录到一个合法组合但注册失败 → 原因进提示行, 按钮仍是旧值。
        window._toggle_capture(ACTION_SAVE_NOW)
        _press(window, "Win_L", 91)
        _press(window, "Control_L", 17)
        _press(window, "s", 83)

        assert _wait_for(
            app, lambda: window._shortcut_error.cget("text") == "快捷键被占用"
        )
        assert window.shortcut_text(ACTION_SAVE_NOW) == format_accelerator(
            DEFAULT_SAVE_ACCELERATOR
        )

        # ③ 校验方式: 成功时下拉停在新值, 状态说明跟着改成新方式(不再显示旧值).
        name_label = window._verification_label("name")
        window._verification_box.set(name_label)
        window._on_verification_selected(name_label)

        assert window._verification_box.get() == name_label
        assert window._verification_mode == "name"
        assert name_label in window._verification_status.cget("text")
    finally:
        window.close()
        app.destroy()
