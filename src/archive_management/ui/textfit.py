"""按像素宽度把文本压进指定的宽度与行数(放不下时补省略号).

Tk 的 Label 没有省略号模式: 文本比控件宽时, 要么把容器撑大 —— 表格里某一行的
名称列变宽后与表头错位, 滚动区还会整体横向溢出 —— 要么被直接裁掉, 后面的字
看不到。这里用字体测量**真实像素宽度**, 把文本压进给定的宽度与行数内。

按显示像素算(而不是按字符数)才能同时照顾中英文混排: 13px 下 10 个汉字约
140px, 而同样字符数的英文只有一半宽。

测量只依赖 ``font.measure(text)``: ``CTkFont`` 本身就是 Tk 字体对象, 直接传即可;
用例里可以传只实现 ``measure`` 的替身, 不需要真的建窗口。
"""

from __future__ import annotations

from typing import Protocol

# 省略号用单个字符(U+2026): 比 "..." 窄, 也不会在折行时被拆开。
ELLIPSIS = "…"


class MeasurableFont(Protocol):
    """能测量文本像素宽度的字体(``CTkFont`` 与 ``tkinter.font.Font`` 都满足)."""

    def measure(self, text: str) -> int:
        """返回文本在当前字体下的像素宽度."""
        ...


def _longest_prefix(text: str, font: MeasurableFont, width: int) -> int:
    """二分找出能放进 ``width`` 的最长前缀长度(一个字符都放不下时为 0)."""
    low, high, best = 1, len(text), 0
    while low <= high:
        middle = (low + high) // 2
        if font.measure(text[:middle]) <= width:
            best, low = middle, middle + 1
        else:
            high = middle - 1
    return best


def _head_length(text: str, font: MeasurableFont, width: int) -> int:
    """返回本行能放的字符数: 优先在空格处断行, 超长的单词才硬切."""
    if font.measure(text) <= width:
        return len(text)
    limit = _longest_prefix(text, font, width)
    if limit <= 0:
        return 0
    if limit >= len(text):
        return limit
    # 只在"词中间"才退回空格处: 若正好停在空格边界, 直接切更自然(也不丢字)。
    space = text[:limit].rfind(" ")
    if space > 0 and not text[limit].isspace():
        return space
    return limit


def _tail(text: str, font: MeasurableFont, width: int) -> str:
    """最后一行的内容: 放不下时尽量多留字符并补省略号."""
    if font.measure(text) <= width:
        return text
    if font.measure(ELLIPSIS) > width:  # 极窄的容器: 连省略号都放不下
        return ""
    limit = _longest_prefix(text, font, width - font.measure(ELLIPSIS))
    return ELLIPSIS if limit <= 0 else f"{text[:limit].rstrip()}{ELLIPSIS}"


def fit_text(text: str, font: MeasurableFont, width: int, *, max_lines: int = 1) -> str:
    """把 ``text`` 压进 ``max_lines`` 行、每行不超过 ``width`` 像素.

    放得下时**原样返回**(不引入多余的换行); 放不下时逐行填满, 最后一行用省略号
    收尾。返回值的每一行都能放进 ``width``, 因此调用方可以再把同一个宽度传给
    Label 的 ``wraplength`` 作为硬上限 —— 即使这里算得偏乐观, Tk 也只会折行,
    不会把容器撑宽。
    """
    if max_lines < 1 or width <= 0 or not text:
        return text
    if font.measure(text) <= width:
        return text
    lines: list[str] = []
    remaining = text.strip()
    while remaining:
        if len(lines) == max_lines - 1:
            lines.append(_tail(remaining, font, width))
            break
        limit = _head_length(remaining, font, width)
        if limit <= 0:  # 宽度连一个字符都容不下, 交给上层决定
            break
        lines.append(remaining[:limit].rstrip())
        remaining = remaining[limit:].lstrip()
    return "\n".join(line for line in lines if line)
