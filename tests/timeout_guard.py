"""超时也要留下现场: 线程栈 + 覆盖率 + coredumpy dump + 一条 Allure 结论.

**为什么需要它**: pytest-timeout 的 ``thread`` 方法在超时后**直接 ``os._exit(1)``**
(它自己的 docstring 就写着 "Dump stack of threads and call os._exit()"), 而 ``os._exit``
跳过整条 ``atexit`` 链 —— 于是正好丢掉两样最想要的东西:

1. **覆盖率数据**: coverage 是进程收尾时统一落盘的, 于是那一片"完全没有数据", 下游只能
   报"缺片", 从报告里根本看不出它是被超时杀死的(2026-10-01 实测, 见 PLAN §20);
2. **失败现场**: :mod:`crash_capture` 的留证挂在 ``pytest_runtest_makereport`` 上, 而
   ``os._exit`` 让 pytest 没机会产出那份 report —— **最需要现场的那类失败(卡死/挂住),
   恰恰一点现场都没有**, ``coredumpy load`` 无从下手。

所以这里借 pytest-timeout 自己留的扩展点接替 ``thread`` 模式:
``pytest_timeout_set_timer`` 是 ``firstresult`` 钩子, 官方 docstring 明说
"Can be overridden by plugins for alternative timeout implementation strategies"。
接管后的顺序是::

    倒出用例至今的输出/日志(上游的 ``timeout_timer`` 也是先做这件事)
      → 打印各线程栈
      → 存覆盖率(与 pytest-cov 自己在会话结束时做的是同一件事)
      → 抓"卡住的那一帧"的 coredumpy dump
      → 写一条 Allure 结论(栈与 dump 都作为它的附件)
      → 冲干净三条输出通道, 最后才 os._exit(1)

**哪些情况不接管**: ``signal`` 模式不动(它抛异常, 本来就走正常收尾, 覆盖率与留证都不缺);
调试器里不设计时器(与上游一致)。也就是说这个模块在 Linux/macOS 上是"备而不接", 只在真有
``--timeout-method=thread`` 生效时才起作用。

**留证本身绝不能成为新的故障源**: 每一步都各自兜住异常, 任何一步失败都只变成结论里的一行
说明, 然后照样 ``os._exit(1)`` —— 超时了就必须退出, 不能因为"想写证据"把测试挂死在那儿。
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
import traceback
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

import crash_capture
from archive_management.services.platforms import current_platform, platform_label

#: 结论项的标题前缀与分类标签(挂进报告的那条结果长什么样, 见 :func:`write_allure_result`).
RESULT_TITLE = "超时留证"
FEATURE = "超时留证"
#: 结论项的状态: 用 ``broken`` 而不是 ``failed`` —— 它不是"断言没过", 是"这条用例没能跑完",
#: 与仓库里"环境损坏"的语义一致(allurerc.mjs 的默认分类会把 broken 归到 Test errors)。
RESULT_STATUS = "broken"
#: dump 超过这个大小就不挂进报告(与 crash_capture 同一条线): 报告是要上传下载的产物。
MAX_ATTACHMENT_BYTES = crash_capture.MAX_DUMP_BYTES

#: pytest-cov 把它那个持有 ``cov_controller`` 的插件注册在私有名字 ``_cov`` 上。
#: 顺序里带上公开写法只是为了以后它改名时不至于静默失效。
_COV_PLUGIN_NAMES = ("_cov", "cov", "pytest_cov")


def set_timer(item: pytest.Item, settings: Any) -> bool | None:
    """接替 pytest-timeout 的计时器(**只在 ``thread`` 模式**).

    返回 ``None`` 表示"这次不接管", ``firstresult`` 会继续问下一个实现(pytest-timeout
    自带的那个) —— 这一点很重要: ``signal`` 模式必须原样交给它, 否则我们就把"能正常
    收尾的那种超时"也换成了 ``os._exit``。
    """
    if _resolved_method(settings) != "thread":
        return None
    with contextlib.suppress(Exception):
        import pytest_timeout

        if not getattr(settings, "disable_debugger_detection", False) and (
            pytest_timeout.is_debugging()
        ):
            return True  # 调试中不设计时器(与上游一致)
    timer = threading.Timer(settings.timeout, on_timeout, (item, settings))
    timer.name = f"timeout_guard {item.nodeid}"
    timer.daemon = True

    def cancel() -> None:
        timer.cancel()
        timer.join()

    # 取消契约: pytest-timeout 的 ``cancel_timer`` 就是调这个属性, 而且上游自己的 ``cancel``
    # 也正好是这两行(`Item` 上没有这个属性, 上游也是这么挂的)。
    item.cancel_timeout = cancel  # type: ignore[attr-defined]
    timer.start()
    return True


def _resolved_method(settings: Any) -> str:
    """上游自己那一套"信号到底能不能用"的判断.

    ``--timeout-method=signal`` 在**非主线程**上收不到 SIGALRM, 上游那里会回退成线程计时器
    (于是又变成 ``os._exit``)—— 所以那种情况同样得接管, 否则又回到"什么都没有"。
    """
    method = str(getattr(settings, "method", ""))
    if method == "signal" and threading.current_thread() is not threading.main_thread():
        return "thread"
    return method


def cancel_timer(item: pytest.Item) -> bool:
    """取消上面那个计时器(契约与 pytest-timeout 自己的实现一致)."""
    cancel = getattr(item, "cancel_timeout", None)
    if cancel:
        cancel()
    return True


def on_timeout(item: pytest.Item, settings: Any) -> None:
    """超时处理: 先把现场落盘, 再退出(**不返回**).

    两条硬约束: ① 无论如何都要以退出码 1 结束 —— 超时意味着这一片已经不可信;
    ② 留证失败不许把它变成"挂在那里不动"(那才是最坏的结果)。
    """
    try:
        preserve_scene(item, settings)
    except Exception as exc:  # 留证自己炸了也不改变结局: 照样以 1 退出
        print(f"[超时留证] 留证失败: {type(exc).__name__}: {exc}", flush=True)
    finally:
        _flush_all(item)
        os._exit(1)


def _flush_all(item: pytest.Item) -> None:
    """退出前把三条输出通道都冲干净(照拄 pytest-timeout 的收尾).

    ``os._exit`` 不会帮我们冲缓冲区 —— 少冲一次, 那句"为什么死"的提示就可能留在缓冲区里。
    """
    with contextlib.suppress(Exception):
        item.config.get_terminal_writer().flush()
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(Exception):
            stream.flush()


def preserve_scene(item: pytest.Item, settings: Any) -> None:
    """把这一刻能留下的东西全部写出来(每一步都各自兜住异常)."""
    timeout = getattr(settings, "timeout", None)
    head = f"用例 {item.nodeid} 超过 {timeout}s 仍未结束"
    say = _reporter(item)
    say(f"\n{head} —— 先把现场落盘, 再退出。")
    # 先把用例到此刻为止的输出/日志倒出来(上游的 ``timeout_timer`` 也是先做这件事):
    # 它们正躺在 pytest 的捕获里, 而 ``os._exit`` 让收尾不跑 —— "卡死之前程序自己打了什么"
    # 往往就是最值钱的那行证据。
    dump_captured_output(item, say)
    stacks = _safe_text(thread_stacks, "取各线程栈")
    say(stacks)
    coverage_note = save_coverage(item)
    say(f"[超时留证] 覆盖率: {coverage_note}")
    dump, dump_note = capture_dump(item, head)
    say(f"[超时留证] 崩溃现场 dump: {dump_note}")
    written = write_allure_result(
        item,
        head=head,
        stacks=stacks,
        coverage_note=coverage_note,
        dump=dump,
        dump_note=dump_note,
    )
    say(f"[超时留证] Allure 结论: {written}")


def _reporter(item: pytest.Item) -> Callable[[str], None]:
    """返回一个**一定看得见**的输出函数(交给 coredumpy 的那份证据是另一回事).

    pytest 默认捕获 stdout, 而 ``os._exit`` 跳过收尾 —— 用 ``print`` 写的结论会连着缓冲区
    一起丢掉(2026-10-01 实测: 日志里只看得见 pytest-timeout 自己写的栈)。所以走终端写入器:
    它直连真正的终端, 与那些栈同一条路。
    """
    try:
        writer = item.config.get_terminal_writer()
    except Exception:
        return lambda text: print(text, flush=True)
    # ``write_raw``: 不解析 markup —— 栈文本里满是指标符与下划线, 解析了会变形。
    return lambda text: writer.write_raw(f"{text}\n", flush=True)


def _safe_text(action: Callable[[], str], what: str) -> str:
    """跑一个"产出一段文本"的步骤, 失败就把它变成一行说明(留证本身不许再抛异常)."""
    try:
        return action()
    except Exception as exc:
        return f"({what}失败: {type(exc).__name__}: {exc})"


def dump_captured_output(item: pytest.Item, say: Callable[[str], None]) -> None:
    """把用例至今的输出与日志倒到终端(步骤与上游的 ``timeout_timer`` 一致).

    三条都要: **捕获的 stdout / stderr**(用例里的 print、第三方库的输出)与 **caplog 的日志**
    (应用 logger 写到这里)。不做这一步, 它们会连同缓冲区一起消失 —— 而这些内容常常比栈
    更能说明"卡住的那一刻程序正在做什么"。
    """
    try:
        capman = item.config.pluginmanager.getplugin("capturemanager")
        if capman is None:
            return
        capman.suspend_global_capture(item)
        stdout, stderr = capman.read_global_capture()
    except Exception as exc:  # 倒不出来也只是少一段输出
        say(f"(倒出捕获输出失败: {type(exc).__name__}: {exc})")
        return
    caplog = item.config.pluginmanager.getplugin("_capturelog")
    handler = getattr(item, "capturelog_handler", None)
    log = ""
    if caplog is not None and handler is not None:
        with contextlib.suppress(Exception):
            log = handler.stream.getvalue()
    for title, body in (
        ("捕获的日志", log),
        ("捕获的 stdout", stdout),
        ("捕获的 stderr", stderr),
    ):
        if body:
            say(f"~~~~~ {title} ~~~~~")
            say(str(body).rstrip("\n"))


def thread_stacks() -> str:
    """所有线程的栈(与 pytest-timeout 打印的是同一份数据, 这里拼成可附件的文本)."""
    frames = sys._current_frames()
    chunks: list[str] = []
    for thread in threading.enumerate():
        chunks.append(f"===== 线程 {thread.name} ({thread.ident}) =====")
        frame = frames.get(thread.ident or -1)
        if frame is None:
            chunks.append("(拿不到这一帧: 线程已退出)")
            continue
        chunks.extend(line.rstrip("\n") for line in traceback.format_stack(frame))
    return "\n".join(chunks)


def print_stacks_via_plugin(item: pytest.Item) -> None:
    """沿用 pytest-timeout 自己的栈输出(保持控制台日志形态与历史一致).

    它是实现细节, 所以整段兜住异常: 换版本改名了也只是少一段控制台输出, 附件里的
    :func:`thread_stacks` 仍然在。
    """
    with contextlib.suppress(Exception):
        import pytest_timeout

        pytest_timeout.dump_stacks(item.config.get_terminal_writer())


def save_coverage(item: pytest.Item) -> str:
    """把覆盖率数据落盘, 返回一句人可读的结论.

    做的正是 pytest-cov 自己在会话结束时做的事(``CovController.finish`` → 停表 + 存盘) ——
    只是提前到"进程被杀之前"。少了这一步, 那一片的覆盖率会**完全消失**: 报告作业只能报
    "缺片", 而缺片的原因(超时)在报告里看不到。
    """
    try:
        controller = _coverage_controller(item)
        if controller is None or getattr(controller, "cov", None) is None:
            return "未启用(没有 pytest-cov 控制器)"
        controller.finish()
    except Exception as exc:  # 存不下来也要说清楚, 而不是让"缺片"变成谜
        return f"保存失败: {type(exc).__name__}: {exc}"
    data_file = getattr(getattr(controller.cov, "config", None), "data_file", None)
    return f"已保存({data_file or '覆盖率数据文件'})"


def _coverage_controller(item: pytest.Item) -> Any:
    """找出 pytest-cov 的控制器(插件私有名字是 ``_cov``, 所以带上兜底查找)."""
    manager = item.config.pluginmanager
    for name in _COV_PLUGIN_NAMES:
        if not manager.hasplugin(name):
            continue
        controller = getattr(manager.getplugin(name), "cov_controller", None)
        if controller is not None:
            return controller
    for plugin in manager.get_plugins():  # 改名了也不至于静默失效
        controller = getattr(plugin, "cov_controller", None)
        if controller is not None:
            return controller
    return None


def capture_dump(item: pytest.Item, head: str) -> tuple[Path | None, str]:
    """抓"卡住的那一帧"的 coredumpy dump, 返回 ``(文件 | None, 说明)``.

    取**主线程**的栈顶帧: pytest 就是在主线程上跑用例的, 超时现场(等锁、等 IO、死循环)
    基本都在那里 —— 2026-10-01 那次 Windows 挂起, 栈正好停在自己的
    ``database.py`` 的 ``connection.commit()`` 上, 主线程即现场。
    """
    try:
        frame = sys._current_frames().get(threading.main_thread().ident or -1)
        directory = item.config.getoption("--crash-dump-dir")
        depth = int(item.config.getoption("--crash-dump-depth"))
        return crash_capture.write_dump(
            node_id=f"{item.nodeid} [timeout]",
            frame=frame,
            description=head,
            directory=directory,
            depth=depth,
        )
    except Exception as exc:  # 抓不到 dump 也不能往外抛, 后面还有要写的东西
        return None, f"生成失败: {type(exc).__name__}: {exc}"


def write_allure_result(
    item: pytest.Item,
    *,
    head: str,
    stacks: str,
    coverage_note: str,
    dump: Path | None,
    dump_note: str,
) -> str:
    """写一条 Allure 结论(栈与 dump 都作为附件), 返回一句人可读的结论.

    直接写结果文件而不是走 ``allure.attach()``: allure-pytest 是在用例**收尾时**才把结果
    落盘的, 而这里马上就要 ``os._exit`` —— 挂到它身上等于什么都没写。

    外面这层只管"别抛异常": 它是留证的最后一步, 在这里抛出去的代价是"连结论都没有"。
    """
    try:
        return _write_allure_result(
            item,
            head=head,
            stacks=stacks,
            coverage_note=coverage_note,
            dump=dump,
            dump_note=dump_note,
        )
    except Exception as exc:
        return f"写入失败: {type(exc).__name__}: {exc}"


def _write_allure_result(
    item: pytest.Item,
    *,
    head: str,
    stacks: str,
    coverage_note: str,
    dump: Path | None,
    dump_note: str,
) -> str:
    """真正拼结果文件的那一段(取舍见 :func:`write_allure_result`)."""
    results_dir = _results_dir(item)
    if results_dir is None:
        return "未配置 --alluredir, 跳过"
    result_id = str(uuid.uuid4())
    attachments = [
        _attach_text(results_dir, result_id, "threads", stacks),
        _attach_text(
            results_dir,
            result_id,
            "notes",
            f"{head}\n\n覆盖率: {coverage_note}\n崩溃现场 dump: {dump_note}\n"
            + (f"落点: {dump}\n" if dump is not None else ""),
        ),
    ]
    if dump is not None and dump.stat().st_size <= MAX_ATTACHMENT_BYTES:
        attachments.append(_attach_file(results_dir, result_id, "coredumpy-dump", dump))
    timestamp = time.time_ns() // 1_000_000
    payload: dict[str, Any] = {
        "uuid": result_id,
        "historyId": str(uuid.uuid5(uuid.NAMESPACE_URL, f"timeout::{item.nodeid}")),
        "fullName": f"archive-management.timeout.{item.nodeid}",
        "name": f"{RESULT_TITLE}: {item.nodeid}",
        "status": RESULT_STATUS,
        "stage": "finished",
        "start": timestamp,
        "stop": timestamp,
        "labels": [
            {"name": "suite", "value": RESULT_TITLE},
            {"name": "epic", "value": "工程与发布"},
            {"name": "feature", "value": FEATURE},
            {"name": "story", "value": item.nodeid},
            {"name": "env", "value": platform_label(current_platform())},
            {"name": "severity", "value": "critical"},
        ],
        "statusDetails": {"message": head},
        "description": (
            f"{head}\n\n"
            f"- 覆盖率: {coverage_note}\n"
            f"- 崩溃现场: {dump_note}\n"
            + (f"- 用 `coredumpy load {dump}` 还原现场。\n" if dump is not None else "")
        ),
        "attachments": attachments,
    }
    target = results_dir / f"{result_id}-result.json"
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return f"{target.name}({len(attachments)} 个附件)"


def _results_dir(item: pytest.Item) -> Path | None:
    """当前运行的 Allure 结果目录(没配 --alluredir 时返回 None).

    这里试两个名字: allure-pytest 把这个选项的 ``dest`` 定成了 ``allure_report_dir``
    (命令行拼写是 ``--alluredir``), 直接用拼写去 ``getoption`` 会**静静地**拿到默认值 ——
    2026-10-01 就是这么白丢了一条结论。
    """
    for name in ("allure_report_dir", "alluredir"):
        with contextlib.suppress(Exception):
            value = item.config.getoption(name, None)
            if value:
                directory = Path(str(value))
                directory.mkdir(parents=True, exist_ok=True)
                return directory
    return None


def _attach_text(
    results_dir: Path, result_id: str, name: str, body: str
) -> dict[str, str]:
    """把一个文本附件写进结果目录."""
    target = results_dir / f"{result_id}-{name}.txt"
    target.write_text(body, encoding="utf-8")
    return {"name": f"{name}.txt", "source": target.name, "type": "text/plain"}


def _attach_file(
    results_dir: Path, result_id: str, name: str, source: Path
) -> dict[str, str]:
    """把一个已有文件复制成附件(dump 留在 ``crash-dumps/`` 里供 ``coredumpy load``).

    媒体类型用 Allure **不认识**的二进制类型: 报告里只给一个下载链接, 不会把上兆字节的
    文本读进预览区把页面卡死(与 crash_capture 同样的取舍)。
    """
    target = results_dir / f"{result_id}-{name}{source.suffix}"
    target.write_bytes(source.read_bytes())
    return {
        "name": target.name,
        "source": target.name,
        "type": crash_capture.DUMP_MEDIA_TYPE,
    }
