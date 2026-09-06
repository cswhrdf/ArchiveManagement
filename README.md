# ArchiveManagement

存档管理工具 —— 用于分支、备份、恢复和导出游戏存档的桌面工具.

## 开发

要求：Python >= 3.12 与 [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --locked          # 安装依赖（含 dev 组）并安装本包
uv run archive-management init --root .\dev-data   # 初始化目录/配置/数据库
uv run archive-management doctor --root .\dev-data # 健康检查
```

质量门禁（与 pre-commit 及 CI 一致）：

```powershell
uv run ruff check .
uv run black --check .
uv run mypy src
uv run pytest --cov
```
