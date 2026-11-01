"""应用图标: SVG 与位图的一致性、位图装配、以及界面接线(不建真窗口).

判据分三层:

* **资源层**(最重要): 入库的 ``app.ico`` / ``app-*.png`` 必须真的出自当前这份
  ``app.svg`` —— "改了 SVG 忘了重画"是这条流水线唯一容易犯的错, 指纹对不上就红;
* **装配层**: Pillow 装出来的 ICO 必须含 Windows 要的全部档位(ffmpeg 的 ``-f ico``
  只写一档, 所以这一步不能省);
* **接线层**: 界面缺资源时退回落样式而不是崩(用假 ctk 与假窗口驱动真实方法)。
"""

from __future__ import annotations

import importlib.util
import struct
import sys
import tkinter as tk
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from customtkinter.windows.ctk_tk import CTk
from customtkinter.windows.ctk_toplevel import CTkToplevel
from PIL import Image

import ci_workflow
from archive_management.services import icon_assets
from archive_management.ui import main_window as main_mod
from archive_management.ui import window_icon
from archive_management.ui.palette import Palette

pytestmark = [
    pytest.mark.package,
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("应用图标与打包资源"),
    pytest.mark.story("SVG 与位图的一致性"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> Any:
    """按路径加载 ``scripts/`` 下的脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    spec = importlib.util.spec_from_file_location(
        name, _REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- 资源层: 入库的位图必须是当前 SVG 的产物 -------------------------------------


def _ico_sizes(path: Path) -> set[tuple[int, ...]]:
    """ICO 里实际装了哪几档尺寸(``Image.ico`` 是插件接口, Pillow 的类型存根里没有它)."""
    with Image.open(path) as icon:
        return {tuple(size) for size in cast(Any, icon).ico.sizes()}


def test_committed_bitmaps_match_the_svg() -> None:
    """入库的位图就是 ``app.svg`` 的最新产物(不一致时把该跑的命令写进失败信息)."""
    state = icon_assets.asset_state()
    assert state.fresh, f"{state.detail} —— 运行 `uv run python scripts/build_icon.py`"


def test_the_ico_carries_every_size_windows_asks_for() -> None:
    """exe 那颗 ICO 里要有 16 到 256 的全部档位(少一档就是某个视图下的糊图)."""
    sizes = _ico_sizes(icon_assets.asset_path(icon_assets.ICO_NAME))
    assert sizes == {(size, size) for size in icon_assets.ICO_SIZES}


def _ico_entry_kinds(path: Path) -> set[str]:
    """ICO 目录里每一档是 BMP 还是 PNG(直接读头八字节, 不问 Pillow)."""
    raw = path.read_bytes()
    count = struct.unpack_from("<H", raw, 4)[0]
    kinds = set()
    for index in range(count):
        _w, _h, _c, _r, _p, _bpp, _size, offset = struct.unpack_from(
            "<BBBBHHII", raw, 6 + index * 16
        )
        head = raw[offset : offset + 8]
        kinds.add("PNG" if head == b"\x89PNG\r\n\x1a\n" else "BMP")
    return kinds


def test_the_window_ico_is_small_and_bitmap_encoded() -> None:
    """给 Tk 的那颗必须档少、BMP 编码 —— 实测喂七档 PNG 大图会把 Tk 弄崩.

    这条不是风格偏好: 2026-10-11 的 access violation 就是这么来的(见
    ``ui/window_icon`` 与 docs/development.md 的「应用图标」), 换回七档时它当场变红。
    """
    path = icon_assets.asset_path(icon_assets.WINDOW_ICO_NAME)
    assert _ico_sizes(path) == {(size, size) for size in icon_assets.WINDOW_ICO_SIZES}
    assert _ico_entry_kinds(path) == {"BMP"}, "Tk 的图标读写器吃的是 DIB, 别再回 PNG"
    assert max(icon_assets.WINDOW_ICO_SIZES) < max(icon_assets.ICO_SIZES)


def test_the_rendered_pngs_have_the_size_their_name_says() -> None:
    """``app-<n>.png`` 的名字就是它的边长(名字与内容不符比没有图更难查)."""
    for size, name in icon_assets.PNG_SIZES:
        with Image.open(icon_assets.asset_path(name)) as image:
            assert image.size == (size, size), name


# --- 装配层: 位图怎么来的 ---------------------------------------------------------


def test_asset_state_reports_every_way_the_bitmaps_can_be_wrong(tmp_path: Path) -> None:
    """四种状态各自给一句能看懂的话: 缺源文件 / 缺位图 / 指纹不符 / 一致."""
    assert not icon_assets.asset_state(tmp_path).fresh

    (tmp_path / icon_assets.SVG_NAME).write_text("<svg/>", encoding="utf-8")
    missing = icon_assets.asset_state(tmp_path)
    assert missing.fresh is False
    assert "缺少位图" in missing.detail

    for path in icon_assets.required_assets(tmp_path):
        path.write_bytes(b"x")
    assert "不是这份 SVG 的最新产物" in icon_assets.asset_state(tmp_path).detail

    icon_assets.write_stamp(tmp_path / icon_assets.SVG_NAME, tmp_path)
    state = icon_assets.asset_state(tmp_path)
    assert state.fresh is True
    assert "与位图一致" in state.detail


def test_summary_only_nags_when_something_is_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """启动日志那一行: 一致时只说状态, 不一致时附带该跑的命令."""
    monkeypatch.setattr(icon_assets, "resource_root", lambda: tmp_path)
    assert "build_icon.py" in icon_assets.summary()

    (tmp_path / icon_assets.SVG_NAME).write_text("<svg/>", encoding="utf-8")
    for path in icon_assets.required_assets(tmp_path):
        path.write_bytes(b"x")
    icon_assets.write_stamp(tmp_path / icon_assets.SVG_NAME, tmp_path)
    assert "build_icon.py" not in icon_assets.summary()


def test_fingerprint_follows_the_file_content(tmp_path: Path) -> None:
    """指纹认内容: 内容一变, 位图就不再算"最新"."""
    svg = tmp_path / "a.svg"
    svg.write_text("<svg/>", encoding="utf-8")
    first = icon_assets.fingerprint(svg)
    svg.write_text("<svg><!-- 改过 --></svg>", encoding="utf-8")
    assert icon_assets.fingerprint(svg) != first
    assert icon_assets.fingerprint(svg) == icon_assets.fingerprint(svg)


def test_fingerprint_ignores_line_endings(tmp_path: Path) -> None:
    """指纹与换行风格无关: 同一份内容, CRLF 与 LF 检出必须同指纹.

    2026-10-15 的 CI 事故守卫: stamp 在 Windows(``autocrlf`` 出 CRLF 工作区)上写入,
    macOS/Linux 却按 LF 检出 —— 直接对工作区字节取哈希时, 同一次重画在两平台的
    指纹不同, 守卫全红(位图本身没有任何问题)。指纹只认内容, "谁检出"不许改变
    "是不是这一版"。
    """
    body = "<svg>\n  <rect/>\n</svg>\n"
    lf = tmp_path / "lf.svg"
    lf.write_bytes(body.encode())
    crlf = tmp_path / "crlf.svg"
    crlf.write_bytes(body.replace("\n", "\r\n").encode())

    assert icon_assets.fingerprint(crlf) == icon_assets.fingerprint(lf)


def test_icon_files_only_reports_what_is_there(tmp_path: Path) -> None:
    """定位图标: 文件在就给路径, 不在就给 None(缺资源是正常状态, 不抛异常)."""
    empty = icon_assets.icon_files(tmp_path)
    assert (empty.ico, empty.window_ico) == (None, None)
    assert (empty.window_png, empty.logo_png) == (None, None)

    (tmp_path / icon_assets.ICO_NAME).write_bytes(b"x")
    (tmp_path / icon_assets.WINDOW_ICO_NAME).write_bytes(b"x")
    (tmp_path / icon_assets.PNG_SIZES[1][1]).write_bytes(b"x")
    found = icon_assets.icon_files(tmp_path)
    assert found.ico == tmp_path / icon_assets.ICO_NAME
    assert found.window_ico == tmp_path / icon_assets.WINDOW_ICO_NAME
    assert found.logo_png == tmp_path / icon_assets.PNG_SIZES[1][1]
    assert found.window_png is None


def test_render_ico_puts_every_size_in_one_file(tmp_path: Path) -> None:
    """装配 ICO 用的是 Pillow(已装 256px 的源图, 各档只缩不放大)."""
    source = tmp_path / "source.png"
    Image.new("RGBA", (256, 256), (18, 30, 48, 255)).save(source)
    target = tmp_path / "out.ico"
    icon_assets.render_ico(source, target)
    assert _ico_sizes(target) == {(size, size) for size in icon_assets.ICO_SIZES}
    assert _ico_entry_kinds(target) == {"PNG"}, "exe 那颗默认就是 PNG 编码"


def test_render_ico_can_write_bitmap_entries(tmp_path: Path) -> None:
    """``bitmap=True`` 出的是 BMP 档(窗口那颗走这条路)."""
    source = tmp_path / "source.png"
    Image.new("RGBA", (256, 256), (18, 30, 48, 255)).save(source)
    target = tmp_path / "window.ico"
    icon_assets.render_ico(
        source, target, sizes=icon_assets.WINDOW_ICO_SIZES, bitmap=True
    )
    assert _ico_sizes(target) == {(size, size) for size in icon_assets.WINDOW_ICO_SIZES}
    assert _ico_entry_kinds(target) == {"BMP"}


def test_ffmpeg_argv_scales_the_svg_into_a_square_png(tmp_path: Path) -> None:
    """命令行参数(只拼不跑): 读 SVG、按目标边长缩放、写一张 PNG."""
    argv = icon_assets.ffmpeg_argv(
        "ffmpeg", tmp_path / "app.svg", tmp_path / "app-64.png", 64
    )
    assert argv[0] == "ffmpeg"
    assert str(tmp_path / "app.svg") in argv
    assert argv[-1] == str(tmp_path / "app-64.png")
    assert f"scale=64:64:flags={icon_assets.SCALE_FLAGS}" in argv
    assert argv[argv.index("-frames:v") + 1] == "1"
    assert "-update" in argv
    assert "rgba" in argv


def test_find_ffmpeg_prefers_the_override_then_the_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """先看环境变量(要真存在), 再看 PATH; 都没有返回 None —— 找不到不算错误."""
    monkeypatch.delenv(icon_assets.FFMPEG_ENV, raising=False)
    assert (
        icon_assets.find_ffmpeg(lambda _name: "ffmpeg-from-path") == "ffmpeg-from-path"
    )
    assert icon_assets.find_ffmpeg(lambda _name: None) is None

    missing = tmp_path / "nope" / "ffmpeg.exe"
    monkeypatch.setenv(icon_assets.FFMPEG_ENV, str(missing))
    assert icon_assets.find_ffmpeg(lambda _name: "ffmpeg-from-path") is None

    real = tmp_path / "ffmpeg.exe"
    real.write_bytes(b"x")
    monkeypatch.setenv(icon_assets.FFMPEG_ENV, f"  {real}  ")
    assert icon_assets.find_ffmpeg(lambda _name: "ffmpeg-from-path") == str(real)


# --- 接线层: 打包脚本、工作流与钩子 ----------------------------------------------


def test_the_packaging_spec_declares_the_icon_and_fails_without_it() -> None:
    """发布产物要带图标; 缺位图要当场失败, 不静默打包."""
    text = (_REPO_ROOT / "packaging" / "archive-management.spec").read_text(
        encoding="utf-8"
    )
    assert text.count("icon=str(icon_file)") == 1, "单文件产物的 EXE 要带上图标"
    assert "resources/app.ico" in text
    assert "raise SystemExit" in text, "缺图标时要当场失败, 不能默默发出默认图标"


def test_the_release_workflow_checks_the_icon_before_packaging() -> None:
    """发布流水线: 先用指纹判"改了 SVG 忘了重画", 再打包.

    2026-10-15 起**不再在 CI 上重画**: 原来还有一条信息探针(`--out-dir` 到 `%TEMP%` 重画一遍并
    逐字节比对)与一个只做 `workflow_dispatch` 的 `Icon probe` 工作流, 都删了 —— 图标只在本地更新
    (`uv run python scripts/build_icon.py --force`)。所以这条只钉两件事: `--check` 在打包之前 +
    **没有**重画步骤回来(它会在 runner 上真跑 ffmpeg, 而发布链不该赌那个)。
    """
    text = ci_workflow.RELEASE_BUILD_WORKFLOW.read_text(encoding="utf-8")
    body = ci_workflow.job_block(text, "build")
    assert "scripts/build_icon.py --check" in body
    check = body.index("build_icon.py --check")
    build = body.index("uv run pyinstaller --noconfirm --clean")
    assert check < build, "先判指纹、再打包: 入库产物陈旧要在打包前拦住"
    assert "--out-dir" not in body, "发布作业里不该再重画图标(那是本地的事)"
    assert "--force" not in body, "同上: 发布只核对入库产物, 不改写它"


def test_the_icon_script_runs_without_an_installed_project(tmp_path: Path) -> None:
    """脚本自己把 ``src`` 放进 ``sys.path``.

    CI 的安装步骤刻意带 ``--no-install-project``(打包靠 spec 里的 pathex), 那种环境里
    ``import archive_management`` 会直接 ModuleNotFoundError —— 实测过, 而 uv 不读 .env,
    所以只能靠脚本自己。这条同时钉住 ``--out-dir`` 的解析。
    """
    module = _load_script("build_icon")
    assert Path(module._SRC) == _REPO_ROOT / "src"
    assert "sys.path.insert" in (_REPO_ROOT / "scripts" / "build_icon.py").read_text(
        encoding="utf-8"
    )
    assert module.build_parser().parse_args([]).out_dir is None
    parsed = module.build_parser().parse_args(["--out-dir", str(tmp_path)])
    assert parsed.out_dir == tmp_path


def test_pre_commit_regenerates_the_bitmaps_when_the_svg_changes() -> None:
    """提交钩子: 只在 SVG 变了时触发, 就地重画并把产物加回暂存区."""
    text = (_REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")
    hook = text.split("- id: build-icon", 1)[1].split("- id:", 1)[0]
    assert "uv run --no-sync" in hook, "钩子不许隐式 sync(见配置文件顶部说明)"
    assert "scripts/build_icon.py --stage" in hook
    assert "files: ^src/archive_management/resources/app\\.svg$" in hook


# --- 接线层: 界面 ------------------------------------------------------------------


class _Recorder:
    """记下被调用的参数(替身控件/替身窗口共用)."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> None:
        self.calls.append((args, kwargs))


class _FakeWidget:
    """只记配置的假控件(不建 Tk)."""

    def __init__(self, parent: Any, **kwargs: Any) -> None:
        self.parent = parent
        self.kwargs = kwargs
        self.configures: list[dict[str, Any]] = []

    def configure(self, **kwargs: Any) -> None:
        self.configures.append(kwargs)


class _FakeKit:
    """假 UiKit: 只关心"有没有登记重绘回调"."""

    def __init__(self) -> None:
        self.painted: list[Any] = []

    def register(self, repaint: Any) -> None:
        self.painted.append(repaint)


class _FakeCtk:
    """假 customtkinter 模块: 只提供品牌位用到的两个控件."""

    def __init__(self) -> None:
        self.frames: list[_FakeWidget] = []
        self.labels: list[_FakeWidget] = []

        def frame(parent: Any, **kwargs: Any) -> _FakeWidget:
            widget = _FakeWidget(parent, **kwargs)
            self.frames.append(widget)
            return widget

        def label(parent: Any, **kwargs: Any) -> _FakeWidget:
            widget = _FakeWidget(parent, **kwargs)
            self.labels.append(widget)
            return widget

        self.CTkFrame = frame
        self.CTkLabel = label


@pytest.fixture
def app_without_window() -> Any:
    """一个没跑 ``__init__`` 的 ``ArchiveApp``: 只挂上这些方法用到的那几个属性."""
    app = object.__new__(main_mod.ArchiveApp)
    app.p = Palette.for_theme("dark")
    app.kit = _FakeKit()  # type: ignore[assignment]  # 替身不必是 UiKit
    return app


def _plan(logo: Path | None) -> icon_assets.IconFiles:
    """造一份"只有品牌位"的资源清单(其余各项与本组用例无关)."""
    return icon_assets.IconFiles(
        ico=None, window_ico=None, window_png=None, logo_png=logo
    )


def _recorders(app: Any) -> tuple[_Recorder, _Recorder]:
    """把窗口那两个方法换成替身(经变量的 ``setattr``: 写常量会触发 B010 与 method-assign)."""
    photo, bitmap = _Recorder(), _Recorder()
    for name, recorder in (("iconphoto", photo), ("iconbitmap", bitmap)):
        setattr(app, name, recorder)
    return photo, bitmap


def test_the_brand_mark_uses_the_logo_bitmap_when_it_is_there(
    app_without_window: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """有 ``app-64.png`` 时顶栏画的是它(30px 的位配 64px 的图)."""
    fake_ctk = _FakeCtk()
    sentinel = object()
    monkeypatch.setattr(main_mod, "ctk", fake_ctk)
    monkeypatch.setattr(main_mod, "_logo_image", lambda host, path: sentinel)
    monkeypatch.setattr(
        icon_assets, "icon_files", lambda: _plan(tmp_path / "app-64.png")
    )

    widget = main_mod.ArchiveApp._build_brand_logo(app_without_window, object())

    assert isinstance(widget, _FakeWidget)
    assert fake_ctk.labels == [widget], "有图就用 CTkLabel 画"
    assert widget.kwargs["image"] is sentinel
    assert app_without_window._logo_image is sentinel, "贴图要留引用, 否则会被回收"
    assert not fake_ctk.frames


def test_the_brand_mark_falls_back_to_the_accent_square(
    app_without_window: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """没有位图(或解不开)时退回原来的强调色方块 —— 少一个标记, 但界面照常."""
    fake_ctk = _FakeCtk()
    monkeypatch.setattr(main_mod, "ctk", fake_ctk)
    monkeypatch.setattr(
        icon_assets, "icon_files", lambda: _plan(tmp_path / "missing.png")
    )
    monkeypatch.setattr(main_mod, "_logo_image", lambda host, path: None)

    widget = main_mod.ArchiveApp._build_brand_logo(app_without_window, object())

    assert isinstance(widget, _FakeWidget)
    assert fake_ctk.frames == [widget]
    assert widget.kwargs["fg_color"] == app_without_window.p.accent
    assert widget.kwargs["width"] == main_mod.LOGO_SIZE
    assert app_without_window.kit.painted, "方块的颜色要跟着主题重绘"
    assert not fake_ctk.labels


def test_the_brand_mark_falls_back_when_there_is_no_asset_at_all(
    app_without_window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """连路径都没有时不建图(不假装有图标)."""
    fake_ctk = _FakeCtk()
    monkeypatch.setattr(main_mod, "ctk", fake_ctk)
    monkeypatch.setattr(icon_assets, "icon_files", lambda: _plan(None))

    widget = main_mod.ArchiveApp._build_brand_logo(app_without_window, object())
    assert fake_ctk.frames == [widget]


def test_logo_image_returns_none_for_a_file_that_is_not_a_picture(
    app_without_window: Any, tmp_path: Path
) -> None:
    """位图被截断/写坏时返回 None(调用方退回落样式), 而不是把启动带崩."""
    broken = tmp_path / "app-64.png"
    broken.write_text("这不是图片", encoding="utf-8")
    assert main_mod._logo_image(app_without_window, broken) is None


class _Rejecting:
    """替身: "Tk 不认这个文件", 一被调用就抛, 并记下发过几次."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: Any, **_kwargs: Any) -> None:
        self.calls += 1
        raise tk.TclError("not for me")


class _FakeWindow:
    """只带图标那两个方法的替身窗口(不建真 Tk)."""

    def __init__(self) -> None:
        self.photo: Any = _Recorder()
        self.bitmap: Any = _Recorder()
        # 名字经变量传: 常量字符串会被 ruff 的 B010 拦。
        for name, target in (("iconphoto", self.photo), ("iconbitmap", self.bitmap)):
            setattr(self, name, target)

    def reject_iconbitmap(self) -> _Rejecting:
        """把 ``iconbitmap`` 换成"Tk 不认"的替身(X11/macOS 上 ICO 就是这种运气).

        注意换的是 Tk 那个名字(``iconbitmap``), 不是记账用的 ``bitmap``。
        """
        rejecting = _Rejecting()
        self.bitmap = self.iconbitmap = rejecting
        return rejecting


def _icons(ico: Path | None, png: Path | None) -> icon_assets.IconFiles:
    """造一份"只有窗口图标"的资源清单(品牌位与本组用例无关)."""
    return icon_assets.IconFiles(
        ico=None, window_ico=ico, window_png=png, logo_png=None
    )


@pytest.fixture
def fake_window() -> Any:
    """替身窗口(当 ``Any`` 用: 它本来就不是真 Tk 窗口, 免得每个调用点都 cast)."""
    return _FakeWindow()


def test_the_main_window_delegates_to_the_shared_icon_helper(
    app_without_window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """主窗口不自己写一套: 调用共享的 ``apply_window_icon``(子窗口由它接管)."""
    calls = _Recorder()
    monkeypatch.setattr(main_mod, "apply_window_icon", calls)

    main_mod.ArchiveApp._apply_window_icon(app_without_window)

    assert calls.calls == [((app_without_window,), {})]


def test_the_window_ico_wins_when_tk_accepts_it(
    fake_window: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """窗口用的是那颗小 ICO(不是给 exe 的七档大图), 成了就不建贴图."""
    ico = tmp_path / "app-window.ico"
    exe_ico = tmp_path / "app.ico"
    rejecting = _Rejecting()
    monkeypatch.setattr(
        icon_assets,
        "icon_files",
        lambda: icon_assets.IconFiles(
            ico=exe_ico, window_ico=ico, window_png=None, logo_png=None
        ),
    )
    monkeypatch.setattr(tk, "PhotoImage", rejecting)

    assert window_icon.apply_window_icon(fake_window) is True

    assert fake_window.bitmap.calls == [((str(ico),), {})]
    assert str(exe_ico) not in [call[0][0] for call in fake_window.bitmap.calls], (
        "exe 那颗别喂给 Tk"
    )
    assert rejecting.calls == 0, "ICO 成了就不该再去建 PNG 贴图"
    assert fake_window.photo.calls == []


def test_a_rejected_ico_falls_back_to_the_png(
    fake_window: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ICO 被 Tk 拒了(X11/macOS, 或文件坏了): 退回 ``iconphoto`` + PNG, 并留引用."""
    ico = tmp_path / "app-window.ico"
    png = tmp_path / "app-256.png"
    rejecting = fake_window.reject_iconbitmap()
    photo = object()

    class _Photo:
        def __init__(self, **kwargs: Any) -> None:
            assert kwargs["master"] is fake_window, "贴图要建在窗口自己的解释器上"
            assert kwargs["file"] == str(png)
            self.payload = photo

    monkeypatch.setattr(icon_assets, "icon_files", lambda: _icons(ico, png))
    monkeypatch.setattr(tk, "PhotoImage", _Photo)

    assert window_icon.apply_window_icon(fake_window) is True

    assert rejecting.calls == 1, "ICO 试过(被拒)"
    stashed = getattr(fake_window, window_icon._PHOTO_ATTR)
    assert fake_window.photo.calls == [((True, stashed), {})]
    assert stashed.payload is photo, "贴图必须留引用, 否则会被回收"


def test_a_window_gets_no_icon_when_there_is_none(
    fake_window: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """一个资源都没有时: 什么都不装, 也不报错(图标不是启动的前提)."""
    rejecting = _Rejecting()
    monkeypatch.setattr(icon_assets, "icon_files", lambda: _icons(None, None))
    monkeypatch.setattr(tk, "PhotoImage", rejecting)

    assert window_icon.apply_window_icon(fake_window) is False

    assert rejecting.calls == 0
    assert fake_window.bitmap.calls == []
    assert fake_window.photo.calls == []


def test_a_broken_png_only_means_no_icon(
    fake_window: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """只有 PNG 而它解不开时返回 False(不把异常漏给调用方)."""
    monkeypatch.setattr(
        icon_assets, "icon_files", lambda: _icons(None, tmp_path / "broken.png")
    )
    monkeypatch.setattr(tk, "PhotoImage", _Rejecting())

    assert window_icon.apply_window_icon(fake_window) is False
    assert fake_window.photo.calls == []


def test_the_titlebar_timer_lands_on_our_icon(
    fake_window: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CTk 那个 200ms 定时器最终调到这里: 落点是我们的图标."""
    ico = tmp_path / "app-window.ico"
    monkeypatch.setattr(icon_assets, "icon_files", lambda: _icons(ico, None))

    window_icon._titlebar_icon(fake_window)

    assert fake_window.bitmap.calls == [((str(ico),), {})]


def test_the_timer_leaves_a_window_that_already_has_an_icon_alone(
    fake_window: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """已经有人设过图标(主窗口在 ``__init__`` 里就设了)的窗口不再动它.

    这是与库同样的判据(``_iconbitmap_method_called``)。我们比库**多**出来的那一次
    ``wm iconbitmap`` 正是实测会把 Tk 弄崩的那个差异(见 ui/window_icon 的说明)。
    """
    monkeypatch.setattr(
        icon_assets, "icon_files", lambda: _icons(tmp_path / "app-window.ico", None)
    )
    turn_on = window_icon._SET_BY_CALLER_ATTR
    setattr(fake_window, turn_on, True)

    window_icon._titlebar_icon(fake_window)

    assert fake_window.bitmap.calls == [], "设过就别再贴第二遍"


def test_the_ctk_default_icon_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """两个窗口类上的私有方法都换成我们的; 再调一次什么都不做(幂等)."""
    method = "_windows_set_titlebar_icon"
    classes = (CTk, CTkToplevel)
    originals = [getattr(cls, method) for cls in classes]
    spy = _Recorder()
    try:
        monkeypatch.setattr(window_icon, "_APPLIED", False)
        window_icon.install_default_window_icon()
        assert [getattr(cls, method) for cls in classes] == [
            window_icon._titlebar_icon
        ] * 2

        for cls in classes:
            setattr(cls, method, spy)
        window_icon.install_default_window_icon()
        assert [getattr(cls, method) for cls in classes] == [spy] * 2, (
            "装过一次就不该再动(否则热重载/多次 import 会把补丁盖回去)"
        )
    finally:
        for cls, original in zip(classes, originals, strict=True):
            setattr(cls, method, original)
