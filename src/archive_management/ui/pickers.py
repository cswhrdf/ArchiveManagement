"""系统文件/目录选择封装.

仅在用户点击"浏览"等按钮时弹出系统对话框; 独立成模块便于在无头
单元测试中用假实现替换, 避免依赖真实图形环境.
"""

from __future__ import annotations

from pathlib import Path
from tkinter import filedialog


def resolve_start_dir(text: str) -> str | None:
    """把路径输入框里的自由文本解析成选择框的打开位置; 解析不出返回 None.

    站在用户角度是"我输的地址能走到多近就走到多近":

    - 空白或相对路径返回 None —— 本应用的路径输入框语义是绝对路径, 相对路径
      按进程工作目录解析只会指到意料之外的地方, 不如交给系统对话框用它自己
      的默认位置(Tk 会沿用本进程上一次选过的目录);
    - 展开 ``~`` 后是已存在的目录就是它; 是已存在的文件取它所在目录;
    - 本身不存在(常见于"打算新建"的路径)则沿祖先往上找第一个存在的目录,
      能落到父目录也算"访问到了";
    - 连一个存在的祖先都没有(拼错的盘符、纯乱码)返回 None, 走系统默认。
    """
    stripped = text.strip()
    if not stripped:
        return None
    candidate = Path(stripped).expanduser()
    if not candidate.is_absolute():
        return None
    if candidate.is_dir():
        return str(candidate)
    for ancestor in candidate.parents:
        if ancestor.is_dir():
            return str(ancestor)
    return None


def pick_directory(*, title: str, start_from: str = "") -> str | None:
    """弹出系统目录选择框; 用户取消时返回 None.

    ``start_from`` 是路径输入框里的现有内容(可为空): 经 ``resolve_start_dir``
    能解析出目录时选择框直接在那个位置打开; 解析不出时不传位置, 由系统
    对话框用它自己的默认 —— 用户已输入的地址优先, 访问不到再退默认。
    """
    start_dir = resolve_start_dir(start_from)
    if start_dir is None:
        chosen = filedialog.askdirectory(title=title)
    else:
        chosen = filedialog.askdirectory(title=title, initialdir=start_dir)
    return chosen or None


def pick_file(*, title: str, start_from: str = "") -> str | None:
    """弹出系统文件选择框; 用户取消时返回 None.

    ``start_from`` 同 ``pick_directory``; 此外输入的正好指向一个文件(已存在,
    或父目录存在而只差文件名)时, 把文件名作为 ``initialfile`` 一并预选/预填
    —— 已存在的文件直接选中, 只差名字的路径预填后回车即得。
    """
    start_dir = resolve_start_dir(start_from)
    if start_dir is None:
        chosen = filedialog.askopenfilename(title=title)
        return chosen or None
    candidate = Path(start_from.strip()).expanduser()
    # 只差最后一级时才预填文件名: 再深就没法猜用户要的是哪一级, 只开目录。
    if candidate.is_absolute() and str(candidate.parent) == start_dir:
        chosen = filedialog.askopenfilename(
            title=title, initialdir=start_dir, initialfile=candidate.name
        )
    else:
        chosen = filedialog.askopenfilename(title=title, initialdir=start_dir)
    return chosen or None


def pick_save_file(*, title: str, initialfile: str | None = None) -> str | None:
    """弹出系统保存文件对话框; 用户取消时返回 None.

    ``initialfile`` 是预填的文件名(通常是按游戏名派生的目录/文件名), 用户仍可
    改成别的名字或目录; 传 None 或空串表示不预填。
    """
    chosen = filedialog.asksaveasfilename(title=title, initialfile=initialfile or "")
    return chosen or None
