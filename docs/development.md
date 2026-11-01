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

同步机制（2026-10-31 起**只在发版时同步**）：`.github/workflows/wiki.yml` 现在是 `workflow_call` + 手动两份入口，由 `release.yml` 的 `wiki` 作业在 **Release 建好之后**调用，把打好版本标注的 `wiki/*.md` **镜像**推送到 GitHub Wiki 的 git 仓库。

- **为什么不跟 `push` 走**：手册要给用户看“这页是哪个版本带上的”，而版本号是合并之后由 `classify` 定出来的（见本文「发布」节）；`dev` 上改的手册在那时还没有版本号。也**不能**监听 `release` 事件 —— `gh release create` 用的是 `GITHUB_TOKEN`，它触发的 release 事件**不会**再生成工作流运行（GitHub 的防环规则，只有 `workflow_dispatch` / `repository_dispatch` 例外），那条路根本没人叫得动。
- **页面上的版本标注**由 `scripts/stamp_wiki_versions.py` 现算，**不写回**仓库里的 `wiki/`：判据是“这一页最后一次改动，首个带上它的 tag”（`git log -1` + `git tag --contains`），语义最小的 tag 当基线，基线那一版就有的内容一律不标（现有文档不追溯）。标注是派生物 —— 评审手册时不该看见它，也别手工往页面里加。
- **纯文档的合入不发版**（`release.yml` 的 `paths-ignore` 覆盖 `**/*.md`），所以那份手册改动会在**下一个代码版本**里一起上线、并被标上那个版本号 —— 这正是“哪个版本带上了它”的准确含义。
- **不在 `main` 历史里的 tag 会被拒绝同步**（手动从 `dev` 发布的那一档）：给手册标一个正式版根本没有的版本号，比不标更糟。手动档必须填 `tag`。

三点注意：

1. **首次需要人工在网页上开一次**：仓库 Settings → Features 勾选 Wikis，并在 Wiki 里保存过至少一页 —— GitHub 是在"第一次保存页面"时才创建 `<repo>.wiki.git`，之前工作流会 clone 失败（报错信息里直接写了这一步）。
2. 页面里的仓库链接用 `{{REPO_URL}}` 占位符（由工作流替换成真实地址），**不要写仓库相对链接**（Wiki 与仓库不在同一棵目录树里，相对链接会变成“不存在的页面”且不报错）。这一条与“章节编号/侧边栏链接齐全/镜像语义/版本标注口径”一起由 `tests/unit/test_wiki_sync.py` 与 `tests/integration/test_wiki_versions.py` 守住。
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

- **三条命令就是本地编包的完整链路**：核对位图（`--check` 只比指纹，不一致就不过）→ PyInstaller（spec 声明的是单文件 exe，2026-10-31 起回到 onefile）→ 产物冒烟（`scripts/smoke_packaged.py`，5 条判据 × 这份产物，只在 Windows 上跑；产物不在默认位置时用 `--exe` 指过去）。CI 的 `release-build.yml` 跑的就是同样这三句，只是冒烟多带 `--report smoke-report.json`：逐条判据落成 JSON（失败也写），由紧跟的 `scripts/create_allure_smoke.py` 转成 Allure 结果项上传 `allure-results-smoke-windows`，CI 汇总报告的「构建后冒烟」一节看的就是它（本地不传 `--report` 则什么都不落盘）。
- 构建前应先通过全部门禁：`uv sync --locked`、Ruff（`check` + `format`）、mypy、pytest；位图也要是最新的（`uv run python scripts/build_icon.py --check`，CI 在构建前就跑这一句）。
- **在仓库根执行**：命令里的 `packaging/archive-management.spec` 是相对路径，`dist/` / `build/` 也落在当前目录下。
- 版本直接读取 `packaging.py` 中的源码版本。
- **本地产物**：`dist/ArchiveManagement.exe`（单文件，双击即用）。**发布页上只有它一个文件**（2026-10-31 定）：早先发布的是 onedir 目录的 zip + `SHA256SUMS.txt` + `build-metadata.json`，那三样只服务“要校验和、要构建元数据”的人，在下载页上却让用户多解压一次才能用；现在 `release-build.yml` 的 `Prepare the release file` 就是把那个 exe 拷进 `release/`。
- **不启用 UPX（2026-10-24 定案）**：`spec` 里那处 `upx=False`（单文件产物的 EXE）是**显式决定**，不是忘了开。`upx=True` 只在构建机装了 UPX 时才生效，缺了会被**静默跳过**——那处以前就是假开关（看上去压了，实际一次没压）。实测（Windows x64 / Python 3.12 / UPX 5.2.1，各 3 次取中位）：onedir **86.5 → 37.6 MB**（打成 zip 41.1 → 31.5；onefile 停产前对照 40.2 → 31.5，数字留档），但 onedir 的「GUI 启动到关窗」从 **1.70 s 涨到 2.07 s**；再算上构建链要多一个外部二进制、UPX 壳是杀软启发式与下载拦截的高频形态（发布包没有签名），所以不压。守卫：`tests/unit/test_package.py` 的两条（`upx=` 必须显式 False + 只有一块 EXE）。要翻案得先在 `release-build.yml` 里装 UPX **并断言构建日志里出现 UPX 行**。
- **冷启动有上限（2026-10-24，J-7①）**：`scripts/smoke_packaged.py` 的 ①无参数启动那条顺手计时（Popen → 沙箱日志里“界面启动”落地），**超限就判红**：onefile **20 s**（对照 onedir 10 s）。本机实测（同一条判据连跑 3 轮）**onefile 2.0 s**（onedir 0.5 s；探测 0.5 s 一轮）；“启动→窗口→关窗”的总时长是另一件事（onefile 3.6 s，见下面的体积与启动时间两条），`--version` 的 1.98 s 也不是它。上限按 CI runner 的最坏情况留了 10~20 倍余量 —— 它拦的是“又往包里塞大件”这类退化，不是性能基准；守卫在 `tests/unit/test_package.py`（产物有上限、恰好等于上限不误判、上限真接进判据并判红）。
- **onefile 的解包残渣（J-7②，2026-10-24；2026-10-31 起产物回到 onefile，这条重新适用）**：onefile 运行时把整份包解到 `%TEMP%\_MEIxxxx`，**正常退出由引导程序自清、被强杀或崩溃则留下约 75 MB**（本机实测 `%TEMP%` 里曾攒 16 个、共 1204.8 MB）。应用**不去动它**：`src` 的权限边界是“自身目录之外只读”，替用户删 `%TEMP%` 里的目录既越界、又分不清哪个还被别的实例用着；要清就手动删 `%TEMP%\_MEI*`（且确保没有本应用在跑）。停产前的对策仍值得留档：`scripts/smoke_packaged.py` 杀进程树的每条判据都把子进程的 `TEMP`/`TMP` 指进本次工作目录（收尾连同工作目录一起删），这条纪律在 onedir 上继续成立（子进程的临时文件归冒烟自己收）；`_MEI` 残渣清点（`_mei_dirs` 与收尾那行 warning）在停产期间被删掉了；现在靠 `_temp_env` 把子进程的临时目录圈进冒烟自己的工作目录来堵住。守卫在 `tests/unit/test_package.py`（`_temp_env` 真的盖住 TEMP/TMP、**五条判据**都走它）。
- **冒烟失败要自带现场（2026-10-26）**：窗口化产物没有控制台，杀掉之后什么都不留 —— 所以限时判据把子进程的 stdout/stderr 落成 `out.txt`/`err.txt`，超时或非零退出时把这两份与 `--root` 下应用日志的末尾一并塞进失败消息（CI 日志里直接能读）。配套地，`doctor`/`init` 的报告改成**一行一发**（`app._report`）：既进控制台也进日志 —— 打包版（`console=False`）本来看不到任何报告，卡在中间时日志末尾就直接指到停在哪一行。守卫：`tests/unit/test_package.py` 的 `test_a_timed_out_command_reports_where_it_stopped` 与 `tests/unit/test_cli.py` 的 `test_doctor_writes_its_report_into_the_log_too`。
- **控制台编不出中文不算崩（2026-10-27 定案，就是上一条抓到的那次）**：runner 是**英文 Windows（cp1252）**，打包版 `doctor` 往控制台打「应用 : 存档管理工具」时 `print` 抛 `UnicodeEncodeError`，窗口化产物还会弹 PyInstaller 的 "Failed to execute script" 框 —— 于是进程卡着等一个永远不会来的点击，冒烟判它 120 s 超时（两份产物同红，本机 cp936 怎么试都复现不了）。修法是 `app.run()` 开头调 `_lenient_console()`：标准流按 `errors="replace"` 重配（编不出就替字，不抛），报告本来以 UTF-8 日志为准；`sys.stdout is None` 时 `print` 本来就什么都不做，不用另判空。守卫：`tests/unit/test_cli.py` 的 `test_doctor_survives_a_cp1252_console`（真拿 cp1252 流跑一遍 doctor）、`test_the_report_never_dies_on_a_console_that_cannot_print_chinese`、`test_doctor_reports_into_the_log_even_without_a_console`。
- **双击就能用**：窗口化产物（`console=False`）不带子命令启动时会直接开界面（见「命令行」那条）；验证方式是双击（或直接启动 exe）后日志里应多一行 `界面启动`，而不是只有 `初始化完成`。CI 里这条路由 `release-build.yml` 的产物冒烟步骤自动守着（`uv run python scripts/smoke_packaged.py`：五条判据 × 那份产物），本地手动跑同一句即可复现。
- 产物为 Windows x64 的**单文件 exe**（2026-10-31 起发布页只挂它；此前一段时间的发布物是 onedir 压缩包 + SHA-256 校验文件 + 构建元数据）；发布包不得包含凭据、开发机绕对路径或测试数据。`spec` 文件显式声明入口模块、图标与 CustomTkinter 主题资源，并按需补充隐藏导入。
- GitHub Actions 在 Windows runner 上构建，冒烟通过后把 `dist/ArchiveManagement.exe` 拷进 `release/` 并上传（手动触发也支持）。当前以 Windows 为首要发布平台，macOS/Linux 打包未纳入。
- 包体积与启动时间（2026-09-19，Windows x64 / Python 3.12，含量化封面依赖 Pillow 12.3.0）：**onefile 约 29.1 MB**（onedir 对照合计约 57.3 MB，其中 `_internal/PIL` 约 12.8 MB，是本轮首次引入的开销；2026-10-31 起产物是 onefile）；在已初始化的数据目录上启动到窗口出现**约 2.1 s**（同条件源码运行同样约 2.1 s，冻结后没有额外开销）。测量方式：`ArchiveManagement.exe gui --root <已初始化目录> --smoke 0.1` 连跑三次取稳定值。
- **体积与冷启动复量（2026-10-19，`--clean` 重编）**：**onefile 40.1 MB**（onedir 对照 86.4 MB；依赖涨了，上面那组 09-19 的数字已偏小）；`--version` 从起进程到退出是 **onefile 1.98 s**（onedir 0.22 s，差的 1.8 s 就是它解包到 `%TEMP%\_MEIxxxx` 的开销）—— 但那是「进程能跑起来」的下限，**不是**「到窗口出现」的耗时（那个仍只有 2.1 s 的基线）。
- **产物不依赖源码树与系统 Python**（2026-10-19 本机实测）：把 `dist/ArchiveManagement.exe` 拷到**仓库外**、并且把 `PATH` 只留 `C:\Windows\System32`（此时 `python` / 项目 `.venv` 都不在 PATH 上）之后，`--version` / `doctor --root <临时>` / `gui --root <临时> --smoke 3` 全部退出 0，`--root` 下 `config`/`data`/`logs` 三处齐、日志里一条「界面启动」（当时 onedir 与 onefile 两份产物都是这个结果；onefile 已于 2026-10-28 停产，现在同一份产物跑 `scripts/smoke_packaged.py` 是 5/5）。**冻结版没有崩溃留证**：`coredumpy` 只声明在 `test` 开发组里、`src` 也不 import 它，打包版出事的现场只有退出码 + 日志 + Windows 事件查看器；而「一行 Python 都没装的干净机器」这类验收只能人工在 Windows Sandbox / 全新 VM 做（`--root` 记得指向临时目录，否则会写进真实用户目录）。
- **打包版 vs 开发版的逐屏比对（本机工具，不进 CI）**：`uv run python scripts/capture_packaged.py still|restart|compare`。`still`/`restart` 是两级噪声标定（应得距离 0 / SSIM 1.0000），`compare` 用同一份种子的两个克隆成对抓拍 dev 与 exe，借 `create_allure_visual.py` 的比对算尺逐对判红绿（清单是 3 张静默可达的画面：主页列表 / 海报视图 / 空库；详情、发现分区等需点击到达的画面不在其内）。输出落在仓库外 `%TEMP%\am-j4-visual\` 下，验收材料不进仓库。要求系统缩放 100%、本地已有单文件产物。

## 发布

构建与发布分成**两份**工作流（判据与取舍落在 `docs/testing-ci.md`）：

- [`.github/workflows/release-build.yml`](.github/workflows/release-build.yml)（Build Windows Package，**只读**）：真正的构建作业 —— 装依赖、跑构建前检查、`--check` 图标指纹、PyInstaller、产物冒烟（结论转 Allure 结果，上传 `allure-results-smoke-windows`）、上传 artifact。有三个调用方（见下表）。
- [`.github/workflows/release.yml`](.github/workflows/release.yml)（Release Windows Package）：只负责“什么时候发”—— `push` 到 `main` 或手动触发；`classify` 作业只读地**复查**版本号（2026-11-01 起：定版在 PR 里做，它不再写 `main`），然后 `uses:` 上面那份构建，`publish` 作业建 Release（写权限只在这一份里），最后由 `wiki` 作业在**发布之后**调 [`.github/workflows/wiki.yml`](.github/workflows/wiki.yml) 同步用户手册（2026-10-31 起；手册的版本标注靠的就是这个 tag，机制见上文「文档与 Wiki」）。

| 入口                                 | 什么时候                                                              | 编什么                                       | 发布?                           |
| ------------------------------------ | --------------------------------------------------------------------- | -------------------------------------------- | ------------------------------- |
| `ci.yml` 的 `windows-package` 作业   | **合入 `main` 的 PR**，且 CI 的结论（含覆盖率门槛与原生质量门）都过了 | 该 PR 的**合并预览提交**                     | ❌ 只上传 artifact（打包门禁）  |
| `release.yml` 的 `push` 到 `main`    | 合入之后                                                              | 定版后的提交（未手改版本时 = bump 提交）     | 每次都发（未手改则 patch+1）    |
| `release.yml` 的 `workflow_dispatch` | 手动                                                                  | `ref` 指的分支或提交（`dev` 只有这一个入口） | 看 `publish` 开关               |
| `dev-release.yml` 的手动档           | 手动，且该提交的 `ci.yml` 必须**已跑绿**                              | 该提交（默认 `dev` 的头）                    | 带 `Pre-release` 标签，先删旧的 |

- **dev 快照（2026-10-31 起，`.github/workflows/dev-release.yml`）**：给 `dev` 编一份能装能看的包，但不占正式版的位置。三件事各自钉住：
  - **前置**：这个提交必须已经跑过 `ci.yml` 且全绿（`gh run list --commit` 判结论，还在跑或有失败都拦）—— 拿红的提交出包等于绕过本仓库唯一的质量关。纯文档提交**没有**运行记录（`ci.yml` 的 `paths-ignore` 覆盖 `**/*.md`），所以它对这一档报"没有运行记录"是正常的：换成含代码改动的提交再触发；
  - **版本号是四段** `<基础版本>.<第 4 位>`，由 `scripts/dev_snapshot_version.py` 按 git 历史算：基础版本取 `packaging.py` 与最近三段 tag 里**语义较大**的那个（`dev` 上的 `packaging.py` 可能还停在上一版 —— 它是在某次 `main` 推进时重建出来的，之后 `main` 上的版本号由合入的 PR 推进，照 dev 自己那份算会得到“比已发布版本还旧”的号），第 4 位 = 距最近一个三段 tag 的提交数（0 记 1；必须 < 65536，Windows 版本资源每段是 16 位字段）。它是派生量：同一个提交永远算出同一个号，且天然单调；
  - **产物里的版本号也是快照号**：`release-build.yml` 的 `version_override` 在构建前把四段号写进**工作区**的 `packaging.py`（不提交），于是 `--version` / `doctor` / `build-metadata.json` 报的是同一个快照号；它只允许配 `checks: build-only`（full 档会跑用例，而用例里有一批“`packaging.py` 是三段”的断言）；
  - 四段 tag **不会**被 `release_bump.py` 看见（它只认三段），所以预发布不会打乱正式版的定版。
- **预发布只留最新一份**：两条发布链在**发包前**都会把此前所有 `Pre-release` 删掉（连 tag —— 四段 tag 不是历史）。dev 出快照前删旧的，避免 Release 页被一堆快照淹没；正式版一发布 dev 快照就作废，同时挂着“最新正式版 + 更旧的 dev 快照”只会让用户下错包。

- **为什么构建与发布不能合成一份**（2026-10-17 实测，不是设计洁癖）：GitHub 对**嵌套工作流**的权限是**逐作业静态校验**的 —— 一份工作流里只要有作业声明 `contents: write`，而调用它那次运行（PR）只给得起 `contents: read`，整份工作流就判 `not valid`（原文：`The nested job 'publish' is requesting 'contents: write', but is only allowed 'contents: read'`），报错在**解析阶段**，一个 job 都起不来，运行期的 `publish: false` 拦不住。所以门禁调的是只读的 `release-build.yml` —— 否则想让门禁跑起来，就得给 PR 那次运行发一个带写权限的 token，而 PR 里的代码是会被拉下来执行的。
- **打包门禁**：`ci.yml` 的 `windows-package` 作业在 `release-build.yml` 上跑 `checks: build-only`（同一轮 CI 已经跑过三平台全量，不在 Windows 上重跑一遍），构建完跑产物冒烟（无参数启动 / `--version` / `doctor` / `gui --smoke` 四条判据 × 那一份产物，外加单实例闸门，全部指向临时 `--root`；2026-10-31 起产物是单文件 exe）。分支保护里要勾的必需检查是 **`CI required packaging status`**（`packaging-gate` 那个恒跑的“结论”作业）—— 干活的那个作业在纯文档改动的 PR 上会被跳过，被跳过的必需检查会永久停在 pending，所以结论交给另一个作业。2026-10-27 重排后 `windows-package` 只等 `changes`/`quality`/`pytest`/`security`（**不再等报告链**），报告作业反过来等它、把冒烟结论收进汇总报告 —— 因此分支保护里**报告链的检查（`pytest-report` / `allure-summary`）也要勾为必需**，否则存在“打包绿了但报告红”的合入盲区（见 [testing-ci.md](testing-ci.md) 第 2 节）。
- **为什么门禁不能写成 `workflow_run`**：跨工作流触发产生的检查挂不到那个 PR 上，永远进不了必需检查 —— 想让“CI 通过才允许合入”成立，编译就得发生在 PR 那次运行里。
- **发布口径（2026-11-01 起：定版在 PR 里做）**：版本号的**单一真相**仍是 `packaging.py` 的 `__version__`（三段语义版本），但**没有任何自动过程会去改它** —— `main` 开了分支保护，合入之后谁都推不进去（`github-actions[bot]` 也一样），所以“合入后自动加一位”必然以 `protected branch` 收场（2026-11-01 实测）。于是定版前移到 **PR 阶段**：要发版就在 PR 里改 `packaging.py` 再推上来，那份 diff 经过评审（它也是“动前两位”大版本的唯一声明通道 —— 故意不让脚本从改动量里猜：semver 段位是对用户的沟通契约，5 行可以是破坏性的 schema 变更，5000 行重构可以用户无感，任何按行数/文件数的启发式都会两头误判）；不想发版就什么都不改，合入后只构建、不新建 Release。忘了改的表现是**安静地不发新版**，所以 `ci.yml` 的 `release-hint` 作业会在合入 `main` 的 PR 上留一条评论（当前号 / 建议的下一版号 / 不改会怎样，按 marker 原地更新，**不判红**）。判定逻辑全在 `scripts/release_bump.py`（只读，不写文件），守卫在 `tests/unit/test_release_bump.py`、`tests/unit/test_release_hint.py` 与 `tests/unit/test_ci_workflow.py` 的两条。
- **发布链的其余细节**：`release.yml` 的 `classify` 现在**只读** —— 它对照“最近发过的 `v*` tag”给这次合入归类（`first-release` / `manual` / `needs-bump`），没有任何写权限；`needs-bump` 时发布作业留一条 `::warning::` 说明“本轮只构建、不发 Release”，免得有人以为发布链坏了。两轮合入紧挨着时工作流按 `concurrency` 串行（后一轮必须等前一轮把新 tag 写完，否则会读到旧 tag）。`v<版本号>` 已存在时仍是**正常出口**：只构建、不重复发 Release（Release 已存在记 `::notice::`；**tag 已存在但 Release 不在**记 `::warning::` —— 那个版本号已作废，重建必然 422，只能把版本号往上加一位）。真的发了就建 `v<版本号>` 的 Release，**只挂一个 `ArchiveManagement.exe`**，正文取自该 PR 里**手写**的「本次变更」一节（2026-10-31 定）。
- **dev 分支的自动重建（2026-10-31 起，`ensure-dev.yml`）**：仓库设置开着“合并后删除来源分支”，所以 `dev -> main` 一合入，`dev` 就没了；这份工作流在 main 每次前进时补一个 —— **只在缺失时创建，绝不移动已存在的 dev**（别的机器上可能正有人在上面干活）。两点注意：① 它**故意不带 `paths-ignore`**，因为纯文档合入同样会删掉 dev，而那正是最需要兜底的场合；② 它在合并那一刻建出来的 dev 指向**合并提交**，随后 `classify` 推 bump 提交的那一步会顺手把 dev 快进到同一个提交 —— 少了这一快进，dev 会永远比 main 少一行版本号，下一次 `dev -> main` 的 PR 就会把它“改回去”。**本地注意**：重建出来的 dev 不再是原来那串提交，本机要 `git fetch --prune` 后 `git switch -C dev origin/dev`，否则下次 push 会被拒（或把旧历史推回去）。
- **手动重编/补发（2026-11-01 起带前置检查）**：Actions → _Release Windows Package_ → Run workflow。`ref` 默认 `main`（重发就保持默认：编的就是当前仓库这份代码）、`checks` 决定要不要在打包机上重跑全量用例、勾上 `publish` 才会发 Release。**勾了 `publish` 会先查"这个版本发过没有"**：`gh release view v<版本>` 成功就直接拦住（编译都不开始），并告诉你怎么删（`gh release delete v<版本> --cleanup-tag`）；删干净了就按 `packaging.py` 里**现在写的**版本号编译并重新发。**但“删掉 Release 再重发同一版本”这条路其实关死了**（2026-11-01 实测）：发布过的 Release 会把它的 tag 锁成**不可变**，重建会 `422 tag_name was used by an immutable release`。**关掉仓库里的 `Enable release immutability` 也不解锁**：那个设置只约束之后的发布，已经用过的 tag 名被永久保留 —— 实测关掉之后重跑同一个版本号仍然 422（仓库里也没有任何 tag ruleset，`Cannot create ref due to creations being restricted` 是这项功能自己在 pre-receive 层留的）。所以前置检查连 **tag** 一起查 —— tag 还在就当场拦住，并提示“要发新版本请把 `__version__` 往上加一位”。守卫：`test_manual_republish_refuses_a_version_that_is_already_released` 与 `test_the_publish_gate_skips_a_version_whose_tag_is_already_taken` 与 `test_the_publish_gate_skips_a_version_whose_tag_is_already_taken`。
- 构建前仍要过全部门禁（`ruff` / `mypy` / 全量用例 + 平台口径的覆盖率判定，见 `release-build.yml` 的 `Run build checks` 与 `Run the test suite under this platform's coverage config`，发布与手动档默认跑、门禁档跳过）与图标指纹检查；产物只上传 `release/` 目录（zip、校验和、元数据）。**覆盖率那一步不许裸跑 `pytest --cov`**：pyproject 里的 `[tool.coverage.report]` 是三平台并集的基线，拿它判单平台会把别的平台专属行记成缺口（2026-10-31 实测：2998 条全绿、覆盖率报 99.94%），所以发布档与 CI 覆盖率作业、本地 `run_tests_local.py --coverage` 一样，先用 `scripts/coverage_platform.py` 写平台配置再用 `coverage run --rcfile=…` 采数、`coverage report --rcfile=…` 判门槛（守卫在 `tests/unit/test_ci_workflow.py`，连“每步退出码要拦”一起钉住）。
