"""性能测试的共享夹具.

性能测试只在 GitHub Actions 执行: 默认 ``testpaths`` 不含本目录, 需要显式指定
``pytest tests/performance -m performance``。每个基准都必须在用例里声明数据规模
与阈值, 阈值超限即用例失败, 因此性能回归会直接体现在 CI 状态上。会话结束时
测量结果写入 ``performance-results.json``/``.csv``, 供 Allure 汇总任务作为
附件上传(见 ``scripts/create_allure_summary.py``)。
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from pathlib import Path

import allure
import pytest

from reporting import PerformanceRecorder

# 机器可读的基准结果(CI 汇总 job 会把它作为 Allure 附件上传).
RESULTS_JSON = Path("performance-results.json")


@pytest.fixture(scope="session")
def perf_recorder() -> Iterator[PerformanceRecorder]:
    """会话级基准记录器: 结束后把结果写到工作区根目录."""
    recorder = PerformanceRecorder()
    try:
        yield recorder
    finally:
        recorder.write(RESULTS_JSON)


@pytest.fixture(autouse=True)
def _attach_measurements(perf_recorder: PerformanceRecorder) -> Iterator[None]:
    """把当前用例产生的测量作为 Allure 附件(含规模/指标/阈值/结果)."""
    start = len(perf_recorder.measurements)
    yield
    fresh = perf_recorder.since(start)
    if fresh:
        allure.attach(
            json.dumps([asdict(item) for item in fresh], ensure_ascii=False, indent=2),
            name="性能测量(规模/指标/阈值/结果)",
            attachment_type=allure.attachment_type.JSON,
        )
