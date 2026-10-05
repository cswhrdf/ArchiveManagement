"""守卫: 每个作业失败时都要留下现场, 而且**只有兜底现场**才传得上去.

约定(见 .github/workflows/ci.yml): 每个作业最后都有一对步骤
``Collect failure diagnostics`` + ``Upload failure diagnostics``。收集步挂 ``if: failure()``
(前面的步骤都成功时**根本不跑**), 但它只负责**判定**: 普通失败的证据已经在 Allure 结果与
运行日志里, 这一对步骤只兜底"进程级崩溃 / 证据缺失"。判定写进 ``$GITHUB_OUTPUT``, 上传步
按它跑 —— 所以"某个用例红了"不会再多一份 artifact, 也不会出现在全局附件里
(用户 2026-10-04 的要求)。

为什么要有这些守卫: 这一整套的价值只在**真失败时**才体现出来 —— 而那时才发现"没配上"已经晚
了。所以用测试把它钉住: 少一个作业、条件写成 ``if: always()``、上传不按判定走、汇总作业忘了
下载或忘了过滤, 都要在本地就红。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("失败现场与作业诊断"),
    pytest.mark.layer("unit"),
]

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "ci.yml"
ALLURE_CONFIG = REPO_ROOT / "allurerc.mjs"


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
    """把 ci.yml 按顶层作业切成 ``{作业名: 文本}``(只要带 ``runs-on`` 的才算作业)."""
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


def test_every_job_has_a_failure_diagnostics_pair() -> None:
    """每个作业都要有那一对步骤, 而且收集步要落在作业的**后半段**(约定是"放最后")."""
    jobs = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))

    assert len(jobs) == 5, f"作业数量变了, 这份守卫要跟着改: {sorted(jobs)}"
    for name, job in jobs.items():
        collect = job.index("name: Collect failure diagnostics")
        assert "name: Upload failure diagnostics" in job, name
        assert "scripts/collect_job_diagnostics.py" in job, name
        assert collect > len(job) / 2, f"{name} 的收集步没有放在作业末尾"
        # 条件必须是"失败才跑": 写成 always() 就等于每轮 CI 都多传一份空产物。
        assert "failure()" in _step(job, "Collect failure diagnostics"), name


def test_upload_runs_only_for_a_backstop_scene() -> None:
    """上传步必须按收集步给出的**兜底判定**跑, 而且判定说了有现场就必须真有东西.

    用户 2026-10-04 的要求: 普通失败(断言输出、门禁不通过、步骤报错)的证据已经在 Allure
    结果与运行日志里, 不要再用一份 artifact 与一段附件把它抄一遍 —— 那一段只留给
    **进程级崩溃 / 证据缺失**。判定只在一个地方算(脚本), 工作流只读它。
    """
    jobs = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))

    for name, job in jobs.items():
        collect = _step(job, "Collect failure diagnostics")
        assert "id: diagnostics" in collect, f"{name} 的收集步没有 id, 上传步读不到判定"
        assert "--github-output" in collect, f"{name} 没把判定写进 $GITHUB_OUTPUT"
        assert "--expect" in collect, f"{name} 没声明预期证据, 判定会永远为假"
        upload = _step(job, "Upload failure diagnostics")
        assert "steps.diagnostics.outputs.scene == 'true'" in upload, (
            f"{name} 的上传步没按兜底判定走, 普通失败也会多传一份"
        )
        # 判定说"有现场"就必须真的有: 静默少传一份, 等于把"附件里为什么没有这个作业"变成谜。
        assert "if-no-files-found: error" in upload, f"{name} 允许静默上传空产物"


def test_summary_job_pulls_every_diagnostics_artifact() -> None:
    """汇总作业要按 pattern 收全各作业的诊断, 并且**不合并**到同一目录."""
    summary = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))["allure-summary"]

    assert "pattern: job-diagnostics-*" in summary, "少了 pattern 就只能收到一份"
    # merge-multiple 会把各份同名的 summary.md 压成一份(后到的盖掉先到的)。
    # 只看**这一步**里的情况: 同一个作业里其它下载步用了它是另一回事。
    step = summary.index("pattern: job-diagnostics-*")
    assert "merge-multiple" not in summary[step : step + 400], (
        "合并到同一目录会互相覆盖"
    )


def test_pytest_job_collects_crash_reports_before_uploading_the_artifact() -> None:
    """pytest 作业的收集步必须排在 crash-dumps 上传步**之前**.

    macOS 的 ReportCrash 在进程死后几秒才异步落盘 ``.ips``, 收集步带着
    ``--crash-report-wait`` 把它等来并拷进 ``crash-dumps/``(2026-10-05 之前收集步排在
    上传之后, 于是那一轮的 crash-dumps artifact 里没有 ``.ips`` —— 完整原生栈只存在于
    摘要里截断的 4000 字里, 用户下载 artifact 只能看到空白的现场)。
    """
    job = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))["pytest"]

    collect = job.index("name: Collect failure diagnostics")
    upload = job.index("name: Upload crash dumps")
    assert collect < upload, "收集步要在 crash-dumps 上传之前: .ips 得先拷进目录再上传"


def test_pytest_job_runs_the_ui_suite_in_its_own_process() -> None:
    """UI 用例必须与非 UI 用例分两个 pytest 进程跑(进程隔离, 2026-10-05).

    macOS 上 Tk 的原生崩溃(SIGTRAP)会带走整个 pytest 进程 —— 覆盖率(pytest-cov 要等
    会话**结束**才落盘)与崩溃点之后的所有用例结果全部陪葬。非 UI 段先跑、UI 段单独
    进程加 ``--cov-append`` 之后, 崩掉时丢的只有 UI 自己那段。
    """
    job = _job_blocks(WORKFLOW.read_text(encoding="utf-8"))["pytest"]

    durable = job.index('-m "not ui" --cov')
    ui = job.index('-m "ui" --cov --cov-append')
    assert durable < ui, "非 UI 段先跑: 它的产物落盘之后, UI 段的崩溃才不陪葬"
    assert "--cov-append" in job[ui:], "UI 段要并进同一份覆盖率数据, 而不是另写一份"


def test_diagnostics_report_lands_in_the_allure_config() -> None:
    """报告首页的全局附件里要有那份"失败现场", 而且它是**按文件在不在条件收的**.

    用户 2026-10-04: 没有兜底现场时这份文档不该生成 —— 否则不点开只会以为"是不是崩过"。
    两处一起才行: 脚本不写文件(见 test_no_backstop_scene_means_no_attachment), 配置按
    ``existsSync`` 决定收不收 —— 否则 Allure 会在报告里留一条"附件找不到"的告警。
    """
    module = _load_script("create_allure_summary")
    config = ALLURE_CONFIG.read_text(encoding="utf-8")

    assert "globalAttachments:" in config, "配置里要有 globalAttachments"
    name = module.FAILURE_DIAGNOSTICS_REPORT.name
    assert name == "allure-failure-diagnostics.md"
    assert name in config, "配置里要点名这份附件"
    assert f'existsSync("{name}")' in config, (
        "这份附件要按文件在不在决定收不收(没有现场时不该出现)"
    )
    # 它是 CI 生成物, 也必须被忽略: 否则本地跑一次汇总就会污染工作区(另两份附件同样在列)。
    ignored = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert name in ignored


def test_diagnostics_report_is_a_pointer_not_a_copy(tmp_path: Path) -> None:
    """现场只清点、不搬运大文件: 摘要里给的是清单, 报告才不会被上兆的 dump 拖死.

    路径故意用一个**不存在**的位置: `crash-dumps/` 在仓库里恰好存在, 拿它当输入就测不到
    "路径不在时怎么写"这条分支。
    """
    module = _load_script("collect_job_diagnostics")
    text = module.build_summary(
        "pytest (ubuntu-latest, 分片 0)", [tmp_path / "crash-dumps"]
    )

    assert "crash-dumps" in text
    assert "**不存在**" in text, "路径不在时要写明, 那本身就是证据"
    assert "`crash-dumps` artifact" in text, "要指路到真正的现场, 而不是把本体塞进来"


def test_summary_lists_files_and_sizes(tmp_path: Path) -> None:
    """存在但很大的路径要报出文件数与大小, 并列出最大的几个文件."""
    module = _load_script("collect_job_diagnostics")
    directory = tmp_path / "allure-results"
    directory.mkdir()
    (directory / "small.json").write_text("{}", encoding="utf-8")
    (directory / "big.json").write_text("x" * 4096, encoding="utf-8")

    text = module.build_summary("label", [directory])

    assert "2 个文件" in text
    assert "`big.json`" in text, "要让人一眼看出哪个文件占地方"


def test_main_writes_the_summary_and_echoes_it(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """主入口把摘要写盘并回显 —— 失败时最顺手的地方仍然是运行日志."""
    module = _load_script("collect_job_diagnostics")
    monkeypatch.setenv("GITHUB_JOB", "pytest")
    output = tmp_path / "job-diagnostics"

    code = module.main(["--output", str(output), "crash-dumps"])
    echoed = capsys.readouterr().out

    assert code == 0
    assert (output / "summary.md").is_file()
    assert "# 作业失败现场: pytest" in echoed, "没传 --label 时要退回 GITHUB_JOB"


def test_no_backstop_scene_means_no_attachment(tmp_path: Path) -> None:
    """没有兜底现场时**不写**这份附件 —— 返回 None, 由调用方保证文件不存在."""
    module = _load_script("create_allure_summary")

    assert module.failure_diagnostics_report(tmp_path / "missing") is None


def test_attachment_keeps_only_backstop_scenes(tmp_path: Path) -> None:
    """只有判定为"有兜底现场"的作业进附件 —— 普通失败不进(用户 2026-10-04 的要求).

    样本按 **CI 真实的目录布局**造: artifact 里装的是 `job-diagnostics/` 的**内容**
    (`summary.md` 在根), 下载到 `failure-diagnostics/<artifact 名>/` 后只有一层 —— 原来
    这里多造了一层 `job-diagnostics/`, 与脚本里那条同样多一层的 glob 一起把那次的 bug 盖
    住了(2026-10-05 从报告里发现"崩溃的作业白上传")。
    """
    module = _load_script("create_allure_summary")
    ordinary = tmp_path / "job-diagnostics-linux-0"
    scene = tmp_path / "job-diagnostics-macos-latest-0"
    for owner, scene_flag in ((ordinary, False), (scene, True)):
        owner.mkdir(parents=True)
        (owner / "summary.md").write_text(
            f"# 作业失败现场: {owner.name}\n", encoding="utf-8"
        )
        (owner / module.FAILURE_DIAGNOSTICS_VERDICT).write_text(
            json.dumps({"scene": scene_flag}), encoding="utf-8"
        )

    text = module.failure_diagnostics_report(tmp_path)

    assert "## job-diagnostics-macos-latest-0" in text
    assert "job-diagnostics-linux-0" not in text
    # 没有 verdict.json 的产物按"有现场"处理: 宁可多显示一段, 也不要在最需要证据的时候
    # 因为少了一个字段把现场藏起来。
    (ordinary / module.FAILURE_DIAGNOSTICS_VERDICT).unlink()
    assert "job-diagnostics-linux-0" in module.failure_diagnostics_report(tmp_path)


def test_diagnostics_are_found_with_an_extra_level_too(tmp_path: Path) -> None:
    """上传时若传的是**父目录**(artifact 里多一层 `job-diagnostics/`), 也要认得出来.

    两种布局都收: 不是为了兼容, 而是因为这种"换一层目录"的改动我们真的做过
    (见 test_attachment_keeps_only_backstop_scenes): 只认一种时, 换布局就会静默失效。
    """
    module = _load_script("create_allure_summary")
    owner = tmp_path / "job-diagnostics-quality" / "job-diagnostics"
    owner.mkdir(parents=True)
    (owner / "summary.md").write_text("# 作业失败现场: quality\n", encoding="utf-8")

    text = module.failure_diagnostics_report(tmp_path)

    assert text is not None
    assert "## job-diagnostics-quality" in text, (
        "节标题仍然用 artifact 名, 不带中间那层"
    )


def test_a_crashed_job_becomes_a_countable_conclusion_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """有兜底现场时, 除了首页附件还要写一条 broken 结论项 —— 否则它进不了任何统计.

    用户 2026-10-05: "有崩溃异常的时候要将异常记录到报告里便于统计"。附件是文档, 不进
    计数也不能按环境/等级筛; 结论项才是可统计的那一份 —— 环境用崩溃作业所在的平台(切到
    那个平台就能看到它), 等级 critical, `testCategory=diagnostics`(不是用例, 原生质量门
    那条"每个平台都要有用例"因此不会把它当成"这个平台测过了")。
    """
    module = _load_script("create_allure_summary")
    diagnostics = tmp_path / "failure-diagnostics"
    owner = diagnostics / "job-diagnostics-macos-latest-0"
    owner.mkdir(parents=True)
    (owner / "summary.md").write_text(
        "# 作业失败现场: pytest (macos-latest, 分片 0)\n", encoding="utf-8"
    )
    (owner / module.FAILURE_DIAGNOSTICS_VERDICT).write_text(
        json.dumps(
            {
                "label": "pytest (macos-latest, 分片 0)",
                "scene": True,
                "reasons": ["收到系统级崩溃报告 python3.12-….ips"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    results = tmp_path / "allure-results"
    results.mkdir()
    attachment = tmp_path / "allure-failure-diagnostics.md"
    monkeypatch.setattr(module, "FAILURE_DIAGNOSTICS_DIRECTORY", diagnostics)
    monkeypatch.setattr(module, "FAILURE_DIAGNOSTICS_REPORT", attachment)
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)

    assert module.main(["--write-failure-diagnostics"]) == 0

    assert attachment.is_file(), "有现场时首页附件照旧要写"
    items = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in results.glob("*-result.json")
    ]
    assert len(items) == 1
    item = items[0]
    labels = {entry["name"]: entry["value"] for entry in item["labels"]}
    assert item["status"] == "broken"
    assert item["name"] == "作业崩溃: pytest (macos-latest, 分片 0)"
    assert labels["env"] == "macOS", "结论项要落在崩溃作业那个平台的环境里"
    assert labels["testCategory"] == "diagnostics"
    assert labels["severity"] == "critical"
    assert item["statusDetails"]["message"].startswith("作业崩溃(兜底现场)")
    assert item["attachments"], "现场摘要要挂在结论项上"
    assert "失败现场已写入" in capsys.readouterr().out


def test_no_scene_writes_neither_attachment_nor_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """没有兜底现场时: 附件清掉、结论项一条不写(用户的后半句要求: 避免误解)."""
    module = _load_script("create_allure_summary")
    attachment = tmp_path / "allure-failure-diagnostics.md"
    attachment.write_text("上一次留下的", encoding="utf-8")
    ordinary = tmp_path / "failure-diagnostics" / "job-diagnostics-linux-0"
    ordinary.mkdir(parents=True)
    (ordinary / "summary.md").write_text("# 普通失败\n", encoding="utf-8")
    (ordinary / module.FAILURE_DIAGNOSTICS_VERDICT).write_text(
        json.dumps({"scene": False}), encoding="utf-8"
    )
    results = tmp_path / "allure-results"
    results.mkdir()
    monkeypatch.setattr(
        module, "FAILURE_DIAGNOSTICS_DIRECTORY", tmp_path / "failure-diagnostics"
    )
    monkeypatch.setattr(module, "FAILURE_DIAGNOSTICS_REPORT", attachment)
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)

    assert module.main(["--write-failure-diagnostics"]) == 0

    assert not attachment.exists(), "没有现场时那份附件不该留在报告里"
    assert list(results.glob("*-result.json")) == [], "也不该写任何结论项"
    assert "没有兜底现场" in capsys.readouterr().out


def test_failure_diagnostics_are_concatenated_by_job(tmp_path: Path) -> None:
    """有兜底现场时按作业分节拼起来, 节标题用 artifact 名(能看出是哪台机器)."""
    module = _load_script("create_allure_summary")
    for name in ("job-diagnostics-macos-latest-0", "job-diagnostics-quality"):
        owner = tmp_path / name
        owner.mkdir(parents=True)
        (owner / "summary.md").write_text(f"# 作业失败现场: {name}\n", encoding="utf-8")
        (owner / module.FAILURE_DIAGNOSTICS_VERDICT).write_text(
            json.dumps({"scene": True}), encoding="utf-8"
        )

    text = module.failure_diagnostics_report(tmp_path)

    assert "## job-diagnostics-macos-latest-0" in text
    assert "## job-diagnostics-quality" in text
    assert text.count("# 作业失败现场: ") == 2, "两份摘要都要在, 不能互相覆盖"


def test_a_single_artifact_laid_out_flat_is_still_recognized(tmp_path: Path) -> None:
    """pattern 只匹配到一个诊断 artifact 时, download-artifact 会把内容**平铺**到下载目录.

    2026-10-05 实测: 那一轮只有 ``job-diagnostics-macos-latest-0`` 一份, 平铺之后
    ``crash_scenes`` 按子目录找一条都匹配不到 —— 汇总日志写着"没有兜底现场", 报告首页的
    "失败现场"附件与 broken 结论项**静默**消失(这正是用户报的"入口一直不生效")。
    这个样本按平铺布局造, 认不出就该红。
    """
    module = _load_script("create_allure_summary")
    diagnostics = tmp_path / "failure-diagnostics"
    diagnostics.mkdir()
    (diagnostics / "summary.md").write_text(
        "# 作业失败现场: pytest (macos-latest, 分片 0)\n", encoding="utf-8"
    )
    (diagnostics / module.FAILURE_DIAGNOSTICS_VERDICT).write_text(
        json.dumps(
            {
                "label": "pytest (macos-latest, 分片 0)",
                "scene": True,
                "reasons": ["faulthandler.log 有栈"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    scenes = module.crash_scenes(diagnostics)

    assert len(scenes) == 1, "平铺布局下也要认出这份现场, 而不是静默跳过"
    assert scenes[0].owner == "pytest (macos-latest, 分片 0)", (
        "丢了 artifact 名, 节标题要退回 verdict.json 里的作业标签"
    )
    assert scenes[0].platform == "macOS", "平台归属不能跟着丢(报告里按环境筛)"
    assert "## pytest (macos-latest, 分片 0)" in module.failure_diagnostics_report(
        diagnostics
    )


def test_diagnostic_label_defaults_to_local_outside_ci(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没有 CI 变量时标成 local: 本地跑同一脚本不该崩, 也不该假装自己是某个作业."""
    module = _load_script("collect_job_diagnostics")
    monkeypatch.delenv("GITHUB_JOB", raising=False)

    assert module.build_summary("local", []).startswith("# 作业失败现场: local")


def test_utf8_helper_does_not_need_the_project_package() -> None:
    """诊断脚本要在 `--no-install-project` 的作业里跑, 所以不能 import 项目包."""
    source = (REPO_ROOT / "scripts" / "collect_job_diagnostics.py").read_text(
        encoding="utf-8"
    )

    assert "from archive_management" not in source
    assert "def ensure_utf8_output()" in source, "要自带一份, 否则中文摘要在本地会乱码"


def test_diagnostics_are_downloaded_before_the_attachment_is_written() -> None:
    """附件必须在"下载各作业诊断"**之后**写 —— 写早了它永远是"本轮没有作业失败".

    现场(用户 2026-10-04): 报告的全局附件里那句话一直不变, 而作业明明是红的。原因是汇总
    脚本的主流程(写性能/安全结论那一步)跑在下载之前, 顺手把附件也写了。
    """
    workflow = WORKFLOW.read_text(encoding="utf-8")

    download = workflow.index("Download failure diagnostics from every job")
    writer = workflow.index("--write-failure-diagnostics")
    assert download < writer, "附件写在了下载之前, 内容必然是空的"
    # 主流程仍要在下载**之前**跑(报告生成需要它写的那些结果), 但它不能再写这份附件。
    assert workflow.index("create_allure_summary.py --expect-platforms") < download


def test_system_crash_reports_are_collected(tmp_path: Path) -> None:
    """系统级崩溃报告(macOS 的 `.ips`)要收进 crash-dumps/ —— 原生栈才是定位 C 崩溃的依据."""
    module = _load_script("collect_job_diagnostics")
    source = tmp_path / "DiagnosticReports"
    source.mkdir()
    fresh = source / "python-2026-10-04.ips"
    fresh.write_text('{"exception": {"signal": "SIGSEGV"}}', encoding="utf-8")
    old = source / "python-2020-01-01.ips"
    old.write_text("上一次运行的现场", encoding="utf-8")
    os.utime(old, (1, 1))
    (source / "not-a-report.txt").write_text("无关文件", encoding="utf-8")
    target = tmp_path / "crash-dumps"

    copied = module.collect_crash_reports(source, target, wait_seconds=0)

    assert [path.name for path in copied] == [fresh.name]
    assert (target / fresh.name).is_file()
    assert not (target / old.name).exists(), "上一次运行的现场不该混进来"


def test_missing_crash_report_directory_costs_nothing(tmp_path: Path) -> None:
    """来源目录不存在(Linux/Windows)时一秒都不等 —— 不能为了 macOS 把别的平台拖慢."""
    module = _load_script("collect_job_diagnostics")
    started = time.monotonic()

    copied = module.collect_crash_reports(
        tmp_path / "没有这个目录", tmp_path / "out", wait_seconds=30
    )

    assert copied == []
    assert time.monotonic() - started < module.POLL_SECONDS


def test_summary_inlines_the_crash_log(tmp_path: Path) -> None:
    """faulthandler 的线程栈要**内联**进摘要: 报告首页就看得见, 不必再去下 artifact."""
    module = _load_script("collect_job_diagnostics")
    crashes = tmp_path / "crash-dumps"
    crashes.mkdir()
    (crashes / "faulthandler.log").write_text(
        "Fatal Python error: Segmentation fault\n"
        '  File "tkinter/__init__.py", line 1379 in update_idletasks\n',
        encoding="utf-8",
    )

    text = module.build_summary("label", [], crashes)

    assert "## 崩溃现场摘录" in text
    assert "### `faulthandler.log`" in text
    assert "in update_idletasks" in text, "栈本身要看得见"


def test_huge_crash_reports_are_truncated_in_the_summary(tmp_path: Path) -> None:
    """上兆的现场只摘开头: 这份附件要在报告里渲染, 整份塞进去会把页面卡死."""
    module = _load_script("collect_job_diagnostics")
    crashes = tmp_path / "crash-dumps"
    crashes.mkdir()
    (crashes / "python-2026-10-04.ips").write_text(
        "x" * (module.INLINE_LIMIT * 3), encoding="utf-8"
    )

    text = module.build_summary("label", [], crashes)

    assert "已截断" in text
    assert len(text) < module.INLINE_LIMIT * 2


def test_ordinary_failure_is_not_a_backstop_scene(tmp_path: Path) -> None:
    """普通失败(断言红了)不算兜底现场: 结果目录在、没有栈 —— 证据在结果里, 不该再抄一遍."""
    module = _load_script("collect_job_diagnostics")
    results = tmp_path / "allure-results"
    results.mkdir()
    (results / "result.json").write_text("{}", encoding="utf-8")

    verdict = module.backstop_verdict([results], tmp_path / "crash-dumps")

    assert verdict.scene is False
    assert verdict.reasons == []


def test_missing_coverage_is_a_backstop_scene(tmp_path: Path) -> None:
    """覆盖率文件是**会话结束**才落盘的: 它不在 = 进程没走到最后(被信号杀了 / 超时中断).

    这正是兜底要抓的那一类: 结果里只会看到"某一片没有产物", 看不到为什么。
    """
    module = _load_script("collect_job_diagnostics")

    verdict = module.backstop_verdict(
        [tmp_path / ".coverage.shard-0"], tmp_path / "crash-dumps"
    )

    assert verdict.scene is True
    assert ".coverage.shard-0" in verdict.reasons[0]


def test_empty_paths_count_as_missing(tmp_path: Path) -> None:
    """空目录与 0 字节文件都算不在: 与"根本没生成"是同一件事."""
    module = _load_script("collect_job_diagnostics")
    empty_directory = tmp_path / "allure-results"
    empty_directory.mkdir()
    empty_file = tmp_path / ".coverage.shard-0"
    empty_file.write_text("", encoding="utf-8")

    verdict = module.backstop_verdict(
        [empty_directory, empty_file], tmp_path / "crash-dumps"
    )

    assert verdict.scene is True
    assert len(module.missing_evidence([empty_directory, empty_file])) == 2


def test_only_a_crash_log_with_a_stack_is_a_backstop_scene(tmp_path: Path) -> None:
    """空的 faulthandler 日志不算现场 —— 那是每轮都有的文件, 有栈才是真崩了."""
    module = _load_script("collect_job_diagnostics")
    crashes = tmp_path / "crash-dumps"
    crashes.mkdir()
    log = crashes / "faulthandler.log"

    log.write_text("", encoding="utf-8")
    assert module.backstop_verdict([], crashes).scene is False

    log.write_text("Fatal Python error: Segmentation fault\n", encoding="utf-8")
    verdict = module.backstop_verdict([], crashes)
    assert verdict.scene is True
    assert "faulthandler.log" in verdict.reasons[0]


def test_system_crash_reports_skip_the_wait_without_a_signal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """没有崩溃迹象就**不等**系统级报告: 普通失败不该为 macOS 的 ReportCrash 多花 20 秒."""
    module = _load_script("collect_job_diagnostics")
    source = tmp_path / "DiagnosticReports"
    source.mkdir()
    (source / "python-2026-10-04.ips").write_text("{}", encoding="utf-8")
    proof = tmp_path / "allure-results"
    proof.mkdir()
    (proof / "result.json").write_text("{}", encoding="utf-8")
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(module, "CRASH_DUMP_DIRECTORY", scratch)
    arguments = module.parse_args(
        [
            "--output",
            str(tmp_path / "job-diagnostics"),
            "--crash-reports-from",
            str(source),
            "--crash-report-wait",
            "30",
        ]
    )

    started = time.monotonic()
    verdict = module.collect_and_judge(arguments, [proof])
    elapsed = time.monotonic() - started

    assert verdict.scene is False
    assert elapsed < module.POLL_SECONDS, "没有迹象时不该等系统级报告"
    assert not scratch.exists(), "没有迹象时不该去碰 crash-dumps"


def test_github_output_carries_the_verdict_on_one_line(tmp_path: Path) -> None:
    """``$GITHUB_OUTPUT`` 里一个键只能占一行: 多行的说明要先压成一行."""
    module = _load_script("collect_job_diagnostics")
    output = tmp_path / "github-output"

    module.write_github_output(
        output, {"scene": "true", "reason": "预期缺失\n第二行\n\n"}
    )

    assert output.read_text(encoding="utf-8").splitlines() == [
        "scene=true",
        "reason=预期缺失 第二行",
    ]


def test_main_writes_the_verdict_and_the_github_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """主入口把判定写进 ``verdict.json`` 与 ``$GITHUB_OUTPUT`` —— 工作流的条件读的就是它."""
    module = _load_script("collect_job_diagnostics")
    monkeypatch.setattr(module, "CRASH_DUMP_DIRECTORY", tmp_path / "crash-dumps")
    output = tmp_path / "job-diagnostics"
    github_output = tmp_path / "github-output"
    missing = tmp_path / ".coverage.shard-0"

    code = module.main(
        [
            "--output",
            str(output),
            "--github-output",
            str(github_output),
            "--expect",
            str(missing),
        ]
    )
    capsys.readouterr()

    assert code == 0
    payload = json.loads((output / module.VERDICT_NAME).read_text(encoding="utf-8"))
    assert payload["scene"] is True
    assert "scene=true" in github_output.read_text(encoding="utf-8")
    # `--expect` 的路径也进清单: 清点与判据不必让人写两遍。
    assert str(missing) in (output / "summary.md").read_text(encoding="utf-8")


def test_workflow_runs_the_script_it_guards() -> None:
    """守卫的作业名与脚本名要对得上 —— 改名了这里就该红, 而不是等 CI 静默少一层诊断."""
    workflow = WORKFLOW.read_text(encoding="utf-8")

    assert (REPO_ROOT / "scripts" / "collect_job_diagnostics.py").is_file()
    assert workflow.count("scripts/collect_job_diagnostics.py") == 5, "五个作业各一次"
