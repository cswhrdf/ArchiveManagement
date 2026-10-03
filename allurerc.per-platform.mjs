/**
 * "逐平台报告"专用的 Allure 配置: 与仓库根的 `allurerc.mjs` 完全一致, **只摘掉质量门**。
 *
 * 为什么需要它(2026-10-04): 3.20.0 起 `allure generate` 阶段也会执行质量门, 而门里的
 * `environmentsTested` 只在**汇总**那一份报告里成立 —— `pytest-report` 作业每次只合并
 * **一个平台**的结果, 用主配置生成会被判"Windows/macOS/Linux 都没测过"并以退 1 收场
 * (实测: 同样的输入在 3.19.1 下能生成成功)。汇总作业继续用 `allurerc.mjs` —— 它本来就
 * 是"跑门"的地方, `allure quality-gate --config allurerc.mjs allure-results` 也在那里跑。
 *
 * 环境映射、历史(historyPath/appendHistory/historyLimit)、运行变量、全局附件、失败归类
 * 全部从主配置**继承** —— 只有一处真相, 升级或改设置时不会漏掉这一份。
 */
import config from "./allurerc.mjs";

const { qualityGate, ...perPlatform } = config;

export default perPlatform;
