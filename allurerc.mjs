/**
 * Allure 3 报告配置.
 *
 * 这里是报告生成设置的**唯一来源**: CI 与本地都只跑 `allure generate allure-results
 * --output allure-report`(不带 `--config`), CLI 会自动读取本文件。以前 CI 会在每个
 * 作业里临时写一份 `.allure/allurerc.json` 并把历史设置重复三遍, 现在那三处写法已删掉 ——
 * 声明式 JSON 也不支持 environments, 合并成一份反而只剩一个地方会写错。
 *
 * 为什么需要这个文件: Allure 3 的"环境(Environment)"是一等维度 —— 同一份报告里可以装
 * 下"同一套用例在不同环境/不同机器"的结果, 用例详情页的「环境」分页就是它, 报告顶部还会
 * 多出环境选择器。但环境**不会**只靠 `env` 标签自动生效: 需要在这里用 matcher 声明
 * "哪个环境对应哪些结果"(见 packages/core 的 resolveStoredEnvironmentIdentity ——
 * 结果上没有显式 environment 字段时才会退到 matcher, 匹配不到就落到隐式的 default)。
 *
 * 我们的用法: tests/conftest.py 会给每条结果写 `env` 标签(取值就是平台显示名),
 * 于是 CI 把 Windows/Ubuntu/macOS 三个平台的结果合并成一份报告后, 平台从"只能靠参数/
 * 标签/套件名辨认"变成"报告里的三个环境", 每个用例都能看到自己在三台机器上的结果。
 *
 * 注意两点:
 * - 只有 `allurerc.js/mjs/ts/mts/cts` 这类**可执行**配置支持 environments,
 *   `allurerc.json/yaml` 这类声明式格式不支持(官方 README 明确说明);
 * - 生成报告时要在本文件所在目录执行(CI 与文档里的命令都是在仓库根目录跑
 *   `allure generate ...`), 否则 CLI 找不到它, 环境会静默退化成 default ——
 *   `scripts/verify_allure_report.py` 会在自检时把这种情况报成失败。
 *
 * 本仓库实际设了九项: 报告标题(`name`)、默认端口(`port`)、历史趋势(`historyPath` /
 * `appendHistory` / `historyLimit`)、运行变量(`variables`)、界面语言、全局附件
 * (`globalAttachments`)、失败归类(`categories`)、质量门(`qualityGate`)与环境映射
 * (三个平台 + 一个 `Common` 环境, 外加启动期校验用的 `allowedEnvironments`)。
 * 其它可用键、以及"生成后自动打开浏览器""单 HTML 报告"为什么故意不设, 见下面各项的注释。
 */
import { existsSync } from "node:fs";

// 失败现场只在真有兜底现场时才由 scripts/create_allure_summary.py 生成 —— 这里按文件在不在
// 决定收不收(见 globalAttachments 那一项的说明)。
const failureDiagnostics = existsSync("allure-failure-diagnostics.md")
  ? ["allure-failure-diagnostics.md"]
  : [];
// 历史修复记录同理: 只在 scripts/repair_allure_history.py **真的动了历史文件**时才生成 ——
// 每轮都挂一份“什么都没修”的记录只会让人以为趋势一直在出问题。
const historyRepair = existsSync("allure-history-repair.md")
  ? ["allure-history-repair.md"]
  : [];

export default {
  // 报告标题(显示在报告头部与 <title> 上). 不写就是通用的 "Allure Report"。
  name: "存档管理工具 · 测试报告",
  // `allure open <报告目录>`(以及 `allure generate --open`)默认使用这个端口.
  // CLI 不传 --port 时才会用到它; 端口被占用就临时换一个: `allure open allure-report --port 8081`.
  port: "9000",
  // 历史趋势: CI 作业会把上一次成功运行的历史文件下载回 .allure/history.jsonl,
  // 生成报告时把本次结果追加上去(条数上限 40), 于是报告里能看到跨运行的趋势。
  historyPath: "./.allure/history.jsonl",
  appendHistory: true,
  historyLimit: 40,
  // 运行变量: 显示在报告**顶部**, 用来放"读者需要、但报告里看不出来"的稳定事实。
  // 只写跨运行不变的东西: 每次运行都会变的(提交号/分支/run id/时间/平台)由
  // `allure-results/environment.properties` 与运行总账负责, 写在这里只会立刻过期。
  // 各平台自己的事实(CI 镜像)放在 `environments` 里的**按环境变量**中, 切环境时跟着变。
  variables: {
    // 与 pyproject.toml 的 [tool.coverage.report] fail_under 一致(守卫会核对)。
    覆盖率门槛: "95%",
    // 本地默认只收集前两类(见 pyproject.toml 的 testpaths): 读报告的人看到没有性能/安全
    // 结果时, 第一反应往往是"漏跑了" —— 这里直接说明那是有意的。
    用例分层: "unit / integration 每次跑 · performance / security 只在 CI",
  },
  // 环境 id 白名单(启动期校验): `environments` 里声明的**每个** id 都必须在这里列出,
  // 否则 `allure generate` 直接以 Internal Error 退出 —— 实测 3.18.0 的原文:
  //   config.environments: environment id "common" is not listed in allowedEnvironments
  // 为什么要它: 环境维度是本仓库报告的主路径, 而"加一个平台"要动的地方不止一处(配置里的
  // matcher、CI 的三个矩阵、汇总作业的 --expect-platforms)。以前漏改这里只会静默少一个
  // 环境, 现在报告根本生成不出来, 且错误信息直接点名是哪个 id。守卫:
  // tests/unit/test_test_config.py 断言这份清单恰好等于 `environments` 的键集合。
  allowedEnvironments: ["windows", "macos", "linux", "common"],
  // 环境映射: 平台 / 公共检查 → 环境(靠结果上的 env 标签匹配, 见文件头说明).
  //
  // 其它可用但这里不写的键(需要时临时用 CLI 参数覆盖即可):
  // - 顶层: `output`(报告目录, 默认 allure-report)、`resultsDir`(结果目录的 glob, generate 用
  //   位置参数更直白)、`hideLabels`(按名隐藏标签, `_` 开头的默认已隐藏)、`open`(生成后
  //   自动开浏览器 —— CI 上会去拉浏览器, 所以不设)、`known-issues`(已知问题文件);
  // - awesome 插件: `theme`(默认 `auto` 跟随系统)、`logo`(头部图标)、`groupBy`(默认
  //   parentSuite/suite/subSuite)、`singleFile`(单 HTML, 会拆掉按需拉的详情资源)。
  // 运行级产物: 报告首页「全局附件」页签装的是"不属于任何一条用例"的产物 —— 报告里
  // 每条结果的附件挂在用例上, 挂不上用例的(整次运行的运行总账之类)就只能放这里。
  // 值是**相对仓库根目录**的 glob(生成报告时必须在本文件所在目录执行, 否则匹配不到;
  // 匹配不到不会报错, 只是没有这条附件)。
  //
  // 只放"没有归属"的东西: 覆盖率/性能/安全报告已经各自挂在对应的汇总项上(见
  // scripts/create_allure_quality.py 与 create_allure_summary.py), 再放进这里只会
  // 让报告 zip 变大一倍, 所以刻意不加。要加就把文件名追加到这个数组。
  // 现在三项: 运行总账(整次运行的结论)、"有意不统计的覆盖"豁免清单(逐条列出
  // `# pragma: no cover` / `# pragma: no branch` 的位置与原因 + exclude_also) ——
  // 后者的数据来自真实源码, 由 scripts/create_allure_summary.py 生成; 以及**失败现场**
  // (把各作业失败时 `collect_job_diagnostics.py` 写的摘要拼成一份)。
  // 第三项是**条件**收的(用户 2026-10-04): 没有兜底现场(崩溃 / 证据缺失)时那份文件
  // 根本不存在 —— 一份永远挂在首页的"失败现场"只会让人以为可能崩过, 点开才发现是空的;
  // 按文件在不在收也顺带避开了"附件找不到"的告警。前两项由 scripts/create_allure_summary.py
  // 写在仓库根, 这个作业的 cwd 就是仓库根。
  globalAttachments: [
    "allure-run-ledger.md",
    "allure-coverage-exclusions.md",
    ...failureDiagnostics,
    ...historyRepair,
  ],
  // 失败归类(Categories): 把"失败/损坏"的结果按**错误文本**分门别类, 与默认的
  // Product errors / Test errors 并存 —— 被某条规则命中的结果会被它"消费"掉, 不再落回默认分类。
  //
  // 为什么要它: 报告的默认分类只分"断言失败"与"环境损坏", 看不出"这是仓库里记过的已知
  // 环境问题"还是"真回归"。分类只影响**呈现**, 不影响质量门(有失败照样红) —— 它的价值是
  // 让下一个人少花时间在已经被解释过的环境问题上。
  //
  // 三条规则(每条都能追溯到仓库里的记录, 别凭印象加):
  // 1. Tk/Tcl 库不可用 —— 症状清单的权威来源是 tests/tk_guard.py 的 KNOWN_TK_SKIP_MARKERS
  //    (uv 托管构建偶发读不到 Tcl/Tk 库数据, 上游 astral-sh/uv#7036)。这里用**数组**匹配器:
  //    数组内每项是与(AND)、数组之间是或(OR), 于是"message 里有"或"trace 里有"都算命中
  //    (实测 3.18.0: 两种形态都落位)。
  //    正则带 `i`: 与 tests/tk_guard.is_known_tk_skip 的语义保持一致 —— Tcl 自己的报错里是
  //    `tcl_findLibrary`, 而白名单里写的是小写, 只按大小写敏感匹配就会漏掉一半拼法。
  //    **注意它的覆盖面**: 已知症状在 GUI 用例里会被 tk_guard 转成**跳过**(跳过不进分类),
  //    所以这条规则抓的是"同一个问题以失败/损坏形态冒出来"的残余情形(没有守卫的地方、
  //    夹具收尾期、或异常被别的异常包住时)。加上 layer 限定是为了掐掉一个会误判的形态:
  //    我们自己的守卫用例(tests/unit/test_gui_retry.py)在失败信息里**会打印整份症状清单**,
  //    不限定层就会把"单测失败"说成"Tk 环境问题"。
  // 2. 数据库瞬时读失败 —— 全量跑里出现过一次的已知偶发。
  // 3. 工程门禁未通过 —— 脚本写入的质量检查结果带 testCategory=quality, 它们失败时
  //    不该混进"用例失败"(那是"代码没过门禁", 不是"哪条用例坏了")。
  //
  // groupBy 里的 layer/status/environment 是内建选择器(取自结果标签), 用来在分类内部再
  // 分层; 每个分类还会默认再按错误消息聚一次(groupByMessage 默认 true)。
  // 两条要留意的语义: matchers 里 `labels` 的字符串会被当**正则**用(这里的取值没有元字符,
  // 安全); 没被任何规则命中的普通失败照旧落进默认分类(实测专门验过这一条, 否则分类会把
  // 无关失败也吞进去)。
  categories: {
    rules: [
      {
        id: "env-tk-library",
        name: "环境:Tk/Tcl 库不可用",
        matchers: [
          {
            statuses: ["failed", "broken"],
            labels: { layer: /integration|e2e/ },
            message:
              /tcl_findLibrary|init\.tcl|tk\.tcl|auto\.tcl|no display name|couldn't connect to display|no \$display environment variable/i,
          },
          {
            statuses: ["failed", "broken"],
            labels: { layer: /integration|e2e/ },
            trace:
              /tcl_findLibrary|init\.tcl|tk\.tcl|auto\.tcl|no display name|couldn't connect to display|no \$display environment variable/i,
          },
        ],
        groupBy: ["layer", "status"],
      },
      {
        id: "env-transient-database",
        name: "环境:数据库瞬时读失败",
        matchers: {
          statuses: ["failed", "broken"],
          message: /unsupported file format|database disk image is malformed/,
        },
        groupBy: ["layer"],
      },
      {
        id: "gate-quality-check",
        name: "工程门禁:质量检查未通过",
        matchers: {
          statuses: ["failed", "broken"],
          labels: { testCategory: "quality" },
        },
        groupBy: ["environment", "status"],
      },
    ],
  },
  /**
   * 质量门(Allure 原生): 规则写在这里, CI 在汇总作业里用
   * `allure quality-gate --config allurerc.mjs allure-results` 跑一遍, 用它的退出码当门禁。
   * 它管的是**整次运行**的结论 —— 有没有失败、通过率、三个平台是否各自有用例结果; 逐项检查
   * (ruff/mypy/静态分析)仍由 ``scripts/create_allure_quality.py`` 负责, 两者互补。
   *
   * **版本要求 ≥ 3.18.0**: 3.13~3.17 在配了 `historyPath` 时会**静默放行**(退 0 且不输出
   * 任何内容) —— 根因是本地历史流的句柄从不销毁, `AllureReport.done()` 永不返回, Node 在
   * 校验前就把进程退掉了(issue #895; 修在 3.18.0 的 PR #962, 另一个 PR #924 至今未合)。
   * CI 从 2026-10-04 起用**浮动标签** `allure@3`(用户决定动态取最新, 当时最新是 3.20.0), 所以
   * 下限改在**运行期**核对: 工作流那句 `Check Allure version` 会解析实际版本, 低于 3.18.0
   * 当场红(静默放行那种失效在报告里看不出来)。升版本前先重跑一遍下面的两行确认失败用例
   * 能让它退 1 —— `tests/unit/test_report_verification.py` 里那条守卫把工作流里写的版本与
   * 这里的最低要求对一遗(浮动标签则要求运行期校验必须在)。
   *
   * **3.20.0 起 `generate` 阶段也会执行质量门**(实测: 单平台的 `allure-results` 在
   * `environmentsTested` 面前会以退 1 收场, 而 3.19.1 在同样输入下能生成成功)。汇总作业
   * 本来就是三个平台齐全, 所以它跑得通; 逐平台的报告作业若撞上它, 用不带 `qualityGate` 的
   * 配置就行(见 `allurerc.per-platform.mjs`)。
   *
   * 注意两点:
   * - `allure generate` **不执行**校验(3.20.0 之前), 所以首页「质量门」页签仍只有
   *   `allure run` 会填; CI 把这里的输出写成 `allure-quality-gate.txt`, 由运行总账
   *   (`allure-run-ledger.md`, 见 `scripts/create_allure_summary.py`)收进报告首页
   *   「全局附件」 —— 那一节就是“整次运行的门到底过没过、输出是什么”的落地处;
   * - `environmentsTested` 只在**汇总报告**里成立(本地单平台跑必然不通过, 这是预期)。
   *
   * 本地复现(在仓库根, 需要子目录里有 allure-results):
   *   npx allure@3 quality-gate --config allurerc.mjs allure-results
   */
  qualityGate: {
    rules: [
      // 第一条规则集: **不过滤**, 看全部结果。
      // - 有一条失败就不算通过(与 CI 整体绿灯一致);
      // - 注意是**小数比率**(内部比的是 passed/total), 不是百分数。
      // 脚本生成的结论项(覆盖率/性能/安全/质量检查)也算在这里: 它们的状态本身就是结论,
      // 滤掉的话"覆盖率项 broken"这类失败就没人管了。
      { maxFailures: 0, successRate: 0.98 },
      // 第二条规则集: **只看真实用例**, 要求每个跑测试的平台各自都有。
      //
      // 为什么不再用 `minTestsCount: 3000` 兜"某个平台的产物漏收": 绝对值会随用例规模往
      // **更松**的方向漂 —— 每条平台用例数涨到 1400 上下之后, 缺一整个平台也仍然高于 3000,
      // 规则静默失效且没有任何信号(实测: 三平台合计 3P+154, 缺一个平台 = 2P+154)。现在用
      // 环境维度表达同一件事: 滤掉脚本生成的结论项后, 那些环境里都还得剩着用例。
      //
      // 判据用**必须有 `framework=pytest`** 而不是"不能带 testCategory": 这个标签由
      // allure-pytest 自己写(真实用例都有), 而我们的脚本产物一律不写它
      // (tests/unit/test_report_verification.py 有守卫盯着 scripts/ 别写)。方向很重要 ——
      // 正向判据漏判时**会红**(规则失效立刻看得见), 反向判据漏判时**会绿**(将来某个脚本
      // 忘了打标签, 规则就悄悄失去意义, 正是上面那个常量踩过的坑)。
      // 实测(3.18.0): 规则集上的 filter 对 environmentsTested 生效 —— 只带汇总项的环境
      // 不算"测过"(用临时配置验过: 同名两组规则, 带 filter 的那组会报"Windows 未被测试")。
      //
      // 已知局限: 这条规则只看"该平台有没有用例结果", 发现不了**部分**漏收(三个分片只
      // 合并进一两片时, 环境、通过率、这条规则全都正常)。那一层交给产物清单:
      // scripts/merge_allure_results.py 写、各平台作业上传、由
      // scripts/verify_allure_report.py --manifest 与最终条数对齐(见 docs/testing.md 第 6 节)。
      //
      // macOS 已于 2026-09-30 恢复: 这里要求三个平台, 与 CI 的三个
      // 矩阵(pytest / pytest-report / security)及汇总作业的 --expect-platforms 必须一致
      // —— tests/unit/test_report_verification.py 会核对这四处的集合。
      //
      // **取值必须是环境的 id(小写), 不是平台显示名**(2026-10-04 定位): 规则比的是每条
      // 结果的 `environment`, 而那是环境身份里的 **id**(`environments` 的键), 于是写
      // "Windows"/"macOS"/"Linux" 会**一个都比不上** —— 症状是门禁一次报全三个
      // `The following environments were not tested: "Windows", "macOS", "Linux"`,
      // 而用例其实三条平台都交齐了(汇总报告里 `quality-gate.json` 的 `testResults: []`
      // 就是"过滤后一条都没剩"的痕迹)。本地用三平台最小结果实测:
      // 期望写 id 通过、写名字必失败; 删掉某个平台的结果后, 规则会**只**报那一个 id 缺失
      // (说明规则本身是有效的, 不是被过滤条件掐空)。守卫:
      // `tests/unit/test_report_verification.py` 会核对这里的 id 都在 `environments` 里
      // 声明过, 并与 CI 矩阵 / `--expect-platforms` 的**显示名**名单逐一对上。
      {
        id: "tests-on-every-platform",
        filter: (tr) =>
          tr.labels.some(
            ({ name, value }) => name === "framework" && value === "pytest",
          ),
        environmentsTested: ["windows", "macos", "linux"],
      },
    ],
  },
  environments: {
    windows: {
      name: "Windows",
      // 按环境的运行变量: 选中该环境时显示在报告顶部(与顶层 `variables` 合并)。
      variables: { "CI 镜像": "windows-latest" },
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "Windows"),
    },
    macos: {
      name: "macOS",
      variables: { "CI 镜像": "macos-latest" },
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "macOS"),
    },
    linux: {
      name: "Linux",
      variables: { "CI 镜像": "ubuntu-latest" },
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "Linux"),
    },
    // 公共检查(ruff / 宿主平台 mypy / 静态分析): 结论**本身与平台无关**, CI 只在一个平台上
    // 跑一遍, 结论项带 env=common。判据是"结论与平台无关", 不是"在哪台机器上跑" ——
    // 宿主平台那次 mypy 除开两条平台专属分支(另有 platform 组专门验)之外, 其余结论在哪个
    // 平台上跑都一样, 所以它归 Common 而不是 Linux。声明成独立环境后, 报告里它们属于
    // Common 而不是某个平台 —— 既不会被环境筛选误伤为"Linux 上的质量检查", 也不必依赖
    // 隐式的 default。
    // (平台专属的 mypy 检查不带 common: 它们在对应平台上执行, 验的就是那个平台, 结论归入
    // 对应平台的环境, 见 scripts/create_allure_quality.py 的 PLATFORM_ENVIRONMENTS。)
    common: {
      // 名字与三个平台保持同一种风格(单个英文词)、与环境 id 一致。
      name: "Common",
      variables: { 执行方: "Ubuntu 上的公共检查(结论与平台无关)" },
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "common"),
    },
  },
};
