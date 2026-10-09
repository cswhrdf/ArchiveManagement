# 测试体系

本文档说明测试如何分类、在哪里执行、结果如何汇总，以及新增测试要遵守的约定，目标是让"本地提交要快"与"CI 有完整证据"同时成立。各小节只保留仍然有效的规则、量法与坑；每次踩坑的完整复盘（日期、现场、修复过程）以测试文件的 docstring 与提交历史为准。

其他文档：[功能说明](features.md)、[游戏库与发现](library.md)、[全局快捷键](hotkeys.md)、[平台支持](platforms.md)、[开发与发布](development.md)。

## 1. 目录与边界

```text
tests/
  conftest.py            # 严重等级 + 四层 Allure 标签 + 审计日志夹具
  helpers.py             # 跨模块共享的测试数据构造器与环境信息
  reporting.py           # 性能基准与安全结论的记录器
  button_support.py      # GUI 按钮测试共享基建(拆自 test_gui_buttons.py, 见 test-refactor-plan.md)
  report_support.py      # 报告/CI 断言共享基建(拆自 test_report_verification.py)
  sql_support.py         # SQLite 后端用例共享基建(拆自 test_sql_backend.py)
  ui_sharing.py          # 共享 UI 会话(见第 2 节)
  sharding.py            # 分片规则: 三级权重(实测耗时优先) + 最慢优先贪心装箱
  durations.py           # 分片权重实测档案的记录/加载/合并(守卫 tests/unit/test_durations.py)
  durations.json         # 档案本体(nodeid → 秒), 由第 7 节的命令刷新
  unit/                  # 纯逻辑与单一边界: 不会真正触达用户数据的临时目录/SQLite
  unit/conftest.py       # 单元层共享 fixture(逐字相同才上提, 见 test-refactor-plan.md)
  integration/           # 真实跨层协作: 配置 + 数据库 + 备份/恢复 + 审计日志 + UI 后端
  performance/           # 规模基准: 数据量与阈值必须显式声明（只在 CI 执行）
  security/              # 不可信输入与危险操作防护（只在 CI 执行）
```

| 类别        | 标记                       | 本地 `pytest` | pre-commit               | CI                                          |
| ----------- | -------------------------- | ------------- | ------------------------ | -------------------------------------------- |
| unit        | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（Windows/macOS/Linux）                  |
| integration | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（Windows/macOS/Linux）                  |
| performance | `@pytest.mark.performance` | **不运行**    | 不运行                   | `quality` job 的一部分（ubuntu，单平台采集） |
| security    | `@pytest.mark.security`    | **不运行**    | 不运行                   | `security` job（Windows/macOS/Linux）        |

> **macOS 自 2026-09-30 起重新纳入 CI**（开发阶段曾因 macOS runner 按 Linux 的 10 倍计价屏蔽过一阵）。
> 平台集合在四处保持一致：CI 三个矩阵（`pytest` / `pytest-report` / `security`）、`allurerc.mjs` 的
> `environmentsTested`、汇总作业的 `--expect-platforms`，由守卫核对；macOS 只给 **1 片**（3 个实例），
> 墙钟与额度的取舍见 `.github/workflows/ci.yml` 矩阵处的注释。

### 严重等级（失败影响面）

每个模块在 `pytestmark` 里声明一个等级，`tests/conftest.py` 把它写成 Allure 的 `severity`。**等级表达"失败的影响面"，与测试层次无关**（层次用 `layer`），并且决定 pre-commit 跑哪些用例：

| 等级       | 判定标准                                           | 例子                                                                    | 本地 pre-commit |
| ---------- | -------------------------------------------------- | ------------------------------------------------------------------------ | --------------- |
| `blocker`  | 安全与数据完整性底线, 以及会让软件崩溃/卡死的缺陷  | 路径越界 / 危险目标必须被拒、篡改快照或清单必须拒绝恢复、界面事件递归崩溃 | 运行            |
| `critical` | 核心业务不可用或结果不正确                         | 备份 / 快照 / 恢复 / 删除计划、仓储事务、迁移、调度、GUI 真实后端、全链路流水线 | 运行            |
| `normal`   | 常规功能与交互                                     | 配置、平台探测、主页聚合、对话框、CLI、热键、审计、存档位置与命名           | 仅 CI           |
| `minor`    | 展示与辅助                                         | 调色板 / 控件样式 / 渲染修正、i18n 文案、打包元数据、演示后端、性能基准     | 仅 CI           |
| `trivial`  | 极低影响                                           | 色值、脚本生成的报告汇总项（覆盖率 / 性能 / 安全摘要）                      | 仅 CI           |

约定：

- 每个模块**恰好声明一个等级**；单个用例可用 `@pytest.mark.blocker` 等覆盖模块默认值。`tests/unit/test_test_config.py` 会校验"恰好一个"，并要求**五个等级都至少有一个模块在用**（否则报告分布退化回一边倒）。
- 漏写时按目录兜底（unit/integration=normal、performance=minor、security=critical），兜底只是为了不冒出 `no_severity` 桶。
- **别按目录或层次照搬等级**：`tests/unit` 里既有 blocker（危险目标判定）也有 minor（调色板色值）。
- 复现本地钩子跑的子集：`uv run pytest --min-severity=critical`。

### 层级（Allure 测试金字塔）

每个模块用 `pytest.mark.layer(...)` 声明测试层次，Allure 的"测试金字塔"与"按层耗时"控件直接读它。**层次描述"用例实际接了什么"，不完全等于所在目录**：

| 层次          | 判定标准                                                                     | 例子                                                             |
| ------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `unit`        | 单个组件 + 替身或内存数据                                                    | 解析器、格式化函数、纯逻辑校验、可注入替身的服务                 |
| `integration` | 真实数据库 / 文件系统 / 领域服务之间的协作（不要求位于 `tests/integration`） | 真 SQLite 的仓储与迁移、真实快照与恢复、跨层备份→恢复→删除流水线 |
| `e2e`         | 从真实入口走完整用户流程                                                     | 真实窗口 + 按钮/菜单操作、命令行入口的完整流程                   |

因此 `tests/unit/` 下的真实 SQLite / 文件系统模块（`test_repository.py`、`test_sql_games.py` 等 `test_sql_*` 系列、`test_restore.py`、`test_backup_service.py`、`test_snapshot.py` 等）声明为 `integration`：它们确实在做跨组件协作。审查时如发现"目录层次"与"实际接的东西"不一致，以实际为准。

默认收集范围由 `pyproject.toml` 的 `testpaths` 决定（只有 `tests/unit` 与 `tests/integration`）。性能与安全测试需要显式指定：

```shell
uv run pytest tests/performance -m performance
uv run pytest tests/security -m security
```

## 2. 共享测试设施

- `tests/helpers.py`：临时 SQLite（`migrated_database`）、游戏与存档位置（`add_game` / `add_location` / `make_save_folder` / `touch_save`）、备份服务（`backup_service` / `sql_archive_service` / `manual_scheduler`）、固定 UTC 时刻（`utc_moment`）、批量造数据（`seed_home_games` / `seed_backup_chain` / `write_text_files`）与运行环境信息（`environment_info`）。`pyproject.toml` 把 `tests` 加进了 `pythonpath` 使其可导入。
- `tests/reporting.py`：`PerformanceRecorder`（耗时/吞吐/内存峰值 + 阈值校验）与 `SecurityRecorder`（记录"场景/期望拦截/实际情况"，未拦截即失败）。
- 存量模块里的小构造器（如 `_service`、`_database`）保留原签名，只把实现委托到共享模块，避免几十处调用点跟着改。
- `tests/ui_sharing.py`：**共享 UI 会话**（session 级 `ui_shared` 夹具）。同一构造配置的界面用例共用一份窗口——每条用例"建窗 + 首帧 + 销毁"合计 1.6~1.9s，几百条 UI 用例的纯生命周期开销以分钟计。用例边界由 `test_scope` 做"进入前对齐基线 / 退出后识别漂移并还原"。三条纪律：
  1. 还原走被测应用自己的动作（`_show_page` / `_switch_view` / `_select_game` / `_on_toggle_theme` / `_set_busy`），不改写内部字段；还原后重新识别验证，对不上基线就**重建整窗**并把原因记进 Allure。
  2. 后端数据被用例改过（删游戏/切主题/改调度）直接重建——共享的是窗口，不是数据。
  3. 会话末尾统一走 `gui_support.close_apps` 收尾（拆不干净与逐用例收尾一样判红）。

  池按工厂函数身份区分：`demo_app` 是全局标准演示池（states / styles / keyboard / tooltip 入池）；构造参数或污染面不同的模块建自己的池（copy_quality 的长文案池、dropdown 的独立池——它会改 `_page_size_box` 的 values/command）。**不**入池的模块维持"每用例一窗"：每条用例要不同构造参数的（sizes / layout / lifecycle / roots / smoke）与重度改后端数据的大户（home / 导入导出）——它们复用不到，硬塞只会换来每次重建。一组共用状态的用例可用 `group_scope` + `pool.rebaseline()` 做统一前后置。守卫：`tests/unit/test_ui_sharing.py`（替身钉逻辑分支）与 `tests/integration/test_gui_shared_session.py`（真窗口上的复用/隔离/共态组/数据漂移重建）。

  两个全局影响：**共享窗会长期坐在 `tkinter._default_root` 槽位上**（收尾兜底不收它，`gui_support._SHARED_ROOTS` 就是为此设的），整批连跑时"新建窗口接管槽位 / 外来根坐上槽位被兜底收走"这些判据会跟着全局状态跑偏——`test_gui_roots.py` 里两条盯槽位纪律的用例开头先 `monkeypatch.setattr(tkinter, "_default_root", None)` 摘空槽位（结束自动还原），判的才是槽位这件事本身。执行中看到两三扇窗口同时开着、某条用例跑完窗口不关，是共享池的预期形态：窗活到会话末尾由 `ui_shared` 统一收。

约定：测试优先复用**真实**领域服务、临时目录与 SQLite；只有外部依赖（系统回收站、系统快捷键、真实网络）才用可注入替身。不为了测试在生产代码里加分支，也不写"只验证 mock 调用次数"的用例。

## 3. 性能基准

每个基准用 `perf_recorder` 显式声明规模、指标与阈值，超出阈值即失败：

```python
with perf_recorder.duration("home.load_home", scale=SCALE, budget_seconds=15.0):
    board = home_window.load_home()
```

当前基准（规模见各用例常量）：

| 基准 | 数据规模 | 指标 | 当前阈值 |
| --- | --- | --- | --- |
| 打开含 500 个游戏的主页 | 500 个游戏、约 2000 条存档 | wall time（冷缓存） | ≤ 15s |
| 深备份链恢复定位 | 400 层链、每层 20 文件 | wall time | ≤ 20s |
| 目录扫描（大量小文件） | 8000 个小文件 | wall time | ≤ 60s |
| SQLite 存储查询 | 10000 条历史 | wall time | ≤ 2s |
| 初始化（空库） | 无数据 | wall time | ≤ 3s |
| UI 关键路径 | 标准演示数据 | wall time | ≤ 10s |

阈值刻意留出宽裕余量（CI 机器与本地差异大），只拦"数量级"级别的回归（N+1 取数、非流式复制、O(n²) 的树/排序）。结果写入 `performance-results.json`（含环境信息与每条测量）与 `performance-results.csv`，由 `scripts/create_allure_summary.py` 转成 Allure 中可检索的测试项。

## 4. 安全测试

| 覆盖 | 场景 | 判据 |
| --- | --- | --- |
| 路径安全 | `../` 穿越、绝对路径注入、UNC 路径、符号链接逃逸 | 拒绝并给出用户能看懂的提示，不创建任何文件 |
| 输入校验 | 超长字符串、二进制注入、特殊字符（`:` `\` `/`）、编码混淆（NFC/NFD） | 拒绝或转义，不崩溃、不落盘 |
| 危险操作确认 | 删除原始存档位置、清空历史 | 必须经过确认流程，取消后零副作用 |
| 备份完整性 | 篡改清单、篡改快照、清单与实际不一致 | 恢复操作必须失败并报告差异，不产生半成品 |
| 并发安全 | 备份期间修改存档、同一位置的并发备份 | 通过锁或排队保证一致性，不死锁 |

约定：不使用真实凭据或真实用户文件；涉及"用户主目录 / 盘符根目录"的用例只做**判定**，不执行写入或删除。

**副作用与网络的政策**：

- 除"删除原始存档位置"（用户显式触发、输入游戏名确认、只删空且走系统回收站）外，软件**不得对应用自身目录之外的任何区域做写/执行/删除**，只读可以。
- 网络请求**只允许取回内容，禁止外发**：只发 GET/HEAD、无请求体、无凭据头、目标主机必须在允许清单内（公开 Steam 图片 CDN 与公开商店接口）。
- 两条政策由 `test_side_effects.py` / `test_outbound_requests.py` 用"动态记账 + 静态扫描"两层守住：动态证明真实流水线合规，静态防将来新增旁路（直接 `subprocess.run`、换 HTTP 客户端）。

结果写入 `security-results.json`（场景/输入摘要/期望/实际/是否拦截），同样汇总进 Allure；未拦截即用例失败。

## 5. 结果与报告元数据

- 每条性能测量记录：规模、指标、取值、单位、阈值、比较方向、是否通过；每条安全结论记录：类别、场景、输入摘要、期望拦截行为、实际结果、是否拦截。
- 环境信息由 `scripts/create_allure_summary.py` 写入 `allure-results/environment.properties`（操作系统与平台族、Python 版本与实现、提交 SHA、分支与 CI run id、测试类别是否执行、覆盖率门槛）；覆盖率摘要由 `scripts/create_allure_coverage.py` 生成。原始 JSON/CSV 与 `coverage.xml` 都作为附件带进报告（脚本按 `RAW_REPORT_FILES` 收集，存在哪个带哪个），保证结论可下载、可追溯；HTML 报告是整站，作为 `coverage-<os>` artifact 上传。
- **汇总结论项的状态由数据决定（报告不许骗人）**：
  - 性能 `Performance baseline` —— 只要有一条测量 `passed` 为假就写 `failed`（描述给出基准名/取值/阈值），结果文件缺失则写 `broken` 并说明原因。
  - 覆盖率**不另出结论项**：每个平台的 `Coverage report` 项自己就是结论——描述第一句固定是"当前覆盖率 X% 大于/小于预期覆盖率 Y%, 验证通过/未通过"（X 按 coverage.py 的 TOTAL 口径 `(行覆盖 + 分支覆盖) / (行总数 + 分支总数)`，Y 读 `pyproject.toml` 的 `[tool.coverage.report] fail_under`），低于门槛该条直接 `failed`，读不到 XML/数字是 `broken`。缺平台（报告作业挂了、产物没合并进来）由运行总账的覆盖率一节指出，不会因为"没有这一条"而静静变绿。
- **运行总账的末节是「证据核对（应有 vs 实有）」**：应有的证据清单由 `scripts/allure_catalog.py` 一族族展开（平台矩阵 × 类别 × 各汇总项），逐项标"已收到 / 缺失"；每个缺失项另写一条 `broken` 结论并打 `::warning::`（门禁跟着红）；「判定」列说明每一项由谁负责。开头一行数"应有 N 项，收到 M 项"；末尾两张小表（结论清单 / 结论项声明的附件文件），粒度不同不合成一张表。为什么需要：Allure 只判"结果文件里有的东西"，合并环节吞掉半个平台时报告照样绿——总账把"应该有什么"摊开在页面上，缺一眼可见。
- 性能 / 覆盖率 / 安全三节每平台一行，结论都从原始数据算出（脚本读文件而不是猜 CI 环境变量）。
- **「有意不统计的覆盖」是报告首页的全局附件**（`allure-coverage-exclusions.md`）：来自 pragma 标记（`# pragma: no cover` 等）与 `pyproject.toml` 的 `exclude_also`，按文件列出"路径:行号—标记—原因"与总条数，未写原因的标记单独指出；解析规则与 `tests/unit/test_coverage_pragmas.py` 共用同一实现（守卫同步，防止两头漂移）。
- **平台以 Allure 的"环境"维度呈现**（2026-10-05 起）：每条结果带 `env` 标签（平台展示名），`allurerc.mjs` 的 matcher 把标签映射成 Allure environment；报告里出现 Windows / macOS / Linux 三个环境，"按环境过滤/对比"直接可用。两个配套兜底：平台作为 `--platform` 参数进入结果身份——三平台 `retryHash` 各不相同（一个用例不会在跨平台合并后变成"重试"）、`historyId` 又共享（历史趋势能连上）；旧的 os 标签 + parentSuite 后缀保留（旧报告兼容 + 兜底）。**生成报告必须在仓库根执行**——Allure CLI 要读到 `allurerc.mjs` 才会识别环境，读不到会**静默**退回 default 环境，`scripts/verify_allure_report.py` 会判"报告不完整"。摘要类结论项同规则：带 env、标题不拼平台名（环境维度已经表达）。
- 用例标题里的参数化 id 会被 pytest 转成 ASCII 转义（`\u7528\u6237`），生成报告时还原（界面与 URL 里都是可读中文）。
- **`allurerc.mjs` 的三项读者辅助**：
  1. **categories 失败归类**——三条规则（id 与 `allurerc.mjs` / `tests/unit/test_test_config.py` 三处同步，改名要一起改）：
     - `env-tk-library`：环境:Tk/Tcl 库不可用（症状清单与 `tk_guard.KNOWN_TK_SKIP_MARKERS` 同源、大小写不敏感、限定 layer 为 integration/e2e）；
     - `env-transient-database`：环境:数据库瞬时读失败（`sqlite3.OperationalError` 且数据库被锁/暂时不可读）；
     - `gate-quality-check`：工程门禁:质量检查未通过（按 `testCategory=quality` 标签挑选）；
     未命中的落进默认的 Product/Test errors 桶。
  2. **variables 报告顶部稳定事实**（覆盖率门槛、分层执行位置等；平台各自的事实按环境变量写）。每次运行会变的（提交号、分支、耗时）由 `environment.properties` 与总账负责，两边不重复不冲突。
  3. **allowedEnvironments 环境 id 白名单**——少列一个 id 时 `allure generate` 直接 Internal Error 硬失败（这也是一种守卫）。
  三项各有守卫（`tests/unit/test_test_config.py`、`tests/unit/test_report_quality_items.py`）。

## 6. CI 流程

```text
            ┌── quality (ubuntu): 静态检查 + 性能基准 + 覆盖率门槛(仅 push) + Allure 报告与总账
push/PR ────┼── pytest (win/mac/linux) × 分片: 单元+集成(按层跳过, PR/push 只跑选中层)
            ├── security (win/mac/linux): 安全测试
            └── required-check: 分层跳过的兜底
                 │
nightly ───── ci.yml@dev(由 nightly.yml 的 gate 判 dev tip < 24h 才调用, 带 ref: dev)
                 │
                 ▼
            pytest-report (ubuntu × 3 平台): 合并各片结果 + 生成 Allure 报告 + 自检
                 │
                 ▼
            allure-summary: 汇总三平台 + 覆盖率合并 + 原生质量门 + 总账 + 报告 artifact
```

- **作业数量也是额度**：PR 与 nightly 一轮 16 个实例（3 平台 ×（3 pytest 片 + security）+ 2 报告 + quality + changes + required-check）。GitHub 按并发作业计价，多开一个分片 = 少一份留给别的 CI 的额度。
- **层跳过（分桶在 changes 作业）**：`changes` 作业对 PR 的改动文件分类，输出 ui / backend 两层——ui 层 = `src/**/ui/**` + `tests/integration/test_gui_*.py` + 视觉基线 + 共享基建（`tests/gui_support.py` / `tests/button_support.py` / `tests/ui_sharing.py`，它们同时命中两层，ui 与 backend 是**或**的关系）；backend 层兜底 `src/**`（除 ui）+ `tests/**`（除 GUI 集成）。push 的四段 pytest 步骤按层放行；security 归 backend（安全测试不看 UI）；quality 不分层（静态检查整个仓库）。纯文档 push 跳过 pytest 作业，`required-check`（总是成功）兜住分支保护规则的必需检查。守卫：`tests/unit/test_ci_workflow.py`。
- **`pytest-report` 与 `allure-summary`（覆盖率门槛 100% 与完整报告）在 PR 与 nightly 都跑**：push 只跑选中的层，切片上的覆盖率天生偏低，在那里判门槛等于自造假红——所以"最完整的证据"在 PR 报告作业里出，与 push 的层选择互不干扰。nightly 由 `nightly.yml` 的 gate（dev tip < 24h 才调用）带 `ref: dev` 调用 `ci.yml`；PR 与 nightly 两层全跑。守卫：`test_pr_and_nightly_run_full_while_push_selects_layers`。
- **省额度的三条**：① concurrency 取消被取代的运行（报告作业的条件必须是 `always() && !cancelled()`，否则前面被取消时报告不生成、上一轮的报告继续留在页面上冒充最新）；② 纯文档 push 用 `paths-ignore`、PR 用 `changes` 作业判路径 + `required-check` 兜底；③ 与平台无关的检查合进 quality、平台专属检查塞进 pytest 片 0（不新开作业）。完整的优化清单与取舍落在 `tests/unit/test_ci_workflow.py`。
- 每个 pytest 作业上传自己的 `allure-results-<platform>-shard<N>`；汇总用 `download-artifact` 的 pattern + `merge-multiple: true` 一次拉齐，`pytest-report` 沿用 `.allure/history.jsonl` 累积历史。
- **历史趋势靠 artifact 活着**（与报告怎么发布无关）：两个报告作业各自走"找上一次**带着同名产物**的运行（不问成败）→ 取回 `.allure/history.jsonl` → `allure generate` 读它并追加本次一行 → 新的 history.jsonl 传回 artifact"（`pytest-report` 用 `allure-resources-<平台>`，`allure-summary` 用 `allure-resources-final`）。要点：① 趋势寿命 = artifact 寿命（默认 90 天），窗口内没有一轮留着它就从零开始（不报错，曲线断了）；② 基线**不要求**那轮 success——失败轮的历史本该出现在趋势里，Allure 的趋势正是用来看失败的（只认成功运行时，连续失败的时期接不上任何一轮基线）；那一轮是否真的传了产物由 finder 的 artifact 检查把关；③ 报告只作为 artifact 存在（2026-10-06 起不再发布到 GitHub Pages），站点是否可达与历史链条无关；④ 恢复要认**两种 artifact 布局**——单路径上传时 `history.jsonl` 落在压缩包根部、多路径时带 `.allure/` 前缀，两种都探测、都找不到时响亮退出；⑤ **质量门不许写历史**——`allure quality-gate` 子命令同样遵循 `appendHistory`，会在半套结果的时点追加一条假快照，跑之前把真历史改名为同目录临时文件（改名即回、不跨设备）、跑完放回。守卫：`tests/unit/test_ci_history.py`。
- **历史文件在生成报告之前自动修一遍**（`scripts/repair_allure_history.py`，排在拷回历史之后、generate 之前）：报告趋势按 `retryHash` **精确匹配**，身份键里每多一个维度（如 `environmentHash`），旧快照的键就整体对不上（丢的是匹配不是数据）。三步：① 同一轮被追加两次的快照去重（按"时间窗 + 键集合重合度"判，留信息更全的那份）；② 旧形状的键按**唯一前缀**补成当前形状（前缀对上多个当前键时按条目自己的 `environment` 对准平台、显示名与 id 统一小写比较；段数比当前还多的键直接丢弃——砍前缀会悄悄并掉平台区别）；③ 补不出唯一值的条目丢掉，一份都连不上时清空重来。动了文件就写 `allure-history-repair.md` 挂进首页全局附件（记实际用的 CLI 版本与最新快照年龄），没修东西就删掉那份记录。守卫：`tests/unit/test_ci_history.py`（三种走向、幂等、"没修不留附件"）。已知局限：键形状刚变的第一轮，基线里还没有新形状的快照，这一轮会误判（下一轮起恢复）——这正是历史基线必须每轮都被消费的原因。
- **每个作业只装自己需要的依赖**：开发依赖拆成 test / coverage / quality / analysis / package 五组，作业按命令装对应组（`uv sync --locked --no-default-groups --group <组>`）。两个坑：顶层必须 `UV_NO_SYNC=1`（否则每个 `uv run` 按默认组先 sync 一遍，把省掉的包全装回来）；同一作业里每次 `uv sync` 必须带**同一组**（Tkinter 修复那步也 sync）。本地开发不受影响（`default-groups` 覆盖全部）。守卫：`test_ci_installs_only_the_dependency_groups_each_job_needs`。
- **每个作业跳过安装项目本身**（`--no-install-project`）：CI 用例靠 pytest 的 `pythonpath` 配置导入源码、静态检查靠 `mypy_path`，不需要可编辑安装。唯一例外 quality：装一次并跑 CLI 冒烟（cwd 换到工作区外跑 `python -m archive_management init`，验证打包入口）。守卫：`test_ci_only_installs_the_project_where_the_cli_smoke_needs_it`。
- **质量门禁由脚本执行**（2026-10-02 起）：`scripts/create_allure_quality.py --group <组>` 把每项检查的退出码、结论与完整输出附件写成 Allure 结果（任一项不过则脚本非 0 退出）。三组：`core`（ruff check / ruff format --check / 宿主 mypy → env=common）、`analysis`（deptry / bandit / pip-audit / radon / xenon → env=common）、`platform`（mypy --platform win32 / darwin）。前两组与性能基准同在 Ubuntu 的 quality 作业；platform 组各自在该平台的 pytest 片 0 执行（排在测试与上传之后）——`--platform` 只决定"检查哪支代码"，结论挂到没跑过的平台环境就是假归属。结论归属：与平台无关的检查带 `env=common` 归入显式声明的 Common 环境（不冒充任何平台）；平台专属 mypy 带平台 env 且真的在那台机器跑（`Check.host_platform`）。点名跑不了的分组时以退出码 2 报错（不是 0——静默跳过等于门禁不存在）。
- **Allure 原生质量门**：`allure quality-gate --config allurerc.mjs allure-results`，退出码直接决定作业成败，输出落 `allure-quality-gate.txt` 并进总账。两条规则集：① 不过滤（maxFailures: 0 / successRate: 0.98），脚本生成的结论项也算——覆盖率项 broken 时必须有人管；② 只看真实用例（filter 选 `framework=pytest` 再 environmentsTested），要求每个平台都有用例。**用环境维度而不是 `minTestsCount`**：绝对计数随用例规模变松（缺整平台仍可能高于计数，静默失效）；判据用 `framework=pytest` 正向标记——正向漏判会红、反向排除漏判会绿。**清单必须写环境 id 不能写显示名**：`environmentsTested` 拿结果上的 environment（id：windows/macos/linux）比清单，写显示名则所有平台全报缺。守卫：`test_quality_gate_asks_every_platform_for_real_tests`。
- 原生质量门排在写运行总账**之前**（总账读它落盘的输出），守卫：`test_native_quality_gate_is_configured_and_pinned`。它管不到"少一片"，那由产物清单负责（`--manifest`）。**CLI 必须 ≥ 3.18.0**：3.13~3.17 配 historyPath 时静默放行（上游 issue #895，修于 3.18.0）；CI 用浮动标签 `allure@3`，下限在 "Check Allure version" 步骤运行期核对。本地复现：`npx allure@3 quality-gate --config allurerc.mjs allure-results`（单平台跑会因 environmentsTested 失败，属预期）。

### 跳过只留给已知的环境问题

GUI 用例把建窗期的 TclError 写成 `pytest.skip("tk 环境不可用: ...")` 的代价是：真正的 Tcl 故障会伪装成一堆 skip（曾让两个平台都静默跳过同一条用例，而跳过不会让任何东西变红）。规矩（`tests/tk_guard.py`）：

- 只有已知环境症状才允许跳过：解释器加载不了 Tcl/Tk 库数据（`tcl_findLibrary` / `init.tcl` / `tk.tcl` / `auto.tcl` / `Can't find a usable …`）或没有显示环境（`no display name` 等）。前一组只可能由 Tcl 自己在装库时说出。
- **白名单只列 Tcl 自己的库数据文件名**：把 `couldn't read file` 这类通用说法整个收进来，会把"应用自己要读的文件打不开"也归成环境问题。两种拼法都能被文件名命中，不必放宽。
- 前缀是"tk 环境不可用"但症状不在清单里时，conftest 的报告钩子把它改成**失败**——要么修掉要么删掉，不留下永远不跑的用例。
- `gui_support.gui_app` 同理：非已知抖动的 TclError 不重试直接抛。

与 Tk 无关的跳过不受影响。守卫：`tests/unit/test_gui_retry.py`（含反向守卫：应用侧读文件失败不许被当成环境问题）。

### 界面用例收尾：根要拆干净，后台线程也要放掉

两条不变式，缺一条都会让**后面的**用例遭殃：

- **不得留下 Tk 根**：`_default_root` 上挂着不能用 / 没拆干净的解释器时，后面每条用例贴图都报"图片不存在"。收尾做验证并补救，断裂按失败报。
- **不得攒下后台调度线程**：`BackgroundScheduler()` 构造即 `start()`，释放入口必须是 `destroy()` 也会走的幂等 `ArchiveApp._release_background()`。守卫：`test_gui_roots.py::test_a_real_backend_does_not_outlive_its_window`（故意绕开收尾、只走产品关窗路径）+ 收尾里的**相对基线**判据——同进程里非 GUI 用例也会建真后端，绝对判据会把别人的存量算到当前用例头上。

### 只在某个平台红的分片失败：先把那份平台差异搬回本地

通用做法：遇到"只有某个平台红"，先定位它依赖哪种形态差异（路径分隔符与大小写、`normpath` 是否幂等、`dir_fd` 相对路径、显示服务、字体度量），再把那个差异做成替身搬进用例——否则这条用例永远只在 CI 的一半平台上有效。两个实例：

- **判重只规范化了一侧**（演示数据存 Windows 风格展示串、输入侧过 `normalize_path`，POSIX 上形态不同永远比不出重复）→ 判重两侧都规范化；守卫把 `normalize_path` 换成 POSIX 风格替身，Windows 上就能复现（`test_ui_demo.py::test_demo_duplicate_locations_are_caught_for_unusual_path_forms`）。
- **裸 Ubuntu 没有中日韩字体**，Tk 把汉字量成近零宽 → CI 装 `fonts-noto-cjk`；用例侧三条纪律：**前提只用拉丁字符表达**（缺字体的机器也量得出宽度）、**前提写成强制断言不写 if**（前提不成立时一条断言都不执行，用例永远绿）、**期望值不能与被测值同源**（面板没被摆放时标签按内容自适应，`measure(shown) <= width` 恒成立；夹具要给容器被框定的宽度：`CTkFrame(width=…)` + `grid_propagate(False)` + `place()`，CTk 的 place 不接受 width/height）。

### 同一路径只有一条位置：判重靠"落库即规范化"

`SaveLocationRepository.duplicate_of` 比的是字符串相等（`LOWER(path) = LOWER(?)`），"同一个文件夹"的前提是两侧都已是规范化形式——这条不变式由写入侧维持：`add_location` / `update_location` / `confirm_candidate` 都先过 `normalize_path`。破坏时失败是**静默**的（`…/saves`、`…/saves/`、`…/saves/.` 被当成三个路径：备份拍两份、恢复写两次）。守卫：`test_sql_locations.py::test_every_stored_save_location_path_is_normalized`、`test_the_same_folder_in_another_spelling_is_rejected`、`test_save_candidates.py::test_confirming_a_candidate_written_differently_keeps_one_location`。

### GUI 用例必须真的跑起来（skip 是有代价的）

"Tk 起不来"被当环境问题处理的代价：真 Tcl 故障伪装成一片 skip，GUI 用例占覆盖率很大一块（实测模拟坏 Tcl：全量 769 passed / 68 skipped，覆盖率 67.88% < 门槛，且失败信息看不出原因）。所以 CI 在 Windows 与 macOS、pytest 之前显式自检：

```shell
uv run python -c "import tkinter; root = tkinter.Tk(); root.destroy(); print('Tkinter OK')"
```

坏掉时非 0 退出直接把原因抬到表面（Linux 的显示环境由 `xvfb-run` 提供，不重复检查）。真发生过的一次：uv 托管的 standalone 解释器靠自身目录的 tcl 数据文件定位 Tcl，副本陈旧/不完整就报 `Can't find a usable init.tcl`（astral-sh/uv#7036）。**不必每次重装**（一次 30MB × 9 个作业）：先普通安装 → 自检（continue-on-error）→ 失败才 `uv python install --reinstall 3.12` + `uv sync --locked` + 再复检一次（这步没有 continue-on-error，修不好照样红）。macOS 片自 2026-10-05 换 `setup-python` 的 python.org 构建 + `UV_PYTHON_PREFERENCE=only-system`，见「macOS 原生崩溃」一节。

### GUI 布局用例不要和"首帧时序"赛跑

- `winfo_width()` 首帧往往只有 1，且可能不再有尺寸变化事件补救。判定"布局停在上一次宽度"这类回归**用假控件**（只实现 `_text` 与 `winfo_width()` 的替身）验看门狗本身，真窗口那条只跑真布局。
- 断言"当前宽度下该截成什么"别写死像素/字符数：用同一个 `fit_text(full, font, label.winfo_width())` 生成期望值比**文本**，像素只卡缝隙上限。
- **重裁必须一定会发生且不靠等秒**：容器事件（滚动区 `<Configure>`）走延后 60ms 的**合并**式排队（已排过就不再排、不撤销；触发源可能连续时必须合并不是推后——Xvfb/CTk 的连续尺寸事件会把"取消重排"式 debounce 无限推后）；标签自己的 `<Configure>` 兜底（内宽变了外宽没变时容器不发事件）。契约守卫：`tests/unit/test_ui_scheduling.py`（替身控件驱动真实调度与裁剪，不建窗）。
- **用例不和延迟赛跑**：断言前先等**不变量本身**成立（显示文本 == 该宽度下的 `fit_text` 结果，见 `_wait_until_the_name_fits`，超时才失败），不泵固定时长。

### 列表里的文字"放得下或带省略号"：判据与六个坑

判据两条：① 每个文本控件的每行都放得下（多行按行分别量）；② **夹具文本必须长到"必须裁"且断言真的出现省略号**（演示数据名字都短，量出的"0 处截断"是假的；`test_gui_text_fit.py` 专门配长文本后端）。

六个坑：

- `wraplength` 断不开没有空格的 CJK 文本（Tk 只在空格断行）——要先断行再补省略号得自己算：`fit_text(text, font, width, max_lines=N)`（`ui/textfit.py`）。
- 裁剪宽度取**被拉伸的容器**不取标签自己（`pack(side="left")` / `grid(sticky="w")` 的标签宽度=文字宽度，按它裁会越裁越短）。共享实现 `widgets.track_fit(容器, 标签, …)`：按容器宽裁、宽变重裁、没变不动。期望值用同一个裁剪函数按实测宽算（比文本不比像素），前提用**纯拉丁**长文本。
- 量不到先看那块区域**在不在清单里**（曾漏的是面板从不在 `_areas` 清单、标签也没走 `fit_label`——判据本身没问题）。
- 会自己换行的标签按断行后的宽度判（比 `winfo_reqwidth()`，不比整段文字宽——Tk 已按 wraplength 断行，拿整段比会假红；`_needed_width()`）。
- **CTkLabel 的 wraplength 是逻辑像素、`winfo_width()` 是物理像素**（CTk 会把 wraplength 乘窗口缩放，125% 屏上写 284 内层拿到 355）——写之前过 `widgets.wrap_budget(window, width)`；inset/minimum/initial 这类设计尺寸本来就是逻辑像素不要换。
- 行/卡片会被重渲染销毁（`_render_games` 异步落地、延后 60ms 的 `_schedule_list_sync` 都是先 destroy 再重建），"等一会儿再量"的引用必须当场重新取（`_live_row` 先 `winfo_exists()`），"贴右"这类由延后任务算出的不变量写成"等到成立"。反过来，**要量"刚渲染/刚设的状态"就别在中间泵事件**（悬停判定：`_set_hover(None)` 后立刻读）。"夹具必须长到被裁"也别靠时间：after(60ms) 的重裁在 CI 上可能还没跑，量之前 `_flush_delayed(app)` 直接调一次。

### 窗口尺寸的判据：屏高用替身，量之前先让窗口真的布局出来

给屏高/屏宽装替身（monkeypatch `tkinter.Misc` 的 `winfo_screenheight` / `winfo_screenwidth`），在 768 / 900 / 1080 三档把所有窗口与对话框量一遍（`test_gui_sizes.py`）。判据两层别合并：**硬线**（所有窗口：窗口+标题栏不出屏幕，标题栏按 48px 自折算——`winfo_height()` 是客户区）；**舒适线**（只对模态弹窗：≤ 屏幕可用高度 80%；工作区窗口各有最小尺寸只守硬线）。

坑：

- 模态对话框在"被替换掉的 `wait_window`"里量时窗口还没真正布局（子控件还是初始值；`update_idletasks()` 不够，要完整 `update()`）。"内容真的能滚"用 `canvas.yview()` 可见跨度 <1 判，不拿两个 `winfo_height()` 相减（后者是被污染读数）；配套断言"正文第一行挂在滚动区里"。正文第一行别用"控件树第一个 CTkLabel"（实测是空文本装饰标签且不在滚动区）；认有文本的标签 + 祖先里有带 `_parent_canvas` 的控件（CTkScrollableFrame 的 Tk 侧直接子控件是内部容器，isinstance 找不到，按 `_parent_canvas` 特征、广度优先取最外层）。
- 构造时的屏高替身要给一个**装不下设计尺寸**的值（768），否则"构造里有没有用尺寸策略"验不出来。
- 舒适线的 chrome 高度有平台差异（同一份代码 Linux 比 Windows 高 7px，正文顶格的对话框只在 Linux CI 超线）：`_present()` 里按实测 `winfo_reqheight()` 超线收多少（`_clamp_to_comfort_line`，收到 `_DIALOG_BODY_MIN` 为止，正文区自己滚）。正文区要**递归**找（批导对话框套在卡片容器里）；守卫要把场景搬到**本机也会溢出**的屏高（`_CLAMP_SCREEN=700`），否则本机永远绿。
- **单位：屏高替身是物理像素、窗口尺寸是逻辑像素**（CTk 写 geometry 会乘窗口缩放；不换算就是 125% 屏上"窗口出屏"假红的来源，见 `widgets.window_scaling`）。同族判据要**允许"最小尺寸装不下"**（主窗口 720 下限在高 DPI 矮屏无解——逐条查策略：设计尺寸 / 屏幕−安全边距 / 最小尺寸取最大，不拿窗口比屏幕）；舒适线也有**够不着**的时候（正文已压到 `_DIALOG_BODY_MIN` 仍超线）：口径是"正文区已在下限 + 整窗不出屏"。

### 按钮配色的判据：底色必须是调色板的 token，且危险色只有一处定义

建窗遍历控件树的 CTkButton，读 `cget("fg_color")` / `("text_color")` / `("state")`，把调色板颜色值反向映射成 token 名逐条断言（`test_gui_styles.py`）。判据：

1. 每个按钮底色必须是某个 token——读到 CTk 默认色对 `['#3B8ED0','#1F6AA5']` 就说明从没被上过色。
2. 同一**父容器**里至多一个主色按钮（按父容器分组不是按窗口：主窗口是多分区窗口）。
3. 「取消」「关闭」永远是次色。
4. **危险色（含 danger_soft）按钮集合 == 登记表**（集合相等不是包含：少了红、多了也红）。
5. **破坏性动作颜色按回调名判不看文案**：command 可读（`button.cget("command")` 返回原回调），取 `__name__`，带 delete/remove/purge/erase/trash/unlink 的必须落在 `widgets.DESTRUCTIVE_STYLES` 或禁用态——拦的是将来（新增删除按钮穿主色自动红）；第 4 条补位拦"匿名回调的确认按钮误穿危险色"（lambda 天然豁免第 5 条）。颜色→样式是反推的（遍历 BUTTON_STYLES 与 button_colors 对比），widgets 改配色这里跟着变。兜底：回调名带删除词的实测集合与 `_DESTRUCTIVE_HANDLERS` 比相等。

实现纪律：**危险色只有一处定义**（`widgets.button_colors(palette, style)`；`paint_button_style()` 供不登记重绘的按钮共用；单测钉"登记式与就地创建的按钮取色一致"）。模态框在 `wait_window` 替身里量、非模态弹窗（`info_dialog` 不调 `wait_window`）建完直接量——否则那个界面一条都量不到（兜底断言"登记表每个界面都量到了"）。**空库那一轮别跳过**：主窗口 kit.apply 的调用都在动作里，空库首启时顶栏/状态栏/页面容器停在默认灰、按钮默认蓝，只有空库能看出（有数据时 `_load_first_game` 顺手刷对）——判据分两条：逐界面量按钮 + 另建空库量"顶栏/页面容器/状态栏恰好三个调色板色、树里没有看得见却穿主题对色的控件"。

### 可用性的判据：禁用必须看起来禁用，忙碌期间不许有"点了没反应"

建窗驱动到目标状态，遍历 CTkButton / CTkRadioButton / CTkCheckBox 读 state 与各颜色 cget（`test_gui_states.py`）。四条：

1. **禁用态必须压暗**（底/描边/字色都换禁用色；只 `configure(state="disabled")` 不重绘的按钮禁用后仍然亮着）。落点：`widgets.paint_button_state(button, palette, style)` + `UiKit.repaint_button()`；**主题重绘也走这条**，否则切主题把禁用按钮画回亮的。
2. 单选/复选框禁用字色必须来自调色板（不给就落到 CTk 主题的另一个灰阶）。
3. 不许有"死了的控件"：可点按钮必须有回调；单选/复选必须绑 variable。
4. **忙碌期间"会启动长操作"的按钮必须禁用**，按**回调名**判不看文案（新增走 `_submit` 的入口忘了置灰自动红）。

兜底：清单里登记的每个长操作回调都必须真的在测量里出现过（防清单对着空白恒绿）。

### 间距/圆角/字号的判据：页面只许用刻度上的值

把界面模块源码读成 ast，统计 `padx` / `pady` / `corner_radius` / `CTkFont(size=…)` 取值。刻度收在 `src/archive_management/ui/metrics.py`（间距 10 档：12 以内步长 2、以上步长 4）与 `typography.py`（字号 9 档）。守卫是**静态的**（`tests/unit/test_ui_metrics.py`，不启动界面）：① padx/pady 整数必须在间距刻度上；② corner_radius 必须是圆角档位（RADIUS_NONE / RADIUS_PILL / RADIUS_SM / RADIUS_MD / RADIUS_LG）；③ CTkFont(size=…) 必须是字号阶梯；④ **兜底**：扫到的文件数与站点数必须与登记数字相等（刻度模块改名、glob 写错、kwarg 改名都会让"0 处违规"空转）。

注意三条：**具名常量是允许的出口但要留注释**（量出来的缩进没法落刻度时写成模块级常量；出口的代价由"站点数相等"兜住——把一批数字挪进常量，站点数会掉、测试立刻红）；**改值按角色改不取最近的数**（取值理由写在 metrics.py 模块文档）；**收敛是视觉变更，必须重抓截图人工过一遍**。

### 卡片/列表行的判据：选中 > 悬停 > 常规，只有一条规则

可点可选中的卡片与列表行（详情页备份卡片、主页列表行/海报卡、发现页候选/目录行、管理窗口位置行）用同一套语言，收敛到 `widgets.card_surface_colors(palette, selected=…, hovered=…)`：选中 → accent_soft 底+描边（刻意不用实心强调色）；未选中悬停 → card_hover 底+card_border；常规 → card 底+card_border。判据两半（`tests/unit/test_ui_widgets.py` + `tests/integration/test_gui_home.py`）：纯函数四组合 × 两套主题；真驱动 `_set_hover` 看重绘，且断言**卡片与子控件都绑了 `<Enter>`**（只绑卡片，鼠标移到文字上反馈会闪掉）。

两个坑：**CustomTkinter 重写了 bind，查询式 `widget.bind("<Enter>")` 永远返回 None**，真绑定在内部 canvas 上——断言接线要读 `widget._canvas.bind("<Enter>")`。**悬停不能顺手写成全量重绘**：只重绘受影响的两张并把 `_hover` 在列表重建时清掉（否则指向已销毁卡片，`bad window path name`）。环境差异两条：`_set_hover(id)` 后**立刻读**（延后重排会把 `_hover` 复位；CTk 的 configure 当场生效不用 pump）；量"其余卡片常规底色"前显式 `_set_hover(None)`（CI 上鼠标可能正好压在卡片上）。

### 计数与时间的判据：一行一页、不带裸数字

每个页面底栏只占一行，计数写成"名词 + N + 量词"、并列用 · 分隔，不许裸数字。守卫：`test_gui_discovery.py` 断言"探测结果页第二行必须为空"并与 `tr("discovery.counts_candidates", …)` 逐字对齐。时间统一 `models.format_stamp`（2026/09/24 20:11）、空值统一 —，由 `test_ui_models.py` 与 `test_gui_lifecycle.py` 兜着。

### 文案完整到达用户的判据（未替换占位符 / 截断可回看 / 空状态）

- **静态（`tests/unit/test_i18n_copy.py`）**：① 遍历 `src/**` 的 `tr(...)` 调用比对字面键占位符与实参名（少传一个界面直接显示 `{name}`；键是变量或 **kwargs 的按"看不出来"跳过——宁可漏报不猜）；② 中文文案里「」引用的面板/按钮名必须在文案表真实存在；③ 中英两份占位符名一致。
- **运行时（`tests/integration/test_gui_copy_quality.py`）**：驱动界面状态收集每个文本控件实际显示的字，断言无 `{}` `}` 残留、被省略号截掉的必须挂悬停提示、自己折行的末行不许只剩标点、概要卡统计值不许退化成一个符号。

两个坑：**"引用名存在吗"不能拿全表做子串比对**（名字本来就写在自己那条文案里，要比"自己以外的"文案）；**夹具必须真的造出必须截断的文本且断言造出来了**（夹具里有 >= 3 处省略号的前提断言）。顺带记一个真 bug 形状：进过"按可用性重绘"登记表的控件被渲染重建销毁后，下一轮重绘会碰不存在的控件（销毁时摘登记 + 重绘时跳过已销毁项）。

### 对比度判据：三档阈值 + 装饰性描边的边界

"这行字看得清吗"必须能算出数值（`ui/contrast.py`：WCAG 2.1 相对亮度与对比度，纯算术不碰 Tk；阈值与逐对登记表在 `tests/unit/test_ui_contrast.py`）：

| 对比场景 | 阈值（WCAG AA） | 覆盖的 token（两套主题同表） |
| --- | --- | --- |
| 正文 / 表格文字 on 背景 | ≥ 4.5:1 | text / text_soft / text_muted × bg / surface / card |
| 大字号 / 图标 / 按钮文字 on 按钮底 | ≥ 3:1 | text on primary / secondary / danger 底 |
| 交互组件描边与状态指示 | ≥ 3:1 | primary / danger 描边 on bg / surface / card |

守卫逐对 parametrize（两套主题各量一遍），失败信息直接给两个 token 名与实测值。兜底四条：每个文字 token 都必须被判过、主要表面色都当过一次底色、登记规模钉死、两套主题的 accent/danger/text_muted/text_disabled 不许同值。另请第三方库 color-contrast 再算一遍（独立实现 WCAG 公式，比值须落 ±0.005 内）。

坑：**探针量表要比守卫登记表更全**（探针也要跟着调色板走）；**别把装饰性描边塞进 3:1**（"不要求"也要登记：1.2:1 下限+理由）；**"不要求"与"要求"写在不同档位里就要拆 token**（输入框与面板曾共用 border，拆出 input_border；候选色要对全部表面色量、先量 CTk 默认 border_width 才动手；守卫：`tests/unit/test_ui_input_border.py` 的 AST 完整性守卫 + `test_gui_theme_repaint` 切主题后单挑输入控件量）；**悬停底色也是文字底色必须一起登记**（漏登记时悬停态文字对比度从来没被算过）。

### 键盘可用性的判据：Tab 走得通、焦点看得见、Esc/回车接得上

CTk 的实测缺口（Tk 8.6 + CTk 6.0）：按钮/单选/复选/开关画在 Canvas 上且内层 takefocus 是空串、聚焦时 border_color 不变、Canvas 无空格/回车绑定。补法在 `ui/keyboard.py`（类级补丁，随 widgets 导入安装）；守卫 `tests/integration/test_gui_keyboard.py` 逐界面量：

1. **Tab 链**：可交互控件 takefocus 让 Tk 走得到（按钮类显式 1），禁用态摘出去（0）。
2. **Tab 顺序 = 视觉顺序**：`tk_focusNext` 按堆叠顺序（≈创建顺序）走，与 grid row/column 无关——量法是比"创建顺序"与"按 (row, column) 排序"。
3. **焦点可见**：聚焦换专用环形色（3px）；主色/危险色实底换对应软底（`widgets.FOCUS_STYLE`，颜色仍在 button_colors 一处定义）；失焦时描边/底/字/悬停全部原样还原（存下来再还原）。"分得开"是数值判据：环对聚焦后底色 ≥ 3:1（`keyboard.FOCUS_RING_MINIMUM`，无头守卫 `tests/unit/test_ui_keyboard.py`）。

坑（挑关键的）：**合成 `<FocusIn>` 不触发绑定**（要量焦点环用真焦点：focus_force 拿、挪走拿 FocusOut；旧实现恒用强调色 + 断言只比"≠ accent"就是假绿）；**失焦还原要先失焦取基准**（对话框打开即定焦，不先放下会得到颠倒基准）；**焦点黑洞控件必须真的被映射且在可见区内**；**不要对窗口本身 focus_force()**（顶掉子控件焦点）；**不要用 tk_focusNext 当量具**（同进程反复建/销窗口时偶发加载不了 Tcl/Tk 库数据）；**类级补丁要对替身控件宽容**（能力探测 + suppress，遇没有该能力的对象安静跳过，否则弄红别人的单测）；**CTkRadioButton 没有可读 border_width、CTkOptionMenu 两样都没有**（`keyboard._border_options` 能读几样读几样；塞同一个 suppress 被中断会 KeyError，描边永远停在焦点环色上）。

### 焦点态不许改变"这颗按钮是什么按钮"

聚焦换色上线后 CI 一次 8 条红，根因一个：**换色把"聚焦态"变成了识别按钮性质的依据**。CI 与本地时序相反（CI 上窗口一建出来定焦就生效，本地常被抢焦）。要点：

- **产品侧真缺陷**：靠"实测底色 == accent/danger"找主按钮的实现，聚焦换软底后找不到 → 回车确认失效。修法：make_reachable 把没聚焦时的原样存到 `widget._focus_saved`，`keyboard.resting_fill(widget)` 是唯一入口。
- **量配色的用例量之前必须先失焦**（`_defocus(window)`），否则量到聚焦态：危险色记成软危险、主色"少了一颗"。
- **量之前等控件树长稳**（`_stabilize` 比形状最多 8 轮——延后重排会在量到一半时整批换控件，对已摘下控件的 focus_force 有"最后焦点"却无 FocusIn）。
- **一次量不到不算数，也不放过真缺陷**：`_focus_and_read` 试三次；三次画不出分两种——对照控件（焦点黑洞"煤鸟"，自己也接焦点环）画得上说明路通、是**真缺陷**报红；煤鸟也画不出才是环境（记 `_UNMEASURED` 附在失败信息里）。
- **把 CI 时序搬回本地的办法**：临时插件包住 `dialogs._present` 再跑 3 轮 `update()`，本地逐条搬回假红、修完确认全绿、删掉插件。

### 分支图的判据：item 账目 + 几何 + 漫游

- **item 账目**：画布 item 数 == 框 + 连线 + 文字（每框两条）+ 折叠标记（每个有孩子的框一个），与 `canvas.find_all()` 长度一致（渲染基准主判据：400 节点链 = 1998，折叠根后 = 4，`tests/performance/test_backup_scale.py`）。
- **几何**：最深一层与最右一列不越 scrollregion；父框中心 = 首末孩子中心中点（纯函数穷举，`tests/unit/test_ui_tree_layout.py`）。
- 交互三条（`tests/integration/test_gui_branch_graph.py`）：**箭头按需显示且"露着哪几条"要量控件本身**（读 `winfo_manager()`，不是 xview()/yview() 的跨度——后者只答"那边还有没有内容"；与按需滚动条同一条纪律）；**拖到框上不能变成选中**（按下先命中测试，松开位移 < 4px 才算点击）；**坐标要换算**（事件给控件坐标、布局给画面坐标，过 canvasx/canvasy）。

实测补过的缺口：箭头在布局变化后不重判（`<Configure>` 触发 `_refresh_arrows`，只在状态真变了时动控件）；按键绑定把 Event 追给回调（签名收下 `_event`，守卫走真实绑定 `event_generate("<Right>")`）；Shift+d 的 keysym 是 `D`（WASD 大小写各绑一条）；箭头按钮要在画布**之后**创建（Tk 堆叠顺序=创建顺序）。一个画布里多个悬停目标时提示由视图自己管（`widgets.HoverTip`：调用方给文案+屏幕坐标，set_hovered 挂/收、set_items 直接收）；画布焦点环用自带 highlightthickness/highlightcolor（不在 keyboard.PATCHED_TYPES 里；环对底色 ≥ 3:1）；**收起的控件拿不到焦点**（place_forget 后 focus_set 无效——"箭头真收起来了"可用"焦点给不到它"再钉一次；反过来合成按键只送给有焦点的控件，event_generate 前先 focus_set）。

### 折叠/展开的判据：空折叠 == 旧行为、藏起来必须留痕

折叠拆"纯函数 → 绘制 → 交互"三层（`tests/unit/test_ui_tree_layout.py`、`tests/integration/test_gui_branch_graph.py`）：

- **空折叠必须逐字段等于旧结果**（最重要）：不传 / 空集合 / 不存在的 id / 叶子——四种输入铺出的图一样（既有几何判据与视觉基线都建在现在这套铺法上）。
- **折叠 = 子树不参与布局**：宽度只算一个框、不产出出边、高度只看剩几层；后代数含间接后代；藏在另一个折叠节点里的折叠不算数。
- **藏起来必须留痕**：说明行写 N 个后代，藏着选中项或当前节点各自标出（含选中/含当前）并变强调色；判据把"看得见的说明行 + 被裁时的悬停全文"合起来看。
- 三条命中各干一件事：标记折叠/展开、框选中、空白平移；按标记上既不平移也不选中。
- **折叠不改选中**（反向守卫）。
- **折叠后视口对齐到刚点的框**（scrollregion 变小 Tk 会把偏移夹回）。
- **折叠集合不持久化**：set_items 时与现有节点取交集；切视图不清。

两个坑：测试 helper 的坐标换算方向别写反（没滚动时恰好相等，一滚动就点空）；折叠痕迹判定先问"这个框是不是折叠着"（否则每个有后代的框都被当成藏着东西）。

### 反馈文案的判据：失败给下一步、结果给去处与计数

机制（忙碌态/取消按钮/FeedbackKind）与文案是两回事，判据落在文案（`tests/unit/test_ui_feedback_copy.py`，纯 i18n 数据）：

- **error.* 每条都要给"下一步"**（两套语言都命中信号词：请/重试/刷新/检查/换一个/try again/…）——拦"只把 OS reason 原样弹出"。
- **登记的 result.* 必须带占位符**（{file}/{path} 去哪儿了、{count}/{files}/{size}/{skipped} 几条）。
- **兜底**：没进登记表的 result.* 必须在豁免清单写明理由；两套语言键一一对应；信号词表本身要真的命中全部 error.*。

### 重要功能键的配色索引：按回调名登记"允许的性质"

与破坏性动作同一手法扩到"重要功能"：`test_gui_styles.py` 的 `_IMPORTANT_ACTIONS` 登记**回调名 → 允许的性质集合**，反推出的性质必须落在集合里；每个登记项至少一条可用态观察（禁用色反推不出本色）。两点：同一功能会有两个入口两个档位（登记的是集合，写窄并注明理由）；**清单必须能被兜底断言检验**（拼错/改名的回调名对着空白恒绿）。

### 报告自检与上传（不要跳过）

报告是静态站点：详情页按需 fetch `data/test-results/<id>.json`，这个目录在传输/解压环节被丢掉时报告只剩汇总（点开用例是空的）。所以每个生成报告的作业在上传前跑 `scripts/verify_allure_report.py`：

- 检查入口资源（index.html、单个 app-*.js、summary.json、test-results.json、widgets/**/statistic.json、tree.json）；
- 逐条核对结果索引引用的详情文件存在、条数与 allure-results 一致；
- 校验用例分组引用的结果 id 都在索引里；
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

报告侧不用额外配置（附件随 allure-resources-* 上传，verify_allure_report.py 一并核对）。**dump 里是真实局部变量**（coredumpy 会遮掉像密钥的串与 os.environ 值）——把报告发给仓库外的人之前先看一眼附件。

**作业级崩溃是另一条线**：每个作业失败时跑 `scripts/collect_job_diagnostics.py`（if: failure()），只有**进程级崩溃/证据缺失**才上传 `job-diagnostics-<作业>`（普通失败的证据已在结果与日志里），汇总收下拼成首页《失败现场(兜底)》附件 + 每个崩溃作业另写一条 broken 结论（标题"作业崩溃: <标签>"、环境=所在平台、testCategory=diagnostics——只挂附件不进计数也不能按环境筛）。**没有兜底现场时两样都不写**（还删掉上一轮留的）：永远存在的"失败现场"只会让人以为崩过。**摘要里被截断的现场要能点着找到**（`--full-copy-at` 写明承载全文的 artifact 与 run 的 artifact 列表页链接；faulthandler 摘录从尾部捞崩溃线程的 Current thread 栈——只截开头全是等在 threading.wait 的旁观线程）。

#### macOS 原生崩溃

崩溃不在本仓库代码，在**解释器自带的 Tcl/Tk**：uv python install 装的 python-build-standalone 构建在 macOS 捆绑 Tcl/Tk 9.0，UI 用例死在 9.0 的 Aqua 位图绘制 use-after-free（EXC_BAD_ACCESS，栈停在 `-[NSCGSContext dealloc]` → `_invalidate` 一族）。处置（三层都有守卫）：

- **从依赖层规避**：pytest 的 macOS 片换成 `setup-python` 的 python.org 构建（自带 8.6）+ `UV_PYTHON_PREFERENCE=only-system`，且自检一步同时断言 `TkVersion < 9`（解释器又带回 9.x 时当场红，提示见这条注释）。
- **重跑包装降级为「偶发崩溃的通用兜底」（保留，带到期日）**：UI 段的 pytest 外面包一层 `scripts/run_pytest_with_crash_retry.py`——只有"进程被信号/原生异常杀死"的退出码（SIGTRAP=133 / SIGSEGV=139 这一类，含 Windows 的 0xC0000005 / 0xC0000409）才触发重跑；断言失败、门禁不过都是普通退出码，**一次都不会重跑**，真回归不会被重试稀释或掩盖。重跑前先清掉崩溃那次新写进 allure-results 的半套结果（快照之前的一律不动，否则重跑后的报告缺半边、同用例出现两份结论）；覆盖率不用清（pytest-cov 只在会话结束落盘，崩溃那次什么都没写）。只重跑一次：第二次仍崩就按它的退出码失败，faulthandler.log 与 .ips 照常由 if: always() 的上传步收走；重跑在运行日志留 `::warning::` 注解、在运行摘要页写一段说明。**这层包装带到期日**（脚本里的 `RETRY_UNTIL`）：过期后直接透传不再重试并打提醒，`tests/unit/test_ci_diagnostics.py` 里"窗口没被忘记"的守卫从到期次日起变红——要么移除这层包装，要么确认仍有值得兜底的偶发崩溃后**明确续期**。

一次真实的偶发就是靠它断的（2026-09-30）：一条 GUI 用例整轮全量里红过一次（单跑、整模块连跑两遍都不复现），手里只剩一个测试名。事后从 crash-dumps 里那次失败留下的 dump 读出根因——`OperationalError: unsupported file format`，栈最深一帧是 `MonitoredDirectoryRepository.list_all()` 的 `connection.execute(`，异常穿过 `_on_scan` 的 `except ArchiveManagementError` 冒到用例：**环境级偶发**（临时 SQLite 文件在那一刻被读成非 SQLite 内容），不是"等得不够"的时序问题。两条可复用的做法：① **先读现场再动手**——dump 是**失败当刻**写的且**写盘**（与有没有 --alluredir 无关），`coredumpy peek crash-dumps` 就能找到是哪条用例的；② 本地跑全量最好照 CI 的写法带上 `--alluredir=allure-results`，否则截图与其余附件不落盘。

### 超时留证（线程栈 + 覆盖率 + dump + 一条结论）

`--timeout` 用 thread 方式，上游收尾是"打印线程栈后 `os._exit(1)`"——跳过整条 atexit，正好丢掉最想要的两样：收尾才落盘的覆盖率数据与挂在 `pytest_runtest_makereport` 上的失败现场。`tests/timeout_guard.py` 借 pytest-timeout 的扩展点（`pytest_timeout_set_timer` / `cancel_timer`，钩子由 conftest 挂上）接管这条路径（只在 thread 模式；signal 模式抛异常走正常收尾，原样交给上游）：

```text
倒出用例至今的输出/日志 → 线程栈 → 覆盖率存盘 → 主线程帧的 coredumpy dump
  → 写一条 Allure 结论（栈与 dump 是附件）→ 冲干净输出 → os._exit(1)
```

要点：结论走**终端写入器**不走 print（pytest 捕获 stdout，`os._exit` 跳过收尾会把缓冲区一起丢）；**先倒出捕获内容**（"卡死之前程序自己打了什么"往往比栈更能说明问题）；覆盖率控制器挂在 pytest-cov 私有插件名 `_cov`、Allure 目录选项叫 `allure_report_dir`（猜错只会静默不写）；dump 超 25 MiB 只记落点。报告里是一条 broken 结论（标题"超时留证: <用例>"、critical），**故意不写 framework/testCategory 标签**（不该改变质量门口径）。留证不改变结局：最后照样以 1 退出（超时本来就不该被"想写证据"拖住）。守卫 `tests/unit/test_timeout_guard.py`，其中两条是接线守卫：钩子必须真挂在 `tests/conftest.py` 上、`pyproject.toml` 里必须仍然是 `--timeout-method=thread`（改成 signal，接管就永远不会生效，"超时后什么都没有"的老问题会静默回来）。

### 分片执行与结果合并

每个平台把用例拆成几片并行跑（Linux 3 片、Windows 2 片、macOS 1 片），再由 `pytest-report` 把各片结果合并成一份——墙钟时间只取决于最慢的那一片。

- **分片规则**（`tests/sharding.py`）：权重**三级取值**——每条用例的实测耗时（`tests/durations.json`）优先，没有实测的退回目录经验权重（集成 2s、安全 0.6s、单元 0.05s，未知目录 0.3s），再"最慢的优先"贪心装箱（LPT）。实测值是必须的：同一模块内单条用例能差 8 倍，只按目录单价装箱等于按**条数**装箱，慢用例会堆在同一片。刷新命令见第 7 节；记录/加载/合并的约定在 `tests/durations.py`，守卫 `tests/unit/test_durations.py`。CI 三平台共用一份档案（用的是同次录制内的相对排序）。
- **参数**是 `--shard-count` / `--shard-index`（默认 `1`/`0` 即不分片），过滤发生在**严重等级过滤之后**：本地 `--min-severity=critical` 选出的子集也能分片跑。三条性质由 `tests/unit/test_sharding.py` 锁住：不重不漏（各片并集 == 全集）、同输入同分片、各片权重接近理想值。
- **不要用 pytest-xdist**：GUI 用例各自起真实窗口，并行只会互相拖慢还引出 Tk 初始化失败；分片是进程级（CI 上机器级）并行。
- **本地全量想快**：`uv run python scripts/run_tests_local.py`（双进程分片，实测省约三分之一墙钟，与单进程逐数一致）。**2 片是甜点不要贪多**（两进程同时操作真实窗口，桌面合成层互相拖慢）。脚本起 N 个进程等全部结束按片打印摘要，pytest 参数用 `--` 透传，片号由脚本注入别再写。
- **合并**在 `pytest-report`（每平台一份，都在 Ubuntu）：`scripts/merge_allure_results.py` 搬各片结果（日志逐片给文件数）；覆盖率 `COVERAGE_FILE=.coverage.shard-<片>` 分片写再 combine。
- **报告可在别的平台生成**：合并/汇总/生成/自检全是纯文件操作，三平台报告都在 Ubuntu 生成（Windows/macOS runner 按 2/10 倍计价）；"结论算哪个平台"由矩阵参数决定（`--platform` / `--expect-platforms`），**不能看 runner.os**（守卫 `test_report_job_does_not_depend_on_the_host_platform`）。
- **跨平台合并覆盖率靠 `relative_files = true`**（各作业都从仓库根跑，combine 把分隔符归一；少了它"合并成功但一个文件都对不上"；守卫造一份反斜杠数据真跑 combine）。数据必须**按平台分开**合并与判门槛。
- **产物清单 `--manifest` 是"少一片"的唯一判据**：少一片时合并照常成功，报告只是安静少一部分（环境、通过率、格式自检、原生质量门全看不出来）。清单记逐分片文件数与结果条数、合并合计、`--expect-shards` 声明缺了谁；随 allure-resources-* 上传，由 `verify_allure_report.py --manifest` 与最终条数对齐。
- **平台专属代码只在别的平台被排除**（`# platform: windows - 原因` + `scripts/coverage_platform.py`）：只可能在自己平台执行的块（注册表探测、fcntl、pmset），按平台判覆盖率时别的平台永远"未覆盖"；整块 no cover 又太粗。做法：conftest 只对 `coverage report/xml` 命令行生效（pytest --cov 那条路不生效——pytest-cov 在导入根 conftest 前就构造好 Coverage 对象），按当前平台派生一份配置（原样带过 `[tool.coverage.*]` 再追加排除别的平台标记行的正则；exclude_also 是替换不是合并）；合并口径用 pyproject 基线（三平台并集），单平台数字用 `coverage_platform.py write --platform <展示名>`。**只排除标记所在行**：多行 if 的 `):` 上标记时块体还算缺口——"别的平台走不到但只是权限差异"的分支应换成可替换谓词 + 三平台都能跑的用例（源码里现在一处平台标记都不剩，机制与守卫保留）。守卫 `tests/unit/test_coverage_platform.py`。
- **门槛 100%**（`pyproject` fail_under 与汇总脚本 COVERAGE_THRESHOLD 两处同改，守卫 `test_coverage_fail_under_matches_pyproject`）。前提是每处够不着的行都有**带原因的豁免**（写法与理由由守卫管）。三平台各自到 100%：平台专属路径靠**注入替身**在别的平台跑到（不能跑的用例不算覆盖，旁边补替身版）。
- **门槛在合并后判但不落在合并作业成败上**：分片作业 `--cov-report= --cov-fail-under=0`；合并作业那步只印日志（continue-on-error）——让它非 0 退出会把"缺产物"与"覆盖率掉"混成同一种红。判定交给报告的 Coverage report 条目（低于门槛状态即 failed）与原生质量门规则集一。守卫 `test_the_report_job_only_judges_missing_pieces`。
- **缺片的覆盖率一概不判门槛**：`collect_coverage_data.py --expect-shards` 逐片对数，缺片非 0 退出、后面全部跳过、末尾让作业红（曾经的坑：coverage xml 自己会合并剩下的数据并执行 fail_under，把"缺数据"说成"覆盖率掉了"；xml 那步带 `--fail-under=0`）。守卫 `test_ci_judges_the_coverage_only_on_complete_shard_data`。
- **产物名不变**（allure-resources-<镜像名> / coverage-<镜像名> / allure-report-<镜像名>——名字带的是哪个平台的产物不是生成机器；改名历史曲线清零）。汇总作业 needs 必须含 pytest-report（否则在产物上传完之前开始下载）。
- **片数按平台给**（Linux 3 / Windows 2 / macOS 1）：总时长 ≈ 片数×固定开销 + 串行时间，便宜平台多开片贵的少开。片号 0 连续编到片数-1；片数出现在四处（矩阵、--shard-count、--shard-index、--expect-shards），不一致会**静默少跑**（守卫逐平台校验）。
- **依赖缓存只让片 0 写**（save-cache: `matrix.shard == 0`）：同平台各片 cache key 相同，并行保存只有第一个抢得到，其余打 Failed to save 噪音。

## 7. 本地生成与查看报告

前置：Allure 3 CLI，与 CI 同一条安装命令（`npm install --global allure@3`——3.13~3.17 的质量门会静默放行，见第 6 节）。`allure-results/`、`allure-report*/`、`.allure/` 都在 .gitignore。

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

分片不改收集结果：三片并集与全量逐条一致，合并后总覆盖率与串行一致。也可用下载的 CI 结果（allure-resources-* 里的 allure-results/）本地复现报告，命令相同；generate 后先自检再打开。

四个坑：

- **报告要经 HTTP 提供**（file:// 被跨域拦掉，用 `allure open` 或任意静态服务器）。
- **`allure open` 的目录是必填参数**（`allure open allure-report --port 8080`）。
- **报告目录已存在时 generate 不刷新数据还把新报告藏进子目录**（上游 issue #691）：症状是 `allure-report/awesome/` 出现、顶层 index.html 还是旧的（"历史不可见"多半是它）。应急看 awesome/ 子目录，干净做法 `rm -rf allure-report` 重新生成。CI 不受影响（全新 runner + .gitignore）；自检把嵌套目录判红（nested_report_problems，守卫 `test_a_nested_report_directory_is_reported`）。
- **打开前先自检**：缺 data/test-results/*.json 时界面照样显示通过/失败，点开用例是空的。

## 8. 新增测试清单

1. 先想清楚这属于哪一层、严重等级是什么（第 1 节），不要照抄邻居。
2. 构造数据优先用 `tests/helpers.py` 的构造器；构造器要传真实路径、真实 SQLite，别拿 `MagicMock` 顶领域服务。
3. 需要临时文件/目录的用 `tmp_path`，不要写仓库目录或用户主目录。
4. 界面用例确认有没有现成的共享基建（`tests/button_support.py`、`tests/ui_sharing.py`）可搭。
5. 用例名与断言信息用中文，按"给定—当—then"组织，读得出业务含义。
6. 涉及平台的差异（路径分隔符、文件名大小写、显示服务）要么显式适配要么写成"只在那个平台跑"，并在用例里写明理由；不要悄悄依赖运行机器。
7. GUI 用例遵守第 6 节各"判据"小节的量法与坑（自检步骤、不与首帧时序赛跑、收尾纪律）。
8. 跑过全量再提交（`uv run python scripts/run_tests_local.py`），确认没有把别的用例弄红。

```shell
# 提交前本地至少跑这些
uv run pytest --min-severity=critical
uv run ruff format . && uv run ruff check . --fix
uv run mypy
```
