---
applyTo: "allurerc.mjs, allurerc.per-platform.mjs, scripts/allure_catalog.py, scripts/create_allure_*.py, scripts/merge_allure_results.py, scripts/verify_allure_report.py, scripts/repair_allure_history.py, scripts/collect_coverage_data.py, tests/conftest.py, tests/reporting.py, tests/report_support.py, tests/crash_capture.py, tests/unit/test_report_*.py, tests/unit/test_ci_*.py, tests/unit/test_sharding.py, tests/unit/test_test_config.py, .github/workflows/*.yml"
description: '本仓库 Allure 报告体系的用法与不变式: 配置的唯一真相、打标与环境维度、结论项与附件、原生质量门、历史趋势、分片合并与自检, 以及"改哪一处必须同步改哪几处"与对应的守卫。'
---

# Allure 报告体系使用规范

这份报告是**合并前唯一会被逐条阅读的证据**（跑绿不等于看得懂），所以它自己也是一套有守卫的代码。本规范整理自本仓库真实踩过的坑：环境静默退化成 default、质量门静默放行、历史趋势每轮清零、缺一片数据被说成"覆盖率掉了"、附件把详情页卡死、汇总作业少了 `Install uv` 导致报告全缺（2026-10-10）。

**核心前提：报告类故障几乎不会让任何用例变红。** 环境退化成 default、历史断链、少一片数据、附件丢失，表现出来都是"报告看起来正常"——所以每一条约定要么写死在配置里，要么由 `tests/unit/test_report_*.py` / `test_ci_*.py` 的守卫钉住。

细节说明在 `docs/testing.md` 第 5~7 节（结果元数据 / CI 流程 / 本地生成），本文只写"改代码时怎么不错"。

## 1. 全景：一次运行里，报告是怎么拼出来的

| 角色                                                                         | 产出                                                                                                                                          | 谁消费                 |
| ---------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------- | ---------------------- |
| `tests/conftest.py`                                                          | 每条结果的四层标签 + 严重等级 + `env`/`os`/`平台` 参数                                                                                        | 环境维度、质量门、分类 |
| 分片 pytest（三个平台各 3 片）                                               | `allure-results/` → 产物 `allure-results-<镜像名>-<片号>`                                                                                     | `pytest-report`        |
| `create_allure_quality.py`（quality 作业，3 组：core / platform / analysis） | `allure-results-quality` / `-quality-platform-<os>` / `-analysis`                                                                             | 汇总作业               |
| `create_allure_visual.py`（quality 作业）                                    | `allure-results-visual`                                                                                                                       | 汇总作业               |
| 性能基准 / 安全用例（quality / security 作业）                               | `allure-results-performance` / `allure-results-security-<os>`                                                                                 | 汇总作业               |
| `create_allure_coverage.py`（`pytest-report`，逐平台）                       | `allure-results-coverage-<平台显示名>`                                                                                                        | 汇总作业               |
| `merge_allure_results.py`（`pytest-report`）                                 | 合并后的 `allure-results` + `allure-manifest.json` → 产物 `allure-resources-<镜像名>`（只含历史 + 清单）与 `allure-report-<镜像名>`（tar.gz） | 人；下一轮取历史       |
| `allure-summary`                                                             | `allure-resources-final`（`.allure/history.jsonl`）+ `allure-report-final`（单个 tar.gz）                                                     | 人                     |

一条硬边界：

- **报告不在产出数据的平台上生成**（两台报告机器都是 Ubuntu）：合并 / 汇总 / 生成 / 自检全是纯文件操作。代价是"这份结论算哪个平台"必须由**参数**给出，**绝不能读 `runner.os`** —— 那台机器上它恒为同一个值，拿它当平台名会把所有平台的结论标错，而且没有任何一步会失败。

## 2. 配置的唯一真相：`allurerc.mjs`

CI 与本地都只跑 `allure generate allure-results --output allure-report`，**不传 `--config`** —— CLI 从 cwd 读这份配置。以前的写法是每个作业临时写一份 `.allure/allurerc.json` 并把历史设置重复三遍，那三处已删除：**重复的配置就是会漂移的配置**。

- **必须在仓库根执行**：换目录 CLI 找不到配置，环境维度**静默**退回 default（报告照样生成、作业照样绿），由 `verify_allure_report.py` 判"报告不完整"。
- **只有 `allurerc.js/mjs/ts/mts/cts` 这类可执行配置支持 `environments`**；声明式 `allurerc.json/yaml` 不支持（官方 README 明确说明），这也是当初合并成一份 `.mjs` 的原因。

各项（改之前先读那里的注释，每项都写着"为什么"）：

| 键                                               | 作用                                            | 改动时要留意                                                                                                                                                                                                                                |
| ------------------------------------------------ | ----------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `name` / `port`                                  | 报告标题 / 本地默认端口                         | 无守卫                                                                                                                                                                                                                                      |
| `historyPath` / `appendHistory` / `historyLimit` | 历史趋势（`.allure/history.jsonl`，上限 40 条） | 由 `test_report_config_keeps_the_history_settings` 钉住"必须留在配置里"                                                                                                                                                                     |
| `variables`                                      | 报告**顶部**的稳定事实                          | 只写跨运行不变的；每次运行会变的（SHA/分支/时间）走 `environment.properties` 与总账                                                                                                                                                         |
| `allowedEnvironments`                            | 环境 id 白名单（启动期校验）                    | 必须**恰好等于** `environments` 的键集合：少列一个 `allure generate` 直接 Internal Error（3.18.0 原文：`environment id "common" is not listed in allowedEnvironments`）。守卫 `test_report_config_declares_every_environment_id_as_allowed` |
| `globalAttachments`                              | 报告首页「全局附件」                            | 只放"挂不上任何用例"的东西；已有结论项的东西别重复挂（让报告 zip 翻倍）                                                                                                                                                                     |
| `categories`                                     | 失败归类（3 条规则）                            | 只影响呈现、不影响质量门；`labels` 的值会被当**正则**用                                                                                                                                                                                     |
| `qualityGate`                                    | 原生质量门（见第 7 节）                         | 只有汇总那份报告该跑                                                                                                                                                                                                                        |
| `environments`                                   | 平台 / Common 四个环境（见第 4 节）             | 改平台集合要四处同改                                                                                                                                                                                                                        |

**两份配置的分工**：`allurerc.per-platform.mjs` 从主配置继承、只摘掉 `qualityGate`（`const { qualityGate, ...perPlatform } = config`）。原因是 3.20.0 起 `allure generate` **也会执行质量门**，而门里的 `environmentsTested` 只在三平台齐全的汇总报告里成立 —— 逐平台作业每次只合并一个平台的结果，用主配置会被判"三个平台都没测过"并以退 1 收场。**新增 generate 调用点时先问：这一份结果里几个平台？** 守卫 `test_per_platform_reports_skip_the_native_gate`（断言继承关系 + 工作流用哪份）。

## 3. 打标：每条结果必须带什么

`tests/conftest.py` 的 `_configure_allure` 在用例开始前写死四层标签 + 一个严重等级，全部由 `pytestmark` 解析而来：

```python
pytestmark = [
    pytest.mark.normal,
    pytest.mark.epic("游戏与存档位置"),
    pytest.mark.feature("游戏探测"),
    pytest.mark.story("注册表读取"),
    pytest.mark.layer("unit"),
]
```

- **四层都必填**：`epic`（闭集）→ `feature`（与生产模块一一对应）→ `story`（具体用户场景）→ `layer`（闭集 `unit/integration/e2e/performance/security`）。守卫 `test_every_test_module_declares_allure_labels`。
- **严重等级恰好一个**：显式标记优先，否则按目录兜底（security→critical、performance→minor、其余→normal）；五级都要有人用。守卫 `test_every_test_module_declares_exactly_one_severity`。
- `layer` 只能用 `allure.dynamic.label("layer", ...)` —— allure-pytest **没有** `allure.layer` 装饰器（守卫 `test_layer_label_is_written_as_raw_label`）。
- 每条结果另带三项"平台冗余"：参数 `平台`、标签 `os`、标签 `env`（值都是平台显示名）。参数与套件名是"配置没被生成端读到"时的兜底；`env` 才是环境维度的入口（见第 4 节）。
- **`env` / `os` 必须在收集期挂成标记**（`conftest._attach_environment_labels`），**不能**在 autouse 夹具里用 `allure.dynamic` 写：`pytest.mark.skip` / `skipif` 由 pytest 在 `pytest_runtest_setup` 最前面求值，**那之后一个夹具都不跑** —— 写在夹具里的标签会整个丢掉，结果落进隐式的 `default` 环境（报告里按平台看不到它，该平台的用例数还比分片自报的少）。`平台` 参数没有标记形态（Allure 只在运行期提供参数接口），仍留在夹具里。守卫 `test_allure_result_carries_the_platform`（钉"标记里有 + 夹具里没有"）与 `tests/unit/test_allure_environment_labels.py`（真跑一条 `skipif` 用例，断言它照样带 env/os）。
- **标题必须写到 `__allure_display_name__`**：allure-pytest 的 `pytest_runtest_setup` 会在所有 fixture 跑完后用 `item.name` 重新赋值，所以 `allure.dynamic.title` 会被冲掉；花括号要转义。描述在收尾时补回（`_restore_description`）。守卫 `test_title_is_readable_after_allure_pytest_renaming` / `test_title_follows_each_parametrized_case`。
- 参数化 id 里的 ASCII 转义（`\u7528\u6237`）在生成报告时还原成中文（`_readable`），守卫 `test_readable_restores_unicode_escapes`。

## 4. 环境维度：平台就是 Allure 的 Environment

数据流：`conftest` 给每条结果写 `env` 标签（平台显示名）→ `allurerc.mjs` 的 `environments.<id>.matcher` 按该标签认领 → 合并三平台结果后，报告里有三个可切换、可对比的环境。

**这里最容易写错的是"两个名字"**：

| 用在哪                                                               | 用哪个名字          | 例                            |
| -------------------------------------------------------------------- | ------------------- | ----------------------------- |
| `env` 标签、`--platform`、`--expect-platforms`                       | **平台显示名**      | `Windows` / `macOS` / `Linux` |
| `environments` 的**键**、`environmentsTested`、`allowedEnvironments` | **环境 id（小写）** | `windows` / `macos` / `linux` |

`environmentsTested` 写显示名的症状是**一次报全三个** `The following environments were not tested: "Windows", "macOS", "Linux"`（用例其实交齐了）；判据是每条结果的 `environment` 字段里存的是**id**。汇总报告里 `quality-gate.json` 的 `testResults: []` 就是"过滤后一条都没剩"的痕迹。守卫 `test_quality_gate_asks_every_platform_for_real_tests`。

**`common` 的判据是"结论与平台无关"，不是"在哪台机器上跑"**：ruff / 宿主平台 mypy / 静态分析只在 Ubuntu 跑一遍，但结论在哪个平台跑都一样 ⇒ 归 `Common`。平台专属检查（`mypy --platform win32/darwin`）**不带 common** —— 它在对应平台上执行、验的就是那个平台（`create_allure_quality.py` 的 `PLATFORM_ENVIRONMENTS` 由 `host_platform` 推出，一处常量）。守卫 `test_quality_items_use_the_declared_common_environment`、`test_quality_items_cover_every_ci_gate`。

**平台集合出现在五处，必须同改**（守卫逐对核对）：`environments` 四个键 ⇄ `qualityGate.environmentsTested`（id）⇄ `allowedEnvironments` ⇄ CI 三个矩阵（pytest / pytest-report / security）⇄ 汇总作业的 `--expect-platforms`（显示名）。守卫 `test_report_config_maps_every_platform_to_an_environment`、`test_required_platforms_match_the_ci_matrix`。

## 5. 结论项：门禁的结果由脚本写成"用例"

覆盖率 / 性能 / 安全 / 视觉 / 静态分析都不以退出码表达结论（那样它们会被混成"作业红"，看不出是哪一项没过），而是由 `scripts/create_allure_*.py` 写成结果项：

- **状态跟着数据走**：不达标 `failed`、读不到文件或写不出结果 `broken`；**不要抛异常代替结论** —— 抛了后面的一致性检查（总账、清单核对）就没机会说话。覆盖率那条结论项的描述第一句固定是"当前覆盖率 X% 大于/小于预期覆盖率 Y%, 验证通过/未通过"，门槛读 `pyproject.toml` 的 `fail_under`（不另写常量）。守卫 `test_coverage_item_fails_the_platform_when_below_the_threshold`、`test_coverage_threshold_comes_from_pyproject`。
- **归属靠 `env` 标签**：`create_allure_coverage.py` 的平台由 `--platform` 给出（主机的 `platform.system()` 会把三平台结论全标成 Linux）。守卫 `test_coverage_item_platform_comes_from_the_flag`。
- **脚本产物不许带 `framework=pytest`**：那是 allure-pytest 给真实用例写的标签，质量门用它区分"用例"与"汇总项"。守卫 `test_quality_gate_asks_every_platform_for_real_tests`（断言四个 `create_allure_*` 里不出现 `"framework"`）。
- **每条结论都要有人负责**：`scripts/allure_catalog.py` 逐族列出"应该有哪一条结论、由哪个上游作业产生、由哪条规则判定"，并在运行总账的「证据核对」里逐项标"已收到 / 缺失"，缺失项另写一条 `broken` 并打 `::warning::`。**加一条结论就要同时加进清单**，双向同步由 `test_the_catalog_is_in_sync_with_the_scripts_that_write_conclusions` 钉住。

## 6. 附件与体积：能挂，但不能把报告拖死

- **失败现场只在失败时留证，且留证失败不许改变用例结果**（`tests/crash_capture.py` 的整段 try）。
- **dump 用 Allure 不认识的媒体类型**（`application/octet-stream`）：用 `attachment_type` 里认识的类型时，上兆字节的 dump 一打开就把页面卡死。守卫在 `tests/unit/test_crash_capture.py`：断言 dump 的媒体类型不以 `text/`、`image/` 开头。
- **必须有上限**：dump ≤ 25 MiB（超限只留落点与大小）、截图 ≤ 3 张、每用例只留一份（`StashKey`）。
- **给字符串媒体类型的附件补真实后缀**：allure 只在拿到枚举时才从枚举推后缀，否则文件会存成 `<uuid>-attachment.attach`。
- **`globalAttachments` 里的可选物要"按文件在不在"收**（`existsSync(...) ? [...] : []`）：一份永远挂在首页的空"失败现场"只会让人以为可能崩过。守卫 `test_diagnostics_report_lands_in_the_allure_config`、`test_global_attachment_matches_what_the_summary_writes`。
- **豁免清单是"人工写、机器列"的**：`allure-coverage-exclusions.md` 逐条列出 `# pragma: no cover` / `no branch` 与 `# platform: ...` 两类标记的位置与原因（数据直接来自源码，由 `create_allure_summary.py` 生成）。写标记时记两条：**原因要回答"这一行为什么到不了/不用测"**，别重复平台名或标记类别（`仅 windows — 原因: winreg 仅 Windows 提供` 这种同义反复等于没写）；**连续、同文件、同平台、同原因的多行会在清单里折叠成一条行号区间**（`…:162-163`），但**逐行标记仍然必需** —— 折叠只改呈现，每一行还是要各自排除（少标一行，它在别的平台就是永久缺口）。守卫 `test_consecutive_identical_platform_markers_collapse_into_one_line`。

## 7. 原生质量门：整次运行的结论

配置在 `allurerc.mjs` 的 `qualityGate`，CI 在汇总作业里用 `allure quality-gate --config allurerc.mjs allure-results` 跑一遍，用它**写进日志的退出码**在末尾判定：

```bash
if: always() && (steps.quality-gate.outcome == 'failure' || steps.verify-platforms.outcome == 'failure')
run: echo "::error::质量门未通过 …" && exit 1
```

- **门禁必须单独成步 + 末尾判定**：校验步骤 `continue-on-error: true`，把 `退出码: N` 追加进 `allure-quality-gate.txt`；否则门一红，作业在生成报告前就中断，连"哪一项没过"的证据都拿不到。
- **顺序不能反**：跑门 → `create_allure_summary.py`（把门日志收进首页「全局附件」的「原生质量门」一节）→ `allure generate`。2026-10-04 之前顺序是反的，那一节永远写着"本次没有质量门输出"。
- **`successRate` 是小数比率**（0.98），不是百分数。
- **不用 `minTestsCount` 兜"某个平台漏收"**：绝对值会随用例规模往更松的方向漂（三平台合计涨到 3P+154 之后，缺一整个平台仍然过线），规则静默失效且没有信号。改成第二组规则集：`filter` 只留 `framework=pytest` + `environmentsTested: ["windows","macos","linux"]`。**判据要选正向**：正向漏判时会**红**，反向（"不能带某标签"）漏判时会**绿**。
- **版本下限至少 3.18.0，且必须在运行期核对**：3.13~3.17 在配了 `historyPath` 时会**静默放行**（退 0 且不输出）—— 根因是历史流句柄不销毁、`AllureReport.done()` 永不返回（issue #895，修在 3.18.0 的 PR #962），这种失效在报告里看不出来。工作流用浮动标签 `allure@3`，所以有一句 `Check Allure version` 解析实际版本并 `::error::` + 退出 1。守卫 `test_native_quality_gate_is_configured_and_pinned`。
- **跑门前要把历史文件藏起来**：`quality-gate` 子命令同样遵循 `appendHistory`，会往 `historyPath` 追加一条"半套快照"冒充上一轮（2026-10-05：最终报告趋势里同一轮出现两个点）。四段式：`mv` 藏真历史 → 跑门 → 删掉它写的 → `mv` 还回。守卫 `test_quality_gate_does_not_touch_the_history_file`。
- **每个驱动 Allure CLI 的作业都必须先装它**：`actions/setup-node@` + `npm install --global allure@3`。少了会在几十行之后以 `allure: command not found`（exit 127）挂掉，结果那一轮的逐平台报告全缺（2026-10-10 run 37958738335）。守卫 `test_every_job_that_drives_the_allure_cli_installs_it`；缓存开关属于 setup-uv / setup-node 自己那一步（见 `github-actions-pitfalls.instructions.md`）。

## 8. 历史趋势：链条比数据更脆

趋势靠"取上一轮的 `allure-resources-*` 产物里的 `.allure/history.jsonl` 再追加"。三件事必须一起盯着：

- **基线只问"有没有产物"，不问那一轮成败**：只认成功运行时，连续失败期（如 macOS Tk 崩溃那几天）会没有任何一轮被消费，基线冻结在更早的成功运行上；恰逢键形状变过，就每轮都按旧形状"修"，趋势永远接不上。取消/没跑到上传的运行自然被跳过。
- **取回时要认两种打包布局**：artifact 内的路径取决于上传侧 `path` 的形状 —— 只列**一个**文件时它落在压缩包**根部**，列多个路径才保留 `.allure/` 前缀。两个恢复步骤都要对 `.previous-allure-resources/.allure/history.jsonl` 与 `.previous-allure-resources/history.jsonl` 依次尝试，都找不到就 `::error::` + `exit 1`（静默跳过 = 每轮把趋势清零重来，2026-10-05 实测）。守卫 `test_history_restore_accepts_both_artifact_layouts`。
- **键形状变了要用 `repair_allure_history.py` 修，不能靠改名糊过去**：三步（去重同轮快照 / 按唯一前缀与 environment 重键 / 实在接不上就重置并写明）；丢失的是**匹配**不是数据；只有真的动了历史才留 `allure-history-repair.md`（按 `existsSync` 条件挂进首页）。守卫 `tests/unit/test_ci_history.py` 一族。

## 9. 合并与自检：少一片是"静默"的那类故障

- **合并脚本逐片报文件数**，缺片只跳过不报错（那时更需要一份报告来看"哪一片没结果"），但**后面的步骤要按缺片跳过**：`collect_coverage_data.py --expect-shards` 逐片点数、缺片非 0 退出、末尾让作业红。别让 `coverage xml` 自己合并剩下的数据并执行 `fail_under` —— 它会把"少了一份数据"说成"覆盖率掉了"（2026-09-30：Windows 被判 89.99% 未达标）。守卫 `test_ci_judges_the_coverage_only_on_complete_shard_data`、`test_the_report_job_only_judges_missing_pieces`。
- **只匹配到一个产物时 `download-artifact` 不建"以产物名命名的子目录"**：单片平台（macOS）必须把 `path` 直接写成那个分片目录名（矩阵字段 `download_path`），多片平台才写 `.`。踩过：macOS 的 12689 个结果文件摊在工作区根，合并脚本按分片目录找、一个都匹配不到，该平台的结论与覆盖率全程没进报告。守卫 `test_single_shard_platform_downloads_into_its_own_shard_directory`。
- **`--manifest` 是"少一片"的唯一判据**：环境、通过率、格式自检、原生质量门都看不出"三片只合并进一两片"。清单由 `merge_allure_results.py` 写、随 `allure-resources-*` 上传、由 `verify_allure_report.py --manifest "allure-manifests/*.json"` 与最终条数对齐。守卫 `test_ci_records_shard_counts_in_a_manifest`。
- **生成前自检，自检不过不发布**：`verify_allure_report.py` 校验详情目录、索引引用的文件、附件、环境 widget、条数；报告目录**不存在**视为"本次没有结果、未生成报告"（退出 0），其余缺资源即退 1。发布（`allure-report.tar.gz`）挂在"出 zip 那次自检成功"上，且**不再发布报告目录本身**。守卫 `test_ci_publishes_only_verified_report`。
- **`default` 环境里不许有结果**：生成端没读到配置时*所有*结果都会进 `default`（由"报告里只有 default 环境"那条拦下）；只个别几条进 `default` 则是**它们没带 `env` 标签**的信号（上面那条 `skip` 成因就是这么表现的：逐平台用例数少几条、汇总报告里同身份的两份还被生成器并掉，索引比结果文件少 1）。检查由 `default_environment_problems()` 做，报错会点名用例（`结果名 (id)`）。守卫 `test_results_that_lost_their_environment_label_are_named`、`test_results_without_an_environment_are_left_alone`。
- **产物名不许改**：`allure-resources-<镜像名>` / `allure-report-<镜像名>` / `coverage-<镜像名>` 里的名字代表"哪个平台的产物"，不是生成机器；改名会让历史曲线清零。
- **别用 `--alluredir` 之外的东西表达归属**：报告目录、`.allure/`、`allure-results*/` 都在 `.gitignore` 里，本地跑出来的产物不入库。

## 10. 排错入口：先读兜底现场，再下结论

CI 红、报告缺块、或者“本地根本不复现的那种失败”来了，**先按这个顺序找证据**（从最强到最弱），不要一上来就猜：

1. **报告首页「全局附件」里的 `allure-failure-diagnostics.md`（《失败现场(兜底)》）**：各作业 `collect_job_diagnostics.py` 写的 `summary.md` 拼成的一份，**只列判定为“有现场”的作业**。这份附件不存在，等于告诉你“这一轮全是普通失败”（断言、门禁、步骤报错）—— 它们的证据在用例详情与运行日志里。
2. **`job-diagnostics-<作业>` / `crash-dumps-<os>-<片号>` 产物**（只在失败时上传，且 `if-no-files-found: error`：说有现场就一定真有东西）：
   - `crash-dumps/faulthandler.log`：进程被信号打死时各线程的 **Python 栈**（哪个线程死在哪个回调）。SIGTRAP 也单独注册过，所以 macOS 的指针认证陷阱 / 断点崩溃（EXC_BREAKPOINT、退出码 133）同样留下栈。
   - `crash-dumps/*.dump`：用例失败**当刻**写的 coredumpy dump（最深 5 层栈帧的局部变量与对象属性，**写盘**，与本次跑没跑 `--alluredir` 无关）。本地读：`coredumpy peek crash-dumps`（先认出是哪条用例的）→ `coredumpy load <文件>`（进 pdb）/ VSCode 的 coredumpy 扩展。
   - macOS 的 `.ips`（`--crash-reports-from ~/Library/Logs/DiagnosticReports` + `--crash-report-wait 20`）：**只有它**能回答“C 层是谁碰坏了内存”。faulthandler 给 Python 栈、`.ips` 给原生栈，两者各管一半，别指望一份顶两份。
   - `verdict.json`：`scene=true|false` 的判定。判成 `false` 时这份产物**根本不存在**，不用去找。
3. **挂在用例上的附件**（报告里点开那条用例）：dump、窗口截图（Windows 按窗口句柄抓，被别的窗口盖住也拍得到）、失败摘要（平台 / Python / 提交号 + 落点 + 复现命令；SQLite 类失败还带稳定的 `sqlite_errorname`/`errorcode`）。**超时**是单独一条 broken 结论（标题 `超时留证: <用例>`、critical、故意不带 `framework`/`testCategory` —— 不该改变质量门口径），附件是线程栈 + 覆盖率快照 + dump。
4. **报告里的“结论项”自己就是判定**：覆盖率没达标就看那条 failed 的描述（第一句就是 X% vs Y%）；质量门看首页「全局附件」的「原生质量门」一节（含退出码）。**少东西**才看运行总账的「证据核对（应有 vs 实有）」与 `--manifest` —— 别用“环境齐全、通过率正常”当作“没漏收”。
5. 最后才回用例详情与运行日志。

读的时候别踩这几条：

- **报告页面里读不了 dump 是有意的**（`application/octet-stream` ⇒ 只给下载链接）：换成 Allure 认识的媒体类型时，上兆字节的 JSON 会被读进预览区，一开报告页就卡死。
- **硬崩溃（SIGSEGV/SIGABRT）留不进报告**：Python 层钩子没机会跑、pytest-cov 也来不及落盘 ⇒ “覆盖率文件不在”本身就是“没跑完”的证据，而不是“覆盖率掉了”。
- **没有兜底现场不代表没问题**：断言失败、门禁不通过**故意**不产生第二份 artifact（已在结果里的东西不再抄一遍）；只有“进程级崩溃”与“证据缺失”两类才兜底。
- **dump 里是真实的局部变量**（coredumpy 会遮掉像密钥的串与 `os.environ` 的值）：把报告发给仓库外的人之前先看一眼附件。
- **本地复现全量时带上 `--alluredir=allure-results`**，否则截图等附件根本不落盘（dump 例外，它永远写盘）。
- **先读现场，再动手**（2026-09-30 的真实一例）：一条 GUI 用例整轮全量里只红过一次，单跑与整模块连跑两遍都不复现，手里只剩一个用例名；从那次失败留下的 dump 里读出 `OperationalError: unsupported file format`，最深一帧是 `MonitoredDirectoryRepository.list_all()` 的 `connection.execute(` —— 结论是“环境级偶发（临时 SQLite 被读成非 SQLite 内容）”，不是“等得不够”的时序问题。一个 dump 顶掉一整天的猜测。

## 11. 改动前的对照表与本地验证

**改一处就要同步的另一处**（每一对都有守卫，除非写明没有）：

| 你改了                                             | 必须一起改                                                                                                                                                                                                                                              |
| -------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 新增/删除一个平台                                  | `environments` 四个键、`allowedEnvironments`、`qualityGate.environmentsTested`（id）、CI 三个矩阵、汇总作业 `--expect-platforms`（显示名）、`test_report_structure` 里的期望集合                                                                        |
| 覆盖率门槛                                         | `pyproject.toml` 的 `fail_under`、`create_allure_summary.COVERAGE_THRESHOLD`（守卫 `test_coverage_fail_under_matches_pyproject`）、`allurerc.mjs` 的 `variables.覆盖率门槛`（**这处没有守卫**：注释里写的"守卫会核对"只覆盖前面两处，改门槛要手动跟上） |
| 片数                                               | CI 矩阵、`--shard-count` / `--shard-index`、`--expect-shards`、报告作业矩阵的 `expect_shards`（`test_ci_shard_count_matches_the_matrix`）                                                                                                               |
| 新增一条结论项                                     | `scripts/allure_catalog.py` 的清单（`test_the_catalog_is_in_sync_with_the_scripts_that_write_conclusions`）+ 产生它的上游作业 + 运行总账里的一节                                                                                                        |
| 新增一个 `allure generate` / `quality-gate` 调用点 | 那个作业要先装 Node 与 CLI（`test_every_job_that_drives_the_allure_cli_installs_it`）；只含一个平台就用 `allurerc.per-platform.mjs`                                                                                                                     |
| 改分类规则 / 环境 id                               | `allurerc.mjs`、守卫里的期望集合、`tests/tk_guard.py` 的症状清单（`test_report_config_categorises_the_known_environment_problems`）                                                                                                                     |

本地验证（`docs/testing.md` 第 7 节有完整命令）：跑一遍带 `--alluredir` 的用例 → 生成 `environment.properties` → `npx allure@3 generate allure-results --config allurerc.mjs --clean --output allure-report` → `verify_allure_report.py` → `npx allure@3 open`（**`file://` 会被跨域拦掉**）。改了配置或脚本，至少跑一次 `tests/unit/test_report_*.py` 与 `test_ci_*.py`：这批守卫不跑用例，几十秒就能给出"哪一处没同步"。
