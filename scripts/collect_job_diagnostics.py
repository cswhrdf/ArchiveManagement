r"""把一个作业的失败现场收进 ``job-diagnostics/``, 供 CI 在**兜底**时上传.

职责边界(2026-10-04 收紧): 普通失败的证据 —— 断言输出、门禁不通过、步骤报错 —— 已经由
前面的步骤写进 Allure 结果与运行日志了, 这里**不重复**它们。本脚本只回答一个问题:
"进程是不是根本没走到最后, 或者有没有运行时崩溃的现场"。判定写进 ``verdict.json`` 与
``$GITHUB_OUTPUT``(``scene=true|false``), 工作流按它决定**要不要上传** —— 没有兜底现场的
失败只留日志, 不多一份 artifact, 也不出现在报告首页那份附件里(用户 2026-10-04 的要求:
"结果里已经体现出来的失败现象不要在全局附件里再出现")。

两类兜底现场(**只有这两类**值得再占一份 artifact):

* **进程级崩溃**: ``crash-dumps/faulthandler.log`` 有栈(进程被信号打死, Python 栈由
  ``tests/crash_capture.py`` 写下), 或从系统目录收到崩溃报告 —— 两类证据各管一半, 不要混:
  faulthandler 给的是**Python 栈**(哪个线程死在哪个回调), 而"C 代码里是谁碰坏了内存"只有
  操作系统那份报告能回答(macOS 的 ``.ips``, 用 ``--crash-reports-from`` 从
  ``~/Library/Logs/DiagnosticReports`` 收过来)。
* **证据缺失**: ``--expect`` 指定的路径不在或为空。pytest-cov 是会话**结束**才落盘的, 进程
  被信号杀掉时覆盖率文件根本不会出现 —— 所以"预期的东西不在"本身就说明"不是用例失败,
  而是没跑完"。

约定(见 .github/workflows/ci.yml): **每个作业**的最后两步是 ``Collect failure
diagnostics`` (``if: failure()``) 与 ``Upload failure diagnostics``
(``if: failure() && steps.diagnostics.outputs.scene == 'true'``) —— 前者只负责判定, 后者的
条件就是上面那个判定。汇总作业再把各份 ``summary.md`` 拼成 ``allure-failure-diagnostics.md``
挂进报告(仓库根 ``allurerc.mjs`` 的 ``globalAttachments``); 判定为"没有兜底现场"的作业由
``create_allure_summary.py`` 在拼接时滤掉。

用法::

    uv run python scripts/collect_job_diagnostics.py --output job-diagnostics \
        --label "pytest (macos-latest, shard 0)" \
        --github-output "$GITHUB_OUTPUT" \
        --expect allure-results --expect .coverage.shard-0 \
        --crash-reports-from "$HOME/Library/Logs/DiagnosticReports" \
        --crash-report-wait 20 \
        --full-copy-at "crash-dumps-macos-latest-0" \
        crash-dumps allure-results allure-manifest.json

``--label`` 不传时退回 ``GITHUB_JOB``; 两者都没有就用 ``"local"``。本地跑同样安全: 只读
给定的路径、只写 ``--output`` 目录(以及把收到的崩溃报告拷进 ``crash-dumps/``)。
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import sys
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


def ensure_utf8_output() -> None:
    """按 UTF-8 重配标准输出.

    这里没有用 ``archive_management.paths.ensure_utf8_output``: 本脚本要在**每个**作业里跑,
    包括用 ``--no-install-project`` 装依赖、项目包压根不可导入的 pytest 作业, 所以不能依赖
    项目本身。做法与那边一致 —— 能重配就重配, 重配不了就算了。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")


# 每个路径最多列多少个"最大文件": 失败现场通常是**少量**大文件, 列全了反而看不出重点。
BIGGEST_FILES = 5
# 单个路径的字节数超过这个值时也提示一句(阈值取 1 MiB: artifact 单文件上限是几百 MiB,
# 但一个 1 MiB 以上的文件出现在这里基本意味着"又有一份完整报告被打进了诊断目录")。
LARGE_FILE_BYTES = 1024 * 1024
# 崩溃现场的落点(tests/crash_capture.py 写的 faulthandler 日志与系统级报告都放这里)。
CRASH_DUMP_DIRECTORY = Path("crash-dumps")
# 兜底判定写在诊断目录里(与 summary.md 同目录): 汇总作业按它滤掉"没有兜底现场"的作业。
# 为什么不直接从 summary.md 里读那句话: 解析人读的文本太脆, 而这份 JSON 是脚本自己写的。
VERDICT_NAME = "verdict.json"
# 系统级崩溃报告的扩展名: macOS 的 ReportCrash 写 ``.ips``(新格式), 旧格式是 ``.crash``。
CRASH_REPORT_SUFFIXES = (".ips", ".crash", ".diag")
# 文本现场最多内联多少**字符**进摘要: 摘要是要挂进报告给人看的, 不能把上百 KB 的原生栈
# 塞进去(报告一打开就卡); 超出部分留在 artifact 里, 报告里给文件名当路标。
INLINE_LIMIT = 4000
# faulthandler 每次致命错误的开头一行: 在日志里的**最后一次**出现后面, 紧跟的就是崩溃
# 线程(``Current thread``)的 Python 栈 —— 摘要只截开头的话它根本进不来(见
# :func:`last_fatal_error_headline`)。
FATAL_ERROR_MARKER = b"Fatal Python error"
# 在大日志里从尾部往回找 :data:`FATAL_ERROR_MARKER` 时一次读多少字节: 1 MiB 一块,
# 百 MiB 级的现场也只要上百次顺序读。相邻块之间要留 marker 长度的重叠, 否则恰好骑在
# 块边界上的那一次出现会被两边同时错过。
BACKWARD_SEARCH_CHUNK = 1024 * 1024
# 等系统级报告时的轮询间隔(ReportCrash 是异步的, 只能轮着看).
POLL_SECONDS = 1.0
# 本进程启动的时刻: 只收**这次**跑出来的报告, 免得把上一次运行的现场也收进来。
# 比较时给一点宽带(见 :data:`CRASH_REPORT_GRACE_SECONDS`)。
STARTED_AT = time.time()
# 判定"这份报告是这次的"时允许的提前量。为什么需要它: 文件时间戳的精度不一定到秒以下
# (实测 Windows 上出现过整秒的 mtime), 而"刚写出来的文件"与"进程启动时刻"可能落在同一秒里
# —— 严格比大小会把它当成上一次运行留下的。60 秒足够宽松, 而真正要排除的是**上一轮 CI**
# 留下的报告(那至少是几分钟以前)。
CRASH_REPORT_GRACE_SECONDS = 60.0


def human_size(size: int) -> str:
    """把字节数写成 ``1.2 MiB`` 这样的短标签(诊断摘要用, 精度够看趋势就行)."""
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KiB"
    return f"{size / (1024 * 1024):.1f} MiB"


def describe(path: Path) -> tuple[str, list[tuple[Path, int]]]:
    """返回 ``(状态说明, [(文件, 字节数), ...])``; 路径不存在时状态里写明原因.

    目录只统计**文件**(不跟符号链接, 免得绕进 runner 的临时目录), 文件直接算一个。
    """
    if not path.exists():
        return "**不存在**", []
    if path.is_file():
        return "文件", [(path, path.stat().st_size)]
    files = [item for item in sorted(path.rglob("*")) if item.is_file()]
    total = sum(item.stat().st_size for item in files)
    return f"{len(files)} 个文件, 共 {human_size(total)}", [
        (item, item.stat().st_size) for item in files
    ]


def crash_excerpts(directory: Path = CRASH_DUMP_DIRECTORY) -> list[Path]:
    """``crash-dumps`` 里值得内联进报告的**文本**现场(按名字排序).

    ``.dump``(coredumpy 的 JSON)不在这里: 它上兆字节, 而且挂进报告的是它的**下载链接**
    (见 ``tests/crash_capture.py`` 的 ``DUMP_MEDIA_TYPE``)。
    """
    if not directory.is_dir():
        return []
    wanted = {".log", *CRASH_REPORT_SUFFIXES}
    return [
        path
        for path in sorted(directory.iterdir())
        if path.is_file() and path.suffix in wanted
    ]


def _read_head(path: Path, chars: int) -> tuple[str, bool]:
    """只读文件开头的若干**字符**(大文件不许整读进内存), 顺带说明是不是已经读到尾.

    按 UTF-8 一个字符最多 4 字节来放大读取量, 再按字符截断 —— 现场文本(栈、``.ips``)
    几乎全是 ASCII, 浪费可以忽略, 而"先 ``read_text`` 再切"的老写法遇上几百 MiB 的
    faulthandler 日志会把收集步自己拖死。
    """
    with path.open("rb") as handle:
        raw = handle.read(chars * 4)
        at_eof = handle.read(1) == b""
    text = raw.decode("utf-8", errors="replace")
    # "读完且字符数没超"才算放行: 按 4:1 放大读时 EOF 只说明文件比请求的字节短,
    # 字符数仍可能超过上限(ASCII 现场文本全是 1:1)。
    return text[:chars], at_eof and len(text) <= chars


# faulthandler 的两种现场头: ``enable()`` 的致命路径写 "Fatal Python error"(崩溃线程
# ``Current thread`` 的栈跟在头后面); ``register()`` 的用户信号路径**没有头**, 直接写
# 各线程栈(第一个块就是崩溃线程)。2026-10-05 的 macOS 分片两者都出现了: 注册的处理器
# 转储完就返回, 故障指令重新执行 → 同一崩溃重复转储了上万份, 而最后的致命头写在文件
# 末尾 308 字节里 —— 致命路径刚开始转储就又崩了, 一个线程栈都没落盘。有货的是**最后
# 一份**用户信号转储。所以尾部捞回要看两个 marker(见 :func:`crash_thread_tail`)。
CURRENT_THREAD_MARKER = b"Current thread"


def _rfind_marker(path: Path, marker: bytes) -> int:
    """在(可能几百 MiB 的)文件里从尾部按块回找 ``marker`` 的最后一次出现.

    找到返回偏移, 没有(或读不了)返回 ``-1``。块之间留 ``len(marker) - 1`` 的重叠:
    骑在块边界上的出现不能靠前后两块各自 miss 掉。``start`` 归零说明整文件扫完了 ——
    必须在这里**显式收场**: 只靠 ``position = start + len(marker) - 1`` 收敛的话,
    "marker 不存在且文件比一块还小"时 position 会永远停在 ``len(marker) - 1`` 上,
    死循环(2026-10-05 由"只有用户信号转储、没有致命头"的守卫测试踩出)。
    """
    try:
        with path.open("rb") as handle:
            position = handle.seek(0, os.SEEK_END)
            while position > 0:
                start = max(0, position - BACKWARD_SEARCH_CHUNK)
                handle.seek(start)
                block = handle.read(position - start)
                index = block.rfind(marker)
                if index != -1:
                    return start + index
                if start == 0:
                    return -1
                # 下一块的**末尾**要伸进本块 marker 长度: 骑在边界上的出现才不会漏。
                position = start + len(marker) - 1
            return -1
    except OSError:  # pragma: no cover - 收集中文件被清掉/不可读
        return -1


def _segment(path: Path, offset: int, chars: int) -> str:
    """从 ``offset`` 起读若干**字符**(按 4:1 放大读字节再按字符截, 不整读大文件)."""
    try:
        with path.open("rb") as handle:
            handle.seek(offset)
            raw = handle.read(chars * 4)
    except OSError:  # pragma: no cover - 收集中文件被清掉/不可读
        return ""
    return raw.decode("utf-8", errors="replace")[:chars]


def last_fatal_error_headline(path: Path) -> str:
    """从(可能几百 MiB 的)faulthandler 日志里捞回**最后一次**致命错误的头部.

    为什么需要它: faulthandler 把**崩溃线程**(``Current thread``)的栈写在那次 dump 的
    最前面, 而日志会随重复触发不断变长 —— 摘要只截开头的话, 读者看到的全是等在
    ``threading.wait`` 里的旁观线程, 真正"死在哪一行"的栈埋在文件尾部(2026-10-05 的
    macOS 分片: 584.5 MiB 的日志, 开头 4000 字里一个现场线程都没有)。找不到(非
    faulthandler 的 ``.log``)时给空串, 不抛。摘要实际用的是它的升级版
    :func:`crash_thread_tail`。
    """
    offset = _rfind_marker(path, FATAL_ERROR_MARKER)
    if offset == -1:
        return ""
    return _segment(path, offset, INLINE_LIMIT)


def crash_thread_tail(path: Path, chars: int = INLINE_LIMIT) -> str:
    """尾部捞回"崩溃线程死在哪": 最后一次致命错误的头部优先, 头是空壳时回退到最后一份转储.

    判据(来自 2026-10-05 的真实现场): 最后一次 ``Fatal Python error`` 头之后紧跟
    ``Current thread`` 才算有货 —— 致命路径"刚开始转储就又崩"时, 头后面只剩
    ``Extension modules`` 一行(那种头没有任何栈); 此时唯一有货的是(注册信号处理器
    反复转储留下的)最后一份用户信号转储, 它的第一个块就是 ``Current thread``。
    """
    fatal = _rfind_marker(path, FATAL_ERROR_MARKER)
    if fatal != -1:
        segment = _segment(path, fatal, chars)
        if (
            CURRENT_THREAD_MARKER in segment.encode("utf-8")
            or _rfind_marker(path, CURRENT_THREAD_MARKER) == -1
        ):
            return segment
    current = _rfind_marker(path, CURRENT_THREAD_MARKER)
    if current != -1:
        return _segment(path, current, chars)
    return ""


def excerpt(path: Path, where: str = "见 artifact") -> str:
    """读一段现场文本; 读不到或不是文本时给一行说明, 不抛.

    超过 :data:`INLINE_LIMIT` 的只内联开头, 并把 ``where``(全文在哪)写进截断标记;
    ``.log``(faulthandler)再例外地补一段尾部捞回的崩溃线程栈 —— 它在最后一次
    ``Fatal Python error`` 的头部, 或(头是空壳时)最后一份用户信号转储里
    (见 :func:`crash_thread_tail`)。
    """
    try:
        head, reached_end = _read_head(path, INLINE_LIMIT)
        size = human_size(path.stat().st_size)
    except OSError as exc:  # pragma: no cover - 权限/收集中被删
        return f"(读不出来: {type(exc).__name__}: {exc})"
    if not head.strip():
        return "(空文件)"
    if reached_end:
        return head
    text = f"{head}\n... (已截断, 全文 {size} {where})"
    if path.suffix == ".log":
        rescued = crash_thread_tail(path)
        if rescued.strip() and rescued[:200] not in head:
            text += (
                "\n\n---- 文件尾部捞回的崩溃线程栈(最后一次 Fatal Python error,"
                "或反复触发时最后一份转储) ----\n"
                f"{rescued}"
            )
    return text


@dataclass(frozen=True)
class BackstopVerdict:
    """这个作业有没有"兜底现场"(普通失败之外的、结果里看不出来的东西)."""

    scene: bool
    reasons: list[str]


def is_empty(path: Path) -> bool:
    """路径不在, 或者是空的(空目录 / 0 字节文件).

    为什么"空"也算: 覆盖率文件被写出来却一行数据都没有, 与"根本没写出来"是同一件事
    —— pytest 没走到会话结束。
    """
    if not path.exists():
        return True
    if path.is_file():
        return path.stat().st_size == 0
    return not any(item.is_file() for item in path.rglob("*"))


def missing_evidence(expectations: Sequence[Path]) -> list[Path]:
    """``--expect`` 里不在或为空的路径(这些就是"进程没走到最后"的证据)."""
    return [path for path in expectations if is_empty(path)]


def crash_evidence(directory: Path | None = None) -> list[str]:
    """``crash-dumps`` 里的崩溃迹象(空的 faulthandler 日志不算现场).

    目录默认值在**调用时**解析(不用默认参数绑死): 单测要能把它换到临时目录上, 否则会去读
    仓库里那份真实目录。
    """
    reasons: list[str] = []
    for path in crash_excerpts(directory or CRASH_DUMP_DIRECTORY):
        if path.suffix == ".log":
            if not is_empty(path):
                reasons.append(f"进程被信号打死(`{path.name}` 里有栈)")
        else:
            reasons.append(f"收到系统级崩溃报告 `{path.name}`")
    return reasons


def backstop_verdict(
    expectations: Sequence[Path], crash_directory: Path | None = None
) -> BackstopVerdict:
    """判定"要不要上传现场": **崩溃证据**或**证据缺失**才算, 普通失败不算."""
    reasons = crash_evidence(crash_directory)
    missing = missing_evidence(expectations)
    if missing:
        names = ", ".join(f"`{path}`" for path in missing)
        reasons.append(f"预期的证据不在(进程没走到会话结束): {names}")
    return BackstopVerdict(scene=bool(reasons), reasons=reasons)


def inventory(paths: Sequence[Path], expectations: Sequence[Path]) -> list[Path]:
    """清点清单 = 位置参数 + ``--expect`` 里还没列过的那些(判据与清单不必让人写两遍)."""
    listed = list(paths)
    for path in expectations:
        if path not in listed:
            listed.append(path)
    return listed


def single_line(text: str) -> str:
    """压成一行: ``$GITHUB_OUTPUT`` 里一个键只能占一行."""
    return " ".join(text.split())


def write_github_output(path: Path, values: Mapping[str, str]) -> None:
    """把判定追加进 ``$GITHUB_OUTPUT``(工作流的 ``if:`` 直接读它)."""
    with path.open("a", encoding="utf-8") as handle:
        for key, value in values.items():
            handle.write(f"{key}={single_line(value)}\n")


def write_verdict(path: Path, label: str, verdict: BackstopVerdict) -> None:
    """写 ``verdict.json`` —— 报告首页那份附件按它筛掉"没有兜底现场"的作业."""
    payload = {"label": label, "scene": verdict.scene, "reasons": verdict.reasons}
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")
    print(f"兜底判定已写入 {path}: scene={verdict.scene}")


def _is_fresh_report(path: Path) -> bool:
    """这份系统级报告是不是**这次**跑出来的(ReportCrash 可能还留着上一次的)."""
    try:
        return (
            path.suffix in CRASH_REPORT_SUFFIXES
            and path.stat().st_mtime >= STARTED_AT - CRASH_REPORT_GRACE_SECONDS
        )
    except OSError:  # pragma: no cover - 收集中文件被清掉
        return False


def collect_crash_reports(
    source: Path | None, target: Path, *, wait_seconds: float
) -> list[Path]:
    """把系统级崩溃报告拷进 ``target``, 返回拷过来的文件(没有就给空表).

    为什么要向操作系统要这份: 段错误发生在 Tk 的 C 代码里时, Python 的 faulthandler 只能
    给出**Python 栈**(哪个线程死在哪个回调 —— 见 ``crash-dumps/faulthandler.log``), 而
    "内存是谁碰坏的"要看信号、出错地址与原生调用栈, 那只有系统级报告里有。macOS 的
    ReportCrash 会把它写进 ``~/Library/Logs/DiagnosticReports``, 但它是**异步**的: 进程死后
    要几秒才落盘, 所以这里等一小会儿。Linux/Windows 上那个目录不存在, 于是**一秒都不等**
    (``wait_seconds`` 只在来源目录真的存在时才生效)。
    """
    if source is None or not source.is_dir():
        return []
    deadline = time.monotonic() + max(wait_seconds, 0.0)
    while True:
        fresh = [path for path in sorted(source.iterdir()) if _is_fresh_report(path)]
        if fresh or time.monotonic() >= deadline:
            break
        time.sleep(POLL_SECONDS)
    target.mkdir(parents=True, exist_ok=True)
    copied: list[Path] = []
    for path in fresh:
        destination = target / path.name
        shutil.copyfile(path, destination)
        copied.append(destination)
    return copied


def run_page_url() -> str:
    """本次运行的页面(CI 变量不全时给空串, 本地跑也不会出错).

    为什么链到运行页而不是往地址后拼 ``artifacts``: 产物就挂在本页的 "Artifacts" 区,
    而 ``<run>/artifacts`` 这个后缀地址在现在的 Actions 界面里打不开(2026-10-05 报告
    读者实测, 点过去是一张失败页)。收集步跑在上传**之前**, 那时单个产物还没有编号
    (上传后才分配), 能给稳定地址的只有运行页本身 —— 产物名已由 ``--full-copy-at``
    写给读者, 在那个区里按名取即可。
    """
    server = os.environ.get("GITHUB_SERVER_URL", "")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    if not (server and repository and run_id):
        return ""
    return f"{server}/{repository}/actions/runs/{run_id}"


def full_copy_hint(names: Sequence[str]) -> str:
    """截断标记里"全文在哪"的半句: 只点名 artifact, **不**放链接.

    名字必须写(2026-10-05 的教训: 只写"见 artifact"三个字, 读者得自己猜是哪一份);
    但链接不进这里 —— 这半句最终落在 ``~~~`` 代码围栏**内**(见 :func:`excerpt`),
    围栏里的 URL 点不了, 一长串 ``github.com`` 还把栈文本斜插成两半, 妨碍读栈
    (2026-10-05 报告读者的第二条反馈)。下载入口统一放"排查提示"末条: 那里在围栏外,
    用 markdown 链接语法指向本次运行页面(见 :func:`build_summary`)。
    """
    if not names:
        return "见 artifact"
    listed = ", ".join(f"`{name}`" for name in names)
    return f"见 artifact {listed}"


def environment_lines() -> list[str]:
    """收集能说明"在哪跑的"的几行(CI 变量缺失时留空, 本地跑也不会出错).

    运行链接与 :func:`run_page_url` 同源: 正文里的链接一律用 markdown 语法
    (``[文本](地址)``), 不裸贴 URL —— 摘要在报告与运行摘要页里都按 markdown 渲染,
    裸 URL 只会让正文变长而不多出任何能力。
    """
    run_url = run_page_url()
    run_line = (
        f"- 运行: [第 {os.environ.get('GITHUB_RUN_ATTEMPT', '-')} 次尝试]({run_url})"
        if run_url
        else "- 运行: (本地)"
    )
    return [
        f"- 作业: {os.environ.get('GITHUB_JOB', '(本地)')}",
        run_line,
        f"- 提交: {os.environ.get('GITHUB_SHA', '(本地)')}",
        f"- 平台: {platform.platform()}",
        f"- Python: {platform.python_version()}",
        f"- 工作目录: {Path.cwd()}",
        f"- 收集时间(UTC): {datetime.now(UTC).isoformat(timespec='seconds')}",
    ]


def build_summary(
    label: str,
    paths: Sequence[Path],
    crash_directory: Path = CRASH_DUMP_DIRECTORY,
    verdict: BackstopVerdict | None = None,
    *,
    full_copy_at: Sequence[str] = (),
) -> str:
    """拼出 ``summary.md`` 的正文(纯函数: 便于单测直接比对).

    ``verdict`` 不传时不写判定节 —— 那样这一页只剩"清单 + 现场", 仍然是可读的。
    ``full_copy_at`` 是承载现场全文的 artifact 名单(工作流知道, 脚本不知道): 截断标记
    与排查提示会点名它们; 下载入口只出现在排查提示末条 —— markdown 链接指向本次运行
    页面(产物挂在运行页的 Artifacts 区), 围栏内的截断标记不嵌链接。
    """
    lines = [f"# 作业失败现场: {label}", ""]
    lines.extend(environment_lines())
    lines.append("")
    if verdict is not None:
        lines.append("## 兜底判定")
        lines.append("")
        if verdict.scene:
            lines.append(
                "**有兜底现场 —— 需要上传**(普通失败的证据不在这里, 它已经在结果里):"
            )
            lines.append("")
            lines.extend(f"- {reason}" for reason in verdict.reasons)
        else:
            lines.append(
                "**没有兜底现场**: 这一次失败的证据已经在 Allure 结果与运行日志里(断言输出、"
                "门禁不通过、步骤报错), 所以**不上传**诊断, 报告首页那份附件里也不会出现这个"
                "作业 —— 这一页只作为兜底: 进程级崩溃或证据缺失时才留内容。"
            )
        lines.append("")
    lines.append("## 路径清点")
    lines.append("")
    lines.append("| 路径 | 状态 | 最大的文件 |")
    lines.append("| --- | --- | --- |")
    for path in paths:
        status, files = describe(path)
        biggest = sorted(files, key=lambda item: item[1], reverse=True)[:BIGGEST_FILES]
        detail = (
            "<br>".join(f"`{item.name}` {human_size(size)}" for item, size in biggest)
            or "—"
        )
        lines.append(f"| `{path}` | {status} | {detail} |")
    lines.append("")
    excerpts = crash_excerpts(crash_directory)
    lines.append("## 崩溃现场摘录")
    lines.append("")
    if not excerpts:
        lines.append("(没有可内联的现场: 进程没被信号打死, 也没有系统级报告)")
        lines.append("")
    for path in excerpts:
        # 用 ``~~~`` 而不是反引号围栏: faulthandler 的栈里可能出现反引号。
        lines.append(f"### `{path.name}`")
        lines.append("")
        lines.append("~~~")
        lines.append(excerpt(path, full_copy_hint(full_copy_at)))
        lines.append("~~~")
        lines.append("")
    lines.append("## 排查提示")
    lines.append("")
    lines.append(
        "- 覆盖率文件(`.coverage.shard-*` / `coverage.xml`) **不在**时, 多半是 pytest "
        "进程没走到会话结束(被信号杀掉或超时中断), 这本身就是证据。"
    )
    lines.append(
        "- `crash-dumps/faulthandler.log` **有内容** = 进程被信号打死了(上面那份摘录里就有"
        "所有线程的 Python 栈); 它是空的而用例仍然失败, 说明失败发生在 Python 层(现场是那份"
        "coredumpy dump)。"
    )
    lines.append(
        "- 上面的 `.ips` 是**操作系统**写的报告: 里面的 `exception`(信号/出错地址)与"
        '`faultingThread` 的原生栈才能回答"C 代码里是谁碰坏了内存"。'
        "ReportCrash 是异步的, 所以收集时等了几秒。"
    )
    pointer: str
    listed = ", ".join(f"`{name}`" for name in full_copy_at)
    url = run_page_url()
    if listed and url:
        pointer = (
            "- 本目录只收清单与文本现场, 不含大文件: 完整的 `.ips` 与 faulthandler 日志"
            f" 见 artifact {listed} —— 下载入口在[本次运行页面]({url})的 Artifacts 区"
        )
    elif listed:
        pointer = (
            "- 本目录只收清单与文本现场, 不含大文件: 完整的 `.ips` 与 faulthandler 日志"
            f" 见 artifact {listed}"
        )
    else:
        pointer = (
            "- 本目录只收清单与文本现场, 不含大文件: coredumpy 的 dump 与完整的 `.ips` "
            "见同名作业的 `crash-dumps` artifact。"
        )
    lines.append(pointer)
    lines.append(
        "- **普通失败不在这一页**: 用例断言、门禁不通过、步骤报错的证据在用例详情与运行日志里"
        '(见上面的"兜底判定"); 这一页只在**进程级崩溃 / 证据缺失**时才有内容。'
    )
    lines.append("")
    return "\n".join(lines)


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """命令行参数: 输出目录、作业标签、要清点的路径."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("job-diagnostics"),
        help="诊断目录(默认 job-diagnostics)",
    )
    parser.add_argument(
        "--label",
        default="",
        help="作业标签, 写在摘要标题里; 默认取 GITHUB_JOB",
    )
    parser.add_argument(
        "--crash-reports-from",
        type=Path,
        default=None,
        help="系统级崩溃报告的来源目录(macOS: ~/Library/Logs/DiagnosticReports)",
    )
    parser.add_argument(
        "--crash-report-wait",
        type=float,
        default=0.0,
        help="等系统级报告落盘的最长秒数(只在来源目录存在且已有崩溃迹象时才等)",
    )
    parser.add_argument(
        "--full-copy-at",
        action="append",
        default=[],
        metavar="NAME",
        help="承载现场全文的 artifact 名: 截断标记与排查提示里会点名它, 排查提示另附"
        " 指向本次运行页面的 markdown 链接(产物挂在运行页的 Artifacts 区); 可重复传",
    )
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        type=Path,
        metavar="PATH",
        help="预期存在的证据(不在或为空 = 进程没走到会话结束); 可重复传",
    )
    parser.add_argument(
        "--github-output",
        type=Path,
        default=None,
        help="把兜底判定追加到这个文件(CI 里传 $GITHUB_OUTPUT)",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="要清点的路径")
    return parser.parse_args(argv)


def collect_and_judge(
    arguments: argparse.Namespace, expectations: Sequence[Path]
) -> BackstopVerdict:
    """先判定一次, **只在已经有崩溃迹象时**才去等系统级报告, 然后连报告一起再判一次.

    为什么不无条件等: ReportCrash 是异步的(进程死后几秒才落盘), 等它要花时间 —— 而普通
    失败(用例断言、门禁不通过)根本不产生系统报告。所以先看本地有没有崩溃迹象(有栈的
    faulthandler 日志、或预期证据缺失), 有才等; 等到了就多一条理由。
    """
    verdict = backstop_verdict(expectations)
    if not verdict.scene:
        return verdict
    collected = collect_crash_reports(
        arguments.crash_reports_from,
        CRASH_DUMP_DIRECTORY,
        wait_seconds=arguments.crash_report_wait,
    )
    if collected:
        names = ", ".join(path.name for path in collected)
        print(f"收到 {len(collected)} 份系统级崩溃报告: {names}")
    return backstop_verdict(expectations)


def write_fallback_summary(path: Path, label: str, verdict: BackstopVerdict) -> None:
    """收集出错时也要留下一页 —— 上传步按判定跑, 不能让它空手而回."""
    try:
        path.write_text(
            f"# 作业失败现场: {label}\n\n"
            f'清单写不出来({verdict.reasons[0]}), 所以按"有现场"处理, '
            "并把这一页留下来。\n",
            encoding="utf-8",
        )
    except OSError as exc:  # pragma: no cover - 连写都写不出来就只能靠日志
        print(f"::warning::连兜底摘要都写不出来: {type(exc).__name__}: {exc}")


def main(argv: Sequence[str] | None = None) -> int:
    """写 ``summary.md`` 与 ``verdict.json``, 并把判定写进 ``$GITHUB_OUTPUT``."""
    ensure_utf8_output()
    args = parse_args(argv)
    label = args.label or os.environ.get("GITHUB_JOB") or "local"
    args.output.mkdir(parents=True, exist_ok=True)
    expectations = list(args.expect)
    summary = args.output / "summary.md"
    try:
        verdict = collect_and_judge(args, expectations)
        text = build_summary(
            label,
            inventory(args.paths, expectations),
            verdict=verdict,
            full_copy_at=args.full_copy_at,
        )
        summary.write_text(text, encoding="utf-8")
        print(text)
        print(f"失败现场摘要已写入 {summary}")
    except Exception as exc:  # 兜底脚本自己不许成为失败点: 任何意外都按"有现场"处理
        # 收集本身出错时按"有现场"处理: 宁可多传一份小的, 也不要在最需要证据的时候因为一个
        # 意外(路径中途消失、编码坏了)把现场丢掉。
        verdict = BackstopVerdict(True, [f"收集失败: {type(exc).__name__}: {exc}"])
        print(f'::warning::失败现场收集出错, 按"有现场"处理: {verdict.reasons[0]}')
        write_fallback_summary(summary, label, verdict)
    write_verdict(args.output / VERDICT_NAME, label, verdict)
    if args.github_output is not None and str(args.github_output):
        write_github_output(
            args.github_output,
            {
                "scene": str(verdict.scene).lower(),
                "reason": "; ".join(verdict.reasons),
            },
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
