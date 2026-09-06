"""UI 调色板.

颜色取自设计稿 ``docs/archive-management-ui.svg``(深色)与其
``*-light.svg``(浅色)中的色值映射。控件按当前主题统一取色,保证
主题切换只改颜色、不改布局与操作语义。
"""

from __future__ import annotations

from dataclasses import dataclass

THEME_NAMES: tuple[str, ...] = ("dark", "light")
DEFAULT_THEME = "dark"


@dataclass(frozen=True)
class Palette:
    """一组命名颜色,供控件在两种主题间一致取色."""

    background: str  # 应用主背景
    topbar: str  # 顶部工具栏背景
    sidebar: str  # 左侧栏背景
    panel: str  # 普通卡片/面板背景
    raised: str  # 工具栏等较高层级背景
    input_bg: str  # 输入/下拉控件背景
    border: str  # 面板描边
    text_primary: str  # 标题等强文本
    text_body: str  # 正文
    text_muted: str  # 次要/说明文本
    accent: str  # 强调主色(teal)
    accent_text: str  # 强调色上的文字
    accent_soft: str  # 强调淡底(选中项)
    accent_soft_text: str  # 强调淡底上的文字
    accent_soft_border: str  # 强调淡底描边
    danger: str  # 危险/主操作珊瑚色
    danger_text: str  # 危险按钮上的文字
    item_hover: str  # 列表项悬浮
    item_active: str  # 列表项选中底
    hero_bg: str  # 概要卡渐变起始色
    badge_manual_bg: str  # “手动”徽章
    badge_manual_text: str
    badge_auto_bg: str  # “自动”徽章
    badge_auto_text: str
    success: str  # 状态成功点

    @classmethod
    def for_theme(cls, name: str) -> Palette:
        """返回指定主题调色板;未知主题回退到深色."""
        if name == "light":
            return LIGHT
        return DARK


DARK = Palette(
    background="#0b1120",
    topbar="#0a1120",
    sidebar="#0d1728",
    panel="#101b2d",
    raised="#111e31",
    input_bg="#0c1627",
    border="#253a55",
    text_primary="#f4f7fb",
    text_body="#d2dbea",
    text_muted="#8291aa",
    accent="#55d6be",
    accent_text="#092329",
    accent_soft="#17343a",
    accent_soft_text="#b8fff0",
    accent_soft_border="#3d867e",
    danger="#d97852",
    danger_text="#26140e",
    item_hover="#16233a",
    item_active="#183c45",
    hero_bg="#1c314d",
    badge_manual_bg="#3a3342",
    badge_manual_text="#e1c8ed",
    badge_auto_bg="#29384a",
    badge_auto_text="#b8c9df",
    success="#55d6be",
)

LIGHT = Palette(
    background="#edf2f4",
    topbar="#ffffff",
    sidebar="#ffffff",
    panel="#ffffff",
    raised="#ffffff",
    input_bg="#f6f8f9",
    border="#d6dee6",
    text_primary="#1b2735",
    text_body="#405168",
    text_muted="#657589",
    accent="#2fae97",
    accent_text="#ffffff",
    accent_soft="#d9eee8",
    accent_soft_text="#087765",
    accent_soft_border="#9fd0c2",
    danger="#d97852",
    danger_text="#ffffff",
    item_hover="#f0f4f6",
    item_active="#d9eee9",
    hero_bg="#e5f0ee",
    badge_manual_bg="#f0e2f2",
    badge_manual_text="#70427b",
    badge_auto_bg="#e8edf1",
    badge_auto_text="#4d5d70",
    success="#1e9b80",
)
