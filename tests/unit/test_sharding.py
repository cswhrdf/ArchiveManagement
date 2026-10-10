"""分片执行与结果合并的守卫用例.

分片是"把用例拆到多个作业并行跑"的地基: 万一漏掉或重复了用例, CI 会安静地少跑或多跑
一部分 —— 这种错误不会自己暴露, 所以这里把三条性质钉死: **不重不漏**、**确定**(同输入
同分片)、**均匀**(最慢的用例不能堆在一片)。此外还要锁住"结果真的被合并了":

- 分片参数必须与 CI 矩阵一致(片数对不上会让一部分用例静默不跑);
- 合并脚本要把各片结果搬进一份目录, 缺片要提示而不是静默少数据。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import ci_workflow
import sharding

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("用例分片执行"),
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
    return ci_workflow.workflow_text()


def _shards_per_platform(text: str) -> dict[str, list[int]]:
    """从 pytest 矩阵里取出"每个平台有哪些片号"."""
    shards: dict[str, list[int]] = {}
    for entry in ci_workflow.matrix_entries(text, "pytest"):
        shards.setdefault(entry["platform"], []).append(int(entry["shard"]))
    return shards


def test_ci_shard_count_matches_the_matrix() -> None:
    """每个平台的片数与它的 --shard-count 必须一致, 且片号连续(否则有用例静默不跑)."""
    text = _workflow_text()
    shards = _shards_per_platform(text)

    hint = "pytest 命令要用矩阵里的片数, 不能写死一个数字"
    assert "--shard-count ${{ matrix.shards }}" in text, hint
    assert re.findall(r"--shard-count (\d+)", text) == [], hint

    for platform, plan in shards.items():
        assert sorted(plan) == list(range(len(plan))), (
            f"{platform} 的片号必须是 0..N-1: {sorted(plan)}"
        )
    # 每条矩阵条目自报的 shards 要与那个平台实际列出的片号个数一致。
    for entry in ci_workflow.matrix_entries(text, "pytest"):
        assert int(entry["shards"]) == len(shards[entry["platform"]]), (
            f"{entry['platform']} 的 shards 与列出条数对不上"
        )


def test_ci_matrix_runners_match_the_platform_labels() -> None:
    """矩阵里的 runner 与平台名必须是同一台机器: 错配就是报告里的假归属.

    报告里的"环境"由矩阵的 platform 决定(报告作业固定跑在 Ubuntu 上, 不能再读
    runner.os), 所以这一对一旦写岔, 一个平台的结论就会被挂到另一个环境里 ——
    而这种错不会让任何一步失败。
    """
    text = _workflow_text()
    entries = [
        *ci_workflow.matrix_entries(text, "pytest"),
        *ci_workflow.matrix_entries(text, "pytest-report"),
    ]

    for entry in entries:
        assert ci_workflow.OS_PLATFORMS[entry["os"]] == entry["platform"], (
            f"{entry['os']} 上跑的不是 {entry['platform']}"
        )


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
    assert "pytest" in ci_workflow.needs_of(text, "pytest-report"), (
        "报告作业要等 pytest 作业跑完(否则报告只反映最慢的那片)"
    )

    # 汇总作业要等齐**所有**产出结论的作业: 漏一个就会漏收它的产物(报告里少一块),
    # 或者在该作业上传完之前就去下载(拿到半份)。新增产出 Allure 结果的作业时一起改这里。
    # 断言只看标识符集合, 不管 YAML 是写成一行还是摊成多行(编辑器会按自己的风格重排)。
    # 平台专属检查/静态分析/性能基准都并在 pytest 与 quality 里, 所以清单里没有它们的名字。
    # `changes` 是那个轻量路径过滤作业: 它决定这轮要不要跑重量级作业(见
    # test_ci_cancels_superseded_runs_and_skips_docs_only_pushes)。
    assert ci_workflow.needs_of(text, "allure-summary") == {
        "changes",
        "quality",
        "pytest-report",
        "security",
    }, "公共检查与性能基准在 quality 里, 所以不需要单独的作业名"


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
    assert [item.files for item in summary.per_source] == [1, 1]
    assert [item.results for item in summary.per_source] == [1, 1]
    assert [item.shard for item in summary.per_source] == ["0", "1"]
    assert summary.copied == 2
    assert summary.results == 2
    assert summary.overwritten == 0


def test_merge_reports_a_shard_that_produced_nothing(tmp_path: Path) -> None:
    """空分片要被计数(0 个文件), 而不是从统计里消失."""
    first = tmp_path / "allure-results-shard-0"
    empty = tmp_path / "allure-results-shard-1"
    _write_result(first, "aaa-result.json")
    empty.mkdir()

    summary = merger.merge_directories([first, empty], output=tmp_path / "out")

    assert [item.files for item in summary.per_source] == [1, 0]
    assert [item.results for item in summary.per_source] == [1, 0]


def test_manifest_records_every_shard_with_its_counts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """产物清单要把逐分片的文件数/结果数与合计写下来(报告就靠它对数)."""
    for index, names in enumerate((("aaa", "bbb"), ("ccc",), ())):
        directory = tmp_path / f"allure-results-shard-{index}"
        directory.mkdir()
        for name in names:
            _write_result(directory, f"{name}-result.json")
    manifest = tmp_path / "allure-manifest.json"

    exit_code = merger.main(
        [
            "--output",
            str(tmp_path / "out"),
            "--platform",
            "Linux",
            "--expect-shards",
            "0,1,2",
            "--manifest",
            str(manifest),
            str(tmp_path / "allure-results-shard-*"),
        ]
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    assert exit_code == 0
    assert payload["platform"] == "Linux"
    assert payload["expected_shards"] == ["0", "1", "2"]
    assert payload["missing_shards"] == []
    assert [item["results"] for item in payload["sources"]] == [2, 1, 0]
    assert payload["total_results"] == 3
    assert payload["total_files"] == 3
    # 日志里逐片给出"文件数(结果数, 片号)": 少一片能从日志一眼看出来。
    printed = capsys.readouterr().out
    assert "2 条结果, 片 0" in printed
    assert "0 条结果, 片 2" in printed


def test_manifest_flags_a_shard_that_never_arrived(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """声明必须有而没找到的片号要写进清单并提醒 —— 这是"少一片"的唯一可靠信号."""
    (tmp_path / "allure-results-shard-0").mkdir()
    (tmp_path / "allure-results-shard-2").mkdir()
    _write_result(tmp_path / "allure-results-shard-0", "aaa-result.json")
    _write_result(tmp_path / "allure-results-shard-2", "ccc-result.json")
    manifest = tmp_path / "allure-manifest.json"

    exit_code = merger.main(
        [
            "--output",
            str(tmp_path / "out"),
            "--expect-shards",
            "0,1,2",
            "--manifest",
            str(manifest),
            str(tmp_path / "allure-results-shard-*"),
        ]
    )
    payload = json.loads(manifest.read_text(encoding="utf-8"))

    # 合并本身不算失败(报告还要照生成): 结论由校验脚本给。
    assert exit_code == 0
    assert payload["missing_shards"] == ["1"]
    assert payload["total_results"] == 2
    assert "缺 1" in capsys.readouterr().err


def test_ci_records_shard_counts_in_a_manifest() -> None:
    """分片条数要写成产物清单并随平台产物上传, 最后由汇总作业对齐.

    少一片时合并照常成功、报告只是安静地少一部分用例(环境、通过率、格式自检全看不出来),
    所以这份清单是唯一判据: 声明期望的片号、记下逐片条数、上传、汇总后对齐。
    """
    text = _workflow_text()
    shards = _shards_per_platform(text)
    declared = {
        entry["platform"]: entry["expect_shards"]
        for entry in ci_workflow.matrix_entries(text, "pytest-report")
    }

    assert set(declared) == set(shards), (
        "报告作业的平台必须与 pytest 矩阵一致(少一个平台的报告就没人生成)"
    )
    for platform, plan in shards.items():
        expected = ",".join(str(index) for index in sorted(plan))
        assert declared[platform] == expected, (
            f"{platform} 声明的片号 {declared[platform]!r} 与矩阵 {expected!r} 不一致"
        )
    assert "--expect-shards ${{ matrix.expect_shards }}" in text, (
        "清单声明的片号要来自矩阵(不一致等于白声明)"
    )
    assert '--platform "${{ matrix.platform }}"' in text, (
        "清单要记下是哪个平台; 报告作业跑在 Ubuntu 上, 不能用 runner.os"
    )
    assert "--manifest allure-manifest.json" in text, "报告作业要把清单写下来"
    # 清单要随平台的产物上传, 否则汇总作业收不到。
    upload = text.split("- name: Upload Allure resources", 1)[1]
    assert "allure-manifest.json" in upload.split("- name:", 1)[0]
    # 汇总作业收集各平台的清单, 并交给自检脚本与最终条数对齐。
    assert "Collect shard manifests" in text
    assert '--manifest "allure-manifests/*.json"' in text


def test_single_shard_platform_downloads_into_its_own_shard_directory() -> None:
    """单片平台的产物必须**直接落进那个分片目录名**里.

    ``download-artifact`` 只在匹配到**多个**产物时才逐个建"以产物名命名的子目录"; 只匹配到
    一个时它把内容直接解到 ``path`` 里, 不建那层目录。macOS 只有 1 片, 正好撞上这条:
    ``path: .`` 时那 12689 个结果文件摊在工作区根, 合并脚本按"分片目录"找就一个都匹配不到
    —— macOS 的结论与覆盖率**全程没进报告** (2026-10-01 run 36754229023 实测: 分片作业自己
    2206 passed、下载也成功且 digest 校验通过, 而合并报"以下模式没匹配到目录")。

    所以单片平台的 ``download_path`` 必须正好是 ``allure-results-<os>-<片号>``; 多片平台仍用
    ``.``(各片各自成目录)。这条守卫读的是工作流文本 —— 它拦的正是"把某个平台减到 1 片"这种
    改动: 片数一减, 下载布局就变了。
    """
    text = _workflow_text()
    entries = ci_workflow.matrix_entries(text, "pytest-report")
    assert entries, "报告作业的矩阵读不到"

    for entry in entries:
        shards = [part.strip() for part in entry["expect_shards"].split(",")]
        path = entry.get("download_path")
        assert path, f"{entry['platform']} 没写 download_path(产物下载落地的目录)"
        if len(shards) == 1:
            assert path == f"allure-results-{entry['os']}-{shards[0]}", (
                f"{entry['platform']} 只有 1 片: 产物会被直接解到 path 里, 而合并脚本按"
                f"分片目录找 —— path 必须正好是 allure-results-{entry['os']}-{shards[0]}, "
                f"现在是 {path!r}"
            )
        else:
            assert path == ".", (
                f"{entry['platform']} 有 {len(shards)} 片, 各片要各自成目录, path 该是 '.'"
            )

    # 只声明字段而不用等于没改: 下载步骤必须真的照它落地。
    assert "path: ${{ matrix.download_path }}" in text, (
        "产物下载的落地目录要来自矩阵(单片平台与多片平台的落地方式不同)"
    )


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
