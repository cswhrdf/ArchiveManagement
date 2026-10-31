"""每条用例的真实耗时: 分片权重的精确数据源.

``tests/sharding.py`` 的分片权重原本只按"目录经验单价"估: integration 一律 2s、unit
一律 0.05s。条数均衡因此不等于耗时均衡 —— 2026-10-07 实测(test_gui_home.py, 27 条
99.3s): 同一模块内单条用例 1.5s~12.1s 相差 8 倍, 按目录单价装箱等于按条数装箱, 慢
用例会堆在同一片把墙钟拖长。

这份模块提供三件事:

1. **记录**: pytest 会话带 ``--record-durations=PATH`` 时, 根 conftest 把每条用例
   setup+call+teardown 三段报告的耗时累加, 会话结束时经 :func:`write` 落盘;
2. **合并**: 分片各记一份时(本地双片、CI 每片一文件), :func:`merge` 拼回全量,
   重叠的用例取**中位数**(对偶发的卡顿鲁棒);
3. **加载**: :func:`load` 读回 ``{nodeid: 秒}``, ``sharding.cost_of`` 拿它做
   **实测优先**的权重, 查不到再退回目录单价 —— 数据过时或缺条目只是退回旧行为,
   不会让分片失败。

落盘文件是"跑的什么就记什么": 用全量跑一次来刷新; 用部分跑会**整体覆盖**(没跑到的
条目在分片时退回目录单价)。schema 不对时 :func:`load` **响亮报错** —— 静默退回会让
坏数据文件永远没人发现。守卫见 ``tests/unit/test_durations.py``。
"""

from __future__ import annotations

import json
import statistics
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

#: 数据文件的固定位置: 与本模块同目录(``tests/``), 像视觉基线一样随仓库走.
DURATIONS_FILE = Path(__file__).with_name("durations.json")

#: 文件格式版本: 结构变化时递增, :func:`parse` 只认自己写的版本.
SCHEMA = 1


def parse(text: str) -> dict[str, float]:
    """把 durations JSON 文本解析成 ``{nodeid: 秒}``; 结构不对时响亮报错.

    报错要能直接指着文件修, 所以每条校验失败都带原因; 调用方(:func:`load`)会把
    文件路径补进异常消息。
    """
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"不是合法 JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("顶层必须是对象")
    if data.get("schema") != SCHEMA:
        raise ValueError(
            f"schema 版本不认识(期望 {SCHEMA}, 拿到 {data.get('schema')!r})"
        )
    raw = data.get("durations")
    if not isinstance(raw, dict):
        raise ValueError("缺少 durations 对象")
    parsed: dict[str, float] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or "::" not in key:
            raise ValueError(f"键必须是 nodeid: {key!r}")
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError(f"{key}: 耗时必须是数字, 拿到 {value!r}")
        if value < 0:
            raise ValueError(f"{key}: 耗时不能为负({value})")
        parsed[key] = float(value)
    return parsed


def load(path: Path = DURATIONS_FILE) -> dict[str, float]:
    """读数据文件; 不存在(还没生成过)返回空表, 内容坏了则报错并带上路径."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    try:
        return parse(text)
    except ValueError as exc:
        raise ValueError(f"{path}: {exc}") from exc


def accumulate(samples: dict[str, float], node_id: str, duration: float) -> None:
    """把一个阶段报告的耗时并进当前用例(一条用例有 setup/call/teardown 三段)."""
    samples[node_id] = samples.get(node_id, 0.0) + duration


def merge(*maps: Mapping[str, float]) -> dict[str, float]:
    """合并多份记录: 重叠的用例取中位数, 只出现一次的照抄.

    本地双片 / CI 多片各记一份时, 同一 nodeid 只会出现在其中一份里(分片不重不漏),
    重叠只发生在"把多次全量运行合起来"的用法上 —— 那正是中位数该出场的地方。
    """
    keys = {key for mapping in maps for key in mapping}
    return {
        key: statistics.median(m[key] for m in maps if key in m) for key in sorted(keys)
    }


def write(path: Path, samples: Mapping[str, float], *, recorded_at: datetime) -> None:
    """把 ``{nodeid: 秒}`` 写成 durations JSON(键排序, 耗时保留 3 位小数)."""
    payload = {
        "recorded": recorded_at.isoformat(timespec="seconds"),
        "schema": SCHEMA,
        "durations": {
            key: round(float(value), 3) for key, value in sorted(samples.items())
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
