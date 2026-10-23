---
description: '改 domain/application/infrastructure/services 或两个 UI 后端时的分层红线: 谁能碰文件系统与 Tk、协议方法为什么必须两边都实现、迁移只许追加、SQL 与审计日志纪律, 以及"改这处必须同步改哪几处"。'
applyTo: src/archive_management/domain/**.py, src/archive_management/application/**.py, src/archive_management/infrastructure/**.py, src/archive_management/services/**.py, src/archive_management/ui/backend.py, src/archive_management/ui/sql_backend.py, src/archive_management/ui/demo_backend.py
---

# 后端分层: 谁能碰什么, 改一处要同步哪几处

一句话：**业务层不认识 Tk、不认识 HTTP、不认识用户磁盘上的任意路径**；需要这类能力就"参数注入 + 有默认实现"。分层图与各层职责见 [AGENTS.md](../../AGENTS.md) 第 3 节，命令行与打包的实现细节见 [docs/development.md](../../docs/development.md)。

## 1. 依赖方向与边界

`domain`（纯模型与规则）← `application`（用例编排，只依赖仓储接口与注入的能力）← `infrastructure`（SQLite 仓储/迁移）与 `services`（快照、调度、探测、回收站、路径判定、封面缓存、审计）。

- **需要新能力时写成参数注入**，默认值在**函数体内**解析：`trash: TrashBackend | None = None` → `backend = trash or send_to_trash`。默认参数在定义时绑定，`monkeypatch` 改模块属性对它无效（这一条踩过多次）。
- **`src/` 里不许有执行外部程序的入口**：`subprocess` / 外部命令行只能出现在 `scripts/`；`tests/security/test_side_effects.py` 会静态扫 token 并动态记账。
- **时间一律 UTC aware**：`from datetime import UTC, datetime` → `datetime.now(UTC)`（ruff 的 DTZ 规则会拦 naive）。

## 2. 协议方法: 两个后端都要实现

`ui/backend.py` 的 `ArchiveService` 是界面唯一的依赖面；真实实现是 `ui/sql_backend.py`，演示实现是 `ui/demo_backend.py`。

- **加一个方法 ⇒ 两个后端同时加**。漏一边时 `mypy` strict 会在"把那个后端传进 `ArchiveApp`"的地方报 `Argument ... has incompatible type` —— 那就是这条不变量的守卫，**别用 `cast` / `# type: ignore` 绕过**。
- **演示后端必须诚实**：不写用户磁盘、不假装成功（写文件类动作回 `result.*_simulated` 这类文案），但内存状态该改就改（GUI 用例靠它验证界面行为）。
- **真实后端改完要 `self._touch()`**（数据版本号 +1）：界面靠它决定"要不要重读"，漏了就是"操作成功但界面不动"。
- `game_id` 对外是 `str`、对内是 `int`：用现成的 `_game_ref()` / `_optional_game_id()` 收窄，别在每个方法里各写一遍转换。

完整的落地步骤（含 i18n、用例与覆盖率收尾）见 skill [archive-service-protocol-method](../skills/archive-service-protocol-method/SKILL.md)。

## 3. 数据层

- 迁移表在 [infrastructure/schema.py](../../src/archive_management/infrastructure/schema.py)：**只许追加 `(版本号, SQL)` 条目**，已经发布过的迁移一个字都不许改（用户的旧库就是按它跑过来的）。
- 一切读写走 `Database.session()`（退出即 commit，异常即 rollback）；仓储是**手写 SQL**，列名写**字面量**（`f-string` 拼列名会被 ruff S608 拦）。
- **新增/删除列要同步四处**：SELECT 列清单、`_row_to_*` 映射、INSERT、UPDATE。漏了 SELECT 会静默读到默认值 —— 这条被咬过多次，改完务必用真实库读一遍。

## 4. 错误与审计

- 只抛 `ArchiveManagementError` 家族（[exceptions.py](../../src/archive_management/exceptions.py)）：快照问题 `SnapshotError`、磁盘与包 `StorageError` / `PackageError`、会话被取消 `OperationCancelledError`、内容未变化 `ContentUnchangedError`。界面只 catch 基类，**别抛裸 `Exception`**。
- 用户操作统一 `log_action(action, *, basic=False, **fields)`：高风险 INFO、基础 DEBUG；路径字段先过 `redacted_path()`。**不落凭据、不落文件内容、不落完整敏感路径**。

## 5. 改完的自检（窄 → 宽）

```shell
uv run mypy                                                              # 协议/类型不变量
uv run pytest tests/unit/test_sql_<主题>.py tests/unit/test_<主题>.py -q   # 两个后端 + 纯逻辑
uv run radon cc -s --min C src                                           # 输出应为空
```

动过函数还要看 `xenon`（CI 的 analysis 作业才跑它，本地最容易漏），要全量就 `uv run python scripts/run_tests_local.py --coverage`。
