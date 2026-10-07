"""
质量项与本地钩子: severity 声明、质量项通过/失败双态、复杂度门与 ruff 上限一致、pre-commit 格式化重试与 cp1252 控制台。拆自 test_report_verification.py(见 docs/test-refactor-plan.md S8)。
"""

from __future__ import annotations

import io
import json
import re
import sys
from pathlib import Path

import pytest

from report_support import (
    _ALLURE_CONFIG,
    _PYPROJECT,
    _REPO_ROOT,
    _WORKFLOW,
    _Layout,
    _load_script,
    _write_result,
    verifier,
)

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


_PRE_COMMIT = _REPO_ROOT / ".pre-commit-config.yaml"


def test_report_summary_items_declare_a_severity() -> None:
    """脚本生成的汇总项要带严重等级与环境标签, 名称里不再拼平台名.

    严重等级: 缺了会在报告里多出一个 ``no_severity`` 桶;
    环境标签: 缺了会被归到 ``default``, 按环境筛选时就看不到性能/安全/覆盖率结论;
    名称不拼平台: 平台现在由"环境"表达(见 docs/testing.md 第 5 节), 写进名称会让报告里
    多出三个同名的独立条目。
    """
    scripts = (
        _REPO_ROOT / "scripts" / "create_allure_coverage.py",
        _REPO_ROOT / "scripts" / "create_allure_summary.py",
    )

    for script in scripts:
        text = script.read_text(encoding="utf-8")
        assert '"name": "severity"' in text, f"{script.name} 未给汇总项写 severity 标签"
        assert '"name": "env"' in text, f"{script.name} 未给汇总项写 env 标签"

    summary = scripts[1].read_text(encoding="utf-8")
    # 标题的唯一来源是清单(scripts/allure_catalog.py), 写入处只引用它 —— 于是"标题里拼没拼
    # 平台名"只需要在一处盯住; 同时也要求写入处不要再各抄一份字面量(那样又会分叉出两份)。
    titles = {item.key: item.title for item in _load_script("allure_catalog").CATALOG}
    assert titles["performance"] == "Performance baseline"
    assert titles["security"] == "Security findings"
    assert 'title="Performance baseline"' not in summary, "标题不该在写入处再抄一份"
    for name in titles.values():
        assert not any(
            platform in name for platform in ("Windows", "Linux", "macOS")
        ), f"标题里不能拼平台名(平台由环境表达): {name}"


def test_quality_items_record_pass_and_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """质量门禁项: 逐项写成结果(状态跟随退出码), 带 env 标签与原始输出附件.

    用例用 ``sys.executable -c`` 冒充两项检查, 免得在用例里真跑 ruff/mypy
    (几秒到一分钟)。
    """
    module = _load_script("create_allure_quality")
    results = tmp_path / "allure-results"
    checks = (
        module.Check("ok", "OK check", (sys.executable, "-c", "print('all good')")),
        module.Check(
            "bad", "Bad check", (sys.executable, "-c", "import sys; sys.exit(3)")
        ),
    )
    monkeypatch.setattr(module, "CHECKS", checks)

    exit_code = module.main(["--results-dir", str(results)])

    assert exit_code == 1, "有检查未通过时脚本必须以非 0 退出(否则质量门禁形同虚设)"
    items = {
        str(payload["name"]): payload
        for payload in (
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted(results.glob("*-result.json"))
        )
    }
    assert set(items) == {"OK check", "Bad check"}

    passed = items["OK check"]
    failed = items["Bad check"]
    platform = module.platform_name()
    assert passed["status"] == "passed"
    assert failed["status"] == "failed"
    assert "退出码 3" in failed["statusDetails"]["message"]
    assert "all good" in passed["description"]

    for payload in items.values():
        labels = {label["name"]: label["value"] for label in payload["labels"]}
        assert labels["severity"] == "trivial"
        assert payload["attachments"], "原始输出要作为附件带进报告"
        assert (results / payload["attachments"][0]["source"]).is_file()
        # 公共检查归入显式声明的环境, 不带 os 标签与平台参数, 见
        # test_quality_items_use_the_declared_common_environment; 执行主机只写进描述。
        assert labels["env"] == module.QUALITY_ENVIRONMENT
        assert "os" not in labels
        assert payload.get("parameters", []) == []
        assert f"本次执行于 {platform}" in payload["description"]


def test_quality_items_cover_every_ci_gate() -> None:
    """三组门禁都在脚本里, 且 CI 分三处调用它们: 漏一项报告就缺项."""
    module = _load_script("create_allure_quality")
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    core = [check.key for check in module.CHECKS if check.group == "core"]
    platform = [check.key for check in module.CHECKS if check.group == "platform"]
    analysis = [check.key for check in module.CHECKS if check.group == "analysis"]
    assert core == ["ruff-check", "ruff-format", "mypy"]
    assert platform == ["mypy-win32", "mypy-darwin"]
    assert analysis == ["deptry", "bandit", "pip-audit", "radon", "xenon"]
    # 平台专属分支: 这两项必须带着 --platform, 否则等于把宿主平台又跑了一遍。
    for check in (item for item in module.CHECKS if item.key.startswith("mypy-")):
        assert "--platform" in check.command
        assert check.host_platform is not None, "平台专属检查要声明自己在哪个平台跑"
    assert "scripts/create_allure_quality.py --group core" in workflow
    assert "scripts/create_allure_quality.py --group platform" in workflow
    assert "scripts/create_allure_quality.py --group analysis" in workflow
    assert "allure-results-quality" in workflow
    assert "allure-results-analysis" in workflow
    # 旧的散装命令不应再单独出现(否则同一批检查会跑两遍).
    assert "run: uv run ruff check ." not in workflow
    assert "run: uv run mypy" not in workflow


def test_complexity_gate_matches_ruffs_mccabe_limit() -> None:
    """复杂度门槛与 pyproject 里 Ruff 的 mccabe 上限必须是同一个数值.

    Radon 比 Ruff 的 C901 多数 ``with``/``assert``/布尔运算, 所以门槛放宽就会与
    Ruff 分叉, 收紧则会违背"两把尺子同分"的约定 —— 这条守卫把两者钉在一起。
    """
    import tomllib

    module = _load_script("create_allure_quality")
    pyproject = tomllib.loads(_PYPROJECT.read_text(encoding="utf-8"))
    limit = pyproject["tool"]["ruff"]["lint"]["mccabe"]["max-complexity"]
    assert limit == module.MAX_COMPLEXITY
    # 10 分对应 Radon 的 B 级(11 分起是 C 级).
    rank = module.COMPLEXITY_RANK
    assert rank == "B"
    xenon = next(check for check in module.CHECKS if check.key == "xenon")
    assert "--max-absolute" in xenon.command
    assert rank in xenon.command


def test_pre_commit_covers_the_dependency_check() -> None:
    """deptry 是唯一同时进本地钩子与 CI 的新工具(用户要求), 配置不能掉."""
    config = _PRE_COMMIT.read_text(encoding="utf-8")
    assert "uv run deptry" in config
    # 依赖清单变了也要重查, 所以钩子的触发范围不止 .py。
    assert "pyproject\\.toml" in config
    assert "uv\\.lock" in config


def test_pre_commit_formats_and_restages_instead_of_only_checking() -> None:
    """排版钩子要"就地排版 + 自己重新暂存", 不能只是 ``--check``.

    出处(用户 2026-10-05): ``--check`` 只报哪个文件会被改, 于是每次都是"提交 -> 被拦下 ->
    手工 ``ruff format`` -> ``git add`` -> 再提交"。钩子把结果加回索引之后, pre-commit 不算它
    "弄脏文件", **这次提交直接带上排版好的内容**(与 ``scripts/compact_json.py`` 同一套做法,
    那一条在 docs/development.md 里已有说明)。

    判据只看这个钩子的那一段: 它的 entry 指向那个脚本, 且不再带 ``--check`` —— CI 那份
    ``ruff format --check .`` 是另一条路径, 不受影响(它要的就是只读判据)。
    """
    config = _PRE_COMMIT.read_text(encoding="utf-8")
    start = config.index("id: ruff-format")
    end = config.find("\n      - id: ", start)
    block = config[start : end if end != -1 else len(config)]
    assert "scripts/ruff_format_and_stage.py" in block, (
        "排版钩子要交给脚本做(它才会暂存)"
    )
    # 只看踩到的**那一行命令**: 上面的注释里正解释着"不再用 --check", 拿整段找会误判。
    entry = next(
        line for line in block.splitlines() if line.strip().startswith("entry:")
    )
    assert "--check" not in entry, "只读检查会在改过文件时拦下提交, 那正是要改掉的流程"
    script = _REPO_ROOT / "scripts" / "ruff_format_and_stage.py"
    assert script.is_file(), "钩子指向的脚本要在"
    # 脚本自己会 git add: 这是"提交一次就过"的关键, 少了它 pre-commit 仍会拦下。
    assert '"add"' in script.read_text(encoding="utf-8")


def test_the_format_hook_retries_when_the_index_is_locked(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """回归(2026-10-05 用户实测): 索引被别的进程占住时, 排版钩子不能直接拦下提交.

    现场: 提交时钩子报 ``未能加入暂存区(请手动 git add)`` 拦下整次提交, 连原因都看不到。
    实测同一批文件在 pre-commit 之外跑 ``git add`` **全是 rc=0**, 在 pre-commit 里则**每次失败
    的文件都不一样** —— 那是 VS Code 的 Git 集成周期性刷新索引时短暂拿住了 ``.git/index.lock``。

    判据(全部打桩, 不碰真仓库): ① 整批只 ``git add`` 一次(逐文件会成倍放大撞锁机会); ② 失败会
    重试; ③ 重试期间只要索引已经等于工作区那一份就算成功(没东西可加, 不该拦提交); ④ 一直失败才
    返回失败, 并把 git 的原始报错打出来(不然用户只看到一句"未能加入暂存区")。
    """
    script = _load_script("ruff_format_and_stage")

    class _Completed:
        """只带脚本用到的那几个字段(真的 ``git`` 由打桩替掉)."""

        def __init__(self, returncode: int) -> None:
            self.returncode = returncode
            self.stdout = ""
            self.stderr = "fatal: Unable to create 'index.lock': File exists."

    calls: list[tuple[str, ...]] = []
    state = {"add_failures": 0, "diff_rc": 1}

    def fake_run_git(_git: str, *arguments: str) -> _Completed:
        calls.append(arguments)
        if arguments[0] == "add":
            if state["add_failures"] > 0:
                state["add_failures"] -= 1
                return _Completed(1)
            return _Completed(0)
        return _Completed(state["diff_rc"])

    monkeypatch.setattr(script.shutil, "which", lambda _name: "git")
    monkeypatch.setattr(script, "run_git", fake_run_git)
    monkeypatch.setattr(script.time, "sleep", lambda _seconds: None)
    paths = [Path("a.py"), Path("b.py")]

    state["add_failures"] = 1
    assert script.stage(paths) == [], "失败一次之后要重试成功"
    adds = [call for call in calls if call[0] == "add"]
    assert len(adds) == 2, "恰好重试一次"
    assert adds[0][2:] == ("a.py", "b.py"), "整批一次 add, 不是逐文件"

    # 索引已经等于工作区那一份: 没东西可加, 不该因为 add 失败就拦下提交。
    calls.clear()
    state.update(add_failures=script.STAGE_ATTEMPTS, diff_rc=0)
    assert script.stage(paths) == [], "索引已一致时算成功"
    assert len([call for call in calls if call[0] == "add"]) == 1, (
        "第一条命令就发现没有差异, 不必重试"
    )

    # 一直失败: 返回失败, 并把 git 的原始报错打出来。
    calls.clear()
    state.update(add_failures=script.STAGE_ATTEMPTS, diff_rc=1)
    assert script.stage(paths) == paths, "重试用完仍失败要如实返回"
    assert len([call for call in calls if call[0] == "add"]) == script.STAGE_ATTEMPTS, (
        "要重试满 STAGE_ATTEMPTS 次"
    )
    assert "index.lock" in capsys.readouterr().err, (
        "失败原因要打出来(否则只有一句'未能加入暂存区')"
    )


def test_main_survives_a_cp1252_console(
    layout: _Layout, monkeypatch: pytest.MonkeyPatch
) -> None:
    """英文版 Windows(CI runner) 的控制台是 cp1252: 打印中文不能崩.

    实际踩过: ``UnicodeEncodeError: 'charmap' codec can't encode characters``
    把报告自检直接打断。脚本自己把输出切成 UTF-8, 因此换成 cp1252 流也能跑完。
    """
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="cp1252")
    monkeypatch.setattr(sys, "stdout", stream)
    monkeypatch.setattr(sys, "stderr", stream)

    exit_code = verifier.main([str(layout.report), "--results", str(layout.results)])
    stream.flush()
    printed = buffer.getvalue().decode("utf-8")

    assert exit_code == 0
    assert "报告资源完整" in printed


def test_quality_gate_report_aggregates_only_quality_checks(tmp_path: Path) -> None:
    """总评只算质量检查项: 性能/安全汇总项不能被当成门禁条目."""
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_result(results, "ruff", "ruff check", "passed", "quality")
    _write_result(results, "mypy", "mypy", "failed", "quality")
    _write_result(results, "perf", "Performance baseline", "passed", "performance")

    assert module.quality_checks(results) == [
        ("mypy", "failed"),
        ("ruff check", "passed"),
    ]
    report = module.quality_gate_report(module.quality_checks(results))
    assert "未通过(1/2 项)" in report
    assert "- 未通过: mypy" in report
    assert "Performance" not in report


def test_quality_gate_report_without_checks() -> None:
    """没跑质量作业的运行(例如本地只跑单元测试)不能说成"通过"."""
    module = _load_script("create_allure_summary")
    report = module.quality_gate_report([])
    assert "不适用" in report
    assert "通过" not in report.replace("不适用", "")


def test_global_attachment_matches_what_the_summary_writes() -> None:
    """首页「全局附件」靠配置里的 glob 匹配 —— 文件名与脚本写的必须一致.

    两份都要在: 运行总账(``QUALITY_GATE_REPORT``)与"有意不统计的覆盖"豁免清单
    (``COVERAGE_EXCLUSIONS_REPORT``)。写错一个名字不会报错, 只是报告里少一份附件。
    """
    module = _load_script("create_allure_summary")
    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    matched = re.search(r"globalAttachments:\s*\[([^\]]*)\]", config)
    assert matched is not None, "配置里要有 globalAttachments"
    # 数组写成多行时会有尾随逗号与缩进, 过滤掉空项再看集合。
    names = {
        name
        for item in matched.group(1).split(",")
        if (name := item.strip().strip('"')) and not name.startswith("...")
    }
    assert names == {
        module.QUALITY_GATE_REPORT.name,
        module.COVERAGE_EXCLUSIONS_REPORT.name,
    }
    # 失败现场那条是**条件**收的(没有兜底现场时那份文件根本不存在), 所以不在这里的集合里
    # —— 两处一起由 tests/unit/test_ci_diagnostics.py 守着。
    assert module.FAILURE_DIAGNOSTICS_REPORT.name in config


def test_quality_items_use_the_declared_common_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """质量检查是公共内容: 结论项带 env=common, 且配置里确实声明了匹配它的环境.

    写成 ``env=Linux`` 会让它变成"某个平台的质量检查"(误导); 完全不写则落进隐式的
    ``default``, 与环境选择器里的名字对不上。这里跑真实写入路径, 并把脚本里的标签值
    与 ``allurerc.mjs`` 的 matcher 钉在一起 —— 两边写岔了报告里会静默退回 default。
    """
    module = _load_script("create_allure_quality")
    probe = module.Check(
        "probe", "Probe check", (sys.executable, "-c", "print('ok')"), "core"
    )
    monkeypatch.setattr(module, "CHECKS", (probe,))
    results = tmp_path / "allure-results"

    assert module.main(["--group", "core", "--results-dir", str(results)]) == 0

    payload = json.loads(
        next(results.glob("*-result.json")).read_text(encoding="utf-8")
    )
    labels = {label["name"]: label["value"] for label in payload["labels"]}
    assert labels["testCategory"] == "quality"
    assert labels["env"] == module.QUALITY_ENVIRONMENT, "公共检查要归入显式声明的环境"
    assert "os" not in labels, "os 标签会把公共检查说成某个平台的结果"
    assert payload.get("parameters", []) == [], "不要再带平台参数"
    assert "env=common" in payload["description"], "描述里要说明它归入哪个环境"

    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    assert 'name: "Common"' in config, "配置里要声明这个环境"
    matcher = f'value === "{module.QUALITY_ENVIRONMENT}"'
    assert matcher in config, f"配置里缺少匹配 {matcher} 的 matcher"


def test_the_gate_category_matches_what_the_scripts_write() -> None:
    """ "工程门禁"分类、写入脚本与运行总账必须用同一对类别标签。

    三处分别是: ``allurerc.mjs`` 的 ``categories`` 规则(挑选质量检查结果)、
    ``scripts/create_allure_quality.py`` 写结果时打的标签、``scripts/create_allure_summary.py``
    收集这些结果时的判据。任一处改名而另外两处没跟着改都不会报错 —— 只会静默地"门禁失败
    不再进那个分类"或"运行总账里不再列质量检查", 所以要有守卫把三处钉在一起。
    """
    writer = _load_script("create_allure_quality")
    summary = _load_script("create_allure_summary")
    gate = _ALLURE_CONFIG.read_text(encoding="utf-8").split("gate-quality-check", 1)[1]
    matched = re.search(r'labels:\s*\{\s*([A-Za-z]+):\s*"([^"]+)"\s*\}', gate)
    assert matched is not None, "配置里要有按类别标签挑选的分类规则"

    label, value = matched.group(1), matched.group(2)
    assert label == writer.CATEGORY_LABEL == summary.QUALITY_CATEGORY_LABEL
    assert value == writer.CATEGORY_VALUE == summary.QUALITY_CATEGORY
