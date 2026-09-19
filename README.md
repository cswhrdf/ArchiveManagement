# ArchiveManagement

用于备份、分支、恢复和整理游戏存档的桌面工具。

它管理多个游戏及其一个或多个存档位置，把每次备份保存为带逐文件哈希清单的完整快照，用分支树和时间线呈现历史，允许随时恢复任一备份，并在明确确认后把不再需要的原始存档目录移入系统回收站。界面用 CustomTkinter 编写，数据存放在本地 SQLite 与应用自有的备份目录中，依赖与虚拟环境由 [uv](https://docs.astral.sh/uv/) 管理。

## 功能概览

- **游戏与存档位置管理**：录入游戏，添加存档目录或文件（记录规范化绝对路径，校验可用性、重复与高风险目标），原始位置与应用管理的备份目录始终分开展示。
- **备份、分支与时间线**：一次备份 = 一份完整快照（临时目录 + 逐文件 SHA-256 + 原子提交，绝不覆盖已有备份）；"向下保存"沿当前节点继"创建分支"从任一节点分出新的线路；内容没变化时不会产生重复备份。
- **自动与定时备份**：按游戏独立配置周期（`30m`/`2h`/`1d`）与自动备份保留份数；自动备份超出保留份数时清理最旧的一份。
- **按需恢复**：恢复前预检快照完整性、写回目标与游戏进程；默认先创建"恢复前安全点"，写回使用同盘暂存 + 原子替换，失败可回滚。
- **原始目录管理**：删除原始存档位置时默认移入系统回收站（绝不永久删除），需输入游戏名确认，并拒绝盘符根目录、用户主目录与备份目录。
- **统一游戏主页**：集中展示全部游戏的平台、存档位置数、备份数、最近活动与风险状态，支持视图/平台/类型/名称筛选、列表与海报两种展示、分页与自定义标签。
- **本地游戏发现**：从 Steam/Epic/GOG/Ubisoft 的安装清单、Windows 注册表与自定义监控目录中发现游戏，由用户确认后导入，不会自动改动游戏库。
- **全局快捷键**：默认 `Win+Alt+S`（保存）与 `Win+Alt+Z`（创建分支），可在设置里现场录制修改（必须含 Win/Ctrl/Alt/Shift 之一且至少一个字母键）。
- **可追溯**：所有用户操作写入滚动日志文件（不落数据库），高风险操作用 INFO、基础操作用DEBUG；日志不含凭据、文件内容与完整敏感路径。

## 快速开始

要求：Python >= 3.12 与 uv。

```shell
uv sync --locked                                              # 安装依赖(含 dev 组)，不安装当前项目本身
uv run python -m archive_management init --root .\dev-data    # 初始化目录/配置/数据库
uv run python -m archive_management doctor --root .\dev-data  # 健康检查
uv run python -m archive_management gui --root .\dev-data     # 启动图形界面
uv run python -m archive_management gui --smoke 1             # GUI 冒烟自检(自动关闭)
uv run pre-commit install                                     # 安装 Git 提交钩子
```

不带子命令运行时默认执行 `init`；`--root` 可以把全部数据收敛到指定目录，便于便携使用与调试。首次启动是空库：用顶栏的"+ 添加游戏"录入游戏或游戏发现中导入已发现的游戏，再在游戏设置里添加"原始存档位置"，之后即可备份、创建分支与恢复。命令行参数与配置文件（含内容非法时自动还原为默认值的行为）见 [docs/development.md](docs/development.md)。

上面的命令都在**仓库根目录**执行。本项目以工具形式开发、**不作为包安装**：仓库根的 `.env`（`uv run` 会自动加载）把 `src` 加进 `PYTHONPATH`，所以 `python -m archive_management` 不需要安装就能运行；绕过 `uv run` 直接调用 `.venv` 里的解释器时它不会生效，需要自己设置 `PYTHONPATH=src`。

## 平台支持

| 能力                           | Windows             | Linux                  | macOS(未进行实机测试)  |
| ------------------------------ | ------------------- | ---------------------- | ---------------------- |
| 应用目录/配置/日志/数据库/备份 | ✅                   | ✅                      | ✅                      |
| 图形界面                       | ✅                   | ✅ 需图形环境           | ✅                      |
| 备份/恢复/删除到系统回收站     | ✅                   | ✅                      | ✅                      |
| 全局快捷键                     | ✅                   | ✅ 需图形环境与监听权限 | ✅ 需授予"辅助功能"权限 |
| 本地游戏发现                   | ✅ 安装清单 + 注册表 | ✅ Steam 清单与监控目录 | ✅ 安装清单(macOS 路径) |
| PyInstaller 打包               | ✅ onedir + onefile  | ⏳ 未纳入               | ⏳ 未纳入               |

表中的"图形环境"指系统提供的图形显示服务（Windows 与 macOS 自带，Linux 上是 X11 或
Wayland）：没有显示器的服务器或 CI 可以先用虚拟显示，例如 `xvfb-run -a uv run pytest`。
各平台探测来源的差异（GOG/Ubisoft 仅 Windows 可用等）与 macOS 的权限、目录位置见
[platforms.md](./docs/platforms.md)；`doctor` 子命令会打印当前平台与注册表探测是否可用。

## 质量门禁

```shell
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --cov
uv run deptry .       # 依赖卫生(未声明 / 多余 / 传递依赖)
```

上面这些除了最后一项之外都在本地提交钩子与 CI 里跑；其中 `pytest --cov` 在 Windows、Ubuntu、macOS 三平台各跑一遍，`ruff` / `mypy`（宿主平台各一次，另加 `--platform win32` / `darwin` 覆盖平台专属分支）/ `deptry` 属于公共检查，CI 只在 Ubuntu 跑一次（结论项带 `env=common`，归入报告的 `Common` 环境）。CI 还会额外跑一次与平台无关的静态分析与依赖检查：`bandit -r src`（源码危险模式）、`pip-audit`（依赖漏洞）、`radon` / `xenon`（复杂度，门槛与 Ruff 的 `mccabe` 同为 10 分）。详见 [development.md](./docs/development.md) 的"质量门禁"一节。

提交钩子只运行本次变动的静态检查与最高严重等级的单元测试（快速反馈）；CI 在 Windows、
Ubuntu、macOS 上运行全量测试，并单独执行性能基准与安全测试，最后合并成一份 Allure 报告。
测试分类、基准阈值与报告内容见 [testing.md](./docs/testing.md)。

## 打包

```shell
uv run pyinstaller --noconfirm --clean packaging/archive-management.spec
```

构建读取 `packaging.py` 中的源码版本，产出 Windows x64 的 onedir 压缩包、onefile 可执行
文件、SHA-256 校验文件与构建元数据。构建与发布流程见 [development.md](./docs/development.md)。

---

## 文档

| 文档                                    | 内容                                                       |
| --------------------------------------- | ---------------------------------------------------------- |
| [features.md](./docs/features.md)       | 界面结构、备份/分支/定时任务、恢复、原始目录管理、操作日志 |
| [library.md](./docs/library.md)         | 统一游戏主页、列表与海报、分类筛选、本地游戏发现           |
| [hotkeys.md](./docs/hotkeys.md)         | 全局快捷键的默认键位、自定义流程与实现约定                 |
| [platforms.md](./docs/platforms.md)     | Windows / macOS / Linux 支持矩阵与平台注意事项             |
| [development.md](./docs/development.md) | 开发环境、命令行、配置文件、质量门禁、打包与发布           |
| [testing.md](./docs/testing.md)         | 测试分类、性能基准与安全测试、Allure 报告汇总              |
