"""测试运行报告设施: 性能基准与安全结论的记录器.

被 ``tests/performance``、``tests/security`` 两组测试共用: 记录器把每条结论
(规模、指标、阈值、取值、是否通过)保存在内存里, 会话结束时写入机器可读的
JSON/CSV, 并由各自的 ``conftest.py`` 按用例附着到 Allure 结果上。
"""

from __future__ import annotations

import csv
import time
import tracemalloc
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from helpers import environment_info, write_json_report

Comparison = Literal["max", "min"]
_MEASUREMENT_FIELDS = (
    "name",
    "scale",
    "metric",
    "value",
    "unit",
    "threshold",
    "comparison",
    "passed",
)


@dataclass(frozen=True)
class Measurement:
    """一次基准测量: 规模、指标、取值、阈值与结论."""

    name: str
    scale: str
    metric: str
    value: float
    unit: str
    threshold: float
    comparison: Comparison
    passed: bool

    def row(self) -> dict[str, Any]:
        """返回可直接写入 CSV 的一行."""
        data = asdict(self)
        data["value"] = round(self.value, 4)
        return data


class PerformanceRecorder:
    """收集性能基准并在会话结束时写入 JSON/CSV."""

    def __init__(self) -> None:
        """初始化空记录列表."""
        self.measurements: list[Measurement] = []

    @contextmanager
    def duration(
        self, name: str, *, scale: str, budget_seconds: float
    ) -> Iterator[None]:
        """测量代码块耗时(秒), 超出预算即失败."""
        start = time.perf_counter()
        try:
            yield
        finally:
            self.record(
                name=name,
                scale=scale,
                metric="duration",
                value=time.perf_counter() - start,
                unit="seconds",
                threshold=budget_seconds,
                comparison="max",
            )

    @contextmanager
    def throughput(
        self, name: str, *, scale: str, total_bytes: int, minimum_mib_per_second: float
    ) -> Iterator[None]:
        """测量代码块吞吐(MiB/s), 低于下限即失败."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = max(time.perf_counter() - start, 1e-9)
            self.record(
                name=name,
                scale=scale,
                metric="throughput",
                value=total_bytes / elapsed / (1024 * 1024),
                unit="MiB/s",
                threshold=minimum_mib_per_second,
                comparison="min",
            )

    @contextmanager
    def peak_memory(
        self, name: str, *, scale: str, budget_mib: float
    ) -> Iterator[None]:
        """测量代码块的内存峰值(MiB), 超出预算即失败."""
        tracemalloc.start()
        try:
            yield
        finally:
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            self.record(
                name=name,
                scale=scale,
                metric="peak_memory",
                value=peak / (1024 * 1024),
                unit="MiB",
                threshold=budget_mib,
                comparison="max",
            )

    def record(
        self,
        *,
        name: str,
        scale: str,
        metric: str,
        value: float,
        unit: str,
        threshold: float,
        comparison: Comparison,
    ) -> Measurement:
        """记录一次测量并校验阈值(超出即失败, 让回归体现在 CI 状态上)."""
        passed = value <= threshold if comparison == "max" else value >= threshold
        measurement = Measurement(
            name=name,
            scale=scale,
            metric=metric,
            value=round(value, 4),
            unit=unit,
            threshold=threshold,
            comparison=comparison,
            passed=passed,
        )
        self.measurements.append(measurement)
        limit = "上限" if comparison == "max" else "下限"
        assert passed, (
            f"性能回归: {name} [{scale}] {metric}={value:.4f}{unit} "
            f"超出{limit} {threshold:g}{unit}"
        )
        return measurement

    def since(self, index: int) -> list[Measurement]:
        """返回第 ``index`` 条之后的测量(用于按用例附着报告)."""
        return self.measurements[index:]

    def write(
        self,
        json_path: Path = Path("performance-results.json"),
        csv_path: Path | None = None,
    ) -> None:
        """把全部测量写入 JSON(可选 CSV), 并附上运行环境信息."""
        if not self.measurements:
            return
        write_json_report(
            json_path,
            {
                "category": "performance",
                "environment": environment_info(),
                "measurements": [asdict(item) for item in self.measurements],
            },
        )
        target = csv_path or json_path.with_suffix(".csv")
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(_MEASUREMENT_FIELDS))
            writer.writeheader()
            for measurement in self.measurements:
                writer.writerow(measurement.row())


@dataclass(frozen=True)
class Finding:
    """一条安全测试结论: 输入场景、期望拦截与实际情况."""

    category: str
    scenario: str
    input_summary: str
    expected: str
    actual: str
    blocked: bool


class SecurityRecorder:
    """收集安全测试结论并在会话结束时写入 JSON."""

    def __init__(self) -> None:
        """初始化空结论列表."""
        self.findings: list[Finding] = []

    def expect_blocked(
        self,
        *,
        category: str,
        scenario: str,
        input_summary: str,
        expected: str,
        actual: str,
        blocked: bool,
    ) -> Finding:
        """记录"是否被拦截"的结论, 未被拦截时立即失败."""
        finding = Finding(
            category=category,
            scenario=scenario,
            input_summary=input_summary,
            expected=expected,
            actual=actual,
            blocked=blocked,
        )
        self.findings.append(finding)
        assert blocked, f"安全回归: {category}/{scenario} 未被拦截({actual})"
        return finding

    def since(self, index: int) -> list[Finding]:
        """返回第 ``index`` 条之后的结论(用于按用例附着报告)."""
        return self.findings[index:]

    def write(self, json_path: Path = Path("security-results.json")) -> None:
        """把全部结论写入 JSON(含运行环境信息)."""
        if not self.findings:
            return
        write_json_report(
            json_path,
            {
                "category": "security",
                "environment": environment_info(),
                "findings": [asdict(item) for item in self.findings],
            },
        )
