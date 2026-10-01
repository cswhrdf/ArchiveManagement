"""界面字号单位: px(基准) 与 rem(相对基准字号).

浏览器里 ``1rem`` = 根字号; 这里取同一套语义: **1rem = 基准字号, 默认 16px**,
用户可在设置里调整(``config.ui.base_font_px``, 见 PLAN 的界面字号条目)。

代码里的字号仍然按 **px 口径**书写(与既有 137 处 ``CTkFont(size=N)`` 一致),
:func:`scaled` 是唯一的换算入口: 设置里选 16px 时倍数为 1.0(界面与既有尺寸完全一致),
选 20px 时所有字号等比放大 25%。

**不引入其它单位**(评估结论):

- ``px``: 基准单位 —— Tk 最终只认像素/点, 因此换算的落点只能是它;
- ``rem``: 引入 —— 需要"跟随用户设置"的地方都该用它;
- ``em``: 不引入 —— Tk 没有字体级联可依附, 每个控件各自持有一份字体, "相对于父元素
  字号"在桌面控件树里没有确定含义, 引进来只会让人误以为有继承;
- ``pt``: 不引入 —— 它是 Tk 的原生单位, 但会跟着系统 DPI 变, 混用会让"用户设定的
  16px"在不同机器上得到不同的物理大小;
- ``%`` / ``vw`` / ``vh``: 不引入 —— 桌面窗口没有稳定的参照物(视口会随窗口缩放),
  需要"占容器比例"的地方由布局权重(``grid`` 的 ``weight``)表达。

现有调用点不必改动: :func:`install_font_scaling` 把缩放装到 ``ctk.CTkFont`` 上,
``CTkFont(size=N)`` 从此都按当前基准字号走; 新代码可以用 :func:`font` / :func:`rem`
把意图写得更直白。
"""

from __future__ import annotations

import contextlib
import sys
import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import customtkinter as ctk

from archive_management.config import BASE_FONT_CHOICES, DEFAULT_BASE_FONT_PX

#: 1rem 的默认像素值(与配置里的默认值同源)。
BASE_FONT_PX = DEFAULT_BASE_FONT_PX
#: 设置窗口列出的可选基准字号。
FONT_CHOICES = BASE_FONT_CHOICES

# -- 字体族 ---------------------------------------------------------------
# ``CTkFont`` 默认取 customtkinter 主题里的 "Roboto", 而**那个字体只在 Windows 上真的装上了**:
# customtkinter 用 GDI 的 ``AddFontResourceEx`` 私有地注册它, 其余平台它的 ``FontManager`` 直接
# 返回 False(见 .venv 里 customtkinter 的 font_manager)。于是在 Linux/macOS 上我们一直在请求一个
# 不存在的族名, 画面长什么样全听系统代换。
#
# 实测 2026-10-02(CI 的 Linux 视觉回归截图): 代换落到只有**核心 X 字体**的 Tk 上 —— 拉丁字形
# 没有抗锯齿("锯齿"), 汉字一个都画不出来(整排按钮是空的, 看上去像"内容没加载完"), 而且**装
# 多少 TTF 都没用**(那份 Tk 根本不看 fontconfig/FreeType)。所以按平台各给一串**真的存在**的
# 候选名, 取系统报告的第一个命中项; 一个都不在就交回 ``None``(保持"听 Tk 的"旧行为)。
#
# 顺序不能随手改: Windows 上 "Roboto" 排第一是为了**不改现有画面**(customtkinter 自带它,
# 界面的字形与基线都是照它采的)。
FAMILY_PREFERENCES: dict[str, tuple[str, ...]] = {
    "win32": ("Roboto", "Microsoft YaHei UI", "Segoe UI"),
    "darwin": ("Roboto", "PingFang SC", "Hiragino Sans GB", "Helvetica Neue"),
    "linux": (
        "Noto Sans CJK SC",
        "Noto Sans SC",
        "Source Han Sans SC",
        "WenQuanYi Micro Hei",
        "Noto Sans",
        "DejaVu Sans",
    ),
}

_FAMILY: str | None = None
_FAMILY_RESOLVED = False

# -- 字号阶梯 ---------------------------------------------------------------
# 界面里的字号只许用这几档(px 口径, 会随"基准字号"等比缩放)。实测 2026-09-27: 173 处
# ``CTkFont(size=...)`` 里有 8 处落在阶梯外(14 与 16 混用), 按角色收敛 --
# **卡片/列表标题与按钮强调同档(13)**, 窗口与页面的标题才用 16。

#: 角标: 海报角上的备份数、状态点这类"数字/符号"。
FONT_TINY = 10
#: 次要说明、表头、提示行。
FONT_HINT = 11
#: 正文(默认)。
FONT_BODY = 12
#: 强调正文: 按钮、列表行标题、卡片标题。
FONT_STRONG = 13
#: 状态点、角标这类"靠字形大小决定视觉直径"的圆点(与强调正文同档)。
FONT_GLYPH = FONT_STRONG
#: 面板小标题(h2)。
FONT_SUBTITLE = 15
#: 窗口与页面标题。
FONT_TITLE = 16
#: 详情页滚到顶时的大号游戏名。
FONT_HERO = 20
#: 详情页标题。
FONT_PAGE_TITLE = 26
#: 海报/头像占位图里的那个大写字母。
FONT_PLACEHOLDER = 28

#: 阶梯的全部档位(守卫按这张表判 ``CTkFont(size=...)``).
FONT_SCALE = (
    FONT_TINY,
    FONT_HINT,
    FONT_BODY,
    FONT_STRONG,
    FONT_SUBTITLE,
    FONT_TITLE,
    FONT_HERO,
    FONT_PAGE_TITLE,
    FONT_PLACEHOLDER,
)


@dataclass(frozen=True)
class FontScale:
    """一次字号换算(基准字号 → 实际字号)."""

    base_px: int = DEFAULT_BASE_FONT_PX

    @property
    def ratio(self) -> float:
        """相对默认基准的倍数: 默认 16px 时正好是 1.0."""
        return self.base_px / DEFAULT_BASE_FONT_PX

    def px(self, value: float) -> int:
        """把"px 口径的字号"换算成实际字号(负数保持负数: Tk 用负值表示像素)."""
        scaled = round(value * self.ratio)
        return scaled if scaled < 0 else max(1, scaled)

    def rem(self, value: float) -> int:
        """把 rem 换算成实际字号(1rem = 基准字号)."""
        scaled = round(value * self.base_px)
        return max(1, scaled)


_SCALE = FontScale()
# 装上缩放之前的原始类(只换模块属性, 不动第三方包本身).
# customtkinter 没有类型标注, 因此这里的基类在 mypy 眼里就是 Any。
_ORIGINAL_FONT: Any = ctk.CTkFont


def current_scale() -> FontScale:
    """返回当前生效的字号换算."""
    return _SCALE


def set_base_font_px(value: int) -> FontScale:
    """设置基准字号(px), 返回生效的换算.

    取值会被夹进配置允许的区间 —— 这是最后一道保护: 传进来的值已经过配置校验,
    但代码里也可能直接调用, 夹一下比抛异常更适合"界面字号"这种纯外观参数。
    """
    global _SCALE
    lowest, highest = min(FONT_CHOICES), max(FONT_CHOICES)
    _SCALE = FontScale(base_px=min(max(int(value), lowest), highest))
    return _SCALE


def scaled(value: float) -> int:
    """把 px 口径的字号换算成当前实际字号."""
    return _SCALE.px(value)


def rem(value: float) -> int:
    """把 rem 换算成当前实际字号(1rem = 基准字号)."""
    return _SCALE.rem(value)


def font(
    size: float, *, weight: str = "normal", family: str | None = None
) -> ctk.CTkFont:
    """按当前基准字号创建一个字体(px 口径的 ``size``)."""
    return ctk.CTkFont(size=scaled(size), weight=weight, family=family)


def _platform_key(system: str | None = None) -> str:
    """把平台归到 :data:`FAMILY_PREFERENCES` 的三档之一(其余平台按 Linux 处理)."""
    value = sys.platform if system is None else system
    if value.startswith("win"):
        return "win32"
    if value.startswith("darwin"):
        return "darwin"
    return "linux"


def preferred_candidates(system: str | None = None) -> tuple[str, ...]:
    """本平台按优先级排好的字体族候选(纯函数, 不碰 Tk)."""
    return FAMILY_PREFERENCES[_platform_key(system)]


def installed_families() -> tuple[str, ...]:
    """Tk 报告的字体族(没有 Tk 根或取不到时给空表)."""
    try:
        from tkinter import font as tkfont

        return tuple(tkfont.families())
    except Exception:  # 没有 Tk 根(pytest 的纯单元用例)/Tcl 异常
        return ()


def resolves_to_itself(family: str) -> bool:
    """这个族名当前真的解析成一个**同名**字体吗(而不是被系统悄悄代换成别的)?

    为什么不能只看 ``font.families()``: Windows 上 customtkinter 用 ``AddFontResourceEx``
    **私有**注册它的 Roboto(FR_PRIVATE | FR_NOT_ENUM), 那个字体能画但**不进** ``families()``
    —— 只查列表会得出"Roboto 不存在"的错误结论, 于是 Windows 画面也会跟着变。
    """
    try:
        from tkinter import font as tkfont

        probe = tkfont.Font(family=family, size=10)
        resolved = str(probe.actual("family"))
        probe.__del__()
    except Exception:  # 没有 Tk 根 / Tcl 异常
        return False
    return resolved.casefold() == family.casefold()


def pick_family(
    candidates: Iterable[str], resolves: Callable[[str], bool]
) -> str | None:
    """按顺序取第一个**解析得到**的族名(纯函数: "能不能解析"由调用方给)."""
    for name in candidates:
        if resolves(name):
            return name
    return None


def current_family() -> str | None:
    """本进程该用的界面字体族(第一次问到就定下来; 没有 Tk 时返回 None 且不缓存).

    解析要在**有 Tk 根之后**才做得了, 所以是惰性的; 而字体在进程内不会变, 命中过一次就
    缓存 —— 界面里上百个字体对象都要问它。
    """
    global _FAMILY, _FAMILY_RESOLVED
    if _FAMILY_RESOLVED:
        return _FAMILY
    chosen = pick_family(preferred_candidates(), resolves_to_itself)
    if chosen is None and not installed_families():
        return None  # 还没有 Tk 根: 这不是环境的结论, 别缓存
    _FAMILY = chosen
    _FAMILY_RESOLVED = True
    return _FAMILY


def _family_for_request(family: str | None) -> str | None:
    """调用方指定了就用它, 没指定就用本平台挑出来的那个(挑不到就 None = 听 Tk 的)."""
    return family if family is not None else current_family()


class _ScaledFont(_ORIGINAL_FONT):  # type: ignore[misc]  # customtkinter 没有类型标注
    """按当前基准字号缩放的 ``CTkFont``(见 :func:`install_font_scaling`).

    另外把"释放 Tcl 字体"这一步**限制在主线程**上, 理由见 :meth:`__del__`。
    """

    def __init__(
        self,
        family: str | None = None,
        size: int | None = None,
        **kwargs: object,
    ) -> None:
        """与 ``CTkFont`` 同签名: 把 ``size`` 按基准字号换算一次, 并补上平台字体族.

        界面上百处 ``CTkFont(size=N)`` 都没写 family, 于是默认落到 customtkinter 主题里的
        "Roboto" —— 那个字体只在 Windows 上存在(见 :data:`FAMILY_PREFERENCES` 的说明)。
        """
        super().__init__(
            family=_family_for_request(family),
            size=None if size is None else scaled(size),
            **kwargs,
        )

    def _delete_tcl_font(self) -> None:
        """真正去调 Tcl 删掉这个字体(只允许在主线程调用)."""
        with contextlib.suppress(Exception):
            super().__del__()

    def __del__(self) -> None:
        r"""主线程上正常释放; 在别的线程上只"寄存", 等主线程来删.

        ``tkinter.font.Font.__del__`` 会调 Tcl(``font delete``), 而 **Tcl/Tk 只有创建
        解释器的那个线程能安全调用**。界面里有上百个字体对象, 它们随控件树成环, 因此由
        循环 GC 回收 —— 而 GC 可能跑在**任何一个正在分配的线程**上。后台 worker 线程
        只要触发一次回收, 就可能在那里终结一个字体对象, 于是:

            Fatal Python error: Segmentation fault
            Current thread ...: Garbage-collecting
              File ".../tkinter/font.py", line 121 in __del__      ← 在 worker 线程上调 Tcl
            主线程同时: _poll_messages → ... → CTkProgressBar.set → canvas.coords

        这是 2026-10-01 CI 上真实发生过的段错误(Linux 分片整片崩溃、覆盖率全丢), 而且是
        竞态 —— 本机复现不出来。所以这里换成"谁都不能在非主线程上碰 Tcl":

        - 主线程: 照旧立刻释放;
        - 其它线程: 把对象**复活**(挂进 :data:`_DEFERRED_FONTS`, ``__del__`` 里重新变成
          可达对象就不会被回收), 交给主线程的 :func:`drain_deferred_fonts` 统一删除。

        代价只是"释放晚一点"; 收益是**不可能**再从 worker 线程调 Tcl。
        """
        if threading.current_thread() is threading.main_thread():
            self._delete_tcl_font()
            return
        # 解释器收尾时模块全局可能已经被清掉, 那时 `append` 会抛 NameError —— 兜住它,
        # 免得 GC 里再冒一句 "Exception ignored in: __del__"。
        with contextlib.suppress(Exception):
            _DEFERRED_FONTS.append(self)


#: 被后台线程"寄存"的字体: ``_ScaledFont.__del__`` 在非主线程上不释放 Tcl 字体, 而是把
#: 对象挂到这里等主线程来删(它们**本来就会被循环 GC 回收**, 挂住只是把释放推迟到主线程)。
_DEFERRED_FONTS: list[_ScaledFont] = []


def drain_deferred_fonts() -> int:
    """在主线程上删掉被后台线程"寄存"的字体, 返回删掉了几个.

    由主窗口的消息泵(:meth:`~archive_management.ui.main_window.ArchiveApp._poll_messages`)
    与测试的收尾各调一次 —— 它们本来就在主线程上跑, 不需要额外的事件源。
    """
    pending, _DEFERRED_FONTS[:] = list(_DEFERRED_FONTS), []
    for font in pending:
        font._delete_tcl_font()
    return len(pending)


def deferred_font_count() -> int:
    """当前寄存了多少个待删字体(给守卫看状态, 不参与界面逻辑)."""
    return len(_DEFERRED_FONTS)


def install_font_scaling() -> None:
    """把字号缩放装到 ``ctk.CTkFont`` 上(幂等).

    界面里有上百处 ``CTkFont(size=N)`` 直接构造字体, 逐个改既容易漏也不值得;
    这里只替换 ``customtkinter`` 模块上的类属性, 之后创建的字体都会经过
    :func:`scaled` —— 与 :func:`archive_management.i18n.set_locale` 一样是
    "进程内全局", 由主窗口在构造界面之前调用一次。
    """
    if ctk.CTkFont is _ScaledFont:  # pragma: no cover - 只有重复调用才会走到
        return
    ctk.CTkFont = _ScaledFont


__all__ = [
    "BASE_FONT_PX",
    "FAMILY_PREFERENCES",
    "FONT_CHOICES",
    "FontScale",
    "current_family",
    "current_scale",
    "deferred_font_count",
    "drain_deferred_fonts",
    "font",
    "install_font_scaling",
    "installed_families",
    "pick_family",
    "preferred_candidates",
    "rem",
    "resolves_to_itself",
    "scaled",
    "set_base_font_px",
]
