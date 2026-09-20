# 开发、命令行与发布

## 环境要求

- Python >= 3.12
- [uv](https://docs.astral.sh/uv/)（依赖与虚拟环境管理）

```shell
uv sync --locked     # 安装依赖（含 dev 组），不安装当前项目本身
uv run pre-commit install
```

上面的命令在**仓库根目录**执行。本项目以工具形式开发、**不作为包安装**（不发布到 PyPI，也不声明 `[project.scripts]`），自身源码靠仓库根的 `.env` 进入导入路径：`uv run` 会自动加载它（内容是 `PYTHONPATH=src`），所以 `python -m archive_management` 不需要安装就能运行。绕过 `uv run`（例如直接调用 `.venv\Scripts\python.exe -m archive_management`）时该文件不会生效，需要自己设置 `PYTHONPATH=src`；个人本地覆盖请另建 `.env.local` 并用 `uv run --env-file .env.local ...`——仓库自带的 `.env` 会被提交，不要往里放密钥（凭据走系统凭据库）。

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
uv run python -m archive_management init --root .\dev-data    # 初始化目录/配置/日志/数据库
uv run python -m archive_management doctor --root .\dev-data  # 健康检查（平台、目录、数据库版本）
uv run python -m archive_management gui --root .\dev-data     # 启动图形界面
uv run python -m archive_management gui --smoke 1             # GUI 冒烟自检（自动关闭）
```

- 不带子命令运行时默认执行 `init`。
- `--root` 可以把全部数据收敛到指定目录（便携模式与开发调试），此时配置、数据、日志、缓存都在该目录下；不加则使用系统约定的应用目录。
- 全局 `--verbose` 让控制台也打印 DEBUG 级基础操作（默认只有 INFO 及以上的高风险操作会打印）。
- 命令要在**仓库根目录**执行：仓库根的 `.env` 提供 `PYTHONPATH=src`（`uv run` 自动加载），其中的 `src` 是相对当前目录解析的。

## 配置文件

配置文件是配置目录下的 `config.json`（主题、全局快捷键、日志参数），由 pydantic 严格校验：未知字段、版本不符、非法快捷键都直接拒绝。

**内容非法时会自动把文件还原为默认值**，并在日志/界面里说明（CLI 的 `init` 与 `doctor` 会在输出里列出一行"配置还原"），应用照常启动；原文件会改名保留为同一目录下的`config.json.invalid`，方便对照自己改错了什么——既不让写坏的配置卡住启动，也不静默丢掉用户的改动。

## 质量门禁

本地提交钩子 + CI 里跑的检查（`pytest --cov` 与平台相关，Windows/Ubuntu/macOS 三平台都跑；其余几项是公共检查，CI 只在 Ubuntu 跑一遍，结论归入报告里显式声明的 `Common` 环境）：

```shell
uv run ruff check .
uv run ruff format --check .
uv run mypy                     # 宿主平台(本地钩子里跑的也是这一条)
uv run mypy --platform win32    # Windows 专属分支(宿主那次只收窄自己那一支)
uv run mypy --platform darwin   # macOS 专属分支
uv run pytest --cov
uv run deptry .                 # 依赖卫生: 未声明 / 多余 / 传递依赖(本地钩子也会跑)
```

与平台无关的静态分析与依赖检查（CI 的 `analysis` job 跑一次，本地想复现就手动执行）：

```shell
uv run bandit -r src   # 源码里的危险模式(exec/eval、弱随机、subprocess、硬编码口令等)
uv run pip-audit       # 已安装依赖的已知漏洞
uv run radon cc -s --min B src                                   # 复杂度报告
uv run xenon --max-absolute B --max-modules F --max-average F src # 复杂度门槛
```

**复杂度的两把尺子同分**：门槛取 `10`，与 Ruff 的 `[tool.ruff.lint.mccabe] max-complexity`完全相同；Radon 的等级对应 1-5 / 6-10 / 11-20 / …，所以“不超过 10”就是“最差只能到 B 级”，`xenon` 的模块级与平均复杂度不设限（Ruff 并不检查这两项）。但两者的**刻度不同**：Radon 会把 `with`、`assert`、布尔运算也算作分支，同一段代码通常比 Ruff 的 C901 高 2~5 分，因此写新函数时以 Radon 为准（`radon cc --min C src` 当前为空，说明全部函数都在 B 级以内）。`tests/unit/test_report_verification.py` 里有守卫，保证两处数值不会各自漂移。

`deptry` 的两处说明：本项目不作为包安装，所以 `known_first_party` 里同时列了 `archive_management` 与 `tests` 下的共享辅助模块。

`bandit` 只扫 `src`：用例里满是 `assert` 与故意构造的脏数据，扫它们只会制造噪音；运行期行为由 `tests/security` 负责（两者互补：Bandit 拦“写法危险”，安全用例拦“行为可被利用”）。

格式化用 `ruff format`，其配置项**刻意锁定为 Black 稳定版的默认风格**（`preview = false`、双引号、空格缩进、尊重魔法尾随逗号），所以换成 ruff 不会把存量代码重排。两处已知差异没有用配置去弥合，而是写成了编码约定：

- **行宽按显示宽度计算**：Ruff 把中日韩宽字符算 2 列（Black 只按字符数），所以含中文的长字符串会比 Black 更早折行；
- **断言消息保持单行**：`assert cond, msg` 放不下时两个工具的折行位置不同（Ruff 折消息、Black 折条件），因此约定把长提示先存进变量（如 `hint = "..."`），断言本身保持一行。若拆分，钩子会永远在两个工具的输出来回跳。

想核实"代码确实仍然符合 Black 风格"，可以用一次性环境跑 Black（不必装进项目依赖）：

```shell
uvx black --diff --line-length 88 --target-version py312 src tests scripts
```

输出应为 **0 个文件需要改动**，且与 `ruff format --check .` 同时成立。

本地提交钩子只对本次变动的 Python 文件执行 Ruff（检查 + 格式化）、mypy，并运行 `blocker`+`critical` 子集（= 数据安全与核心逻辑，等级定义见 [testing.md](testing.md) 的严重等级表），另外还会跑一次 `deptry`（依赖变更与导入变更都得重查，所以它的触发范围含 `pyproject.toml` / `uv.lock`），并把 `src/archive_management/resources/` 顶层的**固定数据清单**（如平台工具排除清单）自动压回单行紧凑 JSON（`scripts/compact_json.py`）。这个钩子跑完会把文件 **`git add` 进暂存区**（这次没改写也会加一次，顺手把索引里可能残留的多行版本同步成单行）：工作区与索引一起变成单行后 pre-commit 就不算它“弄脏了文件”，本次提交直接带上转换好的内容，不需要重新 `git add`；万一把结果加不进索引（没有 git 等），才会退回“就地改写并拦下提交”。`resources/i18n/*.json` 是给人审校的文案资源，不受影响。CI 会在 Windows、Ubuntu、macOS 上运行全量测试，并单独执行性能基准、安全测试与上面那批静态分析工具，最后合并成一份 Allure 报告。测试分类、基准阈值与报告汇总见 [testing.md](testing.md)。

### 想在本地看 Allure 报告：

1. `uv run pytest --alluredir=allure-results`
1. `allure generate allure-results --output allure-report`
1. `allure open allure-report`（标题、界面语言与端口都在 `allurerc.mjs` 里定好了；地址冲突时加 `--port 8081`）
（完整命令、常见坑与报告自检见 [testing.md](testing.md) 第 7 节）。**生成报告时请停在仓库根目录**：`allurerc.mjs` 就在这里，它负责把结果上的平台标签映射成报告的"环境"（Windows/macOS/Linux），换目录执行会让环境静默退回 `default`（自检脚本会把这种情况判为失败）。

用例失败时会**自动留现场**（coredumpy dump + 界面截图 + 摘要，机制与纪律见 [testing.md](testing.md) 第 6 节）：dump 落在仓库根的 `crash-dumps/`（已被忽略），用 `coredumpy load crash-dumps/<用例>.dump` 进 pdb，或在 VSCode 里用 coredumpy 扩展右键打开；不想留就加 `--crash-dump-depth=0`。

CI 的 pytest 作业是**分片执行**的（每平台 3 片并行，报告作业再合并结果与覆盖率）。本地想复现同一套流程：`--shard-count` / `--shard-index` + `scripts/merge_allure_results.py` + `uv run coverage combine`，命令与实测数据见 [testing.md](testing.md) 第 6、7 节。

## 打包

```shell
uv run pyinstaller --noconfirm --clean packaging/archive-management.spec
```

- 构建前应先通过全部门禁：`uv sync --locked`、Ruff（`check` + `format`）、mypy、pytest。
- 版本直接读取 `packaging.py` 中的源码版本。
- 产物为 Windows x64 的 **onedir** 压缩包、**onefile** 可执行文件、SHA-256 校验文件与构建元数据；发布包不得包含 API Key、开发机绝对路径或测试数据。`spec` 文件显式声明入口模块、图标与 CustomTkinter 主题资源，并按需补充隐藏导入。
- GitHub Actions 在 Windows runner 上构建并上传 `dist` 产物（手动触发也支持）。当前以Windows 为首要发布平台，macOS/Linux 打包未纳入。
- 包体积与启动时间（2026-09-19，Windows x64 / Python 3.12，含量化封面依赖 Pillow 12.3.0）：**onedir 合计约 57.3 MB**（其中 `_internal/PIL` 约 12.8 MB，是本轮首次引入的开销）、**onefile 约 29.1 MB**；在已初始化的数据目录上启动到窗口出现**约 2.1 s**（同条件源码运行同样约 2.1 s，冻结后没有额外开销）。测量方式：`ArchiveManagement.exe gui --root <已初始化目录> --smoke 0.1` 连跑三次取稳定值。

## 发布

- 自动构建在 `dev` 或 `bugfix/**` 分支的 Pull Request 合并到 `master` 后触发，也支持在Actions 中手动指定分支或提交重新构建。
- 正式 GitHub Release 的触发条件与版本生成方式尚未确定，当前不会由该流程自动创建 Release。可选方案是提交时手动维护源码版本，或由 CI 生成不写回源码的构建版本。
