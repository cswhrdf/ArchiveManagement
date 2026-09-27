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
 * 本仓库实际设了六项: 报告标题(`name`)、默认端口(`port`)、历史趋势(`historyPath` /
 * `appendHistory` / `historyLimit`)、界面语言、环境映射(三个平台 + 一个 `Common`
 * 环境)与全局附件(`globalAttachments`)。
 * 其它可用键、以及"生成后自动打开浏览器""单 HTML 报告"为什么故意不设, 见下面各项的注释。
 */
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
  plugins: {
    // Awesome 报告(默认报告)的界面选项.
    awesome: {
      options: {
        // 固定界面语言: 不写就跟随浏览器语言, 英文浏览器看到的就是英文界面。
        // 支持 az/br/de/en/es/fr/he/ja/kr/nl/pl/ru/sv/tr/zh。
        reportLanguage: "zh",
        // 刻意不设的两项:
        // - `open: true`: CI 上会去拉起浏览器(无头 runner 只会报错或挂住), 本地要自开就加 `--open`;
        // - `singleFile: true`: 报告会变成单个 HTML, `data/test-results/*.json` 这些按需拉的
        //   资源就没有了, 会直接打破 scripts/verify_allure_report.py 的自检与单 zip 发布。
      },
    },
  },
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
  // 现在两项: 运行总账(整次运行的结论)与"有意不统计的覆盖"豁免清单(逐条列出
  // `# pragma: no cover` / `# pragma: no branch` 的位置与原因 + exclude_also) ——
  // 后者的数据来自真实源码, 由 scripts/create_allure_summary.py 生成。
  globalAttachments: ["allure-run-ledger.md", "allure-coverage-exclusions.md"],
  /**
   * 质量门(Allure 原生): 规则写在这里, CI 在汇总作业里用
   * `allure quality-gate --config allurerc.mjs allure-results` 跑一遍, 用它的退出码当门禁。
   * 它管的是**整次运行**的结论 —— 有没有失败、通过率、三个平台是否各自有用例结果; 逐项检查
   * (ruff/mypy/静态分析)仍由 ``scripts/create_allure_quality.py`` 负责, 两者互补。
   *
   * **版本要求 ≥ 3.18.0**: 3.13~3.17 在配了 `historyPath` 时会**静默放行**(退 0 且不输出
   * 任何内容) —— 根因是本地历史流的句柄从不销毁, `AllureReport.done()` 永不返回, Node 在
   * 校验前就把进程退掉了(issue #895; 修在 3.18.0 的 PR #962, 另一个 PR #924 至今未合)。
   * CI 因此把 CLI 钉在 3.18.0; 升版本前先重跑一遍下面的两行确认失败用例能让它退 1。
   *
   * 注意两点:
   * - `allure generate` **不执行**校验, 所以首页「质量门」页签仍只有 `allure run` 会填;
   *   CI 把这里的输出写成 `allure-quality-gate.txt`, 由运行总账(`allure-run-ledger.md`,
   *   见 `scripts/create_allure_summary.py`)收进报告首页「全局附件」;
   * - `environmentsTested` 只在**汇总报告**里成立(本地单平台跑必然不通过, 这是预期)。
   *
   * 本地复现(在仓库根, 需要子目录里有 allure-results):
   *   npx allure@3.18.0 quality-gate --config allurerc.mjs allure-results
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
      // **macOS 暂时屏蔽**(2026-09-21, 开发阶段省额度: macOS runner 是 Linux 的 10 倍), 
      // 所以这里只要求两个平台。**恢复清单**: 把 "macOS" 加回这个数组, 并同步
      // CI 的三个矩阵(pytest / pytest-report / security)与汇总作业的 --expect-platforms
      // —— 三处必须一致, tests/unit/test_report_verification.py 会核对。见 PLAN.md 第 11.9 节。
      {
        id: "tests-on-every-platform",
        filter: (tr) =>
          tr.labels.some(
            ({ name, value }) => name === "framework" && value === "pytest",
          ),
        environmentsTested: ["Windows", "Linux"],
      },
    ],
  },
  environments: {
    windows: {
      name: "Windows",
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "Windows"),
    },
    macos: {
      // macOS 暂时屏蔽(2026-09-21): CI 里没有这个平台的作业, 所以正常情况下报告里不会出现
      // 这个环境。matcher 保留着 —— 本地在 macOS 上跑一次就能看到它, 恢复 CI 矩阵时也不用改。
      name: "macOS",
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "macOS"),
    },
    linux: {
      name: "Linux",
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
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "common"),
    },
  },
};
