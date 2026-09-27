"""UI 调色板.

颜色取自设计稿 ``docs/archive-management-ui.svg``(深色)与其
``*-light.svg``(浅色)中的色值映射。控件按当前主题统一取色,保证
主题切换只改颜色、不改布局与操作语义。

**浅色主题的强调色、危险色、次要文字与禁用文字按对比度判据重新取过值**
(深色主题的原值本来就达标): 原来 accent 2.44:1、danger 2.75:1、
text_muted 4.17:1、text_disabled 1.84:1,都低于本仓库自己定的下限; 取色依据与
逐对量测见 :mod:`archive_management.ui.contrast` 与 ``docs/testing.md``。
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
    card: str  # 列表项/备份卡片背景(需与 well/panel 明确区分)
    card_border: str  # 列表项/备份卡片描边
    card_hover: str  # 列表项/备份卡片悬停背景
    well: str  # 列表滚动区背景(卡片在其中以“凹槽”方式呈现)
    input_bg: str  # 输入/下拉控件背景
    border: str  # 面板描边
    text_primary: str  # 标题等强文本
    text_body: str  # 正文
    text_hint: str  # 功能说明文字(比 muted 清楚, 比正文淡)
    text_muted: str  # 次要/元数据文本(时间、计数等)
    text_disabled: str  # 禁用态文字(比 muted 更暗, 一眼看出不能点)
    disabled_bg: str  # 禁用态底色(比 raised 更暗)
    disabled_border: str  # 禁用态描边(压暗到几乎看不出)
    accent: str  # 强调主色(teal)
    accent_text: str  # 强调色上的文字
    accent_soft: str  # 强调淡底(选中项)
    accent_soft_text: str  # 强调淡底上的文字
    accent_soft_border: str  # 强调淡底描边
    danger: str  # 危险/主操作珊瑚色
    danger_text: str  # 危险按钮上的文字
    item_hover: str  # 列表项/凸起控件悬浮
    hero_bg: str  # 概要卡渐变起始色
    badge_manual_bg: str  # “手动”徽章
    badge_manual_text: str
    badge_auto_bg: str  # “自动”徽章
    badge_auto_text: str
    badge_safety_bg: str  # “恢复前安全点”徽章(与手动/自动必须是三种可区分的颜色)
    badge_safety_text: str
    success: str  # 状态成功点
    focus_ring: str  # 键盘焦点环(普通底色上): 专用于焦点, 不参与任何语义
    focus_ring_on_fill: str  # 键盘焦点环(主色/危险色实底按钮上): 同上

    @classmethod
    def for_theme(cls, name: str) -> Palette:
        """返回指定主题调色板;未知主题回退到深色."""
        if name == "light":
            return LIGHT
        return DARK

    def selection_colors(self, selected: bool) -> tuple[str, str]:
        """返回选中项的(底色, 描边色), 未选中时返回卡片的常规配色.

        **所有列表/卡片视图共用这一条规则**: 选中 = 描边 + 浅底, 而不是换成主按钮
        那种实心强调色。列表与海报因此有了同一套"我选中了谁"的表达, 也不会与
        "+ 添加游戏 / 打开详情"这类"哪里能点"的实心绿撞在一起。
        """
        if selected:
            return self.accent_soft, self.accent_soft_border
        return self.card, self.card_border


DARK = Palette(
    background="#0b1120",
    topbar="#0a1120",
    sidebar="#0d1728",
    panel="#101b2d",
    raised="#111e31",
    card="#1a2942",
    card_border="#2f4767",
    card_hover="#223657",
    well="#0c1524",
    input_bg="#0c1627",
    border="#253a55",
    text_primary="#f4f7fb",
    text_body="#d2dbea",
    text_hint="#aab8ce",
    text_muted="#8291aa",
    text_disabled="#5a6a80",
    disabled_bg="#0d1523",
    disabled_border="#1b2a3d",
    accent="#55d6be",
    accent_text="#092329",
    accent_soft="#17343a",
    accent_soft_text="#b8fff0",
    accent_soft_border="#3d867e",
    danger="#d97852",
    danger_text="#26140e",
    item_hover="#16233a",
    hero_bg="#1c314d",
    badge_manual_bg="#3a3342",
    badge_manual_text="#e1c8ed",
    badge_auto_bg="#29384a",
    badge_auto_text="#b8c9df",
    badge_safety_bg="#4a3b22",
    badge_safety_text="#f2d6a4",
    success="#55d6be",
    # 焦点环**专用**色(不参与任何语义): 实测"每套主题一个颜色"做不到 ——
    # 深色主题的普通底色都很暗(需要亮环, 最差 9.50:1), 而主色/危险色实底很亮
    # (需要深环, 最差 6.76:1), 两者合并后最优也只有 1.87:1。因此分两档:
    # 亮青在暗底/暗面板上一眼可见; 深青黑在主色与危险色实底上都至少 6.7:1。
    focus_ring="#8ee6ff",
    focus_ring_on_fill="#04141c",
)

LIGHT = Palette(
    background="#edf2f4",
    topbar="#ffffff",
    sidebar="#ffffff",
    panel="#ffffff",
    raised="#ffffff",
    card="#ffffff",
    card_border="#cdd8e3",
    card_hover="#eef4f8",
    well="#f1f4f8",
    input_bg="#f6f8f9",
    border="#d6dee6",
    text_primary="#1b2735",
    text_body="#405168",
    text_hint="#4c5d73",
    text_muted="#5c6b7d",
    text_disabled="#8090a2",
    disabled_bg="#e6ebef",
    disabled_border="#dde3e9",
    accent="#197a68",
    accent_text="#ffffff",
    accent_soft="#d9eee8",
    accent_soft_text="#087765",
    accent_soft_border="#3f8f7b",
    danger="#b0431f",
    danger_text="#ffffff",
    item_hover="#f0f4f6",
    hero_bg="#e5f0ee",
    badge_manual_bg="#f0e2f2",
    badge_manual_text="#70427b",
    badge_auto_bg="#e8edf1",
    badge_auto_text="#4d5d70",
    badge_safety_bg="#f7e7c9",
    badge_safety_text="#8a5a12",
    success="#157a63",
    # 浅色主题的普通底色都很亮(需要深环, 最差 16.87:1), 而主色/危险色实底是深色
    # (需要亮环, 最差 4.58:1) —— 与深色主题同一套两档思路(量测见 DARK 那段注释)。
    focus_ring="#0d1b2a",
    focus_ring_on_fill="#f2f7f4",
)
