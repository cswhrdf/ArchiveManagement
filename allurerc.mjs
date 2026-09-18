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
 * 本仓库实际设了四项: 报告标题(`name`)、默认端口(`port`)、历史趋势(`historyPath` /
 * `appendHistory` / `historyLimit`)与界面语言 + 环境映射。其它可用键、以及"生成后自动
 * 打开浏览器""单 HTML 报告"为什么故意不设, 见下面各项的注释。
 */
export default {
  // 报告标题(显示在报告头部与 <title> 上). 不写就是通用的 "Allure Report"。
  name: "存档管理 · 测试报告",
  // `allure open <报告目录>`(以及 `allure generate --open`)默认使用这个端口.
  // CLI 不传 --port 时才会用到它; 端口被占用就临时换一个: `allure open allure-report --port 8081`.
  port: "8080",
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
  // 环境映射: 平台 → 环境(靠结果上的 env 标签匹配, 见文件头说明).
  //
  // 其它可用但这里不写的键(需要时临时用 CLI 参数覆盖即可):
  // - 顶层: `output`(报告目录, 默认 allure-report)、`resultsDir`(结果目录的 glob, generate 用
  //   位置参数更直白)、`hideLabels`(按名隐藏标签, `_` 开头的默认已隐藏)、`open`(生成后
  //   自动开浏览器 —— CI 上会去拉浏览器, 所以不设)、`known-issues`(已知问题文件);
  // - awesome 插件: `theme`(默认 `auto` 跟随系统)、`logo`(头部图标)、`groupBy`(默认
  //   parentSuite/suite/subSuite)、`singleFile`(单 HTML, 会拆掉按需拉的详情资源)。
  environments: {
    windows: {
      name: "Windows",
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "Windows"),
    },
    macos: {
      name: "macOS",
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "macOS"),
    },
    linux: {
      name: "Linux",
      matcher: ({ labels }) =>
        labels.some(({ name, value }) => name === "env" && value === "Linux"),
    },
  },
};
