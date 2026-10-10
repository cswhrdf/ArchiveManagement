"""
视觉回归门与结论目录: CJK 绘制拒绝、独立起始时间、Tk 版本记录、quality 作业接线、目录与脚本/上游作业同步。拆自 test_report_verification.py(见 docs/test-refactor-plan.md S8)。
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

import ci_workflow
from report_support import (
    _REPO_ROOT,
    _WORKFLOW,
    _conclusion_table,
    _load_script,
    _write_platform_result,
)

pytestmark = [
    pytest.mark.minor,
    pytest.mark.epic("工程与发布"),
    pytest.mark.feature("测试基础设施"),
    pytest.mark.story("报告完整性校验"),
    pytest.mark.layer("unit"),
]


def test_the_visual_gate_refuses_a_frame_that_cannot_draw_cjk(tmp_path: Path) -> None:
    """字体画不出汉字的画面**不许当基线**: 结论照写(带探针), 候选基线一个都不产出.

    出处(2026-10-02): CI 的候选基线里整排按钮是空的(汉字一个都没画出来), 而那一轮在报告里
    是**通过** —— 判据只比"这轮与基线像不像", 基线本身是坏的时候它看不出来。所以脚本先跑
    字体探针, 拿它当门禁。
    """
    module = _load_script("create_allure_visual")
    broken = {"cjk_width": 0, "actual": "fixed"}
    assert module.font_problem(broken), "汉字量不出宽度时必须判不可用"
    assert module.font_problem({"cjk_width": 40, "actual": "fixed"}), (
        "落到核心位图字体也一样不可用(没有抗锯齿)"
    )
    assert not module.font_problem({"cjk_width": 40, "actual": "Noto Sans CJK SC"})

    work = tmp_path / "work"
    work.mkdir()
    (work / "01-画面.png").write_bytes(b"\x89PNG\r\n\x1a\nfake bytes, good enough")
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    results = tmp_path / "results"
    results.mkdir()

    comparison = module.process_screen(
        "01-画面.png",
        order=0,
        work_dir=work,
        baselines=tmp_path / "baselines",
        candidates=candidates,
        results_dir=results,
        host="Linux",
        skipped=(),
        probe={
            **broken,
            "cjk_candidates": [],
            "families": 3,
            "platform": "Linux",
            "python": "3.12.0",
            "tk": "8.6.14",
            "tk_module": "/usr/lib/python3.12/lib-dynload/_tkinter.cpython-312-x86_64-linux-gnu.so",
            "tk_libraries": ["/usr/lib/x86_64-linux-gnu/libtk8.6.so.0"],
            "requested": "Noto Sans CJK SC",
            "ascii_width": 20,
        },
        blocked=module.font_problem(broken),
    )

    assert not comparison.passed
    assert list(candidates.iterdir()) == [], "画不出汉字的图不该被当成候选基线"
    payload = json.loads(
        next(results.glob("*-result.json")).read_text(encoding="utf-8")
    )
    assert payload["status"] == "failed", "必须标红: 这一轮的画面不可信"
    assert "汉字量出来是 0 宽" in payload["statusDetails"]["message"]
    names = [attachment["name"] for attachment in payload["attachments"]]
    assert "fonts.txt" in names, "探针要作为附件进报告(不然下一次还得猜)"
    assert "actual.png" in names, "坏图也要留着: 人要看它坏成什么样"
    # 修法与现场一起写进证据: "用哪个 Tk"是这道门禁的关键输入, 光有版本号说明不了它是哪一份。
    assert "_tkinter" in payload["statusDetails"]["message"], (
        "判不可用时要把当前用的是哪个 `_tkinter` 一起写出来"
    )


def test_each_visual_result_gets_its_own_start_time(tmp_path: Path) -> None:
    """同一轮里每张画面的开始时间必须**各不相同**: 报告里的列表顺序就是它.

    出处(用户 2026-10-03 实测): 报告里视觉回归那一节的顺序是 `01, 03, 02, 05, 04, 06` —— 两两
    成对交换。现场是结果数据里的 `start`: 02 与 03、04 与 05 **完全相同**, 因为每写一条结论都
    现取一次毫秒时间戳, 而相邻两张撞在同一毫秒里 —— 同值在 Allure 里没有稳定的先后。修法是
    "基准 + 序号"(见 :func:`write_result`), 这条用例把那个不变量钉住: 改回"各取一次时间"就红。
    """
    module = _load_script("create_allure_visual")
    work = tmp_path / "work"
    work.mkdir()
    results = tmp_path / "results"
    results.mkdir()
    candidates = tmp_path / "candidates"
    candidates.mkdir()
    for index in range(3):
        (work / f"{index:02d}-画面.png").write_bytes(b"\x89PNG\r\n\x1a\nfake")
        module.process_screen(
            f"{index:02d}-画面.png",
            order=index,
            work_dir=work,
            baselines=tmp_path / "baselines",
            candidates=candidates,
            results_dir=results,
            host="Linux",
            skipped=(),
            probe=None,
        )

    rows = sorted(
        (
            json.loads(path.read_text(encoding="utf-8"))
            for path in results.glob("*-result.json")
        ),
        key=lambda payload: str(payload["name"]),
    )
    assert [row["name"] for row in rows] == [
        "视觉回归 · 00-画面",
        "视觉回归 · 01-画面",
        "视觉回归 · 02-画面",
    ]
    starts = [int(row["start"]) for row in rows]
    # 白盒口径: 钉住"每张的开始时间 = 基准 + 序号"这个不变量本身 —— 只看"互不相同"不够,
    # 写三张结果之间的文件 IO 本来就可能跨过一毫秒, 那样改回"各取一次时间"照样是绿的
    # (咬合验证时实测到过)。
    base = int(module._RUN_STARTED_AT)  # pyright: ignore[reportPrivateUsage]
    assert starts == [base, base + 1, base + 2], (
        f"每张的开始时间应当等于基准 + 序号, 实测 {starts}(基准 {base})"
    )


def test_the_probe_records_which_tk_is_in_use() -> None:
    """探针要记下 `_tkinter` 模块与 Tcl/Tk 库的**路径**, 而不只是版本号.

    出处(2026-10-02): 报告里写着"Linux / 3.12.3 / 8.6.14", 而 3.12.3 是**发行版解释器**的版本
    —— 那一步当时为了拿发行版的 Tk 把解释器也换掉了, 于是报告里出现一个项目里哪里都不用的
    Python 版本。教训是"解释器版本对了不代表 Tk 也对了": 同一个 Tk 8.6 可能来自发行版(走
    Xft/fontconfig, 看得见 TTF 与汉字), 也可能来自解释器自带的副本(只认 X11 核心位图字体)。
    所以探针把两条路径都写下来, 下一次出问题时不用再猜。
    """
    module = _load_script("create_allure_visual")
    libraries = module.loaded_tk_libraries()
    assert isinstance(libraries, list)
    if not sys.platform.startswith("linux"):
        assert libraries == [], "非 Linux 没有 /proc/self/maps: 给空表, 不要编一份出来"

    module_path = "/usr/lib/python3.12/lib-dynload/_tkinter.cpython-312.so"
    library = "/usr/lib/x86_64-linux-gnu/libtk8.6.so.0"
    probe = {
        "platform": "Linux",
        "python": "3.12.11",
        "tk": "8.6.14",
        "tk_module": module_path,
        "tk_libraries": [library],
        "requested": "Noto Sans CJK SC",
        "actual": "Noto Sans CJK SC",
        "families": 12,
        "cjk_candidates": ["Noto Sans CJK SC"],
        "cjk_width": 40,
        "ascii_width": 20,
    }
    text = module.format_probe(probe)

    assert "Tk 模块" in text, "要写出 `_tkinter` 的来路"
    assert module_path in text
    assert "Tcl/Tk 动态库" in text, "要写出真正加载到的库"
    assert library in text
    assert not module.font_problem(probe), "这一份探针就是「可用」的样子"


def test_visual_regression_runs_in_the_quality_job_and_reaches_the_report() -> None:
    """视觉回归(感知哈希 + SSIM)是公共质量作业的一道门禁, 结论要进 Common 环境并进总账.

    四件事必须同时成立, 缺一件这套机制就退化成"跑了但没人看得见":

    ① 脚本在**质量作业**里跑(与 ruff / 静态分析 / 性能基准同一个作业: 它同样与平台无关,
       单独开作业只是多付一整套 checkout / uv / 依赖同步的固定开销);
    ② 它要**真实显示**: runner 上先装 xvfb, 命令走 ``xvfb-run`` —— Tk 在无头环境里起不来,
       而"起不来"绝不能被记成"画面没问题";
    ③ 它**不按平台展开**: 基线图入库、只在 Linux 采, 三个平台各采一套会把"字体度量不同"
       变成一堆假红;
    ④ 结论项带 ``env=common`` + ``testCategory=quality``: 前者让它落进报告里显式声明的
       Common 环境(而不是某个平台), 后者让运行总账的质量检查表与"工程门禁:质量检查未通过"
       分类都能看见它 —— 这就是"视觉回归也要体现到报告里"的落点。
    """
    module = _load_script("create_allure_visual")
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    job = ci_workflow.job_block(workflow, "quality")

    assert "scripts/create_allure_visual.py" in job, "脚本要在质量作业里跑"
    assert "xvfb-run -a" in job, "无头 runner 上必须经 xvfb 起显示"
    assert "xvfb" in job, "对应的系统包也要装"
    assert "allure-results-visual" in job, "结论要作为产物上传"
    assert "tests/visual-baselines" in job, "基线图入库, 从仓库里读"
    # 基线**不能**放未入库的本地目录: 那类目录被它们自己的 .gitignore 挡在仓库外, CI 上根本
    # 不存在(曾经因为复用界面评审用的那个抓图脚本, 这一步在 CI 上以"找不到复用脚本"退出 2)。
    # 只看命令: 注释里正拿这件事当反面说明(与本仓库其它几条守卫同一个口径)。
    # 断言里留着那个目录名 —— 它就是这个坑的代号, 谁再把门禁挂上去就当场红。
    code = "\n".join(
        line for line in job.splitlines() if not line.strip().startswith("#")
    )
    assert "ui-review" not in code, (
        "门禁不许依赖未入库的本地目录(它们不进仓库, 且只是开发阶段的临时物)"
    )
    assert "matrix" not in job, "视觉回归不按平台展开: 基线只按一种渲染采"
    assert "font" in job, "要装 CJK 字体: 否则汉字被量成零宽, 画面与基线对不上"
    # 但"装了字体"还不够: 用的那个 Tk 必须看得到 fontconfig/FreeType。uv 托管的 CPython 里
    # 带的 Tcl/Tk 是 python-build-standalone 自己编的, 在 X11 上只走**核心位图字体**(实测它的
    # libtcl9tk9.0.so 只有 XLoadQueryFont 那套符号), 汉字一个都画不出来 —— 2026-10-02 的候选
    # 基线就是这么空的。修法是**换 Tk 而不换解释器**: 把发行版 python3-tk 的 `_tkinter` 模块
    # (它链发行版 libtk8.6/Xft) 放在 PYTHONPATH 最前面。
    assert "python3-tk" in job, "要装发行版的 Tk: uv 托管那份在 X11 上画不出汉字"
    assert "tcl8.6" in job, "发行版 Tk 的库包也要装"
    assert "tk8.6" in job, "发行版 Tk 的脚本包也要装"
    assert "tk-xft" in job, "发行版那份 `_tkinter` 要留一份给这一步用"
    visual_step = job.split("name: Run the visual regression", 1)[1].split(
        "\n      - name:", 1
    )[0]
    # 解释器必须是**项目版**: 之前这一步用 /usr/bin/python3.12 去拿发行版的 Tk, 而 Ubuntu
    # 24.04 的系统 Python 是 3.12.3 —— 报告里"平台 / Python / Tk"那行因此写着一个项目里哪里
    # 都不用的版本。现在只换 `_tkinter` 模块, 解释器仍归 uv 管(与其余作业同版本)。
    assert "--python-preference" not in visual_step, (
        "视觉回归不许把解释器换成发行版的: 报告里的 Python 一栏要是这一轮真正的解释器"
    )
    assert "--python /usr" not in visual_step, "同上: 解释器归 uv 管, 换的只是 Tk"
    assert 'PYTHONPATH="$RUNNER_TEMP/tk-xft' in visual_step, (
        "发行版的 `_tkinter` 要排在 sys.path 最前面, 否则换不上"
    )
    assert "_tkinter.__file__" in visual_step, (
        "换没换上要当场自检: 不然会拿着一张位图字体的图去比基线"
    )
    assert "UV_PROJECT_ENVIRONMENT" in visual_step, (
        "那一份装进独立环境, 别动本作业其余步骤共用的 .venv"
    )

    # 产出这一步的门禁必须与消费它的上传步一致: 上传那一步是 `always()` +
    # `if-no-files-found: error`, 而这两步原本没写 `if:`(默认 success()) —— 前排的门禁
    # (xenon)一红, 它们就被整段跳过, 目录从没被创建, 于是报出"没有文件"这种误导性的红
    # (2026-10-10 run 37958738335: 日志里既没有装 Xvfb 也没有跑视觉回归, 只有那个上传步)。
    # 同作业的静态分析与性能基准都是"各管各的", 这里跟齐。
    for step_name in (
        "Install Xvfb, a CJK font, and the distro Tk 8.6",
        "Run the visual regression",
    ):
        block = job.split(step_name, 1)[1].split("\n      - name:", 1)[0]
        assert "if: always()" in block, (
            f"{step_name} 要 `if: always()`: 前面的门禁红了不该整段跳过它, "
            "否则上传那一步会以「没有文件」红掉(看起来像没产出, 其实是没跑)"
        )

    summary = ci_workflow.job_block(workflow, "allure-summary")
    assert "pattern: allure-results-*" in summary, (
        "汇总作业要把这份结论收进报告(现在是一份 pattern 把各作业的结果一起收全)"
    )

    # 标签决定归属: 改动这两处会让结论落错环境或从总账里消失。
    assert module.ENVIRONMENT == "common"
    assert (module.CATEGORY_LABEL, module.CATEGORY_VALUE) == ("testCategory", "quality")
    # 判据必须**两条都在**(哈希管"整体变了没有", SSIM 管细粒度), 且门槛是有界的数字。
    assert 0 <= module.MAX_HASH_DISTANCE <= 64, "哈希距离上限要在 64 位以内"
    assert 0.0 < module.MIN_SSIM <= 1.0, "SSIM 下限要在 0~1 之间"
    # 阈值必须落在"实测噪声"与"真要人看的变化"之间(太紧会把 runner 镜像更新变成随机红,
    # 太松等于没判据)。2026-10-05 的 CI 实测(run 37270928260): 同渲染噪声
    # **= 0**(四张与基线逐位相同, SSIM 1.0000); 最小真实变化 = SSIM **0.9819** / 哈希 **4**;
    # 已知真缺陷 = SSIM **0.9722**(11px 行高错)。这里守的是"那个窗口还在", 不是具体数字 ——
    # 调数字请连着那份实测一起改(并把新分布写进这条注释)。
    assert module.MIN_SSIM > 0.9722, "门槛不能高过已知的真缺陷(否则那一类会漏过)"
    assert module.MIN_SSIM < 1.0, "门槛不能等于 1.0: 同渲染也要留一点余量"
    assert 1 <= module.MAX_HASH_DISTANCE < 4, (
        "上限要能给同渲染留 1 位容错, 又要小于实测里最小真实变化的 4"
    )


def _upstream_jobs(workflow: str, job: str) -> set[str]:
    """某个作业的上游(``needs`` 的传递闭包): 只有这些作业的产物它才拿得到.

    头一个作业(如 ``pytest``)没有 ``needs`` —— 上游为空, 不是错。
    """

    def declared(name: str) -> set[str]:
        block = ci_workflow.job_block(workflow, name)
        if not re.search(r"^\s*needs:", block, re.MULTILINE):
            return set()
        return ci_workflow.needs_of(workflow, name)

    found: set[str] = set()
    pending = list(declared(job))
    while pending:
        current = pending.pop()
        if current in found:
            continue
        found.add(current)
        pending.extend(declared(current) - found)
    return found


def test_the_catalog_is_in_sync_with_the_scripts_that_write_conclusions() -> None:
    """结论清单必须与产出方代码**双向**同步 —— 这条就是"自动同步"的守卫.

    为什么不能靠人维护: 清单决定总账里"应有"的一栏, 少了登记就等于那一类结论缺了也不会被
    点名(2026-10-02: 视觉回归的产物与 ``test`` 组都没上传, 报告里安静地少了两节)。
    两个方向都要拦: 代码里写了新身份而清单没登记 -> 缺了看不出; 清单登记了没人写的身份 ->
    总账会一直报一项永远不会出现的缺失。

    清单本身也是"从代码里解析出来的": 质量检查项来自 ``create_allure_quality.py`` 的
    ``CHECKS``(见下一条守卫), 所以在这个脚本里加一项检查不需要回来改清单。
    """
    catalog = _load_script("allure_catalog")
    uncatalogued, unwritten = catalog.identity_gaps(_REPO_ROOT / "scripts")

    assert not uncatalogued, (
        f"这些身份会被写进 Allure 结果, 但清单里没登记(缺了也不会被总账点名): "
        f"{sorted(uncatalogued)} —— 加进 scripts/allure_catalog.py 的 CATALOG"
    )
    assert not unwritten, (
        f"清单登记了这些身份, 但没有任何脚本会写它们(总账会永远报缺失): "
        f"{sorted(unwritten)}"
    )


def test_every_expected_conclusion_names_a_live_script_and_an_upstream_job() -> None:
    """清单里每条结论的"产出脚本 / 上传作业"必须真的存在, 而且那个作业在汇总作业的上游.

    否则清单会慢慢变成一份"看上去很详细"的文档: 作业改了名、脚本搬了家、或者汇总作业
    不再 ``needs`` 那个作业(产物根本下不下来), 都不会有人发现 —— 而总账只会照着清单
    说"应该有", 报出来的缺失无法定位。
    """
    catalog = _load_script("allure_catalog")
    workflow = _WORKFLOW.read_text(encoding="utf-8")
    workflow_jobs = ci_workflow.jobs(workflow)
    upstream = _upstream_jobs(workflow, "allure-summary")

    for item in catalog.CATALOG:
        assert (_REPO_ROOT / item.script).is_file(), (
            f"{item.key} 声明的产出脚本不存在: {item.script}"
        )
        assert item.job in workflow_jobs, (
            f"{item.key} 声明的上传作业不在工作流里: {item.job}"
        )
        assert "actions/upload-artifact" in workflow_jobs[item.job], (
            f"{item.job} 已经不上传任何产物, {item.key} 的缺失会被误报"
        )
        if item.gate == catalog.GATE_ON_DEMAND:
            # 按需那一族(作业崩溃现场)的证据**不来自某个上游作业**: 任何作业(包括汇总作业
            # 自己)崩了, 诊断都由那个作业自己上传、汇总作业就地收下来 —— 所以这里不要求
            # "在 needs 链上"。别的检查(脚本存在/作业存在/真在传产物)照旧。
            continue
        assert item.job in upstream, (
            f"{item.key} 的产物由 {item.job} 上传, 但汇总作业拿不到它"
            f"(不在 needs 链上: {sorted(upstream)})"
        )


def test_expected_items_expand_every_family_and_every_quality_check() -> None:
    """「应有」清单的展开: 每族结论都要出现, 质量检查逐项展开且跟着 ``CHECKS`` 走.

    逐项展开是关键 —— 只写"质量检查这一族"的话, ``bandit`` 那一项没产出时会被同一族的
    别的检查顶上(家族前缀匹配的经典错法), 缺失永远看不出来。
    """
    catalog = _load_script("allure_catalog")
    scripts = _REPO_ROOT / "scripts"
    platforms = ["Windows", "macOS", "Linux"]
    items = catalog.expected_items(platforms, scripts)

    families = {item.producer.key for item in items}
    always_expected = {
        producer.key
        for producer in catalog.CATALOG
        if producer.gate != catalog.GATE_ON_DEMAND
    }
    assert families == always_expected, (
        "每族结论都要在「应有」里 —— 按需的那一族(作业崩溃现场)例外: 它只在真的崩了时才"
        "有, 缺了不算缺失(见 GATE_ON_DEMAND)"
    )

    checks = catalog.quality_checks(scripts)
    assert checks, "要从 create_allure_quality.py 里解析出检查项"
    common = {f"archive-management.quality.{check.key}" for check in checks}
    assert {item.identity for item in items} >= common, "每一项检查都要逐项展开"

    platform_checks = [check for check in checks if check.host_platform]
    environments = catalog.platform_environments(scripts)
    assert (
        environments == _load_script("create_allure_quality").PLATFORM_ENVIRONMENTS
    ), "平台映射要从 create_allure_quality.py 解析出来, 且与它自己的常量一致"
    expected_platform_rows = {
        (item.identity, item.environment)
        for item in items
        if item.producer.key == "quality-platform"
    }
    assert expected_platform_rows == {
        (
            f"archive-management.quality.{check.key}",
            environments[check.host_platform or ""],
        )
        for check in platform_checks
    }, "平台专属检查要落在它自己的平台上"

    # 每平台一项的结论按声明的平台各展开一份; 声明少一个平台, 那个平台就整族消失。
    per_platform = {family.key: 0 for family in catalog.CATALOG}
    for item in items:
        if item.environment:
            per_platform[item.producer.key] += 1
    assert per_platform["tests"] == len(platforms)
    assert per_platform["coverage"] == len(platforms)
    assert per_platform["security"] == len(platforms), (
        "security 每轮三个平台都跑(不做降频), 所以三个平台各要一行"
    )


def test_missing_conclusions_are_loud_in_the_ledger_and_as_broken_items(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """产物没交上来时必须**点名**: 总账里标缺失, 报告里另写一条 broken 结论项.

    以前总账每一节都是"有就渲染、没有就写一句没有", 于是"少了哪一节"完全看不出来 ——
    报告永远是完整的, 只是内容少一块。这里锁定三件事: 应有/实行的逐项对照、缺失项在表里
    标 **缺失**、以及每个缺失项各写一条 broken 结论项(报告里一眼可见, 原生质量门跟着红)。
    """
    module = _load_script("create_allure_summary")
    results = tmp_path / "allure-results"
    results.mkdir()
    # 现象: 只有 Windows 交了产物, macOS/Linux 的覆盖率没了; 视觉回归整族没上传;
    # 质量检查只交了一项(其余检查项各自算缺失, 不会被同一族顶掉)。
    _write_platform_result(
        results,
        "cov-win",
        ("env", "Windows"),
        ("os", "Windows"),
        full_name="archive-management.coverage",
    )
    _write_platform_result(
        results,
        "case-win",
        ("env", "Windows"),
        ("os", "Windows"),
        ("framework", "pytest"),
    )
    _write_platform_result(
        results,
        "ruff",
        ("env", "common"),
        ("os", "Linux"),
        ("testCategory", "quality"),
        full_name="archive-management.quality.ruff-check",
    )
    # 性能/安全数据齐全: 它们的结论项由本脚本自己写, 不能被报成缺失(自报自缺的经典错法)。
    (tmp_path / "performance-results.json").write_text(
        json.dumps(
            {
                "platform": "Linux",
                "environment": {"os_family": "Linux"},
                "measurements": [],
            }
        ),
        encoding="utf-8",
    )
    findings = tmp_path / "security-findings" / "Linux"
    findings.mkdir(parents=True)
    (findings / "security-results.json").write_text(
        json.dumps({"environment": {"os_family": "Linux"}, "findings": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(module, "RESULTS_DIRECTORY", results)
    monkeypatch.setattr(
        module, "PERFORMANCE_JSON", tmp_path / "performance-results.json"
    )
    monkeypatch.setattr(module, "PERFORMANCE_CSV", tmp_path / "performance-results.csv")
    monkeypatch.setattr(module, "SECURITY_JSON", tmp_path / "security-results.json")
    monkeypatch.setattr(
        module, "SECURITY_FINDINGS_DIRECTORY", tmp_path / "security-findings"
    )
    monkeypatch.setattr(
        module, "QUALITY_GATE_REPORT", tmp_path / "allure-run-ledger.md"
    )
    monkeypatch.setattr(
        module, "COVERAGE_EXCLUSIONS_REPORT", tmp_path / "allure-coverage-exclusions.md"
    )
    monkeypatch.setattr(module, "NATIVE_GATE_LOG", tmp_path / "allure-quality-gate.txt")

    assert module.main(["--expect-platforms", "Windows,macOS,Linux"]) == 0, (
        "缺失不改汇总脚本的退出码: 判定交给原生质量门(我们写下的 broken 结论项会让它红)"
    )

    ledger = (tmp_path / "allure-run-ledger.md").read_text(encoding="utf-8")
    table = _conclusion_table(ledger)
    assert "| Coverage report(macOS) |" in table
    assert "**缺失**" in table
    row = next(line for line in table.splitlines() if "Coverage report(macOS)" in line)
    assert row.rstrip().endswith("**缺失** |"), f"缺的那一行要点名: {row}"
    received = next(
        line for line in table.splitlines() if "Coverage report(Windows)" in line
    )
    assert received.rstrip().endswith("已收到 |"), f"交上来的那行不能误报: {received}"
    # 视觉回归整族没有产物: 一行一项(它是公共项, 不分平台)。
    assert "| 视觉回归 |" in table
    assert "**缺失**" in next(
        line for line in table.splitlines() if line.startswith("| 视觉回归 |")
    )
    # 质量检查逐项判定: 交了的算收到, 没交的各自点名(不被同一族别的检查顶掉)。
    assert "| Ruff check |" in table
    for title in ("Bandit 安全扫描", "Xenon 复杂度门槛"):
        assert "**缺失**" in next(
            line for line in table.splitlines() if line.startswith(f"| {title}")
        ), f"{title} 没交产物却没被点名"
    # 本脚本自己写的那两类不能报缺失。
    for title in ("Performance baseline", "Security findings(Linux)"):
        assert "已收到" in next(
            line for line in table.splitlines() if line.startswith(f"| {title}")
        ), f"{title} 由本脚本自己写, 不该被报成缺失"

    written = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted(results.glob("*-result.json"))
    ]
    missing_items = {
        payload["name"]: payload
        for payload in written
        if str(payload.get("fullName", "")).startswith(module.MISSING_IDENTITY)
    }
    assert "缺少结论: Coverage report(macOS)" in missing_items
    assert "缺少结论: 视觉回归" in missing_items
    assert "缺少结论: Performance baseline" not in missing_items
    broken = missing_items["缺少结论: Coverage report(macOS)"]
    assert broken["status"] == "broken", "缺失项要是 broken, 否则质量门不会跟着红"
    assert broken["stage"] == "finished"
    assert "coverage-data" in broken["statusDetails"]["message"], (
        "缺什么要写在失败原因里"
    )
    assert broken["labels"][2]["value"] == "macOS", "缺失项要落在缺的那个平台上"
    # 总账与日志都要看得见(报告是产物, 不点开看不到)。
    printed = capsys.readouterr().out
    assert "::warning::缺少结论: Coverage report(macOS)" in printed
    assert "::warning::缺少结论: Performance baseline" not in printed
    assert "结论项应有" in printed, "控制台也要给出应有/缺失的数目"

    # 再跑一次(现场已经多了那几条 broken 项): 结论不能变 —— 缺失项自己不能被当成"收到了"。
    assert module.main(["--expect-platforms", "Windows,macOS,Linux"]) == 0
    again = (tmp_path / "allure-run-ledger.md").read_text(encoding="utf-8")
    assert _conclusion_table(again) == table, "重复跑汇总脚本时结论清单必须稳定"
