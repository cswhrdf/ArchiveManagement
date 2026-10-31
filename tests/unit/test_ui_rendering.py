"""绘制层修补单元测试.

CustomTkinter 的绘制引擎默认把绘制尺寸向下取整到偶数, 奇数尺寸的控件因此
会丢掉 1px 右边框/下边框(露出的其实是父容器背景)。这里验证修补函数确实关掉
了该取整并且可以重复调用。不需要显示环境: ``DrawEngine`` 只保存 canvas 引用。

另有一条: 下拉框右侧那半段边框的修补(CTk 把它涂成了箭头区底色, 用户 2026-10-02
反馈"右侧边框被下拉符号盖住了") —— 同样用假控件验证, 不需要显示环境。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from customtkinter import CTkCanvas, CTkComboBox, DrawEngine
from PIL import Image, ImageTk

import archive_management.ui.rendering as rendering
from archive_management.ui.rendering import (
    apply_border_rendering_fix,
    apply_combo_border_fix,
    paint_combo_border,
)

#: 仓库根的 ``tests/unit/test_ui_rendering.py`` → 上两级是仓库根。
_TESTS_ROOT = Path(__file__).resolve().parents[2]

pytestmark = [
    pytest.mark.ui,
    pytest.mark.minor,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("通用控件"),
    pytest.mark.story("控件边框渲染"),
    pytest.mark.layer("unit"),
]


class _FakeCanvas:
    """占位 canvas: 引擎构造只保存引用."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.args = args
        self.kwargs = kwargs


def test_apply_border_rendering_fix_only_patches_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """第一次调用执行修补, 重复调用直接返回, 不会反复包装 ``__init__``."""
    monkeypatch.setattr(rendering, "_APPLIED", False)
    monkeypatch.setattr(DrawEngine, "__init__", DrawEngine.__init__)

    assert apply_border_rendering_fix() is True
    assert apply_border_rendering_fix() is False


def test_draw_engine_rounds_to_actual_size() -> None:
    """回归: 绘制引擎必须按真实尺寸绘制, 否则奇数尺寸控件会丢边框."""
    apply_border_rendering_fix()

    engine = DrawEngine(cast("CTkCanvas", _FakeCanvas()))

    assert getattr(engine, "_round_width_to_even_numbers", False) is False
    assert getattr(engine, "_round_height_to_even_numbers", False) is False


class _RecordingCanvas:
    """只把 ``itemconfig`` 记下来的假画布."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def itemconfig(self, item: str, **kwargs: Any) -> None:
        self.calls.append((item, kwargs))


class _FakeCombo:
    """假下拉框: 只保存边框色与画布引用(颜色对按浅色/深色给第一个)."""

    def __init__(self, border_color: str = "#6a7681") -> None:
        self._border_color = border_color
        self._canvas = _RecordingCanvas()

    @staticmethod
    def _apply_appearance_mode(value: Any) -> Any:
        return value[0] if isinstance(value, tuple) else value


def test_paint_combo_border_uses_the_border_color_for_the_right_side() -> None:
    """右侧那半段边框要描回**边框色**(而不是箭头区底色) —— 箭头因此落在边框里面."""
    combo = _FakeCombo()

    paint_combo_border(cast("CTkComboBox", combo))

    assert combo._canvas.calls == [
        ("border_parts_right", {"outline": "#6a7681", "fill": "#6a7681"})
    ]


def test_apply_combo_border_fix_paints_after_every_draw(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """补丁只打一次(幂等); 打上之后每次 ``_draw`` 都会把右侧边框描回边框色."""
    monkeypatch.setattr(CTkComboBox, "_draw", lambda _self: None)  # 假的原始实现
    monkeypatch.setattr(rendering, "_COMBO_APPLIED", False)

    assert apply_combo_border_fix() is True
    assert apply_combo_border_fix() is False

    combo = _FakeCombo()
    CTkComboBox._draw(cast("CTkComboBox", combo))

    assert combo._canvas.calls == [
        ("border_parts_right", {"outline": "#6a7681", "fill": "#6a7681"})
    ]


# --- 图片必须建在"要用它那个窗口"的解释器里 ---------------------------------
# 出处: 2026-10-04 Linux 分片 0 —— 上一个用例的窗口没被拆干净, 默认根指着它;
# 下一条用例贴图标时报 `image "pyimage1" does not exist`。CTkImage 自己贴图时
# 调的是 `ImageTk.PhotoImage(图片)`(**不带 master**), 于是图片落到了默认根那个
# 解释器里, 而标签属于新窗口的解释器。真正走显示环境的验证在
# `tests/integration/test_gui_roots.py`, 这里钉"传没传 master"这件事本身。

_GUARDED_IMAGE_CALL = "ctk.CTkImage("


class _FakePhotoImage:
    """记下构造参数, 代替真要解释器的 ``ImageTk.PhotoImage``."""

    def __init__(self, image: Any, master: Any = None) -> None:
        """记住图片与 master(不碰 Tk)."""
        self.image = image
        self.master = master


def test_host_image_creates_the_photo_image_on_the_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``host_image`` 建贴图时必须把宿主传成 ``master``(否则它会落到默认根上)."""
    monkeypatch.setattr(ImageTk, "PhotoImage", _FakePhotoImage)
    host = object()
    picture = rendering.host_image(
        cast("Any", host), light_image=Image.new("RGB", (10, 10)), size=(10, 10)
    )

    first = picture._get_scaled_light_photo_image((10, 10))
    second = picture._get_scaled_light_photo_image((10, 10))

    assert cast("_FakePhotoImage", first).master is host, "贴图要建在宿主那个解释器里"
    assert first is second, "同一尺寸要复用缓存(与库里的实现一致)"


def test_only_the_rendering_layer_builds_images() -> None:
    """界面层自己建图片必须走 ``host_image``, 不许直接写 ``ctk.CTkImage(...)``.

    这条是静态守卫: 只要有人以后又用库原样的构造器, "图片落到默认根"这个坑就会
    回来 —— 而它只在"会话里有第二个 Tk 根"时发作, 靠读代码很容易漏。
    """
    offenders = [
        f"{path.name}:{number}"
        for path in sorted(
            (_TESTS_ROOT / "src" / "archive_management" / "ui").glob("*.py")
        )
        if path.name != "rendering.py"
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _GUARDED_IMAGE_CALL in line
    ]

    assert offenders == [], f"这些地方要改用 rendering.host_image: {offenders}"


def test_the_dark_photo_image_is_built_on_the_host_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """深色模式的贴图与浅色那条完全对称: 用的是深色原图与它自己那份缓存.

    两条都必须在**宿主**的解释器上建图(见本模块顶部说明), 少一条时切到那个外观模式
    就会报 ``image \"pyimageN\" doesn't exist`` —— 所以这里把两边都钉住。
    """
    image = rendering._HostImage.__new__(rendering._HostImage)
    dark = cast(Image.Image, object())
    image._dark_image = dark
    cache: dict[tuple[int, int], ImageTk.PhotoImage] = {}
    image._scaled_dark_photo_images = cache
    seen: list[tuple[Any, Any, tuple[int, int]]] = []

    def record(
        source: Any,
        target: dict[tuple[int, int], ImageTk.PhotoImage],
        size: tuple[int, int],
    ) -> ImageTk.PhotoImage:
        """记下"拿哪张原图、往哪份缓存里建", 不去碰真 Tk."""
        seen.append((source, target, size))
        return cast(ImageTk.PhotoImage, object())

    # 顶掉这一个实例上的建图步骤: 真建 PhotoImage 要有 Tk 根。
    monkeypatch.setattr(image, "_photo_image", record)

    assert image._get_scaled_dark_photo_image((32, 32)) is not None
    assert seen == [(dark, cache, (32, 32))]
