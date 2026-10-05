"""守卫: Allure 历史文件的自动修复(去重 / 重键 / 兜底重置).

为什么要有这些用例: 历史趋势是"静默的"失败 —— 键的形状一变, 报告里所有旧点都会消失,
而没有任何东西会报错(2026-10-05 实测: 7231 条结果里 7226 条历史长度为 1, 那一条还是本次
运行自己重复追加的快照)。所以把三种走向都钉住: 该连上的连上、该丢的丢、连不上就重置并
写明; 顺带钉住"幂等"与"没修东西就不留附件" —— 前者决定它能不能每轮都跑, 后者决定报告里
会不会挂着一份过期的记录。

CLI 侧的两处接线也在这里守: ``allurerc.mjs`` 的条件附件、``ci.yml`` 里"修复要排在生成之前"。
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("Allure 报告与历史"),
    pytest.mark.layer("unit"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ALLURE_CONFIG = REPO_ROOT / "allurerc.mjs"
REPAIR_NOTE = "allure-history-repair.md"


def _load_script(name: str) -> ModuleType:
    """按路径加载 ``scripts/`` 下的脚本(它们不是包, 只能用 importlib 按文件加载)."""
    spec = importlib.util.spec_from_file_location(
        name, REPO_ROOT / "scripts" / f"{name}.py"
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, module)
    spec.loader.exec_module(module)
    return module


def _job_blocks(workflow: str) -> dict[str, str]:
    """把 ci.yml 按顶层作业切成 ``{作业名: 文本}``(只要带 ``runs-on`` 的才算作业).

    与 ``tests/unit/test_ci_diagnostics.py`` 里同名的那两个是同源的: 跨测试模块 import 会让
    mypy 报 "tests 不是包", 所以这里各留一份(它们很短, 而且只用到正则/切片)。
    """
    matches = list(re.finditer(r"^  ([a-z][a-z0-9_-]+):$", workflow, re.MULTILINE))
    blocks: dict[str, str] = {}
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(workflow)
        blocks[match.group(1)] = workflow[match.start() : end]
    return {name: text for name, text in blocks.items() if "runs-on:" in text}


def _step(job: str, name: str) -> str:
    """切出一个步骤的文本(到下一个步骤名为止): 条件是写在里面的, 不切出来会看成别的步骤的."""
    start = job.index(f"name: {name}")
    end = job.find("\n      - name: ", start)
    return job[start : end if end != -1 else len(job)]


def _case_hash(index: int) -> str:
    """造一个像样的用例哈希(32 位十六进制, 与 Allure 的写法同形)."""
    return f"{index:032x}"


def _snapshot(
    *,
    keys: list[str],
    timestamp_ms: int,
    index: int = 0,
    environments: dict[str, str] | None = None,
) -> dict[str, object]:
    """造一份历史快照(载荷字段只保留脚本会用到的那些).

    ``environments`` 给个别键补上条目自己的环境(重键消歧用): 旧快照写显示名
    (``macOS``)、新快照写 id(``macos``), 与真实文件一致。
    """
    results: dict[str, object] = {}
    for position, key in enumerate(keys):
        entry: dict[str, object] = {"name": f"case-{position}", "status": "passed"}
        if environments and key in environments:
            entry["environment"] = environments[key]
        results[key] = entry
    return {
        "uuid": _case_hash(index + 900),
        "timestamp": timestamp_ms,
        "testResults": results,
    }


def _write_history(path: Path, snapshots: list[dict[str, object]]) -> None:
    """把快照写成历史文件(一行一份)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "\n".join(json.dumps(item) for item in snapshots)
    path.write_text(f"{text}\n", encoding="utf-8")


def _read_keys(path: Path) -> list[set[str]]:
    """读回历史文件里每份快照的键集合."""
    return [
        set(json.loads(line)["testResults"])
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _current_keys(module: ModuleType, path: Path) -> list[str]:
    """读回历史文件里最新那份快照的键(排序后, 便于断言)."""
    return sorted(module.load_snapshots(path)[0][-1].keys())


def test_duplicate_snapshots_of_one_run_are_merged(tmp_path: Path) -> None:
    """同一轮运行被追加的两份快照要合成一份: 否则"本次结果"会冒充"上一次历史"."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    keys = [f"{_case_hash(position)}.{_case_hash(7)}" for position in range(10)]
    _write_history(
        history,
        [
            _snapshot(keys=keys, timestamp_ms=1_790_008_444_155, index=0),
            _snapshot(keys=keys, timestamp_ms=1_790_008_449_814, index=1),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.duplicates == 1
    assert report.kept == 1
    assert len(snapshots) == 1


def test_a_slightly_different_twin_still_counts_as_the_same_run(tmp_path: Path) -> None:
    """两次生成之间多跑/少跑几条用例, 仍然是同一轮(实测两份相差 5 条)."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    keys = [f"{_case_hash(position)}.{_case_hash(7)}" for position in range(1000)]
    _write_history(
        history,
        [
            _snapshot(keys=keys, timestamp_ms=1_790_008_444_155, index=0),
            _snapshot(
                keys=[*keys, f"{_case_hash(9999)}.{_case_hash(7)}"],
                timestamp_ms=1_790_008_470_000,
                index=1,
            ),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.duplicates == 1
    # 留信息更全的那一份(1001 条), 不是随便留一份.
    assert len(snapshots[0].keys()) == 1001


def test_two_runs_hours_apart_are_never_merged(tmp_path: Path) -> None:
    """不同轮运行(相隔小时级)哪怕键完全一样也不能合并 —— 那是两条历史点."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    keys = [f"{_case_hash(position)}.{_case_hash(7)}" for position in range(10)]
    _write_history(
        history,
        [
            _snapshot(keys=keys, timestamp_ms=1_790_008_444_155, index=0),
            _snapshot(keys=keys, timestamp_ms=1_790_015_444_155, index=1),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.duplicates == 0
    assert len(snapshots) == 2


def test_old_keys_are_rekeyed_to_the_current_shape(tmp_path: Path) -> None:
    """旧快照的键少一段(采用 environments 之前)时, 按唯一前缀补成当前形状.

    补完之后旧快照就**能连上**了 —— 这正是"过去的历史重新可见"的关键一步。
    """
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    old_keys = [f"{_case_hash(1)}.{_case_hash(7)}", f"{_case_hash(2)}.{_case_hash(7)}"]
    current_keys = [f"{key}.{_case_hash(8)}" for key in old_keys]
    _write_history(
        history,
        [
            _snapshot(keys=old_keys, timestamp_ms=1_790_000_000_000, index=0),
            _snapshot(keys=current_keys, timestamp_ms=1_790_010_000_000, index=1),
        ],
    )
    report, snapshots = module.repair(history)
    assert (report.old_shape, report.new_shape) == (2, 3)
    assert report.rekeyed == 2
    assert report.unmatched == 0
    assert report.connected == 1
    assert set(snapshots[0].keys()) == set(current_keys)


def test_ambiguous_keys_are_dropped_not_guessed(tmp_path: Path) -> None:
    """一个旧键能补成多个当前键时(同一用例的两个环境)不许猜: 丢掉并计数.

    条目自己没写 environment 时才会走到这里 —— 写了的看下一条用例。
    """
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    ambiguous = f"{_case_hash(1)}.{_case_hash(7)}"
    clean = f"{_case_hash(2)}.{_case_hash(7)}"
    current_keys = [
        f"{ambiguous}.{_case_hash(8)}",
        f"{ambiguous}.{_case_hash(9)}",
        f"{clean}.{_case_hash(8)}",
    ]
    _write_history(
        history,
        [
            _snapshot(keys=[ambiguous, clean], timestamp_ms=1_790_000_000_000, index=0),
            _snapshot(keys=current_keys, timestamp_ms=1_790_010_000_000, index=1),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.unmatched == 1
    assert report.rekeyed == 1
    assert set(snapshots[0].keys()) == {f"{clean}.{_case_hash(8)}"}


def test_platform_ambiguity_is_resolved_by_environment(tmp_path: Path) -> None:
    """旧键对上三个平台的当前键时, 按条目自己的 environment 对准, 不许整条丢掉.

    2026-10-05 的教训: 采用 environments 之后一个旧键(两段)会同时命中同一用例在
    windows/linux/macos 三个环境下的键(三段)。只按"唯一前缀"判会把**全部**旧条目当成
    "补不出唯一值"丢掉, 连接数归零, 兜底直接清空整份历史 —— 修复步骤反而变成毁数据的
    那一步。旧快照的环境写显示名(macOS), 新快照写 id(macos), 比较时统一小写。
    """
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    shared = f"{_case_hash(1)}.{_case_hash(7)}"
    current = {
        f"{shared}.{_case_hash(8)}": "windows",
        f"{shared}.{_case_hash(9)}": "macos",
        f"{shared}.{_case_hash(10)}": "linux",
    }
    _write_history(
        history,
        [
            _snapshot(
                keys=[shared],
                timestamp_ms=1_790_000_000_000,
                index=0,
                environments={shared: "macOS"},
            ),
            _snapshot(
                keys=list(current),
                timestamp_ms=1_790_010_000_000,
                index=1,
                environments=current,
            ),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.unmatched == 0
    assert report.rekeyed == 1
    assert not report.reset, "对得上环境时不许触发清空"
    assert set(snapshots[0].keys()) == {f"{shared}.{_case_hash(9)}"}


def test_keys_beyond_the_current_shape_are_dropped_not_downgraded(
    tmp_path: Path,
) -> None:
    """当前形状比旧键**短**时, 多一段的键不许砍成前缀(会并掉平台之间的区别).

    只能丢弃并计数 —— 于是旧快照一条都连不上, 走兜底: 清空并写明。降级成前缀看似
    "保住了历史", 实际把三个平台的旧点并到一个键上, 趋势会凭空多出错误数据。
    """
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    old_key = f"{_case_hash(1)}.{_case_hash(7)}.{_case_hash(8)}"
    current_key = f"{_case_hash(1)}.{_case_hash(7)}"
    _write_history(
        history,
        [
            _snapshot(keys=[old_key], timestamp_ms=1_790_000_000_000, index=0),
            _snapshot(keys=[current_key], timestamp_ms=1_790_010_000_000, index=1),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.unmatched == 1
    assert snapshots == [], "重置路径下不返回快照(写回时清空文件)"
    assert report.reset, "旧快照一条都连不上时按兜底走: 清空并写明"


def test_mixed_snapshots_keep_keys_already_in_the_current_shape(
    tmp_path: Path,
) -> None:
    """混合形状的快照里, 已是当前段数的键要原样保留(不许再按前缀改名/重复计数)."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    old_key = f"{_case_hash(1)}.{_case_hash(7)}"
    current_key = f"{old_key}.{_case_hash(8)}"
    _write_history(
        history,
        [
            _snapshot(
                keys=[old_key, current_key],
                timestamp_ms=1_790_000_000_000,
                index=0,
            ),
            _snapshot(keys=[current_key], timestamp_ms=1_790_010_000_000, index=1),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.rekeyed == 1, "只有旧形状那条键改名, 已是当前形状的不动"
    assert set(snapshots[0].keys()) == {current_key}


def test_unmatched_entries_are_dropped_and_counted(tmp_path: Path) -> None:
    """补不出对应关系的条目直接丢掉(留着也永远匹配不上), 并在记录里报数."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    gone = f"{_case_hash(404)}.{_case_hash(404)}"
    kept = f"{_case_hash(2)}.{_case_hash(7)}"
    _write_history(
        history,
        [
            _snapshot(keys=[gone, kept], timestamp_ms=1_790_000_000_000, index=0),
            _snapshot(
                keys=[f"{kept}.{_case_hash(8)}"],
                timestamp_ms=1_790_010_000_000,
                index=1,
            ),
        ],
    )
    report, snapshots = module.repair(history)
    assert report.unmatched == 1
    assert report.rekeyed == 1
    assert set(snapshots[0].keys()) == {f"{kept}.{_case_hash(8)}"}


def test_history_is_reset_when_nothing_connects(tmp_path: Path) -> None:
    """重键之后一份旧快照都连不上 ⇒ 清空重新开始, 并把原因写进记录.

    这是"身份算法本身变了"(不是多一个末尾维度)时的兜底: 补不出对应关系就只能重来。
    """
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    _write_history(
        history,
        [
            _snapshot(
                keys=[f"{_case_hash(1)}.{_case_hash(7)}"],
                timestamp_ms=1_790_000_000_000,
                index=0,
            ),
            _snapshot(
                keys=[f"{_case_hash(500)}.{_case_hash(501)}.{_case_hash(502)}"],
                timestamp_ms=1_790_010_000_000,
                index=1,
            ),
        ],
    )
    note = tmp_path / REPAIR_NOTE
    # 先看判定(repair 只算不改文件).
    report, snapshots = module.repair(history)
    assert report.reset is True
    assert report.reason, "重置必须写明原因"
    assert snapshots == []
    # 再看端到端: main 会把历史清空, 并写下带原因的记录.
    assert module.main(["--history", str(history), "--note", str(note)]) == 0
    assert history.read_text(encoding="utf-8") == "", "兜底就是清空历史重新开始"
    text = note.read_text(encoding="utf-8")
    assert "重置" in text
    assert "为什么重置" in text


def test_unreadable_lines_mean_reset(tmp_path: Path) -> None:
    """文件换成别的容器格式(一整个 JSON 数组)时按"认不出来"处理, 而不是当空历史."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    history.write_text('[{"testResults": {}}]', encoding="utf-8")
    report, _snapshots = module.repair(history)
    assert report.reset is True
    assert "认不出来" in report.reason


def test_repair_is_idempotent(tmp_path: Path) -> None:
    """修完再修一次必须什么都不做(它要每轮都跑, 不能越修越乱/越修越慢)."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    old = [f"{_case_hash(1)}.{_case_hash(7)}", f"{_case_hash(2)}.{_case_hash(7)}"]
    _write_history(
        history,
        [
            _snapshot(keys=old, timestamp_ms=1_790_000_000_000, index=0),
            _snapshot(keys=old, timestamp_ms=1_790_000_000_500, index=1),
            _snapshot(
                keys=[f"{key}.{_case_hash(8)}" for key in old],
                timestamp_ms=1_790_010_000_000,
                index=2,
            ),
        ],
    )
    note = tmp_path / REPAIR_NOTE
    assert module.main(["--history", str(history), "--note", str(note)]) == 0
    assert note.exists(), "修过东西就要留下记录"
    first = history.read_text(encoding="utf-8")
    assert module.main(["--history", str(history), "--note", str(note)]) == 0
    assert history.read_text(encoding="utf-8") == first, "第二次不该再改文件"
    assert not note.exists(), "没修东西就要删掉过期记录(否则报告里挂着旧结论)"


def test_no_history_means_no_note(tmp_path: Path) -> None:
    """历史文件不存在(第一次跑)时什么都不做, 也不留记录."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    note = tmp_path / REPAIR_NOTE
    assert module.main(["--history", str(history), "--note", str(note)]) == 0
    assert not note.exists()
    assert not history.exists()


def test_repair_note_is_a_conditional_global_attachment() -> None:
    """修复记录要按文件在不在条件收进首页「全局附件」, 并加进 .gitignore.

    条件收的理由与"失败现场"那份一样: 没修东西时报告里不该挂着一条空白记录; 而它又是
    CI 生成物, 不忽略的话本地跑一次就会污染工作区。
    """
    config = ALLURE_CONFIG.read_text(encoding="utf-8")
    assert "globalAttachments:" in config, "配置里要有 globalAttachments"
    assert REPAIR_NOTE in config, "配置里要点名这份记录"
    assert f'existsSync("{REPAIR_NOTE}")' in config, (
        "这份记录要按文件在不在决定收不收(没修东西时不该出现)"
    )
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert REPAIR_NOTE in ignored


def test_ci_repairs_history_before_generating_the_report() -> None:
    """两处生成报告之前都要先修历史: 顺序反了等于没修.

    逐平台作业与汇总作业各自把历史拷回 ``.allure/history.jsonl``, 各自生成一份报告 ——
    两份都会用到历史, 所以两处都要修, 而且必须排在 ``allure generate`` 之前。
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")
    jobs = _job_blocks(workflow)
    # 拷回历史的那条命令(比只找 "history.jsonl" 精确: 注释里也出现过这个文件名).
    restore_command = (
        "cp .previous-allure-resources/.allure/history.jsonl .allure/history.jsonl"
    )
    checked = 0
    for job_name, job in jobs.items():
        if "allure generate" not in job or restore_command not in job:
            continue
        checked += 1
        repair = job.find("scripts/repair_allure_history.py")
        restore = job.find(restore_command)
        generate = job.find("allure generate")
        assert repair != -1, f"{job_name} 里要有一道历史修复"
        assert restore < repair < generate, (
            f"{job_name} 里的顺序应当是 拷回历史 -> 修历史 -> 生成报告"
        )
        step = _step(job, "Repair previous Allure history")
        assert "--allure-version" in step, "CI 用的是动态版本, 实际版本要记进修复记录"
    assert checked >= 2, "至少要有逐平台与汇总两处生成报告"


def test_the_note_names_the_version_and_the_reason(tmp_path: Path) -> None:
    """记录里要有 CLI 版本与"为什么重置" —— 动态版本下形状再变, 能一眼对上是哪次更新."""
    module = _load_script("repair_allure_history")
    history = tmp_path / "history.jsonl"
    keys = [f"{_case_hash(1)}.{_case_hash(7)}"]
    _write_history(
        history,
        [
            _snapshot(keys=keys, timestamp_ms=1_790_000_000_000, index=0),
            _snapshot(
                keys=[f"{key}.{_case_hash(8)}" for key in keys],
                timestamp_ms=1_790_010_000_000,
                index=1,
            ),
        ],
    )
    report, _snapshots = module.repair(history, allure_version="3.20.0")
    text = module.repair_text(report)
    assert "3.20.0" in text
    assert "retryHash" in text
    assert re.search(r"重键 \d+ 条", text), "要报数, 不能只说'修好了'"
    assert _current_keys(module, history), "读回历史文件要能拿到最新快照的键"
