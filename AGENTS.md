# AGENTS.md — 给 AI 编码助手的仓库指南

面向 AI 助手的入口文件：只保留"动手前必须知道、又不容易从代码里立刻看出来"的部分；仓库的完整规矩以 [CONTRIBUTING.md](CONTRIBUTING.md) 与 [docs/](docs/) 为准，本文只做索引与红线提示。

## 1. 项目概述

- **是什么**：用于备份、分支、恢复与导出**真实游戏存档**的桌面工具（CustomTkinter 界面 + 本地 SQLite + 应用自有的备份目录）；代码由 AI 生成，所以"怎么验证一件事"比"写得漂亮"重要得多 —— 漏一个缺陷到用户那儿就是数据损失。
- **技术栈**：Python >= 3.12、[uv](https://docs.astral.sh/uv/) 管依赖与虚拟环境、`src` 布局（hatchling）、pytest + Allure 3 报告、PyInstaller 打包；运行时依赖见 [pyproject.toml](pyproject.toml) 的 `[project].dependencies`。
- **入口**：`python -m archive_management {init|doctor|gui} --root <目录>`（不带子命令时源码版走 `init`、打包版走 `gui`，见 `app.default_command()`）；开发数据一律收敛到被忽略的 `./dev-data`。
- **单实例**：打包版同时只允许一个界面进程，第二次启动不开新窗而是把已开的那个叫到前台（退出码 3）；开发与用例不受此限。
- **目录速览**：`src/archive_management/`（`domain` `application` `infrastructure` `services` `ui` + `app.py` `config.py` `i18n.py` `logging_config.py`）、`tests/`（`unit` `integration` `performance` `security` + 共享辅助模块）、`scripts/`（测试分片/覆盖率/Allure/图标/打包的工具）、`docs/`（开发文档）、`wiki/`（面向用户的手册，唯一内容源）、`.github/workflows/`、`.github/instructions/`（按路径生效的 AI 规则）。

## 2. 快速命令

```shell
uv sync --locked                                              # 装齐全部开发依赖组
uv run pre-commit install                                     # 装提交钩子
uv run python -m archive_management init --root ./dev-data    # 初始化目录/配置/日志/数据库
uv run python -m archive_management doctor --root ./dev-data  # 健康检查(平台/目录/数据库版本)
uv run python -m archive_management gui --root ./dev-data     # 启动界面
uv run python -m archive_management gui --smoke 1             # GUI 冒烟自检(自动关闭)
```

- **静态门禁**：`uv run ruff check .` / `uv run ruff format .`（CI 用 `--check` 只查不改）/ `uv run mypy` / `uv run deptry .`；复杂度另用 `uv run radon cc -s --min C src`（输出应为空）与 `uv run xenon --max-absolute B --max-modules F --max-average F src`。
- **单点验证**（迭代时用这个，秒级）：`uv run pytest tests/unit/test_xxx.py -q`、`-k <关键词>`、`uv run pytest tests/integration -q`；性能与安全**默认不跑**：`uv run pytest tests/performance`、`uv run pytest tests/security`。
- **要拿全量结果**一律走 `uv run python scripts/run_tests_local.py --coverage`（双进程分片 + 收尾 combine/report），**不要**裸跑单进程 `coverage run -m pytest`；透传参数用 `--` 隔开，且**别再写 `--shard-count` / `--shard-index`**（片号由脚本注入，重复给会被拒绝）。规则见 [.github/instructions/local-full-run.instructions.md](.github/instructions/local-full-run.instructions.md)。
- **报告与打包**：`uv run pytest --alluredir=allure-results` → `allure generate allure-results --output allure-report`（先删旧目录，它不刷新数据）→ `allure open allure-report --port 8080`（必须经 HTTP，`file://` 打不开）；打包 `uv run pyinstaller --noconfirm --clean packaging/archive-management.spec`（一条命令同时产出 onedir 与 onefile），编完用 `uv run python scripts/smoke_packaged.py` 冒烟（仅 Windows）；改了 `app.svg` 后 `uv run python scripts/build_icon.py`。细节见 [docs/development.md](docs/development.md)。

## 3. 后端架构

- **依赖方向**：`domain`（纯模型与规则）← `application`（用例编排，只依赖仓储接口与注入的能力）← `infrastructure`（SQLite 仓储/迁移）与 `services`（快照、调度、进程探测、平台探测、回收站、路径判定、封面缓存、审计日志）。业务层**不直接**碰 Tkinter / HTTP / 文件系统，能力靠参数注入以便替换与测试。
- **界面与业务的分界线是 `src/archive_management/ui/backend.py` 的 `ArchiveService` Protocol**：界面只依赖它。真实实现是 `ui/sql_backend.py`，内存演示实现是 `ui/demo_backend.py`；**新增一个协议方法必须两边都实现**，否则 GUI 用例会红。
- **数据层**：schema 与增量迁移集中在 [infrastructure/schema.py](src/archive_management/infrastructure/schema.py)（`(版本号, SQL)` 列表，启动时在事务内按版本前进）。**只许追加新迁移，不许改写旧迁移**；SQL 全手写、不用 ORM，写列名要字面量（`f-string` 拼列名会被 ruff S608 拦）。
- **主要服务**：`services/snapshot.py`（临时目录 + 逐文件 SHA-256 + 原子提交，绝不覆盖已有备份）、`application/backup.py` + `application/restore.py`（分支/恢复/安全点）、`services/scheduler.py`（APScheduler，`ManualBackend` 供用例手动触发）、`services/processes.py`（进程名探测，名称归一化后"互相包含"匹配）、`services/pathcheck.py`（规范化/危险目标）、`services/platform_scan.py` + `services/steam_cloud.py` + `services/artwork.py`（平台探测、Steam 云清单、封面缓存）、`services/trash.py`（只移入回收站，绝不永久删除）。
- **审计日志**：所有用户操作走 `services/audit.py` 的 `log_action(action, *, basic=False, **fields)` —— 高风险操作 INFO、基础操作 DEBUG；路径字段用 `redacted_path()` 脱敏，**日志不落凭据、文件内容与完整敏感路径**。日志只写文件，不落数据库。
- **资源与文案**：用户可见文案全在 `resources/i18n/{zh-CN,en}.json`（经 `i18n.tr(key, **kw)` 读取，两份必须同键）；`resources/` 顶层的固定数据清单（如 `excluded-games.json`）由提交钩子压成单行，属正常现象。
- **动手改后端前读** [.github/instructions/backend-layering.instructions.md](.github/instructions/backend-layering.instructions.md)（分层红线、两个后端一致性、迁移与审计纪律）；要加/改一个协议方法就按 skill [archive-service-protocol-method](.github/skills/archive-service-protocol-method/SKILL.md) 的步骤走。

## 4. 前端架构

- **CustomTkinter 单窗口应用**：`ui/main_window.py` 的 `ArchiveApp` = 顶栏 + 页面区（`AppPage.HOME` / `AppPage.DETAIL`，两页同格叠放切换）+ 底部状态条；主页 `ui/home_page.py`（游戏库与游戏发现两个分区）、发现面板 `ui/discovery_page.py`、启停队列 `ui/activation_page.py`。
- **独立窗口是组合式**（持有自己的 `CTkToplevel`，**不要子类化 `CTkToplevel`** —— mypy `disallow_subclassing_any` 会拦）：`ui/manage_window.py`（游戏设置/存档位置）、`ui/schedule_window.py`（定时任务）、`ui/settings_window.py`；同一时刻只开一个工作区窗口（重复点=聚焦，换入口=提示先关）。
- **绘制与主题**：`ui/widgets.py` 的 `UiKit` 集中建控件并登记"换主题时怎么重绘"（`kit.apply(palette)` / `kit.repaint_button`）；颜色只能取 `ui/palette.py` 的具名 token；间距/圆角/字号只能用 `ui/metrics.py` 与 `ui/typography.py` 刻度上的值；`ui/rendering.py` 与 `ui/window_icon.py` 在导入时给 CustomTkinter 打两个必要的补丁（偶数取整、窗口图标），不要移除。
- **文本适配**：`ui/textfit.py` 的 `fit_text` 按像素宽度裁剪并补省略号，`ui/widgets.py` 的 `fit_label` / `track_fit` / `track_wraplength` 负责"跟随宽度重裁 + 挂悬停提示"。`wraplength` 只压请求宽度，**两个要一起用**，否则长文本会被静默硬裁。
- **展示模型**：视图数据类（`GameSummary` / `GameDetail` / `BackupItem` / `TaskStatus` / `ScheduleItem` / `HomeBoard` …）都在 `ui/models.py`，界面只读它们；不要在各窗口里各写一遍格式化逻辑。
- **异步**：耗时操作走 `ArchiveApp._submit(work, ok)` 在 worker 线程执行，结果经队列由 `_poll_messages`（100ms 自排）搬回主线程。**后台线程里绝不碰 Tk**；等待结果用 `_drain`（显式轮询直到忙碌标志复位），只 `_pump` 一轮不够。
- **辅助设施**：`ui/keyboard.py`（Tab 顺序、焦点环、Esc/回车接线，绑定一律 `add="+"` 不许顶掉控件自己的处理）、`ui/dialogs.py`（模态对话框）、`ui/pickers.py`（文件/目录选择，通过参数注入以便替身）、`ui/dropdown.py`、`ui/tree_view.py` + `ui/tree_layout.py`（分支图画布，布局是纯函数）。
- **动手改界面前读** [.github/instructions/ui-conventions.instructions.md](.github/instructions/ui-conventions.instructions.md)：文案/颜色/刻度/主题重绘/长文本/线程的判据汇总，以及改完必跑的那几个界面守卫。

## 5. 关键约定

- **文案**：一律 `tr("key")`，不许在界面代码里写死；`.py` 里**不许出现全角标点**（RUF001–003 会拦），中文标点只出现在 i18n 资源里；中文文案用全角标点、末行不许只剩标点。
- **颜色/间距**：颜色只从调色板取（输入控件边界用 `input_border`、面板外框用 `border`）；间距/圆角/字号必须在刻度上 —— 越界与写死颜色都有守卫，别手工争论。
- **按钮文字写"点击后会发生什么"**（`停用游戏` / `启用游戏`、`暂停` / `继续`），当前状态由旁边的**状态词**表达；两件事不许由同一句文字兼任。
- **Tab 顺序 = 控件创建顺序**：新增页头控件时要按 grid 顺序创建，否则键盘顺序会错。
- **归档的权限边界是单一来源**：`domain/game_rules.py` 的 `GameAction` / `ARCHIVED_ACTIONS` / `action_allowed` / `is_active`；归档后只保留删除、导出、取消归档、打开详情。界面用 `GameSummary.allow()`，不要在窗口里各写一遍 `if archived`。
- **定时任务的启停与"游戏是否启用"脱钩**：停用游戏不让任务暂停，只让它到点被跳过（`schedule.skipped`）；归档才顺手暂停。改动这块要同时看 `ui/models.py` 的四种"下次备份"口径。
- **注释写"为什么"**：这个仓库的注释里保存着实测数字、平台差异与"当时为什么选这条路"，改动时保留这类信息，别当噪音删掉。
- **markdown 一段一行**：不按宽度折行（列表项续行并回同一行），表格/代码块/YAML frontmatter 不受影响；长行只改一个词时 diff 才读得懂。
- **提交**：分支 `main` 禁止直接提交（只收 `dev` 或 `bugfix/*` 的 PR）；提交信息写成 `<范围>: <一句话>`，**正文要精炼**（最多两三条要点，只写"为什么"与关键实测数字，别写成大段说明 —— 细节留在 PR 描述、代码注释与 `docs/` 里）；钩子会就地排版并重新暂存，属设计行为。
- **一次性探针用完即删**：为量一个数字写的脚本不要留在仓库里；长输出先落盘再读。

## 6. 验证流程

1. **先窄后宽**：先跑受影响模块的用例（秒级），再跑静态门禁，最后才全量。
2. **静态门禁**：`ruff check .` → `ruff format --check .` → `mypy`；动过函数再加 `radon cc -s --min C src`（应空）与 `xenon`（CI 的 analysis 作业只在这里才跑得到，本地别漏）；依赖或导入变了跑 `deptry .`。
3. **全量 + 覆盖率**：`uv run python scripts/run_tests_local.py --coverage`。覆盖率是**硬门禁：100%**（`[tool.coverage.report] fail_under = 100`）。亏了就补用例；确实够不着的行才写豁免，且必须写对：`# platform: <平台> - 原因` 或 `# pragma: no cover - 原因`（原因 >= 5 字、不许标在 `def`/`class` 行、`no branch` 只能标分支行，规则由 `tests/unit/test_coverage_pragmas.py` 校验）。
4. **守卫必须"咬"**：新写的守卫/用例要故意改坏一次，确认它真的变红，再还原 —— "看起来在守、其实空转"的断言比没有断言更糟。
5. **红了先分清偶发与回归**：判据是"单跑同一模块也绿 + 失败点是已知的时序敏感用例"，两条都满足才按偶发处理；已知偶发与其指纹（表头对齐族、下拉浮层、超时杀片）记在 [.github/instructions/local-full-run.instructions.md](.github/instructions/local-full-run.instructions.md)。
6. **GUI 用例要在三平台想一遍**：CI 的窗口尺寸不由用例决定、字体度量会变、合成按键走焦点窗口、行/卡片会被重建。动界面或写 GUI 用例前先读 [.github/instructions/gui-tests-on-ci.instructions.md](.github/instructions/gui-tests-on-ci.instructions.md)；改滚动区还要读 [ctk-scrollbar-twitch](.github/instructions/ctk-scrollbar-twitch.instructions.md)。
7. **失败现场留证**：每次失败都会挂 coredumpy dump 与界面截图，读法 `coredumpy.Coredumpy.load_data_from_path(<allure-report/data/attachments/*.dump>)` 得到 `{frame, threads, files}` 后沿 `frame.f_back` 走到测试帧读 `f_locals`（**还原出来的是只读代理：读属性可以，调方法不行**）。报告字段：结果详情在 `allure-report/data/test-results/<id>.json` 的 `error.message` / `error.trace` 与 `attachments`。
8. **提交时**钩子自动跑 ruff check/format、compact JSON、图标核对、mypy、deptry、对比度守卫与 `pytest --min-severity=critical`（数据安全 + 核心逻辑子集）。

## 7. 质量检查

- **两层门禁**：本地提交钩子（快，只看最高两档严重等级）与 CI（三平台全量 + 性能 + 安全 + 视觉回归 + 覆盖率门禁 + Allure 原生质量门）。作业清单与矩阵见 [docs/testing-ci.md](docs/testing-ci.md)。
- **测试分层与等级**：`tests/unit`（纯逻辑）/ `tests/integration`（文件系统、真实窗口）/ `tests/performance` / `tests/security`；每个模块在 `pytestmark` 里**恰好声明一个**严重等级（blocker/critical/normal/minor/trivial，判定标准见 [docs/testing.md](docs/testing.md) 第 1 节），并带齐 epic/feature/story/layer 四个 Allure 标签（缺了收集期就报错）。
- **风格**：裸 `assert` + 能定位问题的失败消息（太长就把消息存进变量，保持单行）；环境用 `tmp_path`、替身用 `monkeypatch`（仓库里不用 `unittest.mock`）；用例自带数据，不依赖执行顺序。
- **别绕过门禁**：不许用无原因的 `pytest.skip` 静默掉用例（Tk 环境问题只认白名单症状）、不许放宽 `fail_under` 或复杂度阈值、不许用"既有问题"当借口跳过检查。CI 红了你认为既有问题，在 PR 里写明。
- **UI 守卫族**（改界面前先知道它们在守什么）：`tests/unit/test_ui_metrics.py`（间距/圆角/字号刻度与站点总数）、`test_ui_contrast.py`（对比度三档阈值）、`test_ui_input_border.py`（输入控件边界色）、`test_gui_styles.py`（语义色与危险动作）、`test_gui_layout.py`（对齐/边框/长文本）、`test_gui_states.py`（忙碌与可用性）、`test_gui_copy_quality.py`（文案截断与空状态）。
- **CI 工作流**：`.github/workflows/ci.yml`（PR/push 全量 + 被 nightly 调用的 `workflow_call` 入口）、`nightly.yml`、`release-build.yml`（只读构建，被门禁与发布档共用）、`release.yml`（合入 `main` 后发 Release）、`wiki.yml`。改工作流前读 [.github/instructions/github-actions-pitfalls.instructions.md](.github/instructions/github-actions-pitfalls.instructions.md)，改报告链路前读 [allure-reporting](.github/instructions/allure-reporting.instructions.md)（"改哪一处必须同步改哪几处"）。

## 8. 文档导航

| 文档                                                                    | 什么时候读它                                                                                                                            |
| ----------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| [README.md](README.md)                                                  | 项目概览、平台支持矩阵、文档索引                                                                                                        |
| [CONTRIBUTING.md](CONTRIBUTING.md)                                      | **动手前必读**：环境、常用命令速查、提交前门禁、覆盖率口径、测试怎么写、代码与界面约定、提交与 PR 流程                                  |
| [docs/features.md](docs/features.md)                                    | 界面结构与各功能（备份/分支/定时任务/恢复/原始目录/导入导出/日志）的现行行为                                                            |
| [docs/development.md](docs/development.md)                              | 命令行、配置文件、应用目录、质量门禁、应用图标流水线、打包与发布                                                                        |
| [docs/testing.md](docs/testing.md)                                      | 测试分层与等级、共享测试设施、性能与安全测试、写用例的通用纪律、新增测试清单                            |
| [docs/testing-ui.md](docs/testing-ui.md)                                | 界面用例的环境与收尾纪律、时序与量法、尺寸/配色/交互/文案判据                                          |
| [docs/testing-ci.md](docs/testing-ci.md)                                | CI 作业矩阵、报告自检、失败现场留证、分片与覆盖率口径、本地生成报告                                    |
| [docs/library.md](docs/library.md)                                      | 统一游戏主页、列表与海报、分类筛选、游戏发现、封面与图标                                                                                |
| [docs/export-format.md](docs/export-format.md)                          | 导入导出包的文件结构、字段、上限与读取纪律（改格式先看它）                                                                              |
| [docs/hotkeys.md](docs/hotkeys.md)                                      | 全局快捷键的默认键位、录制流程与实现约定                                                                                                |
| [docs/platforms.md](docs/platforms.md)                                  | Windows/macOS/Linux 支持矩阵、可信存档来源、平台注意事项                                                                                |
| [docs/ui-notes.md](docs/ui-notes.md)                                    | 从界面移出的说明文案（备查）与几处交互取舍的记录                                                                                        |
| [wiki/](wiki/)                                                          | 面向**不接触命令行**的用户手册（也是手册唯一内容源，合入后由工作流镜像到 GitHub Wiki）；这里不许出现 `uv run` / 子命令                  |
| [.github/instructions/](.github/instructions/)                          | 按路径自动生效的 AI 规则：全量跑法、GUI 用例在 CI 上的坑、滚动区抽搐、GitHub Actions 坑、Allure 报告体系、后端分层、界面判据            |
| [.github/skills/](.github/skills/) · [.github/agents/](.github/agents/) | 按需触发的技能与自定义 agent：`archive-service-protocol-method`（加/改协议方法的完整清单）、`test-writer`（按仓库风格写用例并跑到门禁） |

- **文档分工别串台**：`wiki/` 面向用户、`docs/` 面向实现、`README.md` 只留概览与索引、开发命令只在 `CONTRIBUTING.md`。行为改了且用户看得见就同步 `wiki/` 与 README；机制改了同步对应 `docs/*.md`。
- 上面的路径没覆盖到的取舍与待办写在 issue 里，别只留在聊天记录或 PR 描述中。
