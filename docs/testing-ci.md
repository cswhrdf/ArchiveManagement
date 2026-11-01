# CI 流程、分片与报告

本文档说明结果与报告的元数据、CI 作业怎么组合、报告怎么自检、失败现场与超时怎么留证、分片与覆盖率口径，以及本地怎么生成与查看报告。测试如何分层、等级怎么定与写用例的通用纪律见 [testing.md](testing.md)，界面用例的判据见 [testing-ui.md](testing-ui.md)；各小节只保留仍然有效的机制与坑，完整复盘以测试文件的 docstring 与提交历史为准。

其他文档：[测试体系](testing.md)、[界面用例的判据与坑](testing-ui.md)、[开发与发布](development.md)。

## 1. 结果与报告元数据

- 每条性能测量记录：规模、指标、取值、单位、阈值、比较方向、是否通过；每条安全结论记录：类别、场景、输入摘要、期望拦截行为、实际结果、是否拦截。
- 环境信息由 `scripts/create_allure_summary.py` 写入 `allure-results/environment.properties`（操作系统与平台族、Python 版本与实现、提交 SHA、分支与 CI run id、测试类别是否执行、覆盖率门槛）；覆盖率摘要由 `scripts/create_allure_coverage.py` 生成。原始 JSON/CSV 与 `coverage.xml` 都作为附件带进报告（脚本按 `RAW_REPORT_FILES` 收集，存在哪个带哪个），保证结论可下载、可追溯；HTML 报告是整站，作为 `coverage-<os>` artifact 上传。
- **汇总结论项的状态由数据决定（报告不许骗人）**：
  - 性能 `Performance baseline` —— 只要有一条测量 `passed` 为假就写 `failed`（描述给出基准名/取值/阈值），结果文件缺失则写 `broken` 并说明原因。
  - 覆盖率**不另出结论项**：每个平台的 `Coverage report` 项自己就是结论——描述第一句固定是"当前覆盖率 X% 大于/小于预期覆盖率 Y%, 验证通过/未通过"（X 按 coverage.py 的 TOTAL 口径 `(行覆盖 + 分支覆盖) / (行总数 + 分支总数)`，Y 读 `pyproject.toml` 的 `[tool.coverage.report] fail_under`），低于门槛该条直接 `failed`，读不到 XML/数字是 `broken`。缺平台（报告作业挂了、产物没合并进来）由运行总账的覆盖率一节指出，不会因为"没有这一条"而静静变绿。
- **运行总账的末节是「证据核对（应有 vs 实有）」**：应有的证据清单由 `scripts/allure_catalog.py` 一族族展开（平台矩阵 × 类别 × 各汇总项），逐项标"已收到 / 缺失"；每个缺失项另写一条 `broken` 结论并打 `::warning::`（门禁跟着红）；「判定」列说明每一项由谁负责。开头一行数"应有 N 项，收到 M 项"；末尾两张小表（结论清单 / 结论项声明的附件文件），粒度不同不合成一张表。为什么需要：Allure 只判"结果文件里有的东西"，合并环节吞掉半个平台时报告照样绿——总账把"应该有什么"摊开在页面上，缺一眼可见。
- 性能 / 覆盖率 / 安全三节每平台一行，结论都从原始数据算出（脚本读文件而不是猜 CI 环境变量）。
- **「有意不统计的覆盖」是报告首页的全局附件**（`allure-coverage-exclusions.md`）：来自 pragma 标记（`# pragma: no cover` 等）与 `pyproject.toml` 的 `exclude_also`，按文件列出"路径:行号—标记—原因"与总条数，未写原因的标记单独指出；解析规则与 `tests/unit/test_coverage_pragmas.py` 共用同一实现（守卫同步，防止两头漂移）。
- **平台以 Allure 的"环境"维度呈现**（2026-10-05 起）：每条结果带 `env` 标签（平台展示名），`allurerc.mjs` 的 matcher 把标签映射成 Allure environment；报告里出现 Windows / macOS / Linux 三个环境，"按环境过滤/对比"直接可用。`env` 与 `os` 两个标签在**收集期**挂成标记（`conftest._attach_environment_labels`），**不能**写在 autouse 夹具里：`pytest.mark.skip` / `skipif` 由 pytest 在夹具之前求值，写在夹具里的标签会整个丢掉，结果会落进隐式的 `default` 环境——报告里按平台看不到它，逐平台用例数还会比分片自报的少（2026-10-14 run 38594250358 就是这样：Linux/macOS 各少 1 条，报告自检不过、最终报告没上传）。平台参数 `平台` 没有标记形态，仍留在夹具里。两个配套兜底：平台作为 `--platform` 参数进入结果身份——三平台 `retryHash` 各不相同（一个用例不会在跨平台合并后变成"重试"）、`historyId` 又共享（历史趋势能连上）；旧的 os 标签 + parentSuite 后缀保留（旧报告兼容 + 兜底）。**生成报告必须在仓库根执行**——Allure CLI 要读到 `allurerc.mjs` 才会识别环境，读不到会**静默**退回 default 环境，`scripts/verify_allure_report.py` 会判"报告不完整"。摘要类结论项同规则：带 env、标题不拼平台名（环境维度已经表达）。
- 用例标题里的参数化 id 会被 pytest 转成 ASCII 转义（`\u7528\u6237`），生成报告时还原（界面与 URL 里都是可读中文）。
- **`allurerc.mjs` 的三项读者辅助**：
  1. **categories 失败归类**——四条规则（id 与 `allurerc.mjs` / `tests/unit/test_test_config.py` 三处同步，改名要一起改）：
     - `env-tk-library`：环境:Tk/Tcl 库不可用（症状清单与 `tk_guard.KNOWN_TK_SKIP_MARKERS` 同源、大小写不敏感、限定 layer 为 integration/e2e）；
     - `env-transient-database`：环境:数据库瞬时读失败（`sqlite3.OperationalError` 且数据库被锁/暂时不可读）；
     - `gate-quality-check`：工程门禁:质量检查未通过（按 `testCategory=quality` 标签挑选）；
     - `gate-packaging-smoke`：工程门禁:产物冒烟未通过（按 `testCategory=smoke` 标签挑选，2026-10-27 起）；未命中的落进默认的 Product/Test errors 桶。
  2. **variables 报告顶部稳定事实**（覆盖率门槛、分层执行位置等；平台各自的事实按环境变量写）。每次运行会变的（提交号、分支、耗时）由 `environment.properties` 与总账负责，两边不重复不冲突。
  3. **allowedEnvironments 环境 id 白名单**——少列一个 id 时 `allure generate` 直接 Internal Error 硬失败（这也是一种守卫）。三项各有守卫（`tests/unit/test_test_config.py`、`tests/unit/test_report_quality_items.py`）。

## 2. CI 流程

```text
            ┌── quality (ubuntu): 静态检查 + 静态分析 + 性能基准 + 视觉回归
push/PR ────┼── pytest (win/mac/linux) × 3 片: 单元+集成(两段: 先 not ui 再 ui)
            ├── security (win/mac/linux): 安全测试
            ├── required-check: 纯文档改动跳过时的兜底
            └── packaging-gate: 打包门禁的“结论”作业(恒跑, 见下面那条)
                 │
                 ▼  (下面两步在 push / PR 每一轮都跑)
            pytest-report (ubuntu × 3 平台): 合并各片结果 + 生成 Allure 报告 + 自检
                 │
                 ▼
            allure-summary: 汇总三平台 + 覆盖率合并 + 原生质量门 + 总账 + 报告 artifact

合入 main 的 PR 额外: quality/pytest/security 全绿后 ── windows-package (调 release-build.yml:
    构建 + 产物冒烟, 只出 artifact) ── 其冒烟结论(allure-results-smoke-windows)由
    allure-summary 的 `pattern: allure-results-*` 一并收进汇总报告
```

2026-10-27 重排：**方向翻转**——`windows-package` 只等 `changes` / `quality` / `pytest` / `security`（产出结论的核心作业），**不再等报告链**；`allure-summary` 反过来 `needs` 它，好把构建后冒烟的结论产物收进汇总报告。测试红 ⇒ `windows-package` 被跳过（default needs 语义）⇒ `packaging-gate` 判红，合入照样被拦；而报告链的红（覆盖率门槛、原生质量门）由它们自己的必需检查兜住（**分支保护里除了 `CI required packaging status`，`pytest-report` / `allure-summary` 相关检查也要勾为必需**，否则存在"打包绿了但报告红"的合入盲区——重排后这两件事不再互相挡）。

- **打包门禁（2026-10-17 新增，只针对合入 `main` 的 PR）**：“用例绿”与“能编 + 产物能起”是两件事 —— 后者只有真跑一次 PyInstaller 才知道。所以 `ci.yml` 里的 `windows-package` 作业（`uses: ./.github/workflows/release-build.yml`）在 `changes` / `quality` / `pytest` / `security` 都过了之后，拿这个 PR 的**合并预览提交**构建单文件 exe 并跑产物冒烟（2026-10-31 起产物是 onefile）；它**只上传 artifact、不发 Release**（`checks: build-only`：同一轮 CI 已经跑过三平台全量，不在 Windows 上重跑一遍）。冒烟 = `scripts/smoke_packaged.py`（五条判据 × 那一份产物：无参数启动 / `--version` / `doctor` / `gui --smoke` / 单实例闸门；判据取退出码 + 侧效应，**不读 stdout**——窗口化产物没有控制台；无参数启动用 platformdirs 的 `WIN_PD_OVERRIDE_*` 把系统目录整体沙箱进临时目录，不碰真实应用目录；全部外部限时）。守卫：`test_the_build_smokes_the_packages_it_produced`（钉住"构建之后、打 release/ 之前"的位置）。
- **冒烟结论进汇总报告（2026-10-27 起）**：`smoke_packaged.py --report smoke-report.json` 把逐条判据落成 JSON（失败也写），打包作业里紧跟的 `scripts/create_allure_smoke.py` 转成 Allure 结果项（每条判据一条、`env=Windows` 归进那个平台的环境、`testCategory=smoke`）并上传 `allure-results-smoke-windows`——产物名匹配汇总作业的 `pattern: allure-results-*` 但不匹配每平台报告的 `allure-results-<os>-*`（不会被错并进单平台报告）。"该不该有"是**条件期望**：汇总作业按 `needs.windows-package.result` 推导并传 `--expect-smoke-platforms`（success/failure 都要求数，skipped/cancelled 传空=不适用），不适用时整族不进「应有」清单、不报假缺失。守卫：`test_the_packaging_smoke_results_reach_the_final_report` / `test_smoke_expectation_follows_the_declared_platforms`。
- **为什么门禁调的是 `release-build.yml` 而不是 `release.yml`**：嵌套工作流的权限是**逐作业静态校验**的 —— `release.yml` 里那个 `publish` 作业要 `contents: write`，而 PR 那次运行的顶层只有 `contents: read`，整份工作流会被判 `not valid`（原文：`The nested job 'publish' is requesting 'contents: write', but is only allowed 'contents: read'`），连一个 job 都起不来，运行期的 `publish: false` 拦不住。所以构建单独成一份**只读**的 `release-build.yml`（门禁与发布档共用它），写权限只留在 `release.yml` 的发布作业上。守卫：`test_the_packaging_gate_path_never_holds_write_permission`。
- **必需检查是 `CI required packaging status`（要在仓库设置的 Rules / branch protection 里勾上）**：分支保护里的必需检查不能由一个会被条件跳过的作业兼任 —— 纯文档改动的 PR 会跳过 `windows-package`，那样检查会永久停在 pending（与 `required-check` 同一条理由，见 `ci.yml` 开头的注释）。所以门禁=两个作业：干活的“`windows-package`”+ 恒跑的“结论”`packaging-gate`（三种情况给绿：纯文档改动 / 不是合入 main 的 PR / 编译成功；上游红了导致它被跳过 = `skipped` 也判红）。守卫：`tests/unit/test_ci_workflow.py` 里那三条（`test_the_release_workflow_only_publishes_from_main` / `test_the_packaging_gate_gates_pull_requests_into_main` / `test_the_packaging_gate_always_reports_a_conclusion`）。
- **发布只在合入之后（`release.yml` 的 `push: branches: [main]`）**：版本号（`packaging.py`）由人在 PR 里改（2026-11-01 起：`main` 有分支保护，没人能再改它），没变就只构建、不重复发 Release（`v<版本号>` 已存在是正常出口）；变了则建 Release，**只挂一个 `ArchiveManagement.exe`**，正文取自该 PR 里手写的「本次变更」一节（2026-10-31 定：不再附 `SHA256SUMS.txt` 与 `build-metadata.json`，也不再由 CI 自动生成说明）。`dev` **没有**自动档（只有手动 `workflow_dispatch`）。触发与口径见 `docs/development.md` 的「发布」一节。
- **作业数量也是额度**：每一轮（push / PR）16 个实例（3 平台 ×（3 pytest 片 + security）+ 2 报告 + quality + changes + required-check）；**合入 main 的 PR 再多 2 个**：一个 Windows 实例（`windows-package`）与一个秒级的 `release-hint`（ubuntu，只跑脚本 + 留评论），另加 `packaging-gate`（恒跑，不装任何工具 —— 它必须恒跑，理由见上）。GitHub 按并发作业计价，多开一个分片 = 少一份留给别的 CI 的额度。
- **跳过只发生在"纯文档改动"上（分桶在 changes 作业）**：纯文档（`**/*.md` / `docs/**` / `.github/instructions/**` / `.github/prompts/**`）在 push 上被 `paths-ignore` 挡在触发之外，在 PR 上由 `changes` 作业的 dorny 过滤给出 `should_run=false` 从而跳过重量级作业；`required-check`（总是成功）兜住分支保护规则的必需检查。**2026-10-14 起不再按改动路径挑层** —— 那条 ui / backend 两层分桶的规则（2026-10-09 落地）已回退：PR / push 每一轮的四段 pytest 与 security 都跑全量（步骤条件里只剩平台与"第一段跑完 / 整轮没被取消"）。守卫：`test_every_trigger_runs_the_full_suite`。
- **`pytest-report` 与 `allure-summary`（覆盖率门槛 100% 与完整报告）每一轮都跑**：push 与 PR 都是全量，覆盖率门槛与完整报告在哪个事件下都判得出（2026-10-14 起 push 也出报告：之前那条排除理由——"push 只跑选中的层，切片覆盖率天生偏低、在那里判门槛等于自造假红"——随选层回退一起失效了）。守卫：`test_every_trigger_runs_the_full_suite`。
- **省额度的三条**：① concurrency 取消被取代的运行（报告作业的条件必须是 `always() && !cancelled()`，否则前面被取消时报告不生成、上一轮的报告继续留在页面上冒充最新）；② 纯文档 push 用 `paths-ignore`、PR 用 `changes` 作业判路径 + `required-check` 兜底；③ 与平台无关的检查合进 quality、平台专属检查塞进 pytest 片 0（不新开作业）。完整的优化清单与取舍落在 `tests/unit/test_ci_workflow.py`。
- 每个 pytest 作业上传自己的 `allure-results-<platform>-shard<N>`；汇总用 `download-artifact` 的 pattern + `merge-multiple: true` 一次拉齐，`pytest-report` 沿用 `.allure/history.jsonl` 累积历史。
- **历史趋势靠 artifact 活着**（与报告怎么发布无关）：两个报告作业各自走"找上一次**带着同名产物**的运行（不问成败）→ 取回 `.allure/history.jsonl` → `allure generate` 读它并追加本次一行 → 新的 history.jsonl 传回 artifact"（`pytest-report` 用 `allure-resources-<平台>`，`allure-summary` 用 `allure-resources-final`）。要点：① 趋势寿命 = artifact 寿命（默认 90 天），窗口内没有一轮留着它就从零开始（不报错，曲线断了）；② 基线**不要求**那轮 success——失败轮的历史本该出现在趋势里，Allure 的趋势正是用来看失败的（只认成功运行时，连续失败的时期接不上任何一轮基线）；那一轮是否真的传了产物由 finder 的 artifact 检查把关；③ 报告只作为 artifact 存在（2026-10-06 起不再发布到 GitHub Pages），站点是否可达与历史链条无关；④ 恢复要认**两种 artifact 布局**——单路径上传时 `history.jsonl` 落在压缩包根部、多路径时带 `.allure/` 前缀，两种都探测、都找不到时响亮退出；⑤ **质量门不许写历史**——`allure quality-gate` 子命令同样遵循 `appendHistory`，会在半套结果的时点追加一条假快照，跑之前把真历史改名为同目录临时文件（改名即回、不跨设备）、跑完放回。守卫：`tests/unit/test_ci_history.py`。
- **历史文件在生成报告之前自动修一遍**（`scripts/repair_allure_history.py`，排在拷回历史之后、generate 之前）：报告趋势按 `retryHash` **精确匹配**，身份键里每多一个维度（如 `environmentHash`），旧快照的键就整体对不上（丢的是匹配不是数据）。三步：① 同一轮被追加两次的快照去重（按"时间窗 + 键集合重合度"判，留信息更全的那份）；② 旧形状的键按**唯一前缀**补成当前形状（前缀对上多个当前键时按条目自己的 `environment` 对准平台、显示名与 id 统一小写比较；段数比当前还多的键直接丢弃——砍前缀会悄悄并掉平台区别）；③ 补不出唯一值的条目丢掉，一份都连不上时清空重来。动了文件就写 `allure-history-repair.md` 挂进首页全局附件（记实际用的 CLI 版本与最新快照年龄），没修东西就删掉那份记录。守卫：`tests/unit/test_ci_history.py`（三种走向、幂等、"没修不留附件"）。已知局限：键形状刚变的第一轮，基线里还没有新形状的快照，这一轮会误判（下一轮起恢复）——这正是历史基线必须每轮都被消费的原因。
- **每个作业只装自己需要的依赖**：开发依赖拆成 test / coverage / quality / analysis / package 五组，作业按命令装对应组（`uv sync --locked --no-default-groups --group <组>`）。两个坑：顶层必须 `UV_NO_SYNC=1`（否则每个 `uv run` 按默认组先 sync 一遍，把省掉的包全装回来）；同一作业里每次 `uv sync` 必须带**同一组**（Tkinter 修复那步也 sync）。本地开发不受影响（`default-groups` 覆盖全部）。守卫：`test_ci_installs_only_the_dependency_groups_each_job_needs`。
- **每个作业跳过安装项目本身**（`--no-install-project`）：CI 用例靠 pytest 的 `pythonpath` 配置导入源码、静态检查靠 `mypy_path`，不需要可编辑安装。唯一例外 quality：装一次并跑 CLI 冒烟（cwd 换到工作区外跑 `python -m archive_management init`，验证打包入口）。守卫：`test_ci_only_installs_the_project_where_the_cli_smoke_needs_it`。
- **质量门禁由脚本执行**（2026-10-02 起）：`scripts/create_allure_quality.py --group <组>` 把每项检查的退出码、结论与完整输出附件写成 Allure 结果（任一项不过则脚本非 0 退出）。三组：`core`（ruff check / ruff format --check / 宿主 mypy → env=common）、`analysis`（deptry / bandit / pip-audit / radon / xenon → env=common）、`platform`（mypy --platform win32 / darwin）。前两组与性能基准同在 Ubuntu 的 quality 作业；platform 组各自在该平台的 pytest 片 0 执行（排在测试与上传之后）——`--platform` 只决定"检查哪支代码"，结论挂到没跑过的平台环境就是假归属。结论归属：与平台无关的检查带 `env=common` 归入显式声明的 Common 环境（不冒充任何平台）；平台专属 mypy 带平台 env 且真的在那台机器跑（`Check.host_platform`）。点名跑不了的分组时以退出码 2 报错（不是 0——静默跳过等于门禁不存在）。
- **Allure 原生质量门**：`allure quality-gate --config allurerc.mjs allure-results`，退出码直接决定作业成败，输出落 `allure-quality-gate.txt` 并进总账。两条规则集：① 不过滤（maxFailures: 0 / successRate: 0.98），脚本生成的结论项也算——覆盖率项 broken 时必须有人管；② 只看真实用例（filter 选 `framework=pytest` 再 environmentsTested），要求每个平台都有用例。**用环境维度而不是 `minTestsCount`**：绝对计数随用例规模变松（缺整平台仍可能高于计数，静默失效）；判据用 `framework=pytest` 正向标记——正向漏判会红、反向排除漏判会绿。**清单必须写环境 id 不能写显示名**：`environmentsTested` 拿结果上的 environment（id：windows/macos/linux）比清单，写显示名则所有平台全报缺。守卫：`test_quality_gate_asks_every_platform_for_real_tests`。
- 原生质量门排在写运行总账**之前**（总账读它落盘的输出），守卫：`test_native_quality_gate_is_configured_and_pinned`。它管不到"少一片"，那由产物清单负责（`--manifest`）。**CLI 必须 ≥ 3.18.0**：3.13~3.17 配 historyPath 时静默放行（上游 issue #895，修于 3.18.0）；CI 用浮动标签 `allure@3`，下限在 "Check Allure version" 步骤运行期核对。本地复现：`npx allure@3 quality-gate --config allurerc.mjs allure-results`（单平台跑会因 environmentsTested 失败，属预期）。

### 报告自检与上传（不要跳过）

报告是静态站点：详情页按需 fetch `data/test-results/<id>.json`，这个目录在传输/解压环节被丢掉时报告只剩汇总（点开用例是空的）。所以每个生成报告的作业在上传前跑 `scripts/verify_allure_report.py`：

- 检查入口资源（index.html、单个 app-\*.js、summary.json、test-results.json、widgets/\*\*/statistic.json、tree.json）；
- 逐条核对结果索引引用的详情文件存在、条数与 allure-results 一致；
- 校验用例分组引用的结果 id 都在索引里；
- **不许有结果落在 `default` 环境**：生成端没读到配置时**所有**结果都会进 default（由"报告里只有 default 环境"那条拦下），只个别几条进 default 则是**它们没带 `env` 标签**的信号（报告里按平台看不到它们）——检查会点名到用例（`结果名 (id)`）；
- `--expect-platforms` 的每个平台环境里都要有**真实用例**（判据 `framework=pytest`，与质量门同一判据）；
- 传了 `--manifest` 时把"分片自报条数"与最终条数对上（声明的片号都到了、各片自报数 ≤ 结果文件数、报告各平台用例数 ≥ 自报数）。

任一项不过即作业失败且**不上传报告**。自检通过后用 `--archive` 打成单个 `allure-report.tar.gz` 上传（单文件要么完整到达要么报错，不会半损坏）。**平台用例这项是唯一不直接判红的**（continue-on-error，结论由汇总末尾的门禁结论步骤接手）：它是内容问题不是"报告坏了"，拦下来反而拿不到证据。`pytest-report` 传 `--expect-platforms "${{ matrix.platform }}"`（平台来自矩阵不是 `runner.os`——报告都跑在 Ubuntu 上）；汇总传 `Windows,macOS,Linux`。为什么它能发现 `environmentsTested` 发现不了的事：汇总项与平台专属检查都带平台 env，"环境存在"不等于"这个平台测过"；两道都在是有意的（自检能逐平台报数，也不随 CLI 升级静默失效）。

Windows runner 控制台是 cp1252，Python 默认按它输出，**打印中文会 UnicodeEncodeError 打断步骤**：工作流最外层设 `PYTHONUTF8=1`，报告脚本自己也会把 stdout 切 UTF-8（取不到 reconfigure 的替身如 capsys 就跳过）。新增向终端打中文的脚本注意这条。

本地核对命令：

```shell
# 报告内容是否完整
uv run python scripts/verify_allure_report.py allure-report
# 汇总产物是否含所有平台
uv run python scripts/verify_allure_report.py allure-report --expect-platforms Windows,macOS,Linux
# 分片合并后条数是否对上（目录名以片号结尾）
uv run python scripts/merge_allure_results.py allure-results-shard0 allure-results-shard1 -o allure-results --manifest allure-manifest.json
uv run python scripts/verify_allure_report.py allure-report --manifest allure-manifest.json
```

### 失败现场留证（dump + 界面截图）

只在 CI 出现、本地不复现的失败，事后只有一行 traceback——所以用例失败时自动把现场挂到该用例的 Allure 结果（`tests/crash_capture.py`）：

- **崩溃 dump**：coredumpy 写 `crash-dumps/<用例>.dump`（.gitignore 里）。本地打开：`coredumpy load <文件>`（进 pdb）/ VSCode 扩展「Load with coredumpy」/ `coredumpy peek crash-dumps`。**报告里只给下载链接**（附件媒体类型 application/octet-stream——按 text/plain 挂时 Allure 把上兆 JSON 读进预览区，一开报告页就卡死）。
- **界面截图**：本用例创建的窗口（`gui_support.py` 登记表）失败时的画面。Windows 按窗口句柄 `ImageGrab.grab(window=hwnd)`（被盖住也拍得到、不受显示缩放影响）；Linux/macOS 按屏幕区域（需要 DISPLAY / 屏幕录制权限）。抓不到只在摘要写一句原因，绝不影响用例结果。
- **失败摘要**：平台/Python/提交号 + 落点 + 复现命令。SQLite 类失败额外一行错误分类（如 `SQLITE_NOTADB (26)`——类名与消息随平台/版本变，只有 `sqlite_errorname`/`errorcode` 稳定），同时写进摘要与 dump 描述（本地不带 `--alluredir` 时 dump 是唯一现场）。

三条纪律：留证**绝不改变用例结果**（每步兜底）、**失败才留证**（通过的用例不产生文件）、**有上限**（递归深度默认 5、单次 dump 20s、超 25 MiB 只记落点、最多 3 张截图、同一用例只留一次）。参数 `--crash-dump-dir`（默认 crash-dumps）与 `--crash-dump-depth`（0 = 关掉 dump 仍留摘要）。

**段错误这类硬崩溃留不进报告**：SIGSEGV/SIGABRT 直接杀进程，钩子没机会跑（pytest-cov 也来不及落盘）。会话一开始由 `pytest_configure` 把 faulthandler 接到 `crash-dumps/faulthandler.log`（`enable_hard_crash_log`），CI 把目录当 artifact 上传并回显进日志。代价：硬崩溃只留得住线程栈没有局部变量；`faulthandler.enable` 只接受有真实 fileno() 的句柄，这条留证无论成败都不抛。

报告侧不用额外配置（附件随 allure-resources-\* 上传，verify_allure_report.py 一并核对）。**dump 里是真实局部变量**（coredumpy 会遮掉像密钥的串与 os.environ 值）——把报告发给仓库外的人之前先看一眼附件。

**作业级崩溃是另一条线**：每个作业失败时跑 `scripts/collect_job_diagnostics.py`（if: failure()），只有**进程级崩溃/证据缺失**才上传 `job-diagnostics-<作业>`（普通失败的证据已在结果与日志里），汇总收下拼成首页《失败现场(兜底)》附件 + 每个崩溃作业另写一条 broken 结论（标题"作业崩溃: <标签>"、环境=所在平台、testCategory=diagnostics——只挂附件不进计数也不能按环境筛）。**没有兜底现场时两样都不写**（还删掉上一轮留的）：永远存在的"失败现场"只会让人以为崩过。**摘要里被截断的现场要能点着找到**（`--full-copy-at` 写明承载全文的 artifact 与 run 的 artifact 列表页链接；faulthandler 摘录从尾部捞崩溃线程的 Current thread 栈——只截开头全是等在 threading.wait 的旁观线程）。

#### macOS 原生崩溃

崩溃不在本仓库代码，在**解释器自带的 Tcl/Tk**：uv python install 装的 python-build-standalone 构建在 macOS 捆绑 Tcl/Tk 9.0，UI 用例死在 9.0 的 Aqua 位图绘制 use-after-free（EXC_BAD_ACCESS，栈停在 `-[NSCGSContext dealloc]` → `_invalidate` 一族）。处置（三层都有守卫）：

- **从依赖层规避**：pytest 的 macOS 片换成 `setup-python` 的 python.org 构建（自带 8.6）+ `UV_PYTHON_PREFERENCE=only-system`，且自检一步同时断言 `TkVersion < 9`（解释器又带回 9.x 时当场红，提示见这条注释）。
- **重跑包装降级为「偶发崩溃与偶发超时的通用兜底」（保留，带到期日）**：UI 段的 pytest 外面包一层 `scripts/run_pytest_with_crash_retry.py`——两种死法同一类事故（**进程死了，它后面的一切都没了**）：① 崩溃只认"进程被信号/原生异常杀死"的退出码（SIGTRAP=133 / SIGSEGV=139 这一类，含 Windows 的 0xC0000005 / 0xC0000409）；② 超时**不看退出码**，只认 `tests/timeout_guard.py` 留下的痕迹（`fullName` 以 `archive-management.timeout.` 开头的那条结论，或 `*_timeout.dump`）。断言失败、门禁不过两者都没有，**一次都不会重跑**，真回归不会被重试稀释或掩盖。重跑前先清掉死掉那次新写进 allure-results 的半套结果（快照之前的一律不动，否则重跑后的报告缺半边、同用例出现两份结论）；覆盖率不用清（pytest-cov 只在会话结束落盘，那次什么都没写）。崩溃只重跑一次；超时那条路多一步：先原样重跑一遍（环境卡顿多半不会连着来，这一遍就全过了，那条超时结论也随之清掉），若仍超时，第三遍把挂住的那条用例 `--deselect` 掉——**其余用例与覆盖率先拿到手，而那次超时的结论留在报告里**（挂住是真信号，不许静默消失）。每次重跑都在运行日志留 `::warning::` 注解、在运行摘要页写一段说明。**这层包装带到期日**（脚本里的 `RETRY_UNTIL`）：过期后直接透传不再重试并打提醒，`tests/unit/test_ci_diagnostics.py` 里"窗口没被忘记"的守卫从到期次日起变红——要么移除这层包装，要么确认仍有值得兜底的偶发后**明确续期**。

一次真实的偶发就是靠它断的（2026-09-30）：一条 GUI 用例整轮全量里红过一次（单跑、整模块连跑两遍都不复现），手里只剩一个测试名。事后从 crash-dumps 里那次失败留下的 dump 读出根因——`OperationalError: unsupported file format`，栈最深一帧是 `MonitoredDirectoryRepository.list_all()` 的 `connection.execute(`，异常穿过 `_on_scan` 的 `except ArchiveManagementError` 冒到用例：**环境级偶发**（临时 SQLite 文件在那一刻被读成非 SQLite 内容），不是"等得不够"的时序问题。两条可复用的做法：① **先读现场再动手**——dump 是**失败当刻**写的且**写盘**（与有没有 --alluredir 无关），`coredumpy peek crash-dumps` 就能找到是哪条用例的；② 本地跑全量最好照 CI 的写法带上 `--alluredir=allure-results`，否则截图与其余附件不落盘。

### 超时留证（线程栈 + 覆盖率 + dump + 一条结论）

`--timeout` 用 thread 方式，上游收尾是"打印线程栈后 `os._exit(1)`"——跳过整条 atexit，正好丢掉最想要的两样：收尾才落盘的覆盖率数据与挂在 `pytest_runtest_makereport` 上的失败现场。`tests/timeout_guard.py` 借 pytest-timeout 的扩展点（`pytest_timeout_set_timer` / `cancel_timer`，钩子由 conftest 挂上）接管这条路径（只在 thread 模式；signal 模式抛异常走正常收尾，原样交给上游）：

```text
倒出用例至今的输出/日志 → 线程栈 → 覆盖率存盘 → 主线程帧的 coredumpy dump
  → 写一条 Allure 结论（栈与 dump 是附件）→ 冲干净输出 → os._exit(1)
```

要点：结论走**终端写入器**不走 print（pytest 捕获 stdout，`os._exit` 跳过收尾会把缓冲区一起丢）；**先倒出捕获内容**（"卡死之前程序自己打了什么"往往比栈更能说明问题）；覆盖率控制器挂在 pytest-cov 私有插件名 `_cov`、Allure 目录选项叫 `allure_report_dir`（猜错只会静默不写）；dump 超 25 MiB 只记落点。报告里是一条 broken 结论（标题"超时留证: <用例>"、critical），**故意不写 framework/testCategory 标签**（不该改变质量门口径）。留证不改变结局：最后照样以 1 退出（超时本来就不该被"想写证据"拖住）。守卫 `tests/unit/test_timeout_guard.py`，其中两条是接线守卫：钩子必须真挂在 `tests/conftest.py` 上、`pyproject.toml` 里必须仍然是 `--timeout-method=thread`（改成 signal，接管就永远不会生效，"超时后什么都没有"的老问题会静默回来）。

**留证的真正价值在下一个回合**：`os._exit(1)` 也让那之后的用例与覆盖率全没（2026-10-16 的 Windows 片：252 vs 282 条 e2e、覆盖率 99.51% 判红）。所以 CI 外面那层重跑包装还会**接着处置**这次超时（见上一节）：先原样重跑，仍超时就把这条 `--deselect` 掉跑完其余用例，并把这条结论留在报告里当信号。判据只看"这一遍有没有新的超时痕迹"（新写下的那条结论，或 dump 文件**内容变了**——同一条用例再挂一次会覆盖同名 dump，所以只比文件名是不够的）。

### 分片执行与结果合并

每个平台把用例拆成几片并行跑（三个平台各 3 片），再由 `pytest-report` 把各片结果合并成一份——墙钟时间只取决于最慢的那一片。

- **分片规则**（`tests/sharding.py`）：权重**三级取值**——每条用例的实测耗时（`tests/durations.json`）优先，没有实测的退回目录经验权重（集成 2s、安全 0.6s、单元 0.05s，未知目录 0.3s），再"最慢的优先"贪心装箱（LPT）。实测值是必须的：同一模块内单条用例能差 8 倍，只按目录单价装箱等于按**条数**装箱，慢用例会堆在同一片。刷新命令见第 3 节；记录/加载/合并的约定在 `tests/durations.py`，守卫 `tests/unit/test_durations.py`。CI 三平台共用一份档案（用的是同次录制内的相对排序）。
- **参数**是 `--shard-count` / `--shard-index`（默认 `1`/`0` 即不分片），过滤发生在**严重等级过滤之后**：本地 `--min-severity=critical` 选出的子集也能分片跑。三条性质由 `tests/unit/test_sharding.py` 锁住：不重不漏（各片并集 == 全集）、同输入同分片、各片权重接近理想值。
- **不要用 pytest-xdist**：GUI 用例各自起真实窗口，并行只会互相拖慢还引出 Tk 初始化失败；分片是进程级（CI 上机器级）并行。
- **本地全量想快**：`uv run python scripts/run_tests_local.py`（双进程分片，实测省约三分之一墙钟，与单进程逐数一致）。**2 片是甜点不要贪多**（两进程同时操作真实窗口，桌面合成层互相拖慢）。脚本起 N 个进程等全部结束按片打印摘要，pytest 参数用 `--` 透传，片号由脚本注入别再写。
- **连覆盖率门禁一起跑**：`uv run python scripts/run_tests_local.py --coverage` —— 每片用 `coverage run --rcfile=.coverage-platform.rc -m pytest` 起（各写自己的 `.coverage.shard-<片>`，收尾 combine + report，报告的退出码计入脚本退出码；rc 不在时脚本自己生成）。实测 2026-10-14（2850 条）：单进程 14:48 → 双片 13:05，结论逐数一致（100%）。**省得不多是因为这套用例被 GUI 与磁盘 I/O 卡着**；并发还会放大 GUI 用例本来就有的时序敏感点（拿到红先单跑那个模块核实，见 `.github/instructions/local-full-run.instructions.md`；2026-10-14 那条 `test_clicking_the_arrow_opens_the_dropdown` 已经量到成因并让用例自己摆正起点，再红就是回归）。
- **合并**在 `pytest-report`（每平台一份，都在 Ubuntu）：`scripts/merge_allure_results.py` 搬各片结果（日志逐片给文件数）；覆盖率 `COVERAGE_FILE=.coverage.shard-<片>` 分片写再 combine。
- **报告可在别的平台生成**：合并/汇总/生成/自检全是纯文件操作，三平台报告都在 Ubuntu 生成（Windows/macOS runner 按 2/10 倍计价）；"结论算哪个平台"由矩阵参数决定（`--platform` / `--expect-platforms`），**不能看 runner.os**（守卫 `test_report_job_does_not_depend_on_the_host_platform`）。
- **跨平台合并覆盖率靠 `relative_files = true`**（各作业都从仓库根跑，combine 把分隔符归一；少了它"合并成功但一个文件都对不上"；守卫造一份反斜杠数据真跑 combine）。数据必须**按平台分开**合并与判门槛。
- **产物清单 `--manifest` 是"少一片"的唯一判据**：少一片时合并照常成功，报告只是安静少一部分（环境、通过率、格式自检、原生质量门全看不出来）。清单记逐分片文件数与结果条数、合并合计、`--expect-shards` 声明缺了谁；随 allure-resources-\* 上传，由 `verify_allure_report.py --manifest` 与最终条数对齐。
- **平台专属代码只在别的平台被排除**（`# platform: windows - 原因` + `scripts/coverage_platform.py`）：只可能在自己平台执行的块（注册表探测、fcntl、pmset），按平台判覆盖率时别的平台永远"未覆盖"；整块 no cover 又太粗。做法：conftest 只对 `coverage report/xml` 命令行生效（pytest --cov 那条路不生效——pytest-cov 在导入根 conftest 前就构造好 Coverage 对象），按当前平台派生一份配置（原样带过 `[tool.coverage.*]` 再追加排除别的平台标记行的正则；exclude_also 是替换不是合并）；合并口径用 pyproject 基线（三平台并集），单平台数字用 `coverage_platform.py write --platform <展示名>`。**只排除标记所在行**：多行 if 的 `):` 上标记时块体还算缺口——"别的平台走不到但只是权限差异"的分支应换成可替换谓词 + 三平台都能跑的用例。谓词换不掉的是**导入成败本身**：`platform_scan.WinRegistry._module` 的两支出口（`except ImportError` 块标 `linux macos`、导入成功返回那行标 `windows`）用的是平台标记——导入结果由解释器所在平台决定，塞替身只能"演"给统计看。守卫 `tests/unit/test_coverage_platform.py`。
- **门槛 100%**（`pyproject` fail_under 与汇总脚本 COVERAGE_THRESHOLD 两处同改，守卫 `test_coverage_fail_under_matches_pyproject`）。前提是每处够不着的行都有**带原因的豁免**（写法与理由由守卫管）。三平台各自到 100%：能用替身谓词复现的分支换谓词 + 三平台都能跑的用例；导入成败取决于平台的行用平台标记在别的平台排除（2026-10-10 起不再往 `sys.modules` 塞替身模块、也不再换掉模块看到的 `importlib` 来"跑到"导入分支——那种用例量到的是替身，不是平台行为）。
- **门槛在合并后判但不落在合并作业成败上**：分片作业 `--cov-report= --cov-fail-under=0`；合并作业那步只印日志（continue-on-error）——让它非 0 退出会把"缺产物"与"覆盖率掉"混成同一种红。判定交给报告的 Coverage report 条目（低于门槛状态即 failed）与原生质量门规则集一。守卫 `test_the_report_job_only_judges_missing_pieces`。
- **缺片的覆盖率一概不判门槛**：`collect_coverage_data.py --expect-shards` 逐片对数，缺片非 0 退出、后面全部跳过、末尾让作业红（曾经的坑：coverage xml 自己会合并剩下的数据并执行 fail_under，把"缺数据"说成"覆盖率掉了"；xml 那步带 `--fail-under=0`）。守卫 `test_ci_judges_the_coverage_only_on_complete_shard_data`。
- **产物名不变**（allure-resources-<镜像名> / coverage-<镜像名> / allure-report-<镜像名>——名字带的是哪个平台的产物不是生成机器；改名历史曲线清零）。汇总作业 needs 必须含 pytest-report（否则在产物上传完之前开始下载）。
- **三平台片数一致（各 3 片）**：总时长 ≈ 片数×固定开销 + 串行时间；片数只按这个算——2026-10-10 起不再看计价倍率（仓库公开后 Actions 免费）。片号 0 连续编到片数-1；片数出现在四处（矩阵、--shard-count、--shard-index、--expect-shards），不一致会**静默少跑**（守卫逐平台校验）。
- **共享一份 UI 会话的用例整组同片**（`tests/conftest.py` 的 `_SHARED_UI_SESSIONS` + `sharding.shard_plan(groups=...)`）：它们靠 `tests/ui_sharing.py` 的池复用同一扇窗口与同一个后端，拆到两片后每片各自重建一份，复用直接作废。守卫 `tests/unit/test_sharding.py::test_shard_plan_keeps_a_shared_ui_session_in_one_piece`（还有一条反过来要求"真在用共享会话的模块都登记了"）。这份登记有三个消费者：分片装箱（`groups`）、"用完即关"（`SharedUiRegistry.declare_users`，池在最后一个用例结束时就地关闭）、片内聚段（`sharding.cluster_groups` 在收集期把同池模块连成一段执行，池窗不再隔着无关模块闲置挂着，见 [testing.md](testing.md) 第 2 节）。
- **依赖缓存只让片 0 写**（save-cache: `matrix.shard == 0`）：同平台各片 cache key 相同，并行保存只有第一个抢得到，其余打 Failed to save 噪音。
- **Linux 的 Xvfb/字体安装步带"已装即跳过"快路径**：自建持久机上包一轮装过就一直在，而步内的 `apt-get update` 是"包已装齐也每轮必付"的网络敞口（自建机访问发行版源时快时慢，慢的那几轮 setup 段能拖十几分钟，2026-10-24 一轮同机三片排队缺口 764s/353s）；已装即跳过，托管 runner 每次全新、走原冷路径不受影响。视觉回归作业里的同款快路径**只跳过 apt**：末尾拷 `_tkinter` 那段依赖 `$RUNNER_TEMP`（每轮新目录），必须每次都跑。
- **自建 runner 的另两处敞口在机器侧收**（仓库不动）：① Linux/Windows runner 在 `<runner根>/.env` 配 `UV_DEFAULT_INDEX` 指可用镜像，可把偶发的依赖下载慢压掉（uv 按锁文件哈希校验，换镜像不影响完整性；自建机上 uv 本地缓存跨轮留存，冷的只剩"新增依赖"那几次下载）；② Windows runner 把 `_work` 加进 Defender 排除（GitHub 会把 TEMP/TMP 指到 `_work\_temp`，pytest 的 tmp 与 setup-uv 的缓存目录 `setup-uv-cache` 都在那），`python.exe`/`uv.exe` 再加进程排除 —— 三片同机并发时实时扫描会把 busy 抬高 3~4×。

## 3. 本地生成与查看报告

前置：Allure 3 CLI，与 CI 同一条安装命令（`npm install --global allure@3`——3.13~3.17 的质量门会静默放行，见第 2 节）。`allure-results/`、`allure-report*/`、`.allure/` 都在 .gitignore。

```shell
# 1. 跑一遍带 Allure 结果目录（必须带，否则截图与其余附件不落盘）
uv run pytest tests/unit tests/integration --alluredir=allure-results
# 2. 生成报告（在仓库根执行, allurerc.mjs 才会被读到）
uv run python scripts/create_allure_summary.py --allure-results allure-results
npx allure@3 generate allure-results --config allurerc.mjs --clean --output allure-report
# 3. 自检
uv run python scripts/verify_allure_report.py allure-report
# 4. 起服务看（file:// 会被跨域拦掉）
npx allure@3 open allure-report --port 8080
```

只想看某一类用例时把第 1 步换成对应目录（性能/安全必须显式指定）：

```shell
uv run pytest tests/performance -m performance --alluredir=allure-results
uv run pytest tests/security -m security --alluredir=allure-results
```

本地复现 CI 分片流程（同一套参数与合并脚本）：

```shell
uv run pytest --shard-count 2 --shard-index 0 --alluredir=allure-results-shard0
uv run pytest --shard-count 2 --shard-index 1 --alluredir=allure-results-shard1
uv run python scripts/merge_allure_results.py allure-results-shard0 allure-results-shard1 -o allure-results
```

连产物清单一起核对（目录名要以片号结尾）：

```shell
uv run pytest --shard-count 2 --shard-index 0 --alluredir=allure-results-shard0 --manifest shard0.json
uv run pytest --shard-count 2 --shard-index 1 --alluredir=allure-results-shard1 --manifest shard1.json
uv run python scripts/verify_allure_report.py allure-report --manifest shard1.json
```

刷新分片权重实测（界面或用例集改过后值得重跑）：

```shell
uv run python scripts/run_tests_local.py -- --record-durations=tests/durations.json
```

三条要点：① 在**完整套件**上记；② 两片不能写同一份文件（脚本各记一份、按用例取中位数合并）；③ 档案只影响装箱权重不参与断言，写坏会让 sharding.py 导入期报错（守卫 test_durations.py）。

分片不改收集结果：三片并集与全量逐条一致，合并后总覆盖率与串行一致。也可用下载的 CI 结果（allure-resources-\* 里的 allure-results/）本地复现报告，命令相同；generate 后先自检再打开。

四个坑：

- **报告要经 HTTP 提供**（file:// 被跨域拦掉，用 `allure open` 或任意静态服务器）。
- **`allure open` 的目录是必填参数**（`allure open allure-report --port 8080`）。
- **报告目录已存在时 generate 不刷新数据还把新报告藏进子目录**（上游 issue #691）：症状是 `allure-report/awesome/` 出现、顶层 index.html 还是旧的（"历史不可见"多半是它）。应急看 awesome/ 子目录，干净做法 `rm -rf allure-report` 重新生成。CI 不受影响（全新 runner + .gitignore）；自检把嵌套目录判红（nested_report_problems，守卫 `test_a_nested_report_directory_is_reported`）。
- **打开前先自检**：缺 data/test-results/\*.json 时界面照样显示通过/失败，点开用例是空的。
