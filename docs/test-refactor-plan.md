# 测试体系重构计划(2026-10-07)

> 本文档是**执行中的计划**: 每完成一步就在对应条目把 `[ ]` 改成 `[x]`;
> "明确不做"一节记录的是**识别结论**(为什么不动), 防止下一轮重复劳动。
> 现状描述见 `docs/testing.md`, 两者不重复。

## 一、盘点结论(识别阶段, 已完成)

- **规模**: 125 个 `test_*.py` / 2333 个用例, 分四层(`tests/unit|integration|performance|security`);
- **支撑层**: 根 `conftest.py`(autouse 清理 + 元数据) + `helpers.py`(22 个构造器) + `gui_support.py`(GUI 生命周期), 三个子目录里 `performance`/`security` 各有 `conftest.py`, **`unit`/`integration` 没有**。

### 十个维度的现状对照

| 维度 | 现状 | 结论 |
| ---- | ---- | ---- |
| 命名 | 125/125 文件 `test_*.py`, 2333 函数 `test_*`, 0 个 `Test` 类 | ✅ 已达标 |
| 结构 AAA/GWT | 详尽 docstring 讲 Given/When, 裸 assert 做 Then(隐式 AAA) | ✅ 不引入形式化分节注释 |
| 前置 | 0 处 `setUp`/`tearDown`/`unittest.TestCase`, 全 fixture/工厂函数 | ✅ 已达标 |
| 共享 conftest | 根 conftest 完整; 跨文件**同名** fixture 仅 3 组(见下) | ⚠️ S1/S2 上提 |
| 数据 parametrize | 已有 63 处; 全库同前缀用例组仅 6 组(各 2 个) | ⚠️ S3 合并其中真同构的 |
| 隔离 | 每用例自建 `tmp_path` 环境(60 处直建库), 无顺序依赖 | ✅ 已达标 |
| 断言 | 全裸 assert + 失败消息 | ✅ 不引入 pytest-assume |
| 外部依赖 | monkeypatch 1097 处, 0 处 `unittest.mock`; src 无 requests/urllib | ✅ 不引入 pytest-mock/responses |
| 配置 | `[tool.pytest.ini_options]` 完整(testpaths/pythonpath/strict-markers/timeout/26 个 marker) | ✅ 已达标 |
| 覆盖率 | `fail_under = 100` + `[tool.coverage.*]` 完整; `--cov` 按需(CI 分组采集) | ✅ 已达标 |

### 跨文件同名 fixture(3 组, 逐一核对过实现)

| fixture | 出现处 | 实现 | 处置 |
| ------- | ------ | ---- | ---- |
| `database` | `test_locations_cases.py` / `test_repository_maintenance.py` | **逐字相同**(`migrated_database(tmp_path)`) | S1 上提 `tests/unit/conftest.py` |
| `service` | `test_ui_demo.py` / `test_ui_demo_maintenance.py` | **逐字相同**(`DemoArchiveService(delay=0)`) | S2 上提 `tests/unit/conftest.py` |
| `app` | 6 个 `test_gui_*.py` | **同名异构**(Service 不同/`wait_window` 钩子不同/屏幕 patch 不同) | 不上提, 见"明确不做" |

### 用例功能接近的识别结果

- 全库 2333 个用例里, 同文件同前缀组只有 6 组(每组 2 个), 其中**真同构**(只有数据不同)的:
  - `test_ui_palette.py`: `for_theme_dark` / `for_theme_light` / `for_theme_unknown_falls_back_to_dark` —— 同一条断言三种取值;
  - `test_backup_service.py`: `update_meta_rejects_long_note` / `_long_title` —— 对称字段超限;
  - (`create_backup_rejects_unknown_parent` / `_unknown_game` 形状不同(一个 kwarg 一个位置参数), **不合**);
- 跨文件同名用例 4 对(`test_enabling_a_game_disables_the_other_one` 等): 一半在应用服务层(`test_game_state.py` 走 `games_cases`), 一半在后端适配层(`test_sql_backend.py` 走 `SQLArchiveService`) —— **同一业务规则的两道守卫, 刻意分层, 不合并**;
- `helpers.py:396 _stamp` 与 `performance/test_home_scale.py:50 _stamp`: 名同实异(前者是清单 ISO UTC 时间戳, 后者是展示文本), **不是重复**。

## 二、执行步骤

- [x] **S1 `tests/unit/conftest.py`**: 新建, 上提 `database` fixture; `test_locations_cases.py` / `test_repository_maintenance.py` 删本地定义(用例零改动)。
- [x] **S2 同文件上提 `service` fixture**: `test_ui_demo.py` / `test_ui_demo_maintenance.py` 删本地定义。
- [x] **S3 parametrize 合并真同构组**: `test_ui_palette.py` 的 `for_theme` 三连(用 `ids` 保留"未知回落"语义)与 `test_backup_service.py` 的 `update_meta` 超限二连; 断言路径不变, 覆盖率不受影响。
- [x] **S4 静态检查**: `uv run ruff check` / `ruff format --check` / `mypy` 全过。
- [x] **S5 行为验证**: 受影响 6 文件单跑 162 passed; 默认 `testpaths`(unit+integration) 全量 **2726 passed, 9 skipped**(平台专属跳过), 0 失败。

## 三、明确不做(识别结论, 防止重复劳动)

1. **`app` fixture 不上提 integration/conftest**: 6 处同名但配方各异(`_LongTextService` vs `DemoArchiveService`、`wait_window` 量点钩子、屏幕尺寸 patch), 共同的只有 `gui_app(builder, service) + _pump` 两行; 上提只剩间接层, 差异仍留在各文件 —— 收益为负。
2. **`_pump` / `_new_app` 不统一**: 每个 GUI 测试文件一份是"文件自包含"纪律; 实现细节(轮数/秒数/标题/快捷键后端)各有理由, 是模式重复而非实现重复。
3. **跨文件 4 对同名用例不合并**: 应用层与后端适配层各守一道, 合并会丢掉一层守卫。
4. **`create_backup_rejects_unknown_*` 不 parametrize**: 两个用例调用形状不同, 强行参数化需要 lambda, 可读性反而下降。
5. **不引入 pytest-mock / pytest-assume / responses**: monkeypatch 已统一(1097 处)且等价覆盖 mocker; 断言全是裸 assert + 显式失败消息, 无"多断言软失败"需求; src 不用 requests/urllib, responses 无处可挂。
6. **不把 60 处 `migrated_database(tmp_path)` 直调改 fixture / 不动 175 处 `DemoArchiveService(delay=0)`**: 直调是"环境准备写在用例里"的显式风格, 符合"业务构造留在用例"的边界; 只有**逐字相同的 fixture 定义**才算重复。
7. **巨型文件(`test_gui_buttons.py` 297KB 等)不在本轮拆分**: 拆分是结构问题不是重复问题, 单独开一轮(见下"后续可选")。

## 四、后续可做(第二部分, S6 起)

- [x] **S6 命名消歧**: `tests/performance/test_home_scale.py` 的 `_stamp` 改名 `_display_stamp`, 消除与 `helpers._stamp` 的同名误解(定义 + 1 处调用)。
- [x] **S7 拆 `integration/test_gui_buttons.py`**(161 用例 / 7470 行) → 8 个主题文件(schedule/discovery/home/manage/transfer/backup_restore/settings/lifecycle) + `tests/button_support.py`(17 个共享 helper); 单跑 **161 passed**(8:21)。
- [x] **S8 拆 `unit/test_report_verification.py`**(89 用例 / 3424 行) → 6 个主题文件(structure/manifest/ci_workflow/quality_items/conclusions/visual_catalog) + `tests/report_support.py`; `layout` fixture 上提到 `tests/unit/conftest.py`(pytest 只从 conftest/测试模块发现 fixture); 单跑 **89 passed**。
- [x] **S9 拆 `unit/test_sql_backend.py`**(180 用例 / 3661 行) → 9 个主题文件(games/locations/backup/restore/transfer/schedule/discovery/names_artwork/home_misc) + `tests/sql_support.py`; 单跑 **180 passed**(29s)。
- [x] **S10 整体验证**: `ruff check` / `ruff format --check` / `mypy` 全过(mypy 241 文件; 为此在 pyproject 给子目录 `conftest.py` 加了带注释的 exclude —— 它们与 `tests/conftest.py` 同名, mypy 模块发现判 "Duplicate module", 而它们是纯 fixture 装配); `--collect-only` 全量 **2735 = 2726 + 9** 与拆分前一致; `test_gui_home.py -m blocker` 仍能收集到 tags_dialog 那条(装饰器没丢); 默认 testpaths 全量 **2726 passed, 9 skipped, 0 失败**(19:36), 与拆分前(S5)逐数一致。

拆分的原则: **只移动, 不改写**。每个函数体逐字保留, `pytestmark` 整体复制(Allure 层级不因此轮重构变化), 模块级 tkinter skip 块随 header 复制; 共享 helper 进 support 模块, 单文件 helper 随用例走。

执行方式与踩过的坑(供复用): 脚本化机械拆分(ast 定位顶层块 + 用例映射全集断言 + 依赖闭包归属), 过程中修了脚本四个缺陷, 都是**静默错误**, 靠 ruff/collect/单跑三层验证暴露:
1. `ast` 的 `lineno` 不含装饰器 → `@pytest.mark.blocker` 丢过(S7 首版), 修复为起点取 `min(lineno, 装饰器行)`;
2. 依赖闭包传递方向写反(被引用者应继承引用者的文件集), 修复后 helper 归属才准;
3. 散布在 defs 之间的模块级常量/模块级调用赋值(如 `verifier = _load_verifier()`、`_COVERAGE_XML`)首版没搬 → F821, 修复为顶层赋值一律进归属流程, 只有 imports/try/`pytestmark` 属于 header;
4. header 里基于 `__file__` 的锚点(`_REPO_ROOT`)在 support 移到 `tests/` 根后少一层, 手工上移一层并注释。
support 模块必须放 `tests/` 根(`--import-mode=importlib` 下子目录不在 `pythonpath`, 与 `gui_support.py` 同位); ruff 对 fixture 参数按"使用"处理, `from gui_support import gui_app` 不会被 unused-import 误删。

