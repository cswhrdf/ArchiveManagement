# ArchiveManagement

存档管理工具 —— 用于分支、备份、恢复和导出游戏存档的桌面工具.

## 开发

要求：Python >= 3.12 与 [uv](https://docs.astral.sh/uv/).

```shell
uv sync --locked          # 只安装依赖（含 dev 组）,不安装当前项目
uv run python -m archive_management init --root .\dev-data   # 初始化目录/配置/数据库
uv run python -m archive_management doctor --root .\dev-data # 健康检查
uv run pre-commit install # 安装 Git 提交钩子
```

### 启动UI
```shell
uv run python -m archive_management gui --root .\dev-data # 启动图形界面(基于本地 SQLite, 首次为空库)
uv run python -m archive_management gui --smoke 1         # GUI 冒烟自检(自动关闭)
```

GUI 使用本地 SQLite 作为数据源(阶段 C): 首次进入为空库, 通过左侧"+ 添加游戏"录入游戏, 再在"游戏设置"中为该游戏手动添加/编辑/删除"原始存档位置"(支持选择目录或文件, 记录规范化绝对路径并对可用性与重复做校验); 应用自动管理的备份目录与原始位置分开展示, 避免误操作.演示后端仅保留给离线开发与冒烟测试使用.

备份能力(阶段 D):

- "立即创建备份"(或全局快捷键 `Ctrl+Alt+S`)把当前存档位置复制为应用自有存储中的一次快照: 先写入同级的临时目录, 逐文件计算 SHA-256 并复核, 全部成功后才原子改名为正式快照; 目标已存在时直接失败, 绝不覆盖已有备份, 失败或取消不会留下半成品节点.
- "恢复到此节点"把**当前节点**切换到选中的备份(带 ● 标记), 之后的"向下保存"与"创建分支"都从该节点重新开始; 本阶段不会改写磁盘上的原始存档文件.
- "从此处创建分支"以选中节点为父节点并记录分支名(默认 `Branch`); "重命名 / 描述"可修改备份名称与最多 200 字的描述.
- "删除备份"按分支树决定处理方式: 同一线路上的节点被删除时后续节点自动上移; 删除分支根节点会先提示将连带删除其分支下的全部备份, 用户确认后整棵树一并删除.
- 默认展示"分支树"(按层级缩进, 自动备份只显示最新一份), 可切到"时间线"查看全部备份及其所属分支.
- 右侧任务卡可设置定期备份周期(`30m` / `2h` / `1d`, 留空即取消)与自动备份保留份数(默认 3 份); 自动备份超出保留份数时最旧的一份连同快照被清理, 且不会让后续节点变成孤儿.
- 每个备份节点都记录内容哈希与文件清单, 列表中的"已验证"标记来自快照清单的实际校验.

### 质量门禁（与 pre-commit 及 CI 一致）：

安装 pre-commit 后，提交钩子只对本次变动的 Python 文件执行 Ruff、Black 和 mypy，pytest 只运行最高严重等级的 `tests/unit`。

```shell
uv run ruff check .
uv run black --check .
uv run mypy
uv run pytest --cov
```

每个测试用例都有最长执行时间(`pyproject.toml` 中的 `--timeout=60 --timeout-method=thread`)：卡住的用例会被计时器打断并打印所有线程的堆栈，而不是让整套测试一直等下去。个别确实较慢的用例可用 `@pytest.mark.timeout(120)` 单独放宽。

GitHub Actions 会运行全部测试，并生成覆盖率报告和 Allure 报告。手动生成 Allure 结果可使用：

```shell
uv run pytest --alluredir=allure-results --clean-alluredir
npx --yes allure@3 generate allure-results --output allure-report
allure open
```

每个用例都带四层 Allure 标签：标签由 `tests/conftest.py` 统一写入，测试模块用模块级 `pytestmark` 声明默认值，单个用例可用同名标记覆盖（如 `@pytest.mark.story("删除分支")`）；有未声明取值的用例按目录推断层次并在报告里归入“未分类”，`epic`/`layer` 拼错会在收集期直接报错。

| 标签              | 含义                                     | 示例                             |
| ----------------- | ---------------------------------------- | -------------------------------- |
| `@allure.epic`    | 产品级模块（闭集，见 `conftest._EPICS`） | 备份与分支 / 界面框架            |
| `@allure.feature` | 功能模块（与生产模块一一对应）           | 快照服务 / 数据仓储 / 主题调色板 |
| `@allure.story`   | 具体用户场景                             | 删除备份节点 / 深浅色主题切换    |
| `layer`           | 测试层次                                 | unit / integration / e2e         |

`epic`/`feature`/`story` 驱动报告中的“按产品级模块/功能/场景的稳定性分布”；`layer` 只能通过 `allure.dynamic.label("layer", ...)` 写入（`allure-pytest` 没有 `allure.layer` 装饰器），用于“按层耗时直方图”，同时写入 `parentSuite/suite/subSuite` 作为兼容。

稳定性分布控件需要至少 5 次历史运行的数据，CI 通过 `.allure/history.jsonl` 累积。本地想看到填充效果可复用同一套配置（注意每次都要先删掉输出目录，否则 Allure 不会覆盖已有报告）：

```shell
New-Item -ItemType Directory -Force .allure | Out-Null
Set-Content .allure/allurerc.json '{"historyPath": "./.allure/history.jsonl", "appendHistory": true, "historyLimit": 40}'
allure generate allure-results --output allure-report --config ./.allure/allurerc.json
```

### 构建 Windows 测试包

当前自动构建方案暂定为：当 `dev` 或 `bugfix/**` 分支的 Pull Request 合并到 `master` 后触发。也可以在 GitHub Actions 中手动指定分支或提交运行构建：

```shell
uv run pyinstaller --noconfirm --clean packaging/archive-management.spec
```

构建流程会直接读取 `packaging.py` 中的源码版本。每次构建会上传 Windows x64 的 onedir 压缩包、onefile 可执行文件、SHA-256 校验文件和构建元数据，供测试和验收使用。

正式 GitHub Release 的触发条件和版本生成方案尚未确定，当前不会由该流程自动创建 Release。稳定方案可以是提交时手动维护源码版本，或由 CI 生成不写回源码的构建版本，待发布策略确定后再启用。
