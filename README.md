# ArchiveManagement

存档管理工具 —— 用于分支、备份、恢复和导出游戏存档的桌面工具.

## 开发

要求：Python >= 3.12 与 [uv](https://docs.astral.sh/uv/).

```shell
uv sync --locked          # 安装依赖（含 dev 组）并安装本包
uv run archive-management init --root .\dev-data   # 初始化目录/配置/数据库
uv run archive-management doctor --root .\dev-data # 健康检查
uv run pre-commit install # 安装 Git 提交钩子
```

### 启动UI
```shell
uv run archive-management gui --root .\dev-data # 启动图形界面(基于本地 SQLite, 首次为空库)
uv run archive-management gui --smoke 1         # GUI 冒烟自检(自动关闭)
```

GUI 使用本地 SQLite 作为数据源(阶段 C): 首次进入为空库, 通过左侧"+ 添加游戏"录入游戏, 再在"游戏设置"中为该游戏手动添加/编辑/删除"原始存档位置"(支持选择目录或文件, 记录规范化绝对路径并对可用性与重复做校验); 应用自动管理的备份目录与原始位置分开展示, 避免误操作.演示后端仅保留给离线开发与冒烟测试使用.

### 质量门禁（与 pre-commit 及 CI 一致）：

安装 pre-commit 后，提交钩子只对本次变动的 Python 文件执行 Ruff、Black 和 mypy，pytest 只运行最高严重等级的 `tests/unit`。

```shell
uv run ruff check .
uv run black --check .
uv run mypy src
uv run pytest --cov
```

GitHub Actions 会运行全部测试，并生成覆盖率报告和 Allure 报告。手动生成 Allure 结果可使用：

```shell
uv run pytest --alluredir=allure-results
npx --yes allure@3 generate allure-results --output allure-report
allure open
```
