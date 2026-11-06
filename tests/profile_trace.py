"""给显式标记的用例挂一份函数级耗时追踪(viztracer).

viztracer 会把**每个函数调用**写进 trace, 默认配置下一个 1.2 s 的模块能产出 148 MB ——
所以它只对打了 ``@pytest.mark.profile`` 的用例生效, 并且按 1 ms 阈值过滤(实测: 同一模块
1.24 s -> 1.51 s 追踪开销, 7.36 MB; 见 ``.github/instructions/allure-reporting.instructions.md``
第 6 节)。定位目标是"这条全流程用例的时间花在哪几个函数上", 不是给整个套件做追踪。

三条纪律与 ``tests/crash_capture.py`` 相同:

- **只挂在一个平台**: 三平台各挂一份会把报告撑到 ~8 MB/轮, 而结论与平台无关; 默认只在
  Linux 开(CI 的报告基准平台), 本地想临时开就设 ``ARCHIVE_PROFILE_TRACE=1``;
- **附件的媒体类型用 Allure 不认识的** ``application/octet-stream``: 几 MB 的 trace 让
  Allure 渲染会卡死详情页;
- **失败也要落盘**: 卡住或报错的那一轮才是最有价值的样本 —— 整个落盘过程包在 try 里,
  它自己出错绝不能让用例报成内部错误。

除 trace 之外还会挂一份**文本摘要**(Top-N self time), 因为 ``.json`` trace 要下载下来用
Perfetto/``vizviewer`` 打开, 而摘要能在报告里直接读。

viztracer 没装时降级: 只打印一条提示, 不挂附件, 用例照常跑。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import allure
import pytest

import crash_capture

# 触发追踪的标记名(在 pyproject 的 markers 里登记)与临时开启的环境变量.
MARKER = "profile"
ENV_OVERRIDE = "ARCHIVE_PROFILE_TRACE"
# 当次用例的追踪句柄: setup 开始, teardown 报告阶段收尾(那时 Allure 的结果还没关).
TRACE_KEY = pytest.StashKey["Trace | None"]()
# 只保留 ≥1 ms 的帧: 短帧是噪声, 真正占时间的调用会被留下(实测 2,557 条 / 1.24 s).
MIN_DURATION_US = 1000
# 附件体积上限: 超过就只挂摘要(与 crash_capture 的 25 MiB 同一个判断: 报告要能打开).
MAX_TRACE_BYTES = 20 * 1024 * 1024
# 摘要里保留的函数个数.
SUMMARY_TOP = 20


@dataclass(frozen=True)
class Trace:
    """一次进行中的追踪: ``VizTracer`` 实例与落盘位置(类型用 Any 以免 import 期依赖)."""

    tracer: Any
    path: Path


def should_trace(item: pytest.Item, *, platform: str | None = None) -> bool:
    """只有"显式标记 + 允许的平台"才追踪; 其余用例一行额外开销都没有."""
    if item.get_closest_marker(MARKER) is None:
        return False
    if os.environ.get(ENV_OVERRIDE) == "1":
        return True
    current = sys.platform if platform is None else platform
    return current.startswith("linux")


def start(item: pytest.Item) -> Trace | None:
    """开始追踪; 不该追踪或 viztracer 不可用时返回 ``None``(降级, 不影响用例)."""
    if not should_trace(item):
        return None
    try:
        import viztracer
    except ImportError as exc:  # 没装就只提示一句: 追踪是可选证据, 不该让用例变红
        print(f"[profile] viztracer 不可用, 已跳过耗时追踪: {exc}")
        return None
    path = (
        Path(tempfile.gettempdir()) / f"profile-{_slug(item.nodeid)}-{os.getpid()}.json"
    )
    try:
        tracer = viztracer.VizTracer(
            min_duration=MIN_DURATION_US,
            ignore_c_function=True,
            output_file=str(path),
        )
        tracer.start()
    except Exception as exc:  # pragma: no cover - 第三方库启动失败极少见
        print(f"[profile] 启动追踪失败, 已跳过: {type(exc).__name__}: {exc}")
        return None
    return Trace(tracer=tracer, path=path)


def finish(trace: Trace | None) -> None:
    """结束追踪、写出 trace 并挂到报告; 任何一步失败都只打印, 不改用例结果."""
    if trace is None:
        return
    try:
        trace.tracer.stop()
        trace.tracer.save()
    except Exception as exc:  # pragma: no cover - 保存失败时没有可用数据
        print(f"[profile] 保存追踪失败, 已跳过: {type(exc).__name__}: {exc}")
        return
    try:
        crash_capture.attach(evidence(trace.path))
    except Exception as exc:  # pragma: no cover - 附件写不进去不能盖掉真正的失败
        print(f"[profile] 挂载追踪失败, 已跳过: {type(exc).__name__}: {exc}")


def evidence(path: Path) -> list[crash_capture.Evidence]:
    """把 trace 文件与摘要整理成附件; 超过体积上限时只留摘要与落点."""
    size = path.stat().st_size if path.exists() else 0
    summary = summarize_text(path)
    if size <= MAX_TRACE_BYTES:
        return [
            crash_capture.Evidence(
                name=f"耗时追踪 trace · {path.name}",
                attachment_type=crash_capture.DUMP_MEDIA_TYPE,
                path=path,
            ),
            crash_capture.Evidence(
                name="耗时追踪摘要 · Top self time",
                attachment_type=allure.attachment_type.TEXT,
                body=summary.encode("utf-8"),
            ),
        ]
    text = (
        f"{path} 有 {_size_text(size)}, 超过 "
        f"{_size_text(MAX_TRACE_BYTES)} 上限, 只在摘要里留了落点。\n\n"
        f"{summary}"
    )
    return [
        crash_capture.Evidence(
            name="耗时追踪摘要 · Top self time",
            attachment_type=allure.attachment_type.TEXT,
            body=text.encode("utf-8"),
        )
    ]


def summarize_text(path: Path, *, top: int = SUMMARY_TOP) -> str:
    """读 trace 并渲染"哪个函数自己占了多少时间"; 读不出来时给出原因."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return f"追踪文件读不出来: {path}({type(exc).__name__}: {exc})"
    return render_summary(payload, top=top)


def render_summary(payload: dict[str, Any], *, top: int = SUMMARY_TOP) -> str:
    """按 self time 聚合 ``ph == "X"`` 事件, 输出 Top-N 文本(可直接贴进报告)."""
    events = [
        event
        for event in payload.get("traceEvents", [])
        if event.get("ph") == "X" and "dur" in event
    ]
    self_us = self_times(events)
    total = sum(self_us.values())
    lines = [
        f"追踪事件 {len(events)} 条, 合计 {total / 1000:.1f} ms(按 self time 聚合)"
    ]
    ranked = sorted(self_us.items(), key=lambda item: item[1], reverse=True)[:top]
    width = max((len(name) for name, _ in ranked), default=0)
    for name, micros in ranked:
        share = (micros / total * 100) if total else 0.0
        lines.append(f"{micros / 1000:9.1f} ms {share:5.1f}%  {name.ljust(width)}")
    return "\n".join(lines)


def self_times(events: list[dict[str, Any]]) -> dict[str, float]:
    """把嵌套事件按名字累加"自己占的时间"(父减去子, 与 trace 查看器的口径一致)."""
    ordered = sorted(events, key=lambda event: (event["ts"], -event["dur"]))
    stack: list[list[Any]] = []  # [结束时间, 名字, 尚未被儿子占掉的时间]
    totals: dict[str, float] = {}

    def close(entry: list[Any]) -> None:
        totals[entry[1]] = totals.get(entry[1], 0.0) + entry[2]

    for event in ordered:
        start = float(event["ts"])
        duration = float(event["dur"])
        while stack and start >= stack[-1][0]:
            close(stack.pop())
        if stack:
            stack[-1][2] -= duration
        stack.append([start + duration, str(event.get("name", "?")), duration])
    while stack:
        close(stack.pop())
    return totals


def _slug(nodeid: str) -> str:
    """把用例 id 变成安全的文件名片段."""
    return "".join(char if char.isalnum() else "-" for char in nodeid)[-80:]


def _size_text(size: int) -> str:
    """人类可读的体积(只在"超过上限"的摘要里用得着)."""
    return f"{size / (1024 * 1024):.1f} MB"
