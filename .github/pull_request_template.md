<!--
填之前先看一眼 CONTRIBUTING.md 第十节。用不到的条目整行删掉，比留着"待办"更好读。

本项目约定：`main` 不接受直接提交，只接受 `dev` 或 `bugfix/*` 分支的 PR 合入（新功能从 `dev` 拉分支）。

**要发版就在本 PR 里改版本号**：把 `src/archive_management/packaging.py` 的 `__version__` 往上加一位（如 `0.0.2` → `0.0.3`，大版本自己定）；不改的话合入后只构建、不新建 Release（`main` 有分支保护，合入之后谁都改不了它）。忘了改时 `release-hint` 会在这个 PR 下留一条提示。

门禁：**PR 与 push 都跑全量**（三平台分片用例 + 静态检查 + 安全用例；2026-10-14 起不再按改动路径挑层）。**每一轮都出覆盖率判定与完整 Allure 报告**。所以覆盖率结论看那一轮报告里的「Coverage report」条目（或按 CONTRIBUTING 第四节在本机跑那两步）。**合入 `main` 的 PR 还要过打包门禁**（`CI required packaging status`：等 CI 的结论都出来之后，用这个 PR 的合并预览提交构建 Windows 产物并跑产物冒烟）—— 它比别的检查晚一步。 -->

## 本次变更

<!--
这一节**手写**合入 `main` 之后，它会被**原样**取出来当作 GitHub Release 的说明。
所以按"用户会看到什么"来写 —— 新增或修好的行为、需要注意的变化；
不要写提交号、文件清单、内部实现细节（那些在 Commits / Files changed 标签页里已经有）。

取文规则只认 `## 本次变更` 这个二级标题：从这一行开始，到**下一个二级标题**为止。
-->

（写这一版改了什么）

## 动机

<!-- 解决什么问题、什么缺陷。写清"现在的做法为什么不够"，比"我希望多一个按钮"有用得多。 -->

## 做法

<!-- 为什么选它；被否掉的替代方案与原因（这个仓库的注释习惯是把取舍留在代码旁）。 -->

## 怎么验证的

<!-- 必填。跑了什么（命令 + 数字），以及"改坏哪个字它就会红"。只跑了现有用例就照实写。 -->

- [ ] 新增/改动的守卫做过咬合验证（故意改坏 → 确认变红 → 还原）
- [ ] 本地跑过全量或说明为什么不需要：`uv run python scripts/run_tests_local.py`
- [ ] 新用例声明了严重等级（模块里 `pytestmark` 恰好一条），且不依赖执行顺序或别的用例的残留

## Squash 合并署名

<!--
署名区由 Co-author attribution 检查在合并前自动统计 PR 提交作者并更新。 Squash and merge 时，请确认 GitHub 的最终提交说明保留下面生成的 trailer。不要手工编辑两个标记之间的内容。 -->

<!-- co-author-trailers:start -->

（等待 CI 自动统计共同作者）

<!-- co-author-trailers:end -->

## 影响面

- [ ] **数据安全**：动了存档的写入 / 删除 / 恢复 / 导入导出（这类改动请额外写清最坏情况会丢什么）
- [ ] **界面**：附**前后对比截图**（`ui-review/` 是本地复核材料，已被忽略、不进版本库）
- [ ] **平台差异**：Windows / Linux / macOS 中哪一处行为不同（机制见 [docs/platforms.md](docs/platforms.md)）
- [ ] **用户可见行为**：同步了 `wiki/`（必要时含 `README.md`）
- [ ] **机制改动**：同步了对应的 `docs/*.md`
- [ ] **依赖**：改了 `pyproject.toml` / `uv.lock`（提交前先 `uv sync`，钩子不会替你重建环境）
- [ ] 以上都不涉及

## 覆盖率（硬门禁 100%）

- [ ] 新增/改动的代码都有用例覆盖
- [ ] 确实够不着的行按口径写了豁免：平台专属用 `# platform: <平台…> - 原因`，其余用 `# pragma: no cover - 原因`（原因至少 5 字，不许标在 `def`/`class` 行上）
- [ ] 平台豁免改动后用两步口径核过：`coverage run --rcfile=.coverage-platform.rc -m pytest -q` + `coverage report -m --rcfile=.coverage-platform.rc`

## 关联与遗留

<!-- 关联 issue 用 Closes #123。若某条红是你认为的既有问题，在这里注明 —— 别用跳过/放宽判据的方式绕过。 -->
