# 测试体系

本文档说明测试如何分类、写用例的通用纪律，以及新增测试的收尾清单；界面用例的判据与坑在 [testing-ui.md](testing-ui.md)、CI 流程与报告在 [testing-ci.md](testing-ci.md)，目标是让"本地提交要快"与"CI 有完整证据"同时成立。各小节只保留仍然有效的规则、量法与坑；每次踩坑的完整复盘（日期、现场、修复过程）以测试文件的 docstring 与提交历史为准。

其他文档：[界面用例的判据与坑](testing-ui.md)、[CI 流程、分片与报告](testing-ci.md)、[功能说明](features.md)、[游戏库与发现](library.md)、[全局快捷键](hotkeys.md)、[平台支持](platforms.md)、[开发与发布](development.md)。

## 1. 目录与边界

```text
tests/
  conftest.py            # 严重等级 + 四层 Allure 标签 + 审计日志夹具
  helpers.py             # 跨模块共享的测试数据构造器与环境信息
  reporting.py           # 性能基准与安全结论的记录器
  button_support.py      # GUI 按钮测试共享基建(拆自 test_gui_buttons.py)
  report_support.py      # 报告/CI 断言共享基建(拆自 test_report_verification.py)
  sql_support.py         # SQLite 后端用例共享基建(拆自 test_sql_backend.py)
  ui_sharing.py          # 共享 UI 会话(见第 2 节)
  sharding.py            # 分片规则: 三级权重(实测耗时优先) + 最慢优先贪心装箱
  durations.py           # 分片权重实测档案的记录/加载/合并(守卫 tests/unit/test_durations.py)
  durations.json         # 档案本体(nodeid → 秒), 由 testing-ci.md 第 3 节的命令刷新
  unit/                  # 纯逻辑与单一边界: 不会真正触达用户数据的临时目录/SQLite
  unit/conftest.py       # 单元层共享 fixture(逐字相同才上提)
  integration/           # 真实跨层协作: 配置 + 数据库 + 备份/恢复 + 审计日志 + UI 后端
  performance/           # 规模基准: 数据量与阈值必须显式声明（只在 CI 执行）
  security/              # 不可信输入与危险操作防护（只在 CI 执行）
```

| 类别        | 标记                       | 本地 `pytest` | pre-commit               | CI                                           |
| ----------- | -------------------------- | ------------- | ------------------------ | -------------------------------------------- |
| unit        | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（Windows/macOS/Linux）                  |
| integration | 按目录（无专用标记）       | 运行          | 部分（blocker+critical） | 运行（Windows/macOS/Linux）                  |
| performance | `@pytest.mark.performance` | **不运行**    | 不运行                   | `quality` job 的一部分（ubuntu，单平台采集） |
| security    | `@pytest.mark.security`    | **不运行**    | 不运行                   | `security` job（Windows/macOS/Linux）        |

> **macOS 自 2026-09-30 起重新纳入 CI**（开发阶段曾因 macOS runner 按 Linux 的 10 倍计价屏蔽过一阵）。平台集合在四处保持一致：CI 三个矩阵（`pytest` / `pytest-report` / `security`）、`allurerc.mjs` 的 `environmentsTested`、汇总作业的 `--expect-platforms`，由守卫核对；三个平台**各 3 片** （2026-10-10 起：仓库转公开后 Actions 不再计费，"贵平台少开片"的理由随之消失），墙钟与固定开销的取舍见 `.github/workflows/ci.yml` 矩阵处的注释。

### 严重等级（失败影响面）

每个模块在 `pytestmark` 里声明一个等级，`tests/conftest.py` 把它写成 Allure 的 `severity`。**等级表达"失败的影响面"，与测试层次无关**（层次用 `layer`），并且决定 pre-commit 跑哪些用例：

| 等级       | 判定标准                                          | 例子                                                                            | 本地 pre-commit |
| ---------- | ------------------------------------------------- | ------------------------------------------------------------------------------- | --------------- |
| `blocker`  | 安全与数据完整性底线, 以及会让软件崩溃/卡死的缺陷 | 路径越界 / 危险目标必须被拒、篡改快照或清单必须拒绝恢复、界面事件递归崩溃       | 运行            |
| `critical` | 核心业务不可用或结果不正确                        | 备份 / 快照 / 恢复 / 删除计划、仓储事务、迁移、调度、GUI 真实后端、全链路流水线 | 运行            |
| `normal`   | 常规功能与交互                                    | 配置、平台探测、主页聚合、对话框、CLI、热键、审计、存档位置与命名               | 仅 CI           |
| `minor`    | 展示与辅助                                        | 调色板 / 控件样式 / 渲染修正、i18n 文案、打包元数据、演示后端、性能基准         | 仅 CI           |
| `trivial`  | 极低影响                                          | 色值、脚本生成的报告汇总项（覆盖率 / 性能 / 安全摘要）                          | 仅 CI           |

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

  两个全局影响：**共享窗会长期坐在 `tkinter._default_root` 槽位上**（收尾兜底不收它，`gui_support._SHARED_ROOTS` 就是为此设的），整批连跑时"新建窗口接管槽位 / 外来根坐上槽位被兜底收走"这些判据会跟着全局状态跑偏——`test_gui_roots.py` 里两条盯槽位纪律的用例开头先 `monkeypatch.setattr(tkinter, "_default_root", None)` 摘空槽位（结束自动还原），判的才是槽位这件事本身。窗口的开关节奏是**用完即关**：一个池的最后一个用例一结束，窗口就地关闭（`SharedUiRegistry.declare_users` 依据收集期的"用例→池"声明判断还有没有后续使用者，数据源同 `_SHARED_UI_SESSIONS`；没声明的池退回"会话末统一收"的旧行为）。同池的模块还会在收集期**聚成一段**执行（`sharding.cluster_groups`）：按文件名字母序，demo-app 池的 6 个模块之间隔着十几个每用例自建窗的模块，池窗在中间空档里闲置约 225s（占其开启时间 ~87%）；聚段后窗口只在自己那一段里存在，段尾即收——分片并行时每片挂着的闲置共享窗从"最多三扇陪全程"降到"正在用的那扇"，GUI/合成层的互相拖累也随之减轻。至于非池 GUI 模块里"同名窗口每条用例开关一次"，那是逐用例隔离（干净后端 + 干净窗）的既定形态，要减少它得把模块逐个接入共享池（逐模块证明"无业务影响/可还原"），调顺序帮不上。

约定：测试优先复用**真实**领域服务、临时目录与 SQLite；只有外部依赖（系统回收站、系统快捷键、真实网络）才用可注入替身。不为了测试在生产代码里加分支，也不写"只验证 mock 调用次数"的用例。

## 3. 性能基准

每个基准用 `perf_recorder` 显式声明规模、指标与阈值，超出阈值即失败：

```python
with perf_recorder.duration("home.load_home", scale=SCALE, budget_seconds=15.0):
    board = home_window.load_home()
```

当前基准（规模见各用例常量）：

| 基准                    | 数据规模                   | 指标                | 当前阈值 |
| ----------------------- | -------------------------- | ------------------- | -------- |
| 打开含 500 个游戏的主页 | 500 个游戏、约 2000 条存档 | wall time（冷缓存） | ≤ 15s    |
| 深备份链恢复定位        | 400 层链、每层 20 文件     | wall time           | ≤ 20s    |
| 目录扫描（大量小文件）  | 8000 个小文件              | wall time           | ≤ 60s    |
| SQLite 存储查询         | 10000 条历史               | wall time           | ≤ 2s     |
| 初始化（空库）          | 无数据                     | wall time           | ≤ 3s     |
| UI 关键路径             | 标准演示数据               | wall time           | ≤ 10s    |

阈值刻意留出宽裕余量（CI 机器与本地差异大），只拦"数量级"级别的回归（N+1 取数、非流式复制、O(n²) 的树/排序）。结果写入 `performance-results.json`（含环境信息与每条测量）与 `performance-results.csv`，由 `scripts/create_allure_summary.py` 转成 Allure 中可检索的测试项。

## 4. 安全测试

| 覆盖         | 场景                                                                 | 判据                                       |
| ------------ | -------------------------------------------------------------------- | ------------------------------------------ |
| 路径安全     | `../` 穿越、绝对路径注入、UNC 路径、符号链接逃逸                     | 拒绝并给出用户能看懂的提示，不创建任何文件 |
| 输入校验     | 超长字符串、二进制注入、特殊字符（`:` `\` `/`）、编码混淆（NFC/NFD） | 拒绝或转义，不崩溃、不落盘                 |
| 危险操作确认 | 删除原始存档位置、清空历史                                           | 必须经过确认流程，取消后零副作用           |
| 备份完整性   | 篡改清单、篡改快照、清单与实际不一致                                 | 恢复操作必须失败并报告差异，不产生半成品   |
| 并发安全     | 备份期间修改存档、同一位置的并发备份                                 | 通过锁或排队保证一致性，不死锁             |

约定：不使用真实凭据或真实用户文件；涉及"用户主目录 / 盘符根目录"的用例只做**判定**，不执行写入或删除。

**副作用与网络的政策**：

- 除"删除原始存档位置"（用户显式触发、输入游戏名确认、只删空且走系统回收站）外，软件**不得对应用自身目录之外的任何区域做写/执行/删除**，只读可以。
- 网络请求**只允许取回内容，禁止外发**：只发 GET/HEAD、无请求体、无凭据头、目标主机必须在允许清单内（公开 Steam 图片 CDN 与公开商店接口）。
- 两条政策由 `test_side_effects.py` / `test_outbound_requests.py` 用"动态记账 + 静态扫描"两层守住：动态证明真实流水线合规，静态防将来新增旁路（直接 `subprocess.run`、换 HTTP 客户端）。

结果写入 `security-results.json`（场景/输入摘要/期望/实际/是否拦截），同样汇总进 Allure；未拦截即用例失败。

## 5. 写用例的通用纪律

两条跨模块的纪律：平台差异要能搬回本地复现（而不是只在 CI 的一半平台上有效），数据不变式要在写入侧守住（而不是靠用例事后抽查）。

### 只在某个平台红的分片失败：先把那份平台差异搬回本地

通用做法：遇到"只有某个平台红"，先定位它依赖哪种形态差异（路径分隔符与大小写、`normpath` 是否幂等、`dir_fd` 相对路径、显示服务、字体度量），再把那个差异做成替身搬进用例——否则这条用例永远只在 CI 的一半平台上有效。两个实例：

- **判重只规范化了一侧**（演示数据存 Windows 风格展示串、输入侧过 `normalize_path`，POSIX 上形态不同永远比不出重复）→ 判重两侧都规范化；守卫把 `normalize_path` 换成 POSIX 风格替身，Windows 上就能复现（`test_ui_demo.py::test_demo_duplicate_locations_are_caught_for_unusual_path_forms`）。
- **裸 Ubuntu 没有中日韩字体**，Tk 把汉字量成近零宽 → CI 装 `fonts-noto-cjk`；用例侧三条纪律：**前提只用拉丁字符表达**（缺字体的机器也量得出宽度）、**前提写成强制断言不写 if**（前提不成立时一条断言都不执行，用例永远绿）、**期望值不能与被测值同源**（面板没被摆放时标签按内容自适应，`measure(shown) <= width` 恒成立；夹具要给容器被框定的宽度：`CTkFrame(width=…)` + `grid_propagate(False)` + `place()`，CTk 的 place 不接受 width/height）。

### 同一路径只有一条位置：判重靠"落库即规范化"

`SaveLocationRepository.duplicate_of` 比的是字符串相等（`LOWER(path) = LOWER(?)`），"同一个文件夹"的前提是两侧都已是规范化形式——这条不变式由写入侧维持：`add_location` / `update_location` / `confirm_candidate` 都先过 `normalize_path`。破坏时失败是**静默**的（`…/saves`、`…/saves/`、`…/saves/.` 被当成三个路径：备份拍两份、恢复写两次）。守卫：`test_sql_locations.py::test_every_stored_save_location_path_is_normalized`、`test_the_same_folder_in_another_spelling_is_rejected`、`test_save_candidates.py::test_confirming_a_candidate_written_differently_keeps_one_location`。

## 6. 新增测试清单

1. 先想清楚这属于哪一层、严重等级是什么（第 1 节），不要照抄邻居。
2. 构造数据优先用 `tests/helpers.py` 的构造器；构造器要传真实路径、真实 SQLite，别拿 `MagicMock` 顶领域服务。
3. 需要临时文件/目录的用 `tmp_path`，不要写仓库目录或用户主目录。
4. 界面用例确认有没有现成的共享基建（`tests/button_support.py`、`tests/ui_sharing.py`）可搭。
5. 用例名与断言信息用中文，按"给定—当—then"组织，读得出业务含义。
6. 涉及平台的差异（路径分隔符、文件名大小写、显示服务）要么显式适配要么写成"只在那个平台跑"，并在用例里写明理由；不要悄悄依赖运行机器。
7. GUI 用例遵守 [testing-ui.md](testing-ui.md) 各"判据"小节的量法与坑（自检步骤、不与首帧时序赛跑、收尾纪律）。
8. 跑过全量再提交（`uv run python scripts/run_tests_local.py`），确认没有把别的用例弄红。

```shell
# 提交前本地至少跑这些
uv run pytest --min-severity=critical
uv run ruff format . && uv run ruff check . --fix
uv run mypy
```
