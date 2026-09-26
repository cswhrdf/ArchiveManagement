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

| 类别        | 标记                       | 本地 `pytest` | pre-commit               | CI                                           |
| ----------- | -------------------------- | ------------- | ------------------------ | -------------------------------------------- |
| unit        | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（Windows/Linux）                        |
| integration | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（Windows/Linux）                        |
| performance | `@pytest.mark.performance` | **不运行**    | 不运行                   | `quality` job 的一部分（ubuntu，单平台采集） |
| security    | `@pytest.mark.security`    | **不运行**    | 不运行                   | `security` job（Windows/Linux）              |

> **macOS 暂时屏蔽（2026-09-21）**：开发阶段不跑 macOS runner —— 按倍率计费时它是 Linux 的 10 倍。
> 三个平台的矩阵（`pytest` / `pytest-report` / `security`）、`allurerc.mjs` 的 `environmentsTested`
> 与汇总作业的 `--expect-platforms` 都只列了 Windows/Linux，三处由守卫核对着一致。
> **恢复清单**见 `PLAN.md` 第 11.9 节；报告配置里的 `macOS` 环境定义保留着（本地在 macOS 上跑一次就能看到它）。

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

| 文件                         | 覆盖内容                                                                                                                                                                                                                                       |
| ---------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `test_path_escapes.py`       | 清单相对路径越界（`..` / 绝对路径 / 盘符 / UNC / 空值）、危险写回目标判定、篡改清单后的恢复、符号链接不被跟随                                                                                                                                  |
| `test_hostile_input.py`      | 恶意应用配置（未知字段、错类型、越界数值、非法快捷键、版本不兼容）、脏 Steam 数据、审计日志脱敏与截断                                                                                                                                          |
| `test_snapshot_integrity.py` | 快照内容被篡改、文件缺失、清单 JSON 损坏、清单条目类型非法 → 预检标记不可用且恢复被拒绝                                                                                                                                                        |
| `test_side_effects.py`       | **副作用边界**：把进程内的写/删/改名/执行入口全部换成记账版本，跑真实流水线（建库 → 加位置 → 备份 → 恢复 → 删除位置）后断言只动"应用自己的目录 + 用户指定的存档位置"、备份阶段对存档目录**只读**、删除阶段只做"移入回收站"、全程不执行外部程序 |
| `test_outbound_requests.py`  | **出站请求只下行**：真实的封面下载与译名查询跑在假传输上，断言 GET/HEAD、无请求体、无凭据头、主机在允许清单内；另有两条静态扫描（源码里没有写请求、地址都在白名单里）                                                                          |

约定：不使用真实凭据或真实用户文件；涉及"用户主目录 / 盘符根目录"的用例只做**判定**，不执行写入或删除。

**副作用与网络的政策（2026-09-25 明确）**：

- 除"**删除原始存档位置**"这一个由用户显式触发、且要输入游戏名确认的功能（它也只允许删除，且走系统回收站）之外，软件**不得对应用自身目录之外的任何区域做写 / 执行 / 删除**，只读可以。
- 涉及网络请求的**只允许取回内容，禁止外发内容**：只发 GET/HEAD、无请求体、不带授权/Cookie 之类的凭据头，且目标主机必须在允许清单内（公开的 Steam 图片 CDN 与公开商店接口）。
- 这两条由 `test_side_effects.py` / `test_outbound_requests.py` 分别用"动态记账 + 静态扫描"两层守住：动态用例证明真实流水线走出来的路径合规，静态扫描防止将来新增一条没人跑到的旁路（例如直接 `subprocess.run` 或换一个 HTTP 客户端）。

结果写入 `security-results.json`（场景 / 输入摘要 / 期望 / 实际 / 是否拦截），同样汇总进 Allure；未拦截即用例失败，因此安全回归会直接体现在 CI 状态上。

**结论项要能体现失败（2026-09-25 补）**：安全用例只在 `security` 作业里跑（不在 pytest 分片里），所以它的成败原本只体现在那个作业的状态上，而报告里的 `Security findings` 汇总项永远是绿的 —— 实测那次 Linux 有 1 条安全用例失败（2641 条结果里唯一一条 `failed`），报告里却完全查不到。现在这项结论按两条判据定状态：**未拦截的结论**（`blocked` 非真）与**真的失败的安全用例**（按 `layer=security` 与 `env` 标签逐平台统计，`broken` 也算）；任一条命中就写 `failed`（原因进 `statusDetails.message`），运行总账里那行也给出"失败用例数 + 通过/未通过"，原生质量门会跟着红。守卫：`tests/unit/test_report_verification.py`。

**记账器要还原 `dir_fd` 相对路径**：`shutil.rmtree` 在支持 `dir_fd` 的平台上（POSIX）是"打开目录 + `os.unlink(条目名, dir_fd=fd)`"逐个删的，记账器若直接记裸文件名，一次**合法**删除会被判成越界变更（2026-09-25 的 Linux CI 现场：`越界变更: [_Call(phase='restore', kind='delete', path='slot.dat')]`；Windows 不支持 `dir_fd`，所以本机一直是绿的）。现在按 `/proc/self/fd/<fd>`（Linux）或 `/dev/fd/<fd>`（macOS）把相对基准接回去，读不到时**原样返回**（宁可响亮地判越界，也不静默放行）；`test_side_effects.py` 里有一条对应的自检用例（`dir_fd` 不支持的平台会明确 skip）。

## 5. 结果与报告元数据

- 每条性能测量记录：规模、指标、取值、单位、阈值、比较方向、是否通过。
- 每条安全结论记录：类别、场景、输入摘要、期望拦截行为、实际结果、是否拦截。
- 环境信息记录：操作系统与平台族、Python 版本与实现、提交 SHA、分支与 CI run id、测试类别是否执行、覆盖率门槛（`scripts/create_allure_summary.py` 写入`allure-results/environment.properties`）。
- 覆盖率摘要由 `scripts/create_allure_coverage.py` 生成；性能与安全结果由`scripts/create_allure_summary.py` 生成，原始 JSON/CSV 作为附件保留，保证结论可下载、可追溯。覆盖率摘要项同样**把原始 `coverage.xml` 作为附件**带进报告（脚本按 `RAW_REPORT_FILES` 逐个收集，存在哪个带哪个：以后加 `coverage.json` 或把 `--cov-report=term-missing` 的输出重定向成文件，不改代码就会一并附上）；HTML 报告是整站，仍旧作为 `coverage-<os>` artifact 上传。
- **平台以 Allure 的"环境"维度呈现**（这是看出"结果来自哪台机器"的主路径）：每个用例都会写入 `env` 标签（取值就是平台展示名），仓库根的 **`allurerc.mjs`** 用 matcher 把它映射成 Allure 3 的环境。于是一份合并报告里会出现 `Windows` / `Linux` 两个环境（macOS 屏蔽期间；环境选择器、用例详情页的「环境」分页都在这个维度上），而不是只能从参数或套件名后缀里去认平台。
  另保留两样兜底：`平台` 参数（平台也进结果身份：三个平台的同名结果 `retryHash` 各不相同、`isRetry` 均为 `false`，不会互相并成重试；`historyId` 共享，所以历史趋势能连上）与 `os` 标签 + `parentSuite` 后缀（筛选与只认 suite 标签的控件）。
  **生成报告必须在仓库根目录执行**（CI 与本文档的命令都是如此）：环境不会仅因结果带 `env` 标签就生效，CLI 得读到 `allurerc.mjs` 才会识别；读不到时环境会静默退回单个 `default`，`scripts/verify_allure_report.py` 会把这种退化判为报告不完整（它同时打印 `环境: ...` 一行）。性能/安全/覆盖率摘要项也按同一规则处理：带 `env` 标签、**标题不再拼平台名**（三个环境里的标题完全一致，都是 `Coverage report` / `Performance baseline` / `Security findings`），平台由环境表达；`平台` 参数与 `os` 标签作为兜底（与用例结果一致）。
- 用例标题会还原 pytest 对参数化 id 做的 ASCII 转义（`\u7528\u6237` → `用户`），并写在 `@allure.title` 使用的同一属性上（`allure.dynamic.title` 会被 allure-pytest 用 `item.name` 覆盖）。

## 6. CI 流程

```text
quality (ubuntu: 公共检查 ruff check / ruff format / mypy → env=common；静态分析 deptry / bandit /
         pip-audit / radon+xenon 与性能基准也在同一个作业里依次跑)
pytest  (Windows 2 片 / Linux 3 片: 单元 + 集成 + 各片自己的 Allure 结果与覆盖率数据；
         Windows 的**片 0** 另外跑 mypy --platform win32 → 结论归入 Windows 环境)
security    (Windows/Linux: 越权与危险操作防护)
      ↓
pytest-report (每平台一份报告: 合并各片的 Allure 结果与覆盖率,
               **两个平台都在 ubuntu 上生成** → 生成并自检报告)
      ↓
allure-summary (合并全部 allure-results-* → 写入环境信息与质量/性能/安全汇总 → 生成最终报告)
```

**作业数量也是额度**：一轮 CI 是 11 个作业实例（`quality` 1 + `pytest` 5 + `pytest-report` 2 + `security` 2 + `allure-summary` 1），每个实例都要重付一遍 checkout / uv / 依赖同步的固定开销，所以"与平台无关的检查合到一个作业里""平台专属的检查塞进已有平台作业"都是为了少付这笔钱（2026-09-21 档 1：把 `analysis` 并入 `quality`、把 `quality-platform` 折进 `pytest` 的片 0，21 → 18；档 2：把 `performance` 并入 `quality`、报告作业改到 Ubuntu 上跑并按平台给片数，13 → 11，并省掉 Windows runner 的 2 倍计价）。另外两条省额度的约定：连续 push 用 `concurrency` 取消被取代的运行（被取代的那一轮连报告作业也不再启动 —— 报告作业的条件必须是 `always() && !cancelled()`，只写 `always()` 等于“被取消也照跑”，见 `PLAN.md` 第 11.10 节）；`paths-ignore` 只加在 **push** 上 —— PR 被路径过滤跳过会让分支保护里的必需检查永远停在 pending，反而合不了 PR。完整的优化清单与取舍记在 `PLAN.md` 第 11 节。

每个作业都上传自己的 `allure-results-*`，汇总作业用`actions/download-artifact` 的 `pattern` + `merge-multiple` 合并后生成唯一报告，并沿用 `.allure/history.jsonl` 累积历史。

**每个作业只装自己需要的依赖**：开发依赖拆成 `test` / `coverage` / `quality` / `analysis` / `package` 五组（见 `pyproject.toml` 的 `[dependency-groups]`），作业按自己跑的命令装对应组（`uv sync --locked --no-default-groups --group ...`）—— 跑用例的作业不再顺带下载 bandit / pip-audit / pyinstaller。两处容易踩空的地方：① 顶层必须设 `UV_NO_SYNC=1`，否则 `uv run` 会先按**默认组** sync 一次，把整套依赖又装回来（实测在只装了测试组的临时环境里跑一次 `uv run pytest`，uv 装回 51 个包）；② 同一作业里的每次 `uv sync` 必须带同一组（Tkinter 修复步骤那一次也会 sync，少写一组会把刚装好的组删掉）。本地不受影响：`[tool.uv] default-groups` 覆盖全部组，所以 `uv sync` 之后所有工具都在。守卫 `test_ci_installs_only_the_dependency_groups_each_job_needs` 按"命令 ↔ 组"核对（改命令时自动跟着要求对应的组）。

质量门禁本身也由脚本执行：`scripts/create_allure_quality.py --group <组>` 依次跑该组的检查，把每项的退出码、结论与**完整输出附件**写成 Allure 结果（任一项未通过时脚本以非 0 退出，作业照常红）。它分三组：`core`（ruff check / ruff format --check / mypy 宿主平台那一次 → `env=common`）、`analysis`（deptry / bandit / pip-audit / radon / xenon → `env=common`）与 `platform`（mypy 的 `--platform win32` / `--platform darwin`）。**前两组与性能基准在同一个 Ubuntu 作业（`quality`）里依次跑**：它们都与平台无关（不涉及路径分隔符、显示或字体；性能基准也需要固定的运行环境，所以固定在这一个平台上采集），分成更多作业只是多付几套固定开销。`platform` 组**各自在那个平台上执行**，位置是 `pytest` 作业的**片 0**（当前仅 Windows —— macOS 屏蔽期间 `--platform darwin` 这一支不再被执行，代价是 darwin 专属分支暂时没有类型检查覆盖，恢复清单与"改在任意平台上跑并归入 `common`"的备选方案见 `PLAN.md` 第 11.9 节；放在测试与上传之后，免得门禁失败让这次的测试结果拿不到）—— `--platform` 只是"检查哪支代码"，并不校验执行环境，在 Ubuntu 上跑出来的结论挂到 Windows 环境里就是假的归属。**结论归入哪个环境分两种**：与平台无关的检查带 `env=common`，归入 `allurerc.mjs` 里**显式声明**的 `Common` 环境（不是某个平台的环境，也不是隐式的 `default`）；两条平台专属 mypy 检查带对应平台的 `env`，归入报告里那个平台的 `Windows` / `macOS` 环境 —— 它们验的就是那个平台，**而且真的在那台机器上跑**（`Check.host_platform`），所以“标着 Windows 的结论一定产自 Windows”是结构上的事实：`--platform` 只是“检查哪支代码”，不代表执行环境，放在 Ubuntu 上跑虽然也能过，但环境归属就是假的。放在环境选择器里能与该平台的测试结果一起看（执行主机只写进描述，二者不混）。脚本会自己挑适用的一支：显式点名一个在当前平台跑不了的分组时以退出码 2 报错（默默跳过等于这道门禁不存在）。顺便说明为什么宿主平台那次 mypy 仍在 `Common`：公共检查的判据是“**结论本身与平台无关**”，不是“跑在哪台机器上” —— 除开两条平台专属分支（另有 `platform` 组专门验）之外，那次 mypy 在哪个平台上跑都是同一个结论，所以它归 `Common` 而不是 `Linux`；平台专属那两条则相反，它们表达的就是“某平台的代码路径类型对不对”。

除此之外，汇总作业还会跑一次 **Allure 原生质量门**：`allure quality-gate --config allurerc.mjs allure-results`。规则写在 `allurerc.mjs` 的 `qualityGate.rules` 里，管的是整次运行，与逐项检查互补；它的退出码直接决定作业成败，输出写进 `allure-quality-gate.txt` 并由运行总账收进报告首页「全局附件」。规则分两条规则集：第一条不过滤（`maxFailures: 0` / `successRate: 0.98`），脚本生成的结论项也算在内 —— 否则“覆盖率项 broken”这类失败就没人管了；第二条**只看真实用例**，要求每个跑测试的平台都有用例（`filter` 选出带 `framework=pytest` 标签的结果再 `environmentsTested`；当前是 `Windows` / `Linux`，与 CI 矩阵、汇总作业的 `--expect-platforms` 三处一致，守卫会核对）。

**为什么要用环境维度、不用 `minTestsCount: 3000`**：绝对计数会随用例规模往**更松**的方向漂 —— 实测签名是 `3P+154`（每平台 P 条用例），每平台涨到 1400 上下之后，即使缺一整个平台的产物也仍然高于 3000，规则静默失效且没有任何信号（“常量失效时没人知道”正是这类规则最难查的地方）。环境维度不随规模变化：只带汇总项的环境不算“测过”（实测 3.18.0 的规则集级 `filter` 对 `environmentsTested` 生效）。判据用的是 **`framework=pytest` 这类正向标记**而不是“不能带 `testCategory`”这类反向排除：正向判据漏判时**会红**，反向判据漏判时**会绿**（将来某个脚本忘了打标签，它的汇总项就会被当成真实用例）。同一道不变式在仓库自检脚本里也有一份（`--expect-platforms`，见上一节）。**它管不到"少一片"**：那条属于"部分漏收"，由产物清单负责（`--manifest`，见第 6 节的分片段与自检段）。**CLI 版本必须 ≥ 3.18.0**：3.13~3.17 在配了 `historyPath` 时会静默放行（退出 0 且不输出任何内容 —— 根因是本地历史流的句柄悬空，`AllureReport.done()` 永不返回，Node 在校验前就退出了，见 issue [#895](https://github.com/allure-framework/allure3/issues/895)，修于 3.18.0 的 PR #962），所以 CI 把 CLI 钉在 3.18.0。本地复现：`npx allure@3.18.0 quality-gate --config allurerc.mjs allure-results`（单平台跑会因 `environmentsTested` 失败，属预期）。

### 跳过只留给已知的环境问题

GUI 用例的环境守卫会把建窗口期的 `TclError` 写成 `pytest.skip("tk 环境不可用: ...")`。本仓库为此吃过一次亏（2026-09-25）：`test_poster_markers_only_get_a_backing_over_a_real_cover` 在 CI 上稳定抛 `TclError: image "pyimage1" does not exist`，于是 Linux 与 Windows **都**把它跳过了 —— 报告里只看到"跳过了"，用例等于不存在，而跳过又不会让任何东西变红（那条用例已经删掉，它验的规则改由不建窗口的单测钉住：`tests/unit/test_ui_widgets.py::test_poster_backing_uses_the_panel_colour_only_over_a_real_cover`）。

现在的规矩（见 `tests/tk_guard.py`）：

- **只有已知的环境症状**才允许跳过：解释器加载不了 Tcl/Tk 库数据（`tcl_findLibrary` / `init.tcl` / `tk.tcl` / `Can't find a usable …`），或者根本没有显示环境（`no display name` 等）。前一组只可能由 **Tcl 自己**在装库时说出（即使用例完全不碰文件也可能撞上），与用例断言的东西无关；`tk.tcl` 这一条是 2026-09-26 补上的 —— 现场是 `test_gui_buttons.py` 整模块跑时偶有一条用例在建 Tk 根时报 `couldn't read file <...>/tk.tcl`，单跑必过，且把新增用例全部 deselect 后仍会复现。
- **白名单只列 Tcl 自己的库数据文件名**：把 `couldn't read file` 或 `can't find a usable` 这类通用说法整个收进来，会把“应用自己要读的文件打不开”（例如某张封面图找不到）也归成环境问题 —— 那正是这一节开头记的那个错误。两种拼法都能被文件名本身命中（`Can't find a usable tk.tcl` 命中 `tk.tcl`），所以不必放宽。
- 原因前缀是 `tk 环境不可用` 但症状不在清单里时，`tests/conftest.py` 的用例报告钩子**把它改成失败**并给出判定依据 —— 要么修掉，要么删掉那条用例，不留下永远不跑的用例。
- `tests/gui_support.gui_app` 同理：非已知抖动的 `TclError` **不重试**，直接抛出（重试也修不好）。

与 Tk 无关的跳过不受影响（例如"当前环境不允许创建符号链接""虚拟显示器太小"）。守卫：`tests/unit/test_gui_retry.py`（含一条反向守卫：`couldn't read file "cover.png"` 这类应用侧读文件失败**不许**被当成环境问题）。

### 只在某个平台红的分片失败：先把那份平台差异搬回本地

2026-09-25 实测：Linux 分片里 `test_demo_update_location_rejects_a_path_used_by_another_location` 报 `DID NOT RAISE`，同一条用例在 Windows 上是绿的。根因不是平台 bug，而是**判重只规范化了一侧**：演示数据里的位置路径是原样保存的展示字符串（`D:\Games\…`），而输入侧会过一遍 `normalize_path` —— POSIX 上这类字符串属于相对路径，会被拼上工作目录，两侧形态不同就永远比不出重复；Windows 上 `normpath` 对这类路径恰好幂等，于是"恰好"看不出来。修法是判重**两侧都规范化**（`add_location` / `update_location` 两处，与监控目录判重同一套做法）。

补的守卫刻意不依赖平台：用例把 `normalize_path` 换成 **POSIX 风格的替身**（`posixpath.normpath` + 手写的工作目录前缀），于是 Windows 上也能复现 Linux 的形态差异（`tests/unit/test_ui_demo.py::test_demo_duplicate_locations_are_caught_for_unusual_path_forms`）。判据是"去掉修法后这条用例必须在**本机**就红"—— 实测在 Windows 上换回旧写法立刻复现了 CI 的那条 `DID NOT RAISE`。

**通用做法**：遇到"只有某个平台红"的失败，先定位它依赖哪一种形态差异（路径分隔符与大小写、`normpath` 是否幂等、`dir_fd` 相对路径、显示服务、字体度量），再把那个差异做成替身搬进用例 —— 否则这条用例永远只在 CI 的一半平台上有效。

### 同一路径只有一条位置：判重靠"落库即规范化"

`SaveLocationRepository.duplicate_of` 比的是**字符串相等**（`LOWER(path) = LOWER(?)`），它自己不做任何路径规范化 —— 等价于"同一个文件夹"的前提是**两侧都已经是规范化形式**。这条不变式靠写入侧维持：手工新增、改路径、导入确认（`add_location` / `update_location` / `confirm_candidate`）都先过 `normalize_path`。

它一旦被破坏，失败是**静默**的：`…/saves`、`…/saves/`、`…/saves/.` 会被当成三个不同路径，同一个目录登记成多条位置（备份拍两份、恢复写两次、删掉其中一个还会把另一个位置的目标一起带走）。演示后端就是这么漏判的（见上一节）。

三条守卫：

- `tests/unit/test_sql_backend.py::test_every_stored_save_location_path_is_normalized`：走完三条写入路径后，断言库里每行 `path == normalize_path(path)`；
- `tests/unit/test_sql_backend.py::test_the_same_folder_in_another_spelling_is_rejected`：服务入口（新增/改路径）试"同一文件夹的另一种写法"；
- `tests/unit/test_save_candidates.py::test_confirming_a_candidate_written_differently_keeps_one_location`：候选行里存的是另一种写法时，确认不得多出一条（去掉写入前的规范化就会红）。

### GUI 用例必须真的跑起来（skip 是有代价的）

`tests/integration/test_gui_*.py` 会把"Tk 起不来"当作环境问题处理：文件级守卫与每个用例的 `except TclError: pytest.skip(f"tk 环境不可用: ...")`。这样没有显示环境的机器不会一片红，但**代价是真正的 Tcl 故障会伪装成一堆 skip**，而 GUI 用例占覆盖率的很大一块——本机实测（把 `TCL_LIBRARY` 指向不存在的目录来模拟）：全量 `769 passed / 68 skipped`，覆盖率 **67.88% < 85%**，作业会以覆盖率门槛失败，且失败信息里看不出 CLI 之外的原因。

所以 CI 在 Windows 上（macOS 屏蔽期间只剩它）、pytest 之前多跑一步显式自检：

```shell
uv run python -c "import tkinter; root = tkinter.Tk(); root.destroy(); print('Tkinter OK')"
```

Tcl 正常时它只是一行输出；坏掉时会打印 `Can't find a usable init.tcl ...` 并以非 0 退出，直接把原因抬到表面（Linux 的显示环境由 `xvfb-run` 提供，不重复检查）。

这条曾经真实发生过：跑测试的解释器是 `uv python install 3.12` 下载的 uv 托管 standalone 构建，它靠**自身目录里的 tcl 数据文件**定位 Tcl（`<托管目录>/tcl/tcl8.6`），那份副本一旦陈旧或不完整，`tkinter` 就报 `Can't find a usable init.tcl`（上游是 python-build-standalone 的已知怪癖，见 astral-sh/uv#7036；本机 venv 用的是 python.org 的 CPython，自带完整 tcl，所以本机复现不出来）。既然重建解释器就能修好，工作流里用 `uv python install --reinstall 3.12`，而不是事后设 `TCL_LIBRARY`/`TK_LIBRARY`——后者路径随平台变，还会影响其它 Tcl 使用者。

**但不必每次都重装**（一次约 30MB 下载 × 9 个 pytest 作业），所以流程是"先普通安装 → 自检失败才重装"：

1. `uv python install 3.12`（命中 uv 下载缓存时很快）；
2. Tkinter 自检（`id: tkinter`，`continue-on-error: true` —— 失败不等于作业失败，只是给一次修复机会）；
3. 只有上一步**失败**时才走修复：`uv python install --reinstall 3.12` → `uv sync --locked`（让虚拟环境跟着新解释器走）→ **再复检一次**。这一步没有 `continue-on-error`，所以修不好照样红 —— "Tcl 坏掉不能静默变成一堆 skip"这条不变式没有被削弱，只是从"每次都重装"变成"失败才重装"（守卫会把这条语义钉住：自检的 id、修复的触发条件、修完的复检缺一不可）。

### GUI 布局用例不要和"首帧时序"赛跑

名称按宽度重裁这类断言最容易在平台之间飘：`winfo_width()` 在首帧往往只有 1（各平台完成布局的时机不同），而且可能**不再有尺寸变化事件**来补救，于是名称停在"兜底宽度"的短文本上 —— 人眼看不出来，只有断言能发现。产品侧对这种"宽度还没量出来"的情况安排了有上限的重试（`home_page._REFIT_MAX_ATTEMPTS`）；用例侧两条纪律：

- 判定"布局停在上一次宽度"这类回归**用假控件**（只实现 `_text` 与 `winfo_width()` 的替身），而不是在真窗口上改完文本再泵事件循环：后台重裁会在不同平台上以不同时机把文本改回去，自检会时灵时不灵。`test_gui_layout.py` 里两条用例正是这么分工的：一条跑真布局，一条用假标签验看门狗本身。
- 断言"当前宽度下应当截成什么"时别写死像素或字符数：用同一个 `fit_text(full, font, label.winfo_width())` 生成期望值，比对**文本**（与字体是否缺字形无关），像素只用来卡"缝隙上限"。

**重裁本身还必须保证"一定会发生"，而且不能靠"等够多少秒"去赌**。它有两层触发机制，缺一层就会漏：

- **容器事件**（滚动区 `<Configure>`）负责"可用宽度变了"这件事，走延后 60ms 的**合并**式排队：已排过队就不再排，也**不撤销**已排的那个（`HomePage._schedule_list_sync`、`ArchiveApp._on_content_resize`）。改成"每次请求先取消再重新计时"看起来更平滑，但触发源是**连续**的尺寸事件（无窗口管理器的 Xvfb、CustomTkinter 的延迟重绘），任务会被无限推后、永远轮不到执行。判据：触发源**离散**（用户按键、单次点击）才可以用"取消重排"的 debounce（如设置窗口的录制收尾计时器）；触发源**可能连续**，就必须合并而不是推后。
- **标签自己的 `<Configure>`** 负责兜住容器事件不来的情况：内宽变了而外宽没变（滚动条出现/消失、表头内边距被重算）、行刚重建且标签刚量到真实宽度时，滚动区都不会发事件。所以名称标签直接盯自己的宽度，量到就**立刻**按它裁一次（`HomePage._on_name_resize` → `_fit_row_name`，宽度没变则不重写文本，避免"改文本 → 新事件 → 再裁"互相追）。这两层合起来才让"名称按当前宽度显示"成为**不用等多久**就会成立的事实 —— 而不是"恰好没被下一次事件推后"的运气。

对应地，**用例这边不要和这段延迟赛跑**：名称的显示被两个延迟源影响（重裁任务延后 60ms；行还会因为异步加载落地而**整体重建**，重建后先回到兜底宽度的短文本）。所以断言前先等**不变量本身**成立（显示文本 == 该宽度下的 `fit_text` 结果，见 `_wait_until_the_name_fits`，超时才失败），而不是泵固定时长的事件循环 —— CI 上真实挂过：Linux 上异步数据到得晚，断言正好落在"新行刚建好、重裁还没轮到"的那一瞬间，报出来的字数与标签宽度对不上（现场 dump 里 `_sync_job` 还挂着、`_refit_attempts` 才 1）。契约守卫见 `tests/unit/test_ui_scheduling.py`（用替身控件驱动真实的调度与裁剪方法，不建窗口）。

### 报告自检与发布（不要跳过）

报告是一套静态站点：用例详情页打开时才去取`data/test-results/<结果 id>.json`。这个目录一旦在传输或解压环节被丢掉，报告就只剩汇总与用例树——界面能看到用例通过与否，点开用例却是空的（生成阶段本身没问题，用同一个 allure 版本本地生成就有这些文件）。

因此每个生成报告的作业在发布前都要跑 `scripts/verify_allure_report.py`：

- 检查入口资源：`index.html`、单个 `app-*.js`、`summary.json`、`test-results.json`、`widgets/**/statistic.json`、`widgets/**/tree.json`；
- 逐条核对结果索引（`test-results.json` 的 `byId`）引用的详情文件是否存在，并确认条数与 `allure-results` 里的结果文件一致；
- 校验用例分组（`data/test-env-groups/*.json`）引用的结果 id 都在索引里；
- `--expect-platforms` 指定的每个平台环境里都要有**真实用例**结果（判据 `framework=pytest`，与质量门那条规则同一个判据）；
- 传了 `--manifest` 时，还要把"分片自报的条数"与最终条数对上（见下）。

任一项不通过即作业失败，且**不发布报告**（`Upload Allure report` 只在自检成功时执行）；标准输出里的"结果索引 / 详情文件 / 用例分组"三个计数就是排查入口。自检通过后用 `--zip` 把报告打成单个 `allure-report.zip` 发布：单文件要么完整到达、要么直接报错，不会出现"整个目录被悄悄丢掉"的半损坏状态（改动前的 artifact 就踩过一次）。

**平台用例这一项是唯一不拦发布的**（`continue-on-error: true`，结论由汇总作业末尾的门禁结论步骤接手）：它属于**内容**问题而不是"报告坏了"，而报告正是用来看"哪个平台没数据"的地方 —— 拦下来反而拿不到证据（分片作业全挂时更需要看到报告）。两处接线：

- `pytest-report` 作业传 `--expect-platforms "${{ matrix.platform }}"`（平台来自矩阵，不是 `runner.os` —— 两个平台的报告都跑在 Ubuntu 上），逐平台自查；
- 汇总作业传 `--expect-platforms Windows,Linux`，在生成完最终报告之后运行（它不拦发布，结论由末尾的门禁结论步骤接手），输出里的 `按平台用例: Windows 1155 用例 + 2 汇总项, ...` 就是"哪个平台只剩汇总项"的直接证据。

为什么它能发现质量门的 `environmentsTested` 发现不了的事：覆盖率/安全汇总项、平台专属的质量检查都带平台的 `env`，所以"环境存在"不等于"这个平台测过"。两道都在（Allure 规则 + 仓库自检）是有意的 —— 后者能逐平台报数，也不会因为 CLI 升级后 `filter` 语义变化而静默失效。已验证（2026-09-21，用真实报告重建的 3493 条结果）：删掉 Linux 的 1166 条用例后，自检报 `这些平台里没有用例结果: Linux (脚本生成的汇总项不算用例; 逐平台: ... Linux: 0 用例 / 3 汇总项)`，质量门报 `tests-on-every-platform/environmentsTested`。

Windows runner 的控制台是 cp1252：Python 默认按该编码输出，**打印中文会直接 `UnicodeEncodeError` 打断步骤**（报告自检在 CI 上踩过）。因此工作流最外层设了 `PYTHONUTF8=1`，两个报告脚本自己也会把标准输出切成 UTF-8（取不到 `reconfigure` 的替身如 pytest `capsys` 就跳过）。新增会向终端打中文的脚本时注意这条。

下载 artifact 后本地核对（先解压外层 artifact，再解压其中的 `allure-report.zip`）：

```shell
uv run python scripts/verify_allure_report.py allure-report          # 只自检
uv run python scripts/verify_allure_report.py allure-report --zip    # 自检并重新打包
uv run python scripts/verify_allure_report.py allure-report --expect-platforms Windows,macOS,Linux
uv run python scripts/verify_allure_report.py allure-report --results allure-results --manifest "allure-manifests/*.json"
```

`--manifest` 的三条判据（都在"内容检查"那一侧，不拦发布）：声明必须有的片号都到了、各分片自报的结果数**不超过**结果目录里的结果文件数（超了说明合并之后掉过数据）、报告里每个平台的用例数**不少于**该平台分片自报的结果数。输出里的 `分片产物清单: Linux 725 条结果(2 片), 缺片 1` 就是缺片的直接证据。

### 失败现场留证（dump + 界面截图）

只在 CI 出现、本地怎么跑都不复现的失败，事后能拿到的往往只有一行 traceback。所以用例失败时会自动把现场挂到**该用例的 Allure 结果**上（`tests/crash_capture.py`）：

- **崩溃现场 dump**：`coredumpy` 把最深一层栈帧的局部变量与对象属性写成 `crash-dumps/<用例>.dump`（在 `.gitignore` 里），附件与摘要都会写明落点。本地打开：`coredumpy load <文件>`（进 pdb），或在 VSCode 里用 coredumpy 扩展右键「Load with coredumpy」；只想知道哪个 dump 是哪条用例，用 `coredumpy peek crash-dumps`。
- **界面截图**：本用例创建的窗口（`tests/gui_support.py` 的登记表）在失败时的画面。Windows 走 `ImageGrab.grab(window=hwnd)` 按**窗口句柄**抓：窗口被别的窗口盖住（全屏游戏、多个用例窗口叠放）也拍得到，也不受显示缩放影响（Tk 报逻辑坐标、按屏幕区域抓拿的是物理像素，缩放不是 100% 时会错位；本次就是用这条修掉的）；Linux 按屏幕区域抓（需要 `DISPLAY`，CI 由 xvfb 提供），macOS 同（需要屏幕录制权限）——抓不到时只在摘要里写一句原因，绝不影响用例结果。
- **失败现场摘要**：平台 / Python / 提交号 + dump 与截图落点 + 复现命令，让报告里不只有一堆附件。

三条纪律写在模块注释里：留证**绝不改变用例结果**（每一步各自兜底，整段编排外面还有一层，出错只打一行日志）、**失败才留证**（通过的用例不产生任何文件）、**有上限**（递归深度默认 5、单次 dump 时限 20s、超过 25 MiB 的 dump 只记落点不挂附件、最多 3 张截图，同一用例只留一次——失败后 teardown 常跟着再报一次错）。参数：`--crash-dump-dir`（默认 `crash-dumps`）与 `--crash-dump-depth`（`0` = 关掉 dump，仍留摘要）。

报告侧不需要额外配置：附件由 allure-pytest 写进 `allure-results`，随 `allure-resources-<平台>` artifact 上传，`scripts/verify_allure_report.py` 会把它们一并核对（缺附件即报告不完整）。**dump 里是真实的局部变量**（coredumpy 默认会遮掉像密钥的字符串与 `os.environ` 的值）—— 把报告或 artifact 发给仓库以外的人之前先看一眼附件。

### 分片执行与结果合并

套件变长后，CI 的墙钟时间几乎全压在 pytest 上（2026-09 实测：Windows 298s / macOS 260s / Linux 132s，整次工作流约 9 分钟）。现在每个平台把用例拆成几片并行跑（Linux 3 片、Windows 2 片），再由 `pytest-report` 把各片结果合并成一份——墙钟时间只取决于最慢的那一片。

- **分片规则**在 `tests/sharding.py`：先给目录经验权重（集成 2s、安全 0.6s、单元 0.05s，未知目录 0.3s），再“最慢的优先”贪心装箱（LPT）。套件耗时几乎都在 GUI 用例上，只按**条数**平分会把慢的全堆在一片；实测三片 71s / 81s / 81s（理想 77s）。
- **参数**是 `--shard-count` / `--shard-index`（默认 `1`/`0` 即不分片），过滤发生在**严重等级过滤之后**：本地 `--min-severity=critical` 选出的子集也能分片跑。三条性质由 `tests/unit/test_sharding.py` 锁住：不重不漏（各片并集 == 全集）、同输入同分片、各片权重接近理想值。
- **不要用 pytest-xdist 代替分片**：本机实测 `-n 4` 让 `tests/unit` 从 39s 降到 21s，但 `tests/integration` 没有收益（222s），`tests/integration/test_gui_buttons.py` 反而从 143s 变成 174s，并多出 Tk 初始化失败（`invalid command name "tcl_findLibrary"`）。GUI 用例各自起真实窗口，并行只会互相拖慢；分片是**进程级**并行（CI 上还是**机器级**），不碰这个坑。
- **合并**在 `pytest-report`（每平台一份，但都跑在 Ubuntu 上）：`scripts/merge_allure_results.py` 把各片结果目录搬进一份 `allure-results`（日志逐片给文件数，少一片能一眼看出来）；覆盖率用 `COVERAGE_FILE=.coverage.shard-<片>` 分片写，再 `uv run coverage combine` 合成一份。
- **报告可以在别的平台上生成**：合并、覆盖率汇总、报告生成与自检全是纯文件操作，与产出数据的机器无关，所以两个平台的报告都在 **Ubuntu** 上生成（Windows runner 要按 2 倍计价，而结论完全一样；顺带那批 pwsh 分支也不用维护了）。"结论算哪个平台的"因此改由矩阵参数决定（`--platform` / `--expect-platforms`），**不能再看 `runner.os`** —— 那两台机器都是 Linux。守卫 `test_report_job_does_not_depend_on_the_host_platform` 盯着这一点。
- **跨平台合并覆盖率数据靠 `relative_files = true`**：数据里记的是**相对工作目录**的文件名（各平台的作业与报告作业都从仓库根跑），Windows 记下的是 `src\archive_management\app.py`，`coverage combine` 会把分隔符换成本机的那一种并归一到真实文件；覆盖率数字与平台无关，只有"哪台机器产的"不同。少了这个设置会变成"合并成功但一个文件都对不上"（报告空掉，而且只在 Linux 上才暴露），所以 `tests/unit/test_test_config.py` 造一份反斜杠形式的数据真跑一次 combine。注意数据必须**按平台分开**合并与判门槛（两个平台的数据混进同一个数据文件，某个平台掉了一半数据也看不出来）。
- **产物清单（`--manifest`）是"少一片"的唯一判据**：少一片时合并照常成功，报告只是安静地少一部分用例 —— 环境、通过率、格式自检、甚至 Allure 原生质量门的 `environmentsTested` 全都看不出来（2026-09-21 实测：删掉 Linux 的一整片 382 条用例后，质量门 `exit 0`）。所以合并时同时写一份 JSON：逐分片的文件数与**结果**条数、合并合计、以及 `--expect-shards 0,1,2` 声明必须有而实际没找到的片号。它随 `allure-resources-<平台>` 上传，汇总作业收集成 `allure-manifests/*.json`，由 `scripts/verify_allure_report.py --manifest` 与最终条数对齐（见第 6 节的自检那段）。清单给的三个数分别是：各分片自报的结果数、`allure-results` 里实际的结果文件数、报告里各平台的用例数。
- **覆盖率门槛只在合并后判**：单片覆盖率天生偏低，所以分片作业用 `--cov-report=`（关掉报告）与 `--cov-fail-under=0`（关掉门槛），合并后单独一步跑 `uv run coverage report`（阈值仍取 pyproject 的 `[tool.coverage.report] fail_under`）。之所以单独成步：原生命令的非零退出码只有作为该步**最后一条**命令时才会让作业失败，混在一起写会让门槛静默失效。
- **产物名不变**：`pytest-report` 上传的仍是 `allure-resources-<runner 镜像名>` / `coverage-<runner 镜像名>` / `allure-report-<runner 镜像名>`（名字里带的是哪个平台的**产物**，不是生成它的机器——两台报告作业都在 Ubuntu 上），汇总作业照旧读它们（所以它的 `needs` 里必须有 `pytest-report`，否则会在产物上传完成前开始下载，报告静默地少掉各平台的测试结果）。改名会让各平台报告的历史曲线清零，所以保持不动。
- **片数按平台给**（Linux 3 片、Windows 2 片）：某平台总时长 ≈ 片数 × 固定开销 + 串行测试时间 T，所以减片省额度、加片省墙钟——便宜的平台多开片，贵的平台少开片。矩阵因此写成 `include` 逐条列（`os × shard` 两个轴表达不了"各平台片数不同"），片号必须从 0 连续编到"片数-1"。片数出现在四处（矩阵条目的 `shard`/`shards`、`--shard-count`、传给 pytest 的 `--shard-index`、报告作业的 `--expect-shards`）：不一致会让**一部分用例静默不跑**或清单声明一个没人跑的片号，所以 `tests/unit/test_sharding.py` 会逐平台校验。
- **依赖缓存只让片 0 写**（`save-cache: ${{ matrix.shard == 0 }}`）：同一平台的各片算出的 cache key 完全相同，并行保存时只有第一个能抢到，其余片会打印 `Failed to save: Unable to reserve cache ... another job may be creating this cache`（分片上线后三个平台都出现过）。写入者按平台唯一，其余片与其它作业全部 `save-cache: false`（只读复用）—— 守卫会盯住这条不变式。

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

本地想连产物清单一起核对（片号从目录名末尾认，所以目录名要以片号结尾）：

```shell
uv run python scripts/merge_allure_results.py --output allure-results \
  --platform Linux --expect-shards 0,1,2 --manifest allure-manifest.json "allure-results-shard-*"
uv run python scripts/verify_allure_report.py allure-report --results allure-results \
  --expect-platforms Linux --manifest allure-manifest.json
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
