# 测试体系

本文档说明测试如何分类、在哪里执行、结果如何汇总，以及新增测试时要遵守的约定。目标是让"本地提交要快"和"CI 要有完整证据"两件事同时成立。

其他文档：[功能说明](features.md)、[游戏库与发现](library.md)、[全局快捷键](hotkeys.md)、[平台支持](platforms.md)、[开发与发布](development.md)。

## 1. 目录与边界

```text
tests/
  conftest.py            # 严重等级 + 四层 Allure 标签 + 审计日志夹具
  helpers.py             # 跨模块共享的测试数据构造器与环境信息
  reporting.py           # 性能基准与安全结论的记录器
  unit/                  # 纯逻辑与单一边界: 不会真正触达用户数据的临时目录/SQLite
  integration/           # 真实跨层协作: 配置 + 数据库 + 备份/恢复 + 审计日志 + UI 后端
  performance/           # 规模基准: 数据量与阈值必须显式声明（只在 CI 执行）
  security/              # 不可信输入与危险操作防护（只在 CI 执行）
```

| 类别        | 标记                       | 本地 `pytest` | pre-commit               | CI                                      |
| ----------- | -------------------------- | ------------- | ------------------------ | --------------------------------------- |
| unit        | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（三平台）                          |
| integration | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（三平台）                          |
| performance | `@pytest.mark.performance` | **不运行**    | 不运行                   | `performance` job（ubuntu，单平台采集） |
| security    | `@pytest.mark.security`    | **不运行**    | 不运行                   | `security` job（三平台）                |

### 严重等级（失败影响面）

每个模块在 `pytestmark` 里声明一个等级，`tests/conftest.py` 把它写成 Allure 的 `severity`。**等级表达“失败的影响面”，与测试层次无关**（层次用 `layer`），并且决定 pre-commit 跑哪些用例：

| 等级       | 判定标准                   | 例子                                                                            | 本地 pre-commit |
| ---------- | -------------------------- | ------------------------------------------------------------------------------- | --------------- |
| `blocker`  | 安全与数据完整性底线       | 路径越界 / 危险目标必须被拒、快照或清单被篡改必须拒绝恢复                       | 运行            |
| `critical` | 核心业务不可用或结果不正确 | 备份 / 快照 / 恢复 / 删除计划、仓储事务、迁移、调度、GUI 真实后端、全链路流水线 | 运行            |
| `normal`   | 常规功能与交互             | 配置、平台探测、主页聚合、对话框、CLI、热键、审计、存档位置与命名               | 仅 CI           |
| `minor`    | 展示与辅助                 | 调色板 / 控件样式 / 渲染修正、i18n 文案、打包元数据、演示后端、性能基准         | 仅 CI           |
| `trivial`  | 极低影响                   | 色值、脚本生成的报告汇总项（覆盖率 / 性能 / 安全摘要）                          | 仅 CI           |

约定：

- 每个模块**恰好声明一个等级**；单个用例可用 `@pytest.mark.blocker` 等覆盖模块默认值。`tests/unit/test_test_config.py` 会校验“恰好一个”，并要求**五个等级都至少有一个模块在用**（否则报告分布会退化回一边倒）。
- 漏写时按目录兜底（unit/integration=normal、performance=minor、security=critical），兜底只是为了不冒出 `no_severity` 桶。
- **别按目录或层次照搬等级**：`tests/unit` 里既有 blocker（危险目标判定）也有 minor（调色板色值）。
- 复现本地钩子跑的子集：`uv run pytest --min-severity=critical`。

### 层级（Allure 测试金字塔）

每个模块用 `pytest.mark.layer(...)` 声明测试层次，Allure 的“测试金字塔”与“按层耗时”控件直接读它。**层次描述“用例实际接了什么”，不完全等于所在目录**：

| 层次          | 判定标准                                                                     | 例子                                                             |
| ------------- | ---------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `unit`        | 单个组件 + 替身或内存数据                                                    | 解析器、格式化函数、纯逻辑校验、可注入替身的服务                 |
| `integration` | 真实数据库 / 文件系统 / 领域服务之间的协作（不要求位于 `tests/integration`） | 真 SQLite 的仓储与迁移、真实快照与恢复、跨层备份→恢复→删除流水线 |
| `e2e`         | 从真实入口走完整用户流程                                                     | 真实窗口 + 按钮/菜单操作、命令行入口的完整流程                   |

因此 `tests/unit/` 下的真实 SQLite / 文件系统模块（`test_repository.py`、`test_sql_backend.py`、`test_restore.py`、`test_backup_service.py`、`test_snapshot.py` 等）声明为 `integration`：它们确实在做跨组件协作，报告应当如实反映这一点。审查时如发现“目录层次”与“实际接的东西”不一致，以实际为准。

默认收集范围由 `pyproject.toml` 的 `testpaths` 决定（只有 `tests/unit` 与`tests/integration`）。性能与安全测试需要显式指定：

```shell
uv run pytest tests/performance -m performance
uv run pytest tests/security -m security
```

## 2. 共享测试设施

- `tests/helpers.py`：临时 SQLite（`migrated_database`）、游戏与存档位置（`add_game` / `add_location` / `make_save_folder` / `touch_save`）、备份服务（`backup_service` / `sql_archive_service` / `manual_scheduler`）、固定 UTC 时刻（`utc_moment`）、批量造数据（`seed_home_games` / `seed_backup_chain` /`write_text_files`）以及运行环境信息（`environment_info`）。为了让它能被导入，`pyproject.toml` 把 `tests` 加进了 `pythonpath`。
- `tests/reporting.py`：`PerformanceRecorder`（耗时 / 吞吐 / 内存峰值 + 阈值校验）与 `SecurityRecorder`（记录"场景 / 期望拦截 / 实际情况"，未拦截即失败）。
- 存量模块里的小构造器（如 `_service`、`_database`）保留原签名，只把实现委托到共享模块，避免几十处调用点跟着改。

约定：测试优先复用**真实**领域服务、临时目录与 SQLite；只有外部依赖（系统回收站、系统快捷键、真实网络）才用可注入替身。不为了测试在生产代码里加分支，也不写"只验证 mock 调用次数"的用例。

## 3. 性能基准

每个基准都用 `perf_recorder` 显式声明规模、指标与阈值，超出阈值即失败：

```python
with perf_recorder.duration("home.load_home", scale=SCALE, budget_seconds=15.0):
    board = home_window.load_home()
```

当前基准（规模见用例常量）：

| 基准                                                        | 规模                          | 指标     | 阈值         |
| ----------------------------------------------------------- | ----------------------------- | -------- | ------------ |
| `home.rows_query` / `home.load_facts` / `home.build_report` | 2000 款游戏 × 2 位置 × 3 备份 | 耗时     | 5 / 10 / 3 s |
| `home.filter_home`                                          | 上述规模 × 5 种筛选           | 耗时     | 4 s          |
| `home.load_home` / `backend.load_home`                      | 上述规模                      | 耗时     | 15 / 20 s    |
| `home.load_home_memory` / `backend.load_home_memory`        | 上述规模                      | 内存峰值 | 400 MiB      |
| `backup.list_for_game` / `backup.build_tree`                | 400 个备份节点                | 耗时     | 3 s          |
| `ui.timeline_order` / `ui.branch_order`                     | 400 个备份节点                | 耗时     | 3 s          |
| `backend.list_backups`                                      | 400 个备份节点                | 耗时     | 5 s          |
| `snapshot.create` / `snapshot.verify`                       | 300 个 32 KiB 文件            | 耗时     | 30 s         |
| `snapshot.create_throughput` / `verify_throughput`          | 同上                          | 吞吐     | ≥ 4 MiB/s    |
| `snapshot.create_memory`                                    | 同上                          | 内存峰值 | 64 MiB       |

阈值刻意留出宽裕余量（CI 机器与本地差异大），只拦"数量级"级别的回归：例如主页取数退化成按游戏逐个查询的 N+1、快照复制不再流式读取、树/排序退化成 O(n²)。

结果写入 `performance-results.json`（含环境信息与每条测量）与`performance-results.csv`，并由 `scripts/create_allure_summary.py` 转成 Allure 中可检索的测试项（指标表 + 原始文件附件）。

## 4. 安全测试

覆盖范围与用例边界：

| 文件                         | 覆盖内容                                                                                                      |
| ---------------------------- | ------------------------------------------------------------------------------------------------------------- |
| `test_path_escapes.py`       | 清单相对路径越界（`..` / 绝对路径 / 盘符 / UNC / 空值）、危险写回目标判定、篡改清单后的恢复、符号链接不被跟随 |
| `test_hostile_input.py`      | 恶意应用配置（未知字段、错类型、越界数值、非法快捷键、版本不兼容）、脏 Steam 数据、审计日志脱敏与截断         |
| `test_snapshot_integrity.py` | 快照内容被篡改、文件缺失、清单 JSON 损坏、清单条目类型非法 → 预检标记不可用且恢复被拒绝                       |

约定：不使用真实凭据或真实用户文件；涉及"用户主目录 / 盘符根目录"的用例只做**判定**，不执行写入或删除。

结果写入 `security-results.json`（场景 / 输入摘要 / 期望 / 实际 / 是否拦截），同样汇总进 Allure；未拦截即用例失败，因此安全回归会直接体现在 CI 状态上。

## 5. 结果与报告元数据

- 每条性能测量记录：规模、指标、取值、单位、阈值、比较方向、是否通过。
- 每条安全结论记录：类别、场景、输入摘要、期望拦截行为、实际结果、是否拦截。
- 环境信息记录：操作系统与平台族、Python 版本与实现、提交 SHA、分支与 CI run id、测试类别是否执行、覆盖率门槛（`scripts/create_allure_summary.py` 写入`allure-results/environment.properties`）。
- 覆盖率摘要由 `scripts/create_allure_coverage.py` 生成；性能与安全结果由`scripts/create_allure_summary.py` 生成，原始 JSON/CSV 作为附件保留，保证结论可下载、可追溯。覆盖率摘要项同样**把原始 `coverage.xml` 作为附件**带进报告（脚本按 `RAW_REPORT_FILES` 逐个收集，存在哪个带哪个：以后加 `coverage.json` 或把 `--cov-report=term-missing` 的输出重定向成文件，不改代码就会一并附上）；HTML 报告是整站，仍旧作为 `coverage-<os>` artifact 上传。
- **平台以 Allure 的"环境"维度呈现**（这是看出"结果来自哪台机器"的主路径）：每个用例都会写入 `env` 标签（取值就是平台展示名），仓库根的 **`allurerc.mjs`** 用 matcher 把它映射成 Allure 3 的环境。于是一份合并报告里有 `Windows / macOS / Linux` 三个环境：报告顶部出现环境选择器，每个用例详情页的「环境」分页会逐个列出它在三个平台上的结果（含状态、耗时与跳转链接），而不是只能从参数或套件名后缀里去认平台。
  另保留两样兜底：`平台` 参数（平台也进结果身份：三个平台的同名结果 `retryHash` 各不相同、`isRetry` 均为 `false`，不会互相并成重试；`historyId` 共享，所以历史趋势能连上）与 `os` 标签 + `parentSuite` 后缀（筛选与只认 suite 标签的控件）。
  **生成报告必须在仓库根目录执行**（CI 与本文档的命令都是如此）：环境不会仅因结果带 `env` 标签就生效，CLI 得读到 `allurerc.mjs` 才会识别；读不到时环境会静默退回单个 `default`，`scripts/verify_allure_report.py` 会把这种退化判为报告不完整（它同时打印 `环境: ...` 一行）。性能/安全/覆盖率摘要项也按同一规则处理：带 `env` 标签、**标题不再拼平台名**（三个环境里的标题完全一致，都是 `Coverage report` / `Performance baseline` / `Security findings`），平台由环境表达；`平台` 参数与 `os` 标签作为兜底（与用例结果一致）。
- 用例标题会还原 pytest 对参数化 id 做的 ASCII 转义（`\u7528\u6237` → `用户`），并写在 `@allure.title` 使用的同一属性上（`allure.dynamic.title` 会被 allure-pytest 用 `item.name` 覆盖）。

## 6. CI 流程

```text
quality (3 平台: ruff check / ruff format / mypy; 结论只把 Ubuntu 那份带进报告)
analysis (ubuntu: deptry 依赖卫生 / bandit 安全扫描 / pip-audit 依赖漏洞 / radon+xenon 复杂度)
pytest  (3 平台 × 3 片: 单元 + 集成 + 各片自己的 Allure 结果与覆盖率数据)
performance (ubuntu: 基准与阈值)
security    (3 平台: 越权与危险操作防护)
      ↓
pytest-report (每平台一个作业: 合并各片的 Allure 结果与覆盖率 → 生成并自检报告)
      ↓
allure-summary (合并全部 allure-results-* → 写入环境信息与质量/性能/安全汇总 → 生成最终报告)
```

每个作业都上传自己的 `allure-results-*`，汇总作业用`actions/download-artifact` 的 `pattern` + `merge-multiple` 合并后生成唯一报告，并沿用 `.allure/history.jsonl` 累积历史。

质量门禁本身也由脚本执行：`scripts/create_allure_quality.py --group <组>` 依次跑该组的检查，把每项的退出码、结论与**完整输出附件**写成 Allure 结果（任一项未通过时脚本以非 0 退出，作业照常红）。它分两组：`core`（ruff check / ruff format --check / mypy，公共检查：与平台无关，只在 Ubuntu 跑一遍；mypy 另外跑 `--platform win32` 与 `--platform darwin` 两次，覆盖平台专属分支）与 `analysis`（deptry / bandit / pip-audit / radon / xenon，同样与平台无关，只跑 Ubuntu 一遍）。两组都只在 Ubuntu 执行，所以汇总报告里每个门禁只出现一条；质量结论项带 `env=common`，归入 `allurerc.mjs` 里**显式声明**的 `Common` 环境（不是某个平台的环境，也不是隐式的 `default`）；性能之外的第二类"脚本生成项"就长这样（详见第 5 节）。

除此之外，汇总作业还会跑一次 **Allure 原生质量门**：`allure quality-gate --config allurerc.mjs allure-results`。规则写在 `allurerc.mjs` 的 `qualityGate.rules` 里，管的是整次运行（失败数 / 用例数 / 通过率 / 三个平台是否都合并进来了），与逐项检查互补；它的退出码直接决定作业成败，输出写进 `allure-quality-gate.txt` 并由运行总账收进报告首页「全局附件」。**CLI 版本必须 ≥ 3.18.0**：3.13~3.17 在配了 `historyPath` 时会静默放行（退出 0 且不输出任何内容 —— 根因是本地历史流的句柄悬空，`AllureReport.done()` 永不返回，Node 在校验前就退出了，见 issue [#895](https://github.com/allure-framework/allure3/issues/895)，修于 3.18.0 的 PR #962），所以 CI 把 CLI 钉在 3.18.0。本地复现：`npx allure@3.18.0 quality-gate --config allurerc.mjs allure-results`（单平台跑会因 `environmentsTested` 失败，属预期）。

### GUI 用例必须真的跑起来（skip 是有代价的）

`tests/integration/test_gui_*.py` 会把"Tk 起不来"当作环境问题处理：文件级守卫与每个用例的 `except TclError: pytest.skip(f"tk 环境不可用: ...")`。这样没有显示环境的机器不会一片红，但**代价是真正的 Tcl 故障会伪装成一堆 skip**，而 GUI 用例占覆盖率的很大一块——本机实测（把 `TCL_LIBRARY` 指向不存在的目录来模拟）：全量 `769 passed / 68 skipped`，覆盖率 **67.88% < 85%**，作业会以覆盖率门槛失败，且失败信息里看不出 CLI 之外的原因。

所以 CI 在 Windows/macOS 上、pytest 之前多跑一步显式自检：

```shell
uv run python -c "import tkinter; root = tkinter.Tk(); root.destroy(); print('Tkinter OK')"
```

Tcl 正常时它只是一行输出；坏掉时会打印 `Can't find a usable init.tcl ...` 并以非 0 退出，直接把原因抬到表面（Linux 的显示环境由 `xvfb-run` 提供，不重复检查）。

这条曾经真实发生过：跑测试的解释器是 `uv python install 3.12` 下载的 uv 托管 standalone 构建，它靠**自身目录里的 tcl 数据文件**定位 Tcl（`<托管目录>/tcl/tcl8.6`），那份副本一旦陈旧或不完整，`tkinter` 就报 `Can't find a usable init.tcl`（上游是 python-build-standalone 的已知怪癖，见 astral-sh/uv#7036；本机 venv 用的是 python.org 的 CPython，自带完整 tcl，所以本机复现不出来）。既然重建解释器就能修好，工作流里用 `uv python install --reinstall 3.12`，而不是事后设 `TCL_LIBRARY`/`TK_LIBRARY`——后者路径随平台变，还会影响其它 Tcl 使用者。

### GUI 布局用例不要和"首帧时序"赛跑

名称按宽度重裁这类断言最容易在平台之间飘：`winfo_width()` 在首帧往往只有 1（各平台完成布局的时机不同），而且可能**不再有尺寸变化事件**来补救，于是名称停在"兜底宽度"的短文本上 —— 人眼看不出来，只有断言能发现。产品侧对这种"宽度还没量出来"的情况安排了有上限的重试（`home_page._REFIT_MAX_ATTEMPTS`）；用例侧两条纪律：

- 判定"布局停在上一次宽度"这类回归**用假控件**（只实现 `_text` 与 `winfo_width()` 的替身），而不是在真窗口上改完文本再泵事件循环：后台重裁会在不同平台上以不同时机把文本改回去，自检会时灵时不灵。`test_gui_layout.py` 里两条用例正是这么分工的：一条跑真布局，一条用假标签验看门狗本身。
- 断言"当前宽度下应当截成什么"时别写死像素或字符数：用同一个 `fit_text(full, font, label.winfo_width())` 生成期望值，比对**文本**（与字体是否缺字形无关），像素只用来卡"缝隙上限"。

### 报告自检与发布（不要跳过）

报告是一套静态站点：用例详情页打开时才去取`data/test-results/<结果 id>.json`。这个目录一旦在传输或解压环节被丢掉，报告就只剩汇总与用例树——界面能看到用例通过与否，点开用例却是空的（生成阶段本身没问题，用同一个 allure 版本本地生成就有这些文件）。

因此每个生成报告的作业在发布前都要跑 `scripts/verify_allure_report.py`：

- 检查入口资源：`index.html`、单个 `app-*.js`、`summary.json`、`test-results.json`、`widgets/**/statistic.json`、`widgets/**/tree.json`；
- 逐条核对结果索引（`test-results.json` 的 `byId`）引用的详情文件是否存在，并确认条数与 `allure-results` 里的结果文件一致；
- 校验用例分组（`data/test-env-groups/*.json`）引用的结果 id 都在索引里。

任一项不通过即作业失败，且**不发布报告**（`Upload Allure report` 只在自检成功时执行）；标准输出里的"结果索引 / 详情文件 / 用例分组"三个计数就是排查入口。自检通过后用 `--zip` 把报告打成单个 `allure-report.zip` 发布：单文件要么完整到达、要么直接报错，不会出现"整个目录被悄悄丢掉"的半损坏状态（改动前的 artifact 就踩过一次）。

Windows runner 的控制台是 cp1252：Python 默认按该编码输出，**打印中文会直接 `UnicodeEncodeError` 打断步骤**（报告自检在 CI 上踩过）。因此工作流最外层设了 `PYTHONUTF8=1`，两个报告脚本自己也会把标准输出切成 UTF-8（取不到 `reconfigure` 的替身如 pytest `capsys` 就跳过）。新增会向终端打中文的脚本时注意这条。

下载 artifact 后本地核对（先解压外层 artifact，再解压其中的 `allure-report.zip`）：

```shell
uv run python scripts/verify_allure_report.py allure-report          # 只自检
uv run python scripts/verify_allure_report.py allure-report --zip    # 自检并重新打包
```

### 失败现场留证（dump + 界面截图）

只在 CI 出现、本地怎么跑都不复现的失败，事后能拿到的往往只有一行 traceback。所以用例失败时会自动把现场挂到**该用例的 Allure 结果**上（`tests/crash_capture.py`）：

- **崩溃现场 dump**：`coredumpy` 把最深一层栈帧的局部变量与对象属性写成 `crash-dumps/<用例>.dump`（在 `.gitignore` 里），附件与摘要都会写明落点。本地打开：`coredumpy load <文件>`（进 pdb），或在 VSCode 里用 coredumpy 扩展右键「Load with coredumpy」；只想知道哪个 dump 是哪条用例，用 `coredumpy peek crash-dumps`。
- **界面截图**：本用例创建的窗口（`tests/gui_support.py` 的登记表）在失败时的画面。Windows 走 `ImageGrab.grab(window=hwnd)` 按**窗口句柄**抓：窗口被别的窗口盖住（全屏游戏、多个用例窗口叠放）也拍得到，也不受显示缩放影响（Tk 报逻辑坐标、按屏幕区域抓拿的是物理像素，缩放不是 100% 时会错位；本次就是用这条修掉的）；Linux 按屏幕区域抓（需要 `DISPLAY`，CI 由 xvfb 提供），macOS 同（需要屏幕录制权限）——抓不到时只在摘要里写一句原因，绝不影响用例结果。
- **失败现场摘要**：平台 / Python / 提交号 + dump 与截图落点 + 复现命令，让报告里不只有一堆附件。

三条纪律写在模块注释里：留证**绝不改变用例结果**（每一步各自兜底，整段编排外面还有一层，出错只打一行日志）、**失败才留证**（通过的用例不产生任何文件）、**有上限**（递归深度默认 5、单次 dump 时限 20s、超过 25 MiB 的 dump 只记落点不挂附件、最多 3 张截图，同一用例只留一次——失败后 teardown 常跟着再报一次错）。参数：`--crash-dump-dir`（默认 `crash-dumps`）与 `--crash-dump-depth`（`0` = 关掉 dump，仍留摘要）。

报告侧不需要额外配置：附件由 allure-pytest 写进 `allure-results`，随 `allure-resources-<平台>` artifact 上传，`scripts/verify_allure_report.py` 会把它们一并核对（缺附件即报告不完整）。**dump 里是真实的局部变量**（coredumpy 默认会遮掉像密钥的字符串与 `os.environ` 的值）—— 把报告或 artifact 发给仓库以外的人之前先看一眼附件。

### 分片执行与结果合并

套件变长后，CI 的墙钟时间几乎全压在 pytest 上（2026-09 实测：Windows 298s / macOS 260s / Linux 132s，整次工作流约 9 分钟）。现在每个平台把用例拆成 **3 片并行**跑，再由 `pytest-report` 把各片结果合并成一份——墙钟时间只取决于最慢的那一片。

- **分片规则**在 `tests/sharding.py`：先给目录经验权重（集成 2s、安全 0.6s、单元 0.05s，未知目录 0.3s），再“最慢的优先”贪心装箱（LPT）。套件耗时几乎都在 GUI 用例上，只按**条数**平分会把慢的全堆在一片；实测三片 71s / 81s / 81s（理想 77s）。
- **参数**是 `--shard-count` / `--shard-index`（默认 `1`/`0` 即不分片），过滤发生在**严重等级过滤之后**：本地 `--min-severity=critical` 选出的子集也能分片跑。三条性质由 `tests/unit/test_sharding.py` 锁住：不重不漏（各片并集 == 全集）、同输入同分片、各片权重接近理想值。
- **不要用 pytest-xdist 代替分片**：本机实测 `-n 4` 让 `tests/unit` 从 39s 降到 21s，但 `tests/integration` 没有收益（222s），`tests/integration/test_gui_buttons.py` 反而从 143s 变成 174s，并多出 Tk 初始化失败（`invalid command name "tcl_findLibrary"`）。GUI 用例各自起真实窗口，并行只会互相拖慢；分片是**进程级**并行（CI 上还是**机器级**），不碰这个坑。
- **合并**在 `pytest-report`（每平台一个作业）：`scripts/merge_allure_results.py` 把各片结果目录搬进一份 `allure-results`（日志逐片给文件数，少一片能一眼看出来）；覆盖率用 `COVERAGE_FILE=.coverage.shard-<片>` 分片写，再 `uv run coverage combine` 合成一份。
- **覆盖率门槛只在合并后判**：单片覆盖率天生偏低，所以分片作业用 `--cov-report=`（关掉报告）与 `--cov-fail-under=0`（关掉门槛），合并后单独一步跑 `uv run coverage report`（阈值仍取 pyproject 的 `[tool.coverage.report] fail_under`）。之所以单独成步：原生命令的非零退出码只有作为该步**最后一条**命令时才会让作业失败，混在一起写会让门槛静默失效。
- **产物名不变**：`pytest-report` 上传的仍是 `allure-resources-<平台>` / `coverage-<平台>` / `allure-report-<平台>`，汇总作业照旧读它们（所以它的 `needs` 里必须有 `pytest-report`，否则会在产物上传完成前开始下载，报告静默地少掉各平台的测试结果）。
- 片数出现在三处（`matrix.shard` 列表、`--shard-count`、传给 pytest 的 `--shard-index`）：两边不一致会让**一部分用例静默不跑**，所以 `tests/unit/test_sharding.py` 会校验矩阵与 `--shard-count` 一致、矩阵编号真的是 `0..N-1`。

## 7. 本地生成与查看报告

前置：Allure 3 CLI，与 CI 同一条安装命令（`npm install --global allure@3.18.0` —— 钉住版本，3.13~3.17 的质量门会静默放行，见本节末；本地与 CI 的报告目录结构一致，本地报告包可以直接用上一节的自检脚本核对）。`allure-results/`、`allure-report*/`、`.allure/` 都在 `.gitignore` 里，不会进版本库。

```shell
# 1) 跑本地测试并产出 Allure 结果(目录名与 CI 一致, 后续命令可直接复用)
uv run pytest --alluredir=allure-results

# 2) 由结果生成静态报告
allure generate allure-results --output allure-report

# 3) 生成后直接打开浏览器(等价于 generate 之后再 open)
allure generate allure-results --output allure-report --open

# 4) 打开已经生成好的报告(端口用 allurerc.mjs 里的默认值 8080; 冲突时加 `--port 8081`)
allure open allure-report

# 5) 一步到位: 直接从结果目录生成并打开(不落地产出报告目录)
allure open allure-results
```

报告的标题、界面语言与默认端口都在仓库根的 `allurerc.mjs` 里（`name` / `plugins.awesome.options.reportLanguage` / `port`），所以上面这些命令不需要额外参数：报告标题是 `存档管理 · 测试报告`，界面固定中文（与浏览器语言无关）。另外两个刻意**不**设的选项写在配置的注释里：`open: true`（CI 上会去拉起浏览器）与 `singleFile: true`（会拆掉 `data/test-results/*.json` 这些按需拉的资源，直接打破自检与单 zip 发布）。

只想看某一类用例时，把第 1 步换成对应目录（性能/安全测试必须显式指定）：

```shell
uv run pytest tests/performance -m performance --alluredir=allure-results
uv run pytest tests/security -m security --alluredir=allure-results
```

本地想复现 CI 的分片流程（同一套参数与合并脚本）：

```shell
# 各片一份覆盖率数据与结果目录(单片覆盖率偏低是正常的, 所以关掉门槛)
for k in 0 1 2; do
  COVERAGE_FILE=".coverage.shard-$k" uv run pytest --shard-count 3 --shard-index "$k" \
    --cov --cov-report= --cov-fail-under=0 --alluredir="allure-results-shard-$k"
done

uv run python scripts/merge_allure_results.py --output allure-results "allure-results-shard-*"
uv run coverage combine && uv run coverage report   # 门槛在这里判(合并后的总覆盖率)
allure generate allure-results --output allure-report
```

分片只改变“哪些用例在哪一次运行里跑”，不改收集结果：三片并集与全量收集逐条一致（`--collect-only` 核对过 1068 条），合并后的总覆盖率也与串行一致（91%）。命令行的 `--shard-count` / `--shard-index` 说明见上一节。

也可以用下载下来的 CI 结果（artifact `allure-resources-*` 里的 `allure-results/`）在本地复现 CI 报告，命令与上面完全相同；`allure generate` 之后建议先跑一次自检再打开。

四个容易踩的坑：

- **报告要经 HTTP 提供**：控件数据与用例详情都是前端按需 `fetch` 的相对路径，直接双击 `allure-report/index.html`（`file://`）会被浏览器的跨域策略拦掉，界面只剩加载动画或空壳。用 `allure open`，或任意静态服务器。
- **`allure open` 的目录是必填参数**：只写 `allure open --port 8080` 会直接报错退出，正确写法是 `allure open allure-report --port 8080`。
- **报告目录已存在时 `allure generate` 不会刷新数据**：实测先删一个 `data/test-results/*.json` 再生成，该文件仍然缺失（2634 → 2633）。要重新生成就先删掉 `allure-report` 目录。
- **打开前先自检**（见上一节）：缺 `data/test-results/*.json` 时界面照样显示"通过/失败"，点开用例却是空的。

## 8. 新增测试清单

1. 选对目录：纯逻辑进 `unit`，需要真实文件/数据库协作进 `integration`，规模基准进`performance`，防护类进 `security`。
2. 声明四层 Allure 标签（`epic`/`feature`/`story`/`layer`）。`tests/unit/test_test_config.py`会扫描全部测试模块，缺标签会直接失败。
3. 选择严重等级：按**失败影响面**在模块 `pytestmark` 里声明一个等级（blocker/critical/normal/minor/trivial，判定标准见第 1 节的严重等级表）；需要更细的区分时，给单个用例加 `@pytest.mark.blocker` 等标记。
4. 优先用 `tests/helpers.py` 的构造器，避免在模块里再抄一份临时数据库/游戏数据构造。
5. 性能测试必须给出规模与阈值，安全测试必须用 `security_recorder.expect_blocked`记录结论。
6. 断言消息写成单行：长提示先存进变量（`hint = "..."`），`assert` 本身保持一行。Ruff 与 Black 对"折行的断言消息"排布不同，只有单行写法能让两个工具输出一致（见 [development.md](development.md) 的质量门禁一节）。
7. 本地验证：

```shell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov
uv run pytest tests/performance -m performance
uv run pytest tests/security -m security
```
