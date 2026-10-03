"""调色板对比度守卫: 两套主题的每一对"文字 / 状态指示 / 输入边界"都要够清楚.

判据与算法见 :mod:`archive_management.ui.contrast`, 量测数据见
``docs/testing.md``「对比度判据」一节. 这里只做一件事: 把**要判的对**逐条登记,
再对两套主题逐对量一遍 —— 谁把颜色改淡了, 哪一对不达标会直接指出名字与数值。

登记表是"只增不减"的: 新加一个文字色却忘了判它, 兜底断言
(:func:`test_every_text_token_is_judged`) 会变红, 而不是静默放过。

除了本仓库的公式, 每一对还会请**第三方库** ``color-contrast`` 再算一遍: 它与我们出自
不同作者, 一起写错的概率极低 —— 这是我们那条公式唯一的"外部对照"
(见 :func:`test_the_third_party_oracle_agrees_with_us_on_every_pair`)。
"""

from __future__ import annotations

from dataclasses import fields

import pytest
from color_contrast import check_contrast

from archive_management.ui.contrast import (
    DISABLED_MINIMUM,
    NON_TEXT_MINIMUM,
    TEXT_MINIMUM,
    contrast_ratio,
    relative_luminance,
)
from archive_management.ui.palette import DARK, LIGHT, Palette

pytestmark = [
    pytest.mark.trivial,
    pytest.mark.epic("界面框架"),
    pytest.mark.feature("主题与配色"),
    pytest.mark.story("对比度达标"),
    pytest.mark.layer("unit"),
]

# 文字会出现在哪些底色上(控件实际会用的组合, 取调色板里的全部表面色)。
# 两个**悬停**底色也算在内: 卡片/行在鼠标划过来时会换成它们, 上面的文字并没有跟着变 ——
# 实测漏登记的那个(深色 card_hover 上的 text_muted)只有 3.80:1。
SURFACES: tuple[str, ...] = (
    "background",
    "topbar",
    "sidebar",
    "panel",
    "raised",
    "card",
    "card_hover",
    "item_hover",
    "well",
    "input_bg",
)

# 常规文字: 4.5:1(WCAG 2.1 AA)。
_TEXT_TOKENS: tuple[str, ...] = ("text_primary", "text_body", "text_hint", "text_muted")
TEXT_PAIRS: tuple[tuple[str, str], ...] = (
    *((token, surface) for token in _TEXT_TOKENS for surface in SURFACES),
    ("accent_text", "accent"),
    ("accent_soft_text", "accent_soft"),
    ("danger_text", "danger"),
    ("badge_manual_text", "badge_manual_bg"),
    ("badge_auto_text", "badge_auto_bg"),
    ("badge_safety_text", "badge_safety_bg"),
)

# 禁用态文字: 规范豁免, 但我们自己设 2.5:1 的底线(禁用**不等于**看不见)。
DISABLED_PAIRS: tuple[tuple[str, str], ...] = tuple(
    ("text_disabled", surface) for surface in (*SURFACES, "disabled_bg")
)

# 非文字的状态指示: 选中描边、状态点、强调色图标 —— 3:1。
STATE_PAIRS: tuple[tuple[str, str], ...] = (
    ("accent", "background"),
    ("accent", "card"),
    # 折叠标记(I-9.5.2): 它画在 `card` / `card_hover` 两种底色上, 藏着选中项/当前节点时
    # 用强调色 —— G5 的先例(新图形出现就要登记它落在的每一种底色), 漏登记过一次的教训。
    ("accent", "card_hover"),
    ("accent", "panel"),
    ("accent", "well"),
    ("accent", "input_bg"),
    ("success", "background"),
    ("success", "card"),
    ("danger", "background"),
    ("danger", "card"),
    ("danger", "raised"),
    ("accent_soft_border", "accent_soft"),
    ("accent_soft_border", "card"),
    ("accent_soft_border", "well"),
)

# 输入控件的**边界**: WCAG 1.4.11 要求 3:1 —— 它承载"这里可以输入"的信息, 与状态指示
# 同一档; 下面 DECORATION_PAIRS 那些装饰性描边不适用(所以"别把装饰性描边塞进 3:1"
# 这条管不到这里)。原来输入框与面板共用 `border`, 实测最差 **1.20:1**(浅色 `border`
# 对 `card_hover`) —— 边界等于看不见; 现在单独取 `input_border`(取值均已实测达标)。
# 按**全部表面色**登记: 输入框可能落在其中任何一个上, 比"实际用到的组合"更严。
BOUNDARY_PAIRS: tuple[tuple[str, str], ...] = tuple(
    ("input_border", surface) for surface in SURFACES
)

# 装饰性描边(卡片外框/分隔线): **不承**担"这块区域是什么"的信息(内容自身
# 的文字对比度独立达标), 因此不设 3:1 硬门槛; 登记它们只是为了挡住
# "描边色被改得和底色一样"这种事故。1.2:1 是"肉眼还能看出有一条边"的下限。
# 注意: 输入框的边界**不在这里** —— 它承载"这里可以输入", 见上面的 BOUNDARY_PAIRS。
DECORATION_PAIRS: tuple[tuple[str, str], ...] = (
    ("card_border", "card"),
    ("card_border", "well"),
    ("border", "panel"),
)
DECORATION_MINIMUM = 1.2

ALL_PAIRS: tuple[tuple[str, str], ...] = (
    TEXT_PAIRS + DISABLED_PAIRS + STATE_PAIRS + BOUNDARY_PAIRS + DECORATION_PAIRS
)

THEMES = {"dark": DARK, "light": LIGHT}
# 兜底: 登记的配对数(改动登记表时必须一起改, 免得"删掉一对"悄悄溜过去)。
_EXPECTED_TEXT_PAIRS = 46
_EXPECTED_DISABLED_PAIRS = 11
_EXPECTED_STATE_PAIRS = 14
_EXPECTED_BOUNDARY_PAIRS = 10


def _minimum(pair: tuple[str, str]) -> float:
    if pair in TEXT_PAIRS:
        return TEXT_MINIMUM
    if pair in DISABLED_PAIRS:
        return DISABLED_MINIMUM
    if pair in STATE_PAIRS or pair in BOUNDARY_PAIRS:
        return NON_TEXT_MINIMUM
    return DECORATION_MINIMUM


def _color(palette: Palette, token: str) -> str:
    return str(getattr(palette, token))


@pytest.mark.parametrize("theme", sorted(THEMES))
@pytest.mark.parametrize(
    ("foreground", "background"),
    ALL_PAIRS,
    ids=[f"{fg}-on-{bg}" for fg, bg in ALL_PAIRS],
)
def test_pairs_meet_the_contrast_floor(
    theme: str, foreground: str, background: str
) -> None:
    """逐对量对比度: 不达标时报告里直接给出两个 token 名与实测值."""
    palette = THEMES[theme]
    ratio = contrast_ratio(_color(palette, foreground), _color(palette, background))
    floor = _minimum((foreground, background))
    assert ratio >= floor, (
        f"{theme}: {foreground}({_color(palette, foreground)}) on "
        f"{background}({_color(palette, background)}) = {ratio:.2f}:1 < {floor}:1"
    )


def test_registry_sizes_are_pinned() -> None:
    """登记表规模钉死: 少一对(悄悄不判了)或多一对(忘记同步数字)都要变红."""
    assert len(TEXT_PAIRS) == _EXPECTED_TEXT_PAIRS
    assert len(DISABLED_PAIRS) == _EXPECTED_DISABLED_PAIRS
    assert len(STATE_PAIRS) == _EXPECTED_STATE_PAIRS
    assert len(BOUNDARY_PAIRS) == _EXPECTED_BOUNDARY_PAIRS


def test_every_text_token_is_judged() -> None:
    """兜底: 调色板里每个"当文字用"的 token 都必须出现在登记表里.

    新增一个 ``text_warning`` 之类的颜色却没登记, 它会悄悄地不上判据 ——
    这条断言就是防这种情况, 顺手也防住了"把某个 text token 从登记表里删了"。
    """
    judged = {foreground for foreground, _ in ALL_PAIRS}
    text_tokens = {
        field.name
        for field in fields(Palette)
        if field.name.startswith("text_") or field.name.endswith("_text")
    }
    assert text_tokens <= judged, f"没有登记对比度判据的文字色: {text_tokens - judged}"


def test_every_surface_is_used_as_a_background() -> None:
    """兜底: 文字判据要覆盖调色板里的表面色, 不能只挑几个浅的判."""
    used = {background for _, background in TEXT_PAIRS}
    assert set(SURFACES) <= used


def test_the_two_themes_do_not_share_the_colors_that_were_fixed() -> None:
    """浅色主题的强调/危险/次要/禁用色是**按对比度重取**的, 不能与深色共用.

    这四类颜色在浅色主题上原本不达标(实测 accent 2.44:1 / danger 2.61:1 /
    text_muted 4.17:1 / text_disabled 1.84:1), 改成深色主题的同名色会立刻
    把上面的配对打红; 这条断言把"两套主题就是两套值"这个前提也钉住。
    """
    for token in ("accent", "danger", "text_muted", "text_disabled"):
        assert _color(DARK, token) != _color(LIGHT, token), token


def test_contrast_ratio_is_symmetric_and_bounded() -> None:
    """对比度公式本身的边界: 黑白 21:1、同色 1:1、与前后顺序无关."""
    assert contrast_ratio("#000000", "#ffffff") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#ffffff", "#000000") == pytest.approx(21.0, abs=0.01)
    assert contrast_ratio("#123456", "#123456") == pytest.approx(1.0, abs=0.001)
    assert contrast_ratio("#197a68", "#edf2f4") == pytest.approx(
        contrast_ratio("#edf2f4", "#197a68"), abs=1e-9
    )


def test_relative_luminance_matches_the_reference_values() -> None:
    """相对亮度的参考值(纯黑 0、纯白 1、中灰 0.2159)必须对得上."""
    assert relative_luminance("#000000") == pytest.approx(0.0, abs=1e-9)
    assert relative_luminance("#ffffff") == pytest.approx(1.0, abs=1e-9)
    assert relative_luminance("#808080") == pytest.approx(0.2159, abs=1e-3)


# 第三方库(``color-contrast``)实现了同一条 WCAG 公式, 这里拿它当"第二把算尺".
# 它只给**布尔**结论(``check_contrast`` 不返回比值), 所以反着用: 把门槛卡在我们的比值
# 上下各 _ORACLE_TOLERANCE, 两个方向它都答对, 就等于"它算出的比值与我们相差不超过这个容差".
# 容差只留给浮点: 公式真写错(漏了 sRGB 线性化、权重写反、通道取错)差的是量级, 不是末位.
_ORACLE_TOLERANCE = 0.005


def _oracle_problems(theme: str, palette: Palette, pair: tuple[str, str]) -> list[str]:
    """把一对颜色的对账结果整理成问题清单(空列表表示两边一致).

    只对**比值**下判据, 不另判一次"过不过门槛": 两边都用 ``>=`` 比门槛, 结论翻转需要这一对
    正好贴在门槛上(小于容差) —— 实测登记表里最贴线的一对(浅色 ``accent_soft_text`` on
    ``accent_soft``, 4.5206:1 对 4.5:1)还有 0.0206 的余量, 146 次量测里没有一次落进容差,
    所以"过不过"的断言在这里必然是绿的(空转), 交给上面的比值断言覆盖。
    """
    foreground, background = pair
    first, second = _color(palette, foreground), _color(palette, background)
    ratio = contrast_ratio(first, second)
    where = f"{theme}: {foreground} on {background}"
    problems: list[str] = []
    if not check_contrast(first, second, level=ratio - _ORACLE_TOLERANCE):
        problems.append(f"{where} 我们算 {ratio:.2f}:1, 第三方算得更低")
    if check_contrast(first, second, level=ratio + _ORACLE_TOLERANCE):
        problems.append(f"{where} 我们算 {ratio:.2f}:1, 第三方算得更高")
    return problems


def test_the_third_party_oracle_agrees_with_us_on_every_pair() -> None:
    """第三方库(colour 系)独立实现的 WCAG 公式必须与我们的结论一致.

    这条断言的价值在**实现是别人写的**: 我们的公式与登记表出自同一支笔, 一起写错时
    彼此印证不出来。它对 **73 对登记色、两套主题共 146 次量测**各算一遍, 比值必须落在
    我们的 ±容差内 —— 而门槛判定(3 / 4.5 / 2.5 / 1.2)由此自然一致: 要翻转需要某对颜色
    贴在门槛上, 而实测最贴线的一对还有 0.0206 的余量(见 :func:`_oracle_problems`)。
    """
    problems = [
        problem
        for theme, palette in sorted(THEMES.items())
        for pair in ALL_PAIRS
        for problem in _oracle_problems(theme, palette, pair)
    ]
    assert not problems, "第三方对比度实现与我们的结论不一致: " + " | ".join(problems)


def test_the_third_party_oracle_matches_the_reference_extremes() -> None:
    """两端行为对账: 黑白约 21:1、同色 1:1、与前后顺序无关.

    它自己的档位枚举 ``AccessibilityLevel.AA18 = 3`` 命名反直觉(18pt 那一档才是 3:1),
    所以我们只用它的**数值**门槛接口(``level=<float>``), 这条断言就是钉住这件事。
    """
    assert check_contrast("#000000", "#ffffff", level=20.99) is True
    assert check_contrast("#ffffff", "#000000", level=20.99) is True
    assert check_contrast("#000000", "#ffffff", level=21.01) is False
    assert check_contrast("#123456", "#123456", level=1.0) is True
    assert check_contrast("#123456", "#123456", level=DECORATION_MINIMUM) is False
