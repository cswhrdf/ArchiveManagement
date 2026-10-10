"""分片权重实测数据(durations.json)的守卫用例.

``sharding.cost_of`` 从"目录经验单价"升级成"实测优先、单价兜底"之后, 有两类失败
必须被钉住: **实测值被静默忽略**(那会退回按条数装箱, 慢用例堆在同一片, 墙钟
悄悄变长却没有任何红灯), 以及**坏数据文件让整套测试起不来**(加载要响亮报错,
报错里要能指着文件修)。此外 ``scripts/run_tests_local.py`` 对 ``--record-durations``
的"每片各记一份、收尾合并"改写也在这里锁住 —— 那条路坏了的话, 双片会互相覆盖
记录, 刷出来的数据只剩后结束那一片。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import durations
import sharding

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("分片权重实测数据"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_script(name: str) -> Any:
    """按路径加载 ``scripts/`` 下的脚本(``scripts`` 不在 pythonpath 里, 不能直接 import)."""
    spec = importlib.util.spec_from_file_location(
        name, _REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module: ModuleType = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# 模块级加载一次: 脚本无副作用(入口在 __main__ 守卫里)。
runner = _load_script("run_tests_local")

_NODE = "tests/unit/test_models.py::test_case_0"


# ------------------------------------------------------------ cost_of 三级取值


def test_a_recorded_duration_beats_the_directory_price(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """有实测值的用例按实测装箱: 目录单价只在查不到时兜底."""
    monkeypatch.setattr(sharding, "_DURATION_OVERRIDES", {_NODE: 9.5})

    assert sharding.cost_of(_NODE) == 9.5
    assert sharding.load_of([_NODE, _NODE, _NODE]) == pytest.approx(28.5)


def test_zero_is_a_valid_recorded_duration(monkeypatch: pytest.MonkeyPatch) -> None:
    """0.0 是合法实测值: 判空必须用 ``is not None``, 真值判断会把 0 当成"没记录"."""
    monkeypatch.setattr(sharding, "_DURATION_OVERRIDES", {_NODE: 0.0})

    assert sharding.cost_of(_NODE) == 0.0


def test_recorded_lookups_survive_param_escape_sequences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """参数化 id 的 ``\\uXXXX`` 转义自带反斜杠: 全串归一会把实测值查丢(2026-10-07 实锤)."""
    escaped = "tests/integration/test_x.py::test_a[file_kind-socket-\\u672a\\u77e5]"
    monkeypatch.setattr(sharding, "_DURATION_OVERRIDES", {escaped: 4.2})

    assert sharding.cost_of(escaped) == 4.2
    # Windows 风格的路径分隔也要能兜底查到(参数转义不受影响)。
    assert sharding.cost_of(escaped.replace("integration/", "integration\\", 1)) == 4.2


def test_cases_without_a_recording_fall_back_to_directory_prices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有实测的用例退回目录单价: 集成贵于单元, 未知目录有兜底, 反斜杠要归一."""
    monkeypatch.setattr(sharding, "_DURATION_OVERRIDES", {})

    assert sharding.cost_of("tests/integration/test_x.py::test_a") > sharding.cost_of(
        "tests/unit/test_x.py::test_a"
    )
    assert sharding.cost_of(r"tests\unit\test_x.py::test_a") == sharding.cost_of(
        "tests/unit/test_x.py::test_a"
    )
    assert sharding.cost_of("somewhere/test_new.py::test_a") == sharding.DEFAULT_COST


def test_recorded_heavy_cases_land_in_different_shards(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实测权重要真的把慢用例摊开: 三条 10s 的用例必须各占一片, 而不是堆在一起."""
    heavy = {
        f"tests/integration/test_a.py::test_slow_{index}": 10.0 for index in range(3)
    }
    light = {f"tests/unit/test_b.py::test_case_{index}": 0.01 for index in range(90)}
    monkeypatch.setattr(sharding, "_DURATION_OVERRIDES", heavy | light)

    buckets = sharding.shard_plan(sorted(heavy | light), count=3)

    homes = [
        next(i for i, bucket in enumerate(buckets) if node in bucket) for node in heavy
    ]
    assert sorted(homes) == [0, 1, 2], f"三条慢用例落在了一片: {homes}"


# ------------------------------------------------------------ 记录/合并/加载


def test_accumulate_sums_the_phase_reports() -> None:
    """一条用例的耗时是 setup/call/teardown 三段报告之和."""
    samples: dict[str, float] = {}

    durations.accumulate(samples, _NODE, 0.1)
    durations.accumulate(samples, _NODE, 3.2)
    durations.accumulate(samples, _NODE, 0.4)
    durations.accumulate(samples, "tests/unit/test_x.py::test_other", 0.2)

    assert samples == {
        _NODE: pytest.approx(3.7),
        "tests/unit/test_x.py::test_other": 0.2,
    }


def test_write_then_load_round_trips(tmp_path: Path) -> None:
    """写入再读回不丢数据; 耗时保留 3 位小数(文件也由此保持可 diff)."""
    target = tmp_path / "nested" / "durations.json"

    durations.write(
        target,
        {_NODE: 1.2349, "tests/unit/test_x.py::test_zero": 0.0},
        recorded_at=datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC),
    )

    assert durations.load(target) == {
        _NODE: 1.235,
        "tests/unit/test_x.py::test_zero": 0.0,
    }


@pytest.mark.parametrize(
    "payload",
    [
        "not json",
        "[]",
        json.dumps({"durations": {}}),
        json.dumps({"schema": 99, "durations": {}}),
        json.dumps({"schema": 1}),
        json.dumps({"schema": 1, "durations": []}),
        json.dumps({"schema": 1, "durations": {"no-node-separator": 1.0}}),
        json.dumps({"schema": 1, "durations": {_NODE: -1.0}}),
        json.dumps({"schema": 1, "durations": {_NODE: True}}),
    ],
)
def test_parse_rejects_broken_payloads(payload: str) -> None:
    """坏数据要响亮报错: 静默退回目录单价会让坏文件永远没人发现."""
    with pytest.raises(ValueError):
        durations.parse(payload)


def test_load_names_the_file_when_the_content_is_broken(tmp_path: Path) -> None:
    """报错里要带文件路径 —— 那是要拿去修的文件, 不是一段悬空的 JSON."""
    broken = tmp_path / "durations.json"
    broken.write_text(json.dumps({"schema": 99, "durations": {}}), encoding="utf-8")

    # 路径里满是反斜杠, 当正则用会炸, 先转义。
    with pytest.raises(ValueError, match=re.escape(str(broken))):
        durations.load(broken)


def test_merge_takes_the_median_for_overlaps() -> None:
    """合并重叠取中位数: 偶发的卡顿不该污染多次全量运行的综合值."""
    merged = durations.merge(
        {_NODE: 1.0, "tests/unit/test_x.py::test_only_left": 2.0},
        {_NODE: 3.0},
    )

    assert merged == {_NODE: 2.0, "tests/unit/test_x.py::test_only_left": 2.0}


def test_the_committed_file_stays_well_formed() -> None:
    """随仓库提交的 durations.json: 可解析、键是规范 nodeid 且文件存在、sharding 用的就是它."""
    if not durations.DURATIONS_FILE.exists():
        pytest.skip("tests/durations.json 还没生成(全量跑一次 --record-durations 刷新)")

    recorded = durations.load()

    assert recorded, "数据文件不该是空的"
    for node_id, seconds in recorded.items():
        # 反斜杠只允许出现在参数化 id 的 ``\uXXXX`` 转义里(那在 ``::`` 之后);
        # 文件部分必须是正斜杠, 否则 Windows 之外的平台对不上路径。
        file_part, separator, _ = node_id.partition("::")
        assert separator, f"键必须是 nodeid: {node_id}"
        assert "\\" not in file_part, f"文件部分要用正斜杠: {node_id}"
        assert (_REPO_ROOT / file_part).is_file(), f"用例文件不存在: {node_id}"
        assert seconds >= 0, f"耗时不能为负: {node_id} = {seconds}"
    assert recorded == sharding._DURATION_OVERRIDES, (
        "sharding 加载的数据与这份文件不一致(模块被重复导入了?)"
    )


# ------------------------------------------------------------ 脚本的分片改写


def test_the_runner_rewrites_the_record_target_per_shard(tmp_path: Path) -> None:
    """每片的记录要落到片私有文件: 两片写同一份会互相覆盖, 只剩后结束的那片."""
    args = ["-q", "--record-durations=tests/durations.json", "--timeout=90"]
    target = runner._split_record_target(args)

    assert target == Path("tests/durations.json")
    shard0 = runner._shard_pytest_args(args, target, tmp_path, 0)
    shard1 = runner._shard_pytest_args(args, target, tmp_path, 1)

    assert shard0 == [
        "-q",
        f"--record-durations={tmp_path / 'shard-0-durations.json'}",
        "--timeout=90",
    ]
    assert shard1 == [
        "-q",
        f"--record-durations={tmp_path / 'shard-1-durations.json'}",
        "--timeout=90",
    ]


def test_the_runner_accepts_the_space_separated_form(tmp_path: Path) -> None:
    """``--record-durations PATH`` 的写法也要认: 摘不出目标就没人改写, 两片照样撞车."""
    args = ["--record-durations", "tests/durations.json", "-q"]

    target = runner._split_record_target(args)

    assert target == Path("tests/durations.json")
    rewritten = runner._shard_pytest_args(args, target, tmp_path, 0)
    assert rewritten == [
        "--record-durations",
        str(tmp_path / "shard-0-durations.json"),
        "-q",
    ]


def test_the_runner_leaves_args_alone_without_a_record_target() -> None:
    """不记录耗时时参数原样透传(与改造前的行为一致)."""
    args = ["-q", "--timeout=90"]

    assert runner._split_record_target(args) is None
    assert runner._shard_pytest_args(args, None, Path("nowhere"), 3) == args


def test_the_runner_merges_the_shard_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """收尾把各片记录合并成一份: 片失败不拦合并(用例结论与耗时数据互不拖累)."""
    stamp = datetime(2026, 10, 7, 12, 0, 0, tzinfo=UTC)
    durations.write(
        tmp_path / "shard-0-durations.json", {_NODE: 1.0}, recorded_at=stamp
    )
    durations.write(
        tmp_path / "shard-1-durations.json",
        {"tests/integration/test_y.py::test_b": 2.5},
        recorded_at=stamp,
    )
    target = tmp_path / "merged.json"

    runner._merge_recorded_durations(target, tmp_path, 2)

    assert durations.load(target) == {
        _NODE: 1.0,
        "tests/integration/test_y.py::test_b": 2.5,
    }
    assert "已合并 2 片、2 条用例" in capsys.readouterr().out


def test_the_runner_does_not_clobber_without_any_records(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """一份片记录都没有时不写目标: 硬崩溃后的空合并不该清掉既有数据."""
    target = tmp_path / "merged.json"

    runner._merge_recorded_durations(target, tmp_path, 2)

    assert not target.exists()
    assert "一份分片记录都没找到" in capsys.readouterr().err
