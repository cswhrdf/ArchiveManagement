# 平台支持（Windows / macOS / Linux）

| 能力                                   | Windows                                         | Linux                                  | macOS                                   |
| -------------------------------------- | ----------------------------------------------- | -------------------------------------- | --------------------------------------- |
| 应用目录 / 配置 / 日志 / 数据库 / 备份 | ✅ 人工实测                                      | ✅ CI 跑全量                            | ✅ CI 跑全量                             |
| 图形界面                               | ✅ 人工实测 + 29 张截图                          | ✅ CI（xvfb + Noto CJK 字体）           | ✅ CI（首次运行待观察）                  |
| 备份 / 恢复 / 删除到系统回收站         | ✅ 回收站内容已逐字节核对                        | ✅ CI 跑全量                            | ✅ CI 跑全量                             |
| 全局快捷键                             | ✅ 真后端注册两个默认键实测                      | ✅ 需图形环境与监听权限                 | ✅ 需授予"辅助功能"权限                  |
| 探测：Steam                            | ✅ 注册表 + 默认安装目录                         | ✅ `~/.steam`、XDG 与 Flatpak/Snap 目录 | ✅ `~/Library/Application Support/Steam` |
| 探测：Epic                             | ✅ `%PROGRAMDATA%\Epic\...`                      | ❌ 无官方启动器，用监控目录             | ✅ `/Users/Shared/Epic Games/...`        |
| 探测：GOG / Ubisoft                    | ✅ 注册表                                        | ❌ 同 macOS                             | ❌ 没有注册表，用监控目录                |
| 监控目录                               | ✅ 人工实测                                      | ✅                                      | ✅                                       |
| PyInstaller 打包                       | ✅ onedir + onefile（已构建并量过体积/启动时间） | ⏳ 未纳入                               | ⏳ 未纳入                                |

平台相关实现集中在 `services/platforms.py`（平台识别）、`services/platform_scan.py`（按平台选择探测来源）、`services/hotkeys.py`（默认组合键与权限检测）。`uv run python -m archive_management doctor` 会先打印当前平台与注册表探测是否可用。

## 探测来源的平台差异

- **Steam 在三个平台上都可用**，只是安装位置不同：Windows 先读注册表登记的路径，再回落到默认安装目录；macOS 用 `~/Library/Application Support/Steam`；Linux 依次尝试 `~/.steam`、XDG 数据目录以及 Flatpak/Snap 沙箱内的目录。识别方式统一是库清单`libraryfolders.vdf` + `appmanifest_*.acf`。
- **Epic 只在 Windows 与 macOS 可用**（分别是 `%PROGRAMDATA%` 下的 `*.item` 清单与`/Users/Shared/Epic Games/...`）；Linux 没有官方启动器，该来源返回空。
- **GOG 与 Ubisoft 的安装位置只写在 Windows 注册表里**，因此在 macOS/Linux 上**显式跳过**（日志会记下"仅 Windows 可用"，而不是让查询静默返回空结果），这两个平台请用监控目录覆盖。注册表相关的探测只会在 Windows 上执行，其它平台连注册表读取器都不会构造。Ubisoft Connect 的子键是数字安装 id，游戏名取子键里的 `GameName`/`DisplayName`，都没有时退回安装目录的目录名。
- 监控目录在三个平台上的行为完全一致。

## 可信存档来源（按平台）

存档位置只接受**可信来源**，绝不按路径模板推断（推错的路径一旦被确认，恢复与删除会动到不该动的地方）。目前实际接入的来源只有一个：

| 平台                                    | 可信来源                                               | 状态                                             |
| --------------------------------------- | ------------------------------------------------------ | ------------------------------------------------ |
| Steam                                   | 云端同步清单 `userdata/<账号>/<appid>/remotecache.vdf` | ✅ 已实现（`services/steam_cloud.py`）            |
| Epic / GOG / Ubisoft Connect / 监控目录 | —                                                      | ⏳ 接口保留，本期一律“暂不支持”，界面提示手动添加 |
| 任意平台                                | 平台官方清单、游戏自带配置                             | ⏳ 尚未接入，只能手动填写                         |

- **Steam 云端清单怎么用**：清单里的相对路径要配上它记的 `root` 编号才能变成绝对路径。已知映射：`1` = 游戏安装目录、`2` = 用户的“文档”、`3` = `%LOCALAPPDATA%`、`12` = `%LOCALAPPDATA%Low`（与 Steam SDK 的 `ERemoteStorageFileRoot` 一致）。**只在把它与已安装清单（`appmanifest_*.acf`）求交集之后使用** —— 这个文件包含已卸载但保留了云同步状态的游戏，不筛一遍会凭空多出一批候选。
- **没有清单就没有候选**：没开云同步、清单缺失或损坏、`root` 编号认不出来，都只记 DEBUG 日志并返回空结果（“拿不到来源”是正常降级，不是错误），界面会说“需要手动添加”。
- **候选必须经用户确认**：探测结果里展示路径与危险标记，导入对话框里可改可勾选，确认后才写进存档位置（危险目标会被拦下；详细流程见 [library.md](./library.md) 的“存档路径推断”）。
- **其它平台的降级路径**：Epic / GOG / Ubisoft 走 `UnsupportedPlatformAdapter` —— 能力声明为不支持、返回空结果并带上“暂未实现”的说明，安装探测依旧可用，存档位置与封面由用户手动提供；它们将来接入时只需实现适配器接口，上层流程不用改。

## 这张表是按什么验的（跨平台与发布前验证）

上表里的 ✅ 分两类证据，**没证据的写"待观察"而不是打勾**：

- **CI 跑全量**：`pytest` / `pytest-report` / `security` 三个矩阵都按平台展开（Linux 3 片、Windows 2 片、
  macOS 1 片 —— macOS 按 ×10 计价所以少开片，见 `PLAN.md` 第 11.9 节），所以"单元 + 集成 + 安全"
  在每个平台都是真的执行过的结论，报告里按环境分开呈现。macOS 于 2026-09-30 恢复，**首次运行待观察**。
- **人工实测（本机 Windows，2026-09-30）**：真 `pynput` 后端注册两个默认全局快捷键（都 `registered=True`）；
  回收站删除后能在 `$Recycle.Bin` 里找到那个文件且内容逐字节一致（不存在时抛 `StorageError`，不会静默成功、
  也不会回退成永久删除）；`dangerous_target_reason` 在真盘上逐条验过（盘符根 / 用户主目录 / 受保护目录三种
  形态都拒，`protect_subpaths=False` 时"安装目录内的存档"放行）；`ui-review/capture.py` 抓了 29 张界面截图
  并用 `ui-review/screens/` 里的人眼复核（含受最近改动影响的列表对齐与批量导入弹窗）。
- **未验证并写明原因**：打包产物（onedir/onefile）的视觉一致性 —— 需要阶段 J/K 的发布流水线，而当前
  `release.yml` 只做 Windows、macOS 的 `.app` 未纳入；截图脚本是导入源码跑的，对打包版跑要另写
  "启动 exe + 按窗口句柄抓图"的入口（`crash_capture.grab_png(window=hwnd)` 已具备这条能力）。

## macOS 注意事项

- **全局快捷键必须授权**：macOS 会静默拦截未授权进程的按键监听，因此监听器启动后会检查`pynput` 的 `IS_TRUSTED`，未授权时停掉监听器并把"辅助功能"这一可操作原因交给界面，而不是显示"已注册"却永不触发。从终端开发运行时，需要授权的是终端/解释器（打包后是应用本体）。
- 应用目录由 `platformdirs` 解析：数据与配置在 `~/Library/Application Support/ArchiveManagement/`，日志在 `~/Library/Logs/ArchiveManagement/`。
- 进程探测会把 macOS 的 `.app` 包名与 Windows 的 `.exe` 一样在比较前去掉。
- CustomTkinter 在 Python 3.10 以下会调 `defaults write` 修改窗口外观；本项目要求Python >= 3.12，因此不会执行任何外部命令。

## Linux 注意事项

- **图形界面与全局快捷键都需要图形环境**（图形显示服务）：桌面与装有桌面环境的机器自带，没有显示器的服务器或 CI 需要先提供虚拟显示，例如 `xvfb-run -a uv run pytest`。
- 纯 Wayland 会话下，全局按键监听是否可用取决于合成器（必要时需要 `uinput` 权限），因此可能无法全局生效；X11 会话不受此限制。
