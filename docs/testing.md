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

| 类别        | 标记                       | 本地 `pytest` | pre-commit | CI                                      |
| ----------- | -------------------------- | ------------- | ---------- | --------------------------------------- |
| unit        | 目录默认 `critical`        | 运行          | 运行       | 运行（三平台）                          |
| integration | 目录默认 `normal`          | 运行          | 不运行     | 运行（三平台）                          |
| performance | `@pytest.mark.performance` | **不运行**    | 不运行     | `performance` job（ubuntu，单平台采集） |
| security    | `@pytest.mark.security`    | **不运行**    | 不运行     | `security` job（ubuntu/windows/macos）  |

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
- 覆盖率摘要由 `scripts/create_allure_coverage.py` 生成；性能与安全结果由`scripts/create_allure_summary.py` 生成，原始 JSON/CSV 作为附件保留，保证结论可下载、可追溯。

## 6. CI 流程

```text
quality (3 平台: ruff / black / mypy)
pytest  (3 平台: 单元 + 集成 + 覆盖率 + Allure)
performance (ubuntu: 基准与阈值)
security    (3 平台: 越权与危险操作防护)
      ↓
allure-summary (合并全部 allure-results-* → 写入环境信息与性能/安全汇总 → 生成最终报告)
```

每个作业都上传自己的 `allure-results-*`，汇总作业用`actions/download-artifact` 的 `pattern` + `merge-multiple` 合并后生成唯一报告，并沿用 `.allure/history.jsonl` 累积历史。

## 7. 新增测试清单

1. 选对目录：纯逻辑进 `unit`，需要真实文件/数据库协作进 `integration`，规模基准进`performance`，防护类进 `security`。
2. 声明四层 Allure 标签（`epic`/`feature`/`story`/`layer`）。`tests/unit/test_test_config.py`会扫描全部测试模块，缺标签会直接失败。
3. 选择严重等级：默认 unit=critical、integration=normal，性能/安全=normal；需要时用`@pytest.mark.blocker` / `minor` 等覆盖。
4. 优先用 `tests/helpers.py` 的构造器，避免在模块里再抄一份临时数据库/游戏数据构造。
5. 性能测试必须给出规模与阈值，安全测试必须用 `security_recorder.expect_blocked`记录结论。
6. 本地验证：

```shell
uv run ruff check .
uv run black --check .
uv run mypy
uv run pytest --cov
uv run pytest tests/performance -m performance
uv run pytest tests/security -m security
```
