# 开发、命令行与发布

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）

```shell
uv sync --locked     # 安装依赖
uv run pre-commit install
```

开发依赖按**作业**拆成五组（`test` / `coverage` / `quality` / `analysis` / `package`，见 `pyproject.toml` 的 `[dependency-groups]`）：CI 里每个作业只装自己需要的那一组（`uv sync --locked --no-default-groups --group ...`，另外靠顶层 `UV_NO_SYNC=1` 拦住 `uv run` 的隐式 sync），本地则通过 `[tool.uv] default-groups` 一次装齐，所以上面的命令与以前完全一样。要只跑某一类检查时也可以手动 `uv sync --no-default-groups --group test`（注意它会把其它组从 `.venv` 里卸掉）。

上面的命令可以在**任意目录**执行。项目以**可编辑包**装进 `.venv`（`pyproject.toml` 的 `[tool.uv] package = true`）：`uv sync` 往 site-packages 里放的只是一个指向 `src` 的 `.pth`（几十字节）与一份 dist-info，**源码不进 site-packages**、改代码不需要重装，因此 `python -m archive_management` 与绕过 `uv run` 直接调用 `.venv` 里的解释器（Windows 是 `.venv\Scripts\python.exe`，Linux 是 `.venv/bin/python`）都能导入自身源码，不再需要 `PYTHONPATH`。CI 里则相反：用例靠 pytest 的 `pythonpath` 导入源码、静态检查靠 `mypy_path`，所以每处 `uv sync` 都带 `--no-install-project`（不构建、也不必下载构建后端），只有质量作业装一次并跑一次 CLI 冒烟 —— 见 [testing-ci.md](testing-ci.md) 与 `.github/workflows/ci.yml`。

**首次 `uv sync` 需要联网取一次构建后端**：可编辑安装必须真的构建一次，uv 会去解析 `pyproject.toml` 的 `build-system.requires`（`hatchling`）。构建依赖不参与 `uv.lock`，所以锁文件齐备也可能卡在这一步，报 `Failed to resolve requirements from build-system.requires` / `Failed to fetch: https://pypi.org/simple/hatchling/`。访问 PyPI 不畅时先给 uv 配镜像：

```shell
UV_DEFAULT_INDEX=https://mirrors.aliyun.com/pypi/simple uv sync --locked   # 一次性
```

```toml
# 一次配置长期有效：用户级 ~/.config/uv/uv.toml（Windows 是 %APPDATA%\uv\uv.toml）
[[index]]
url = "https://mirrors.aliyun.com/pypi/simple"
default = true        # 标为默认索引，同时停用 PyPI 官方索引
```

装过一次之后构建后端就进了 uv 缓存，之后的 `uv sync --locked` 不再需要网络（`--no-install-project` 的那几处更是从不构建）。

镜像与锁文件有一个连带的坑：`uv.lock` 里记的是**每个包来自哪个索引**（官方 `https://pypi.org/simple`），配了镜像之后 uv 就认为锁过期 —— `--locked` 直接报 `The lockfile needs to be updated`（不会写文件），而普通的 `uv sync` / `uv run` 会**把锁里的地址全改写成镜像地址**（这种锁千万不要提交）。在只能走镜像的机器上应该用 `--frozen`（等价于 `UV_FROZEN=1`）：按锁安装、不校验、不改写。真在那种机器上改了依赖（`uv lock` / `uv add`）之后，把地址换回来再提交：

```shell
sed -i 's#https://mirrors.aliyun.com/pypi/simple#https://pypi.org/simple#g; s#https://mirrors.aliyun.com/pypi/packages/#https://files.pythonhosted.org/packages/#g' uv.lock
```

项目采用 `src` 布局（包名 `archive_management`），业务层不直接调用 Tkinter、HTTP 或文件系统：领域模型与用例通过接口注入基础设施，便于替换实现与测试。

```text
src/archive_management/
  app.py                 # 命令行入口与依赖组装
  config.py              # 应用配置的加载、校验与持久化
  domain/                # 游戏、备份、分支、发现等领域模型与纯规则
  application/           # 用例：备份、恢复、删除、发现、主页
  infrastructure/        # SQLite、schema 迁移、路径、仓储
  services/              # 快照、调度、快捷键、探测、审计、回收站等能力
  ui/                    # CustomTkinter 窗口、页面、对话框、后端适配
tests/
docs/
packaging/               # PyInstaller spec
```

## 命令行

```shell
uv run python -m archive_management init --root ./dev-data    # 初始化目录/配置/日志/数据库
uv run python -m archive_management doctor --root ./dev-data  # 健康检查（平台、目录、数据库版本）
uv run python -m archive_management gui --root ./dev-data     # 启动图形界面
uv run python -m archive_management gui --smoke 1             # GUI 冒烟自检（自动关闭）
```

- 不带子命令运行时默认执行 `init`；**打包产物例外：不带子命令时直接开图形界面** —— 双击的正是这条路，而窗口化的 exe 没有控制台，`init` 的报告只会打进一个不存在的控制台（屏幕上什么都没有，看起来就是打不开；2026-10-11 报的就是它）。判据是“解析后的子命令为空”，所以打包版只给 `--root` 之类的全局参数也会开界面（见 `app.default_command` 与 `tests/unit/test_cli.py` 的三条守卫）。
- `--root` 可以把全部数据收敛到指定目录（便携模式与开发调试），此时配置、数据、日志、缓存都在该目录下；不加则使用系统约定的应用目录。
- **打包版同时只允许开一个界面进程**（2026-10-12 要求）：两个进程各持同一个数据库、备份根与全局快捷键注册会互相踩，用户看到的是“点了没反应”“状态突然回退”这类没有报错的怪事。
  - **第二个实例不开新窗、也不弹提示，而是把已经开着的那个叫到前台**（同一天用户的追加要求：“重复打开窗口应该直接跳转到上次打开的窗口”）：它往 `data/instance.focus` 写一条 `pid=… at=…` 请求就退出（**退出码 3**，与 argparse 的 2 分开），已经开着的那个在 100ms 轮询里看到就 `deiconify + lift + focus_force`（**先抬窗口、抬完再清文件**：文件没了就是回执）。
  - **Windows 还要多一步**（2026-10-12 实测的坑）：前台锁定 —— 后台进程自己 `deiconify/lift` 只把窗口恢复成**可见**，前台焦点抢不过来（实测：第二个实例跑完前台仍是编辑器）。而“用户刚双击起来的那个进程”手上有这个权利，所以第二个实例另外调一次 `user32.AllowSetForegroundWindow(<已有实例的 pid>)`，并**等回执再退**（那份权限可能随它退出而失效）。日志里 `granted=` / `handled=` 就是这两步的结果：窗口没到前台时一眼能分清是“权限没交出去”还是“对方没响应”。其它平台没有这道闸（X11 由窗口管理器决定，macOS 由系统处理），那一步是空操作。
  - 为什么用“文件 + 轮询”而不是窗口消息/跨进程句柄：一是软件**不执行外部程序**，二是这条路三个平台完全一样，不需要每个平台一套窗口系统 API。
  - 闸门只在打包版生效（`app.is_packaged()`）：开发与用例都要能同时开多个窗口（分片跑集成用例时每片都在自己的进程里建真窗口），而且集成用例直接调 `ui.main_window.run_gui`，根本不经过这条闸门。
  - 诊断用的逃生门：设 `ARCHIVE_MANAGEMENT_ALLOW_MULTI=1` 就照开（想同时跑两个打包版做对比时用）。
  - 锁是 `data/instance.lock` 上的操作系统级锁（`services/single_instance.py`）：锁跟着进程走，进程无论怎么消失（正常退出/崩溃/被任务管理器结束）都由系统释放，所以**不会留下把应用永久锁在门外的陈旧锁**。锁与请求文件都落在数据目录，因此 `--root` 便携实例各用各的。
  - 验收：打包版开两个进程 —— 第二个应当让第一个的窗口到前台（**最小化时会被恢复**，前台窗口标题应当变成应用名）、自己退出码 3，两边的日志里各多一行（`已请它把窗口叫到前台(… granted=True handled=True)` / `第二个实例请求到前台(…)`）。前台的判定可以直接量：看当前前台窗口的标题是不是应用名。
- 全局 `--verbose` 本次启动就打开调试日志（默认只记录 INFO 及以上的高风险操作）；同一个开关也可以在软件设置里拨（写入 `config.json` 的 `logging.debug`，默认关闭，拨完立即生效）。

## 配置文件

配置文件是配置目录下的 `config.json`（主题、全局快捷键、日志参数、界面字号、是否记住窗口大小与位置、上次关闭时的窗口尺寸位置），由 pydantic 严格校验：未知字段、版本不符、非法快捷键都直接拒绝。

`window` 段（`width`/`height`/`x`/`y`）是程序自己写的：关窗时记下主窗口当时的尺寸与位置（**最大化/最小化/全屏关闭时跳过这一项**，见 `ui/main_window.py` 的 `current_window_geometry`），下次打开时夹进当前屏幕再用；四项缺一项就当没记过（用设计尺寸）。手改这四个值时超范围会被逐字段修复剔掉，不会把窗口摆到屏幕外。

**内容非法时会自动把文件还原为默认值**，并在日志/界面里说明（CLI 的 `init` 与 `doctor` 会在输出里列出一行"配置还原"），应用照常启动；原文件会改名保留为同一目录下的`config.json.invalid`，方便对照自己改错了什么——既不让写坏的配置卡住启动，也不静默丢掉用户的改动。

### 应用目录：数据、日志、备份与缓存都在哪

四类目录由 :mod:`platformdirs` 解析（`infrastructure/paths.py`），**打包版与源码版完全一致，且与 exe / 源码所在目录无关**：

| 角色                             | Windows                                   | Linux                                                         | macOS                                              |
| -------------------------------- | ----------------------------------------- | ------------------------------------------------------------- | -------------------------------------------------- |
| 配置（`config.json`）            | `%LOCALAPPDATA%\ArchiveManagement\`       | `$XDG_CONFIG_HOME/ArchiveManagement`（`~/.config/…`）         | `~/Library/Application Support/ArchiveManagement/` |
| 数据（数据库 / 备份 / 导出）     | 同上                                      | `$XDG_DATA_HOME/ArchiveManagement`（`~/.local/share/…`）      | 同上                                               |
| 日志（`archive-management.log`） | `%LOCALAPPDATA%\ArchiveManagement\Logs\`  | `$XDG_STATE_HOME/ArchiveManagement/log`（`~/.local/state/…`） | `~/Library/Logs/ArchiveManagement/`                |
| 缓存（游戏译名、封面/图标）      | `%LOCALAPPDATA%\ArchiveManagement\Cache\` | `~/.cache/ArchiveManagement`                                  | `~/Library/Caches/ArchiveManagement/`              |

- 数据目录下是 `archive-management.db` + `backups/`（应用自有备份，**绝不直接写用户的原始存档目录**）+ `exports/`（自动导出的归档包）+ `artwork/`（用户自己指定的封面/图标，用到时才创建）+ `instance.lock` / `instance.focus`（打包版的单实例锁与“请到前台”请求文件，见「命令行」一节；两个文件删了都会自动重建）；缓存目录下是 `game-names.json` 与 `artwork/`（下载来的封面/图标，可安全删除）。
- **换版本、移动或删掉程序目录都不会动到这些目录**；想彻底重置就删掉它们（清理步骤见 `wiki/10 常见问题与故障排查`）。
- **想看当前生效的真实路径**：打包版 `ArchiveManagement.exe doctor`、源码版 `uv run python -m archive_management doctor`，它会把配置 / 数据 / 日志 / 数据库 / 备份根逐行打出来（不带参数启动的 `init` 也会打同一张表）。
- **便携模式**：给程序加 `--root <目录>`，四类目录都收敛到 `<目录>\{config,data,logs,cache}` 下（打包版可以把参数写进快捷方式；“只给 `--root`”也直接开界面，见「命令行」一节）。

## 质量门禁

常用命令的速查表在 [CONTRIBUTING.md](../CONTRIBUTING.md) 第二节，这里只讲这些检查各自的口径。

本地提交钩子 + CI 里跑的检查（用例与覆盖率在 Windows / Ubuntu / macOS 三个平台各跑一遍；其余几项是公共检查，CI 只在 Ubuntu 跑一遍，结论归入报告里显式声明的 `Common` 环境）：

```shell
uv run ruff check .
uv run ruff format --check .
uv run mypy                     # 宿主平台(本地钩子里跑的也是这一条)
uv run pytest                   # 用例(覆盖率见下面那条)
uv run deptry .                 # 依赖卫生: 未声明 / 多余 / 传递依赖(本地钩子也会跑)
```

**覆盖率是硬门禁（`fail_under = 100`），但它要按平台口径判**：`uv run pytest --cov` 给的是**合并口径**的粗数字（不做平台排除，因为 pytest-cov 在导入根 conftest 之前就构造好了 Coverage 对象），与门槛不是一个口径。按门禁核走命令行两步（全量约 14 分钟；只想定位单个文件就配 `--data-file` + `--include`，另见 [CONTRIBUTING.md](../CONTRIBUTING.md) 第四节）：

```shell
uv run python scripts/coverage_platform.py write   # 生成 .coverage-platform.rc(不入库)
uv run python -m coverage run --rcfile=.coverage-platform.rc -m pytest -q
uv run python -m coverage report -m --rcfile=.coverage-platform.rc
```

平台专属的类型检查（CI 里各在对应平台上的 `quality-platform` 作业跑一次，本地想复现就手动执行）：

```shell
uv run mypy --platform win32    # Windows 专属分支，在 Windows 上跑
uv run mypy --platform darwin   # macOS 专属分支，在 macOS 上跑
```

这两条只在对应平台上执行（`scripts/create_allure_quality.py` 的 `platform` 组）：`--platform` 只把类型检查指向某支代码，不代表执行环境；在别的平台上跑虽然也能过，但报告里"Windows 的结论"就是假的归属，所以脚本会按平台自己挑，放错平台的组直接报错。

与平台无关的静态分析与依赖检查（CI 的 `analysis` job 跑一次，本地想复现就手动执行）：

```shell
uv run bandit -r src   # 源码里的危险模式(exec/eval、弱随机、subprocess、硬编码口令等)
uv run pip-audit       # 已安装依赖的已知漏洞
uv run radon cc -s --min B src                                   # 复杂度报告
uv run xenon --max-absolute B --max-modules F --max-average F src # 复杂度门槛
```

**复杂度的两把尺子同分**：门槛取 `10`，与 Ruff 的 `[tool.ruff.lint.mccabe] max-complexity`完全相同；Radon 的等级对应 1-5 / 6-10 / 11-20 / …，所以“不超过 10”就是“最差只能到 B 级”，`xenon` 的模块级与平均复杂度不设限（Ruff 并不检查这两项）。但两者的**刻度不同**：Radon 会把 `with`、`assert`、布尔运算也算作分支，同一段代码通常比 Ruff 的 C901 高 2~5 分，因此写新函数时以 Radon 为准（`radon cc --min C src` 当前为空，说明全部函数都在 B 级以内）。`tests/unit/test_report_quality_items.py` 里有守卫，保证两处数值不会各自漂移。

`deptry` 的两处说明：CI 的静态分析作业带 `--no-install-project`（项目没装进 `.venv`），依赖是按"仓库源码"解析的，所以 `known_first_party` 里同时列了 `archive_management` 与 `tests` 下的共享辅助模块。

`bandit` 只扫 `src`：用例里满是 `assert` 与故意构造的脏数据，扫它们只会制造噪音；运行期行为由 `tests/security` 负责（两者互补：Bandit 拦“写法危险”，安全用例拦“行为可被利用”）。

格式化用 `ruff format`，其配置项**刻意锁定为 Black 稳定版的默认风格**（`preview = false`、双引号、空格缩进、尊重魔法尾随逗号），所以换成 ruff 不会把存量代码重排。两处已知差异没有用配置去弥合，而是写成了编码约定：

- **行宽按显示宽度计算**：Ruff 把中日韩宽字符算 2 列（Black 只按字符数），所以含中文的长字符串会比 Black 更早折行；
- **断言消息保持单行**：`assert cond, msg` 放不下时两个工具的折行位置不同（Ruff 折消息、Black 折条件），因此约定把长提示先存进变量（如 `hint = "..."`），断言本身保持一行。若拆分，钩子会永远在两个工具的输出来回跳。

想核实"代码确实仍然符合 Black 风格"，可以用一次性环境跑 Black（不必装进项目依赖）：

```shell
uvx black --diff --line-length 88 --target-version py312 src tests scripts
```

输出应为 **0 个文件需要改动**，且与 `ruff format --check .` 同时成立。

本地提交钩子只对本次变动的 Python 文件执行 Ruff（检查 + 格式化）、mypy，并运行 `blocker`+`critical` 子集（= 数据安全与核心逻辑，等级定义见 [testing.md](testing.md) 的严重等级表），另外还会跑一次 `deptry`（依赖变更与导入变更都得重查，所以它的触发范围含 `pyproject.toml` / `uv.lock`），并把 `src/archive_management/resources/` 顶层的**固定数据清单**（如平台工具排除清单）自动压回单行紧凑 JSON（`scripts/compact_json.py`）。这个钩子跑完会把文件 **`git add` 进暂存区**（这次没改写也会加一次，顺手把索引里可能残留的多行版本同步成单行）：工作区与索引一起变成单行后 pre-commit 就不算它“弄脏了文件”，本次提交直接带上转换好的内容，不需要重新 `git add`；万一把结果加不进索引（没有 git 等），才会退回“就地改写并拦下提交”。`resources/i18n/*.json` 是给人审校的文案资源，不受影响。

**排版钩子也是这套做法（2026-10-05 起）**：`ruff format` 那条钩子不再用 `--check`，而是 `scripts/ruff_format_and_stage.py` —— 就地排版、把结果 `git add` 回暂存区，于是**本次提交一次就带上排版好的内容**（以前的流程是“提交 → 被拦下 → 手工 `uv run ruff format <文件>` → `git add` → 再提交”）。改动不会被吞掉：排版结果与工作区一致地进索引，pre-commit 因此不算它“弄脏了文件”；只有加不进索引（没有 git、权限问题）时才退回“拦下提交”。CI 那侧的 `uv run ruff format --check .` 保持只读判据不变（见 [testing-ci.md](testing-ci.md) 的质量门禁一节）。

CI 会在 Windows、Ubuntu、macOS 上运行全量测试（三个平台的矩阵与分片数见 [testing-ci.md](testing-ci.md)），并单独执行性能基准、安全测试与上面那批静态分析工具，最后合并成一份 Allure 报告。测试分类与基准阈值见 [testing.md](testing.md)，报告汇总见 [testing-ci.md](testing-ci.md)。

### 想在本地看 Allure 报告：

1. `uv run pytest --alluredir=allure-results`
1. `allure generate allure-results --output allure-report`
1. `allure open allure-report`（标题、界面语言、端口、失败归类、运行变量与环境白名单都在 `allurerc.mjs` 里定好了；地址冲突时加 `--port 8081`）（完整命令、常见坑与报告自检见 [testing-ci.md](testing-ci.md)）。**生成报告时请停在仓库根目录**：`allurerc.mjs` 就在这里，它负责把结果上的平台标签映射成报告的"环境"（Windows/macOS/Linux），换目录执行会让环境静默退回 `default`（自检脚本会把这种情况判为失败）。

用例失败时会**自动留现场**（coredumpy dump + 界面截图 + 摘要，机制与纪律见 [testing-ci.md](testing-ci.md)）：dump 落在仓库根的 `crash-dumps/`（已被忽略），用 `coredumpy load crash-dumps/<用例>.dump` 进 pdb，或在 VSCode 里用 coredumpy 扩展右键打开；不想留就加 `--crash-dump-depth=0`。

CI 的 pytest 作业是**分片执行**的（三平台各 3 片并行，报告作业再合并结果与覆盖率）。本地想复现同一套流程：`--shard-count` / `--shard-index` + `scripts/merge_allure_results.py` + `uv run coverage combine`，命令与实测数据见 [testing-ci.md](testing-ci.md)。

## 文档与 Wiki

文档分三层，**不要互相复制内容**：

- `wiki/`：**面向用户的使用手册**，按章节拆分（`Home.md` 是目录、`_Sidebar.md` 是侧边栏、其余是 `NN 标题.md`）。它是手册的**唯一内容源**，改手册就改这里。
- `docs/`：**面向开发与实现**的说明（界面结构、平台差异、测试体系、打包发布、CI 优化）。
- `CONTRIBUTING.md`：**面向贡献者**的指南（常用命令速查、提交前要过的门禁、覆盖率口径、测试与代码约定、PR 流程）。

仓库里的 markdown **不按宽度折行**：一段/一条写成一行（改文档时别手动折行），这样 diff 里改一个词就是一行；表格、代码块与 frontmatter 不在此列。

同步机制：`.github/workflows/wiki.yml` 在 `wiki/**` 变化时（push 到 `dev`/`main`）或手动触发时，把 `wiki/*.md` **镜像**推送到 GitHub Wiki 的 git 仓库。两点注意：

1. **首次需要人工在网页上开一次**：仓库 Settings → Features 勾选 Wikis，并在 Wiki 里保存过至少一页 —— GitHub 是在"第一次保存页面"时才创建 `<repo>.wiki.git`，之前工作流会 clone 失败（报错信息里直接写了这一步）。
2. 页面里的仓库链接用 `{{REPO_URL}}` 占位符（由工作流替换成真实地址），**不要写仓库相对链接**（Wiki 与仓库不在同一棵目录树里，相对链接会变成"不存在的页面"且不报错）。这两条与"章节编号/侧边栏链接齐全/镜像语义"一起由 `tests/unit/test_wiki_sync.py` 守住。
3. **手册面向不接触命令行的用户**：只写窗口里的操作、数据与日志位置，不写 `uv run` / `python -m` / `--root` / 子命令这类内容（启动、初始化、便携模式属于 README、[CONTRIBUTING.md](../CONTRIBUTING.md) 第二节的常用命令速查与本文档）。软件按平台各自编译发布，用户拿到的是可执行包，双击即用。同样由 `tests/unit/test_wiki_sync.py` 守住。

## 应用图标

图标的**唯一真相**是 `src/archive_management/resources/app.svg`（只改这一个文件），界面与打包读的都是它的产物：

| 产物             | 给谁用                                                                           |
| ---------------- | -------------------------------------------------------------------------------- |
| `app.ico`        | exe 的 PE 资源（资源管理器/Alt-Tab）；内含 16/24/32/48/64/128/256 七档           |
| `app-window.ico` | **窗口/任务栏**图标（Tk 的 `wm iconbitmap`）；只 16/24/32/48 四档且 **BMP 编码** |
| `app-256.png`    | 非 Windows 的窗口图标（`iconphoto`）                                             |
| `app-64.png`     | 顶栏的品牌位（30px 控件配 2x 位图）                                              |
| `app-icon.stamp` | 记下这批位图出自哪一版 SVG（sha256）—— 守卫与启动日志都读它                      |

重画：`uv run python scripts/build_icon.py`（`--force` 无条件重画，`--check` 只检查、不一致退 1）。位图已是最新时它是**空操作**，所以随手跑一次也无妨。

### 为什么光栅化用外部工具（ffmpeg），而不是一个 Python 库

- **软件本身不执行任何外部程序**（安全策略，动态与静态两道守卫见 `tests/security/test_side_effects.py`），所以「SVG → 位图」只发生在**开发/构建期**；打包产物里只有位图，与 ffmpeg 无关。
- `cairosvg` 在 Windows 上要额外的 cairo 原生库（不是纯 wheel）；`svglib` + `reportlab` 对渐变、滤镜的支持有限。为「一辈子只发生几次」的重画动作引入任何一条系统依赖都不划算。
- ffmpeg 是**可选**的：只要位图是最新的（正常情况），没装 ffmpeg 也一切照旧；只有改了 SVG 才轮到它出场。
- 本机确认 ffmpeg 能画 SVG：`ffmpeg -hide_banner -version` 的输出里要有 `--enable-librsvg`（官方 BtbN 构建有）。不在 `PATH` 上时用 `--ffmpeg <路径>`，或设环境变量 `ARCHIVE_MANAGEMENT_FFMPEG`。
- **ICO 必须由 Pillow 装配**：实测 ffmpeg 的 `-f ico` 只写**一档**尺寸（BMP 编码），而 Windows 的 16px 列表视图与 256px 大图标都得好看；Pillow 本来就是运行依赖（`CTkImage` 要用），由它把 256px 的 PNG 缩成七档装进一个 `.ico`，既不多依赖也不多一个外部工具。
- **为什么窗口另用一颗小 ICO**（2026-10-11 实测）：把七档那颗交给 Tk 之后，集成套件跑到第 200 个窗口左右会在 `update_idletasks` 里以 **access violation** 死掉（原生栈留在 `crash-dumps/faulthandler.log`），而同一批文件不装窗口图标补丁跑是绿的；换成四档 BMP（`bitmap_format="bmp"`，Tk 的 Windows 图标读写器本来吃的就是 DIB）后恢复全绿。exe 的 PE 资源由 **Windows** 读，不经过 Tk 的手，所以那颗不需要跟着变小。

### 什么时候会自动重画

| 时机   | 做什么                                                                                                                        |
| ------ | ----------------------------------------------------------------------------------------------------------------------------- |
| 提交时 | pre-commit 的 `build-icon` 钩子**只在 `app.svg` 有改动时**触发，重画后自己 `git add`（与 `compact-json` 同一条路）            |
| 发布时 | `release-build.yml` 打包前跑 `--check`，不一致判红。**故意不自动重画** —— 那会把一个陈旧的提交默默修好，没人发现              |
| 启动时 | 软件只**核对**并把结果写进日志（`应用图标: …`）；不一致时日志里直接给出该跑的那条命令。不重画的原因同上（软件不执行外部程序） |

改了 SVG 而本机又没有 ffmpeg 时，唯一会失败的是「提交」那一步（钩子报错里给出 `--ffmpeg` 与环境变量两条路）；此时你也可以只提交 SVG、让有工具的人补产物 —— 但发布前 `--check` 一定会拦住。

### 窗口图标：CTk 会替每个窗口贴上它自己那颗

CustomTkinter 在**每个**窗口建好 **200ms** 后自己去贴 `CustomTkinter_icon_Windows.ico`（这个动作只在 Windows 上排），条件是「这个窗口上没人调用过 `iconbitmap`」：

- 主窗口逃得过 —— `ArchiveApp.__init__` 里 `_apply_window_icon()` 已经显式设过我们的；
- **每个子窗口都中招**：设置、定时任务、游戏管理、所有对话框，标题栏与任务栏里都是 CTk 那颗（2026-10-11 报的就是它）。

库没留接口，所以 `ui/window_icon.py` 把 `CTk` 与 `CTkToplevel` 的私有方法 `_windows_set_titlebar_icon` 换掉（与 `ui/rendering.py` 换 `DrawEngine` 同一类手法）。换掉之后那次延时动作落到 `apply_window_icon`：

- 有 `app-window.ico`（四档 BMP）就用它（`iconbitmap`：标题栁 16 / 缩放 24 与 32 / 高 DPI 任务栏 48）；被 Tk 拒了就退回 `iconphoto` + `app-256.png`（X11/macOS 只认图片）；都没有则什么都不做 —— 图标不是启动的前提，任何失败都只返回 `False`；
- **判据跟库一样**：窗口上已经有人设过图标（主窗口在 `__init__` 里就设了）就不再贴——我们比库多出来的那一次 `wm iconbitmap` 正是上面那个 access violation 的差异；
- 接线在 `ui/widgets.py` 模块级（与 `apply_border_rendering_fix()` 那几条一起），import 一次就够；
- 库升级把那个私有名字改掉时**不会出错**，只是 CTk 照旧贴它那颗 —— 那时守卫变红，正好提醒来改这里。

守卫两处：`tests/integration/test_gui_window_icon.py`（真窗口 + 泵事件到 200ms 之后，看那次 `iconbitmap` 收到哪个文件；已咬过一口：把接线断开时它量到的正是 `CustomTkinter_icon_Windows.ico`）与 `tests/unit/test_icon_assets.py`（两个窗口类都换成了我们的实现、再调一次是空操作）。

### CI 怎么验这条流水线

`release-build.yml` 的构建作业里只有**一条**图标步骤: `Check the application icon is up to date`（硬门禁）—— `--check` 只比指纹, 不一致就判红。发布**不依赖** runner 上有渲染工具, 也**不在 CI 上重画**。

**2026-10-15 起 CI 不再渲染图标**（用户要求）: 原来还有一条信息步骤（`Probe the icon pipeline on this runner`，打印 runner 上那个 ffmpeg 版本行、往 `%TEMP%` 里重画一遍并与入库产物逐字节比对，失败只记 `::warning::`）与一个只做 `workflow_dispatch` 的 `Icon probe` 工作流（`.github/workflows/icon-probe.yml`，渲染到 `icon-probe-out/` 并上传 artifact），两个都删了 —— 判定 "改了 SVG 忘了重画" 靠 `--check` 就够了, 而 "这台 runner 能不能画" 不再是流水线要关心的事（图标只在本地更新: `uv run python scripts/build_icon.py --force`，它就地重画并更新指纹）。

想在本机看不改仓库的渲染结果, 用 `uv run python scripts/build_icon.py --force --out-dir icon-probe-out`（落点已在 `.gitignore` 里）。

> **为什么位图入库而不是靠 CI 现画**：位图五个文件合计约 **53 KB**；不入库的话，干净检出时界面会缺一块（顶栏退回纯色方块、窗口无图标），而且 `fail_under=100` 会逼着**每个 pytest 作业（三平台×三片）**都装 ffmpeg 并先生成位图（“有位图就用图”那条分支得被走到），最后发布还得赌 runner 上那份 ffmpeg 带 librsvg。换来的只是 53 KB。

> **脚本不依赖“项目已安装”**：CI 的安装步骤帯 `--no-install-project`，那种环境里 `import archive_management` 会直接 `ModuleNotFoundError`（实测），所以 `scripts/build_icon.py` 顶部自己把 `src` 放进 `sys.path`（守卫 `test_the_icon_script_runs_without_an_installed_project`）。

## 打包

```shell
uv run python scripts/build_icon.py --check
uv run pyinstaller --noconfirm --clean packaging/archive-management.spec
uv run python scripts/smoke_packaged.py
```

- **三条命令就是本地编包的完整链路**：核对位图（`--check` 只比指纹，不一致就不过）→ PyInstaller（spec 里同时声明 onedir 与 onefile，所以**一条命令产出两份**）→ 产物冒烟（`scripts/smoke_packaged.py`，5 条判据 × 两份产物，只在 Windows 上跑；产物不在默认位置时用 `--onedir` / `--onefile` 指过去）。CI 的 `release-build.yml` 跑的就是同样这三句。
- 构建前应先通过全部门禁：`uv sync --locked`、Ruff（`check` + `format`）、mypy、pytest；位图也要是最新的（`uv run python scripts/build_icon.py --check`，CI 在构建前就跑这一句）。
- **在仓库根执行**：命令里的 `packaging/archive-management.spec` 是相对路径，`dist/` / `build/` 也落在当前目录下。
- 版本直接读取 `packaging.py` 中的源码版本。
- **本地产物**（同一条命令同时产出两份）：`dist/ArchiveManagement/`（onedir，一个目录）与 `dist/ArchiveManagement.exe`（onefile，与目录**同名**的单个 exe —— 目录与文件可以共存，取名字时别把它们看成一回事）。**zip 压缩包、`SHA256SUMS.txt` 与 `build-metadata.json` 不是这一条命令产的** —— 那是 `.github/workflows/release-build.yml` 的 `Prepare build files` 步骤在构建之后自己打包出来的，别把本地构建与发布产线混为一谈。
- **不启用 UPX（2026-10-24 定案）**：`spec` 里三处 `upx=False` 是**显式决定**，不是忘了开。`upx=True` 只在构建机装了 UPX 时才生效，缺了会被**静默跳过**——那三处以前就是假开关（看上去压了，实际一次没压）。实测（Windows x64 / Python 3.12 / UPX 5.2.1，各 3 次取中位）：onedir **86.5 → 37.6 MB**（打成 zip 41.1 → 31.5）、onefile **40.2 → 31.5 MB**，但 onedir 的「GUI 启动到关窗」从 **1.70 s 涨到 2.07 s**（onefile 3.61 → 3.63 s，解包开销把它盖住了）；再算上构建链要多一个外部二进制、UPX 壳是杀软启发式与下载拦截的高频形态（发布包没有签名），所以不压。守卫：`tests/unit/test_package.py` 的两条（`upx=` 必须显式 False + 三处站点齐）。要翻案得先在 `release-build.yml` 里装 UPX **并断言构建日志里出现 UPX 行**。
- **冷启动有上限（2026-10-24，J-7①）**：`scripts/smoke_packaged.py` 的 ①无参数启动那条顺手计时（Popen → 沙箱日志里“界面启动”落地），**超限就判红**：onedir **10 s**、onefile **20 s**。本机实测（同一条判据连跑 3 轮）**onedir 0.5 s / onefile 2.0 s**（探测 0.5 s 一轮，所以 onedir 那格是“不超过 0.5 s”）；“启动→窗口→关窗”的总时长是另一件事（1.70 / 3.6 s，见下面的体积与启动时间两条），`--version` 的 0.22 / 1.98 s 也不是它。上限按 CI runner 的最坏情况留了 10~20 倍余量 —— 它拦的是“又往包里塞大件”这类退化，不是性能基准；守卫在 `tests/unit/test_package.py`（两份产物都有上限、恰好等于上限不误判、上限真接进判据并判红）。
- **onefile 的解包残渣（J-7②，2026-10-24）**：onefile 运行时把整份包解到 `%TEMP%\_MEIxxxx`，**正常退出由引导程序自清、被强杀或崩溃则留下约 75 MB**（本机实测 `%TEMP%` 里已攒 16 个、共 1204.8 MB）。应用**不去动它**：`src` 的权限边界是“自身目录之外只读”，替用户删 `%TEMP%` 里的目录既越界、又分不清哪个还被别的实例用着；要清就手动删 `%TEMP%\_MEI*`（且确保没有本应用在跑）。**我们自己的工具会制造这种残渣** ——`scripts/smoke_packaged.py` 的 ①无参数启动与 ⑤单实例闸门都会杀进程树，现在这两处把子进程的 `TEMP`/`TMP` 指进本次工作目录（收尾连同工作目录一起删），并在结果里多打一行“临时目录残渣: 本次新增 N 个 \_MEI\*”；修前一次冒烟实测 16 → 18 个（+150.7 MB），修后 **18 → 18**。守卫在 `tests/unit/test_package.py`（`_temp_env` 真的盖住 TEMP/TMP、①与⑤都走它、`_mei_dirs` 只认 `_MEI` 前缀目录）。
- **双击就能用**：窗口化产物（`console=False`）不带子命令启动时会直接开界面（见「命令行」那条）；验证方式是双击（或直接启动 exe）后日志里应多一行 `界面启动`，而不是只有 `初始化完成`。CI 里这条路由 `release-build.yml` 的产物冒烟步骤自动守着（`uv run python scripts/smoke_packaged.py`：五条判据 × onedir/onefile），本地手动跑同一句即可复现。
- 产物为 Windows x64 的 **onedir** 压缩包、**onefile** 可执行文件、SHA-256 校验文件与构建元数据；发布包不得包含凭据、开发机绝对路径或测试数据。`spec` 文件显式声明入口模块、图标与 CustomTkinter 主题资源，并按需补充隐藏导入。
- GitHub Actions 在 Windows runner 上构建，冒烟通过后把 `dist/` 打成 `release/` 里的 zip、onefile、`SHA256SUMS.txt` 与 `build-metadata.json` 并上传（手动触发也支持）。当前以 Windows 为首要发布平台，macOS/Linux 打包未纳入。
- 包体积与启动时间（2026-09-19，Windows x64 / Python 3.12，含量化封面依赖 Pillow 12.3.0）：**onedir 合计约 57.3 MB**（其中 `_internal/PIL` 约 12.8 MB，是本轮首次引入的开销）、**onefile 约 29.1 MB**；在已初始化的数据目录上启动到窗口出现**约 2.1 s**（同条件源码运行同样约 2.1 s，冻结后没有额外开销）。测量方式：`ArchiveManagement.exe gui --root <已初始化目录> --smoke 0.1` 连跑三次取稳定值。
- **体积与冷启动复量（2026-10-19，`--clean` 重编）**：**onedir 86.4 MB / onefile 40.1 MB**（依赖涨了，上面那组 09-19 的数字已偏小）；`--version` 从起进程到退出是 **onedir 0.22 s / onefile 1.98 s** —— 差的 1.8 s 就是 onefile 解包到 `%TEMP%\_MEIxxxx` 的开销，但那是「进程能跑起来」的下限，**不是**「到窗口出现」的耗时（那个仍只有 onedir 的 2.1 s 基线，onefile 还没单独量）。
- **产物不依赖源码树与系统 Python**（2026-10-19 本机实测）：把 `dist/ArchiveManagement` 整份拷到**仓库外**、并且把 `PATH` 只留 `C:\Windows\System32`（此时 `python` / 项目 `.venv` 都不在 PATH 上）之后，`--version` / `doctor --root <临时>` / `gui --root <临时> --smoke 3` 全部退出 0，`--root` 下 `config`/`data`/`logs` 三处齐、日志里一条「界面启动」；onedir 与 onefile 两份产物都是这个结果，同一份产物跑 `scripts/smoke_packaged.py` 是 10/10。**冻结版没有崩溃留证**：`coredumpy` 只声明在 `test` 开发组里、`src` 也不 import 它，打包版出事的现场只有退出码 + 日志 + Windows 事件查看器；而「一行 Python 都没装的干净机器」这类验收只能人工在 Windows Sandbox / 全新 VM 做（`--root` 记得指向临时目录，否则会写进真实用户目录）。
- **打包版 vs 开发版的逐屏比对（本机工具，不进 CI）**：`uv run python scripts/capture_packaged.py still|restart|compare`。`still`/`restart` 是两级噪声标定（应得距离 0 / SSIM 1.0000），`compare` 用同一份种子的两个克隆成对抓拍 dev 与 exe，借 `create_allure_visual.py` 的比对算尺逐对判红绿（清单是 3 张静默可达的画面：主页列表 / 海报视图 / 空库；详情、发现分区等需点击到达的画面不在其内）。输出落在仓库外 `%TEMP%\am-j4-visual\` 下，验收材料不进仓库。要求系统缩放 100%、本地已有 onedir 产物。

## 发布

构建与发布分成**两份**工作流（判据与取舍落在 `docs/testing-ci.md`）：

- [`.github/workflows/release-build.yml`](.github/workflows/release-build.yml)（Build Windows Package，**只读**）：真正的构建作业 —— 装依赖、跑构建前检查、`--check` 图标指纹、PyInstaller、产物冒烟、上传 artifact。有两个调用方（见下表）。
- [`.github/workflows/release.yml`](.github/workflows/release.yml)（Release Windows Package）：只负责“什么时候发”—— `push` 到 `main` 或手动触发；它先 `uses:` 上面那一份拿到产物，再由 `publish` 作业建 Release（写权限只在这一份里）。

| 入口                                 | 什么时候                                                              | 编什么                                       | 发布?                          |
| ------------------------------------ | --------------------------------------------------------------------- | -------------------------------------------- | ------------------------------ |
| `ci.yml` 的 `windows-package` 作业   | **合入 `main` 的 PR**，且 CI 的结论（含覆盖率门槛与原生质量门）都过了 | 该 PR 的**合并预览提交**                     | ❌ 只上传 artifact（打包门禁） |
| `release.yml` 的 `push` 到 `main`    | 合入之后                                                              | 那个合并提交                                 | 版本号变了才发                 |
| `release.yml` 的 `workflow_dispatch` | 手动                                                                  | `ref` 指的分支或提交（`dev` 只有这一个入口） | 看 `publish` 开关              |

- **为什么构建与发布不能合成一份**（2026-10-17 实测，不是设计洁癖）：GitHub 对**嵌套工作流**的权限是**逐作业静态校验**的 —— 一份工作流里只要有作业声明 `contents: write`，而调用它那次运行（PR）只给得起 `contents: read`，整份工作流就判 `not valid`（原文：`The nested job 'publish' is requesting 'contents: write', but is only allowed 'contents: read'`），报错在**解析阶段**，一个 job 都起不来，运行期的 `publish: false` 拦不住。所以门禁调的是只读的 `release-build.yml` —— 否则想让门禁跑起来，就得给 PR 那次运行发一个带写权限的 token，而 PR 里的代码是会被拉下来执行的。
- **打包门禁**：`ci.yml` 的 `windows-package` 作业在 `release-build.yml` 上跑 `checks: build-only`（同一轮 CI 已经跑过三平台全量，不在 Windows 上重跑一遍），构建完跑产物冒烟（无参数启动 / `--version` / `doctor` / `gui --smoke` 四条判据 × onedir 与 onefile，外加单实例闸门，全部指向临时 `--root`）。分支保护里要勾的必需检查是 **`CI required packaging status`**（`packaging-gate` 那个恒跑的“结论”作业）—— 干活的那个作业在纯文档改动的 PR 上会被跳过，被跳过的必需检查会永久停在 pending，所以结论交给另一个作业。
- **为什么门禁不能写成 `workflow_run`**：跨工作流触发产生的检查挂不到那个 PR 上，永远进不了必需检查 —— 想让“CI 通过才允许合入”成立，编译就得发生在 PR 那次运行里。
- **发布口径**：版本号由提交者手改 `packaging.py`，CI 只读不改（不注入 tag 版本）。**同一个版本合入两次是正常的事**（小修、文档、只改守卫），所以 `v<版本号>` 已经存在时这一轮只构建、不重复发 Release（记一条 `::notice::`，不算失败）。真的发了就建 `v<版本号>` 的 Release，附上 zip、单文件 exe、`SHA256SUMS.txt` 与 `build-metadata.json`。
- **手动重编/补发**：Actions → _Release Windows Package_ → Run workflow（给 `ref`，需要发布时把 `publish` 打开）。
- 构建前仍要过全部门禁（`ruff` / `mypy` / `pytest --cov`，见 `release-build.yml` 的 `Run build checks`，发布与手动档默认跑、门禁档跳过）与图标指纹检查；产物只上传 `release/` 目录（zip、onefile、校验和、元数据）。
