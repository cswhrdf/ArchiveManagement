"""分片执行与结果合并的守卫用例.

分片是"把用例拆到多个作业并行跑"的地基: 万一漏掉或重复了用例, CI 会安静地少跑或多跑
一部分 —— 这种错误不会自己暴露, 所以这里把三条性质钉死: **不重不漏**、**确定**(同输入
同分片)、**均匀**(最慢的用例不能堆在一片)。此外还要锁住"结果真的被合并了":

- 分片参数必须与 CI 矩阵一致(片数对不上会让一部分用例静默不跑);
- 合并脚本要把各片结果搬进一份目录, 缺片要提示而不是静默少数据。
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import sharding

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("用例分片执行"),
    pytest.mark.layer("unit"),
]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_WORKFLOW = _REPO_ROOT / ".github" / "workflows" / "ci.yml"


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
merger = _load_script("merge_allure_results")


# 造一批带有"重慢用例"的样本: 集成用例(每个 2s)才是耗时的来源。
_HEAVY = [
    f"tests/integration/test_gui_buttons.py::test_case_{index}" for index in range(40)
]
_LIGHT = [f"tests/unit/test_models.py::test_case_{index}" for index in range(400)]
_OTHER = [
    f"tests/security/test_path_escapes.py::test_case_{index}" for index in range(10)
]
_SAMPLE = _HEAVY + _LIGHT + _OTHER


def test_shard_plan_covers_every_case_exactly_once() -> None:
    """不重不漏: 各片拼起来正好是全集(顺序无关)."""
    buckets = sharding.shard_plan(_SAMPLE, count=4)

    merged = [node for bucket in buckets for node in bucket]
    assert len(merged) == len(set(merged))
    assert sorted(merged) == sorted(_SAMPLE)


def test_shard_plan_is_deterministic() -> None:
    """确定: 同样的输入两次得到同样的分片(否则两次运行的对照就失去意义)."""
    first = sharding.shard_plan(_SAMPLE, count=3)
    second = sharding.shard_plan(list(reversed(_SAMPLE)), count=3)

    assert first == second


def test_shard_plan_balances_the_slow_cases() -> None:
    """均匀: 慢用例被摊开, 各片权重接近理想值(最慢的优先贪心装箱)."""
    buckets = sharding.shard_plan(_SAMPLE, count=4)

    loads = sorted(sharding.load_of(bucket) for bucket in buckets)
    ideal = sharding.load_of(_SAMPLE) / 4
    assert loads[0] <= ideal <= loads[-1]
    # 贪心装箱对本样本能做到"最重的一片不超过理想值的 1.1 倍".
    assert loads[-1] <= ideal * 1.1


def test_shard_plan_rejects_a_bad_count() -> None:
    """分片数必须 ≥ 1: 0 片会让"所有用例都跑不到"变成静默的成功."""
    with pytest.raises(ValueError, match="至少为 1"):
        sharding.shard_plan(_SAMPLE, count=0)


def test_cost_of_uses_directory_weights() -> None:
    """权重按目录估: 集成用例明显贵于单元用例, 没见过的目录有兜底值."""
    assert sharding.cost_of("tests/integration/test_x.py::test_a") > sharding.cost_of(
        "tests/unit/test_x.py::test_a"
    )
    assert sharding.cost_of("tests/unit/test_x.py::test_a") > 0
    assert sharding.cost_of(r"tests\unit\test_x.py::test_a") == sharding.cost_of(
        "tests/unit/test_x.py::test_a"
    )
    assert sharding.cost_of("somewhere/test_new.py::test_a") == sharding.DEFAULT_COST


# ------------------------------------------------------------ CI 分片参数一致性


def _workflow_text() -> str:
    """读取 CI 工作流文本(与 test_report_verification.py 一样直接看文本)."""
    return _WORKFLOW.read_text(encoding="utf-8")


def _matrix_shards(text: str) -> list[int]:
    """取出 pytest 矩阵里的分片列表."""
    match = re.search(r"^\s*shard: \[([0-9,\s]+)\]$", text, re.MULTILINE)
    assert match is not None, "CI 里找不到分片矩阵(shard: [...])"
    return [int(value) for value in re.findall(r"\d+", match.group(1))]


def test_ci_shard_count_matches_the_matrix() -> None:
    """片数与 --shard-count 必须一致: 矩阵多一片会让那部分用例永远不跑."""
    text = _workflow_text()
    shards = _matrix_shards(text)

    counts = {int(value) for value in re.findall(r"--shard-count (\d+)", text)}
    hint = (
        "CI 的 shard 矩阵与 --shard-count 不一致; "
        f"矩阵={shards}, --shard-count={sorted(counts)}"
    )
    assert counts == {len(shards)}, hint
    assert shards == list(range(len(shards))), "分片编号必须是 0..N-1"


def test_ci_passes_the_shard_index_to_pytest() -> None:
    """矩阵编号要真的传给 pytest(--shard-index), 否则每个作业都跑全量."""
    text = _workflow_text()

    hint = "pytest 命令行里没有引用矩阵的分片编号"
    assert "--shard-index ${{ matrix.shard }}" in text, hint


def test_ci_merges_shard_results_with_the_script() -> None:
    """分片结果必须交给合并脚本, 并等 pytest 作业跑完(否则报告只反映一片)."""
    text = _workflow_text()

    hint = "CI 没有用 scripts/merge_allure_results.py 合并分片结果"
    assert "scripts/merge_allure_results.py" in text, hint
    assert re.search(r"needs: \[pytest\]", text) is not None
    assert "needs: [quality, pytest-report, performance, security]" in text


# ---------------------------------------------------------------- 结果合并脚本


def _write_result(directory: Path, name: str) -> Path:
    """造一个 Allure 结果文件, 返回它的路径."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("{}\n", encoding="utf-8")
    return path


def test_merge_expands_glob_patterns(tmp_path: Path) -> None:
    """模式展开: 通配符在 pwsh 里不会自己展开, 所以必须由脚本处理."""
    for name in ("allure-results-shard-0", "allure-results-shard-1"):
        (tmp_path / name).mkdir()

    found, missing = merger.expand_sources(
        [
            str(tmp_path / "allure-results-shard-*"),
            str(tmp_path / "allure-results-none-*"),
        ]
    )

    assert [path.name for path in found] == [
        "allure-results-shard-0",
        "allure-results-shard-1",
    ]
    assert missing == [str(tmp_path / "allure-results-none-*")]


def test_merge_copies_every_shard_file(tmp_path: Path) -> None:
    """各片的结果都搬进同一目录, 逐片计数(少一片能从日志看出来)."""
    first = tmp_path / "allure-results-shard-0"
    second = tmp_path / "allure-results-shard-1"
    _write_result(first, "aaa-result.json")
    _write_result(second, "bbb-result.json")
    output = tmp_path / "allure-results"

    summary = merger.merge_directories([first, second], output=output)

    assert sorted(path.name for path in output.iterdir()) == [
        "aaa-result.json",
        "bbb-result.json",
    ]
    assert [count for _source, count in summary.per_source] == [1, 1]
    assert summary.copied == 2
    assert summary.overwritten == 0


def test_merge_reports_a_shard_that_produced_nothing(tmp_path: Path) -> None:
    """空分片要被计数(0 个文件), 而不是从统计里消失."""
    first = tmp_path / "allure-results-shard-0"
    empty = tmp_path / "allure-results-shard-1"
    _write_result(first, "aaa-result.json")
    empty.mkdir()

    summary = merger.merge_directories([first, empty], output=tmp_path / "out")

    assert [count for _source, count in summary.per_source] == [1, 0]


def test_merge_warns_about_colliding_files(tmp_path: Path) -> None:
    """重名覆盖要计数: 分片结果被复用/编号撞车时不能静默盖掉一条结果."""
    first = tmp_path / "allure-results-shard-0"
    second = tmp_path / "allure-results-shard-1"
    _write_result(first, "same-result.json")
    _write_result(second, "same-result.json")

    summary = merger.merge_directories([first, second], output=tmp_path / "out")

    assert summary.overwritten == 1


def test_merge_main_returns_one_when_nothing_matched(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """一个文件都没搬进来时返回 1: 报告作业要因此红掉, 而不是生成空报告."""
    exit_code = merger.main(
        ["--output", str(tmp_path / "out"), str(tmp_path / "allure-results-missing-*")]
    )

    assert exit_code == 1
    assert "没匹配到目录" in capsys.readouterr().err


def test_merge_main_reports_the_merged_shards(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """正常合并返回 0, 并把每片的文件数与人可读的合计打进日志."""
    for index in range(2):
        _write_result(
            tmp_path / f"allure-results-shard-{index}", f"{index}-result.json"
        )

    exit_code = merger.main(
        [
            "--output",
            str(tmp_path / "allure-results"),
            str(tmp_path / "allure-results-shard-*"),
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "已合并 2 个分片目录, 2 个文件" in output, f"合并日志: {output!r}"
    assert "allure-results-shard-0: 1 个文件" in output, f"合并日志: {output!r}"
