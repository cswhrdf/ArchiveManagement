"""失败现场留证: coredumpy dump + 界面截图 → 当前用例的 Allure 附件.

有些失败(**尤其是 GUI 与并发场景**)只在 CI、只在某个平台、只在某个时序下出现: 本地反复跑
都不复现, 事后手里只剩一行 traceback 和一段日志。这里在**用例失败的那一刻**把现场留下来:

1. **崩溃现场 dump**(`coredumpy <https://github.com/gaogaotiantian/coredumpy>`_): 把最深一层
   栈帧的局部变量与对象属性写成 JSON, 事后 ``coredumpy load <文件>``(或 VSCode 的 coredumpy
   扩展)就能像断在现场一样翻当时的变量 —— 这是"难以复现的问题"最缺的东西。它在报告里按
   :data:`DUMP_MEDIA_TYPE` 的媒体类型挂上去: Allure 对不认识的类型**只给下载链接、不渲染
   预览**, 因此打开报告不会因为把上兆字节的 JSON 读进预览区而卡死;
2. **界面截图**: 本用例创建的窗口(``tests/gui_support.py`` 的登记表)在失败时的画面, 直接贴进
   报告的当前用例 —— "界面画歪了/被裁了/卡住了"这类问题, 一行文字说不过一张图。Windows 按
   窗口句柄抓(被别的窗口盖住也拍得到, 详见 :func:`_grab_screen`), 其它平台按屏幕区域抓;
3. **失败现场摘要**: 平台/Python/提交号 + dump 与截图的落点 + 复现命令, 让报告里不只有一堆
   附件, 还能回答"这是在什么环境上跑的、怎么把现场调出来"。

三条硬约束:

- **绝不改变用例结果**: 留证是附加动作, 任何一步出错都只变成一句话(附件里或日志里) ——
  留证失败既不能把通过的用例变红, 也不能盖掉原本的失败原因;
- **失败才留证**: 通过的用例不产生任何文件(没人会去看通过用例的现场);
- **有上限**: 递归深度可控(默认 :data:`DEFAULT_DEPTH`)、单次 dump 有时限
  (:data:`DUMP_TIMEOUT_SECONDS`, coredumpy 默认 60s 对 CI 太长)、超过 :data:`MAX_DUMP_BYTES`
  的 dump 不挂进报告(只写落点与大小), 截图最多 :data:`MAX_SCREENSHOTS` 张。

dump 里是**真实的局部变量**, 可能带上用户路径与配置内容: coredumpy 默认会遮掉看起来像密钥的
字符串与 ``os.environ`` 里的值(见其 ``config.hide_secret`` / ``config.hide_environ``)。往公开
仓库传报告前请确认附件里没有不该公开的东西。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType, TracebackType
from typing import Any

import allure
import pytest
from allure_commons.types import AttachmentType

# 现场文件的落点: 仓库根下的独立目录(已在 .gitignore 里), 便于本地 `coredumpy load`。
DUMP_DIRECTORY = Path("crash-dumps")
DUMP_EXTENSION = ".dump"
# dump 附件的媒体类型: 用 Allure **不认识**的二进制类型, 报告里就只给一个下载链接,
# 不会再开一块预览区。coredumpy 的 dump 是纯文本(JSON), 以前按 ``text/plain`` 挂,
# Allure 会把整份文件读进来渲染 —— 实测上兆字节的 dump 一打开就把页面卡死。
DUMP_MEDIA_TYPE = "application/octet-stream"
# 递归深度: 5 层足够看清"谁带着什么参数调到了这里"; 再深就开始翻 GUI 控件与数据库连接的对象图,
# 又慢又大(实测 Tk 窗口的失败栈在 depth=5 时 dump 约 1 MiB)。
DEFAULT_DEPTH = 5
# coredumpy 自己的兜底时限(默认 60s)。留证是在失败之后跑的, 不能让一次 dump 拖住整个套件。
DUMP_TIMEOUT_SECONDS = 20
# 超过这个大小的 dump 不挂进报告(报告会作为 artifact 发布与下载)。
MAX_DUMP_BYTES = 25 * 1024 * 1024
# 一次失败最多截几张窗口(一个用例建多个窗口时, 前几张通常已经够看)。
MAX_SCREENSHOTS = 3
# 文件名长度上限(过长在 Windows 上会踩路径长度限制)。
MAX_FILE_NAME = 120
# 一个用例只留一次现场: 失败后 teardown 常常跟着再报一次错, 第二次没有新信息, 只会让附件翻倍。
CAPTURED = pytest.StashKey[bool]()

# 文件名里只保留这些字符, 其余压成下划线(避免路径分隔符/冒号/参数里的怪异字符)。
_UNSAFE = re.compile(r"[^0-9A-Za-z._-]+")
Box = tuple[int, int, int, int]
# (窗口, 窗口在屏幕上的范围) -> PIL 图像: 注入这个替身就能在不建真窗口的情况下测截图。
Grabber = Callable[[Any, Box], Any]


@dataclass(frozen=True)
class Evidence:
    """一条待挂到当前用例报告上的现场证据.

    ``attachment_type`` 可以是 :class:`AttachmentType` 枚举, 也可以直接给一个媒体类型
    字符串(如 :data:`DUMP_MEDIA_TYPE`) —— allure 拿到字符串就原样写进结果的 ``type``,
    因此可以避开它认识的那几种类型, 只要一个下载链接。
    """

    name: str
    attachment_type: AttachmentType | str
    body: bytes | None = None
    path: Path | None = None


@dataclass(frozen=True)
class Shot:
    """一次截图的尝试结果: 成功给出 PNG 字节, 失败给出原因."""

    title: str
    png: bytes | None
    note: str


def dump_target(node_id: str, *, directory: Path) -> Path:
    """给用例算一个稳定、可读、能直接 ``coredumpy load`` 的 dump 文件名.

    同名文件会被覆盖: 同一条用例反复失败时只留最新的一次现场, 不会越攒越多。
    """
    safe = _UNSAFE.sub("_", node_id.replace("::", "__")).strip("_")
    if len(safe) > MAX_FILE_NAME:
        digest = hashlib.sha256(node_id.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe[: MAX_FILE_NAME - 9]}_{digest}"
    return directory / f"{safe}{DUMP_EXTENSION}"


def deepest_frame(traceback: TracebackType | None) -> FrameType | None:
    """取最深一层栈帧(异常真正抛出的位置); 没有 traceback 时返回 None."""
    if traceback is None:
        return None
    while traceback.tb_next is not None:
        traceback = traceback.tb_next
    return traceback.tb_frame


def exception_text(excinfo: Any) -> str:
    """把异常信息压成一行(``类型: 说明``); 拿不到异常时返回空串."""
    value = getattr(excinfo, "value", None)
    if value is None:
        return ""
    return f"{type(value).__name__}: {value}"


def sqlite_error_details(excinfo: Any) -> str:
    """SQLite 异常的**错误分类**(``SQLITE_NOTADB (26)``); 不是 SQLite 异常时返回空串.

    为什么要单独把分类掳出来: ``OperationalError`` 这个类名对"文件不是数据库"(NOTADB)、
    "打不开"(CANTOPEN)、"库被锁住"(BUSY)、"没有这张表"(ERROR) 是**一模一样**的, 而四者的
    处理方向完全不同。更坑的是消息文本会随平台/版本变 —— ``file is not a database`` 与
    ``unsupported file format`` 其实是同一类(NOTADB), 光看文本会以为是两件事。
    只有 ``sqlite_errorname`` / ``sqlite_errorcode``(Python 3.11+ 提供)是稳定的。

    2026-09-30 那次偶发就是靠它定类的: 一条 GUI 用例在整轮全量里红了一次(本地单跑不复现),
    dump 里写着 ``OperationalError: unsupported file format`` + 临时库路径, 于是能立刻把它归成
    "临时文件在那一刻被读成非 SQLite 内容"而不是"时序/等得不够"。
    """
    import sqlite3

    value = getattr(excinfo, "value", None)
    if not isinstance(value, sqlite3.Error):
        return ""
    name = str(getattr(value, "sqlite_errorname", "") or type(value).__name__)
    code = getattr(value, "sqlite_errorcode", None)
    return f"{name} ({code})" if code is not None else name


def write_dump(
    *,
    node_id: str,
    frame: FrameType | None,
    description: str,
    directory: Path,
    depth: int,
) -> tuple[Path | None, str]:
    """把崩溃现场写成 dump 文件, 返回 (文件路径, 人可读结论).

    留证失败不影响用例: ``import`` 失败(没装开发依赖)、写盘失败、coredumpy 自己出错, 都只
    变成一句说明。
    """
    if depth <= 0:
        return None, "已关闭(递归深度为 0)"
    target = dump_target(node_id, directory=directory)
    try:
        import coredumpy

        coredumpy.config.dump_timeout = DUMP_TIMEOUT_SECONDS
        written = coredumpy.dump(
            frame=frame, description=description, depth=depth, path=str(target)
        )
    except Exception as exc:  # 留证是附加动作: 出错只记录, 不能盖掉真正的失败
        return None, f"生成失败: {type(exc).__name__}: {exc}"
    path = Path(str(written))
    if not path.is_file():
        return None, f"生成失败: 没有写出文件({path})"
    return path, f"已生成({_size_text(path.stat().st_size)})"


def window_box(window: Any) -> Box | None:
    """窗口在屏幕上的范围 ``(left, top, right, bottom)``; 未映射/未布局/已销毁时返回 None.

    未布局(宽或高 ≤ 1)时截图只会得到一条缝, 所以按"拿不到"处理。
    """
    from tkinter import TclError

    try:
        if not window.winfo_ismapped():
            return None
        width = int(window.winfo_width())
        height = int(window.winfo_height())
        if width <= 1 or height <= 1:
            return None
        left = int(window.winfo_rootx())
        top = int(window.winfo_rooty())
    except (TclError, AttributeError, TypeError, ValueError):
        return None
    return (left, top, left + width, top + height)


def grab_png(
    window: Any, box: Box, *, grabber: Grabber | None = None
) -> tuple[bytes | None, str]:
    """把窗口拍下来, 返回 (PNG 字节, 说明); 拍不到时字节为 None.

    说明里带实际像素尺寸 —— 抓到的是窗口还是屏幕区域, 从尺寸上就能看出来。
    """
    try:
        image = (grabber or _grab_screen)(window, box)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        width, height = image.size
    except Exception as exc:  # 截图失败不改变用例结果, 只记原因
        return None, f"截图失败: {type(exc).__name__}: {exc}"
    return buffer.getvalue(), f"已抓取 {width}x{height}"


def _grab_screen(window: Any, box: Box) -> Any:
    """按平台抓屏: Windows 优先按**窗口句柄**抓, 否则按屏幕区域抓.

    Windows 的 ``ImageGrab.grab(window=...)``(PrintWindow) 只用窗口自己就可以拍, 因此:

    - 窗口被别的窗口盖住(全屏游戏/多个用例窗口叠起来)也能拍到;
    - 不受显示缩放的干扰 —— Tk 报的是逻辑坐标, 按屏幕区域抓拿到的是物理像素,
      缩放不是 100% 时区域与窗口会错位(本机实测确实如此);

    Linux 需要 X 显示(CI 由 xvfb 提供), macOS 需要屏幕录制权限 —— 这两个平台只能按
    屏幕区域抓, 失败时由调用方记成附件里的一句话。
    """
    from PIL import ImageGrab

    handle = _window_handle(window)
    if handle is not None:
        try:
            return ImageGrab.grab(window=handle)
        except Exception:  # 个别窗口/驱动不支持 PrintWindow, 退回按屏幕区域抓
            handle = None
    if sys.platform.startswith("linux"):
        return ImageGrab.grab(bbox=box, xdisplay=os.environ.get("DISPLAY") or None)
    return ImageGrab.grab(bbox=box)


def _window_handle(window: Any) -> int | None:
    """窗口的原生句柄(只在 Windows 上有意义); 取不到时返回 None.

    先用 ``wm_frame()``(整个顶层窗口), 拿不到再用 ``winfo_id()``。
    """
    if not sys.platform.startswith("win"):
        return None
    for value in (_wm_frame(window), _widget_id(window)):
        if value:
            try:
                return int(value, 16)
            except ValueError:
                continue
    return None


def _wm_frame(window: Any) -> str:
    """顶层窗口的窗口管理器 id(十六进制字符串); 取不到时返回空串."""
    try:
        return str(window.wm_frame())
    except Exception:
        return ""


def _widget_id(window: Any) -> str:
    """控件自身的窗口 id(十六进制字符串); 取不到时返回空串."""
    try:
        return hex(int(window.winfo_id()))
    except Exception:
        return ""


def window_title(window: Any) -> str:
    """窗口标题; 取不到时用占位文字(标题只影响附件名, 不该影响留证)."""
    try:
        title = str(window.title()).strip()
    except Exception:  # 标题取不到不影响截图本身
        return "窗口"
    return title or "窗口"


def screenshots(
    apps: Sequence[Any],
    *,
    grabber: Grabber | None = None,
    limit: int = MAX_SCREENSHOTS,
) -> list[Shot]:
    """给本用例创建的窗口逐个截图, 返回每张的结果(含跳过原因).

    每个窗口单独兜底: 某个窗口状态怪异(几何取不到、驱动抓不动)只影响它自己,
    后面的窗口照拍 —— 现场能留一张是一张。
    """
    shots: list[Shot] = []
    for window in apps[:limit]:
        title = window_title(window)
        try:
            box = window_box(window)
            if box is None:
                shots.append(Shot(title=title, png=None, note="窗口未显示或已销毁"))
                continue
            png, note = grab_png(window, box, grabber=grabber)
        except Exception as exc:  # 单个窗口出问题不影响其它窗口与用例结果
            shots.append(
                Shot(
                    title=title, png=None, note=f"截图失败: {type(exc).__name__}: {exc}"
                )
            )
            continue
        shots.append(Shot(title=title, png=png, note=note))
    return shots


def failure_evidence(
    *,
    node_id: str,
    phase: str,
    excinfo: Any,
    directory: Path,
    depth: int,
    apps: Sequence[Any] = (),
) -> list[Evidence]:
    """整理失败现场: dump 文件 + 界面截图 + 摘要(纯决策, 不碰报告, 便于单测).

    dump 文件本身会落在 ``directory`` 下 —— 摘要与附件都要引用它的路径。
    """
    description = f"{node_id} [{phase}]\n{exception_text(excinfo)}"
    sqlite_details = sqlite_error_details(excinfo)
    if sqlite_details:
        # 也写进 dump 的描述: 本地跑不带 ``--alluredir`` 时, dump 是**唯一**留下来的现场。
        description = f"{description}\nSQLite: {sqlite_details}"
    dump_path, dump_note = write_dump(
        node_id=node_id,
        frame=deepest_frame(getattr(excinfo, "tb", None)),
        description=description,
        directory=directory,
        depth=depth,
    )
    shots = screenshots(apps) if apps else []
    evidence = [_summary(node_id, phase, excinfo, dump_path, dump_note, shots)]
    if dump_path is not None:
        size = dump_path.stat().st_size
        if size <= MAX_DUMP_BYTES:
            evidence.append(
                Evidence(
                    name=f"崩溃现场 dump · {dump_path.name}",
                    attachment_type=DUMP_MEDIA_TYPE,
                    path=dump_path,
                )
            )
        else:
            evidence.append(
                _text(
                    "崩溃现场 dump 未挂载",
                    f"{dump_path} 有 {_size_text(size)}, 超过 {_size_text(MAX_DUMP_BYTES)} 上限, "
                    "只在摘要里留了落点。",
                )
            )
    evidence.extend(
        Evidence(
            name=f"失败时界面截图 · {shot.title}",
            attachment_type=allure.attachment_type.PNG,
            body=shot.png,
        )
        for shot in shots
        if shot.png is not None
    )
    return evidence


def attach(evidence: Sequence[Evidence]) -> None:
    """把证据挂到当前用例的 Allure 报告上(大文件走 ``attach.file``, 不读进内存)."""
    for item in evidence:
        if item.path is not None:
            # 给 media type 是字符串的附件补上真实后缀: allure 只在拿到枚举时才从枚举里
            # 推后缀, 否则会把文件存成 ``<uuid>-attachment.attach``, 下载下来是个没后缀的
            # 文件(``coredumpy load`` 还得自己改名).
            suffix = item.path.suffix.lstrip(".")
            # allure 没给 attach.file 标注类型(attach 有), 但大文件只能走它: 不读进内存。
            allure.attach.file(  # type: ignore[no-untyped-call]
                str(item.path),
                name=item.name,
                attachment_type=item.attachment_type,
                extension=suffix or None,
            )
        else:
            allure.attach(
                item.body or b"", name=item.name, attachment_type=item.attachment_type
            )


def attach_failure_evidence(
    item: pytest.Item,
    call: pytest.CallInfo[Any],
    *,
    directory: Path,
    depth: int,
    apps: Sequence[Any] = (),
) -> list[Evidence]:
    """编排: 用例失败时留证并挂到它的报告上; 同一个用例只留一次.

    整段包在 try 里: 留证是**附加动作**, 它自己出错(第三方库抽风、附件写不进去)绝不能让
    pytest 报成内部错误 —— 那会把真正的失败原因彻底盖掉。出错只在日志里留一行。
    """
    if item.stash.get(CAPTURED, False):
        return []
    item.stash[CAPTURED] = True
    try:
        evidence = failure_evidence(
            node_id=item.nodeid,
            phase=call.when,
            excinfo=call.excinfo,
            directory=directory,
            depth=depth,
            apps=apps,
        )
        attach(evidence)
    except Exception as exc:  # 留证失败不能改变用例结果, 更不能盖掉真正的失败
        print(f"[crash-capture] 留证失败, 已跳过: {type(exc).__name__}: {exc}")
        return []
    return evidence


def _text(name: str, content: str) -> Evidence:
    """一条文本附件(UTF-8 字节, 保持与二进制附件同一条通路)."""
    return Evidence(
        name=name,
        attachment_type=allure.attachment_type.TEXT,
        body=content.encode("utf-8"),
    )


def _summary(
    node_id: str,
    phase: str,
    excinfo: Any,
    dump_path: Path | None,
    dump_note: str,
    shots: Sequence[Shot],
) -> Evidence:
    """写"失败现场摘要": 环境 + 留证落点 + 复现命令."""
    from helpers import environment_info

    environment = environment_info()
    lines = [
        "# 失败现场",
        "",
        f"- 用例: `{node_id}`",
        f"- 阶段: {phase}",
        f"- 异常: {exception_text(excinfo) or '(无)'}",
    ]
    sqlite_details = sqlite_error_details(excinfo)
    if sqlite_details:
        lines.append(f"- SQLite 错误分类: {sqlite_details}")
    lines += [
        f"- 环境: {environment['os']} · Python {environment['python']} · 提交 {environment['commit']}",
        f'- 复现: `uv run pytest "{node_id}" -q`',
        "",
        "## 留证",
        "",
    ]
    if dump_path is not None:
        lines += [
            f"- 崩溃现场 dump: {dump_path} {dump_note}",
            f"  - 打开方式: `coredumpy load {dump_path}` 或在 VSCode 里用 coredumpy 扩展右键"
            "「Load with coredumpy」",
        ]
    else:
        lines.append(f"- 崩溃现场 dump: 未生成({dump_note})")
    if shots:
        for shot in shots:
            state = "已附在下面" if shot.png is not None else shot.note
            lines.append(f"- 界面截图「{shot.title}」: {state}")
    else:
        lines.append("- 界面截图: 本用例没有创建界面窗口, 无现场可截")
    return _text("失败现场摘要", "\n".join(lines) + "\n")


def _size_text(size: int) -> str:
    """人类可读的大小(留证目录里最大也就几十 MiB, 不做 TB 级换算)."""
    return (
        f"{size / 1024 / 1024:.1f} MiB"
        if size >= 1024 * 1024
        else f"{size / 1024:.0f} KiB"
    )
