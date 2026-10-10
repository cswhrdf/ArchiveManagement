# 贡献指南

感谢愿意花时间。这是一个**会读写真实游戏存档**的桌面工具，所以它对"怎么验证一件事"比较较真：
这里多数约定的出发点不是洁癖，而是"这个缺陷漏到用户那儿就是数据损失"。下面按 **上手 → 怎么改 →
怎么交** 的顺序写；要查更细的机制时，正文里都给了 `docs/` 的落点。

- 许可：贡献按仓库现有的 **BSD-3-Clause** 发布（见 [LICENSE](LICENSE)）。
- 语言：文档、issue、代码注释与断言消息都用**中文**（提交信息不限语言）；面向用户的文案走 i18n 目录（见下文）。
- 默认分支是 `main`；`main` 与 `dev` 都会触发 CI。

## 一、准备环境

要求 Python >= 3.12 与 [uv](https://docs.astral.sh/uv/)。与 [README](README.md) / [docs/development.md](docs/development.md)
的快速开始同源：

```shell
uv sync --locked                                            # 装齐五个开发依赖组
uv run pre-commit install                                   # 装提交钩子
uv run python -m archive_management init --root ./dev-data   # 初始化目录/配置/日志/数据库
uv run python -m archive_management doctor --root ./dev-data # 健康检查(平台/目录/数据库版本)
uv run python -m archive_management gui --root ./dev-data    # 启动界面
uv run python -m archive_management gui --smoke 1            # GUI 冒烟自检(自动关闭)
```

- 开发数据全部收敛在 `--root ./dev-data` 下（配置、日志、数据库、备份都在里面，已被 `.gitignore` 忽略）。
- Linux 上跑界面需要图形环境，没有显示器时用虚拟显示：`xvfb-run -a uv run pytest`。
- 快捷键权限、注册表探测、回收站等平台差异见 [docs/platforms.md](docs/platforms.md)。

**改过依赖之后，先 `uv sync` 再提交。** 提交钩子里的 `uv run` 全部带 `--no-sync`，它不会替你重建
环境（原因见 [第七节](#七两个坑)）。

## 二、提交前要过的门禁

### 本地钩子：快，但仍然是"会拦人"的

`pre-commit install` 之后，每次提交自动跑 7 条（配置在 [.pre-commit-config.yaml](.pre-commit-config.yaml)）：

| 钩子 | 干什么 |
| ---- | ------ |
| `ruff check (autofix)` | 静态检查并自动修可修的项 |
| `ruff format` | **就地排版并把结果加回暂存区**（不是只报"哪些文件会被改"） |
| `compact JSON` | 把 `src/archive_management/resources/` 顶层的固定数据清单压回单行紧凑格式 |
| `mypy` | 宿主平台上的类型检查（strict） |
| `deptry` | 依赖卫生：未声明 / 多余 / 传递依赖（依赖清单或导入变了都会重跑） |
| `color-contrast` | 调色板的对比度守卫（只在调色板/对比度相关文件变动时跑） |
| `pytest (blocker+critical)` | 最高两档严重等级的用例 = 数据安全 + 核心逻辑 |

想手动跑同一套：`uv run --no-sync pre-commit run --all-files`。

### CI：全量、三平台、分片

- **PR**：三个平台各跑一遍**全量**（Linux 3 片、Windows 2 片、macOS 1 片），分片只影响墙钟，
  不影响结论；静态检查、性能基准、视觉回归也在这轮跑，并出**覆盖率门禁**与**完整 Allure 报告**。
- **push 到 `dev` / `main`**：只按改动路径跑相关的分片（确认没改坏），**不出报告**。
- **nightly（UTC 19:00 = 北京次日凌晨 3 点）**：由 `nightly.yml` 判断 `dev` 24h 内有没有新提交，
  有就带 `ref: dev` 调用 `ci.yml` 跑一轮全量 + 报告。全量链与报告链**只有 `ci.yml` 一份定义**
  （`nightly.yml` 只有"门 + 调用"，不复制任何作业）。
- 公共检查（ruff / mypy / deptry / bandit / pip-audit / radon + xenon）只在 Ubuntu 跑一次，带 `env=common`。
- 所以覆盖率结论在 **PR 与 nightly 的报告**里看（`Coverage report` 条目）；push 那一轮没有报告，
  只有分片日志与分片产物。想在本机核，就按第三节跑那两步口径。

细节（矩阵、分片权重、报告自检、失败现场留证）见 [docs/testing.md](docs/testing.md) 第 4、6 节。

## 三、覆盖率是硬门禁：100%

`pyproject.toml` 的 `[tool.coverage.report] fail_under` 是 **100**，平台标记机制与豁免规则在
[scripts/coverage_platform.py](scripts/coverage_platform.py) 与
[docs/testing.md](docs/testing.md) 的"有意不统计的覆盖"一节。三条要点：

1. **判门禁必须全量。** 覆盖率是"整套用例合起来覆盖了什么"，任何子集运行（包括 VS Code 测试面板
   显示的行内覆盖率、只跑某个文件的用例）给出的都是"这次跑了哪些用例"的数字，与结论无关 ——
   同一个文件在不同子集里可以是 25%、56% 或 100%。
2. **两种口径别混。** `pytest --cov` 走的是合并口径（不做平台排除，因为 pytest-cov 在导入根
   conftest 之前就构造好了 Coverage 对象）；按平台判定要走命令行两步：

   ```shell
   uv run python -m coverage run --rcfile=.coverage-platform.rc -m pytest -q   # 全量, 约 14 分钟
   uv run python -m coverage report -m --rcfile=.coverage-platform.rc
   ```

3. **只想定位某个文件缺在哪**（秒级）：跑**这个文件自己的用例**再看它的报告，例如

   ```shell
   uv run python -m coverage run --rcfile=.coverage-platform.rc --data-file="$env:TEMP\.coverage-x" -m pytest tests/unit/test_ui_dropdown.py tests/integration/test_gui_dropdown.py -q
   uv run python -m coverage report -m --rcfile=.coverage-platform.rc --data-file="$env:TEMP\.coverage-x" --include="*dropdown.py"
   ```

   注意 `--include` 在 Windows 上要用 `*dropdown.py` 这种**不带正斜杠**的写法（数据里的路径是反斜杠）。

**缺了行怎么办**：优先补用例。确实够不着的行（防御分支、别的平台才走得到的块）才写豁免，且必须写对：

- 平台专属代码写 `# platform: <平台…> - 原因`（只在**别的**平台上被排除，自己平台上照常统计）；
- 真的只能写 `pragma` 时，写法是 `# pragma: no cover - 原因` / `# pragma: no branch - 原因`：原因至少
  5 个字、不能是 `TODO`/`无`/`待补`，**不许标在 `def`/`class` 行上**，`no branch` 只能标在分支行
  （这些规则由 `tests/unit/test_coverage_pragmas.py` 逐一校验，写错等于静默失效）。

## 四、测试怎么写

- **分层**：`tests/unit`（纯逻辑）/ `tests/integration`（文件系统、真实窗口）/ `tests/performance` / `tests/security`。
  默认 `pytest` 跑前两层；性能与安全只在 CI（或显式指定目录）跑。
- **严重等级**：每个模块在 `pytestmark` 里**恰好声明一个**等级（`blocker` / `critical` / `normal` /
  `minor` / `trivial`，判定标准见 [docs/testing.md](docs/testing.md) 第 1 节），需要更细时给单个用例加
  `@pytest.mark.blocker` 之类。本地钩子跑的就是 `--min-severity=critical` 这个子集。
- **风格**：裸 `assert` + 能定位问题的失败消息；环境用 `tmp_path`、替身用 `monkeypatch`（仓库里不用
  `unittest.mock`）；用例自带数据，不依赖执行顺序或别的用例的残留。
- **咬合验证**：写完守卫/用例，**故意改坏一次**，确认它真的变红，再还原（"看起来在守、其实空转"的断言
  比没有断言更糟）。新增一个守卫时，最好同时给出"改坏哪个字它就红"。
- **探针用完即删**：为了量一个数字写的一次性脚本不要留在仓库里；长输出先落盘再读（避免 traceback 被截断）。
- **三平台都要想一遍**：CI 上的桌面尺寸、字体度量（CJK 字体可能缺失）、窗口管理器行为都与本机不同。
  GUI 用例的实测坑见 [.github/instructions/gui-tests-on-ci.instructions.md](.github/instructions/gui-tests-on-ci.instructions.md)。
- 新增用例的收尾清单见 [docs/testing.md](docs/testing.md) 第 8 节。

## 五、代码约定

- **格式与检查**：交给工具，不要手工争论。`ruff format` 的配置刻意锁定为 **Black 稳定版默认风格**；
  `ruff check` 的选择集与 `mypy`（strict）都必须干净。两条编码约定是配置补不了的：含中文的长字符串按
  **显示宽度**折行；断言消息保持单行（太长就先存进变量），详见 [docs/development.md](docs/development.md)
  的"质量门禁"一节。
- **`.py` 里不要出现全角标点**（RUF001/002/003 会拦）：中文标点只出现在**面向用户**的文案资源里，
  代码注释/字符串里该用半角，必要时用码位（如 `\uff0c`）表达。
- **用户可见文案走 i18n**：`src/archive_management/resources/i18n/*.json`，不要写死在界面代码里；
  中英文案里标点风格有守卫盯着。`resources/` 顶层的固定数据清单会被钩子压成单行，属正常现象。
- **日志纪律**：不落凭据、不落文件内容、不落完整敏感路径；高风险操作 INFO、基础操作 DEBUG。
- **分层**：业务层（`domain/`、`application/`）不直接调用 Tkinter、HTTP 或文件系统，能力通过接口注入，
  便于替换与测试（目录导览见 [docs/development.md](docs/development.md)）。
- **注释写"为什么"**：这个仓库的注释习惯是把踩过的坑写进去（实测数字、平台差异、当时为什么选这条路），
  改动时请保留这类信息，别把它们当噪音删掉。

## 六、界面改动的额外要求

界面是这个项目里"判据最多"的部分 —— 颜色、间距、圆角、字号、对比度、键盘可用性、文案截断都有守卫：

- **颜色只能从调色板取**（`ui/palette.py` 的具名 token），包括输入控件的边界用 `input_border`、
  面板外框用 `border` 这种区分；写死颜色不会跟主题重绘，对比度守卫与主题重绘守卫都会红。
- **对比度**有下限（正文 4.5:1、非文字与输入边界 3:1、禁用态 2.5:1），改动颜色前先看
  [docs/testing.md](docs/testing.md) 的"对比度判据"一节。
- **键盘可用性**：能聚焦、有可见焦点环、Esc/回车接得上（补丁在 `ui/keyboard.py`）。
- **文案**：全角标点、末行不许只剩标点、空状态要给下一步 —— 都有用例量。
- 动手前建议先读 [docs/ui-notes.md](docs/ui-notes.md) 与 [docs/features.md](docs/features.md)。
- 界面改动请在 PR 里附**前后对比截图**。`ui-review/` 是本地复核材料（已被忽略），不进版本库。

## 七、两个坑

**1. 提交时报 `os error 32 另一个程序正在使用此文件`。** 那不是代码问题：钩子里的 `uv run` 没关隐式
sync 时，"刚改过依赖 + 提交"会让提交过程重建 `.venv`，而编辑器常驻的 `ruff.exe` 正占着它，复制失败、
环境卡在半装。现在钩子都带 `--no-sync`，所以正常情况下不会再发生；真遇到时按这个顺序处理：

1. 在**终端**里（别在编辑器插件正跑 ruff 时）`uv sync`；
2. 仍报占用：`Get-Process ruff | Select-Object Id,Path` 找到占用者，**重载 VS Code 窗口**（或临时禁用
   Ruff 扩展）后再 `uv sync`；
3. 改过依赖却忘了同步：钩子会明确报"找不到工具"，`uv sync` 之后重试提交即可。

**2. 本地全量太慢。** 默认 `uv run pytest` 只跑单测与集成；要看全量用

```shell
uv run python scripts/run_tests_local.py    # 两个进程分片跑, 约省 1/3 墙钟
```

**不要**用 `pytest-xdist` 代替分片：GUI 用例各自起真实窗口，多个 worker 会互相拖垮（实测还会多出
Tk 初始化失败）。分片/耗时档案的机制见 [docs/testing.md](docs/testing.md) 第 6 节。

## 八、文档与手册

文档分两层，**不要互相复制内容**：

- `wiki/`：**面向用户的使用手册**，也是手册的唯一内容源（改手册就改这里，`.github/workflows/wiki.yml`
  会把它镜像到 GitHub Wiki）。
- `docs/`：**面向开发与实现**的说明（命令行、平台差异、测试体系、打包发布、CI）。

行为改了、用户看得见，就顺手同步 `wiki/` 与 [README](README.md)；机制改了，同步对应 `docs/*.md`；
还开着的取舍与待办记在 issue 里（别只留在聊天记录或 PR 描述里）。

另外仓库里有一批**给 AI 助手的路径级规则**（`.github/instructions/*.instructions.md`，按路径自动生效）：
用 AI 助手改界面滚动区、CI 工作流或 GUI 用例时，让其读对应那份，可以少踩已经记录过的坑。

## 九、提交与拉取请求

- **分支**：`main` **禁止直接提交**，只接受 `dev` 或 `bugfix/*` 分支的 PR 合入。**开新功能一律从 `dev` 拉
  主题分支**（`dev` 是集成分支：功能先合回 `dev`，再由 `dev` 合入 `main`）；修缺陷用 `bugfix/<一句话>`。
- **提交信息**：**不要求中文**（用什么语言都行），一行说清"做了什么"，必要时在正文补"为什么"与实测数字。
  仓库里现有的常见形态是 `<范围>: <一句话>`，例如 `分支图: 让悬停那条守卫读 hovered`、`CI: PR 按改动路径分层`。
- **提交时**钩子会自动跑第二节那套；排版与 JSON 压缩钩子会把结果加回暂存区（这是设计行为，不需要你
  再 `git add` 一次）。
- **PR 里请写清四件事**：动机（解决什么）、做法（为什么选它）、**怎么验证的**（跑了什么、改坏什么会红）、
  影响面（平台、界面、数据安全）。界面改动附截图，涉及用户可见行为附 `wiki/` 更新。开 PR 时
  [.github/pull_request_template.md](.github/pull_request_template.md) 会把这几块预先填好，用不到的条目删掉即可。
- CI 绿了再请求评审；如果某条红是你认为的**既有问题**，在 PR 里注明，别用跳过/放宽判据的方式绕过。

遇到拿不准的事，直接开一个 issue 问（用哪个模板都行），比改完再返工便宜。
