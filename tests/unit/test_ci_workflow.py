"""
CI workflow 结构断言: 作业拓扑/平台矩阵/工件去重/发布只发校验过的报告/macOS Tk9 规避/硬崩溃留证。拆自 test_report_verification.py(见 docs/test-refactor-plan.md S8)。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

import ci_workflow
from report_support import (
    _ALLURE_CONFIG,
    _REPO_ROOT,
    _WORKFLOW,
    _load_script,
    _write_result,
    declared_environment_names,
    required_environment_ids,
    required_platforms,
    verifier,
)

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


def test_ci_publishes_only_verified_report() -> None:
    """CI 必须先自检报告再发布, 并且发布单个 zip(整目录被静默丢掉的情况不会再出现)."""
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    # 只看命令行: 注释里也会指路到这个脚本(例如说明 zip 里的目录结构), 那是文档而不是门禁。
    code = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )
    verifications = code.count("scripts/verify_allure_report.py")

    assert verifications >= 3, "两个作业的结构自检 + 汇总作业的平台用例检查"
    # 发布 zip 的前提是**出 zip 的那次自检**通过: 两个作业各一份。
    commands = [
        line.strip()
        for line in code.splitlines()
        if line.strip().startswith("run:") and "verify_allure_report.py" in line
    ]
    assert len(commands) == verifications, "自检点数量与命令数量对不上"
    archive_checks = [command for command in commands if "--archive" in command]
    assert len(archive_checks) == 2, "pytest 与汇总作业各出一份归档报告"
    # 挂在"出 zip 的那次自检通过"上的地方就是两份 `Upload ... Allure report`
    # (2026-10-06 移除 Pages 发布后少了"解开发布"那一步)。
    # 只数命令行(注释里也会引这句话当说明), 所以先把注释剥掉再数。
    gated = code.count("steps.verify-report.outcome == 'success'")
    assert gated == len(archive_checks), f"自检与归档挂钩的地方对不上: {gated}"
    assert workflow.count("path: allure-report.tar.gz") == len(archive_checks)
    assert "path: allure-report/" not in workflow, "报告目录不再直接发布"
    # 平台用例检查: pytest 作业的报告传矩阵里的平台(报告作业固定跑在 Ubuntu 上,
    # runner.os 会说 Linux), 汇总作业传汇总要求的两个平台 —— 两处都要有, 少一处就等于
    # 少一道检查(只数 `run:` 行, 注释不算)。
    platform_checks = [
        command for command in commands if "--expect-platforms" in command
    ]
    assert len(platform_checks) == 2, "pytest 作业与汇总作业各要检查一次平台用例"
    per_platform_flag = '--expect-platforms "${{ matrix.platform }}"'
    assert sum(per_platform_flag in c for c in platform_checks) == 1
    # 汇总作业要在这份合并报告里要求三个平台都有**用例**结果(不是只要"环境存在")。
    summary_flag = "--expect-platforms Windows,macOS,Linux"
    assert sum(summary_flag in c for c in platform_checks) == 1
    # 汇总作业里那条不拦发布(报告正是用来看"哪个平台没数据"的地方), 所以它的结论必须有
    # 地方接手: 末尾的门禁结论步骤要带上它, 否则失败了也没人管。
    step = workflow.split("name: Check every platform contributed tests", 1)[1]
    assert "continue-on-error: true" in step.split("\n      - name:", 1)[0]
    verdict = workflow.split("name: Report quality gate verdict", 1)[-1]
    assert "steps.verify-platforms.outcome == 'failure'" in verdict, (
        "平台用例检查的结论必须由末尾的门禁结论步骤接手"
    )


def test_ci_judges_the_coverage_only_on_complete_shard_data() -> None:
    """覆盖率链条: 缺片要点名、不合并、不判门槛、不出残缺报告, 并且作业照样红.

    2026-09-30 的 CI 实测: 少一个分片的覆盖率产物时, ``cp coverage-data-*/.coverage.shard-*``
    只报一句 ``cannot stat``, 后面 ``coverage xml`` 会自己合并剩下那份并照 ``fail_under``
    报"覆盖率不达标" —— 报告里写着"Windows 89.99% 未达标", 而真相是数据不全。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "pytest-report")
    code = "\n".join(
        line for line in job.splitlines() if not line.strip().startswith("#")
    )

    assert "cp coverage-data-*" not in code, "通配符抄数据看不出缺的是哪一片"
    collect = code.split("name: Collect coverage data", 1)[1].split("- name:", 1)[0]
    assert "scripts/collect_coverage_data.py" in collect, "要按片号收集"
    assert '--expect-shards "${{ matrix.expect_shards }}"' in collect, (
        "收集要按矩阵声明核对片号(声明了却没到的那些才是缺片)"
    )
    assert "continue-on-error: true" in collect, (
        "缺片时要让后面的步骤按 outcome 各自决定"
    )
    assert re.search(r"^\s+id: collect-coverage$", collect, re.M), (
        "后面的步骤要靠这个 id 判断数据全不全"
    )

    # 合并 / 判门槛 / 出报告 / 挂结论四件都只在"片到齐"时做。
    for step in (
        "Combine coverage data",
        "Enforce the coverage threshold",
        "Write the coverage report",
        "Attach coverage report to Allure",
    ):
        body = code.split(f"name: {step}", 1)[1].split("- name:", 1)[0]
        assert "steps.collect-coverage.outcome == 'success'" in body, (
            f"{step} 没有按'片到齐'放行: 残缺数据会变成一个看起来'覆盖率掉了'的结论"
        )
    # 门槛只在一处判: `coverage xml` 自己也会执行 fail_under, 那会让同一个失败被报两次,
    # 而且文案把"缺片"说成"覆盖率不够"。
    assert "coverage xml -o coverage.xml --fail-under=0" in code, (
        "写报告的那一步不该再判一次门槛"
    )
    # 缺片的结论必须落在作业状态上: 收集那步是 continue-on-error, 所以要有接手的一步。
    gap = code.split("name: Report the shard gap", 1)
    assert len(gap) == 2, "缺片要有一道专门让作业变红的门禁"
    assert "steps.collect-coverage.outcome != 'success'" in gap[1]
    assert "exit 1" in gap[1]


def test_the_report_job_only_judges_missing_pieces() -> None:
    """合并作业的红只来自"用例缺了 / 报告没生成出来", 覆盖率判定交给报告与原生质量门.

    出处(用户 2026-10-05): "这一步里用例的业务错误不应该在这步报出失败, 这一步应该只关心用例
    是否缺失, 合并报告是否失败"。以前 `coverage report` 低于门槛就以非 0 退出 ⇒ 合并作业变红,
    而那个红与"某一片没上传产物"长得**一模一样**: 看到红的人分不清是漏收了还是结果不达标。

    判定并没有丢, 两道都在(少一道才叫把门禁变成警告): 低于门槛时报告里那条 `Coverage report`
    结果本身是 `failed`, 汇总作业的原生质量门(规则集一: 不过滤 + `maxFailures: 0`)把它算进去。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "pytest-report")
    code = "\n".join(
        line for line in job.splitlines() if not line.strip().startswith("#")
    )

    threshold = code.split("name: Enforce the coverage threshold", 1)[1].split(
        "- name:", 1
    )[0]
    assert "continue-on-error: true" in threshold, (
        "覆盖率判定不该决定合并作业的成败(它与'缺片'的红分不开)"
    )
    # "缺了 / 没生成出来"仍然要红: 这几步不许被放过。
    for step in (
        "Merge shard Allure results",
        "Generate Allure report",
        "Verify Allure report",
    ):
        body = code.split(f"name: {step}", 1)[1].split("- name:", 1)[0]
        assert "continue-on-error: true" not in body, (
            f"{step} 必须能把这个作业弄红(它管的是'缺了 / 没生成出来')"
        )
    # 判定接手的第一道: 低于门槛时那条结论项的状态就是 failed(`verdict_section` 的
    # 第一个参数是 0~1 的比例, 第二个是百分数门槛 —— 与 pyproject 的 fail_under 同单位)。
    coverage = _load_script("create_allure_coverage")
    _section, status, _message = coverage.verdict_section(0.5, 95)
    assert status == "failed", "门槛判定要落成报告里那条结论项的 failed 状态"
    _section, status, _message = coverage.verdict_section(0.99, 95)
    assert status == "passed", "达标的那一侧仍要是 passed(别把结论写成恒红)"


def test_collect_coverage_data_names_the_missing_shard(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """缺片要点名(哪一片)并以非 0 退出, 而且**不摊平**(下游据此跳过硬门槛)."""
    module = _load_script("collect_coverage_data")
    monkeypatch.chdir(tmp_path)
    shard0 = tmp_path / "coverage-data-windows-latest-0" / ".coverage.shard-0"
    shard0.parent.mkdir()
    shard0.write_bytes(b"a")

    exit_code = module.main(["--expect-shards", "0,1"])
    captured = capsys.readouterr()

    assert exit_code == 1
    assert "缺 1" in captured.err, "失败信息要点名缺的是哪一片"
    assert "0,1" in captured.err, "也要说明声明了哪几片"
    assert not (tmp_path / ".coverage.shard-0").exists(), "缺片时不该摊平"
    assert "片 0" in captured.out, "找到了哪几片也要打出来(便于与日志里的产物清单对数)"


def test_collect_coverage_data_accepts_the_flat_download_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """只匹配到一个产物时下载动作会把它直接解到当前目录(没有 coverage-data-* 这层).

    这正是那次 CI 失败的另一半: 通配符 `coverage-data-*/...` 一个都匹配不到, 于是那一片
    静默地没进合并。
    """
    module = _load_script("collect_coverage_data")
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".coverage.shard-0").write_bytes(b"a")
    nested = tmp_path / "coverage-data-windows-latest-1" / ".coverage.shard-1"
    nested.parent.mkdir()
    nested.write_bytes(b"b")

    assert module.main(["--expect-shards", "0,1"]) == 0
    assert (tmp_path / ".coverage.shard-1").exists(), "子目录里的那片要摊平到工作目录"
    assert (tmp_path / ".coverage.shard-0").read_bytes() == b"a"


def _version_tuple(text: str) -> tuple[int, ...]:
    """把 ``"3.19.1"`` 这样的版本号拆成可比大小的元组(段数不同的也能比)."""
    return tuple(int(part) for part in text.split("."))


def test_per_platform_reports_skip_the_native_gate() -> None:
    """逐平台的报告生成要用**不带质量门**的配置 —— 3.20.0 起 generate 阶段也会跑门.

    门里的 ``environmentsTested`` 只在**汇总**那份报告里成立; ``pytest-report`` 每次都只合并
    一个平台, 用主配置生成会退 1(实测 3.20.0: 同样输入在 3.19.1 下能生成成功)。汇总作业
    继续用主配置 —— 它本来就是跑门的地方。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    config = (_REPO_ROOT / "allurerc.per-platform.mjs").read_text(encoding="utf-8")

    assert 'from "./allurerc.mjs"' in config, "设置只有一处真相: 从主配置继承"
    assert "qualityGate" in config, "它要说明自己摘掉的是什么"
    assert (
        "allure generate allure-results --output allure-report "
        "--config allurerc.per-platform.mjs" in workflow
    )
    # 汇总作业那句仍不带 --config: 它要用主配置里的门。
    assert "allure generate allure-results --output allure-report\n" in workflow


def test_native_quality_gate_is_configured_and_pinned() -> None:
    """Allure 原生质量门: 规则写在配置里, CI 跑它并用退出码定成败, CLI 钉在 3.18.0.

    曾经的结论是"不用原生门禁": 3.13~3.17 一旦配了 ``historyPath`` 就静默放行(退 0
    且不输出任何内容 —— 本地历史流句柄悬空, ``AllureReport.done()`` 永不返回, issue
    #895), 而本仓库必须有 ``historyPath``。3.18.0 的 PR #962 修好后它才可用, 所以这里
    同时锁住"配了规则"与"CLI 钉在修好的版本上" —— 版本一旦被改回浮动标签或降到 3.18
    以下, 门禁会静默失效而没人发现。

    另外: ``allure generate`` 不执行校验, 所以首页「质量门」页签仍靠 ``allure run`` 填;
    CI 把这里门的输出写进日志, 由运行总账收进报告首页「全局附件」。
    """
    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    assert re.search(r"^\s*qualityGate\s*:", config, re.MULTILINE) is not None
    for rule in ("maxFailures", "successRate", "environmentsTested"):
        assert rule in config, f"配置里缺少规则 {rule}"
    minimum = re.search(r"版本要求 ≥ ([0-9.]+)", config)
    assert minimum is not None, "注释里要写明版本要求与原因"
    assert "#895" in config, "注释里要留 issue 号, 便于日后重测"

    workflow = _WORKFLOW.read_text(encoding="utf-8")
    gate = "allure quality-gate --config allurerc.mjs allure-results"
    assert gate in workflow, "CI 要真的跑原生质量门"
    # 版本判据是**两处的关系**, 不是写死一个号: 工作流里钉的版本必须 ≥ 配置里写的最低要求,
    # 而且工作流里只能有一处出处。2026-10-03 的 CI 就红在这条上 —— 工作流已经升到 3.19.1,
    # 而断言还在找 3.18.0(两处都是人肉同步的注释, 不同步时没有东西会提醒)。
    pinned = set(re.findall(r"npm install --global allure@([0-9.]+)", workflow))
    assert len(pinned) == 1, f"CLI 版本必须只有一处出处: {sorted(pinned)}"
    version = pinned.pop()
    # 两种写法都允许, 但都必须真的满足下限:
    #   * 钉住的完整版本(``3.19.1``): 静态就能判;
    #   * 浮动标签(``3``): 必须在 CI 里**运行期**判定, 否则 3.13~3.17 那种"静默放行门槛"
    #     会静默回来(2026-10-04: 用户有意把版本改成动态取最新, 下限因此从静态改成运行期)。
    parts = _version_tuple(version)
    if len(parts) < 2:
        # 浮动标签: 下限静态判不了, 必须在 CI 里**运行期**比一次, 而且过期时要响亮地红
        # (3.13~3.17 配 historyPath 会静默放行质量门, 那种失效在报告里看不出来)。
        assert "allure --version" in workflow, "浮动标签必须当场打印实际版本"
        assert "::error::Allure CLI" in workflow, "浮动标签要在 CI 里真的比一次下限"
        assert parts[0] >= _version_tuple(minimum.group(1))[0], (
            f"CI 用的浮动标签 {version} 连最低要求的主版本都不到"
        )
    else:
        assert parts >= _version_tuple(minimum.group(1)), (
            f"CI 钉的 {version} 低于配置里要求的最低版本 {minimum.group(1)}"
        )
    assert "Report quality gate verdict" in workflow, "门禁结论要能决定作业成败"
    # 先跑门禁(留日志给总账), 再生成报告: 顺序反了日志就进不了报告。
    assert workflow.index(gate) < workflow.index("Generate final Allure report")
    # 还得**先于写运行总账那一步**: 总账在 "Summarize performance and security into Allure"
    # 里拼, 它把门禁日志收进首页「全局附件」的「原生质量门」一节 —— 2026-10-04 之前
    # 顺序是反的, 那一节永远写着"本次没有质量门输出"。
    assert workflow.index(gate) < workflow.index(
        "Summarize performance and security into Allure"
    ), "门禁日志先落地, 运行总账才收得到它"


def test_every_job_that_drives_the_allure_cli_installs_it() -> None:
    """跑 `allure` 命令的作业必须有装它的步骤(Node + 全局 CLI): 少了全是 command not found.

    2026-10-10 实测(run 37958738335 的汇总作业): 那个作业里 `Install uv` 与
    `Set up Node.js` / `Install Allure 3 CLI` 一起不见了, 于是 `uv ...` 与 `allure ...`
    两类命令**每一步**都以 exit 127 结束 —— 质量门没跑、报告没生成, 而"上传报告"那一步挂
    在 `steps.verify-report.outcome == 'success'` 上, 于是 `allure-report-final` 这份产物
    根本不存在, 作业状态却只说"质量门未通过"(0 个产物这件事看不出来)。
    所以把"用了什么工具就得先装它"钉成不变式: 与 setup-uv 那条守卫同一个道理。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    for name, body in ci_workflow.jobs(workflow).items():
        commands = "\n".join(
            line for line in body.splitlines() if not line.strip().startswith("#")
        )
        if not re.search(
            r"\ballure (?:generate|quality-gate|open|--version)\b", commands
        ):
            continue
        hint = (
            f"{name} 里跑了 allure 命令, 却没有装它的步骤"
            "(要 `actions/setup-node@` + `npm install --global allure@3`)"
        )
        assert "actions/setup-node@" in commands, hint
        assert "npm install --global allure@" in commands, hint


def test_quality_gate_asks_every_platform_for_real_tests() -> None:
    """要求三个平台各自都有真实用例, 而不是靠会漂的绝对计数.

    ``minTestsCount: 3000`` 这样的常量会随用例规模往**更松**的方向漂: 实测签名是
    3P+154(每平台 P 条用例), 每平台涨到 1400 上下之后, 缺一整个平台的运行也仍然高于
    3000 —— 规则静默失效, 而且失效时没有任何信号。所以改成滤掉脚本生成的结论项后要求
    三个环境里都还有用例(环境维度上一个平台都没有时会直接报"未被测试")。

    判据必须是**正向**的(必须有 ``framework=pytest``), 否则会悄悄变绿: 反向判据
    ("不能带 testCategory")遇到将来某个脚本忘了打标签, 那条汇总项就会被当成真实用例,
    规则从此失去意义 —— 而正向判据漏判时会直接变红。因此这里同时锁住两件事: 配置里
    按 ``framework`` 选, 而 ``scripts/`` 下的产物一律不写这个标签。
    """
    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    # 注释里可以拿它当反例说明(所以只禁止它作为**规则**出现)。
    assert re.search(r"^\s*minTestsCount\s*:", config, re.MULTILINE) is None, (
        "绝对计数会随规模变松, 已换成环境维度"
    )
    gate = config.split("qualityGate:", 1)[1]
    assert "filter:" in gate, "只看真实用例的规则集要带 filter"
    # 判据必须与校验脚本一致: 两处都在回答"这条结果是用例还是脚本产物"。
    assert verifier.REAL_TEST_LABEL == "framework"
    assert verifier.REAL_TEST_VALUE == "pytest"
    assert f'name === "{verifier.REAL_TEST_LABEL}"' in gate
    assert f'value === "{verifier.REAL_TEST_VALUE}"' in gate
    # 期望值必须是**环境 id**(小写): 门禁比的是结果的 `environment`(环境身份里的 id),
    # 写平台显示名一个都比不上(2026-10-04: 那样会一次报全三个"未被测试", 而三条平台的
    # 用例其实都在; 本地用三平台最小结果复现过)。
    ids = required_environment_ids()
    assert ids == ("windows", "macos", "linux"), (
        "要求哪几个平台要有用例: 改这里要同步 CI 矩阵与 --expect-platforms(见同文件那条守卫)"
    )
    assert all(env_id == env_id.lower() for env_id in ids), (
        "环境 id 是小写的; 写成显示名('Windows')会导致规则一个都比不上"
    )
    assert declared_environment_names()["windows"] == "Windows"
    # 规则集要有 id: 门禁失败时输出的是 `<规则集 id>/<规则名>`, 一眼看出是哪条不过。
    assert re.search(r'id:\s*"[\w-]+"', gate) is not None

    # 只有"写结果"的脚本受这条约束(校验脚本要**读**这个标签, 不在其列: 它自己就是
    # 用 framework=pytest 判断"这条结果是用例还是脚本产物"的那个实现)。
    writers = (
        "create_allure_quality",
        "create_allure_coverage",
        "create_allure_summary",
        "create_allure_visual",
    )
    for script in writers:
        text = (_REPO_ROOT / "scripts" / f"{script}.py").read_text(encoding="utf-8")
        assert '"framework"' not in text, (
            f"{script}.py 写了 framework 标签: 脚本生成的结论项会被当成真实用例"
        )


def test_run_ledger_has_every_section_and_flags_missing_artifacts(
    tmp_path: Path,
) -> None:
    """运行总账要覆盖四类结论与产物清单, 而且看得出原始文件缺失.

    总账是报告首页唯一能回答"这次运行产出了什么、原始数据在哪"的地方, 也是全局附件
    不在 ``verify_allure_report.py`` 校验范围内时补上的那道核对。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    _write_result(results, "ruff", "Ruff check", "passed", "quality")
    (results / "cov-attachment.xml").write_text(
        '<?xml version="1.0" ?>\n'
        '<coverage line-rate="0.9182" branch-rate="0.75" version="7.5.0">\n</coverage>\n',
        encoding="utf-8",
    )
    (results / "cov-result.json").write_text(
        json.dumps(
            {
                "uuid": "cov",
                "name": "Coverage report",
                "fullName": "archive-management.coverage",
                "status": "passed",
                "labels": [{"name": "env", "value": "Windows"}],
                "attachments": [
                    {"name": "coverage.xml", "source": "cov-attachment.xml"},
                    {"name": "coverage.json", "source": "missing.json"},
                ],
            }
        ),
        encoding="utf-8",
    )
    performance = {
        "environment": {"os_family": "Linux"},
        "measurements": [{"name": "backup", "passed": True}],
    }
    security = [
        {"environment": {"os_family": "Linux"}, "findings": [{"blocked": True}]}
    ]

    payloads = module.result_payloads(results)
    ledger = module.run_ledger(results, payloads, performance, security)

    assert ledger.startswith("# 运行总账")
    for section in (
        "## 质量门",
        "## 覆盖率",
        "## 性能基准",
        "## 安全测试",
        "## 证据核对(应有 vs 实有)",
        "### ① 结论清单",
        "### ② 结论项声明的附件文件",
    ):
        assert section in ledger
    # 读序: 结论在前, 审计性的核对在末尾 —— 但"缺不漏"的那一行数留在开头。
    assert ledger.index("## 安全测试") < ledger.index("## 证据核对(应有 vs 实有)")
    assert ledger.rstrip().endswith("说明这段证据在打包/下载环节丢了。")
    assert "- 证据核对: 应有 " in ledger, "开头要留一行数, 不然移到末尾就看不出来了"
    assert "91.82%" in ledger, "覆盖率数字要取自原始 XML"
    assert "75.00%" in ledger, "分支覆盖率也要写出来"
    assert "?" not in ledger.split("## 覆盖率")[1].split("##")[0], "覆盖率不该是问号"
    assert "| 平台 | 结论条数 | 未拦截条数 | 失败用例 | 结论 | 原始结论 |" in ledger, (
        "表头要说清每一列的含义"
    )
    assert "已收录" in ledger, "产物清单的状态列要用能看懂的词"
    assert "| Windows |" in ledger, "覆盖率按平台各一行"
    assert "全部达标(1 项)" in ledger, "性能基准给一行结论"
    assert "| Linux | 1 | 0 | 0 | 通过 |" in ledger, (
        "安全那一行要给出结论数、未拦截数、失败用例与结论"
    )
    assert "**缺失**" in ledger, "声明了却找不到的原始文件必须被标出来"


def test_platform_specific_checks_use_the_platform_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """平台专属检查要落进**那个平台**的环境里, 而不是 Common.

    ``mypy --platform win32`` 验的是 Windows 专属代码路径, 结论放在 ``Common`` 里会被混进
    "与平台无关"的一堆结论中; 放进 Windows 环境才能和那个平台的测试结果一起看(环境选择器
    一筛就只剩这个平台的东西)。环境标签由 ``Check.host_platform`` 推出, 所以"标签"与
    "该在哪台机器上跑"是同一个事实, 不会各自漂移; 本用例把派生结果与 ``allurerc.mjs`` 里
    的环境名、matcher 钉在一起。
    """
    module = _load_script("create_allure_quality")
    assert module.PLATFORM_ENVIRONMENTS == {"win32": "Windows", "darwin": "macOS"}

    # 真实的检查表: 两条平台专属的 mypy 检查必须带上各自平台的环境标签。
    environments = {check.key: check.environment for check in module.CHECKS}
    assert environments["mypy-win32"] == "Windows"
    assert environments["mypy-darwin"] == "macOS"
    assert environments["mypy"] == module.QUALITY_ENVIRONMENT
    assert environments["ruff-check"] == module.QUALITY_ENVIRONMENT

    results = tmp_path / "allure-results"
    probes = tuple(
        module.Check(
            f"probe-{flag}",
            f"Probe check ({flag})",
            (sys.executable, "-c", "print('ok')"),
            group="platform",
            host_platform=flag,
        )
        for flag in module.PLATFORM_ENVIRONMENTS
    )
    monkeypatch.setattr(module, "CHECKS", probes)
    # 探针归各自平台所有, 而当前机器只可能是其中之一: 把"当前平台"固定成 win32,
    # 于是这条用例在任何平台上都跑同一套流程(另一条探针走"跳过"分支)。
    original_select = module.select_checks
    monkeypatch.setattr(
        module,
        "select_checks",
        lambda group, _host: original_select(group, "win32"),
    )

    assert module.main(["--group", "platform", "--results-dir", str(results)]) == 0

    config = _ALLURE_CONFIG.read_text(encoding="utf-8")
    payloads = [
        json.loads(item.read_text(encoding="utf-8"))
        for item in sorted(results.glob("*-result.json"))
    ]
    assert len(payloads) == 1, "只有当前平台那一支会产出结果"
    payload = payloads[0]
    labels = {label["name"]: label["value"] for label in payload["labels"]}
    assert labels["testCategory"] == "quality"
    # 平台由 env 表达: 不带 os 标签(也不带平台参数), 执行主机只写进描述。
    assert "os" not in labels
    assert payload.get("parameters", []) == []
    # 跑在 win32 上, 所以结果的环境就是 Windows(而不是 Common)。
    assert labels["env"] == module.PLATFORM_ENVIRONMENTS["win32"]
    assert f"env={module.PLATFORM_ENVIRONMENTS['win32']}" in payload["description"]
    for environment in module.PLATFORM_ENVIRONMENTS.values():
        assert f'name: "{environment}"' in config, f"配置里要声明 {environment} 环境"
        matcher = f'value === "{environment}"'
        assert matcher in config, f"配置里缺少匹配 {matcher} 的 matcher"


def test_quality_job_runs_on_one_platform() -> None:
    """公共检查(ruff/mypy/静态分析/性能基准)只在一个平台上跑一遍, 而且在一个作业里.

    跑多平台只会得到多份一样的结论; 拆成多个作业则要多付一整套 checkout / uv / 依赖同步
    的固定开销。所以三批公共检查在同一个 Ubuntu 作业里依次跑。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  quality:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 quality job"
    job = block.group(1)
    assert "runs-on: ubuntu-latest" in job
    assert "matrix" not in job, "质量作业不应按平台展开"
    assert "--group core" in job, "公共质量门禁在这个作业里跑"
    assert "--group analysis" in job, "静态分析也在这个作业里跑"
    assert "pytest tests/performance" in job, "性能基准也在这个作业里跑"
    assert "allure-results-performance" in job, "性能结果照旧要上传(汇总作业要读)"
    assert "\n  analysis:\n" not in workflow, "不要再把静态分析拆成独立作业"
    assert "\n  performance:\n" not in workflow, "不要再把性能基准拆成独立作业"


def test_report_job_does_not_depend_on_the_host_platform() -> None:
    """报告作业跑在 Ubuntu 上, 平台必须由矩阵给出 —— 不能看 runner.os.

    合并、覆盖率汇总、报告生成与自检都是纯文件操作, 所以两个平台都在 Ubuntu 上跑(省掉
    Windows runner 的 2 倍计价与那些 pwsh 分支)。但"这份结果算哪个平台的"由数据决定:
    再用 ``runner.os`` 当平台名, 两个平台的结论都会被标成 Linux(两台机器都是 Linux)。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "pytest-report")

    assert "runs-on: ubuntu-latest" in job
    # 只看命令, 不看注释(注释里会拿 runner.os 当反面说明)。
    steps = job.split("steps:", 1)[1]
    commands = "\n".join(
        line for line in steps.splitlines() if not line.strip().startswith("#")
    )
    assert "runner.os" not in commands, (
        "报告作业里不能再按 runner.os 分支: 两台都是 Linux, 平台只能来自矩阵"
    )
    platforms = {
        entry["platform"]
        for entry in ci_workflow.matrix_entries(workflow, "pytest-report")
    }
    assert platforms == set(required_platforms()), (
        "报告作业的平台必须与质量门要求的平台一致"
    )


def test_platform_checks_only_run_on_their_own_platform(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """平台专属检查只能跑在自己那一支上, 跑不了的分组要明确报错而不是默默放过.

    ``--platform`` 是"检查哪一支代码", 不是"在哪台机器上跑": 放到别的平台上照样退 0,
    报告里却会出现一条"产自 Ubuntu 的 Windows 结论"。所以脚本按 ``Check.host_platform``
    自己挑: 不匹配就跳过并写出理由; 显式要一个在当前平台全都跑不了的分组时以退出码 2
    报错 —— 悄悄什么都不做等于这道门禁不存在。
    """
    module = _load_script("create_allure_quality")
    original = module.select_checks
    plain = module.Check(
        "probe-plain", "Probe plain", (sys.executable, "-c", "print('ok')")
    )
    win32_probe = module.Check(
        "probe-win32",
        "Probe win32",
        (sys.executable, "-c", "print('ok')"),
        group="platform",
        host_platform="win32",
    )
    monkeypatch.setattr(module, "CHECKS", (plain, win32_probe))

    # 直接看挑选逻辑: 在 win32 上只跑自己那一支, 在 linux 上整组都不适用(all 仍能跑公共检查)。
    assert [item.key for item in original("platform", "win32")[0]] == ["probe-win32"]
    assert original("platform", "linux")[0] == ()
    assert [item.key for item in original("platform", "linux")[1]] == ["probe-win32"]
    assert [item.key for item in original("all", "linux")[0]] == ["probe-plain"]
    assert [item.key for item in original("all", "linux")[1]] == ["probe-win32"]

    monkeypatch.setattr(
        module, "select_checks", lambda group, _host: original(group, "linux")
    )

    # 跑全部: 不适用的那支跳过并说明理由, 其余照常跑。
    results = tmp_path / "allure-results"
    assert module.main(["--results-dir", str(results)]) == 0
    written = [
        json.loads(item.read_text(encoding="utf-8"))["name"]
        for item in sorted(results.glob("*-result.json"))
    ]
    assert written == ["Probe plain"]
    printed = capsys.readouterr().out
    assert "跳过 Probe win32" in printed
    assert "只在 Windows 上执行" in printed
    assert "(当前平台 " in printed, "跳过时要写清当前平台"

    # 显式点名一个在当前平台跑不了的分组: 退出码 2, 且不写任何"通过"的结论。
    wrong_platform = tmp_path / "wrong-platform"
    assert (
        module.main(["--group", "platform", "--results-dir", str(wrong_platform)]) == 2
    )
    assert not list(wrong_platform.glob("*-result.json"))
    assert "平台专属检查必须在对应平台上跑" in capsys.readouterr().err


def test_platform_check_runs_in_a_job_on_its_own_platform() -> None:
    """平台专属检查要真的跑在对应平台上, 且结论要被汇总作业收走.

    ``mypy --platform win32`` / ``darwin`` 只把类型检查指向某支代码, 并不校验执行环境:
    在 Ubuntu 上跑出来的结论挂到 Windows 环境里就是假的归属。所以它放在 pytest 作业的
    **片 0**、且仅限 Windows/macOS(单独开作业只是多付一套固定开销 —— 而且其中一个是
    最贵的 macOS runner)。这里把"跑在哪个平台"与"标着哪个平台"钉在一起。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    block = re.search(r"\n  pytest:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 pytest job"
    job = block.group(1)
    step = job.split("name: Run the platform-specific check", 1)[1]
    step_condition = step.split("run:", 1)[0]
    assert "matrix.shard == 0" in step_condition, "只需一片跑: 多跑只是重复"
    assert "runner.os != 'Linux'" in step_condition, "平台专属检查不该跑到 Ubuntu 上"
    assert "always()" in step_condition, "测试失败也要给出这条结论"
    assert "--group platform" in step
    assert "allure-results-quality-platform-${{ matrix.os }}" in job
    # 公共质量作业不再代跑平台专属检查(否则同一批检查会跑两遍, 归属还重复)。
    quality = re.search(r"\n  quality:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert quality is not None
    assert "--group platform" not in quality.group(1)
    assert "\n  quality-platform:\n" not in workflow, "也不该再有独立的平台检查作业"

    # 汇总作业必须能等到它, 并把产物收进报告。
    summary = re.search(r"\n  allure-summary:\n(.*)", workflow, re.DOTALL)
    assert summary is not None, "ci.yml 里找不到 allure-summary job"
    assert "pattern: allure-results-*" in summary.group(1), (
        "汇总要用一份 pattern 收全各作业的结果(平台专属检查那份也在里面)"
    )


def test_ci_workflow_only_uses_top_level_keys_github_knows() -> None:
    """顶层只许出现 GitHub 认得的键 —— 多写一个 `x-...` 就等于整份工作流不生效.

    出处(2026-10-06 实测): 为了共用分支规则与"纯文档改动"的忽略清单, 有人在顶层开了
    `x-ci-branches` / `x-ci-ignore-patterns` 两个"扩展键"来放 YAML 锚点 —— 这在很多 YAML
    工具里行得通, 而 GitHub 的解析器直接报 `Unexpected value 'x-ci-branches'`: 整个工作流
    **一条作业都不会跑**(连"文档改动不跑 CI"都成了假象 —— 不是不跑, 是没有 CI 可跑)。
    锚点挂在认得的键上即可(见下一条用例), 这道守卫把顶层键的集合钉死。
    """

    workflow = _WORKFLOW.read_text(encoding="utf-8")
    allowed = {
        "name",
        "run-name",
        "on",
        "permissions",
        "env",
        "defaults",
        "concurrency",
        "jobs",
    }
    for path in (_WORKFLOW, ci_workflow.NIGHTLY_WORKFLOW):
        workflow = path.read_text(encoding="utf-8")
        found = {
            match.group(1)
            for line in workflow.splitlines()
            # 只用顶层行: 注释与缩进行不算(块状字符串的内容都是缩进的)。
            if line
            and not line[0].isspace()
            and not line.startswith("#")
            and (match := re.match(r"([A-Za-z][\w-]*):", line)) is not None
        }

        assert found, f"{path.name}: 一个顶层键都没找到, 这条守卫失去了检查对象"
        assert found <= allowed, (
            f"{path.name} 顶层出现了 GitHub 不认的键: {sorted(found - allowed)}"
            " —— 它们会让整份工作流报 Unexpected value 而不生效"
        )


def test_ci_cancels_superseded_runs_and_skips_docs_only_changes() -> None:
    """连续 push 要取消被取代的运行; 纯文档改动不必跑整套 CI, 但必需检查必须拿到结论.

    旧的一轮没人看, 却跟新的一轮一样贵(约 21 个作业)。

    路径过滤**不能直接加在 pull_request 上**: 命中忽略时整条工作流不触发, 分支保护里的
    必需检查永远停在 pending, PR 反而合不了。所以两边用两种办法: push 用 `paths-ignore`
    (真的不跑); PR 用轻量 `changes` 作业判路径, 再由重量级作业的 `if:` 条件跳过 —— 这样
    检查结果总会出现。忽略清单用 YAML 锚点声明一次, 两个触发器共用同一份规则。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    assert re.search(r"^concurrency:\n", workflow, re.MULTILINE) is not None
    assert "cancel-in-progress: true" in workflow
    assert "group: ${{ github.workflow }}-${{ github.ref }}" in workflow, (
        "按 ref 分组: PR 与 dev/main 各管各的"
    )

    # 忽略清单与分支规则各声明一次(锚点), push 用它们; PR 侧引用同一份分支规则。
    # 锚点必须挂在 GitHub 认得的键上 —— 顶层开 `x-ci-*` 键会让整份工作流不生效,
    # 由下面 test_ci_workflow_only_uses_top_level_keys_github_knows 守卫。
    push_block = workflow.split("\n  push:\n", 1)[1].split("\n  pull_request:", 1)[0]
    assert re.search(r"branches: &ci_branches", push_block), "分支规则用锚点声明一次"
    assert re.search(r"paths-ignore: &ci_ignore_patterns", push_block), (
        "忽略清单用锚点声明一次, 免得 push / PR 两份规则走散"
    )
    declared = set(re.findall(r'^\s+- "([^"]+)"$', push_block, re.M))
    assert declared == {
        "**/*.md",
        "docs/**",
        ".github/instructions/**",
        ".github/prompts/**",
    }, f"push 的忽略清单变了: {sorted(declared)}"

    pull_block = workflow.split("\n  pull_request:", 1)[1].split("\njobs:", 1)[0]
    assert "paths-ignore" not in pull_block, "PR 不能用路径过滤: 必需检查会停在 pending"
    assert "branches: *ci_branches" in pull_block, "PR 的分支规则与 push 同一份(锚点)"

    # `changes` 作业里的 filters 是**块状字符串**(锚点进不去), 所以两边必须逐条对齐:
    # 少一条就会"push 不跑、PR 照跑"(或反过来), 而这种事在日志里看不出来。
    filter_block = workflow.split("filters: |", 1)[1].split("\n\n", 1)[0]
    negated = set(re.findall(r"^\s+- '!([^']+)'$", filter_block, re.M))
    assert negated == declared, (
        f"push 的忽略清单与 PR 的路径过滤要逐条一致: {sorted(declared)} vs {sorted(negated)}"
    )

    # PR 侧靠轻量作业判路径 + 重量级作业条件跳过, 并且有一个"无论如何都给结论"的门禁作业。
    assert workflow.count("dorny/paths-filter") == 1, "用一份路径过滤实现, 别各写一份"
    assert "needs.changes.outputs.should_run == 'true'" in workflow, (
        "重量级作业要按路径过滤的结果跳过"
    )
    gate = ci_workflow.job_block(workflow, "required-check")
    assert "always()" in ci_workflow.job_condition(workflow, "required-check"), (
        "门禁作业要无条件运行: 它要在 PR 上给必需检查一个结论"
    )
    assert "needs: [changes]" in gate, "门禁要等路径过滤出结果"


def test_pr_and_nightly_run_full_while_push_selects_layers() -> None:
    """PR / nightly 跑全量, **只有 push** 按改动路径选层(2026-10-09 换向).

    - **PR = 全量**: 合并前要完整证据, 所以四段 pytest 与 security 都不看层输出;
    - **push = 分层**: 合并那一轮只为"确认没改坏"(报告留到 nightly), 所以按改动路径
      选层 —— 只改 UI(src/archive_management/ui、test_gui_*、视觉基线)只跑 UI 段
      (冒烟都挂在 test_gui_* 上且 smoke+ui 双标记, ``-m "ui"`` 天然含冒烟), 其它代码
      改动只跑非 UI 段; 共享基础设施(依赖清单 / conftest / 分片器 / 这份工作流)两层
      都跑 —— 只跑一层等于拿半套证据赌合并;
    - **报告 = 除 push 以外的每一轮**: PR 与 nightly 都要出报告(覆盖率门槛
      fail_under=100 与完整报告), 只有 push 那一轮不出 —— 它只跑选中层, 而切片的
      覆盖率天生偏低, 在那里判门槛等于自造假红;
    - **nightly 拆在 nightly.yml**: 它只做两件事 —— 判断 dev 自上一轮以来有无新提交
      (tip 提交年龄 < 24h), 有就带 `ref: dev` 调用本文件。全量链与报告链仍只有
      ci.yml 一份定义, 两份文件不会各自漂移。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")

    # 定时那条线在 nightly.yml: 它只有"门 + 调用", 不复制任何作业定义。
    nightly = ci_workflow.NIGHTLY_WORKFLOW.read_text(encoding="utf-8")
    # 排版不参与判据: 仓库里两种缩进风格并存(见 tests/ci_workflow.py 的 jobs()), 这里只钉
    # 结构 —— cron 挂在 schedule 下的列表里、调用 ci.yml 时紧跟 with/ref: dev。
    schedule = re.search(r"^( *)schedule:\n( *)- cron: ", nightly, re.M)
    assert schedule is not None, (
        "nightly(schedule)触发丢了: 覆盖率门槛与完整报告要有它兜底"
    )
    assert len(schedule.group(2)) > len(schedule.group(1)), (
        "cron 要挂在 schedule 下那一层列表里, 不是与它同层"
    )
    assert "uses: ./.github/workflows/ci.yml" in nightly, (
        "nightly 要调用 ci.yml, 而不是复制一份流水线(两份分片矩阵迟早漂移)"
    )
    assert re.search(
        r"uses: \./\.github/workflows/ci\.yml\n\s+with:\n\s+ref: dev\b", nightly
    ), "nightly 调用 ci.yml 时必须带 `ref: dev`: 这一轮测的要是 dev 的代码"
    assert "86400" in nightly, "提交年龄门槛(24h)要与每日一跑的节奏对上"
    assert "if: needs.gate.outputs.run == 'true'" in nightly, (
        "dev 24h 内没动时整个跳过: 没变化的一轮不产生新证据"
    )

    # ci.yml 里不该再留定时触发与"今晚要不要跑"的判断。
    code = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )
    assert not re.search(r"^  schedule:", code, re.M), (
        "定时触发已搬到 nightly.yml: ci.yml 里只留 workflow_call"
    )
    assert re.search(r"^  workflow_call:\n", code, re.M), (
        "ci.yml 要能被 nightly 调用(workflow_call)"
    )
    assert "freshness" not in code, (
        "要不要跑由 nightly.yml 的 gate 决定, ci.yml 里不该有第二份"
    )
    assert code.count("ref: ${{ inputs.ref || github.sha }}") == code.count(
        "actions/checkout@v7"
    ), "每个检出都要认 inputs.ref, 否则 nightly 那一轮测的还是默认分支"

    changes = ci_workflow.job_block(workflow, "changes")
    for output in ("should_run", "ui", "backend"):
        assert output + ": ${{ steps.decide.outputs." + output + " }}" in changes, (
            f"changes 作业要经 decide 步输出 {output}"
        )
    filter_step = changes.split("name: Detect files that require CI", 1)[1]
    filter_step = filter_step.split("- name:", 1)[0]
    assert "if: github.event_name != 'schedule'" in filter_step, (
        "schedule 下 dorny 没有基线可比: 要跳过它, 由 decide 步兜底"
    )
    assert "EVENT_NAME: ${{ github.event_name }}" in changes
    assert '"$EVENT_NAME" = "schedule"' in changes, (
        "decide 步在 schedule 下走专门分支(层输出全绿)"
    )
    # 门在 nightly.yml 的 gate 作业里: 直接检出 dev, 再读它的 tip 提交时间。
    gate = ci_workflow.job_block(nightly, "gate")
    assert "ref: dev" in gate, "gate 要检出 dev: 下面读的就是它的 tip"
    assert "git log -1 --format=%ct" in gate, "取 dev 的 tip 提交时间"
    assert "github.event_name" not in gate, (
        "门就是 nightly 自己的职责: 不必再按事件分支(检出的已经是 dev)"
    )
    assert "HEAD_IS_FRESH: ${{ steps.freshness.outputs.fresh }}" not in changes, (
        "freshness 已搬到 nightly.yml: ci.yml 里只留「被调用时照跑」那一段"
    )
    assert 'echo "should_run=true"' in changes, (
        "被 nightly 调用时直接放行: 今晚要不要跑由 nightly.yml 的 gate 判"
    )

    # 层过滤的形状: ui = UI 专属 + 共享基础设施; backend = 除文档与 UI 专属外的一切。
    ui_block = changes.split("\n            ui:", 1)[1].split(
        "\n            backend:", 1
    )[0]
    ui_entries = {
        entry.removeprefix("- '").removesuffix("'")
        for entry in (
            line.strip()
            for line in ui_block.splitlines()
            if line.strip().startswith("- '")
        )
    }
    assert ui_entries == {
        "src/archive_management/ui/**",
        "tests/integration/test_gui_*.py",
        "tests/visual-baselines/**",
        "pyproject.toml",
        "uv.lock",
        "tests/conftest.py",
        "tests/sharding.py",
        "tests/ci_workflow.py",
        ".github/workflows/ci.yml",
        ".github/workflows/nightly.yml",
    }, f"ui 层的路径集合变了: {sorted(ui_entries)}"
    backend_block = changes.split("\n            backend:", 1)[1].split("- name:", 1)[0]
    backend_negated = {
        entry.removeprefix("- '").removesuffix("'")
        for entry in (
            line.strip()
            for line in backend_block.splitlines()
            if line.strip().startswith("- '!")
        )
    }
    assert backend_negated == {
        "!**/*.md",
        "!docs/**",
        "!.github/instructions/**",
        "!.github/prompts/**",
        "!src/archive_management/ui/**",
        "!tests/integration/test_gui_*.py",
        "!tests/visual-baselines/**",
    }, f"backend 层的排除集合变了: {sorted(backend_negated)}"
    assert "- '**'" in backend_block, "backend 要兜底: 分不清的改动宁可多跑不漏跑"

    # 四段 pytest 步骤: **只有 push** 按层放行, PR / nightly 无视层输出(前缀放行)。
    pytest_job = ci_workflow.job_block(workflow, "pytest")
    for step_name, layer in (
        ("non-UI suite first (Linux)", "backend"),
        ("non-UI suite first (Windows/macOS)", "backend"),
        ("UI suite in its own process (Linux)", "ui"),
        ("UI suite in its own process (Windows/macOS)", "ui"),
    ):
        step = pytest_job.split(f"Pytest with coverage, {step_name}", 1)[1]
        step = step.split("- name:", 1)[0]
        condition = re.search(r"^        if: (.+)$", step, re.M)
        assert condition is not None, f"{step_name} 没有 if 条件"
        assert "github.event_name != 'push' || " in condition.group(1), (
            f"{step_name}: PR / nightly 必须无视层输出、两段全跑"
        )
        assert f"needs.changes.outputs.{layer} == 'true'" in condition.group(1), (
            f"{step_name}: push 上要按 {layer} 层放行"
        )
    # 段的标记表达式不许顺手改掉(两段互补不重不漏是分片与产物清单核对的前提;
    # 注释里也写着这对标记, 先剥掉再数)。
    code = "\n".join(
        line for line in pytest_job.splitlines() if not line.strip().startswith("#")
    )
    assert code.count('-m "not ui"') == 2
    assert code.count('-m "ui"') == 2

    pytest_condition = ci_workflow.job_condition(workflow, "pytest")
    assert "needs.changes.outputs.should_run == 'true'" in pytest_condition
    assert "github.event_name" not in pytest_condition, (
        "作业级不看事件: 层取舍只发生在 push 的步骤级, 否则 PR 会被误跳过"
    )

    for job in ("pytest-report", "allure-summary"):
        report_condition = ci_workflow.job_condition(workflow, job)
        assert "github.event_name != 'push'" in report_condition, (
            f"{job}: PR 与 nightly 都要出报告, 只有 push 那一轮不出"
        )
    security_condition = ci_workflow.job_condition(workflow, "security")
    assert "needs.changes.outputs.backend == 'true'" in security_condition, (
        "安全用例是后端语义, 纯 UI 的 push 跳过 security"
    )
    assert "github.event_name != 'push' ||" in security_condition
    quality_condition = ci_workflow.job_condition(workflow, "quality")
    assert "github.event_name" not in quality_condition, (
        "静态检查所在的 quality 作业每轮照旧, 既不是报告也不分层"
    )

    smoke_module = (
        _REPO_ROOT / "tests" / "integration" / "test_gui_smoke.py"
    ).read_text(encoding="utf-8")
    assert "pytest.mark.smoke" in smoke_module, (
        "test_gui_* 要挂 smoke 标记(UI 层含冒烟的前提)"
    )
    assert "pytest.mark.ui" in smoke_module, (
        'test_gui_* 要挂 ui 标记(-m "ui" 才能选中冒烟用例)'
    )


def test_report_jobs_do_not_start_for_a_superseded_run() -> None:
    """被取代的运行别再花时间做报告: 两个报告作业的条件都要排除“整轮被取消”.

    ``always()`` 的语义是“即使被取消也返回 true”, 所以只写 ``always()`` 会让已经被
    取代的那一轮照样启动合并/汇总作业 —— 那正是“取消了却还在跑报告”的来源。这里同时
    钉住两件事: ``always()`` 必须在(依赖作业失败时仍要出报告, 这是原有意图), 只是得
    配上 ``!cancelled()``。
    """
    text = ci_workflow.workflow_text()

    for job in ("pytest-report", "allure-summary"):
        condition = ci_workflow.job_condition(text, job)
        assert "always()" in condition, f"{job} 少了 always(): 依赖作业失败时也要出报告"
        assert "!cancelled()" in condition, f"{job} 在整轮被取消后仍会启动"


def test_the_pages_deploy_is_gone_and_the_report_is_an_artifact() -> None:
    """报告只作为 `allure-report-final` 产物存在: 不再发布到 GitHub Pages(2026-10-06).

    删干净比"留一半"重要 —— 站点发布涉及三处彼此耦合的配置, 漏一处就会留下把没通过自检的
    报告发出去的路径, 或者让 forks 上的 PR 拿到一条恒红的检查:
    ① 三个发布步骤(`Unpack the report for Pages` / `Upload the report as a Pages artifact` /
       `Deploy to GitHub Pages`)都不在了;
    ② `pages: write` / `id-token: write` 权限与 `github-pages` 环境一起删掉 —— 顶层只给
       `contents: read` / `actions: read`, 作业级权限**覆盖**顶层, 所以这里要显式写回
       `contents: read`(漏了它 checkout 直接失败)与 `actions: read`(取本轮产物);
    ③ 报告仍要作为产物上传(自检通过后才上传), 想浏览的人下它。
    """
    workflow = ci_workflow.workflow_text()
    job = ci_workflow.job_block(workflow, "allure-summary")
    # 只看真正生效的行: 注释里正拿 "github-pages" 当反面说明(与本仓库其它守卫同一个口径
    # —— 注释是文档, 不是配置)。
    code = "\n".join(
        line for line in job.splitlines() if not line.strip().startswith("#")
    )
    header = code.split("steps:", 1)[0]

    for gone in (
        "Deploy to GitHub Pages",
        "Unpack the report for Pages",
        "actions/upload-pages-artifact",
        "actions/deploy-pages",
    ):
        assert gone not in code, f"发布已经移除, 不该还留着 {gone}"
    assert "pages: write" not in header, "发布用的权限要一起删掉"
    assert "id-token: write" not in header, "发布用的 OIDC 权限要一起删掉"
    assert "github-pages" not in code, "发布用的 environment 也要一起删掉"
    assert "environment:" not in header, (
        "站点用的 environment 只能挂作业级, 删发布就该一起删"
    )

    assert ci_workflow.needs_of(workflow, "allure-summary") == {
        "changes",
        "quality",
        "pytest-report",
        "security",
    }, "汇总要等齐三批产物(报告还没传完就开始合并会静默少一部分)"
    assert "Generate final Allure report" in code, "汇总本身还在这里"
    assert "name: allure-report-final" in code, "报告仍要作为产物上传"
    assert "allure-report.tar.gz" in code, "上传的是自检通过后打好的那个归档"
    assert "steps.verify-report.outcome == 'success'" in code, "没通过自检的报告不上传"
    assert "contents: read" in header, "作业级权限是覆盖: 漏了它 checkout 会失败"
    assert "actions: read" in header, "取本轮的产物要它"


def test_report_artifacts_do_not_duplicate_the_whole_result_set() -> None:
    """同一个平台的结果集只能上传**一次** —— 合并后的副本再传一份是白花的额度。

    现场(2026-10-04, 用户要求减少上传): `allure-resources-<平台>` 里除了历史与产物清单,
    还带着一整份合并后的 `allure-results/`(每个平台一万多个文件), 而它**没有任何消费者**:
    两个报告作业取回历史时都只 `cp .../.allure/history.jsonl`; 汇总作业收的是同一批文件
    (`scripts/merge_allure_results.py` 按文件名搬到一处, 不改内容), 直接下分片产物即可。
    `allure-resources-final` 里那份更贵 —— 它是所有平台的合集, 是本工作流里最大的单笔上传。
    """
    workflow = ci_workflow.workflow_text()

    for step_name, artifact in (
        ("Upload Allure resources", "allure-resources-${{ matrix.os }}"),
        ("Upload final Allure resources", "allure-resources-final"),
    ):
        # 按**步骤名**取(而不是产物名): 产物名在"下载上一次历史"那一步里也会出现, 而它排在
        # 上传之前 —— 按产物名切会切到那一步上, 断言就变成在检查下载了。
        upload = workflow.split(f"name: {step_name}", 1)[1].split("- name:", 1)[0]
        # 只看真正生效的行: 这一步的注释里正拿 `allure-results/` 当反面说明(与本仓库其它几条
        # 守卫同一个口径 —— 注释是文档, 不是配置)。
        body = "\n".join(
            line for line in upload.splitlines() if not line.strip().startswith("#")
        )
        assert f"name: {artifact}" in body, f"{step_name} 上传的是 {artifact}"
        assert "allure-results/" not in body, (
            f"{artifact} 又带上了整份结果集: 分片产物已经传过一遍了"
        )
        assert ".allure/history.jsonl" in body, "历史文件必须在(趋势靠它)"
        assert "include-hidden-files: true" in body, "历史文件是隐藏文件, 要显式放行"

    # 汇总作业要**一次**把各作业的结果收全: 以前是五次按名字下载 + 从合并副本里摊平。
    summary = ci_workflow.job_block(workflow, "allure-summary")
    assert "pattern: allure-results-*" in summary, (
        "汇总要能一份 pattern 收全(分片 + 质量/分析/性能/平台检查/视觉/安全)"
    )
    assert "merge-multiple: false" in summary, (
        "各片要落在自己目录里: 摊平那一步按目录收, 缺了哪一片看得出"
    )
    for gone in (
        "name: Download performance Allure results",
        "name: Download security Allure results",
        "name: Download quality Allure results",
        "name: Download analysis Allure results",
        "name: Download visual Allure results",
        "name: Download platform check Allure results",
    ):
        assert gone not in summary, f"{gone} 已被一份 pattern 取代, 留着就是重复下载"


def test_htmlcov_is_not_uploaded_by_every_platform() -> None:
    """HTML 覆盖率报告不再三份都传: 同一份数据已经在报告里以 `coverage.xml` 附件挂着。

    两道口径要一起守住: 不再上传它之后, 本地的产物也不能留着不管 —— 生成步骤只写 XML,
    诊断清单里也不该还挂着它(否则每次失败都报一句"那个目录不存在", 而那是正常的)。
    只看**命令与上传路径**(注释里正拿这件事当反面说明, 与本仓库其它几条守卫同一个口径)。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in workflow.splitlines() if not line.strip().startswith("#")
    )

    assert "htmlcov" not in code, "已经不上传了, 命令与清单里都不该再出现"
    assert "coverage html" not in code, (
        "别再生成本地产物了: 传不上去, 只会让人以为有人看"
    )
    assert "coverage xml -o coverage.xml --fail-under=0" in code


def test_history_trends_survive_the_artifact_round_trip() -> None:
    """历史趋势靠 artifact 往返, 与报告是否发布无关。

    机制(这条守卫要钉住的不变式): 两个报告作业各自"找上一次**带着同名产物**的运行(不问
    成败) → 取回 `.allure/history.jsonl` → 生成报告(读它并追加本次一行) → 把新的
    history.jsonl 重新传进产物"。基线**不许**再要求 `conclusion === "success"`:
    2026-10-05 的教训 —— 只认成功运行时, 连续失败的时期(macOS Tk 崩溃那几天)没有任何
    一轮被消费, 基线冻结在键形状变更(environments 多出 environmentHash)之前的成功运行上,
    红运行写得再多也永远接不上, 报告里每条用例只剩自己的点。失败轮的历史行本来就该出现在
    趋势里(Allure 的趋势正是用来看失败的); "那一轮是否真的传了产物"由 finder 的 artifact
    检查把关, 中途取消/没跑到上传的运行自然被跳过。

    发布移除后仍要守一件事: `continue-on-error` **不能挂在作业上** —— 挂上去会把质量门的
    结论一起吞掉(那样门禁失败也只是条绿记录)。
    """
    workflow = ci_workflow.workflow_text()

    for job in ("pytest-report", "allure-summary"):
        block = ci_workflow.job_block(workflow, job)
        assert "Resolve previous run with Allure history" in block, (
            f"{job} 要先找到带着历史产物的上一轮运行"
        )
        assert 'conclusion === "success"' not in block, (
            f"{job} 的基线不许只认成功运行: 失败时期会把链条冻结在旧基线上, 历史永远接不上"
        )
        assert 'status === "completed"' in block, (
            f"{job} 只认已完成的运行(仍在跑/被取消的没有产物可取)"
        )
        hint = f"{job} 要取回上一次的历史文件"
        # 单文件上传的产物里 history.jsonl 落在压缩包**根部**(不带 .allure/ 前缀),
        # 多路径上传才保留前缀 —— 两种布局都得认(2026-10-05 断链事故: 只按带前缀的
        # 路径去 cp, final 链每轮清零, 最终报告的历史看起来"全丢了")。更细的守卫在
        # test_ci_history.py 的 test_history_restore_accepts_both_artifact_layouts。
        assert ".previous-allure-resources/.allure/history.jsonl" in block, hint
        assert ".previous-allure-resources/history.jsonl" in block, hint
        assert "include-hidden-files: true" in block, (
            f"{job} 要把新的历史文件传回产物(它是隐藏文件, 必须显式放行)"
        )

    summary = ci_workflow.job_block(workflow, "allure-summary")
    job_keys = summary.split("steps:", 1)[0]
    assert "continue-on-error" not in job_keys, (
        "不能挂在作业上: 那会把质量门的结论一起吞掉"
    )


def test_ci_keeps_the_evidence_of_a_hard_crash() -> None:
    """进程级崩溃(SIGSEGV/SIGABRT) 留不进 Allure —— 必须靠 artifact 把 ``crash-dumps`` 带出来.

    2026-10-03 的 macOS 分片实测: 段错误发生在界面的 ``update_idletasks`` 里, 进程被内核
    杀掉, coredumpy(挂在"用例失败"这个 Python 钩子上)根本没机会跑, pytest-cov 也来不及
    落盘覆盖率 —— 事后只剩"那一次运行的日志"这一个地方可查。所以这个目录要当产物上传,
    并在同一个作业里回显进运行日志(调试时最顺手的地方还是日志)。
    """
    job = ci_workflow.job_block(ci_workflow.workflow_text(), "pytest")

    upload = job.split("name: Upload crash dumps", 1)[1].split("\n      - name:", 1)[0]
    assert "if: always()" in upload, "用例失败/进程崩溃后仍然要上传"
    assert "path: crash-dumps/" in upload
    assert "if-no-files-found: ignore" in upload, (
        "大多数分片本来就没有现场: 不该因此把作业变红(与覆盖率产物'缺了就报错'的取舍不同)"
    )

    echo = job.split("name: Echo crash dumps into the run log", 1)[1]
    assert "cat " in echo, "崩溃日志要回显进运行日志(不该逼着人先下载 artifact)"


def test_tkinter_check_still_fails_the_job_when_tcl_is_broken() -> None:
    """Tkinter 自检要保留"修不好就红"的语义(只是从"每次重装"改成"失败才重装").

    先普通安装解释器、自检失败时才 `--reinstall` 是省额度用的, 但**不能**把这道守卫削弱:
    那一步失败的 `continue-on-error` 必须由紧随其后的修复步骤接手(它再复检一次, 且没有
    continue-on-error), 否则 Tcl 坏掉只会表现为一堆 GUI 用例 skip, 覆盖率门槛变成难定位的失败。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  pytest:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 pytest job"
    job = block.group(1)

    setup = job.split("name: Set up Python", 1)[1].split("- name:", 1)[0]
    # 只看命令本身(注释里会提到 --reinstall 作为反面说明)。
    setup_command = setup.split("run:", 1)[1].strip()
    assert setup_command.startswith("uv python install 3.12")
    assert "--reinstall" not in setup_command, "不应无条件重装解释器(那等于每次都重下)"

    check = job.split("name: Verify Tkinter works (Windows/macOS)", 1)[1]
    check_head = check.split("run:", 1)[0]
    assert "id: tkinter" in check_head, "修复步骤要靠这个 id 判断自检结果"
    assert "continue-on-error: true" in check_head

    repair = job.split("name: Repair the interpreter and re-check Tkinter", 1)[1]
    repair_head = repair.split("run:", 1)[0]
    repair_body = repair.split("run:", 1)[1].split("- name:", 1)[0]
    assert "steps.tkinter.outcome == 'failure'" in repair_head, "只在自检失败时重装"
    # 只看步骤自己的键(注释里会写"这一步没有 continue-on-error"来解释语义)。
    repair_keys = [
        line.strip()
        for line in repair_head.splitlines()
        if not line.strip().startswith("#")
    ]
    assert "continue-on-error: true" not in repair_keys, "修复步骤自己要能拦住提交"
    assert "uv python install --reinstall 3.12" in repair_body
    assert "uv sync --locked" in repair_body, "重装解释器后让虚拟环境跟上"
    assert repair_body.count("import tkinter") == 1, "修完必须复检一次"


def test_macos_swaps_the_interpreter_to_dodge_tk_9() -> None:
    """macOS 的解释器换成 python.org 构建(setup-python 再分发, 捆 Tcl/Tk 8.6).

    2026-10-05 两轮实证: uv 托管的 python-build-standalone 在 macOS 捆 **Tcl/Tk 9.0**,
    UI 分片死在 9.0 的 Aqua 位图绘制 use-after-free 上(``-[NSCGSContext dealloc]`` 一族),
    而且"退出码闸门 + 原地重跑"救不了 —— 重跑那次崩在同一处, faulthandler.log 里两条
    ``Fatal Python error``、体积翻倍到 1.1 GiB。依赖层面的规避: python.org 官方构建捆
    Tcl/Tk 8.6, 无此缺陷(actions/python-versions 在 macOS 上对 3.11+ 直接再分发
    python.org 的 universal2 安装包)。三件套缺一不可: 只认系统解释器、真的装 python.org
    构建、盯住 Tk 版本的断言步(镜像哪天回到 9.x 立刻红, 而不是等 UI 分段崩成一串)。
    """
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    block = re.search(r"\n  pytest:\n(.*?)\n  \w", workflow, re.DOTALL)
    assert block is not None, "ci.yml 里找不到 pytest job"
    job = block.group(1)

    # 1) macOS 片只认机器上已装好的解释器: uv 不得再下载 standalone(它捆 Tk 9.0)。
    assert (
        "UV_PYTHON_PREFERENCE: "
        "${{ matrix.os == 'macos-latest' && 'only-system' || 'managed' }}" in job
    ), "macOS 片要 only-system, 其它平台保持 uv 默认的 managed"

    # 2) 解释器来自 setup-python(macOS 上对 3.11+ 再分发 python.org 构建)。
    setup = job.split(
        "name: Set up Python (macOS, python.org build with Tcl/Tk 8.6)", 1
    )[1].split("- name:", 1)[0]
    assert "actions/setup-python@v7" in setup
    assert 'python-version: "3.12"' in setup
    assert "if: runner.os == 'macOS'" in setup

    # 3) Tk 版本的"眼睛": 断言步必须真的检查主版本, 否则规避只是自我感觉良好。
    guard = job.split("name: Assert Tcl/Tk stays on the 8.6 series (macOS)", 1)[1]
    guard = guard.split("- name:", 1)[0]
    assert "TkVersion < 9" in guard

    # 4) "重装 uv 解释器"这味药对 macOS 不存在(它的解释器不是 uv 装的): uv 安装步与
    #    修复步都要排除 macOS, 失败由 macOS 专属步骤响亮报错 —— 不许静默滑过去。
    uv_setup = job.split("name: Set up Python (uv-managed, Linux/Windows)", 1)[1]
    assert "if: runner.os != 'macOS'" in uv_setup.split("- name:", 1)[0]
    repair_head = job.split("name: Repair the interpreter and re-check Tkinter", 1)[1]
    assert "runner.os != 'macOS'" in repair_head.split("run:", 1)[0]
    loud = job.split(
        "name: Fail loudly if Tkinter is broken on the python.org build", 1
    )[1].split("- name:", 1)[0]
    assert "if: steps.tkinter.outcome == 'failure' && runner.os == 'macOS'" in loud
    assert "exit 1" in loud, "失败要说出来, 别让 GUI 用例静默 skip 成覆盖率谜团"


def test_quality_output_reaches_the_report_without_control_sequences(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """报告里的原始输出不能带终端控制码(实测 deptry 会带 ANSI 颜色 → 显示成乱码).

    探针检查会真的打印 ANSI 颜色与 ``\\r`` 进度行, 走的是与真实检查完全相同的
    写入路径: 附件与描述里都不该剩下控制码, 进度行只保留最后一段。
    """
    module = _load_script("create_allure_quality")
    script = "print('\\x1b[1m\\x1b[32mok\\x1b[m'); print('half\\rfinal')"
    probe = module.Check("probe", "Probe check", (sys.executable, "-c", script), "core")
    monkeypatch.setattr(module, "CHECKS", (probe,))
    results = tmp_path / "allure-results"

    assert module.main(["--group", "core", "--results-dir", str(results)]) == 0

    payload = json.loads(
        next(results.glob("*-result.json")).read_text(encoding="utf-8")
    )
    attached = (results / payload["attachments"][0]["source"]).read_text(
        encoding="utf-8"
    )
    assert "\x1b" not in attached, f"附件里还有终端控制码: {attached!r}"
    assert "\x1b" not in payload["description"], "描述里也不该有控制码"
    assert "ok" in attached
    assert "final" in attached
    # subprocess 读文本时走 universal newlines, ``\r`` 已被翻成 ``\n``; 助手自身仍要能
    # 收敛残留的 ``\r``(工具直接写字节 / 用了别的新行模式时会出现), 以及剥掉颜色码。
    assert module.sanitize_output("half\rfinal") == "final"
    assert module.sanitize_output("\x1b[32mok\x1b[m") == "ok"


def test_run_ledger_includes_the_native_quality_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """总账要收录原生质量门: 有日志时报出退出码与原始输出, 没日志时明说未执行.

    CI 会在生成报告前跑一次门禁并把输出(末尾带一行 ``退出码: N``)留在仓库根; 总账
    据此给出结论 —— 不去猜 CLI 的输出文本。本地直接跑汇总脚本时没有这份日志, 那一节
    要写"未执行", 而不是让读者以为门禁通过了。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    (results / "t1-result.json").write_text(
        json.dumps(
            {
                "uuid": "t1",
                "name": "某个用例",
                "fullName": "demo#case",
                "status": "passed",
                "labels": [{"name": "env", "value": "Linux"}],
            }
        ),
        encoding="utf-8",
    )
    payloads = module.result_payloads(results)

    # 没有日志: 写明未执行, 不能看起来像通过。
    empty = module.run_ledger(results, payloads, None, [])
    assert "## 原生质量门(Allure CLI)" in empty
    assert "未执行" in empty

    # 有日志: 退出码 1 → 未通过; 颜色码要剥掉, 退出码那行翻成结论后不再照抄。
    monkeypatch.chdir(tmp_path)
    (tmp_path / "allure-quality-gate.txt").write_text(
        "\x1b[31mQuality Gate failed with following issues:\x1b[m\n"
        "maxFailures: 1 exceeds 0\n"
        "退出码: 1\n",
        encoding="utf-8",
    )
    ledger = module.run_ledger(results, payloads, None, [])

    assert "**未通过(退出码 1)**" in ledger
    assert "Quality Gate failed" in ledger
    assert "\x1b" not in ledger, "CLI 输出的颜色码不能进报告"
    assert "退出码: 1" not in ledger, "退出码翻成结论后不该再原文照抄"
