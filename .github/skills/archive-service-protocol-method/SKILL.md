---
name: archive-service-protocol-method
description: '在 ArchiveService 协议(ui/backend.py)上新增或修改一个方法时用: 按"协议 → sql_backend → demo_backend → 界面接线 → i18n 两份 → 用例 → 覆盖率"的顺序落地, 含每一步的落点、漏一处就会红的那条守卫(两个后端一致性由 mypy 兜住)与自检命令。Use when adding or changing an ArchiveService protocol method, wiring a new backend capability into the UI, or when a GUI test fails because one backend lacks a method.'
argument-hint: "要新增/修改的协议方法, 与它服务的界面动作(例: 列出某游戏的导出历史)"
---

# 新增/修改一个 `ArchiveService` 协议方法

`ui/backend.py` 的 `ArchiveService` 是界面唯一的依赖面；真实实现是 `SqlArchiveService`（`ui/sql_backend.py`），演示实现是 `DemoArchiveService`（`ui/demo_backend.py`）。**任何一步漏了都会在某个守卫上变红**，照下面的顺序做最省事。

## 何时用

- 界面要展示或触发一个后端还没提供的能力（新页面、新按钮、任务卡新字段）。
- 已有方法要加参数、改语义或改返回的展示模型。
- **不是这个场景**：纯排版/主题问题去看 [ui-conventions](../../instructions/ui-conventions.instructions.md)；纯表结构变更去看 [backend-layering](../../instructions/backend-layering.instructions.md)。

## 步骤

1. **先找现成的形状**：在 `tests/unit/test_ui_models.py`（展示模型）与 `tests/integration/test_gui_*.py`（界面路径）里搜同类动作，抄它们的数据形状与断言口径；文案键先在 `resources/i18n/zh-CN.json` 里 grep 一遍，能复用就别新造。
2. **协议**：在 `ui/backend.py` 的 `ArchiveService` 里加签名（只写类型，不写实现）。返回**展示模型**（`ui/models.py` 的 dataclass）或已本地化的文案，不要返回裸 dict、ORM 实体或异常。
3. **真实后端** `ui/sql_backend.py`：业务规则放 `application/` 的用例函数里，后端只做"取参 → 调用 → 映射视图模型"。写操作成功末尾 `self._touch()`（数据版本号 +1，界面据此重读）；审计走 `log_action(...)`，路径过 `redacted_path()`。
4. **演示后端** `ui/demo_backend.py`：内存实现同一语义。**不写用户磁盘、不假装成功**（写文件类动作回 `result.*_simulated` 文案），但内存状态要真的变 —— GUI 用例全靠它验证界面行为。
5. **界面接线**：回调里 `_submit(work, ok)` 跑后台、`_poll_messages` 回主线程；忙碌与可用性走现成的 `_update_actions()` / `_set_busy()`，**后台线程里不许碰 Tk**。
6. **i18n**：两份 JSON 同键；中文全角标点、末行不许只剩标点；`.py` 里不许出现全角标点。
7. **用例三条起步**：演示后端语义一条（`tests/unit/test_ui_demo.py`）、真后端语义一条（`tests/unit/test_sql_<主题>.py`）、界面路径一条（`tests/integration/test_gui_<主题>.py`；对话框替身用 `tests/button_support.py` 的 `_patch_dialogs`，等后台动作结束用同处的 `_drain`）。模块 `pytestmark` 要带齐 epic/feature/story/layer 四个标签 + **恰好一个**严重等级（缺了收集期就报错，守卫见 `tests/unit/test_test_config.py`）。
8. **咬一口**：把关键分支改坏一次（去掉 `_touch()`、让演示后端假装写了文件、把前置校验删掉），确认新用例真的红，再还原 —— `git diff --stat` 里不该留下那处改动。

## 自检

```shell
uv run mypy                                                       # 两个后端缺方法会在这里红(别用 cast 绕过)
uv run pytest tests/unit/test_ui_demo.py tests/unit/test_sql_<主题>.py -q
uv run pytest tests/integration/test_gui_<主题>.py -q
uv run python scripts/run_tests_local.py --coverage               # 收尾: 覆盖率硬门禁 100%
```

覆盖率亏了就按模块定位（别全量重跑）：

```shell
uv run python -m coverage run --rcfile=.coverage-platform.rc --data-file="$env:TEMP\.coverage-x" -m pytest tests/unit/test_sql_<主题>.py tests/integration/test_gui_<主题>.py -q
uv run python -m coverage report -m --rcfile=.coverage-platform.rc --data-file="$env:TEMP\.coverage-x" --include="*<模块>.py"
```

## 常见漏点

| 症状                                           | 多半是                                                       |
| ---------------------------------------------- | ------------------------------------------------------------ |
| 操作成功但界面不动                             | 真实后端忘了 `self._touch()`                                 |
| `mypy` 报 `Argument ... has incompatible type` | 只有一个后端实现了新方法                                     |
| 手点不对但 GUI 用例全绿                        | 演示后端"顺手简化"了语义（跳过了前置校验、假装写了文件）     |
| 切主题后新控件还是旧配色                       | 那个窗口的 `restyle()` / `apply_palette()` 清单没加它        |
| 收集期报 `UsageError`                          | `pytestmark` 缺 epic/feature/story/layer，或挂了两档严重等级 |
| 长文案被硬裁                                   | 只设了 `wraplength`，没配 `fit_text`                         |
