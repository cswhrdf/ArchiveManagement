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

```shell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov
```

格式化用 `ruff format`，其配置项**刻意锁定为 Black 稳定版的默认风格**（`preview = false`、双引号、空格缩进、尊重魔法尾随逗号），所以换成 ruff 不会把存量代码重排。两处已知差异没有用配置去弥合，而是写成了编码约定：

- **行宽按显示宽度计算**：Ruff 把中日韩宽字符算 2 列（Black 只按字符数），所以含中文的长字符串会比 Black 更早折行；
- **断言消息保持单行**：`assert cond, msg` 放不下时两个工具的折行位置不同（Ruff 折消息、Black 折条件），因此约定把长提示先存进变量（如 `hint = "..."`），断言本身保持一行。若拆分，钩子会永远在两个工具的输出来回跳。

想核实"代码确实仍然符合 Black 风格"，可以用一次性环境跑 Black（不必装进项目依赖）：

```shell
uvx black --diff --line-length 88 --target-version py312 src tests scripts
```

输出应为 **0 个文件需要改动**，且与 `ruff format --check .` 同时成立。

本地提交钩子只对本次变动的 Python 文件执行 Ruff（检查 + 格式化）、mypy，并运行 `blocker`+`critical` 子集（= 数据安全与核心逻辑，等级定义见 [testing.md](testing.md) 的严重等级表）；CI 会在 Windows、Ubuntu、macOS 上运行全量测试，并单独执行性能基准与安全测试，最后合并成一份 Allure 报告。测试分类、基准阈值与报告汇总见 [testing.md](testing.md)。

### 想在本地看 Allure 报告：

1. `uv run pytest --alluredir=allure-results`
1. `allure generate allure-results --output allure-report`
1. `allure open allure-report`（标题、界面语言与端口都在 `allurerc.mjs` 里定好了；地址冲突时加 `--port 8081`）
（完整命令、常见坑与报告自检见 [testing.md](testing.md) 第 7 节）。**生成报告时请停在仓库根目录**：`allurerc.mjs` 就在这里，它负责把结果上的平台标签映射成报告的"环境"（Windows/macOS/Linux），换目录执行会让环境静默退回 `default`（自检脚本会把这种情况判为失败）。

## 打包

```shell
uv run pyinstaller --noconfirm --clean packaging/archive-management.spec
```

- 构建前应先通过全部门禁：`uv sync --locked`、Ruff（`check` + `format`）、mypy、pytest。
- 版本直接读取 `packaging.py` 中的源码版本。
- 产物为 Windows x64 的 **onedir** 压缩包、**onefile** 可执行文件、SHA-256 校验文件与构建元数据；发布包不得包含 API Key、开发机绝对路径或测试数据。`spec` 文件显式声明入口模块、图标与 CustomTkinter 主题资源，并按需补充隐藏导入。
- GitHub Actions 在 Windows runner 上构建并上传 `dist` 产物（手动触发也支持）。当前以Windows 为首要发布平台，macOS/Linux 打包未纳入。

## 发布

- 自动构建在 `dev` 或 `bugfix/**` 分支的 Pull Request 合并到 `master` 后触发，也支持在Actions 中手动指定分支或提交重新构建。
- 正式 GitHub Release 的触发条件与版本生成方式尚未确定，当前不会由该流程自动创建 Release。可选方案是提交时手动维护源码版本，或由 CI 生成不写回源码的构建版本。
